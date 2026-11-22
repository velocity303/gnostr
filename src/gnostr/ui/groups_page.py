# src/gnostr/ui/groups_page.py
"""NIP-29 Communities UI (spec nipw-communities-v1 §5).

GroupsPage: the kind-10009 registry list (cached rows from the gateway,
refreshed by a snapshot REQ of the user's 10009) plus an add-group entry
accepting an naddr or "wss://relay group-id".

GroupDetailPage: preview-gated join flow. NIP-11 fetch -> relay-signed
39000/39001 (verified against the NIP-11 self key BEFORE anything renders)
-> preview card -> Join (9021) -> OK-ack -> membership probe. Private groups
hard-abort (v1 has no NIP-42). Activity REQ (kinds 1/6/7/9/11/1111/9734,
#h tag) runs ONLY on the group relay. Posting is P3-IMPL-B scope; this page
displays activity newest-first under a "Recent activity" header.

Mobile-first: single column, AdwClamp, pushed onto the shared
Adw.NavigationView like every other page (back contract on Phosh).
"""

import json
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, GObject, Pango

from .. import groups, nostr_utils

AdwClamp = Adw.Clamp

# NIPW-P3-HARDEN: every async step gets a deadline — no silent stalls.
JOIN_CONFIRM_TIMEOUT_MS = 10_000  # OK/39002 confirmation window after publish
RESOLVE_META_TIMEOUT_MS = 6_000  # 39000 metadata window after NIP-11 ok


def _diag(step, result, relay="", ms=None):
    """Structured one-line diagnostic (device-log contract):
    [GROUP] step=<name> relay=<url> result=ok|fail [ms=<n>]
    so a stall on FLX1 pinpoints the exact step without a debugger."""
    ms_txt = f" ms={int(ms)}" if ms is not None else ""
    print(f"[GROUP] step={step} relay={relay} result={result}{ms_txt}", flush=True)


# sub_id prefix -> diagnostic step name (groups.sub_id_for prefixes)
_SUB_STEPS = {
    "gmeta": "sub_meta",
    "gmemset": "sub_members",
    "gtl": "sub_timeline",
    "gmem": "sub_membership",
}


def _short_pk(pk):
    return f"{pk[:8]}…{pk[-4:]}" if pk and len(pk) > 16 else (pk or "")


