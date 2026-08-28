#!/usr/bin/env python3
# Copyright (C) 2026 Andrei Errapart
# SPDX-License-Identifier: GPL-2.0-or-later
"""Convert an OrCAD-generated schematic PDF into an editable KiCad project.

The converter deliberately has two layers:

* semantic objects (wires, symbols, pins, and labels) are reconstructed from
  the color and geometry conventions decoded by :mod:`pdf_dump`;
* PDF vectors and text which were not consumed by a semantic object are kept
  as KiCad page graphics, preserving note pages and annotations.

PDF is a presentation format, so hidden fields and pin metadata cannot always
be recovered.  The generated project records that limitation in its title and
uses deterministic generic symbols where the PDF does not identify a library
part.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print(
        "PyMuPDF not found. Install with: pip install PyMuPDF",
        file=sys.stderr,
    )
    sys.exit(1)

import pdf_dump


SCRIPT_DIR = Path(__file__).resolve().parent
KICAD_VERSION = 20260306
BODY_COLOR = pdf_dump.BODY_COLOR
PIN_COLOR = pdf_dump.PIN_COLOR
PIN_NAME_COLOR = pdf_dump.PIN_NAME_COLOR
WIRE_COLOR = pdf_dump.WIRE_COLOR
BUS_COLOR = "#0000ff"
REF_RE = pdf_dump.REF_RE
GEOM_TOL = pdf_dump.GEOM_TOL
BODY_CURVE_JOIN_TOL = 0.06
BODY_HALF_JOIN_TOL = 1.5
TRANSISTOR_GATE_JOIN_TOL = 0.55
REFERENCE_DIRECTION_TOL = 0.8
JUNCTION_COLOR = "#ff0000"
NO_CONNECT_COLOR = "#803f00"
# KiCad applies this scale internally when it renders an outline font.  OrCAD
# uses an Arial-compatible outline font in the PDFs handled by this converter,
# so divide the PDF em size before emitting a KiCad `(size ...)`.
KICAD_OUTLINE_FONT_COMPENSATION = 1.4
ORCAD_TEXT_FACE = "Arial"
PIN_LENGTH = 2.54
PIN_TEXT_SIZE = 1.27
DNP_SUFFIX_RE = re.compile(r"\s*\*DNP\s*$", re.IGNORECASE)
LABEL_RE = re.compile(r"^[A-Za-z_+][A-Za-z0-9_./+#\-\[\]<>:]*$")
# Altium net and rail names may begin with a digit ("1.1V_LPDDR4", "3.3V").
ALTIUM_NET_NAME_RE = re.compile(r"^[A-Za-z0-9_+][A-Za-z0-9_./+#\-\[\]<>:]*$")
GLOBAL_LABEL_PAGE_REFERENCE_RE = pdf_dump.GLOBAL_LABEL_PAGE_REFERENCE_RE
MECHANICAL_REF_RE = re.compile(r"^(?:SCR|SP)\d+[A-Z]?$")
MULTI_UNIT_REF_RE = re.compile(r"^([A-Z]{1,4}\d+)([A-Z])$")
MERGED_PASSIVE_PREFIX_RE = re.compile(r"^(FB|R|C|L)(\d.*)$", re.IGNORECASE)
PAPER_SCALES = {
    # Capture's standard "fit to A4" print factors.  A3 is reduced to 70%,
    # rather than the mathematically exact sqrt(1/2), in the supplied PDFs.
    "A0": 4.0,
    "A1": 20.0 / 7.0,
    "A2": 2.0,
    "A3": 10.0 / 7.0,
    "A4": 1.0,
}
PAPER_SIZES = {
    "A0": (1189.0, 841.0),
    "A1": (841.0, 594.0),
    "A2": (594.0, 420.0),
    "A3": (420.0, 297.0),
    "A4": (297.0, 210.0),
}
PASSIVE_PACKAGE_ALIASES = {
    "01005": "01005",
    "0201": "0201",
    "0402": "0402",
    "0603": "0603",
    "0805": "0805",
    "1005": "0402",
    "1206": "1206",
    "1210": "1210",
    "1608": "0603",
    "1812": "1812",
    "2010": "2010",
    "2012": "0805",
    "2512": "2512",
    "3216": "1206",
    "3225": "1210",
    "4532": "1812",
    "5025": "2010",
    "6332": "2512",
}
PASSIVE_FOOTPRINT_SUFFIXES = {
    "01005": "01005_0402Metric",
    "0201": "0201_0603Metric",
    "0402": "0402_1005Metric",
    "0603": "0603_1608Metric",
    "0805": "0805_2012Metric",
    "1206": "1206_3216Metric",
    "1210": "1210_3225Metric",
    "1812": "1812_4532Metric",
    "2010": "2010_5025Metric",
    "2512": "2512_6332Metric",
}
PASSIVE_FOOTPRINT_FAMILIES = {
    "R": ("Resistor_SMD", "R"),
    "C": ("Capacitor_SMD", "C"),
    "L": ("Inductor_SMD", "L"),
    "FB": ("Inductor_SMD", "L"),
}
STANDARD_PASSIVE_LIB_IDS = {
    "R": "Device:R",
    "C": "Device:C",
    "L": "Device:L",
    "FB": "Device:FerriteBead",
}
STANDARD_TESTPOINT_LIB_ID = "Connector:TestPoint"
STANDARD_POWER_NAMES = (
    "+10V", "+12C", "+12L", "+12LF", "+12P", "+12V", "+12VA",
    "+15V", "+1V0", "+1V1", "+1V2", "+1V35", "+1V5", "+1V8",
    "+24V", "+28V", "+2V5", "+2V8", "+3.3V", "+3.3VA", "+3.3VADC",
    "+3.3VDAC", "+3.3VP", "+36V", "+3V0", "+3V3", "+3V8", "+48V",
    "+4V", "+5C", "+5F", "+5P", "+5V", "+5VA", "+5VD", "+5VL",
    "+5VP", "+6V", "+7.5V", "+8V", "+9V", "+9VA", "+BATT", "+VDC",
    "+VSW", "-10V", "-12V", "-12VA", "-15V", "-24V", "-2V5", "-36V",
    "-3V3", "-48V", "-5V", "-5VA", "-6V", "-8V", "-9V", "-9VA",
    "-BATT", "-VDC", "-VSW", "AC", "Earth", "Earth_Clean",
    "Earth_Protective", "GND", "GND1", "GND2", "GND3", "GNDA", "GNDD",
    "GNDPWR", "GNDREF", "GNDS", "HT", "LINE", "NEUT", "PRI_HI",
    "PRI_LO", "PRI_MID", "PWR_FLAG", "VAA", "VAC", "VBUS", "VCC",
    "VCCQ", "VCOM", "VD", "VDC", "VDD", "VDDA", "VDDF", "VEE",
    "VMEM", "VPP", "VS", "VSS", "VSSA", "Vdrive",
)
STANDARD_POWER_NAME_MAP = {
    name.upper(): name for name in STANDARD_POWER_NAMES
}
STANDARD_PASSIVE_PIN_OFFSET = 3.81
_KICAD_STANDARD_SYMBOLS: dict[str, str] = {}


def _esc(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _safe_library_symbol_name(value) -> str:
    name = re.sub(r"[^A-Za-z0-9_]+", "_", str(value))
    return name or "Symbol"


def _text_key(text: dict) -> tuple:
    return (
        text.get("text"),
        text.get("x"),
        text.get("y"),
        text.get("x1"),
        text.get("y1"),
        text.get("angle"),
    )


def _point(line: dict, end: int) -> tuple[float, float]:
    return (line[f"x{end}"], line[f"y{end}"])


def _distance(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _point_close(a, b, tolerance=GEOM_TOL) -> bool:
    return _distance(a, b) <= tolerance


def _point_in_bbox(point, bbox: dict, margin=0.0) -> bool:
    return (
        bbox["x0"] - margin <= point[0] <= bbox["x1"] + margin
        and bbox["y0"] - margin <= point[1] <= bbox["y1"] + margin
    )


def _bbox_for_lines(lines: list[dict]) -> dict:
    xs = [value for line in lines for value in (line["x1"], line["x2"])]
    ys = [value for line in lines for value in (line["y1"], line["y2"])]
    return {"x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys)}


def _bbox_for_rectangle(rectangle: dict) -> dict:
    return {
        "x0": rectangle["x0"],
        "y0": rectangle["y0"],
        "x1": rectangle["x1"],
        "y1": rectangle["y1"],
    }


def _bbox_union(*boxes: dict) -> dict:
    return {
        "x0": min(box["x0"] for box in boxes),
        "y0": min(box["y0"] for box in boxes),
        "x1": max(box["x1"] for box in boxes),
        "y1": max(box["y1"] for box in boxes),
    }


def _bbox_distance(a: dict, b: dict) -> float:
    dx = max(a["x0"] - b["x1"], b["x0"] - a["x1"], 0.0)
    dy = max(a["y0"] - b["y1"], b["y0"] - a["y1"], 0.0)
    return math.hypot(dx, dy)


def _text_bbox(text: dict) -> dict:
    return {
        "x0": min(text["x"], text["x1"]),
        "y0": min(text["y"], text["y1"]),
        "x1": max(text["x"], text["x1"]),
        "y1": max(text["y"], text["y1"]),
    }


def _line_matches_pin(line: dict, pin: dict) -> bool:
    p0, p1 = _point(line, 1), _point(line, 2)
    hot = (pin["hot"]["x"], pin["hot"]["y"])
    other = (pin["other"]["x"], pin["other"]["y"])
    return (
        _point_close(p0, hot) and _point_close(p1, other)
    ) or (
        _point_close(p1, hot) and _point_close(p0, other)
    )


def _point_to_segment(point, a, b) -> tuple[float, tuple[float, float]]:
    dx, dy = b[0] - a[0], b[1] - a[1]
    denom = dx * dx + dy * dy
    if denom == 0:
        return _distance(point, a), a
    t = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / denom
    t = max(0.0, min(1.0, t))
    nearest = (a[0] + t * dx, a[1] + t * dy)
    return _distance(point, nearest), nearest


def _point_on_any_wire(point, wires: list[dict], tolerance=GEOM_TOL):
    best = None
    for wire in wires:
        a = (wire["start"]["x"], wire["start"]["y"])
        b = (wire["end"]["x"], wire["end"]["y"])
        dist, nearest = _point_to_segment(point, a, b)
        if dist <= tolerance and (best is None or dist < best[0]):
            best = (dist, nearest, wire)
    return best


def _nearest_wire_point(points, wires: list[dict], max_distance: float):
    best = None
    for point in points:
        for wire in wires:
            a = (wire["start"]["x"], wire["start"]["y"])
            b = (wire["end"]["x"], wire["end"]["y"])
            dist, nearest = _point_to_segment(point, a, b)
            if dist <= max_distance and (best is None or dist < best[0]):
                best = (dist, nearest, wire)
    return best


def _curve_bbox(curve: dict) -> dict:
    points = curve["points"]
    return {
        "x0": min(point[0] for point in points),
        "y0": min(point[1] for point in points),
        "x1": max(point[0] for point in points),
        "y1": max(point[1] for point in points),
    }


def _bboxes_touch(first: dict, second: dict, tolerance: float) -> bool:
    return not (
        first["x1"] < second["x0"] - tolerance
        or second["x1"] < first["x0"] - tolerance
        or first["y1"] < second["y0"] - tolerance
        or second["y1"] < first["y0"] - tolerance
    )


def _wire_degree(point, wires: list[dict], tolerance=GEOM_TOL) -> int:
    """Return the number of wire branches incident at *point*.

    A point in the middle of a segment contributes two branches while a
    segment endpoint contributes one.  This also handles a T whose main wire
    was not split by the PDF producer.
    """
    degree = 0
    for wire in wires:
        start = (wire["start"]["x"], wire["start"]["y"])
        end = (wire["end"]["x"], wire["end"]["y"])
        distance, _nearest = _point_to_segment(point, start, end)
        if distance > tolerance:
            continue
        if (
            _point_close(point, start, tolerance)
            or _point_close(point, end, tolerance)
        ):
            degree += 1
        else:
            degree += 2
    return degree


def _looks_like_passive_value(value: str) -> bool:
    """Return whether *value* can be a passive value fused to a reference."""
    if not re.match(r"^(?:\d+(?:\.\d*)?|\.\d+)", value):
        return False
    return bool(
        "/" in value
        or "%" in value
        or re.search(r"[pnumkKMGR](?:Ω|ohm)?(?:\b|$)", value)
    )


def _split_merged_reference_values(page: dict) -> None:
    """Split Capture text spans such as ``R13110K/0603`` and ``TP21TP_PAD``.

    PyMuPDF can coalesce adjacent reference and value fields into one span.
    References elsewhere on the page disambiguate the run of digits: on a
    page containing R133--R138, ``R13110K/0603`` is R131 plus 10K/0603,
    rather than R13 plus 110K/0603.

    The reference replaces the original item and the value is appended.  That
    preserves indexes already recorded by ``pdf_dump.decode_page``.
    """
    texts = page.get("texts", [])
    known: dict[str, list[int]] = {}
    for text in texts:
        match = re.fullmatch(
            r"(FB|R|C|L)(\d+)[A-Z]?",
            text.get("text", "").strip(),
            re.IGNORECASE,
        )
        if match:
            known.setdefault(match.group(1).upper(), []).append(
                int(match.group(2))
            )

    additions = []
    for index, text in enumerate(list(texts)):
        original = text.get("text", "").strip()
        if (
            text.get("color") != "#000000"
            or REF_RE.fullmatch(original)
        ):
            continue
        if page.get("flavor") == "altium":
            # llPDFLib merges horizontally adjacent spans: a designator with
            # its value ("R117 0R") or two designators ("TP14TP15").
            parts = None
            space_match = re.fullmatch(r"(\S+)\s+(\S.*)", original)
            if space_match and REF_RE.fullmatch(space_match.group(1)):
                parts = space_match.groups()
            else:
                pair_match = re.fullmatch(
                    r"(TP\d+[A-Z]?)(TP\d+[A-Z]?)", original
                )
                if pair_match:
                    parts = pair_match.groups()
            if parts:
                first, second = parts
                first_text = {**text, "text": first}
                second_text = {**text, "text": second}
                split_fraction = len(first) / len(original)
                if int(round(text.get("angle", 0))) % 180 == 0:
                    split = (
                        text["x"] + (text["x1"] - text["x"]) * split_fraction
                    )
                    first_text["x1"] = split
                    second_text["x"] = split
                else:
                    split = (
                        text["y"] + (text["y1"] - text["y"]) * split_fraction
                    )
                    first_text["y1"] = split
                    second_text["y"] = split
                texts[index] = first_text
                additions.append(second_text)
                continue
        testpoint_match = re.fullmatch(
            r"(TP\d+[A-Z]?)(TP_PAD)",
            original,
            re.IGNORECASE,
        )
        if testpoint_match:
            reference, value = testpoint_match.groups()
            reference_text = {**text, "text": reference}
            value_text = {**text, "text": value}
            split_fraction = len(reference) / len(original)
            if int(round(text.get("angle", 0))) % 180 == 0:
                split = text["x"] + (text["x1"] - text["x"]) * split_fraction
                reference_text["x1"] = split
                value_text["x"] = split
            else:
                split = text["y"] + (text["y1"] - text["y"]) * split_fraction
                reference_text["y1"] = split
                value_text["y"] = split
            texts[index] = reference_text
            additions.append(value_text)
            continue
        match = MERGED_PASSIVE_PREFIX_RE.match(original)
        if not match:
            continue
        prefix, tail = match.group(1).upper(), match.group(2)
        digit_count = len(tail) - len(tail.lstrip("0123456789"))
        candidates = []
        reference_numbers = known.get(prefix, [])
        digit_lengths = Counter(
            len(str(number)) for number in reference_numbers
        )
        modal_length = (
            min(
                digit_lengths,
                key=lambda length: (-digit_lengths[length], length),
            )
            if digit_lengths else None
        )
        for split_at in range(1, digit_count):
            reference_digits = tail[:split_at]
            passive_value = tail[split_at:]
            if not _looks_like_passive_value(passive_value):
                continue
            reference_number = int(reference_digits)
            length_penalty = (
                abs(len(reference_digits) - modal_length) * 1000
                if modal_length is not None else 0
            )
            distance_penalty = (
                min(
                    abs(reference_number - known_number)
                    for known_number in reference_numbers
                )
                if reference_numbers else 0
            )
            value_number = re.match(
                r"^(?:\d+(?:\.\d*)?|\.\d+)",
                passive_value,
            ).group(0)
            value_penalty = 0
            if (
                value_number == "0"
                and len(passive_value) > 1
                and passive_value[1].isalpha()
            ):
                value_penalty += 100
            value_penalty += max(
                0,
                len(value_number.split(".", 1)[0].lstrip("0")) - 3,
            )
            candidates.append(
                (
                    length_penalty,
                    distance_penalty,
                    value_penalty,
                    -len(reference_digits),
                    f"{prefix}{reference_digits}",
                    passive_value,
                )
            )
        if not candidates:
            continue
        *_score, reference, passive_value = min(candidates)
        texts[index] = {**text, "text": reference}
        additions.append({**text, "text": passive_value})
        known.setdefault(prefix, []).append(int(reference[len(prefix):]))
    texts.extend(additions)


@dataclass(frozen=True)
class CoordinateTransform:
    paper: str
    scale: float
    origin: float

    def value(self, coordinate: float) -> float:
        return round((coordinate - self.origin) * self.scale, 2)

    def delta(self, distance: float) -> float:
        return round(distance * self.scale, 2)

    def xy(self, x: float, y: float) -> tuple[float, float]:
        return self.value(x), self.value(y)


class UuidFactory:
    def __init__(self, seed: bytes):
        self.namespace = uuid.uuid5(
            uuid.NAMESPACE_URL, "pdf2kicad:" + hashlib.sha256(seed).hexdigest()
        )
        self.index = 0

    def new(self, kind="object") -> str:
        self.index += 1
        return str(uuid.uuid5(self.namespace, f"{kind}:{self.index}"))


def _paper_for_page_size(page: dict) -> str:
    """The standard paper whose landscape size best matches the PDF page."""
    width = page.get("width") or 0.0
    height = page.get("height") or 0.0
    return min(
        PAPER_SIZES,
        key=lambda paper: (
            abs(PAPER_SIZES[paper][0] - width)
            + abs(PAPER_SIZES[paper][1] - height)
        ),
    )


def detect_paper(pages: list[dict], requested: str) -> str:
    if requested != "auto":
        return requested
    if pages and all(page.get("flavor") == "altium" for page in pages):
        # Altium PDFs are printed 1:1; the sheet size stated in the title
        # block does not describe an additional print reduction.
        return _paper_for_page_size(pages[0])
    candidates = []
    for page in pages:
        worksheet = (page.get("decoded") or {}).get("worksheet")
        worksheet_size = (
            (worksheet or {}).get("fields", {}).get("size", "").upper()
        )
        if worksheet_size in PAPER_SCALES:
            candidates.append(worksheet_size)
        for text in page["texts"]:
            value = text.get("text", "").strip().upper()
            if (
                value in PAPER_SCALES
                and text["x"] >= page["width"] * 0.65
                and text["y"] >= page["height"] * 0.80
            ):
                candidates.append(value)
    if candidates:
        return Counter(candidates).most_common(1)[0][0]
    return "A4"


def coordinate_transform(paper: str) -> CoordinateTransform:
    scale = PAPER_SCALES[paper]
    # Capture's source grid is 2.54 mm.  Printed PDFs retain one reduced
    # grid step as the border/origin offset.
    return CoordinateTransform(paper, scale, 2.54 / scale)


def has_source_worksheet(page_records: list[dict]) -> bool:
    """Return whether any source page has a decoded worksheet frame."""
    return any(
        (record["semantic"].get("worksheet") or {}).get("frame")
        for record in page_records
    )


def sanitize_page_name(value: str) -> str:
    """Match dsn2kicad.hs sanitizePageName."""
    sanitized = "".join(
        character
        if character.isalnum() or character in "_.-"
        else "_"
        for character in value
    )
    return re.sub(r"_+", "_", sanitized).strip("_")


def _canonical_page_heading(value: str) -> str:
    value = re.sub(r"\s+", " ", value.strip())
    normalized = value.casefold()
    exact = {
        "block diagram": "BLOCK",
        "por control": "PWR_on_cnt",
        "system config": "Sys_Config",
        "soc clock": "Clock",
        "qspi flashrom": "QSPIFlash",
        "gpio": "SoC_GPIO",
        "mipi dsi": "MIPI-DSI",
        "extended gpio": "Ext_GPIO",
    }
    if normalized in exact:
        return exact[normalized]
    if re.fullmatch(r"usb2(?:\.0)?", normalized):
        return "USB2"
    if re.fullmatch(r"usb3(?:\.\d+)?", normalized):
        return "USB3"
    value = re.sub(r"\s+Sub\s+Board$", "", value, flags=re.IGNORECASE)
    return sanitize_page_name(value)


def _visible_page_heading(page: dict) -> str | None:
    green_texts = [
        text for text in page["texts"]
        if (
            text.get("color") == "#008000"
            and int(round(text.get("angle", 0.0))) % 360 == 0
            and text.get("text", "").strip()
        )
    ]
    if not green_texts:
        return None
    largest_size = max(float(text.get("size") or 0.0) for text in green_texts)
    if largest_size <= 0:
        return None
    candidates = [
        text for text in green_texts
        if float(text.get("size") or 0.0) >= largest_size * 0.80
    ]
    candidates.sort(key=lambda text: (text["y"], text["x"]))
    values = []
    for text in candidates:
        value = text["text"].strip()
        if (
            "evaluation board" in value.casefold()
            and len(candidates) > 1
        ):
            continue
        canonical = _canonical_page_heading(value)
        if canonical and canonical not in values:
            values.append(canonical)
    priority = {
        "Clock": 0,
        "Sys_Config": 1,
        "PWR_on_cnt": 2,
    }
    values = [
        value for _index, value in sorted(
            enumerate(values),
            key=lambda item: (priority.get(item[1], 100 + item[0]), item[0]),
        )
    ]
    return "_".join(values) or None


def detect_sheet_names(pages: list[dict]) -> list[str]:
    """Reconstruct DSN-style page stream names from visible PDF headings."""
    bases = []
    source_numbers = []
    source_count = len(pages)
    for index, page in enumerate(pages, 1):
        worksheet = (page.get("decoded") or {}).get("worksheet") or {}
        fields = worksheet.get("fields") or {}
        source_number = fields.get("sheet", "")
        if source_number.isdigit():
            page_number = int(source_number)
        else:
            page_number = index
        source_numbers.append(page_number)
        if fields.get("sheet_count", "").isdigit():
            source_count = max(source_count, int(fields["sheet_count"]))

        page_name = fields.get("page_name")
        decoded = page.get("decoded") or {}
        if (
            page_number == 1
            and not decoded.get("wires")
            and not decoded.get("components")
            and not (page.get("flavor") == "altium" and page_name)
        ):
            base = "NOTE"
        elif page_name:
            base = sanitize_page_name(page_name)
            if re.match(r"^\d+[_-]", base):
                base = re.sub(r"^\d+[_-]", "", base)
        else:
            base = _visible_page_heading(page)
        if not base:
            base = "NOTE" if page_number == 1 else "Page"
        bases.append(base)

    totals = Counter(bases)
    occurrences = Counter()
    width = max(2, len(str(source_count)))
    names = []
    for page_number, base in zip(source_numbers, bases):
        occurrences[base] += 1
        if totals[base] > 1 and base != "Page":
            base = f"{base}{occurrences[base]}"
        names.append(
            sanitize_page_name(f"{page_number:0{width}d}_{base}")
        )
    return names


def _geometry_clusters(
    page: dict,
    excluded_indexes: set[int],
    excluded_curve_indexes: set[int] | None = None,
) -> list[dict]:
    body_records = [
        (index, line)
        for index, line in enumerate(page["lines"])
        if (
            index not in excluded_indexes
            and line.get("color") == BODY_COLOR
            and pdf_dump._line_length(line) >= GEOM_TOL
        )
    ]
    parent = list(range(len(body_records)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first, second):
        first, second = find(first), find(second)
        if first != second:
            parent[second] = first

    endpoints: dict[tuple[int, int], list[int]] = {}
    tolerance = 0.06
    for record_index, (_line_index, line) in enumerate(body_records):
        for x, y in (_point(line, 1), _point(line, 2)):
            key = (round(x / tolerance), round(y / tolerance))
            for other in endpoints.get(key, []):
                union(record_index, other)
            endpoints.setdefault(key, []).append(record_index)

    # Some Capture symbols terminate a short body-colored pin extension on
    # the middle of a sloped body edge.  Endpoint-only grouping splits those
    # extensions (and their electrical pins) away from the symbol body.  Only
    # merge that precise pattern: one end touches the interior of another
    # body segment and the free end is attached to a pin-colored line.
    pin_endpoint_buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for line in page["lines"]:
        if line.get("color") != PIN_COLOR:
            continue
        for point in (_point(line, 1), _point(line, 2)):
            key = (
                round(point[0] / tolerance),
                round(point[1] / tolerance),
            )
            pin_endpoint_buckets.setdefault(key, []).append(point)

    def touches_pin_endpoint(point: tuple[float, float]) -> bool:
        key = (
            round(point[0] / tolerance),
            round(point[1] / tolerance),
        )
        return any(
            _point_close(point, candidate, tolerance)
            for x_offset in (-1, 0, 1)
            for y_offset in (-1, 0, 1)
            for candidate in pin_endpoint_buckets.get(
                (key[0] + x_offset, key[1] + y_offset),
                [],
            )
        )

    def is_pin_extension(extension: dict, body: dict) -> bool:
        body_first, body_second = _point(body, 1), _point(body, 2)
        if (
            abs(body_second[0] - body_first[0]) <= tolerance
            or abs(body_second[1] - body_first[1]) <= tolerance
        ):
            return False
        for contact, outer in (
            (_point(extension, 1), _point(extension, 2)),
            (_point(extension, 2), _point(extension, 1)),
        ):
            if (
                not touches_pin_endpoint(outer)
                or min(
                    _distance(contact, body_first),
                    _distance(contact, body_second),
                )
                <= tolerance
                or _point_to_segment(
                    contact,
                    body_first,
                    body_second,
                )[0]
                > tolerance
            ):
                continue
            return True
        return False

    body_bboxes = [
        _bbox_for_lines([line])
        for _line_index, line in body_records
    ]
    for first_index, (_line_index, first) in enumerate(body_records):
        for second_index in range(first_index + 1, len(body_records)):
            if not _bboxes_touch(
                body_bboxes[first_index],
                body_bboxes[second_index],
                tolerance,
            ):
                continue
            second = body_records[second_index][1]
            if is_pin_extension(first, second) or is_pin_extension(
                second,
                first,
            ):
                union(first_index, second_index)

    grouped: dict[int, list[tuple[int, dict]]] = {}
    for record_index, record in enumerate(body_records):
        grouped.setdefault(find(record_index), []).append(record)

    clusters = []
    for entries in grouped.values():
        body = [line for _index, line in entries]
        clusters.append(
            {
                "entries": list(entries),
                "body_lines": body,
                "pin_lines": [],
                "bbox": _bbox_for_lines(body),
            }
        )

    # Build components outward from body geometry.  Pin-to-pin contact is an
    # electrical connection between two symbols, not evidence that their
    # bodies belong to one symbol. Directly connected symbols can otherwise
    # be merged into a single component.
    assigned_pin_indexes = set()
    for index, line in enumerate(page["lines"]):
        if (
            index in excluded_indexes
            or line.get("color") != PIN_COLOR
            or pdf_dump._line_length(line) < GEOM_TOL
        ):
            continue
        candidates = []
        for cluster_index, cluster in enumerate(clusters):
            distance = min(
                _point_to_segment(
                    point,
                    _point(body_line, 1),
                    _point(body_line, 2),
                )[0]
                for point in (_point(line, 1), _point(line, 2))
                for body_line in cluster["body_lines"]
            )
            if distance <= GEOM_TOL:
                candidates.append((distance, cluster_index))
        if not candidates:
            continue
        _distance_score, cluster_index = min(candidates)
        clusters[cluster_index]["entries"].append((index, line))
        clusters[cluster_index]["pin_lines"].append(line)
        assigned_pin_indexes.add(index)

    clusters = [cluster for cluster in clusters if cluster["pin_lines"]]

    # Capacitor plates and the two disconnected colored halves of filled
    # diode glyphs each appear as aligned one-pin bodies.  Merge only close,
    # opposed halves; adjacent complete passives are farther apart.
    changed = True
    while changed:
        changed = False
        for first_index in range(len(clusters)):
            first = clusters[first_index]
            if len(first["pin_lines"]) != 1:
                continue
            for second_index in range(first_index + 1, len(clusters)):
                second = clusters[second_index]
                if len(second["pin_lines"]) != 1:
                    continue
                if (
                    _bbox_distance(first["bbox"], second["bbox"])
                    > BODY_HALF_JOIN_TOL
                ):
                    continue
                merged_bbox = _bbox_union(first["bbox"], second["bbox"])
                merged_probe = {
                    "bbox": merged_bbox,
                    "pin_lines": (
                        first["pin_lines"] + second["pin_lines"]
                    ),
                }
                if not _has_symmetric_two_pin_geometry(
                    {"pins": _pins_from_cluster(merged_probe)}
                ):
                    continue
                merged_entries = first["entries"] + second["entries"]
                merged_body = first["body_lines"] + second["body_lines"]
                merged_pins = first["pin_lines"] + second["pin_lines"]
                clusters[first_index] = {
                    "entries": merged_entries,
                    "body_lines": merged_body,
                    "pin_lines": merged_pins,
                    "bbox": merged_bbox,
                }
                del clusters[second_index]
                changed = True
                break
            if changed:
                break

    # Inductor bodies in Capture PDFs are commonly emitted entirely as
    # segmented Bézier curves.  Recover curve-only bodies with their two pin
    # stubs instead of leaving the curves as graphics and turning the nearby
    # "L<n>" text into a bodyless placeholder symbol.
    excluded_curve_indexes = excluded_curve_indexes or set()
    curve_records = []
    for index, curve in enumerate(page.get("curves", [])):
        if (
            index in excluded_curve_indexes
            or curve.get("color") != BODY_COLOR
            or not curve.get("points")
        ):
            continue
        curve_bbox = _curve_bbox(curve)
        if any(
            _bboxes_touch(curve_bbox, cluster["bbox"], BODY_CURVE_JOIN_TOL)
            for cluster in clusters
        ):
            continue
        curve_records.append((index, curve, curve_bbox))

    curve_parent = list(range(len(curve_records)))

    def curve_find(index):
        while curve_parent[index] != index:
            curve_parent[index] = curve_parent[curve_parent[index]]
            index = curve_parent[index]
        return index

    def curve_union(first, second):
        first, second = curve_find(first), curve_find(second)
        if first != second:
            curve_parent[second] = first

    for first_index, (_index, _curve, first_bbox) in enumerate(curve_records):
        for second_index in range(first_index + 1, len(curve_records)):
            second_bbox = curve_records[second_index][2]
            if _bboxes_touch(
                first_bbox,
                second_bbox,
                BODY_CURVE_JOIN_TOL,
            ):
                curve_union(first_index, second_index)

    grouped_curves: dict[int, list[tuple[int, dict, dict]]] = {}
    for record_index, record in enumerate(curve_records):
        grouped_curves.setdefault(curve_find(record_index), []).append(record)

    for curve_group in grouped_curves.values():
        bbox = _bbox_union(*(record[2] for record in curve_group))
        pin_entries = []
        pin_lines = []
        for index, line in enumerate(page["lines"]):
            if (
                index in excluded_indexes
                or index in assigned_pin_indexes
                or line.get("color") != PIN_COLOR
                or pdf_dump._line_length(line) < GEOM_TOL
                or not _bboxes_touch(
                    _bbox_for_lines([line]),
                    bbox,
                    GEOM_TOL,
                )
            ):
                continue
            pin_entries.append((index, line))
            pin_lines.append(line)
        if not pin_lines:
            continue
        clusters.append(
            {
                "entries": pin_entries,
                "body_lines": [],
                "body_curves": [record[1] for record in curve_group],
                "pin_lines": pin_lines,
                "bbox": bbox,
                "curve_indexes": {
                    record[0] for record in curve_group
                },
            }
        )
    return clusters


def _pins_from_cluster(cluster: dict) -> list[dict]:
    bbox = cluster["bbox"]
    center = ((bbox["x0"] + bbox["x1"]) / 2, (bbox["y0"] + bbox["y1"]) / 2)
    pins = []
    for line in cluster["pin_lines"]:
        first, second = _point(line, 1), _point(line, 2)
        if _distance(first, center) >= _distance(second, center):
            hot, other = first, second
        else:
            hot, other = second, first
        pins.append(
            {
                "hot": {"x": hot[0], "y": hot[1]},
                "other": {"x": other[0], "y": other[1]},
                "length": round(_distance(hot, other), 3),
            }
        )
    pins.sort(key=lambda pin: (pin["hot"]["y"], pin["hot"]["x"]))
    for number, pin in enumerate(pins, 1):
        pin["number"] = str(number)
    return pins


def _assign_missing_pin_numbers(pins: list[dict]) -> None:
    used = {str(pin["number"]) for pin in pins if pin.get("number")}
    next_number = 1
    for pin in sorted(
        pins, key=lambda item: (item["hot"]["y"], item["hot"]["x"])
    ):
        if pin.get("number"):
            continue
        while str(next_number) in used:
            next_number += 1
        pin["number"] = str(next_number)
        used.add(str(next_number))
        next_number += 1


def _recover_visible_pin_numbers(
    page: dict,
    component: dict,
    consumed_texts: set[tuple],
) -> set[tuple]:
    """Use numeric PDF labels beside pins on non-rectangular symbols."""
    pins = component.get("pins", [])
    replaceable = [
        index
        for index, pin in enumerate(pins)
        if not pin.get("number_text")
    ]
    if len(pins) < 3 or not replaceable:
        return set()

    candidates = [
        text
        for text in pdf_dump._pin_number_candidates(
            page["texts"], page.get("flavor")
        )
        if _text_key(text) not in consumed_texts
    ]
    scores = []
    for pin_index in replaceable:
        pin = {
            **pins[pin_index],
            "side": pdf_dump._pin_orientation(pins[pin_index]),
        }
        for text_index, text in enumerate(candidates):
            score = pdf_dump._pin_number_score(pin, text)
            if score is not None:
                scores.append((score, pin_index, text_index))

    paired_pins = set()
    paired_texts = set()
    recovered = set()
    for _score, pin_index, text_index in sorted(scores):
        if pin_index in paired_pins or text_index in paired_texts:
            continue
        text = candidates[text_index]
        pins[pin_index]["number"] = text["text"].strip()
        pins[pin_index]["number_text"] = text
        paired_pins.add(pin_index)
        paired_texts.add(text_index)
        recovered.add(_text_key(text))

    if recovered:
        used = {
            str(pin["number"])
            for pin in pins
            if pin.get("number_text") and pin.get("number")
        }
        next_number = 1
        for pin_index in replaceable:
            if pin_index in paired_pins:
                continue
            while str(next_number) in used:
                next_number += 1
            pins[pin_index]["number"] = str(next_number)
            used.add(str(next_number))
            next_number += 1
    return recovered


def _recover_spacer_pin(
    page: dict,
    component: dict,
    wires: list[dict],
    excluded_line_indexes: set[int],
) -> set[int]:
    """Recover an SP pin stub separated slightly from its drawn body."""
    if (
        component.get("pins")
        or not str(component.get("reference") or "").startswith("SP")
    ):
        return set()

    bbox = component["bbox"]
    candidates = []
    for index, candidate in enumerate(page["lines"]):
        if (
            index in excluded_line_indexes
            or candidate.get("color") != PIN_COLOR
            or not 0.2 <= pdf_dump._line_length(candidate) <= 8.0
            or _bbox_distance(_bbox_for_lines([candidate]), bbox) > 1.5
        ):
            continue
        for hot, other in (
            (_point(candidate, 1), _point(candidate, 2)),
            (_point(candidate, 2), _point(candidate, 1)),
        ):
            wire_hit = _point_on_any_wire(
                hot,
                wires,
                max(GEOM_TOL, 0.08),
            )
            if not wire_hit:
                continue
            other_bbox = {
                "x0": other[0],
                "y0": other[1],
                "x1": other[0],
                "y1": other[1],
            }
            hot_bbox = {
                "x0": hot[0],
                "y0": hot[1],
                "x1": hot[0],
                "y1": hot[1],
            }
            other_distance = _bbox_distance(other_bbox, bbox)
            hot_distance = _bbox_distance(hot_bbox, bbox)
            if other_distance > 1.5 or other_distance >= hot_distance:
                continue
            candidates.append(
                (
                    wire_hit[0] + other_distance,
                    index,
                    wire_hit[1],
                    other,
                )
            )
    if not candidates:
        return set()

    _score, line_index, hot, other = min(candidates)
    component["pins"] = [{
        "hot": {"x": hot[0], "y": hot[1]},
        "other": {"x": other[0], "y": other[1]},
        "length": round(_distance(hot, other), 3),
        "number": "1",
        "line_index": line_index,
    }]
    return {line_index}


def _recover_negated_pin_names(page: dict, component: dict) -> set[int]:
    """Convert PDF overline strokes into KiCad negated pin-name markup."""
    consumed_lines = set()
    component_lines = component.setdefault("line_indexes", set())
    for pin in component.get("pins", []):
        name_text = pin.get("name_text")
        name = str(pin.get("name") or "").strip()
        if (
            not name_text
            or not name
            or name.startswith("~{")
            or int(round(name_text.get("angle", 0))) % 180 != 0
        ):
            continue
        text_bbox = _text_bbox(name_text)
        text_width = max(text_bbox["x1"] - text_bbox["x0"], 0.1)
        candidates = []
        for index, line in enumerate(page["lines"]):
            if (
                index in consumed_lines
                or line.get("color") != PIN_NAME_COLOR
                or abs(line["y1"] - line["y2"]) > GEOM_TOL
            ):
                continue
            line_x0, line_x1 = sorted((line["x1"], line["x2"]))
            overlap = max(
                0.0,
                min(line_x1, text_bbox["x1"])
                - max(line_x0, text_bbox["x0"]),
            )
            line_y = (line["y1"] + line["y2"]) / 2
            if (
                overlap < text_width * 0.75
                or not (
                    text_bbox["y0"] - 0.3
                    <= line_y
                    <= text_bbox["y0"] + 0.1
                )
            ):
                continue
            candidates.append(
                (
                    abs(line_y - text_bbox["y0"])
                    + abs(line_x0 - text_bbox["x0"])
                    + abs(line_x1 - text_bbox["x1"]),
                    index,
                )
            )
        if not candidates:
            continue
        _score, index = min(candidates)
        pin["name"] = f"~{{{name}}}"
        pin["negated"] = True
        component_lines.add(index)
        consumed_lines.add(index)
    return consumed_lines


def _reference_matches_pin_count(reference: str, pin_count: int) -> bool:
    match = re.match(r"([A-Za-z]+)", reference or "")
    prefix = match.group(1).upper() if match else ""
    if prefix in ("R", "C", "L", "FB", "F"):
        return pin_count <= 2
    if prefix == "TP":
        return pin_count <= 1
    return True


def _repair_mismatched_references(page: dict, components: list[dict]) -> None:
    """Replace impossible passive references assigned to multi-pin bodies."""
    references = [
        text
        for text in page["texts"]
        if (
            text.get("color") == "#000000"
            and REF_RE.match(text.get("text", "").strip())
        )
    ]
    reserved = {
        _text_key(component["reference_text"])
        for component in components
        if (
            component.get("reference_text")
            and _reference_matches_pin_count(
                (component.get("reference") or ""),
                len(component.get("pins", [])),
            )
        )
    }
    for component in components:
        pin_count = len(component.get("pins", []))
        if _reference_matches_pin_count(
            (component.get("reference") or ""),
            pin_count,
        ):
            continue
        candidates = []
        for text in references:
            key = _text_key(text)
            value = text.get("text", "").strip()
            if (
                key in reserved
                or not _reference_matches_pin_count(value, pin_count)
            ):
                continue
            distance = _bbox_distance(
                component["bbox"],
                _text_bbox(text),
            )
            if distance <= 12.0:
                candidates.append((distance, text))
        if not candidates:
            continue
        _distance_score, replacement = min(
            candidates,
            key=lambda item: (
                item[0],
                item[1].get("text", ""),
                item[1].get("y", 0.0),
                item[1].get("x", 0.0),
            ),
        )
        component["reference"] = replacement["text"].strip()
        component["reference_text"] = replacement
        reserved.add(_text_key(replacement))


def _augment_component_geometry(page: dict, component: dict) -> None:
    """Pull disconnected PDF body strokes back into their native symbol."""
    bbox = component["bbox"]
    original_width = max(bbox["x1"] - bbox["x0"], 0.1)
    original_height = max(bbox["y1"] - bbox["y0"], 0.1)

    rectangle_indexes = []
    body_rectangles = []
    center = (
        (bbox["x0"] + bbox["x1"]) / 2,
        (bbox["y0"] + bbox["y1"]) / 2,
    )
    for index, rectangle in enumerate(page.get("rectangles", [])):
        if rectangle.get("color") != BODY_COLOR:
            continue
        rectangle_bbox = _bbox_for_rectangle(rectangle)
        rectangle_width = rectangle_bbox["x1"] - rectangle_bbox["x0"]
        rectangle_height = rectangle_bbox["y1"] - rectangle_bbox["y0"]
        if (
            _point_in_bbox(center, rectangle_bbox, GEOM_TOL)
            and rectangle_width <= original_width + 6.0
            and rectangle_height <= original_height + 6.0
        ):
            rectangle_indexes.append(index)
            body_rectangles.append(rectangle)
    if body_rectangles:
        bbox = _bbox_union(
            bbox,
            *(_bbox_for_rectangle(rectangle) for rectangle in body_rectangles),
        )

    body_lines = []
    component_line_indexes = {
        *component.get("line_indexes", set()),
        *(
            pin["line_index"]
            for pin in component.get("pins", [])
            if pin.get("line_index") is not None
        ),
    }
    reference_match = re.match(
        r"([A-Za-z]+)",
        component.get("reference") or "",
    )
    reference_prefix = (
        reference_match.group(1).upper() if reference_match else ""
    )
    remaining_body_lines = [
        (index, line, _bbox_for_lines([line]))
        for index, line in enumerate(page["lines"])
        if (
            line.get("color") == BODY_COLOR
            or (
                reference_prefix in ("D", "LD")
                and line.get("color") is None
                and float(line.get("width") or 0.0) == 0.0
            )
        )
    ]
    body_join_tolerance = (
        TRANSISTOR_GATE_JOIN_TOL
        if reference_prefix == "Q"
        else GEOM_TOL
    )
    if reference_prefix in ("D", "LD", "Q"):
        changed = True
        while changed:
            changed = False
            for candidate in list(remaining_body_lines):
                index, line, line_bbox = candidate
                if not _bboxes_touch(
                    line_bbox,
                    bbox,
                    body_join_tolerance,
                ):
                    continue
                body_lines.append(line)
                component_line_indexes.add(index)
                bbox = _bbox_union(bbox, line_bbox)
                remaining_body_lines.remove(candidate)
                changed = True
    else:
        for index, line, line_bbox in remaining_body_lines:
            if not _bboxes_touch(line_bbox, bbox, GEOM_TOL):
                continue
            body_lines.append(line)
            component_line_indexes.add(index)
        if body_lines:
            bbox = _bbox_union(bbox, _bbox_for_lines(body_lines))

    curve_indexes = {
        index
        for pin in component.get("pins", [])
        for index in pin.get("curve_indexes", [])
    }
    body_curves = []
    remaining_curves = [
        (index, curve, _curve_bbox(curve))
        for index, curve in enumerate(page.get("curves", []))
        if curve.get("color") == BODY_COLOR and curve.get("points")
    ]
    changed = True
    while changed:
        changed = False
        for candidate in list(remaining_curves):
            index, curve, curve_bbox = candidate
            if not _bboxes_touch(
                curve_bbox,
                bbox,
                BODY_CURVE_JOIN_TOL,
            ):
                continue
            curve_indexes.add(index)
            body_curves.append(curve)
            bbox = _bbox_union(bbox, curve_bbox)
            remaining_curves.remove(candidate)
            changed = True

    pins = component["pins"]
    for index, line in enumerate(page["lines"]):
        if line.get("color") != PIN_COLOR:
            continue
        if any(_line_matches_pin(line, pin) for pin in pins):
            component_line_indexes.add(index)
            continue
        first, second = _point(line, 1), _point(line, 2)
        first_edge = pdf_dump._dist_point_to_rect_edge(
            first[0], first[1], bbox
        )
        second_edge = pdf_dump._dist_point_to_rect_edge(
            second[0], second[1], bbox
        )
        if first_edge <= GEOM_TOL and not _point_in_bbox(
            second, bbox, GEOM_TOL / 2
        ):
            other, hot = first, second
        elif second_edge <= GEOM_TOL and not _point_in_bbox(
            first, bbox, GEOM_TOL / 2
        ):
            other, hot = second, first
        else:
            continue
        pins.append(
            {
                "hot": {"x": hot[0], "y": hot[1]},
                "other": {"x": other[0], "y": other[1]},
                "length": round(_distance(hot, other), 3),
            }
        )
        component_line_indexes.add(index)

    _assign_missing_pin_numbers(pins)
    component["bbox"] = bbox
    component["body_lines"] = body_lines
    component["body_rectangles"] = body_rectangles
    component["body_curves"] = body_curves
    component["line_indexes"] = component_line_indexes
    component["rectangle_indexes"] = set(rectangle_indexes)
    component["curve_indexes"] = curve_indexes


def _recover_connector_pin_labels(
    page: dict,
    component: dict,
) -> set[tuple]:
    """Recover black pin-name text used by connector library symbols."""
    if (
        not re.match(r"^CN\d", (component.get("reference") or ""))
        or len(component.get("pins", [])) <= 2
        or any(pin.get("name") for pin in component["pins"])
    ):
        return set()

    consumed = set()
    for pin in component["pins"]:
        number_text = pin.get("number_text")
        if number_text and pin.get("number"):
            pin["name"] = pin["number"]
            pin["name_text"] = number_text
            consumed.add(_text_key(number_text))

    label_texts = [
        text
        for text in page["texts"]
        if (
            text.get("color") == "#000000"
            and re.fullmatch(r"F\d+", text.get("text", "").strip())
        )
    ]
    scores = []
    for text_index, text in enumerate(label_texts):
        for pin_index, pin in enumerate(component["pins"]):
            if pin.get("name"):
                continue
            scored_pin = {**pin, "side": pdf_dump._pin_orientation(pin)}
            score = pdf_dump._pin_number_score(scored_pin, text)
            if score is not None:
                scores.append((score, pin_index, text_index))

    paired_pins = set()
    paired_texts = set()
    for _score, pin_index, text_index in sorted(scores):
        if pin_index in paired_pins or text_index in paired_texts:
            continue
        text = label_texts[text_index]
        value = text["text"].strip()
        pin = component["pins"][pin_index]
        pin["number"] = value
        pin["name"] = value
        pin["number_text"] = text
        pin["name_text"] = text
        consumed.add(_text_key(text))
        paired_pins.add(pin_index)
        paired_texts.add(text_index)
    return consumed


def _suppress_numeric_j_pin_names(component: dict) -> None:
    """Discard PDF pin-number spans misclassified as J pin names."""
    if not re.fullmatch(r"J\d+[A-Z]?", (component.get("reference") or "")):
        return
    names = [
        str(pin["name"]).strip()
        for pin in component.get("pins", [])
        if pin.get("name")
    ]
    if not names or any(not name.isdigit() for name in names):
        return
    for pin in component["pins"]:
        pin.pop("name", None)


def _maximum_cardinality_pairs(
    pair_scores: list[tuple[float, int, int]],
) -> list[tuple[float, int, int]]:
    """Return a minimum-cost matching among maximum-cardinality pairings."""
    if not pair_scores:
        return []

    text_indexes = sorted({text for _score, _cluster, text in pair_scores})
    cluster_indexes = sorted({
        cluster for _score, cluster, _text in pair_scores
    })
    text_nodes = {
        text_index: offset + 1
        for offset, text_index in enumerate(text_indexes)
    }
    cluster_nodes = {
        cluster_index: offset + 1 + len(text_indexes)
        for offset, cluster_index in enumerate(cluster_indexes)
    }
    source = 0
    sink = 1 + len(text_indexes) + len(cluster_indexes)
    graph: list[list[list]] = [[] for _index in range(sink + 1)]

    def add_edge(start: int, end: int, capacity: int, cost: float) -> list:
        forward = [end, len(graph[end]), capacity, cost]
        reverse = [start, len(graph[start]), 0, -cost]
        graph[start].append(forward)
        graph[end].append(reverse)
        return forward

    for text_index in text_indexes:
        add_edge(source, text_nodes[text_index], 1, 0.0)
    for cluster_index in cluster_indexes:
        add_edge(cluster_nodes[cluster_index], sink, 1, 0.0)

    candidate_edges = {}
    scores = {}
    for score, cluster_index, text_index in pair_scores:
        scores[(cluster_index, text_index)] = score
        candidate_edges[(cluster_index, text_index)] = add_edge(
            text_nodes[text_index],
            cluster_nodes[cluster_index],
            1,
            score,
        )

    # Successive shortest augmenting paths on the residual graph produce
    # maximum cardinality first and minimum total score within that cardinality.
    while True:
        distance = [math.inf] * len(graph)
        previous: list[tuple[int, int] | None] = [None] * len(graph)
        in_queue = [False] * len(graph)
        queue = [source]
        distance[source] = 0.0
        in_queue[source] = True
        queue_index = 0
        while queue_index < len(queue):
            node = queue[queue_index]
            queue_index += 1
            in_queue[node] = False
            for edge_index, edge in enumerate(graph[node]):
                end, _reverse, capacity, cost = edge
                if capacity <= 0:
                    continue
                candidate_distance = distance[node] + cost
                if candidate_distance >= distance[end] - 1e-9:
                    continue
                distance[end] = candidate_distance
                previous[end] = (node, edge_index)
                if not in_queue[end]:
                    queue.append(end)
                    in_queue[end] = True
        if previous[sink] is None:
            break
        node = sink
        while node != source:
            prior, edge_index = previous[node]
            edge = graph[prior][edge_index]
            reverse_index = edge[1]
            edge[2] -= 1
            graph[node][reverse_index][2] += 1
            node = prior

    return sorted(
        (
            scores[(cluster_index, text_index)],
            cluster_index,
            text_index,
        )
        for (cluster_index, text_index), edge in candidate_edges.items()
        if edge[2] == 0
    )


def decode_components(page: dict) -> tuple[list[dict], set[tuple], set[int]]:
    _split_merged_reference_values(page)
    decoded = page.get("decoded") or pdf_dump.decode_page(page)
    wires = decoded["wires"]
    known = copy.deepcopy(decoded["components"])
    used_texts: set[tuple] = set()
    used_line_indexes: set[int] = set()
    used_curve_indexes: set[int] = set()

    page_texts_by_key = {
        _text_key(text): text
        for text in page["texts"]
    }
    for component in known:
        reference_text = component.get("reference_text")
        if reference_text:
            component["reference_text"] = page_texts_by_key.get(
                _text_key(reference_text),
                reference_text,
            )
    _repair_mismatched_references(page, known)
    for component in known:
        reference_text = component.get("reference_text")
        if reference_text:
            used_texts.add(_text_key(reference_text))
        bbox = component["bbox"]
        recovered_spacer_lines = _recover_spacer_pin(
            page,
            component,
            wires,
            used_line_indexes,
        )
        body_lines = []
        component_line_indexes = set(recovered_spacer_lines)
        for index, line in enumerate(page["lines"]):
            if line.get("color") == BODY_COLOR and (
                _point_in_bbox(_point(line, 1), bbox, GEOM_TOL)
                and _point_in_bbox(_point(line, 2), bbox, GEOM_TOL)
            ):
                body_lines.append(line)
                component_line_indexes.add(index)
            elif line.get("color") == PIN_COLOR and any(
                _line_matches_pin(line, pin) for pin in component["pins"]
            ):
                component_line_indexes.add(index)
        _assign_missing_pin_numbers(component["pins"])
        component["body_lines"] = body_lines
        component["line_indexes"] = component_line_indexes
        _augment_component_geometry(page, component)
        _recover_negated_pin_names(page, component)
        used_texts.update(
            _recover_visible_pin_numbers(page, component, used_texts)
        )
        _suppress_numeric_j_pin_names(component)
        used_texts.update(_recover_connector_pin_labels(page, component))
        for pin in component["pins"]:
            for key in ("number_text", "name_text"):
                if pin.get(key):
                    used_texts.add(_text_key(pin[key]))
        component_line_indexes = component["line_indexes"]
        used_line_indexes.update(component_line_indexes)
        used_curve_indexes.update(component.get("curve_indexes", set()))

    clusters = _geometry_clusters(
        page,
        used_line_indexes,
        used_curve_indexes,
    )
    reference_texts = [
        text
        for text in page["texts"]
        if pdf_dump.is_reference_text(text, page.get("flavor"))
        and _text_key(text) not in used_texts
        and _text_key(text) not in {
            _text_key(component["reference_text"])
            for component in known
            if component.get("reference_text")
        }
    ]

    # Associate references and geometric clusters globally so dense R/C banks
    # cannot consume a neighbour's symbol just because of input ordering.
    pair_scores = []
    for cluster_index, cluster in enumerate(clusters):
        for text_index, text in enumerate(reference_texts):
            if not _reference_matches_pin_count(
                text.get("text", ""),
                len(cluster["pin_lines"]),
            ):
                continue
            distance = _bbox_distance(cluster["bbox"], _text_bbox(text))
            if distance <= 6.0:
                text_bbox = _text_bbox(text)
                text_center = (
                    (text_bbox["x0"] + text_bbox["x1"]) / 2,
                    (text_bbox["y0"] + text_bbox["y1"]) / 2,
                )
                cluster_center = (
                    (cluster["bbox"]["x0"] + cluster["bbox"]["x1"]) / 2,
                    (cluster["bbox"]["y0"] + cluster["bbox"]["y1"]) / 2,
                )
                angle = int(round(text.get("angle", 0))) % 180
                cross_offset = (
                    cluster_center[1] - text_center[1]
                    if angle == 0
                    else cluster_center[0] - text_center[0]
                )
                # Rotated reference text commonly protrudes about 0.7 mm
                # beyond its own body.  Treat that small negative offset as
                # alignment noise while still rejecting the previous body in
                # a dense bank several millimetres away.
                score = (
                    distance
                    + max(
                        0.0,
                        -cross_offset - REFERENCE_DIRECTION_TOL,
                    )
                    * 5.0
                )
                pair_scores.append((score, cluster_index, text_index))
    paired_clusters = set()
    paired_texts = set()
    for _score, cluster_index, text_index in _maximum_cardinality_pairs(
        pair_scores
    ):
        cluster = clusters[cluster_index]
        text = reference_texts[text_index]
        pins = _pins_from_cluster(cluster)
        component_line_indexes = {index for index, _line in cluster["entries"]}
        component = {
            "reference": text["text"],
            "reference_text": text,
            "bbox": cluster["bbox"],
            "pins": pins,
            "body_lines": cluster["body_lines"],
            "body_curves": cluster.get("body_curves", []),
            "line_indexes": component_line_indexes,
            "curve_indexes": set(cluster.get("curve_indexes", set())),
        }
        _augment_component_geometry(page, component)
        _recover_negated_pin_names(page, component)
        used_texts.update(
            _recover_visible_pin_numbers(page, component, used_texts)
        )
        for pin in component["pins"]:
            for key in ("number_text", "name_text"):
                if pin.get(key):
                    used_texts.add(_text_key(pin[key]))
        component_line_indexes = component["line_indexes"]
        known.append(component)
        used_texts.add(_text_key(text))
        used_line_indexes.update(component_line_indexes)
        paired_clusters.add(cluster_index)
        paired_texts.add(text_index)

    # Mechanical screw/spacer symbols may have no electrically colored pin.
    for text_index, text in enumerate(reference_texts):
        if text_index in paired_texts:
            continue
        if not MECHANICAL_REF_RE.match(text["text"]):
            continue
        pins = []
        bbox = _text_bbox(text)
        if text["text"].startswith("SP"):
            points = [
                (bbox["x0"], bbox["y0"]),
                (bbox["x0"], bbox["y1"]),
                (bbox["x1"], bbox["y0"]),
                (bbox["x1"], bbox["y1"]),
            ]
            wire_hit = _nearest_wire_point(points, wires, 8.0)
            if wire_hit:
                hot = wire_hit[1]
                pins = [{
                    "hot": {"x": hot[0], "y": hot[1]},
                    "other": {"x": hot[0], "y": hot[1]},
                    "length": 0.0,
                    "number": "1",
                }]
                bbox = {
                    "x0": hot[0] - 1.0,
                    "y0": hot[1] - 1.0,
                    "x1": hot[0] + 1.0,
                    "y1": hot[1] + 1.0,
                }
        known.append(
            {
                "reference": text["text"],
                "reference_text": text,
                "bbox": bbox,
                "pins": pins,
                "body_lines": [],
                "line_indexes": set(),
            }
        )
        used_texts.add(_text_key(text))

    # Keep references which the PDF exposes but whose glyph did not use the
    # standard body/pin colors (mounting holes and a few vendor-library
    # symbols do this).  Pin-like text already consumed by a decoded symbol is
    # excluded.  Requiring a schematic page with wires avoids interpreting the
    # block-diagram note page's Uxx annotations as components.
    if wires:
        for text in reference_texts:
            if _text_key(text) in used_texts:
                continue
            if any(
                component.get("reference") == text.get("text", "").strip()
                for component in known
            ):
                continue
            bbox = _text_bbox(text)
            pins = []
            if text["text"].startswith("TP"):
                points = [
                    (bbox["x0"], bbox["y0"]),
                    (bbox["x0"], bbox["y1"]),
                    (bbox["x1"], bbox["y0"]),
                    (bbox["x1"], bbox["y1"]),
                ]
                wire_hit = _nearest_wire_point(points, wires, 5.0)
                if wire_hit:
                    hot = wire_hit[1]
                    pins = [{
                        "hot": {"x": hot[0], "y": hot[1]},
                        "other": {"x": hot[0], "y": hot[1]},
                        "length": 0.0,
                        "number": "1",
                    }]
                    bbox = {
                        "x0": hot[0] - 1.0,
                        "y0": hot[1] - 1.0,
                        "x1": hot[0] + 1.0,
                        "y1": hot[1] + 1.0,
                    }
            known.append({
                "reference": text["text"],
                "reference_text": text,
                "bbox": bbox,
                "pins": pins,
                "body_lines": [],
                "line_indexes": set(),
            })
            used_texts.add(_text_key(text))

    known = [component for component in known if component.get("reference")]
    for component in known:
        reference_text = component.get("reference_text") or {}
        if reference_text.get("dnp"):
            component["dnp"] = True
    known.sort(
        key=lambda component: (
            component["reference"],
            component["bbox"]["y0"],
            component["bbox"]["x0"],
        )
    )
    return known, used_texts, used_line_indexes


def _default_value(reference: str) -> str:
    match = re.match(r"([A-Za-z]+)", reference)
    return match.group(1) if match else "PDF_COMPONENT"


def _passive_kind(component: dict) -> str | None:
    reference = component.get("source_reference") or component.get(
        "reference", ""
    )
    match = re.match(r"^(FB|R|C|L)\d", reference, re.IGNORECASE)
    return match.group(1).upper() if match else None


def _standard_symbol_definition(lib_id: str) -> str:
    """Load one bundled KiCad library symbol under its canonical ID."""
    if lib_id in _KICAD_STANDARD_SYMBOLS:
        return _KICAD_STANDARD_SYMBOLS[lib_id]

    library, name = lib_id.split(":", 1)
    library_path = SCRIPT_DIR / "kicad_symbols" / f"{library}.kicad_sym"
    content = library_path.read_text(encoding="utf-8")
    marker = f'\t(symbol "{name}"'
    start = content.find(marker)
    if start < 0:
        raise RuntimeError(f"{library_path} does not contain {name}")
    depth = 0
    seen_open = False
    end = start
    for end in range(start, len(content)):
        if content[end] == "(":
            depth += 1
            seen_open = True
        elif content[end] == ")":
            depth -= 1
        if seen_open and depth == 0:
            end += 1
            break
    lines = content[start:end].splitlines()
    definition = (
        "\n".join(
            [f'\t\t(symbol "{lib_id}"']
            + ["\t" + line for line in lines[1:]]
        )
        + "\n"
    )
    _KICAD_STANDARD_SYMBOLS[lib_id] = definition
    return definition


def _standard_passive_definition(kind: str) -> str:
    return _standard_symbol_definition(STANDARD_PASSIVE_LIB_IDS[kind])


def _passive_package_code(value: str) -> tuple[str, str] | None:
    """Return (source code, imperial code) from a delimited value field."""
    package_codes = "|".join(
        sorted(PASSIVE_PACKAGE_ALIASES, key=len, reverse=True)
    )
    matches = list(
        re.finditer(
            rf"(?<![A-Za-z0-9])({package_codes})(?![A-Za-z0-9])",
            value,
        )
    )
    if not matches:
        return None
    source_code = matches[-1].group(1)
    return source_code, PASSIVE_PACKAGE_ALIASES[source_code]


def _infer_passive_footprint(component: dict) -> None:
    kind = _passive_kind(component)
    if kind is None or _two_terminal_axis(component) is None:
        return
    package = _passive_package_code(component.get("value", ""))
    if package is None:
        return
    source_code, imperial_code = package
    suffix = PASSIVE_FOOTPRINT_SUFFIXES.get(imperial_code)
    family = PASSIVE_FOOTPRINT_FAMILIES.get(kind)
    if suffix is None or family is None:
        return
    # KiCad does not provide generic 2010/2512 capacitor footprints.
    if kind == "C" and imperial_code in ("2010", "2512"):
        return
    library, prefix = family
    component["package"] = source_code
    component["footprint"] = f"{library}:{prefix}_{suffix}"


def _can_standardize_passive(
    component: dict,
) -> bool:
    if (
        _passive_kind(component) is None
        or _two_terminal_axis(component) is None
    ):
        return False
    pins = component["pins"]
    return {str(pin.get("number")) for pin in pins} == {"1", "2"}


def _configure_standard_passive(
    component: dict,
    transform: CoordinateTransform,
) -> None:
    """Record a stock Device symbol placement and its canonical hotpoints."""
    kind = _passive_kind(component)
    axis = _two_terminal_axis(component)
    assert kind is not None and axis is not None
    pins_by_number = {
        str(pin["number"]): pin
        for pin in component["pins"]
    }
    pin1 = pins_by_number["1"]
    pin2 = pins_by_number["2"]
    origin = (
        (pin1["hot"]["x"] + pin2["hot"]["x"]) / 2,
        (pin1["hot"]["y"] + pin2["hot"]["y"]) / 2,
    )
    if axis == "horizontal":
        angle = 90 if pin1["hot"]["x"] < pin2["hot"]["x"] else 270
        coordinate = "x"
    else:
        angle = 0 if pin1["hot"]["y"] > pin2["hot"]["y"] else 180
        coordinate = "y"
    offset = STANDARD_PASSIVE_PIN_OFFSET / transform.scale
    center_coordinate = origin[0] if coordinate == "x" else origin[1]
    hotpoints = {}
    for number, pin in pins_by_number.items():
        sign = 1 if pin["hot"][coordinate] > center_coordinate else -1
        if axis == "horizontal":
            hotpoints[number] = (origin[0] + sign * offset, origin[1])
        else:
            hotpoints[number] = (origin[0], origin[1] + sign * offset)

    component["standard_passive"] = kind
    component["standard_lib_id"] = STANDARD_PASSIVE_LIB_IDS[kind]
    component["standard_origin"] = origin
    component["standard_angle"] = angle
    component["standard_hotpoints"] = hotpoints


def _configure_standard_testpoint(component: dict) -> None:
    """Place KiCad's stock TestPoint with its pin on the recovered hotpoint."""
    if (
        not re.fullmatch(
            r"TP\d+[A-Z]?",
            (component.get("reference") or ""),
            re.IGNORECASE,
        )
        or len(component.get("pins", [])) != 1
        or str(component["pins"][0].get("number")) != "1"
    ):
        return
    pin = component["pins"][0]
    hotpoint = (pin["hot"]["x"], pin["hot"]["y"])
    component["standard_testpoint"] = True
    component["standard_lib_id"] = STANDARD_TESTPOINT_LIB_ID
    component["standard_origin"] = hotpoint
    component["standard_angle"] = (_pin_direction(pin) - 90) % 360
    component["standard_hotpoints"] = {"1": hotpoint}


