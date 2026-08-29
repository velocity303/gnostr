"""Flatpak install-list contract guard.

Regression (c4f182d): profile_nips.py shipped in the repo but was missing from
src/gnostr/meson.build install_data, so the Flatpak crashed at import while
everything worked in a dev checkout. This test parses every meson.build under
src/ and asserts each package directory's *.py files appear in its install_data
list — add a module without installing it and CI goes red instead of Flatpak.
"""

import os
import re

SRC_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr")
)


def _installed_py_files(meson_build_path):
    """Extract the file names listed in install_data([...]) of a meson.build."""
    with open(meson_build_path, encoding="utf-8") as f:
        text = f.read()
    installed = set()
    for block in re.findall(r"install_data\s*\(\s*\[(.*?)\]", text, re.DOTALL):
        installed.update(re.findall(r"'([^'\"]+\.py)'", block))
    # bare install_data('file.py', ...) (no list form)
    for single in re.findall(r"install_data\s*\(\s*'([^'\"]+\.py)'", text):
        installed.add(single)
    return installed


def _package_dirs_under_src():
    """All dirs under src/gnostr that contain a meson.build."""
    dirs = []
    for root, _subdirs, files in os.walk(SRC_DIR):
        if "meson.build" in files:
            dirs.append(root)
    return dirs


def test_every_package_dir_has_a_meson_build():
    """Each sub-package of gnostr must have its own meson.build (installed via
    subdir()) — a new sub-package without one is never shipped."""
    # The root install lists subdirs explicitly; verify each subdir() target exists.
    root_meson = os.path.join(SRC_DIR, "meson.build")
    with open(root_meson, encoding="utf-8") as f:
        text = f.read()
    subdirs = re.findall(r"subdir\('([^']+)'\)", text)
    for sub in subdirs:
        assert os.path.isfile(
            os.path.join(SRC_DIR, sub, "meson.build")
        ), f"subdir('{sub}') declared but src/gnostr/{sub}/meson.build missing"
    # And every immediate sub-package dir with .py files must be declared.
    for entry in sorted(os.listdir(SRC_DIR)):
        entry_dir = os.path.join(SRC_DIR, entry)
        if not os.path.isdir(entry_dir) or entry == "__pycache__":
            continue
        has_py = any(f.endswith(".py") for f in os.listdir(entry_dir))
        if has_py:
            assert entry in subdirs, (
                f"src/gnostr/{entry}/ contains .py files but is not declared "
                f"via subdir('{entry}') in src/gnostr/meson.build — it will be "
                f"missing from the Flatpak"
            )


def test_all_core_modules_are_in_install_list():
    """Every src/gnostr/*.py must appear in the root install_data list."""
    installed = _installed_py_files(os.path.join(SRC_DIR, "meson.build"))
    on_disk = {
        f for f in sorted(os.listdir(SRC_DIR)) if f.endswith(".py")
    }
    missing = on_disk - installed
    assert not missing, (
        f"modules present in src/gnostr/ but missing from install_data in "
        f"src/gnostr/meson.build (will break the Flatpak at import): "
        f"{sorted(missing)}"
    )


def test_all_subpackage_modules_are_in_install_lists():
    """Same contract for each sub-package's own meson.build."""
    for pkg_dir in _package_dirs_under_src():
        if pkg_dir == SRC_DIR:
            continue  # covered by test_all_core_modules_are_in_install_list
        installed = _installed_py_files(os.path.join(pkg_dir, "meson.build"))
        on_disk = {f for f in os.listdir(pkg_dir) if f.endswith(".py")}
        missing = on_disk - installed
        rel = os.path.relpath(pkg_dir, os.path.join(SRC_DIR, ".."))
        assert not missing, (
            f"modules in {rel}/ missing from its meson.build install_data "
            f"(Flatpak import crash waiting to happen): {sorted(missing)}"
        )
