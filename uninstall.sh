#!/usr/bin/env bash
# Remove Flatpal from user-local XDG paths.
set -u

PREFIX="${HOME}/.local"
BIN_DIR="${PREFIX}/bin"
APP_DIR="${PREFIX}/share/flatpal"
DESKTOP_DIR="${PREFIX}/share/applications"
ICON_HICOLOR="${PREFIX}/share/icons/hicolor"

# Must match install.sh's set so we sweep every size we installed.
ICON_SIZES=(16 24 32 48 64 96 128 192 256 512)

removed=0
remove_file() {
  if [[ -e "$1" || -L "$1" ]]; then
    rm -f "$1" && { echo "  removed $1"; removed=$((removed+1)); }
  fi
}

remove_file "${BIN_DIR}/flatpal"
remove_file "${DESKTOP_DIR}/flatpal.desktop"

for size in "${ICON_SIZES[@]}"; do
  dir="${ICON_HICOLOR}/${size}x${size}/apps"
  remove_file "${dir}/flatpal.png"
  remove_file "${dir}/com.hawwwran.flatpal.png"
done

# Legacy paths from earlier installs — clean up if still around.
remove_file "${ICON_HICOLOR}/scalable/apps/flatpal.svg"
remove_file "${APP_DIR}/flatpal.py"

if [[ -d "${APP_DIR}" ]]; then
  rm -rf "${APP_DIR}" && { echo "  removed ${APP_DIR}/"; removed=$((removed+1)); }
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${DESKTOP_DIR}" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "${ICON_HICOLOR}" >/dev/null 2>&1 || true
fi

if [[ "${removed}" -eq 0 ]]; then
  echo "Nothing to remove — Flatpal is not installed."
else
  echo "Removed ${removed} item(s)."
fi
