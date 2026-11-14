"""VID-FLX1-NV12: tests for the NV12-direct tier gate + renderer wiring.

video_probe gate functions are exercised against fake factory/enum objects
(conftest mocks gi). The renderer tier helpers run against the mocked Gst.
The _build_and_deliver wiring is verified by source scan (house pattern — the
build path is a monolith that cannot be invoked under mocks).
"""

from types import SimpleNamespace

import pytest

import gnostr.renderer as renderer
import gnostr.video_probe as vp


@pytest.fixture(autouse=True)
def reset_nv12_gate():
    vp._nv12_gate_cache = None
    yield
    vp._nv12_gate_cache = None


# ------------------------------------------------------------------ fakes


class _Member:
    def __init__(self, value, nick):
        self._value = value
        self.value_nick = nick

    def __int__(self):
        return self._value


class _EnumMeta(type):
    """Makes `list(type(default))` iterate the fake enum members, mirroring
    how PyGObject exposes registered GEnums (the _enum_pairs idiom)."""

    def __iter__(cls):
        return iter(cls._members)


class _IOMode(metaclass=_EnumMeta):
    _members = [
        _Member(0, "auto"),
        _Member(1, "read"),
        _Member(3, "userptr"),
        _Member(7, "dmabuf"),
    ]


class _Spec:
    def __init__(self, default=None):
        self._default = default if default is not None else _IOMode()

    def get_default_value(self):
        return self._default


class _El:
    def __init__(self, spec=None):
        self._spec = spec
        self.nulled = False

    def find_property(self, name):
        return self._spec if name == "capture-io-mode" else None

    def set_state(self, state):
        self.nulled = True

    def get_static_pad(self, name):
        return getattr(self, "_pad", None)


class _Factory:
    def __init__(self, el):
        self._el = el

    def create(self, name):
        return self._el


def _fake_gst(factories, state_null=0):
    return SimpleNamespace(
        ElementFactory=SimpleNamespace(
            find=lambda n: factories.get(n),
        ),
        State=SimpleNamespace(NULL=state_null),
    )


# ------------------------------------------------- capture_io_mode_value


def test_capture_io_mode_reads_mmap_live(monkeypatch):
    # mmap at a NON-STANDARD value: an implementation that hardcoded 2 would
    # return 2 here and fail — the value must come from the live enum.
    monkeypatch.setattr(_IOMode, "_members", [
        _Member(0, "auto"),
        _Member(1, "read"),
        _Member(5, "mmap"),
        _Member(7, "dmabuf"),
    ])
    monkeypatch.setattr(vp, "Gst", _fake_gst({"v4l2h264dec": _Factory(_El(_Spec()))}))
    assert vp.capture_io_mode_value("v4l2h264dec") == 5


def test_capture_io_mode_missing_factory(monkeypatch):
    monkeypatch.setattr(vp, "Gst", _fake_gst({}))
    assert vp.capture_io_mode_value("v4l2h264dec") is None


def test_capture_io_mode_no_property(monkeypatch):
    monkeypatch.setattr(vp, "Gst", _fake_gst({"v4l2h264dec": _Factory(_El(None))}))
    assert vp.capture_io_mode_value("v4l2h264dec") is None


def test_capture_io_mode_nick_absent(monkeypatch):
    monkeypatch.setattr(_IOMode, "_members", [_Member(0, "auto"), _Member(1, "read")])
    monkeypatch.setattr(vp, "Gst", _fake_gst({"v4l2h264dec": _Factory(_El(_Spec()))}))
    assert vp.capture_io_mode_value("v4l2h264dec") is None


# ------------------------------------------- sink NV12 systemmem + gate


class _CapsStr:
    def __init__(self, s):
        self._s = s

    def to_string(self):
        return self._s


class _Tmpl:
    def __init__(self, caps):
        self._caps = caps

    def get_caps(self):
        return self._caps


class _Pad:
    def __init__(self, tmpl):
        self._tmpl = tmpl

    def get_pad_template(self):
        return self._tmpl


def _sink_el(caps_str):
    el = _El()
    el._pad = _Pad(_Tmpl(_CapsStr(caps_str)))
    return el


SINK_CAPS_NV12 = (
    "video/x-raw(memory:DMABuf),format=(string)DMA_DRM; "
    "video/x-raw,format=(string){ I420, NV12, RGBA }"
)
SINK_CAPS_NO_SYS_NV12 = (
    "video/x-raw(memory:DMABuf),format=(string)NV12; "
    "video/x-raw,format=(string){ I420, RGBA }"
)


