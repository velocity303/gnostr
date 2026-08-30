# GNostr - Flatpak application build configuration
#
# This meson.build configures the project for GNOME-style Flatpak builds.
# Python source files are installed to pkgdatadir/gnostr so the launcher
# script can import them at runtime using sys.path manipulation.
project(
  'gnostr',
  [],
  version: '0.1.0',
  meson_version: '>= 1.4.0',
  default_options: [
    'buildtype=debugoptimized',
    'warning_level=2'
  ]
)
gnome = import('gnome')
i18n = import('i18n')
datadir = get_option('datadir')
pkgdatadir = get_option('prefix') / datadir
# Python installation for use by src/gnostr.in launcher
py_mod = import('python')
python_installation = py_mod.find_installation('python3')

# Sub-projects (order matters: po→data→src)
subdir('po')
subdir('data')
subdir('src')

# Run unit tests via pytest (per-file targets for Builder granularity)
pytest_prog = find_program('pytest', required: false)
if pytest_prog.found()
  test_env = {
    'PYTHONPATH': meson.project_source_root(),
  }
  test('test_gateway', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_gateway.py'], env: test_env, timeout: 120)
  test('test_profile_service', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_profile_service.py'], env: test_env, timeout: 120)
  test('test_profile_metadata', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_profile_metadata_service.py'], env: test_env, timeout: 120)
  test('test_resource_management', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_resource_management.py'], env: test_env, timeout: 120)
  test('test_following', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_following.py'], env: test_env, timeout: 120)
  test('test_nip_builders', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_nip_builders.py'], env: test_env, timeout: 120)
  test('test_publish_ack', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_publish_ack.py'], env: test_env, timeout: 120)
  test('test_feed_pagination', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_feed_pagination.py'], env: test_env, timeout: 120)
  test('test_feed_model', pytest_prog, args: ['--verbose', meson.project_source_root() / 'tests/test_feed_model.py'], env: test_env, timeout: 120)
  endif

# Real-GTK feed harness (tier 2): exercises ui.feed_view.FeedView against a
# real Gtk.ScrolledWindow + real FeedModel + real Database. Needs a display
# (xvfb-run) and real GTK4/Adw (the Flatpak SDK python3 in Builder). The
# script self-skips with exit code 77 (meson "skip") when either is
# unavailable, so it is safe to register unconditionally; only registered
# when both programs exist so a build without them stays quiet.
xvfb_run = find_program('xvfb-run', required: false)
gtk_python = find_program('python3', required: false)
if xvfb_run.found() and gtk_python.found()
  test('test_feed_gtk_real', xvfb_run,
    args: ['-a', gtk_python, meson.project_source_root() / 'tests/test_feed_gtk_real.py'],
    timeout: 120)
endif
