from __future__ import annotations

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import ezdxf
import ezdxf.bbox
from ezdxf import colors
from ezdxf.document import Drawing
from ezdxf.enums import TextEntityAlignment
from ezdxf.math import Matrix44

LAYER_CUT = "CUT"
LAYER_ETCH = "ETCH"

MARGIN = 2.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BadgeTemplate:
    url: str
    logo_uri: Optional[str]
    corner_radius_mm: float = 3.0


@dataclass(frozen=True)
class PersonInfo:
    name: str
    pronoun: str
    lanyard_hole: bool = True


def load_template(path: Path) -> BadgeTemplate:
    data = json.loads(path.read_text())
    return BadgeTemplate(
        url=str(data.get("url", "")),
        logo_uri=data.get("logo_uri") or None,
        corner_radius_mm=float(data.get("corner_radius_mm", 3.0)),
    )


def load_person(path: Path) -> PersonInfo:
    data = json.loads(path.read_text())
    return PersonInfo(
        name=str(data.get("name", "")),
        pronoun=str(data.get("pronoun", "")),
        lanyard_hole=bool(data.get("lanyard_hole", True)),
    )


# ---------------------------------------------------------------------------
# Layers / shared helpers
# ---------------------------------------------------------------------------

def ensure_layers(doc: Drawing) -> None:
    if LAYER_CUT not in doc.layers:
        doc.layers.add(LAYER_CUT, color=colors.RED)
    if LAYER_ETCH not in doc.layers:
        doc.layers.add(LAYER_ETCH, color=colors.BLUE)


