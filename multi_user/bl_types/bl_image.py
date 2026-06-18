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

from pathlib import Path

import bpy
import logging

from replication.protocol import ReplicatedDatablock
from .dump_anything import Dumper, Loader
from .bl_file import get_filepath
from .bl_datablock import preserve_replicated_datablock, resolve_datablock_from_uuid


format_to_ext = {
    'BMP': 'bmp',
    'IRIS': 'sgi',
    'PNG': 'png',
    'JPEG': 'jpg',
    'JPEG2000': 'jp2',
    'TARGA': 'tga',
    'TARGA_RAW': 'tga',
    'CINEON': 'cin',
    'DPX': 'dpx',
    'OPEN_EXR_MULTILAYER': 'exr',
    'OPEN_EXR': 'exr',
    'HDR': 'hdr',
    'TIFF': 'tiff',
    'AVI_JPEG': 'avi',
    'AVI_RAW': 'avi',
    'FFMPEG': 'mpeg',
}


def _image_filename(data: dict, image: bpy.types.Image | None = None) -> str:
    filename = Path(data.get('filename') or '').name
    if filename:
        return filename

    name = data.get('name') or (image.name if image else 'image')
    fallback = Path(name).name
    if not Path(fallback).suffix:
        fallback = f"{fallback}.png"
    return fallback


class BlImage(ReplicatedDatablock):
    bl_id = "images"
    bl_class = bpy.types.Image
    bl_check_common = False
    bl_icon = 'IMAGE_DATA'
    bl_reload_parent = False

    @staticmethod
    def construct(data: dict) -> object:
        image = bpy.data.images.new(
            name=data['name'],
            width=data['size'][0],
            height=data['size'][1]
        )
        preserve_replicated_datablock(image)
        return image

    @staticmethod
    def load(data: dict, datablock: object):
        loader = Loader()
        loader.load(datablock, data)

        datablock.source = 'FILE'
        filename = _image_filename(data, datablock)
        filepath = Path(get_filepath(filename))
        datablock.filepath_raw = str(filepath)
        color_space_name = data.get("colorspace")

        if color_space_name:
            datablock.colorspace_settings.name = color_space_name

        preserve_replicated_datablock(datablock)

        if filepath.is_file():
            try:
                datablock.reload()
                logging.info(
                    "Image %s loaded from %s (%sx%s, users=%s, fake_user=%s)",
                    datablock.name,
                    filepath,
                    datablock.size[0],
                    datablock.size[1],
                    datablock.users,
                    datablock.use_fake_user,
                )
            except Exception as exc:
                logging.warning(
                    "Image %s: reload failed for %s (%s)",
                    datablock.name,
                    filepath,
                    exc,
                )
        else:
            logging.warning(
                "Image %s: texture file missing at %s (will retry after file sync)",
                datablock.name,
                filepath,
            )

    @staticmethod
    def dump(datablock: object) -> dict:
        filename = Path(datablock.filepath).name
        if not filename:
            filename = _image_filename({'name': datablock.name}, datablock)

        data = {
            "filename": filename
        }

        dumper = Dumper()
        dumper.depth = 2
        dumper.include_filter = [
            "name",
            # 'source',
            'size',
            'alpha_mode']
        data.update(dumper.dump(datablock))
        data['colorspace'] = datablock.colorspace_settings.name

        return data

    @staticmethod
    def resolve(data: dict) -> object:
        uuid = data.get('uuid')
        return resolve_datablock_from_uuid(uuid, bpy.data.images)

    @staticmethod
    def resolve_deps(datablock: object) -> list[object]:
        deps = []

        if datablock.packed_file:
            filename = Path(bpy.path.abspath(datablock.filepath)).name
            datablock.filepath_raw = get_filepath(filename)
            datablock.save()
            # An image can't be unpacked to the modified path
            # TODO: make a bug report
            datablock.unpack(method="REMOVE")

        elif datablock.source == "GENERATED":
            filename = f"{datablock.name}.png"
            datablock.filepath = get_filepath(filename)
            datablock.save()

        if datablock.filepath:
            deps.append(Path(bpy.path.abspath(datablock.filepath)))

        return deps

    @staticmethod
    def needs_update(datablock: object, data:dict)-> bool:
        if datablock.is_dirty:
            datablock.save()

        return True


def reload_images_waiting_for_files(repository) -> int:
    """Reload images whose texture files arrived after the image datablock was applied."""
    from replication.constants import UP

    reloaded = 0
    for node in repository.graph.values():
        if node.state != UP or not node.instance or not node.data:
            continue
        if node.data.get('type_id') != 'Image':
            continue

        image = node.instance
        filepath = Path(bpy.path.abspath(image.filepath))
        if not filepath.is_file():
            continue

        needs_reload = (
            image.size[0] == 0
            or image.size[1] == 0
            or not getattr(image, 'has_data', True)
        )
        if not needs_reload:
            continue

        try:
            image.reload()
            preserve_replicated_datablock(image)
            reloaded += 1
            logging.info(
                "Image %s reloaded after file sync (%sx%s)",
                image.name,
                image.size[0],
                image.size[1],
            )
        except Exception as exc:
            logging.warning(
                "Image %s: delayed reload failed for %s (%s)",
                image.name,
                filepath,
                exc,
            )
    return reloaded


_type = bpy.types.Image
_class = BlImage
