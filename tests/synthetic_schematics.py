#!/usr/bin/env python3
# Copyright (C) 2026 Andrei Errapart
# SPDX-License-Identifier: GPL-2.0-or-later
"""Synthetic schematic PDFs with known connectivity.

Each schematic here is drawn in memory with PyMuPDF in the drawing
language one of the supported EDA tools prints, so that pdf2kicad can be
validated end to end without any real design.  Everything in them -- part
designators, values, net names, sheet names, title-block fields and
coordinates -- is invented.  Each schematic comes with its reference
netlist: every net as a frozenset of (reference, pin number) pairs, unused
pins as one-pin nets, and the name of every net the drawing names.

Two-pin passives in these drawings carry no printed pin numbers, as in the
tools' stock libraries.  The converter numbers such pins from the left or
top; the reference netlist uses the same convention, but a comparison
should treat the two pins of the parts in ``unnumbered`` as unordered
(see ``canonical_nets``).

Run as a program to write every schematic and its reference netlist:

    python3 tests/synthetic_schematics.py OUT_DIR

writes ``<name>.pdf`` and ``<name>.nets.json`` for each schematic, ready to
be converted with ``scripts/pdf2kicad`` and compared by hand with
``kicad-cli sch export netlist``.  It needs PyMuPDF, for example from the
virtual environment the ``scripts/pdf2kicad`` wrapper creates.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF


MM = 72.0 / 25.4  # PDF points per millimetre
G = 2.54  # the 0.1 in schematic grid, in millimetres

Node = tuple[str, str]  # (reference, pin number)
Point = tuple[float, float]

# The box PyMuPDF reports around a span of each base-14 font, as fractions
# of the font size above and below the baseline.
FONT_BOX = {
    "helv": (1.075, 0.299),
    "tiro": (1.053, 0.281),
    "cour": (0.932, 0.317),
}

# OrCAD Capture's print colours (mirrors scripts/pdf_dump.py).
ORCAD_WIRE = "#4200ff"
ORCAD_BUS = "#0000ff"
ORCAD_BODY = "#cc8005"
ORCAD_PIN = "#aa8744"
ORCAD_PIN_NAME = "#0000cc"
ORCAD_OFF_PAGE = "#ff0000"
ORCAD_JUNCTION = "#ff0000"
ORCAD_NO_CONNECT = "#803f00"
ORCAD_HEADING = "#008000"
BLACK = "#000000"

# Altium Designer's default sheet colours (mirrors scripts/pdf_dump.py).
ALTIUM_WIRE = "#000080"
ALTIUM_GRAPHIC = "#0000ff"
ALTIUM_BODY_FILL = "#ffffb0"
ALTIUM_ACCENT = "#800000"
ALTIUM_JUNCTION = "#4b4b94"
ALTIUM_NO_ERC = "#ff0000"
ALTIUM_TEXT = "#000080"
ALTIUM_PORT_FILL = "#ffff80"


@dataclass(frozen=True)
class Part:
    """A component as the converted KiCad project should contain it."""

    value: str
    pins: tuple[str, ...]
    dnp: bool = False
    units: int = 1


@dataclass(frozen=True)
class SheetExpectation:
    """What the converter should recover from one page."""

    file_stem: str
    fields: dict[str, str]
    power: tuple[str, ...] = ()
    global_labels: tuple[str, ...] = ()
    local_labels: tuple[str, ...] = ()
    junctions: tuple[Point, ...] = ()
    no_connects: tuple[Point, ...] = ()
    buses: int = 0
    bus_entries: int = 0


@dataclass(frozen=True)
class SyntheticSchematic:
    """A drawn PDF and what converting it must give.

    ``nets`` holds every net, one-pin nets included, as frozensets of
    (reference, pin number); references are the KiCad ones, so both units
    of a multi-unit part share theirs.  ``net_names`` names the nets the
    drawing names, by label or power port.  ``unnumbered`` lists the parts
    whose pins carry no printed number.  ``sheets`` describes each page.
    """

    name: str
    flavor: str
    pdf: bytes
    parts: dict[str, Part]
    nets: frozenset[frozenset[Node]]
    net_names: dict[frozenset[Node], str]
    unnumbered: frozenset[str]
    sheets: tuple[SheetExpectation, ...]

    @property
    def references(self) -> list[str]:
        return sorted(self.parts)


def _rgb(color: str | None) -> tuple[float, float, float] | None:
    if color is None:
        return None
    return tuple(int(color[index:index + 2], 16) / 255 for index in (1, 3, 5))


class _Sheet:
    """One PDF page; coordinates in millimetres from the top-left corner."""

    def __init__(self, document: fitz.Document, width=297.0, height=210.0):
        self.page = document.new_page(width=width * MM, height=height * MM)

    def line(self, color: str, x1, y1, x2, y2, width=0.15) -> None:
        self.page.draw_line(
            (x1 * MM, y1 * MM), (x2 * MM, y2 * MM),
            color=_rgb(color), width=width * MM,
        )

    def lines(self, color: str, points: list[Point], width=0.15) -> None:
        """Consecutive points joined by separate strokes."""
        for start, end in zip(points, points[1:]):
            self.line(color, *start, *end, width=width)

    def rect(self, x0, y0, x1, y1, color=None, fill=None, width=0.15) -> None:
        self.page.draw_rect(
            fitz.Rect(x0 * MM, y0 * MM, x1 * MM, y1 * MM),
            color=_rgb(color), fill=_rgb(fill),
            width=width * MM if color else 0,
        )

    def polygon(self, points: list[Point], color, fill, width) -> None:
        shape = self.page.new_shape()
        shape.draw_polyline([(x * MM, y * MM) for x, y in points])
        shape.finish(
            color=_rgb(color), fill=_rgb(fill), width=width * MM,
            closePath=True,
        )
        shape.commit()

    def circle(self, cx, cy, radius, color, fill=None, width=0.05) -> None:
        shape = self.page.new_shape()
        shape.draw_circle((cx * MM, cy * MM), radius * MM)
        shape.finish(color=_rgb(color), fill=_rgb(fill), width=width * MM)
        shape.commit()

    def hop(self, cx, cy, radius, color, width) -> None:
        """A semicircle over (cx, cy), from (cx - radius) to (cx + radius)."""
        k = 0.5523 * radius
        quarters = (
            ((cx - radius, cy), (cx - radius, cy - k),
             (cx - k, cy - radius), (cx, cy - radius)),
            ((cx, cy - radius), (cx + k, cy - radius),
             (cx + radius, cy - k), (cx + radius, cy)),
        )
        shape = self.page.new_shape()
        for quarter in quarters:
            shape.draw_bezier(*[(x * MM, y * MM) for x, y in quarter])
        shape.finish(color=_rgb(color), width=width * MM, closePath=False)
        shape.commit()

    @staticmethod
    def text_width(value: str, size: float, font="helv") -> float:
        points = fitz.get_text_length(value, fontname=font, fontsize=size * MM)
        return points / MM

    def text(self, value: str, x, baseline, size, color=BLACK, font="helv"):
        self.page.insert_text(
            (x * MM, baseline * MM), value,
            fontsize=size * MM, fontname=font, color=_rgb(color),
        )

    def text_at(
        self,
        value: str,
        x,
        y,
        size,
        color=BLACK,
        font="helv",
        align="left",
        valign="baseline",
    ) -> None:
        """Place a text by an edge or the centre of the box PyMuPDF reports.

        ``align`` is left, center or right of ``x``; ``valign`` is baseline,
        top, middle or bottom of ``y``.
        """
        ascent, descent = FONT_BOX[font]
        width = self.text_width(value, size, font)
        x0 = {"left": x, "center": x - width / 2, "right": x - width}[align]
        baseline = {
            "baseline": y,
            "top": y + ascent * size,
            "middle": y + (ascent - descent) / 2 * size,
            "bottom": y - descent * size,
        }[valign]
        self.text(value, x0, baseline, size, color, font)


def _nets(
    parts: dict[str, Part],
    connected: list[tuple[str | None, list[Node]]],
) -> tuple[frozenset[frozenset[Node]], dict[frozenset[Node], str]]:
    """The full netlist: the given nets plus every unused pin on its own."""
    nets = []
    names = {}
    used = set()
    for name, nodes in connected:
        net = frozenset(nodes)
        if len(net) != len(nodes) or used & net:
            raise ValueError(f"net {name or sorted(net)} repeats a pin")
        unknown = [
            node for node in net
            if node[0] not in parts or node[1] not in parts[node[0]].pins
        ]
        if unknown:
            raise ValueError(
                f"net {name or sorted(net)} has unknown pins {unknown}"
            )
        used |= net
        nets.append(net)
        if name:
            names[net] = name
    for reference, part in parts.items():
        for pin in part.pins:
            if (reference, pin) not in used:
                nets.append(frozenset({(reference, pin)}))
    return frozenset(nets), names


def canonical_nets(
    nets, unnumbered: frozenset[str] | set[str]
) -> frozenset[frozenset[Node]]:
    """Nets with the two pins of each unnumbered part made alike."""
    return frozenset(
        frozenset(
            (reference, "~" if reference in unnumbered else pin)
            for reference, pin in net
        )
        for net in nets
    )


# --------------------------------------------------------------------------
# OrCAD Capture drawing language


def _orcad_worksheet(sheet: _Sheet, fields: dict[str, str], heading: str):
    """An inset frame, a ruled title block in the corner, a green heading."""
    x0, y0, x1, y1 = 5.0, 5.0, 292.0, 205.0
    sheet.lines(BLACK, [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])
    size = 1.5
    cells = {
        "company": (200.0, 175.0, 292.0, 182.0),
        "title": (200.0, 182.0, 292.0, 191.0),
        "size": (200.0, 191.0, 214.0, 198.0),
        "document_number": (214.0, 191.0, 276.0, 198.0),
        "revision": (276.0, 191.0, 292.0, 198.0),
        "date": (200.0, 198.0, 250.0, 205.0),
        "sheet": (250.0, 198.0, 292.0, 205.0),
    }
    for cell in cells.values():
        sheet.rect(*cell, color=BLACK, width=0.15)
    labels = {
        "title": "Title",
        "size": "Size",
        "document_number": "Document Number",
        "revision": "Rev",
        "date": "Date:",
    }
    for key, label in labels.items():
        cx0, cy0, _cx1, _cy1 = cells[key]
        sheet.text_at(label, cx0 + 1.0, cy0 + 0.6, size, valign="top")
        value_size = size * (1.4 if key == "title" else 1.0)
        sheet.text_at(fields[key], cx0 + 3.0, cy0 + 3.4, value_size,
                      valign="top")
    cx0, cy0, _cx1, _cy1 = cells["company"]
    sheet.text_at(fields["company"], cx0 + 30.0, cy0 + 2.0, 2.0, valign="top")
    cx0, cy0, _cx1, _cy1 = cells["sheet"]
    for offset, value in (
        (1.0, "Sheet"), (12.0, fields["sheet"]),
        (17.0, "of"), (22.0, fields["sheet_count"]),
    ):
        sheet.text_at(value, cx0 + offset, cy0 + 2.0, size, valign="top")
    sheet.text_at(heading, 12.0, 12.0, 4.0, ORCAD_HEADING, valign="top")


def _orcad_box(
    sheet: _Sheet,
    reference: str,
    value: str,
    body: tuple[float, float, float, float],
    pins: list[tuple[str, str, Point]],
) -> None:
    """A rectangular part: numbered and named pins, designator and value.

    Each pin is (number, name, hot point); the pin runs from its hot point
    straight to the nearest body edge.
    """
    x0, y0, x1, y1 = body
    sheet.rect(x0, y0, x1, y1, color=ORCAD_BODY, width=0.15)
    for number, name, (hx, hy) in pins:
        if hx < x0:
            other, side = (x0, hy), "left"
        elif hx > x1:
            other, side = (x1, hy), "right"
        elif hy < y0:
            other, side = (hx, y0), "top"
        else:
            other, side = (hx, y1), "bottom"
        sheet.line(ORCAD_PIN, hx, hy, *other)
        mid = ((hx + other[0]) / 2, (hy + other[1]) / 2)
        if side in ("left", "right"):
            sheet.text_at(number, mid[0], hy - 0.35, 1.27, align="center",
                          valign="bottom")
        else:
            sheet.text_at(number, hx - 0.4, mid[1], 1.27, align="right",
                          valign="middle")
        if side == "left":
            sheet.text_at(name, x0 + 0.8, hy, 1.27, ORCAD_PIN_NAME,
                          valign="middle")
        elif side == "right":
            sheet.text_at(name, x1 - 0.8, hy, 1.27, ORCAD_PIN_NAME,
                          align="right", valign="middle")
        elif side == "top":
            sheet.text_at(name, hx, y0 + 0.5, 1.27, ORCAD_PIN_NAME,
                          align="center", valign="top")
        else:
            sheet.text_at(name, hx, y1 - 0.5, 1.27, ORCAD_PIN_NAME,
                          align="center", valign="bottom")
    sheet.text_at(reference, x0, y0 - 0.8, 1.5, valign="bottom")
    sheet.text_at(value, x0, y1 + 0.6, 1.5, valign="top")


def _orcad_vertical_resistor(
    sheet: _Sheet, reference: str, value: str, x: float, top: float,
    *, outline: str = "lines",
) -> None:
    """A rectangle resistor standing upright, its pin hot points at
    ``(x, top)`` and ``(x, top + 4 G)``."""
    body = (x - G / 2, top + G, x + G / 2, top + 3 * G)
    if outline == "lines":
        bx0, by0, bx1, by1 = body
        sheet.lines(ORCAD_BODY, [(bx0, by0), (bx1, by0), (bx1, by1),
                                 (bx0, by1), (bx0, by0)])
    else:
        sheet.rect(*body, color=ORCAD_BODY, width=0.15)
    sheet.line(ORCAD_PIN, x, top, x, top + G)
    sheet.line(ORCAD_PIN, x, top + 3 * G, x, top + 4 * G)
    sheet.text_at(reference, x + G, top + 1.6 * G, 1.5, valign="middle")
    sheet.text_at(value, x + G, top + 2.6 * G, 1.5, valign="middle")


def _orcad_horizontal_zigzag(
    sheet: _Sheet, reference: str, value: str, left: float, y: float
) -> None:
    """A zigzag resistor lying flat, hot points at ``left`` and
    ``left + 4 G``."""
    start, end = left + G, left + 3 * G
    step = (end - start) / 8
    points = [(start, y)]
    for index, offset in enumerate((-0.76, 0.76, -0.76, 0.76)):
        points.append((start + step * (2 * index + 1), y + offset))
    points.append((end, y))
    sheet.lines(ORCAD_BODY, points)
    sheet.line(ORCAD_PIN, left, y, start, y)
    sheet.line(ORCAD_PIN, end, y, left + 4 * G, y)
    middle = left + 2 * G
    sheet.text_at(reference, middle, y - 1.3, 1.5, align="center",
                  valign="bottom")
    sheet.text_at(value, middle, y + 1.3, 1.5, align="center", valign="top")


def _orcad_vertical_capacitor(
    sheet: _Sheet, reference: str, value: str, x: float, top: float
) -> None:
    """Two plates and two pins, hot points at ``(x, top)`` and below."""
    plate = top + G
    gap = 0.4 * G
    sheet.line(ORCAD_BODY, x - G / 2, plate, x + G / 2, plate)
    sheet.line(ORCAD_BODY, x - G / 2, plate + gap, x + G / 2, plate + gap)
    sheet.line(ORCAD_PIN, x, top, x, plate)
    sheet.line(ORCAD_PIN, x, plate + gap, x, plate + gap + G)
    sheet.text_at(reference, x + G, plate - 0.6, 1.5, valign="middle")
    sheet.text_at(value, x + G, plate + gap + 0.9, 1.5, valign="middle")


def _orcad_ground(sheet: _Sheet, x: float, y: float) -> None:
    """Capture's GND triangle hanging from its hot point (x, y)."""
    half = G / 2
    sheet.lines(BLACK, [(x - half, y), (x + half, y), (x, y + half),
                        (x - half, y)])
    sheet.text_at("GND", x, y + half + 0.5, 1.3, align="center", valign="top")


