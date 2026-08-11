from __future__ import annotations
"""Unit tests for the cracked-pane placement math (pure numpy, no Blender).

``glass_crack.crack_placement`` turns a world-space impact into an offset on
the pane's own plane — the input to the crack shader.  The key contracts:

* the impact is projected onto the pane's fitted plane and CLAMPED inside the
  pane's UV outline (a rock cannot punch a hole outside the glass),
* the pane basis (u, v) is orthonormal, so the shader's 2D web is undistorted,
* ``world_to_cache_local`` is the exact inverse of ``local_to_world`` including
  the ground-shift that playback applies outside the mesh.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pytest

from runtime.glass_crack import (
    crack_placement,
    hole_radius_for,
    world_to_cache_local,
)
from runtime.impact_detect import local_to_world


def _quad():
    """A 2 x 2 m quad lying in the X-Y plane (pane normal = +Z)."""
    return np.array([[0.0, 0.0, 0.0],
                     [2.0, 0.0, 0.0],
                     [2.0, 2.0, 0.0],
                     [0.0, 2.0, 0.0]], dtype=np.float64)


def _rotz(deg):
    t = np.deg2rad(deg)
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def test_hole_radius_scales_with_severity_and_scale():
    small = hole_radius_for(0.05, 0.0)
    hard = hole_radius_for(0.05, 1.0)
    assert 0.0 < small < hard
    assert small == pytest.approx(0.05 * 0.5)
    assert hard == pytest.approx(0.05 * 2.0)


def test_hole_radius_clamped_nonnegative():
    assert hole_radius_for(-5.0, 1.0) == 0.0


def test_placement_projects_impact_onto_pane_plane():
    verts = _quad() @ _rotz(30.0)   # raked pane, tilted in the car
    impact = np.array([1.0, 1.0, 5.0]) @ _rotz(30.0)  # z is off-plane
    p = crack_placement(verts, impact, 0.05)
    # In-plane centre is the projected impact, clamped to the pane's outline.
    assert p.centre[2] == pytest.approx(0.0, abs=1e-9)
    assert p.centre[:2] == pytest.approx(
        (np.array([1.0, 1.0, 0.0]) @ _rotz(30.0))[:2], abs=1e-9)
    assert np.isclose(np.linalg.norm(p.u), 1.0)
    assert np.isclose(np.linalg.norm(p.v), 1.0)
    assert np.isclose(np.dot(p.u, p.v), 0.0, atol=1e-9)
    assert p.pane_radius == pytest.approx(np.sqrt(2.0), abs=1e-9)


def test_placement_clamps_impact_inside_the_pane():
    verts = _quad()
    # Impact 10 m outside the pane: the hole must land ON the glass.
    p = crack_placement(verts, np.array([20.0, 20.0, 0.0]), 0.05)
    assert 0.0 <= p.centre[0] <= 2.0
    assert 0.0 <= p.centre[1] <= 2.0


def test_placement_clamps_hole_radius_to_pane():
    verts = _quad()
    p = crack_placement(verts, np.array([1.0, 1.0, 0.0]), 100.0)
    assert p.hole_radius <= 0.6 * p.pane_radius
    assert p.hole_radius > 0.0


def test_placement_requires_vertices():
    with pytest.raises(ValueError):
        crack_placement(np.zeros((2, 3)), np.zeros(3), 0.05)


def test_world_to_cache_local_round_trips():
    transform = np.array([10.0, -4.0, 2.5,   # translation
                          1.0, 0.0, 0.0,     # row 0 of the rotation
                          0.0, 0.0, 1.0,     # row 1 (90 deg about Y)
                          0.0, -1.0, 0.0],   # row 2
                         dtype=np.float64)
    local = np.array([0.7, -1.2, 0.3], dtype=np.float64)
    world = local_to_world(local[None, :], transform, ground_shift=1.7)[0]
    back = world_to_cache_local(world, transform, ground_shift=1.7)
    assert back == pytest.approx(local, abs=1e-9)


def test_world_to_cache_local_handles_missing_transform():
    local = world_to_cache_local(np.array([5.0, 5.0, 5.0]), None,
                                 ground_shift=1.0)
    assert local == pytest.approx(np.array([5.0, 5.0, 4.0]))
