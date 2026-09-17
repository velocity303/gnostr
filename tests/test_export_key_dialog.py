"""Portrait-fit regression (Librem 5 field bug): every monospace blob label
(export dialog + onboarding wizard) must pass wrap_mode=WORD_CHAR. Plain
WORD wrap never breaks a single long token like an ncryptsec/nsec string, so
the label demanded more width than the phone screen and the text overflowed
off-display. Source-level check: the widget-mock test environment can't
introspect real GTK construction."""

import re

import pytest

SOURCES = [
    "src/gnostr/dialogs.py",
    "src/gnostr/ui/onboarding.py",
]


@pytest.mark.parametrize("path", SOURCES)
def test_monospace_labels_wrap_word_char(path):
    text = open(path).read()
    # every Gtk.Label(...) call that carries css_classes=["monospace"]
    calls = re.findall(
        r"Gtk\.Label\((.*?)\n\s*\)", text, flags=re.S
    )
    mono = [c for c in calls if 'css_classes=["monospace"]' in c]
    assert mono, f"{path}: no monospace Gtk.Label found"
    for c in mono:
        assert "wrap_mode=Pango.WrapMode.WORD_CHAR" in c, (
            f"{path}: monospace label lacks WORD_CHAR wrap:\n{c}"
        )