def _enrich_component(
    component: dict,
    transform: CoordinateTransform,
    *,
    infer_footprints: bool,
    use_kicad_rcl: bool,
) -> None:
    """Normalize value metadata and enable requested passive enrichment."""
    value = str(component.get("value") or "")
    dnp_match = DNP_SUFFIX_RE.search(value)
    component["dnp"] = bool(dnp_match) or bool(component.get("dnp"))
    if dnp_match:
        component["value"] = value[:dnp_match.start()].rstrip()
    component["_transform"] = transform
    _configure_standard_testpoint(component)
    if infer_footprints:
        _infer_passive_footprint(component)
    if use_kicad_rcl and _can_standardize_passive(component):
        _configure_standard_passive(component, transform)


def _local_label_anchor(text: dict) -> tuple[float, float]:
    bbox = _text_bbox(text)
    size = max(float(text.get("size") or 0), 0.1)
    angle = int(round(text.get("angle", 0))) % 360
    if angle == 0:
        return (
            text["x"] - 0.218 * size,
            text["y1"] + 0.160 * size,
        )
    return (
        (bbox["x0"] + bbox["x1"]) / 2,
        (bbox["y0"] + bbox["y1"]) / 2,
    )


def assign_values(
    page: dict, components: list[dict], consumed_texts: set[tuple]
) -> None:
    wires = (page.get("decoded") or {}).get("wires", [])
    aligned_pair_scores = []
    body_pair_scores = []
    for component_index, component in enumerate(components):
        reference_text = component.get("reference_text")
        component["value"] = _default_value(component["reference"])
        reference_prefix = component["value"]
        if not reference_text:
            continue
        reference_bbox = _text_bbox(reference_text)
        reference_center = (
            (reference_bbox["x0"] + reference_bbox["x1"]) / 2,
            (reference_bbox["y0"] + reference_bbox["y1"]) / 2,
        )
        reference_angle = (
            int(round(reference_text.get("angle", 0))) % 180
        )
        size = max(float(reference_text.get("size") or 0), 0.1)
        for text_index, text in enumerate(page["texts"]):
            key = _text_key(text)
            if key in consumed_texts:
                continue
            value = text.get("text", "").strip()
            if (
                not value
                or text.get("color") != "#000000"
                or REF_RE.match(value)
            ):
                continue
            if (
                page.get("flavor") == "altium"
                and not text.get("font", "").startswith("Times")
            ):
                # Altium parameter texts are Times New Roman; Arial and
                # Courier hits are sheet annotations and pin texts.
                continue
            value_angle = int(round(text.get("angle", 0))) % 180
            same_angle = value_angle == reference_angle
            if (
                _looks_like_passive_value(value)
                and reference_prefix not in ("R", "C", "L", "FB")
            ):
                continue
            if (
                wires
                and LABEL_RE.match(value)
                and _nearest_wire_point(
                    [_local_label_anchor(text)],
                    wires,
                    max(0.42, float(text.get("size") or 0.1) * 0.30),
                )
            ):
                continue
            if abs(float(text.get("size") or 0) - size) > size * 0.35:
                continue
            text_bbox = _text_bbox(text)
            bbox_distance = _bbox_distance(reference_bbox, text_bbox)
            text_center = (
                (text_bbox["x0"] + text_bbox["x1"]) / 2,
                (text_bbox["y0"] + text_bbox["y1"]) / 2,
            )
            dx = text_center[0] - reference_center[0]
            dy = text_center[1] - reference_center[1]
            if reference_angle == 0:
                cross_distance = abs(dy)
                cross_offset = dy
                along_distance = abs(dx)
            else:
                cross_distance = abs(dx)
                cross_offset = dx
                along_distance = abs(dy)
            passive_prefix = reference_prefix in ("R", "C", "L", "FB")
            ordering_code_penalty = (
                20.0
                if passive_prefix and not _looks_like_passive_value(value)
                else 0.0
            )
            if (
                same_angle
                and bbox_distance <= 6.0
                and cross_distance <= (
                    max(4.5, size * 4.0)
                    if passive_prefix
                    else max(2.0, size * 1.8)
                )
            ):
                # Capture centers or aligns reference/value text along the
                # symbol axis.
                aligned_score = (
                    bbox_distance
                    + cross_distance * 4.0
                    + along_distance * 0.05
                    + ordering_code_penalty
                    + max(
                        0.0,
                        -cross_offset - REFERENCE_DIRECTION_TOL,
                    )
                    * 5.0
                )
                if (
                    reference_prefix in ("R", "C", "L", "FB")
                    and component.get("bbox")
                ):
                    component_bbox = component["bbox"]
                    component_center = (
                        (
                            component_bbox["x0"]
                            + component_bbox["x1"]
                        ) / 2,
                        (
                            component_bbox["y0"]
                            + component_bbox["y1"]
                        ) / 2,
                    )
                    aligned_score += (
                        _distance(text_center, component_center) * 3.0
                    )
                aligned_pair_scores.append(
                    (
                        aligned_score,
                        component_index,
                        text_index,
                        key,
                    )
                )

            component_bbox = component.get("bbox")
            if component_bbox and passive_prefix:
                body_distance = _bbox_distance(component_bbox, text_bbox)
                if body_distance <= 6.0:
                    component_center = (
                        (
                            component_bbox["x0"]
                            + component_bbox["x1"]
                        ) / 2,
                        (
                            component_bbox["y0"]
                            + component_bbox["y1"]
                        ) / 2,
                    )
                    body_pair_scores.append(
                        (
                            body_distance
                            + _distance(text_center, component_center) * 0.2
                            + (0.0 if same_angle else 0.5)
                            + ordering_code_penalty,
                            component_index,
                            text_index,
                            key,
                        )
                    )
            if (
                component_bbox
                and reference_prefix in (
                    "U",
                    "CN",
                    "J",
                    "JSW",
                    "FB",
                    "FL",
                    "SP",
                )
                and not (
                    reference_prefix == "U"
                    and (
                        value.isdigit()
                        or _looks_like_passive_value(value)
                    )
                )
            ):
                gap = text_bbox["y0"] - component_bbox["y1"]
                horizontal_gap = max(
                    component_bbox["x0"] - text_bbox["x1"],
                    text_bbox["x0"] - component_bbox["x1"],
                    0.0,
                )
                left_protrusion = max(
                    component_bbox["x0"] - text_bbox["x0"],
                    0.0,
                )
                center_alignment = abs(
                    text_center[0]
                    - (
                        component_bbox["x0"] + component_bbox["x1"]
                    ) / 2
                )
                if (
                    gap >= 0.0
                    and gap <= max(3.0, size * 2.8)
                    and horizontal_gap <= max(1.5, size * 1.4)
                    and min(
                        left_protrusion,
                        center_alignment,
                    ) <= max(1.5, size * 1.4)
                ):
                    left_alignment = abs(
                        text_bbox["x0"] - component_bbox["x0"]
                    )
                    body_pair_scores.append(
                        (
                            max(gap, 0.0)
                            + horizontal_gap * 2.0
                            + min(
                                left_alignment,
                                center_alignment,
                            ) * 0.1,
                            component_index,
                            text_index,
                            key,
                        )
                    )

    paired_components = set()
    paired_texts = set()
    for _score, component_index, text_index in _maximum_cardinality_pairs(
        [
            (score, component_index, text_index)
            for score, component_index, text_index, _key
            in aligned_pair_scores
        ]
    ):
        key = _text_key(page["texts"][text_index])
        value_text = page["texts"][text_index]
        component = components[component_index]
        component["value"] = value_text["text"]
        component["value_text"] = value_text
        consumed_texts.add(key)
        paired_components.add(component_index)
        paired_texts.add(key)

    for _score, component_index, text_index in _maximum_cardinality_pairs(
        [
            (score, component_index, text_index)
            for score, component_index, text_index, key in body_pair_scores
            if (
                component_index not in paired_components
                and key not in paired_texts
            )
        ]
    ):
        key = _text_key(page["texts"][text_index])
        value_text = page["texts"][text_index]
        component = components[component_index]
        component["value"] = value_text["text"]
        component["value_text"] = value_text
        consumed_texts.add(key)
        paired_components.add(component_index)
        paired_texts.add(key)


