# The launcher (src/gnostr) adds pkgdatadir/gnostr to sys.path at runtime

pkg_name = 'gnostr'

install_data(
  [
    # Core modules
    '__init__.py',
    'main.py',
    'client.py',
    'database.py',
    'dialogs.py',
    'key_manager.py',
    'nostr_utils.py',
    'renderer.py',
    'connection_status.py',
    'window.py',
  ],
  install_dir: pkgdatadir / pkg_name
)

# Install sub-packages (directories)
subdir('util')
subdir('gateway')
subdir('service')

# Compile and install the GResource bundle (GTK UI files)
gnome.compile_resources(
  pkg_name,
  'gnostr.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
)

# Configure and install the application launcher script
conf = configuration_data()
conf.set('PYTHON', python_installation.full_path())
conf.set('VERSION', meson.project_version())
conf.set('pkgdatadir', pkgdatadir)
conf.set('localedir', datadir / 'locale')

configure_file(
  input: 'gnostr.in',
  output: 'gnostr',
  configuration: conf,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x'
)
