#!/usr/bin/env python3
# Copyright (C) 2026 Andrei Errapart
# SPDX-License-Identifier: GPL-2.0-or-later
"""
pdf_dump — Dump vector lines, curves, rectangles, and text from a PDF as JSON.

Uses PyMuPDF (fitz) to extract drawing primitives and text spans from each
page, converting coordinates from PDF points to millimeters.

Output: a JSON object with a "pages" array, each containing "lines",
"curves", "rectangles", and "texts" arrays. With "--decode", pages also
contain a "decoded" object with component bodies, pins, wires, and global
labels inferred from OrCAD off-page connectors.

Usage:
    scripts/pdf_dump <input.pdf> [--page N] [--raw] [--decode]
"""

from __future__ import annotations

import json
import math
import re
import sys

try:
    import fitz  # PyMuPDF
except ImportError:
    print("PyMuPDF not found. Install with: pip install PyMuPDF",
          file=sys.stderr)
    sys.exit(1)


PT_TO_MM = 25.4 / 72.0
# OrCAD schematic PDF drawing colors documented in doc/ORCAD_PDF_FORMAT.md.
BODY_COLOR = "#cc8005"
PIN_COLOR = "#aa8744"
WIRE_COLOR = "#4200ff"
PIN_NUMBER_COLOR = "#000000"
PIN_NAME_COLOR = "#0000cc"
GLOBAL_LABEL_TEXT_COLOR = "#ff0000"
GLOBAL_LABEL_PAGE_REFERENCE_RE = re.compile(
    r"<[0-9]+(?:\s*,\s*[0-9]+)*>"
)
GEOM_TOL = 0.18
EDGE_TOL = 0.30
MIN_SYMBOL_W = 2.0
MIN_SYMBOL_H = 2.0
PIN_NUMBER_MAX_LEN = 4
PIN_NAME_TOL = 0.45
CHEVRON_MIN_ARM = 0.25
CHEVRON_MAX_ARM = 3.0
CHEVRON_POINT_TOL = 0.12
REF_RE = re.compile(
    r"^(?:U|CN|J|P|R|C|L|D|Q|Y|FB|TP|SW|DSW|SD|SCR|SP|X|F|VR)\d+[A-Z]?$"
)


def pt2mm(pt: float) -> float:
    return round(pt * PT_TO_MM, 3)


def _pt_xy(p):
    """Get (x, y) from a fitz.Point or tuple."""
    return (p.x if hasattr(p, 'x') else p[0],
            p.y if hasattr(p, 'y') else p[1])


def _rect_xywh(rect):
    """Get (x0, y0, x1, y1) from a fitz.Rect or tuple."""
    if hasattr(rect, 'x0'):
        return rect.x0, rect.y0, rect.x1, rect.y1
    return rect[0], rect[1], rect[2], rect[3]


def _color_hex(color) -> str | None:
    """Convert an RGB float tuple to a #rrggbb hex string."""
    if color is None:
        return None
    r, g, b = color
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"


def _color_int_hex(color_int) -> str:
    """Convert a PyMuPDF integer color (0xRRGGBB) to #rrggbb."""
    return f"#{color_int:06x}"


def _bezier_points(p0, p1, p2, p3, n=16):
    """Interpolate a cubic Bezier curve into n+1 points (in PDF points)."""
    points = []
    for i in range(n + 1):
        t = i / n
        s = 1 - t
        x = s*s*s*p0[0] + 3*s*s*t*p1[0] + 3*s*t*t*p2[0] + t*t*t*p3[0]
        y = s*s*s*p0[1] + 3*s*s*t*p1[1] + 3*s*t*t*p2[1] + t*t*t*p3[1]
        points.append((x, y))
    return points


def _span_angle(s: dict) -> float:
    """Compute text angle in degrees from a span's writing direction."""
    dx, dy = s.get('dir', (1.0, 0.0))
    deg = round(math.degrees(math.atan2(-dy, dx)), 1)
    if deg < 0:
        deg += 360
    if deg == 0:
        deg = 0.0
    return deg


def _round_coord(v: float) -> float:
    return round(v, 3)


def _line_length(line: dict) -> float:
    return math.hypot(line["x2"] - line["x1"], line["y2"] - line["y1"])


def _line_points(line: dict):
    return ((line["x1"], line["y1"]), (line["x2"], line["y2"]))


def _point_dict(point) -> dict:
    return {"x": _round_coord(point[0]), "y": _round_coord(point[1])}


def _dist_point_to_rect_edge(x: float, y: float, bbox: dict) -> float:
    inside_x = bbox["x0"] - EDGE_TOL <= x <= bbox["x1"] + EDGE_TOL
    inside_y = bbox["y0"] - EDGE_TOL <= y <= bbox["y1"] + EDGE_TOL
    dists = []
    if inside_x:
        dists.extend([abs(y - bbox["y0"]), abs(y - bbox["y1"])])
    if inside_y:
        dists.extend([abs(x - bbox["x0"]), abs(x - bbox["x1"])])
    return min(dists) if dists else float("inf")


def _point_in_rect_margin(x: float, y: float, bbox: dict, margin: float) -> bool:
    return (
        bbox["x0"] - margin <= x <= bbox["x1"] + margin
        and bbox["y0"] - margin <= y <= bbox["y1"] + margin
    )


def _merged_coverage(segments, start: float, end: float) -> float:
    """Return how much of [start, end] is covered by collinear segments."""
    if end < start:
        start, end = end, start
    clipped = []
    for s0, s1 in segments:
        a, b = sorted((s0, s1))
        a = max(a, start)
        b = min(b, end)
        if b + GEOM_TOL >= a:
            clipped.append((a, b))
    if not clipped:
        return 0.0
    clipped.sort()
    total = 0.0
    cur0, cur1 = clipped[0]
    for a, b in clipped[1:]:
        if a <= cur1 + GEOM_TOL:
            cur1 = max(cur1, b)
        else:
            total += cur1 - cur0
            cur0, cur1 = a, b
    total += cur1 - cur0
    return max(0.0, total)


def _has_edge(segments, start: float, end: float) -> bool:
    span = abs(end - start)
    if span < GEOM_TOL:
        return False
    return _merged_coverage(segments, start, end) >= span * 0.85


def _hv_segments(lines: list[dict], color: str) -> tuple[list[dict], list[dict]]:
    horizontals = []
    verticals = []
    for line in lines:
        if line.get("color") != color or _line_length(line) < GEOM_TOL:
            continue
        x1, y1 = line["x1"], line["y1"]
        x2, y2 = line["x2"], line["y2"]
        if abs(y1 - y2) <= GEOM_TOL and abs(x1 - x2) > GEOM_TOL:
            x0, x3 = sorted((x1, x2))
            horizontals.append({"y": _round_coord((y1 + y2) / 2),
                                "x0": x0, "x1": x3, "line": line})
        elif abs(x1 - x2) <= GEOM_TOL and abs(y1 - y2) > GEOM_TOL:
            y0, y3 = sorted((y1, y2))
            verticals.append({"x": _round_coord((x1 + x2) / 2),
                              "y0": y0, "y1": y3, "line": line})
    return horizontals, verticals


def _segments_at(values: list[dict], key: str, coord: float, akey: str, bkey: str):
    return [
        (seg[akey], seg[bkey])
        for seg in values
        if abs(seg[key] - coord) <= GEOM_TOL
    ]


