    @staticmethod
    def render(content, window_ref, post_widget_ref=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        
        # Debug: log content status
        print(f"[Renderer] Content type: {type(content)}, length: {len(content) if content else 0}")
        
        # Fix: Handle None/empty content with visible error message
        if not content:
            print("[Renderer] WARNING: Empty or None content received")
            error_label = Gtk.Label(label="[No content to display]", xalign=0)
            error_label.add_css_class("dim-label")
            box.append(error_label)
            return box

        try:
            clean_content = html.unescape(content)
            
            # Debug: show first 200 chars of content
            print(f"[Renderer] Content preview: {clean_content[:200]}")
            
            parts = ContentRenderer.LINK_REGEX.split(clean_content)
            print(f"[Renderer] LINK_REGEX split produced {len(parts)} parts")
            
            # Debug: check if any part matches a URL
            url_parts_found = 0
            for i, part in enumerate(parts):
                if part and ContentRenderer.LINK_REGEX.match(part):
                    url_parts_found += 1
                    print(f"[Renderer] Part {i} matches URL: {part[:80]}")
                    print(f"[Renderer]   is_image: {ContentRenderer.is_image_url(part)}, is_video: {ContentRenderer.is_video_url(part)}")
            
            if url_parts_found == 0:
                print(f"[Renderer] WARNING: No URL parts detected! Checking for common URL patterns...")
                # Check for common URL patterns that might not be caught
                if "http" in clean_content.lower():
                    print(f"[Renderer] Found 'http' in content but regex didn't match")
                if "www." in clean_content.lower():
                    print(f"[Renderer] Found 'www.' in content but regex didn't match")
                if ".com" in clean_content.lower() or ".org" in clean_content.lower():
                    print(f"[Renderer] Found TLD in content but regex didn't match")
            
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
                        print(f"[Renderer] Adding nostr card: {clean_part[:50]}")
                        ContentRenderer._add_nostr_card(
                            box, clean_part, window_ref, post_widget_ref
                        )
                    elif ContentRenderer.is_image_url(clean_part):
                        print(f"[Renderer] Adding image: {clean_part[:80]}")
                        ContentRenderer._add_image(box, clean_part, window_ref)
                    elif ContentRenderer.is_video_url(clean_part):
                        print(f"[Renderer] Adding video: {clean_part[:80]}")
                        ContentRenderer._add_video(box, clean_part, window_ref)
                    elif "youtube.com/watch" in clean_part or "youtu.be/" in clean_part:
                        print(f"[Renderer] Adding YouTube: {clean_part[:80]}")
                        raw_stream_url = get_youtube_stream(clean_part)
                        ContentRenderer._add_video(box, raw_stream_url, window_ref, clean_part)
                    else:
                        print(f"[Renderer] Adding link: {clean_part[:80]}")
                        ContentRenderer._add_link(box, clean_part)

                    if trailing:
                        current_text_buffer.append(trailing)
                else:
                    current_text_buffer.append(part)

            if current_text_buffer:
                ContentRenderer._add_text(box, "".join(current_text_buffer))

            # Debug: check if anything was added
            child_count = sum(1 for _ in box)
            print(f"[Renderer] Rendered {child_count} child widgets\n")
            
        except Exception as e:
            print(f"[Renderer] Render Error: {e}")
            print(f"[Renderer] Traceback: {traceback.format_exc()}")
            error_label = Gtk.Label(label=f"[Render error: {str(e)[:50]}]", xalign=0)
            error_label.add_css_class("dim-label")
            box.append(error_label)

        return box
