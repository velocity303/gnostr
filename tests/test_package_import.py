"""Verifies the gnostr package imports without circular-import errors and that
profile_nips is available as a package attribute (regression test for the
Flatpak circular-import failure)."""


def test_gnostr_package_imports_cleanly():
    import gnostr

    assert hasattr(gnostr, "profile_nips")
    assert hasattr(gnostr, "client")
    assert hasattr(gnostr, "database")


def test_profile_nips_is_stdlib_only():
    """profile_nips must not import gnostr (that caused the circular import)."""
    import inspect

    import gnostr.profile_nips as pn

    src = inspect.getsource(pn)
    assert "import gnostr" not in src
    assert "from gnostr" not in src
