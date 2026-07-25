from __future__ import annotations
"""Unit tests for the rigid-prop section (steering wheel/pedals/needles).

Props are a v6 addition: a trailing static section in the .bmc gated by
FLAG_HAS_PROPS.  The critical guarantee is that this section is purely additive
— it never touches the flexmesh pool's vertex_count or the per-frame layout, so
old captures still read identically and the car-transform offset is unchanged.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np

from importer import capture_format as bmc
from importer.cache_builder import _prop_to_blender_frozen, _quat_to_matrix


def _sample_prop(name="prop_steer", with_uv=True):
    verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float32)
    indices = np.array([0, 1, 2, 0, 2, 3], dtype=np.uint32)
    uvs = (np.array([[0, 0], [1, 0], [0, 1], [1, 1]], dtype=np.float32)
           if with_uv else None)
    return bmc.PropEntry(
        name=name,
        material_id=7,
        position=np.array([2.0, -3.0, 0.5], dtype=np.float32),
        rotation=np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32),
        vertices=verts,
        indices=indices,
        uvs=uvs,
    )


def test_prop_section_roundtrip():
    props = [_sample_prop("prop_steer", True), _sample_prop("prop_needle", False)]
    blob = bmc.pack_prop_section(props)
    out = bmc.unpack_prop_section(blob)
    assert len(out) == 2
    a, b = out
    assert a.name == "prop_steer" and b.name == "prop_needle"
    assert a.material_id == 7
    np.testing.assert_array_equal(a.vertices, props[0].vertices)
    np.testing.assert_array_equal(a.indices, props[0].indices)
    np.testing.assert_array_equal(a.uvs, props[0].uvs)
    np.testing.assert_allclose(a.position, props[0].position)
    np.testing.assert_allclose(a.rotation, props[0].rotation)
    # Second prop had no UVs -> preserved as None.
    assert b.uvs is None


def test_empty_prop_section():
    blob = bmc.pack_prop_section([])
    assert bmc.unpack_prop_section(blob) == []


def test_has_props_flag():
    h = bmc.BmcHeader(version=1, vertex_count=100, index_count=300,
                      primitive_count=1, material_count=1,
                      flags=bmc.FLAG_HAS_PROPS, frame_size=0, static_size=0)
    assert h.has_props
    # Legacy header (no prop flag) must report False — backward compat.
    h2 = bmc.BmcHeader(version=1, vertex_count=100, index_count=300,
                       primitive_count=1, material_count=1,
                       flags=bmc.FLAG_HAS_UVS | bmc.FLAG_HAS_TRANSFORM,
                       frame_size=0, static_size=0)
    assert not h2.has_props


def test_frozen_bake_identity_rotation():
    # With identity rotation, the frozen bake is: gltf = v + (px,pz,-py),
    # then Blender (x,-z,y).  Check a single vertex end to end.
    verts = np.array([[1.0, 2.0, 3.0]], dtype=np.float32)
    pos = np.array([10.0, 20.0, 30.0], dtype=np.float32)  # px,py,pz
    rot = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
    out = _prop_to_blender_frozen(verts, pos, rot)
    # gltf translation = (px, pz, -py) = (10, 30, -20); v stays (1,2,3)
    # gltf point = (11, 32, -17); Blender (x,-z,y) = (11, 17, 32)
    np.testing.assert_allclose(out[0], [11.0, 17.0, 32.0], atol=1e-5)


def test_quat_matrix_orthonormal():
    q = np.array([0.1, 0.2, 0.3, 0.9], dtype=np.float64)
    q = q / np.linalg.norm(q)
    R = _quat_to_matrix(q)
    np.testing.assert_allclose(R @ R.T, np.eye(3), atol=1e-6)
    assert abs(np.linalg.det(R) - 1.0) < 1e-6