def _orcad_supply(sheet: _Sheet, name: str, x: float, y: float) -> None:
    """A supply bar on a stem standing on its hot point (x, y)."""
    sheet.line(BLACK, x, y, x, y - G)
    sheet.line(BLACK, x - G / 2, y - G, x + G / 2, y - G)
    sheet.text_at(name, x, y - G - 0.6, 1.5, align="center", valign="bottom")


def _orcad_off_page(
    sheet: _Sheet, name: str, reference: str, x: float, y: float, side: str
) -> None:
    """An off-page connector: a double chevron pointing right, its wire
    end at ``x``, and the red name and page reference on ``side``."""
    if side == "right":
        base = x
    else:
        base = x - 1.0
    for offset in (0.0, 0.4):
        apex = (base + offset + 0.6, y)
        sheet.line(BLACK, base + offset, y - 0.6, *apex)
        sheet.line(BLACK, base + offset, y + 0.6, *apex)
    size = 1.5
    if side == "right":
        sheet.text_at(name, base + 1.3, y, size, ORCAD_OFF_PAGE,
                      valign="middle")
        end = base + 1.3 + sheet.text_width(name, size)
        sheet.text_at(reference, end + 0.8, y, size, ORCAD_OFF_PAGE,
                      valign="middle")
    else:
        sheet.text_at(name, base - 0.3, y, size, ORCAD_OFF_PAGE,
                      align="right", valign="middle")
        start = base - 0.3 - sheet.text_width(name, size)
        sheet.text_at(reference, start - 0.8, y, size, ORCAD_OFF_PAGE,
                      align="right", valign="middle")


