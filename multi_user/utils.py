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
                                   STATE_SYNCING, STATE_WAITING, UP)


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
_warning_log_buffer = []
_WARNING_LOG_BUFFER_MAX = 200
_FETCH_PROGRESS_STATE = {
    'active': False,
    'total': 0,
    'suppressed_steps': 0,
    'milestones_logged': set(),
}
_network_log_aggregates: dict[str, dict] = {}
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
    if level >= logging.WARNING:
        _warning_log_buffer.append(line)
        if len(_warning_log_buffer) > _WARNING_LOG_BUFFER_MAX:
            del _warning_log_buffer[:len(_warning_log_buffer) - _WARNING_LOG_BUFFER_MAX]


def reset_diagnostic_log_state():
    """Clear collapsed-progress and warning buffers (e.g. on disconnect)."""
    global _warning_log_buffer, _network_log_aggregates
    _warning_log_buffer = []
    _network_log_aggregates = {}
    reset_fetch_progress_log()
    flush_all_network_log_aggregates("session end")


def network_log_aggregate(key: str, level: int, message: str, *args):
    """Count repeated log lines; only the first occurrence is written immediately."""
    state = _network_log_aggregates.setdefault(key, {'count': 0, 'message': message, 'args': args})
    state['count'] += 1
    if state['count'] == 1:
        network_log(level, message, *args)


def flush_network_log_aggregate(key: str, level: int, summary_message: str):
    """Emit a summary for a collapsed repeated log series."""
    state = _network_log_aggregates.pop(key, None)
    if not state or state['count'] <= 1:
        return
    network_log(
        level,
        "%s (collapsed %s similar log lines; last: %s)",
        summary_message,
        state['count'] - 1,
        state['message'] % state['args'] if state['args'] else state['message'],
    )


def flush_all_network_log_aggregates(context_label: str):
    """Flush all active aggregate counters with summaries."""
    keys = list(_network_log_aggregates.keys())
    for key in keys:
        state = _network_log_aggregates.get(key)
        if not state:
            continue
        count = state['count']
        if count <= 1:
            _network_log_aggregates.pop(key, None)
            continue
        network_log(
            logging.INFO,
            "%s: %s — %s occurrence(s) (individual lines collapsed)",
            context_label,
            key,
            count,
        )
        _network_log_aggregates.pop(key, None)


def reset_fetch_progress_log():
    _FETCH_PROGRESS_STATE['active'] = False
    _FETCH_PROGRESS_STATE['total'] = 0
    _FETCH_PROGRESS_STATE['suppressed_steps'] = 0
    _FETCH_PROGRESS_STATE['milestones_logged'] = set()


def network_log_fetch_progress(current: int, total: int):
    """Log fetch/push progress at milestones instead of every single step."""
    if total <= 0:
        network_log(logging.INFO, "fetch progress %s/?", current)
        return

    state = _FETCH_PROGRESS_STATE
    if not state['active'] or state['total'] != total:
        if state['active'] and state['suppressed_steps']:
            network_log(
                logging.INFO,
                "fetch progress segment ended (%s step updates collapsed)",
                state['suppressed_steps'],
            )
        reset_fetch_progress_log()
        state['active'] = True
        state['total'] = total
        state['milestones_logged'].add(0)
        network_log(logging.INFO, "fetch progress started 0/%s", total)

    if current < 0:
        return

    milestone_pcts = (25, 50, 75, 100)
    milestone_values = {0, total}
    for pct in milestone_pcts:
        milestone_values.add(max(0, min(total, int(total * pct / 100))))
    if total > 0:
        milestone_values.add(total - 1)

    if current in milestone_values and current not in state['milestones_logged']:
        state['milestones_logged'].add(current)
        pct = int(100 * current / total) if total else 0
        network_log(
            logging.INFO,
            "fetch progress %s/%s (%s%%)",
            current,
            total,
            pct,
        )
    else:
        state['suppressed_steps'] += 1

    if current >= total:
        suppressed = state['suppressed_steps']
        network_log(
            logging.INFO,
            "fetch progress complete %s/%s (%s intermediate updates not logged)",
            total,
            total,
            suppressed,
        )
        reset_fetch_progress_log()


