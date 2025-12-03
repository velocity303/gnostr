subdir('icons')

# Desktop File
desktop_file = i18n.merge_file(
  input: 'me.velocitynet.Gnostr.desktop',
  output: 'me.velocitynet.Gnostr.desktop',
  type: 'desktop',
  po_dir: '../po',
  install: true,
  install_dir: datadir / 'applications'
)

# AppData / Metainfo
appdata_file = i18n.merge_file(
  input: 'me.velocitynet.Gnostr.metainfo.xml',
  output: 'me.velocitynet.Gnostr.metainfo.xml',
  po_dir: '../po',
  install: true,
  install_dir: datadir / 'metainfo'
)

# GSettings Schema
install_data('me.velocitynet.Gnostr.gschema.xml',
  install_dir: datadir / 'glib-2.0/schemas'
)
# Compile schemas locally for testing (optional but good practice)
gnome.compile_schemas(
  depend_files: 'me.velocitynet.Gnostr.gschema.xml'
)
