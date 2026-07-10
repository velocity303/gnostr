#!/usr/bin/env python3
"""
Fix for video/GIF playback in Gnostr Flatpak.
Uses GStreamer pipeline with appsink and Gtk.Picture for frame rendering.
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gst', '1.0')
from gi.repository import Gtk, Adw, GLib, Gdk, GdkPixbuf, Pango, Gst
import threading
import urllib.request
import concurrent.futures

class VideoPlayer:
    """GStreamer-based video player using appsink and Gtk.Picture."""

    @staticmethod
    def load_and_play(url, container, spinner, window_ref=None):
        """Load and play a video or animated GIF."""
        def on_ready(video):
            if spinner and spinner.get_parent() == container:
                container.remove(spinner)
            if video:
                # Remove from any existing parent before reusing
                if video.get_parent() is not None:
                    parent = video.get_parent()
                    if parent is not None:
                        parent.remove(video)
                video.set_halign(Gtk.Align.FILL)
                video.set_valign(Gtk.Align.FILL)
                container.append(video)
            else:
                container.append(Gtk.Image.new_from_icon_name("video-symbolic"))

        VideoPlayer._load_video(url, on_ready)

    @staticmethod
    def _load_video(url, callback):
        """Load video using GStreamer pipeline with appsink."""
        try:
            print(f"VideoPlayer._load_video({url})")
            Gst.init(None)
            print(f"Gst.init() succeeded")

            # Build a GStreamer pipeline
            pipeline = Gst.parse_launch(
                f"uridecodebin uri={url} ! "
                f"videoconvert ! videoscale ! "
                f"queue ! appsink name=sink"
            )
            print(f"Gst.parse_launch() succeeded for {url}")

            # Create a Gtk.Picture widget
            video = Gtk.Picture()
            video.set_default_size(640, 360)
            video.set_halign(Gtk.Align.FILL)
            video.set_valign(Gtk.Align.FILL)
            print(f"Gtk.Picture created for {url}")

            # Start the pipeline
            pipeline.set_state(Gst.State.PLAYING)

            # Capture frames from the appsink and update the picture
            sink = pipeline.get_by_name("sink")
            VideoPlayer._capture_frames(sink, video)

        except Exception as gst_e:
            print(f"GStreamer pipeline failed: {gst_e}")
            import traceback
            traceback.print_exc()
            video = Gtk.Label(label="Video not supported")
            # Try alternative: just show a placeholder
            video = Gtk.Picture.new_for_paintable(None)

        GLib.idle_add(callback, video)

    @staticmethod
    def _capture_frames(sink, picture):
        """Capture frames from the appsink and update the picture."""
        try:
            sample = sink.emit("pull-sample", 500)
            if sample:
                buffer = sample.get_buffer()
                caps = sample.get_caps()
                width, height = VideoPlayer._get_resolution(caps)
                pixbuf = VideoPlayer._buffer_to_pixbuf(buffer, width, height)
                if pixbuf:
                    texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                    picture.set_paintable(texture)
                    # Schedule next frame capture
                    GLib.timeout_add_seconds(1, lambda: VideoPlayer._capture_frames(sink, picture))
                else:
                    print("Failed to convert buffer to pixbuf")
        except Exception as e:
            print(f"Error capturing frame: {e}")

    @staticmethod
    def _get_resolution(caps):
        """Get width and height from GStreamer caps."""
        width, height = 640, 360
        if caps:
            structure = caps.get_structure(0)
            if structure:
                width = structure.get_int("width")[1]
                height = structure.get_int("height")[1]
        return width, height

    @staticmethod
    def _buffer_to_pixbuf(buffer, width, height):
        """Convert GStreamer buffer to GdkPixbuf."""
        try:
            # Map the buffer
            success, map_info = buffer.map("read")
            if not success:
                return None

            # Get the data
            data = bytes(map_info.data)

            # Create a pixbuf from the raw data
            # Note: This is a simplified conversion.
            # For full support, you'd need to handle different pixel formats.
            pixbuf = GdkPixbuf.Pixbuf.new_from_data(
                data,
                GdkPixbuf.Colorspace.RGB,
                False,  # has alpha
                8,      # bits per sample
                width,
                height,
                width * 4,  # row stride
                None      # destroy closure
            )

            # Unmap the buffer
            buffer.unmap(map_info)

            return pixbuf
        except Exception as e:
            print(f"Error converting buffer to pixbuf: {e}")
            return None


# Test the fix
if __name__ == "__main__":
    # Test with a sample video URL
    test_url = "https://sample-videos.com/video123/mp4/720/big_buck_bunny_720p_1mb.mp4"

    # Create a simple test window
    win = Gtk.Window()
    win.set_title("Video Player Test")
    win.set_default_size(800, 600)

    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    win.set_child(vbox)

    # Create a container for the video
    video_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    vbox.append(video_container)

    # Create a spinner
    spinner = Gtk.Spinner()
    spinner.start()
    video_container.append(spinner)

    # Load and play the video
    VideoPlayer.load_and_play(test_url, video_container, spinner)

    win.connect("destroy", lambda x: Gtk.main_quit())
    win.show()
    Gtk.main()
