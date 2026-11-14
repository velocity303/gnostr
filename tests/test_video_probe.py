"""VID-PROBE: tests for the capability probe + CPU measurement harness.

video_probe.py is pure logic + defensive gi calls; conftest mocks gi so the
module imports cleanly. run_probe is exercised against fake window/display/
factory objects; the sampler against a monkeypatched clock. The renderer
wiring is verified by source scan (house pattern — the bus handler is a
closure inside _build_and_deliver and cannot be invoked directly).
"""

import enum
import time
from types import SimpleNamespace

import pytest

import gnostr.video_probe as vp


@pytest.fixture(autouse=True)
def reset_probe_state():
    vp._probe_state.update({"scheduled": False, "done": False, "attempts": 0})
    yield


# ------------------------------------------------------------------ tier


def test_tier_dmabuf():
    assert vp.tier_from_caps("video/x-raw(memory:DMABuf),format=NV12") == "dmabuf"


def test_tier_glmemory_is_nv12():
    assert vp.tier_from_caps("video/x-raw(memory:GLMemory),format=NV12") == "nv12"


def test_tier_system_nv12():
    assert vp.tier_from_caps("video/x-raw,format=NV12") == "nv12"


def test_tier_rgba_is_cpu():
    # The current pipeline's negotiated caps → cpu (card: current tier = cpu).
    assert vp.tier_from_caps("video/x-raw,format=RGBA") == "cpu"


def test_tier_empty_is_cpu():
    assert vp.tier_from_caps("") == "cpu"
    assert vp.tier_from_caps(None) == "cpu"


# ------------------------------------------------------------------ PERF line


def test_perf_host_https():
    assert vp.perf_host("https://cdn.example.com/a/b.mp4?x=1") == "cdn.example.com"


def test_perf_host_file_is_local():
    assert vp.perf_host("file:///home/user/vid.mp4") == "local"


def test_perf_line_format():
    line = vp.perf_line("cpu", "https://vid.example/x.mp4", 1.4159, 3)
    assert line == "🎬[PERF] tier=cpu url=vid.example cpu=1.42 fps_dropped=3"


def test_perf_line_no_dropped():
    line = vp.perf_line("dmabuf", "https://vid.example/x.mp4", 0.5, None)
    assert line == "🎬[PERF] tier=dmabuf url=vid.example cpu=0.50"
    assert "fps_dropped" not in line


# ------------------------------------------------------------------ sampler


class FakePipeline:
    """Plain object standing in for the playbin3 instance."""

    def __init__(self):
        self._measured_caps = "video/x-raw,format=RGBA"
        self._qos_dropped = 2


def test_cpu_seconds_reads_proc():
    # Real call on Linux: must return a plausible non-negative float.
    val = vp._cpu_seconds()
    assert isinstance(val, float)
    assert val >= 0.0


def test_sampler_measures_cores(monkeypatch):
    t0 = time.monotonic()
    # Fake 2.0 cores of burn: cpu seconds grow at 2x wall time.
    monkeypatch.setattr(vp, "_cpu_seconds", lambda: (time.monotonic() - t0) * 2.0)
    s = vp.CpuSampler("https://x.example/v.mp4", interval=0.02)
    s.start()
    s.start()  # idempotent
    time.sleep(0.12)
    cores = s.stop()
    assert cores is not None
    assert 1.0 <= cores <= 3.0
    assert s.stop() is None  # second stop is a no-op


def test_sampler_stop_without_start():
    assert vp.CpuSampler("u").stop() is None


def test_start_stop_sampler_logs_perf_once(monkeypatch, capsys):
    t0 = time.monotonic()
    monkeypatch.setattr(vp, "_cpu_seconds", lambda: (time.monotonic() - t0) * 1.0)
    p = FakePipeline()
    s = vp.start_sampler(p, "https://vid.example/x.mp4")
    assert s is not None
    assert vp.start_sampler(p, "https://vid.example/x.mp4") is s  # reused
    time.sleep(0.05)
    cores = vp.stop_sampler(p, "https://vid.example/x.mp4")
    assert cores is not None
    out = capsys.readouterr().out
    assert "🎬[PERF] tier=cpu url=vid.example cpu=" in out
    assert "fps_dropped=2" in out
    # Second stop must not double-log.
    assert vp.stop_sampler(p, "https://vid.example/x.mp4") is None
    assert capsys.readouterr().out == ""


# ------------------------------------------------------------------ probe


class _IOMode(enum.IntEnum):
    AUTO = 0
    READ = 1
    MMAP = 2
    DMABUF = 4


class _Gtype:
    name = "GskCairoRenderer"


class _Renderer:
    __gtype__ = _Gtype()


class _Window:
    def get_surface(self):
        return object()

    def get_renderer(self):
        return _Renderer()


class _DmabufFormats:
    def get_n_formats(self):
        return 2

    def contains(self, fmt, modifier):
        return fmt == vp.DRM_FORMAT_NV12 and modifier == vp.DRM_MOD_LINEAR


class _Display:
    def get_dmabuf_formats(self):
        return _DmabufFormats()


class _Spec:
    def get_default_value(self):
        return _IOMode.AUTO


class _Struct:
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name

    def to_string(self):
        return self._name


class _Caps:
    NAMES = ["video/x-raw(memory:DMABuf)", "video/x-raw(memory:GLMemory)"]

    def get_n_structures(self):
        return len(self.NAMES)

    def get_structure(self, i):
        return _Struct(self.NAMES[i])

    def to_string(self):
        return "; ".join(self.NAMES)


