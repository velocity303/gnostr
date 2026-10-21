# src/gnostr/uploader.py
"""Media upload: local file -> {url, mime, dim, sha256} (rich editor phase 2).

Scope: editor-gtk-scope (rich editor spec). NIP-96 is `unrecommended`
(deprecated for NIP-B7/Blossom) but nostr.build et al still dominate deployed
hosts, so both transports exist and the backend is GSettings-configurable
(upload-backend / upload-server-url / upload-max-mb) — future-proofing against
nostr.build capacity/funding changes.

Contract: upload_file(path) -> {url, mime, dim, sha256} or raises
UploadError(reason). Callers run this OFF the GTK main loop (threading.Thread,
house pattern from service/dm_service.py) and marshal results back via
GLib.idle_add.

Image dimensions are read from the file header only (pure stdlib — no Pillow
in the GNOME runtime, verified on-device). Video dims stay None; the renderer
imeta enrichment is v2 scope.
"""

import base64
import hashlib
import json
import mimetypes
import os
import struct
import urllib.error
import urllib.request

APP_ID = "tech.livingonlinux.gnostr"
# Blossom is the default: nostr.build's legacy anonymous multipart endpoint
# (/api/v2/nostrbuild) now answers 405 Method Not Allowed — verified live
# 2026-10-20 — so the old default silently failed every attach. Blossom
# (BUD-02 PUT + BUD-01 auth) works; blossom.primal.net verified live.
DEFAULT_BACKEND = "blossom"
DEFAULT_SERVER_URL = "https://blossom.primal.net"
DEFAULT_MAX_MB = 20

# Uploads run on a worker thread; generous but bounded (mobile networks).
TIMEOUT_S = 60


class UploadError(Exception):
    """User-presentable failure reason (safe to show in a toast)."""


def _settings():
    """Gio.Settings for the app schema, or None when unavailable (dev run,
    unpackaged tree, missing compiled schema)."""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        return Gio.Settings.new(APP_ID)
    except Exception:
        return None


def settings_get(key, fallback):
    """GSettings read with a hard fallback: unpackaged/dev runs and a missing
    compiled schema must never break the composer."""
    s = _settings()
    if s is None:
        return fallback
    try:
        if key == "upload-backend":
            return s.get_string("upload-backend") or fallback
        if key == "upload-server-url":
            return s.get_string("upload-server-url") or fallback
        if key == "upload-max-mb":
            return s.get_int("upload-max-mb")
    except Exception:
        return fallback
    return fallback


def settings_set(key, value):
    """GSettings write for the in-app Media Upload preferences. Returns True
    on success; False when the schema is unavailable (dev run) so the UI can
    tell the user the change could not be persisted."""
    s = _settings()
    if s is None:
        return False
    try:
        if key == "upload-max-mb":
            s.set_int(key, int(value))
        else:
            s.set_string(key, str(value))
        return True
    except Exception:
        return False


# -- header-only dimension sniffing (no Pillow) ------------------------------


