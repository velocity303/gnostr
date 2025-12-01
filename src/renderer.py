import re
import html
import gi
import urllib.request
import threading
import concurrent.futures
from urllib.parse import urlparse
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Pango', '1.0')
from gi.repository import Gtk, Adw, GLib, Gdk, GdkPixbuf, Pango
import nostr_utils

class ContentRenderer:
    LINK_REGEX = re.compile(r'((?:https?://|nostr:)[^\s]+)')
    IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
    VIDEO_EXTS = {'.mp4', '.mov', '.webm'}

    @staticmethod
    def is_image_url(url):
        try:
            path = urlparse(url).path.lower()
            return any(path.endswith(ext) for ext in ContentRenderer.IMAGE_EXTS)
        except: return False

    @staticmethod
    def is_video_url(url):
        try:
            path = urlparse(url).path.lower()
            return any(path.endswith(ext) for ext in ContentRenderer.VIDEO_EXTS)
        except: return False

    @staticmethod
    def render(content, window_ref, post_widget_ref=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        if not content: return box
        
        try:
            clean_content = html.unescape(content)
            parts = ContentRenderer.LINK_REGEX.split(clean_content)
            current_text_buffer = []

            for part in parts:
                if not part: continue

                if ContentRenderer.LINK_REGEX.match(part):
                    if current_text_buffer:
                        ContentRenderer._add_text(box, "".join(current_text_buffer))
                        current_text_buffer = []
                    
                    clean_part = part.rstrip(".,;!?)]}")
                    trailing = part[len(clean_part):]
                    
                    if clean_part.startswith("nostr:"): 
                        ContentRenderer._add_nostr_card(box, clean_part, window_ref, post_widget_ref)
                    elif ContentRenderer.is_image_url(clean_part):
                        ContentRenderer._add_image(box, clean_part)
                    elif ContentRenderer.is_video_url(clean_part):
                        ContentRenderer._add_link_button(box, clean_part, "▶ Watch Video")
                    else: 
                        ContentRenderer._add_link(box, clean_part) 
                        
                    if trailing:
                        current_text_buffer.append(trailing)
                else: 
                    current_text_buffer.append(part)
            
            if current_text_buffer: 
                ContentRenderer._add_text(box, "".join(current_text_buffer))

        except Exception as e:
            print(f"Render Error: {e}")
            ContentRenderer._add_text(box, content)
            
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
        disp = label if label else (url[:47] + "..." if len(url)>50 else url)
        markup = f'<a href="{GLib.markup_escape_text(url)}">{GLib.markup_escape_text(disp)}</a>'
        lbl = Gtk.Label(label=markup, xalign=0, wrap=True, selectable=True, use_markup=True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_ellipsize(Pango.EllipsizeMode.END) 
        box.append(lbl)

    @staticmethod
    def _add_link_button(box, url, label):
        btn = Gtk.LinkButton(uri=url, label=label, halign=Gtk.Align.START)
        box.append(btn)

    @staticmethod
    def _add_image(box, url):
        img_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        img_box.set_halign(Gtk.Align.START)

        spinner = Gtk.Spinner()
        spinner.start()
        img_box.append(spinner)
        box.append(img_box)

        ImageLoader.load_image_into_widget(url, img_box, spinner)

    @staticmethod
    def _add_nostr_card(box, uri, window, post_widget_ref=None):
        try:
            parts = uri.split(":")
            if len(parts) < 2: return
            
            bech32_str = parts[1]
            is_event = "nevent" in uri or "note" in uri
            is_profile = "nprofile" in uri or "npub" in uri
            
            if is_event:
                hex_id = ContentRenderer._extract_hex_id(bech32_str)
                if not hex_id: return

                event = window.db.get_event_by_id(hex_id)
                
                quote_frame = Gtk.Frame(css_classes=["quote-card"])
                quote_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
                quote_frame.set_child(quote_box)

                if event:
                    ContentRenderer._build_quote_content(quote_box, event, window)
                else:
                    lbl = Gtk.Label(label=f"Loading Quoted Event...", css_classes=["dim-label"])
                    quote_box.append(lbl)
                    window.client.request_once(f"quote_{hex_id[:8]}", {"ids": [hex_id], "limit": 1})

                    if post_widget_ref:
                        if not hasattr(post_widget_ref, 'quote_widgets'):
                            post_widget_ref.quote_widgets = []
                        post_widget_ref.quote_widgets.append((hex_id, quote_box))

                wrapper_btn = Gtk.Button(css_classes=["flat", "quote-wrapper"])
                wrapper_btn.set_child(quote_frame)
                wrapper_btn.connect("clicked", lambda b: window.show_thread(hex_id, "Unknown", "Loading..."))
                box.append(wrapper_btn)

            elif is_profile:
                hex_pk = ContentRenderer._extract_hex_id(bech32_str)
                if not hex_pk: return

                prof_frame = Gtk.Frame(css_classes=["profile-card"])
                prof_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
                prof_frame.set_child(prof_box)

                av = Adw.Avatar(size=32, show_initials=True, text="?")
                prof_box.append(av)

                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                lbl_name = Gtk.Label(label="User Profile", css_classes=["heading"], xalign=0)
                lbl_sub = Gtk.Label(label=hex_pk[:8]+"...", css_classes=["caption", "dim-label"], xalign=0)
                vbox.append(lbl_name)
                vbox.append(lbl_sub)
                prof_box.append(vbox)

                profile = window.db.get_profile(hex_pk)
                if profile:
                    name = profile.get('display_name') or profile.get('name')
                    if name:
                        lbl_name.set_label(name)
                        av.set_text(name)
                    if profile.get('picture'):
                        ImageLoader.load_avatar(profile['picture'], lambda t: av.set_custom_image(t))
                else:
                    window.client.fetch_profile(hex_pk)

                if post_widget_ref:
                    if not hasattr(post_widget_ref, 'mention_widgets'):
                        post_widget_ref.mention_widgets = []
                    post_widget_ref.mention_widgets.append((hex_pk, lbl_name, av))

                wrapper_btn = Gtk.Button(css_classes=["flat", "quote-wrapper"])
                wrapper_btn.set_child(prof_frame)
                def on_click_prof(b):
                    window.show_profile(hex_pk)
                wrapper_btn.connect("clicked", on_click_prof)
                box.append(wrapper_btn)

        except Exception as e:
            print(f"Card Render Error: {e}")

    @staticmethod
    def _build_quote_content(container, event, window):
        pubkey = event['pubkey']
        prof = window.db.get_profile(pubkey)
        name = pubkey[:8]
        if prof: name = prof.get('display_name') or prof.get('name') or name

        h_box = Gtk.Box(spacing=6)
        av = Adw.Avatar(size=24, show_initials=True, text=name)
        if prof and prof.get('picture'):
            ImageLoader.load_avatar(prof['picture'], lambda t: av.set_custom_image(t))

        lbl_name = Gtk.Label(label=name, css_classes=["heading", "caption-heading"])
        h_box.append(av)
        h_box.append(lbl_name)
        container.append(h_box)

        content = event.get('content', '')
        if len(content) > 140: content = content[:140] + "..."
        lbl_content = Gtk.Label(label=content, wrap=True, xalign=0, max_width_chars=40)
        lbl_content.set_ellipsize(Pango.EllipsizeMode.END)
        container.append(lbl_content)

    @staticmethod
    def _extract_hex_id(bech32_str):
        try:
            hrp, data = nostr_utils.bech32_decode(bech32_str)
            if not data: return None
            acc = 0; bits = 0; ret = []; maxv = 255; max_acc = (1 << 12) - 1
            for value in data:
                if value < 0 or (value >> 5): return None
                acc = ((acc << 5) | value) & max_acc
                bits += 5
                while bits >= 8: bits -= 8; ret.append((acc >> bits) & maxv)
            raw_bytes = bytes(ret)
            if hrp in ["note", "npub"]: return raw_bytes.hex()
            if hrp in ["nevent", "nprofile"]:
                i = 0
                while i < len(raw_bytes):
                    if i + 2 > len(raw_bytes): break
                    t = raw_bytes[i]; l = raw_bytes[i+1]
                    if i + 2 + l > len(raw_bytes): break
                    if t == 0 and l == 32: return raw_bytes[i+2 : i+2+l].hex()
                    i += 2 + l
        except: pass
        return None

def _launch_ext(win, s):
    try: Gtk.UriLauncher(uri=f"https://njump.me/{s}").launch(win, None, None)
    except: pass

class ImageLoader:
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=16)
    _cache = {}
    _cache_lock = threading.Lock()
    _ongoing = {}
    _ongoing_lock = threading.Lock()

    # Default Desktop Limit
    MAX_WIDTH = 800

    @staticmethod
    def load_avatar(url, callback):
        ImageLoader._request_image(url, callback, size=(64,64))

    @staticmethod
    def load_image_into_widget(url, container, spinner):
        def on_ready(texture):
            if spinner and spinner.get_parent() == container:
                container.remove(spinner)

            if texture:
                p = Gtk.Picture.new_for_paintable(texture)
                p.set_can_shrink(False)
                p.set_halign(Gtk.Align.START)
                container.append(p)
            else:
                container.append(Gtk.Image.new_from_icon_name("image-missing-symbolic"))

        ImageLoader._request_image(url, on_ready, size=None)

    @staticmethod
    def _request_image(url, callback, size=None):
        if not url:
            callback(None); return

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
                req = urllib.request.Request(url, headers={'User-Agent': 'Gnostr/1.0'})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = r.read()

                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
                pix = loader.get_pixbuf()

                if pix:
                    # DYNAMIC SCALING LOGIC
                    limit = ImageLoader.MAX_WIDTH
                    w = pix.get_width()
                    h = pix.get_height()

                    if w > limit:
                        scale = limit / w
                        new_w = limit
                        new_h = int(h * scale)
                        pix = pix.scale_simple(new_w, new_h, GdkPixbuf.InterpType.BILINEAR)

                    texture = Gdk.Texture.new_for_pixbuf(pix)
        except: pass

        GLib.idle_add(ImageLoader._notify_main_thread, url, texture)

    @staticmethod
    def _notify_main_thread(url, texture):
        if texture:
            with ImageLoader._cache_lock:
                ImageLoader._cache[url] = texture

        with ImageLoader._ongoing_lock:
            callbacks = ImageLoader._ongoing.pop(url, [])

        for (cb, size) in callbacks:
            cb(texture)
        return False
