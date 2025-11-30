# Gnostr (Under Construction)

Gnostr is a native Linux Nostr client designed specifically for the GNOME Desktop environment.  It leverages an adaptive user interface, providing a great experience on both desktop and mobile Linux devices, such as the Librem 5 or PinePhone.

## Features
[![Screenshot of Gnostr](screenshot.png)](screenshot.png)
*   **Adaptive UI:** Utilizes `Adw.NavigationSplitView` for a sidebar layout on desktop and a navigation stack on mobile, ensuring a consistent and intuitive experience across different screen sizes.
*   **Secure Storage:** Private keys (nsec) are stored securely in the system keyring using `libsecret`, preventing exposure of sensitive data in plain text.
*   **Nostr Protocol:** Connects to relays via WebSockets to fetch and publish events. (Currently focused on read-only functionality).
*   **GNOME Integration:**  Built with GNOME technologies for seamless integration with the desktop environment.

## Getting Started
Use GNOME Builder to build this application. If you clone into your project directory and build it should download the necessary SDKs and libraries and allow you to build on your own system. Flatpaks will be made available soon.

## Buy me a beer

If you like this project and would like to support its development:

bc1qkgcg44wxmmjrt5uvnya5g2zqd84pm6mawjp3s

## License

This project is licensed under the GPL 3 License. See the LICENSE file for details.
