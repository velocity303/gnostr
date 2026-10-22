"""
YouTube playback via local mux (Oct 2026 regression).

YouTube retired combined A/V progressive streams: `best[ext=mp4]` resolves a
URL that 403s at fetch time, and the remaining HLS muxed variants die in
tsdemux on the Flatpak GStreamer build. The fix: at play time, yt-dlp muxes
the DASH pair to a local file off the main thread, then playbin3 plays the
file:// URI. These tests pin the dispatch logic (no main-thread yt-dlp run,
file:// handoff, failure path) without touching the network.
"""

import os
from unittest.mock import MagicMock, Mock, patch

import pytest


@pytest.fixture(scope="function", autouse=True)
def _reset_video_player_state():
    import sys

    import gnostr.renderer as r

    r.VideoPlayer._cache.clear()
    yield
    r.VideoPlayer._cache.clear()


def test_is_youtube():
    from gnostr.renderer import _is_youtube

    assert _is_youtube("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert _is_youtube("https://youtu.be/dQw4w9WgXcQ")
    assert not _is_youtube("https://vimeo.com/12345")
    assert not _is_youtube("https://example.com/clip.mp4")


def test_youtube_download_returns_newest_file_uri(tmp_path):
    import gnostr.renderer as r

    old = tmp_path / "old.mp4"
    old.write_bytes(b"x")
    os.utime(old, (0, 0))
    new = tmp_path / "new.webm"
    new.write_bytes(b"yy")
    (tmp_path / "partial.part").write_bytes(b"z")

    with (
        patch.object(r, "_youtube_cache_dir", return_value=str(tmp_path)),
        patch.object(r.subprocess, "run") as run,
    ):
        uri = r._youtube_download("https://youtu.be/x")
    assert uri is not None
    assert uri.startswith("file://")
    assert uri.endswith("new.webm")
    assert run.call_args.args[0][:2] == ["yt-dlp", "--no-warnings"]
    assert "timeout" in run.call_args.kwargs


def test_youtube_download_swallows_failures(tmp_path):
    import gnostr.renderer as r

    with (
        patch.object(r, "_youtube_cache_dir", return_value=str(tmp_path)),
        patch.object(
            r.subprocess, "run", side_effect=OSError("yt-dlp not found")
        ),
    ):
        assert r._youtube_download("https://youtu.be/x") is None


def test_fetch_player_youtube_dispatches_off_main_thread():
    """A YouTube URL must go to the executor, never build a pipeline inline."""
    import gnostr.renderer as r

    submitted = []
    with (
        patch.object(
            r.VideoPlayer._executor,
            "submit",
            side_effect=lambda fn, *a, **k: submitted.append((fn, a)),
        ),
        patch.object(r.Gst, "parse_launch") as parse,
    ):
        r.VideoPlayer._fetch_player(
            "https://youtu.be/abc", Mock(), original_url=None, autoplay=True
        )
    assert len(submitted) == 1
    fn, args = submitted[0]
    assert fn is r.VideoPlayer._prepare_youtube
    assert args[0] == "https://youtu.be/abc"
    parse.assert_not_called()


def test_prepare_youtube_builds_from_local_file():
    """After muxing, the pipeline is built from the file:// URI and cached
    under the original watch URL so a second play tap hits the cache."""
    import gnostr.renderer as r

    idle_calls = []
    with (
        patch.object(r, "_youtube_download", return_value="file:///tmp/x.webm"),
        patch.object(r.GLib, "idle_add", side_effect=lambda fn, *a: idle_calls.append((fn, a))),
    ):
        r.VideoPlayer._prepare_youtube(
            "https://youtu.be/abc", Mock(), None, True
        )
    assert len(idle_calls) == 1
    fn, args = idle_calls[0]
    assert fn is r.VideoPlayer._build_and_deliver
    assert args[0] == "file:///tmp/x.webm"  # pipeline built from local file
    assert args[2] == "https://youtu.be/abc"  # original_url for the link-out
    assert args[4] == "https://youtu.be/abc"  # cache_key = watch URL


def test_prepare_youtube_failure_reports_none():
    import gnostr.renderer as r

    cb = Mock()
    with (
        patch.object(r, "_youtube_download", return_value=None),
        patch.object(r.GLib, "idle_add", side_effect=lambda fn, *a: fn(*a)),
    ):
        r.VideoPlayer._prepare_youtube("https://youtu.be/abc", cb, None, True)
    cb.assert_called_once_with(None)


def test_build_and_deliver_caches_under_cache_key():
    """A muxed file built with cache_key must be reachable via the watch URL."""
    import gnostr.renderer as r

    # Gst is a Mock in conftest; parse_launch returns a Mock pipeline so the
    # builder runs to completion and writes the cache entry.
    cb = Mock()
    r.VideoPlayer._build_and_deliver(
        "file:///tmp/x.webm",
        cb,
        original_url="https://youtu.be/abc",
        autoplay=True,
        cache_key="https://youtu.be/abc",
    )
    assert "https://youtu.be/abc" in r.VideoPlayer._cache
