project(
  'flatpal',
  version: '0.5.0',
  license: 'GPL-3.0-or-later',
  meson_version: '>= 0.59.0',
)

app_id = 'io.github.hawwwran.flatpal'
python = import('python').find_installation('python3')

subdir('flatpal')
subdir('data')

install_data(
  'LICENSE',
  install_dir: get_option('datadir') / 'licenses' / app_id,
)

configure_file(
  input: 'flatpal.in',
  output: 'flatpal',
  configuration: {'PYTHON': python.full_path()},
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)