def decode_power_ports(
    page: dict,
    wires: list[dict],
    consumed_texts: set[tuple],
    consumed_lines: set[int],
) -> list[dict]:
    decoded = page.get("decoded") or {}
    if not wires and not decoded.get("components"):
        return []

    ports = []
    seen = set()
    worksheet_line_indexes = set(
        ((decoded.get("worksheet") or {}).get("line_indexes") or [])
    )
    black_lines = [
        (index, line)
        for index, line in enumerate(page["lines"])
        if (
            line.get("color") == "#000000"
            and index not in worksheet_line_indexes
        )
    ]

    # GND is rendered as a closed triangle whose base midpoint is the wire
    # hotpoint.  pdf_dump already identifies the two diagonal arms.
    for chevron in pdf_dump._chevrons(page["lines"]):
        if worksheet_line_indexes.intersection(chevron["line_indexes"]):
            continue
        first = page["lines"][chevron["line_indexes"][0]]
        second = page["lines"][chevron["line_indexes"][1]]
        if first.get("color") != "#000000" or second.get("color") != "#000000":
            continue
        apex = chevron["apex"]

        def other_endpoint(line):
            p0, p1 = _point(line, 1), _point(line, 2)
            return p1 if _point_close(p0, apex, 0.13) else p0

        q, r = other_endpoint(first), other_endpoint(second)
        base_indexes = [
            index
            for index, line in black_lines
            if (
                (
                    _point_close(_point(line, 1), q, 0.13)
                    and _point_close(_point(line, 2), r, 0.13)
                )
                or (
                    _point_close(_point(line, 2), q, 0.13)
                    and _point_close(_point(line, 1), r, 0.13)
                )
            )
        ]
        if not base_indexes:
            continue
        hot = ((q[0] + r[0]) / 2, (q[1] + r[1]) / 2)
        wire_hit = _point_on_any_wire(hot, wires, 0.16)
        if wire_hit:
            hot = wire_hit[1]
        away = (apex[0] - hot[0], apex[1] - hot[1])
        length = math.hypot(*away)
        if length == 0:
            continue
        away = (away[0] / length, away[1] / length)
        names = []
        for text in page["texts"]:
            value = text.get("text", "").strip()
            if (
                text.get("color") != "#000000"
                or _text_key(text) in consumed_texts
                or not LABEL_RE.fullmatch(value)
                or REF_RE.fullmatch(value)
                or any(character.islower() for character in value)
                or re.search(r"(?:GND|VSS)", value) is None
            ):
                continue
            center = (
                (text["x"] + text["x1"]) / 2,
                (text["y"] + text["y1"]) / 2,
            )
            offset = (center[0] - hot[0], center[1] - hot[1])
            along = offset[0] * away[0] + offset[1] * away[1]
            across = abs(offset[0] * away[1] - offset[1] * away[0])
            if 0.5 <= along <= 6.0 and across <= 2.5:
                names.append((along + across * 2.0, text))
        text = min(names, key=lambda entry: entry[0])[1] if names else None
        name = text["text"].upper() if text else "GND"
        key = (name, round(hot[0], 2), round(hot[1], 2))
        if key not in seen:
            line_indexes = sorted({
                *chevron["line_indexes"],
                base_indexes[0],
            })
            ports.append({
                "name": name,
                "point": hot,
                "kind": "power",
                "glyph": "ground",
                "angle": {
                    "down": 0,
                    "right": 90,
                    "up": 180,
                    "left": 270,
                }[chevron["direction"]],
                "line_indexes": line_indexes,
                **({"text": text} if text else {}),
            })
            if text:
                consumed_texts.add(_text_key(text))
            consumed_lines.update(line_indexes)
            seen.add(key)

    # Positive rails use a short T bar and perpendicular stem.  The visible
    # power name is aligned beyond the bar, opposite the wire hotpoint.
    for bar_index, bar in black_lines:
        bar_start, bar_end = _point(bar, 1), _point(bar, 2)
        bar_length = _distance(bar_start, bar_end)
        if not (0.5 <= bar_length <= 3.0):
            continue
        horizontal = abs(bar_start[1] - bar_end[1]) <= 0.08
        vertical = abs(bar_start[0] - bar_end[0]) <= 0.08
        if not (horizontal or vertical):
            continue
        midpoint = (
            (bar_start[0] + bar_end[0]) / 2,
            (bar_start[1] + bar_end[1]) / 2,
        )
        for stem_index, stem in black_lines:
            if stem is bar:
                continue
            stem_start, stem_end = _point(stem, 1), _point(stem, 2)
            stem_length = _distance(stem_start, stem_end)
            if not (0.4 <= stem_length <= 3.0):
                continue
            stem_horizontal = abs(stem_start[1] - stem_end[1]) <= 0.08
            stem_vertical = abs(stem_start[0] - stem_end[0]) <= 0.08
            if horizontal and not stem_vertical:
                continue
            if vertical and not stem_horizontal:
                continue
            if _point_close(stem_start, midpoint, 0.13):
                hot = stem_end
            elif _point_close(stem_end, midpoint, 0.13):
                hot = stem_start
            else:
                continue
            wire_hit = _point_on_any_wire(hot, wires, 0.16)
            if wire_hit:
                hot = wire_hit[1]
            away = (midpoint[0] - hot[0], midpoint[1] - hot[1])
            length = math.hypot(*away)
            if length == 0:
                continue
            away = (away[0] / length, away[1] / length)
            names = []
            for text in page["texts"]:
                if (
                    text.get("color") != "#000000"
                    or _text_key(text) in consumed_texts
                    or not LABEL_RE.match(text.get("text", "").strip())
                ):
                    continue
                center = (
                    (text["x"] + text["x1"]) / 2,
                    (text["y"] + text["y1"]) / 2,
                )
                offset = (center[0] - midpoint[0], center[1] - midpoint[1])
                along = offset[0] * away[0] + offset[1] * away[1]
                across = abs(offset[0] * away[1] - offset[1] * away[0])
                if -0.5 <= along <= 10.0 and across <= 5.0:
                    names.append((along + across * 2.0, text))
            if not names:
                continue
            _score, text = min(names, key=lambda entry: entry[0])
            name = text["text"].upper()
            key = (name, round(hot[0], 2), round(hot[1], 2))
            if key not in seen:
                if abs(away[0]) >= abs(away[1]):
                    direction = "right" if away[0] > 0 else "left"
                else:
                    direction = "down" if away[1] > 0 else "up"
                line_indexes = sorted({bar_index, stem_index})
                ports.append(
                    {
                        "name": name,
                        "point": hot,
                        "kind": "power",
                        "glyph": "supply",
                        "angle": {
                            "up": 0,
                            "left": 90,
                            "down": 180,
                            "right": 270,
                        }[direction],
                        "text": text,
                        "line_indexes": line_indexes,
                    }
                )
                consumed_texts.add(_text_key(text))
                consumed_lines.update(line_indexes)
                seen.add(key)
    return ports


