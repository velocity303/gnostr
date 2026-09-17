#!/usr/bin/env python3
"""Live fullscreen capture on Plasma Mobile via KWin ScreenShot2 (D-Bus, fd
passing). Spectacle returns stale bytes there and grim lacks the screencopy
protocol; ScreenShot2 is the working path. Dumps KWin's raw BGRA buffer;
convert to PNG locally with pmshot.py. Usage: pmcap OUT.bgra WxH"""
import os
import sys

import dbus

out = sys.argv[1]
bus = dbus.SessionBus()
obj = bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2")
iface = dbus.Interface(obj, "org.kde.KWin.ScreenShot2")

rfd, wfd = os.pipe()
result = iface.CaptureWorkspace({}, dbus.UnixFD(wfd))
os.close(wfd)

w, h = int(result["width"]), int(result["height"])
bpl = int(result.get("bytes-per-array", w * 4))
chunks = []
while True:
    try:
        b = os.read(rfd, bpl * h)
    except OSError:
        break
    if not b:
        break
    chunks.append(b)
    if sum(len(c) for c in chunks) >= bpl * h:
        break
os.close(rfd)
data = b"".join(chunks)[: bpl * h]
with open(out, "wb") as f:
    f.write(data)
print(f"saved {out} {w}x{h} bpl={bpl} got={len(data)}")
