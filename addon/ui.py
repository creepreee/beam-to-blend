import os

import bpy
from bpy.types import Panel, PropertyGroup
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty


def _default_workers() -> int:
    """A safe default worker count: leave 2 cores for Blender/OS, cap at 8."""
    cpu = os.cpu_count() or 1
    return max(1, min(8, cpu - 2))


print("[BeamNG] add-on loading...")


def _on_start_frame_update(self, context):
    """Slide the whole animation along the timeline on the LIVE handler.

    The cache-frame mapping is pure arithmetic on the start offset, so this
    needs no re-import — the handler re-derives frame_start/frame_end and
    re-runs the current frame.
    """
    try:
        from runtime import frame_handler
        frame_handler.update_start_frame(int(self.start_frame))
    except Exception:
        pass


def _on_playback_fps_update(self, context):
    """Push the new playback speed to the live handler (no re-import needed)."""
    try:
        from runtime import frame_handler
        frame_handler.update_fps(playback_fps=int(self.playback_fps))
    except Exception:
        pass


def _on_output_fps_update(self, context):
    """Push the new output/render fps to the live handler (no re-import needed)."""
    try:
        from runtime import frame_handler
        frame_handler.update_fps(output_fps=int(self.output_fps))
    except Exception:
        pass


def _on_tyre_update(self, context):
    """Push tyre ground-contact settings to the live handler and redraw.

    Fires on every slider drag so the flattening is tunable interactively — the
    handler re-runs the current frame, so there is no need to scrub or re-import.
    """
    try:
        from runtime import frame_handler
        frame_handler.update_tyre(
            amount=self.tyre_flatten,
            extra=self.tyre_deflection,
            bulge=self.tyre_bulge,
            release=self.tyre_release,
            ground_z=self.tyre_ground_z,
            names=self.tyre_names,
        )
    except Exception:
        pass


def _on_smooth_stop_update(self, context):
    """Toggle/retune the smooth-stop tail on the LIVE handler (no re-import)."""
    try:
        from runtime import frame_handler
        frame_handler.update_smooth_stop(
            enabled=bool(self.car_smooth_stop),
            frames=int(self.car_smooth_stop_frames),
            start_frame=int(self.car_smooth_stop_start),
        )
    except Exception:
        pass


