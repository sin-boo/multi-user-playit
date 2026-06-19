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


import bpy
import logging
import re

from .dump_anything import Loader, Dumper
from replication.constants import FETCHED, UP
from replication import porcelain
from replication.protocol import ReplicatedDatablock

from .bl_datablock import (
    get_datablock_from_uuid,
    preserve_replicated_datablock,
    resolve_datablock_from_uuid,
)
from ..utils import ASSET_TYPE_IDS, network_log
from .bl_action import (
    dump_animation_data,
    load_animation_data,
    resolve_animation_dependencies,
)
from bpy.types import (
    NodeSocketGeometry,
    NodeSocketShader,
    NodeSocketVirtual,
    NodeSocketCollection,
    NodeSocketObject,
    NodeSocketMaterial,
)

NODE_SOCKET_INDEX = re.compile(r"\[(\d*)\]")
IGNORED_SOCKETS = [
    "NodeSocketGeometry",
    "NodeSocketShader",
    "CUSTOM",
    "NodeSocketVirtual",
]
IGNORED_SOCKETS_TYPES = (NodeSocketGeometry, NodeSocketShader, NodeSocketVirtual)
ID_NODE_SOCKETS = (NodeSocketObject, NodeSocketCollection, NodeSocketMaterial)
_ENUM_SOCKET_TYPE_NAMES = frozenset({'NodeSocketMenu', 'NodeSocketEnum'})


def _socket_accepts_default_value(socket, loaded_value) -> bool:
    """Return False for empty or version-incompatible enum/menu socket values."""
    if loaded_value == "" or loaded_value is None:
        return False

    bl_idname = getattr(socket, 'bl_idname', '') or type(socket).__name__
    if bl_idname in _ENUM_SOCKET_TYPE_NAMES or 'Menu' in bl_idname or 'Enum' in bl_idname:
        enum_items = getattr(socket, 'enum_items', None)
        if enum_items is not None:
            valid = {item.identifier for item in enum_items}
            return loaded_value in valid
    return True


def _apply_socket_default_value(socket, loaded_value) -> bool:
    """Apply a dumped socket default, skipping cross-version enum mismatches."""
    try:
        if isinstance(socket, ID_NODE_SOCKETS):
            resolved = get_datablock_from_uuid(loaded_value, None)
            if resolved is None:
                return False
            socket.default_value = resolved
            return True
        if not _socket_accepts_default_value(socket, loaded_value):
            return False
        socket.default_value = loaded_value
        return True
    except (TypeError, ValueError):
        return False


def _get_live_node(
    node_tree: bpy.types.NodeTree,
    node_id: str,
    node_data: dict | None = None,
):
    """Resolve a dumped node id to a live node (id, name, or bl_idname fallback)."""
    target_node = node_tree.nodes.get(node_id)
    if target_node is not None:
        return target_node
    if node_data:
        name = node_data.get('name')
        if name and name != node_id:
            target_node = node_tree.nodes.get(name)
            if target_node is not None:
                return target_node
    return None


def _missing_dump_node_ids(
    tree_data: dict,
    node_tree: bpy.types.NodeTree,
) -> list[str]:
    """Return dumped node ids that are not present in the live node tree."""
    dump_nodes = tree_data.get('nodes') if tree_data else None
    if not dump_nodes or node_tree is None:
        return list(dump_nodes.keys()) if dump_nodes else []
    return [
        node_id for node_id in dump_nodes
        if _get_live_node(node_tree, node_id, dump_nodes[node_id]) is None
    ]


def _material_tree_matches_dump(tree_data: dict, node_tree: bpy.types.NodeTree) -> bool:
    """True when every dumped node exists in the live tree (not just Blender defaults)."""
    dump_nodes = tree_data.get('nodes') if tree_data else None
    if not dump_nodes or node_tree is None:
        return False
    return len(_missing_dump_node_ids(tree_data, node_tree)) == 0


def load_node_io(nodes_data: dict, node_tree: bpy.types.ShaderNodeTree):
    for node_id, node_data in nodes_data.items():
        target_node = _get_live_node(node_tree, node_id, node_data)
        if target_node is None:
            logging.debug(
                "Node IO skip: dumped node %r not found in live tree",
                node_id,
            )
            continue
        inputs_data = node_data.get('inputs')
        if inputs_data:
            inputs = [
                i for i in target_node.inputs
                if not isinstance(i, IGNORED_SOCKETS_TYPES)
                and hasattr(i, "default_value")
            ]
            for idx, inpt in enumerate(inputs):
                if idx < len(inputs_data):
                    loaded_input = inputs_data[idx]
                    if not _apply_socket_default_value(inpt, loaded_input):
                        logging.debug(
                            "Node %s input %s default skipped (cross-version or not ready)",
                            target_node.name,
                            inpt.name,
                        )
                else:
                    logging.warning(f"Node {target_node.name} input length mismatch.")

        outputs_data = node_data.get('outputs')
        if outputs_data:
            outputs = [
                o for o in target_node.outputs
                if not isinstance(o, IGNORED_SOCKETS_TYPES)
                and hasattr(o, "default_value")
            ]
            for idx, output in enumerate(outputs):
                if idx < len(outputs_data):
                    loaded_output = outputs_data[idx]
                    if not _apply_socket_default_value(output, loaded_output):
                        logging.debug(
                            "Node %s output %s default skipped (cross-version or not ready)",
                            target_node.name,
                            output.name,
                        )
                else:
                    logging.warning(
                        f"Node {target_node.name} output length mismatch.")