def decode_global_labels(
    page: dict,
    wires: list[dict],
    buses: list[dict],
    components: list[dict],
    consumed_texts: set[tuple],
    consumed_lines: set[int],
) -> list[dict]:
    # Capture prints cross-page destinations as separate red text spans beside
    # off-page ports.  Their placement varies between Capture versions, but
    # their syntax is unambiguous; never preserve them as residual graphics.
    for text in page["texts"]:
        if (
            text.get("color") == pdf_dump.GLOBAL_LABEL_TEXT_COLOR
            and GLOBAL_LABEL_PAGE_REFERENCE_RE.fullmatch(
                text.get("text", "").strip()
            )
        ):
            consumed_texts.add(_text_key(text))

    decoded = page.get("decoded") or {}
    if not wires and not buses and not decoded.get("components"):
        return []
    labels = []
    seen = set()
    for label in pdf_dump.decode_global_labels(page):
        apex = (label["apex"]["x"], label["apex"]["y"])
        base = (label["base"]["x"], label["base"]["y"])
        direction = label["direction"]
        line_indexes = [
            index
            for index in label.get("line_indexes", [])
            if 0 <= index < len(page.get("lines", []))
        ]
        glyph_points = [
            point
            for line_index in line_indexes
            for point in (
                _point(page["lines"][line_index], 1),
                _point(page["lines"][line_index], 2),
            )
        ]
        text = label.get("text") or {}
        if glyph_points and all(
            key in text for key in ("x", "y", "x1", "y1")
        ):
            # Bidirectional Capture ports contain opposing chevrons.  The
            # chevron nearest the text does not consistently point toward the
            # text, so selecting an extreme from its direction alone can put
            # the attachment one grid step onto the label body.  The wire is
            # at the glyph extreme opposite the source text.
            text_center = (
                (text["x"] + text["x1"]) / 2,
                (text["y"] + text["y1"]) / 2,
            )
            glyph_center = (
                sum(point[0] for point in glyph_points) / len(glyph_points),
                sum(point[1] for point in glyph_points) / len(glyph_points),
            )
            if direction in ("left", "right"):
                text_is_positive = text_center[0] >= glyph_center[0]
                point = (
                    (
                        min(point[0] for point in glyph_points)
                        if text_is_positive
                        else max(point[0] for point in glyph_points)
                    ),
                    apex[1],
                )
                direction = "right" if text_is_positive else "left"
            else:
                text_is_positive = text_center[1] >= glyph_center[1]
                point = (
                    apex[0],
                    (
                        min(point[1] for point in glyph_points)
                        if text_is_positive
                        else max(point[1] for point in glyph_points)
                    ),
                )
                direction = "down" if text_is_positive else "up"
        else:
            hotpoint = label.get("hotpoint")
            if hotpoint:
                point = (hotpoint["x"], hotpoint["y"])
            else:
                # Backward compatibility for decoder data without a complete
                # glyph hotpoint.
                point = (2 * base[0] - apex[0], 2 * base[1] - apex[1])

        hit = _nearest_wire_point([point], wires, 0.25)
        if hit:
            point = hit[1]
        else:
            bus_hit = _nearest_wire_point([point], buses, 0.25)
            if bus_hit:
                point = bus_hit[1]
            pins = [
                (pin["hot"]["x"], pin["hot"]["y"])
                for component in components
                for pin in component["pins"]
            ]
            pin_hits = [
                (_distance(point, pin), pin)
                for pin in pins
                if _distance(point, pin) <= 0.25
            ]
            if not bus_hit and pin_hits:
                point = min(pin_hits, key=lambda candidate: candidate[0])[1]
        name = label["name"].upper()
        key = (name, round(point[0], 2), round(point[1], 2))
        if key in seen:
            continue
        angle = {
            "right": 0,
            "left": 180,
            "up": 90,
            "down": 270,
        }[direction]
        labels.append(
            {
                "name": name,
                "point": point,
                "kind": "global",
                "angle": angle,
                "direction": direction,
            }
        )
        seen.add(key)
        consumed_lines.update(label.get("line_indexes", []))
        consumed_texts.update(
            _text_key(page_reference)
            for page_reference in label.get("page_references", [])
        )
        # Find the original full text span to suppress its graphical duplicate.
        for text in page["texts"]:
            if (
                text.get("text") == label["name"]
                and abs(text["x"] - label["text"]["x"]) <= GEOM_TOL
                and abs(text["y"] - label["text"]["y"]) <= GEOM_TOL
            ):
                consumed_texts.add(_text_key(text))
                break
    return labels