def _rect_has_body_edges(bbox: dict, horizontals: list[dict], verticals: list[dict]) -> bool:
    top = _segments_at(horizontals, "y", bbox["y0"], "x0", "x1")
    bottom = _segments_at(horizontals, "y", bbox["y1"], "x0", "x1")
    left = _segments_at(verticals, "x", bbox["x0"], "y0", "y1")
    right = _segments_at(verticals, "x", bbox["x1"], "y0", "y1")
    return (
        _has_edge(top, bbox["x0"], bbox["x1"])
        and _has_edge(bottom, bbox["x0"], bbox["x1"])
        and _has_edge(left, bbox["y0"], bbox["y1"])
        and _has_edge(right, bbox["y0"], bbox["y1"])
    )


def _find_body_rectangles(lines: list[dict], rectangles: list[dict]) -> list[dict]:
    horizontals, verticals = _hv_segments(lines, BODY_COLOR)
    candidates = []

    for rect in rectangles:
        if rect.get("color") == BODY_COLOR:
            w = abs(rect["x1"] - rect["x0"])
            h = abs(rect["y1"] - rect["y0"])
            if w >= MIN_SYMBOL_W and h >= MIN_SYMBOL_H:
                candidates.append({
                    "x0": min(rect["x0"], rect["x1"]),
                    "y0": min(rect["y0"], rect["y1"]),
                    "x1": max(rect["x0"], rect["x1"]),
                    "y1": max(rect["y0"], rect["y1"]),
                })

    for bbox in _body_component_rectangles(lines):
        if _rect_has_body_edges(bbox, horizontals, verticals):
            candidates.append(bbox)

    return _dedupe_rectangles(candidates)


