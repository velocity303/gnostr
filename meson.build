project('gnostr',
  version: '0.1.0',
  meson_version: '>= 0.59.0',
)

i18n = import('i18n')
gnome = import('gnome')
python = import('python')

# Locate Python installation
python_installation = python.find_installation('python3')

# Standard variables
prefix = get_option('prefix')
bindir = prefix / get_option('bindir')
datadir = prefix / get_option('datadir')
pkgdatadir = datadir / meson.project_name()
localedir = prefix / get_option('localedir')

# Dependencies
dependency('gtk4', version: '>= 4.10.0')
dependency('libadwaita-1', version: '>= 1.2.0')

subdir('data')
subdir('src')
subdir('po')