class _PadTemplate:
    def get_caps(self):
        return _Caps()


class _Pad:
    def get_pad_template(self):
        return _PadTemplate()


class _Element:
    def __init__(self, sink=False):
        self._sink = sink

    def find_property(self, name):
        return _Spec() if name == "capture-io-mode" else None

    def get_static_pad(self, name):
        return _Pad() if (self._sink and name == "sink") else None

    def set_state(self, state):
        pass


class _Factory:
    def __init__(self, name):
        self._name = name

    def create(self, name):
        return _Element(sink=(self._name == "gtk4paintablesink"))


class _ElementFactory:
    @staticmethod
    def find(name):
        if name in ("v4l2h264dec", "gtk4paintablesink"):
            return _Factory(name)
        return None


@pytest.fixture
def fake_gi(monkeypatch):
    monkeypatch.setattr(
        vp,
        "Gtk",
        SimpleNamespace(GraphicsOffload=object, GraphicsOffloadEnabled=object),
    )
    monkeypatch.setattr(
        vp,
        "Gdk",
        SimpleNamespace(Display=SimpleNamespace(get_default=lambda: _Display())),
    )
    monkeypatch.setattr(
        vp,
        "Gst",
        SimpleNamespace(ElementFactory=_ElementFactory, State=SimpleNamespace(NULL=0)),
    )


def test_run_probe_block(fake_gi, capsys):
    vp.run_probe(_Window())
    out = capsys.readouterr().out
    assert "🎬[PROBE] ── video capability probe ──" in out
    assert "gsk_renderer=GskCairoRenderer" in out
    assert "graphics_offload=Gtk.GraphicsOffload=yes GraphicsOffloadEnabled=yes" in out
    assert "dmabuf_formats=available count=2 nv12_linear=True" in out
    # Enum VALUES are read, not assumed (device-dependent).
    assert "v4l2h264dec[AUTO=0, READ=1, MMAP=2, DMABUF=4]" in out
    assert (
        "first_structs=['video/x-raw(memory:DMABuf)', 'video/x-raw(memory:GLMemory)']"
        in out
    )
    assert "predicted_tier=" in out


def test_ensure_probe_schedules_once(monkeypatch, capsys):
    scheduled = []
    monkeypatch.setattr(
        vp,
        "GLib",
        SimpleNamespace(timeout_add=lambda ms, cb: scheduled.append((ms, cb))),
    )
    w = _Window()
    vp.ensure_probe(w)
    vp.ensure_probe(w)
    vp.ensure_probe(None)
    assert len(scheduled) == 1
    # The deferred tick runs the probe (window is "presented").
    assert scheduled[0][1]() is False
    assert "🎬[PROBE]" in capsys.readouterr().out


def test_probe_tick_retries_unmapped_window(monkeypatch):
    class _Unmapped(_Window):
        def get_surface(self):
            return None

    assert vp._probe_tick(_Unmapped()) is True  # retry requested
    assert vp._probe_state["attempts"] == 1


# ------------------------------------------------------------------ wiring


def test_renderer_wiring_present():
    """Source scan: renderer.py drives the probe + sampler at the right points."""
    import gnostr.renderer as r

    src = open(r.__file__, encoding="utf-8").read()
    assert "from . import video_probe" in src
    # Probe scheduled from the render path (startup).
    assert "video_probe.ensure_probe(window_ref)" in src
    # Sampler opens on the pipeline's own PLAYING transition, closes on stop.
    assert "video_probe.start_sampler(p, media_url)" in src
    assert "video_probe.stop_sampler(p, media_url)" in src
    # Eviction closes the window before set NULL.
    assert "video_probe.stop_sampler(p, old_url)" in src


# ------------------------------------------- VID-FLX1-DMABUF sink-format gate


def test_sink_dmabuf_gate_blocks_nv12less_sink(fake_gi, monkeypatch):
    """FLX1 case: the sink's DMABuf struct is DMA_DRM-only. The v4l2
    decoders export NV12-in-dmabuf, which could NEVER match — the tier
    stalls silently at runtime (black rectangle, no bus error), so the
    gate must read the template and refuse."""
    monkeypatch.setattr(vp, "_sink_dmabuf_nv12_cache", None)
    assert vp._sink_dmabuf_accepts_nv12() is False


def test_sink_dmabuf_gate_allows_nv12_sink(fake_gi, monkeypatch):
    monkeypatch.setattr(
        _Caps,
        "NAMES",
        ["video/x-raw(memory:DMABuf),format=(string)NV12"],
    )
    monkeypatch.setattr(vp, "_sink_dmabuf_nv12_cache", None)
    assert vp._sink_dmabuf_accepts_nv12() is True


def test_dmabuf_gate_requires_sink_nv12(fake_gi, monkeypatch):
    monkeypatch.setattr(vp, "_sink_dmabuf_nv12_cache", None)
    monkeypatch.setattr(vp, "_dmabuf_gate_cache", None)
    monkeypatch.setattr(vp, "nv12_tier_available", lambda: True)
    monkeypatch.setattr(vp, "_nv12_linear_importable", lambda: True)
    monkeypatch.setattr(
        vp,
        "capture_io_mode_value",
        lambda n, nick="mmap": 4 if nick == "dmabuf" else 2,
    )
    # Default fake sink: DMABuf struct without NV12 -> gate closed.
    assert vp.dmabuf_tier_available() is False
    monkeypatch.setattr(vp, "_sink_dmabuf_nv12_cache", True)
    monkeypatch.setattr(vp, "_dmabuf_gate_cache", None)
    assert vp.dmabuf_tier_available() is True
