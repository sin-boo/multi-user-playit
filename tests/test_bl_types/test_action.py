import os

import pytest
from deepdiff import DeepDiff

import bpy
import random
from multi_user.bl_types.bl_action import BlAction

from bpy_extras import anim_utils

INTERPOLATION = ['CONSTANT', 'LINEAR', 'BEZIER', 'SINE', 'QUAD', 'CUBIC', 'QUART', 'QUINT', 'EXPO', 'CIRC', 'BACK', 'BOUNCE', 'ELASTIC']
FMODIFIERS = ['GENERATOR', 'FNGENERATOR', 'ENVELOPE', 'CYCLES', 'NOISE', 'LIMITS', 'STEPPED']

# @pytest.mark.parametrize('blendname', ['test_action.blend'])
def test_action(clear_blend):
    # Generate a random action

    bpy.ops.mesh.primitive_plane_add()
    plane = bpy.data.objects[0]
    plane.animation_data_create()

    datablock = bpy.data.actions.new("sdsad")

    plane.animation_data.action = datablock
    datablock.fcurve_ensure_for_datablock(plane, 'location', index=0, group_name="")

    channelbag = anim_utils.action_get_channelbag_for_slot(plane.animation_data.action, plane.animation_data.action_slot)
    fcurve_sample = channelbag.fcurves[0]
    fcurve_sample.keyframe_points.add(100)

    for i, point in enumerate(fcurve_sample.keyframe_points):
        point.co[0] = i
        point.co[1] = random.randint(-10,10)
        point.interpolation = INTERPOLATION[random.randint(0, len(INTERPOLATION)-1)]

    for mod_type in FMODIFIERS:
        fcurve_sample.modifiers.new(mod_type)
    # Test
    implementation = BlAction()
    expected = implementation.dump(datablock)
    bpy.data.actions.remove(datablock)

    test = implementation.construct(expected)
    implementation.load(expected, test)
    result = implementation.dump(test)

    assert not DeepDiff(expected, result)