def _orcad_label(sheet: _Sheet, name: str, x: float, wire_y: float) -> None:
    """A net alias sitting on a horizontal wire, starting at ``x``."""
    size = 1.27
    # Capture anchors an alias at its baseline, just above the wire; this
    # puts the converter's anchor estimate on the wire itself.
    sheet.text(name, x, wire_y - (FONT_BOX["helv"][1] + 0.160) * size, size)


def _orcad_junction(sheet: _Sheet, x: float, y: float) -> None:
    sheet.circle(x, y, 0.4, ORCAD_JUNCTION, ORCAD_JUNCTION)


def _orcad_no_connect(sheet: _Sheet, x: float, y: float) -> None:
    arm = 0.6
    sheet.line(ORCAD_NO_CONNECT, x - arm, y - arm, x + arm, y + arm)
    sheet.line(ORCAD_NO_CONNECT, x + arm, y - arm, x - arm, y + arm)


def _wire(sheet: _Sheet, color: str, points: list[Point], width=0.15):
    sheet.lines(color, points, width=width)


def orcad_schematic() -> SyntheticSchematic:
    """A two-page Capture-style schematic.

    Page 1, an input stage: a bias divider R1/R2 from a supply to ground
    with a decoupling capacitor C1, feeding unit A of a dual amplifier U1;
    feedback through R3 and a not-fitted R4, joined by a net alias that
    appears three times; the output leaves through an off-page connector.
    Two T junctions are dotted, one more joins the feedback to the output,
    and the alias wire to R4 crosses the feedback wire without connecting.

    Page 2, an output stage: the off-page signal enters unit B of U1, wired
    as a follower into a driver U2 whose two outputs run over a bus, through
    four bus entries, to a receiver U3; U3's spare output has a no-connect
    marker.  U2 takes the same supply and ground as page 1.
    """
    document = fitz.open()
    document.set_metadata({
        "title": "Synthetic OrCAD Sheet",
        "creator": "Synthetic schematic generator (OrCAD Capture style)",
        "producer": "PyMuPDF",
    })
    common = {
        "title": "Synthetic OrCAD Sheet",
        "company": "Synthetic Circuits Workshop",
        "size": "A4",
        "document_number": "SYN-ORC-0001",
        "revision": "B",
        "date": "March 14, 2026",
        "sheet_count": "2",
    }

    # Page 1: input stage.
    sheet = _Sheet(document)
    fields_1 = {**common, "sheet": "1"}
    _orcad_worksheet(sheet, fields_1, "Synthetic Input Stage")
    rx = 25 * G  # R1 and R2 stand on x = 63.5
    _orcad_supply(sheet, "VSYN_3V3", rx, 19 * G)
    _orcad_vertical_resistor(sheet, "R1", "47K", rx, 19 * G)
    _wire(sheet, ORCAD_WIRE, [(rx, 23 * G), (rx, 29 * G)])
    _orcad_vertical_resistor(sheet, "R2", "12K", rx, 29 * G)
    _wire(sheet, ORCAD_WIRE, [(rx, 33 * G), (rx, 34 * G)])
    _orcad_ground(sheet, rx, 34 * G)
    # The bias node: a T off R1-R2, a second T down to C1.
    _wire(sheet, ORCAD_WIRE, [(rx, 25 * G), (36 * G, 25 * G)])
    _orcad_junction(sheet, rx, 25 * G)
    cx = 30 * G
    _wire(sheet, ORCAD_WIRE, [(cx, 25 * G), (cx, 27 * G)])
    _orcad_junction(sheet, cx, 25 * G)
    _orcad_vertical_capacitor(sheet, "C1", "220n", cx, 27 * G)
    _orcad_ground(sheet, cx, 27 * G + 2.4 * G)
    _orcad_box(sheet, "U1A", "SYN-OPAMP", (38 * G, 23 * G, 43 * G, 28 * G), [
        ("3", "INP", (36 * G, 25 * G)),
        ("2", "INN", (36 * G, 27 * G)),
        ("1", "OUT", (45 * G, 26 * G)),
    ])
    # Feedback: the alias joins U1A's inverting input, R3 and R4.
    _wire(sheet, ORCAD_WIRE, [(32 * G, 27 * G), (36 * G, 27 * G)])
    _orcad_label(sheet, "SYN_FB", 32.5 * G, 27 * G)
    _orcad_horizontal_zigzag(sheet, "R3", "22K", 38 * G, 32 * G)
    _wire(sheet, ORCAD_WIRE, [(34 * G, 32 * G), (38 * G, 32 * G)])
    _orcad_label(sheet, "SYN_FB", 34.5 * G, 32 * G)
    _wire(sheet, ORCAD_WIRE, [(42 * G, 32 * G), (48 * G, 32 * G),
                              (48 * G, 26 * G)])
    _wire(sheet, ORCAD_WIRE, [(45 * G, 26 * G), (55 * G, 26 * G)])
    _orcad_junction(sheet, 48 * G, 26 * G)
    _orcad_off_page(sheet, "SYN_SIGNAL", "<2>", 55 * G, 26 * G, "right")
    # R4 is not fitted; its alias wire crosses the feedback wire at
    # (48 G, 29 G) with no junction.
    _wire(sheet, ORCAD_WIRE, [(46 * G, 29 * G), (52 * G, 29 * G)])
    _orcad_label(sheet, "SYN_FB", 49 * G, 29 * G)
    _orcad_vertical_resistor(sheet, "R4", "33K*DNP", 52 * G, 29 * G,
                             outline="rect")
    _wire(sheet, ORCAD_WIRE, [(52 * G, 33 * G), (52 * G, 34 * G)])
    _orcad_ground(sheet, 52 * G, 34 * G)

    # Page 2: output stage.
    sheet = _Sheet(document)
    fields_2 = {**common, "sheet": "2"}
    _orcad_worksheet(sheet, fields_2, "Synthetic Output Stage")
    _orcad_off_page(sheet, "SYN_SIGNAL", "<1>", 13 * G, 25 * G, "left")
    _wire(sheet, ORCAD_WIRE, [(13 * G, 25 * G), (18 * G, 25 * G)])
    _orcad_box(sheet, "U1B", "SYN-OPAMP", (20 * G, 23 * G, 25 * G, 28 * G), [
        ("5", "INP", (18 * G, 25 * G)),
        ("6", "INN", (18 * G, 27 * G)),
        ("7", "OUT", (27 * G, 26 * G)),
    ])
    # Follower: the inverting input loops under the body to the output.
    _wire(sheet, ORCAD_WIRE, [(18 * G, 27 * G), (18 * G, 30 * G),
                              (29 * G, 30 * G), (29 * G, 26 * G)])
    _wire(sheet, ORCAD_WIRE, [(27 * G, 26 * G), (38 * G, 26 * G)])
    _orcad_junction(sheet, 29 * G, 26 * G)
    _orcad_box(sheet, "U2", "SYN-DRV", (40 * G, 23 * G, 46 * G, 29 * G), [
        ("1", "IN", (38 * G, 26 * G)),
        ("2", "Q0", (48 * G, 25 * G)),
        ("3", "Q1", (48 * G, 27 * G)),
        ("4", "VDD", (44 * G, 21 * G)),
        ("5", "VSS", (44 * G, 31 * G)),
    ])
    _orcad_supply(sheet, "VSYN_3V3", 44 * G, 21 * G)
    _orcad_ground(sheet, 44 * G, 31 * G)
    # The bus: two members leave U2 through entries and reach U3.
    bus_x = 52 * G
    _wire(sheet, ORCAD_BUS, [(bus_x, 22 * G), (bus_x, 34 * G)], width=0.4)
    for row, name in ((25, "SYN_D0"), (27, "SYN_D1")):
        _wire(sheet, ORCAD_WIRE, [(48 * G, row * G), (51 * G, row * G),
                                  (bus_x, (row - 1) * G)])
        _orcad_label(sheet, name, 48.5 * G, row * G)
    for row, name in ((29, "SYN_D0"), (31, "SYN_D1")):
        _wire(sheet, ORCAD_WIRE, [(bus_x, (row - 1) * G), (53 * G, row * G),
                                  (56 * G, row * G)])
        _orcad_label(sheet, name, 53.4 * G, row * G)
    _orcad_box(sheet, "U3", "SYN-RCV", (58 * G, 27 * G, 62 * G, 33 * G), [
        ("1", "D0", (56 * G, 29 * G)),
        ("2", "D1", (56 * G, 31 * G)),
        ("3", "SPARE", (64 * G, 30 * G)),
    ])
    _orcad_no_connect(sheet, 64 * G, 30 * G)

    parts = {
        "R1": Part("47K", ("1", "2")),
        "R2": Part("12K", ("1", "2")),
        "R3": Part("22K", ("1", "2")),
        "R4": Part("33K", ("1", "2"), dnp=True),
        "C1": Part("220n", ("1", "2")),
        "U1": Part("SYN-OPAMP", ("1", "2", "3", "5", "6", "7"), units=2),
        "U2": Part("SYN-DRV", ("1", "2", "3", "4", "5")),
        "U3": Part("SYN-RCV", ("1", "2", "3")),
    }
    nets, names = _nets(parts, [
        ("VSYN_3V3", [("R1", "1"), ("U2", "4")]),
        ("GND", [("R2", "2"), ("C1", "2"), ("R4", "2"), ("U2", "5")]),
        (None, [("R1", "2"), ("R2", "1"), ("C1", "1"), ("U1", "3")]),
        ("SYN_FB", [("U1", "2"), ("R3", "1"), ("R4", "1")]),
        ("SYN_SIGNAL", [("U1", "1"), ("R3", "2"), ("U1", "5")]),
        (None, [("U1", "6"), ("U1", "7"), ("U2", "1")]),
        ("SYN_D0", [("U2", "2"), ("U3", "1")]),
        ("SYN_D1", [("U2", "3"), ("U3", "2")]),
    ])
    sheets = (
        SheetExpectation(
            "01_Synthetic_Input_Stage", fields_1,
            power=("GND", "GND", "GND", "VSYN_3V3"),
            global_labels=("SYN_SIGNAL",),
            local_labels=("SYN_FB", "SYN_FB", "SYN_FB"),
            junctions=((rx, 25 * G), (cx, 25 * G), (48 * G, 26 * G)),
        ),
        SheetExpectation(
            "02_Synthetic_Output_Stage", fields_2,
            power=("GND", "VSYN_3V3"),
            global_labels=("SYN_SIGNAL",),
            local_labels=("SYN_D0", "SYN_D0", "SYN_D1", "SYN_D1"),
            junctions=((29 * G, 26 * G),),
            no_connects=((64 * G, 30 * G),),
            buses=1,
            bus_entries=4,
        ),
    )
    return SyntheticSchematic(
        name="synthetic_orcad",
        flavor="orcad",
        pdf=document.tobytes(no_new_id=True),
        parts=parts,
        nets=nets,
        net_names=names,
        unnumbered=frozenset({"R1", "R2", "R3", "R4", "C1"}),
        sheets=sheets,
    )


