#!/usr/bin/env python3
"""Real-GTK4/Adw harness for the Communities pages (NIPW-LFUX-3).

Brief: nipw2026-11-longform-theming-v1 (gnostr-scopes). Asserts the brief's
composition contract on REAL widgets driven through REAL signals:

- GroupDetailPage header card: title-2 title, dim-label relay/status/flags,
  about at normal weight, .card box — the brief §2.1 header treatment.
- Verb semantics (§2.3): affirmative verbs are suggested-action (Join, Post,
  Publish[drafts, asserted in test_longform_gtk_real.py]), destructive verbs
  are destructive-action (Leave, Delete). Clicks run the real machine:
  preview -> join -> join_sent -> pending -> member (39002 push), with real
  publish-result and group-event-received emissions.
- Composer gating + apply posting unchanged (§2.4): post_row visible only for
  members; Adw.EntryRow 'apply' (Enter) posts a signed kind-1 with h-tag.
- §2.4.4 consistency pass: a group activity row and a longform comment row
  are the SAME recipe (vertical Box spacing 4, dim-label meta line with
  ' · ' grammar, normal-weight wrapped body at max-width-chars 80), proven
  by building both from the real code paths and comparing structurally.
- GroupsPage list: registry-cache rows are Adw.ActionRow (the house list
  idiom shared with the article list), invalid relay hints dim-label +
  tooltip, row activate navigates, add-entry handles valid + invalid input
  through real Entry.activate / toast.

No behavior contract is tested here — join/machine/watchdog semantics are
owned by test_group_join_race.py / test_group_hardening.py (tier-1). This
harness is composition + real-signal wiring only.

Runs standalone (NOT under the pytest suite: conftest mocks gi):
    xvfb-run -a /usr/bin/python3 tests/test_groups_gtk_real.py
Exit 0 all green, 1 failure, 77 GTK unavailable.
"""
import importlib.util
import json
import os
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src", "gnostr"))

criticals = []


def _log_handler(domain, level, message, user_data):
    criticals.append("%s: %s" % (domain, message.strip()))
    return False


try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, GObject, Gtk, Pango
except Exception as e:
    print("SKIP: real GTK4/Adw unavailable (%s)" % e)
    sys.exit(77)

GLib.log_set_handler(
    None,
    GLib.LogLevelFlags.LEVEL_ERROR | GLib.LogLevelFlags.LEVEL_CRITICAL,
    _log_handler,
    None,
)

# Stub the gnostr package so the REAL modules load without running
# gnostr/__init__ (which needs a compiled GResource). Same machinery as
# tests/test_group_timeline_gtk_real.py.
_gp = types.ModuleType("gnostr")
_gp.__path__ = [SRC]
sys.modules["gnostr"] = _gp
_ui = types.ModuleType("gnostr.ui")
_ui.__path__ = [os.path.join(SRC, "ui")]
sys.modules["gnostr.ui"] = _ui

import gnostr.groups as groups  # noqa: E402  (real logic layer, GTK-free)
import gnostr.nostr_utils as nostr_utils  # noqa: E402


