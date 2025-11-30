import re
import html
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Pango', '1.0')
from gi.repository import Gtk, Adw, GLib, Gdk, GdkPixbuf, Pango
import urllib.request
import threading
import nostr_utils

class ContentRenderer:
    LINK_REGEX = re.compile(r'((?:https?://|nostr:)[^\s]+)')
    IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
    VIDEO_EXTS = ('.mp4', '.mov', '.webm') 

    @staticmethod
    def render(content, window_ref):
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
                    lower = clean_part.lower()
                    
                    if clean_part.startswith("nostr:"): 
                        ContentRenderer._add_nostr_card(box, clean_part, window_ref)
                    elif lower.endswith(ContentRenderer.IMAGE_EXTS): 
                        ContentRenderer._add_image(box, clean_part)
                    elif lower.endswith(ContentRenderer.VIDEO_EXTS): 
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
        img_box = Gtk.Box(halign=Gtk.Align.START, margin_top=6, margin_bottom=6)
        spinner = Gtk.Spinner(); spinner.start(); img_box.append(spinner); box.append(img_box)
        ImageLoader.load_image_into_widget(url, img_box, spinner)

    @staticmethod
    def _add_nostr_card(box, uri, window):
        try:
            parts = uri.split(":")
            if len(parts) < 2: return
            
            bech32_str = parts[1]
            is_event = "nevent" in uri or "note" in uri
            is_profile = "nprofile" in uri or "npub" in uri
            
            btn = Gtk.Button(css_classes=["flat", "card"])
            row = Adw.ActionRow()
            row.set_activatable(False)
            
            short_id = bech32_str[:10] + "..." + bech32_str[-6:]
            
            if is_event:
                row.set_title("Quoted Event")
                row.set_subtitle(short_id)
                row.add_prefix(Gtk.Image.new_from_icon_name("chat-bubble-symbolic"))
                def on_click_evt(b):
                    hex_id = ContentRenderer._extract_hex_id(bech32_str)
                    if hex_id: window.show_thread(hex_id, "Unknown", "Loading...")
                    else: _launch_ext(window, bech32_str)
                btn.connect("clicked", on_click_evt)
                
            elif is_profile:
                row.set_title("User Profile")
                row.set_subtitle(short_id)
                row.add_prefix(Gtk.Image.new_from_icon_name("avatar-default-symbolic"))
                def on_click_prof(b):
                    hex_pk = ContentRenderer._extract_hex_id(bech32_str) 
                    if hex_pk: window.show_profile(hex_pk)
                    else: _launch_ext(window, bech32_str)
                btn.connect("clicked", on_click_prof)
            
            btn.set_child(row)
            box.append(btn)
        except Exception as e:
            print(f"Card Render Error: {e}")

    @staticmethod
    def _extract_hex_id(bech32_str):
        try:
            hrp, data = nostr_utils.bech32_decode(bech32_str)
            if not data: return None
            
            # 5-bit to 8-bit conversion
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
    @staticmethod
    def load_avatar(url, callback): ImageLoader._fetch(url, callback, size=(64,64))
    @staticmethod
    def load_image_into_widget(url, container, spinner):
        def on_ready(texture):
            if spinner: container.remove(spinner)
            if texture:
                p = Gtk.Picture.new_for_paintable(texture)
                p.set_can_shrink(True); p.set_content_fit(Gtk.ContentFit.SCALE_DOWN); p.set_halign(Gtk.Align.START)
                container.append(p)
            else: container.append(Gtk.Image.new_from_icon_name("image-missing-symbolic"))
        ImageLoader._fetch(url, on_ready)
    @staticmethod
    def _fetch(url, callback, size=None):
        def _bg():
            try:
                if not url.startswith("http"): GLib.idle_add(callback, None); return
                req = urllib.request.Request(url, headers={'User-Agent': 'Gnostr/1.0'})
                with urllib.request.urlopen(req, timeout=10) as r: data = r.read()
                loader = GdkPixbuf.PixbufLoader(); loader.write(data); loader.close()
                pix = loader.get_pixbuf()
                if not pix: GLib.idle_add(callback, None); return
                if size: pix = pix.scale_simple(size[0], size[1], GdkPixbuf.InterpType.BILINEAR)
                GLib.idle_add(callback, Gdk.Texture.new_for_pixbuf(pix))
            except: GLib.idle_add(callback, None)
        threading.Thread(target=_bg, daemon=True).start()