SOCKET_ATTRIBUTES = [
    'name',
    'socket_type',
    'item_type',
    'in_out',
    'min_value',
    'max_value',
    'subtype',
    'structure_type',
    'description',
]

PANEL_ATTRIBUTES = [
    'name',
    'item_type',
    'description',
    'default_closed',
]

_SOCKET_ONLY_ATTRIBUTES = frozenset({
    'socket_type', 'in_out', 'min_value', 'max_value', 'subtype', 'structure_type',
})


def _interface_include_filter(item) -> list[str]:
    if getattr(item, 'item_type', None) == 'PANEL':
        return PANEL_ATTRIBUTES
    return SOCKET_ATTRIBUTES


def _safe_interface_socket_type(item) -> str | None:
    if getattr(item, 'item_type', None) != 'SOCKET':
        return None
    try:
        return item.socket_type
    except AttributeError:
        return None

def load_node(node_data: dict, node_tree: bpy.types.ShaderNodeTree):
    """ Load a node into a node_tree from a dict

        :arg node_data: dumped node data
        :type node_data: dict
        :arg node_tree: target node_tree
        :type node_tree: bpy.types.NodeTree
    """
    loader = Loader()
    try:
        target_node = node_tree.nodes.new(type=node_data["bl_idname"])
    except Exception as exc:
        logging.warning(
            "Skipping unsupported material node %s (%s): %s",
            node_data.get("name", "?"),
            node_data.get("bl_idname", "?"),
            exc,
        )
        return None
    target_node.select = False
    loader.load(target_node, node_data)
    image_uuid = node_data.get('image_uuid', None)
    node_tree_uuid = node_data.get('node_tree_uuid', None)

    if image_uuid and not target_node.image:
        image = resolve_datablock_from_uuid(image_uuid, bpy.data.images)
        if image is None:
            logging.warning("Material image not ready yet: %s", image_uuid)
        else:
            target_node.image = image

    if node_tree_uuid:
        target_node.node_tree = get_datablock_from_uuid(node_tree_uuid, None)

    if target_node.bl_idname == 'GeometryNodeRepeatOutput':
        target_node.repeat_items.clear()
        for sock_name, sock_type in node_data['repeat_items'].items():
            target_node.repeat_items.new(sock_type, sock_name)

    return target_node


def dump_node(node: bpy.types.ShaderNode) -> dict:
    """ Dump a single node to a dict

        :arg node: target node
        :type node: bpy.types.Node
        :retrun: dict
    """

    node_dumper = Dumper()
    node_dumper.depth = 1
    node_dumper.exclude_filter = [
        "dimensions",
        "show_expanded",
        "name_full",
        "select",
        "bl_label",
        "bl_height_min",
        "bl_height_max",
        "bl_height_default",
        "bl_width_min",
        "bl_width_max",
        "type",
        "bl_icon",
        "bl_width_default",
        "bl_static_type",
        "show_tetxure",
        "is_active_output",
        "hide",
        "show_options",
        "show_preview",
        "show_texture",
        "outputs",
        "width_hidden"
    ]

    dumped_node = node_dumper.dump(node)

    if node.parent:
        dumped_node['parent'] = node.parent.name

    dump_io_needed = (node.type not in ['REROUTE', 'OUTPUT_MATERIAL'])

    if dump_io_needed:
        io_dumper = Dumper()
        io_dumper.depth = 2
        io_dumper.include_filter = ["default_value"]

        if hasattr(node, 'inputs'):
            dumped_node['inputs'] = []
            inputs = [i for i in node.inputs if not isinstance(i, IGNORED_SOCKETS_TYPES)]
            for idx, inpt in enumerate(inputs):
                if hasattr(inpt, 'default_value'):
                    if isinstance(inpt.default_value, bpy.types.ID):
                        dumped_input = inpt.default_value.uuid
                    else:
                        dumped_input = io_dumper.dump(inpt.default_value)

                    dumped_node['inputs'].append(dumped_input)

        if hasattr(node, 'outputs'):
            dumped_node['outputs'] = []
            for idx, output in enumerate(node.outputs):
                if not isinstance(output, IGNORED_SOCKETS_TYPES):
                    if hasattr(output, 'default_value'):
                        dumped_node['outputs'].append(
                            io_dumper.dump(output.default_value))

    if hasattr(node, 'color_ramp'):
        ramp_dumper = Dumper()
        ramp_dumper.depth = 4
        ramp_dumper.include_filter = [
            'elements',
            'alpha',
            'color',
            'position',
            'interpolation',
            'hue_interpolation',
            'color_mode'
        ]
        dumped_node['color_ramp'] = ramp_dumper.dump(node.color_ramp)
    if hasattr(node, 'mapping'):
        curve_dumper = Dumper()
        curve_dumper.depth = 5
        curve_dumper.include_filter = [
            'curves',
            'points',
            'location'
        ]
        dumped_node['mapping'] = curve_dumper.dump(node.mapping)
    if hasattr(node, 'image') and getattr(node, 'image'):
        dumped_node['image_uuid'] = node.image.uuid
    if hasattr(node, 'node_tree') and getattr(node, 'node_tree'):
        dumped_node['node_tree_uuid'] = node.node_tree.uuid

    if node.bl_idname == 'GeometryNodeRepeatInput':
        dumped_node['paired_output'] = node.paired_output.name

    if node.bl_idname == 'GeometryNodeRepeatOutput':
        dumped_node['repeat_items'] = {item.name: item.socket_type for item in node.repeat_items}
    return dumped_node


