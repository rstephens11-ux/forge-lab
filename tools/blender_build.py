#!/usr/bin/env python3
"""Build a stylized character in Blender from Forge Lab's SLIDER NUMBERS.

    /Applications/Blender.app/Contents/MacOS/Blender -b -P tools/blender_build.py -- \
        --picks characters/bighead/character.json --out drafts/3d/bighead [--sheet]

WHY THIS IS THE SAME FEATURE AS THE SLIDERS
-------------------------------------------
The sliders in Forge Lab are NUMBERS. `compose_character_prompt()` turns those numbers into
words for an image model. This turns the SAME numbers into GEOMETRY. One source of truth,
two renderers.

⚠️ The tables below mirror the 7-level phrase tables in `forge_lab.py` (`CHAR_SLIDERS`). Same
levels, same meaning. Edit one and you must edit the other.

HOW THE BODY IS MADE - and why it is not primitives any more
------------------------------------------------------------
Version 1 bolted spheres and boxes together and came out looking worse than a Wii character,
because it WAS worse: separate closed solids intersecting each other, no continuous surface.
This version builds a **skeleton of verts and edges**, gives each joint a RADIUS from the
sliders, then applies a Skin modifier + Subdivision Surface. That produces ONE continuous mesh
with real limb topology - the same technique behind most procedural character generators, and
the reason a Wii-era model reads as a body rather than a pile of parts.

    arm thickness +2  ->  a bigger RADIUS on the elbow/wrist skin verts  ->  thicker arms

So a slider is not "a word we hope the image model honours". Here it is a literal number.

⚠️ BLENDER IS Z-UP. +Y is the way the character FACES.
⚠️ Face features are PROJECTED ONTO the head ellipsoid, never placed at a fixed fraction of
head_r - at 0.86R the head is still 0.51R wide and the features end up buried with a blank face.

Verified headless on Blender 5.2.1 LTS (macOS). Cycles/CPU only: no GPU, never touches forge.
"""
import json
import math
import os
import sys

import bpy
import mathutils

# ---------------------------------------------------------------------------------------
# Level tables. Index 0..6 == slider level -3..+3, exactly as in forge_lab.py.
# MULTIPLIERS, so 1.0 is deliberately level 3 (the middle, "ordinary").
# ---------------------------------------------------------------------------------------
T = {
    #                -3     -2     -1    0(1.0)    +1     +2     +3
    "Height":        (0.62, 0.72, 0.85, 1.00, 1.12, 1.26, 1.45),
    "Frame":         (0.55, 0.70, 0.85, 1.00, 1.25, 1.50, 1.90),
    "Shoulders":     (0.60, 0.75, 0.90, 1.00, 1.20, 1.45, 1.80),
    "Head size":     (0.35, 0.55, 0.80, 1.00, 1.35, 1.75, 2.40),
    "Head shape":    (0.65, 0.80, 0.90, 1.00, 1.15, 1.35, 1.70),
    "Brow":          (0.40, 0.60, 0.80, 1.00, 1.30, 1.70, 2.20),
    "Eyes":          (0.50, 0.70, 0.85, 1.00, 1.30, 1.70, 2.30),
    "Nose":          (0.30, 0.50, 0.75, 1.00, 1.40, 2.00, 3.00),
    "Mouth":         (0.45, 0.65, 0.82, 1.00, 1.25, 1.60, 2.10),
    "Ears":          (0.30, 0.50, 0.75, 1.00, 1.60, 2.40, 3.60),
    "Chin":          (0.50, 0.70, 0.85, 1.00, 1.20, 1.50, 1.90),
    "Neck":          (0.15, 0.40, 0.70, 1.00, 1.40, 1.90, 2.60),
    "Arm length":    (0.60, 0.75, 0.88, 1.00, 1.15, 1.35, 1.60),
    "Arm thickness": (0.50, 0.70, 0.85, 1.00, 1.25, 1.55, 2.00),
    "Leg length":    (0.60, 0.75, 0.88, 1.00, 1.15, 1.35, 1.60),
    "Leg thickness": (0.50, 0.70, 0.85, 1.00, 1.25, 1.55, 2.00),
}

BASE_H = 1.75


def argv_after_ddash():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def opt(name, default=None):
    a = argv_after_ddash()
    if name in a:
        i = a.index(name)
        if i + 1 < len(a):
            return a[i + 1]
    return default


