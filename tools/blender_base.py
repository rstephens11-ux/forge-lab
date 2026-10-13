#!/usr/bin/env python3
"""Build a REAL character in Blender from Forge Lab's slider numbers.

    /Applications/Blender.app/Contents/MacOS/Blender -b -P tools/blender_base.py -- \
        --picks characters/bighead/character.json --out drafts/3d/base_bighead [--sheet]

WHY THIS REPLACED THE SCRIPTED BODY
-----------------------------------
v1/v2 built the character out of primitives (spheres, boxes, then a Skin-modifier skeleton).
Ryan's verdict: "this looks terrible still ... i want an xbox level character in blender".
He was right and the reason is structural - sculpted anatomy, edge-loop topology and surface
detail do not come out of primitive_uv_sphere_add, at ANY effort level.

⚠️ Higgsfield and Astra did not MODELLED their characters either - they GENERATE them. The
honest equivalent here is to stop modelling and start with professional geometry.

WHAT THIS USES
--------------
Blender Studio's **Human Base Meshes** (CC0, 49 MB, requires Blender 4.2+):
    ~/forge-lab-assets/hbm/human-base-meshes-bundle-v1.4.1/human_base_meshes_bundle.blend

  GEO-body_male_realistic          10,582 verts  quad topology, real anatomy
  GEO-head_animation_realistic      3,242 verts  riggable head
  GEO-head_sculpting_realistic      3,307 verts  high-detail sculpt head

That is Xbox-level base geometry, free and CC0, and it renders in seconds on the CPU.

⚠️ NO ARMATURE and NO vertex groups in the bundle, so joints are DERIVED from the geometry.
The mesh is in an A-pose: arms angled down and out, hands at ~40-50% of height. Measure, never
assume - a wrong joint puts the deformation on the wrong body part and still renders fine.
"""
import json
import math
import os
import sys

import bpy
import mathutils

BUNDLE = os.path.expanduser(
    "~/forge-lab-assets/hbm/human-base-meshes-bundle-v1.4.1/human_base_meshes_bundle.blend")
BODY = "GEO-body_male_realistic"
HEAD = "GEO-head_animation_realistic"

