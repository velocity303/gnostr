application_id = 'me.velocitynet.Gnostr'

# Scalable icon (256x256+)
scalable_src = join_paths('hicolor', 'scalable', 'apps')
install_data(
  join_paths(scalable_src, application_id + '.svg'),
  install_dir: get_option('datadir') / 'icons' / scalable_src,
)

# Symbolic icon (for Adwaita theming integration)
symbolic_src = join_paths('hicolor', 'symbolic', 'apps')
install_data(
  join_paths(symbolic_src, application_id + '-symbolic.svg'),
  install_dir: get_option('datadir') / 'icons' / symbolic_src,
)
