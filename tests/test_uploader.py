"""Uploader tests (rich editor phase 2).

Tier-1: pure functions only — imeta tag building, header-only image
dimension sniffing, response normalization, size gate. No network, no gi
(settings_get must fall back cleanly when Gio is absent/mocked).
"""

import json
import os
import struct
import sys
import tempfile

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from gnostr import uploader


def _png(path, w, h):
    # minimal PNG header + IHDR (magic + length + type + width/height)
    ihdr = struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr)


def _gif(path, w, h):
    with open(path, "wb") as f:
        f.write(b"GIF89a" + struct.pack("<HH", w, h) + b"\x00" * 10)


def test_image_dimensions_png_gif():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "a.png")
        _png(p, 800, 600)
        assert uploader.image_dimensions(p) == (800, 600)
        g = os.path.join(d, "a.gif")
        _gif(g, 320, 240)
        assert uploader.image_dimensions(g) == (320, 240)


def test_image_dimensions_unknown_returns_none():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "a.txt")
        with open(p, "w") as f:
            f.write("not an image")
        assert uploader.image_dimensions(p) is None


def test_imeta_tag_full():
    tag = uploader.imeta_tag(
        {
            "url": "https://cdn/x.png",
            "mime": "image/png",
            "dim": "800x600",
            "sha256": "ab" * 32,
        }
    )
    assert tag == [
        "imeta",
        "url=https://cdn/x.png",
        "m=image/png",
        "dim=800x600",
        "x=" + "ab" * 32,
    ]


def test_imeta_tag_requires_url_and_mime():
    assert uploader.imeta_tag({"url": "u", "mime": ""}) is None
    assert uploader.imeta_tag({"url": "", "mime": "image/png"}) is None
    assert uploader.imeta_tag(None) is None


def test_imeta_tag_minimal():
    tag = uploader.imeta_tag({"url": "u", "mime": "video/mp4"})
    assert tag == ["imeta", "url=u", "m=video/mp4"]


def test_parse_result_nostrbuild_shape():
    obj = {
        "data": {
            "url": "https://nostr.build/i/x.jpg",
            "type": "image/jpeg",
            "payload": {"width": 1024, "height": 768},
            "sha256": "ff" * 32,
        }
    }
    r = uploader._parse_result(obj)
    assert r["url"] == "https://nostr.build/i/x.jpg"
    assert r["dim"] == "1024x768"
    assert r["mime"] == "image/jpeg"


def test_parse_result_blossom_shape():
    obj = {"url": "https://cdn/y.png", "sha256": "aa" * 32, "type": "image/png"}
    r = uploader._parse_result(obj)
    assert r["url"] == "https://cdn/y.png"
    assert r["dim"] is None
    assert r["mime"] == "image/png"


def test_parse_result_rejects_garbage():
    for bad in (None, [], {"nope": 1}):
        try:
            uploader._parse_result(bad)
            raise AssertionError(f"should have raised for {bad!r}")
        except uploader.UploadError:
            pass


def test_size_gate_refuses_before_network(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "big.bin")
        with open(p, "wb") as f:
            f.write(b"\0" * 3 * 1024 * 1024)
        # settings_get fallback is 20 MB; force a tiny cap via monkeypatch
        monkeypatch.setattr(
            uploader, "settings_get", lambda k, fb: 1 if k == "upload-max-mb" else fb
        )
        try:
            uploader.upload_file(p)
            raise AssertionError("should have raised UploadError")
        except uploader.UploadError as e:
            assert "MB upload limit" in str(e)


def test_missing_file_raises():
    try:
        uploader.upload_file("/nonexistent/x.png")
        raise AssertionError("should have raised")
    except uploader.UploadError:
        pass


def test_settings_get_falls_back_without_gi():
    # Under the mocked-gi conftest (or no gi at all), every key must return
    # the fallback rather than raising — the composer must never break on
    # a missing schema.
    assert uploader.settings_get("upload-backend", "nostr.build") is not None
    assert uploader.settings_get("upload-max-mb", 20) is not None


def test_blossom_auth_event_shape():
    ev = uploader.blossom_auth_event("https://cdn")
    assert ev["kind"] == 24242
    assert ["t", "upload"] in ev["tags"]
    assert any(t[0] == "expiration" for t in ev["tags"])


# -- EXIF strip (phase 3) ------------------------------------------------------


def _jpeg(path, with_exif=True, with_sos=False):
    """Minimal marker sequence: SOI [+APP1] [+DQT] [+SOS+scan] EOI."""
    seg = bytearray(b"\xff\xd8")
    if with_exif:
        payload = b"Exif\x00\x00" + b"GPSDATA" * 4
        seg += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    dqt = b"\x00\x11" + b"\x00" * 8
    seg += b"\xff\xdb" + struct.pack(">H", len(dqt) + 2) + dqt
    if with_sos:
        sos = b"\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"
        seg += b"\xff\xda" + struct.pack(">H", len(sos) + 2) + sos
        seg += b"\xde\xad\xbe\xef\xff\x00\xff\xd9"  # fake scan data + EOI
    else:
        seg += b"\xff\xd9"
    with open(path, "wb") as f:
        f.write(bytes(seg))
    return str(path)


def test_strip_exif_removes_app1(tmp_path):
    src = _jpeg(tmp_path / "a.jpg")
    out = uploader.strip_exif(src)
    assert out != src  # temp file created
    data = open(out, "rb").read()
    assert b"GPSDATA" not in data
    assert data[:2] == b"\xff\xd8"
    assert b"\xff\xdb" in data  # DQT survived
    os.unlink(out)


def test_strip_exif_keeps_sos_scan_data(tmp_path):
    src = _jpeg(tmp_path / "b.jpg", with_sos=True)
    out = uploader.strip_exif(src)
    data = open(out, "rb").read()
    assert b"GPSDATA" not in data
    assert b"\xff\xda" in data and b"\xde\xad\xbe\xef" in data  # scan intact
    os.unlink(out)


def test_strip_exif_noop_without_app1(tmp_path):
    src = _jpeg(tmp_path / "c.jpg", with_exif=False)
    assert uploader.strip_exif(src) == src


def test_strip_exif_noop_for_png(tmp_path):
    src = str(tmp_path / "d.png")
    _png(src, 4, 4)
    assert uploader.strip_exif(src) == src


def test_strip_exif_survives_garbage_jpeg(tmp_path):
    # truncated marker data must return the original, never raise
    src = str(tmp_path / "e.jpg")
    with open(src, "wb") as f:
        f.write(b"\xff\xd8\xff\xe1\x00\xffgarbage")
    assert uploader.strip_exif(src) == src