class BeamNGSceneProperties(PropertyGroup):
    sequence_dir: StringProperty(
        name="Sequence Folder",
        description="Folder containing the .glb frame files",
        subtype="DIR_PATH",
        default="",
    )
    cache_path: StringProperty(
        name="Cache File",
        description="Path to the .bvc cache file",
        subtype="FILE_PATH",
        default="",
    )
    use_chunked: BoolProperty(
        name="Chunked Playback (faster)",
        description="Merge small parts into groups for fewer GPU uploads. "
                    "Source objects stay available in a hidden collection.",
        default=False,
    )
    weld_cache: BoolProperty(
        name="Weld duplicate vertices",
        description="Collapse coincident (seam-split) vertices when building the "
                    "cache. Smaller cache + lets Shade Smooth / Weighted Normal work. "
                    "The build verifies per frame that welded vertices never "
                    "separate, and aborts if any do.",
        default=False,
    )
    vehicle_dir: StringProperty(
        name="Vehicle Folder",
        description="Folder containing extracted BeamNG vehicle files "
                    "(with .materials.json and textures). "
                    "Typically the vehicle's root folder (e.g. flanje_e180).",
        subtype="DIR_PATH",
        default="",
    )
    use_game_textures: BoolProperty(
        name="Include Base Game Textures",
        description="Also resolve materials from the game's content/vehicles "
                    "zips. Mods inherit shared materials (tyres, brake discs, "
                    "mirrors, licence plates) from the base game, so without "
                    "this those parts import untextured",
        default=True,
    )
    game_dir: StringProperty(
        name="Game Folder",
        description="BeamNG.drive install folder (the one containing "
                    "content\\vehicles). Leave empty to auto-detect",
        subtype="DIR_PATH",
        default="",
    )
    workers: IntProperty(
        name="Parallel Workers",
        description="Number of worker processes for scanning and cache building. "
                    "Frames are read in parallel across CPU cores (each frame is "
                    "independent), giving a large speedup on long sequences. "
                    "1 = sequential. Falls back to sequential automatically if the "
                    "process pool can't start.",
        default=_default_workers(),
        min=1,
        max=64,
        soft_max=32,
    )
    start_frame: IntProperty(
        name="Start at Frame",
        description="Animation starts at this Blender timeline FRAME — cache "
                    "frame 0 lands on this frame number, the same number the "
                    "timeline shows. LIVE: drag it after importing and the "
                    "whole animation slides, no re-import needed. Note this is "
                    "a frame, not a second, so it does not follow Output FPS — "
                    "set Output FPS first, then pick the start frame.",
        default=0,
        min=0,
        soft_max=2000,
        update=_on_start_frame_update,
    )
    playback_fps: IntProperty(
        name="Playback Speed (src fps)",
        description="Animation SPEED: how many CAPTURED (source) frames advance "
                    "per real second. This is the '15 that felt right' clock. "
                    "The capture is recorded at 60 fps (realtime); lower values "
                    "give slow motion (24 = 2.5x slow-mo, 15 = 4x slow-mo). "
                    "This is INDEPENDENT of Output FPS, so the render plays at "
                    "exactly the speed you tuned in the viewport. LIVE: drag it "
                    "after importing and the speed changes in place, no "
                    "re-import (and no re-linking materials) needed.",
        default=24,
        min=1,
        max=240,
        soft_max=60,
        update=_on_playback_fps_update,
    )
    output_fps: IntProperty(
        name="Output FPS (smoothness)",
        description="Scene render frame rate (scene.render.fps) — how SMOOTH the "
                    "playback/render is, NOT how fast. Higher = smoother motion "
                    "with in-between Blender frames; the animation still lasts "
                    "the same wall-clock time set by Playback Speed. Set to 60 "
                    "for smooth 60fps renders that play at the tuned speed. "
                    "LIVE: takes effect immediately, no re-import needed.",
        default=60,
        min=1,
        max=240,
        soft_max=120,
        update=_on_output_fps_update,
    )
    car_smooth_stop: BoolProperty(
        name="Smooth Car Stop",
        description="Ease the car to a full rest after the last captured frame "
                    "instead of freezing it instantly mid-pose. The car keeps "
                    "gliding along its residual motion — sliding, tilting, "
                    "settling — with velocity decaying smoothly to zero, so the "
                    "crash ends with a natural settle rather than a snap. "
                    "LIVE: toggling it takes effect immediately, no re-import.",
        default=False,
        update=_on_smooth_stop_update,
    )
    car_smooth_stop_frames: IntProperty(
        name="Stop Frames",
        description="Length of the smooth-stop tail (Blender timeline frames) "
                    "added past the onset. More frames = a longer, softer glide "
                    "to rest; 0 = instant stop (as if the feature were off). "
                    "About 30 is a gentle settle at 60 fps.",
        default=30,
        min=0,
        max=600,
        soft_max=180,
        update=_on_smooth_stop_update,
    )
    car_smooth_stop_start: IntProperty(
        name="Start at Frame",
        description="Timeline frame at which the smooth-stop settle begins "
                    "(0 = start at the end of the captured sequence, the "
                    "default). Set this to an earlier frame to cut the "
                    "remaining captured motion and ease the car to rest from "
                    "that point instead — useful when the crash has already "
                    "settled on screen but the capture kept rolling. LIVE: "
                    "no re-import needed.",
        default=0,
        min=0,
        max=50000,
        update=_on_smooth_stop_update,
    )

    # --- tyre ground-contact deformation -------------------------------
    # BeamNG's tyre mesh is rigid, so a loaded tyre never shows a contact
    # patch — it just sinks into the ground.  These fake the rubber squash at
    # playback time, entirely from the cached geometry's distance to the ground
    # plane, so a tyre lifted off the ground goes perfectly round again.
    tyre_flatten: FloatProperty(
        name="Tyre Flatten",
        description="Strength of fake tyre ground-contact flattening. "
                    "0 = off (rigid BeamNG tyres, zero cost), 1 = tyre sits "
                    "flat on the ground plane. The contact patch grows and "
                    "shrinks with the real physics load, and returns to fully "
                    "round when the wheel leaves the ground",
        default=0.0,
        min=0.0,
        max=1.0,
        step=2,
        precision=2,
        subtype="FACTOR",
        update=_on_tyre_update,
    )
    tyre_deflection: FloatProperty(
        name="Static Deflection (m)",
        description="Extra squash of the lower carcass while in contact, in "
                    "metres. This is what gives a resting tyre a visible patch "
                    "even when the hub has not sunk into the ground. Weighted "
                    "by height below the axle, so the tread flattens and the "
                    "bead near the rim stays put",
        default=0.02,
        min=0.0,
        max=0.2,
        soft_max=0.06,
        step=1,
        precision=3,
        update=_on_tyre_update,
    )
    tyre_bulge: FloatProperty(
        name="Sidewall Bulge",
        description="How much of the squashed rubber bulges out sideways along "
                    "the wheel's axle, as a fraction of the squash depth. "
                    "0 = no bulge (pure flattening)",
        default=0.6,
        min=0.0,
        max=2.0,
        soft_max=1.0,
        step=5,
        precision=2,
        update=_on_tyre_update,
    )
    tyre_release: FloatProperty(
        name="Lift-off Release (m)",
        description="Height above the ground at which flattening reaches zero. "
                    "The effect ramps off over this distance, so a wheel that "
                    "leaves the ground (car lifted, jump, rollover) smoothly "
                    "recovers its round shape instead of holding a flat spot",
        default=0.03,
        min=0.001,
        max=0.5,
        soft_max=0.1,
        step=1,
        precision=3,
        update=_on_tyre_update,
    )
    tyre_ground_z: FloatProperty(
        name="Ground Z",
        description="World Z height of the ground plane the tyres flatten "
                    "against. Leave at 0 when using the importer's auto-ground",
        default=0.0,
        min=-1000.0,
        max=1000.0,
        step=1,
        precision=3,
        update=_on_tyre_update,
    )
    tyre_names: StringProperty(
        name="Tyre Name Match",
        description="Comma-separated substrings identifying tyre objects "
                    "(case-insensitive). Only matching objects are deformed, so "
                    "rims, hubs and brakes stay rigid",
        default="tire,tyre",
        update=_on_tyre_update,
    )


