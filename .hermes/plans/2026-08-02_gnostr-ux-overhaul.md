# Gnostr UX Overhaul — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Fix gnostr's mobile scrolling, data-completeness, video controls, mention styling, and navigation regressions across 8 reported areas.

**Architecture:** Python + GTK4/libadwaita, monolithic renderer (`src/gnostr/renderer.py`) + split UI widgets (`src/gnostr/ui/`). Data flows client → DB → widgets. Changes are mostly small, targeted edits; the two bigger pieces are decorative mentions (FlowBox) and video control simplification.

**Tech Stack:** GTK4, Adw, GStreamer (playbin3), SQLite (via `gnostr/database.py`), Nostr client (`gnostr/client.py`).

---

## Phase 1 — Quick wins (small diffs, high value)

### Task 1: Fix @ mention spacing (`text@user` → `text @user`)

**Objective:** Restore the whitespace that `LINK_REGEX` consumes before a nostr URI so mentions read inline with leading space.

**Root cause:** `render()` splits with `LINK_REGEX = (?:^|\s)((?:https?://|nostr:)[^\s]+)` — the non-capturing `(?:^|\s)` **eats the leading space** during `re.split`, so the mention fragment renders flush against the prior word (`text@user`).

**Files:** `src/gnostr/renderer.py` — inside `render()`, when building the inline mention fragment (the `if hex_pk:` branch around line ~76):

```python
# prepend the space the regex consumed; drop it if the mention starts the post
text_fragments.append((hex_pk, f'<a href="nostr:{hex_pk}">@{GLib.markup_escape_text(name)}</a>'))
text_fragments.append((None, " "))
```

**Better fix (DRY):** instead of appending a literal space, change `LINK_REGEX` to not consume the leading whitespace — use a lookbehind: `re.compile(r"(?<![\w])((?:https?://|nostr:)[^\s]+)")` — then the space stays in the text fragment and every link/mention gets correct spacing automatically. Verify the split still isolates URLs.

**Validation:** `pytest` on a small render test (construct `ContentRenderer.render("hello nostr:npub1... world", mock)`) asserting the markup contains `@…` preceded by a space; manually eyeball a feed post in-app.

**Commit:** `fix: @ mention inline spacing`

---

### Task 2: Back-on-profile shows blank feed

**Objective:** Loading a profile first (sidebar → Profile) then hitting Back should reveal a populated feed, not a blank one.

**Root cause:** `MainWindow.__init__` (main.py ~112-124) never calls `switch_feed` — the feed page is only populated when the user clicks a sidebar item (`on_menu_selected` → `switch_feed`). Going straight to Profile pushes the profile page over an empty feed root.

**Files:** `src/gnostr/main.py` — after `perform_login` in `__init__`, kick the feed once:

```python
GLib.idle_add(self.client.connect_all)
GLib.timeout_add_seconds(300, self.on_auto_refresh)
# populate the feed root so Back never lands on a blank page
self.switch_feed("following" if self.pub_key else "global")
```

Guard `switch_feed` against re-entrancy (it currently clears + re-subscribes every call; the 300s auto-refresh and menu clicks already call it — add a cheap `self.active_feed_type == feed_type` early-return to avoid wipe-on-wipe).

**Validation:** fresh login → sidebar Profile → Back → feed shows posts. Also confirm no double-subscribe on startup.

**Commit:** `fix: load feed on startup so back navigation is never blank`

---

### Task 3: Mobile scroll vs text-highlight + long-press copy

**Objective:** Touch-drag over a post should scroll, not select text; long-press offers "Copy post".

**Root cause:** renderer labels use `selectable=True` (`_add_text`, `_text_label` in renderer.py), so touch drags on text grab selection instead of feeding the scroller.

**Files:**
- `src/gnostr/renderer.py` — set `selectable=False` on `_add_text` and `_text_label` (keep `use_markup`/link handling).
- `src/gnostr/ui/post_widget.py` — add a `Gtk.GestureLongPress` controller that copies the whole post:

```python
lp = Gtk.GestureLongPress()
lp.connect("pressed", lambda g, x, y: self._copy_post())
self.add_controller(lp)

def _copy_post(self):
    clip = Gdk.Display.get_default().get_clipboard()
    clip.set(self.main_window, Gdk.ContentFormats... )  # or clip.set_text via Gdk
    # simplest: self.main_window.add_toast(Adw.Toast(title="Copied post"))
```

Use the same clipboard pattern as `ProfileView.copy_to_clipboard` (main.py has `Gdk.ContentProvider.new_for_value`). Store the raw `content` string on the widget for copying.

**Tradeoff:** dropping `selectable` removes desktop text-selection entirely — acceptable per user ("maybe a long press gives the option to copy post"). Keep selectability off only on the feed/thread; links still work via `activate-link`.

**Validation:** on-device touch scroll no longer selects; long-press shows a toast + puts post text on the clipboard.

**Commit:** `fix: scroll over text; add long-press copy post`

---

### Task 4: Long hero posts collapse so replies are visible

