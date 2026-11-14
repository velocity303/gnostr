"""VID-PROBE: startup capability probe + per-playback CPU measurement harness.

Two independent jobs, neither of which touches the playback path:

1. ``ensure_probe(window)`` — logs ONE ``🎬[PROBE]`` block shortly after the
   window maps: active GSK renderer, Gtk.GraphicsOffload availability,
   Gdk dmabuf formats (NV12+LINEAR?), v4l2 decoder presence with their
   capture-io-mode ENUM VALUES (device-dependent — never assume mmap=2),
   and the gtk4paintablesink sink-pad template caps.

2. ``start_sampler`` / ``stop_sampler`` — a background thread measuring
   process CPU (utime+stime, all threads) over each PLAYING window; on stop
   it logs ``🎬[PERF] tier=<dmabuf|nv12|cpu> url=<host> cpu=<cores> fps_dropped=N``.
   The tier is measured from the caps actually negotiated at the sink pad
   (``pipeline._measured_caps``, written by ``VideoPlayer._inspect_pipeline``),
   not guessed — with the current playbin3+videoconvert pipeline that is
   always ``cpu``.

Every probe section is individually guarded: a missing API on one device
must never take down the block on another.
"""

import os
import struct
import threading
import time
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
try:
    gi.require_version("Gst", "1.0")
except Exception:
    pass
from gi.repository import GLib, Gdk, Gtk, Gst

# DRM fourcc 'NV12' (little-endian) — what Gdk.DmabufFormats reports.
DRM_FORMAT_NV12 = struct.unpack("<I", b"NV12")[0]
DRM_MOD_LINEAR = 0

# v4l2 decoder candidates (mainline stateless + legacy vendor names).
_V4L2_DECODERS = (
    "v4l2slh264dec",
    "v4l2slh265dec",
    "v4l2slvp8dec",
    "v4l2slvp9dec",
    "v4l2h264dec",
    "v4l2h265dec",
)

# ---------------------------------------------------------------- tier / line


def tier_from_caps(caps_str):
    """Classify the negotiated sink caps into a tier label.

    memory:DMABuf → dmabuf (zero-copy); memory:GLMemory or plain NV12 →
    nv12 (GPU or CPU-side NV12 upload path); anything else (RGBA system
    memory — today's videoconvert output) → cpu.
    """
    s = caps_str or ""
    if "memory:DMABuf" in s:
        return "dmabuf"
    # Real Gst.Caps.to_string() qualifies the field: `format=(string)NV12`.
    # Match the bare fourcc so real negotiated caps classify correctly.
    if "memory:GLMemory" in s or "NV12" in s:
        return "nv12"
    return "cpu"


def perf_host(url):
    """URL → host for the PERF line (file:// and friends → 'local')."""
    try:
        host = urlparse(url).netloc
        if host:
            return host
    except Exception:
        pass
    return "local"


def perf_line(tier, url, cores, fps_dropped=None):
    line = f"🎬[PERF] tier={tier} url={perf_host(url)} cpu={cores:.2f}"
    if fps_dropped is not None:
        line += f" fps_dropped={fps_dropped}"
    return line


# ---------------------------------------------------------------- CPU sampler

_CLK_TCK = 100
try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK") or 100
except Exception:
    pass


def _cpu_seconds():
    """Process-wide CPU seconds (utime+stime across ALL threads).

    /proc/self/stat's utime/stime are thread-group aggregates, so the
    GStreamer streaming/decode threads count — that is exactly the cost we
    are measuring. os.times() is the fallback outside /proc sandboxes.
    """
    try:
        with open("/proc/self/stat", "rb") as f:
            # comm may contain spaces/parens — split after the LAST ')'.
            fields = f.read().rsplit(b")", 1)[1].split()
        # fields[0] is state (field 3); utime is field 14 → index 11.
        return (int(fields[11]) + int(fields[12])) / _CLK_TCK
    except Exception:
        t = os.times()
        return t.user + t.system