def flag(name):
    return name in argv_after_ddash()


def level(picks, axis):
    try:
        return max(-3, min(3, int(picks.get(axis, 0))))
    except Exception:
        return 0


def mul(picks, axis):
    return T.get(axis, (1.0,) * 7)[level(picks, axis) + 3]


# ---------------------------------------------------------------------------------------
# materials - cached, because the comparison sheet builds the character four times
# ---------------------------------------------------------------------------------------
_MATS = {}


def mat(name, rgb, rough=0.62, spec=0.35):
    if name in _MATS:
        return _MATS[name]
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes.get("Principled BSDF")
    if b:
        b.inputs["Base Color"].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
        b.inputs["Roughness"].default_value = rough
        if "Specular IOR Level" in b.inputs:
            b.inputs["Specular IOR Level"].default_value = spec
        elif "Specular" in b.inputs:
            b.inputs["Specular"].default_value = spec
    _MATS[name] = m
    return m


SKIN = lambda: mat("skin", (0.80, 0.60, 0.47), 0.55)
CLOTH = lambda: mat("cloth", (0.20, 0.23, 0.30), 0.85, 0.15)
DARK = lambda: mat("dark", (0.07, 0.07, 0.08), 0.60)
HAIR = lambda: mat("hair", (0.13, 0.10, 0.09), 0.72)
TROUSER = lambda: mat("trouser", (0.13, 0.14, 0.17), 0.88, 0.12)


# ---------------------------------------------------------------------------------------
# mesh helpers
# ---------------------------------------------------------------------------------------
def _obj_from_mesh(name, me, m):
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    if m:
        ob.data.materials.append(m)
    return ob


def skin_mesh(name, verts, edges, radii, root, m, branch=0.5):
    """⚠️ THE QUALITY JUMP. A skeleton + per-joint radii + Skin + Subsurf gives ONE continuous
    organic mesh with real limb topology, instead of intersecting solids."""
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, edges, [])
    me.update()
    ob = _obj_from_mesh(name, me, m)
    sk = ob.modifiers.new("skin", "SKIN")
    sk.use_smooth_shade = True
    try:
        sk.branch_smoothing = branch
    except Exception:
        pass
    sv = me.skin_vertices[0].data
    for i, r in enumerate(radii):
        if i < len(sv):
            sv[i].radius = (r[0], r[1])
    if 0 <= root < len(sv):
        sv[root].use_root = True
    ss = ob.modifiers.new("subsurf", "SUBSURF")
    ss.levels = 2
    ss.render_levels = 2
    return ob


def shape(name, verts, faces, m, smooth=True):
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    ob = _obj_from_mesh(name, me, m)
    for p in me.polygons:
        p.use_smooth = smooth
    return ob


def ico(name, loc, r, m, scale=(1, 1, 1), subdiv=2, smooth=True):
    """A subdivided icosphere: rounder than a UV sphere and cheaper on the poles."""
    bpy.ops.mesh.primitive_ico_sphere_add(radius=r, location=loc, subdivisions=subdiv)
    o = bpy.context.active_object
    o.name = name
    o.scale = scale
    for p in o.data.polygons:
        p.use_smooth = smooth
    o.data.materials.append(m)
    return o


def tube(name, a, b, r, m, verts=10):
    """A capped cylinder between two points - for the neck and the limbs' accents."""
    a, b = mathutils.Vector(a), mathutils.Vector(b)
    d = b - a
    length = d.length
    if length < 1e-6:
        return None
    bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=length, vertices=verts,
                                        location=(a + b) * 0.5)
    o = bpy.context.active_object
    o.name = name
    o.rotation_mode = "QUATERNION"
    o.rotation_quaternion = d.to_track_quat("Z", "Y")
    for p in o.data.polygons:
        p.use_smooth = True
    o.data.materials.append(m)
    return o


