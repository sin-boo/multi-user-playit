import os

import pytest
from deepdiff import DeepDiff

import bpy
import random
from multi_user.bl_types.bl_curve import BlCurve

@pytest.mark.parametrize('curve_type', ['TEXT','BEZIER', 'NURBS'])
def test_curve(clear_blend, curve_type):
    if curve_type == 'TEXT':
        bpy.ops.object.text_add(enter_editmode=False, align='WORLD', location=(0, 0, 0))
    elif curve_type == 'BEZIER':
        bpy.ops.curve.primitive_bezier_curve_add(enter_editmode=False, align='WORLD', location=(0, 0, 0))
    elif curve_type == 'NURBS': #TODO: NURBS support
        bpy.ops.surface.primitive_nurbs_surface_curve_add(radius=1, enter_editmode=False, align='WORLD', location=(0, 0, 0))

    datablock = bpy.data.curves[0]

    implementation = BlCurve()
    expected = implementation.dump(datablock)
    bpy.data.curves.remove(datablock)

    test = implementation.construct(expected)
    implementation.load(expected, test)
    result = implementation.dump(test)

    assert not DeepDiff(expected, result)
