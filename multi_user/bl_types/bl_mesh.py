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


import logging

import bpy
from replication.exception import ContextError
from replication.protocol import ReplicatedDatablock

from ..utils import get_preferences
from .bl_action import (dump_animation_data, load_animation_data,
                        resolve_animation_dependencies)
from .bl_datablock import resolve_datablock_from_uuid
from .bl_material import dump_materials_slots, load_materials_slots
from .dump_anything import (Dumper, Loader, np_dump_collection,
                            np_dump_collection_primitive, np_load_collection,
                            np_load_collection_primitives)

VERTICE = ['co']

EDGE = [
    'vertices',
    'use_seam',
    'use_edge_sharp',
]
LOOP = [
    'vertex_index',
    'normal',
]

POLYGON = [
    'loop_total',
    'loop_start',
    'use_smooth',
    'material_index',
]

GENERIC_ATTRIBUTES =[
    'crease_vert',
    'crease_edge',
    'bevel_weight_vert',
    'bevel_weight_edge'
]

GENERIC_ATTRIBUTES_ENSURE = {
    'crease_vert': 'vertex_crease_ensure',
    'crease_edge': 'edge_crease_ensure'
}

_MESH_SCALAR_KEYS = [
    'name',
    'use_customdata_edge_bevel',
]

if bpy.app.version < (4, 1, 0):
    _MESH_SCALAR_KEYS.extend(['use_auto_smooth', 'auto_smooth_angle'])

_MESH_LOAD_SKIP = frozenset({
    'vertices',
    'edges',
    'loops',
    'polygons',
    'uv_layers',
    'vertex_colors',
    'attributes',
    'vertex_count',
    'egdes_count',
    'loop_count',
    'poly_count',
    'materials',
    'animation_data',
    'type_id',
    'uuid',
})


def _mesh_has_vertex_colors(mesh: bpy.types.Mesh) -> bool:
    return hasattr(mesh, 'vertex_colors') and mesh.vertex_colors is not None


def _ensure_vertex_color_layer(mesh: bpy.types.Mesh, name: str):
    if _mesh_has_vertex_colors(mesh):
        if name not in mesh.vertex_colors:
            mesh.vertex_colors.new(name=name)
        return mesh.vertex_colors[name].data
    if hasattr(mesh, 'color_attributes'):
        if name not in mesh.color_attributes:
            mesh.color_attributes.new(name, 'BYTE_COLOR', 'CORNER')
        layer = mesh.color_attributes[name]
        if layer.domain == 'CORNER':
            return layer.data
    return None


def _dump_vertex_colors(mesh: bpy.types.Mesh) -> dict:
    dumped = {}
    if _mesh_has_vertex_colors(mesh) and len(mesh.vertex_colors):
        for color_map in mesh.vertex_colors:
            dumped[color_map.name] = {
                'data': np_dump_collection_primitive(color_map.data, 'color')
            }
    elif hasattr(mesh, 'color_attributes'):
        for layer in mesh.color_attributes:
            if layer.data_type not in {'BYTE_COLOR', 'FLOAT_COLOR'}:
                continue
            getter = 'color' if layer.data_type == 'BYTE_COLOR' else 'color_srgb'
            dumped[layer.name] = {
                'data': np_dump_collection_primitive(layer.data, getter),
                'data_type': layer.data_type,
                'domain': layer.domain,
            }
    return dumped


def _load_vertex_colors(mesh: bpy.types.Mesh, layers_data: dict):
    for layer_name, layer_data in layers_data.items():
        color_data = _ensure_vertex_color_layer(mesh, layer_name)
        if color_data is None:
            logging.debug("Skipping vertex color layer %s (unsupported on this Blender version)", layer_name)
            continue
        np_load_collection_primitives(color_data, 'color', layer_data['data'])