# ---------------------------------------------------------------------------------------
# the character. Z is UP. +Y is the way the character FACES.
# ---------------------------------------------------------------------------------------
def build(picks):
    h = BASE_H * mul(picks, "Height")
    leg_l = h * 0.44 * mul(picks, "Leg length")
    leg_t = h * 0.040 * mul(picks, "Leg thickness")
    tors_h = h * 0.30
    tors_w = h * 0.165 * mul(picks, "Shoulders")
    tors_d = h * 0.105 * mul(picks, "Frame")
    neck_len = h * 0.056 * mul(picks, "Neck")
    head_r = h * 0.066 * mul(picks, "Head size")
    hshape = mul(picks, "Head shape")
    arm_l = h * 0.21 * mul(picks, "Arm length")
    arm_t = h * 0.036 * mul(picks, "Arm thickness")

    hip_z = leg_l
    chest_z = hip_z + tors_h * 0.72
    neck_base = hip_z + tors_h
    head_z = neck_base + neck_len + head_r * 0.86
    sw = tors_w * 0.52                       # shoulder half-width
    hipx = leg_t * 0.95                      # leg spacing
    ankle_z = leg_t * 0.55

    # ---- the skeleton: vertex positions and the bones between them --------------------
    # ⚠️ A bare pelvis -> chest chain produced a uniform CYLINDER for a torso, because the
    # silhouette is made of the RADII at each joint. Definition therefore needs JOINTS to carry
    # it: glute, waist (narrow), chest (wide), shoulder line. Same for taper down a limb.
    V, R, E = [], [], []

    def add(p, r):
        V.append(p)
        R.append(r if isinstance(r, tuple) else (r, r))
        return len(V) - 1

    def bone(i, j):
        E.append((i, j))

    shoulder_z = hip_z + tors_h
    hip = add((0, 0, hip_z), (tors_w * 0.64, tors_d * 0.68))
    waist = add((0, 0, hip_z + tors_h * 0.42), (tors_w * 0.46, tors_d * 0.46))
    chest = add((0, 0, hip_z + tors_h * 0.72), (tors_w * 0.66, tors_d * 0.58))
    shldr = add((0, 0, shoulder_z - h * 0.004), (tors_w * 0.54, tors_d * 0.50))
    nbase = add((0, 0, neck_base), (h * 0.044, h * 0.044))
    ntop = add((0, 0, neck_base + neck_len * 0.94), (h * 0.042, h * 0.042))
    for _a, _b in ((hip, waist), (waist, chest), (chest, shldr), (shldr, nbase), (nbase, ntop)):
        bone(_a, _b)

    for s in (-1, 1):
        # shoulder mass, then a tapering arm: shoulder -> elbow -> wrist
        sh = add((s * (tors_w * 0.50 + arm_t * 0.80), 0, shoulder_z - h * 0.006),
                 (arm_t * 1.80, arm_t * 1.75))
        el = add((s * (tors_w * 0.50 + arm_t * 0.62), h * 0.008, shoulder_z - arm_l * 0.52),
                 (arm_t * 1.00, arm_t * 1.00))
        wr = add((s * (tors_w * 0.50 + arm_t * 0.46), h * 0.014, shoulder_z - arm_l),
                 (arm_t * 0.74, arm_t * 0.74))
        bone(shldr, sh)
        bone(sh, el)
        bone(el, wr)
        # leg: glute -> knee -> ankle -> toe, tapering
        hp = add((s * hipx, 0, hip_z - h * 0.012), (leg_t * 1.48, leg_t * 1.44))
        kn = add((s * hipx, 0, ankle_z + (hip_z - ankle_z) * 0.45),
                 (leg_t * 1.00, leg_t * 1.00))
        an = add((s * hipx, h * 0.006, ankle_z), (leg_t * 0.72, leg_t * 0.72))
        toe = add((s * hipx, h * 0.074, leg_t * 0.28), (leg_t * 0.68, leg_t * 1.18))
        bone(hip, hp)
        bone(hp, kn)
        bone(kn, an)
        bone(an, toe)

    body = skin_mesh("body", V, E, R, hip, CLOTH())
    parts = [body]
    try:
        _sv = body.data.skin_vertices[0].data
        print("  radii: hip %.3f waist %.3f chest %.3f shoulder %.3f | arm %.3f/%.3f/%.3f"
              " | leg %.3f/%.3f"
              % (_sv[0].radius[0], _sv[1].radius[0], _sv[2].radius[0], _sv[3].radius[0],
                 _sv[6].radius[0], _sv[7].radius[0], _sv[8].radius[0],
                 _sv[9].radius[0], _sv[10].radius[0]), flush=True)
    except Exception as _e:
        print("  !! SKIN RADII NOT APPLIED: %r" % _e, flush=True)

    # ---- hands: mittens, the Wii idiom, and cheaper than fingers you cannot see --------
    for s in (-1, 1):
        parts.append(ico("hand", (s * (tors_w * 0.50 + arm_t * 0.46), h * 0.014,
                                  shoulder_z - arm_l - arm_t * 0.60),
                         arm_t * 1.10, SKIN(), scale=(0.92, 1.20, 1.30)))

    # ---- neck + head -----------------------------------------------------------------
    # ⚠️ No separate neck tube any more: the skin chain runs nbase -> ntop, so the neck is
    # part of the SAME continuous surface as the torso. Bolting a cylinder on was the old shape.
    parts.append(ico("head", (0, 0, head_z), head_r, SKIN(),
                     scale=(hshape, 1.0, 0.96), subdiv=3))
    parts.append(ico("hair", (0, -head_r * 0.05, head_z + head_r * 0.26), head_r * 0.99,
                     HAIR(), scale=(hshape * 1.04, 1.04, 0.74), subdiv=2))

    # ---- face: PROJECTED ONTO the ellipsoid, never at a fixed fraction of head_r -------
    SX, SY, SZ = hshape, 1.0, 0.96

    def surf_y(x, dz):
        t = 1.0 - (x / (head_r * SX)) ** 2 - (dz / (head_r * SZ)) ** 2
        return head_r * SY * math.sqrt(max(t, 0.0))

    bz = head_r * 0.42 * SZ
    parts.append(ico("brow", (0, surf_y(0, bz) * 0.90, head_z + bz),
                     head_r * 0.30, SKIN(), scale=(2.5 * SX, 0.42 * mul(picks, "Brow"), 0.55),
                     subdiv=2))
    eye_r = head_r * 0.175 * mul(picks, "Eyes")
    ez = head_r * 0.06 * SZ
    ex = head_r * 0.33 * SX
    for s in (-1, 1):
        parts.append(ico("eye", (s * ex, surf_y(ex, ez) - eye_r * 0.30, head_z + ez),
                         eye_r, DARK(), scale=(1.0, 1.35, 0.92), subdiv=2))
    ear_s = mul(picks, "Ears")
    for s in (-1, 1):
        parts.append(ico("ear", (s * (head_r * SX * 0.90 + head_r * 0.11 * ear_s),
                                 0, head_z + head_r * 0.04),
                         head_r * 0.30 * ear_s, SKIN(), scale=(0.28, 0.95, 1.20), subdiv=2))
    nose_l = head_r * 0.66 * mul(picks, "Nose")
    nz = head_r * 0.10 * SZ
    parts.append(ico("nose", (0, surf_y(0, nz) * 0.52 + nose_l * 0.44, head_z - nz),
                     nose_l * 0.44, SKIN(), scale=(0.62, 1.25, 0.72), subdiv=2))
    mw = head_r * 0.44 * mul(picks, "Mouth")
    mz = head_r * 0.40 * SZ
    parts.append(ico("mouth", (0, surf_y(0, mz) * 0.94, head_z - mz),
                     head_r * 0.13, DARK(), scale=(mw / (head_r * 0.13), 0.85, 0.55), subdiv=2))
    chin = head_r * 0.32 * mul(picks, "Chin")
    cz = head_r * 0.72 * SZ
    parts.append(ico("chin", (0, surf_y(0, cz) * 0.66, head_z - cz),
                     head_r * 0.30, SKIN(), scale=(SX * 1.05, chin / (head_r * 0.30), 0.78),
                     subdiv=2))

    total_h = head_z + head_r * 0.96
    return parts, {"height_m": round(total_h, 3), "head_radius_m": round(head_r, 4),
                   "head_diameter_m": round(head_r * 2, 3),
                   "head_vs_body": round((head_r * 2) / max(total_h, 0.01), 3),
                   "skin_verts": len(V)}


