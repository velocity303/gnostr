# DOX — src/gnostr/ui/

## Purpose
GTK widget components — all reusable UI elements for displaying Nostr data. Each widget is a self-contained GTK composite that receives data and renders it.

## Ownership
- `feed_view.py` — FeedView: scrollable post list with post_box container
- `post_widget.py` — PostWidget: individual event card with interactive like/repost/reply buttons (icon + count)
- `profile_view.py` — ProfileView: user profile display with avatar, name, bio
- `thread_view.py` — ThreadView: event thread with reply tree
- `sidebar.py` — Sidebar: navigation menu (global/following/profile/search)

## Local Contracts
- Widgets render data that's already cached; when a needed piece is missing (author profile, reply parent, quote event) they may trigger a fetch via the client — they never decide what to show beyond that
- FeedView.posts_box is a Gtk.Box that PostWidget instances are prepended to
- PostWidget stores event_id, pubkey, content, and tracks metrics labels; shows a compact relative-time caption (now/Nm/Nh/Nd/date) at the top-right of the header, resolved from the DB when not passed in
- PostWidget has `.quote_widgets` for nostr event quote cards and `.inline_mention_labels` for inline @mention text labels
- Profile @mentions render inline in the text flow (small bold `@name` Pango links); clicking opens the profile view via `activate-link`. Inline avatar-in-mention is a follow-up (needs a Gtk.FlowBox render rework)
- Sidebar emits menu signals that MainWindow handles for feed switching
- ThreadView fetches thread data via client.fetch_thread(), not inline
- ThreadView has a Refresh Thread button: re-fetches root/replies/reactions, then re-renders the hero, replies_box, and metric labels from the DB / client.metrics
- ThreadView renders the WHOLE thread (hero + context + toggle + replies) in a single `Gtk.ScrolledWindow` so long posts stay navigable; the hero PostWidget and its Show more/less toggle live in a dedicated `hero_section` sub-container. The toggle calls `PostWidget.set_content` to swap only the hero's content box — it NEVER rebuilds the PostWidget (rebuilding re-ran the async render chain and orphaned mid-load widgets, causing posts to vanish/glitch)
- PostWidget.set_content(content) swaps just the rendered content box (child 1, between header and footer) on an existing widget — keeps widget identity, avoids re-running image loads/video pipelines/profile fetch. Because Gtk.Box has NO index-based `insert()` in these bindings, it removes ALL children and re-appends them in order with the new rendered box in slot 1; header/footer are re-appended as the SAME widget objects (no re-render), only the content box is new. `PostWidget.insert_time_sorted` likewise only calls `remove` on actual children (`c.get_parent() is box` guard — removing a non-child raises gtk_box_remove's parent assertion)
- PostWidget.insert_time_sorted(box, widget) inserts a post into a box keeping newest-at-top (descending created_at); used for LIVE insertions so new/backfilled posts slot into the correct time position. GTK4 Gtk.Box exposes NO index-based insert (neither `insert()` nor `reorder_child()` exist in these bindings), so it rebuilds the box using only the guaranteed `append`/`remove` primitives — collect children, splice the new widget at the right index, re-append in order. Bulk loads (switch_feed, reload_replies) just `append`, since the DB queries already return `ORDER BY created_at DESC`
- PostWidget carries `.created_at` (resolved from the DB when not passed) so sorted insertion works for live events too
- PostWidget like-toggle icon: `Gtk.Image` uses `set_from_icon_name()` (NOT `set_icon_name` — that setter does not exist on Gtk.Image and crashed every PostWidget on feed load). The icon `Gtk.Image` ref is kept on the button as `btn.icon_img` (no fragile `get_child()` walk) and set via `_resolve_icon_name()` — confirms the glyph against the active icon theme and falls back to known-good names so the un-liked outline star never renders blank. GTK4 `Gtk.IconTheme` exposes `has_icon()` only — `has_icon_pixbuf` is GTK2/3 and raised AttributeError on every PostWidget (feed failed to load). The `.liked` CSS class (accent color, registered by the `Gtk.CssProvider` in `GnostrApp.do_activate`) toggles with like state for an unmistakable filled/accented star
- PostWidget click gesture: long-press copy uses `Gtk.GestureLongPress` with `delay-factor=1.5` (~750ms, lengthened from ~500ms so a casual tap doesn't copy); the click gesture opens the thread on a normal click. The video frame's own `Gtk.GestureClick` CLAIMS its sequence on press (`set_state(CLAIMED)`), which DENIES the enclosing PostWidget's open-thread gesture for that same event — so clicking media plays/pauses only and never navigates into the post. The footer action buttons (like/repost/reply) each carry their own `GestureClick` that also CLAIMS on press for the same reason — tapping an action never opens the thread. Buttons wire only when logged in (`main_window.pub_key` set) and never on the hero card; `PostWidget.__init__` takes `root_id`/`root_pk` (thread root event id + author) so the reply compose pre-threads to the thread root per NIP-01
- ThreadView renders replies as a reply TREE (parent→children via the LAST `e` tag = direct reply target per NIP-01), depth-first with indentation, so a reply to a reply sits beneath its direct parent even if newer; newest-first within each sibling group. On a live thread reply it reloads the tree from the DB (dedups) and requests that reply's own replies (`#e:[eid]`) so deeper nesting fills in live
- PostWidget fetches missing author/mention profiles via client.fetch_profile (TTL-cached); names/avatars re-render via on_profile_updated on arrival
- ThreadView fetches missing reply parents via client.request_once and renders them into context_box when they arrive (incl. the grandparent chain)

## Work Guidance
- Widgets use Gtk4/Adw patterns — Gtk.Box, Gtk.Label, Adw.Avatar, Gtk.Button, Gtk.Frame
- CSS classes used: `dim-label`, `heading`, `caption`, `caption-heading`, `quote-card`, `profile-card`, `quote-wrapper`, `flat`
- Posts are kept in time order via `PostWidget.insert_time_sorted` (newest at top) in both feed and thread views — widgets never decide ordering themselves, the insert helper does
- All labels use `xalign=0` for left alignment, wrap=True for long content
- ProfileView shows avatar with Adw.Avatar, supports both image and video profile pictures; back/refresh toolbar sits at the top of the layout (upper-left), outside the centered header; on other users' profiles it has a Follow/Unfollow toggle (updates the DB via `set_following` + publishes a kind-3 contact list)
- Do NOT put business logic in widgets — they render data, they don't decide what to show

## Verification
- Widget construction tests pass without GTK assertion failures
- All widgets can be instantiated in test fixtures with mocked parent window
- CSS class strings match actual GNOME theme classes
