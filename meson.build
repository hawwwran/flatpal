project(
  'flatpal',
  version: '0.2.2',
  license: 'GPL-3.0-or-later',
  meson_version: '>= 0.59.0',
)

app_id = 'io.github.hawwwran.flatpal'
python = import('python').find_installation('python3')

subdir('flatpal')
subdir('data')
subdir('bin')

install_data(
  'LICENSE',
  install_dir: get_option('datadir') / 'licenses' / app_id,
)