class BeamNGDebrisProperties(PropertyGroup):
    """Tunables for the impact-debris feature (see runtime.debris_spawn)."""

    debris_density: FloatProperty(
        name="Debris Density",
        description="Global multiplier on how much debris every impact sheds. "
                    "1.0 keeps the per-impact amounts below; 2.0 doubles every "
                    "piece count (hero shards, fine particles and glass "
                    "fragments); 0.5 halves them. Lower it on weak machines — "
                    "the hero rigid bodies are the main cost",
        default=1.0,
        min=0.1,
        max=5.0,
        step=5,
        precision=2,
    )
    debris_scale: FloatProperty(
        name="Shard Scale",
        description="Global multiplier on the SIZE of every shattered piece — "
                    "hero shards and glass fragments alike. 1.0 keeps pieces at "
                    "real-world scale; 1.5 makes chunky, slow-falling slabs; "
                    "0.5 makes fine gravel. Does not change their launch speed",
        default=1.0,
        min=0.2,
        max=3.0,
        step=5,
        precision=2,
    )
    debris_hero_count: IntProperty(
        name="Hero Pieces",
        description="How many rigid-body shards (solid chunks broken off the "
                    "panel) each impact spawns. These are the heavy, fully "
                    "physics-simulated pieces and make up most of the visible "
                    "pile. More = richer wreckage but slower to simulate and "
                    "bake",
        default=14,
        min=0,
        max=200,
    )
    debris_max_hero: IntProperty(
        name="Max Hero Pieces (total)",
        description="Cap on the total number of rigid bodies across ALL "
                    "impacts. If a long capture hits the cap, later impacts "
                    "spawn fewer shards instead of hanging Blender. Raise it "
                    "only for short or sparse captures",
        default=240,
        min=0,
        max=2000,
    )
    debris_fine_count: IntProperty(
        name="Fine Particles",
        description="Small lightweight chips sprayed from each impact, "
                    "simulated as particles rather than rigid bodies (cheap). "
                    "They add the 'dust and splinters' cloud around the pile. "
                    "Raise for a denser cloud; the particle cost is mild",
        default=90,
        min=0,
        max=1000,
    )
    debris_speed: FloatProperty(
        name="Launch Speed",
        description="Outward launch speed (m/s) fired from the impact point. "
                    "0 (the default) means debris is only shed — it separates "
                    "and falls, carrying the panel's own momentum, landing in a "
                    "tight pile right under the impact. Raise it (e.g. 3-10) to "
                    "fire pieces outward in a cone so they land metres away — "
                    "the 'explosion' look. 20 is a violent blast that scatters "
                    "everything far",
        default=0.0,
        min=0.0,
        max=20.0,
        step=5,
        precision=2,
    )
    debris_bounciness: FloatProperty(
        name="Particles + Debris Bounciness",
        description="How much every piece bounces off the ground — hero shards, "
                    "glass fragments and fine particles alike. 0 means a piece "
                    "touches down once and stays (a dense, dead pile). 1 lets "
                    "pieces bounce and skitter several times before settling (a "
                    "lively, widely scattered pile). 0.25 is a natural "
                    "compromise",
        default=0.25,
        min=0.0,
        max=1.0,
        step=2,
        precision=2,
        subtype="FACTOR",
    )
    debris_scatter: FloatProperty(
        name="Scatter",
        description="Sideways separation speed (m/s) so shed material spreads "
                    "over a patch instead of stacking in one column. This is "
                    "NOT a launch (see Launch Speed) — it just widens the "
                    "settled pile a little. Values below about 1.0 are ignored "
                    "because a minimum launch speed always applies, so shards "
                    "never sit in a dead heap. Use ~1.5-3 to noticeably widen "
                    "the pile",
        default=0.45,
        min=0.0,
        max=5.0,
        step=5,
        precision=2,
    )
    debris_spread: FloatProperty(
        name="Spray Spread",
        description="Cone half-angle (degrees) the launched debris sprays into. "
                    "55 = a wide, natural cone. Narrow it (20-30) for a tight "
                    "directional blast; open it past 90 to throw debris "
                    "sideways and backwards as well",
        default=55.0,
        min=5.0,
        max=180.0,
        step=5,
        precision=1,
    )
    debris_min_severity: FloatProperty(
        name="Min Severity",
        description="Impacts below this severity spawn NO debris at all. "
                    "Severity runs from 0 (a tiny scrape) to 1 (a full-speed "
                    "smash). 0.12 skips grazing scrapes; raise it to stop small "
                    "impacts cluttering the scene",
        default=0.12,
        min=0.0,
        max=1.0,
        step=2,
        precision=2,
        subtype="FACTOR",
    )
    debris_min_blast_severity: FloatProperty(
        name="Min Blast Severity",
        description="Impacts BELOW this severity drop debris straight down — "
                    "no outward blast (no firework spray), just shards shedding "
                    "and falling. Impacts AT or ABOVE it fire the full launch "
                    "blast. Reference: back-landing is ~0.20-0.23, door-smash "
                    "~0.41-0.55",
        default=0.35,
        min=0.0,
        max=1.0,
        step=2,
        precision=2,
        subtype="FACTOR",
    )
    debris_variants: IntProperty(
        name="Shard Variants",
        description="How many distinct shard meshes are generated per part and "
                    "material. 16 gives good variety so the spray reads as a "
                    "statistical cloud instead of repeated identical shapes; "
                    "more = more variety but more memory. 1 makes every shard "
                    "share one shape",
        default=16,
        min=1,
        max=32,
    )
    debris_settle_frames: IntProperty(
        name="Settle Frames",
        description="Extra frames simulated past the last impact so the debris "
                    "has time to fall, bounce and settle before the animation "
                    "ends. Too few and pieces freeze mid-air at the last "
                    "impact. 260 is about 4.3 seconds at 60 fps",
        default=260,
        min=0,
        max=2000,
    )
    debris_seed: IntProperty(
        name="Random Seed",
        description="Random seed for all shard shapes, positions and "
                    "velocities. With the same seed the same scene always "
                    "rebuilds identically (deterministic rendering); change it "
                    "to get a different-looking pile",
        default=12345,
        min=0,
        max=2 ** 31 - 1,
    )
    debris_shatter_glass: BoolProperty(
        name="Shatter Glass",
        description="Break glass panes out of the car when they shatter and "
                    "spawn falling fragments. Off: glass keeps its intact pane "
                    "mesh and never sheds — no glass debris at all",
        default=True,
    )
    glass_crack_deform: FloatProperty(
        name="Crack Threshold",
        description="How much deformation (m) a glass pane needs before it "
                    "starts to craze — a web of cracks but still in the frame. "
                    "Below this the pane is untouched. 0.006 m is 6 mm, so even "
                    "small dents already show cracks",
        default=0.006,
        min=0.0,
        max=0.2,
        precision=4,
    )
    glass_shatter_deform: FloatProperty(
        name="Shatter Threshold",
        description="How much deformation (m) a pane needs before it detaches "
                    "from the car and breaks up into fragments. Must be above "
                    "the Crack Threshold. 0.022 m is 22 mm of panel "
                    "deformation",
        default=0.022,
        min=0.0,
        max=0.5,
        precision=4,
    )
    glass_shatter_ground_depth: FloatProperty(
        name="Ground Strike Depth",
        description="A pane whose lowest vertex sinks at least this far (m) "
                    "below the ground plane has struck the road face-on, so it "
                    "shatters regardless of measured deformation. Catches panes "
                    "the deformation metric never sees — e.g. a perfectly flat "
                    "landing",
        default=0.03,
        min=0.0,
        max=1.0,
        precision=4,
    )
    glass_crack_enabled: BoolProperty(
        name="Crack Panes",
        description="Decorate panes hit hard enough to craze but not hard "
                    "enough to shatter. The pane KEEPS its glass and keeps "
                    "animating — only a material is applied, fading in at the "
                    "frame the pane was struck",
        default=True,
    )
    glass_crack_use_image: BoolProperty(
        name="Use Crack Texture",
        description="Paint the damage from your own crack image instead of the "
                    "procedural crack web. The Image Texture node is added to "
                    "the pane's material even when no file is set, so you can "
                    "drop a PNG into it and position it by hand with the "
                    "Mapping node wired next to it",
        default=True,
    )
    glass_crack_image: StringProperty(
        name="Crack Texture",
        description="Crack image painted onto cracked panes. Its ALPHA is the "
                    "mask: opaque pixels show damage, clear pixels leave the "
                    "glass untouched. Leave empty to wire up the node and "
                    "assign the image yourself in the shader editor",
        default="",
        subtype="FILE_PATH",
    )
    glass_crack_span: FloatProperty(
        name="Crack Size",
        description="How wide (m) the crack texture spans across the pane, "
                    "centred on the impact point. Larger values spread the same "
                    "image over more glass",
        default=1.2,
        min=0.01,
        max=10.0,
        precision=2,
    )
    glass_crack_scale: FloatProperty(
        name="Hole Size",
        description="Size of the hole punched at the impact, scaled by how hard "
                    "the pane was hit. Only used by the PROCEDURAL crack (when "
                    "Use Crack Texture is off); the hole is clamped so it can "
                    "never exceed 60% of the pane",
        default=0.05,
        min=0.0,
        max=1.0,
        precision=3,
    )
    glass_edge_retain: FloatProperty(
        name="Edge Retain",
        description="Width of the glass fringe that stays glued in the window "
                    "frame, as a fraction of the pane's half-extent measured "
                    "inward from the outline. 0.05 keeps a thin ring around the "
                    "whole aperture; larger values keep more glass in the "
                    "frame, so the shattered hole looks smaller",
        default=0.05,
        min=0.0,
        max=0.5,
        step=2,
        precision=2,
        subtype="FACTOR",
    )