# --------------------------------------------------------------------------
# Altium Designer drawing language


def _altium_template(sheet: _Sheet, fields: dict[str, str]) -> None:
    """A thin border and Altium's labelled title block, bottom right."""
    thin = 0.042
    sheet.lines(BLACK, [(10.0, 10.0), (287.0, 10.0), (287.0, 200.0),
                        (10.0, 200.0), (10.0, 10.0)], width=thin)
    for y in (175.0, 185.0, 193.0):
        sheet.line(BLACK, 177.0, y, 287.0, y, width=thin)
    sheet.line(BLACK, 177.0, 175.0, 177.0, 200.0, width=thin)
    sheet.line(BLACK, 240.0, 185.0, 240.0, 200.0, width=thin)
    label, value = 1.8, 1.8
    sheet.text("Title:", 180.0, 181.0, label)
    sheet.text(fields["title"], 192.0, 181.0, 2.5, ALTIUM_TEXT)
    sheet.text("Number:", 180.0, 190.0, label)
    sheet.text(fields["document_number"], 195.0, 190.0, value, ALTIUM_TEXT)
    sheet.text("Rev:", 242.0, 190.0, label)
    sheet.text(fields["revision"], 250.0, 190.0, value, ALTIUM_TEXT)
    sheet.text("Date:", 180.0, 198.0, label)
    sheet.text(fields["date"], 192.0, 198.0, value, ALTIUM_TEXT)
    sheet.text("Page", 242.0, 198.0, label)
    sheet.text(fields["sheet"], 252.0, 198.0, value, ALTIUM_TEXT)
    sheet.text("of", 258.0, 198.0, label)
    sheet.text(fields["sheet_count"], 264.0, 198.0, value, ALTIUM_TEXT)