class BlMesh(ReplicatedDatablock):
    use_delta = True

    bl_id = "meshes"
    bl_class = bpy.types.Mesh
    bl_check_common = False
    bl_icon = 'MESH_DATA'
    bl_reload_parent = True

    @staticmethod
    def construct(data: dict) -> object:
        return bpy.data.meshes.new(data.get("name"))

    @staticmethod
    def load(data: dict, datablock: object):
        if not datablock or datablock.is_editmode:
            raise ContextError

        load_animation_data(data.get('animation_data'), datablock)

        loader = Loader()
        loader.exclure_filter = list(_MESH_LOAD_SKIP)
        loader.load(
            datablock,
            {key: value for key, value in data.items() if key in _MESH_SCALAR_KEYS},
        )

        # Geometry first — do not wait for materials/textures (fixes skeleton-only sync)
        if datablock.vertices:
            datablock.clear_geometry()

        datablock.vertices.add(data["vertex_count"])
        datablock.edges.add(data["egdes_count"])
        datablock.loops.add(data["loop_count"])
        datablock.polygons.add(data["poly_count"])

        np_load_collection(data['vertices'], datablock.vertices, VERTICE)
        np_load_collection(data['edges'], datablock.edges, EDGE)
        np_load_collection(data['loops'], datablock.loops, LOOP)
        np_load_collection(data["polygons"], datablock.polygons, POLYGON)

        if 'uv_layers' in data:
            for layer in data['uv_layers']:
                if layer not in datablock.uv_layers:
                    datablock.uv_layers.new(name=layer)

                np_load_collection_primitives(
                    datablock.uv_layers[layer].data,
                    'uv',
                    data["uv_layers"][layer]['data'])

        if 'vertex_colors' in data:
            _load_vertex_colors(datablock, data['vertex_colors'])

        for attribute_name, attribute_data_type, attribute_domain, attribute_data in data.get("attributes", []):
            if attribute_name not in datablock.attributes:
                datablock.attributes.new(
                    attribute_name,
                    attribute_data_type,
                    attribute_domain
                )
            np_load_collection(
                attribute_data,
                datablock.attributes[attribute_name].data,
                ['value'],
            )

        datablock.validate()
        datablock.update()

        src_materials = data.get('materials')
        if src_materials:
            try:
                load_materials_slots(src_materials, datablock.materials)
            except Exception as exc:
                logging.warning(
                    "Mesh %s: materials not ready yet, geometry synced without them (%s)",
                    datablock.name,
                    exc,
                )

    @staticmethod
    def dump(datablock: object) -> dict:
        if (datablock.is_editmode or bpy.context.mode == "SCULPT") and not get_preferences().sync_flags.sync_during_editmode:
            raise ContextError("Mesh is in edit mode")
        mesh = datablock

        dumper = Dumper()
        dumper.depth = 1
        dumper.include_filter = list(_MESH_SCALAR_KEYS)

        data = dumper.dump(mesh)

        data['animation_data'] = dump_animation_data(datablock)

        data["vertex_count"] = len(mesh.vertices)
        data["vertices"] = np_dump_collection(mesh.vertices, VERTICE)

        data["egdes_count"] = len(mesh.edges)
        data["edges"] = np_dump_collection(mesh.edges, EDGE)

        data["attributes"] = []
        for attribute_name in GENERIC_ATTRIBUTES:
            if attribute_name in datablock.attributes:
                attribute_data = datablock.attributes.get(attribute_name)
                dumped_attr_data = np_dump_collection(attribute_data.data, ['value'])

                data["attributes"].append(
                    (
                        attribute_name,
                        attribute_data.data_type,
                        attribute_data.domain,
                        dumped_attr_data
                    )
                )

        data["poly_count"] = len(mesh.polygons)
        data["polygons"] = np_dump_collection(mesh.polygons, POLYGON)

        data["loop_count"] = len(mesh.loops)
        data["loops"] = np_dump_collection(mesh.loops, LOOP)

        if mesh.uv_layers:
            data['uv_layers'] = {}
            for layer in mesh.uv_layers:
                data['uv_layers'][layer.name] = {}
                data['uv_layers'][layer.name]['data'] = np_dump_collection_primitive(layer.data, 'uv')

        vertex_colors = _dump_vertex_colors(mesh)
        if vertex_colors:
            data['vertex_colors'] = vertex_colors

        data['materials'] = dump_materials_slots(datablock.materials)
        return data

    @staticmethod
    def resolve_deps(datablock: object) -> list[object]:
        deps = list(resolve_animation_dependencies(datablock))
        for material in datablock.materials:
            if material:
                deps.append(material)
        return deps

    @staticmethod
    def resolve(data: dict) -> object:
        uuid = data.get('uuid')
        return resolve_datablock_from_uuid(uuid, bpy.data.meshes)

    @staticmethod
    def needs_update(datablock: object, data: dict) -> bool:
        return ('EDIT' not in bpy.context.mode and bpy.context.mode != 'SCULPT') \
            or get_preferences().sync_flags.sync_during_editmode


_type = bpy.types.Mesh
_class = BlMesh
