#!/usr/bin/env python3
"""Activate a Gnostr sidebar menu row via AT-SPI Action (no touch injection).

Usage: phone_act.py "Messages"
"""
import sys
import time

import pyatspi


def app():
    for a in pyatspi.Registry.getDesktop(0):
        try:
            if (a.name or "").lower().startswith("gnostr"):
                return a
        except Exception:
            pass
    return None


def walk(node, depth=0):
    if depth > 18:
        return
    try:
        yield node
    except Exception:
        return
    try:
        for i in range(node.childCount):
            yield from walk(node.getChildAtIndex(i), depth + 1)
    except Exception:
        pass


def main():
    target = sys.argv[1]
    a = app()
    if not a:
        sys.exit("no gnostr app")
    hits = []
    for n in walk(a):
        try:
            name = (n.name or "").strip()
            if name == target:
                hits.append(n)
        except Exception:
            continue
    print(f"found {len(hits)} node(s) named {target!r}")
    for n in hits:
        role = n.getRoleName()
        acts = None
        try:
            acts = n.queryAction()
        except Exception:
            pass
        print(f"  role={role} actions={'none' if acts is None else [acts.getName(i) for i in range(acts.nActions)]}")
        if acts is not None and acts.nActions > 0:
            acts.doAction(0)
            print("  doAction(0) fired")
            time.sleep(2.5)
            break
    # report resulting page
    names = set()
    for n in walk(app()):
        try:
            if n.getRoleName() == "grouping" and (n.name or "").strip():
                names.add(n.name.strip())
        except Exception:
            pass
    print("pages now:", sorted(names))


main()
