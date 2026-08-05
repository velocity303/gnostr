# DOX — src/gnostr/ui/

## Purpose
GTK widget components — all reusable UI elements for displaying Nostr data. Each widget is a self-contained GTK composite that receives data and renders it.

## Ownership
- `feed_view.py` — FeedView: scrollable post list with post_box container
- `post_widget.py` — PostWidget: individual event card with likes/reposts/replies labels
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
- PostWidget.set_content(content) swaps just the rendered content box (child index 1, between header and footer) on an existing widget — keeps widget identity, avoids re-running image loads/video pipelines/profile fetch
- PostWidget.insert_time_sorted(box, widget) inserts a post into a box keeping newest-at-top (descending created_at); used by BOTH the feed and thread so live/backfilled posts slot into the correct time position instead of blind prepend/append. Implements the GTK4 idiom `append()` then `reorder_child(widget, idx)` — Gtk.Box has no `insert()` (Gtk3-only)
- PostWidget carries `.created_at` (resolved from the DB when not passed) so sorted insertion works for live events too
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
