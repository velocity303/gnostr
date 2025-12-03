# Install Python sources into a 'gnostr' package subdirectory
# This allows 'from gnostr import main' to work in the launcher
gnostr_sources = [
  'main.py',
  'client.py',
  'database.py',
  'dialogs.py',
  'key_manager.py',
  'nostr_utils.py',
  'renderer.py',
  'window.py',
  # Add any other python files you have here
]

install_data(gnostr_sources, 
  install_dir: pkgdatadir / 'gnostr'
)

# Compile and install the resource bundle (UI files)
gnome.compile_resources('gnostr',
  'gnostr.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
)

# Configure the launcher script (gnostr.in)
conf = configuration_data()
conf.set('PYTHON', python_installation.full_path())
conf.set('VERSION', meson.project_version())
conf.set('pkgdatadir', pkgdatadir)
conf.set('localedir', localedir)

configure_file(
  input: 'gnostr.in',
  output: 'gnostr',
  configuration: conf,
  install: true,
  install_dir: bindir,
  install_mode: 'rwxr-xr-x'
)