def _resolve_node_socket(sockets, index, identifier=None):
    """Resolve a dumped socket reference by index, then by identifier."""
    if index is not None:
        idx = int(index)
        if 0 <= idx < len(sockets):
            return sockets[idx]
    if identifier:
        for socket in sockets:
            if getattr(socket, 'identifier', None) == identifier:
                return socket
    return None


def load_links(links_data, node_tree):
    """ Load node_tree links from a list

        :arg links_data: dumped node links
        :type links_data: list
        :arg node_tree: node links collection
        :type node_tree: bpy.types.NodeTree
    """

    for link in links_data:
        to_node = node_tree.nodes.get(link['to_node'])
        from_node = node_tree.nodes.get(link['from_node'])
        if to_node is None or from_node is None:
            logging.warning(
                "Skipping link: node missing (to=%s, from=%s)",
                link.get('to_node'),
                link.get('from_node'),
            )
            continue

        to_socket = _resolve_node_socket(
            to_node.inputs,
            link.get('to_socket'),
            link.get('to_socket_identifier'),
        )
        from_socket = _resolve_node_socket(
            from_node.outputs,
            link.get('from_socket'),
            link.get('from_socket_identifier'),
        )
        if to_socket is None or from_socket is None:
            logging.debug(
                "Skipping link %s.%s -> %s.%s: socket not found "
                "(inputs=%d, outputs=%d)",
                link.get('from_node'),
                link.get('from_socket'),
                link.get('to_node'),
                link.get('to_socket'),
                len(to_node.inputs),
                len(from_node.outputs),
            )
            continue

        try:
            node_tree.links.new(to_socket, from_socket)
        except Exception as e:
            logging.debug(
                "Skipping link %s.%s -> %s.%s: %s",
                link.get('from_node'),
                link.get('from_socket'),
                link.get('to_node'),
                link.get('to_socket'),
                e,
            )


def dump_links(links):
    """ Dump node_tree links collection to a list

        :arg links: node links collection
        :type links: bpy.types.NodeLinks
        :retrun: list
    """

    links_data = []

    for link in links:
        to_socket = NODE_SOCKET_INDEX.search(
            link.to_socket.path_from_id()).group(1)
        from_socket = NODE_SOCKET_INDEX.search(
            link.from_socket.path_from_id()).group(1)
        links_data.append({
            'to_node': link.to_node.name,
            'to_socket': to_socket,
            'to_socket_identifier': getattr(link.to_socket, 'identifier', None),
            'from_node': link.from_node.name,
            'from_socket': from_socket,
            'from_socket_identifier': getattr(link.from_socket, 'identifier', None),
        })

    return links_data


def dump_node_tree(node_tree: bpy.types.ShaderNodeTree) -> dict:
    """ Dump a shader node_tree to a dict including links and nodes

        :arg node_tree: dumped shader node tree
        :type node_tree: bpy.types.ShaderNodeTree`
        :return: dict
    """
    node_tree_data = {
        'nodes': {node.name: dump_node(node) for node in node_tree.nodes},
        'links': dump_links(node_tree.links),
        'name': node_tree.name,
        'type': type(node_tree).__name__
    }

    sockets = [item for item in node_tree.interface.items_tree if item.item_type in ['SOCKET', 'PANEL']]
    node_tree_data['interface'] = dump_node_tree_sockets(sockets)

    return node_tree_data


def dump_node_tree_sockets(sockets: bpy.types.Collection) -> dict:
    """ dump sockets of a shader_node_tree

        :arg target_node_tree: target node_tree
        :type target_node_tree: bpy.types.NodeTree
        :arg socket_id: socket identifer
        :type socket_id: str
        :return: dict
    """
    socket_dumper = Dumper()
    sockets_data = []
    for socket in sockets:
        socket_dumper.include_filter = _interface_include_filter(socket)
        socket_data = socket_dumper.dump(socket)
        if socket.parent and socket.parent.index != -1:
            socket_data['parent'] = socket.parent.index 
        sockets_data.append(
            socket_data
        )

    return sockets_data