class BeamNGPhysicsProperties(PropertyGroup):
    """Physics panel settings (ported from Simply Shatter)."""

    auto_keyframe: BoolProperty(
        name="Auto Keyframe",
        description="Automatically keyframe hide/unhide for shatter effect at current frame",
        default=False,
    )
    keep_animation: BoolProperty(
        name="Keep Animation",
        description="Preserve original animation when shattering (bake world-space transforms)",
        default=False,
    )
    # Collision settings
    collision_margin: FloatProperty(
        name="Collision Margin",
        description="Margin for collision shapes",
        default=0.01,
        min=0.0,
        max=1.0,
    )
    # Boundary settings
    boundary_pin_radius: FloatProperty(
        name="Pin Radius",
        description="Radius within which constraints are created between boundaries and parts",
        default=1.5,
        min=0.1,
        max=50.0,
    )
    boundary_use_stuck: BoolProperty(
        name="Use Stuck",
        description="Use stuck constraints to keep pieces together until threshold",
        default=False,
    )
    boundary_animated: BoolProperty(
        name="Animated",
        description="Make boundaries kinematic (animated)",
        default=True,
    )
    boundary_breakable: BoolProperty(
        name="Breakable",
        description="Make boundary constraints breakable",
        default=True,
    )
    boundary_break_threshold: FloatProperty(
        name="Break Threshold",
        description="Force threshold at which constraints break",
        default=0.5,
        min=0.0,
        max=100.0,
    )
    boundary_break_randomize: FloatProperty(
        name="Randomize Breaking",
        description="Randomize breaking threshold per constraint",
        default=0.3,
        min=0.0,
        max=1.0,
    )
    # Bake settings
    baked_to_keyframes: BoolProperty(
        name="Baked To Keyframes",
        description="True after 'Bake to Keyframes' has been run",
        default=False,
    )
    # Cleanup settings
    cleanup_use_current_keyframe: BoolProperty(
        name="Use Current Frame",
        description="Use current frame as cleanup start instead of the value below",
        default=True,
    )
    cleanup_keyframe_start: IntProperty(
        name="Start Frame",
        description="Start frame to remove keyframes from (inclusive)",
        default=0,
        min=0,
    )
    cleanup_smooth_value: IntProperty(
        name="Smooth Value",
        description="Number of frames to smooth/damp movement over",
        default=30,
        min=1,
        max=180,
    )


