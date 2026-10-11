#!/usr/bin/env python3
"""State-aware Gnostr phone driver.

Prints the active page name from the AT-SPI tree (the NavigationPage whose
grouping is named, ignoring the always-present sidebar 'Menu' page), so taps
can be sequenced against real state instead of guesses.

Usage:
  phone_nav.py state
  phone_nav.py open_menu        # tap hamburger, verify menu page present
  phone_nav.py tap_messages     # tap Messages row center, print resulting page
"""
import subprocess
import sys
import time

import pyatspi

HAMBURGER = (45, 109)
MESSAGES = (360, 716)


def app():
    for a in pyatspi.Registry.getDesktop(0):
        try:
            if (a.name or "").lower().startswith("gnostr"):
                return a
        except Exception:
            pass
    return None


def groupings(node, depth=0, out=None):
    if out is None:
        out = []
    if depth > 16:
        return out
    try:
        if node.getRoleName() == "grouping" and (node.name or "").strip():
            out.append(node.name.strip())
    except Exception:
        return out
    try:
        for i in range(node.childCount):
            groupings(node.getChildAtIndex(i), depth + 1, out)
    except Exception:
        pass
    return out


def node_count(node, n=0, depth=0):
    if depth > 16:
        return n
    n += 1
    try:
        for i in range(node.childCount):
            n = node_count(node.getChildAtIndex(i), n, depth + 1)
    except Exception:
        pass
    return n


def state():
    a = app()
    if not a:
        return None, []
    return node_count(a), groupings(a)


def tap(x, y, hold=150):
    subprocess.run(
        ["sudo", "python3", "/home/purism/lab/touchinj.py", "tap", str(x), str(y), str(hold)],
        check=True,
    )
    time.sleep(2.5)


def show(tag):
    n, g = state()
    print(f"[{tag}] nodes={n} pages={sorted(set(g))}")
    return n, g


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "state"
    n, g = show("before")
    if cmd == "state":
        pass
    elif cmd == "open_menu":
        if "Menu" not in g:
            tap(*HAMBURGER)
            n, g = show("after-hamburger")
        print("MENU_OPEN" if "Menu" in g else "MENU_CLOSED")
    elif cmd == "tap_messages":
        if "Menu" not in g:
            tap(*HAMBURGER)
            n, g = show("after-hamburger")
            if "Menu" not in g:
                sys.exit("could not open menu")
        tap(*MESSAGES)
        show("after-messages")
