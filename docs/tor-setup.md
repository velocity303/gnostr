# Using Tor with gnostr

gnostr routes its network traffic (relays, LNURL, media) through a **SOCKS5
proxy** when you enable one in Preferences. It does **not** start or manage Tor
itself — you run the standard open-source `tor` daemon, and gnostr talks to it.
With the proxy enabled, `.onion` relays work, and your IP is hidden from
clearnet relays too.

## 1. Install and start tor

gnostr expects a tor SOCKS5 proxy on `127.0.0.1:9050` (the tor default).

**Fedora / Nobara:**
```
sudo dnf install tor
sudo systemctl enable --now tor
```

**Debian / Ubuntu:**
```
sudo apt install tor
sudo systemctl enable --now tor
```

**Arch:**
```
sudo pacman -S tor
sudo systemctl enable --now tor
```

**Librem 5 / PureOS:** PureOS ships `tor` in its repos — use the `apt` lines
above over SSH or the terminal app. Orbot (the Android Tor app) does not apply
on the Librem 5 under Phosh; run the system daemon instead.

### Minimal torrc

The packaged default works as-is. If you need a minimal `/etc/tor/torrc`:

```
SocksPort 127.0.0.1:9050
```

### Verify it is listening and bootstrapped

```
# 1. Something is listening on 9050:
ss -ltn | grep 9050

# 2. It answers the SOCKS5 greeting (prints "socks5 ok" on success):
python3 -c 'import socket; s=socket.create_connection(("127.0.0.1",9050),2); s.send(b"\x05\x01\x00"); print("socks5 ok" if s.recv(2)==b"\x05\x00" else "not a no-auth socks5 proxy")'

# 3. Tor has finished building circuits:
sudo torctl status   # or: journalctl -u tor | tail  -> look for "100% fetched"
```

## 2. Point gnostr at it

Preferences → Proxy:
- **Enable proxy** — on
- **Host** — `127.0.0.1`
- **Port** — `9050`

Leave "resolve hostnames remotely" enabled (required for `.onion` addresses:
the hostname is sent to tor untouched, never to your local DNS).

Add onion relays as usual, e.g. `ws://TEST_ONION_PLACEHOLDER.onion` — plain
`ws://` (no TLS) is correct and expected for `.onion` relays: the onion layer
is already encrypted end-to-end.

## 3. Flatpak users

The default `127.0.0.1:9050` works out of the box with the gnostr Flatpak:
`--share=network` shares the host network namespace, so the sandbox reaches a
tor daemon bound to the host loopback (verified on the Librem 5 with tor
0.4.9.11). The gateway-IP workaround (`SocksPort <gateway-ip>:9050`) is only
needed for sandbox setups with an isolated network namespace, such as
`podman --net=slirp4netns`.

If you ever need to check reachability from inside the sandbox:
```
flatpak run --command=sh tech.livingonlinux.gnostr
timeout 2 bash -c 'echo > /dev/tcp/127.0.0.1/9050' && echo loopback-ok || echo loopback-fail
```

## 4. What the tor icon means

When you have `.onion` relays or the proxy enabled, a small icon appears next
to the connection dot in the sidebar:

- **dim icon** — proxy is off; your onion relays are inactive (they will not
  be contacted, and posts from them are missing until Tor is connected).
- **plain icon** — tor proxy reachable; onion relays connected.
- **warning icon** — proxy enabled but not reachable: start tor (§1), or fix
  the host/port. gnostr skips onion relays while the proxy is down and picks
  them up automatically the moment it answers again — no restart needed.

The Relay Activity log records each tor up/down transition, so gaps in your
feed are explainable after the fact.

## 5. Privacy note (honest scope)

With the proxy enabled, relay connections, LNURL lookups, and media fetches
all go through tor; gnostr makes no other outbound connections. gnostr does
not isolate circuits per relay (all traffic shares tor's circuits), and it
never falls back to clearnet for an `.onion` endpoint — if tor is down, onion
relays simply stay inactive.