class BEAMNG_PT_main(Panel):
    bl_label = "BeamNG"
    bl_idname = "BEAMNG_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BeamNG"

    @classmethod
    def poll(cls, context):
        return True

    def draw(self, context):
        props = context.scene.beamng
        layout = self.layout

        layout.label(text="BeamNG Cache Importer")

        box = layout.box()
        box.prop(props, "sequence_dir")
        box.prop(props, "cache_path")
        box.prop(props, "use_chunked")
        box.prop(props, "weld_cache")
        box.prop(props, "workers")
        box.prop(props, "start_frame")
        box.prop(props, "playback_fps")
        box.prop(props, "output_fps")
        box.prop(props, "car_smooth_stop")
        if props.car_smooth_stop:
            box.prop(props, "car_smooth_stop_frames")
            box.prop(props, "car_smooth_stop_start")
        # These three retune the imported cache in place (see
        # runtime.frame_handler.update_start_frame / update_fps), so say so —
        # otherwise the natural assumption is that they need a re-import.
        note = box.column(align=True)
        note.scale_y = 0.7
        note.label(text="Timing is live — no re-import needed.", icon="INFO")

        col = layout.column(align=True)
        col.operator("beamng.build_cache", text="1. Build Cache", icon="EXPORT")
        col.operator("beamng.import_cache", text="2. Import Cache", icon="IMPORT")

        col.separator()
        col.prop(props, "vehicle_dir")
        col.prop(props, "use_game_textures")
        sub = col.column(align=True)
        sub.enabled = props.use_game_textures
        sub.prop(props, "game_dir")
        col.operator("beamng.assign_textures", text="3. Assign Textures", icon="TEXTURE")

        col.separator()
        col.operator("beamng.export_alembic", text="4. Export to Alembic", icon="EXPORT")


