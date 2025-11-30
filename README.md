# Gnostr

### Gnostr is a native Linux Nostr client built for the GNOME Desktop environment. It utilizes an adaptive UI that works seamlessly on both desktop and mobile Linux devices (like the Librem 5 or PinePhone).

Features

Adaptive UI: Uses Adw.NavigationSplitView to provide a sidebar layout on desktop and a navigation stack on mobile.

Secure Storage: Private keys (nsec) are stored securely in the system keyring using libsecret, never in plain text.

Nostr Protocol: Connects to relays via WebSockets to fetch and publish events. (Right now mostly read only support)

License

MIT License. See LICENSE for details.