**Objective:** In ThreadView, a long top post should collapse/shrink so replies aren't pushed off-screen.

**Files:** `src/gnostr/ui/thread_view.py` — wrap the hero content in a collapsible:

- After building the hero `PostWidget`, if its content text length > ~500 chars, show the first N chars + a `Gtk.Button("Show more")` that expands.
- Implement as a `Gtk.Expander` or a toggle button that reveals the full `PostWidget`. Keep `self.hero_widget` reference (already stored) for the metrics/refresh wiring.

```python
# in ThreadView.__init__, after building hero
if len(content) > 500:
    expander = Gtk.Expander(label=f"Show full post ({len(content)} chars)")
    expander.add_css_class("flat")  # or use a button + toggle
    expander.set_expanded(False)
    # wrap the hero in a box, swap child on toggle
```

Simplest robust: build the hero into a container box; if long, also add a "Show more" button that removes the clamp. Reuse the collapse state so `refresh()`/`_replace_hero` rebuilds with the same collapsed default.

**Validation:** open a long post → hero shows truncated + "Show more"; replies visible in viewport; toggle expands the full text.

**Commit:** `fix: collapse long hero posts in thread view`

---

## Phase 2 — Data completeness (fetch missing parts)

### Task 5: Fetch missing profile names / mention avatars / reply authors

**Objective:** When rendering, fetch any missing profile metadata instead of showing fallback `pubkey[:8]`.

**Root cause:** `renderer._mention_name()` reads `db.get_profile` but never calls `client.fetch_profile` when absent; `PostWidget` builds name/avatar from `db.get_profile` only.