def log_warning_error_summary(label: str, max_lines: int = 20):
    """Surface recent WARNING/ERROR lines from the network log buffer."""
    if not _warning_log_buffer:
        network_log(
            logging.INFO,
            "%s: no WARNING/ERROR lines in network log buffer yet",
            label,
        )
        return
    network_log(
        logging.WARNING,
        "%s: %s WARNING/ERROR line(s) in network log buffer (showing last %s):",
        label,
        len(_warning_log_buffer),
        min(max_lines, len(_warning_log_buffer)),
    )
    for line in _warning_log_buffer[-max_lines:]:
        network_log(logging.WARNING, "  %s", line.replace("[MULTIUSER-NET] ", "", 1))


def log_session_role_diagnostics(context: bpy.types.Context | None = None):
    """Log host/client flags — mismatches here can change sync behavior."""
    ctx = context or bpy.context
    runtime = getattr(ctx.window_manager, 'session', None)
    wm_is_host = getattr(runtime, 'is_host', None) if runtime is not None else None
    info = get_connected_session_info(ctx)
    info_is_host = info.get('is_host')
    network_log(
        logging.INFO,
        "session role: wm.session.is_host=%s connected_session_info.is_host=%s "
        "textures_fetch_enabled=%s active_scene=%r",
        wm_is_host,
        info_is_host,
        textures_fetch_enabled(ctx),
        getattr(ctx.window.scene, 'name', None) if ctx.window else None,
    )
    if wm_is_host is not info_is_host:
        network_log(
            logging.WARNING,
            "session role mismatch: window_manager.session.is_host (%s) != "
            "connected_session_info.is_host (%s) — client init paths may run on host",
            wm_is_host,
            info_is_host,
        )


def is_session_host(context: bpy.types.Context | None = None) -> bool:
    """True when this Blender instance is hosting (not a remote client)."""
    ctx = context or bpy.context
    runtime = getattr(ctx.window_manager, 'session', None)
    if runtime is not None and getattr(runtime, 'is_host', False):
        return True
    return bool(get_connected_session_info(ctx).get('is_host'))


def should_skip_host_echo_apply(node_ref, initial_sync_active: bool) -> bool:
    """Skip destructive re-apply when the host echoes its own pushed snapshot."""
    if not is_session_host():
        return False
    if not initial_sync_active:
        return False
    if node_ref is None or node_ref.instance is None or not node_ref.data:
        return False
    type_id = node_ref.data.get('type_id')
    if type_id in FILE_ASSET_TYPE_IDS | {'Image'}:
        return False
    return True


def promote_fetched_node_to_up(node_ref) -> None:
    """Mark a FETCHED replication node UP without running load()."""
    from replication.constants import UP

    node_ref.state = UP


def log_pending_sync_datablocks(repository, label: str, max_names: int = 15):
    """List datablocks still in FETCHED state (not yet applied)."""
    if repository is None:
        return

    from replication.constants import FETCHED

    by_type: dict[str, list[str]] = {}
    for node in repository.graph.values():
        if node.state != FETCHED or not node.data:
            continue
        type_id = node.data.get('type_id', 'unknown')
        name = node.data.get('name', node.uuid)
        by_type.setdefault(type_id, []).append(str(name))

    if not by_type:
        network_log(logging.INFO, "%s: no FETCHED datablocks pending apply", label)
        return

    total = sum(len(names) for names in by_type.values())
    network_log(
        logging.WARNING,
        "%s: %s datablock(s) still FETCHED (not applied): %s",
        label,
        total,
        {k: len(v) for k, v in sorted(by_type.items())},
    )
    for type_id, names in sorted(by_type.items()):
        shown = names[:max_names]
        suffix = f" ... +{len(names) - max_names} more" if len(names) > max_names else ""
        network_log(
            logging.WARNING,
            "  %s: %s%s",
            type_id,
            ', '.join(shown),
            suffix,
        )


