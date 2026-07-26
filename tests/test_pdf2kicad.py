#!/usr/bin/env python3
# Copyright (C) 2026 Andrei Errapart
# SPDX-License-Identifier: GPL-2.0-or-later

import json
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pdf2kicad  # noqa: E402


def line(color, x1, y1, x2, y2):
    return {
        "color": color,
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
    }


def component(reference, pin_number="1"):
    return {
        "reference": reference,
        "value": "Example IC",
        "bbox": {"x0": 10.0, "y0": 10.0, "x1": 15.0, "y1": 15.0},
        "body_lines": [],
        "pins": [
            {
                "number": pin_number,
                "hot": {"x": 8.0, "y": 12.5},
                "other": {"x": 10.0, "y": 12.5},
                "length": 2.0,
            }
        ],
    }


class CoordinateTransformTests(unittest.TestCase):
    def test_capture_a2_print_transform(self):
        transform = pdf2kicad.coordinate_transform("A2")
        self.assertEqual(transform.xy(45.72, 41.91), (88.9, 81.28))

    def test_capture_a3_print_transform(self):
        transform = pdf2kicad.coordinate_transform("A3")
        self.assertEqual(transform.xy(53.34, 55.118), (73.66, 76.2))

    def test_paper_is_detected_from_title_block(self):
        page = {
            "width": 297.0,
            "height": 210.0,
            "texts": [{"text": "A3", "x": 270.0, "y": 190.0}],
        }
        self.assertEqual(pdf2kicad.detect_paper([page], "auto"), "A3")
        self.assertEqual(pdf2kicad.detect_paper([page], "A2"), "A2")


class SheetNamingTests(unittest.TestCase):
    @staticmethod
    def page(number, *headings, components=None, wires=None):
        return {
            "texts": [
                {
                    "text": heading,
                    "color": "#008000",
                    "x": float(index * 20),
                    "y": 10.0,
                    "size": 4.0,
                    "angle": 0,
                }
                for index, heading in enumerate(headings)
            ],
            "decoded": {
                "components": components or [],
                "wires": wires or [],
                "worksheet": {
                    "fields": {
                        "sheet": str(number),
                        "sheet_count": "18",
                    },
                },
            },
        }

    def test_visible_headings_match_dsn_style_sheet_names(self):
        pages = [
            self.page(1, "Example Evaluation Board"),
            self.page(2, "BLOCK Diagram"),
            self.page(
                3,
                "PoR Control",
                "SoC CLOCK",
                "System Config",
                components=[component("R1")],
            ),
            self.page(13, "SoC_POWER", components=[component("R2")]),
            self.page(14, "SoC_POWER", components=[component("R3")]),
        ]

        self.assertEqual(
            pdf2kicad.detect_sheet_names(pages),
            [
                "01_NOTE",
                "02_BLOCK",
                "03_Clock_Sys_Config_PWR_on_cnt",
                "13_SoC_POWER1",
                "14_SoC_POWER2",
            ],
        )

    def test_source_page_name_uses_haskell_sanitizer(self):
        page = self.page(7, components=[component("R1")])
        page["decoded"]["worksheet"]["fields"]["page_name"] = (
            "07 Clock / System Config"
        )

        self.assertEqual(
            pdf2kicad.detect_sheet_names([page]),
            ["07_Clock_System_Config"],
        )


