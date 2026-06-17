# ##### BEGIN GPL LICENSE BLOCK #####
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# ##### END GPL LICENSE BLOCK #####


import importlib
import logging
import math
import re
import sys
import time
from collections.abc import Iterable
from pathlib import Path
import tomllib
from functools import cache
import bpy
from replication.constants import (CONNECTING, FETCHED, STATE_ACTIVE, STATE_AUTH,
                                   STATE_CONFIG, STATE_INITIAL, STATE_LOBBY,
                                   STATE_QUITTING, STATE_SRV_SYNC,
                                   STATE_SYNCING, STATE_WAITING)


NETWORK_LOGGER_NAME = "multi_user.network"
_last_connect_target = ("", 5555)
_connected_session_info = {
    "host": "",
    "port": 5555,
    "server_name": "",
    "use_server_password": False,
    "use_admin_password": False,
    "is_host": False,
}
_session_log_path = ""
_network_log_buffer = []
_NETWORK_LOG_BUFFER_MAX = 2000
SESSION_STATE_LABELS = {
    STATE_INITIAL: "OFFLINE",
    STATE_AUTH: "AUTHENTICATION",
    CONNECTING: "CONNECTING",
    STATE_LOBBY: "LOBBY",
    STATE_SYNCING: "FETCHING",
    STATE_SRV_SYNC: "PUSHING",
    STATE_ACTIVE: "ONLINE",
    STATE_QUITTING: "QUITTING",
}


def get_network_logger():
    return logging.getLogger(NETWORK_LOGGER_NAME)


def network_log(level, message, *args):
    """Log and mirror to the system console for Blender's scripting / console view."""
    logger = get_network_logger()
    logger.log(level, message, *args)
    try:
        text = message % args if args else message
    except TypeError:
        text = message
    line = f"[MULTIUSER-NET] {text}"
    print(line, flush=True)
    _network_log_buffer.append(line)
    if len(_network_log_buffer) > _NETWORK_LOG_BUFFER_MAX:
        del _network_log_buffer[:len(_network_log_buffer) - _NETWORK_LOG_BUFFER_MAX]


def set_session_log_file(path: str):
    global _session_log_path
    _session_log_path = path


def get_active_session_log_path():
    if _session_log_path and Path(_session_log_path).is_file():
        return _session_log_path
    import logging
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            return handler.baseFilename
    return None