def body_profile(body, bands=12):
    """Width (X) and depth (Y) of the EVALUATED body mesh, banded by height.

    The point: "the torso is a uniform cylinder" and "the limbs do not taper" are claims about
    SHAPE. Measure them instead of arguing about a render.
    """
    dg = bpy.context.evaluated_depsgraph_get()
    ev = body.evaluated_get(dg)
    me = ev.to_mesh()
    co = [tuple(v.co) for v in me.vertices]
    ev.to_mesh_clear()
    if not co:
        return []
    z0 = min(c[2] for c in co)
    z1 = max(c[2] for c in co)
    out = []
    for b in range(bands):
        lo = z0 + (z1 - z0) * b / bands
        hi = z0 + (z1 - z0) * (b + 1) / bands
        sel = [c for c in co if lo <= c[2] < hi]
        if sel:
            out.append((lo, hi, 2 * max(abs(c[0]) for c in sel),
                        2 * max(abs(c[1]) for c in sel)))
        else:
            out.append((lo, hi, 0.0, 0.0))
    return out


def move_group(objs, dx, ang):
    """Offset a built character and spin it about the world Z axis, about its own origin."""
    ca, sa = math.cos(ang), math.sin(ang)
    for o in objs:
        x, y, z = o.location
        o.location = (x * ca - y * sa + dx, x * sa + y * ca, z)
        o.rotation_euler.z += ang          # XYZ euler: Z applies last, so this is world-Z