def log_scene_visibility_audit(
    label: str,
    repository=None,
    context: bpy.types.Context | None = None,
    max_issues: int = 25,
):
    """Scan the active scene for common reasons geometry might not show in the viewport."""
    ctx = context or bpy.context
    try:
        scene = ctx.window.scene
    except Exception:
        network_log(logging.WARNING, "%s visibility audit: no window context", label)
        return

    if scene is None:
        network_log(logging.WARNING, "%s visibility audit: no active scene", label)
        return

    runtime = getattr(ctx.window_manager, 'session', None)
    linked = count_scene_linked_objects(scene)
    network_log(
        logging.INFO,
        "%s visibility audit: scene=%r scene.objects=%s linked_in_tree=%s "
        "wm.is_host=%s session_info.is_host=%s",
        label,
        scene.name,
        len(scene.objects),
        linked,
        getattr(runtime, 'is_host', None) if runtime is not None else None,
        get_connected_session_info(ctx).get('is_host'),
    )

    issues: list[str] = []
    try:
        view_layer = ctx.view_layer
    except Exception:
        view_layer = None

    for obj in scene.objects:
        reasons: list[str] = []
        if obj.hide_viewport:
            reasons.append('hide_viewport')
        if view_layer is not None:
            try:
                if obj.hide_get(view_layer=view_layer):
                    reasons.append('hide_in_view_layer')
            except Exception:
                pass
        if obj.display_type != 'TEXTURED':
            reasons.append(f'display_type={obj.display_type}')
        if obj.type == 'MESH' and obj.data is not None and len(obj.data.vertices) == 0:
            reasons.append('mesh_0_vertices')
        if obj.type == 'MESH' and obj.data is None:
            reasons.append('mesh_data_missing')
        for mod in getattr(obj, 'modifiers', []):
            if mod.type == 'NODES' and not getattr(mod, 'node_group', None):
                reasons.append(f'gn_{mod.name}_no_node_tree')
            if mod.type == 'BOOLEAN':
                operand = getattr(mod, 'object', None)
                if operand is None:
                    reasons.append(f'bool_{mod.name}_no_operand')
                elif operand.hide_viewport:
                    reasons.append(f'bool_{mod.name}_operand_hidden')
        if reasons:
            issues.append(f"  {obj.name!r} ({obj.type}): {', '.join(reasons)}")

    if issues:
        network_log(
            logging.WARNING,
            "%s visibility audit: %s object(s) with possible viewport issues:",
            label,
            len(issues),
        )
        for line in issues[:max_issues]:
            network_log(logging.WARNING, line)
        if len(issues) > max_issues:
            network_log(
                logging.WARNING,
                "  ... and %s more object(s) with issues",
                len(issues) - max_issues,
            )
    else:
        network_log(
            logging.INFO,
            "%s visibility audit: no obvious viewport issues in scene.objects",
            label,
        )

    if repository is not None:
        log_pending_sync_datablocks(repository, f"{label} (pending apply)")


def log_object_apply_diagnostics(obj: bpy.types.Object, label: str):
    """Log only when an applied object has traits that often hide geometry."""
    if obj is None or not isinstance(obj, bpy.types.Object):
        return

    reasons: list[str] = []
    if obj.hide_viewport:
        reasons.append('hide_viewport')
    if obj.display_type != 'TEXTURED':
        reasons.append(f'display_type={obj.display_type}')
    if obj.type == 'MESH':
        if obj.data is None:
            reasons.append('mesh_data_missing')
        elif len(obj.data.vertices) == 0:
            reasons.append('mesh_0_vertices')
    for mod in getattr(obj, 'modifiers', []):
        if mod.type == 'NODES' and not getattr(mod, 'node_group', None):
            reasons.append(f'gn_{mod.name}_no_node_tree')
        if mod.type == 'BOOLEAN':
            operand = getattr(mod, 'object', None)
            if operand is None:
                reasons.append(f'bool_{mod.name}_no_operand')
            elif operand.hide_viewport:
                reasons.append(f'bool_{mod.name}_operand_hidden')
    if obj.instance_type == 'COLLECTION' and obj.instance_collection is None:
        reasons.append('collection_instance_missing')

    if reasons:
        network_log(
            logging.WARNING,
            "%s: object %r (%s) possible visibility issue: %s",
            label,
            obj.name,
            obj.type,
            ', '.join(reasons),
        )


def log_modifier_load_diagnostics(object_name: str, modifiers: bpy.types.bpy_prop_collection):
    """Log geometry/boolean modifiers that may not evaluate after reload."""
    issues: list[str] = []
    for mod in modifiers:
        if mod.type == 'NODES' and not getattr(mod, 'node_group', None):
            issues.append(f"GN '{mod.name}' has no node_tree")
        if mod.type == 'BOOLEAN':
            operand = getattr(mod, 'object', None)
            if operand is None:
                issues.append(f"Boolean '{mod.name}' has no operand object")
    if issues:
        network_log(
            logging.WARNING,
            "Modifier reload on %r: %s",
            object_name,
            '; '.join(issues),
        )


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
    reset_diagnostic_log_state()
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
    sync_states = (STATE_SYNCING, STATE_SRV_SYNC, STATE_WAITING)
    if previous_state in sync_states and current_state not in sync_states:
        if _FETCH_PROGRESS_STATE['active']:
            network_log(
                logging.INFO,
                "fetch progress segment ended on state change (%s step updates collapsed)",
                _FETCH_PROGRESS_STATE['suppressed_steps'],
            )
        reset_fetch_progress_log()
        if current_state == STATE_ACTIVE:
            flush_all_network_log_aggregates("sync finished")
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


