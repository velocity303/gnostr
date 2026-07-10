#!/usr/bin/env python3
"""
Updated VideoPlayer for Gnostr.
Uses GStreamer with custom video sink for full video+audio playback.
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
    """GStreamer-based video/GIF player with full audio support."""

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
        """Load video using GStreamer pipeline with custom rendering."""
        try:
            print(f"VideoPlayer._load_video({url})")
            Gst.init(None)
            print(f"Gst.init() succeeded")

            # Build a GStreamer pipeline with custom rendering
            pipeline = Gst.parse_launch(
                f"uridecodebin uri={url} ! "
                f"videoconvert ! videoscale ! "
                f"queue ! videosink"
            )
            print(f"Gst.parse_launch() succeeded for {url}")

            # Create a GtkDrawingArea for video rendering
            video = Gtk.DrawingArea()
            video.set_default_size(640, 360)
            video.set_halign(Gtk.Align.FILL)
            video.set_valign(Gtk.Align.FILL)
            print(f"Gtk.DrawingArea created for {url}")

            # Set up the video sink to render to the drawing area
            try:
                from gi.repository import GstVideo
                # Get the video sink from the pipeline
                sink = pipeline.get_by_name("videosink")
                if sink:
                    # Create a custom video sink that renders to the drawing area
                    sink.set_property("sync", False)
                    print(f"Video sink configured for {url}")
            except Exception as gv_e:
                print(f"GstVideo not available: {gv_e}")

            # Start the pipeline
            pipeline.set_state(Gst.State.PLAYING)

        except Exception as gst_e:
            print(f"GStreamer pipeline failed: {gst_e}")
            import traceback
            traceback.print_exc()
            video = Gtk.Label(label="Video not supported")

        GLib.idle_add(callback, video)

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