def _body_component_rectangles(lines: list[dict]) -> list[dict]:
    body_lines = [
        line for line in lines
        if line.get("color") == BODY_COLOR and _line_length(line) >= GEOM_TOL
    ]
    if not body_lines:
        return []

    parent = list(range(len(body_lines)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    endpoints = {}

    def point_key(x, y):
        return (round(x / GEOM_TOL), round(y / GEOM_TOL))

    for i, line in enumerate(body_lines):
        for x, y in _line_points(line):
            key = point_key(x, y)
            for other in endpoints.get(key, []):
                union(i, other)
            endpoints.setdefault(key, []).append(i)

    components = {}
    for i, line in enumerate(body_lines):
        root = find(i)
        components.setdefault(root, []).append(line)

    rects = []
    for comp in components.values():
        xs = []
        ys = []
        for line in comp:
            xs.extend([line["x1"], line["x2"]])
            ys.extend([line["y1"], line["y2"]])
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        if x1 - x0 >= MIN_SYMBOL_W and y1 - y0 >= MIN_SYMBOL_H:
            rects.append({
                "x0": _round_coord(x0),
                "y0": _round_coord(y0),
                "x1": _round_coord(x1),
                "y1": _round_coord(y1),
            })
    return rects


def _dedupe_rectangles(rects: list[dict]) -> list[dict]:
    rects = sorted(
        rects,
        key=lambda r: ((r["x1"] - r["x0"]) * (r["y1"] - r["y0"]), r["x0"], r["y0"]),
        reverse=True,
    )
    kept = []
    for rect in rects:
        area = (rect["x1"] - rect["x0"]) * (rect["y1"] - rect["y0"])
        duplicate = False
        for prev in kept:
            ix0 = max(rect["x0"], prev["x0"])
            iy0 = max(rect["y0"], prev["y0"])
            ix1 = min(rect["x1"], prev["x1"])
            iy1 = min(rect["y1"], prev["y1"])
            inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
            if inter >= area * 0.80:
                duplicate = True
                break
        if not duplicate:
            kept.append(rect)
    kept.sort(key=lambda r: (r["y0"], r["x0"]))
    return kept


def _text_center(text: dict) -> tuple[float, float]:
    return ((text["x"] + text["x1"]) / 2, (text["y"] + text["y1"]) / 2)


def _nearest_reference(bbox: dict, texts: list[dict]) -> dict | None:
    refs = [t for t in texts if REF_RE.match(t.get("text", ""))]
    if not refs:
        return None

    def score(text):
        cx, cy = _text_center(text)
        # Distance to nearest point on the top edge of the body
        nearest_x = max(bbox["x0"], min(cx, bbox["x1"]))
        dist = math.hypot(cx - nearest_x, cy - bbox["y0"])
        inside_or_near = _point_in_rect_margin(cx, cy, bbox, 8.0)
        penalty = 0.0 if inside_or_near else 25.0
        return (dist + penalty, dist)

    best = min(refs, key=score)
    if score(best)[0] > 45.0:
        return None
    return {
        "text": best["text"],
        "x": best["x"],
        "y": best["y"],
        "x1": best["x1"],
        "y1": best["y1"],
        "angle": best["angle"],
    }


def _pin_orientation(pin: dict) -> str:
    dx = pin["hot"]["x"] - pin["other"]["x"]
    dy = pin["hot"]["y"] - pin["other"]["y"]
    if abs(dx) >= abs(dy):
        return "left" if dx < 0 else "right"
    return "up" if dy < 0 else "down"


def _pin_number_candidates(texts: list[dict]) -> list[dict]:
    candidates = []
    for text in texts:
        value = text.get("text", "").strip()
        if (
            not value
            or len(value) > PIN_NUMBER_MAX_LEN
            or text.get("color") != PIN_NUMBER_COLOR
            or not any(ch.isdigit() for ch in value)
        ):
            continue
        candidates.append(text)
    return candidates


def _text_distance_to_point(text: dict, x: float, y: float) -> float:
    cx, cy = _text_center(text)
    return math.hypot(cx - x, cy - y)


def _pin_number_score(pin: dict, text: dict) -> float | None:
    hot = pin["hot"]
    other = pin["other"]
    side = pin["side"]
    tx0, ty0 = text["x"], text["y"]
    tx1, ty1 = text["x1"], text["y1"]
    cx, cy = _text_center(text)

    if side in ("left", "right"):
        x0, x1 = sorted((hot["x"], other["x"]))
        y = hot["y"]
        above = ty1 <= y + 0.5 and y - ty1 <= 4.0
        inside_or_near_segment = tx1 >= x0 - 0.5 and tx0 <= x1 + 2.5
        vertically_near = ty0 <= y + 2.0 and ty1 >= y - 2.0
        if above and inside_or_near_segment and vertically_near:
            target_x = other["x"]
            return abs(target_x - cx) + abs(y - cy)
    else:
        y0, y1 = sorted((hot["y"], other["y"]))
        x = hot["x"]
        left = tx1 <= x + 0.5 and x - tx1 <= 5.0
        between_or_near_segment = ty1 >= y0 - 0.5 and ty0 <= y1 + 2.5
        horizontally_near = tx0 <= x + 2.0 and tx1 >= x - 2.0
        if left and between_or_near_segment and horizontally_near:
            target_y = other["y"]
            return abs(x - cx) + abs(target_y - cy)
    return None


def _pin_number_for_pin(pin: dict, texts: list[dict], used: set[int]) -> tuple[int, dict] | None:
    candidates = []
    for i, text in enumerate(texts):
        if i in used:
            continue
        score = _pin_number_score(pin, text)
        if score is not None:
            candidates.append((score, i, text))
    if not candidates:
        return None
    _, i, text = min(candidates, key=lambda item: item[0])
    hot = pin["hot"]
    other = pin["other"]
    if min(
        _text_distance_to_point(text, hot["x"], hot["y"]),
        _text_distance_to_point(text, other["x"], other["y"]),
    ) > 7.0:
        return None
    return i, {
        "text": text["text"],
        "x": text["x"],
        "y": text["y"],
        "x1": text["x1"],
        "y1": text["y1"],
        "angle": text["angle"],
    }


def _pin_name_candidates(texts: list[dict]) -> list[dict]:
    return [
        text for text in texts
        if (
            text.get("text", "").strip()
            and text.get("color") == PIN_NAME_COLOR
        )
    ]


def _pin_name_score(pin: dict, bbox: dict, text: dict) -> float | None:
    hot = pin["hot"]
    side = pin["side"]
    tx0, ty0 = text["x"], text["y"]
    tx1, ty1 = text["x1"], text["y1"]
    cx, cy = _text_center(text)
    midx = (bbox["x0"] + bbox["x1"]) / 2
    midy = (bbox["y0"] + bbox["y1"]) / 2

    if side == "left":
        y = hot["y"]
        if ty0 - PIN_NAME_TOL <= y <= ty1 + PIN_NAME_TOL:
            if tx1 >= bbox["x0"] - PIN_NAME_TOL and tx0 <= midx + PIN_NAME_TOL:
                return abs(cy - y) + max(0.0, tx0 - bbox["x0"]) * 0.05
    elif side == "right":
        y = hot["y"]
        if ty0 - PIN_NAME_TOL <= y <= ty1 + PIN_NAME_TOL:
            if tx0 <= bbox["x1"] + PIN_NAME_TOL and tx1 >= midx - PIN_NAME_TOL:
                return abs(cy - y) + max(0.0, bbox["x1"] - tx1) * 0.05
    elif side == "up":
        x = hot["x"]
        if tx0 - PIN_NAME_TOL <= x <= tx1 + PIN_NAME_TOL:
            if ty1 >= bbox["y0"] - PIN_NAME_TOL and ty0 <= midy + PIN_NAME_TOL:
                return abs(cx - x) + max(0.0, ty0 - bbox["y0"]) * 0.05
    elif side == "down":
        x = hot["x"]
        if tx0 - PIN_NAME_TOL <= x <= tx1 + PIN_NAME_TOL:
            if ty0 <= bbox["y1"] + PIN_NAME_TOL and ty1 >= midy - PIN_NAME_TOL:
                return abs(cx - x) + max(0.0, bbox["y1"] - ty1) * 0.05
    return None


def _pin_name_for_pin(pin: dict, bbox: dict, texts: list[dict], used: set[int]) -> tuple[int, dict] | None:
    candidates = []
    for i, text in enumerate(texts):
        if i in used:
            continue
        score = _pin_name_score(pin, bbox, text)
        if score is not None:
            candidates.append((score, i, text))
    if not candidates:
        return None
    _, i, text = min(candidates, key=lambda item: item[0])
    return i, {
        "text": text["text"],
        "x": text["x"],
        "y": text["y"],
        "x1": text["x1"],
        "y1": text["y1"],
        "angle": text["angle"],
    }


def _pins_for_body(
    bbox: dict,
    lines: list[dict],
    texts: list[dict],
    reference_text: dict | None = None,
) -> list[dict]:
    pins = []
    center = ((bbox["x0"] + bbox["x1"]) / 2, (bbox["y0"] + bbox["y1"]) / 2)
    seen = set()
    pin_number_texts = _pin_number_candidates(texts)
    pin_name_texts = _pin_name_candidates(texts)
    if reference_text:
        pin_number_texts = [
            text for text in pin_number_texts
            if not (
                text["text"] == reference_text["text"]
                and abs(text["x"] - reference_text["x"]) <= GEOM_TOL
                and abs(text["y"] - reference_text["y"]) <= GEOM_TOL
            )
        ]
    for line in lines:
        if line.get("color") != PIN_COLOR:
            continue
        length = _line_length(line)
        if length < 0.4 or length > 12.0:
            continue
        p0, p1 = _line_points(line)
        d0 = _dist_point_to_rect_edge(p0[0], p0[1], bbox)
        d1 = _dist_point_to_rect_edge(p1[0], p1[1], bbox)
        if min(d0, d1) > EDGE_TOL:
            continue
        if not (
            _point_in_rect_margin(p0[0], p0[1], bbox, 8.0)
            or _point_in_rect_margin(p1[0], p1[1], bbox, 8.0)
        ):
            continue

        c0 = math.hypot(p0[0] - center[0], p0[1] - center[1])
        c1 = math.hypot(p1[0] - center[0], p1[1] - center[1])
        other, hot = (p0, p1) if c0 <= c1 else (p1, p0)
        key = (round(hot[0], 2), round(hot[1], 2), round(other[0], 2), round(other[1], 2))
        if key in seen:
            continue
        seen.add(key)
        pin = {
            "hot": _point_dict(hot),
            "other": _point_dict(other),
            "length": _round_coord(length),
        }
        pin["side"] = _pin_orientation(pin)
        pins.append(pin)
    pins.sort(key=lambda p: (p["side"], p["hot"]["y"], p["hot"]["x"]))
    used_texts = set()
    for pin in pins:
        number = _pin_number_for_pin(pin, pin_number_texts, used_texts)
        if number:
            text_index, number_text = number
            used_texts.add(text_index)
            pin["number"] = number_text["text"]
            pin["number_text"] = number_text
    used_name_texts = set()
    for pin in pins:
        name = _pin_name_for_pin(pin, bbox, pin_name_texts, used_name_texts)
        if name:
            text_index, name_text = name
            used_name_texts.add(text_index)
            pin["name"] = name_text["text"]
            pin["name_text"] = name_text
    return pins


def _promote_numeric_pin_names(pins: list[dict]) -> bool:
    if not pins or any(pin.get("number") for pin in pins):
        return False
    if not all(pin.get("name") and pin["name"].isdigit() for pin in pins):
        return False
    for pin in pins:
        pin["number"] = pin.pop("name")
        if "name_text" in pin:
            pin["number_text"] = pin.pop("name_text")
    return True


def _decode_wires(lines: list[dict]) -> list[dict]:
    wires = []
    seen = set()
    for line in lines:
        if line.get("color") != WIRE_COLOR or _line_length(line) < GEOM_TOL:
            continue
        p0, p1 = _line_points(line)
        key = tuple(round(v, 2) for point in sorted((p0, p1)) for v in point)
        if key in seen:
            continue
        seen.add(key)
        wires.append({
            "start": _point_dict(p0),
            "end": _point_dict(p1),
            "length": _round_coord(_line_length(line)),
        })
    wires.sort(key=lambda w: (w["start"]["y"], w["start"]["x"], w["end"]["y"], w["end"]["x"]))
    return wires


def _points_close(a, b, tolerance=CHEVRON_POINT_TOL) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= tolerance


def _chevrons(lines: list[dict]) -> list[dict]:
    """Find the one- or two-chevron glyph used by OrCAD off-page ports."""
    arms = []
    for index, line in enumerate(lines):
        dx = line["x2"] - line["x1"]
        dy = line["y2"] - line["y1"]
        length = math.hypot(dx, dy)
        if not (CHEVRON_MIN_ARM <= length <= CHEVRON_MAX_ARM):
            continue
        if abs(dx) <= GEOM_TOL or abs(dy) <= GEOM_TOL:
            continue
        if not (0.55 <= abs(dx / dy) <= 1.8):
            continue
        arms.append((index, line))

    found = []
    seen = set()
    for arm_index, (first_index, first) in enumerate(arms):
        first_points = _line_points(first)
        for second_index, second in arms[arm_index + 1:]:
            if first.get("color") != second.get("color"):
                continue
            second_points = _line_points(second)
            for fi, apex in enumerate(first_points):
                for si, other_apex in enumerate(second_points):
                    if not _points_close(apex, other_apex):
                        continue
                    q = first_points[1 - fi]
                    r = second_points[1 - si]
                    axis = None
                    if (
                        abs(q[0] - r[0]) <= CHEVRON_POINT_TOL
                        and abs(q[1] - r[1]) > 2 * GEOM_TOL
                        and abs(apex[1] - (q[1] + r[1]) / 2)
                            <= CHEVRON_POINT_TOL * 1.5
                    ):
                        axis = "horizontal"
                    elif (
                        abs(q[1] - r[1]) <= CHEVRON_POINT_TOL
                        and abs(q[0] - r[0]) > 2 * GEOM_TOL
                        and abs(apex[0] - (q[0] + r[0]) / 2)
                            <= CHEVRON_POINT_TOL * 1.5
                    ):
                        axis = "vertical"
                    if axis is None:
                        continue

                    base = ((q[0] + r[0]) / 2, (q[1] + r[1]) / 2)
                    key = (
                        axis,
                        round(apex[0] / CHEVRON_POINT_TOL),
                        round(apex[1] / CHEVRON_POINT_TOL),
                        round(base[0] / CHEVRON_POINT_TOL),
                        round(base[1] / CHEVRON_POINT_TOL),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    if axis == "horizontal":
                        direction = "right" if apex[0] > base[0] else "left"
                    else:
                        direction = "down" if apex[1] > base[1] else "up"
                    found.append({
                        "axis": axis,
                        "direction": direction,
                        "apex": apex,
                        "base": base,
                        "line_indexes": (first_index, second_index),
                    })
    return found


def _text_chevron_score(text: dict, chevron: dict) -> float | None:
    angle = int(round(text.get("angle", 0.0))) % 360
    axis = "horizontal" if angle in (0, 180) else "vertical"
    if chevron["axis"] != axis:
        return None

    apex = chevron["apex"]
    base = chevron["base"]
    size = max(float(text.get("size", 0.0)), 0.1)
    if axis == "horizontal":
        center = (text["y"] + text["y1"]) / 2
        cross_distance = abs((apex[1] + base[1]) / 2 - center)
        gap = max(
            text["x"] - max(apex[0], base[0]),
            min(apex[0], base[0]) - text["x1"],
            0.0,
        )
    else:
        center = (text["x"] + text["x1"]) / 2
        cross_distance = abs((apex[0] + base[0]) / 2 - center)
        gap = max(
            text["y"] - max(apex[1], base[1]),
            min(apex[1], base[1]) - text["y1"],
            0.0,
        )

    if cross_distance > max(0.45, size * 0.35):
        return None
    # Old Capture PDFs leave room for a separately rendered page reference
    # between the net text and port glyph. The newer PDFs place them together.
    if gap > max(1.0, size * 3.0):
        return None
    return gap + cross_distance


def _complete_chevron_line_indexes(
    selected: dict,
    chevrons: list[dict],
) -> list[int]:
    """Return every arm in one compact OrCAD port glyph."""
    indexes = set(selected["line_indexes"])
    frontier = [selected["base"]]
    seen = {tuple(selected["line_indexes"])}
    axis_index = 0 if selected["axis"] == "horizontal" else 1
    chevron_depth = abs(
        selected["apex"][axis_index] - selected["base"][axis_index]
    )
    join_tolerance = max(CHEVRON_POINT_TOL, chevron_depth * 0.5)
    while frontier:
        point = frontier.pop()
        for chevron in chevrons:
            key = tuple(chevron["line_indexes"])
            if (
                key in seen
                or chevron["axis"] != selected["axis"]
                or chevron["direction"] != selected["direction"]
                or not _points_close(
                    chevron["apex"],
                    point,
                    join_tolerance,
                )
            ):
                continue
            seen.add(key)
            indexes.update(chevron["line_indexes"])
            frontier.append(chevron["base"])

    # Bidirectional ports add an opposite-facing pair on the wire side.  It is
    # separated from the text-side chain by a small gap, but shares its axis
    # and centreline.  Grow the compact glyph interval without absorbing ports
    # on adjacent, closely stacked wires.
    cross_index = 1 - axis_index
    selected_cross = (
        selected["apex"][cross_index] + selected["base"][cross_index]
    ) / 2
    cross_tolerance = max(CHEVRON_POINT_TOL * 1.5, chevron_depth * 0.3)
    changed = True
    while changed:
        changed = False
        included = [
            chevron
            for chevron in chevrons
            if tuple(chevron["line_indexes"]) in seen
        ]
        interval_start = min(
            min(chevron["apex"][axis_index], chevron["base"][axis_index])
            for chevron in included
        )
        interval_end = max(
            max(chevron["apex"][axis_index], chevron["base"][axis_index])
            for chevron in included
        )
        for chevron in chevrons:
            key = tuple(chevron["line_indexes"])
            chevron_cross = (
                chevron["apex"][cross_index]
                + chevron["base"][cross_index]
            ) / 2
            chevron_start = min(
                chevron["apex"][axis_index],
                chevron["base"][axis_index],
            )
            chevron_end = max(
                chevron["apex"][axis_index],
                chevron["base"][axis_index],
            )
            interval_gap = max(
                interval_start - chevron_end,
                chevron_start - interval_end,
                0.0,
            )
            if (
                key in seen
                or chevron["axis"] != selected["axis"]
                or abs(chevron_cross - selected_cross) > cross_tolerance
                or interval_gap > chevron_depth
            ):
                continue
            seen.add(key)
            indexes.update(chevron["line_indexes"])
            changed = True
    return sorted(indexes)


def _page_reference_for_label(
    label_text: dict,
    direction: str,
    texts: list[dict],
) -> dict | None:
    """Find the red <page,...> annotation immediately beyond a port name."""
    size = max(float(label_text.get("size", 0.0)), 0.1)
    angle = int(round(label_text.get("angle", 0.0))) % 360
    horizontal = direction in ("left", "right")
    label_cross = (
        (label_text["y"] + label_text["y1"]) / 2
        if horizontal
        else (label_text["x"] + label_text["x1"]) / 2
    )
    candidates = []
    for text in texts:
        value = text.get("text", "").strip()
        if (
            text is label_text
            or text.get("color") != GLOBAL_LABEL_TEXT_COLOR
            or not GLOBAL_LABEL_PAGE_REFERENCE_RE.fullmatch(value)
            or int(round(text.get("angle", 0.0))) % 360 != angle
            or abs(float(text.get("size") or 0.0) - size) > size * 0.35
        ):
            continue
        text_cross = (
            (text["y"] + text["y1"]) / 2
            if horizontal
            else (text["x"] + text["x1"]) / 2
        )
        cross_distance = abs(text_cross - label_cross)
        if direction == "right":
            gap = text["x"] - label_text["x1"]
        elif direction == "left":
            gap = label_text["x"] - text["x1"]
        elif direction == "down":
            gap = text["y"] - label_text["y1"]
        else:
            gap = label_text["y"] - text["y1"]
        if (
            -GEOM_TOL <= gap <= max(1.5, size * 3.0)
            and cross_distance <= max(0.45, size * 0.35)
        ):
            candidates.append((max(gap, 0.0) + cross_distance, text))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def decode_global_labels(page_data: dict) -> list[dict]:
    """Decode OrCAD off-page ports from red text plus chevron geometry.

    Red text alone is insufficient: it is also used for page references,
    warnings, and, in newer Capture PDFs, some ordinary annotations. Off-page
    ports are distinguished by their adjacent 45-degree chevron glyph.
    """
    chevrons = _chevrons(page_data["lines"])
    labels = []
    seen = set()
    for text in page_data["texts"]:
        name = text.get("text", "").strip()
        if not name or text.get("color") != GLOBAL_LABEL_TEXT_COLOR:
            continue
        if GLOBAL_LABEL_PAGE_REFERENCE_RE.fullmatch(name):
            continue
        angle = int(round(text.get("angle", 0.0))) % 360
        if angle not in (0, 90, 180, 270):
            continue
        candidates = []
        for chevron in chevrons:
            score = _text_chevron_score(text, chevron)
            if score is not None:
                candidates.append((score, chevron))
        if not candidates:
            continue
        _, chevron = min(candidates, key=lambda item: item[0])
        key = (
            name,
            round(text["x"] / GEOM_TOL),
            round(text["y"] / GEOM_TOL),
            angle,
        )
        if key in seen:
            continue
        seen.add(key)
        line_indexes = _complete_chevron_line_indexes(
            chevron,
            chevrons,
        )
        glyph_points = [
            point
            for line_index in line_indexes
            for point in _line_points(page_data["lines"][line_index])
        ]
        if chevron["axis"] == "horizontal":
            hot_x = (
                min(point[0] for point in glyph_points)
                if chevron["direction"] == "right"
                else max(point[0] for point in glyph_points)
            )
            hotpoint = (hot_x, chevron["apex"][1])
        else:
            hot_y = (
                min(point[1] for point in glyph_points)
                if chevron["direction"] == "down"
                else max(point[1] for point in glyph_points)
            )
            hotpoint = (chevron["apex"][0], hot_y)
        page_reference = _page_reference_for_label(
            text,
            chevron["direction"],
            page_data["texts"],
        )
        labels.append({
            "name": name,
            "direction": chevron["direction"],
            "angle": angle,
            "apex": _point_dict(chevron["apex"]),
            "base": _point_dict(chevron["base"]),
            "hotpoint": _point_dict(hotpoint),
            "line_indexes": line_indexes,
            "page_references": (
                [{
                    "text": page_reference["text"],
                    "x": page_reference["x"],
                    "y": page_reference["y"],
                    "x1": page_reference["x1"],
                    "y1": page_reference["y1"],
                    "size": page_reference.get("size"),
                    "angle": page_reference.get("angle"),
                    "color": page_reference.get("color"),
                }]
                if page_reference else []
            ),
            "text": {
                "x": text["x"], "y": text["y"],
                "x1": text["x1"], "y1": text["y1"],
                "size": text.get("size"), "angle": text.get("angle"),
                "color": text.get("color"),
            },
        })
    labels.sort(key=lambda label: (
        label["text"]["y"], label["text"]["x"], label["name"],
    ))
    return labels


WORKSHEET_LABEL_ALIASES = {
    "title": {"title"},
    "size": {"size"},
    "document_number": {
        "document number", "document no", "doc no", "code-nr.", "km-nr.",
    },
    "revision": {"rev", "revision", "issue", "swv"},
    "date": {"date"},
    "sheet": {"sheet", "page"},
    "page_name": {"page name"},
    "of": {"of", "o f"},
}


def _worksheet_frame(page_data: dict) -> dict | None:
    """Find Capture's inset drawing frame without relying on a paper size."""
    width = page_data["width"]
    height = page_data["height"]
    horizontals = []
    verticals = []
    for index, line in enumerate(page_data["lines"]):
        if line.get("color") != "#000000":
            continue
        x0, x1 = sorted((line["x1"], line["x2"]))
        y0, y1 = sorted((line["y1"], line["y2"]))
        if y1 - y0 <= GEOM_TOL and x1 - x0 >= width * 0.80:
            horizontals.append((index, x0, x1, (y0 + y1) / 2))
        if x1 - x0 <= GEOM_TOL and y1 - y0 >= height * 0.80:
            verticals.append((index, (x0 + x1) / 2, y0, y1))

    tolerance = max(0.45, GEOM_TOL * 2)
    candidates = []
    for top in horizontals:
        if not 0.3 <= top[3] <= height * 0.10:
            continue
        for bottom in horizontals:
            if (
                # "Fit to page" exports can leave a sizeable unused bottom
                # margin while the frame itself still spans most of the page.
                bottom[3] <= height * 0.78
                or abs(top[1] - bottom[1]) > tolerance
                or abs(top[2] - bottom[2]) > tolerance
            ):
                continue
            for left in verticals:
                if (
                    abs(left[1] - top[1]) > tolerance
                    or abs(left[2] - top[3]) > tolerance
                    or abs(left[3] - bottom[3]) > tolerance
                ):
                    continue
                for right in verticals:
                    if (
                        abs(right[1] - top[2]) > tolerance
                        or abs(right[2] - top[3]) > tolerance
                        or abs(right[3] - bottom[3]) > tolerance
                    ):
                        continue
                    candidates.append({
                        "x0": top[1],
                        "y0": top[3],
                        "x1": top[2],
                        "y1": bottom[3],
                        "line_indexes": [
                            top[0], right[0], bottom[0], left[0],
                        ],
                    })
    if not candidates:
        return None
    # Some exporters draw both an outer trim border and the actual coordinate
    # frame.  The most inset qualifying rectangle is the worksheet frame.
    return min(
        candidates,
        key=lambda box: (
            (box["x1"] - box["x0"]) * (box["y1"] - box["y0"]),
            -box["x0"] - box["y0"],
        ),
    )


def _worksheet_label_name(value: str) -> str | None:
    normalized = re.sub(r"\s+", " ", value.strip().lower().rstrip(":"))
    for name, aliases in WORKSHEET_LABEL_ALIASES.items():
        if normalized in aliases:
            return name
    return None


def _worksheet_text_values(cell: dict, texts: list[dict]) -> list[str]:
    values = []
    seen = set()
    tolerance = max(0.35, GEOM_TOL * 2)
    for text in sorted(texts, key=lambda item: (item["x"], item["y"])):
        center = (
            (text["x"] + text["x1"]) / 2,
            (text["y"] + text["y1"]) / 2,
        )
        if not (
            cell["x0"] - tolerance <= center[0] <= cell["x1"] + tolerance
            and cell["y0"] - tolerance <= center[1] <= cell["y1"] + tolerance
        ):
            continue
        value = text.get("text", "").strip()
        key = (
            value,
            round(text["x"] / GEOM_TOL),
            round(text["y"] / GEOM_TOL),
        )
        if (
            not value
            or _worksheet_label_name(value) is not None
            or key in seen
        ):
            continue
        seen.add(key)
        values.append(value)
    return values


def _boxes_touch(first: dict, second: dict, tolerance: float) -> bool:
    return not (
        first["x1"] < second["x0"] - tolerance
        or second["x1"] < first["x0"] - tolerance
        or first["y1"] < second["y0"] - tolerance
        or second["y1"] < first["y0"] - tolerance
    )


def _primitive_bbox(primitive: dict) -> dict:
    if "points" in primitive:
        points = primitive["points"]
        return {
            "x0": min(point[0] for point in points),
            "y0": min(point[1] for point in points),
            "x1": max(point[0] for point in points),
            "y1": max(point[1] for point in points),
        }
    if "x2" in primitive:
        return {
            "x0": min(primitive["x1"], primitive["x2"]),
            "y0": min(primitive["y1"], primitive["y2"]),
            "x1": max(primitive["x1"], primitive["x2"]),
            "y1": max(primitive["y1"], primitive["y2"]),
        }
    if "x" in primitive:
        return {
            "x0": min(primitive["x"], primitive["x1"]),
            "y0": min(primitive["y"], primitive["y1"]),
            "x1": max(primitive["x"], primitive["x1"]),
            "y1": max(primitive["y"], primitive["y1"]),
        }
    return {
        "x0": min(primitive["x0"], primitive.get("x1", primitive["x0"])),
        "y0": min(primitive["y0"], primitive.get("y1", primitive["y0"])),
        "x1": max(primitive["x0"], primitive.get("x1", primitive["x0"])),
        "y1": max(primitive["y0"], primitive.get("y1", primitive["y0"])),
    }


def _worksheet_title_block(
    page_data: dict,
    frame: dict,
    anchors: list[tuple[str, dict]],
) -> tuple[dict | None, list[dict]]:
    """Find a variable-layout corner block from its cells or ruled lines."""
    frame_width = frame["x1"] - frame["x0"]
    frame_height = frame["y1"] - frame["y0"]
    tolerance = max(0.5, GEOM_TOL * 3)

    cells = []
    seen_cells = set()
    for rectangle in page_data["rectangles"]:
        if (
            rectangle.get("color") != "#000000"
            or rectangle["x0"] < frame["x0"] + frame_width * 0.35
            or rectangle["y0"] < frame["y0"] + frame_height * 0.60
            or rectangle["x1"] > frame["x1"] + tolerance
            or rectangle["y1"] > frame["y1"] + tolerance
        ):
            continue
        key = tuple(
            round(rectangle[name] / GEOM_TOL)
            for name in ("x0", "y0", "x1", "y1")
        )
        if key not in seen_cells:
            seen_cells.add(key)
            cells.append(rectangle)

    components = []
    for cell in cells:
        touching = [
            index
            for index, component in enumerate(components)
            if any(_boxes_touch(cell, other, tolerance) for other in component)
        ]
        if not touching:
            components.append([cell])
            continue
        merged = [cell]
        for index in reversed(touching):
            merged.extend(components.pop(index))
        components.append(merged)

    def anchor_count(component):
        return sum(
            any(
                cell["x0"] - tolerance <= (text["x"] + text["x1"]) / 2
                <= cell["x1"] + tolerance
                and cell["y0"] - tolerance <= (text["y"] + text["y1"]) / 2
                <= cell["y1"] + tolerance
                for cell in component
            )
            for _name, text in anchors
        )

    if components:
        best = max(
            components,
            key=lambda component: (anchor_count(component), len(component)),
        )
        if anchor_count(best) >= 2:
            return ({
                "x0": min(cell["x0"] for cell in best),
                "y0": min(cell["y0"] for cell in best),
                "x1": max(cell["x1"] for cell in best),
                "y1": max(cell["y1"] for cell in best),
            }, best)

    if len(anchors) < 2:
        return None, []

    anchor_left = min(text["x"] for _name, text in anchors)
    probe_left = anchor_left - frame_width * 0.02
    probe_top = frame["y0"] + frame_height * 0.65
    minimum_length = max(1.0, min(frame_width, frame_height) * 0.015)
    structural_lines = []
    for line in page_data["lines"]:
        bbox = _primitive_bbox(line)
        if (
            line.get("color") == "#000000"
            and _line_length(line) >= minimum_length
            and bbox["x0"] >= probe_left
            and bbox["y0"] >= probe_top
            and bbox["x1"] <= frame["x1"] + tolerance
            and bbox["y1"] <= frame["y1"] + tolerance
        ):
            structural_lines.append(bbox)
    if structural_lines:
        return ({
            "x0": min(box["x0"] for box in structural_lines),
            "y0": min(box["y0"] for box in structural_lines),
            "x1": max(box["x1"] for box in structural_lines),
            "y1": max(box["y1"] for box in structural_lines),
        }, [])

    return ({
        "x0": max(frame["x0"], probe_left),
        "y0": max(
            frame["y0"],
            min(text["y"] for _name, text in anchors) - frame_height * 0.08,
        ),
        "x1": frame["x1"],
        "y1": frame["y1"],
    }, [])


def _is_worksheet_primitive(
    primitive: dict,
    frame: dict,
    title_block: dict | None,
) -> bool:
    bbox = _primitive_bbox(primitive)
    # Text bounding boxes commonly overhang the ruled cell/frame by almost a
    # millimetre even though their baselines are inside it.
    tolerance = max(2.0, GEOM_TOL * 10)
    in_border_band = (
        bbox["x1"] <= frame["x0"] + tolerance
        or bbox["x0"] >= frame["x1"] - tolerance
        or bbox["y1"] <= frame["y0"] + tolerance
        or bbox["y0"] >= frame["y1"] - tolerance
    )
    center = (
        (bbox["x0"] + bbox["x1"]) / 2,
        (bbox["y0"] + bbox["y1"]) / 2,
    )
    in_title_block = (
        title_block is not None
        and title_block["x0"] - tolerance <= center[0]
        <= title_block["x1"] + tolerance
        and title_block["y0"] - tolerance <= center[1]
        <= title_block["y1"] + tolerance
    )
    return in_border_band or in_title_block


def _unique_worksheet_texts(texts: list[dict]) -> list[dict]:
    unique = []
    seen = set()
    for text in texts:
        key = (
            text.get("text", "").strip(),
            round(text["x"] / GEOM_TOL),
            round(text["y"] / GEOM_TOL),
            int(round(text.get("angle", 0.0))) % 360,
        )
        if key not in seen:
            seen.add(key)
            unique.append(text)
    return unique


def _worksheet_fields(
    texts: list[dict],
    anchors: list[tuple[str, dict]],
    title_block: dict | None,
    cells: list[dict],
) -> dict:
    if title_block is None:
        return {}
    tolerance = max(0.5, GEOM_TOL * 3)
    block_texts = [
        text for text in texts
        if _is_worksheet_primitive(
            text,
            {
                "x0": -1e9, "y0": -1e9,
                "x1": 1e9, "y1": 1e9,
            },
            title_block,
        )
    ]
    anchors_by_name = {}
    for name, text in anchors:
        anchors_by_name.setdefault(name, []).append(text)

    def center(text):
        return (
            (text["x"] + text["x1"]) / 2,
            (text["y"] + text["y1"]) / 2,
        )

    def containing_cells(anchor):
        point = center(anchor)
        return sorted(
            (
                cell for cell in cells
                if (
                    cell["x0"] - tolerance <= point[0] <= cell["x1"] + tolerance
                    and cell["y0"] - tolerance <= point[1]
                    <= cell["y1"] + tolerance
                )
            ),
            key=lambda cell: (
                (cell["x1"] - cell["x0"]) * (cell["y1"] - cell["y0"])
            ),
        )

    def nearby_values(name, validator=None):
        candidates = []
        for anchor in anchors_by_name.get(name, []):
            for cell in containing_cells(anchor):
                values = _worksheet_text_values(cell, block_texts)
                if validator:
                    values = [value for value in values if validator(value)]
                if values:
                    return values
            anchor_center = center(anchor)
            size = max(float(anchor.get("size") or 0), 1.0)
            for text in block_texts:
                value = text.get("text", "").strip()
                if (
                    not value
                    or _worksheet_label_name(value) is not None
                    or (validator and not validator(value))
                ):
                    continue
                text_center = center(text)
                dx = text["x"] - anchor["x"]
                dy = text_center[1] - anchor_center[1]
                if (
                    dx >= -tolerance
                    and -size <= dy <= max(size * 4, (
                        title_block["y1"] - title_block["y0"]
                    ) * 0.24)
                ):
                    candidates.append((
                        abs(dy) * 3 + max(dx, 0),
                        value,
                    ))
        return [min(candidates)[1]] if candidates else []

    fields = {}
    title_values = nearby_values("title")
    if title_values:
        fields["title"] = max(title_values, key=len)
    size_values = nearby_values(
        "size",
        lambda value: re.fullmatch(
            r"(?:A[0-5]|[A-E])",
            value.upper(),
        ) is not None,
    )
    if size_values:
        fields["size"] = size_values[0].upper()
    document_values = nearby_values("document_number")
    if document_values:
        fields["document_number"] = max(document_values, key=len)
    revision_values = nearby_values("revision")
    if revision_values:
        fields["revision"] = min(revision_values, key=len)
    date_values = nearby_values("date")
    if date_values:
        fields["date"] = max(date_values, key=len)
    page_name_values = nearby_values("page_name")
    if page_name_values:
        fields["page_name"] = max(page_name_values, key=len)

    title_anchors = anchors_by_name.get("title", [])
    if title_anchors:
        title_y = min(center(anchor)[1] for anchor in title_anchors)
        company_candidates = [
            text.get("text", "").strip()
            for text in block_texts
            if (
                text.get("text", "").strip()
                and _worksheet_label_name(text.get("text", "")) is None
                and center(text)[1] < title_y - tolerance
                and not re.fullmatch(
                    r"[A-Z0-9]",
                    text.get("text", "").strip(),
                )
            )
        ]
        if company_candidates:
            fields["company"] = max(company_candidates, key=len)

    page_pattern = re.compile(
        r"(?:page|sheet)\s*:?\s*(\d+)\s*"
        r"(?:of|o\s*f)\s*(\d+)",
        re.IGNORECASE,
    )
    page_text = None
    for text in block_texts:
        match = page_pattern.fullmatch(text.get("text", "").strip())
        if match:
            fields["sheet"], fields["sheet_count"] = match.groups()
            page_text = text
            break

    if "sheet" not in fields:
        for anchor in anchors_by_name.get("sheet", []):
            anchor_center = center(anchor)
            size = max(float(anchor.get("size") or 0), 1.0)
            numbers = sorted(
                (
                    (center(text)[0], text.get("text", "").strip())
                    for text in block_texts
                    if (
                        re.fullmatch(r"\d+", text.get("text", "").strip())
                        and center(text)[0] >= anchor_center[0] - tolerance
                        and abs(center(text)[1] - anchor_center[1])
                        <= size * 1.5
                    )
                ),
            )
            if numbers:
                fields["sheet"] = numbers[0][1]
            if len(numbers) >= 2:
                fields["sheet_count"] = numbers[-1][1]
            if numbers:
                break

    # Some older line-only blocks put an unlabeled revision immediately after
    # a combined "PAGE n OF m" span.
    if "revision" not in fields and page_text is not None:
        page_center = center(page_text)
        trailing = [
            text.get("text", "").strip()
            for text in block_texts
            if (
                text["x"] >= page_text["x1"] - tolerance
                and abs(center(text)[1] - page_center[1])
                <= max(float(page_text.get("size") or 0), 1.0)
                and re.fullmatch(
                    r"[A-Za-z]?\d+(?:\.\d+)*",
                    text.get("text", "").strip(),
                )
            )
        ]
        if trailing:
            fields["revision"] = min(trailing, key=len)
    return fields


def decode_worksheet(page_data: dict) -> dict | None:
    """Decode and identify an OrCAD worksheet for native KiCad replacement."""
    frame = _worksheet_frame(page_data)
    if frame is None:
        return None

    frame_width = frame["x1"] - frame["x0"]
    frame_height = frame["y1"] - frame["y0"]
    unique_texts = _unique_worksheet_texts(page_data["texts"])
    anchors = [
        (name, text)
        for text in unique_texts
        if (
            text["x"] >= frame["x0"] + frame_width * 0.35
            and text["y"] >= frame["y0"] + frame_height * 0.60
            and (name := _worksheet_label_name(text.get("text", "")))
            is not None
        )
    ]
    title_block, cells = _worksheet_title_block(
        page_data,
        frame,
        anchors,
    )
    fields = _worksheet_fields(
        unique_texts,
        anchors,
        title_block,
        cells,
    )

    line_indexes = {
        index
        for index, line in enumerate(page_data["lines"])
        if _is_worksheet_primitive(line, frame, title_block)
    }
    line_indexes.update(frame["line_indexes"])
    rectangle_indexes = {
        index
        for index, rectangle in enumerate(page_data["rectangles"])
        if (
            _is_worksheet_primitive(rectangle, frame, title_block)
            or (
                rectangle.get("fill") == "#ffffff"
                and rectangle["x0"] <= frame["x0"]
                and rectangle["y0"] <= frame["y0"]
                and rectangle["x1"] >= frame["x1"]
                and rectangle["y1"] >= frame["y1"]
            )
        )
    }
    curve_indexes = {
        index
        for index, curve in enumerate(page_data["curves"])
        if _is_worksheet_primitive(curve, frame, title_block)
    }
    text_indexes = {
        index
        for index, text in enumerate(page_data["texts"])
        if _is_worksheet_primitive(text, frame, title_block)
    }

    return {
        "frame": {
            name: _round_coord(frame[name])
            for name in ("x0", "y0", "x1", "y1")
        },
        "title_block": (
            {
                name: _round_coord(title_block[name])
                for name in ("x0", "y0", "x1", "y1")
            }
            if title_block is not None else None
        ),
        "fields": fields,
        "line_indexes": sorted(line_indexes),
        "rectangle_indexes": sorted(rectangle_indexes),
        "curve_indexes": sorted(curve_indexes),
        "text_indexes": sorted(text_indexes),
    }


def decode_page(page_data: dict) -> dict:
    """Infer schematic objects from OrCAD PDF drawing primitives."""
    components = []
    for bbox in _find_body_rectangles(page_data["lines"], page_data["rectangles"]):
        ref = _nearest_reference(bbox, page_data["texts"])
        pins = _pins_for_body(bbox, page_data["lines"], page_data["texts"], ref)
        promoted_pin_names = _promote_numeric_pin_names(pins)
        components.append({
            "reference": ref["text"] if ref else None,
            "reference_text": ref,
            "promoted_pin_names_to_numbers": promoted_pin_names,
            "bbox": {
                "x0": _round_coord(bbox["x0"]),
                "y0": _round_coord(bbox["y0"]),
                "x1": _round_coord(bbox["x1"]),
                "y1": _round_coord(bbox["y1"]),
            },
            "pins": pins,
        })

    components.sort(key=lambda c: (
        c["reference"] is None,
        c["reference"] or "",
        c["bbox"]["y0"],
        c["bbox"]["x0"],
    ))
    wires = _decode_wires(page_data["lines"])
    global_labels = decode_global_labels(page_data)
    worksheet = decode_worksheet(page_data)
    return {
        "components": components,
        "wires": wires,
        "global_labels": global_labels,
        "worksheet": worksheet,
        "summary": {
            "components": len(components),
            "pins": sum(len(c["pins"]) for c in components),
            "wires": len(wires),
            "global_labels": len(global_labels),
            "worksheet": worksheet is not None,
        },
    }


def extract_drawings(page, raw: bool):
    """Extract all drawing primitives from a page.

    Returns (lines, curves, rectangles) where:
      lines: [{"x1", "y1", "x2", "y2", "color", "width"}, ...]
      curves: [{"points": [[x,y],...], "color", "width"}, ...]
      rectangles: [{"x0", "y0", "x1", "y1", "color", "fill", "width"}, ...]
    """
    conv = (lambda v: round(v, 3)) if raw else pt2mm
    lines = []
    curves = []
    rectangles = []

    for d in page.get_drawings():
        color = _color_hex(d.get('color'))
        fill = _color_hex(d.get('fill'))
        width = d.get('width') or 0

        for item in d.get('items', []):
            kind = item[0]

            if kind == 'l':
                p1, p2 = item[1], item[2]
                x1, y1 = _pt_xy(p1)
                x2, y2 = _pt_xy(p2)
                if abs(x1 - x2) < 0.01 and abs(y1 - y2) < 0.01:
                    continue
                lines.append({
                    "x1": conv(x1), "y1": conv(y1),
                    "x2": conv(x2), "y2": conv(y2),
                    "color": color,
                    "width": round(width * PT_TO_MM, 3) if not raw else round(width, 3),
                })

            elif kind == 'c':
                pts = [_pt_xy(p) for p in item[1:]]
                if len(pts) == 4:
                    interp = _bezier_points(pts[0], pts[1], pts[2], pts[3])
                    curves.append({
                        "points": [[conv(x), conv(y)] for x, y in interp],
                        "control_points": [[conv(x), conv(y)] for x, y in pts],
                        "color": color,
                        "fill": fill,
                        "width": round(width * PT_TO_MM, 3) if not raw else round(width, 3),
                    })
                elif len(pts) >= 2:
                    curves.append({
                        "points": [[conv(x), conv(y)] for x, y in pts],
                        "color": color,
                        "fill": fill,
                        "width": round(width * PT_TO_MM, 3) if not raw else round(width, 3),
                    })

            elif kind == 're':
                rx0, ry0, rx1, ry1 = _rect_xywh(item[1])
                rectangles.append({
                    "x0": conv(rx0), "y0": conv(ry0),
                    "x1": conv(rx1), "y1": conv(ry1),
                    "color": color,
                    "fill": fill,
                    "width": round(width * PT_TO_MM, 3) if not raw else round(width, 3),
                })

    return lines, curves, rectangles


def extract_texts(page, raw: bool):
    """Extract all text spans from a page.

    Returns [{"text", "x", "y", "x1", "y1", "size", "angle", "color", "font"}, ...]
    """
    conv = (lambda v: round(v, 3)) if raw else pt2mm
    td = page.get_text("dict")
    texts = []

    for block in td.get('blocks', []):
        if 'lines' not in block:
            continue
        for line in block['lines']:
            line_dir = line.get('dir', (1.0, 0.0))
            for span in line.get('spans', []):
                txt = span['text'].strip()
                if not txt:
                    continue
                bbox = span.get('bbox', [0, 0, 0, 0])
                angle = _span_angle({'dir': line_dir})
                texts.append({
                    "text": txt,
                    "x": conv(bbox[0]),
                    "y": conv(bbox[1]),
                    "x1": conv(bbox[2]),
                    "y1": conv(bbox[3]),
                    "size": round(span.get('size', 0) * PT_TO_MM, 2) if not raw else round(span.get('size', 0), 2),
                    "angle": angle,
                    "color": _color_int_hex(span.get('color', 0)),
                    "font": span.get('font', ''),
                })

    return texts


def dump_page(page, raw: bool, decode: bool = False) -> dict:
    """Extract all vector elements from a single page."""
    lines, curves, rectangles = extract_drawings(page, raw)
    texts = extract_texts(page, raw)

    pw, ph = page.rect.width, page.rect.height
    conv = (lambda v: round(v, 3)) if raw else pt2mm

    result = {
        "width": conv(pw),
        "height": conv(ph),
        "lines": lines,
        "curves": curves,
        "rectangles": rectangles,
        "texts": texts,
    }
    if decode:
        result["decoded"] = decode_page(result)
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Dump vector lines, curves, rectangles, and text from a PDF as JSON.")
    parser.add_argument("pdf", help="Input PDF file")
    parser.add_argument("--page", type=int, default=None,
                        help="Dump only this page (1-based)")
    parser.add_argument("--raw", action="store_true",
                        help="Output coordinates in PDF points instead of millimeters")
    parser.add_argument("--compact", action="store_true",
                        help="Compact JSON output (no indentation)")
    parser.add_argument("--decode", action="store_true",
                        help="Infer symbol bodies, pins, wires, and global labels")
    args = parser.parse_args()
    if args.decode and args.raw:
        parser.error("--decode uses millimeter heuristics; omit --raw")

    doc = fitz.open(args.pdf)

    if args.page is not None:
        if args.page < 1 or args.page > len(doc):
            print(f"Page {args.page} out of range (1-{len(doc)})",
                  file=sys.stderr)
            sys.exit(1)
        page = doc[args.page - 1]
        result = {
            "file": args.pdf,
            "total_pages": len(doc),
            "units": "pt" if args.raw else "mm",
            "pages": [
                {"page": args.page, **dump_page(page, args.raw, args.decode)},
            ],
        }
    else:
        pages = []
        for i in range(len(doc)):
            pages.append({"page": i + 1, **dump_page(doc[i], args.raw, args.decode)})
        result = {
            "file": args.pdf,
            "total_pages": len(doc),
            "units": "pt" if args.raw else "mm",
            "pages": pages,
        }

    indent = None if args.compact else 2
    json.dump(result, sys.stdout, indent=indent)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