def _altium_box(
    sheet: _Sheet,
    designator: str,
    comment: str,
    body: tuple[float, float, float, float],
    pins: list[tuple[str, str, Point]],
) -> None:
    """A library part: filled body, black pins, Courier names inside, Arial
    numbers outside, Times designator above and comment below."""
    x0, y0, x1, y1 = body
    sheet.rect(x0, y0, x1, y1, fill=ALTIUM_BODY_FILL)
    sheet.rect(x0, y0, x1, y1, color=ALTIUM_ACCENT, width=0.254)
    for number, name, (hx, hy) in pins:
        if hx < x0:
            other, side = (x0, hy), "left"
        elif hx > x1:
            other, side = (x1, hy), "right"
        elif hy < y0:
            other, side = (hx, y0), "top"
        else:
            other, side = (hx, y1), "bottom"
        sheet.line(BLACK, hx, hy, *other, width=0.254)
        mid = ((hx + other[0]) / 2, (hy + other[1]) / 2)
        if side in ("left", "right"):
            sheet.text_at(number, mid[0], hy - 0.4, 1.3, align="center",
                          valign="bottom")
        else:
            sheet.text_at(number, hx - 0.4, mid[1], 1.3, align="right",
                          valign="middle")
        if side == "left":
            sheet.text_at(name, x0 + 0.8, hy, 1.5, font="cour",
                          valign="middle")
        elif side == "right":
            sheet.text_at(name, x1 - 0.8, hy, 1.5, font="cour",
                          align="right", valign="middle")
        elif side == "top":
            sheet.text_at(name, hx, y0 + 0.6, 1.5, font="cour",
                          align="center", valign="top")
        else:
            sheet.text_at(name, hx, y1 - 0.6, 1.5, font="cour",
                          align="center", valign="bottom")
    sheet.text_at(designator, x0, y0 - 0.8, 2.0, ALTIUM_TEXT, "tiro",
                  valign="bottom")
    sheet.text_at(comment, x0, y1 + 0.6, 2.0, ALTIUM_TEXT, "tiro",
                  valign="top")


