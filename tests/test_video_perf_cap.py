"""Perf pass 2: the delivered-frame cap defaults to the Librem 5 physical feed
width (720x406) and is overridable via GNOSTR_VIDEO_CAP for on-device A/B.
_parse_cap reads the env at call time — pure string logic, safe under the gi
mocks.

Do NOT importlib.reload(renderer) here: reload rebinds the module's Gst/Gtk
names to fresh mocks and leaks into later tests in the same pytest process
(the viewport-pause tests broke when run after a reload)."""

from gnostr.renderer import _parse_cap


def test_default_cap_matches_physical_feed_width(monkeypatch):
    monkeypatch.delenv("GNOSTR_VIDEO_CAP", raising=False)
    assert _parse_cap() == (720, 406)


def test_env_override_parses(monkeypatch):
    monkeypatch.setenv("GNOSTR_VIDEO_CAP", "1280x720")
    assert _parse_cap() == (1280, 720)


def test_env_override_zero_disables_cap(monkeypatch):
    monkeypatch.setenv("GNOSTR_VIDEO_CAP", "0x0")
    assert _parse_cap() == (0, 0)


def test_negative_override_clamps_to_disabled(monkeypatch):
    monkeypatch.setenv("GNOSTR_VIDEO_CAP", "-5x-9")
    assert _parse_cap() == (0, 0)


def test_garbage_override_falls_back_to_default(monkeypatch):
    for junk in ("", "abc", "720", "x", "720x"):
        monkeypatch.setenv("GNOSTR_VIDEO_CAP", junk)
        assert _parse_cap() == (720, 406), junk


def test_module_defaults_match_parse_cap_default(monkeypatch):
    # The module-level constants are what the pipeline builder reads; with no
    # env set they must equal the no-env default.
    import gnostr.renderer as r

    monkeypatch.delenv("GNOSTR_VIDEO_CAP", raising=False)
    assert (r._MAX_VIDEO_WIDTH, r._MAX_VIDEO_HEIGHT) == _parse_cap()