def add_rounded_rect(msp, x: float, y: float, w: float, h: float,
                     r: float, layer: str) -> None:
    """Rounded rectangle as LWPOLYLINE with bulge arcs."""
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    if r == 0.0:
        msp.add_lwpolyline(
            [(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
            close=True, dxfattribs={"layer": layer},
        )
        return
    bulge = 0.41421356237309503  # tan(22.5°)
    pts = [
        (x + r,     y,          0.0),
        (x + w - r, y,          bulge),
        (x + w,     y + r,      0.0),
        (x + w,     y + h - r,  bulge),
        (x + w - r, y + h,      0.0),
        (x + r,     y + h,      bulge),
        (x,         y + h - r,  0.0),
        (x,         y + r,      bulge),
    ]
    msp.add_lwpolyline(pts, format="xyb", close=True, dxfattribs={"layer": layer})


BADGE_W, BADGE_H = 50.0, 75.0


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# DXF logo insertion
# ---------------------------------------------------------------------------

def insert_dxf_logo(msp, logo_path: Path, zone_x: float, zone_y: float,
                    zone_w: float, zone_h: float) -> None:
    logo_doc = ezdxf.readfile(str(logo_path))
    logo_msp = logo_doc.modelspace()

    bb = ezdxf.bbox.extents(logo_msp)
    if bb is None or bb.size.x == 0 or bb.size.y == 0:
        print(f"  Warning: could not compute bounding box for {logo_path}, skipping logo")
        return

    lw, lh = bb.size.x, bb.size.y
    scale = min(zone_w / lw, zone_h / lh)

    # Centre in zone
    tx = zone_x + (zone_w - lw * scale) / 2.0 - bb.extmin.x * scale
    ty = zone_y + (zone_h - lh * scale) / 2.0 - bb.extmin.y * scale

    m = Matrix44.scale(scale, scale, 1) @ Matrix44.translate(tx, ty, 0)

    skipped = 0
    for entity in logo_msp:
        try:
            e = entity.copy()
            e.transform(m)
            e.dxf.layer = LAYER_ETCH
            msp.add_entity(e)
        except Exception:
            skipped += 1

    if skipped:
        print(f"  Warning: skipped {skipped} entity/entities from {logo_path}")


# ---------------------------------------------------------------------------
# SVG logo insertion
# ---------------------------------------------------------------------------

SVG_NS = "http://www.w3.org/2000/svg"


def _parse_svg_viewbox(root: ET.Element) -> tuple[float, float, float, float]:
    vb = root.get("viewBox") or root.get("viewbox")
    if vb:
        parts = re.split(r"[\s,]+", vb.strip())
        if len(parts) == 4:
            return tuple(float(p) for p in parts)  # type: ignore[return-value]
    w = float(root.get("width", "100").rstrip("px"))
    h = float(root.get("height", "100").rstrip("px"))
    return 0.0, 0.0, w, h


def _parse_d(d: str) -> list[list[tuple[float, float]]]:
    """
    Very small SVG path 'd' parser.  Returns a list of sub-paths, each a
    list of 2-D points (after cubic/quadratic/arc approximation).
    """
    # tokenise into (cmd, [nums]) pairs
    tokens = re.findall(r"([MmLlHhVvCcSsQqTtAaZz])|([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", d)
    commands: list[tuple[str, list[float]]] = []
    current_cmd: Optional[str] = None
    current_nums: list[float] = []
    for cmd, num in tokens:
        if cmd:
            if current_cmd is not None:
                commands.append((current_cmd, current_nums))
            current_cmd = cmd
            current_nums = []
        elif num:
            current_nums.append(float(num))
    if current_cmd is not None:
        commands.append((current_cmd, current_nums))

    sub_paths: list[list[tuple[float, float]]] = []
    cur_path: list[tuple[float, float]] = []
    cx, cy = 0.0, 0.0
    start_x, start_y = 0.0, 0.0
    # Tracks the last cubic cp2 for smooth bezier reflection (s/S)
    last_cp2: tuple[float, float] = (0.0, 0.0)
    last_cmd_was_cubic = False

    def flush():
        nonlocal cur_path
        if len(cur_path) >= 2:
            sub_paths.append(cur_path)
        cur_path = []

    def _cubic_bezier_pts(p0, p1, p2, p3, steps=20):
        pts = []
        for i in range(steps + 1):
            t = i / steps
            mt = 1 - t
            x = mt**3*p0[0] + 3*mt**2*t*p1[0] + 3*mt*t**2*p2[0] + t**3*p3[0]
            y = mt**3*p0[1] + 3*mt**2*t*p1[1] + 3*mt*t**2*p2[1] + t**3*p3[1]
            pts.append((x, y))
        return pts

    def _quad_bezier_pts(p0, p1, p2, steps=16):
        pts = []
        for i in range(steps + 1):
            t = i / steps
            mt = 1 - t
            x = mt**2*p0[0] + 2*mt*t*p1[0] + t**2*p2[0]
            y = mt**2*p0[1] + 2*mt*t*p1[1] + t**2*p2[1]
            pts.append((x, y))
        return pts

    def _svg_arc_to_cubics(x1, y1, rx, ry, x_rotation, large_arc, sweep, x2, y2):
        """Convert SVG arc to list of cubic bezier control-point tuples."""
        if rx == 0 or ry == 0:
            return [((x1, y1), (x2, y2), (x2, y2), (x2, y2))]
        phi = math.radians(x_rotation)
        cos_phi, sin_phi = math.cos(phi), math.sin(phi)
        dx = (x1 - x2) / 2
        dy = (y1 - y2) / 2
        x1p = cos_phi * dx + sin_phi * dy
        y1p = -sin_phi * dx + cos_phi * dy
        # Clamp radii
        lam = (x1p / rx)**2 + (y1p / ry)**2
        if lam > 1:
            sq = math.sqrt(lam)
            rx *= sq; ry *= sq
        num = max(0.0, rx**2 * ry**2 - rx**2 * y1p**2 - ry**2 * x1p**2)
        den = rx**2 * y1p**2 + ry**2 * x1p**2
        sq = math.sqrt(num / den) if den else 0.0
        if large_arc == sweep:
            sq = -sq
        cxp = sq * rx * y1p / ry
        cyp = -sq * ry * x1p / rx
        cx_c = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2
        cy_c = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2

        def angle(ux, uy, vx, vy):
            n = math.sqrt(ux**2 + uy**2) * math.sqrt(vx**2 + vy**2)
            if n == 0:
                return 0.0
            c = max(-1.0, min(1.0, (ux*vx + uy*vy) / n))
            a = math.acos(c)
            if ux * vy - uy * vx < 0:
                a = -a
            return a

        theta1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
        dtheta = angle((x1p - cxp) / rx, (y1p - cyp) / ry,
                       (-x1p - cxp) / rx, (-y1p - cyp) / ry)
        if not sweep and dtheta > 0:
            dtheta -= 2 * math.pi
        elif sweep and dtheta < 0:
            dtheta += 2 * math.pi

        n_segs = max(1, math.ceil(abs(dtheta) / (math.pi / 2)))
        dt = dtheta / n_segs
        alpha = math.sin(dt) * (math.sqrt(4 + 3 * math.tan(dt / 2)**2) - 1) / 3

        cubics = []
        t = theta1
        px = cos_phi * rx * math.cos(t) - sin_phi * ry * math.sin(t) + cx_c
        py = sin_phi * rx * math.cos(t) + cos_phi * ry * math.sin(t) + cy_c
        dpx = -cos_phi * rx * math.sin(t) - sin_phi * ry * math.cos(t)
        dpy = -sin_phi * rx * math.sin(t) + cos_phi * ry * math.cos(t)
        for _ in range(n_segs):
            t2 = t + dt
            qx = cos_phi * rx * math.cos(t2) - sin_phi * ry * math.sin(t2) + cx_c
            qy = sin_phi * rx * math.cos(t2) + cos_phi * ry * math.sin(t2) + cy_c
            dqx = -cos_phi * rx * math.sin(t2) - sin_phi * ry * math.cos(t2)
            dqy = -sin_phi * rx * math.sin(t2) + cos_phi * ry * math.cos(t2)
            cubics.append((
                (px, py),
                (px + alpha * dpx, py + alpha * dpy),
                (qx - alpha * dqx, qy - alpha * dqy),
                (qx, qy),
            ))
            px, py, dpx, dpy, t = qx, qy, dqx, dqy, t2
        return cubics

    for cmd, nums in commands:
        rel = cmd.islower()
        c = cmd.lower()

        if c == "m":
            flush()
            it = iter(nums)
            first = True
            for x, y in zip(it, it):
                if rel and not first:
                    cx += x; cy += y
                elif rel:
                    cx += x; cy += y
                else:
                    cx = x; cy = y
                if first:
                    start_x, start_y = cx, cy
                    first = False
                cur_path.append((cx, cy))
            last_cmd_was_cubic = False

        elif c == "l":
            it = iter(nums)
            for x, y in zip(it, it):
                if rel:
                    cx += x; cy += y
                else:
                    cx = x; cy = y
                cur_path.append((cx, cy))
            last_cmd_was_cubic = False

        elif c == "h":
            for x in nums:
                cx = cx + x if rel else x
                cur_path.append((cx, cy))
            last_cmd_was_cubic = False

        elif c == "v":
            for y in nums:
                cy = cy + y if rel else y
                cur_path.append((cx, cy))
            last_cmd_was_cubic = False

        elif c == "c":
            it = iter(nums)
            for x1, y1, x2, y2, x, y in zip(it, it, it, it, it, it):
                if rel:
                    p1 = (cx + x1, cy + y1)
                    p2 = (cx + x2, cy + y2)
                    ep = (cx + x, cy + y)
                else:
                    p1 = (x1, y1); p2 = (x2, y2); ep = (x, y)
                pts = _cubic_bezier_pts((cx, cy), p1, p2, ep)
                cur_path.extend(pts[1:])
                last_cp2 = p2
                cx, cy = ep
            last_cmd_was_cubic = True

        elif c == "s":
            it = iter(nums)
            for x2, y2, x, y in zip(it, it, it, it):
                if rel:
                    p2 = (cx + x2, cy + y2)
                    ep = (cx + x, cy + y)
                else:
                    p2 = (x2, y2); ep = (x, y)
                # Implicit cp1: reflection of previous cp2 through current point
                if last_cmd_was_cubic:
                    p1 = (2 * cx - last_cp2[0], 2 * cy - last_cp2[1])
                else:
                    p1 = (cx, cy)
                pts = _cubic_bezier_pts((cx, cy), p1, p2, ep)
                cur_path.extend(pts[1:])
                last_cp2 = p2
                cx, cy = ep
            last_cmd_was_cubic = True

        elif c == "q":
            it = iter(nums)
            for x1, y1, x, y in zip(it, it, it, it):
                if rel:
                    p1 = (cx + x1, cy + y1)
                    ep = (cx + x, cy + y)
                else:
                    p1 = (x1, y1); ep = (x, y)
                pts = _quad_bezier_pts((cx, cy), p1, ep)
                cur_path.extend(pts[1:])
                cx, cy = ep
            last_cmd_was_cubic = False

        elif c == "a":
            it = iter(nums)
            for rx, ry, x_rot, la, sw, x, y in zip(it, it, it, it, it, it, it):
                la, sw = int(la), int(sw)
                ex = (cx + x) if rel else x
                ey = (cy + y) if rel else y
                for cubic in _svg_arc_to_cubics(cx, cy, rx, ry, x_rot, la, sw, ex, ey):
                    pts = _cubic_bezier_pts(*cubic)
                    cur_path.extend(pts[1:])
                cx, cy = ex, ey
            last_cmd_was_cubic = False

        elif c == "z":
            cur_path.append((start_x, start_y))
            flush()
            cx, cy = start_x, start_y
            last_cmd_was_cubic = False

    flush()
    return sub_paths


def _parse_coords(s: str) -> list[float]:
    return [float(v) for v in re.split(r"[\s,]+", s.strip()) if v]


def _walk_svg(elem: ET.Element, vx: float, vy: float, vh: float,
              paths_out: list[list[tuple[float, float]]]) -> None:
    tag = elem.tag.replace(f"{{{SVG_NS}}}", "")
    if tag == "defs":
        return

    def fy(y_svg: float) -> float:
        """Flip SVG Y to DXF Y."""
        return (vy + vh) - y_svg

    if tag == "rect":
        x = float(elem.get("x", 0))
        y = float(elem.get("y", 0))
        w = float(elem.get("width", 0))
        h = float(elem.get("height", 0))
        if w > 0 and h > 0:
            paths_out.append([
                (x, fy(y)),
                (x + w, fy(y)),
                (x + w, fy(y + h)),
                (x, fy(y + h)),
                (x, fy(y)),
            ])

    elif tag in ("circle", "ellipse"):
        cx = float(elem.get("cx", 0))
        cy_s = float(elem.get("cy", 0))
        if tag == "circle":
            rx = ry = float(elem.get("r", 0))
        else:
            rx = float(elem.get("rx", 0))
            ry = float(elem.get("ry", 0))
        if rx > 0 and ry > 0:
            # 4-arc cubic bezier approximation
            k = 0.5522847498
            pts: list[tuple[float, float]] = []
            for cubic in [
                ((cx + rx, fy(cy_s)),       (cx + rx,      fy(cy_s - k*ry)),
                 (cx + k*rx, fy(cy_s - ry)), (cx,           fy(cy_s - ry))),
                ((cx,        fy(cy_s - ry)), (cx - k*rx,   fy(cy_s - ry)),
                 (cx - rx,   fy(cy_s - k*ry)), (cx - rx,   fy(cy_s))),
                ((cx - rx,   fy(cy_s)),      (cx - rx,      fy(cy_s + k*ry)),
                 (cx - k*rx, fy(cy_s + ry)), (cx,           fy(cy_s + ry))),
                ((cx,        fy(cy_s + ry)), (cx + k*rx,   fy(cy_s + ry)),
                 (cx + rx,   fy(cy_s + k*ry)), (cx + rx,   fy(cy_s))),
            ]:
                p0, p1, p2, p3 = cubic
                steps = 16
                for i in range(steps + 1):
                    t = i / steps
                    mt = 1 - t
                    x = mt**3*p0[0] + 3*mt**2*t*p1[0] + 3*mt*t**2*p2[0] + t**3*p3[0]
                    y = mt**3*p0[1] + 3*mt**2*t*p1[1] + 3*mt*t**2*p2[1] + t**3*p3[1]
                    if pts and i == 0:
                        continue
                    pts.append((x, y))
            paths_out.append(pts)

    elif tag == "line":
        x1 = float(elem.get("x1", 0))
        y1 = float(elem.get("y1", 0))
        x2 = float(elem.get("x2", 0))
        y2 = float(elem.get("y2", 0))
        paths_out.append([(x1, fy(y1)), (x2, fy(y2))])

    elif tag in ("polyline", "polygon"):
        raw = _parse_coords(elem.get("points", ""))
        if len(raw) >= 4:
            pts = [(raw[i], fy(raw[i + 1])) for i in range(0, len(raw) - 1, 2)]
            if tag == "polygon" and pts:
                pts.append(pts[0])
            paths_out.append(pts)

    elif tag == "path":
        d = elem.get("d", "")
        if d:
            raw_paths = _parse_d(d)
            for rp in raw_paths:
                # flip Y
                paths_out.append([(px, fy(py)) for px, py in rp])

    # Recurse into groups and unknown containers
    if tag in ("g", "svg", "a", "symbol"):
        for child in elem:
            _walk_svg(child, vx, vy, vh, paths_out)


def insert_svg_logo(msp, logo_path: Path, zone_x: float, zone_y: float,
                    zone_w: float, zone_h: float) -> None:
    tree = ET.parse(str(logo_path))
    root = tree.getroot()

    vx, vy, vw, vh = _parse_svg_viewbox(root)

    paths: list[list[tuple[float, float]]] = []
    _walk_svg(root, vx, vy, vh, paths)

    if not paths:
        print(f"  Warning: no renderable paths found in {logo_path}, skipping logo")
        return

    # Overall bounding box of collected paths
    all_pts = [pt for p in paths for pt in p]
    min_x = min(pt[0] for pt in all_pts)
    max_x = max(pt[0] for pt in all_pts)
    min_y = min(pt[1] for pt in all_pts)
    max_y = max(pt[1] for pt in all_pts)

    lw = max_x - min_x
    lh = max_y - min_y
    if lw == 0 or lh == 0:
        print(f"  Warning: zero-size logo in {logo_path}, skipping")
        return

    scale = min(zone_w / lw, zone_h / lh)
    tx = zone_x + (zone_w - lw * scale) / 2.0 - min_x * scale
    ty = zone_y + (zone_h - lh * scale) / 2.0 - min_y * scale

    for path_pts in paths:
        transformed = [(px * scale + tx, py * scale + ty) for px, py in path_pts]
        if len(transformed) >= 2:
            msp.add_lwpolyline(transformed, dxfattribs={"layer": LAYER_ETCH})


# ---------------------------------------------------------------------------
# Logo dispatch
# ---------------------------------------------------------------------------

def uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme in ("", "file"):
        # plain path or file:// URI
        if parsed.scheme == "file":
            return Path(parsed.path)
        return Path(uri)
    raise ValueError(f"Unsupported URI scheme {parsed.scheme!r} — only local file paths are supported")


def insert_logo(msp, logo_uri: str, zone_x: float, zone_y: float,
                zone_w: float, zone_h: float) -> None:
    logo_path = uri_to_path(logo_uri)
    if not logo_path.exists():
        print(f"  Warning: logo file not found: {logo_path}, skipping")
        return
    suffix = logo_path.suffix.lower()
    if suffix == ".dxf":
        insert_dxf_logo(msp, logo_path, zone_x, zone_y, zone_w, zone_h)
    elif suffix == ".svg":
        insert_svg_logo(msp, logo_path, zone_x, zone_y, zone_w, zone_h)
    else:
        print(f"  Warning: unsupported logo format {suffix!r}, skipping")


# ---------------------------------------------------------------------------
# Main badge builder
# ---------------------------------------------------------------------------

URL_HEIGHT      = 2.5   # mm — font cap height for the bottom URL line
NAME_BOTTOM     = 15.0  # mm — distance from badge bottom to bottom of name text
NAME_MAX_HEIGHT  = 12.5  # mm — maximum name font size
CHAR_WIDTH_RATIO = 0.75  # conservative width/height ratio per character (Ubuntu Regular ~0.72)
PRONOUN_HEIGHT   = 3.5   # mm — pronoun font size
GAP              = 1.0   # mm — general spacing between layout zones


def build_badge(template: BadgeTemplate, person: PersonInfo) -> Drawing:
    doc = ezdxf.new(setup=True)
    ensure_layers(doc)
    doc.styles.add("Ubuntu", font="Ubuntu-R.ttf")
    doc.styles.add("UbuntuBold", font="Ubuntu-Bold.ttf")
    msp = doc.modelspace()

    w, h = BADGE_W, BADGE_H

    # --- Outline ---
    add_rounded_rect(msp, 0.0, 0.0, w, h, template.corner_radius_mm, LAYER_CUT)

    # --- Border (1 mm inset, 1 mm thick → two etch lines) ---
    border_inset = 1.0
    border_thick = 1.0
    for i in (0, 1):
        inset = border_inset + i * border_thick
        r = max(0.0, template.corner_radius_mm - inset)
        add_rounded_rect(msp, inset, inset, w - 2 * inset, h - 2 * inset, r, LAYER_ETCH)

    # --- Lanyard hole (top centre, opposite the URL) ---
    slot_w, slot_h = 13.5, 3.5
    slot_r = slot_h / 2.0  # pill shape
    hole_cx, hole_cy = w / 2.0, h - 8.0

    if person.lanyard_hole:
        add_rounded_rect(msp,
                         hole_cx - slot_w / 2.0, hole_cy - slot_h / 2.0,
                         slot_w, slot_h, slot_r, LAYER_CUT)

    # --- Name font size: fit to badge width, capped at NAME_MAX_HEIGHT ---
    avail_name_w = w - 2 * MARGIN
    if person.name:
        name_height = min(
            avail_name_w / (len(person.name) * CHAR_WIDTH_RATIO),
            NAME_MAX_HEIGHT,
        )
    else:
        name_height = 0.0

    # --- Logo zone (between name and lanyard hole) ---
    content_y = NAME_BOTTOM + name_height + GAP
    top_limit = (hole_cy - slot_h / 2.0 - GAP) if person.lanyard_hole else (h - MARGIN)
    content_x = MARGIN
    content_w = w - 2 * MARGIN
    content_h = top_limit - content_y

    if template.logo_uri:
        logo_size = min(content_w, content_h)
        logo_zone: Optional[tuple] = (
            content_x + (content_w - logo_size) / 2.0,
            content_y + (content_h - logo_size) / 2.0,
            logo_size, logo_size,
        )
    else:
        logo_zone = None

    # --- Logo ---
    if logo_zone and template.logo_uri:
        lx, ly, lw2, lh2 = logo_zone
        insert_logo(msp, template.logo_uri, lx, ly, lw2, lh2)

    # --- Name (fixed position: bottom at NAME_BOTTOM, centred, Ubuntu Regular) ---
    if person.name:
        t = msp.add_text(
            person.name,
            dxfattribs={"layer": LAYER_ETCH, "height": name_height, "style": "Ubuntu"},
        )
        t.set_placement((w / 2.0, NAME_BOTTOM), align=TextEntityAlignment.BOTTOM_CENTER)

    # --- Pronoun (right-justified, top flush with name bottom, Ubuntu Regular) ---
    if person.pronoun:
        t = msp.add_text(
            person.pronoun,
            dxfattribs={"layer": LAYER_ETCH, "height": PRONOUN_HEIGHT, "style": "Ubuntu"},
        )
        t.set_placement((w - MARGIN - 1.0, NAME_BOTTOM - GAP - 1.0), align=TextEntityAlignment.TOP_RIGHT)

    # --- URL (bottom centre, Ubuntu Bold, lowercase) ---
    if template.url:
        t = msp.add_text(
            template.url.lower(),
            dxfattribs={"layer": LAYER_ETCH, "height": URL_HEIGHT, "style": "UbuntuBold"},
        )
        t.set_placement((w / 2.0, MARGIN + URL_HEIGHT / 2.0),
                        align=TextEntityAlignment.CENTER)

    return doc


# ---------------------------------------------------------------------------
# Laser sheet arrangement
# ---------------------------------------------------------------------------

INCH_MM = 25.4


def arrange_for_laser(
    badge_dir: Path,
    material_w_in: float = 24.0,
    material_h_in: float = 12.0,
    margin_mm: float = 1.0,
) -> list[Drawing]:
    """
    Arrange all DXF badge files in a directory onto one or more laser-ready
    sheets.

    Badges are packed left-to-right in rows from the bottom of the material
    upward.  Rows are sorted tallest-first so the leftover space is a single
    large rectangle at the far end of the last sheet.

    Parameters
    ----------
    badge_dir:
        Directory containing the individual badge ``.dxf`` files.
    material_w_in / material_h_in:
        Material dimensions in inches.  Defaults to 24 × 12 in.
    margin_mm:
        Clear gap maintained around every badge — 1 mm on each side,
        giving 2 mm between adjacent badges and 1 mm at material edges.

    Returns
    -------
    list[Drawing]
        One Drawing per sheet required.  The caller is responsible for saving.

    Raises
    ------
    ValueError
        If a single badge is larger than the usable material area.
    """
    badge_paths = sorted(badge_dir.glob("*.dxf"))
    if not badge_paths:
        return []

    mat_w = material_w_in * INCH_MM
    mat_h = material_h_in * INCH_MM

    def _load_and_measure(path: Path) -> tuple[float, float, float, float, Drawing]:
        """Load a DXF file and return (width, height, origin_x, origin_y, doc)."""
        doc = ezdxf.readfile(str(path))
        bb = ezdxf.bbox.extents(doc.modelspace())
        if bb is None or bb.size.x == 0 or bb.size.y == 0:
            return BADGE_W, BADGE_H, 0.0, 0.0, doc
        return (
            float(bb.size.x), float(bb.size.y),
            float(bb.extmin.x), float(bb.extmin.y),
            doc,
        )

    # Load all badges and sort tallest-first for compact row packing
    measured = [_load_and_measure(p) for p in badge_paths]
    measured.sort(key=lambda r: r[1], reverse=True)

    sheets: list[Drawing] = []

    def _new_sheet() -> tuple[Drawing, object]:
        doc = ezdxf.new(setup=True)
        ensure_layers(doc)
        return doc, doc.modelspace()

    sheet_doc, msp = _new_sheet()
    cur_x = margin_mm
    cur_y = margin_mm
    row_h = 0.0

    for bw, bh, ox, oy, badge_doc in measured:
        if bw + 2 * margin_mm > mat_w or bh + 2 * margin_mm > mat_h:
            raise ValueError(
                f"Badge ({bw:.1f} × {bh:.1f} mm) is too large for "
                f"{mat_w:.1f} × {mat_h:.1f} mm material with "
                f"{margin_mm} mm margins."
            )

        # Wrap to next row if badge won't fit horizontally
        if cur_x + bw + margin_mm > mat_w:
            cur_x = margin_mm
            cur_y += row_h + margin_mm
            row_h = 0.0

        # Wrap to next sheet if badge won't fit vertically
        if cur_y + bh + margin_mm > mat_h:
            sheets.append(sheet_doc)
            sheet_doc, msp = _new_sheet()
            cur_x = margin_mm
            cur_y = margin_mm
            row_h = 0.0

        # Copy badge entities into the sheet, translated to (cur_x, cur_y)
        m = Matrix44.translate(cur_x - ox, cur_y - oy, 0)
        for entity in badge_doc.modelspace():
            try:
                e = entity.copy()
                e.transform(m)
                msp.add_entity(e)
            except Exception:
                pass

        cur_x += bw + margin_mm
        row_h = max(row_h, bh)

    sheets.append(sheet_doc)
    return sheets


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_DEFAULT_TEMPLATE = BadgeTemplate(
    url="url.com",
    logo_uri=None,
)


def _save(doc: Drawing, path: Path) -> None:
    doc.saveas(str(path))
    print(f"Wrote {path.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a laser-cuttable DXF badge")
    parser.add_argument("template", type=Path, nargs="?", default=None,
                        help="Path to badge template JSON file (optional)")
    parser.add_argument("person", type=Path, nargs="?", default=None,
                        help="Path to person JSON file (optional)")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="Output DXF path (single-template mode only)")
    args = parser.parse_args()

    person = load_person(args.person) if args.person else PersonInfo(name="", pronoun="")
    outputs = Path("outputs")

    template = load_template(args.template) if args.template else _DEFAULT_TEMPLATE

    if args.output:
        out_path = args.output
    elif args.person:
        out_path = outputs / args.person.with_suffix(".dxf").name
    elif args.template:
        out_path = outputs / args.template.with_suffix(".dxf").name
    else:
        out_path = outputs / "out.dxf"

    _save(build_badge(template, person), out_path)


if __name__ == "__main__":
    main()
