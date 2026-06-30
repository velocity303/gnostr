subdir('icons')

# Desktop File
desktop_file = i18n.merge_file(
  input: 'tech.livingonlinux.gnostr.desktop',
  output: 'tech.livingonlinux.gnostr.desktop',
  type: 'desktop',
  po_dir: '../po',
  install: true,
  install_dir: datadir / 'applications'
)

# AppData / Metainfo
appdata_file = i18n.merge_file(
  input: 'tech.livingonlinux.gnostr.metainfo.xml',
  output: 'tech.livingonlinux.gnostr.metainfo.xml',
  po_dir: '../po',
  install: true,
  install_dir: datadir / 'metainfo'
)

# GSettings Schema
install_data('tech.livingonlinux.gnostr.gschema.xml',
  install_dir: datadir / 'glib-2.0/schemas'
)
# Compile schemas locally for testing (optional but good practice)
gnome.compile_schemas(
  depend_files: 'tech.livingonlinux.gnostr.gschema.xml'
)
