project(
  'gnostr',
  [],
  version: '0.1.0'
)

# 1. Fetch the python site-packages path from the container environment
py_mod = import('python')
py_installation = py_mod.find_installation('python3')
python_site_packages = py_installation.get_install_dir()

pytest_exe = find_program('pytest', required: true)
build_root = meson.project_source_root()

# --- TESTING CONFIGURATION ---
test(
  'unit-tests',
  pytest_exe,
  args : [
    '--verbose',
    build_root / 'tests'
  ],
  env : ['PYTHONPATH=' + build_root]
)

# --- INSTALLATION CONFIGURATION ---

# 2. FIXED: Use install_data for an explicit file list, targeting site-packages/gnostr/
install_data(
  [
    'src/__init__.py',       # Don't forget your init file if you have one!
    'src/main.py',
    'src/key_manager.py',    # Make sure your key_manager is included here too
    'src/service/profile_service.py',
    'src/service/profile_metadata_service.py',
    'src/service/feed_service.py',
    'src/nostr_utils.py',
    'src/database.py',
    'src/renderer.py',
    'src/dialogs.py',
    'src/client.py'
  ],
  install_dir: python_site_packages / 'gnostr'
)

# 3. Install the application executable launcher wrapper script
install_data(
  'src/gnostr',
  install_dir : get_option('bindir'),
  install_mode: 'rwxr-xr-x'
)