FILE_ASSET_TYPE_IDS = frozenset({'WindowsPath', 'PosixPath'})
LOAD_BEFORE_MATERIAL_TYPE_IDS = FILE_ASSET_TYPE_IDS | {'Image'}
ASSET_TYPE_IDS = frozenset({'Material', 'Image'}) | FILE_ASSET_TYPE_IDS
ASSET_APPLY_ORDER = {
    'WindowsPath': 0,
    'PosixPath': 0,
    'Image': 1,
    'Material': 2,
}
# Apply tiers for non-asset datablocks (lower = earlier).
APPLY_TYPE_TIER = {
    'Material': 1,
    'ShaderNodeTree': 1,
    'GeometryNodeTree': 1,
    'Mesh': 2,
    'meshes': 2,
    'Curve': 2,
    'curves': 2,
    'Armature': 2,
    'armatures': 2,
    'Camera': 2,
    'cameras': 2,
    'Light': 2,
    'lights': 2,
    'Metaball': 2,
    'metaballs': 2,
    'Lattice': 2,
    'lattices': 2,
    'Font': 2,
    'fonts': 2,
    'Speaker': 2,
    'speakers': 2,
    'LightProbe': 2,
    'lightprobes': 2,
    'Volume': 2,
    'volumes': 2,
    'GreasePencil': 2,
    'grease_pencils': 2,
    'Object': 3,
    'objects': 3,
    'Collection': 4,
    'collections': 4,
    'Scene': 5,
    'scenes': 5,
    'World': 5,
    'worlds': 5,
}
OBJECT_TYPE_IDS = frozenset({'Object', 'objects'})
MESH_TYPE_IDS = frozenset({'Mesh', 'meshes'})
MESH_OBJECT_TYPES = frozenset({'MESH'})
HIERARCHY_TYPE_IDS = frozenset({
    'Scene', 'scenes', 'Collection', 'collections', 'World', 'worlds',
})
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


def is_mesh_geometry_loaded(node_ref) -> bool:
    """True when a mesh node has its vertex data applied."""
    if not node_ref or not node_ref.data:
        return False
    if node_ref.data.get('type_id') not in MESH_TYPE_IDS:
        return True
    vertex_count = node_ref.data.get('vertex_count', 0)
    if vertex_count <= 0:
        return True
    mesh = node_ref.instance
    return isinstance(mesh, bpy.types.Mesh) and len(mesh.vertices) > 0


def repair_incomplete_meshes(repository) -> int:
    """Re-queue meshes marked UP but still missing geometry."""
    if repository is None:
        return 0

    from replication.constants import FETCHED, UP

    repaired = 0
    for node in repository.graph.values():
        if not node.data or node.data.get('type_id') not in MESH_TYPE_IDS:
            continue
        if node.state != UP:
            continue
        if is_mesh_geometry_loaded(node):
            continue
        node.state = FETCHED
        repaired += 1
    return repaired


def count_scene_linked_objects(scene: bpy.types.Scene) -> int:
    """Count objects linked under a scene's master collection tree."""
    def count_collection(collection: bpy.types.Collection) -> int:
        total = len(collection.objects)
        for child in collection.children:
            total += count_collection(child)
        return total

    try:
        return count_collection(scene.collection)
    except Exception:
        return len(scene.objects)


def get_scene_apply_progress() -> tuple[int, int]:
    """Return (applied, total) for non-asset datablocks still in FETCHED state."""
    from replication.interface import session

    if not session or not getattr(session, 'repository', None):
        return (0, 0)

    total = 0
    applied = 0
    for node in session.repository.graph.values():
        if not node.data:
            continue
        type_id = node.data.get('type_id')
        if type_id in ASSET_TYPE_IDS:
            continue
        total += 1
        if node.state != FETCHED:
            applied += 1
    return (applied, total)


def build_index_sorted_ranks(repository) -> dict[str, int]:
    """Map node UUID to topological apply rank from the replication graph."""
    index_sorted = getattr(repository, 'index_sorted', None)
    if not index_sorted:
        return {}
    if not isinstance(index_sorted, (list, tuple)):
        index_sorted = list(index_sorted)
    return {uuid: rank for rank, uuid in enumerate(index_sorted)}