class CpuSampler:
    """Background utime+stime delta over a playback window → cores consumed.

    start()/stop() are idempotent; stop() returns cores (2 dp) or None if
    the window never started. The thread only samples once a second — the
    final delta is taken at stop(), so the number is exact for the window.
    """

    def __init__(self, url, interval=1.0):
        self.url = url
        self.interval = interval
        self.running = False
        self._stop_evt = threading.Event()
        self._thread = None
        self._t0 = 0.0
        self._c0 = 0.0
        self.last_cores = None

    def start(self):
        if self.running:
            return
        self._t0 = time.monotonic()
        self._c0 = _cpu_seconds()
        self._stop_evt.clear()
        self.running = True
        self._thread = threading.Thread(
            target=self._loop, name="gnostr-cpu-sampler", daemon=True
        )
        self._thread.start()

    def _loop(self):
        while not self._stop_evt.wait(self.interval):
            self.last_cores = self._cores(time.monotonic(), _cpu_seconds())

    def _cores(self, t1, c1):
        wall = max(t1 - self._t0, 1e-3)
        return round((c1 - self._c0) / wall, 2)

    def stop(self):
        if not self.running:
            return None
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1.5)
        self.running = False
        self.last_cores = self._cores(time.monotonic(), _cpu_seconds())
        return self.last_cores


def start_sampler(pipeline, url):
    """Attach (or reuse) the CPU sampler for this pipeline's PLAYING window."""
    try:
        sampler = getattr(pipeline, "_cpu_sampler", None)
        if sampler is None:
            sampler = CpuSampler(url)
            pipeline._cpu_sampler = sampler
        if not sampler.running:
            sampler.start()
        return sampler
    except Exception:
        return None


def stop_sampler(pipeline, url):
    """Stop the sampler and log the 🎬[PERF] line. No-op if not running."""
    try:
        sampler = getattr(pipeline, "_cpu_sampler", None)
        if sampler is None or not sampler.running:
            return None
        cores = sampler.stop()
        if cores is None:
            return None
        tier = tier_from_caps(getattr(pipeline, "_measured_caps", "") or "")
        dropped = getattr(pipeline, "_qos_dropped", None)
        print(perf_line(tier, url, cores, dropped))
        return cores
    except Exception:
        return None


# ------------------------------------------------------------------- probe

_probe_state = {"scheduled": False, "done": False, "attempts": 0}


def ensure_probe(window):
    """Schedule the one-shot 🎬[PROBE] block (first render call wins).

    Deferred ~1.5 s so the window is mapped/presented — get_renderer()
    needs a surface and get_surface() is None before present.
    """
    if window is None or _probe_state["scheduled"]:
        return
    _probe_state["scheduled"] = True
    try:
        GLib.timeout_add(1500, lambda w=window: _probe_tick(w) or False)
    except Exception:
        pass


def _probe_tick(window):
    """Timeout callback: run the probe; retry while the window is unmapped."""
    if _probe_state["done"] or _probe_state["attempts"] >= 3:
        _probe_state["done"] = True
        return False
    _probe_state["attempts"] += 1
    try:
        surface = window.get_surface()
    except Exception:
        surface = None
    if surface is None:
        return True  # not presented yet — retry
    run_probe(window)
    _probe_state["done"] = True
    return False


def _renderer_name(window):
    try:
        renderer = window.get_renderer()
        if renderer is None:
            return "none (unrealized)"
        return renderer.__gtype__.name
    except Exception as exc:
        return f"error ({exc.__class__.__name__})"


def _graphics_offload():
    try:
        has_offload = hasattr(Gtk, "GraphicsOffload")
        has_enabled = hasattr(Gtk, "GraphicsOffloadEnabled")
        return (
            f"Gtk.GraphicsOffload={'yes' if has_offload else 'no'} "
            f"GraphicsOffloadEnabled={'yes' if has_enabled else 'no'}"
        )
    except Exception as exc:
        return f"error ({exc.__class__.__name__})"