class WorksheetTests(unittest.TestCase):
    @staticmethod
    def text(value, x, y, x1, y1):
        return {
            "text": value,
            "color": "#000000",
            "x": x,
            "y": y,
            "x1": x1,
            "y1": y1,
            "size": 1.0,
            "angle": 0,
        }

    @staticmethod
    def rectangle(x0, y0, x1, y1):
        return {
            "x0": x0,
            "y0": y0,
            "x1": x1,
            "y1": y1,
            "color": "#000000",
            "fill": None,
            "width": 0.1,
        }

    def test_variable_cell_worksheet_is_parsed_and_consumed(self):
        page = {
            "width": 100.0,
            "height": 80.0,
            "lines": [
                line("#000000", 2.0, 2.0, 98.0, 2.0),
                line("#000000", 98.0, 2.0, 98.0, 78.0),
                line("#000000", 98.0, 78.0, 2.0, 78.0),
                line("#000000", 2.0, 78.0, 2.0, 2.0),
            ],
            # Five merged cells deliberately differ from Capture's common
            # seven-cell layout.
            "rectangles": [
                self.rectangle(55.0, 58.0, 98.0, 62.0),
                self.rectangle(55.0, 62.0, 98.0, 68.0),
                self.rectangle(55.0, 68.0, 88.0, 74.0),
                self.rectangle(88.0, 68.0, 98.0, 74.0),
                self.rectangle(55.0, 74.0, 98.0, 78.0),
            ],
            "curves": [],
            "texts": [
                self.text("Example Corporation", 65.0, 59.0, 82.0, 61.0),
                self.text("Title:", 56.0, 63.0, 61.0, 65.0),
                self.text("Example Board", 65.0, 65.0, 78.0, 67.0),
                self.text("Size:", 56.0, 69.0, 60.0, 71.0),
                self.text("A3", 56.0, 71.0, 58.0, 73.0),
                self.text("Document Number:", 64.0, 69.0, 74.0, 71.0),
                self.text("DOC-42", 74.0, 71.0, 81.0, 73.0),
                self.text("Issue", 89.0, 69.0, 93.0, 71.0),
                self.text("2.1", 93.0, 71.0, 96.0, 73.0),
                self.text("Date:", 56.0, 75.0, 60.0, 77.0),
                self.text("April 4, 2026", 61.0, 75.0, 72.0, 77.0),
                self.text("Page:", 75.0, 75.0, 79.0, 77.0),
                self.text("4", 80.0, 75.0, 81.0, 77.0),
                self.text("of", 83.0, 75.0, 85.0, 77.0),
                self.text("9", 87.0, 75.0, 88.0, 77.0),
            ],
        }

        worksheet = pdf2kicad.pdf_dump.decode_worksheet(page)

        self.assertIsNotNone(worksheet)
        self.assertEqual(
            worksheet["fields"],
            {
                "title": "Example Board",
                "size": "A3",
                "document_number": "DOC-42",
                "revision": "2.1",
                "date": "April 4, 2026",
                "company": "Example Corporation",
                "sheet": "4",
                "sheet_count": "9",
            },
        )
        self.assertEqual(worksheet["line_indexes"], [0, 1, 2, 3])
        self.assertEqual(worksheet["rectangle_indexes"], list(range(5)))
        self.assertEqual(worksheet["text_indexes"], list(range(15)))

        header = pdf2kicad._header(
            pdf2kicad.UuidFactory(b"worksheet"),
            "A3",
            "Fallback",
            worksheet["fields"],
        )
        self.assertIn('(title "Example Board")', header)
        self.assertIn('(company "Example Corporation")', header)
        self.assertIn('(rev "2.1")', header)
        self.assertIn('Document Number: DOC-42', header)
        self.assertIn('Source sheet 4 of 9', header)

    def test_frame_only_page_is_removed_without_inventing_fields(self):
        page = {
            "width": 100.0,
            "height": 80.0,
            "lines": [
                line("#000000", 2.0, 2.0, 98.0, 2.0),
                line("#000000", 98.0, 2.0, 98.0, 78.0),
                line("#000000", 98.0, 78.0, 2.0, 78.0),
                line("#000000", 2.0, 78.0, 2.0, 2.0),
            ],
            "rectangles": [],
            "curves": [],
            "texts": [],
        }

        worksheet = pdf2kicad.pdf_dump.decode_worksheet(page)

        self.assertEqual(worksheet["fields"], {})
        self.assertIsNone(worksheet["title_block"])
        self.assertEqual(worksheet["line_indexes"], [0, 1, 2, 3])

    def test_line_only_title_block_is_not_cell_count_dependent(self):
        page = {
            "width": 100.0,
            "height": 80.0,
            "lines": [
                line("#000000", 2.0, 2.0, 98.0, 2.0),
                line("#000000", 98.0, 2.0, 98.0, 78.0),
                line("#000000", 98.0, 78.0, 2.0, 78.0),
                line("#000000", 2.0, 78.0, 2.0, 2.0),
                line("#000000", 60.0, 60.0, 98.0, 60.0),
                line("#000000", 60.0, 68.0, 98.0, 68.0),
                line("#000000", 60.0, 74.0, 98.0, 74.0),
                line("#000000", 60.0, 60.0, 60.0, 78.0),
            ],
            "rectangles": [],
            "curves": [],
            "texts": [
                self.text("Title", 61.0, 61.0, 65.0, 63.0),
                self.text("Line Board", 66.0, 64.0, 75.0, 67.0),
                self.text("Date:", 61.0, 69.0, 65.0, 71.0),
                self.text("May 5, 2026", 66.0, 69.0, 76.0, 71.0),
                self.text("PAGE 2 OF 5", 76.0, 75.0, 88.0, 77.0),
                self.text("3.0", 90.0, 75.0, 94.0, 77.0),
            ],
        }

        worksheet = pdf2kicad.pdf_dump.decode_worksheet(page)

        self.assertEqual(
            worksheet["title_block"],
            {"x0": 60.0, "y0": 60.0, "x1": 98.0, "y1": 78.0},
        )
        self.assertEqual(worksheet["fields"]["title"], "Line Board")
        self.assertEqual(worksheet["fields"]["date"], "May 5, 2026")
        self.assertEqual(worksheet["fields"]["sheet"], "2")
        self.assertEqual(worksheet["fields"]["sheet_count"], "5")
        self.assertEqual(worksheet["fields"]["revision"], "3.0")

    def test_page_without_frame_has_no_worksheet(self):
        page = {
            "width": 100.0,
            "height": 80.0,
            "lines": [line("#000000", 10.0, 10.0, 20.0, 10.0)],
            "rectangles": [],
            "curves": [],
            "texts": [],
        }
        self.assertIsNone(pdf2kicad.pdf_dump.decode_worksheet(page))