T = {
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


def argv_after_ddash():
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def opt(name, default=None):
    a = argv_after_ddash()
    if name in a and a.index(name) + 1 < len(a):
        return a[a.index(name) + 1]
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
# influences
# ---------------------------------------------------------------------------------------
def w_sphere(co, c, R):
    d = (co - c).length
    return 0.0 if d >= R else (1.0 - d / R) ** 2


def _seg_t(co, a, b):
    ab = b - a
    L2 = ab.length_squared
    if L2 < 1e-12:
        return 0.0, a
    t = max(0.0, min(1.0, (co - a).dot(ab) / L2))
    return t, a + ab * t


def w_capsule(co, a, b, R):
    _t, cp = _seg_t(co, a, b)
    d = (co - cp).length
    return 0.0 if d >= R else (1.0 - d / R) ** 2


def deform(obj, weight, target, tag=""):
    """Blend each vertex toward target(co) by weight(co). Returns how many moved.

    ⚠️ Returns the count so a silent no-op is impossible: a deformation that moves 0 vertices
    still renders perfectly and looks like the slider simply does nothing.
    """
    n = 0
    for v in obj.data.vertices:
        w = weight(v.co)
        if w > 1e-6:
            v.co = v.co.lerp(target(v.co), w)
            n += 1
    print("    %-26s moved %5d verts" % (tag or "deform", n), flush=True)
    return n


def scale_about(pivot, f, axes=(1, 1, 1)):
    def t(co):
        d = co - pivot
        return pivot + mathutils.Vector((d.x * f * axes[0], d.y * f * axes[1],
                                         d.z * f * axes[2]))
    return t


def radial_axis(a, b, f, along=1.0):
    """Scale RADIALLY about the segment a-b (thickness), leaving the axis length alone."""
    def t(co):
        _t, cp = _seg_t(co, a, b)
        return cp + (co - cp) * f
    return t


# ---------------------------------------------------------------------------------------
# measure the base mesh: joints derived from geometry, never assumed
# ---------------------------------------------------------------------------------------
def measure_body(obj):
    co = [v.co for v in obj.data.vertices]
    z0 = min(c.z for c in co)
    z1 = max(c.z for c in co)
    H = z1 - z0

    def band(flo, fhi):
        return [c for c in co if z0 + H * flo <= c.z < z0 + H * fhi]

    # the torso is |x| < 0.13H; arms live outside that. Sample the arm axis at two heights.
    def arm_x(flo, fhi):
        s = [c for c in band(flo, fhi) if abs(c.x) > 0.13 * H]
        if not s:
            return 0.0
        return max(abs(c.x) for c in s)

    def torso_half(flo, fhi):
        s = [c for c in band(flo, fhi) if abs(c.x) <= 0.13 * H]
        return max((abs(c.x) for c in s), default=0.0)

    J = {
        "H": H, "z0": z0, "z1": z1, "x_center": 0.0,
        "shoulder_z": z0 + H * 0.80, "shoulder_x": max(torso_half(0.76, 0.84), 0.09 * H),
        "arm_hi_z": z0 + H * 0.74, "arm_hi_x": arm_x(0.72, 0.78),
        "arm_lo_z": z0 + H * 0.50, "arm_lo_x": arm_x(0.48, 0.54),
        "hand_z": z0 + H * 0.46, "hand_x": arm_x(0.42, 0.50),
        "hip_z": z0 + H * 0.53, "hip_x": torso_half(0.50, 0.56) * 0.62,
        "knee_z": z0 + H * 0.28, "knee_x": torso_half(0.26, 0.30) * 0.72,
        "ankle_z": z0 + H * 0.05, "ankle_x": torso_half(0.04, 0.08) * 0.80,
        "waist_z": z0 + H * 0.60, "waist_x": torso_half(0.58, 0.62),
        "torso_d": max((abs(c.y) for c in band(0.60, 0.75)), default=0.10 * H),
    }
    return J


def measure_head(obj):
    co = [v.co for v in obj.data.vertices]
    xs = [c.x for c in co]
    ys = [c.y for c in co]
    zs = [c.z for c in co]
    # the nose sticks out: the side of the head with the greater |y| extent is the FRONT
    front = 1.0 if abs(max(ys)) >= abs(min(ys)) else -1.0
    return {"x": (min(xs), max(xs)), "y": (min(ys), max(ys)), "z": (min(zs), max(zs)),
            "w": max(xs) - min(xs), "h": max(zs) - min(zs), "d": max(ys) - min(ys),
            "front": front, "center": mathutils.Vector(((min(xs) + max(xs)) / 2,
                                                        (min(ys) + max(ys)) / 2,
                                                        (min(zs) + max(zs)) / 2))}


# ---------------------------------------------------------------------------------------
# the build
# ---------------------------------------------------------------------------------------
def build(picks):
    bpy.ops.wm.open_mainfile(filepath=BUNDLE)
    body = bpy.data.objects.get(BODY)
    if body is None:
        print("BLENDER_BASE FAIL: %s not in the bundle" % BODY, file=sys.stderr)
        sys.exit(2)

    # ⚠️ THE BASE BODY ALREADY HAS A COMPLETE HEAD. Bolting the separate head mesh on top
    # gave the figure TWO heads - its own, plus a detached one floating above it - and the
    # floating one was the object the sliders drove, so "Head size" looked inert on the real
    # figure. The head is a REGION of the body mesh; there is no second object.
    for o in list(bpy.data.objects):
        if o is not body:
            bpy.data.objects.remove(o, do_unlink=True)
    head = body

    # ⚠️ THREE coordinate traps, all of which render perfectly while being wrong.
    #  1. The bundle's objects carry a big non-zero location (the body sits at x = -2.264), so
    #     v.co is LOCAL while every joint derived below is WORLD. Deforming with a world pivot
    #     against local coords moves ZERO vertices and looks exactly like a slider doing nothing.
    #  2. Measuring joints BEFORE moving the figure to the origin gives joint x values in the
    #     bundle's own offset frame, so every pivot lands in empty space.
    #  3. Centring only Z leaves X offset, and the subject renders half out of frame.
    # Order matters: apply transforms -> move body to origin -> THEN measure -> deform.
    bpy.ops.object.select_all(action="DESELECT")
    for o in (body, head):
        o.select_set(True)
    bpy.context.view_layer.objects.active = body
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    def vbox(objs):
        x0 = y0 = z0 = 1e9
        x1 = y1 = z1 = -1e9
        for o in objs:
            for v in o.data.vertices:
                x0 = min(x0, v.co.x); x1 = max(x1, v.co.x)
                y0 = min(y0, v.co.y); y1 = max(y1, v.co.y)
                z0 = min(z0, v.co.z); z1 = max(z1, v.co.z)
        return (x0, x1), (y0, y1), (z0, z1)

    def shift(objs, d):
        for o in objs:
            for v in o.data.vertices:
                v.co = v.co + d

    # ---- body to the origin, feet on the ground --------------------------------------
    (bx0, bx1), (by0, by1), (bz0, bz1) = vbox([body])
    shift([body], mathutils.Vector((-(bx0 + bx1) / 2, -(by0 + by1) / 2, -bz0)))
    print("  body moved to origin (was x %.3f..%.3f)" % (bx0, bx1), flush=True)

    # ---- ONLY NOW measure the joints -------------------------------------------------
    J = measure_body(body)
    print("  joints: H=%.3f  shoulder z=%.3f x=%.3f | hip z=%.3f x=%.3f | hand z=%.3f x=%.3f"
          % (J["H"], J["shoulder_z"], J["shoulder_x"], J["hip_z"], J["hip_x"],
             J["hand_z"], J["hand_x"]), flush=True)
    if J["hand_x"] > J["H"] * 0.45:
        print("  !! hand_x %.3f implausible for H=%.3f - joints are in a bogus frame"
              % (J["hand_x"], J["H"]), flush=True)

    # ---- the HEAD REGION: measured from the mesh, not a second object -----------------
    z_head = J["z0"] + J["H"] * 0.855
    hv = [v.co.copy() for v in body.data.vertices if v.co.z >= z_head]
    hx0 = min(c.x for c in hv); hx1 = max(c.x for c in hv)
    hy0 = min(c.y for c in hv); hy1 = max(c.y for c in hv)
    hz0 = min(c.z for c in hv); hz1 = max(c.z for c in hv)
    HR = (hz1 - hz0) * 0.5
    HEADP = mathutils.Vector(((hx0 + hx1) / 2, (hy0 + hy1) / 2, (hz0 + hz1) / 2))
    front = 1.0 if abs(hy1) >= abs(hy0) else -1.0
    NECKB = mathutils.Vector((HEADP.x, HEADP.y, hz0 - HR * 0.55))
    print("  head region: %d verts, centre z=%.3f R=%.3f, front %+.0fY, %.3f wide %.3f tall"
          % (len(hv), HEADP.z, HR, front, hx1 - hx0, hz1 - hz0), flush=True)

    H = J["H"]
    for o in (body,):
        bpy.context.view_layer.objects.active = o

    # ---- BODY, driven by the sliders ---------------------------------------------------
    # shoulders: widen the yoke, not the whole torso
    sf = mul(picks, "Shoulders")
    if abs(sf - 1.0) > 1e-3:
        c = mathutils.Vector((0, 0, J["shoulder_z"]))
        deform(body, lambda co, c=c: w_sphere(co, c, H * 0.20),
               lambda co, c=c, sf=sf: scale_about(c, 1.0, (sf, 1.0, 1.0))(co), "shoulders")
    # frame: depth of the torso
    ff = mul(picks, "Frame")
    if abs(ff - 1.0) > 1e-3:
        c = mathutils.Vector((0, 0, J["waist_z"]))
        deform(body, lambda co, c=c: w_sphere(co, c, H * 0.22),
               lambda co, c=c, ff=ff: scale_about(c, 1.0, (ff, ff, 1.0))(co), "frame")

    for s in (-1, 1):
        # arms: thickness radially about the arm axis, length along it
        sh = mathutils.Vector((s * J["shoulder_x"], 0, J["shoulder_z"]))
        hd = mathutils.Vector((s * J["hand_x"], 0, J["hand_z"]))
        at = mul(picks, "Arm thickness")
        if abs(at - 1.0) > 1e-3:
            deform(body, lambda co, a=sh, b=hd: w_capsule(co, a, b, H * 0.10),
                   radial_axis(sh, hd, at), "arm thickness")
        al = mul(picks, "Arm length")
        if abs(al - 1.0) > 1e-3:
            deform(body, lambda co, a=sh, b=hd: w_capsule(co, a, b, H * 0.16),
                   lambda co, a=sh, b=hd, al=al: a + (co - a) * 1.0
                   if False else _stretch(co, a, b, al), "arm length")
        # legs
        hp = mathutils.Vector((s * J["hip_x"], 0, J["hip_z"]))
        an = mathutils.Vector((s * J["ankle_x"], 0, J["ankle_z"]))
        lt = mul(picks, "Leg thickness")
        if abs(lt - 1.0) > 1e-3:
            deform(body, lambda co, a=hp, b=an: w_capsule(co, a, b, H * 0.11),
                   radial_axis(hp, an, lt), "leg thickness")
        ll = mul(picks, "Leg length")
        if abs(ll - 1.0) > 1e-3:
            deform(body, lambda co, a=hp, b=an: w_capsule(co, a, b, H * 0.20),
                   lambda co, a=hp, b=an, ll=ll: _stretch(co, a, b, ll), "leg length")

    # ---- HEAD, driven by the sliders: scaled about the NECK BASE so it grows upward -----
    hs = mul(picks, "Head size")
    hshape = mul(picks, "Head shape")
    if abs(hs - 1.0) > 1e-3 or abs(hshape - 1.0) > 1e-3:
        deform(body, lambda co, c=HEADP: w_sphere(co, c, HR * 2.4),
               lambda co, c=NECKB, hs=hs, hshape=hshape:
               scale_about(c, 1.0, (hs * hshape, hs, hs))(co), "head size/shape")

    nz = mul(picks, "Neck")
    if abs(nz - 1.0) > 1e-3:
        deform(body, lambda co, c=HEADP: w_sphere(co, c, HR * 2.4),
               lambda co, c=NECKB, nz=nz: c + (co - c) * mathutils.Vector((1, 1, nz)),
               "neck length")

    # facial regions, expressed as fractions of the HEAD so they survive a head-size change
    hw, hh = (hx1 - hx0) * hs, (hz1 - hz0) * hs
    feats = (("Ears", (0.50, 0.0, 0.0), 0.42, "ears"),
             ("Nose", (0.0, 0.46, -0.06), 0.26, "nose"),
             ("Brow", (0.0, 0.34, 0.20), 0.34, "brow"),
             ("Mouth", (0.0, 0.36, -0.34), 0.24, "mouth"),
             ("Chin", (0.0, 0.26, -0.56), 0.28, "chin"),
             ("Eyes", (0.22, 0.40, 0.30), 0.26, "eyes"))
    for axis, off, rf, tag in feats:
        m = mul(picks, axis)
        if abs(m - 1.0) < 1e-3:
            continue
        ox, oy, oz = off
        if tag in ("ears", "eyes"):
            for s in (-1, 1):
                c = mathutils.Vector(((s * abs(ox) if ox else 0.5) * hw * hshape,
                                      front * oy * hw, HEADP.z + oz * hh))
                deform(body, lambda co, c=c, R=rf * hw: w_sphere(co, c, R),
                       scale_about(c, m), "%s %+d" % (tag, s))
        else:
            c = mathutils.Vector((0.0, front * oy * hw,
                                  HEADP.z - HR + HR * (1.0 + oz * 2.0) * 0.5 + oz * hh * 0.5))
            deform(body, lambda co, c=c, R=rf * hw: w_sphere(co, c, R),
                   scale_about(c, m), tag)

    # ---- height: scale the WHOLE figure about the ground -------------------------------
    hf = mul(picks, "Height")
    if abs(hf - 1.0) > 1e-3:
        for o in (body, head):
            o.scale = (o.scale.x * hf, o.scale.y * hf, o.scale.z * hf)

    return body, head, J


def _stretch(co, a, b, f):
    """Scale a point's distance from `a` ALONG the a->b axis, leaving the perpendicular alone."""
    d = b - a
    if d.length < 1e-9:
        return co
    n = d.normalized()
    rel = co - a
    along = rel.dot(n)
    return co + n * (along * (f - 1.0))


# ---------------------------------------------------------------------------------------
# materials by REGION - the bundle is bare anatomy, so clothes are assigned per face
# ---------------------------------------------------------------------------------------
def dress(body, head, J):
    def mk(name, rgb, rough):
        m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
        m.use_nodes = True
        b = m.node_tree.nodes.get("Principled BSDF")
        if b:
            b.inputs["Base Color"].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
            b.inputs["Roughness"].default_value = rough
        return m

    skin = mk("skin", (0.86, 0.63, 0.49), 0.52)
    shirt = mk("shirt", (0.20, 0.26, 0.38), 0.80)
    trouser = mk("trouser", (0.13, 0.14, 0.17), 0.85)
    shoe = mk("shoe", (0.05, 0.05, 0.06), 0.55)

    head.data.materials.clear()
    head.data.materials.append(skin)

    body.data.materials.clear()
    for m in (skin, shirt, trouser, shoe):
        body.data.materials.append(m)
    H, z0 = J["H"], J["z0"]
    waist = z0 + H * 0.56
    for p in body.data.polygons:
        c = p.center
        if c.z < z0 + H * 0.052:
            p.material_index = 3                      # shoes
        elif c.z > z0 + H * 0.845:
            p.material_index = 0                      # head, face, neck -> skin
        elif abs(c.x) > 0.185 * H and c.z < z0 + H * 0.58:
            p.material_index = 0                      # hands only (below the sleeve)
        elif c.z >= waist:
            p.material_index = 1                      # shirt, full sleeves
        else:
            p.material_index = 2                      # trousers
    return skin, shirt, trouser, shoe


# ---------------------------------------------------------------------------------------
# studio + render
# ---------------------------------------------------------------------------------------
def look_at(obj, target):
    d = mathutils.Vector(target) - obj.location
    obj.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()


def scene_bbox(objs):
    """REAL geometry extents, from the vertices through matrix_world.

    ⚠️ NOT o.location +/- o.dimensions. Those describe the object's ORIGIN, and this pipeline
    moves VERTICES rather than origins - so the origin-based box reported a figure centred on
    z=0 while the real body spanned 0..2.01. The camera then aimed at the FEET and framed the
    shins, which is why the character rendered "headless" while every number looked sane.
    """
    bpy.context.view_layer.update()
    xs, ys, zs = [], [], []
    for o in objs:
        mw = o.matrix_world
        for v in o.data.vertices:
            w = mw @ v.co
            xs.append(w.x)
            ys.append(w.y)
            zs.append(w.z)
    return (min(xs), max(xs)), (min(ys), max(ys)), (min(zs), max(zs))


def fit_distance(w, h, lens=85.0, sensor=36.0, rx=760, ry=760, margin=1.08):
    if rx >= ry:
        th = (sensor * 0.5) / lens
        tv = th * (float(ry) / rx)
    else:
        tv = (sensor * 0.5) / lens
        th = tv * (float(rx) / ry)
    return max(w * 0.5 / th, h * 0.5 / tv) * margin


def studio(cz, width):
    sc = bpy.context.scene
    w = bpy.data.worlds.new("W")
    sc.world = w
    w.use_nodes = True
    bgn = w.node_tree.nodes.get("Background")
    if bgn:
        bgn.inputs[0].default_value = (0.42, 0.44, 0.48, 1.0)
        bgn.inputs[1].default_value = 0.9
    for pos, en, size in (((width * 0.8, width * 1.1, cz * 1.9), 120, width * 1.1),
                          ((-width * 1.0, width * 0.5, cz * 1.5), 55, width * 1.4),
                          ((0, -width * 1.2, cz * 1.7), 70, width * 0.9)):
        bpy.ops.object.light_add(type="AREA", location=pos)
        lt = bpy.context.active_object
        lt.data.energy = en
        lt.data.size = size
        look_at(lt, (0, 0, cz))
    return sc


def setup_render(rx, ry, samples):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = samples
    sc.cycles.use_denoising = True
    sc.render.resolution_x = rx
    sc.render.resolution_y = ry
    sc.render.image_settings.file_format = "PNG"
    try:
        # ⚠️ Filmic desaturated the skin to a pale blue-grey and the character read as a
        # mannequin. Standard keeps the material colours honest.
        sc.view_settings.view_transform = "Standard"
        sc.view_settings.look = "None"
        # Standard does not roll off highlights, so the face blew out and read as featureless.
        sc.view_settings.exposure = -0.55
    except Exception:
        pass
    return sc


def shoot(sc, cam, aim, path, box):
    look_at(cam, aim)
    bpy.context.view_layer.update()
    from bpy_extras.object_utils import world_to_camera_view
    (x0, x1), (y0, y1), (z0, z1) = box
    ndc = [world_to_camera_view(sc, cam, mathutils.Vector((x, y, z)))
           for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    nx0, nx1 = min(q.x for q in ndc), max(q.x for q in ndc)
    ny0, ny1 = min(q.y for q in ndc), max(q.y for q in ndc)
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)
    return nx0, nx1, ny0, ny1


def main():
    picks_path = opt("--picks")
    out = opt("--out", "/tmp/base_char")
    res = int(opt("--res", "820"))
    samples = int(opt("--samples", "80"))
    if not picks_path or not os.path.exists(picks_path):
        print("BLENDER_BASE FAIL: --picks %r not found" % picks_path, file=sys.stderr)
        sys.exit(2)
    if not os.path.exists(BUNDLE):
        print("BLENDER_BASE FAIL: base mesh bundle missing at %s" % BUNDLE, file=sys.stderr)
        sys.exit(2)
    with open(picks_path) as f:
        doc = json.load(f)
    picks = doc.get("picks", doc) if isinstance(doc, dict) else {}

    body, head, J = build(picks)
    dress(body, head, J)
    objs = [body]   # one mesh: the head is part of it
    box = scene_bbox(objs)
    (x0, x1), (y0, y1), (z0, z1) = box
    z_mid, y_mid = (z0 + z1) * 0.5, (y0 + y1) * 0.5
    cz = z0 + (z1 - z0) * 0.55

    # ⚠️ Is the head actually RENDERABLE? An object can exist in bpy.data, be deformed
    # correctly, sit at the right height - and still never appear, if it is hidden or lives in a
    # collection that is not linked to the scene. That renders as a headless figure.
    for _o in (body, head):
        print("  %-28s hide_render=%s hide_viewport=%s in_view_layer=%s colls=%s"
              % (_o.name, _o.hide_render, _o.hide_viewport,
                 _o.name in bpy.context.view_layer.objects,
                 [c.name for c in _o.users_collection]), flush=True)
    print("  combined bbox z %.3f..%.3f  x %.3f..%.3f"
          % (z0, z1, x0, x1), flush=True)

    sc = studio(cz, (z1 - z0))
    setup_render(res, res, samples)
    turn_d = fit_distance(max(x1 - x0, y1 - y0), z1 - z0, rx=res, ry=res, margin=1.06)
    bpy.ops.object.camera_add(location=(0, turn_d, cz))
    cam = bpy.context.active_object
    cam.data.lens = 85
    sc.camera = cam

    names = ["front", "right", "back", "left"]
    written = []
    for i, nm in enumerate(names):
        a = (math.pi / 2) * i
        # ⚠️ The base body faces -Y (measured: front = -1Y). A camera at +Y for a=0 shoots
        # his BACK, which is exactly what happened - every "front" render was a rear view.
        cam.location = (turn_d * math.sin(a), -turn_d * math.cos(a), cz)
        r = shoot(sc, cam, (0.0, y_mid, cz), "%s_%s.png" % (out, nm), box)
        print("  frame %-5s x %.3f..%.3f c %.3f | y %.3f..%.3f c %.3f"
              % (nm, r[0], r[1], (r[0] + r[1]) * .5, r[2], r[3], (r[2] + r[3]) * .5), flush=True)
        written.append(sc.render.filepath)

    if flag("--sheet"):
        # the SAME built character, copied four times, one per quarter turn
        bpy.ops.object.select_all(action="DESELECT")
        for o in objs:
            o.select_set(True)
        bpy.context.view_layer.objects.active = body
        group = [o for o in objs]
        gap = (z1 - z0) * 0.62
        made = []
        for i in range(4):
            bpy.ops.object.select_all(action="DESELECT")
            for o in objs:
                o.select_set(True)
            bpy.context.view_layer.objects.active = body
            bpy.ops.object.duplicate(linked=False)
            dup = [o for o in bpy.context.selected_objects]
            ang = (math.pi / 2) * i
            ca, sa = math.cos(ang), math.sin(ang)
            for o in dup:
                x, y, z = o.location
                o.location = (x * ca - y * sa + (i - 1.5) * gap, x * sa + y * ca, z)
                # (sheet faces are rotated the same way as the turntable)
                o.rotation_euler.z += ang
                bpy.ops.object.select_all(action="DESELECT")
            made += dup
        allobj = objs + made
        sb = scene_bbox(allobj)
        (sx0, sx1), (sy0, sy1), (sz0, sz1) = sb
        scz = (sz0 + sz1) * 0.5
        wide = sx1 - sx0
        sc = studio(scz, (sz1 - sz0))
        setup_render(int(res * 2.3), int(res * 1.02), samples)
        bpy.ops.object.camera_add(location=(0, wide * 1.0, scz))
        cam2 = bpy.context.active_object
        cam2.data.lens = 85
        sc.camera = cam2
        d2 = fit_distance(wide, sz1 - sz0, rx=int(res * 2.3), ry=int(res * 1.02), margin=1.05)
        cam2.location = (0, d2, scz)
        r = shoot(sc, cam2, (0.0, 0.0, scz), "%s_sheet.png" % out, sb)
        print("  sheet      x %.3f..%.3f | y %.3f..%.3f" % r, flush=True)
        written.append(sc.render.filepath)

    tb = len(body.data.vertices)
    print("BLENDER_BASE OK")
    print("  base verts : %d (body incl. its own head)" % tb)
    sb2 = scene_bbox(objs)
    print("  figure     : %.3f m tall" % (sb2[2][1] - sb2[2][0]))
    print("  written    : %s" % ", ".join(written))


if __name__ == "__main__":
    main()
