import re
import html
from collections import OrderedDict
import gi
import urllib.request
import threading
import concurrent.futures
from urllib.parse import urlparse
import traceback
import subprocess
import time

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GstVideo", "1.0")
gi.require_version("GstApp", "1.0")
try:
    gi.require_version("Gst", "1.0")
    print("Gst 1.0 required successfully")
except Exception as e:
    print(f"Failed to require Gst 1.0: {e}")
from gi.repository import Gtk, Adw, GLib, Gdk, GdkPixbuf, Pango, Gst, GstApp, Gio

from . import nostr_utils


# Set to True to enable verbose 🎬 debug logging
_DEBUG_VIDEO = False

# Task 10 (measured): videos decode at up to 1080p into a ~600px feed widget.
# Cap the frame size delivered to the sink so decodebin3 scales to fit this box
# first — cuts per-frame copy / GPU-upload cost on low-power mobile. decodebin3
# auto-inserts videoscale to satisfy the sink caps; 0 disables the cap.
_MAX_VIDEO_WIDTH = 1280
_MAX_VIDEO_HEIGHT = 720


def _vlog(msg):
    if _DEBUG_VIDEO:
        print(msg)


class ContentRenderer:
    # Lookbehind (not (?:^|\s)) so the leading whitespace before a link/mention is
    # NOT consumed by the split — keeps inline spacing: "text @user", not "text@user".
    LINK_REGEX = re.compile(r"(?<!\w)((?:https?://|nostr:)[^\s]+)")
    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
    VIDEO_EXTS = {".mp4", ".mov", ".webm", ".gif"}

    @staticmethod
    def is_image_url(url):
        try:
            path = urlparse(url).path.lower()
            return any(path.endswith(ext) for ext in ContentRenderer.IMAGE_EXTS)
        except Exception:
            return False

    @staticmethod
    def is_video_url(url):
        try:
            path = urlparse(url).path.lower()
            return any(path.endswith(ext) for ext in ContentRenderer.VIDEO_EXTS)
        except Exception:
            return False

    @staticmethod
    def _add_video(box, url, window_ref, original_url=None):
        video_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        video_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        video_area.set_halign(Gtk.Align.FILL)
        video_area.set_hexpand(True)
        video_area.set_size_request(-1, 200)

        is_gif = url.lower().endswith(".gif")

        if is_gif:
            # GIFs are small animated images; keep the existing preroll+loop UX.
            spinner = Gtk.Spinner()
            spinner.start()
            spinner.set_halign(Gtk.Align.CENTER)
            spinner.set_valign(Gtk.Align.CENTER)
            spinner.set_vexpand(True)
            video_area.append(spinner)
            VideoPlayer.load_and_play(url, video_area, spinner, window_ref, original_url)
        else:
            # Real videos: lazy-load. Don't build a playbin3 pipeline (and pull
            # the stream) until the user actually taps play — prerolling every
            # video in a feed builds 6+ concurrent pipelines and costs seconds of
            # preroll (measured, Task 10). Show a placeholder "playable video"
            # graphic instead; the pipeline builds on first play.
            placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            placeholder.set_halign(Gtk.Align.CENTER)
            placeholder.set_valign(Gtk.Align.CENTER)
            ph_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
            ph_icon.set_pixel_size(56)
            ph_icon.set_opacity(0.9)
            ph_label = Gtk.Label(label="Play video")
            ph_label.add_css_class("dim-label")
            placeholder.append(ph_icon)
            placeholder.append(ph_label)
            video_area._placeholder = placeholder
            video_area.append(placeholder)

        video_box.append(video_area)

        # Clicking the video frame toggles play/pause (or lazily starts playback).
        click_ctrl = Gtk.GestureClick()
        # CLAIM the click sequence on press so the enclosing PostWidget's
        # open-thread gesture is DENIED for this same event — clicking the video
        # must play/pause only, never navigate into the post. GTK4 lets a
        # descendant gesture claim a sequence to stop parent gestures firing
        # (cooperative sequence-state check documented in GtkGesture).
        click_ctrl.connect(
            "pressed",
            lambda g, n, x, y: g.set_state(Gtk.EventSequenceState.CLAIMED),
        )
        click_ctrl.connect(
            "released",
            lambda c, n, x, y: ContentRenderer._start_playback(
                video_area, url, window_ref, original_url, is_gif
            ),
        )
        video_area.add_controller(click_ctrl)

        # Animated GIFs loop; the seek bar + mute button don't apply — skip them.
        if not is_gif:
            # Position/seek bar with the mute button adjacent to it (no volume UI).
            position_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            position_row.set_margin_start(6)
            position_row.set_margin_end(6)
            position_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
            position_scale.set_range(0, 100)
            position_scale.set_value(0)
            position_scale.set_hexpand(True)
            position_scale.set_draw_value(False)
            position_scale.set_sensitive(False)
            position_scale.connect("value-changed", lambda s: VideoPlayer.seek_to(video_area, s.get_value()))
            position_label = Gtk.Label(label="0:00 / 0:00")
            mute_btn = Gtk.Button(icon_name="audio-volume-high-symbolic")
            mute_btn.set_tooltip_text("Mute/Unmute")
            mute_btn.connect("clicked", lambda b: VideoPlayer.toggle_mute(video_area, mute_btn))
            position_row.append(position_scale)
            position_row.append(position_label)
            position_row.append(mute_btn)
            video_box.append(position_row)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        controls.set_margin_top(6)
        controls.set_margin_bottom(6)
        controls.set_margin_start(6)
        controls.set_margin_end(6)

        # No dedicated play button — the video frame is the play/pause control
        # (lazy-start on first tap, toggle on later taps).

        if original_url and ("youtube.com" in original_url or "youtu.be" in original_url):
            yt_link = Gtk.LinkButton(uri=original_url, label="Open in YouTube")
            yt_link.set_halign(Gtk.Align.END)
            yt_link.set_hexpand(True)
            controls.append(yt_link)

        video_box.append(controls)
        box.append(video_box)

    @staticmethod
    def _start_playback(video_area, url, window_ref, original_url, is_gif=False):
        """Lazy playback bootstrap. If the pipeline is already built, just
        toggle; otherwise remove the placeholder, show a spinner, and build the
        pipeline with autoplay. Keeps preroll cost to videos the user actually
        plays (Task 10 contention fix)."""
        video = VideoPlayer._find_video(video_area)
        if video and getattr(video, "_pipeline", None) is not None:
            VideoPlayer.toggle_play(video_area)
            return
        # First play tap — remove the placeholder if present, swap in a spinner,
        # then build + autoplay.
        ph = getattr(video_area, "_placeholder", None)
        if ph is not None and ph.get_parent() == video_area:
            video_area.remove(ph)
            try:
                delattr(video_area, "_placeholder")
            except Exception:
                pass
        # If a dead video widget (evicted pipeline, _pipeline=None) is still in
        # the container, remove it so the spinner + fresh build replace it.
        if video is not None and video.get_parent() == video_area:
            video_area.remove(video)
        spinner = Gtk.Spinner()
        spinner.start()
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_valign(Gtk.Align.CENTER)
        spinner.set_vexpand(True)
        video_area.append(spinner)
        VideoPlayer.load_and_play(
            url, video_area, spinner, window_ref, original_url, autoplay=True
        )

    @staticmethod
    def render(content, window_ref, post_widget_ref=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        
        if not content:
            error_label = Gtk.Label(label="[No content to display]", xalign=0)
            error_label.add_css_class("dim-label")
            box.append(error_label)
            return box

        try:
            clean_content = html.unescape(content)
            parts = ContentRenderer.LINK_REGEX.split(clean_content)
            # Accumulate (pubkey_or_None, pango_markup) fragments for one inline text
            # label. Profile mentions become small inline @name links; other text is
            # escaped plain text that flows together with them.
            text_fragments = []

            def flush_text():
                if not text_fragments:
                    return
                has_mention = any(pk for pk, _ in text_fragments)
                markup = "".join(frag for _, frag in text_fragments)
                lbl = ContentRenderer._text_label(markup, window_ref)
                if has_mention:
                    lbl.mention_fragments = list(text_fragments)
                    if post_widget_ref:
                        if not hasattr(post_widget_ref, "inline_mention_labels"):
                            post_widget_ref.inline_mention_labels = []
                        post_widget_ref.inline_mention_labels.append(lbl)
                text_fragments.clear()
                box.append(lbl)

            for part in parts:
                if not part:
                    continue

                if ContentRenderer.LINK_REGEX.match(part):
                    clean_part = part.rstrip(".!,?;']}" )
                    trailing = part[len(clean_part) :]

                    if clean_part.startswith("nostr:"):
                        if "nprofile" in clean_part or "npub" in clean_part:
                            hex_pk = ContentRenderer._extract_hex_id(clean_part.split(":", 1)[1] if ":" in clean_part else clean_part)
                            if hex_pk:
                                name = ContentRenderer._mention_name(hex_pk, window_ref)
                                # Decorative inline mention: bold @name link.
                                disp = f'<span weight="bold">@{GLib.markup_escape_text(name)}</span>'
                                text_fragments.append((hex_pk, f'<a href="nostr:{hex_pk}">{disp}</a>'))
                                if trailing:
                                    text_fragments.append((None, GLib.markup_escape_text(trailing)))
                                continue
                            # malformed profile uri -> plain link
                            flush_text()
                            ContentRenderer._add_link(box, clean_part)
                            if trailing:
                                text_fragments.append((None, GLib.markup_escape_text(trailing)))
                            continue
                        # nostr event quote -> block card
                        flush_text()
                        ContentRenderer._add_nostr_card(box, clean_part, window_ref, post_widget_ref)
                        if trailing:
                            text_fragments.append((None, GLib.markup_escape_text(trailing)))
                        continue
                    # http(s) link / media -> block widget
                    flush_text()
                    if ContentRenderer.is_image_url(clean_part):
                        ContentRenderer._add_image(box, clean_part, window_ref)
                    elif ContentRenderer.is_video_url(clean_part):
                        ContentRenderer._add_video(box, clean_part, window_ref)
                    elif "youtube.com/watch" in clean_part or "youtu.be/" in clean_part:
                        raw_stream_url = get_youtube_stream(clean_part)
                        ContentRenderer._add_video(box, raw_stream_url, window_ref, clean_part)
                    else:
                        ContentRenderer._add_link(box, clean_part)
                    if trailing:
                        text_fragments.append((None, GLib.markup_escape_text(trailing)))
                else:
                    text_fragments.append((None, GLib.markup_escape_text(part)))
            flush_text()

        except Exception as e:
            error_label = Gtk.Label(label=f"[Render error: {str(e)[:50]}]", xalign=0)
            error_label.add_css_class("dim-label")
            box.append(error_label)

        return box

    @staticmethod
    def _add_text(box, text):
        label = Gtk.Label(label=text, xalign=0)
        label.set_use_markup(False)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(60)
        box.append(label)

    @staticmethod
    def _text_label(markup, window_ref):
        """Inline text label rendered with Pango markup. nostr: links (profile
        mentions) are routed to the profile view via activate-link."""
        lbl = Gtk.Label(label=markup, xalign=0, use_markup=True)
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_max_width_chars(60)
        lbl.connect("activate-link", lambda l, url: ContentRenderer._on_mention_link(url, window_ref))
        return lbl

    @staticmethod
    def _on_mention_link(url, window_ref):
        if url.startswith("nostr:"):
            hex_pk = url[len("nostr:"):]
            if window_ref and len(hex_pk) == 64 and hasattr(window_ref, "show_profile"):
                window_ref.show_profile(hex_pk)
                return True
        return False

    @staticmethod
    def _mention_name(hex_pk, window_ref):
        name = None
        try:
            if window_ref:
                prof = window_ref.db.get_profile(hex_pk)
                if prof:
                    name = prof.get("display_name") or prof.get("name")
                else:
                    # Profile not cached yet — fetch it so the mention's name/pic
                    # resolves once the kind-0 arrives (TTL-cached, deduped).
                    window_ref.client.fetch_profile(hex_pk)
        except Exception:
            pass
        return name or hex_pk[:8]

    @staticmethod
    def _add_link(box, url, label=None):
        disp = label if label else (url[:47] + "..." if len(url) > 50 else url)
        markup = f'<a href="{GLib.markup_escape_text(url)}">{GLib.markup_escape_text(disp)}</a>'
        lbl = Gtk.Label(
            label=markup, xalign=0, wrap=True, use_markup=True
        )
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(lbl)

    @staticmethod
    def _add_image(box, url, window_ref):
        img_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        img_box.set_halign(Gtk.Align.FILL)
        img_box.set_hexpand(True)
        img_box.set_size_request(-1, 200)

        spinner = Gtk.Spinner()
        spinner.start()
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_valign(Gtk.Align.CENTER)
        spinner.set_vexpand(True)
        img_box.append(spinner)
        box.append(img_box)

        ImageLoader.load_image_into_widget(url, img_box, spinner, window_ref)

    @staticmethod
    def _add_nostr_card(box, url, window, post_widget_ref=None):
        try:
            parts = url.split(":")
            if len(parts) < 2:
                return

            bech32_str = parts[1]
            # Only event references render as quote cards here; profile references
            # are rendered inline as @mentions in render().
            if "nevent" not in url and "note" not in url:
                return

            hex_id = ContentRenderer._extract_hex_id(bech32_str)
            if not hex_id:
                return

            event = window.db.get_event_by_id(hex_id)

            quote_frame = Gtk.Frame(css_classes=["quote-card"])
            quote_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=6,
                margin_top=8,
                margin_bottom=8,
                margin_start=8,
                margin_end=8,
            )
            quote_frame.set_child(quote_box)

            if event:
                ContentRenderer._build_quote_content(quote_box, event, window)
            else:
                lbl = Gtk.Label(label=f"Loading Quoted Event...", css_classes=["dim-label"])
                quote_box.append(lbl)
                window.client.request_once(
                    f"quote_{hex_id[:8]}", {"ids": [hex_id], "limit": 1}
                )

                if post_widget_ref:
                    if not hasattr(post_widget_ref, "quote_widgets"):
                        post_widget_ref.quote_widgets = []
                    post_widget_ref.quote_widgets.append((hex_id, quote_box))

            wrapper_btn = Gtk.Button(css_classes=["flat", "quote-wrapper"])
            wrapper_btn.set_child(quote_frame)
            wrapper_btn.connect(
                "clicked",
                lambda b: window.show_thread(hex_id, "Unknown", "Loading..."),
            )
            box.append(wrapper_btn)

        except Exception as e:
            print(f"[Renderer] Card Render Error: {e}")

    @staticmethod
    def _build_quote_content(container, event, window):
        pubkey = event["pubkey"]
        prof = window.db.get_profile(pubkey)
        name = pubkey[:8]
        if prof:
            name = prof.get("display_name") or prof.get("name") or name

        h_box = Gtk.Box(spacing=6)
        av = Adw.Avatar(size=24, show_initials=True, text=name)
        if prof and prof.get("picture"):
            picture_url = prof["picture"]
            if ContentRenderer.is_video_url(picture_url):
                h_box.append(av)
                av_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
                av_container.set_size_request(24, 24)
                av_container.set_halign(Gtk.Align.CENTER)
                VideoLoader.load_and_play(picture_url, av_container, None)
                h_box.append(av_container)
            else:
                h_box.append(av)
                ImageLoader.load_avatars(
                    prof["picture"], lambda t: av.set_custom_image(t)
                )
        else:
            h_box.append(av)

        lbl_name = Gtk.Label(label=name, css_classes=["heading", "caption-heading"])
        h_box.append(lbl_name)
        container.append(h_box)

        content = event.get("content", "")
        if len(content) > 140:
            content = content[:140] + "..."
        lbl_content = Gtk.Label(label=content, wrap=True, xalign=0, max_width_chars=40)
        lbl_content.set_ellipsize(Pango.EllipsizeMode.END)
        container.append(lbl_content)

    @staticmethod
    def _extract_hex_id(bech32_str):
        try:
            hrp, data = nostr_utils.bech32_decode(bech32_str)
            if not data:
                return None
            acc = 0
            bits = 0
            ret = []
            maxv = 255
            max_acc = (1 << 12) - 1
            for value in data:
                if value < 0 or (value >> 5):
                    return None
                acc = ((acc << 5) | value) & max_acc
                bits += 5
                while bits >= 8:
                    bits -= 8
                    ret.append((acc >> bits) & maxv)
            raw_bytes = bytes(ret)
            if hrp in ["note", "npub"]:
                return raw_bytes.hex()
            if hrp in ["nevent", "nprofile"]:
                i = 0
                while i < len(raw_bytes):
                    if i + 2 > len(raw_bytes):
                        break
                    t = raw_bytes[i]
                    l = raw_bytes[i + 1]
                    if i + 2 + l > len(raw_bytes):
                        break
                    if t == 0 and l == 32:
                        return raw_bytes[i + 2 : i + 2 + l].hex()
                    i += 2 + l
        except Exception:
            pass
        return None


