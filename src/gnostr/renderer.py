import re
import html
import gi
import urllib.request
import threading
import concurrent.futures
from urllib.parse import urlparse
import traceback
import subprocess

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


class ContentRenderer:
    LINK_REGEX = re.compile(r"(?:^|\s)((?:https?://|nostr:)[^\s]+)")
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

        spinner = Gtk.Spinner()
        spinner.start()
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_valign(Gtk.Align.CENTER)
        spinner.set_vexpand(True)
        video_area.append(spinner)
        
        VideoPlayer.load_and_play(url, video_area, spinner, window_ref, original_url)
        video_box.append(video_area)
        
        
        # Position/seek bar
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
        position_row.append(position_scale)
        position_row.append(position_label)
        video_box.append(position_row)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        controls.set_margin_top(6)
        controls.set_margin_bottom(6)
        controls.set_margin_start(6)
        controls.set_margin_end(6)
        
        play_btn = Gtk.Button(icon_name="media-playback-start-symbolic")
        play_btn.set_tooltip_text("Play/Pause")
        play_btn.set_size_request(40, 40)
        play_btn.connect("clicked", lambda b: VideoPlayer.toggle_play(video_area))
        controls.append(play_btn)
        
        mute_btn = Gtk.Button(icon_name="audio-volume-muted-symbolic")
        mute_btn.set_tooltip_text("Mute/Unmute")
        mute_btn.set_size_request(40, 40)
        mute_btn.connect("clicked", lambda b: VideoPlayer.toggle_mute(video_area, mute_btn))
        controls.append(mute_btn)
        
        vol_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        vol_scale.set_range(0, 100)
        vol_scale.set_value(0)
        vol_scale.set_size_request(100, -1)
        vol_scale.set_hexpand(True)
        vol_scale.connect("value-changed", lambda s: VideoPlayer.set_volume(video_area, s.get_value() / 100))
        controls.append(vol_scale)
        
        if original_url and ("youtube.com" in original_url or "youtu.be" in original_url):
            yt_link = Gtk.LinkButton(uri=original_url, label="Open in YouTube")
            yt_link.set_halign(Gtk.Align.END)
            yt_link.set_hexpand(True)
            controls.append(yt_link)
        
        video_box.append(controls)
        box.append(video_box)

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
            current_text_buffer = []

            for part in parts:
                if not part:
                    continue

                if ContentRenderer.LINK_REGEX.match(part):
                    if current_text_buffer:
                        ContentRenderer._add_text(box, "".join(current_text_buffer))
                        current_text_buffer = []

                    clean_part = part.rstrip(".!,?;']}" )
                    trailing = part[len(clean_part) :]

                    if clean_part.startswith("nostr:"):
                        ContentRenderer._add_nostr_card(box, clean_part, window_ref, post_widget_ref)
                    elif ContentRenderer.is_image_url(clean_part):
                        ContentRenderer._add_image(box, clean_part, window_ref)
                    elif ContentRenderer.is_video_url(clean_part):
                        ContentRenderer._add_video(box, clean_part, window_ref)
                    elif "youtube.com/watch" in clean_part or "youtu.be/" in clean_part:
                        raw_stream_url = get_youtube_stream(clean_part)
                        ContentRenderer._add_video(box, raw_stream_url, window_ref, clean_part)
                    else:
                        ContentRenderer._add_link(box, clean_part)

                    if trailing:
                        current_text_buffer.append(trailing)
                else:
                    current_text_buffer.append(part)

            if current_text_buffer:
                ContentRenderer._add_text(box, "".join(current_text_buffer))

        except Exception as e:
            error_label = Gtk.Label(label=f"[Render error: {str(e)[:50]}]", xalign=0)
            error_label.add_css_class("dim-label")
            box.append(error_label)

        return box

    @staticmethod
    def _add_text(box, text):
        label = Gtk.Label(label=text, xalign=0, selectable=True)
        label.set_use_markup(False)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(60)
        box.append(label)

    @staticmethod
    def _add_link(box, url, label=None):
        disp = label if label else (url[:47] + "..." if len(url) > 50 else url)
        markup = f'<a href="{GLib.markup_escape_text(url)}">{GLib.markup_escape_text(disp)}</a>'
        lbl = Gtk.Label(
            label=markup, xalign=0, wrap=True, selectable=True, use_markup=True
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
            is_event = "nevent" in url or "note" in url
            is_profile = "nprofile" in url or "npub" in url

            if is_event:
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

            elif is_profile:
                hex_pk = ContentRenderer._extract_hex_id(bech32_str)
                if not hex_pk:
                    return

                prof_frame = Gtk.Frame(css_classes=["profile-card"])
                prof_box = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL,
                    spacing=10,
                    margin_top=8,
                    margin_bottom=8,
                    margin_start=8,
                    margin_end=8,
                )
                prof_frame.set_child(prof_box)

                av = Adw.Avatar(size=32, show_initials=True, text="?")
                prof_box.append(av)

                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                lbl_name = Gtk.Label(label="User Profile", css_classes=["heading"], xalign=0)
                lbl_sub = Gtk.Label(
                    label=hex_pk[:8] + "...",
                    css_classes=["caption", "dim-label"],
                    xalign=0,
                )
                vbox.append(lbl_name)
                vbox.append(lbl_sub)
                prof_box.append(vbox)

                profile = window.db.get_profile(hex_pk)
                if profile:
                    name = profile.get("display_name") or profile.get("name")
                    if name:
                        lbl_name.set_label(name)
                        av.set_text(name)
                    if profile.get("picture"):
                        picture_url = profile["picture"]
                        if ContentRenderer.is_video_url(picture_url):
                            prof_box.remove(av)
                            av_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
                            av_container.set_size_request(32, 32)
                            av_container.set_halign(Gtk.Align.CENTER)
                            VideoLoader.load_and_play(picture_url, av_container, None)
                            prof_box.append(av_container)
                        else:
                            ImageLoader.load_avatars(
                                profile["picture"], lambda t: av.set_custom_image(t)
                            )
                else:
                    window.client.fetch_profile(hex_pk)

                if post_widget_ref:
                    if not hasattr(post_widget_ref, "mention_widgets"):
                        post_widget_ref.mention_widgets = []
                    post_widget_ref.mention_widgets.append((hex_pk, lbl_name, av))

                wrapper_btn = Gtk.Button(css_classes=["flat", "quote-wrapper"])
                wrapper_btn.set_child(prof_frame)

                def on_click_prof(b):
                    window.show_profile(hex_pk)

                wrapper_btn.connect("clicked", on_click_prof)
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
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=16)
    _cache = {}
    _cache_lock = threading.Lock()
    _ongoing = {}
    _ongoing_lock = threading.Lock()

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

        ImageLoader._executor.submit(ImageLoader._worker_fetch, url)

    @staticmethod
    def _worker_fetch(url):
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
                    texture = Gdk.Texture.new_for_pixbuf(pix)
        except Exception:
            pass
        GLib.idle_add(ImageLoader._notify_main_thread, url, texture)

    @staticmethod
    def _notify_main_thread(url, texture):
        if texture:
            with ImageLoader._cache_lock:
                ImageLoader._cache[url] = texture

        with ImageLoader._ongoing_lock:
            callbacks = ImageLoader._ongoing.pop(url, [])

        for cb, size in callbacks:
            cb(texture)
        return False