def _altium_resistor(
    sheet: _Sheet, designator: str, value: str, hot: Point, vertical: bool
) -> None:
    """A blue rectangle resistor with 2.5 mm black pins; ``hot`` is the
    left or top pin end, the other pin ends 10 mm further on."""
    x, y = hot
    if vertical:
        sheet.line(BLACK, x, y, x, y + 2.5, width=0.254)
        sheet.line(BLACK, x, y + 7.5, x, y + 10.0, width=0.254)
        sheet.lines(ALTIUM_GRAPHIC, [
            (x - 0.75, y + 2.5), (x + 0.75, y + 2.5), (x + 0.75, y + 7.5),
            (x - 0.75, y + 7.5), (x - 0.75, y + 2.5),
        ], width=0.254)
        sheet.text_at(designator, x + 2.0, y + 4.3, 1.8, ALTIUM_TEXT, "tiro",
                      valign="middle")
        sheet.text_at(value, x + 2.0, y + 6.6, 1.8, ALTIUM_TEXT, "tiro",
                      valign="middle")
    else:
        sheet.line(BLACK, x, y, x + 2.5, y, width=0.254)
        sheet.line(BLACK, x + 7.5, y, x + 10.0, y, width=0.254)
        sheet.lines(ALTIUM_GRAPHIC, [
            (x + 2.5, y - 0.75), (x + 7.5, y - 0.75), (x + 7.5, y + 0.75),
            (x + 2.5, y + 0.75), (x + 2.5, y - 0.75),
        ], width=0.254)
        sheet.text_at(designator, x + 5.0, y - 1.4, 1.8, ALTIUM_TEXT, "tiro",
                      align="center", valign="bottom")
        sheet.text_at(value, x + 5.0, y + 1.4, 1.8, ALTIUM_TEXT, "tiro",
                      align="center", valign="top")


def _altium_capacitor(
    sheet: _Sheet, designator: str, value: str, hot: Point
) -> None:
    """Two blue plates, upright; ``hot`` is the top pin end."""
    x, y = hot
    sheet.line(BLACK, x, y, x, y + 2.5, width=0.254)
    sheet.line(BLACK, x, y + 3.5, x, y + 6.0, width=0.254)
    sheet.line(ALTIUM_GRAPHIC, x - 1.25, y + 2.5, x + 1.25, y + 2.5,
               width=0.254)
    sheet.line(ALTIUM_GRAPHIC, x - 1.25, y + 3.5, x + 1.25, y + 3.5,
               width=0.254)
    sheet.text_at(designator, x + 2.5, y + 2.1, 1.8, ALTIUM_TEXT, "tiro",
                  valign="middle")
    sheet.text_at(value, x + 2.5, y + 4.6, 1.8, ALTIUM_TEXT, "tiro",
                  valign="middle")


