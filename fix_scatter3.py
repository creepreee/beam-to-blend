import re

with open('runtime/debris_spawn.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find the exact section to replace
idx = content.find('floor = settings.ground_z + GROUND_CLEARANCE - low_off\n        # Position jitter: offset start slightly along pane plane.')
if idx == -1:
    print("Start not found")
    exit(1)

end = content.find('        # Horizontal velocity (XY only; Z forced to -2 m/s).', idx)
if end == -1:
    print("End not found")
    exit(1)

old = content[idx:end]

# The exact old text
old_text = """floor = settings.ground_z + GROUND_CLEARANCE - low_off
        # Position jitter: offset start slightly along pane plane.
        jitter_scale = 0.03 * float(rng.uniform(0.5, 1.5))
        jitter_dir = np.array([float(rng.uniform(-1, 1)),
                               float(rng.uniform(-1, 1)), 0.0])
        jitter_dir = jitter_dir / (np.linalg.norm(jitter_dir) + 1e-9)
        base = np.array(frag.centre, dtype=np.float64) + jitter_dir * jitter_scale

        # Compute velocity: outward from impact, with jitter, forced downward.
        blow = float(np.exp(-2.4 * frag.impact_distance))
        away = np.array(frag.centre, dtype=np.float64) - np.array(event.position, dtype=np.float64)
        n = np.linalg.norm(away)
        away = away / n if n > 1e-6 else np.array((0.0, 0.0, 1.0))

        # Direction jitter: rotate away around pane_normal (±35°) and tilt (±15°).
        theta = float(rng.uniform(-0.61, 0.61))  # ±35°
        c, s = np.cos(theta), np.sin(theta)
        away_rot = (away * c + np.cross(pane_normal, away) * s +
                    pane_normal * (pane_normal @ away) * (1 - c))
        tilt = float(rng.uniform(-0.26, 0.26))  # ±15°
        away_rot = away_rot * np.cos(tilt) + pane_normal * np.sin(tilt)
        away = away_rot / np.linalg.norm(away_rot)

        # Base separation speed: even at speed=0, give each fragment a strong
        # random outward kick so they don't fall as a solid sheet.
        # With scatter=5, need much stronger separation to overcome radial clumping.
        scatter = max(0.0, float(settings.scatter)) * (0.4 + 0.6 * intensity)
        base_sep = scatter * float(rng.uniform(8.0, 15.0))  # was 0.8-1.8, now 8-15 for real separation
        launch_speed = (settings.speed * profile_for("glass").speed_bias
                        * (0.3 + 1.4 * intensity))
        vel = away * (launch_speed + base_sep) * blow * float(rng.uniform(0.55, 1.4))
        vel = vel + part_vel * settings.inherit_velocity * intensity
        vel[2] = min(vel[2], -2.0)  # force downward

        # Horizontal velocity (XY only; Z forced to -2 m/s).
        vel_xy = vel[:2].copy()
        dt = 1.0 / max(1e-6, float(output_fps))"""

new_text = """floor = settings.ground_z + GROUND_CLEARANCE - low_off
        # Position jitter: offset start SIGNIFICANTLY along pane plane to prevent clumping.
        # Was 3cm, now 0.5-2.0m to ensure fragments start separated.
        jitter_scale = float(rng.uniform(0.5, 2.0))
        jitter_dir = np.array([float(rng.uniform(-1, 1)),
                               float(rng.uniform(-1, 1)), 0.0])
        jitter_dir = jitter_dir / (np.linalg.norm(jitter_dir) + 1e-9)
        base = np.array(frag.centre, dtype=np.float64) + jitter_dir * jitter_scale

        # Compute velocity: outward from impact, with jitter, forced downward.
        blow = float(np.exp(-2.4 * frag.impact_distance))
        away = np.array(frag.centre, dtype=np.float64) - np.array(event.position, dtype=np.float64)
        n = np.linalg.norm(away)
        away = away / n if n > 1e-6 else np.array((0.0, 0.0, 1.0))

        # Direction jitter: rotate away around pane_normal (±60°) and tilt (±30°).
        # Increased from ±35°/±15° to ensure real angular spread.
        theta = float(rng.uniform(-1.05, 1.05))  # ±60°
        c, s = np.cos(theta), np.sin(theta)
        away_rot = (away * c + np.cross(pane_normal, away) * s +
                    pane_normal * (pane_normal @ away) * (1 - c))
        tilt = float(rng.uniform(-0.52, 0.52))  # ±30°
        away_rot = away_rot * np.cos(tilt) + pane_normal * np.sin(tilt)
        away = away_rot / np.linalg.norm(away_rot)

        scatter = max(0.0, float(settings.scatter)) * (0.4 + 0.6 * intensity)
        base_sep = scatter * float(rng.uniform(8.0, 15.0))
        launch_speed = (settings.speed * profile_for("glass").speed_bias
                        * (0.3 + 1.4 * intensity))
        vel = away * (launch_speed + base_sep) * blow * float(rng.uniform(0.55, 1.4))
        vel = vel + part_vel * settings.inherit_velocity * intensity
        vel[2] = min(vel[2], -2.0)  # force downward

        # Horizontal velocity (XY only; Z forced to -2 m/s).
        vel_xy = vel[:2].copy()
        dt = 1.0 / max(1e-6, float(output_fps))"""

if old in content:
    content = content.replace(old, new_text)
    with open('runtime/debris_spawn.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('Replaced successfully')
else:
    print('OLD STRING NOT FOUND')
    # Find the difference
    idx = content.find('floor = settings.ground_z + GROUND_CLEARANCE - low_off')
    if idx >= 0:
        print('Found at', idx)
        print(repr(content[idx:idx+500]))