def scene_bbox(objs):
    xs, ys, zs = [], [], []
    for o in objs:
        if o.type != "MESH":
            continue
        for ax, acc in ((0, xs), (1, ys), (2, zs)):
            half = o.dimensions[ax] * 0.5
            acc.extend([o.location[ax] - half, o.location[ax] + half])
    return (min(xs), max(xs)), (min(ys), max(ys)), (min(zs), max(zs))


def fit_distance(width, height, lens=70.0, sensor=36.0, rx=760, ry=760, margin=1.10):
    """Camera distance that FITS a w x h subject, derived from the lens and the render aspect.
    Guessing a multiple of the height is what cropped the top of the head off."""
    if rx >= ry:
        tan_h = (sensor * 0.5) / lens
        tan_v = tan_h * (float(ry) / rx)
    else:
        tan_v = (sensor * 0.5) / lens
        tan_h = tan_v * (float(rx) / ry)
    return max(width * 0.5 / tan_h, height * 0.5 / tan_v) * margin


def look_at(obj, target):
    """An explicit look-at. A TRACK_TO constraint silently did nothing in background renders."""
    d = mathutils.Vector(target) - obj.location
    obj.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()


def studio(centre_z, width):
    scene = bpy.context.scene
    world = bpy.data.worlds.new("W")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.55, 0.56, 0.60, 1.0)
        bg.inputs[1].default_value = 1.0
    for pos, energy, size in (((width * 0.9, width * 1.2, centre_z * 1.75), 480, width * 1.1),
                              ((-width * 1.1, width * 0.55, centre_z * 1.35), 170, width * 1.4),
                              ((0.0, -width * 1.3, centre_z * 1.55), 240, width * 1.0)):
        bpy.ops.object.light_add(type="AREA", location=pos)
        lt = bpy.context.active_object
        lt.data.energy = energy
        lt.data.size = size
        look_at(lt, (0, 0, centre_z))
    return scene


def setup_render(res_x, res_y, samples):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = samples
    sc.cycles.use_denoising = True
    sc.render.resolution_x = res_x
    sc.render.resolution_y = res_y
    sc.render.image_settings.file_format = "PNG"
    sc.render.film_transparent = False
    # ⚠️ Blender 4+ defaults to AgX, which desaturates hard - the character came back
    # looking like one flat white mannequin. Standard keeps the material colours honest.
    try:
        sc.view_settings.view_transform = "Standard"
        sc.view_settings.look = "None"
    except Exception:
        pass
    return sc


def shoot(scene, cam, aim, path, box):
    look_at(cam, aim)
    bpy.context.view_layer.update()
    from bpy_extras.object_utils import world_to_camera_view
    (bx0, bx1), (by0, by1), (bz0, bz1) = box
    ndc = [world_to_camera_view(scene, cam, mathutils.Vector((x, y, z)))
           for x in (bx0, bx1) for y in (by0, by1) for z in (bz0, bz1)]
    nx0, nx1 = min(q.x for q in ndc), max(q.x for q in ndc)
    ny0, ny1 = min(q.y for q in ndc), max(q.y for q in ndc)
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    return (nx0, nx1, ny0, ny1)


