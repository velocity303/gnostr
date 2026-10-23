"""Phone-form-factor window sizing (FLX1 blank-band fix).

detect_display_metrics() must maximize the window on narrow monitors:
phosh clamps the 950x700 default to a partial-height window, leaving a
blank band below the app (sidebar footer appeared mid-screen).

MainWindow is a Mock base under the conftest gi mocks, so the real method
is extracted from source via ast (house pattern, test_dm_ui_wiring.py).
"""
import ast
import os
import types
from unittest.mock import MagicMock, Mock

import pytest

_MAIN_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "main.py"
    )
)


def _detect_metrics_fn():
    src = open(_MAIN_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow"
    )
    fn = next(
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "detect_display_metrics"
    )
    code = compile(ast.Module(body=[fn], type_ignores=[]), _MAIN_PATH, "exec")
    ns = {}
    exec(code, ns)
    return ns["detect_display_metrics"]


def _fake_gdk(monitor_width, scale=2):
    geo = Mock()
    geo.width = monitor_width
    geo.height = 1206
    monitor = Mock()
    monitor.get_geometry.return_value = geo
    monitor.get_scale_factor.return_value = scale
    monitors = Mock()
    monitors.get_n_items.return_value = 1
    monitors.get_item.return_value = monitor
    display = Mock()
    display.get_monitors.return_value = monitors
    gdk = MagicMock()
    gdk.Display.get_default.return_value = display
    return gdk


def _self_stub(gdk):
    loader = MagicMock()
    loader.MAX_WIDTH = 800
    win = types.SimpleNamespace(maximize=Mock())
    win.detect_display_metrics = types.MethodType(_detect_metrics_fn(), win)
    return win, {"Gdk": gdk, "ImageLoader": loader}


def test_phone_monitor_maximizes_and_scales_images():
    gdk = _fake_gdk(540, scale=2)
    win, ns = _self_stub(gdk)
    ns["ImageLoader"].MAX_WIDTH = 800
    fn = _detect_metrics_fn()
    fn.__globals__["Gdk"] = gdk
    fn.__globals__["ImageLoader"] = ns["ImageLoader"]
    fn(win)
    win.maximize.assert_called_once()
    assert ns["ImageLoader"].MAX_WIDTH == 1080  # 540 * scale 2


def test_desktop_monitor_does_not_maximize():
    gdk = _fake_gdk(1920)
    win, ns = _self_stub(gdk)
    ns["ImageLoader"].MAX_WIDTH = 42
    fn = _detect_metrics_fn()
    fn.__globals__["Gdk"] = gdk
    fn.__globals__["ImageLoader"] = ns["ImageLoader"]
    fn(win)
    win.maximize.assert_not_called()
    assert ns["ImageLoader"].MAX_WIDTH == 800


def test_display_failure_falls_back_to_800():
    gdk = MagicMock()
    gdk.Display.get_default.side_effect = RuntimeError("no display")
    win, ns = _self_stub(gdk)
    ns["ImageLoader"].MAX_WIDTH = 42
    fn = _detect_metrics_fn()
    fn.__globals__["Gdk"] = gdk
    fn.__globals__["ImageLoader"] = ns["ImageLoader"]
    fn(win)
    win.maximize.assert_not_called()
    assert ns["ImageLoader"].MAX_WIDTH == 800
