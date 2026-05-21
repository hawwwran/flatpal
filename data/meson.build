install_data(
  '@0@.desktop'.format(app_id),
  install_dir: get_option('datadir') / 'applications',
)

install_data(
  '@0@.metainfo.xml'.format(app_id),
  install_dir: get_option('datadir') / 'metainfo',
)

# All native pixmap sizes ship into hicolor/<size>x<size>/apps. Source
# files keep their flatpal-icon-* basenames in the repo; install renames
# them to <APP_ID>.png so GNOME Shell matches the running window's
# xdg-toplevel app_id (= Adw application_id) for the taskbar icon.
# Flatpak rejects hicolor icons larger than 512×512 at export time, so the
# 1024×1024 master in data/ ships only as a source asset (used by the icon
# pipeline, the .desktop dialog preview on the host, and tools/preview_*).
icon_sizes = [16, 24, 32, 48, 64, 96, 128, 192, 256, 512]
foreach size : icon_sizes
  install_data(
    'flatpal-icon-@0@x@0@.png'.format(size),
    install_dir: get_option('datadir') / 'icons' / 'hicolor' / '@0@x@0@'.format(size) / 'apps',
    rename: '@0@.png'.format(app_id),
  )
endforeach