def load_node_tree_sockets(interface: bpy.types.NodeTreeInterface,
                           sockets_data: dict):
    """ load sockets of a shader_node_tree

        :arg target_node_tree: target node_tree
        :type target_node_tree: bpy.types.NodeTree
        :arg socket_id: socket identifer
        :type socket_id: str
        :arg socket_data: dumped socket data
        :type socket_data: dict
    """
    interface.clear()

    for socket_data in sockets_data:
        item_type = socket_data.get('item_type')
        socket_loader = Loader()
        if item_type == 'SOCKET':
            socket = interface.new_socket(
                socket_data['name'],
                in_out=socket_data['in_out'],
                socket_type=socket_data['socket_type'],
            )
            socket_loader.exclure_filter = ['default_closed']
        elif item_type == 'PANEL':
            socket = interface.new_panel(
                socket_data['name'],
                description=socket_data.get('description', ''),
                default_closed=socket_data.get('default_closed', False),
            )
            socket_loader.exclure_filter = list(_SOCKET_ONLY_ATTRIBUTES)
        else:
            logging.debug("Skipping unknown node-tree interface item %r", item_type)
            continue
        socket_loader.load(socket, socket_data)

def load_node_tree(node_tree_data: dict, target_node_tree: bpy.types.ShaderNodeTree) -> dict:
    """Load a shader node_tree from dumped data

        :arg node_tree_data: dumped node data
        :type node_tree_data: dict
        :arg target_node_tree: target node_tree
        :type target_node_tree: bpy.types.NodeTree
    """
    # TODO: load only required nodes
    target_node_tree.nodes.clear()

    if not target_node_tree.is_property_readonly('name'):
        target_node_tree.name = node_tree_data['name']

    if 'interface' in node_tree_data:
        load_node_tree_sockets(target_node_tree.interface, node_tree_data['interface'])

    # Load nodes
    for node in node_tree_data["nodes"]:
        load_node(node_tree_data["nodes"][node], target_node_tree)

    for node_id, node_data in node_tree_data["nodes"].items():
        target_node = target_node_tree.nodes.get(node_id, None)
        if target_node is None:
            continue
        elif 'parent' in node_data:
            target_node.parent =  target_node_tree.nodes[node_data['parent']]
        else:
            target_node.parent = None

    # Load geo node repeat zones
    zone_input_to_pair = [node_data for node_data in node_tree_data["nodes"].values() if node_data['bl_idname'] == 'GeometryNodeRepeatInput']
    for node_input_data in zone_input_to_pair:
        zone_input = target_node_tree.nodes.get(node_input_data['name'])
        zone_output = target_node_tree.nodes.get(node_input_data['paired_output'])
        if zone_input is None or zone_output is None:
            continue

        zone_input.pair_with_output(zone_output)

    finalize_node_tree(node_tree_data, target_node_tree)


def resolve_image_refs(node_tree_data: dict, target_node_tree: bpy.types.NodeTree) -> int:
    """Assign image datablocks to TEX_IMAGE nodes once images are available."""
    rebound = 0
    for node_id, node_data in node_tree_data["nodes"].items():
        image_uuid = node_data.get('image_uuid')
        if not image_uuid:
            continue
        target_node = _get_live_node(target_node_tree, node_id, node_data)
        if target_node is None or not hasattr(target_node, 'image'):
            continue
        image = resolve_datablock_from_uuid(image_uuid, bpy.data.images)
        if image is None:
            logging.debug(
                "Image ref pending for node %s image_uuid=%s",
                node_id,
                image_uuid,
            )
            continue
        if target_node.image != image:
            target_node.image = image
            rebound += 1
    return rebound


def resolve_node_group_refs(node_tree_data: dict, target_node_tree: bpy.types.NodeTree) -> int:
    """Assign nested node groups once their datablocks are available."""
    rebound = 0
    for node_id, node_data in node_tree_data["nodes"].items():
        node_tree_uuid = node_data.get('node_tree_uuid')
        if not node_tree_uuid:
            continue
        target_node = _get_live_node(target_node_tree, node_id, node_data)
        if target_node is None:
            continue
        node_group = get_datablock_from_uuid(node_tree_uuid, None)
        if node_group is None:
            logging.warning(
                "Node %s references missing node group %s",
                node_id,
                node_tree_uuid,
            )
            continue
        if target_node.node_tree != node_group:
            target_node.node_tree = node_group
            rebound += 1
    return rebound


def node_tree_needs_finalize(tree_data: dict, target_node_tree: bpy.types.NodeTree) -> bool:
    """True when dumped refs exist locally but are not yet bound on the live tree."""
    if not tree_data or target_node_tree is None:
        return False
    for node_id, node_data in tree_data.get("nodes", {}).items():
        image_uuid = node_data.get('image_uuid')
        if image_uuid:
            target_node = _get_live_node(target_node_tree, node_id, node_data)
            if (
                target_node is not None
                and hasattr(target_node, 'image')
                and not target_node.image
                and resolve_datablock_from_uuid(image_uuid, bpy.data.images) is not None
            ):
                return True
        node_tree_uuid = node_data.get('node_tree_uuid')
        if node_tree_uuid:
            target_node = _get_live_node(target_node_tree, node_id, node_data)
            group = get_datablock_from_uuid(node_tree_uuid, None)
            if (
                target_node is not None
                and hasattr(target_node, 'node_tree')
                and group is not None
                and target_node.node_tree != group
            ):
                return True
    return False


