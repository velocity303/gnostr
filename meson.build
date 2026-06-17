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
endif