class BEAMNG_PT_tyres(Panel):
    """Fake tyre ground-contact deformation (BeamNG tyre meshes are rigid)."""
    bl_label = "Tyre Contact"
    bl_idname = "BEAMNG_PT_tyres"
    bl_parent_id = "BEAMNG_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BeamNG"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        # Mirror the master strength in the header so it reads as on/off at a
        # glance even when the panel is collapsed.
        props = context.scene.beamng
        self.layout.label(
            text="", icon="CHECKBOX_HLT" if props.tyre_flatten > 0.0
            else "CHECKBOX_DEHLT")

    def draw(self, context):
        props = context.scene.beamng
        layout = self.layout
        layout.use_property_split = True

        layout.prop(props, "tyre_flatten")

        col = layout.column()
        col.active = props.tyre_flatten > 0.0
        col.prop(props, "tyre_deflection")
        col.prop(props, "tyre_bulge")
        col.prop(props, "tyre_release")
        col.prop(props, "tyre_ground_z")
        col.prop(props, "tyre_names")

        if props.tyre_flatten > 0.0:
            note = layout.column(align=True)
            note.scale_y = 0.7
            note.label(text="Live — updates as you drag.", icon="INFO")


class BEAMNG_PT_debris(Panel):
    """Impact debris: detect impacts, spawn hero/fine debris, bake it."""
    bl_label = "Debris"
    bl_idname = "BEAMNG_PT_debris"
    bl_parent_id = "BEAMNG_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BeamNG"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        # Only show the header icon when the scene actually has debris.
        from runtime.debris_spawn import DEBRIS_COLLECTION
        has = bpy.data.collections.get(DEBRIS_COLLECTION) is not None
        self.layout.label(
            text="", icon="CHECKBOX_HLT" if has else "CHECKBOX_DEHLT")

    def draw(self, context):
        props = context.scene.beamng_debris
        layout = self.layout
        layout.use_property_split = True

        amt = layout.box()
        amt.label(text="Amounts", icon="PARTICLE_DATA")
        amt.prop(props, "debris_density")
        amt.prop(props, "debris_hero_count")
        amt.prop(props, "debris_max_hero")
        amt.prop(props, "debris_fine_count")
        phys = layout.box()
        phys.label(text="Physics", icon="PHYSICS")
        phys.prop(props, "debris_bounciness")
        phys.prop(props, "debris_scatter")
        phys.prop(props, "debris_speed")
        phys.prop(props, "debris_spread")

        det = layout.box()
        det.label(text="When to Spawn", icon="TIME")
        det.prop(props, "debris_min_severity")
        det.prop(props, "debris_min_blast_severity")

        gen = layout.box()
        gen.label(text="Generation", icon="MOD_BUILD")
        gen.prop(props, "debris_variants")
        gen.prop(props, "debris_settle_frames")
        gen.prop(props, "debris_seed")

        glass = layout.box()
        glass.label(text="Glass", icon="SHADING_RENDERED")
        glass.prop(props, "debris_shatter_glass")
        sub = glass.column(align=True)
        sub.active = props.debris_shatter_glass
        sub.prop(props, "glass_crack_deform")
        sub.prop(props, "glass_shatter_deform")
        sub.prop(props, "glass_shatter_ground_depth")
        sub.prop(props, "glass_edge_retain")

        crack = glass.box()
        crack.active = props.debris_shatter_glass
        crack.prop(props, "glass_crack_enabled")
        ccol = crack.column(align=True)
        ccol.active = props.debris_shatter_glass and props.glass_crack_enabled
        ccol.prop(props, "glass_crack_use_image")
        if props.glass_crack_use_image:
            ccol.prop(props, "glass_crack_image")
            ccol.prop(props, "glass_crack_span")
        else:
            ccol.prop(props, "glass_crack_scale")

        col = layout.column(align=True)
        col.scale_y = 1.2
        col.operator("beamng.build_debris", text="Build Debris", icon="MOD_PARTICLES")
        col.operator("beamng.clear_debris", text="Clear Debris", icon="TRASH")