def finalize_node_tree(
    node_tree_data: dict,
    target_node_tree: bpy.types.NodeTree,
    owner_name: str = "",
) -> dict[str, int]:
    """Resolve image/group references and wire links after all node trees are loaded."""
    stats = {'images_rebound': 0, 'groups_rebound': 0, 'links_wired': 0, 'links_skipped': 0}
    if not node_tree_data or target_node_tree is None:
        return stats
    stats['images_rebound'] = resolve_image_refs(node_tree_data, target_node_tree)
    stats['groups_rebound'] = resolve_node_group_refs(node_tree_data, target_node_tree)
    load_node_io(node_tree_data["nodes"], target_node_tree)
    target_node_tree.links.clear()
    load_links(node_tree_data["links"], target_node_tree)
    stats['links_wired'] = len(target_node_tree.links)
    stats['links_skipped'] = max(0, len(node_tree_data.get('links', [])) - stats['links_wired'])
    if owner_name and (stats['images_rebound'] or stats['groups_rebound'] or stats['links_wired']):
        network_log(
            logging.INFO,
            "Finalized node tree for %r: images=%s groups=%s links=%s (skipped=%s)",
            owner_name,
            stats['images_rebound'],
            stats['groups_rebound'],
            stats['links_wired'],
            stats['links_skipped'],
        )
    return stats


_node_trees_finalized = False
_geometry_node_trees_finalized = False
_node_tree_finalize_queue = []
_geometry_node_tree_finalize_queue = []
_node_tree_finalize_total = 0
_geometry_node_tree_finalize_total = 0
_NODE_TREE_FINALIZE_BATCH_SIZE = 8


def reset_node_tree_finalize_state():
    global _node_trees_finalized, _geometry_node_trees_finalized
    global _node_tree_finalize_queue, _geometry_node_tree_finalize_queue
    global _node_tree_finalize_total, _geometry_node_tree_finalize_total
    _node_trees_finalized = False
    _geometry_node_trees_finalized = False
    _node_tree_finalize_queue = []
    _geometry_node_tree_finalize_queue = []
    _node_tree_finalize_total = 0
    _geometry_node_tree_finalize_total = 0


def reset_material_finalize_state():
    """Allow material node-tree finalization to run again after shader groups apply."""
    global _node_trees_finalized, _node_tree_finalize_queue, _node_tree_finalize_total
    _node_trees_finalized = False
    _node_tree_finalize_queue = []
    _node_tree_finalize_total = 0


def _finalize_queue(queue: list, batch_size: int = _NODE_TREE_FINALIZE_BATCH_SIZE) -> int:
    from .. import shared_data

    finalized = 0
    while queue and finalized < batch_size:
        node_tree_data, target_node_tree, owner_name = queue.pop(0)
        owner = getattr(target_node_tree, 'id_data', None)
        owner_uuid = getattr(owner, 'uuid', None)
        if owner_uuid:
            shared_data.session.applied_updates.append(owner_uuid)
        try:
            finalize_node_tree(node_tree_data, target_node_tree, owner_name=owner_name)
        except Exception as exc:
            logging.exception(
                "Failed to finalize node tree for %r: %s",
                owner_name or owner,
                exc,
            )
            network_log(
                logging.ERROR,
                "Failed to finalize node tree for %r: %s: %s",
                owner_name or getattr(owner, 'name', '?'),
                type(exc).__name__,
                exc,
            )
        finalized += 1
    return finalized


def get_material_node_tree_finalize_progress() -> tuple[int, int]:
    remaining = len(_node_tree_finalize_queue)
    return (_node_tree_finalize_total - remaining, _node_tree_finalize_total)


def maybe_finalize_node_trees(repository) -> None:
    """Finalize node trees once their datablocks have been applied."""
    global _node_trees_finalized, _geometry_node_trees_finalized
    global _node_tree_finalize_queue, _geometry_node_tree_finalize_queue
    global _node_tree_finalize_total, _geometry_node_tree_finalize_total

    if not _geometry_node_trees_finalized:
        pending_groups = any(
            node.state == FETCHED and node.data and
            node.data.get('type_id') == 'GeometryNodeTree'
            for node in repository.graph.values()
        )
        if not pending_groups:
            if not _geometry_node_tree_finalize_queue and _geometry_node_tree_finalize_total == 0:
                for node_id in repository.heads:
                    node_ref = repository.graph.get(node_id)
                    if node_ref is None or not node_ref.data:
                        continue
                    if node_ref.data.get('type_id') == 'GeometryNodeTree':
                        owner_name = node_ref.data.get('name', node_id)
                        _geometry_node_tree_finalize_queue.append(
                            (node_ref.data, node_ref.instance, owner_name)
                        )
                _geometry_node_tree_finalize_total = len(_geometry_node_tree_finalize_queue)

            _finalize_queue(_geometry_node_tree_finalize_queue)
            if not _geometry_node_tree_finalize_queue:
                _geometry_node_trees_finalized = True

    if _node_trees_finalized:
        return

    for node in repository.graph.values():
        if node.state == FETCHED and node.data and \
                node.data.get('type_id') in ASSET_TYPE_IDS:
            return

    pending_shader_groups = any(
        node.state == FETCHED and node.data and
        node.data.get('type_id') == 'ShaderNodeTree'
        for node in repository.graph.values()
    )
    if pending_shader_groups:
        return

    if not _node_tree_finalize_queue and _node_tree_finalize_total == 0:
        seen_trees: set[int] = set()
        for node in repository.graph.values():
            if node.state != UP or not node.data or not node.instance:
                continue
            if node.data.get('type_id') != 'Material' or not node.data.get('use_nodes'):
                continue
            material = node.instance
            if material.node_tree is None or 'node_tree' not in node.data:
                continue
            tree_key = id(material.node_tree)
            if tree_key in seen_trees:
                continue
            seen_trees.add(tree_key)
            owner_name = node.data.get('name', node.uuid)
            _node_tree_finalize_queue.append(
                (node.data['node_tree'], material.node_tree, owner_name)
            )
        _node_tree_finalize_total = len(_node_tree_finalize_queue)

    if _node_tree_finalize_queue and _node_tree_finalize_total > 0:
        network_log(
            logging.INFO,
            "Material node-tree finalize queue: %s/%s remaining",
            len(_node_tree_finalize_queue),
            _node_tree_finalize_total,
        )

    _finalize_queue(_node_tree_finalize_queue)
    if not _node_tree_finalize_queue:
        _node_trees_finalized = True