def _dmabuf_line():
    try:
        display = Gdk.Display.get_default()
        if display is None or not hasattr(display, "get_dmabuf_formats"):
            return "unavailable (no get_dmabuf_formats)"
        fmts = display.get_dmabuf_formats()
        if fmts is None:
            return "unavailable (display has no dmabuf support)"
        try:
            count = fmts.get_n_formats()
        except Exception:
            count = -1
        try:
            # GTK 4.10+: direct membership check (fourcc + modifier).
            nv12_linear = fmts.contains(DRM_FORMAT_NV12, DRM_MOD_LINEAR)
        except Exception:
            try:
                nv12_linear = any(
                    getattr(e, "format", None) == DRM_FORMAT_NV12
                    and getattr(e, "modifier", None) == DRM_MOD_LINEAR
                    for e in fmts.get_formats()
                )
            except Exception:
                nv12_linear = "unknown"
        return f"available count={count} nv12_linear={nv12_linear}"
    except Exception as exc:
        return f"error ({exc.__class__.__name__})"


def _enum_pairs(spec):
    """capture-io-mode enum as 'nick=value' pairs. The enum class is reached
    through the param-spec default (PyGObject wraps registered enums as
    iterable classes); device-dependent values are READ, never assumed."""
    try:
        default = spec.get_default_value()
        members = list(type(default))
        return ", ".join(
            f"{getattr(m, 'value_nick', getattr(m, 'name', '?'))}={int(m)}"
            for m in members
        )
    except Exception:
        return f"(values unreadable, default={spec.get_default_value()!r})"


def _v4l2_line():
    found = []
    try:
        for name in _V4L2_DECODERS:
            factory = Gst.ElementFactory.find(name)
            if factory is None:
                continue
            detail = "no capture-io-mode"
            try:
                el = factory.create(name)
                spec = el.find_property("capture-io-mode") if el else None
                if spec is not None:
                    detail = _enum_pairs(spec)
                if el is not None:
                    el.set_state(Gst.State.NULL)
            except Exception:
                pass
            found.append(f"{name}[{detail}]")
    except Exception as exc:
        return f"error ({exc.__class__.__name__})"
    return ", ".join(found) if found else "none registered"


def _paintablesink_line():
    try:
        factory = Gst.ElementFactory.find("gtk4paintablesink")
        if factory is None:
            return "gtk4paintablesink NOT in runtime"
        el = factory.create("vid-probe-sink")
        if el is None:
            return "gtk4paintablesink present but not instantiable"
        try:
            pad = el.get_static_pad("sink")
            tmpl = pad.get_pad_template() if pad is not None else None
        finally:
            el.set_state(Gst.State.NULL)
        if tmpl is None:
            return "gtk4paintablesink sink template missing"
        try:
            caps = tmpl.get_caps()
        except Exception:
            caps = getattr(tmpl, "caps", None)
        if caps is None:
            return "gtk4paintablesink sink template has no caps"
        try:
            n = caps.get_n_structures()
        except Exception:
            n = caps.get_size()
        names = [caps.get_structure(i).get_name() for i in range(min(2, n))]
        return f"first_structs={names} full={caps.to_string()[:160]}"
    except Exception as exc:
        return f"error ({exc.__class__.__name__}: {exc})"


def _predicted_tier(renderer, dmabuf_line, v4l2_line):
    """Informational only — the PERF line's tier is measured from negotiated
    caps, this is the pre-playback best guess from probe facts."""
    try:
        nv12_ok = "nv12_linear=True" in dmabuf_line
        hw = v4l2_line != "none registered" and "error" not in v4l2_line
        if "vulkan" in renderer.lower():
            return "cpu (vulkan GSK: no GL context for sink upload)"
        if nv12_ok and hw:
            return "dmabuf (hardware decoder + NV12 LINEAR dmabuf import)"
        if hw and "gl" in renderer.lower():
            return "nv12 (hardware decoder + GL upload)"
        return "cpu"
    except Exception:
        return "cpu"


# --------------------------------------------------------------- VID-FLX1-NV12
# NV12-direct tier gate. VID-PROBE verified gtk4paintablesink's SystemMemory
# structs include NV12 — on a device with the stateful v4l2h264dec (FLX1's
# MediaTek) the renderer can hand NV12 straight to the sink: no videoconvert,
# no full-frame RGBA memcpy. Enum values are READ live from the property spec,
# never hardcoded (card rule: "do not hardcode 2").