def _launch_ext(win, s):
    try:
        Gtk.UriLauncher(uri=f"https://njump.me/{s}").launch(win, None, None)
    except Exception:
        pass


def get_youtube_stream(url):
    try:
        result = subprocess.run(
            ["yt-dlp", "-g", "-f", "best[ext=mp4]", url],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Failed to resolve YouTube URL: {e}")
        return url


class ImageLoader:
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)
    _cache = OrderedDict()  # bounded LRU: evicts oldest beyond _CACHE_MAX
    _cache_lock = threading.Lock()
    _ongoing = {}
    _ongoing_lock = threading.Lock()
    _CACHE_MAX = 64  # keep texture memory bounded on mobile
    MAX_WIDHT = 800  # max inline image dimension (main.py detect_display_metrics may override)

    @staticmethod
    def load_avatars(url, callback):
        ImageLoader._request_image(url, callback, size=(64, 64))

    @staticmethod
    def load_image_into_widget(url, container, spinner, window_ref=None):
        def on_ready(texture):
            if spinner and spinner.get_parent() == container:
                container.remove(spinner)

            if texture:
                width = texture.get_width()
                height = texture.get_height()
                ratio = width / height if height > 0 else 1.0

                available_width = 600
                if window_ref:
                    win_w = window_ref.get_width()
                    if win_w < 650:
                        available_width = win_w - 40
                    else:
                        available_width = 600

                req_height = int(available_width / ratio)
                container.set_size_request(-1, req_height)

                p = Gtk.Picture.new_for_paintable(texture)
                p.set_can_shrink(True)
                p.set_content_fit(Gtk.ContentFit.CONTAIN)
                p.set_halign(Gtk.Align.FILL)

                container.append(p)
            else:
                container.append(Gtk.Image.new_from_icon_name("image-missing-symbolic"))

        ImageLoader._request_image(url, on_ready, size=None)

    @staticmethod
    def _request_image(url, callback, size=None):
        if not url:
            callback(None)
            return

        with ImageLoader._cache_lock:
            if url in ImageLoader._cache:
                callback(ImageLoader._cache[url])
                return

        with ImageLoader._ongoing_lock:
            if url in ImageLoader._ongoing:
                ImageLoader._ongoing[url].append((callback, size))
                return
            else:
                ImageLoader._ongoing[url] = [(callback, size)]

        ImageLoader._executor.submit(ImageLoader._worker_fetch, url, size)

    @staticmethod
    def _worker_fetch(url, size=None):
        texture = None
        try:
            if url.startswith("http"):
                req = urllib.request.Request(url, headers={"User-Agent": "Gnostr/1.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = r.read()
                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
                pix = loader.get_pixbuf()
                if pix:
                    # Downscale before texture creation — never hold full-res
                    # textures on mobile. size=(w,h) fits within box; None caps to MAX_WIDHT.
                    nw, nh = ImageLoader._target_size(pix, size)
                    if nw < pix.get_width() or nh < pix.get_height():
                        pix = pix.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
                    texture = Gdk.Texture.new_for_pixbuf(pix)
        except Exception:
            pass
        GLib.idle_add(ImageLoader._notify_main_thread, url, texture)

    @staticmethod
    def _target_size(pix, size=None):
        w, h = pix.get_width(), pix.get_height()
        if size and size[0] and size[1]:
            scale = min(size[0] / w, size[1] / h, 1.0)
        else:
            scale = min(ImageLoader.MAX_WIDHT / w, 1.0)
        return max(1, int(w * scale)), max(1, int(h * scale))

    @staticmethod
    def _notify_main_thread(url, texture):
        if texture:
            with ImageLoader._cache_lock:
                ImageLoader._cache[url] = texture
                ImageLoader._cache.move_to_end(url)
                while len(ImageLoader._cache) > ImageLoader._CACHE_MAX:
                    ImageLoader._cache.popitem(last=False)

        with ImageLoader._ongoing_lock:
            callbacks = ImageLoader._ongoing.pop(url, [])

        for cb, size in callbacks:
            cb(texture)
        return False


class VideoLoader:
    @staticmethod
    def load_and_play(url, container, spinner, window_ref=None, autoplay=False):
        VideoPlayer.load_and_play(url, container, spinner, window_ref, autoplay=autoplay)


class VideoPlayer:
    _cache = OrderedDict()  # bounded LRU of live players
    _lock = threading.Lock()
    _CACHE_MAX = 6  # tear down decoders once they scroll far out of view

    @staticmethod
    def load_and_play(url, container, spinner, window_ref=None, original_url=None, autoplay=False):
        def on_ready(video):
            if spinner and spinner.get_parent() == container:
                container.remove(spinner)

            if video:
                req_w, req_h = container.get_size_request()
                if req_w > 0 and req_h > 0:
                    video.set_size_request(req_w, req_h)
                else:
                    video.set_size_request(-1, 200)
                video.set_halign(Gtk.Align.FILL)
                video.set_valign(Gtk.Align.FILL)

                if video.get_parent() is not None:
                    video.get_parent().remove(video)
                container.append(video)
                VideoPlayer._start_position_timer(container, video)
                if autoplay and hasattr(video, "_pipeline"):
                    video._pipeline.set_state(Gst.State.PLAYING)
                    video._is_playing = True

            else:
                container.append(Gtk.Image.new_from_icon_name("video-symbolic"))

        VideoPlayer._fetch_player(url, on_ready, original_url=original_url, autoplay=autoplay)

    @staticmethod
    def _fetch_player(url, callback, original_url=None, autoplay=False):
        with VideoPlayer._lock:
            if url in VideoPlayer._cache:
                cached_video = VideoPlayer._cache[url]
                # If the cached pipeline was evicted (set NULL, _pipeline=None),
                # the widget is dead/blank — treat it as a miss so a fresh
                # pipeline + widget are built (fixes blank video after navigating
                # away and back). Drop the dead entry and fall through to rebuild.
                if getattr(cached_video, "_pipeline", None) is None:
                    del VideoPlayer._cache[url]
                else:
                    if original_url and hasattr(cached_video, "_original_url"):
                        cached_video._original_url = original_url
                    VideoPlayer._cache.move_to_end(url)  # LRU touch
                    callback(cached_video)
                    return

        video = None
        try:
            _vlog(f"🎬 [Video] Building pipeline for: {url[:80]}...")

            # Create playbin3 with just the URI — it handles all formats internally
            pipeline = Gst.parse_launch(
                f"playbin3 uri={url}"
            )

            if not pipeline:
                _vlog("🎬 [Video] FAIL: Gst.parse_launch returned None")
                raise RuntimeError("Pipeline creation returned None")

            _vlog("🎬 [Video] Pipeline created OK")

            # Use gtk4paintablesink — GStreamer renders frames to a Gdk.Paintable
            # natively in C with proper frame-dropping. No per-frame Python
            # conversion (fixes stutter on non-GIF video).
            sink = Gst.ElementFactory.make("gtk4paintablesink", "sink")
            if not sink:
                _vlog("🎬 [Video] FAIL: could not create gtk4paintablesink element")
                raise RuntimeError("gtk4paintablesink creation failed")

            # Task 10: cap delivered frame size. Wrap the sink in a capsfilter bin so
            # decodebin3 scales to at most _MAX_VIDEO_WIDTH x _MAX_VIDEO_HEIGHT before
            # delivery (decode still runs at stream res, but the per-frame copy / GPU
            # upload cost drops). Fall back to a direct sink if the bin can't build so
            # video always works.
            #
            # The capsfilter caps carry only a width/height range (no `format`), which
            # otherwise makes playbin3's autoplug skip inserting videoconvert — the
            # decoder's raw format (e.g. NV12) then hits gtk4paintablesink (RGBA-only)
            # and errors with "unsupported pixel format". A videoconvert inside the bin
            # guarantees format adaptation to the sink's format, so it's deterministic
            # regardless of autoplug's negotiation.
            video_sink = sink
            if _MAX_VIDEO_WIDTH and _MAX_VIDEO_HEIGHT:
                try:
                    capfilter = Gst.ElementFactory.make("capsfilter", "mobile_res_cap")
                    capfilter.set_property(
                        "caps",
                        Gst.Caps.from_string(
                            f"video/x-raw,width=(int)[1,{_MAX_VIDEO_WIDTH}],"
                            f"height=(int)[1,{_MAX_VIDEO_HEIGHT}]"
                        ),
                    )
                    conv = Gst.ElementFactory.make("videoconvert", "res_fmt_conv")
                    sink_bin = Gst.Bin.new()
                    sink_bin.add(capfilter)
                    sink_bin.add(conv)
                    sink_bin.add(sink)
                    capfilter.link(conv)
                    conv.link(sink)
                    ghost = Gst.GhostPad.new("sink", capfilter.get_static_pad("sink"))
                    sink_bin.add_pad(ghost)
                    video_sink = sink_bin
                    _vlog(
                        f"🎬 [Video] video-sink capped "
                        f"≤{_MAX_VIDEO_WIDTH}x{_MAX_VIDEO_HEIGHT} +fmtconv"
                    )
                except Exception:
                    video_sink = sink  # keep video working

            pipeline.set_property("video-sink", video_sink)
            _vlog("🎬 [Video] gtk4paintablesink created and set as video-sink OK")

            # Create a Gtk.Picture and bind the sink's paintable directly — no
            # manual frame loop, no per-frame texture churn.
            picture = Gtk.Picture()
            picture.set_can_shrink(True)
            paintable = sink.get_property("paintable")
            if paintable:
                picture.set_paintable(paintable)
            _vlog("🎬 [Video] Gtk.Picture bound to gtk4paintablesink paintable")

            # Bus for error/EOS handling
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            def on_bus_message(bus, msg, p=pipeline, media_url=url):
                # Gst.MessageType is a flags enum — use bitwise AND to check
                t = msg.type
                if t == Gst.MessageType.ERROR:
                    try:
                        err, debug = msg.parse_error()
                        _vlog(f"🎬 [Media Codec Error] {media_url}\n  -> {err.message}")
                    except Exception:
                        _vlog(f"🎬 [Media] Bus message: {t}")
                elif t == Gst.MessageType.EOS:
                    _vlog(f"🎬 [Media] EOS — looping {media_url[:50]}...")
                    p.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
                elif t == Gst.MessageType.WARNING:
                    try:
                        err, debug = msg.parse_warning()
                        _vlog(f"🎬 [Media Warning] {media_url}\n  -> {err.message}")
                    except Exception:
                        _vlog(f"🎬 [Media] Bus message: {t}")
                elif t & Gst.MessageType.STATE_CHANGED:
                    old, new, pending = msg.parse_state_changed()
                    if pending == Gst.State.VOID_PENDING:
                        _elapsed = ""
                        try:
                            _elapsed = (
                                f"{(time.monotonic() - p._t0) * 1000:.0f}ms"
                                if hasattr(p, "_t0")
                                else ""
                            )
                        except Exception:
                            _elapsed = ""
                        _vlog(
                            f"🎬 [Media] State changed: {old} → {new} "
                            f"{_elapsed} {media_url[:40]}"
                        )
                        # Task 10: report decoder + negotiated video caps once per
                        # pipeline. Deferred ~600ms after the first PAUSED/PLAYING
                        # so caps have time to negotiate — with lazy-load autoplay
                        # the state-change races ahead of negotiation, so querying
                        # inline returned caps=None for most videos (measurement
                        # regression). The timeout keeps the measurement non-blocking.
                        if new in (Gst.State.PAUSED, Gst.State.PLAYING) and not getattr(
                            p, "_caps_reported", False
                        ):
                            p._caps_reported = True
                            GLib.timeout_add(
                                600,
                                lambda pp=p, u=media_url: (
                                    VideoPlayer._inspect_pipeline(pp, u)
                                    or False
                                ),
                            )
                else:
                    # Only surface the real dropped-frame signal (QOS, (1<<24)).
                    # LATENCY (1<<19), SEGMENT_DONE, PROGRESS, CLOCK_UPDATE are benign
                    # pipeline noise that flooded the log (hundreds of lines) and
                    # masked the QOS signal. Use numeric bitmasks — enum names like
                    # QOS/LATENCY/CLOCK_UPDATE are inconsistently present across
                    # Flatpak GStreamer bindings, so getattr() can silently return 0.
                    _qos_bit = 1 << 24  # GST_MESSAGE_QOS
                    if t & _qos_bit:
                        # QOS is the real dropped-frame signal: gtk4paintablesink
                        # reports when it drops / late-processes frames. Aggregate
                        # per-pipeline so we can attribute stutter to a specific URL
                        # (correlate with measured caps at eviction). parse_qos is
                        # best-effort; never let it break the bus handler.
                        try:
                            _q = msg.parse_qos()
                            _prop = float(_q[5]) if len(_q) > 5 else 1.0
                            _drops = int(_q[7]) if len(_q) > 7 else 0
                        except Exception:
                            _prop, _drops = 1.0, 0
                        p._qos_events = getattr(p, "_qos_events", 0) + 1
                        p._qos_dropped = getattr(p, "_qos_dropped", 0) + _drops
                        p._qos_prop_min = min(
                            getattr(p, "_qos_prop_min", 1.0), _prop
                        )
            bus.connect("message", on_bus_message)
            _vlog("🎬 [Video] Bus watcher connected")

            # Start in paused state
            pipeline.set_state(Gst.State.PAUSED)
            _vlog("🎬 [Video] Pipeline set to PAUSED")

            video = picture
            video._pipeline = pipeline
            video._sink = sink
            video._is_playing = False
            video._is_muted = False
            video._original_url = original_url or url
            pipeline._t0 = time.monotonic()  # Task 10 perf: build-start timestamp
            pipeline._caps_reported = False  # inspect once at first preroll

            # Play at full volume; users control loudness at the system level.
            pipeline.set_property("volume", 1.0)

            _vlog("🎬 [Video] Player setup complete — waiting for play")

        except Exception as e:
            _vlog(f"🎬 [Video] Pipeline failed: {e}")
            video = None

        if video:
            with VideoPlayer._lock:
                VideoPlayer._cache[url] = video
                VideoPlayer._cache.move_to_end(url)
                # Tear down oldest players so decoders don't accumulate on mobile.
                # The evicted widget is scrolled out of view; it will re-create if revisited.
                while len(VideoPlayer._cache) > VideoPlayer._CACHE_MAX:
                    old_url, old_video = VideoPlayer._cache.popitem(last=False)
                    _vlog(
                        f"🎬[PERF] evicting player: {old_url[:60]} "
                        f"(cache={len(VideoPlayer._cache)}/{VideoPlayer._CACHE_MAX})\n"
                    )
                    try:
                        p = getattr(old_video, "_pipeline", None)
                        if p:
                            # Task 10: per-pipeline QOS summary before teardown.
                            # qos_events = dropped-frame reports; qos_dropped = sum of
                            # buffers dropped-late; qos_prop_min = worst 1.0→0 quality
                            # ratio (1.0 = perfect, lower = worse stutter).
                            _ev = getattr(p, "_qos_events", 0)
                            _dp = getattr(p, "_qos_dropped", 0)
                            _pm = getattr(p, "_qos_prop_min", 1.0)
                            _caps = getattr(p, "_measured_caps", "n/a")
                            _vlog(
                                f"🎬[PERF-QOS] {old_url[:55]} events={_ev} "
                                f"dropped_late={_dp} worst_prop={_pm:.2f} "
                                f"caps={_caps[:40]}"
                            )
                            p.set_state(Gst.State.NULL)  # release decoder + buffers
                        # Mark the widget dead so a later cache-hit/start rebuilds
                        # fresh instead of toggling a NULL'd pipeline (blank video
                        # after navigating away and back). The cache entry is popped
                        # below, but the widget may still be in a feed.
                        old_video._pipeline = None
                    except Exception:
                        pass

        callback(video)

    @staticmethod
    def _inspect_pipeline(pipeline, url):
        """Task 10 perf diagnostic (measure-first). Reports the negotiated video
        caps (resolution / framerate) — the key measurement — then the decoder
        element(s). Caps are queried and printed first so the report survives even
        if the decoder walk fails; the walk is best-effort and never blocks."""
        caps_str = "none"
        try:
            sink = pipeline.get_property("video-sink")
            if sink:
                pad = None
                if hasattr(sink, "get_by_name"):
                    # video-sink may be the Task 10 capsfilter bin — descend to the
                    # real gtk4paintablesink (named "sink") for its delivered caps.
                    real = sink.get_by_name("sink")
                    pad = real.get_static_pad("sink") if real else None
                if pad is None:
                    pad = sink.get_static_pad("sink")
                caps = pad.get_current_caps() if pad else None
                caps_str = caps.to_string() if caps else "none"
        except Exception:
            caps_str = "query-error"

        # Stash for the eviction QOS summary — correlate measured resolution with
        # the stutter this pipeline accumulated.
        try:
            pipeline._measured_caps = caps_str
        except Exception:
            pass

        decoders = []
        try:

            def _walk(bin_, depth=0):
                for el in bin_.iterate_elements():
                    name = el.get_name().lower()
                    if any(
                        k in name
                        for k in (
                            "decoder",
                            "v4l2",
                            "vaapi",
                            "avdec",
                            "omx",
                            "mfx",
                            "d3d",
                            "nvdec",
                        )
                    ):
                        decoders.append(el.get_name())
                    if isinstance(el, Gst.Bin):
                        _walk(el, depth + 1)

            _walk(pipeline)
        except Exception:
            pass

        print(
            f"🎬[PERF] {url[:60]} decoders={decoders or ['auto']} "
            f"video_caps={caps_str}"
        )

    @staticmethod
    def _find_video(video_container):
        """Find the video widget (Gtk.Picture) in the container."""
        for child in video_container:
            if isinstance(child, Gtk.Picture):
                return child
        return None

    @staticmethod
    def toggle_play(video_container):
        video = VideoPlayer._find_video(video_container)
        if not video or getattr(video, "_pipeline", None) is None:
            _vlog("🎬 [Video] toggle_play: no live pipeline found")
            return
        pipeline = video._pipeline
        video._is_playing = not video._is_playing
        _vlog(f"🎬 [Video] toggle_play: {'PLAYING' if video._is_playing else 'PAUSED'}")
        if video._is_playing:
            ret = pipeline.set_state(Gst.State.PLAYING)
            _vlog(f"🎬 [Video] set_state(PLAYING) returned: {ret}")
        else:
            ret = pipeline.set_state(Gst.State.PAUSED)
            _vlog(f"🎬 [Video] set_state(PAUSED) returned: {ret}")

    @staticmethod
    def toggle_mute(video_container, mute_button):
        video = VideoPlayer._find_video(video_container)
        if not video or not hasattr(video, '_pipeline'):
            _vlog("🎬 [Video] toggle_mute: no video or pipeline found")
            return
        pipeline = video._pipeline
        video._is_muted = not video._is_muted
        _vlog(f"🎬 [Video] toggle_mute: {'MUTED' if video._is_muted else 'UNMUTED'}")
        pipeline.set_property("volume", 0.0 if video._is_muted else 1.0)
        mute_button.set_icon_name(
            "audio-volume-muted-symbolic" if video._is_muted else "audio-volume-high-symbolic"
        )

    @staticmethod
    def seek_to(video_container, percent):
        video = VideoPlayer._find_video(video_container)
        if not video or not hasattr(video, '_pipeline'):
            return
        pipe = video._pipeline
        dur = video._duration_ns if hasattr(video, '_duration_ns') and video._duration_ns > 0 else 0
        if dur > 0:
            ns = int(dur * percent / 100.0)
            pipe.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, ns)

    @staticmethod
    def _start_position_timer(video_container, video):
        """Start a GLib timeout to update position/seek bar every 500ms"""
        def update():
            pipe = video._pipeline
            if not pipe:
                return True
            # Find position scale and label
            video_box = video_container.get_parent()
            if not video_box:
                return True
            position_scale = None
            position_label = None
            children = list(video_box)
            if len(children) >= 3:
                pos_row = children[1]
                if isinstance(pos_row, Gtk.Box):
                    for c in pos_row:
                        if isinstance(c, Gtk.Scale):
                            position_scale = c
                        elif isinstance(c, Gtk.Label):
                            position_label = c
            if not position_scale:
                return True

            state = pipe.get_state(0)
            if state[1] != Gst.State.PLAYING:
                return True

            # Query duration
            dur_result = pipe.query_duration(Gst.Format.TIME)
            if dur_result[0]:
                dur_ns = dur_result[1]
                video._duration_ns = dur_ns
            else:
                dur_ns = video._duration_ns if hasattr(video, '_duration_ns') else 0

            # Query position
            pos_result = pipe.query_position(Gst.Format.TIME)
            if pos_result[0]:
                pos_ns = pos_result[1]
                if dur_ns > 0:
                    pct = pos_ns * 100.0 / dur_ns
                    position_scale.set_value(pct)
                    position_scale.set_sensitive(True)
                    # Update label
                    pos_sec = pos_ns // 1000000000
                    dur_sec = dur_ns // 1000000000
                    pos_min, pos_s = divmod(pos_sec, 60)
                    dur_min, dur_s = divmod(dur_sec, 60)
                    if position_label:
                        position_label.set_text(f"{pos_min}:{pos_s:02d} / {dur_min}:{dur_s:02d}")
            return True

        GLib.timeout_add(500, update)