def log_material_node_tree_diagnostics(
    material_name: str,
    tree_data: dict,
    node_tree: bpy.types.NodeTree,
    label: str = "diagnostic",
) -> None:
    """Log node/image/link readiness for a material shader tree."""
    dump_nodes = tree_data.get('nodes', {})
    live_names = {n.name for n in node_tree.nodes}
    dump_names = set(dump_nodes.keys())
    matched = dump_names & live_names
    missing_in_live = _missing_dump_node_ids(tree_data, node_tree)
    extra_in_live = live_names - dump_names

    unbound_images = 0
    available_images = 0
    for node_id, node_data in dump_nodes.items():
        image_uuid = node_data.get('image_uuid')
        if not image_uuid:
            continue
        live_node = _get_live_node(node_tree, node_id, node_data)
        image = resolve_datablock_from_uuid(image_uuid, bpy.data.images)
        if live_node is not None and hasattr(live_node, 'image') and not live_node.image:
            if image is not None:
                available_images += 1
            else:
                unbound_images += 1

    network_log(
        logging.INFO,
        "%s material %r: nodes live=%s dump=%s matched=%s "
        "missing=%s extra=%s links=%s/%s unbound_images=%s images_available=%s",
        label,
        material_name,
        len(live_names),
        len(dump_names),
        len(matched),
        len(missing_in_live),
        len(extra_in_live),
        len(node_tree.links),
        len(tree_data.get('links', [])),
        unbound_images,
        available_images,
    )
    if missing_in_live:
        network_log(
            logging.DEBUG,
            "%s material %r missing live nodes: %s",
            label,
            material_name,
            sorted(missing_in_live)[:12],
        )


def repair_material_node_trees(repository) -> dict[str, int]:
    """Re-bind image and group refs on all synced materials after dependencies exist."""
    from replication.constants import UP

    stats = {
        'materials': 0,
        'rebuilt': 0,
        'images_rebound': 0,
        'groups_rebound': 0,
        'links_wired': 0,
        'failed': 0,
    }
    for node in repository.graph.values():
        if node.state != UP or not node.instance or not node.data:
            continue
        if node.data.get('type_id') != 'Material':
            continue
        if not node.data.get('use_nodes') or not node.data.get('node_tree'):
            continue
        material = node.instance
        material_name = node.data.get('name', material.name)
        if material.node_tree is None:
            network_log(
                logging.WARNING,
                "Repair skip: material %r has no node_tree instance",
                material_name,
            )
            continue
        tree_data = node.data['node_tree']
        try:
            missing_nodes = _missing_dump_node_ids(tree_data, material.node_tree)
            if missing_nodes:
                network_log(
                    logging.INFO,
                    "Repair rebuild: material %r missing %s/%s dumped node(s) "
                    "(live=%s, e.g. %s)",
                    material_name,
                    len(missing_nodes),
                    len(tree_data.get('nodes', {})),
                    len(material.node_tree.nodes),
                    missing_nodes[:6],
                )
                load_node_tree(tree_data, material.node_tree)
                stats['rebuilt'] += 1
            else:
                log_material_node_tree_diagnostics(
                    material_name,
                    tree_data,
                    material.node_tree,
                    label="pre-repair",
                )
                fin_stats = finalize_node_tree(
                    tree_data,
                    material.node_tree,
                    owner_name=material_name,
                )
                stats['images_rebound'] += fin_stats['images_rebound']
                stats['groups_rebound'] += fin_stats['groups_rebound']
                stats['links_wired'] += fin_stats['links_wired']
            stats['materials'] += 1
            log_material_node_tree_diagnostics(
                material_name,
                tree_data,
                material.node_tree,
                label="post-repair",
            )
        except Exception as exc:
            stats['failed'] += 1
            logging.exception("Failed to repair material %r", material_name)
            network_log(
                logging.ERROR,
                "Failed to repair material %r: %s: %s",
                material_name,
                type(exc).__name__,
                exc,
            )
    return stats