def decode_junctions(
    page: dict,
    wires: list[dict],
    consumed_curves: set[int],
) -> list[tuple[float, float]]:
    """Replace Capture's filled red PDF dots with native KiCad junctions."""
    eligible = []
    for index, curve in enumerate(page.get("curves", [])):
        if (
            curve.get("color") != JUNCTION_COLOR
            or curve.get("fill") != JUNCTION_COLOR
        ):
            continue
        bbox = _curve_bbox(curve)
        if max(bbox["x1"] - bbox["x0"], bbox["y1"] - bbox["y0"]) > 1.0:
            continue
        eligible.append((index, bbox))

    parent = list(range(len(eligible)))

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(first, second):
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    order = sorted(
        range(len(eligible)),
        key=lambda item: eligible[item][1]["x0"],
    )
    for order_index, first in enumerate(order):
        first_bbox = eligible[first][1]
        for second in order[order_index + 1:]:
            second_bbox = eligible[second][1]
            if second_bbox["x0"] > first_bbox["x1"] + 0.06:
                break
            if _bboxes_touch(first_bbox, second_bbox, 0.06):
                union(first, second)

    groups = {}
    for item in range(len(eligible)):
        groups.setdefault(find(item), []).append(item)

    endpoints = {
        (wire[end]["x"], wire[end]["y"])
        for wire in wires
        for end in ("start", "end")
    }
    junctions = []
    seen = set()
    for group in groups.values():
        if len(group) < 4:
            continue
        bboxes = [eligible[item][1] for item in group]
        bbox = {
            "x0": min(candidate["x0"] for candidate in bboxes),
            "y0": min(candidate["y0"] for candidate in bboxes),
            "x1": max(candidate["x1"] for candidate in bboxes),
            "y1": max(candidate["y1"] for candidate in bboxes),
        }
        width = bbox["x1"] - bbox["x0"]
        height = bbox["y1"] - bbox["y0"]
        if (
            not 0.15 <= width <= 1.2
            or not 0.15 <= height <= 1.2
            or not 0.65 <= width / height <= 1.55
        ):
            continue
        center = (
            (bbox["x0"] + bbox["x1"]) / 2,
            (bbox["y0"] + bbox["y1"]) / 2,
        )
        candidates = sorted(
            (
                (_distance(center, endpoint), endpoint)
                for endpoint in endpoints
                if _distance(center, endpoint) <= max(width, height) * 0.8
            ),
            key=lambda candidate: candidate[0],
        )
        point = next(
            (
                candidate
                for _distance_to_center, candidate in candidates
                if _wire_degree(candidate, wires) >= 3
            ),
            None,
        )
        if point is None:
            continue
        key = (round(point[0], 3), round(point[1], 3))
        if key in seen:
            consumed_curves.update(eligible[item][0] for item in group)
            continue
        seen.add(key)
        junctions.append(point)
        consumed_curves.update(eligible[item][0] for item in group)
    junctions.sort(key=lambda point: (point[1], point[0]))
    return junctions


def decode_buses(page: dict) -> list[dict]:
    """Decode Capture's blue bus segments without treating entries as wires."""
    buses = []
    seen = set()
    for line in page.get("lines", []):
        if line.get("color") != BUS_COLOR:
            continue
        start = _point(line, 1)
        end = _point(line, 2)
        key = tuple(
            round(value, 2)
            for point in sorted((start, end))
            for value in point
        )
        if key in seen:
            continue
        seen.add(key)
        buses.append(
            {
                "start": {"x": start[0], "y": start[1]},
                "end": {"x": end[0], "y": end[1]},
            }
        )
    buses.sort(
        key=lambda bus: (
            bus["start"]["y"],
            bus["start"]["x"],
            bus["end"]["y"],
            bus["end"]["x"],
        )
    )
    return buses


