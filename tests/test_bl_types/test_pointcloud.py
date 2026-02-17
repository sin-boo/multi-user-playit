from deepdiff import DeepDiff

import bpy
from multi_user.bl_types.bl_pointcloud import BlPointCloud


def test_pointcloud(clear_blend):
    datablock = bpy.data.pointclouds.new('test')
    datablock.attributes.new('position', 'FLOAT_VECTOR', 'POINT')
    datablock.attributes[0].data.foreach_set('vector', [0, 0, 0] * 100)
    implementation = BlPointCloud()
    expected = implementation.dump(datablock)
    bpy.data.pointclouds.remove(datablock)

    test = implementation.construct(expected)
    implementation.load(expected, test)
    result = implementation.dump(test)

    assert not DeepDiff(expected, result)