def get_node_tree_dependencies(node_tree: bpy.types.NodeTree) -> list:
    def has_image(node): return (
        node.type in ['TEX_IMAGE', 'TEX_ENVIRONMENT'] and node.image)

    def has_node_group(node): return (
        hasattr(node, 'node_tree') and node.node_tree)

    def has_texture(node):
        return node.type in ["ATTRIBUTE_SAMPLE_TEXTURE", "TEXTURE"] and node.texture

    deps = []

    for node in node_tree.nodes:
        if has_image(node):
            deps.append(node.image)
        elif has_node_group(node):
            deps.append(node.node_tree)
        elif has_texture(node):
            deps.append(node.texture)

    return deps


def dump_materials_slots(materials: bpy.types.bpy_prop_collection) -> list:
    """ Dump material slots collection

        :arg materials: material slots collection to dump
        :type materials: bpy.types.bpy_prop_collection
        :return: list of tuples (mat_uuid, mat_name)
    """
    return [(m.uuid, m.name) for m in materials if m]


def load_materials_slots(src_materials: list, dst_materials: bpy.types.bpy_prop_collection):
    """ Load material slots

        :arg src_materials: dumped material collection (ex: object.materials)
        :type src_materials: list of tuples (uuid, name)
        :arg dst_materials: target material collection pointer
        :type dst_materials: bpy.types.bpy_prop_collection
    """
    # MATERIAL SLOTS
    dst_materials.clear()

    for mat_uuid, mat_name in src_materials:
        mat_ref = None
        if mat_uuid:
            mat_ref = get_datablock_from_uuid(mat_uuid, None)
        else:
            mat_ref = bpy.data.materials.get(mat_name)
        if mat_ref is None:
            logging.warning(
                "Material slot unresolved: uuid=%s name=%s",
                mat_uuid,
                mat_name,
            )
        dst_materials.append(mat_ref)


def refresh_mesh_material_slots(repository) -> int:
    """Re-bind mesh material slots after materials/images have synced."""
    from replication.constants import UP

    refreshed = 0
    for node in repository.graph.values():
        if node.state != UP or not node.instance or not node.data:
            continue
        if node.data.get('type_id') != 'meshes':
            continue
        materials = node.data.get('materials')
        if not materials:
            continue

        mesh = node.instance
        before = [slot.name if slot else None for slot in mesh.materials]
        unresolved_before = sum(
            1 for mat_uuid, _ in materials
            if mat_uuid and get_datablock_from_uuid(mat_uuid, None) is None
        )
        load_materials_slots(materials, mesh.materials)
        after = [slot.name if slot else None for slot in mesh.materials]

        if before != after:
            refreshed += 1
            logging.info(
                "Mesh %s material slots updated: %s -> %s",
                mesh.name,
                before,
                after,
            )
        elif unresolved_before:
            network_log(
                logging.INFO,
                "Mesh %r still has %s unresolved material slot(s) after refresh",
                mesh.name,
                unresolved_before,
            )
    return refreshed


def register_scene_material_assets(repository) -> dict[str, int]:
    """Register materials, nested node groups, and texture deps on the host repository."""
    stats = {'materials': 0, 'node_groups': 0, 'skipped': 0, 'missing_groups': 0}
    seen_materials: set[int] = set()
    seen_groups: set[int] = set()

    def register_node_group(node_group) -> None:
        if node_group is None:
            return
        group_id = id(node_group)
        if group_id in seen_groups:
            return
        seen_groups.add(group_id)
        if repository.get_node_by_datablock(node_group):
            return
        try:
            porcelain.add(repository, node_group, skip_unsupported=True)
            stats['node_groups'] += 1
            logging.info("Registered node group for sync: %s", node_group.name)
        except Exception as exc:
            logging.warning("Failed to register node group %s: %s", node_group.name, exc)
            return
        if node_group.nodes:
            for dep in get_node_tree_dependencies(node_group):
                if isinstance(dep, bpy.types.Image):
                    continue
                if isinstance(dep, bpy.types.NodeTree):
                    register_node_group(dep)

    def register_material(material) -> None:
        if material is None:
            return
        material_id = id(material)
        if material_id in seen_materials:
            return
        seen_materials.add(material_id)
        if repository.get_node_by_datablock(material):
            stats['skipped'] += 1
            return
        try:
            porcelain.add(repository, material, skip_unsupported=True)
            stats['materials'] += 1
            logging.info("Registered material for sync: %s", material.name)
        except Exception as exc:
            logging.warning("Failed to register material %s: %s", material.name, exc)
            return
        if material.use_nodes and material.node_tree:
            for dep in get_node_tree_dependencies(material.node_tree):
                if isinstance(dep, bpy.types.Image):
                    continue
                if isinstance(dep, bpy.types.NodeTree):
                    register_node_group(dep)

    for mesh in bpy.data.meshes:
        for material in mesh.materials:
            register_material(material)

    for material in bpy.data.materials:
        register_material(material)

    for material in bpy.data.materials:
        if not material.use_nodes or not material.node_tree:
            continue
        for node in material.node_tree.nodes:
            if not hasattr(node, 'node_tree') or not node.node_tree:
                continue
            if not repository.get_node_by_datablock(node.node_tree):
                stats['missing_groups'] += 1
                logging.warning(
                    "Material %s references node group %s not registered for sync",
                    material.name,
                    node.node_tree.name,
                )

    return stats


