#!/usr/bin/env python3
# Copyright (C) 2026 Andrei Errapart
# SPDX-License-Identifier: GPL-2.0-or-later
"""Convert the synthetic schematics end to end and check what comes out.

Each PDF from tests/synthetic_schematics.py is converted with
pdf2kicad.convert_pdf_bytes, the emitted .kicad_sch sheets are read back,
and their connectivity is worked out here the way KiCad does it: a wire
end, pin, label, power pin or junction on a wire connects to it, two wires
crossing mid-span do not, labels connect by name on their sheet, and
global labels and power symbols connect by name across sheets.  The result
must equal the schematic's reference netlist.  No KiCad installation is
needed.
"""

from __future__ import annotations

import math
import re
import sys
import unittest
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "scripts"))
sys.path.insert(0, str(TESTS))

import pdf2kicad  # noqa: E402
import synthetic_schematics as synthetic  # noqa: E402

# Hundredths of a millimetre: the converter writes coordinates to 0.01 mm.
IntPoint = tuple[int, int]

_TOKEN_RE = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def parse_sexpr(text: str) -> list:
    """Nested lists of atoms and unquoted strings."""
    stack: list[list] = [[]]
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        if token == "(":
            stack.append([])
        elif token == ")":
            node = stack.pop()
            stack[-1].append(node)
        elif token.startswith('"'):
            stack[-1].append(re.sub(r"\\(.)", r"\1", token[1:-1]))
        else:
            stack[-1].append(token)
    (root,) = stack[0]
    return root


def children(node: list, key: str) -> list[list]:
    return [
        item for item in node[1:]
        if isinstance(item, list) and item and item[0] == key
    ]


def child(node: list, key: str) -> list | None:
    found = children(node, key)
    return found[0] if found else None


def property_value(node: list, name: str) -> str | None:
    for item in children(node, "property"):
        if item[1] == name:
            return item[2]
    return None


def _iu(value) -> int:
    return round(float(value) * 100)


def _at(node: list) -> tuple[float, float, int]:
    at = child(node, "at")
    angle = int(float(at[3])) if len(at) > 3 else 0
    return float(at[1]), float(at[2]), angle


def _place(origin: tuple[float, float, int], local) -> IntPoint:
    """A library point (Y up) placed by a symbol instance (Y down)."""
    x, y, angle = origin
    lx, ly = local
    radians = math.radians(angle)
    cos, sin = round(math.cos(radians)), round(math.sin(radians))
    return (
        _iu(x + lx * cos - ly * sin),
        _iu(y - (lx * sin + ly * cos)),
    )


@dataclass
class KicadSheet:
    """The electrically relevant content of one emitted .kicad_sch."""

    file_name: str
    text: str
    title_block: dict[str, list[str]] = field(default_factory=dict)
    wires: list[tuple[IntPoint, IntPoint]] = field(default_factory=list)
    junctions: list[IntPoint] = field(default_factory=list)
    no_connects: list[IntPoint] = field(default_factory=list)
    labels: list[tuple[str, str, IntPoint]] = field(default_factory=list)
    power: list[tuple[str, str, IntPoint]] = field(default_factory=list)
    pins: list[tuple[str, str, IntPoint]] = field(default_factory=list)
    symbols: list[dict] = field(default_factory=list)
    lib_pins: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    buses: int = 0
    bus_entries: int = 0


