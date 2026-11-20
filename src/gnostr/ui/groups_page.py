# src/gnostr/ui/groups_page.py
"""NIP-29 Communities UI (spec nipw-communities-v1 §5).

GroupsPage: the kind-10009 registry list (cached rows from the gateway,
refreshed by a snapshot REQ of the user's 10009) plus an add-group entry
accepting an naddr or "wss://relay group-id".

GroupDetailPage: preview-gated join flow. NIP-11 fetch -> relay-signed
39000/39001 (verified against the NIP-11 self key BEFORE anything renders)
-> preview card -> Join (9021) -> OK-ack -> membership probe. Private groups
hard-abort (v1 has no NIP-42). Timeline REQ (kinds 1/7/9, #h tag) runs ONLY
on the group relay. Posting is P3-IMPL-B scope; this page displays.

Mobile-first: single column, AdwClamp, pushed onto the shared
Adw.NavigationView like every other page (back contract on Phosh).
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango

from .. import groups

AdwClamp = Adw.Clamp


def _short_pk(pk):
    return f"{pk[:8]}…{pk[-4:]}" if pk and len(pk) > 16 else (pk or "")


class GroupsPage(Gtk.Box):
    """The Communities list: groups from the user's kind-10009 registry."""

    def __init__(self, main_window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.main_window = main_window
        self.client = main_window.client
        self._refs = {}  # (relay, group_id) -> GroupRef
        self._sub_id = groups.sub_id_for("glist", "self")
        self._handler = None

        bar = Gtk.Box(spacing=12, halign=Gtk.Align.START, margin_top=6,
                      margin_bottom=6, margin_start=6)
        btn_back = Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat"])
        btn_back.set_tooltip_text("Back")
        btn_back.connect("clicked", lambda b: main_window.content_nav.pop())
        bar.append(btn_back)
        btn_refresh = Gtk.Button(icon_name="view-refresh-symbolic", css_classes=["flat"])
        btn_refresh.set_tooltip_text("Reload groups")
        btn_refresh.connect("clicked", self._reload)
        bar.append(btn_refresh)
        self.append(bar)

        # Add-group entry: naddr or "wss://relay.example group-id"
        add_row = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_bottom=6)
        self.add_entry = Gtk.Entry()
        self.add_entry.set_hexpand(True)
        self.add_entry.set_placeholder_text("Add group — naddr… or wss://relay group-id")
        self.add_entry.connect("activate", self._on_add)
        add_row.append(self.add_entry)
        btn_add = Gtk.Button(label="Add")
        btn_add.connect("clicked", self._on_add)
        add_row.append(btn_add)
        self.append(add_row)

        clamp = AdwClamp()
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.connect("row-activated", self._on_row_activated)
        clamp.set_child(self.list_box)
        scroll.set_child(clamp)
        self.append(scroll)

        self._empty = Gtk.Label(label="No groups yet.\nAdd one by naddr above.")
        self._empty.set_justify(Gtk.Justification.CENTER)
        self._empty.add_css_class("dim-label")
        self._empty.set_wrap(True)
        self._empty.set_margin_top(48)
        self._empty.set_visible(False)
        self.append(self._empty)

        self._handler = self.client.connect("quote-event-received", self._on_event)
        self.connect("unrealize", self._on_unrealize)
        self._load_cache()
        self._reload()

    # -- data ---------------------------------------------------------------

    def _gateway(self):
        return getattr(self.client, "_groups_gateway", None)

    def _load_cache(self):
        gw = self._gateway()
        if gw is None:
            return
        try:
            for row in gw.list_groups():
                self._refs[(row["relay"], row["group_id"])] = groups.GroupRef(
                    row["group_id"], row["relay"], row.get("name")
                )
        except Exception:
            pass
        self._rebuild()

    def _reload(self, _btn=None):
        """Snapshot REQ for our own kind-10009 on the general read relays
        (the registry itself is NOT group traffic — only group content is)."""
        pk = self.client.my_pubkey
        if not pk:
            return
        self.client.request_once(self._sub_id, groups.groups_list_filter(pk))

    def _on_event(self, _client, event_json):
        try:
            import json

            ev = json.loads(event_json)
        except Exception:
            return
        if ev.get("kind") != groups.KIND_GROUPS_LIST:
            return
        if ev.get("pubkey") != self.client.my_pubkey:
            return
        refs = groups.parse_10009(ev)
        try:
            from gi.repository import Gio

            Gio.Settings.new("tech.livingonlinux.gnostr").set_int(
                "groups-last-sync", int(ev.get("created_at") or 0)
            )
        except Exception:
            pass
        gw = self._gateway()
        for ref in refs:
            self._refs[(ref.relay, ref.id)] = ref
            if gw is not None and ref.valid:
                try:
                    gw.upsert_group(ref.relay, ref.id, name=ref.name)
                except Exception:
                    pass
        self._rebuild()

    def _rebuild(self):
        row = self.list_box.get_first_child()
        while row is not None:
            nxt = row.get_next_sibling()
            self.list_box.remove(row)
            row = nxt
        for (relay, gid), ref in sorted(self._refs.items()):
            r = Adw.ActionRow(title=ref.display_name(), subtitle=relay)
            r.set_activatable(True)
            if not ref.valid:
                r.add_css_class("dim-label")
                r.set_tooltip_text(f"invalid relay hint: {ref.relay}")
            r._group_key = (relay, gid)
            r._group_name = ref.name
            self.list_box.append(r)
        self._empty.set_visible(not self._refs)

    # -- actions --------------------------------------------------------------

    def _on_row_activated(self, _lb, row):
        relay, gid = getattr(row, "_group_key", (None, None))
        if relay:
            self.main_window.show_group_detail(relay, gid, name=getattr(row, "_group_name", None))

    def _on_add(self, _w):
        text = self.add_entry.get_text().strip()
        if not text:
            return
        target = None
        if text.startswith("naddr"):
            d = groups.decode_naddr(text)
            if d and groups.validate_relay_url(d["relay"]):
                target = (d["relay"], d["identifier"], None)
        else:
            parsed = groups.parse_relay_group_input(text)
            if parsed:
                target = (parsed[0], parsed[1], None)
        if target is None:
            self.main_window.add_toast(Adw.Toast(title="Not a valid naddr or relay + group id"))
            return
        self.add_entry.set_text("")
        self.main_window.show_group_detail(target[0], target[1], invite=target[2])

    def _on_unrealize(self, *_):
        if self._handler is not None:
            try:
                self.client.disconnect(self._handler)
            except Exception:
                pass
            self._handler = None