class BlMaterial(ReplicatedDatablock):
    use_delta = True

    bl_id = "materials"
    bl_class = bpy.types.Material
    bl_check_common = False
    bl_icon = 'MATERIAL_DATA'
    bl_reload_parent = False
    bl_reload_child = True

    @staticmethod
    def construct(data: dict) -> object:
        material = bpy.data.materials.new(data["name"])
        preserve_replicated_datablock(material)
        return material

    _LOAD_SKIP_KEYS = frozenset({
        'node_tree',
        'grease_pencil',
        'animation_data',
        'nodes_animation_data',
        'type_id',
        'uuid',
    })

    @staticmethod
    def load(data: dict, datablock: object):
        loader = Loader()
        safe_data = {
            key: value for key, value in data.items()
            if key not in BlMaterial._LOAD_SKIP_KEYS
        }
        loader.load(datablock, safe_data)

        is_grease_pencil = data.get('is_grease_pencil')
        use_nodes = data.get('use_nodes')

        if is_grease_pencil:
            if not datablock.is_grease_pencil:
                bpy.data.materials.create_gpencil_data(datablock)
            loader.load(datablock.grease_pencil, data['grease_pencil'])
        elif use_nodes and data.get('node_tree'):
            if datablock.node_tree is None:
                datablock.use_nodes = True

            tree_data = data['node_tree']
            if _material_tree_matches_dump(tree_data, datablock.node_tree):
                if node_tree_needs_finalize(tree_data, datablock.node_tree):
                    finalize_node_tree(
                        tree_data,
                        datablock.node_tree,
                        owner_name=datablock.name,
                    )
                load_animation_data(
                    data.get('nodes_animation_data'),
                    datablock.node_tree,
                )
            else:
                network_log(
                    logging.INFO,
                    "Material %r: loading node tree from dump "
                    "(live=%s nodes, dump=%s nodes)",
                    datablock.name,
                    len(datablock.node_tree.nodes) if datablock.node_tree else 0,
                    len(tree_data.get('nodes', {})),
                )
                load_node_tree(tree_data, datablock.node_tree)
                load_animation_data(
                    data.get('nodes_animation_data'),
                    datablock.node_tree,
                )
        load_animation_data(data.get('animation_data'), datablock)
        preserve_replicated_datablock(datablock)
        logging.info(
            "Material %s loaded (use_nodes=%s, users=%s, fake_user=%s)",
            datablock.name,
            datablock.use_nodes,
            datablock.users,
            datablock.use_fake_user,
        )

    @staticmethod
    def dump(datablock: object) -> dict:
        mat_dumper = Dumper()
        mat_dumper.depth = 2
        mat_dumper.include_filter = [
            'name',
            'blend_method',
            'shadow_method',
            'alpha_threshold',
            'show_transparent_back',
            'use_backface_culling',
            'use_screen_refraction',
            'use_sss_translucency',
            'refraction_depth',
            'preview_render_type',
            'use_preview_world',
            'pass_index',
            'use_nodes',
            'diffuse_color',
            'specular_color',
            'roughness',
            'specular_intensity',
            'metallic',
            'line_color',
            'line_priority',
            'is_grease_pencil'
        ]
        data = mat_dumper.dump(datablock)

        if datablock.is_grease_pencil:
            gp_mat_dumper = Dumper()
            gp_mat_dumper.depth = 3

            gp_mat_dumper.include_filter = [
                'color',
                'fill_color',
                'mix_color',
                'mix_factor',
                'mix_stroke_factor',
                # 'texture_angle',
                # 'texture_scale',
                # 'texture_offset',
                'pixel_size',
                'hide',
                'lock',
                'ghost',
                # 'texture_clamp',
                'flip',
                'use_overlap_strokes',
                'show_stroke',
                'show_fill',
                'alignment_mode',
                'pass_index',
                'mode',
                'stroke_style',
                # 'stroke_image',
                'fill_style',
                'gradient_type',
                # 'fill_image',
                'use_stroke_holdout',
                'use_overlap_strokes',
                'use_fill_holdout',
            ]
            data['grease_pencil'] = gp_mat_dumper.dump(datablock.grease_pencil)
        elif datablock.use_nodes:
            data['node_tree'] = dump_node_tree(datablock.node_tree)
            data['nodes_animation_data'] = dump_animation_data(datablock.node_tree)

        data['animation_data'] = dump_animation_data(datablock)

        return data

    @staticmethod
    def resolve(data: dict) -> object:
        uuid = data.get('uuid')
        return resolve_datablock_from_uuid(uuid, bpy.data.materials)

    @staticmethod
    def resolve_deps(datablock: object) -> list[object]:
        deps = []

        if datablock.use_nodes and datablock.node_tree:
            deps.extend(get_node_tree_dependencies(datablock.node_tree))
            deps.extend(resolve_animation_dependencies(datablock.node_tree))
        deps.extend(resolve_animation_dependencies(datablock))

        return deps


_type = bpy.types.Material
_class = BlMaterial
