# tools/ — phone dev helpers

Developer helper scripts for the two phone targets. Not shipped in the Flatpak.

## Devices
- `librem5` (phosh, SSH alias in ~/.ssh/config): screenshots via `grim` (works out of the box).
- `librem5-pm` (Plasma Mobile/postmarketOS, user@192.168.5.183): screenshots via `tools/pmshot`.

## pmshot
`tools/pmshot <local.png>` — Plasma Mobile capture. Pitfalls discovered 2026-09-17 (all must hold or frames come back frozen/identical bytes):
1. grim fails: this KWin wlroots build exposes no zwlr-screencopy protocol.
2. Legacy org.kde.kwin.Screenshot API is gone; portal Screenshot requests are accepted but never answered (interactive and non-interactive).
3. Working path is `spectacle -b -n -o` on the session bus (`/tmp/dbus-*`, NOT /run/user/10000/bus — Plasma runs under dbus-run-session).
4. The capture buffer FREEZES when the screen blanks/locks: identical bytes for any content. Screen lock AND screen blanking are disabled on the device; if shots go stale, check `/sys/class/backlight/backlight-dsi/bl_power` (0=on) and power settings.

## Plasma Mobile gotchas
- pmOS does not autoreconnect Wi-Fi on reboot.
- FDE prompt blocks boot (keep FDE, prompt once per boot; no clean way to disable post-install — chosen at flash time).

## App deploy to PM
Tarball the `repo` dir from librem5, `flatpak remote-add --user gnostr-local file:///home/user/lab/repo` (no-gpg-verify), `flatpak install --user --reinstall`.

## Build note (GNOME 50 runtime)
The runtime update removed `appstream-compose` from the SDK sandbox; flatpak-builder now fails on that final step only. Workaround: `flatpak build-export repo build-dir` manually (verify `build-dir/metadata` has `command=gnostr`, not pytest) + `flatpak install --reinstall`.

## Rendering on PM
The app logged `Unable to create a GL context` (GTK fell back off GL); rendering works, watch for it if visuals regress. `GSK_RENDERER=cairo` forces the software renderer explicitly.