class SemanticRecoveryTests(unittest.TestCase):
    def test_disconnected_capacitor_halves_become_one_component(self):
        reference = {
            "text": "C1",
            "color": "#000000",
            "x": 10.0,
            "y": 8.0,
            "x1": 11.0,
            "y1": 9.0,
            "size": 1.0,
            "angle": 0,
        }
        page = {
            "lines": [
                line(pdf2kicad.BODY_COLOR, 10.0, 10.0, 10.0, 11.0),
                line(pdf2kicad.PIN_COLOR, 8.0, 10.0, 10.0, 10.0),
                line(pdf2kicad.BODY_COLOR, 10.5, 10.0, 10.5, 11.0),
                line(pdf2kicad.PIN_COLOR, 10.5, 10.0, 12.5, 10.0),
            ],
            "texts": [reference],
            "decoded": {
                "components": [],
                "wires": [],
            },
        }

        components, consumed_texts, consumed_lines = (
            pdf2kicad.decode_components(page)
        )

        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["reference"], "C1")
        self.assertEqual(len(components[0]["pins"]), 2)
        self.assertEqual(consumed_lines, {0, 1, 2, 3})
        self.assertIn(pdf2kicad._text_key(reference), consumed_texts)

    def test_uuid_factory_is_deterministic(self):
        first = pdf2kicad.UuidFactory(b"fixture")
        second = pdf2kicad.UuidFactory(b"fixture")
        self.assertEqual(
            [first.new("wire"), first.new("symbol")],
            [second.new("wire"), second.new("symbol")],
        )

    def test_project_file_is_valid_json(self):
        project = json.loads(pdf2kicad.project_file("example"))
        self.assertEqual(project["meta"]["filename"], "example.kicad_pro")