def read_sheet(file_name: str, text: str) -> KicadSheet:
    root = parse_sexpr(text)
    sheet = KicadSheet(file_name, text)
    for item in child(root, "title_block")[1:]:
        sheet.title_block.setdefault(item[0], []).append(item[-1])

    libraries = {}
    for symbol in children(child(root, "lib_symbols"), "symbol"):
        pins = []
        units = [(0, symbol)] + [
            (int(re.fullmatch(r".*_(\d+)_(\d+)", unit[1]).group(1)), unit)
            for unit in children(symbol, "symbol")
        ]
        for unit, node in units:
            for pin in children(node, "pin"):
                at = child(pin, "at")
                pins.append((
                    unit,
                    child(pin, "number")[1],
                    (float(at[1]), float(at[2])),
                ))
        libraries[symbol[1]] = {
            "power": child(symbol, "power") is not None,
            "pins": pins,
        }
        sheet.lib_pins[symbol[1]] = [
            (unit, number) for unit, number, _local in pins
        ]

    for wire in children(root, "wire"):
        start, end = children(child(wire, "pts"), "xy")
        sheet.wires.append((
            (_iu(start[1]), _iu(start[2])),
            (_iu(end[1]), _iu(end[2])),
        ))
    for junction in children(root, "junction"):
        x, y, _angle = _at(junction)
        sheet.junctions.append((_iu(x), _iu(y)))
    for marker in children(root, "no_connect"):
        x, y, _angle = _at(marker)
        sheet.no_connects.append((_iu(x), _iu(y)))
    for kind, key in (("local", "label"), ("global", "global_label")):
        for label in children(root, key):
            x, y, _angle = _at(label)
            sheet.labels.append((kind, label[1], (_iu(x), _iu(y))))
    sheet.buses = len(children(root, "bus"))
    sheet.bus_entries = len(children(root, "bus_entry"))

    for symbol in children(root, "symbol"):
        if child(symbol, "mirror") is not None:
            raise AssertionError("mirrored symbols are not modelled")
        lib_id = child(symbol, "lib_id")[1]
        unit = int(child(symbol, "unit")[1])
        origin = _at(symbol)
        library = libraries[lib_id]
        reference = property_value(symbol, "Reference")
        value = property_value(symbol, "Value")
        placed = [
            (number, _place(origin, local))
            for pin_unit, number, local in library["pins"]
            if pin_unit in (0, unit)
        ]
        if library["power"]:
            for _number, point in placed:
                sheet.power.append((value, lib_id, point))
            continue
        sheet.symbols.append({
            "reference": reference,
            "value": value,
            "unit": unit,
            "lib_id": lib_id,
            "dnp": child(symbol, "dnp")[1] == "yes",
            "in_bom": child(symbol, "in_bom")[1] == "yes",
            "pins": {number: point for number, point in placed},
        })
        for number, point in placed:
            sheet.pins.append((reference, number, point))
    return sheet


def _on_segment(point: IntPoint, start: IntPoint, end: IntPoint) -> bool:
    (px, py), (ax, ay), (bx, by) = point, start, end
    return (
        (bx - ax) * (py - ay) == (by - ay) * (px - ax)
        and min(ax, bx) <= px <= max(ax, bx)
        and min(ay, by) <= py <= max(ay, by)
    )


class _UnionFind:
    def __init__(self):
        self.parent: dict = {}

    def find(self, item):
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, first, second) -> None:
        first, second = self.find(first), self.find(second)
        if first != second:
            self.parent[second] = first


def netlist(sheets: list[KicadSheet]):
    """Nets of (reference, pin) and the label or power names on each."""
    groups = _UnionFind()
    for index, sheet in enumerate(sheets):
        def point(location, index=index):
            return ("point", index, location)

        anchors = [location for wire in sheet.wires for location in wire]
        anchors += [location for _ref, _number, location in sheet.pins]
        anchors += [location for _kind, _name, location in sheet.labels]
        anchors += [location for _name, _lib, location in sheet.power]
        anchors += sheet.junctions
        for wire_index, (start, end) in enumerate(sheet.wires):
            wire = ("wire", index, wire_index)
            for location in anchors:
                if _on_segment(location, start, end):
                    groups.union(wire, point(location))
        for reference, number, location in sheet.pins:
            groups.union(("pin", reference, number), point(location))
        for kind, name, location in sheet.labels:
            scope = ("global",) if kind == "global" else ("local", index)
            groups.union(("name", *scope, name), point(location))
        for name, _lib_id, location in sheet.power:
            groups.union(("name", "global", name), point(location))

    nets: dict = {}
    names: dict = {}
    for item in list(groups.parent):
        root = groups.find(item)
        if item[0] == "pin":
            nets.setdefault(root, set()).add(item[1:])
        elif item[0] == "name":
            names.setdefault(root, set()).add(item[-1])
    return {
        frozenset(members): frozenset(names.get(root, ()))
        for root, members in nets.items()
    }


def convert(schematic: synthetic.SyntheticSchematic, **options):
    files, summary = pdf2kicad.convert_pdf_bytes(
        schematic.pdf, f"{schematic.name}.pdf", **options
    )
    sheets = [
        read_sheet(page["sheet_file"], files[page["sheet_file"]])
        for page in summary["pages"]
    ]
    return files, summary, sheets


