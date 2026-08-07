# Gnostr Social Actions (Like / Repost / Reply) Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Let the user like (kind-7 reaction), repost (kind-6), and reply (kind-1 with threading tags) any post they see, publishing standards-compliant Nostr events that other clients understand.

**Architecture:** Add three publish methods to `NostrClient` (reusing the existing `sign_event` + `publish` + `publish_post` pattern), a shared `_build_and_publish(kind, content, tags)` helper to avoid triplicating the build/sign/publish boilerplate (DRY), and turn the PostWidget footer metric labels into interactive buttons wired to those methods. The **read side already exists** — `client._handle_event` already counts kind 7/6/1 events into `self.metrics` and emits `metrics-updated`, and `main.py` already updates `lbl_likes/lbl_reposts/lbl_replies`. This plan only adds the write path plus a small DB query for "did I already like this?"

**Tech Stack:** Python 3.11, GTK4/libadwaita, existing `nostr_utils.sign_event` / `client.publish` / `dialogs.ComposeWindow`.

---

## Nostr Standards (the "why" behind the tags)

- **Reaction (NIP-25):** kind `7`. Content = a single char/emoji (`+` to add, `-` to remove/undo). Tags MUST be `[["e", <target event id>], ["p", <target author pubkey>]]`.
- **Repost (NIP-18):** kind `6`, *generic* form — `content` = the raw JSON serialization of the original event; tags MUST be `[["k", "<orig kind>"], ["e", <orig event id>], ["p", <orig author pubkey>]]`. (The `k` tag lets relays/clients know what kind was reposted.)
- **Reply (NIP-01):** kind `1` with `e` + `p` tags. First `e` tag = thread *root*; last `e` tag = *direct parent* (this app's `_direct_parent` already resolves the last `e` tag, so replies must be `[["e", root_id], ["e", direct_parent_id], ["p", root_author], ["p", direct_parent_author]]`).

The app already keys metrics off the **first** `e` tag via `get_ref_id` (returns the first `e` tag value), so the root-id-first ordering keeps metrics attribution correct.

## Current State (verified)

- `client.publish_post(content)` builds kind-1 with **empty tags** — replies are NOT threaded today.
- `client._handle_event` already: dedups by `seen_events`, counts kind 7→likes / 6→reposts / 1→replies into `self.metrics[target]`, emits `metrics-updated(eid, likes, reposts, replies)`, saves every event to the DB.
- `main.on_metrics_updated` already updates `widget.lbl_likes/lbl_reposts/lbl_replies` by event_id.
- `PostWidget` footer (`post_widget.py:95-113`) builds three **labels** (non-interactive) via `mk_met(icon, label)`.
- `PostWidget` has a whole-card `Gtk.GestureClick` that opens the thread (`post_widget.py:115-123`) — **buttons inside the card must consume clicks** so tapping Like/Repost/Reply doesn't also open the thread.

---

## Phased Tasks (each = 2–5 min, commit after each)

### Task 1: Add `_build_and_publish` helper + `publish_reaction`

**Objective:** One DRY publish helper and the kind-7 reaction method.

**Files:**
- Modify: `src/gnostr/client.py` (after `publish_post`, ~line 292)

**Step 1: Write failing tests** — `tests/test_client_publish.py`:
```python
def test_publish_reaction_builds_kind7(mocker):
    c = mocker.Mock(); client = NostrClient(mocker.Mock())
    client.my_pubkey = "aa"*32; client.my_privkey = "bb"*32
    client.publish = mocker.Mock()
    mocker.patch("gnostr.nostr_utils.sign_event", return_value={"id": "x"})
    ok = client.publish_reaction("target_id", "target_pk", "+")
    assert ok
    ev = client.publish.call_args.args[0]
    assert ev["kind"] == 7 and ev["content"] == "+"
    assert ["e", "target_id"] in ev["tags"] and ["p", "target_pk"] in ev["tags"]

def test_publish_reaction_requires_key():
    client = NostrClient(mocker.Mock()); client.my_privkey = None
    assert not client.publish_reaction("t", "p", "+")
```
Run: `pytest tests/test_client_publish.py -v` → FAIL (AttributeError: no method).

**Step 2: Implement** (in `client.py`):
```python
def _build_and_publish(self, kind, content, tags):
    if not self.my_privkey or not self.my_pubkey:
        print("❌ No private key loaded"); return False
    event = {"pubkey": self.my_pubkey, "created_at": int(time.time()),
             "kind": kind, "tags": tags, "content": content}
    signed = gnostr.nostr_utils.sign_event(event, self.my_privkey)
    if signed:
        self.publish(signed); return True
    return False

def publish_reaction(self, target_event_id, target_pubkey, content="+"):
    """NIP-25: kind-7 reaction. content '+' adds, '-' removes/undo."""
    tags = [["e", target_event_id], ["p", target_pubkey]]
    return self._build_and_publish(7, content, tags)
```

**Step 3:** `pytest tests/test_client_publish.py -v` → PASS.

**Step 4:** Commit: `git commit -m "feat: kind-7 reaction publish + shared publish helper"`

---

### Task 2: Add `publish_repost` (NIP-18)

**Objective:** Kind-6 generic repost embedding the original event JSON.

**Files:**
- Modify: `src/gnostr/client.py`

**Step 1: Test** — in `tests/test_client_publish.py`:
```python
def test_publish_repost_embeds_original():
    client = NostrClient(mocker.Mock()); client.my_privkey = "bb"*32
    client.my_pubkey = "aa"*32; client.publish = mocker.Mock()
    mocker.patch("gnostr.nostr_utils.sign_event", return_value={"id": "y"})
    orig = {"id": "o1", "kind": 1, "content": "hi", "tags": [], "pubkey": "pk",
            "created_at": 1, "sig": "s"}
    ok = client.publish_repost("o1", "pk", orig)
    ev = client.publish.call_args.args[0]
    assert ev["kind"] == 6 and json.loads(ev["content"])["id"] == "o1"
    assert ["k", "1"] in ev["tags"] and ["e", "o1"] in ev["tags"] and ["p", "pk"] in ev["tags"]
```

**Step 2: Implement** — need the original event's JSON. The DB does **not** store raw JSON, so reconstruct from the DB row. Add a DB accessor first (this task) and use it:
```python
# database.py
def get_event_by_id(self, event_id):  # already exists — returns dict with id/pubkey/kind/content/tags/sig/created_at
```
```python
# client.py
def publish_repost(self, target_event_id, target_pubkey, original_event=None):
    """NIP-18: kind-6 generic repost. content = original event JSON."""
    ev = original_event or self.db.get_event_by_id(target_event_id)
    if not ev:
        return False
    content = json.dumps(ev, separators=(",", ":"))
    tags = [["k", str(ev.get("kind", 1))], ["e", target_event_id], ["p", target_pubkey]]
    return self._build_and_publish(6, content, tags)
```

**Step 3:** Run tests → PASS. **Step 4:** Commit `feat: kind-6 repost publish (NIP-18)`.

---

### Task 3: Thread replies — extend `publish_post` with reply context

**Objective:** Kind-1 replies carry root + direct-parent `e`/`p` tags so they nest correctly (matches the existing last-`e` tree logic).

**Files:**
- Modify: `src/gnostr/client.py` (`publish_post` signature)

**Step 1: Test:**
```python
def test_publish_reply_has_root_and_parent_tags():
    client = NostrClient(mocker.Mock()); client.my_privkey = "bb"*32
    client.my_pubkey = "aa"*32; client.publish = mocker.Mock()
    mocker.patch("gnostr.nostr_utils.sign_event", return_value={"id": "z"})
    client.publish_post("my reply", reply_to={"root": "R1", "parent": "P1",
                                              "root_pk": "rp", "parent_pk": "pp"})
    ev = client.publish.call_args.args[0]
    etags = [t for t in ev["tags"] if t[0] == "e"]
    ptags = [t for t in ev["tags"] if t[0] == "p"]
    assert etags == [["e", "R1"], ["e", "P1"]]  # root first, parent last
    assert ["p", "rp"] in ptags and ["p", "pp"] in ptags
```

**Step 2: Implement** — extend `publish_post` to accept optional reply context:
```python
def publish_post(self, content, reply_to=None):
    """kind-1 text post. reply_to = dict(root, parent, root_pk, parent_pk)
    to thread the reply per NIP-01 (root e-tag first, direct parent last)."""
    tags = []
    if reply_to:
        tags = [["e", reply_to["root"]], ["e", reply_to["parent"]],
                ["p", reply_to["root_pk"]], ["p", reply_to["parent_pk"]]]
    return self._build_and_publish(1, content, tags)
```
(This replaces the body of the current `publish_post`.)

**Step 3:** Run tests (existing `publish_post` callers pass — `reply_to` defaults to None). **Step 4:** Commit `feat: thread replies with root+parent tags`.

---

### Task 4: DB query for "did I already like this?"

**Objective:** Support like-toggle (kind-7 `+`/`-`) by knowing the current user's reaction.

**Files:**
- Modify: `src/gnostr/database.py`

**Step 1: Test** (in `tests/test_database.py`):
```python
def test_user_reaction_returns_content():
    db = Database(":memory:")
    db.save_event({"id": "r1", "pubkey": "me", "kind": 7, "content": "+",
                   "tags": json.dumps([["e", "post1"], ["p", "author"]]),
                   "created_at": 1, "sig": "s"})
    assert db.user_reaction("post1", "me") == "+"
    assert db.user_reaction("post2", "me") is None
```

**Step 2: Implement:**
```python
def user_reaction(self, target_event_id, pubkey):
    cur = self.conn.cursor()
    cur.execute("SELECT content FROM events WHERE kind=7 AND pubkey=? "
                "AND tags LIKE ?", (pubkey, f'%"e","{target_event_id}"%'))
    row = cur.fetchone()
    return row[0] if row else None
```

**Step 3:** Run → PASS. **Step 4:** Commit `feat: user_reaction query for like-toggle`.

---

### Task 5: PostWidget — interactive Like / Repost / Reply buttons

**Objective:** Make the footer metrics interactive; buttons consume clicks so the thread-open gesture doesn't fire.

**Files:**
- Modify: `src/gnostr/ui/post_widget.py` (footer block, lines 95–113 + interaction block 114–123)

**Step 1: Test** (construction still passes under mocked GTK; add a button-existence test):
```python
def test_footer_has_action_buttons():
    w = PostWidget(mock_window, "pk", "content", "eid", [], created_at=0)
    assert hasattr(w, "btn_like") and hasattr(w, "btn_repost") and hasattr(w, "btn_reply")
```

**Step 2: Implement** — replace the `mk_met` labels with `Gtk.Button`s, keeping the same icons + counts, and wire them:
```python
self.lbl_replies, self.btn_reply = self._mk_action_btn("chat-bubble-symbolic", "0")
self.lbl_reposts, self.btn_repost = self._mk_action_btn("media-playlist-repeat-symbolic", "0")
self.lbl_likes,  self.btn_like   = self._mk_action_btn("starred-symbolic", "0")
# wire (only if logged in + not hero):
self.btn_like.connect("clicked", self._on_like)
self.btn_repost.connect("clicked", self._on_repost)
self.btn_reply.connect("clicked", self._on_reply)
```
Handlers call `self.main_window.client.publish_*` and toast on success/failure, then optimistically bump the local label.

**Critical — stop the card's open-thread gesture:** buttons sit inside the PostWidget that has the whole-card `GestureClick`. Each `Gtk.Button` handles its own press/release, but the card gesture may still fire for the same sequence. Add, on each button:
```python
btn.add_controller(click_ctrl)  # or ensure button's own gesture CLAIMS on press
```
Concretely: give each action button its own `Gtk.GestureClick` that `set_state(CLAIMED)` on press — same pattern already used for the video frame (renderer.py:120-122). This DENIES the card's open-thread gesture for that button's press, so tapping Like/Repost/Reply never opens the thread.

**Step 3:** Run `pytest tests/` → 30+ pass, no GTK assertion. **Step 4:** Commit `feat: interactive like/repost/reply buttons`.

---

### Task 6: Like-toggle state (filled vs. empty icon) + undo

**Objective:** Show the like button filled when the user already liked; clicking toggles `+`/`-`.

**Files:**
- Modify: `src/gnostr/ui/post_widget.py` (+ use `db.user_reaction`)

**Step 1: Test:** like button reflects `user_reaction` state; clicking when not liked publishes `+`, when liked publishes `-`.

**Step 2: Implement:**
```python
my_pk = self.main_window.pub_key
liked = self.main_window.db.user_reaction(event_id, my_pk) if my_pk else None
self._liked = bool(liked)
# set icon: starred-symbolic vs non-starred based on _liked; update on toggle
```
On click: `publish_reaction(event_id, pubkey, "-" if self._liked else "+")`, flip `_liked`, swap icon, decrement/increment `lbl_likes`.

**Step 3/4:** tests → PASS; commit `feat: like toggle with +/− undo`.

---

### Task 7: Reply + Repost UX (open compose, confirm)

**Objective:** Reply opens the existing `ComposeWindow` pre-threaded; Repost confirms then publishes.

**Files:**
- Modify: `src/gnostr/ui/post_widget.py` (or `main.py` helper)

**Reply:** on `btn_reply`, open `ComposeWindow(parent, handle_post)` where `handle_post(text)` calls `client.publish_post(text, reply_to={"root": root_id, "parent": event_id, "root_pk": root_author, "parent_pk": pubkey})`. For a root post, `root == parent == event_id` (root_author == pubkey). Pass `root_id`/`root_author` into PostWidget (from the thread's hero, or default to self for feed-level replies).

**Repost:** `Adw.AlertDialog` confirm → `client.publish_repost(event_id, pubkey, original_event)` (fetch original via `db.get_event_by_id`), toast result. No quote text for now (YAGNI — generic repost is the minimal standards-compliant form).

**Step 3/4:** tests + commit `feat: reply compose + repost confirm`.

---

### Task 8: DOX pass + full verification

**Files:**
- Modify: `src/gnostr/AGENTS.md` (client contracts) and `src/gnostr/ui/AGENTS.md` (post widget contract) — these are base64-encoded, so decode → edit → re-encode.

**Step 1:** Re-read `src/gnostr/AGENTS.md` and `ui/AGENTS.md`; walk DOX chain per repo rule.

**Step 2:** Document: new publish methods (kinds 7/6/threaded-1), `_build_and_publish` helper, `user_reaction` query, footer action buttons + click-consumption pattern.

**Step 3:** `pytest tests/` (all pass), `black src/`, `flake8 src/ --max-line-length=120`, `isort`.

**Step 4:** Commit `docs: DOX for social actions`. **Step 5:** Flatpak build + push to Gitea, user rebuilds.

---

## Risks / Tradeoffs / Open Questions

- **Click consumption is the trickiest part** (Task 5). The card-wide open-thread gesture vs. footer buttons needs the same CLAIM-on-press pattern already proven for the video frame. If a button click still opens the thread, fall back to `Gtk.Popover`/`GestureSingle` on the buttons with `set_state(CLAIMED)` verified against the card gesture — test in the running app, not just unit tests.
- **Repost content reconstruction** assumes the DB row round-trips to valid original-event JSON. Key order in the embedded JSON doesn't matter (parsers accept any order), but `sig`/`id` must be present and correct — the DB stores both, so this holds.
- **Undo semantics:** NIP-25 uses a *second* kind-7 with content `-` (not event deletion). This is the standard, but relays/clients vary on honoring it; some count it as a separate reaction. Acceptable — matches the spec.
- **Repost of a repost / like-of-like:** `get_ref_id` returns the first `e` tag, so a repost of a repost counts toward the *original* event. Correct per NIP-18 (the `e` tag points at the original). No extra work needed.
- **Hero post (thread view) buttons:** the plan wires buttons only for non-hero cards (is_hero check) to avoid double-wiring in ThreadView; decide during Task 5 whether hero should also get them (likely yes via `hero_section`, but isolated).
- **Optimistic count updates** can drift if the relay doesn't echo our event; acceptable for v1, re-sync on `metrics-updated` arrival (which the existing handler already does).
