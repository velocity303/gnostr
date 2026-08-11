"""SearchDialog construction smoke test — verifies the dialog builds without
GTK assertion failures under the mocked gi.repository (same limitation as
other Adw.Window subclasses: construction is verified on-device)."""

from unittest.mock import MagicMock


def test_search_dialog_constructs():
    mw = MagicMock()
    mw.client = MagicMock()
    mw.db = MagicMock()
    mw.content_nav = MagicMock()
    from gnostr.dialogs import SearchDialog

    dlg = SearchDialog(mw)
    assert dlg is not None