def _altium_ground(sheet: _Sheet, x: float, y: float) -> None:
    """A GND bar port below its hot point (x, y)."""
    sheet.line(ALTIUM_ACCENT, x, y, x, y + 2.5, width=0.254)
    sheet.line(ALTIUM_ACCENT, x - 1.25, y + 2.5, x + 1.25, y + 2.5,
               width=0.254)
    sheet.text_at("GND", x, y + 3.2, 1.8, ALTIUM_ACCENT, "tiro",
                  align="center", valign="top")


def _altium_supply(sheet: _Sheet, name: str, x: float, y: float) -> None:
    """A circle-style supply port above its hot point (x, y)."""
    sheet.line(ALTIUM_ACCENT, x, y, x, y - 2.0, width=0.254)
    sheet.circle(x, y - 2.4, 0.4, ALTIUM_ACCENT, width=0.254)
    sheet.text_at(name, x, y - 3.4, 1.8, ALTIUM_ACCENT, "tiro",
                  align="center", valign="bottom")


def _altium_net_label(sheet: _Sheet, name: str, x: float, wire_y: float):
    """A net label whose baseline rests on a horizontal wire."""
    sheet.text_at(name, x, wire_y - 0.2, 1.8, ALTIUM_ACCENT, "tiro",
                  valign="bottom")


def _altium_port(sheet: _Sheet, name: str, x: float, y: float) -> None:
    """A right-pointing port whose left edge meets a wire end at (x, y)."""
    length, half, tip = 15.0, 1.25, 2.5
    sheet.polygon([
        (x, y - half), (x + length, y - half), (x + length + tip, y),
        (x + length, y + half), (x, y + half),
    ], ALTIUM_ACCENT, ALTIUM_PORT_FILL, 0.042)
    sheet.text_at(name, x + 2.0, y, 1.8, ALTIUM_ACCENT, "tiro",
                  valign="middle")


def _altium_junction(sheet: _Sheet, x: float, y: float) -> None:
    sheet.circle(x, y, 0.5, ALTIUM_JUNCTION, ALTIUM_JUNCTION)


def _altium_no_erc(sheet: _Sheet, x: float, y: float) -> None:
    arm = 1.02
    sheet.line(ALTIUM_NO_ERC, x - arm, y - arm, x + arm, y + arm,
               width=0.042)
    sheet.line(ALTIUM_NO_ERC, x - arm, y + arm, x + arm, y - arm,
               width=0.042)