class MultiUnitTests(unittest.TestCase):
    def test_sequential_suffixes_across_pages_become_units(self):
        unit_a = component("U12A", "1")
        unit_b = component("U12B", "2")
        unit_c = component("U12C", "3")
        groups = pdf2kicad.detect_multi_units(
            [
                {"components": [unit_a]},
                {"components": [unit_b, unit_c]},
            ]
        )

        self.assertEqual(list(groups), ["U12"])
        self.assertEqual([item["reference"] for item in groups["U12"]], ["U12"] * 3)
        self.assertEqual(
            [item["source_reference"] for item in groups["U12"]],
            ["U12A", "U12B", "U12C"],
        )
        self.assertEqual([item["unit"] for item in groups["U12"]], [1, 2, 3])

    def test_gaps_duplicates_and_bare_references_are_not_merged(self):
        components = [
            component("U1A"),
            component("U1C"),
            component("U2A"),
            component("U2B"),
            component("U2B"),
            component("U3"),
            component("U3A"),
            component("U3B"),
            component("U4A"),
        ]
        groups = pdf2kicad.detect_multi_units([{"components": components}])
        self.assertEqual(groups, {})
        self.assertEqual([item["reference"] for item in components], [
            "U1A", "U1C", "U2A", "U2B", "U2B",
            "U3", "U3A", "U3B", "U4A",
        ])

    def test_multi_unit_library_and_placements_use_base_reference(self):
        transform = pdf2kicad.coordinate_transform("A2")
        unit_a = component("U7A", "1")
        unit_b = component("U7B", "2")
        groups = pdf2kicad.detect_multi_units(
            [{"components": [unit_a, unit_b]}]
        )
        for member in groups["U7"]:
            member["_transform"] = transform
        units = [
            (member["unit"], member["_transform"], member)
            for member in groups["U7"]
        ]

        definition = pdf2kicad._symbol_definition(
            pdf2kicad.UuidFactory(b"multi-unit"),
            transform,
            unit_a,
            "pdf2kicad:U7_multi",
            units=units,
        )
        placement_a = pdf2kicad._component_instance(
            pdf2kicad.UuidFactory(b"unit-a"),
            transform,
            unit_a,
            "pdf2kicad:U7_multi",
        )
        placement_b = pdf2kicad._component_instance(
            pdf2kicad.UuidFactory(b"unit-b"),
            transform,
            unit_b,
            "pdf2kicad:U7_multi",
        )

        self.assertIn('(symbol "U7_multi_1_0"', definition)
        self.assertIn('(symbol "U7_multi_1_1"', definition)
        self.assertIn('(symbol "U7_multi_2_0"', definition)
        self.assertIn('(symbol "U7_multi_2_1"', definition)
        self.assertIn('(property "Reference" "U7"', placement_a)
        self.assertIn("\t\t(unit 1)\n", placement_a)
        self.assertIn('(property "Reference" "U7"', placement_b)
        self.assertIn("\t\t(unit 2)\n", placement_b)