class GroupDetailPage(Gtk.Box):
    """One community: verified preview card + join/leave + timeline."""

    def __init__(self, main_window, relay, group_id, name=None, invite=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.main_window = main_window
        self.client = main_window.client
        self.relay = relay
        self.group_id = group_id
        self.machine = groups.GroupJoinMachine(
            group_id, relay, client_pk(main_window), invite=invite
        )
        self._meta = None
        self._self_pk = None  # relay NIP-11 self key (39000 signer gate)
        self._seen_event_ids = set()
        self._sub_ids = []
        self._handlers = []

        bar = Gtk.Box(spacing=12, halign=Gtk.Align.START, margin_top=6,
                      margin_bottom=6, margin_start=6)
        btn_back = Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat"])
        btn_back.set_tooltip_text("Back")
        btn_back.connect("clicked", self._on_back)
        bar.append(btn_back)
        self.append(bar)

        clamp = AdwClamp()
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(6)
        clamp.set_child(outer)
        scroll.set_child(clamp)
        self.append(scroll)

        # --- preview card (only ever populated with VERIFIED metadata) ---
        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                            margin_start=12, margin_end=12)
        self.card.add_css_class("card")
        self.card.set_margin_bottom(6)
        self.title_lbl = Gtk.Label(label=name or group_id)
        self.title_lbl.add_css_class("title-2")
        self.title_lbl.set_wrap(True)
        self.title_lbl.set_xalign(0)
        self.card.append(self.title_lbl)
        self.relay_lbl = Gtk.Label(label=relay)
        self.relay_lbl.add_css_class("dim-label")
        self.relay_lbl.set_xalign(0)
        self.relay_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.card.append(self.relay_lbl)
        self.about_lbl = Gtk.Label(label="Loading group info…")
        self.about_lbl.set_wrap(True)
        self.about_lbl.set_xalign(0)
        self.card.append(self.about_lbl)
        self.flags_lbl = Gtk.Label(label="")
        self.flags_lbl.add_css_class("dim-label")
        self.flags_lbl.set_xalign(0)
        self.flags_lbl.set_visible(False)
        self.card.append(self.flags_lbl)
        action_row = Gtk.Box(spacing=12, margin_top=6)
        self.join_btn = Gtk.Button(label="Join group")
        self.join_btn.add_css_class("suggested-action")
        self.join_btn.set_sensitive(False)
        self.join_btn.connect("clicked", self._on_join_clicked)
        action_row.append(self.join_btn)
        self.leave_btn = Gtk.Button(label="Leave")
        self.leave_btn.add_css_class("destructive-action")
        self.leave_btn.set_visible(False)
        self.leave_btn.connect("clicked", self._on_leave_clicked)
        action_row.append(self.leave_btn)
        self.status_lbl = Gtk.Label(label="")
        self.status_lbl.set_wrap(True)
        self.status_lbl.add_css_class("dim-label")
        action_row.append(self.status_lbl)
        self.card.append(action_row)
        outer.append(self.card)

        # --- timeline ---
        self.timeline = Gtk.ListBox()
        self.timeline.set_selection_mode(Gtk.SelectionMode.NONE)
        outer.append(self.timeline)

        self._handlers.append(
            self.client.connect("group-event-received", self._on_group_event)
        )
        self._handlers.append(
            self.client.connect("publish-result", self._on_publish_result)
        )
        self.connect("unrealize", self._on_unrealize)

        self._start_resolve()

    # -- resolve + preview (spec §2: gate BEFORE any 9021) --------------------

    def _start_resolve(self):
        self.machine.begin()
        self._open_subs()
        import threading

        def _worker():
            doc = groups.fetch_nip11(self.relay)
            GLib.idle_add(self._on_nip11, doc)

        threading.Thread(target=_worker, daemon=True).start()

    def _open_subs(self):
        c = self.client
        gid = self.group_id
        # metadata (snapshot) + admins + members + live timeline — group
        # relay ONLY (spec §3: never general relays).
        subs = [
            (groups.sub_id_for("gmeta", gid), groups.metadata_filter(gid), True),
            (groups.sub_id_for("gmemset", gid), groups.members_filter(gid), True),
            (groups.sub_id_for("gtl", gid), groups.timeline_filter(gid), False),
        ]
        pk = client_pk(self.main_window)
        if pk:
            subs.append(
                (groups.sub_id_for("gmem", gid), groups.membership_filter(gid, pk), True)
            )
        for sub_id, filt, snap in subs:
            if c.subscribe_on_relay(self.relay, sub_id, filt, snapshot=snap):
                self._sub_ids.append(sub_id)

    def _on_nip11(self, doc):
        self._self_pk = groups.nip11_self_pk(doc)
        state = self.machine.on_nip11(doc)
        if state == "aborted":
            self._show_abort()
        return False

    def _on_group_event(self, _client, relay_url, event_json):
        import json

        if relay_url != self.relay:
            return
        try:
            ev = json.loads(event_json)
        except Exception:
            return
        kind = ev.get("kind")
        if kind == groups.KIND_GROUP_METADATA:
            self_pk = self._nip11_pk()
            meta = groups.parse_group_metadata(ev, self_pk, relay=self.relay)
            if meta is None:
                return  # failed the self-key gate: never rendered
            self._meta = meta
            state = self.machine.on_metadata(meta)
            self._render_preview()
            if state == "aborted":
                self._show_abort()
            gw = getattr(self.client, "_groups_gateway", None)
            if gw is not None:
                try:
                    gw.upsert_group(self.relay, self.group_id, **{
                        k: v for k, v in meta.to_dict().items()
                        if k in ("name", "picture", "about", "banner", "flags",
                                 "supported_kinds", "fetched_at")
                    })
                except Exception:
                    pass
        elif kind == groups.KIND_GROUP_ADMINS:
            admins = groups.parse_group_admins(ev, self._nip11_pk())
            if admins and self._meta is not None:
                self._meta.admins = admins
                self._render_preview()
        elif kind == groups.KIND_GROUP_MEMBERS:
            n = groups.count_members(ev)
            if self._meta is not None and n:
                self._meta.member_count = n
                self._render_preview()
            # 39002 set is the authoritative membership answer on khatru
            # relays (open joins emit no 9000/9001 audit events — live
            # probe 2026-11-20). The relay also PUSHES updated 39002s on the
            # same sub, so join/leave confirmations arrive here.
            pk = client_pk(self.main_window)
            is_in = groups.is_member_in_members_event(ev, pk) if pk else None
            if is_in is not None:
                self.machine.on_membership("member" if is_in else "removed")
                self._render_status()
        elif kind in groups.GROUP_TIMELINE_KINDS:
            self._append_timeline_event(ev)
        elif kind in (groups.KIND_GROUP_ADD_USER, groups.KIND_GROUP_REMOVE_USER):
            pk = client_pk(self.main_window)
            state = groups.latest_membership([ev], pk) if pk else "unknown"
            if state != "unknown":
                self.machine.on_membership(state)
                self._render_status()

    def _nip11_pk(self):
        # None until NIP-11 resolved — parse_group_metadata then returns None
        # and nothing unverified is ever rendered (preview gate).
        return self._self_pk

    # -- join / leave -----------------------------------------------------------

    def _on_join_clicked(self, _btn):
        if self.machine.closed_without_invite():
            self.main_window.add_toast(
                Adw.Toast(title="This group is closed — an invite link is required")
            )
            return
        pk = client_pk(self.main_window)
        if not pk:
            return
        ev = groups.build_join_event(pk, self.group_id, invite=self.machine.invite)
        # machine gate: only leaves 'preview' — unverified/private never gets here
        if self.machine.on_join(ev["id"]) == "preview":
            return
        if not self.client.dispatch_signed_to_relay(
            ev, self.relay, label="Group join"
        ):
            self.machine.rejection_reason = "signer unavailable"
            self._render_status()

    def _on_leave_clicked(self, _btn):
        pk = client_pk(self.main_window)
        if not pk:
            return
        ev = groups.build_leave_event(pk, self.group_id)
        self._leave_event_id = ev["id"]
        self.client.dispatch_signed_to_relay(ev, self.relay, label="Group leave")

    def _on_publish_result(self, _client, event_id, accepted, message, relay_url, label):
        if event_id == getattr(self, "_leave_event_id", None):
            if accepted:
                gw = getattr(self.client, "_groups_gateway", None)
                if gw is not None:
                    try:
                        gw.set_membership(self.relay, self.group_id, "left")
                    except Exception:
                        pass
                self.status_lbl.set_text("Left the group")
                self.leave_btn.set_visible(False)
                self._sync_registry(add=False)
            return
        if event_id != self.machine.join_event_id:
            return
        state = self.machine.on_join_ok(bool(accepted), message or "")
        if state in ("member", "already_member"):
            gw = getattr(self.client, "_groups_gateway", None)
            if gw is not None:
                try:
                    gw.set_membership(self.relay, self.group_id, "member")
                except Exception:
                    pass
            self._sync_registry(add=True)
        self._render_status()

    def _sync_registry(self, add=True):
        """Republish the replacement 10009 after a confirmed join/leave:
        every gateway row whose membership is 'member' (viewed-but-not-joined
        groups stay out)."""
        gw = getattr(self.client, "_groups_gateway", None)
        if gw is None:
            return
        try:
            refs = []
            for row in gw.list_groups():
                relay, gid = row["relay"], row["group_id"]
                if not groups.validate_relay_url(relay):
                    continue
                joined = gw.get_membership(relay, gid) == "member"
                if relay == self.relay and gid == self.group_id:
                    joined = add
                if joined:
                    refs.append(
                        groups.GroupRef(gid, relay, row.get("name") or None)
                    )
        except Exception:
            return
        self.client.publish_groups_list(refs)

    # -- rendering ---------------------------------------------------------------

    def _render_preview(self):
        m = self._meta
        if m is None:
            return
        self.title_lbl.set_text(m.name or self.group_id)
        self.about_lbl.set_text(m.about or "")
        chips = list(m.flags)
        if m.member_count:
            chips.append(f"{m.member_count} members")
        if m.admins:
            chips.append(f"{len(m.admins)} admin(s)")
        self.flags_lbl.set_text(" · ".join(chips))
        self.flags_lbl.set_visible(bool(chips))
        if self.machine.state == "preview":
            self.join_btn.set_sensitive(True)
        self._render_status()

    def _render_status(self):
        s = self.machine.state
        reasons = {
            "aborted": self.machine.abort_reason,
            "rejected": self.machine.rejection_reason,
        }
        text = {
            "idle": "", "resolving": "Checking relay…",
            "preview": "", "join_sent": "Join requested…",
            "member": "✓ Member", "pending": "Join pending approval",
            "already_member": "✓ Already a member",
            "rejected": f"Join rejected: {reasons.get('rejected') or ''}",
            "aborted": reasons.get("aborted") or "",
        }.get(s, "")
        self.status_lbl.set_text(text)
        joined = s in ("member", "already_member")
        self.join_btn.set_visible(not joined)
        self.leave_btn.set_visible(joined)
        self.join_btn.set_sensitive(s == "preview" and not self.machine.closed_without_invite())

    def _show_abort(self):
        self.about_lbl.set_text(self.machine.abort_reason or "Group unavailable")
        self.join_btn.set_sensitive(False)
        self._render_status()

    def _append_timeline_event(self, ev):
        eid = ev.get("id")
        if not eid or eid in self._seen_event_ids:
            return
        self._seen_event_ids.add(eid)
        kind = ev.get("kind")
        author = _short_pk(ev.get("pubkey", ""))
        if kind == 1:
            title, body = author, (ev.get("content") or "")[:500]
        elif kind == 7:
            title, body = author, "❤ reacted"
        else:
            title, body = author, "⚡ zapped"
        row = Adw.ActionRow(title=title, subtitle=body)
        row.get_subtitle_label().set_wrap(True)
        row.get_subtitle_label().set_lines(4)
        row.get_subtitle_label().set_ellipsize(Pango.EllipsizeMode.END)
        self.timeline.append(row)

    def _on_back(self, _btn):
        self.main_window.content_nav.pop()

    def _on_unrealize(self, *_):
        for h in self._handlers:
            try:
                self.client.disconnect(h)
            except Exception:
                pass
        self._handlers = []
        for sub_id in self._sub_ids:
            self.client.close_sub_on_relay(self.relay, sub_id)
        self._sub_ids = []


def client_pk(main_window):
    return getattr(main_window.client, "my_pubkey", None) or getattr(
        main_window, "pub_key", None
    )


# AdwClamp is spelled Adw.Clamp in the GI namespace; alias for readability.
AdwClamp = Adw.Clamp
