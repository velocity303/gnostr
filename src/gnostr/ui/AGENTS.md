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
- All widgets receive data, never fetch it themselves — data flows from client → widget
- FeedView.posts_box is a Gtk.Box that PostWidget instances are prepended to
- PostWidget stores event_id, pubkey, content, and tracks metrics labels; shows a compact relative-time caption (now/Nm/Nh/Nd/date) at the top-right of the header, resolved from the DB when not passed in
- PostWidget has `.quote_widgets` for nostr event quote cards and `.inline_mention_labels` for inline @mention text labels
- Profile @mentions render inline in the text flow (small `@name` Pango links); clicking opens the profile view via `activate-link`
- Sidebar emits menu signals that MainWindow handles for feed switching
- ThreadView fetches thread data via client.fetch_thread(), not inline
- ThreadView has a Refresh Thread button: re-fetches root/replies/reactions, then re-renders the hero, replies_box, and metric labels from the DB / client.metrics
- client._handle_event dedups by event id FIRST, so thread refreshes never double-count reaction metrics

## Work Guidance
- Widgets use Gtk4/Adw patterns — Gtk.Box, Gtk.Label, Adw.Avatar, Gtk.Button, Gtk.Frame
- CSS classes used: `dim-label`, `heading`, `caption`, `caption-heading`, `quote-card`, `profile-card`, `quote-wrapper`, `flat`
- PostWidget prepends to posts_box (newest at top) — no sorting logic in widget
- All labels use `xalign=0` for left alignment, wrap=True for long content
- ProfileView shows avatar with Adw.Avatar, supports both image and video profile pictures; back/refresh toolbar sits at the top of the layout (upper-left), outside the centered header
- Do NOT put business logic in widgets — they render data, they don't decide what to show

## Verification
- Widget construction tests pass without GTK assertion failures
- All widgets can be instantiated in test fixtures with mocked parent window
- CSS class strings match actual GNOME theme classes