def asset_apply_sort_key(
    type_id: str | None,
    node_uuid: str,
    index_ranks: dict[str, int],
) -> tuple[int, int, int]:
    """Sort datablocks: assets, then geometry data, objects, collections, scene."""
    rank = index_ranks.get(node_uuid, len(index_ranks))
    if type_id in ASSET_TYPE_IDS:
        return (0, ASSET_APPLY_ORDER.get(type_id, 99), rank)
    tier = APPLY_TYPE_TIER.get(type_id, 2)
    return (tier, rank, 0)


def is_object_apply_ready(node_ref, repository) -> bool:
    """True when an Object node's data dependency is loaded enough to construct."""
    from replication.constants import UP

    data = node_ref.data if node_ref else None
    if not data or data.get('type_id') not in OBJECT_TYPE_IDS:
        return True

    obj_type = data.get('type')
    if obj_type == 'EMPTY':
        return True

    data_uuid = data.get('data_uuid')
    if not data_uuid:
        return True

    dep_node = repository.graph.get(data_uuid)
    if dep_node is None:
        return False
    if dep_node.state != UP:
        return False

    if obj_type not in MESH_OBJECT_TYPES:
        return dep_node.instance is not None

    mesh = dep_node.instance
    if mesh is None:
        return False
    return is_mesh_geometry_loaded(dep_node)


def refresh_scene_hierarchy(repository) -> int:
    """Re-apply collections and scenes so late-arriving objects get linked."""
    from replication import porcelain
    from . import shared_data

    if repository is None:
        return 0

    collection_nodes = []
    scene_nodes = []
    for node in repository.graph.values():
        if not node.data or node.state != UP or node.instance is None:
            continue
        type_id = node.data.get('type_id')
        if type_id in ('Collection', 'collections'):
            collection_nodes.append(node)
        elif type_id in ('Scene', 'scenes'):
            scene_nodes.append(node)

    refreshed = 0
    for node in collection_nodes:
        try:
            shared_data.session.applied_updates.append(node.uuid)
            porcelain.apply(repository, node.uuid, force=True)
            refreshed += 1
        except Exception:
            logging.debug("Failed to refresh collection %s", node.uuid)

    for node in scene_nodes:
        try:
            shared_data.session.applied_updates.append(node.uuid)
            porcelain.apply(repository, node.uuid, force=True)
            refreshed += 1
        except Exception:
            logging.debug("Failed to refresh scene %s", node.uuid)

    if refreshed:
        network_log(logging.INFO, "Refreshed scene hierarchy (%s node(s))", refreshed)
    return refreshed


def has_pending_fetched_assets(repository) -> bool:
    if repository is None:
        return False
    for node in repository.graph.values():
        if node.state != FETCHED or not node.data:
            continue
        if node.data.get('type_id') in ASSET_TYPE_IDS:
            return True
    return False


INITIAL_SYNC_TYPE_IDS = frozenset({
    'Mesh', 'meshes', 'Object', 'objects', 'Collection', 'collections',
    'Scene', 'scenes', 'World', 'worlds', 'GeometryNodeTree', 'Action',
    'Curve', 'curves', 'Camera', 'cameras',
})


def count_fetched_datablocks(repository, type_ids: set[str] | frozenset[str] | None = None) -> int:
    if repository is None:
        return 0
    count = 0
    for node in repository.graph.values():
        if node.state != FETCHED or not node.data:
            continue
        if type_ids is None or node.data.get('type_id') in type_ids:
            count += 1
    return count


def has_pending_initial_sync_datablocks(repository) -> bool:
    """True while scene content (meshes, objects, hierarchy) is still FETCHED."""
    return count_fetched_datablocks(repository, INITIAL_SYNC_TYPE_IDS) > 0


def has_pending_fetched_asset_type(repository, type_ids: set[str] | frozenset[str]) -> bool:
    if repository is None:
        return False
    for node in repository.graph.values():
        if node.state != FETCHED or not node.data:
            continue
        if node.data.get('type_id') in type_ids:
            return True
    return False


