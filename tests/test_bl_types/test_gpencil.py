import os

import pytest
from deepdiff import DeepDiff

import bpy
from multi_user.bl_types.bl_gpencil import BlGpencil


def test_gpencil(clear_blend, register_uuid):
    bpy.ops.object.grease_pencil_add(type='MONKEY')

    datablock = bpy.data.grease_pencils[0]

    implementation = BlGpencil()
    expected = implementation.dump(datablock)
    bpy.data.grease_pencils.remove(datablock)

    test = implementation.construct(expected)
    implementation.load(expected, test)
    result = implementation.dump(test)

    assert not DeepDiff(expected, result)
