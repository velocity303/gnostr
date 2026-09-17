#!/usr/bin/env python3
"""Fullscreen screenshot via the freedesktop desktop portal.

Plasma Mobile (KWin wlroots, postmarketOS) exposes neither the zwlr screencopy
protocol (grim fails) nor the org.kde.kwin.Screenshot API, so the portal is
the only working capture path. Usage: pmshot.py OUT.png"""
import sys
import time

import dbus

out = sys.argv[1]
bus = dbus.SessionBus()
obj = bus.get_object("org.freedesktop.portal.Desktop",
                     "/org/freedesktop/portal/desktop")
iface = dbus.Interface(obj, "org.freedesktop.portal.Screenshot")

state = {}

token = iface.Screenshot("", {"interactive": dbus.Boolean(False)})


def on_response(uri, options):
    state["uri"] = str(uri)


req = bus.get_object("org.freedesktop.portal.Desktop", token)
sig = req.connect_to_signal("Response", on_response)

deadline = time.time() + 20
while time.time() < deadline and "uri" not in state:
    bus.process_events(block=True, max_replies=10)
sig.remove()

if "uri" not in state:
    print("FAILED: no portal response", file=sys.stderr)
    sys.exit(1)

src = state["uri"].replace("file://", "")
with open(src, "rb") as f:
    data = f.read()
with open(out, "wb") as f:
    f.write(data)
print("saved", out)