def log_replication_graph_summary(repository, label: str = "graph") -> None:
    """Log datablock counts by replication state and asset readiness."""
    if repository is None:
        return

    from replication.constants import FETCHED, UP

    type_counts: dict[str, dict[str, int]] = {}
    asset_details = []

    for node in repository.graph.values():
        if not node.data:
            continue
        type_id = node.data.get('type_id', 'unknown')
        state_name = {FETCHED: 'FETCHED', UP: 'UP'}.get(node.state, str(node.state))
        type_counts.setdefault(type_id, {})
        type_counts[type_id][state_name] = type_counts[type_id].get(state_name, 0) + 1

        if type_id not in ASSET_TYPE_IDS:
            continue

        name = node.data.get('name', '?')
        instance = node.instance
        extra = ''
        if type_id == 'Image' and instance is not None:
            filepath = getattr(instance, 'filepath', '')
            packed = getattr(instance, 'packed_file', None) is not None
            extra = f" filepath={filepath!r} users={instance.users} fake={instance.use_fake_user} packed={packed}"
        elif type_id == 'Material' and instance is not None:
            extra = f" users={instance.users} fake={instance.use_fake_user} use_nodes={instance.use_nodes}"
        elif type_id in ('WindowsPath', 'PosixPath'):
            extra = f" path={node.data.get('name', '?')}"

        asset_details.append(
            f"  {type_id} {name!r} state={state_name} instance={'yes' if instance else 'no'}{extra}"
        )

    network_log(logging.INFO, "%s summary: %s", label, type_counts)
    for line in asset_details:
        network_log(logging.INFO, line)


def textures_fetch_enabled(context: bpy.types.Context | None = None) -> bool:
    ctx = context or bpy.context
    runtime = getattr(ctx.window_manager, 'session', None)
    return bool(runtime and runtime.textures_fetch_enabled)


def enable_textures_fetch(context: bpy.types.Context | None = None) -> bool:
    """Enable material/image sync tracking for UI progress and node-tree finalization."""
    ctx = context or bpy.context
    runtime = getattr(ctx.window_manager, 'session', None)
    if runtime is None or runtime.textures_fetch_enabled:
        return False
    runtime.textures_fetch_enabled = True
    return True


def ensure_client_asset_sync(context: bpy.types.Context | None = None) -> None:
    """Start syncing images and materials as soon as a client joins a session."""
    if enable_textures_fetch(context):
        network_log(logging.INFO, "Material and image sync enabled for client session")


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


def switch_client_to_host_scene(repository) -> bool:
    """Make a client view the synced (populated) scene after the initial sync.

    A client cannot start from zero scenes, so ``clean_scene()`` leaves a local
    empty "bootstrap" scene behind. The host's real scene arrives as a separate
    datablock (and is renamed, e.g. ``Scene.001``) which means the client keeps
    looking at the empty local scene and sees nothing even though every
    datablock was fetched correctly. This points all windows at the populated
    synced scene and removes the now-useless bootstrap scene.

    Returns ``True`` once the view was switched to a non-empty synced scene.
    """
    if repository is None:
        return False

    from . import shared_data
    from replication import porcelain

    synced_scenes = []
    for node in repository.graph.values():
        if not node.data or node.data.get('type_id') != 'Scene':
            continue
        instance = node.instance
        if isinstance(instance, bpy.types.Scene):
            synced_scenes.append(instance)

    if not synced_scenes:
        return False

    def _obj_count(scene):
        return count_scene_linked_objects(scene)

    # Prefer the scene with the most linked content (not bpy.data orphans).
    target = max(synced_scenes, key=_obj_count)
    if _obj_count(target) == 0:
        refresh_scene_hierarchy(repository)
        target = max(synced_scenes, key=_obj_count)
    if _obj_count(target) == 0:
        return False

    switched = False
    try:
        windows = bpy.context.window_manager.windows
    except Exception:
        windows = []
    for window in windows:
        try:
            if window.scene is not target:
                window.scene = target
                switched = True
        except Exception:
            continue

    # Drop the leftover empty bootstrap scene so it stops polluting the session.
    bootstrap_name = shared_data.session.bootstrap_scene_name
    if bootstrap_name and bootstrap_name != target.name:
        leftover = bpy.data.scenes.get(bootstrap_name)
        if (
            leftover is not None
            and leftover is not target
            and _obj_count(leftover) == 0
            and len(bpy.data.scenes) > 1
        ):
            leftover_uuid = getattr(leftover, 'uuid', None)
            if leftover_uuid and repository.graph.get(leftover_uuid) is not None:
                try:
                    porcelain.rm(
                        repository,
                        leftover_uuid,
                        remove_dependencies=False,
                    )
                except Exception:
                    logging.debug("Could not remove bootstrap scene node")
            try:
                bpy.data.scenes.remove(leftover)
            except Exception:
                logging.debug("Could not remove leftover bootstrap scene")

    return switched or _obj_count(target) > 0


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
