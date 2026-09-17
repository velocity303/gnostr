"""Portrait-fit regression (Librem 5 field bug): the profile page must scroll
as a whole and cap banner height, and the welcome page must separate its two
pill buttons. The mocked-GTK test env can't introspect real construction, so
these are source-level guards, same pattern as test_export_key_dialog.py.
"""

PROFILE = "src/gnostr/ui/profile_view.py"
MAIN = "src/gnostr/main.py"
RENDERER = "src/gnostr/renderer.py"


def test_profile_view_wraps_whole_page_in_one_scrolledwindow():
    text = open(PROFILE).read()
    assert "self._scroll = Gtk.ScrolledWindow()" in text
    assert "self.set_child(self._scroll)" in text
    assert "self._scroll.set_child(self.layout)" in text
    # exactly ONE ScrolledWindow in the file: the nested posts-only one was
    # removed (it fought the outer one for touch events)
    assert text.count("Gtk.ScrolledWindow()") == 1


def test_profile_banner_height_capped():
    text = open(PROFILE).read()
    assert "max_height=200" in text
    # renderer honors the cap with cover-crop
    r = open(RENDERER).read()
    assert "max_height=None" in r
    assert "Gtk.ContentFit.COVER" in r


def test_welcome_buttons_have_vertical_spacing():
    text = open(MAIN).read()
    assert 'label="Login"' in text
    # the button box must declare spacing (was 0 -> pills glued together)
    import re

    m = re.search(r"bx = Gtk\.Box\((.*?)\)", text, re.S)
    assert m and "spacing=" in m.group(1), "welcome button box lacks spacing"