def image_dimensions(path):
    """(w, h) from the file header for PNG/JPEG/GIF/WebP, else None."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            if head.startswith(b"\x89PNG\r\n\x1a\n"):
                w, h = struct.unpack(">II", head[16:24])
                return (w, h)
            if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
                w, h = struct.unpack("<HH", head[6:10])
                return (w, h)
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                fmt = head[12:16]
                if fmt == b"VP8X" and len(head) >= 30:
                    w = int.from_bytes(head[24:27], "little") + 1
                    h = int.from_bytes(head[27:30], "little") + 1
                    return (w, h)
                if fmt == b"VP8L" and len(head) >= 25:
                    bits = int.from_bytes(head[21:25], "little")
                    return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
                if fmt == b"VP8 " and len(head) >= 30:
                    w = struct.unpack("<H", head[26:28])[0] & 0x3FFF
                    h = struct.unpack("<H", head[28:30])[0] & 0x3FFF
                    return (w, h)
            if head[:2] == b"\xff\xd8":  # JPEG: walk segment markers
                f.seek(2)
                while True:
                    marker = f.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    while marker[1] == 0xFF:
                        marker = f.read(1)
                        if not marker:
                            return None
                        marker = bytes([0xFF, marker[0]])
                    m = marker[1]
                    if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                        continue
                    size = struct.unpack(">H", f.read(2))[0]
                    if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                        seg = f.read(5)
                        h, w = struct.unpack(">HH", seg[1:5])
                        return (w, h)
                    f.seek(size - 2, os.SEEK_CUR)
    except Exception:
        return None
    return None


# -- EXIF strip (privacy) -------------------------------------------------------


def strip_exif(path):
    """Return a path to upload for `path`, with JPEG EXIF/XMP (APP1) segments
    removed — phone photos embed GPS coordinates there. Non-JPEG files and
    JPEGs without APP1 are returned unchanged (no temp file created). On any
    parse surprise the ORIGINAL path is returned: never block an upload over
    metadata surgery.

    Caller is responsible for deleting a returned temp file (a different path
    than the input means we created it).
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return path
    if data[:2] != b"\xff\xd8":  # not a JPEG
        return path
    out = bytearray(data[:2])
    i = 2
    stripped = False
    n = len(data)
    try:
        while i < n - 1:
            if data[i] != 0xFF:
                return path  # not a marker boundary — bail, upload original
            # run of 0xFF padding bytes
            j = i
            while j < n and data[j] == 0xFF:
                j += 1
            if j >= n:
                return path
            m = data[j]
            marker = data[i : j + 1]
            j += 1
            if m == 0xD9:  # EOI: copy the rest verbatim
                out += data[i:]
                break
            if m == 0xDA:  # SOS: entropy-coded data follows; every APP1
                out += data[i:]  # segment precedes SOS, so stop parsing
                break
            if m == 0x01 or 0xD0 <= m <= 0xD7:  # standalone markers
                out += marker
                i = j
                continue
            if j + 2 > n:
                return path
            (size,) = struct.unpack(">H", data[j : j + 2])
            if size < 2 or j + size > n:
                return path  # corrupt/truncated — upload original
            segment = data[i : j + size]
            if m in (0xE1, 0xE5):  # APP1 = EXIF/XMP, APP5 = priv markers
                stripped = True
            else:
                out += segment
            i = j + size
    except (IndexError, struct.error):
        return path
    if not stripped:
        return path
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix="gnostr-", suffix=".jpg")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(bytes(out))
    except OSError:
        os.unlink(tmp)
        return path
    return tmp


# -- auth (BUD-01) --------------------------------------------------------------


def blossom_auth_event(server_url, kind=24242):
    """Unsigned BUD-01 auth event. kind 24242; content is the human-readable
    request description BUD-01 asks for; tags: t=upload, x=<sha256 of the
    file> (appended by the caller), expiration. Callers sign it with the
    normal signing funnel (client._sign) — works with remote backends too."""
    import time

    return {
        "kind": kind,
        "created_at": int(time.time()),
        "tags": [
            ["t", "upload"],
            ["expiration", str(int(time.time()) + 600)],
        ],
        "content": f"Upload to {server_url}",
    }


def _auth_header(sign_fn, auth_event):
    signed = sign_fn(auth_event)
    if not signed:
        raise UploadError("Could not sign upload request")
    b64 = base64.b64encode(json.dumps(signed, separators=(",", ":")).encode()).decode()
    return f"Nostr {b64}"


# -- transports ---------------------------------------------------------------


def _multipart(fields, file_field, path, mime):
    """Hand-rolled multipart/form-data (stdlib has no encoder)."""
    boundary = (
        "----gnostr" + hashlib.sha256(path.encode() + os.urandom(8)).hexdigest()[:24]
    )
    parts = []
    for name, value in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            ).encode()
        )
    fname = os.path.basename(path)
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
            f'filename="{fname}"\r\nContent-Type: {mime}\r\n\r\n'
        ).encode()
    )
    with open(path, "rb") as f:
        parts.append(f.read())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def _post_multipart(url, fields, path, mime, extra_headers=None):
    body, boundary = _multipart(fields, "file", path, mime)
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "gnostr/1.0",
            **(extra_headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode())