def find_latest_session_log(cache_directory: str):
    cache = Path(cache_directory)
    if not cache.is_dir():
        return None
    logs = sorted(
        cache.glob("multiuser_*.log"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return str(logs[0]) if logs else None


def get_network_log_buffer_text():
    return "\n".join(_network_log_buffer)


def read_log_file(path: str):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def open_log_in_text_editor(context, content: str, title: str, filepath: str | None = None):
    """Show log text in a Text Editor area (reuse or create one)."""
    text_name = f"MU_Log_{title}" if title else "MU_Log"
    text = bpy.data.texts.get(text_name)
    if text is None:
        text = bpy.data.texts.new(text_name)
    text.clear()
    text.write(content)
    if filepath:
        text.filepath = filepath

    screen = context.window.screen
    for area in screen.areas:
        if area.type == 'TEXT_EDITOR':
            space = area.spaces.active
            space.text = text
            space.show_line_numbers = True
            space.show_word_wrap = True
            return text

    for area in screen.areas:
        if area.type in {'VIEW_3D', 'PROPERTIES', 'PREFERENCES', 'DOPESHEET_EDITOR'}:
            area.type = 'TEXT_EDITOR'
            space = area.spaces.active
            space.text = text
            space.show_line_numbers = True
            space.show_word_wrap = True
            return text

    return text


def open_log_externally(path: str):
    import os
    import platform
    import subprocess

    abs_path = str(Path(path).resolve())
    system = platform.system()
    if system == "Windows":
        os.startfile(abs_path)
    elif system == "Darwin":
        subprocess.Popen(["open", abs_path])
    else:
        subprocess.Popen(["xdg-open", abs_path])
    return abs_path


def set_connected_session_info(
    host: str,
    port: int,
    server_name: str = "",
    use_server_password: bool = False,
    use_admin_password: bool = False,
    is_host: bool = False,
):
    global _last_connect_target, _connected_session_info
    _last_connect_target = (host, port)
    _connected_session_info = {
        "host": host,
        "port": port,
        "server_name": server_name or "(unnamed)",
        "use_server_password": use_server_password,
        "use_admin_password": use_admin_password,
        "is_host": is_host,
    }


def clear_connected_session_info():
    global _connected_session_info
    _connected_session_info = {
        "host": "",
        "port": 5555,
        "server_name": "",
        "use_server_password": False,
        "use_admin_password": False,
        "is_host": False,
    }


def get_connected_session_info(context=None):
    """Return endpoint metadata for the active or last-connected session."""
    info = dict(_connected_session_info)
    if info["host"]:
        return info

    if context is not None:
        settings = get_preferences()
        presets = settings.server_preset
        if presets:
            index = context.window_manager.server_index
            if index > len(presets) - 1:
                index = 0
            preset = presets[index]
            return {
                "host": preset.ip,
                "port": preset.port,
                "server_name": preset.server_name,
                "use_server_password": preset.use_server_password,
                "use_admin_password": preset.use_admin_password,
                "is_host": False,
            }

    host, port = _last_connect_target
    return {
        "host": host,
        "port": port,
        "server_name": "",
        "use_server_password": False,
        "use_admin_password": False,
        "is_host": False,
    }


def log_session_state_change(previous_state, current_state, reason: str = ""):
    previous = SESSION_STATE_LABELS.get(previous_state, str(previous_state))
    current = SESSION_STATE_LABELS.get(current_state, str(current_state))
    network_log(logging.INFO, "session state: %s -> %s", previous, current)
    if reason:
        network_log(logging.INFO, "session detail: %s", reason)
    if current_state == STATE_AUTH:
        network_log(
            logging.INFO,
            "waiting for server auth reply (timeout = connection timeout in preferences)",
        )



IP_REGEX = re.compile(
    r"^(([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.){3}"
    r"([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])$"
)
HOSTNAME_REGEX = re.compile(
    r"^(([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9\-]*[a-zA-Z0-9])\.)*"
    r"([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9\-]*[A-Za-z0-9])$"
)


def normalize_server_address(value: str):
    """Split host[:port] pasted from playit.gg or similar into host and port."""
    value = value.strip()
    if not value:
        return value, None

    if value.startswith("["):
        end = value.find("]")
        if end != -1 and len(value) > end + 1 and value[end + 1] == ":":
            host = value[1:end]
            port_str = value[end + 2:]
            if port_str.isdigit():
                return host, int(port_str)
        return value, None

    if ":" in value:
        host, _, port_str = value.rpartition(":")
        if host and port_str.isdigit():
            return host.strip(), int(port_str)

    return value, None


def validate_server_host(host: str):
    host = host.strip()
    if IP_REGEX.fullmatch(host):
        return host
    if HOSTNAME_REGEX.fullmatch(host):
        return host
    return None


CLEARED_DATABLOCKS = [
    "actions",
    "armatures",
    "cache_files",
    "cameras",
    "collections",
    "curves",
    "filepath",
    "fonts",
    "grease_pencils",
    "grease_pencils_v3",
    "images",
    "lattices",
    "libraries",
    "lightprobes",
    "lights",
    "linestyles",
    "masks",
    "materials",
    "meshes",
    "metaballs",
    "movieclips",
    "node_groups",
    "objects",
    "paint_curves",
    "particles",
    "scenes",
    "shape_keys",
    "sounds",
    "speakers",
    "texts",
    "textures",
    "volumes",
    "worlds",
]


def find_from_attr(attr_name, attr_value, list):
    for item in list:
        if getattr(item, attr_name, None) == attr_value:
            return item
    return None


ASSET_TYPE_IDS = frozenset({'file', 'images', 'materials'})
FINALIZE_BLOCKING_TYPE_IDS = frozenset({'file', 'images', 'materials', 'node_groups'})
TEXTURE_SHADING_TYPES = frozenset({'MATERIAL', 'RENDERED'})


def printProgressBar(iteration, total, prefix='', suffix='', decimals=1, length=100, fill='█', fill_empty='  '):
    """Build a text progress bar for UI overlays."""
    if total == 0:
        return ""
    filledLength = int(length * iteration // total)
    bar = fill * filledLength + fill_empty * (length - filledLength)
    return f"{prefix} |{bar}| {iteration}/{total}{suffix}"


def is_texture_shading_active(context: bpy.types.Context) -> bool:
    """True when any 3D viewport in the window uses Material or Rendered shading."""
    window = getattr(context, 'window', None)
    if window is None:
        return False
    for area in window.screen.areas:
        if area.type != 'VIEW_3D':
            continue
        for space in area.spaces:
            if space.type == 'VIEW_3D' and space.shading.type in TEXTURE_SHADING_TYPES:
                return True
    return False


def get_asset_sync_progress() -> tuple[int, int]:
    """Return (applied, total) for asset datablocks in the replication graph."""
    from replication.interface import session
    from .bl_types.bl_material import get_material_node_tree_finalize_progress

    if not session or not getattr(session, 'repository', None):
        return (0, 0)

    total = 0
    applied = 0
    for node in session.repository.graph.values():
        if not node.data:
            continue
        if node.data.get('type_id') not in ASSET_TYPE_IDS:
            continue
        total += 1
        if node.state != FETCHED:
            applied += 1

    finalized, finalize_total = get_material_node_tree_finalize_progress()
    total += finalize_total
    applied += finalized

    return (applied, total)


def is_deferred_asset_type(type_id: str | None) -> bool:
    return type_id in ASSET_TYPE_IDS


def textures_fetch_enabled(context: bpy.types.Context | None = None) -> bool:
    ctx = context or bpy.context
    runtime = getattr(ctx.window_manager, 'session', None)
    return bool(runtime and runtime.textures_fetch_enabled)


def enable_textures_fetch(context: bpy.types.Context | None = None) -> bool:
    """Enable deferred texture/material sync after user picks Material/Rendered shading."""
    ctx = context or bpy.context
    runtime = getattr(ctx.window_manager, 'session', None)
    if runtime is None or runtime.textures_fetch_enabled:
        return False
    runtime.textures_fetch_enabled = True
    return True


def reset_textures_fetch_state(context: bpy.types.Context | None = None) -> None:
    ctx = context or bpy.context
    runtime = getattr(ctx.window_manager, 'session', None)
    if runtime is not None:
        runtime.textures_fetch_enabled = False
    update_textures_fetch_on_shading_change._previous = False


def update_textures_fetch_on_shading_change(context: bpy.types.Context | None = None) -> bool:
    """Start deferred asset apply when the user switches into Material/Rendered shading."""
    ctx = context or bpy.context
    current = is_texture_shading_active(ctx)
    previous = getattr(update_textures_fetch_on_shading_change, '_previous', False)
    update_textures_fetch_on_shading_change._previous = current
    if current and not previous:
        return enable_textures_fetch(ctx)
    return False


def schedule_flush_history(delay: float = 0.5) -> None:
    """Defer undo history flush so connect does not block the UI."""

    def _flush():
        flush_history()
        return None

    bpy.app.timers.register(_flush, first_interval=delay)


def flush_history():
    try:
        logging.debug("Flushing history")
        for i in range(bpy.context.preferences.edit.undo_steps+1):
            bpy.ops.ed.undo_push(message="Multiuser history flush")
    except RuntimeError:
        logging.error("Fail to overwrite history")


def get_state_str(state):
    state_str = 'UNKOWN'
    if state == STATE_WAITING:
        state_str = 'WARMING UP DATA'
    elif state == STATE_SYNCING:
        state_str = 'FETCHING'
    elif state == STATE_AUTH:
        state_str = 'AUTHENTICATION'
    elif state == STATE_CONFIG:
        state_str = 'CONFIGURATION'
    elif state == STATE_ACTIVE:
        state_str = 'ONLINE'
    elif state == STATE_SRV_SYNC:
        state_str = 'PUSHING'
    elif state == STATE_INITIAL:
        state_str = 'OFFLINE'
    elif state == STATE_QUITTING:
        state_str = 'QUITTING'
    elif state == CONNECTING:
        state_str = 'LAUNCHING SERVICES'
    elif state == STATE_LOBBY:
        state_str = 'LOBBY'

    return state_str


def clean_scene():
    for type_name in CLEARED_DATABLOCKS:
        sub_collection_to_avoid = [
            bpy.data.linestyles.get('LineStyle'),
            bpy.data.materials.get('Dots Stroke')
        ]
        try:
            type_collection = getattr(bpy.data, type_name)
        except AttributeError:
            continue
        else:
            items_to_remove = [i for i in type_collection if i not in sub_collection_to_avoid]
            for item in items_to_remove:
                try:
                    type_collection.remove(item)
                    logging.info(item.name)
                except Exception:
                    continue

    # Clear sequencer
    bpy.context.scene.sequence_editor_clear()


def get_selected_objects(scene, active_view_layer):
    return [obj.uuid for obj in scene.objects if obj.select_get(view_layer=active_view_layer)]


def resolve_from_id(id, optionnal_type=None):
    for category in dir(bpy.data):
        root = getattr(bpy.data, category)
        if isinstance(root, Iterable):
            if id in root and ((optionnal_type is None) or (optionnal_type.lower() in root[id].__class__.__name__.lower())):
                return root[id]
    return None


def get_preferences():
    if __package__ not in bpy.context.preferences.addons:
        return None
    else:
        return bpy.context.preferences.addons[__package__].preferences


@cache
def get_version():
    # Get version from multi_user/blender_manifest.toml
    file = Path(__file__).parent.joinpath("blender_manifest.toml")
    if not file.exists():
        return "unknown"

    # Read the file
    with open(file, "rb") as f:
        data = tomllib.load(f)
        return data.get("version", "unknown")


def current_milli_time():
    return int(round(time.time() * 1000))


def get_expanded_icon(prop: bpy.types.BoolProperty) -> str:
    if prop:
        return 'DISCLOSURE_TRI_DOWN'
    else:
        return 'DISCLOSURE_TRI_RIGHT'


# Taken from here: https://stackoverflow.com/a/55659577
def get_folder_size(folder):
    return ByteSize(sum(file.stat().st_size for file in Path(folder).rglob('*')))


class ByteSize(int):

    _kB = 1024
    _suffixes = 'B', 'kB', 'MB', 'GB', 'PB'

    def __new__(cls, *args, **kwargs):
        return super().__new__(cls, *args, **kwargs)

    def __init__(self, *args, **kwargs):
        self.bytes = self.B = int(self)
        self.kilobytes = self.kB = self / self._kB**1
        self.megabytes = self.MB = self / self._kB**2
        self.gigabytes = self.GB = self / self._kB**3
        self.petabytes = self.PB = self / self._kB**4
        *suffixes, last = self._suffixes
        suffix = next((
            suffix
            for suffix in suffixes
            if 1 < getattr(self, suffix) < self._kB
        ), last)
        self.readable = suffix, getattr(self, suffix)

        super().__init__()

    def __str__(self):
        return self.__format__('.2f')

    def __repr__(self):
        return '{}({})'.format(self.__class__.__name__, super().__repr__())

    def __format__(self, format_spec):
        suffix, val = self.readable
        return '{val:{fmt}} {suf}'.format(val=math.ceil(val), fmt=format_spec, suf=suffix)

    def __sub__(self, other):
        return self.__class__(super().__sub__(other))

    def __add__(self, other):
        return self.__class__(super().__add__(other))

    def __mul__(self, other):
        return self.__class__(super().__mul__(other))

    def __rsub__(self, other):
        return self.__class__(super().__sub__(other))

    def __radd__(self, other):
        return self.__class__(super().__add__(other))

    def __rmul__(self, other):
        return self.__class__(super().__rmul__(other))