def _load_ui(name):
    spec = importlib.util.spec_from_file_location(
        "gnostr.ui." + name, os.path.join(SRC, "ui", name + ".py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gnostr.ui." + name] = mod
    spec.loader.exec_module(mod)
    return mod


groups_page = _load_ui("groups_page")
articles_page = _load_ui("articles_page")

# No network: NIP-11 resolves instantly to the harness relay doc, so the
# page reaches 'preview' (the render state the header contract is asserted
# in) without a live relay.
RELAY = "wss://relay.invalid"
GID = "test-group"

RELAY_PRIV, RELAY_PK = nostr_utils.generate_keypair()
USER_PRIV, USER_PK = nostr_utils.generate_keypair()
AUTHOR_PRIV, AUTHOR_PK = nostr_utils.generate_keypair()
NIP11_DOC = {"name": "harness relay", "supported_nips": [29], "pubkey": RELAY_PK}
groups.fetch_nip11 = lambda relay_url, opener=None, timeout=10: dict(NIP11_DOC)


def _signed(ev, priv):
    return nostr_utils.sign_event(dict(ev), priv)


def _meta_event():
    return _signed(
        {
            "pubkey": RELAY_PK,
            "created_at": 1763000000,
            "kind": groups.KIND_GROUP_METADATA,
            "tags": [["d", GID], ["name", "Blossom Garden"],
                     ["about", "growing together"]],
            "content": "",
        },
        RELAY_PRIV,
    )


def _members_event(pks):
    return _signed(
        {
            "pubkey": RELAY_PK,
            "created_at": 1763000010,
            "kind": groups.KIND_GROUP_MEMBERS,
            "tags": [["d", GID]] + [["p", p] for p in pks],
            "content": "",
        },
        RELAY_PRIV,
    )


class FakeClient(GObject.Object):
    """Real GObject so connect()/emit() exercise real signal machinery."""

    __gsignals__ = {
        "group-event-received": (GObject.SignalFlags.RUN_LAST, None, (str, str)),
        "quote-event-received": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "publish-result": (
            GObject.SignalFlags.RUN_LAST, None, (str, bool, str, str, str),
        ),
        "relay-connected": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self, gateway=None):
        super().__init__()
        self.my_pubkey = USER_PK
        self.dispatched = []
        self.subs_opened = []
        self.requests = []
        self._groups_gateway = gateway

    def subscribe_on_relay(self, url, sub_id, filters, snapshot=False):
        self.subs_opened.append((sub_id, snapshot))
        return True  # live relay posture for this harness

    def request_once(self, sub_id, filters):
        self.requests.append(sub_id)
        return True

    def dispatch_signed_to_relay(self, event, url, label=""):
        self.dispatched.append(nostr_utils.sign_event(dict(event), USER_PRIV))
        return True

    def publish_groups_list(self, refs):
        # Registry sync (kind-10009 rewrite) — recorded, not exercised:
        # the 10009 logic is tier-1 territory (test_group_registry).
        self.dispatched.append({"kind": groups.KIND_GROUPS_LIST, "refs": refs})
        return True


class FakeGw:
    """Gateway double: only the list/cache surface GroupsPage touches."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.membership = {}

    def list_groups(self):
        return list(self.rows)

    def get_group(self, relay, gid):
        for r in self.rows:
            if r["relay"] == relay and r["group_id"] == gid:
                return r
        return None

    def get_membership(self, relay, gid):
        return self.membership.get((relay, gid))

    def upsert_group(self, relay, gid, **kw):
        pass

    def set_membership(self, relay, gid, state):
        self.membership[(relay, gid)] = state


def _mw(client, nav=None):
    calls = {"detail": [], "toasts": []}
    mw = types.SimpleNamespace(
        client=client,
        pub_key=USER_PK,
        content_nav=nav if nav is not None else Adw.NavigationView(),
        add_toast=lambda t: calls["toasts"].append(t.get_title()),
        show_group_detail=lambda relay, gid, name=None, invite=None:
            calls["detail"].append((relay, gid, name)),
    )
    mw._calls = calls
    return mw


def _pump(seconds=0.4):
    deadline = time.time() + seconds
    ctx = GLib.MainContext.default()
    while time.time() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.01)


def _detail_page(gw=None):
    client = FakeClient(gateway=gw)
    page = groups_page.GroupDetailPage(_mw(client), RELAY, GID)
    _pump()  # let the resolve worker hand the NIP-11 doc back
    return client, page


def _timeline_rows(page):
    rows = []
    child = page.timeline.get_first_child()
    while child is not None:
        rows.append(child)
        child = child.get_next_sibling()
    return rows


def _row_parts(row):
    box = row.get_child()
    assert isinstance(box, Gtk.Box), "timeline row content is not a Box"
    meta = box.get_first_child()
    body = meta.get_next_sibling()
    return box, meta, body


def _label_recipe(label, box):
    """Structural recipe of one feed row (the brief §1/§2.4.4 contract):
    the properties a row must carry, independent of surface."""
    return {
        "box": box.get_orientation() == Gtk.Orientation.VERTICAL,
        "spacing": box.get_spacing(),
        "meta_is_label": isinstance(meta := label, Gtk.Label),
        "meta_dim": "dim-label" in meta.get_css_classes(),
        "meta_xalign": meta.get_xalign(),
        "meta_grammar": " · " in meta.get_text(),
    }


def check_detail_header_card():
    """§2.1 header: title-2 title, dim relay/status/flags, normal about,
    inside a .card. Verified metadata renders title via the 39000 name."""
    client, page = _detail_page()
    client.emit("group-event-received", RELAY, json.dumps(_meta_event()))
    _pump(0.05)
    assert "card" in page.card.get_css_classes(), "header is not a .card box"
    assert "title-2" in page.title_lbl.get_css_classes(), (
        "group title must be title-2 (brief §2.1)"
    )
    assert page.title_lbl.get_text() == "Blossom Garden", (
        "verified 39000 name must drive the title, got %r"
        % page.title_lbl.get_text()
    )
    assert "dim-label" in page.relay_lbl.get_css_classes(), (
        "relay line must be dim (brief §2.1)"
    )
    assert page.relay_lbl.get_ellipsize() == Pango.EllipsizeMode.MIDDLE
    assert page.about_lbl.get_text() == "growing together"
    assert "dim-label" not in page.about_lbl.get_css_classes(), (
        "about text is content at normal weight, never dim-label"
    )
    assert "dim-label" in page.flags_lbl.get_css_classes(), (
        "flags chips line must be dim (brief §2.1)"
    )
    assert "dim-label" in page.status_lbl.get_css_classes(), (
        "status line must be dim (brief §2.1)"
    )
    # §2.3 verb semantics on the build itself:
    assert "suggested-action" in page.join_btn.get_css_classes()
    assert "destructive-action" not in page.join_btn.get_css_classes()
    assert "destructive-action" in page.leave_btn.get_css_classes()
    assert "suggested-action" not in page.leave_btn.get_css_classes()
    assert "suggested-action" in page.post_btn.get_css_classes()
    assert not page.leave_btn.get_visible(), "leave hidden before membership"
    assert not page.post_row.get_visible(), (
        "composer hidden before membership (unchanged gate)"
    )
    print("PASS: detail header card + verb semantics on real widgets")


def check_join_verb_to_membership():
    """Real Join click drives the real machine to member through real
    publish-result + 39002 push emissions; button visibility flips exactly
    as _render_status dictates (behavior unchanged, wiring proven)."""
    gw = FakeGw([])
    client, page = _detail_page(gw=gw)
    client.emit("group-event-received", RELAY, json.dumps(_meta_event()))
    _pump(0.05)
    assert page.join_btn.get_sensitive(), "open group must arm Join at preview"
    page.join_btn.emit("clicked")
    assert len(client.dispatched) == 1, "Join must dispatch one event"
    ev = client.dispatched[0]
    assert ev["kind"] == groups.KIND_GROUP_JOIN_REQUEST and ev.get("sig")
    assert [t for t in ev["tags"] if t[0] == "h"] == [["h", GID]]
    assert page.machine.state == "join_sent"
    client.emit("publish-result", ev["id"], True, "", RELAY, "")
    assert page.machine.state in ("pending", "member")
    assert "awaiting approval" in page.status_lbl.get_text() or \
        page.machine.state == "member", page.status_lbl.get_text()
    client.emit("group-event-received", RELAY, json.dumps(_members_event([USER_PK])))
    assert page.machine.state == "member", "39002 push must land membership"
    assert page.status_lbl.get_text() == "✓ Member"
    assert not page.join_btn.get_visible() and page.leave_btn.get_visible(), (
        "member state must swap Join for Leave"
    )
    assert page.post_row.get_visible(), "member unlocks the composer"
    assert gw.membership.get((RELAY, GID)) == "member", "persisted membership"
    print("PASS: Join click -> machine -> publish-result -> 39002 -> member")


def check_composer_apply_posts():
    """Enter/apply on the real Adw.EntryRow posts a signed kind-1 with the
    group h-tag (composition: EntryRow + suggested-action Post; wiring:
    real 'apply' signal, same handler as the Post button)."""
    gw = FakeGw([])
    client, page = _detail_page(gw=gw)
    client.emit("group-event-received", RELAY, json.dumps(_meta_event()))
    page.join_btn.emit("clicked")
    ev_id = client.dispatched[0]["id"]
    client.emit("publish-result", ev_id, True, "", RELAY, "")
    client.emit("group-event-received", RELAY, json.dumps(_members_event([USER_PK])))
    assert page.post_row.get_visible()
    assert isinstance(page.post_entry, Adw.EntryRow), (
        "composer entry must be the house Adw.EntryRow (brief §2.4)"
    )
    assert page.post_entry.get_title() == "Post to group…"
    page.post_entry.set_text("hello from the harness")
    page.post_entry.emit("apply")  # real Enter machinery, not a direct call
    post = [e for e in client.dispatched if e["kind"] == 1]
    assert len(post) == 1, "apply must dispatch exactly one kind-1"
    assert post[0].get("sig") and [t for t in post[0]["tags"] if t[0] == "h"] == \
        [["h", GID]]
    assert post[0]["content"] == "hello from the harness"
    # The relay echo lands the post in the activity list through the real
    # group-event-received path (no optimistic local append exists — the
    # live tail is the relay's own subscription echo, unchanged behavior).
    client.emit("group-event-received", RELAY, json.dumps(post[0]))
    rows = _timeline_rows(page)
    assert any(
        r is not None and _row_parts(r)[2] is not None
        and _row_parts(r)[2].get_text() == "hello from the harness"
        for r in rows
    ), "relay-echoed own post must appear in the activity list"
    print("PASS: composer apply posts signed kind-1 (EntryRow house shape)")


def check_activity_row_recipe_matches_comment_row():
    """§2.4.4 consistency pass, proven structurally: a group activity row
    (built by the real signal path) and a longform comment row (built by
    the real _render_comments) carry the SAME recipe — vertical Box
    spacing 4, dim-label meta line '{who} · {ago}' grammar, normal-weight
    wrapped body at max-width-chars 80. The surfaces may no longer drift
    back to two different row dialects."""
    client, page = _detail_page()
    post_ev = _signed(
        {
            "pubkey": AUTHOR_PK,
            "created_at": int(time.time()) - 600,
            "kind": 1,
            "tags": [["h", GID]],
            "content": "a group post body",
        },
        AUTHOR_PRIV,
    )
    client.emit("group-event-received", RELAY, json.dumps(post_ev))
    rows = _timeline_rows(page)
    assert len(rows) == 1
    g_box, g_meta, g_body = _row_parts(rows[0])
    g_recipe = _label_recipe(g_meta, g_box)

    # The reference comment row, built by the REAL reader renderer.
    shim = types.SimpleNamespace(
        comments_box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0),
        comments_count=Gtk.Label(),
        comments_empty=Gtk.Label(),
        db=types.SimpleNamespace(get_profile=lambda pk: None),
        _db_comment_events=[{
            "id": "c" * 64,
            "pubkey": AUTHOR_PK,
            "kind": 1111,
            "created_at": int(time.time()) - 600,
            "content": "a longform comment body",
            "tags": [["a", "wss://relay.invalid:30023~author~test"]],
        }],
        _merge_comments=articles_page.ArticleReaderPage._merge_comments,
        _binding_caption=lambda info: "",
    )
    articles_page.ArticleReaderPage._render_comments(shim)
    c_box = shim.comments_box.get_first_child()
    assert isinstance(c_box, Gtk.Box)
    c_meta = c_box.get_first_child()
    c_body = c_meta.get_next_sibling()
    c_recipe = _label_recipe(c_meta, c_box)

    assert g_recipe == c_recipe, (
        "row recipes drifted: group %r != comment %r" % (g_recipe, c_recipe)
    )
    assert g_recipe["spacing"] == 4 and c_recipe["spacing"] == 4

    assert g_body is not None and c_body is not None
    for b in (g_body, c_body):
        assert b.get_wrap(), "body must wrap"
        assert b.get_xalign() == 0, "body must be start-aligned"
        assert b.get_max_width_chars() == 80, "house body measure (80 chars)"
        assert "dim-label" not in b.get_css_classes(), (
            "body text is never dim-label (brief §1.1)"
        )
    assert g_meta.get_text().startswith("posted · ")
    assert "ago" in g_meta.get_text(), "meta line carries the _ago tail"
    # Agreed surface difference: activity bodies cap at 4 lines (list
    # context), reader comments render full text.
    assert g_body.get_lines() == 4 and g_body.get_ellipsize() == \
        Pango.EllipsizeMode.END
    assert c_body.get_lines() == -1, "comment bodies are not line-capped"
    print("PASS: activity row recipe == comment row recipe (consistency pass)")


def check_groups_page_list():
    """GroupsPage: registry-cache rows carry the house list idiom (ActionRow
    with relay subtitle — same dialect as the article list), invalid relay
    hints are dim-label + tooltip, real row activate navigates via
    show_group_detail, and the add-entry handles valid + invalid input via
    real Entry.activate (toast for garbage, navigation otherwise)."""
    gw = FakeGw([
        {"relay": RELAY, "group_id": GID, "name": "Blossom Garden"},
        {"relay": "not a relay", "group_id": "broken-group", "name": "Broken"},
    ])
    client = FakeClient(gateway=gw)
    page = groups_page.GroupsPage(_mw(client))
    assert client.requests == [groups.sub_id_for("glist", "self")], (
        "GroupsPage must snapshot-REQ the kind-10009 registry on build"
    )
    rows = []
    child = page.list_box.get_first_child()
    while child is not None:
        rows.append(child)
        child = child.get_next_sibling()
    assert len(rows) == 2, "expected 2 registry rows, got %d" % len(rows)
    assert all(isinstance(r, Adw.ActionRow) for r in rows), (
        "group list rows are the house ActionRow list idiom"
    )
    titles = {r.get_title(): r for r in rows}
    good, bad = titles["Blossom Garden"], titles["Broken"]
    assert good.get_subtitle() == RELAY, good.get_subtitle()
    assert "dim-label" not in good.get_css_classes(), (
        "valid group row is normal weight"
    )
    assert "dim-label" in bad.get_css_classes(), (
        "invalid relay hint must be dim (it is noise, not a group)"
    )
    assert (bad.get_tooltip_text() or "").startswith("invalid relay hint")
    assert not page._empty.get_visible()

    # Real signal machinery on the real ListBox (GtkListBoxRow.activate() is
    # gesture/input-only in GTK4 — it does not programmatically emit
    # row-activated; the house-real pattern is emitting the widget's own
    # signal, which GObject type-checks against the connected handler).
    page.list_box.emit("row-activated", good)
    detail = page.main_window._calls["detail"]
    assert detail == [(RELAY, GID, "Blossom Garden")], detail

    page.add_entry.set_text("naddr garbage input")
    page.add_entry.emit("activate")  # real Entry.activate signal
    toasts = page.main_window._calls["toasts"]
    assert len(toasts) == 1 and "valid" in toasts[0], toasts

    page.add_entry.set_text("%s new-group" % RELAY)
    page.add_entry.emit("activate")
    detail = page.main_window._calls["detail"]
    assert detail[-1][0] == RELAY and detail[-1][1] == "new-group", detail
    print("PASS: groups list idiom + activate navigation + add-entry input")


def main():
    checks = (
        check_detail_header_card,
        check_join_verb_to_membership,
        check_composer_apply_posts,
        check_activity_row_recipe_matches_comment_row,
        check_groups_page_list,
    )
    failed = 0
    for check in checks:
        try:
            check()
        except Exception as e:
            failed += 1
            print("FAIL: %s: %s" % (check.__name__, e))
    if criticals:
        failed += 1
        print("FAIL: %d GTK criticals: %s" % (len(criticals), criticals[:4]))
    if failed:
        print("%d/%d checks FAILED" % (failed, len(checks)))
        sys.exit(1)
    print("PASS: all %d groups real-GTK checks green" % len(checks))


if __name__ == "__main__":
    main()