NV12_TIER_DECODER = "v4l2h264dec"
# The gate matches ONLY the stateful decoder on purpose: L5's stateless
# v4l2slh264dec has its own tuned capped-cpu tier and the card requires
# "Desktop/L5 unaffected".
_NV12_GATE_DECODERS = (NV12_TIER_DECODER,)

_nv12_gate_cache = None


def capture_io_mode_value(element_name, nick="mmap"):
    """Live enum value of `nick` in the element's capture-io-mode property.
    Returns None when the property/enum/nick is unreadable — callers must then
    leave the property at its driver default rather than guess a number."""
    try:
        factory = Gst.ElementFactory.find(element_name)
        if factory is None:
            return None
        el = factory.create(f"nv12-gate-{element_name}")
        if el is None:
            return None
        try:
            spec = el.find_property("capture-io-mode")
            if spec is None:
                return None
            default = spec.get_default_value()
            for member in list(type(default)):
                if getattr(member, "value_nick", None) == nick:
                    return int(member)
        finally:
            try:
                el.set_state(Gst.State.NULL)
            except Exception:
                pass
    except Exception:
        return None
    return None


def _sink_advertises_nv12_systemmem():
    """True when gtk4paintablesink's sink template carries a plain
    video/x-raw struct (no memory: qualifier — DMABuf/GLMemory structs do
    not count) that includes NV12."""
    try:
        factory = Gst.ElementFactory.find("gtk4paintablesink")
        if factory is None:
            return False
        el = factory.create("nv12-gate-sink")
        if el is None:
            return False
        try:
            pad = el.get_static_pad("sink")
            tmpl = pad.get_pad_template() if pad is not None else None
        finally:
            try:
                el.set_state(Gst.State.NULL)
            except Exception:
                pass
        if tmpl is None:
            return False
        try:
            caps = tmpl.get_caps()
        except Exception:
            caps = getattr(tmpl, "caps", None)
        if caps is None:
            return False
        for s in caps.to_string().split(";"):
            if "video/x-raw" in s and "memory:" not in s and "NV12" in s:
                return True
    except Exception:
        return False
    return False


def nv12_tier_available():
    """Whether this device qualifies for the NV12-direct tier (cached — the
    factory registry and pad templates are static once Gst.init has run).
    Desktop: no v4l2 decoder -> False. L5: stateless decoder only -> False.
    FLX1: v4l2h264dec + system-memory NV12 sink caps -> True."""
    global _nv12_gate_cache
    if _nv12_gate_cache is None:
        ok = False
        try:
            if any(
                Gst.ElementFactory.find(n) is not None for n in _NV12_GATE_DECODERS
            ):
                ok = _sink_advertises_nv12_systemmem()
        except Exception:
            ok = False
        _nv12_gate_cache = ok
    return _nv12_gate_cache


def run_probe(window):
    """Log the one-shot 🎬[PROBE] block. Safe to call directly (tests)."""
    # ElementFactory.find needs the plugin registry loaded. Gst.init is
    # idempotent — the app already did this on the playback path; this only
    # matters when the probe runs standalone (device verification runs).
    try:
        if not Gst.is_initialized():
            Gst.init(None)
    except Exception:
        pass
    renderer = _renderer_name(window)
    offload = _graphics_offload()
    dmabuf = _dmabuf_line()
    v4l2 = _v4l2_line()
    sink_caps = _paintablesink_line()
    predicted = _predicted_tier(renderer, dmabuf, v4l2)
    print("🎬[PROBE] ── video capability probe ──")
    print(f"🎬[PROBE] gsk_renderer={renderer}")
    print(f"🎬[PROBE] graphics_offload={offload}")
    print(f"🎬[PROBE] dmabuf_formats={dmabuf}")
    print(f"🎬[PROBE] v4l2_decoders={v4l2}")
    print(f"🎬[PROBE] gtk4paintablesink_sink={sink_caps}")
    print(f"🎬[PROBE] predicted_tier={predicted}")