def test_sink_advertises_nv12_systemmem(monkeypatch):
    monkeypatch.setattr(vp, "Gst", _fake_gst({"gtk4paintablesink": _Factory(_sink_el(SINK_CAPS_NV12))}))
    assert vp._sink_advertises_nv12_systemmem() is True


def test_sink_dmabuf_only_nv12_does_not_count(monkeypatch):
    monkeypatch.setattr(vp, "Gst", _fake_gst(
        {"gtk4paintablesink": _Factory(_sink_el(SINK_CAPS_NO_SYS_NV12))}
    ))
    assert vp._sink_advertises_nv12_systemmem() is False


def test_gate_true_on_flx1_shape(monkeypatch):
    monkeypatch.setattr(vp, "Gst", _fake_gst(
        {
            "v4l2h264dec": _Factory(_El(_Spec())),
            "gtk4paintablesink": _Factory(_sink_el(SINK_CAPS_NV12)),
        }
    ))
    assert vp.nv12_tier_available() is True


def test_gate_false_desktop_no_v4l2(monkeypatch):
    monkeypatch.setattr(vp, "Gst", _fake_gst(
        {"gtk4paintablesink": _Factory(_sink_el(SINK_CAPS_NV12))}
    ))
    assert vp.nv12_tier_available() is False


def test_gate_false_l5_stateless_only(monkeypatch):
    # L5 registers v4l2slh264dec — deliberately NOT in the gate.
    monkeypatch.setattr(vp, "Gst", _fake_gst(
        {
            "v4l2slh264dec": _Factory(_El(_Spec())),
            "gtk4paintablesink": _Factory(_sink_el(SINK_CAPS_NV12)),
        }
    ))
    assert vp.nv12_tier_available() is False


def test_gate_false_sink_lacks_system_nv12(monkeypatch):
    monkeypatch.setattr(vp, "Gst", _fake_gst(
        {
            "v4l2h264dec": _Factory(_El(_Spec())),
            "gtk4paintablesink": _Factory(_sink_el(SINK_CAPS_NO_SYS_NV12)),
        }
    ))
    assert vp.nv12_tier_available() is False


def test_gate_cached(monkeypatch):
    calls = []

    class _Find:
        def find(self, n):
            calls.append(n)
            return None

    monkeypatch.setattr(vp, "Gst", SimpleNamespace(
        ElementFactory=_Find(), State=SimpleNamespace(NULL=0)
    ))
    assert vp.nv12_tier_available() is False
    n = len(calls)
    assert vp.nv12_tier_available() is False
    assert len(calls) == n  # second call served from cache


# ---------------------------------------------------- renderer tier select


def test_select_tier_env_forced(monkeypatch):
    monkeypatch.setenv("GNOSTR_VIDEO_TIER", "cpu")
    assert renderer._select_video_tier() == "cpu"
    monkeypatch.setenv("GNOSTR_VIDEO_TIER", "NV12")
    assert renderer._select_video_tier() == "nv12"


def test_select_tier_auto(monkeypatch):
    monkeypatch.delenv("GNOSTR_VIDEO_TIER", raising=False)
    monkeypatch.setattr(renderer.video_probe, "nv12_tier_available", lambda: True)
    assert renderer._select_video_tier() == "nv12"
    monkeypatch.setattr(renderer.video_probe, "nv12_tier_available", lambda: False)
    assert renderer._select_video_tier() == "cpu"


# ------------------------------------------------- renderer nv12 sink bin


class _Bin:
    def __init__(self):
        self.added = []
        self.pads = []

    def add(self, el):
        self.added.append(el)
        return True

    def add_pad(self, pad):
        self.pads.append(pad)
        return True


class _CapEl:
    def __init__(self):
        self.props = {}
        self.links = []

    def set_property(self, k, v):
        self.props[k] = v

    def get_static_pad(self, name):
        return ("pad", name)

    def link(self, other):
        self.links.append(other)
        return True


class _SinkEl:
    pass


def _gst_for_bin(make_result):
    made = []

    def make(fact, name):
        made.append((fact, name))
        return make_result

    return SimpleNamespace(
        Bin=SimpleNamespace(new=lambda: _Bin()),
        ElementFactory=SimpleNamespace(make=make),
        Caps=SimpleNamespace(from_string=lambda s: ("caps", s)),
        GhostPad=SimpleNamespace(new=lambda name, pad: ("ghost", name, pad)),
    ), made