class VideoLoader:
    @staticmethod
    def load_and_play(url, container, spinner, window_ref=None):
        VideoPlayer.load_and_play(url, container, spinner, window_ref)


class VideoPlayer:
    _cache = {}
    _lock = threading.Lock()

    @staticmethod
    def load_and_play(url, container, spinner, window_ref=None, original_url=None):
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

            else:
                container.append(Gtk.Image.new_from_icon_name("video-symbolic"))

        VideoPlayer._fetch_player(url, on_ready, original_url=original_url)

    @staticmethod
    def _fetch_player(url, callback, original_url=None):
        with VideoPlayer._lock:
            if url in VideoPlayer._cache:
                cached_video = VideoPlayer._cache[url]
                if original_url and hasattr(cached_video, '_original_url'):
                    cached_video._original_url = original_url
                callback(cached_video)
                return

        video = None
        try:
            print(f"🎬 [Video] Building pipeline for: {url[:80]}...")

            # Create playbin3 with just the URI — it handles all formats internally
            pipeline = Gst.parse_launch(
                f"playbin3 uri={url}"
            )

            if not pipeline:
                print("🎬 [Video] FAIL: Gst.parse_launch returned None")
                raise RuntimeError("Pipeline creation returned None")

            print("🎬 [Video] Pipeline created OK")

            # Create appsink separately and set as video-sink property
            # (pipeline string approach doesn't expose the named element)
            sink = Gst.ElementFactory.make("appsink", "sink")
            if not sink:
                print("🎬 [Video] FAIL: could not create appsink element")
                raise RuntimeError("appsink creation failed")

            pipeline.set_property("video-sink", sink)
            sink.set_property("caps", Gst.Caps.from_string("video/x-raw,format=RGB"))
            print("🎬 [Video] appsink created and set as video-sink OK")

            # Create a Gtk.Picture to display frames
            picture = Gtk.Picture()
            picture.set_can_shrink(True)
            print("🎬 [Video] Gtk.Picture created")

            def on_sample(s):
                try:
                    sample = s.pull_sample()
                    if sample:
                        print(f"🎬 [Video] Frame received — pulling sample")
                        buf = sample.get_buffer()
                        caps = sample.get_caps()
                        if caps and buf:
                            structure = caps.get_structure(0)
                            width = structure.get_int("width")[1]
                            height = structure.get_int("height")[1]
                            print(f"🎬 [Video] Frame size: {width}x{height}")
                            fmt = structure.get_string("format")
                            if fmt:
                                print(f"🎬 [Video] Pixel format: {fmt}")
                            bufsize = buf.get_size()
                            expected_rgb = width * height * 3
                            print(f"🎬 [Video] Buffer size: {bufsize}, expected RGB: {expected_rgb}")

                            success, map_info = buf.map(Gst.MapFlags.READ)
                            if success:
                                try:
                                    raw = bytes(map_info.data)
                                    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
                                        GLib.Bytes.new(raw),
                                        GdkPixbuf.Colorspace.RGB, False, 8,
                                        width, height, width * 3
                                    )
                                    if pixbuf:
                                        print(f"🎬 [Video] Pixbuf created OK, creating texture")
                                        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                                        GLib.idle_add(picture.set_paintable, texture)
                                        print(f"🎬 [Video] Texture set on picture via idle_add")
                                    else:
                                        print(f"🎬 [Video] FAIL: pixbuf is None")
                                except Exception as e:
                                    print(f"🎬 [Video] FAIL in frame conversion: {e}")
                                buf.unmap(map_info)
                        else:
                            print(f"🎬 [Video] Sample has no caps/buf: caps={caps}, buf={buf}")
                    else:
                        print(f"🎬 [Video] emit('pull-sample') returned None — no frame available")
                except Exception as e:
                    print(f"🎬 [Video] FAIL in on_sample: {e}")
                return Gst.FlowReturn.OK

            sink.set_property("emit-signals", True)
            sink.set_property("max-buffers", 1)
            sink.set_property("drop", True)
            sink.connect("new-sample", on_sample)
            print("🎬 [Video] appsink configured and connected")

            # Bus for error/EOS handling
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            def on_bus_message(bus, msg, p=pipeline, media_url=url):
                # Gst.MessageType is a flags enum — use bitwise AND to check
                t = msg.type
                if t & Gst.MessageType.ERROR:
                    try:
                        err, debug = msg.parse_error()
                        print(f"🎬 [Media Codec Error] {media_url}\n  -> {err.message}")
                    except Exception:
                        print(f"🎬 [Media] Bus message: {t}")
                elif t & Gst.MessageType.EOS:
                    print(f"🎬 [Media] EOS — looping {media_url[:50]}...")
                    p.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
                elif t & Gst.MessageType.WARNING:
                    try:
                        err, debug = msg.parse_warning()
                        print(f"🎬 [Media Warning] {media_url}\n  -> {err.message}")
                    except Exception:
                        print(f"🎬 [Media] Bus message: {t}")
                elif t & Gst.MessageType.STATE_CHANGED:
                    old, new, pending = msg.parse_state_changed()
                    if pending == Gst.State.VOID_PENDING:
                        print(f"🎬 [Media] State changed: {old} → {new}")
                else:
                    print(f"🎬 [Media] Bus message: {t}")
            bus.connect("message", on_bus_message)
            print("🎬 [Video] Bus watcher connected")

            # Start in paused state
            pipeline.set_state(Gst.State.PAUSED)
            print("🎬 [Video] Pipeline set to PAUSED")

            video = picture
            video._pipeline = pipeline
            video._appsink = sink
            video._is_playing = False
            video._is_muted = True
            video._original_url = original_url or url

            print("🎬 [Video] Player setup complete — waiting for play")

        except Exception as e:
            print(f"🎬 [Video] Pipeline failed: {e}")
            video = None

        if video:
            with VideoPlayer._lock:
                VideoPlayer._cache[url] = video

        callback(video)

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
        if not video or not hasattr(video, '_pipeline'):
            print("🎬 [Video] toggle_play: no video or pipeline found")
            return
        pipeline = video._pipeline
        video._is_playing = not video._is_playing
        print(f"🎬 [Video] toggle_play: {'PLAYING' if video._is_playing else 'PAUSED'}")
        if video._is_playing:
            ret = pipeline.set_state(Gst.State.PLAYING)
            print(f"🎬 [Video] set_state(PLAYING) returned: {ret}")
        else:
            ret = pipeline.set_state(Gst.State.PAUSED)
            print(f"🎬 [Video] set_state(PAUSED) returned: {ret}")

    @staticmethod
    def toggle_mute(video_container, mute_button):
        video = VideoPlayer._find_video(video_container)
        if not video or not hasattr(video, '_pipeline'):
            print("🎬 [Video] toggle_mute: no video or pipeline found")
            return
        pipeline = video._pipeline
        video._is_muted = not video._is_muted
        print(f"🎬 [Video] toggle_mute: {'MUTED' if video._is_muted else 'UNMUTED'}")
        pipeline.set_property("volume", 0.0 if video._is_muted else 1.0)
        mute_button.set_icon_name(
            "audio-volume-muted-symbolic" if video._is_muted else "audio-volume-high-symbolic"
        )

    @staticmethod
    def set_volume(video_container, volume):
        video = VideoPlayer._find_video(video_container)
        if not video or not hasattr(video, '_pipeline'):
            return
        pipeline = video._pipeline
        video._is_muted = (volume == 0.0)
        pipeline.set_property("volume", volume)
        if volume > 0:
            controls = video_container.get_next_sibling()
            if controls:
                for child in controls:
                    if isinstance(child, Gtk.Button):
                        if child.get_icon_name() == "audio-volume-muted-symbolic":
                            child.set_icon_name("audio-volume-high-symbolic")
                            break
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
