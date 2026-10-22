"""tor-v1-docs guard: the tor setup guide must exist, stay complete, and be
reachable from both the README and the in-app Settings dialog.

Source-level checks (no GTK): the doc is the v1 tor contract (spec
gnostr-tor-onion-support-v1 A.3/A.4) — gnostr never starts tor, the README
teaches the system daemon. The V6 probe landed the empirical Flatpak wording
(shared netns: loopback works out of the box), so a placeholder/TODO left in
the doc means the probe result was reverted.
"""

import os
import re

REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
DOC = os.path.join(REPO, "docs", "tor-setup.md")


def _doc():
    with open(DOC) as f:
        return f.read()


def test_doc_exists():
    assert os.path.exists(DOC)


def test_doc_covers_required_sections():
    text = _doc()
    for needle in [
        "dnf install tor",
        "apt install tor",
        "pacman -S tor",
        "SocksPort 127.0.0.1:9050",  # minimal torrc
        "ss -ltn | grep 9050",  # verification one-liners
        "\\x05\\x01\\x00",  # SOCKS5 greeting probe
        "Orbot",  # Librem 5 note
        "Phosh",
        "systemctl",
    ]:
        assert needle in text, f"tor-setup.md missing: {needle!r}"


def test_doc_has_no_unresolved_probe_todo():
    # V6 probe result landed (2026-10-21): Flatpak shares the host netns.
    text = _doc()
    assert not re.search(r"\bTODO\b", text), "gateway-IP TODO left in tor-setup.md"
    assert (
        "shared network namespace" in text.lower() or "shares the host network" in text
    )


def test_readme_links_the_guide():
    with open(os.path.join(REPO, "README.md")) as f:
        assert "docs/tor-setup.md" in f.read()


def test_settings_dialog_links_the_guide():
    with open(os.path.join(REPO, "src/gnostr/dialogs.py")) as f:
        text = f.read()
    assert "_build_tor_group" in text
    assert "tor-setup.md" in text
    # group must actually be built by the window ctor, not dead code
    assert re.search(r"self\._build_tor_group\(page\)", text)


def test_meson_installs_the_guide():
    # install_data lives in the ROOT meson.build (src/ cannot reference ../..)
    with open(os.path.join(REPO, "meson.build")) as f:
        text = f.read()
    assert "docs/tor-setup.md" in text
    assert "gnostr" in text and "docs" in text