def test_build_nv12_sink_bin_chain(monkeypatch):
    cap = _CapEl()
    gst, made = _gst_for_bin(cap)
    monkeypatch.setattr(renderer, "Gst", gst)
    sink = _SinkEl()
    bin_ = renderer._build_nv12_sink_bin(sink)
    assert bin_ is not None
    assert made == [("capsfilter", "nv12_direct")]
    assert cap.props["caps"] == ("caps", "video/x-raw,format=NV12")
    assert bin_.added == [cap, sink]
    assert cap.links == [sink]  # capsfilter -> sink, nothing else in the bin
    assert bin_.pads == [("ghost", "sink", ("pad", "sink"))]


def test_build_nv12_sink_bin_no_capsfilter(monkeypatch):
    gst, _ = _gst_for_bin(None)
    monkeypatch.setattr(renderer, "Gst", gst)
    assert renderer._build_nv12_sink_bin(_SinkEl()) is None


# ------------------------------------------------- element-setup wiring


class _Pipeline:
    def __init__(self):
        self.props = {}
        self.handlers = []

    def set_property(self, k, v):
        self.props[k] = v

    def connect(self, name, handler):
        self.handlers.append((name, handler))


class _DecoderEl:
    """Stands in for a freshly created decoder element inside playbin3."""

    def __init__(self, factory_name=None):
        self.set_props = []
        self._factory = factory_name or renderer.video_probe.NV12_TIER_DECODER

    def get_factory(self):
        if self._factory is None:
            return None
        outer = self

        class _F:
            def get_name(self):
                return outer._factory

        return _F()

    def set_property(self, k, v):
        self.set_props.append((k, v))


def test_apply_decoder_io_mode(monkeypatch):
    monkeypatch.setattr(
        renderer.video_probe, "capture_io_mode_value", lambda n: 7
    )
    p = _Pipeline()
    assert renderer._apply_decoder_io_mode(p) == 7
    # playbin3 has no decoder-props property — the ONLY real hook is the
    # element-setup signal (verified live on GStreamer 1.26).
    assert [name for name, _ in p.handlers] == ["element-setup"]
    # Firing the signal with the hardware decoder must apply the live value.
    dec = _DecoderEl()
    p.handlers[0][1](p, dec)
    assert dec.set_props == [("capture-io-mode", 7)]
    # Non-matching elements are left untouched.
    other = _DecoderEl(factory_name="avdec_h264")
    p.handlers[0][1](p, other)
    assert other.set_props == []


def test_apply_decoder_io_mode_unreadable(monkeypatch):
    monkeypatch.setattr(
        renderer.video_probe, "capture_io_mode_value", lambda n: None
    )
    p = _Pipeline()
    assert renderer._apply_decoder_io_mode(p) is None
    assert p.handlers == []  # never guess a number into the pipeline


# ------------------------------------------------- tier label honesty


def test_tier_from_caps_real_qualified_strings():
    # Real Gst.Caps.to_string() qualifies fields: format=(string)NV12.
    # The PERF tier label must reflect what actually ran (acceptance).
    real_nv12 = (
        "video/x-raw, width=(int)1280, height=(int)720, "
        "interlace-mode=(string)progressive, "
        "pixel-aspect-ratio=(fraction)1/1, format=(string)NV12"
    )
    assert vp.tier_from_caps(real_nv12) == "nv12"
    real_rgba = (
        "video/x-raw, width=(int)720, height=(int)406, "
        "format=(string)RGBA"
    )
    assert vp.tier_from_caps(real_rgba) == "cpu"


# ------------------------------------------------- build-path source scan


def _build_src():
    import inspect

    return inspect.getsource(renderer.VideoPlayer._build_and_deliver)


def test_build_path_wires_tiers():
    src = _build_src()
    assert "tier = _select_video_tier()" in src
    assert "_build_nv12_sink_bin(sink)" in src
    assert "_apply_decoder_io_mode(pipeline)" in src
    # cpu capped chain must stay gated behind tier == "cpu" (fallback intact).
    assert 'if tier == "cpu" and _MAX_VIDEO_WIDTH' in src
    # nv12 tier prefers GraphicsOffload, falls back to Gtk.Picture.
    assert "GraphicsOffload" in src


def test_find_video_prefers_tagged_widget():
    tagged = SimpleNamespace()
    tagged._gnostr_video_widget = True
    assert renderer.VideoPlayer._find_video([tagged]) is tagged