class GlobalLabelTests(unittest.TestCase):
    def test_all_orcad_page_references_are_consumed(self):
        page_reference = {
            "text": "<7,15,16,18>",
            "color": "#ff0000",
            "x": 20.0,
            "y": 9.0,
            "x1": 28.0,
            "y1": 11.0,
            "size": 2.0,
            "angle": 0,
        }
        ordinary_red_text = {
            **page_reference,
            "text": "<RESET>",
            "x": 30.0,
            "x1": 38.0,
        }
        black_bracketed_number = {
            **page_reference,
            "color": "#000000",
            "x": 40.0,
            "x1": 44.0,
        }
        page = {
            "decoded": {},
            "texts": [
                page_reference,
                ordinary_red_text,
                black_bracketed_number,
            ],
        }
        consumed_texts = set()

        labels = pdf2kicad.decode_global_labels(
            page,
            [],
            consumed_texts,
            set(),
        )

        self.assertEqual(labels, [])
        self.assertEqual(
            consumed_texts,
            {pdf2kicad._text_key(page_reference)},
        )

    def test_complete_double_chevron_is_consumed(self):
        page = {
            "lines": [
                line("#000000", 8.4, 10.0, 9.0, 9.4),
                line("#000000", 8.4, 10.0, 9.0, 10.6),
                line("#000000", 8.8, 10.0, 9.4, 9.4),
                line("#000000", 8.8, 10.0, 9.4, 10.6),
                line("#000000", 10.0, 9.4, 10.6, 10.0),
                line("#000000", 10.0, 10.6, 10.6, 10.0),
                line("#000000", 10.4, 9.4, 11.0, 10.0),
                line("#000000", 10.4, 10.6, 11.0, 10.0),
            ],
            "texts": [{
                "text": "RESET_N",
                "color": "#ff0000",
                "x": 11.0,
                "y": 9.0,
                "x1": 19.0,
                "y1": 11.0,
                "size": 2.0,
                "angle": 0,
            }, {
                "text": "<11>",
                "color": "#ff0000",
                "x": 20.0,
                "y": 9.0,
                "x1": 24.0,
                "y1": 11.0,
                "size": 2.0,
                "angle": 0,
            }],
        }

        labels = pdf2kicad.pdf_dump.decode_global_labels(page)

        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["direction"], "right")
        self.assertEqual(labels[0]["line_indexes"], list(range(8)))
        self.assertEqual(labels[0]["hotpoint"], {"x": 8.4, "y": 10.0})
        self.assertEqual(
            [reference["text"] for reference in labels[0]["page_references"]],
            ["<11>"],
        )

        page["decoded"] = {"components": [{}]}
        consumed_texts = set()
        consumed_lines = set()
        pdf2kicad.decode_global_labels(
            page,
            [],
            consumed_texts,
            consumed_lines,
        )
        self.assertIn(pdf2kicad._text_key(page["texts"][1]), consumed_texts)

    def test_source_direction_controls_native_label_orientation(self):
        expected_angles = {
            "right": 0,
            "left": 180,
            "up": 90,
            "down": 270,
        }
        for direction, expected_angle in expected_angles.items():
            with self.subTest(direction=direction):
                decoded_label = {
                    "name": "RESET_N",
                    "direction": direction,
                    "apex": {"x": 9.0, "y": 10.0},
                    "base": {"x": 10.0, "y": 10.0},
                    "line_indexes": [2, 3],
                    "text": {"x": 7.0, "y": 9.0},
                }
                page = {
                    "decoded": {"components": [{}]},
                    "texts": [{
                        "text": "RESET_N",
                        "x": 7.0,
                        "y": 9.0,
                        "x1": 9.0,
                        "y1": 11.0,
                        "angle": 0,
                    }],
                }
                consumed_texts = set()
                consumed_lines = set()
                with mock.patch.object(
                    pdf2kicad.pdf_dump,
                    "decode_global_labels",
                    return_value=[decoded_label],
                ):
                    labels = pdf2kicad.decode_global_labels(
                        page,
                        [],
                        consumed_texts,
                        consumed_lines,
                    )

                self.assertEqual(labels[0]["angle"], expected_angle)
                self.assertEqual(labels[0]["direction"], direction)
                self.assertEqual(consumed_lines, {2, 3})
                self.assertEqual(len(consumed_texts), 1)

    def test_left_facing_label_body_is_away_from_right_hand_wire(self):
        label = {
            "name": "RESET_N",
            "point": (11.0, 10.0),
            "kind": "global",
            "direction": "left",
            "angle": 180,
        }
        rendered = pdf2kicad._label(
            pdf2kicad.UuidFactory(b"left-global-label"),
            pdf2kicad.coordinate_transform("A4"),
            label,
        )
        self.assertIn("(at 8.46 7.46 180)", rendered)
        self.assertIn("(justify right)", rendered)


if __name__ == "__main__":
    unittest.main()
