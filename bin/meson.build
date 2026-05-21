# Launcher script generation. Lives in its own build subdir so the
# generated file at <builddir>/bin/flatpal doesn't clash with the
# Python-package build dir at <builddir>/flatpal/ (which subdir('flatpal')
# in the root meson.build creates).
configure_file(
  input: '../flatpal.in',
  output: 'flatpal',
  configuration: {'PYTHON': python.full_path()},
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)