def _blossom_put(server_url, path, mime, auth_value):
    server = server_url.rstrip("/")
    with open(path, "rb") as f:
        blob = f.read()
    req = urllib.request.Request(
        f"{server}/upload",
        data=blob,
        method="PUT",
        headers={
            "Content-Type": mime,
            "Authorization": auth_value,
            "User-Agent": "gnostr/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode())


def _parse_result(obj):
    """Normalize either transport's response to the contract dict.
    nostr.build v2 returns {data:{url,mv,payload:{width,height},sha256,...}};
    Blossom BUD-02 returns {url,sha256,size,type,...}."""
    if not isinstance(obj, dict):
        raise UploadError("Upload server returned an unexpected response")
    inner = obj.get("data")
    data = inner if isinstance(inner, dict) else obj
    url = data.get("url") or obj.get("url")
    if not url:
        raise UploadError("Upload server returned no URL")
    sha = data.get("sha256") or obj.get("sha256") or ""
    dim = None
    payload = data.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    w = payload.get("width") or data.get("width")
    h = payload.get("height") or data.get("height")
    if w and h:
        dim = f"{w}x{h}"
    mime = data.get("mime") or data.get("type") or obj.get("type") or ""
    return {"url": url, "mime": mime, "dim": dim, "sha256": sha}


# -- public API ---------------------------------------------------------------


def upload_file(path, sign_fn=None):
    """Upload `path` using the configured backend. sign_fn(event)->signed is
    required for Blossom auth (pass client._sign); nostr.build's legacy
    endpoint accepts anonymous uploads and ignores it.

    Returns {url, mime, dim, sha256}; raises UploadError on any failure.
    """
    if not path or not os.path.isfile(path):
        raise UploadError("File not found")
    # Privacy: strip JPEG EXIF (GPS etc.) before anything touches the wire.
    # strip_exif returns the original path when there's nothing to do.
    clean = strip_exif(path)
    try:
        return _upload_clean(clean, sign_fn)
    finally:
        if clean != path:
            try:
                os.unlink(clean)
            except OSError:
                pass


def _upload_clean(path, sign_fn):
    """upload_file() core; `path` is already EXIF-stripped."""
    size = os.path.getsize(path)
    max_mb = settings_get("upload-max-mb", DEFAULT_MAX_MB)
    if max_mb and size > max_mb * 1024 * 1024:
        raise UploadError(f"File is larger than the {max_mb} MB upload limit")
    mime, _ = mimetypes.guess_type(path)
    mime = mime or "application/octet-stream"
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
    sha_hex = sha.hexdigest()

    backend = settings_get("upload-backend", DEFAULT_BACKEND)
    try:
        if backend == "blossom":
            server = settings_get("upload-server-url", DEFAULT_SERVER_URL)
            if sign_fn is None:
                raise UploadError("Blossom upload needs a signer")
            auth_ev = blossom_auth_event(server)
            auth_ev["tags"].append(["x", sha_hex])
            auth = _auth_header(sign_fn, auth_ev)
            obj = _blossom_put(server, path, mime, auth)
        else:
            # nostr.build legacy NIP-96-style endpoint (anonymous upload).
            obj = _post_multipart(
                "https://nostr.build/api/v2/nostrbuild", {}, path, mime
            )
    except UploadError:
        raise
    except urllib.error.HTTPError as e:
        raise UploadError(f"Upload server error ({e.code})")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise UploadError("Upload failed: network error")
    except (ValueError, KeyError):
        raise UploadError("Upload server returned an unexpected response")

    result = _parse_result(obj)
    if not result["dim"]:
        dims = image_dimensions(path)
        if dims:
            result["dim"] = f"{dims[0]}x{dims[1]}"
    result["sha256"] = result["sha256"] or sha_hex
    result["mime"] = result["mime"] or mime
    return result


def imeta_tag(result):
    """NIP-92 imeta tag from an upload_file() result, or None if the required
    fields (url + m) are missing. dim/x are included when known."""
    if not result or not result.get("url") or not result.get("mime"):
        return None
    tag = ["imeta", f"url={result['url']}", f"m={result['mime']}"]
    if result.get("dim"):
        tag.append(f"dim={result['dim']}")
    if result.get("sha256"):
        tag.append(f"x={result['sha256']}")
    return tag