**Files:**
- `src/gnostr/renderer.py` — in `_mention_name`, if no profile: `window_ref.client.fetch_profile(hex_pk)` (TTL-cached already), so the name/avatar resolves after the kind-0 arrives and `on_profile_updated` re-renders the inline label.
- `src/gnostr/ui/post_widget.py` — if `prof` is None, call `self.main_window.client.fetch_profile(pubkey)`.
- `src/gnostr/main.py` `on_profile_updated` — already re-renders inline mention labels + post avatars/names (from prior work); verify it covers reply-author widgets (they're `PostWidget`s in `event_widgets`).

**Validation:** open a thread with an unknown author → name/pic appear once relay returns kind-0; mention label updates to the real name.

**Commit:** `feat: fetch missing profile metadata on render`

---

### Task 6: Load reply context (the post being replied to)

**Objective:** Clicking a reply in the feed should load the parent post even if it's not in the DB.

**Root cause:** `ThreadView.load_parent_context` (thread_view.py ~95-137) only renders the parent if `db.get_event_by_id(parent_id)` is non-None; otherwise it silently does nothing.

**Files:** `src/gnostr/ui/thread_view.py` — in `load_parent_context`, when `parent_ev` is None, request it and render on arrival:

```python
if parent_id:
    parent_ev = self.main_window.db.get_event_by_id(parent_id)
    if parent_ev:
        ...existing recursive + render...
    else:
        self.client.request_once(f"parent_{parent_id[:8]}", {"ids": [parent_id], "limit": 1})
        # store pending parent_id; when client 'event-received' for this id arrives,
        # call load_parent_context again (or render directly)
```

Wire a pending-parent list and handle it in `on_event_received` (thread_view.py) — if `eid == pending_parent_id`, re-run `load_parent_context(tags)` so the parent (and its grandparent chain) renders. Note the recursive grandparent fetch: each level that's missing must also `request_once`.

**Validation:** open a reply whose parent isn't cached → parent post appears once fetched; grandparent chain loads too.

**Commit:** `feat: fetch missing reply parent context`

---

## Phase 3 — UX simplification (medium)

### Task 7: Simplify video controls (GIFs + volume)

**Objective:** Remove non-functional GIF controls, drop volume UI, play at full volume, mute button beside the progress bar, click-video toggles play/pause.

**Files:** `src/gnostr/renderer.py` — `_add_video` (lines ~57-123) and `VideoPlayer` helpers.

- **GIFs (`.gif` in `VIDEO_EXTS`):** skip the progress/position bar and mute button entirely — they don't apply to a looping animation. Just render the video area (autoplay, loop).
- **Volume controls:** delete `vol_scale` and its `value-changed` wiring; set pipeline volume to 1.0 always (`video.set_volume?`/`Gst` volume element or `pipeline.props.volume = 1.0`). Users control loudness at the system level.
- **Controls layout:** keep a single bottom row = [play/pause][position slider][mute]. Position the mute button adjacent to the progress bar (immediately after it).
- **Click-to-toggle:** add a `Gtk.GestureClick` on the video area that calls `VideoPlayer.toggle_play(video_area)`.

**Validation:** GIF loops with no scrubber/mute; video has play + slider + mute only; clicking the frame toggles play/pause; volume always full.

**Commit:** `feat: simplify video controls (no volume, GIF cleanup)`

---

### Task 8: Decorative @ mentions with profile picture

**Objective:** Make mentions more decorative and add the author's avatar inline.

**Current:** inline Pango `<a>` link in a `Gtk.Label` — can't embed an image in markup.

**Approach (two options, pick per effort):**

- **Option A (CSS only, quick):** style the mention link with a CSS class (accent color, bold @) and a `Gtk.Label` subclass or `css_classes=["mention-link"]`. No avatar.
- **Option B (avatar inline, bigger):** render the text line as a `Gtk.FlowBox` (wraps like a paragraph) where each text run is a `Gtk.Label` and each mention is a small `Gtk.Button([Adw.Avatar(16)][@name])`. Requires reworking `render()`'s text accumulation from a single markup label to a FlowBox of runs — touches `render()`, `flush_text()`, and mention tracking (`mention_fragments` / `inline_mention_labels`).

**Recommendation:** ship Option A first (decorative, no avatar), then Option B as a follow-up. Option B is where the existing `_mention_name`/`load_avatars` machinery feeds the avatar.

**Validation:** mention visibly distinct from body text; (B) avatar shows + name updates on profile fetch.

**Commit:** `feat: decorative @ mentions` then `feat: inline mention avatars`

---

### Task 9: Follow / unfollow toggle on profile page

**Objective:** Add a Follow/Unfollow button on another user's profile.

**Files:** `src/gnostr/ui/profile_view.py` + `src/gnostr/database.py` + `src/gnostr/client.py`

- Add a toggle button in `ProfileView`'s header (below npub, or in the toolbar). Initial state from `db.get_following_list(self.main_window.pub_key)` membership.
- `database.py` — add a targeted toggle instead of `save_contacts` (which replaces the whole list): e.g. `add_following(owner, pubkey)` / `remove_following(owner, pubkey)` (single INSERT / DELETE on the `following` table).
- On toggle, update DB, then publish a **kind 3 contact-list** event via `client` (fetch the current list, add/remove the pubkey, sign, send) so follows propagate to relays.
- Hide the toggle on your own profile (can't follow yourself).

**Validation:** follow a user → button flips to "Unfollow", DB row added, kind-3 event sent; unfollow reverses; your own profile shows no toggle.

**Commit:** `feat: follow/unfollow on profile page`

---

## Phase 4 — Performance (investigation)

### Task 10: Video playback stutters on mobile

**Objective:** Reduce stutter in GStreamer playback on mobile/ARM.

**Current:** `VideoPlayer._fetch_player` builds `playbin3 uri=...` with appsink frame extraction (`_start_position_timer`, `set_state` etc.). A prior commit added "Force RGBA conversion for mobile videos + VAAPI env var".

**Investigation plan (measure first, then tune):**
1. Instrument: log pipeline state changes, dropped frames (`appsink` `GstSample` cadence), and `set_state` timing per URL.
2. Check decoder selection — `playbin3` auto-picks; on ARM test if `h264dec`/`avdec_h264` (software) vs `v4l2`/`vaapi`/`omx` hardware decode is in use. Try forcing `decodebin3` caps / `videoconvert` → `capsfilter` to `video/x-raw(BGR)` or `RGBA` at the target size.
3. Add a resolution cap: downscale via `videoconvert` + `capsfilter` (e.g. max 1280x720) before the appsink to cut decode+copy cost — mobile screens don't need 4K frames.
4. Raise `_CACHE_MAX`/teardown policy so scrolling doesn't churn decoders; keep LRU but verify the "tear down once far out of view" logic isn't thrashing on a long feed.
5. Consider a frame-drop policy in the appsink (drop late frames) for smoothness over completeness.

**Deliverable:** a short perf report (state-change log + decoder used + fps) and a targeted tuning commit; avoid speculative config changes without measurements.

**Commit:** `perf: tune video pipeline for mobile` (after measuring)

---

## Cross-cutting concerns

- **DOX pass:** every task touches `src/gnostr/ui/AGENTS.md` (PostWidget/ThreadView/ProfileView contracts) and possibly `src/gnostr/AGENTS.md` (renderer/client contracts). Update nearest owning AGENTS.md per change — the user's DOX workflow requires walking the tree before edits and updating after.
- **Tests:** add/refresh `tests/test_renderer.py` (mention spacing, missing-profile fetch) and `tests/test_thread_view.py` (parent fetch, collapse). Run: `pytest tests/ -q` (with the pre-existing `test_codec_support.py::test_load_and_play_success` known-fail).
- **Verification on device:** mobile scroll/click, GIF controls, video stutter, follow toggle, blank-feed fix all need a Flatpak rebuild on the user's machine.
- **Risk:** Task 8 Option B (FlowBox) and Task 10 (video tuning) are the two highest-risk items — isolate them into their own PRs/commits and review before merging.

## Suggested execution order

Phase 1 (Tasks 1-4) → Phase 2 (Tasks 5-6) → Phase 3 (Tasks 7-9) → Phase 4 (Task 10). Each phase is independently shippable; land them as separate commits so the user can rebuild/test incrementally.