class BEAMNG_PT_physics(Panel):
    """Physics settings for debris simulation (ported from Simply Shatter)."""
    bl_label = "Physics (EXPERIMENTAL)"
    bl_idname = "BEAMNG_PT_physics"
    bl_parent_id = "BEAMNG_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BeamNG"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        props = context.scene.beamng_physics
        layout = self.layout

        # Experimental warning
        box = layout.box()
        box.label(text="EXPERIMENTAL - Ported from Simply Shatter", icon="ERROR")
        box.label(text="May not work correctly with all capture data.")

        # Apply Physics section
        box = layout.box()
        box.label(text="Apply Physics", icon="PHYSICS")
        row = box.row(align=True)
        row.scale_y = 1.3
        row.operator("beamng.apply_physics", text="Apply Physics", icon="PHYSICS")
        box.prop(props, "auto_keyframe")
        box.prop(props, "keep_animation")

        # Collision Settings
        box = layout.box()
        box.label(text="Collision Settings", icon="MOD_PHYSICS")
        box.prop(props, "collision_margin")
        row = box.row(align=True)
        row.scale_y = 1.2
        row.operator("beamng.add_colliders", text="Add Selected as Colliders", icon="ADD")

        # Boundary Settings
        box = layout.box()
        box.label(text="Boundary Settings", icon="FORCE_FORCE")
        box.prop(props, "boundary_pin_radius")
        box.prop(props, "boundary_use_stuck")
        box.prop(props, "boundary_animated")
        box.prop(props, "boundary_breakable")
        sub = box.column(align=True)
        sub.active = props.boundary_breakable
        sub.prop(props, "boundary_break_threshold")
        sub.prop(props, "boundary_break_randomize")
        row = box.row(align=True)
        row.scale_y = 1.2
        row.operator("beamng.add_boundaries", text="Add Selected as Boundaries", icon="ADD")
        row.operator("beamng.remove_boundary", text="Remove Boundaries", icon="TRASH")

        # Physics Quality
        box = layout.box()
        box.label(text="Solver Quality", icon="SETTINGS")
        row = box.row(align=True)
        row.scale_y = 1.2
        row.operator("beamng.physics_preview", text="Preview", icon="PLAY")
        row.operator("beamng.physics_final", text="Final", icon="FILE_REFRESH")

        # Bake Settings
        box = layout.box()
        box.label(text="Bake Settings", icon="REC")
        row = box.row(align=True)
        row.scale_y = 1.3
        row.operator("beamng.bake_to_keyframes", text="Bake to Keyframes", icon="REC")

        # Cleanup
        enabled_cleanup = props.baked_to_keyframes
        box = layout.box()
        box.label(text="Clean Up", icon="TRASH")
        row = box.row(align=True)
        row.enabled = enabled_cleanup
        row.prop(props, "cleanup_use_current_keyframe", text="Use Current Frame", toggle=True, icon="KEY_HLT")
        sub = row.row()
        sub.enabled = enabled_cleanup and not props.cleanup_use_current_keyframe
        sub.prop(props, "cleanup_keyframe_start", text="Start Frame")
        row = box.row(align=True)
        row.enabled = enabled_cleanup
        row.prop(props, "cleanup_smooth_value", text="Smooth Value", slider=True)
        row = box.row(align=True)
        row.enabled = enabled_cleanup
        row.operator("beamng.reduce_velocity", text="Clean Up Jiggling", icon="FORCE_HARMONIC")


_classes = [
    BeamNGSceneProperties,
    BeamNGDebrisProperties,
    BeamNGPhysicsProperties,
    BEAMNG_PT_main,
    BEAMNG_PT_tyres,
    BEAMNG_PT_debris,
    BEAMNG_PT_physics,
]


def register():
    print("[BeamNG] registering panel and properties...")
    for cls in _classes:
        bpy.utils.register_class(cls)
        print(f"[BeamNG] registered {cls.__name__}")
    bpy.types.Scene.beamng = bpy.props.PointerProperty(type=BeamNGSceneProperties)
    bpy.types.Scene.beamng_debris = bpy.props.PointerProperty(type=BeamNGDebrisProperties)
    bpy.types.Scene.beamng_physics = bpy.props.PointerProperty(type=BeamNGPhysicsProperties)
    print("[BeamNG] Scene.beamng / Scene.beamng_debris / Scene.beamng_physics properties set")


def unregister():
    print("[BeamNG] unregistering...")
    del bpy.types.Scene.beamng
    del bpy.types.Scene.beamng_debris
    del bpy.types.Scene.beamng_physics
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    print("[BeamNG] done")
