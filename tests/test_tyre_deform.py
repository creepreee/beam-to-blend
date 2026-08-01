from __future__ import annotations
"""Unit tests for fake tyre ground-contact flattening.

The guarantees that matter for the feature to look right:

* a tyre resting on / sunk into the ground gets a FLAT patch at ground level;
* the patch grows with load (deeper hub => wider patch) — this is what makes it
  read as real rubber rather than a constant scale;
* a tyre lifted clear of the ground is EXACTLY round again, bit-for-bit, with no
  residual flat spot (the user's specific complaint about the naive approach);
* nothing accumulates across frames — frame N depends only on frame N;
* amount=0 is a true no-op, and non-tyre objects are never touched.

Pure numpy, no Blender needed.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pytest

from runtime.tyre_deform import (
    TyreSettings,
    axle_axis,
    flatten_tyre,
    height_basis_from_transform,
    parse_names,
)

UP = np.array([0.0, 0.0, 1.0], dtype=np.float32)


def _wheel(radius=0.32, width=0.2, centre_z=0.32, n_theta=64, n_w=5):
    """A tyre-shaped cylindrical shell: axle along +X, spinning in the YZ plane."""
    th = np.linspace(0.0, 2.0 * math.pi, n_theta, endpoint=False)
    xs = np.linspace(-0.5 * width, 0.5 * width, n_w)
    pts = []
    for x in xs:
        for t in th:
            pts.append((x, radius * math.cos(t), centre_z + radius * math.sin(t)))
    return np.array(pts, dtype=np.float32)


def _settings(**kw):
    base = dict(amount=1.0, extra=0.0, bulge=0.0, release=0.03)
    base.update(kw)
    return TyreSettings(**base)


# --- the core contact patch -------------------------------------------------

def test_penetrating_tyre_gets_flat_patch_at_ground():
    """A hub sunk below the unloaded radius produces a flat patch at Z=0."""
    pos = _wheel(centre_z=0.30)  # radius 0.32 => 0.02 m of penetration
    assert pos[:, 2].min() < 0.0
    out, squash = flatten_tyre(pos, UP, 0.0, _settings())
    assert out[:, 2].min() >= -1e-6, "no vertex may remain below the ground"
    assert squash > 0.0
    # The clipped vertices form a flat patch, not a rounded one.
    patch = out[np.abs(out[:, 2]) < 1e-5]
    assert len(patch) > 0
    assert patch[:, 2].std() < 1e-6, "contact patch must be flat"


def test_patch_grows_with_load():
    """Deeper penetration => wider contact patch (load-sensitive, not constant)."""
    s = _settings()
    widths = []
    for centre_z in (0.315, 0.30, 0.28):  # progressively more loaded
        out, _ = flatten_tyre(_wheel(centre_z=centre_z), UP, 0.0, s)
        on_ground = np.abs(out[:, 2]) < 1e-5
        widths.append(float(np.ptp(out[on_ground][:, 1])))
    assert widths[0] < widths[1] < widths[2], f"patch must grow with load: {widths}"


def test_static_deflection_gives_patch_without_penetration():
    """A tyre exactly touching the ground still flattens (the 'extra' term).

    Without this a resting-but-not-sunk wheel would have nothing to clip and so
    would show no patch at all.
    """
    pos = _wheel(centre_z=0.32)  # bottom exactly at Z=0, nothing to clip
    assert abs(float(pos[:, 2].min())) < 1e-6
    out, squash = flatten_tyre(pos, UP, 0.0, _settings(extra=0.02))
    assert squash > 0.0
    assert out[:, 2].min() >= -1e-6
    patch = out[np.abs(out[:, 2]) < 1e-5]
    assert len(patch) > 1, "static deflection must produce a real patch"


def test_deflection_spares_the_bead_and_upper_carcass():
    """The squash is weighted to the tread; the top of the tyre must not move."""
    pos = _wheel(centre_z=0.32)
    out, _ = flatten_tyre(pos, UP, 0.0, _settings(extra=0.02))
    top = pos[:, 2] > 0.60  # near the crown, well above the axle
    np.testing.assert_allclose(out[top], pos[top], atol=1e-6)


# --- lift-off release: the user's actual complaint --------------------------

def test_airborne_tyre_is_exactly_round():
    """Lifted clear of the ground => untouched geometry, zero residual flatness."""
    pos = _wheel(centre_z=0.32 + 0.10)  # 100 mm of air, well past release
    out, squash = flatten_tyre(pos, UP, 0.0, _settings(extra=0.02))
    assert squash == 0.0
    np.testing.assert_array_equal(out, pos)


def test_release_ramps_smoothly_to_zero():
    """Squash decreases monotonically to 0 as the tyre lifts through `release`."""
    s = _settings(extra=0.02, release=0.03)
    squashes = []
    for gap in (0.0, 0.005, 0.01, 0.02, 0.029, 0.031):
        _out, sq = flatten_tyre(_wheel(centre_z=0.32 + gap), UP, 0.0, s)
        squashes.append(sq)
    assert squashes[0] > 0.0
    assert all(a >= b for a, b in zip(squashes, squashes[1:])), squashes
    assert squashes[-1] == 0.0, "past `release` the effect must be exactly zero"


def test_no_state_carried_between_frames():
    """Landing then lifting returns the identical original mesh (no hysteresis).

    This is the 'tyres stay slightly flattened from the start' failure mode: the
    deformation must be a pure function of the current frame's geometry.
    """
    s = _settings(extra=0.02, bulge=0.6)
    airborne = _wheel(centre_z=0.50)
    first, _ = flatten_tyre(airborne, UP, 0.0, s)
    flatten_tyre(_wheel(centre_z=0.28), UP, 0.0, s)  # a heavily loaded frame
    again, _ = flatten_tyre(airborne, UP, 0.0, s)
    np.testing.assert_array_equal(first, again)
    np.testing.assert_array_equal(again, airborne)


def test_input_array_is_never_mutated():
    """The cache is a read-only mmap view — deforming must copy, not write back."""
    pos = _wheel(centre_z=0.29)
    original = pos.copy()
    flatten_tyre(pos, UP, 0.0, _settings(extra=0.02, bulge=0.6))
    np.testing.assert_array_equal(pos, original)


# --- master switch / filtering ---------------------------------------------

def test_amount_zero_is_a_noop():
    pos = _wheel(centre_z=0.28)
    out, squash = flatten_tyre(pos, UP, 0.0, _settings(amount=0.0, extra=0.02))
    assert squash == 0.0
    assert out is pos, "disabled must short-circuit before any work"


def test_amount_scales_the_flattening():
    """Half strength lifts a penetrating vertex half the way to the ground."""
    pos = _wheel(centre_z=0.30)
    deepest = float(pos[:, 2].min())
    half, _ = flatten_tyre(pos, UP, 0.0, _settings(amount=0.5))
    assert half[:, 2].min() == pytest.approx(deepest * 0.5, abs=1e-6)


def test_name_filter_matches_tyres_only():
    s = TyreSettings()
    assert s.matches("tire_01a_16x7_26")
    assert s.matches("tire_01a_16x7_26DDD")
    assert s.matches("Front_Tyre_L")
    # Rims, hubs, brakes and body panels must stay rigid.
    for name in ("flanje_e180_wheel", "flanje_e180_wheelcap", "brake_disc_plain",
                 "brake_hub_5l", "flanje_e180_body", "flanje_e180_door_FL"):
        assert not s.matches(name), name


def test_parse_names_forms():
    assert parse_names("tire, tyre") == ("tire", "tyre")
    assert parse_names(["Tire", " Rubber "]) == ("tire", "rubber")
    assert parse_names("") == ("tire", "tyre")     # empty falls back to default
    assert parse_names(None) == ("tire", "tyre")


# --- bulge ------------------------------------------------------------------

def test_bulge_widens_the_tyre_along_the_axle():
    pos = _wheel(centre_z=0.29)
    flat_only, _ = flatten_tyre(pos, UP, 0.0, _settings(bulge=0.0))
    bulged, _ = flatten_tyre(pos, UP, 0.0, _settings(bulge=1.0))
    # Axle is +X for our test wheel, so the bulge shows up as extra X spread.
    assert float(np.ptp(bulged[:, 0])) > float(np.ptp(flat_only[:, 0]))
    # ...and only near the ground: the crown keeps its original width.
    crown = pos[:, 2] > 0.55
    np.testing.assert_allclose(bulged[crown, 0], flat_only[crown, 0], atol=1e-6)


def test_bulge_never_pushes_vertices_back_below_ground():
    """The bulge must be horizontal.

    A cambered/steered axle tilts out of the ground plane, so displacing along it
    would shove sidewall vertices back down through the ground the contact-patch
    step just lifted them onto (measured ~3.5 mm of re-penetration on real
    capture data before the axle was flattened into the ground plane).
    """
    pos = _wheel(centre_z=0.29)
    # Tilt the whole wheel to give it camber, so the axle is no longer horizontal.
    a = math.radians(12.0)
    R = np.array([[math.cos(a), 0, -math.sin(a)],
                  [0, 1, 0],
                  [math.sin(a), 0, math.cos(a)]], dtype=np.float32)
    pos = (pos @ R.T).astype(np.float32)
    out, _ = flatten_tyre(pos, UP, 0.0, _settings(extra=0.02, bulge=1.5))
    assert out[:, 2].min() >= -1e-5, (
        f"bulge re-penetrated the ground by {out[:, 2].min()*1000:.3f} mm")


def test_squash_is_clamped_to_a_fraction_of_the_radius():
    """A wildly wrong ground_z over-squashes but never pancakes the wheel."""
    from runtime.tyre_deform import MAX_SQUASH_RATIO

    radius, extra = 0.32, 0.02
    pos = _wheel(radius=radius, centre_z=radius)
    # Ground 1 m ABOVE the tyre: without the clamp the whole lower half collapses.
    out, squash = flatten_tyre(pos, UP, -1.0, _settings(extra=extra))
    # `squash` reports the deflection twice (once as its own term, once inside the
    # clipped depth it creates), so the bound carries 2*extra on top of the clamp.
    assert squash <= MAX_SQUASH_RATIO * radius + 2.0 * extra + 1e-6, squash
    # The geometric guarantee: the wheel keeps most of its height, i.e. it still
    # reads as a wheel rather than a pancake.
    assert float(np.ptp(out[:, 2])) > 0.6 * (2.0 * radius)


def test_axle_axis_recovers_the_spin_axis():
    axis = axle_axis(_wheel(), UP)
    assert axis is not None
    assert abs(abs(float(axis[0])) - 1.0) < 0.05, f"expected ~+/-X, got {axis}"


def test_axle_axis_declines_on_non_wheel_clouds():
    assert axle_axis(np.zeros((4, 3), dtype=np.float32), UP) is None   # too few
    # A flat horizontal plate's smallest-variance axis is vertical -> not a wheel.
    plate = np.random.default_rng(0).normal(size=(400, 3)).astype(np.float32)
    plate[:, 2] *= 0.001
    assert axle_axis(plate, UP) is None


# --- height basis from the cache's rigid transform --------------------------

def test_height_basis_identity_transform():
    tf = np.array([0, 0, 0.5] + [1, 0, 0, 0, 1, 0, 0, 0, 1], dtype=np.float32)
    up, off = height_basis_from_transform(tf)
    np.testing.assert_allclose(up, [0, 0, 1], atol=1e-6)
    assert off == pytest.approx(0.5, abs=1e-6)


def test_height_basis_folds_in_ground_z():
    tf = np.array([0, 0, 0.5] + [1, 0, 0, 0, 1, 0, 0, 0, 1], dtype=np.float32)
    _up, off = height_basis_from_transform(tf, ground_z=0.2)
    assert off == pytest.approx(0.3, abs=1e-6)


def test_height_basis_matches_world_height_when_rolled():
    """`pos @ up + offset` must equal the true world Z for a rotated vehicle."""
    a = math.radians(30.0)  # roll about the vehicle's X axis
    M = np.array([[1, 0, 0],
                  [0, math.cos(a), -math.sin(a)],
                  [0, math.sin(a), math.cos(a)]], dtype=np.float64)
    p = np.array([3.0, -2.0, 0.7])
    tf = np.concatenate([p, M.reshape(-1)]).astype(np.float32)
    up, off = height_basis_from_transform(tf)

    local = np.array([[0.4, 0.9, -0.3], [-1.0, 0.2, 0.8]], dtype=np.float32)
    world_z = (local @ M.T + p)[:, 2]          # true world height
    measured = local @ up + off                # what flatten_tyre uses
    np.testing.assert_allclose(measured, world_z, atol=1e-5)


def test_height_basis_without_transform_data():
    up, off = height_basis_from_transform(None, ground_z=0.25)
    np.testing.assert_allclose(up, [0, 0, 1], atol=1e-6)
    assert off == pytest.approx(-0.25, abs=1e-6)


def test_rolled_vehicle_flattens_against_world_ground():
    """A rolled wheel still flattens on the WORLD ground, not its local XY."""
    a = math.radians(25.0)
    M = np.array([[1, 0, 0],
                  [0, math.cos(a), -math.sin(a)],
                  [0, math.sin(a), math.cos(a)]], dtype=np.float64)
    tf = np.concatenate([[0, 0, 0], M.reshape(-1)]).astype(np.float32)
    up, off = height_basis_from_transform(tf)

    local = _wheel(centre_z=0.32)
    # Lower it in the vehicle frame until it penetrates the world ground.
    local[:, 2] -= 0.03
    out, squash = flatten_tyre(local, up, off, _settings())
    assert squash > 0.0
    world_z = out @ np.asarray(M[2], dtype=np.float32)
    assert world_z.min() >= -1e-5, "must not sit below the world ground plane"


# --- settings (de)serialisation for undo/reload recovery --------------------

def test_settings_roundtrip():
    s = TyreSettings(amount=0.8, extra=0.015, bulge=0.4, release=0.05,
                     ground_z=1.25)
    s.update(names="tire,rubber")
    back = TyreSettings.from_dict(s.to_dict())
    assert back.to_dict() == s.to_dict()
    assert back.names == ("tire", "rubber")
    assert back.enabled


def test_settings_update_ignores_none_and_unknown():
    s = TyreSettings(amount=0.5)
    s.update(amount=None, bogus=1.0, bulge=0.25)
    assert s.amount == 0.5
    assert s.bulge == 0.25
    assert not hasattr(s, "bogus")


def test_enabled_threshold():
    assert not TyreSettings(amount=0.0).enabled
    assert TyreSettings(amount=0.5).enabled