def main():
    picks_path = opt("--picks")
    out = opt("--out", "/tmp/blender_char")
    res = int(opt("--res", "760"))
    samples = int(opt("--samples", "64"))
    if not picks_path or not os.path.exists(picks_path):
        print("BLENDER_BUILD FAIL: --picks %r not found" % picks_path, file=sys.stderr)
        sys.exit(2)
    with open(picks_path) as f:
        doc = json.load(f)
    picks = doc.get("picks", doc) if isinstance(doc, dict) else {}

    bpy.ops.wm.read_factory_settings(use_empty=True)
    _MATS.clear()

    parts, stats = build(picks)
    meshes = [o for o in parts if o.type == "MESH"]
    faces = sum(len(o.data.polygons) for o in meshes)
    bpy.context.view_layer.update()
    (x0, x1), (y0, y1), (z0, z1) = scene_bbox(meshes)
    z_mid, y_mid = (z0 + z1) * 0.5, (y0 + y1) * 0.5
    span = max(x1 - x0, z1 - z0)

    scene = studio(z_mid, span)
    setup_render(res, res, samples)
    turn_d = fit_distance(max(x1 - x0, y1 - y0), z1 - z0, rx=res, ry=res, margin=1.10)
    bpy.ops.object.camera_add(location=(0, turn_d, z_mid))
    cam = bpy.context.active_object
    cam.data.lens = 70
    scene.camera = cam

    names = ["front", "right", "back", "left"]
    written = []
    for i, nm in enumerate(names):
        a = (math.pi / 2) * i
        cam.location = (turn_d * math.sin(a), turn_d * math.cos(a),
                        z_mid + (z1 - z0) * 0.22)
        r = shoot(scene, cam, (0.0, y_mid, z_mid), "%s_%s.png" % (out, nm),
                  ((x0, x1), (y0, y1), (z0, z1)))
        print("  frame %-5s x %.3f..%.3f c %.3f | y %.3f..%.3f c %.3f"
              % (nm, r[0], r[1], (r[0] + r[1]) * 0.5, r[2], r[3], (r[2] + r[3]) * 0.5),
              flush=True)
        written.append(scene.render.filepath)

    # ⚠️ MUST run BEFORE the sheet rebuild: that calls read_factory_settings, which throws
    # the first build away, and this then measures a dead object and prints an empty table.
    try:
        prof = body_profile(parts[0])
    except Exception as e:
        prof = []
        print("  !! profile failed: %r" % e, flush=True)
    if prof:
        print("  body silhouette, bottom to top (width / depth):", flush=True)
        for lo, hi, w, d in prof:
            print("     z %.2f-%.2f   %5.2f m wide  %5.2f m deep   %s"
                  % (lo, hi, w, d, "#" * max(1, int(round(w * 40)))), flush=True)
    else:
        print("  !! silhouette table EMPTY - measurement did not run", flush=True)

    # ---- the comparison sheet: the SAME character four times, one image -------------
    if flag("--sheet"):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        _MATS.clear()
        groups = []
        gap = stats["height_m"] * 0.62
        for i in range(4):
            p, _ = build(picks)
            move_group(p, (i - 1.5) * gap, (math.pi / 2) * i)
            groups += [o for o in p if o.type == "MESH"]
        bpy.context.view_layer.update()
        (sx0, sx1), (sy0, sy1), (sz0, sz1) = scene_bbox(groups)
        sz_mid = (sz0 + sz1) * 0.5
        wide = (sx1 - sx0)
        scene = studio(sz_mid, wide * 0.55)
        setup_render(int(res * 2.4), int(res * 1.05), samples)
        sheet_d = fit_distance(wide, sz1 - sz0, rx=int(res * 2.4), ry=int(res * 1.05),
                               margin=1.08)
        bpy.ops.object.camera_add(location=(0, sheet_d, sz_mid))
        cam = bpy.context.active_object
        cam.data.lens = 70
        scene.camera = cam
        r = shoot(scene, cam, (0.0, 0.0, sz_mid), "%s_sheet.png" % out,
                  ((sx0, sx1), (sy0, sy1), (sz0, sz1)))
        print("  sheet      x %.3f..%.3f | y %.3f..%.3f" % r, flush=True)
        written.append(scene.render.filepath)

    print("BLENDER_BUILD OK")
    print("  objects : %d  faces: %d  skeleton verts: %d"
          % (len(meshes), faces, stats["skin_verts"]))
    print("  height  : %s m" % stats["height_m"])
    print("  head    : %s m across  (%.1f%% of height)"
          % (stats["head_diameter_m"], 100.0 * stats["head_vs_body"]))
    print("  written : %s" % ", ".join(written))


if __name__ == "__main__":
    main()