def _ago(ts):
    """Relative timestamp for activity rows (phone-friendly, no clock needed)."""
    d = max(0, int(time.time()) - int(ts))
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


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
        # metadata/admins/members events that arrived before the NIP-11 self
        # pubkey resolved (first-search race) — replayed by _on_nip11
        self._pre_nip11_events = []
        # NIPW-P3-HARDEN reliability state
        self._step_t0 = {}        # diag step -> monotonic open time (ms calc)
        self._step_logged = set()  # diag steps already reported ok
        self._pending_subs = []   # (sub_id, filt, snap) queued for relay-up
        self._resolve_deadline_id = None
        self._join_timeout_id = None
        self._leave_timeout_id = None
        # gmeta EOSE arrived while NIP-11 was still in flight — classify it
        # right after the NIP-11 replay instead of waiting for the deadline
        # (NIPW-P3-FIX-3, Bug A).
        self._meta_eose_seen = False

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
        # NIPW-P3-HARDEN: explicit error state gets a Retry button — a failed
        # resolve must never leave the page silently at 'resolving'.
        self.retry_btn = Gtk.Button(label="Retry")
        self.retry_btn.set_visible(False)
        self.retry_btn.set_halign(Gtk.Align.START)
        self.retry_btn.connect("clicked", self._on_retry_clicked)
        self.card.append(self.retry_btn)
        outer.append(self.card)

        # --- recent activity (NIPW-P3-ACTIVITY) ---
        activity_head = Gtk.Label(label="Recent activity", xalign=0)
        activity_head.add_css_class("dim-label")
        activity_head.set_margin_top(6)
        outer.append(activity_head)
        self.timeline = Gtk.ListBox()
        self.timeline.set_selection_mode(Gtk.SelectionMode.NONE)
        outer.append(self.timeline)

        # --- composer (NIPW-P3-IMPL-B; spec §3 "Posting": single entry + send
        # button pinned at the bottom) --- hidden unless the state machine
        # reports membership: khatru-family group relays reject kind-1s from
        # non-members, and a composer that can only produce rejections is a
        # lie on the screen.
        self._post_ids = set()
        post_row = Gtk.Box(spacing=8, margin_top=6)
        self.post_entry = Adw.EntryRow(title="Post to group…")
        self.post_entry.set_hexpand(True)
        self.post_btn = Gtk.Button(label="Post")
        self.post_btn.add_css_class("suggested-action")
        self.post_btn.set_valign(Gtk.Align.CENTER)
        self.post_btn.connect("clicked", self._on_post_clicked)
        # Enter (or the row's apply checkmark) posts too — 'apply' is the
        # Adw.EntryRow signal for both, present since EntryRow's introduction.
        self.post_entry.connect("apply", lambda *_: self._on_post_clicked(None))
        post_row.append(self.post_entry)
        post_row.append(self.post_btn)
        post_row.set_visible(False)
        self.post_row = post_row
        outer.append(post_row)

        self._handlers.append(
            self.client.connect("group-event-received", self._on_group_event)
        )
        self._handlers.append(
            self.client.connect("publish-result", self._on_publish_result)
        )
        self._handlers.append(
            self.client.connect("relay-connected", self._on_relay_connected)
        )
        # NIPW-P3-FIX-3 (Bug A): 'no events before EOSE' on the metadata
        # snapshot is a terminal, classifiable fact — the page must learn it
        # from the signal, not from the dead-sub deadline (which used to end
        # in resolve_failed + re-REQ loops on revisits). signal_query guard:
        # test stubs may not declare the signal.
        if GObject.signal_query("sub-eose", type(self.client)):
            self._handlers.append(
                self.client.connect("sub-eose", self._on_sub_eose)
            )
        self.connect("unrealize", self._on_unrealize)

        self._start_resolve()

    # -- resolve + preview (spec §2: gate BEFORE any 9021) --------------------

    def _start_resolve(self):
        self.machine.begin()
        self._seed_cached_meta()  # NIPW-P3-FIX-3 (Bug A): render the cached
        # row immediately; the live 39000 REQ continues as a background
        # refresh and can only improve what's already shown.
        self._open_subs()
        import threading

        def _worker():
            # fetch_nip11 now retries internally (timeout + 2 retries) and
            # serves the per-relay TTL cache — re-entry is instant.
            t0 = time.monotonic()
            doc = groups.fetch_nip11(self.relay)
            ms = int((time.monotonic() - t0) * 1000)
            GLib.idle_add(self._on_nip11, doc, ms)

        threading.Thread(target=_worker, daemon=True).start()

    def _open_subs(self):
        gid = self.group_id
        # metadata (snapshot) + admins + members + live timeline — group
        # relay ONLY (spec §3: never general relays).
        subs = [
            (groups.sub_id_for("gmeta", gid), groups.metadata_filter(gid), True),
            # 39002/9000/9001 must stay LIVE subs: the relay pushes updated
            # 39002s on the open sub, and that push is the join/leave
            # confirmation. As snapshots they closed at EOSE and the
            # confirmation silently never arrived (FLX1 inconsistency).
            (groups.sub_id_for("gmemset", gid), groups.members_filter(gid), False),
            (groups.sub_id_for("gtl", gid), groups.timeline_filter(gid), False),
        ]
        pk = client_pk(self.main_window)
        if pk:
            subs.append(
                (groups.sub_id_for("gmem", gid), groups.membership_filter(gid, pk), False)
            )
        for sub_id, filt, snap in subs:
            self._open_sub(sub_id, filt, snap)

    def _open_sub(self, sub_id, filt, snap):
        step = _SUB_STEPS.get(sub_id.split("_", 1)[0], "sub")
        if self.client.subscribe_on_relay(self.relay, sub_id, filt, snapshot=snap):
            if sub_id not in self._sub_ids:
                self._sub_ids.append(sub_id)
            self._step_t0[step] = time.monotonic()
        else:
            # relay object could not be created (bad scheme / onion inert):
            # queue and retry on relay-connected — never silently skip.
            if (sub_id, filt, snap) not in self._pending_subs:
                self._pending_subs.append((sub_id, filt, snap))
            _diag(step, "fail", self.relay)

    def _on_relay_connected(self, _client, url):
        if url != self.relay or not self._pending_subs:
            return
        pending, self._pending_subs = self._pending_subs, []
        for sub_id, filt, snap in pending:
            self._open_sub(sub_id, filt, snap)

    # -- revisit cache + EOSE classification (NIPW-P3-FIX-3, Bug A) -----------

    def _gw(self):
        """Gateway accessor: real clients set _groups_gateway (via
        get_groups_gateway()); test doubles set the attribute directly."""
        return getattr(self.client, "_groups_gateway", None)

    def _persist_membership(self, state):
        gw = self._gw()
        if gw is None:
            return
        try:
            gw.set_membership(self.relay, self.group_id, state)
        except Exception:
            pass

    def _seed_cached_meta(self):
        """Revisit must NEVER wait on the live 39000 snapshot: the gateway
        row was upserted from VERIFIED metadata on an earlier visit, so
        render it on entry. The live sub_meta REQ continues as a background
        refresh — its result (event, empty EOSE, or deadline) can update the
        row but never stalls the page. Membership seeds too: 'member' stays
        member, a persisted join-request stays 'requested', never preview."""
        gw = self._gw()
        if gw is None:
            return
        try:
            row = gw.get_group(self.relay, self.group_id)
            mem = gw.get_membership(self.relay, self.group_id)
        except Exception:
            return
        if row and (row.get("name") or row.get("about") or row.get("picture")):
            meta = groups.GroupMeta(
                self.relay,
                self.group_id,
                name=row.get("name"),
                picture=row.get("picture"),
                banner=row.get("banner"),
                about=row.get("about"),
                flags=row.get("flags") or [],
                supported_kinds=row.get("supported_kinds"),
                admins=row.get("admins") or [],
                member_count=row.get("member_count"),
                fetched_at=row.get("fetched_at"),
            )
            if self.machine.state == "resolving":
                if self.machine.on_metadata(meta) == "aborted":
                    self._show_abort()  # cached private group: gate still applies
                    return
            else:
                self.machine.meta = meta  # keeps the closed/invite gate honest
            self._meta = meta
            self._render_preview()
            _diag("cached_meta", "ok", self.relay)
        if mem == "member":
            self.machine.on_membership("member")
            self._render_status()
        elif mem == "pending":
            self.machine.on_membership("pending")  # requested stays requested
            self._render_status()

    def _on_sub_eose(self, _client, sub_id, relay_url):
        if relay_url != self.relay or sub_id not in self._sub_ids:
            return
        if sub_id != groups.sub_id_for("gmeta", self.group_id):
            return
        if "sub_meta" in self._step_logged:
            return  # metadata landed before EOSE: nothing to settle
        if self._self_pk is None:
            # NIP-11 still in flight (replay hasn't run): classify after it does.
            self._meta_eose_seen = True
            return
        self._classify_meta_eose()

    def _classify_meta_eose(self):
        """EOSE with no 39000 is 'empty but fine' from the relay's side, not
        a dead sub: settle it ONCE. Cached render → background refresh simply
        found nothing (no failure shown, never any re-REQ — snapshot subs are
        excluded from the CLOSED sweep in handle_sub_closed). No cache → one
        explicit retryable failure behind the Retry button."""
        self._meta_eose_seen = False
        if self._resolve_deadline_id is not None:
            GLib.source_remove(self._resolve_deadline_id)
            self._resolve_deadline_id = None
        if self._meta is not None or self.machine.state != "resolving":
            _diag("sub_meta", "empty", self.relay)
            return
        self.machine.on_resolve_failed("relay returned no group metadata")
        _diag("sub_meta", "fail", self.relay)
        self._show_resolve_failed()

    def _on_nip11(self, doc, ms=None):
        _diag("nip11", "ok" if doc is not None else "fail", self.relay, ms)
        self._self_pk = groups.nip11_self_pk(doc)
        state = self.machine.on_nip11(doc)
        if state == "aborted":
            self._show_abort()
            return False
        if state == "resolve_failed":
            self._show_resolve_failed()
            return False
        # NIP-11 answered but the 39000 snapshot may still never arrive (sub
        # refused, slow relay): arm a deadline so 'resolving' always ends in
        # either preview or an explicit retryable error.
        self._arm_resolve_deadline()
        # FLX1 first-search race: snapshot metadata/admins/members events can
        # land BEFORE this NIP-11 fetch resolves; they failed the self-key gate
        # and were dropped, so the first search half-rendered (timeline only)
        # while the cached second search worked. Replay the buffered events so
        # first search == second search.
        pending, self._pre_nip11_events = self._pre_nip11_events, []
        for ev in pending:
            self._process_group_event(ev)
        if getattr(self, "_meta_eose_seen", False) and self._self_pk is not None:
            # gmeta EOSE arrived while NIP-11 was in flight; the replay above
            # just ran — classify the (still) empty snapshot now.
            self._classify_meta_eose()
        return False

    def _arm_resolve_deadline(self):
        if self._resolve_deadline_id is not None:
            return
        self._resolve_deadline_id = GLib.timeout_add(
            RESOLVE_META_TIMEOUT_MS, self._on_resolve_deadline
        )

    def _on_resolve_deadline(self):
        self._resolve_deadline_id = None
        if self.machine.state == "resolving":
            self.machine.on_resolve_failed("group metadata not found on relay")
            _diag("sub_meta", "fail", self.relay)
            self._show_resolve_failed()
        return False

    def _show_resolve_failed(self):
        reason = getattr(self.machine, "resolve_reason", None) or "Group unavailable"
        self.about_lbl.set_text(f"{reason} — tap Retry.")
        self.join_btn.set_sensitive(False)
        self.retry_btn.set_visible(True)
        self._render_status()

    def _on_retry_clicked(self, _btn):
        self.retry_btn.set_visible(False)
        self.about_lbl.set_text("Loading group info…")
        # resolve_failed -> resolving (begin() alone only leaves 'idle'), then
        # re-open every sub and re-fetch NIP-11 (relay-layer dedup absorbs
        # no-op repeats; the NIP-11 TTL cache makes the refetch instant when
        # it already worked).
        self.machine.retry_resolve()
        self._start_resolve()

    def _on_group_event(self, _client, relay_url, event_json):
        import json

        if relay_url != self.relay:
            return
        try:
            ev = json.loads(event_json)
        except Exception:
            return
        self._process_group_event(ev)

    def _process_group_event(self, ev):
        kind = ev.get("kind")
        if (
            kind
            in (
                groups.KIND_GROUP_METADATA,
                groups.KIND_GROUP_ADMINS,
                groups.KIND_GROUP_MEMBERS,
            )
            and self._nip11_pk() is None
        ):
            # NIP-11 has not resolved yet — the self-key gate can verify
            # nothing, and dropping here is what made the first search
            # half-render (FLX1 device test). Buffer (bounded) and let
            # _on_nip11 replay once the relay self pubkey lands.
            if len(self._pre_nip11_events) < 64:
                self._pre_nip11_events.append(ev)
            return
        if kind == groups.KIND_GROUP_METADATA:
            self_pk = self._nip11_pk()
            meta = groups.parse_group_metadata(ev, self_pk, relay=self.relay)
            if meta is None:
                return  # failed the self-key gate: never rendered
            self._mark_step_ok("sub_meta")
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
                self._mark_step_ok("sub_members")
                self._meta.admins = admins
                self._render_preview()
        elif kind == groups.KIND_GROUP_MEMBERS:
            n = groups.count_members(ev)
            if self._meta is not None and n:
                self._mark_step_ok("sub_members")
                self._meta.member_count = n
                self._render_preview()
            # 39002 set is the authoritative membership answer on khatru
            # relays (open joins emit no 9000/9001 audit events — live
            # probe 2026-11-20). The relay also PUSHES updated 39002s on the
            # same sub, so join/leave confirmations arrive here.
            pk = client_pk(self.main_window)
            is_in = groups.is_member_in_members_event(ev, pk) if pk else None
            if is_in is not None:
                self._cancel_join_timeout()
                old = self.machine.state
                if is_in:
                    state = self.machine.on_membership("member")
                    _diag("member_confirm", "ok", self.relay)
                    if state != old and state == "member":
                        self._persist_membership("member")
                        self._sync_registry(add=True)
                else:
                    # NIPW-P3-FIX-3 (Bug C): absence from the 39002 set is
                    # NOT 'removed'. On an approval-gated group it IS the
                    # honest 'requested — awaiting approval' state (FLX1:
                    # blossom join_ok ok but status stayed ambiguous). Only
                    # the 9000/9001 audit branch below reports removal.
                    self.machine.on_membership("unknown")
                    if self.machine.state in ("join_sent", "pending"):
                        _diag("member_pending", "ok", self.relay)
                        self._persist_membership("pending")
                self._render_status()
        elif kind in groups.GROUP_TIMELINE_KINDS:
            self._mark_step_ok("sub_timeline")
            self._append_timeline_event(ev)
        elif kind in (groups.KIND_GROUP_ADD_USER, groups.KIND_GROUP_REMOVE_USER):
            pk = client_pk(self.main_window)
            state = groups.latest_membership([ev], pk) if pk else "unknown"
            if state != "unknown":
                self._cancel_join_timeout()
                old = self.machine.state
                new = self.machine.on_membership(state)
                if state == "member":
                    _diag("member_confirm", "ok", self.relay)
                    if new == "member" and new != old:
                        self._persist_membership("member")
                        self._sync_registry(add=True)
                else:
                    # 9000 audit naming us: a real removal (the only path
                    # that says 'removed' — see the 39002 branch above).
                    _diag("member_confirm", "fail", self.relay)
                    if old in ("member", "join_sent", "pending"):
                        self._persist_membership("left")
                        self._sync_registry(add=False)
                self._render_status()

    def _nip11_pk(self):
        # None until NIP-11 resolved — parse_group_metadata then returns None
        # and nothing unverified is ever rendered (preview gate).
        return self._self_pk

    # -- structured diagnostics + confirm timers ---------------------------------

    def _mark_step_ok(self, step):
        """First-event arrival for a sub: log step=ok with latency once. The
        ms figure is REQ-open -> first event, the number that distinguishes
        'relay is slow' from 'relay is dead' in device logs."""
        if step in self._step_logged:
            return
        self._step_logged.add(step)
        t0 = self._step_t0.get(step)
        ms = int((time.monotonic() - t0) * 1000) if t0 else None
        _diag(step, "ok", self.relay, ms)
        if step == "sub_meta" and self._resolve_deadline_id is not None:
            GLib.source_remove(self._resolve_deadline_id)
            self._resolve_deadline_id = None

    def _arm_join_timeout(self):
        self._cancel_join_timeout()
        self._join_timeout_id = GLib.timeout_add(
            JOIN_CONFIRM_TIMEOUT_MS, self._on_join_timeout
        )

    def _cancel_join_timeout(self):
        if self._join_timeout_id is not None:
            GLib.source_remove(self._join_timeout_id)
            self._join_timeout_id = None

    def _cancel_leave_timeout(self):
        if getattr(self, "_leave_timeout_id", None) is not None:
            GLib.source_remove(self._leave_timeout_id)
            self._leave_timeout_id = None

    def _on_join_timeout(self):
        self._join_timeout_id = None
        if self.machine.state != "join_sent":
            return False  # membership event or OK already settled it
        self.machine.on_join_timeout()
        _diag("join_ok", "fail", self.relay)
        self._render_status()
        self.main_window.add_toast(
            Adw.Toast(title="Join not confirmed by relay — try again")
        )
        return False

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
        # build_join_event returns an UNSIGNED template (no id) — signing
        # happens downstream in dispatch_signed_to_relay. The event id is the
        # SHA-256 of the canonical serialization [0,pubkey,created_at,kind,
        # tags,content] — sig-independent — so the template's computed id is
        # byte-identical to the signed event's id the OK-ack will match on.
        # (FLX1 device test: reading ev["id"] here raised KeyError.)
        join_id = nostr_utils.compute_event_id(ev)
        # machine gate: only leaves 'preview' — unverified/private never gets here
        if self.machine.on_join(join_id) == "preview":
            return
        if not self.client.dispatch_signed_to_relay(
            ev, self.relay, label="Group join"
        ):
            self.machine.rejection_reason = "signer unavailable"
            self._render_status()
            _diag("join_pub", "fail", self.relay)
            return
        _diag("join_pub", "ok", self.relay)
        self._render_status()  # immediate feedback: 'Join requested…'
        self._arm_join_timeout()

    def _on_leave_clicked(self, _btn):
        pk = client_pk(self.main_window)
        if not pk:
            return
        ev = groups.build_leave_event(pk, self.group_id)
        # unsigned template again — record the id the signed event will carry
        # (publish-result matches on it); see _on_join_clicked.
        self._leave_event_id = nostr_utils.compute_event_id(ev)
        if not self.client.dispatch_signed_to_relay(
            ev, self.relay, label="Group leave"
        ):
            self._leave_event_id = None
            self.main_window.add_toast(Adw.Toast(title="Could not send leave (signer)"))
            _diag("leave_pub", "fail", self.relay)
            return
        _diag("leave_pub", "ok", self.relay)
        # NIPW-P3-HARDEN: a leave with no OK ack must fail loudly, not sit
        # showing 'Left' optimistically while the relay ignored us.
        self._cancel_leave_timeout()
        self._leave_timeout_id = GLib.timeout_add(
            JOIN_CONFIRM_TIMEOUT_MS, self._on_leave_timeout
        )

    def _on_leave_timeout(self):
        self._leave_timeout_id = None
        if getattr(self, "_leave_event_id", None) is None:
            return False  # already confirmed by publish-result
        self._leave_event_id = None
        _diag("leave_ok", "fail", self.relay)
        self.main_window.add_toast(
            Adw.Toast(title="Leave not confirmed by relay — try again")
        )
        return False

    def _on_post_clicked(self, _btn):
        """Post to the group (NIPW-P3-IMPL-B): kind 1 + h tag + best-effort
        previous tags, group relay ONLY (spec §3 routing contract).

        No optimistic append: the page's live gtl sub hears the relay's echo
        on the same connection and _append_activity dedupes by id — what
        renders is the post the relay actually accepted, not the one we
        assumed it would.
        """
        text = (self.post_entry.get_text() or "").strip()
        if not text:
            return
        pk = client_pk(self.main_window)
        if not pk:
            return
        gw = getattr(self.client, "_groups_gateway", None)
        stored = []
        if gw is not None:
            try:
                stored = gw.get_events(self.relay, self.group_id) or []
            except Exception:
                stored = []
        ev = groups.build_group_post_event(
            pk, self.group_id, text, groups.previous_tags(stored, self_pk=pk)
        )
        # the unsigned template's id == the signed event's id (sig-independent
        # serialization — see _on_join_clicked); the OK-ack matches on it.
        self._post_ids.add(nostr_utils.compute_event_id(ev))
        if not self.client.dispatch_signed_to_relay(
            ev, self.relay, label="Group post"
        ):
            self._post_ids.clear()
            self.main_window.add_toast(Adw.Toast(title="Could not post (signer)"))
            _diag("post_pub", "fail", self.relay)
            return
        _diag("post_pub", "ok", self.relay)
        self.post_entry.set_text("")

    def _on_publish_result(self, _client, event_id, accepted, message, relay_url, label):
        if event_id == getattr(self, "_leave_event_id", None):
            self._leave_event_id = None
            self._cancel_leave_timeout()
            if accepted:
                _diag("leave_ok", "ok", self.relay)
                gw = getattr(self.client, "_groups_gateway", None)
                if gw is not None:
                    try:
                        gw.set_membership(self.relay, self.group_id, "left")
                    except Exception:
                        pass
                self.status_lbl.set_text("Left the group")
                self.leave_btn.set_visible(False)
                self._sync_registry(add=False)
            else:
                # relay refused the leave: say so — never silently keep the
                # member UI while the relay still lists us.
                _diag("leave_ok", "fail", self.relay)
                self.main_window.add_toast(
                    Adw.Toast(title=f"Leave rejected: {message or 'relay refused'}")
                )
            return
        if event_id in self._post_ids:
            self._post_ids.discard(event_id)
            if accepted:
                # the relay's echo arrives via the live gtl sub and renders
                # as an activity row — log the ack, nothing else to do.
                _diag("post_ok", "ok", relay_url)
            else:
                _diag("post_ok", "fail", relay_url)
                self.main_window.add_toast(
                    Adw.Toast(title=f"Post rejected: {message or 'relay refused'}")
                )
            return
        if event_id != self.machine.join_event_id:
            return
        self._cancel_join_timeout()
        state = self.machine.on_join_ok(bool(accepted), message or "")
        _diag("join_ok", "ok" if accepted else "fail", self.relay)
        if state == "pending":
            # NIPW-P3-FIX-3 (Bug C): relay ACCEPTED the 9021 — membership is
            # still an open question (blossom waits on an admin). Say so in
            # the log AND persist it, so a revisit shows 'requested' rather
            # than falling back to preview.
            _diag("member_pending", "ok", self.relay)
            self._persist_membership("pending")
        if state == "rejected":
            self.main_window.add_toast(
                Adw.Toast(title=f"Join rejected: {self.machine.rejection_reason}")
            )
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
            "resolve_failed": getattr(self.machine, "resolve_reason", None),
        }
        text = {
            "idle": "", "resolving": "Checking relay…",
            "preview": "", "join_sent": "Join requested…",
            "member": "✓ Member",
            # NIPW-P3-FIX-3 (Bug C): accepted != confirmed. The pending text
            # must say REQUESTED, not pretend membership exists.
            "pending": "Join requested — awaiting approval",
            "already_member": "✓ Already a member",
            "rejected": f"Join rejected: {reasons.get('rejected') or ''}",
            "aborted": reasons.get("aborted") or "",
            "resolve_failed": reasons.get("resolve_failed") or "Group unavailable",
        }.get(s, "")
        self.status_lbl.set_text(text)
        joined = s in ("member", "already_member")
        self.join_btn.set_visible(not joined)
        self.leave_btn.set_visible(joined)
        # NIPW-P3-IMPL-B: posting is a member action (see composer comment).
        self.post_row.set_visible(joined)
        self.join_btn.set_sensitive(s == "preview" and not self.machine.closed_without_invite())
        # NIPW-P3-HARDEN: the Retry button belongs to the retryable failure
        # state only — hidden in every other state.
        self.retry_btn.set_visible(s == "resolve_failed")

    def _show_abort(self):
        self.about_lbl.set_text(self.machine.abort_reason or "Group unavailable")
        self.join_btn.set_sensitive(False)
        self._render_status()

    def _activity_row_content(self, ev):
        """(verb, body) per timeline kind — a glance must say WHAT happened.
        Live audit 2026-11-22: group pages looked EMPTY because the groups
        under test post NO kind-1 notes (blossom = kind-9 Blossom file
        events; wisp = kind-11 videos + kind-1111 comments)."""
        kind = ev.get("kind")
        content = ev.get("content") or ""
        if kind == 1:
            return "posted", content[:500]
        if kind == 6:
            return "🔁 reposted", content[:500]
        if kind == 7:
            c = content.strip()
            verb = {"+": "❤ reacted", "-": "👎 disliked"}.get(c)
            if verb is None:
                verb = f"reacted “{c}”" if c else "reacted"
            return verb, ""
        if kind == 9:
            # Blossom file announcement: content = {"file": {"name": ...}, ...}
            name = ""
            try:
                blob = json.loads(content)
                name = str((blob.get("file") or {}).get("name") or "")
            except (ValueError, AttributeError):
                pass
            return "📁 shared a file", name[:500]
        if kind == 11:
            title = next(
                (t[1] for t in ev.get("tags") or []
                 if len(t) >= 2 and t[0] == "title"),
                "",
            )
            return "🎥 shared a video", (title or content)[:500]
        if kind == 1111:
            return "💬 commented", content[:500]
        if kind == 9734:
            # Zap request: content is a BOLT11 invoice — never echo it,
            # just the act (receipts 9735 live on the payee's outbox, not
            # the group relay).
            return "⚡ zapped", ""
        return "activity", content[:500]

    def _append_timeline_event(self, ev):
        eid = ev.get("id")
        if not eid or eid in self._seen_event_ids:
            return
        self._seen_event_ids.add(eid)
        verb, body = self._activity_row_content(ev)
        author = _short_pk(ev.get("pubkey", ""))
        ts = ev.get("created_at")
        title = f"{verb} · {author}" + (f" · {_ago(ts)}" if ts else "")
        row = Adw.ActionRow(title=title, subtitle=body)
        # Adw.ActionRow has NO get_subtitle_label() accessor — the old
        # subtitle-label calls raised AttributeError per event (FLX1 device
        # test) BEFORE the append, so the timeline stayed empty.
        # set_subtitle_lines caps the (wrapping-by-default) subtitle at 4
        # lines with ellipsis. Guarded by tests/test_group_timeline_gtk_real.py.
        row.set_subtitle_lines(4)
        # Newest first: "recent activity" is not recent if 100 stale rows
        # must be scrolled past on a phone to reach the live tail.
        self.timeline.insert(row, 0)

    def _on_back(self, _btn):
        self.main_window.content_nav.pop()

    def _on_unrealize(self, *_):
        for timer in ("_resolve_deadline_id", "_join_timeout_id", "_leave_timeout_id"):
            tid = getattr(self, timer, None)
            if tid is not None:
                GLib.source_remove(tid)
                setattr(self, timer, None)
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