class _SyntheticSchematicChecks:
    """Checks shared by every synthetic schematic; mixed into a TestCase."""

    build = None

    @classmethod
    def setUpClass(cls):
        cls.schematic = cls.build()
        cls.files, cls.summary, cls.sheets = convert(cls.schematic)
        cls.transform = pdf2kicad.coordinate_transform(cls.summary["paper"])

    def kicad_point(self, point) -> IntPoint:
        x, y = self.transform.xy(*point)
        return _iu(x), _iu(y)

    def assert_connectivity(self, sheets):
        recovered = netlist(sheets)
        unnumbered = self.schematic.unnumbered
        expected = synthetic.canonical_nets(self.schematic.nets, unnumbered)
        self.assertEqual(len(expected), len(self.schematic.nets))
        names = {}
        for net, net_names in recovered.items():
            names[next(iter(synthetic.canonical_nets([net], unnumbered)))] = (
                net_names
            )
        missing = sorted(sorted(net) for net in expected - names.keys())
        extra = sorted(sorted(net) for net in names.keys() - expected)
        self.assertEqual((missing, extra), ([], []))
        for net in self.schematic.nets:
            canonical = next(iter(synthetic.canonical_nets([net], unnumbered)))
            name = self.schematic.net_names.get(net)
            self.assertEqual(
                names[canonical],
                frozenset({name} if name else ()),
                sorted(net),
            )

    def test_connectivity_matches_the_reference_netlist(self):
        self.assert_connectivity(self.sheets)

    def test_connectivity_survives_standard_kicad_passives(self):
        _files, summary, sheets = convert(self.schematic, use_kicad_rcl=True)
        standardized = sum(
            page["standardized_passives"] for page in summary["pages"]
        )
        self.assertEqual(standardized, len(self.schematic.unnumbered))
        self.assert_connectivity(sheets)

    def test_flavor_and_paper(self):
        self.assertEqual(self.summary["flavor"], self.schematic.flavor)
        self.assertEqual(self.summary["paper"], "A4")

    def test_every_part_is_recovered_with_its_value(self):
        recovered = {}
        for sheet in self.sheets:
            for symbol in sheet.symbols:
                recovered.setdefault(symbol["reference"], []).append(symbol)
        self.assertEqual(sorted(recovered), self.schematic.references)
        for reference, part in self.schematic.parts.items():
            placements = recovered[reference]
            with self.subTest(reference=reference):
                self.assertEqual(
                    sorted(symbol["unit"] for symbol in placements),
                    list(range(1, part.units + 1)),
                )
                self.assertEqual(
                    {symbol["value"] for symbol in placements}, {part.value}
                )
                self.assertEqual(
                    {symbol["dnp"] for symbol in placements}, {part.dnp}
                )
                self.assertEqual(
                    {symbol["in_bom"] for symbol in placements},
                    {not part.dnp},
                )
                pins = sorted(
                    number
                    for symbol in placements
                    for number in symbol["pins"]
                )
                self.assertEqual(pins, sorted(part.pins))

    def test_sheets_are_named_from_the_page(self):
        self.assertEqual(
            [page["sheet_name"] for page in self.summary["pages"]],
            [sheet.file_stem for sheet in self.schematic.sheets],
        )
        root = parse_sexpr(self.files[f"{self.schematic.name}.kicad_sch"])
        self.assertEqual(
            [
                property_value(sheet, "Sheetname")
                for sheet in children(root, "sheet")
            ],
            [sheet.file_stem for sheet in self.schematic.sheets],
        )

    def test_title_block_fields(self):
        for page, sheet, expected in zip(
            self.summary["pages"], self.sheets, self.schematic.sheets
        ):
            with self.subTest(sheet=expected.file_stem):
                fields = page["worksheet"]
                for key, value in expected.fields.items():
                    self.assertEqual(fields.get(key), value, key)
                block = sheet.title_block
                self.assertEqual(block["title"], [expected.fields["title"]])
                self.assertEqual(block["date"], [expected.fields["date"]])
                self.assertEqual(block["rev"], [expected.fields["revision"]])
                comments = block["comment"]
                self.assertIn(
                    "Document Number: "
                    + expected.fields["document_number"],
                    comments,
                )
                self.assertIn(
                    f"Source sheet {expected.fields['sheet']} of "
                    f"{expected.fields['sheet_count']}",
                    comments,
                )

    def test_power_symbols(self):
        for sheet, expected in zip(self.sheets, self.schematic.sheets):
            with self.subTest(sheet=expected.file_stem):
                self.assertEqual(
                    sorted(name for name, _lib, _point in sheet.power),
                    sorted(expected.power),
                )
                for name, lib_id, _point in sheet.power:
                    self.assertEqual(
                        lib_id,
                        "power:GND" if name == "GND" else "power:VCC",
                    )

    def test_labels(self):
        for sheet, expected in zip(self.sheets, self.schematic.sheets):
            with self.subTest(sheet=expected.file_stem):
                kinds = {"global": [], "local": []}
                for kind, name, _point in sheet.labels:
                    kinds[kind].append(name)
                self.assertEqual(
                    sorted(kinds["global"]), sorted(expected.global_labels)
                )
                self.assertEqual(
                    sorted(kinds["local"]), sorted(expected.local_labels)
                )

    def test_junctions_no_connects_and_buses(self):
        for sheet, expected in zip(self.sheets, self.schematic.sheets):
            with self.subTest(sheet=expected.file_stem):
                self.assertEqual(
                    sorted(sheet.junctions),
                    sorted(self.kicad_point(point)
                           for point in expected.junctions),
                )
                self.assertEqual(
                    sorted(sheet.no_connects),
                    sorted(self.kicad_point(point)
                           for point in expected.no_connects),
                )
                pin_points = {location for *_pin, location in sheet.pins}
                for marker in sheet.no_connects:
                    self.assertIn(marker, pin_points)
                self.assertEqual(sheet.buses, expected.buses)
                self.assertEqual(sheet.bus_entries, expected.bus_entries)


