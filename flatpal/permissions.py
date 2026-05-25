"""Parse `flatpak info -m <id>` output and summarise sandbox permissions.

We hand-roll the INI parser because real flatpak metadata can contain duplicate
section headers like `[Extension foo]` and `[Extension bar]`; configparser
allows duplicates only in narrow modes and complicates the API. A flat
hand-rolled parser is easier to reason about and to test.
"""

from __future__ import annotations

from typing import List


def parse_flatpak_metadata(text: str) -> dict:
    """Parse INI-style text into {section_name: {key: value}}.

    Duplicate section names merge keys (last value wins for the same key).
    Lines outside any section, empty lines, and lines without '=' are ignored.
    """
    result: dict = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            result.setdefault(current, {})
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[current][key.strip()] = value.strip()
    return result


def _split_list(value: str) -> list:
    """`network;ipc;` -> ['network', 'ipc']. Tolerates missing trailing semicolon."""
    return [p for p in (s.strip() for s in value.split(";")) if p]


def summarize_permissions(metadata: dict) -> List[dict]:
    """Return a compact list of permission rows for the detail UI.

    Each row: {label, value, icon, granted}. `granted=False` means the row is
    still useful to show but the sandbox does NOT grant this exposure.
    """
    ctx = metadata.get("Context", {})
    shared = set(_split_list(ctx.get("shared", "")))
    sockets = set(_split_list(ctx.get("sockets", "")))
    devices = set(_split_list(ctx.get("devices", "")))
    filesystems = _split_list(ctx.get("filesystems", ""))
    features = set(_split_list(ctx.get("features", "")))

    rows: List[dict] = []

    # Network
    rows.append({
        "label": "Network",
        "value": "Full access" if "network" in shared else "No access",
        "icon": "network-wireless-symbolic",
        "granted": "network" in shared,
    })

    # Inter-process communication
    rows.append({
        "label": "Inter-process",
        "value": "Host IPC" if "ipc" in shared else "Sandboxed",
        "icon": "system-run-symbolic",
        "granted": "ipc" in shared,
    })

    # Display
    display_parts = []
    if "wayland" in sockets:
        display_parts.append("Wayland")
    if "x11" in sockets:
        display_parts.append("X11")
    elif "fallback-x11" in sockets:
        display_parts.append("X11 (fallback)")
    rows.append({
        "label": "Display",
        "value": ", ".join(display_parts) if display_parts else "None",
        "icon": "video-display-symbolic",
        "granted": bool(display_parts),
    })

    # Audio
    audio_parts = []
    if "pulseaudio" in sockets:
        audio_parts.append("PulseAudio")
    if "pipewire" in sockets:
        audio_parts.append("PipeWire")
    rows.append({
        "label": "Audio",
        "value": ", ".join(audio_parts) if audio_parts else "None",
        "icon": "audio-x-generic-symbolic",
        "granted": bool(audio_parts),
    })

    # Devices
    if "all" in devices:
        dev_value = "All devices"
    else:
        wanted = [d for d in devices if d not in {"none"}]
        dev_value = ", ".join(wanted).title() if wanted else "None"
    rows.append({
        "label": "Devices",
        "value": dev_value,
        "icon": "drive-harddisk-symbolic",
        "granted": "all" in devices or any(d != "none" for d in devices),
    })

    # Filesystems
    if filesystems:
        fs_label_map = {
            "host": "All host files",
            "home": "Home directory",
            "host-os": "OS files",
            "host-etc": "/etc",
        }
        labels = [fs_label_map.get(f, f) for f in filesystems]
        fs_value = ", ".join(labels)
        granted = True
    else:
        fs_value = "Sandbox only"
        granted = False
    rows.append({
        "label": "Filesystem",
        "value": fs_value,
        "icon": "folder-symbolic",
        "granted": granted,
    })

    # D-Bus surfaces summary (counts only)
    sess = len(metadata.get("Session Bus Policy", {}))
    syst = len(metadata.get("System Bus Policy", {}))
    if sess or syst:
        parts = []
        if sess:
            parts.append(f"{sess} session bus")
        if syst:
            parts.append(f"{syst} system bus")
        rows.append({
            "label": "D-Bus",
            "value": ", ".join(parts),
            "icon": "preferences-system-symbolic",
            "granted": True,
        })

    # Bluetooth / features
    if "bluetooth" in features:
        rows.append({
            "label": "Bluetooth",
            "value": "Granted",
            "icon": "bluetooth-active-symbolic",
            "granted": True,
        })

    return rows