def decode_bus_entries(
    wires: list[dict], buses: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Separate diagonal Capture bus entries from ordinary signal wires."""
    signal_wires = []
    entries = []
    for wire in wires:
        start = (wire["start"]["x"], wire["start"]["y"])
        end = (wire["end"]["x"], wire["end"]["y"])
        dx, dy = end[0] - start[0], end[1] - start[1]
        if (
            min(abs(dx), abs(dy)) < 0.4
            or max(abs(dx), abs(dy)) > 3.0
            or abs(abs(dx) - abs(dy)) > 0.1
        ):
            signal_wires.append(wire)
            continue
        start_on_bus = _point_on_any_wire(start, buses, 0.08)
        end_on_bus = _point_on_any_wire(end, buses, 0.08)
        if bool(start_on_bus) == bool(end_on_bus):
            signal_wires.append(wire)
            continue
        bus_point, wire_point = (
            (start, end) if start_on_bus else (end, start)
        )
        entries.append(
            {
                # KiCad's bus-entry origin is the signal-wire end; its size
                # vector points from there to the bus.
                "start": {"x": wire_point[0], "y": wire_point[1]},
                "end": {"x": bus_point[0], "y": bus_point[1]},
            }
        )
    entries.sort(
        key=lambda entry: (
            entry["start"]["y"],
            entry["start"]["x"],
            entry["end"]["y"],
            entry["end"]["x"],
        )
    )
    return signal_wires, entries


def decode_no_connects(
    page: dict,
    components: list[dict],
    consumed_lines: set[int],
) -> list[tuple[float, float]]:
    """Recover Capture's brown X markers as native KiCad no-connects."""
    no_connects = []
    seen = set()
    for component in components:
        for pin in component.get("pins", []):
            hot = (pin["hot"]["x"], pin["hot"]["y"])
            positive = []
            negative = []
            for index, line in enumerate(page["lines"]):
                if line.get("color") != NO_CONNECT_COLOR:
                    continue
                first, second = _point(line, 1), _point(line, 2)
                dx, dy = second[0] - first[0], second[1] - first[1]
                length = math.hypot(dx, dy)
                center = (
                    (first[0] + second[0]) / 2,
                    (first[1] + second[1]) / 2,
                )
                # Capture draws four short X arms; Altium two full diagonals.
                if (
                    not 0.5 <= length <= 3.4
                    or min(abs(dx), abs(dy)) < 0.25
                    or _distance(center, hot) > 0.15
                ):
                    continue
                target = positive if dx * dy > 0 else negative
                target.append((_distance(center, hot), index))
            if not positive or not negative:
                continue
            point_key = (round(hot[0], 3), round(hot[1], 3))
            if point_key not in seen:
                no_connects.append(hot)
                seen.add(point_key)
            consumed_lines.add(min(positive)[1])
            consumed_lines.add(min(negative)[1])
    no_connects.sort(key=lambda point: (point[1], point[0]))
    return no_connects


def decode_local_labels(
    page: dict, wires: list[dict], consumed_texts: set[tuple]
) -> list[dict]:
    labels = []
    seen = set()
    for text in page["texts"]:
        key = _text_key(text)
        value = text.get("text", "").strip()
        if (
            key in consumed_texts
            or text.get("color") not in ("#000000", "#008000")
            or not LABEL_RE.match(value)
            or ("_" not in value and any(character.islower() for character in value))
            or len(value) > 100
        ):
            continue
        bbox = _text_bbox(text)
        size = max(float(text.get("size") or 0), 0.1)
        anchor = _local_label_anchor(text)
        max_distance = max(0.42, size * 0.30)
        # The Capture text baseline is the electrical anchor.  In dense bus
        # breakouts a far text-box corner may be even closer to a diagonal bus
        # entry, so only consider corners when the baseline has no wire.
        hit = _nearest_wire_point([anchor], wires, max_distance)
        fallback_points = [
            (bbox["x0"], bbox["y0"]),
            (bbox["x0"], bbox["y1"]),
            (bbox["x1"], bbox["y0"]),
            (bbox["x1"], bbox["y1"]),
            ((bbox["x0"] + bbox["x1"]) / 2, bbox["y0"]),
            ((bbox["x0"] + bbox["x1"]) / 2, bbox["y1"]),
            (bbox["x0"], (bbox["y0"] + bbox["y1"]) / 2),
            (bbox["x1"], (bbox["y0"] + bbox["y1"]) / 2),
        ]
        if not hit:
            hit = _nearest_wire_point(
                fallback_points,
                wires,
                max_distance,
            )
        if not hit:
            continue
        point = hit[1]
        name = value.upper()
        label_key = (name, round(point[0], 2), round(point[1], 2))
        if label_key in seen:
            consumed_texts.add(key)
            continue
        labels.append(
            {
                "name": name,
                "point": point,
                "angle": int(round(text.get("angle", 0))) % 360,
                "kind": "local",
                "text": text,
            }
        )
        consumed_texts.add(key)
        seen.add(label_key)
    return labels


def decode_power_ports_altium(
    page: dict,
    wires: list[dict],
    components: list[dict],
    consumed_texts: set[tuple],
    consumed_lines: set[int],
    consumed_curves: set[int],
) -> list[dict]:
    """Recover Altium power ports: a maroon glyph beside a maroon net name.

    Style 2 (GND bar) and style 6 (earth) are line-only glyphs; style 0
    rails carry a small circle.  The stub endpoint on a wire or pin is the
    electrical hotpoint.
    """
    if not wires and not components:
        return []
    accent = pdf_dump.ALTIUM_ACCENT_COLOR
    records = []
    for index, line in enumerate(page["lines"]):
        if (
            index in consumed_lines
            or line.get("color") != accent
            or line.get("dashed")
            or pdf_dump._line_length(line) < GEOM_TOL
        ):
            continue
        records.append(("line", index, line, _bbox_for_lines([line])))
    for index, curve in enumerate(page.get("curves", [])):
        if index in consumed_curves or curve.get("color") != accent:
            continue
        records.append(("curve", index, curve, _curve_bbox(curve)))
    if not records:
        return []

    parent = list(range(len(records)))

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(first, second):
        first, second = find(first), find(second)
        if first != second:
            parent[second] = first

    for first in range(len(records)):
        for second in range(first + 1, len(records)):
            if _bboxes_touch(records[first][3], records[second][3], 0.3):
                union(first, second)
    groups: dict[int, list[int]] = {}
    for item in range(len(records)):
        groups.setdefault(find(item), []).append(item)

    pin_hots = [
        (pin["hot"]["x"], pin["hot"]["y"])
        for component in components
        for pin in component.get("pins", [])
    ]
    ports = []
    seen = set()
    for group in groups.values():
        bbox = _bbox_union(*(records[item][3] for item in group))
        glyph_points = [
            point
            for item in group
            if records[item][0] == "line"
            for point in (
                _point(records[item][2], 1),
                _point(records[item][2], 2),
            )
        ]
        if not glyph_points:
            continue
        hot = None
        hits = []
        for point in glyph_points:
            wire_hit = _point_on_any_wire(point, wires, 0.3)
            if wire_hit:
                hits.append((wire_hit[0], wire_hit[1]))
        if hits:
            hot = min(hits)[1]
        else:
            pin_hits = [
                (_distance(point, pin), pin)
                for point in glyph_points
                for pin in pin_hots
                if _distance(point, pin) <= 0.3
            ]
            if pin_hits:
                hot = min(pin_hits)[1]
        if hot is None:
            continue
        names = []
        for text in page["texts"]:
            value = text.get("text", "").strip()
            if (
                text.get("color") != accent
                or _text_key(text) in consumed_texts
                or not ALTIUM_NET_NAME_RE.match(value)
            ):
                continue
            distance = _bbox_distance(bbox, _text_bbox(text))
            if distance <= 4.0:
                names.append((distance, text))
        if not names:
            continue
        _score, text = min(names, key=lambda entry: entry[0])
        name = text["text"].strip()
        key = (name, round(hot[0], 2), round(hot[1], 2))
        if key in seen:
            continue
        seen.add(key)
        glyph = (
            "supply"
            if any(records[item][0] == "curve" for item in group)
            else "ground"
        )
        center = (
            (bbox["x0"] + bbox["x1"]) / 2,
            (bbox["y0"] + bbox["y1"]) / 2,
        )
        away = (center[0] - hot[0], center[1] - hot[1])
        if abs(away[0]) >= abs(away[1]):
            direction = "right" if away[0] > 0 else "left"
        else:
            direction = "down" if away[1] > 0 else "up"
        angle_map = (
            {"down": 0, "right": 90, "up": 180, "left": 270}
            if glyph == "ground"
            else {"up": 0, "left": 90, "down": 180, "right": 270}
        )
        line_indexes = sorted(
            records[item][1] for item in group if records[item][0] == "line"
        )
        ports.append({
            "name": name,
            "point": hot,
            "kind": "power",
            "glyph": glyph,
            "angle": angle_map[direction],
            "text": text,
            "line_indexes": line_indexes,
        })
        consumed_texts.add(_text_key(text))
        consumed_lines.update(line_indexes)
        consumed_curves.update(
            records[item][1] for item in group if records[item][0] == "curve"
        )
    ports.sort(key=lambda port: (port["point"][1], port["point"][0]))
    return ports


def decode_net_labels_altium(
    page: dict, wires: list[dict], consumed_texts: set[tuple]
) -> list[dict]:
    """Altium net labels: maroon texts anchored on a wire, globally scoped."""
    labels = []
    seen = set()
    accent = pdf_dump.ALTIUM_ACCENT_COLOR
    for text in page["texts"]:
        key = _text_key(text)
        value = text.get("text", "").strip()
        if (
            key in consumed_texts
            or text.get("color") != accent
            or not ALTIUM_NET_NAME_RE.match(value)
            or len(value) > 100
        ):
            continue
        bbox = _text_bbox(text)
        size = max(float(text.get("size") or 0), 0.1)
        max_distance = max(0.8, size * 0.6)
        probe_points = [
            (bbox["x0"], bbox["y0"]),
            (bbox["x0"], bbox["y1"]),
            (bbox["x1"], bbox["y0"]),
            (bbox["x1"], bbox["y1"]),
            ((bbox["x0"] + bbox["x1"]) / 2, bbox["y0"]),
            ((bbox["x0"] + bbox["x1"]) / 2, bbox["y1"]),
            (bbox["x0"], (bbox["y0"] + bbox["y1"]) / 2),
            (bbox["x1"], (bbox["y0"] + bbox["y1"]) / 2),
        ]
        hit = _nearest_wire_point(probe_points, wires, max_distance)
        if not hit:
            continue
        point = hit[1]
        label_key = (value, round(point[0], 2), round(point[1], 2))
        if label_key in seen:
            consumed_texts.add(key)
            continue
        center = (
            (bbox["x0"] + bbox["x1"]) / 2,
            (bbox["y0"] + bbox["y1"]) / 2,
        )
        if int(round(text.get("angle", 0))) % 180 == 0:
            direction = "right" if point[0] <= center[0] else "left"
        else:
            direction = "down" if point[1] <= center[1] else "up"
        labels.append({
            "name": value,
            "point": point,
            "kind": "global",
            "angle": {
                "right": 0,
                "left": 180,
                "up": 90,
                "down": 270,
            }[direction],
            "direction": direction,
        })
        consumed_texts.add(key)
        seen.add(label_key)
    labels.sort(key=lambda label: (label["point"][1], label["point"][0]))
    return labels


def decode_page(page: dict) -> dict:
    flavor = page.get("flavor")
    decoded = page.get("decoded") or pdf_dump.decode_page(page)
    buses = decode_buses(page)
    wires, bus_entries = decode_bus_entries(decoded["wires"], buses)
    components, consumed_texts, semantic_lines = decode_components(page)
    semantic_rectangles = {
        index
        for component in components
        for index in component.get("rectangle_indexes", set())
    }
    semantic_curves = {
        index
        for component in components
        for index in component.get("curve_indexes", set())
    }
    junctions = decode_junctions(page, wires, semantic_curves)
    no_connects = decode_no_connects(
        page,
        components,
        semantic_lines,
    )
    if flavor == "altium":
        power_ports = decode_power_ports_altium(
            page,
            wires,
            components,
            consumed_texts,
            semantic_lines,
            semantic_curves,
        )
    else:
        power_ports = decode_power_ports(
            page,
            wires,
            consumed_texts,
            semantic_lines,
        )
    assign_values(page, components, consumed_texts)
    if flavor == "altium":
        # Flat net-identifier scope: every Altium net label merges by name
        # across all pages, which is exactly a KiCad global label.
        global_labels = decode_net_labels_altium(page, wires, consumed_texts)
        local_labels = []
    else:
        global_labels = decode_global_labels(
            page,
            wires,
            buses,
            components,
            consumed_texts,
            semantic_lines,
        )
        local_labels = decode_local_labels(page, wires, consumed_texts)
    worksheet = decoded.get("worksheet")
    if worksheet:
        semantic_lines.update(worksheet.get("line_indexes", []))
        semantic_rectangles.update(worksheet.get("rectangle_indexes", []))
        semantic_curves.update(worksheet.get("curve_indexes", []))
        consumed_texts.update(
            _text_key(page["texts"][index])
            for index in worksheet.get("text_indexes", [])
            if 0 <= index < len(page["texts"])
        )
    return {
        "components": components,
        "wires": wires,
        "buses": buses,
        "bus_entries": bus_entries,
        "junctions": junctions,
        "no_connects": no_connects,
        "power_ports": power_ports,
        "global_labels": global_labels,
        "local_labels": local_labels,
        "consumed_texts": consumed_texts,
        "semantic_lines": semantic_lines,
        "semantic_rectangles": semantic_rectangles,
        "semantic_curves": semantic_curves,
        "worksheet": worksheet,
    }


def detect_multi_units(semantics: list[dict]) -> dict[str, list[dict]]:
    """Detect U1A, U1B, ... designators and annotate true KiCad units.

    A group is accepted only when every suffix occurs exactly once, there are
    at least two units, and the suffixes form an uninterrupted sequence
    beginning with A.  A bare reference with the same base (for example U1)
    makes the group ambiguous and prevents merging.
    """
    candidates: dict[str, dict[str, list[dict]]] = {}
    bare_references = set()
    for semantic in semantics:
        for component in semantic["components"]:
            reference = component["reference"]
            match = MULTI_UNIT_REF_RE.fullmatch(reference)
            if match:
                base, suffix = match.groups()
                candidates.setdefault(base, {}).setdefault(suffix, []).append(
                    component
                )
            elif re.fullmatch(r"[A-Z]{1,4}\d+", reference) and component.get(
                "pins"
            ):
                # A pinless text-only match is too weak to veto unit merging.
                bare_references.add(reference)

    groups = {}
    for base, suffix_map in sorted(candidates.items()):
        if base in bare_references or any(
            len(components) != 1 for components in suffix_map.values()
        ):
            continue
        suffixes = sorted(suffix_map)
        if len(suffixes) < 2:
            continue
        expected = [
            chr(ord("A") + index)
            for index in range(ord(suffixes[-1]) - ord("A") + 1)
        ]
        if suffixes != expected:
            continue

        members = []
        for unit, suffix in enumerate(suffixes, 1):
            component = suffix_map[suffix][0]
            component["source_reference"] = component["reference"]
            component["reference"] = base
            component["unit"] = unit
            component["multi_unit"] = base
            component["multi_unit_count"] = len(suffixes)
            members.append(component)

        # A multi-unit KiCad symbol has one shared value.  Prefer unit A's
        # recovered value, which is also deterministic when only the generic
        # "U" fallback is available.
        shared_value = members[0]["value"]
        shared_dnp = any(component.get("dnp", False) for component in members)
        shared_footprints = {
            component["footprint"]
            for component in members
            if component.get("footprint")
        }
        for component in members:
            component["value"] = shared_value
            component["dnp"] = shared_dnp
            if len(shared_footprints) == 1:
                component["footprint"] = next(iter(shared_footprints))
        groups[base] = members
    return groups


def rename_duplicate_references(
    semantics: list[dict], multi_unit_groups: dict[str, list[dict]]
) -> dict[str, int]:
    """Give repeated designators on different pages unique suffixes.

    A PDF of a multi-channel or alternate-assembly design can print the same
    logical designator on several pages; KiCad would fold those symbols into
    one component and corrupt the netlist.  Members of a detected multi-unit
    group intentionally share their reference and are left alone.
    """
    multi_unit_members = {
        id(component)
        for members in multi_unit_groups.values()
        for component in members
    }
    seen: dict[str, int] = {}
    renamed: dict[str, int] = {}
    for semantic in semantics:
        for component in semantic["components"]:
            if id(component) in multi_unit_members:
                continue
            reference = component.get("reference")
            if not reference:
                continue
            occurrence = seen.get(reference, 0) + 1
            seen[reference] = occurrence
            if occurrence > 1:
                component["source_reference"] = reference
                component["reference"] = f"{reference}_{occurrence}"
                renamed[reference] = renamed.get(reference, 1) + 1
    return renamed


def _rgba(color: str | None, alpha=1) -> str:
    if not color or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        return f"0 0 0 {alpha}"
    return (
        f"{int(color[1:3], 16)} {int(color[3:5], 16)} "
        f"{int(color[5:7], 16)} {alpha}"
    )


def _header(
    factory: UuidFactory,
    paper: str,
    title: str,
    worksheet_fields: dict | None = None,
    schematic_uuid: str | None = None,
) -> str:
    fields = worksheet_fields or {}
    title_block = [
        "\t(title_block\n",
        f'\t\t(title "{_esc(fields.get("title") or title)}")\n',
    ]
    for key, kicad_name in (
        ("date", "date"),
        ("revision", "rev"),
        ("company", "company"),
    ):
        if fields.get(key):
            title_block.append(
                f'\t\t({kicad_name} "{_esc(fields[key])}")\n'
            )
    comments = []
    if fields.get("document_number"):
        comments.append(f'Document Number: {fields["document_number"]}')
    if fields.get("page_name"):
        comments.append(f'Page Name: {fields["page_name"]}')
    if fields.get("sheet"):
        source_sheet = f'Source sheet {fields["sheet"]}'
        if fields.get("sheet_count"):
            source_sheet += f' of {fields["sheet_count"]}'
        comments.append(source_sheet)
    comments.append(
        "Reconstructed from PDF; hidden metadata may be unavailable"
    )
    for number, comment in enumerate(comments[:9], 1):
        title_block.append(
            f'\t\t(comment {number} "{_esc(comment)}")\n'
        )
    title_block.append("\t)\n")
    return (
        "(kicad_sch\n"
        f"\t(version {KICAD_VERSION})\n"
        '\t(generator "pdf2kicad")\n'
        '\t(generator_version "0.1")\n'
        f'\t(uuid "{schematic_uuid or factory.new("schematic")}")\n'
        f'\t(paper "{paper}")\n'
        + "".join(title_block)
    )


def _wire(factory, transform, wire) -> str:
    x1, y1 = transform.xy(wire["start"]["x"], wire["start"]["y"])
    x2, y2 = transform.xy(wire["end"]["x"], wire["end"]["y"])
    return (
        "\t(wire\n"
        f"\t\t(pts (xy {x1:.2f} {y1:.2f}) (xy {x2:.2f} {y2:.2f}))\n"
        "\t\t(stroke (width 0) (type default))\n"
        f'\t\t(uuid "{factory.new("wire")}")\n'
        "\t)\n"
    )


def _junction(factory, transform, point) -> str:
    x, y = transform.xy(*point)
    return (
        "\t(junction\n"
        f"\t\t(at {x:.2f} {y:.2f})\n"
        "\t\t(diameter 0)\n"
        "\t\t(color 0 0 0 0)\n"
        f'\t\t(uuid "{factory.new("junction")}")\n'
        "\t)\n"
    )


def _bus(factory, transform, bus) -> str:
    x1, y1 = transform.xy(bus["start"]["x"], bus["start"]["y"])
    x2, y2 = transform.xy(bus["end"]["x"], bus["end"]["y"])
    return (
        "\t(bus\n"
        f"\t\t(pts (xy {x1:.2f} {y1:.2f}) (xy {x2:.2f} {y2:.2f}))\n"
        "\t\t(stroke (width 0) (type default))\n"
        f'\t\t(uuid "{factory.new("bus")}")\n'
        "\t)\n"
    )


def _bus_entry(factory, transform, entry) -> str:
    x, y = transform.xy(entry["start"]["x"], entry["start"]["y"])
    dx = transform.delta(entry["end"]["x"] - entry["start"]["x"])
    dy = transform.delta(entry["end"]["y"] - entry["start"]["y"])
    return (
        "\t(bus_entry\n"
        f"\t\t(at {x:.2f} {y:.2f})\n"
        f"\t\t(size {dx:.2f} {dy:.2f})\n"
        "\t\t(stroke (width 0) (type default))\n"
        f'\t\t(uuid "{factory.new("bus-entry")}")\n'
        "\t)\n"
    )


def _no_connect(factory, transform, point) -> str:
    x, y = transform.xy(*point)
    return (
        "\t(no_connect\n"
        f"\t\t(at {x:.2f} {y:.2f})\n"
        f'\t\t(uuid "{factory.new("no-connect")}")\n'
        "\t)\n"
    )


def _label(factory, transform, label) -> str:
    x, y = transform.xy(*label["point"])
    angle = int(label.get("angle", 0)) % 360
    name = _esc(label["name"])
    if label["kind"] == "global":
        justify = "right" if angle == 180 else "left"
        return (
            f'\t(global_label "{name}"\n'
            "\t\t(shape bidirectional)\n"
            f"\t\t(at {x:.2f} {y:.2f} {angle})\n"
            f"\t\t(effects (font (size 1.27 1.27)) (justify {justify}))\n"
            f'\t\t(uuid "{factory.new("global-label")}")\n'
            '\t\t(property "Intersheetrefs" "${INTERSHEET_REFS}"\n'
            "\t\t\t(at 0 0 0)\n"
            "\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n"
            "\t\t)\n"
            "\t)\n"
        )
    justify = "right bottom" if angle in (180, 270) else "left bottom"
    return (
        f'\t(label "{name}"\n'
        f"\t\t(at {x:.2f} {y:.2f} {angle})\n"
        f"\t\t(effects (font (size 1.27 1.27)) (justify {justify}))\n"
        f'\t\t(uuid "{factory.new("label")}")\n'
        "\t)\n"
    )


def _power_lib_name(power: dict) -> str:
    name = str(power.get("name") or "").strip().upper()
    fallback = "GND" if power.get("glyph") == "ground" else "VCC"
    return STANDARD_POWER_NAME_MAP.get(name, fallback)


def _power_symbol_definition(power: dict) -> str:
    name = _esc(_power_lib_name(power))
    upper_name = name.upper()
    ground = upper_name.startswith(("GND", "EARTH"))
    negative = name.startswith("-") or upper_name in ("VEE", "VSS", "VSSA")
    if ground:
        reference_y = -6.35
        value_y = -3.81
        pin_angle = 270
        body = (
            f'\t\t\t(symbol "{name}_0_1"\n'
            "\t\t\t\t(polyline\n"
            "\t\t\t\t\t(pts (xy 0 0) (xy 0 -1.27) "
            "(xy 1.27 -1.27) (xy 0 -2.54) "
            "(xy -1.27 -1.27) (xy 0 -1.27))\n"
            "\t\t\t\t\t(stroke (width 0) (type default))\n"
            "\t\t\t\t\t(fill (type none))\n"
            "\t\t\t\t)\n"
            "\t\t\t)\n"
        )
    elif negative:
        reference_y = 3.81
        value_y = -3.556
        pin_angle = 270
        body = (
            f'\t\t\t(symbol "{name}_0_1"\n'
            "\t\t\t\t(polyline\n"
            "\t\t\t\t\t(pts (xy -0.762 -1.27) (xy 0 -2.54))\n"
            "\t\t\t\t\t(stroke (width 0) (type default))\n"
            "\t\t\t\t\t(fill (type none))\n"
            "\t\t\t\t)\n"
            "\t\t\t\t(polyline\n"
            "\t\t\t\t\t(pts (xy 0 -2.54) (xy 0.762 -1.27))\n"
            "\t\t\t\t\t(stroke (width 0) (type default))\n"
            "\t\t\t\t\t(fill (type none))\n"
            "\t\t\t\t)\n"
            "\t\t\t\t(polyline\n"
            "\t\t\t\t\t(pts (xy 0 0) (xy 0 -2.54))\n"
            "\t\t\t\t\t(stroke (width 0) (type default))\n"
            "\t\t\t\t\t(fill (type none))\n"
            "\t\t\t\t)\n"
            "\t\t\t)\n"
        )
    else:
        reference_y = -3.81
        value_y = 3.556
        pin_angle = 90
        body = (
            f'\t\t\t(symbol "{name}_0_1"\n'
            "\t\t\t\t(polyline\n"
            "\t\t\t\t\t(pts (xy -0.762 1.27) (xy 0 2.54))\n"
            "\t\t\t\t\t(stroke (width 0) (type default))\n"
            "\t\t\t\t\t(fill (type none))\n"
            "\t\t\t\t)\n"
            "\t\t\t\t(polyline\n"
            "\t\t\t\t\t(pts (xy 0 2.54) (xy 0.762 1.27))\n"
            "\t\t\t\t\t(stroke (width 0) (type default))\n"
            "\t\t\t\t\t(fill (type none))\n"
            "\t\t\t\t)\n"
            "\t\t\t\t(polyline\n"
            "\t\t\t\t\t(pts (xy 0 0) (xy 0 2.54))\n"
            "\t\t\t\t\t(stroke (width 0) (type default))\n"
            "\t\t\t\t\t(fill (type none))\n"
            "\t\t\t\t)\n"
            "\t\t\t)\n"
        )
    return (
        f'\t\t(symbol "power:{name}"\n'
        "\t\t\t(power global)\n"
        "\t\t\t(pin_numbers hide)\n"
        "\t\t\t(pin_names (offset 0) hide)\n"
        "\t\t\t(exclude_from_sim no)\n"
        "\t\t\t(in_bom yes)\n"
        "\t\t\t(on_board yes)\n"
        '\t\t\t(property "Reference" "#PWR"\n'
        f"\t\t\t\t(at 0 {reference_y} 0)\n"
        "\t\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n"
        "\t\t\t)\n"
        f'\t\t\t(property "Value" "{name}"\n'
        f"\t\t\t\t(at 0 {value_y} 0)\n"
        "\t\t\t\t(effects (font (size 1.27 1.27)))\n"
        "\t\t\t)\n"
        f"{body}"
        f'\t\t\t(symbol "{name}_1_1"\n'
        "\t\t\t\t(pin power_in line\n"
        f"\t\t\t\t\t(at 0 0 {pin_angle})\n"
        "\t\t\t\t\t(length 0)\n"
        '\t\t\t\t\t(name "" '
        "(effects (font (size 1.27 1.27))))\n"
        '\t\t\t\t\t(number "1" '
        "(effects (font (size 1.27 1.27))))\n"
        "\t\t\t\t)\n"
        "\t\t\t)\n"
        "\t\t)\n"
    )


def _power_symbol_instance(
    factory,
    transform,
    power: dict,
    project_name: str = "",
    instance_path: str = "/",
) -> str:
    x, y = transform.xy(*power["point"])
    angle = int(power.get("angle", 0)) % 360
    name = _esc(power["name"])
    lib_name = _esc(_power_lib_name(power))
    reference = _esc(power.get("reference", "#PWR0001"))
    text = power.get("text")
    if text:
        value_x, value_y = transform.xy(
            (text["x"] + text["x1"]) / 2,
            (text["y"] + text["y1"]) / 2,
        )
        value_angle = int(round(text.get("angle", 0))) % 360
        value_hide = ""
    else:
        value_x, value_y = x, y
        value_angle = angle
        value_hide = " (hide yes)"
    return (
        "\t(symbol\n"
        f'\t\t(lib_id "power:{lib_name}")\n'
        f"\t\t(at {x:.2f} {y:.2f} {angle})\n"
        "\t\t(unit 1)\n"
        "\t\t(exclude_from_sim no)\n"
        "\t\t(in_bom yes)\n"
        "\t\t(on_board yes)\n"
        "\t\t(dnp no)\n"
        f'\t\t(uuid "{factory.new("power-symbol")}")\n'
        f'\t\t(property "Reference" "{reference}"\n'
        f"\t\t\t(at {x:.2f} {y:.2f} {angle})\n"
        "\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n"
        "\t\t)\n"
        f'\t\t(property "Value" "{name}"\n'
        f"\t\t\t(at {value_x:.2f} {value_y:.2f} {value_angle})\n"
        f"\t\t\t(effects (font (size 1.27 1.27)){value_hide})\n"
        "\t\t)\n"
        f'\t\t(pin "1" (uuid "{factory.new("power-pin")}"))\n'
        "\t\t(instances\n"
        f'\t\t\t(project "{_esc(project_name)}"\n'
        f'\t\t\t\t(path "{_esc(instance_path)}"\n'
        f'\t\t\t\t\t(reference "{reference}")\n'
        "\t\t\t\t\t(unit 1)\n"
        "\t\t\t\t)\n"
        "\t\t\t)\n"
        "\t\t)\n"
        "\t)\n"
    )


def _pin_direction(pin: dict) -> int:
    hot = pin["hot"]
    other = pin["other"]
    # KiCad library coordinates are Y-up while schematic/PDF coordinates are
    # Y-down.
    dx = other["x"] - hot["x"]
    dy = hot["y"] - other["y"]
    if abs(dx) >= abs(dy):
        return 0 if dx > 0 else 180
    return 90 if dy > 0 else 270


def _two_terminal_axis(component: dict) -> str | None:
    """Return the common axis of a genuine two-terminal passive body."""
    pins = component.get("pins", [])
    if len(pins) != 2:
        return None
    vectors = [
        (
            pin["other"]["x"] - pin["hot"]["x"],
            pin["other"]["y"] - pin["hot"]["y"],
        )
        for pin in pins
    ]
    lengths = [math.hypot(*vector) for vector in vectors]
    if min(lengths) <= 0.01:
        return None
    dot = (
        vectors[0][0] * vectors[1][0]
        + vectors[0][1] * vectors[1][1]
    )
    cross = abs(
        vectors[0][0] * vectors[1][1]
        - vectors[0][1] * vectors[1][0]
    )
    if dot > -0.98 * lengths[0] * lengths[1]:
        return None
    if cross > 0.05 * lengths[0] * lengths[1]:
        return None
    hot_delta = (
        pins[1]["hot"]["x"] - pins[0]["hot"]["x"],
        pins[1]["hot"]["y"] - pins[0]["hot"]["y"],
    )
    hot_separation = math.hypot(*hot_delta)
    hot_cross = abs(
        vectors[0][0] * hot_delta[1]
        - vectors[0][1] * hot_delta[0]
    )
    if hot_cross > 0.05 * lengths[0] * max(hot_separation, 0.01):
        return None
    return "horizontal" if abs(hot_delta[0]) >= abs(hot_delta[1]) else "vertical"


def _pin_numbers_hidden(components: list[dict]) -> bool:
    return all(
        component.get("standard_passive")
        or len(component.get("pins", [])) == 1
        or _has_symmetric_two_pin_geometry(component)
        for component in components
    )


def _minimum_pin_length(components: list[dict]) -> float:
    """Return the pin length needed by the longest visible pin number."""
    longest_number = max(
        (
            len(str(pin.get("number") or ""))
            for component in components
            for pin in component.get("pins", [])
        ),
        default=0,
    )
    return max(PIN_LENGTH, (longest_number + 1) * PIN_TEXT_SIZE)


def _pin_for_output(
    transform: CoordinateTransform,
    component: dict,
    pin: dict,
    minimum_length: float | None,
) -> tuple[tuple[float, float], float]:
    """Return the PDF-space hotpoint and KiCad length for an emitted pin."""
    hot = (pin["hot"]["x"], pin["hot"]["y"])
    other = (pin["other"]["x"], pin["other"]["y"])
    standard_hotpoints = component.get("standard_hotpoints")
    if standard_hotpoints:
        return standard_hotpoints[str(pin["number"])], PIN_LENGTH
    current_length = max(
        0.01,
        transform.delta(pin.get("length", _distance(hot, other))),
    )
    if minimum_length is None or current_length >= minimum_length - 0.01:
        return hot, current_length

    dx, dy = other[0] - hot[0], other[1] - hot[1]
    distance = math.hypot(dx, dy)
    if distance > 0.01:
        inward_x, inward_y = dx / distance, dy / distance
    else:
        bbox = component["bbox"]
        center_x = (bbox["x0"] + bbox["x1"]) / 2
        center_y = (bbox["y0"] + bbox["y1"]) / 2
        center_dx, center_dy = hot[0] - center_x, hot[1] - center_y
        if abs(center_dx) >= abs(center_dy):
            inward_x = 1.0 if center_dx <= 0 else -1.0
            inward_y = 0.0
        else:
            inward_x = 0.0
            inward_y = 1.0 if center_dy <= 0 else -1.0

    source_length = minimum_length / transform.scale
    return (
        other[0] - inward_x * source_length,
        other[1] - inward_y * source_length,
    ), minimum_length


def _pin_point_key(point: tuple[float, float]) -> tuple[float, float]:
    return round(point[0], 3), round(point[1], 3)


def _relocated_point(
    point: tuple[float, float],
    relocations: dict[tuple[float, float], tuple[float, float]],
) -> tuple[float, float]:
    return relocations.get(_pin_point_key(point), point)


def _wire_key(
    wire: dict,
) -> tuple[tuple[float, float], tuple[float, float]]:
    return tuple(sorted((
        _pin_point_key((wire["start"]["x"], wire["start"]["y"])),
        _pin_point_key((wire["end"]["x"], wire["end"]["y"])),
    )))


def _wire_endpoint_can_move(
    wire: dict,
    old_point: tuple[float, float],
    new_point: tuple[float, float],
) -> bool:
    """Return whether moving an endpoint preserves the wire's direction."""
    start = (wire["start"]["x"], wire["start"]["y"])
    end = (wire["end"]["x"], wire["end"]["y"])
    if _point_close(old_point, start):
        other_point = end
    elif _point_close(old_point, end):
        other_point = start
    else:
        return False
    wire_dx = other_point[0] - old_point[0]
    wire_dy = other_point[1] - old_point[1]
    wire_length = math.hypot(wire_dx, wire_dy)
    if wire_length <= 0.001:
        return False
    move_dx = new_point[0] - old_point[0]
    move_dy = new_point[1] - old_point[1]
    cross_distance = abs(move_dx * wire_dy - move_dy * wire_dx) / wire_length
    return cross_distance <= GEOM_TOL


def _wire_keeps_point(
    wire: dict,
    old_point: tuple[float, float],
    new_point: tuple[float, float],
) -> bool:
    start = (wire["start"]["x"], wire["start"]["y"])
    end = (wire["end"]["x"], wire["end"]["y"])
    distance, _nearest = _point_to_segment(old_point, start, end)
    if distance > GEOM_TOL:
        return False
    if not (
        _point_close(old_point, start)
        or _point_close(old_point, end)
    ):
        return True
    return not _wire_endpoint_can_move(wire, old_point, new_point)


@dataclass
class PinRelocation:
    native_moves: dict[tuple[float, float], tuple[float, float]]
    standard_moves: dict[tuple[float, float], tuple[float, float]]
    object_moves: dict[tuple[float, float], tuple[float, float]]
    bridges: list[dict]
    suppressed_wires: set[
        tuple[tuple[float, float], tuple[float, float]]
    ]


def _pin_relocations(
    components: list[dict],
    transform: CoordinateTransform,
    multi_unit_groups: dict[str, list[dict]],
    semantic: dict | None = None,
) -> PinRelocation:
    """Resolve native pin extension and stock Device pin relocation."""
    semantic = semantic or {}
    wires = semantic.get("wires", [])
    pin_points: dict[tuple[float, float], dict] = {}
    standard_pin_points: dict[tuple[float, float], dict] = {}
    standard_pin_pairs = set()
    pin_owners = []
    for component in components:
        group_name = component.get("multi_unit")
        symbol_components = (
            multi_unit_groups[group_name]
            if group_name in multi_unit_groups
            else [component]
        )
        minimum_length = (
            None
            if _pin_numbers_hidden(symbol_components)
            else _minimum_pin_length(symbol_components)
        )
        component_transform = component.get("_transform", transform)
        original_pin_keys = []
        for pin in component.get("pins", []):
            old_hot = (pin["hot"]["x"], pin["hot"]["y"])
            old_key = _pin_point_key(old_hot)
            original_pin_keys.append(old_key)
            pin_owners.append((old_key, (component.get("reference") or "")))
            new_hot, _length = _pin_for_output(
                component_transform,
                component,
                pin,
                minimum_length,
            )
            if component.get("standard_passive"):
                if not _point_close(old_hot, new_hot, 0.001):
                    record = standard_pin_points.setdefault(
                        old_key,
                        {"old": old_hot, "moves": []},
                    )
                    record["moves"].append({
                        "reference": (component.get("reference") or ""),
                        "new": new_hot,
                    })
                continue
            record = pin_points.setdefault(
                old_key,
                {"old": old_hot, "outputs": []},
            )
            if not any(
                _point_close(new_hot, output, 0.001)
                for output in record["outputs"]
            ):
                record["outputs"].append(new_hot)
        if component.get("standard_passive") and len(original_pin_keys) == 2:
            standard_pin_pairs.add(tuple(sorted(original_pin_keys)))

    # A nonstandard symbol pin can share the recovered hotpoint of a stock
    # Device pin. Keep that old point as the native symbol's anchor so its own
    # pin-length adjustment gets a bridge instead of silently disconnecting.
    for key, standard_record in standard_pin_points.items():
        if key not in pin_points:
            continue
        old_hot = standard_record["old"]
        if not any(
            _point_close(old_hot, output, 0.001)
            for output in pin_points[key]["outputs"]
        ):
            pin_points[key]["outputs"].append(old_hot)

    native_moves = {}
    bridges = []
    seen_bridges = set()

    def add_bridge(
        old_point: tuple[float, float],
        new_point: tuple[float, float],
    ) -> None:
        bridge_key = tuple(
            sorted((_pin_point_key(old_point), _pin_point_key(new_point)))
        )
        if bridge_key in seen_bridges:
            return
        bridges.append(
            {
                "start": {"x": old_point[0], "y": old_point[1]},
                "end": {"x": new_point[0], "y": new_point[1]},
            }
        )
        seen_bridges.add(bridge_key)

    for key, record in pin_points.items():
        old_hot = record["old"]
        outputs = record["outputs"]
        if all(_point_close(old_hot, output, 0.001) for output in outputs):
            continue
        stationary = next(
            (
                output
                for output in outputs
                if _point_close(old_hot, output, 0.001)
            ),
            None,
        )
        anchor = stationary or outputs[0]
        native_moves[key] = anchor
        for output in outputs:
            if _point_close(anchor, output, 0.001):
                continue
            add_bridge(anchor, output)

    suppressed_wires = {
        _wire_key(wire)
        for wire in wires
        if _wire_key(wire) in standard_pin_pairs
    }
    regular_wires = [
        wire for wire in wires if _wire_key(wire) not in suppressed_wires
    ]
    power_points = {
        _pin_point_key(tuple(power["point"]))
        for power in semantic.get("power_ports", [])
    }
    label_points = {
        _pin_point_key(tuple(label["point"]))
        for label in (
            semantic.get("global_labels", [])
            + semantic.get("local_labels", [])
        )
    }

    standard_moves = {}
    anchored_standard_points = set()
    for key, record in standard_pin_points.items():
        distinct_outputs = []
        for move in record["moves"]:
            if not any(
                _point_close(move["new"], output, 0.001)
                for output in distinct_outputs
            ):
                distinct_outputs.append(move["new"])
        if len(distinct_outputs) == 1:
            standard_moves[key] = distinct_outputs[0]
        else:
            anchored_standard_points.add(key)

        old_hot = record["old"]
        for move in record["moves"]:
            new_hot = move["new"]
            reference = move["reference"]
            anchored = (
                key in anchored_standard_points
                or key in power_points
                or key in label_points
                or any(
                    owner_key == key and owner != reference
                    for owner_key, owner in pin_owners
                )
                or any(
                    _wire_keeps_point(wire, old_hot, new_hot)
                    for wire in regular_wires
                )
            )
            if anchored:
                anchored_standard_points.add(key)
                add_bridge(old_hot, new_hot)

    object_moves = dict(native_moves)
    for key, new_hot in standard_moves.items():
        if key in anchored_standard_points:
            object_moves.pop(key, None)
        else:
            object_moves[key] = new_hot

    return PinRelocation(
        native_moves=native_moves,
        standard_moves=standard_moves,
        object_moves=object_moves,
        bridges=bridges,
        suppressed_wires=suppressed_wires,
    )


def _wire_with_relocated_pins(
    wire: dict,
    relocation: PinRelocation,
) -> dict:
    def relocated_endpoint(point: tuple[float, float]) -> tuple[float, float]:
        standard_point = relocation.standard_moves.get(_pin_point_key(point))
        if standard_point is not None and _wire_endpoint_can_move(
            wire,
            point,
            standard_point,
        ):
            return standard_point
        return _relocated_point(point, relocation.native_moves)

    start = relocated_endpoint((wire["start"]["x"], wire["start"]["y"]))
    end = relocated_endpoint((wire["end"]["x"], wire["end"]["y"]))
    return {
        **wire,
        "start": {"x": start[0], "y": start[1]},
        "end": {"x": end[0], "y": end[1]},
    }


def _has_symmetric_two_pin_geometry(component: dict) -> bool:
    pins = component.get("pins", [])
    if _two_terminal_axis(component) is None or any(
        pin.get("name") not in (None, "", "~") for pin in pins
    ):
        return False
    vectors = [
        (
            pin["other"]["x"] - pin["hot"]["x"],
            pin["other"]["y"] - pin["hot"]["y"],
        )
        for pin in pins
    ]
    lengths = [math.hypot(*vector) for vector in vectors]
    if min(lengths) <= 0.01:
        return False
    if abs(lengths[0] - lengths[1]) > max(lengths) * 0.05:
        return False
    return True


def _symbol_local_point(
    transform: CoordinateTransform,
    origin: tuple[float, float],
    point: tuple[float, float],
) -> tuple[float, float]:
    """Return a local point whose emitted absolute position stays exact."""
    origin_x, origin_y = transform.xy(*origin)
    point_x, point_y = transform.xy(*point)
    return round(point_x - origin_x, 2), round(origin_y - point_y, 2)


def _symbol_unit_definition(
    transform: CoordinateTransform,
    component: dict,
    base: str,
    unit: int,
    *,
    multi_unit: bool,
    minimum_pin_length: float | None,
) -> str:
    bbox = component["bbox"]
    origin = (
        (bbox["x0"] + bbox["x1"]) / 2,
        (bbox["y0"] + bbox["y1"]) / 2,
    )

    def local(point):
        return _symbol_local_point(transform, origin, point)

    body = []
    for line in component.get("body_lines", []):
        x1, y1 = local(_point(line, 1))
        x2, y2 = local(_point(line, 2))
        width = max(0.15, transform.delta(line.get("width", 0.1)))
        body.append(
            "\t\t\t\t(polyline\n"
            f"\t\t\t\t\t(pts (xy {x1:.2f} {y1:.2f}) "
            f"(xy {x2:.2f} {y2:.2f}))\n"
            f"\t\t\t\t\t(stroke (width {width:.2f}) (type default))\n"
            "\t\t\t\t\t(fill (type none))\n"
            "\t\t\t\t)\n"
        )
    for rectangle in component.get("body_rectangles", []):
        x0, y0 = local((rectangle["x0"], rectangle["y0"]))
        x1, y1 = local((rectangle["x1"], rectangle["y1"]))
        width = max(
            0.15,
            transform.delta(rectangle.get("width", 0.1)),
        )
        body.append(
            "\t\t\t\t(rectangle\n"
            f"\t\t\t\t\t(start {x0:.2f} {y0:.2f})\n"
            f"\t\t\t\t\t(end {x1:.2f} {y1:.2f})\n"
            f"\t\t\t\t\t(stroke (width {width:.2f}) (type default))\n"
            "\t\t\t\t\t(fill (type none))\n"
            "\t\t\t\t)\n"
        )
    for curve in component.get("body_curves", []):
        points = " ".join(
            f"(xy {x:.2f} {y:.2f})"
            for x, y in (
                local(tuple(point)) for point in curve["points"]
            )
        )
        width = max(0.15, transform.delta(curve.get("width", 0.1)))
        body.append(
            "\t\t\t\t(polyline\n"
            f"\t\t\t\t\t(pts {points})\n"
            f"\t\t\t\t\t(stroke (width {width:.2f}) (type default))\n"
            "\t\t\t\t\t(fill (type none))\n"
            "\t\t\t\t)\n"
        )
    if not body:
        x0, y0 = local((bbox["x0"], bbox["y0"]))
        x1, y1 = local((bbox["x1"], bbox["y1"]))
        if abs(x1 - x0) < 0.2:
            x0, x1 = -1.27, 1.27
        if abs(y1 - y0) < 0.2:
            y0, y1 = -1.27, 1.27
        body.append(
            "\t\t\t\t(rectangle\n"
            f"\t\t\t\t\t(start {x0:.2f} {y0:.2f})\n"
            f"\t\t\t\t\t(end {x1:.2f} {y1:.2f})\n"
            "\t\t\t\t\t(stroke (width 0.15) (type default))\n"
            "\t\t\t\t\t(fill (type background))\n"
            "\t\t\t\t)\n"
        )

    pin_parts = []
    for pin in component["pins"]:
        hot, length = _pin_for_output(
            transform,
            component,
            pin,
            minimum_pin_length,
        )
        x, y = local(hot)
        number = _esc(pin["number"])
        name = _esc(pin.get("name") or "~")
        graphic_style = pin.get("graphic_style", "line")
        if graphic_style not in ("line", "inverted"):
            graphic_style = "line"
        pin_parts.append(
            f"\t\t\t\t(pin passive {graphic_style}\n"
            f"\t\t\t\t\t(at {x:.2f} {y:.2f} {_pin_direction(pin)})\n"
            f"\t\t\t\t\t(length {length:.2f})\n"
            f'\t\t\t\t\t(name "{name}" '
            "(effects (font (size 1.27 1.27))))\n"
            f'\t\t\t\t\t(number "{number}" '
            "(effects (font (size 1.27 1.27))))\n"
            "\t\t\t\t)\n"
        )

    body_name = f"{base}_{unit}_0" if multi_unit else f"{base}_0_1"
    return (
        f'\t\t\t(symbol "{body_name}"\n'
        + "".join(body)
        + "\t\t\t)\n"
        f'\t\t\t(symbol "{base}_{unit}_1"\n'
        + "".join(pin_parts)
        + "\t\t\t)\n"
    )


def _symbol_definition(
    _factory: UuidFactory,
    transform: CoordinateTransform,
    component: dict,
    lib_id: str,
    units: list[tuple[int, CoordinateTransform, dict]] | None = None,
) -> str:
    base = lib_id.split(":", 1)[-1]
    if units is None:
        units = [(1, transform, component)]
    unit_components = [
        unit_component
        for _unit, _transform, unit_component in units
    ]
    hide_pin_numbers = _pin_numbers_hidden(unit_components)
    minimum_pin_length = (
        None if hide_pin_numbers else _minimum_pin_length(unit_components)
    )
    hide_names = "\t\t\t(pin_names (offset 1.016) hide)\n" if not any(
        pin.get("name")
        for unit_component in unit_components
        for pin in unit_component["pins"]
    ) else "\t\t\t(pin_names (offset 1.016))\n"
    hide_numbers = "\t\t\t(pin_numbers hide)\n" if hide_pin_numbers else ""
    footprint = _esc(component.get("footprint") or "")
    footprint_property = (
        f'\t\t\t(property "Footprint" "{footprint}"\n'
        "\t\t\t\t(at 0 0 0)\n"
        "\t\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n"
        "\t\t\t)\n"
        if footprint else ""
    )
    return (
        f'\t\t(symbol "{lib_id}"\n'
        "\t\t\t(exclude_from_sim no)\n"
        "\t\t\t(in_bom yes)\n"
        "\t\t\t(on_board yes)\n"
        f"{hide_numbers}"
        f"{hide_names}"
        '\t\t\t(property "Reference" "U"\n'
        "\t\t\t\t(at 0 -2.54 0)\n"
        "\t\t\t\t(effects (font (size 1.27 1.27)))\n"
        "\t\t\t)\n"
        f'\t\t\t(property "Value" "{_esc(component["value"])}"\n'
        "\t\t\t\t(at 0 2.54 0)\n"
        "\t\t\t\t(effects (font (size 1.27 1.27)))\n"
        "\t\t\t)\n"
        f"{footprint_property}"
        + "".join(
            _symbol_unit_definition(
                unit_transform,
                unit_component,
                base,
                unit,
                multi_unit=len(units) > 1,
                minimum_pin_length=minimum_pin_length,
            )
            for unit, unit_transform, unit_component in units
        )
        + "\t\t)\n"
    )


def _component_instance(
    factory: UuidFactory,
    transform: CoordinateTransform,
    component: dict,
    lib_id: str,
    project_name: str = "",
    instance_path: str = "/",
) -> str:
    bbox = component["bbox"]
    origin = component.get("standard_origin") or (
        (bbox["x0"] + bbox["x1"]) / 2,
        (bbox["y0"] + bbox["y1"]) / 2,
    )
    x, y = transform.xy(*origin)
    instance_angle = int(component.get("standard_angle", 0)) % 360
    ref_text = component.get("reference_text")
    if ref_text:
        ref_at = transform.xy(
            (ref_text["x"] + ref_text["x1"]) / 2,
            (ref_text["y"] + ref_text["y1"]) / 2,
        )
        ref_angle = int(round(ref_text.get("angle", 0))) % 180
        ref_size = max(
            0.8,
            transform.delta(ref_text.get("size") or 1.27)
            / KICAD_OUTLINE_FONT_COMPENSATION,
        )
    else:
        ref_at = (x, y - 2.54)
        ref_angle = 0
        ref_size = 1.27
    value_text = component.get("value_text")
    if value_text:
        value_at = transform.xy(
            (value_text["x"] + value_text["x1"]) / 2,
            (value_text["y"] + value_text["y1"]) / 2,
        )
        value_angle = int(round(value_text.get("angle", 0))) % 180
        value_size = max(
            0.8,
            transform.delta(value_text.get("size") or 1.27)
            / KICAD_OUTLINE_FONT_COMPENSATION,
        )
    else:
        value_at = (x, y + 2.54)
        value_angle = 0
        value_size = 1.27
    ref_angle = (ref_angle - instance_angle) % 180
    value_angle = (value_angle - instance_angle) % 180
    reference = _esc(component["reference"])
    value = _esc(component["value"])
    footprint = _esc(component.get("footprint") or "")
    dnp = bool(component.get("dnp"))
    unit = component.get("unit", 1)
    ref_style = _text_font_style(ref_text or {})
    value_style = _text_font_style(value_text or {})
    parts = [
        "\t(symbol\n",
        f'\t\t(lib_id "{lib_id}")\n',
        f"\t\t(at {x:.2f} {y:.2f} {instance_angle})\n",
        f"\t\t(unit {unit})\n",
        "\t\t(exclude_from_sim no)\n",
        f"\t\t(in_bom {'no' if dnp else 'yes'})\n",
        "\t\t(on_board yes)\n",
        f"\t\t(dnp {'yes' if dnp else 'no'})\n",
        f'\t\t(uuid "{factory.new("symbol")}")\n',
        f'\t\t(property "Reference" "{reference}"\n',
        f"\t\t\t(at {ref_at[0]:.2f} {ref_at[1]:.2f} {ref_angle})\n",
        f'\t\t\t(effects (font (face "{ORCAD_TEXT_FACE}") '
        f"(size {ref_size:.2f} {ref_size:.2f}){ref_style}))\n",
        "\t\t)\n",
        f'\t\t(property "Value" "{value}"\n',
        f"\t\t\t(at {value_at[0]:.2f} {value_at[1]:.2f} {value_angle})\n",
        f'\t\t\t(effects (font (face "{ORCAD_TEXT_FACE}") '
        f"(size {value_size:.2f} {value_size:.2f}){value_style}))\n",
        "\t\t)\n",
    ]
    if footprint:
        parts.extend(
            [
                f'\t\t(property "Footprint" "{footprint}"\n',
                f"\t\t\t(at {x:.2f} {y:.2f} 0)\n",
                "\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n",
                "\t\t)\n",
            ]
        )
    for pin in component["pins"]:
        parts.extend(
            [
                f'\t\t(pin "{_esc(pin["number"])}"\n',
                f'\t\t\t(uuid "{factory.new("symbol-pin")}")\n',
                "\t\t)\n",
            ]
        )
    parts.extend(
        [
            "\t\t(instances\n",
            f'\t\t\t(project "{_esc(project_name)}"\n',
            f'\t\t\t\t(path "{_esc(instance_path)}"\n',
            f'\t\t\t\t\t(reference "{reference}")\n',
            f"\t\t\t\t\t(unit {unit})\n",
            "\t\t\t\t)\n",
            "\t\t\t)\n",
            "\t\t)\n",
            "\t)\n",
        ]
    )
    return "".join(parts)


def _source_color(primitive: dict) -> str | None:
    """The authored color of a primitive the decoder may have recolored."""
    if "source_color" in primitive:
        return primitive["source_color"]
    return primitive.get("color")


def _graphic_line(factory, transform, line) -> str:
    x1, y1 = transform.xy(line["x1"], line["y1"])
    x2, y2 = transform.xy(line["x2"], line["y2"])
    width = max(0.05, transform.delta(line.get("width") or 0.05))
    return (
        "\t(polyline\n"
        f"\t\t(pts (xy {x1:.2f} {y1:.2f}) (xy {x2:.2f} {y2:.2f}))\n"
        f"\t\t(stroke (width {width:.2f}) (type default) "
        f"(color {_rgba(_source_color(line))}))\n"
        f'\t\t(uuid "{factory.new("graphic-line")}")\n'
        "\t)\n"
    )


def _graphic_rectangle(factory, transform, rectangle) -> str:
    x0, y0 = transform.xy(rectangle["x0"], rectangle["y0"])
    x1, y1 = transform.xy(rectangle["x1"], rectangle["y1"])
    width = max(0.05, transform.delta(rectangle.get("width") or 0.05))
    fill = rectangle.get("fill")
    fill_part = (
        f"(fill (type color) (color {_rgba(fill)}))"
        if fill
        else "(fill (type none))"
    )
    return (
        "\t(rectangle\n"
        f"\t\t(start {x0:.2f} {y0:.2f})\n"
        f"\t\t(end {x1:.2f} {y1:.2f})\n"
        f"\t\t(stroke (width {width:.2f}) (type default) "
        f"(color {_rgba(_source_color(rectangle))}))\n"
        f"\t\t{fill_part}\n"
        f'\t\t(uuid "{factory.new("graphic-rectangle")}")\n'
        "\t)\n"
    )


def _graphic_curve(factory, transform, curve) -> str:
    points = " ".join(
        f"(xy {transform.value(point[0]):.2f} {transform.value(point[1]):.2f})"
        for point in curve["points"]
    )
    width = max(0.05, transform.delta(curve.get("width") or 0.05))
    return (
        "\t(polyline\n"
        f"\t\t(pts {points})\n"
        f"\t\t(stroke (width {width:.2f}) (type default) "
        f"(color {_rgba(_source_color(curve))}))\n"
        f'\t\t(uuid "{factory.new("graphic-curve")}")\n'
        "\t)\n"
    )


def _text_font_style(text: dict) -> str:
    styles = []
    if text.get("bold"):
        styles.append("(bold yes)")
    if text.get("italic"):
        styles.append("(italic yes)")
    return (" " + " ".join(styles)) if styles else ""


def _graphic_text(factory, transform, text) -> str:
    x, y = transform.xy(text["x"], text["y1"])
    size = max(
        0.5,
        transform.delta(text.get("size") or 1.0)
        / KICAD_OUTLINE_FONT_COMPENSATION,
    )
    angle = int(round(text.get("angle", 0))) % 360
    nudge = 0.2 * size
    if angle == 0:
        y += nudge
    elif angle == 90:
        x += nudge
    elif angle == 180:
        y -= nudge
    elif angle == 270:
        x -= nudge
    style_part = _text_font_style(text)
    return (
        f'\t(text "{_esc(text["text"])}"\n'
        "\t\t(exclude_from_sim no)\n"
        f"\t\t(at {x:.2f} {y:.2f} {angle})\n"
        f'\t\t(effects (font (face "{ORCAD_TEXT_FACE}") '
        f"(size {size:.2f} {size:.2f}){style_part} "
        f"(color {_rgba(_source_color(text))})) (justify left bottom))\n"
        f'\t\t(uuid "{factory.new("graphic-text")}")\n'
        "\t)\n"
    )


def _component_lib_ids(components: list[dict]) -> list[str]:
    multi_unit_ids = {}
    used_names = set()
    lib_ids = []
    for component in components:
        standard_lib_id = component.get("standard_lib_id")
        if standard_lib_id:
            lib_ids.append(standard_lib_id)
            continue

        multi_unit = component.get("multi_unit")
        if multi_unit in multi_unit_ids:
            lib_ids.append(multi_unit_ids[multi_unit])
            continue

        safe_value = _safe_library_symbol_name(
            component.get("value") or "Symbol"
        )
        safe_name = safe_value
        suffix = 2
        while safe_name in used_names:
            safe_name = f"{safe_value}_{suffix}"
            suffix += 1
        used_names.add(safe_name)
        lib_id = f"pdf2kicad:{safe_name}"
        if multi_unit:
            multi_unit_ids[multi_unit] = lib_id
        lib_ids.append(lib_id)
    return lib_ids


def render_page(
    factory: UuidFactory,
    page: dict,
    semantic: dict,
    transform: CoordinateTransform,
    title: str,
    keep_graphics: bool,
    multi_unit_groups: dict[str, list[dict]] | None = None,
    *,
    project_name: str = "",
    instance_path: str = "/",
) -> str:
    multi_unit_groups = multi_unit_groups or {}
    worksheet_fields = (
        (semantic.get("worksheet") or {}).get("fields") or {}
    )
    pin_relocation = _pin_relocations(
        semantic["components"],
        transform,
        multi_unit_groups,
        semantic,
    )
    parts = [
        _header(factory, transform.paper, title, worksheet_fields),
        "\t(lib_symbols\n",
    ]
    emitted_power_definitions = set()
    for power in semantic["power_ports"]:
        lib_name = _power_lib_name(power)
        if lib_name not in emitted_power_definitions:
            parts.append(_power_symbol_definition(power))
            emitted_power_definitions.add(lib_name)
    lib_ids = _component_lib_ids(semantic["components"])
    emitted_definitions = set()
    for component, lib_id in zip(semantic["components"], lib_ids):
        multi_unit = component.get("multi_unit")
        standard_lib_id = component.get("standard_lib_id")
        if lib_id in emitted_definitions:
            continue
        if standard_lib_id:
            parts.append(_standard_symbol_definition(standard_lib_id))
        elif multi_unit:
            units = [
                (member["unit"], member["_transform"], member)
                for member in multi_unit_groups[multi_unit]
            ]
            parts.append(
                _symbol_definition(
                    factory,
                    transform,
                    component,
                    lib_id,
                    units=units,
                )
            )
        else:
            parts.append(_symbol_definition(factory, transform, component, lib_id))
        emitted_definitions.add(lib_id)
    parts.append("\t)\n")

    for wire in semantic["wires"]:
        if _wire_key(wire) in pin_relocation.suppressed_wires:
            continue
        parts.append(
            _wire(
                factory,
                transform,
                _wire_with_relocated_pins(wire, pin_relocation),
            )
        )
    for bridge in pin_relocation.bridges:
        parts.append(_wire(factory, transform, bridge))
    for bus in semantic.get("buses", []):
        parts.append(_bus(factory, transform, bus))
    for entry in semantic.get("bus_entries", []):
        parts.append(_bus_entry(factory, transform, entry))
    for junction in semantic.get("junctions", []):
        parts.append(
            _junction(
                factory,
                transform,
                _relocated_point(junction, pin_relocation.object_moves),
            )
        )
    for point in semantic.get("no_connects", []):
        parts.append(
            _no_connect(
                factory,
                transform,
                _relocated_point(point, pin_relocation.object_moves),
            )
        )
    for component, lib_id in zip(semantic["components"], lib_ids):
        parts.append(
            _component_instance(
                factory,
                transform,
                component,
                lib_id,
                project_name,
                instance_path,
            )
        )
    for power in semantic["power_ports"]:
        relocated_power = {
            **power,
            "point": _relocated_point(
                power["point"],
                pin_relocation.object_moves,
            ),
        }
        parts.append(
            _power_symbol_instance(
                factory,
                transform,
                relocated_power,
                project_name,
                instance_path,
            )
        )
    for label in semantic["global_labels"] + semantic["local_labels"]:
        relocated_label = {
            **label,
            "point": _relocated_point(
                label["point"],
                pin_relocation.object_moves,
            ),
        }
        parts.append(_label(factory, transform, relocated_label))

    if keep_graphics:
        for index, line in enumerate(page["lines"]):
            if (
                line.get("color") in (WIRE_COLOR, BUS_COLOR)
                or index in semantic["semantic_lines"]
            ):
                continue
            if "source_color" in line and line["source_color"] is None:
                # A recolored fill boundary had no visible stroke of its own.
                continue
            parts.append(_graphic_line(factory, transform, line))
        for index, rectangle in enumerate(page["rectangles"]):
            if index not in semantic["semantic_rectangles"]:
                parts.append(_graphic_rectangle(factory, transform, rectangle))
        for index, curve in enumerate(page["curves"]):
            if index not in semantic["semantic_curves"]:
                parts.append(_graphic_curve(factory, transform, curve))
        for text in page["texts"]:
            if _text_key(text) not in semantic["consumed_texts"]:
                parts.append(_graphic_text(factory, transform, text))

    parts.append("\t(embedded_fonts no)\n)\n")
    return "".join(parts)


def render_root(
    factory: UuidFactory,
    project_name: str,
    page_filenames: list[str],
    page_names: list[str],
    root_uuid: str,
    sheet_uuids: list[str],
) -> str:
    parts = [
        _header(
            factory,
            "A3",
            f"{project_name} (PDF import)",
            schematic_uuid=root_uuid,
        ),
        "\t(lib_symbols)\n",
    ]
    rows = 4
    for index, (filename, name, sheet_uuid) in enumerate(
        zip(page_filenames, page_names, sheet_uuids)
    ):
        row, column = index % rows, index // rows
        x, y = 15 + column * 68, 25 + row * 17
        parts.append(
            "\t(sheet\n"
            f"\t\t(at {x} {y})\n"
            "\t\t(size 60 12)\n"
            "\t\t(exclude_from_sim no)\n"
            "\t\t(in_bom yes)\n"
            "\t\t(on_board yes)\n"
            "\t\t(dnp no)\n"
            "\t\t(fields_autoplaced yes)\n"
            "\t\t(stroke (width 0.1524) (type solid))\n"
            "\t\t(fill (color 0 0 0 0))\n"
            f'\t\t(uuid "{sheet_uuid}")\n'
            f'\t\t(property "Sheetname" "{_esc(name)}"\n'
            f"\t\t\t(at {x} {y - 0.7} 0)\n"
            "\t\t\t(show_name no)\n"
            "\t\t\t(do_not_autoplace no)\n"
            "\t\t\t(effects (font (size 1.27 1.27)) (justify left top))\n"
            "\t\t)\n"
            f'\t\t(property "Sheetfile" "{_esc(filename)}"\n'
            f"\t\t\t(at {x} {y + 12.7} 0)\n"
            "\t\t\t(show_name no)\n"
            "\t\t\t(do_not_autoplace no)\n"
            "\t\t\t(effects (font (size 1.27 1.27)) (justify left top))\n"
            "\t\t)\n"
            "\t\t(instances\n"
            f'\t\t\t(project "{_esc(project_name)}"\n'
            f'\t\t\t\t(path "/{root_uuid}"\n'
            f'\t\t\t\t\t(page "{index + 2}")\n'
            "\t\t\t\t)\n"
            "\t\t\t)\n"
            "\t\t)\n"
            "\t)\n"
        )
    parts.append(
        "\t(sheet_instances\n"
        '\t\t(path "/" (page "1"))\n'
        "\t)\n"
        "\t(embedded_fonts no)\n"
        ")\n"
    )
    return "".join(parts)


def generate_worksheet() -> str:
    """Render KiCad's default worksheet with its margins moved to the edges."""
    return (
        "(page_layout\n"
        "    (setup (textsize 1.5 1.5) (linewidth 0.15) "
        "(textlinewidth 0.15)\n"
        "      (left_margin 0) (right_margin 0) "
        "(top_margin 0) (bottom_margin 0))\n"
        "    (rect (comment \"rect around the title block\") "
        "(linewidth 0.15) (start 110 34) (end 2 2))\n"
        "    (rect (start 0 0 ltcorner) (end 0 0 rbcorner) "
        "(repeat 2) (incrx 2) (incry 2))\n"
        "    (line (start 50 2 ltcorner) (end 50 0 ltcorner) "
        "(repeat 30) (incrx 50))\n"
        "    (tbtext \"1\" (pos 25 1 ltcorner) "
        "(font (size 1.3 1.3)) (repeat 100) (incrx 50))\n"
        "    (line (start 50 2 lbcorner) (end 50 0 lbcorner) "
        "(repeat 30) (incrx 50))\n"
        "    (tbtext \"1\" (pos 25 1 lbcorner) "
        "(font (size 1.3 1.3)) (repeat 100) (incrx 50))\n"
        "    (line (start 0 50 ltcorner) (end 2 50 ltcorner) "
        "(repeat 30) (incry 50))\n"
        "    (tbtext \"A\" (pos 1 25 ltcorner) "
        "(font (size 1.3 1.3)) (justify center) "
        "(repeat 100) (incry 50))\n"
        "    (line (start 0 50 rtcorner) (end 2 50 rtcorner) "
        "(repeat 30) (incry 50))\n"
        "    (tbtext \"A\" (pos 1 25 rtcorner) "
        "(font (size 1.3 1.3)) (justify center) "
        "(repeat 100) (incry 50))\n"
        "    (tbtext \"Date: %D\" (pos 87 6.9))\n"
        "    (line (start 110 5.5) (end 2 5.5))\n"
        "    (tbtext \"%K\" (pos 109 4.1) "
        "(comment \"KiCad version\"))\n"
        "    (line (start 110 8.5) (end 2 8.5))\n"
        "    (tbtext \"Rev: %R\" (pos 24 6.9) "
        "(font bold) (justify left))\n"
        "    (tbtext \"Size: %Z\" (comment \"Paper format name\") "
        "(pos 109 6.9))\n"
        "    (tbtext \"Id: %S/%N\" (comment \"Sheet id\") "
        "(pos 24 4.1))\n"
        "    (line (start 110 12.5) (end 2 12.5))\n"
        "    (tbtext \"Title: %T\" (pos 109 10.7) "
        "(font bold italic (size 2 2)))\n"
        "    (tbtext \"File: %F\" (pos 109 14.3))\n"
        "    (line (start 110 18.5) (end 2 18.5))\n"
        "    (tbtext \"Sheet: %P\" (pos 109 17))\n"
        "    (tbtext \"%Y\" (comment \"Company name\") "
        "(pos 109 20) (font bold))\n"
        "    (tbtext \"%C0\" (comment \"Comment 0\") (pos 109 23))\n"
        "    (tbtext \"%C1\" (comment \"Comment 1\") (pos 109 26))\n"
        "    (tbtext \"%C2\" (comment \"Comment 2\") (pos 109 29))\n"
        "    (tbtext \"%C3\" (comment \"Comment 3\") (pos 109 32))\n"
        "    (line (start 90 8.5) (end 90 5.5))\n"
        "    (line (start 26 8.5) (end 26 2))\n"
        ")\n"
    )


def project_file(project_name: str, worksheet_name: str | None = None) -> str:
    schematic = {"drawing": {}, "meta": {"version": 1}}
    if worksheet_name:
        schematic["page_layout_descr_file"] = worksheet_name
    return json.dumps(
        {
            "meta": {
                "filename": f"{project_name}.kicad_pro",
                "version": 3,
            },
            "schematic": schematic,
        },
        indent=2,
    ) + "\n"


def convert_pdf(
    pdf_path: Path,
    *,
    paper: str = "auto",
    keep_graphics: bool = True,
    infer_footprints: bool = False,
    use_kicad_rcl: bool = False,
    flavor: str = "auto",
) -> tuple[dict[str, str], dict]:
    pdf_bytes = pdf_path.read_bytes()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if flavor == "auto":
        flavor = pdf_dump.detect_flavor(doc.metadata)
    pages = []
    for index in range(len(doc)):
        pages.append(
            {
                "page": index + 1,
                **pdf_dump.dump_page(
                    doc[index], raw=False, decode=True, flavor=flavor
                ),
            }
        )
    factory = UuidFactory(pdf_bytes)
    project_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", pdf_path.stem).strip("_")
    project_name = project_name or "pdf_schematic"

    output = {}
    page_files = []
    page_names = []
    summaries = []
    page_records = []
    sheet_names = detect_sheet_names(pages)
    for index, page in enumerate(pages, 1):
        selected_paper = detect_paper([page], paper)
        transform = coordinate_transform(selected_paper)
        semantic = decode_page(page)
        for component in semantic["components"]:
            _enrich_component(
                component,
                transform,
                infer_footprints=infer_footprints,
                use_kicad_rcl=use_kicad_rcl,
            )
        page_records.append(
            {
                "index": index,
                "page": page,
                "paper": selected_paper,
                "transform": transform,
                "semantic": semantic,
            }
        )

    multi_unit_groups = detect_multi_units(
        [record["semantic"] for record in page_records]
    )
    if flavor == "altium":
        # Multi-channel designs print the same logical designator on several
        # pages; keep the flat KiCad project annotation-unique.
        rename_duplicate_references(
            [record["semantic"] for record in page_records],
            multi_unit_groups,
        )
    source_has_worksheet = has_source_worksheet(page_records)
    root_uuid = factory.new("schematic")
    sheet_uuids = [
        factory.new("sheet")
        for _record in page_records
    ]
    power_number = 1
    for record in page_records:
        for power in record["semantic"]["power_ports"]:
            power["reference"] = f"#PWR{power_number:04d}"
            power_number += 1
    for record in page_records:
        index = record["index"]
        page = record["page"]
        selected_paper = record["paper"]
        transform = record["transform"]
        semantic = record["semantic"]
        page_name = sheet_names[index - 1]
        page_filename = f"{page_name}.kicad_sch"
        output[page_filename] = render_page(
            factory,
            page,
            semantic,
            transform,
            f"{page_name} (PDF import)",
            keep_graphics,
            multi_unit_groups,
            project_name=project_name,
            instance_path=f"/{root_uuid}/{sheet_uuids[index - 1]}",
        )
        page_files.append(page_filename)
        page_names.append(page_name)
        summaries.append(
            {
                "page": index,
                "sheet_name": page_name,
                "sheet_file": page_filename,
                "paper": selected_paper,
                "wires": len(semantic["wires"]),
                "buses": len(semantic["buses"]),
                "bus_entries": len(semantic["bus_entries"]),
                "junctions": len(semantic["junctions"]),
                "no_connects": len(semantic["no_connects"]),
                "components": len(semantic["components"]),
                "dnp_components": sum(
                    bool(component.get("dnp"))
                    for component in semantic["components"]
                ),
                "pins": sum(
                    len(component["pins"])
                    for component in semantic["components"]
                ),
                "local_labels": len(semantic["local_labels"]),
                "global_labels": len(semantic["global_labels"]),
                "power_ports": len(semantic["power_ports"]),
                "inferred_footprints": [
                    {
                        "reference": component["reference"],
                        "package": component["package"],
                        "footprint": component["footprint"],
                    }
                    for component in semantic["components"]
                    if component.get("footprint")
                ],
                "standardized_passives": sum(
                    bool(component.get("standard_passive"))
                    for component in semantic["components"]
                ),
                "worksheet": (
                    (semantic.get("worksheet") or {}).get("fields")
                    if semantic.get("worksheet") else None
                ),
            }
        )

    output[f"{project_name}.kicad_sch"] = render_root(
        factory,
        project_name,
        page_files,
        page_names,
        root_uuid,
        sheet_uuids,
    )
    worksheet_name = None
    if source_has_worksheet:
        worksheet_name = f"{project_name}.kicad_wks"
        output[worksheet_name] = generate_worksheet()
    output[f"{project_name}.kicad_pro"] = project_file(
        project_name,
        worksheet_name,
    )
    papers = [page_summary["paper"] for page_summary in summaries]
    paper_summary = papers[0] if len(set(papers)) == 1 else "mixed"
    return output, {
        "project": project_name,
        "flavor": flavor,
        "paper": paper_summary,
        "multi_units": {
            reference: len(members)
            for reference, members in multi_unit_groups.items()
        },
        "pages": summaries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert an OrCAD schematic PDF into an editable KiCad project."
    )
    parser.add_argument("pdf", type=Path, help="Input schematic PDF")
    parser.add_argument(
        "output_dir",
        type=Path,
        nargs="?",
        help="Output directory (default: <PDF stem>_kicad)",
    )
    parser.add_argument(
        "--paper",
        choices=["auto", *PAPER_SCALES],
        default="auto",
        help="Source sheet size; auto reads the PDF title block (default: auto)",
    )
    parser.add_argument(
        "--no-graphics",
        action="store_true",
        help="Emit semantic schematic objects only, omitting residual PDF graphics",
    )
    parser.add_argument(
        "--infer-footprints",
        action="store_true",
        help="Infer standard KiCad footprints for two-terminal SMD passives",
    )
    parser.add_argument(
        "--kicad-rcl",
        action="store_true",
        help="Use standard KiCad Device symbols for two-terminal passives",
    )
    parser.add_argument(
        "--summary-json",
        action="store_true",
        help="Print the conversion summary as JSON",
    )
    parser.add_argument(
        "--flavor",
        choices=["auto", "orcad", "altium"],
        default="auto",
        help="Source EDA tool; auto reads the PDF metadata (default: auto)",
    )
    args = parser.parse_args()

    if not args.pdf.is_file():
        parser.error(f"PDF not found: {args.pdf}")
    output_dir = args.output_dir or Path(f"{args.pdf.stem}_kicad")
    print(f"Opening {args.pdf.name}...", file=sys.stderr)
    output, summary = convert_pdf(
        args.pdf,
        paper=args.paper,
        keep_graphics=not args.no_graphics,
        infer_footprints=args.infer_footprints,
        use_kicad_rcl=args.kicad_rcl,
        flavor=args.flavor,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in output.items():
        (output_dir / filename).write_text(content, encoding="utf-8")

    if args.summary_json:
        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(
            f"  {summary['paper']}, {len(summary['pages'])} pages",
            file=sys.stderr,
        )
        if summary["multi_units"]:
            units = sum(summary["multi_units"].values())
            print(
                f"  {len(summary['multi_units'])} multi-unit components, "
                f"{units} units",
                file=sys.stderr,
            )
        for page in summary["pages"]:
            print(
                f"  [{page['page']:2d}] {page['wires']} wires, "
                f"{page['buses']} buses, "
                f"{page['bus_entries']} bus entries, "
                f"{page['junctions']} junctions, "
                f"{page['no_connects']} no-connects, "
                f"{page['components']} components, {page['pins']} pins, "
                f"{page['dnp_components']} DNP, "
                f"{len(page['inferred_footprints'])} footprints, "
                f"{page['standardized_passives']} standard passives, "
                f"{page['local_labels']} local labels, "
                f"{page['global_labels']} global labels, "
                f"{page['power_ports']} power ports",
                file=sys.stderr,
            )
        print(f"\nDone → {output_dir}/", file=sys.stderr)


if __name__ == "__main__":
    main()