class SyntheticOrcadSchematicTests(
    _SyntheticSchematicChecks, unittest.TestCase
):
    build = staticmethod(synthetic.orcad_schematic)

    def symbols(self, reference):
        return [
            (index, symbol)
            for index, sheet in enumerate(self.sheets)
            for symbol in sheet.symbols
            if symbol["reference"] == reference
        ]

    def test_units_of_one_part_merge_across_pages(self):
        self.assertEqual(self.summary["multi_units"], {"U1": 2})
        placements = self.symbols("U1")
        self.assertEqual(
            [(index, symbol["unit"]) for index, symbol in placements],
            [(0, 1), (1, 2)],
        )
        lib_ids = {symbol["lib_id"] for _index, symbol in placements}
        self.assertEqual(len(lib_ids), 1)
        for index, symbol in placements:
            self.assertEqual(
                sorted(self.sheets[index].lib_pins[symbol["lib_id"]]),
                [(1, "1"), (1, "2"), (1, "3"), (2, "5"), (2, "6"), (2, "7")],
            )

    def test_not_fitted_value_is_cleaned(self):
        ((_index, symbol),) = self.symbols("R4")
        self.assertEqual(symbol["value"], "33K")
        self.assertTrue(symbol["dnp"])
        self.assertFalse(symbol["in_bom"])
        self.assertEqual(self.summary["pages"][0]["dnp_components"], 1)

    def test_worksheet_is_replaced_by_a_kicad_drawing_sheet(self):
        self.assertIn("synthetic_orcad.kicad_wks", self.files)
        for sheet, expected in zip(self.sheets, self.schematic.sheets):
            self.assertEqual(
                sheet.title_block["company"], [expected.fields["company"]]
            )
            self.assertNotIn('"Document Number"', sheet.text)

    def test_off_page_page_references_are_not_left_as_text(self):
        for sheet in self.sheets:
            self.assertNotIn('"<1>"', sheet.text)
            self.assertNotIn('"<2>"', sheet.text)
            self.assertNotIn('(text "SYN_SIGNAL"', sheet.text)

    def test_crossing_without_a_dot_stays_unconnected(self):
        crossing = self.kicad_point((48 * synthetic.G, 29 * synthetic.G))
        sheet = self.sheets[0]
        through = [
            wire for wire in sheet.wires
            if _on_segment(crossing, *wire) and crossing not in wire
        ]
        self.assertEqual(len(through), 2)
        self.assertNotIn(crossing, sheet.junctions)


class SyntheticAltiumSchematicTests(
    _SyntheticSchematicChecks, unittest.TestCase
):
    build = staticmethod(synthetic.altium_schematic)

    def test_port_becomes_a_global_label_at_its_wire_end(self):
        port = self.kicad_point((155.0, 65.0))
        self.assertIn(("global", "SYN_LINK", port), self.sheets[0].labels)
        wire_ends = {end for wire in self.sheets[0].wires for end in wire}
        self.assertIn(port, wire_ends)
        self.assertNotIn('(text "SYN_LINK"', self.sheets[0].text)

    def test_horizontal_hop_is_bridged_over_the_crossing_wire(self):
        sheet = self.sheets[0]
        gap = tuple(
            sorted((self.kicad_point((139.25, 65.0)),
                    self.kicad_point((140.75, 65.0))))
        )
        self.assertIn(gap, [tuple(sorted(wire)) for wire in sheet.wires])
        crossing = self.kicad_point((140.0, 65.0))
        through = [
            wire for wire in sheet.wires
            if _on_segment(crossing, *wire) and crossing not in wire
        ]
        self.assertEqual(len(through), 2)
        self.assertNotIn(crossing, sheet.junctions)

    def test_power_ports_keep_their_glyph(self):
        glyphs = Counter(
            (name, lib_id)
            for sheet in self.sheets
            for name, lib_id, _point in sheet.power
        )
        self.assertEqual(glyphs, Counter({
            ("GND", "power:GND"): 4,
            ("VSYN_5V0", "power:VCC"): 4,
        }))

    def test_page_name_comes_from_the_title(self):
        for page, expected in zip(
            self.summary["pages"], self.schematic.sheets
        ):
            self.assertEqual(
                page["worksheet"]["page_name"], expected.fields["title"]
            )


if __name__ == "__main__":
    unittest.main()