def altium_schematic() -> SyntheticSchematic:
    """A two-page Altium-style schematic, flat net-identifier scope.

    Sheet A: a controller U4 with a pull-up R5 and a capacitor C2 on its
    enable net (a dotted T), a sense net leaving by net label, an output
    that leaves through a port and hops over the wire from U4's auxiliary
    pin to R6, and supply-circle and ground-bar power ports.

    Sheet B: a load U5 whose inputs carry the port's and the sense net's
    names as net labels, a pull-up R7 on the link joined by a T dotted in
    the navy junction colour, the same supply and ground ports, and a
    no-ERC marker on U5's spare pin.
    """
    document = fitz.open()
    document.set_metadata({
        "title": "Synthetic Altium Sheet",
        "creator": "Altium Designer (synthetic schematic generator)",
        "producer": "llPDFLib (synthetic schematic generator)",
    })
    common = {
        "document_number": "SYN-ALT-0002",
        "revision": "C",
        "date": "2026-04-21",
        "sheet_count": "2",
    }

    # Sheet A.
    sheet = _Sheet(document)
    fields_a = {**common, "title": "Synthetic Altium Sheet A", "sheet": "1"}
    _altium_template(sheet, fields_a)
    _altium_box(sheet, "U4", "SYN-CTRL", (100.0, 60.0, 120.0, 80.0), [
        ("1", "EN", (95.0, 65.0)),
        ("2", "SNS", (95.0, 75.0)),
        ("3", "OUT", (125.0, 65.0)),
        ("4", "AUX", (125.0, 75.0)),
        ("5", "VDD", (115.0, 55.0)),
        ("6", "GND", (115.0, 85.0)),
    ])
    _altium_supply(sheet, "VSYN_5V0", 115.0, 55.0)
    _altium_ground(sheet, 115.0, 85.0)
    # Enable: R5 pulls up, C2 hangs off a dotted T.
    _altium_supply(sheet, "VSYN_5V0", 70.0, 45.0)
    _altium_resistor(sheet, "R5", "68K", (70.0, 45.0), vertical=True)
    _wire(sheet, ALTIUM_WIRE, [(70.0, 55.0), (70.0, 65.0), (95.0, 65.0)],
          width=0.254)
    _wire(sheet, ALTIUM_WIRE, [(77.5, 65.0), (77.5, 72.5)], width=0.254)
    _altium_junction(sheet, 77.5, 65.0)
    _altium_net_label(sheet, "SYN_EN", 81.0, 65.0)
    _altium_capacitor(sheet, "C2", "10u", (77.5, 72.5))
    _altium_ground(sheet, 77.5, 78.5)
    # Sense: a labelled stub.
    _wire(sheet, ALTIUM_WIRE, [(84.0, 75.0), (95.0, 75.0)], width=0.254)
    _altium_net_label(sheet, "SYN_SNS", 85.0, 75.0)
    # Output: two collinear pieces bridged by a hop over x = 140.
    _wire(sheet, ALTIUM_WIRE, [(125.0, 65.0), (139.25, 65.0)], width=0.254)
    sheet.hop(140.0, 65.0, 0.75, ALTIUM_WIRE, 0.254)
    _wire(sheet, ALTIUM_WIRE, [(140.75, 65.0), (155.0, 65.0)], width=0.254)
    _altium_port(sheet, "SYN_LINK", 155.0, 65.0)
    # Auxiliary: up through the hop to R6, which is grounded.
    _wire(sheet, ALTIUM_WIRE, [(125.0, 75.0), (140.0, 75.0), (140.0, 50.0),
                               (145.0, 50.0)], width=0.254)
    _altium_resistor(sheet, "R6", "150R", (145.0, 50.0), vertical=False)
    _altium_ground(sheet, 155.0, 50.0)

    # Sheet B.
    sheet = _Sheet(document)
    fields_b = {**common, "title": "Synthetic Altium Sheet B", "sheet": "2"}
    _altium_template(sheet, fields_b)
    _altium_box(sheet, "U5", "SYN-LOAD", (100.0, 60.0, 117.5, 77.5), [
        ("1", "IN", (95.0, 62.5)),
        ("2", "FB", (95.0, 72.5)),
        ("3", "GND", (112.5, 82.5)),
        ("4", "NC", (122.5, 67.5)),
        ("5", "VDD", (112.5, 55.0)),
    ])
    _altium_supply(sheet, "VSYN_5V0", 112.5, 55.0)
    _altium_ground(sheet, 112.5, 82.5)
    _altium_no_erc(sheet, 122.5, 67.5)
    _wire(sheet, ALTIUM_WIRE, [(75.0, 62.5), (95.0, 62.5)], width=0.254)
    _altium_net_label(sheet, "SYN_LINK", 77.0, 62.5)
    # A pull-up on the link, joined by a T dotted in the navy variant.
    _altium_supply(sheet, "VSYN_5V0", 90.0, 47.5)
    _altium_resistor(sheet, "R7", "100K", (90.0, 47.5), vertical=True)
    _wire(sheet, ALTIUM_WIRE, [(90.0, 57.5), (90.0, 62.5)], width=0.254)
    sheet.circle(90.0, 62.5, 0.5, ALTIUM_WIRE, ALTIUM_WIRE)
    _wire(sheet, ALTIUM_WIRE, [(80.0, 72.5), (95.0, 72.5)], width=0.254)
    _altium_net_label(sheet, "SYN_SNS", 82.0, 72.5)

    parts = {
        "U4": Part("SYN-CTRL", ("1", "2", "3", "4", "5", "6")),
        "U5": Part("SYN-LOAD", ("1", "2", "3", "4", "5")),
        "R5": Part("68K", ("1", "2")),
        "R6": Part("150R", ("1", "2")),
        "R7": Part("100K", ("1", "2")),
        "C2": Part("10u", ("1", "2")),
    }
    nets, names = _nets(parts, [
        ("VSYN_5V0", [("R5", "1"), ("U4", "5"), ("U5", "5"), ("R7", "1")]),
        ("SYN_EN", [("R5", "2"), ("C2", "1"), ("U4", "1")]),
        ("GND", [("C2", "2"), ("U4", "6"), ("R6", "2"), ("U5", "3")]),
        ("SYN_SNS", [("U4", "2"), ("U5", "2")]),
        ("SYN_LINK", [("U4", "3"), ("U5", "1"), ("R7", "2")]),
        (None, [("U4", "4"), ("R6", "1")]),
    ])
    sheets = (
        SheetExpectation(
            "01_Synthetic_Altium_Sheet_A", fields_a,
            power=("GND", "GND", "GND", "VSYN_5V0", "VSYN_5V0"),
            global_labels=("SYN_EN", "SYN_LINK", "SYN_SNS"),
            junctions=((77.5, 65.0),),
        ),
        SheetExpectation(
            "02_Synthetic_Altium_Sheet_B", fields_b,
            power=("GND", "VSYN_5V0", "VSYN_5V0"),
            global_labels=("SYN_LINK", "SYN_SNS"),
            junctions=((90.0, 62.5),),
            no_connects=((122.5, 67.5),),
        ),
    )
    return SyntheticSchematic(
        name="synthetic_altium",
        flavor="altium",
        pdf=document.tobytes(no_new_id=True),
        parts=parts,
        nets=nets,
        net_names=names,
        unnumbered=frozenset({"R5", "R6", "R7", "C2"}),
        sheets=sheets,
    )


def all_schematics() -> list[SyntheticSchematic]:
    return [orcad_schematic(), altium_schematic()]


def reference_netlist(schematic: SyntheticSchematic) -> dict:
    """The schematic's expected netlist as plain JSON data."""
    def sort_key(net):
        return (schematic.net_names.get(net) is None,
                schematic.net_names.get(net) or "", sorted(net))

    return {
        "schematic": schematic.name,
        "flavor": schematic.flavor,
        "note": (
            "Pins of the parts listed in 'unnumbered' have no printed "
            "number; they are numbered from the left or top, but compare "
            "them as an unordered pair."
        ),
        "unnumbered": sorted(schematic.unnumbered),
        "components": {
            reference: {
                "value": part.value,
                "dnp": part.dnp,
                "units": part.units,
                "pins": list(part.pins),
            }
            for reference, part in sorted(schematic.parts.items())
        },
        "nets": [
            {
                "name": schematic.net_names.get(net),
                "nodes": [list(node) for node in sorted(net)],
            }
            for net in sorted(schematic.nets, key=sort_key)
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write the synthetic schematic PDFs and their "
                    "reference netlists."
    )
    parser.add_argument("out_dir", type=Path, help="output directory")
    args = parser.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for schematic in all_schematics():
        pdf_path = args.out_dir / f"{schematic.name}.pdf"
        nets_path = args.out_dir / f"{schematic.name}.nets.json"
        pdf_path.write_bytes(schematic.pdf)
        nets_path.write_text(
            json.dumps(reference_netlist(schematic), indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"{pdf_path}\n{nets_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
