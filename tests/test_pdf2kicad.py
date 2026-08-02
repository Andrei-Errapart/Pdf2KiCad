#!/usr/bin/env python3
# Copyright (C) 2026 Andrei Errapart
# SPDX-License-Identifier: GPL-2.0-or-later

import copy
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

    def test_symbol_local_point_preserves_rounded_absolute_hotpoint(self):
        transform = pdf2kicad.coordinate_transform("A2")
        origin = (231.119, 13.6525)
        hotpoint = (227.33, 13.97)

        local_x, local_y = pdf2kicad._symbol_local_point(
            transform,
            origin,
            hotpoint,
        )
        origin_x, origin_y = transform.xy(*origin)

        self.assertEqual((local_x, local_y), (-7.58, -0.63))
        self.assertEqual(
            (
                round(origin_x + local_x, 2),
                round(origin_y - local_y, 2),
            ),
            transform.xy(*hotpoint),
        )


class PinLengthTests(unittest.TestCase):
    @staticmethod
    def named_two_pin_component():
        return {
            "reference": "U1",
            "value": "Example IC",
            "bbox": {
                "x0": 10.0,
                "y0": 10.0,
                "x1": 15.0,
                "y1": 15.0,
            },
            "body_lines": [],
            "pins": [
                {
                    "number": "1",
                    "name": "LEFT",
                    "hot": {"x": 8.0, "y": 11.5},
                    "other": {"x": 10.0, "y": 11.5},
                    "length": 2.0,
                },
                {
                    "number": "123",
                    "name": "RIGHT",
                    "hot": {"x": 17.0, "y": 13.5},
                    "other": {"x": 15.0, "y": 13.5},
                    "length": 2.0,
                },
            ],
        }

    def test_all_pins_fit_the_longest_visible_pin_number(self):
        rendered = pdf2kicad._symbol_definition(
            pdf2kicad.UuidFactory(b"elongated-pins"),
            pdf2kicad.coordinate_transform("A4"),
            self.named_two_pin_component(),
            "pdf2kicad:U1",
        )

        self.assertEqual(rendered.count("(length 5.08)"), 2)
        self.assertIn("(at -7.58 1.00 0)", rendered)
        self.assertIn("(at 7.58 -1.00 180)", rendered)

    def test_wire_endpoints_follow_elongated_pin_hotpoints(self):
        part = self.named_two_pin_component()
        relocations, bridges = pdf2kicad._pin_relocations(
            [part],
            pdf2kicad.coordinate_transform("A4"),
            {},
        )
        wire = {
            "start": {"x": 8.0, "y": 11.5},
            "end": {"x": 3.0, "y": 11.5},
        }

        relocated = pdf2kicad._wire_with_relocated_pins(
            wire,
            relocations,
        )

        self.assertEqual(relocated["start"], {"x": 4.92, "y": 11.5})
        self.assertEqual(relocated["end"], wire["end"])
        self.assertEqual(bridges, [])


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

    @staticmethod
    def page_record(paper, frame, title_block):
        return {
            "transform": pdf2kicad.coordinate_transform(paper),
            "semantic": {
                "worksheet": {
                    "frame": frame,
                    "title_block": title_block,
                },
            },
        }

    def test_source_worksheet_generates_default_kicad_style_at_page_edge(self):
        records = [
            self.page_record(
                "A2",
                {"x0": 1.27, "y0": 1.27, "x1": 295.656, "y1": 208.661},
                {
                    "x0": 249.936,
                    "y0": 197.231,
                    "x1": 295.656,
                    "y1": 208.661,
                },
            ),
            self.page_record(
                "A3",
                {"x0": 1.778, "y0": 1.778, "x1": 292.142, "y1": 206.079},
                {
                    "x0": 228.134,
                    "y0": 190.077,
                    "x1": 292.142,
                    "y1": 206.079,
                },
            ),
        ]

        self.assertTrue(pdf2kicad.has_source_worksheet(records))
        worksheet = pdf2kicad.generate_worksheet()
        self.assertIn(
            "(left_margin 0) (right_margin 0) "
            "(top_margin 0) (bottom_margin 0)",
            worksheet,
        )
        self.assertIn(
            "(start 0 0 ltcorner) (end 0 0 rbcorner)",
            worksheet,
        )
        self.assertIn("(start 110 34) (end 2 2)", worksheet)
        self.assertIn("(repeat 100)", worksheet)
        for field in (
            "Date: %D",
            "Rev: %R",
            "Size: %Z",
            "Id: %S/%N",
            "Title: %T",
            "File: %F",
            "Sheet: %P",
            "%Y",
            "%C0",
            "%C3",
        ):
            self.assertIn(field, worksheet)

    def test_pages_without_source_worksheet_do_not_generate_one(self):
        records = [{
            "transform": pdf2kicad.coordinate_transform("A4"),
            "semantic": {"worksheet": None},
        }]
        self.assertFalse(pdf2kicad.has_source_worksheet(records))

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
    @staticmethod
    def semantic_text(value, x, y, x1, y1, angle=0):
        return {
            "text": value,
            "color": "#000000",
            "x": x,
            "y": y,
            "x1": x1,
            "y1": y1,
            "size": 1.0,
            "angle": angle,
        }

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

    def test_body_stub_touching_edge_interior_stays_with_symbol(self):
        page = {
            "lines": [
                line(pdf2kicad.BODY_COLOR, 10.0, 15.0, 20.0, 10.0),
                line(pdf2kicad.BODY_COLOR, 20.0, 10.0, 20.0, 20.0),
                line(pdf2kicad.BODY_COLOR, 20.0, 20.0, 10.0, 15.0),
                # This endpoint lands on the middle of the sloped edge.
                line(pdf2kicad.BODY_COLOR, 16.0, 12.0, 16.0, 10.0),
                line(pdf2kicad.PIN_COLOR, 7.0, 15.0, 10.0, 15.0),
                line(pdf2kicad.PIN_COLOR, 20.0, 15.0, 23.0, 15.0),
                line(pdf2kicad.PIN_COLOR, 16.0, 8.0, 16.0, 10.0),
            ],
            "curves": [],
        }

        clusters = pdf2kicad._geometry_clusters(page, set())

        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]["body_lines"]), 4)
        self.assertEqual(len(clusters[0]["pin_lines"]), 3)

    def test_directly_connected_resistor_and_laser_diode_stay_separate(self):
        r296 = self.semantic_text("R296", 11.0, 10.0, 14.0, 11.0)
        ld7 = self.semantic_text("LD7", 11.0, 17.0, 13.0, 18.0)
        page = {
            "lines": [
                line(pdf2kicad.BODY_COLOR, 10.0, 10.0, 10.0, 12.0),
                line(pdf2kicad.PIN_COLOR, 10.0, 8.0, 10.0, 10.0),
                line(pdf2kicad.PIN_COLOR, 10.0, 12.0, 10.0, 14.0),
                # The two component pins meet directly at (10, 14).
                line(pdf2kicad.PIN_COLOR, 10.0, 14.0, 10.0, 16.0),
                line(pdf2kicad.BODY_COLOR, 10.0, 16.0, 10.0, 17.0),
                line(None, 10.0, 17.0, 10.8, 18.2),
                line(None, 10.8, 18.2, 9.2, 18.2),
                line(None, 9.2, 18.2, 10.0, 17.0),
                line(pdf2kicad.BODY_COLOR, 10.0, 18.2, 10.0, 20.0),
                line(pdf2kicad.PIN_COLOR, 10.0, 20.0, 10.0, 22.0),
            ],
            "rectangles": [],
            "curves": [],
            "texts": [r296, ld7],
            "decoded": {
                "components": [],
                "wires": [],
            },
        }

        components, consumed_texts, _consumed_lines = (
            pdf2kicad.decode_components(page)
        )
        by_reference = {
            component["reference"]: component
            for component in components
        }

        self.assertIsNotNone(pdf2kicad.REF_RE.fullmatch("LD7"))
        self.assertEqual(set(by_reference), {"R296", "LD7"})
        self.assertEqual(len(by_reference["R296"]["pins"]), 2)
        self.assertEqual(len(by_reference["LD7"]["pins"]), 2)
        self.assertTrue(by_reference["R296"]["body_lines"])
        self.assertEqual(len(by_reference["LD7"]["body_lines"]), 5)
        self.assertIn(pdf2kicad._text_key(r296), consumed_texts)
        self.assertIn(pdf2kicad._text_key(ld7), consumed_texts)

    def test_curve_only_inductor_becomes_native_two_pin_symbol(self):
        reference = self.semantic_text("L17", 10.0, 6.0, 12.0, 7.0)
        value = self.semantic_text("2R2", 10.0, 7.0, 12.0, 8.0)
        page = {
            "lines": [
                line(pdf2kicad.PIN_COLOR, 8.0, 10.0, 10.0, 10.0),
                line(pdf2kicad.PIN_COLOR, 14.04, 10.0, 16.0, 10.0),
            ],
            "rectangles": [],
            "curves": [
                {
                    "points": [[10.0, 10.0], [11.0, 9.0], [12.0, 10.0]],
                    "color": pdf2kicad.BODY_COLOR,
                    "fill": None,
                    "width": 0.1,
                },
                {
                    "points": [[12.04, 10.0], [13.0, 9.0], [14.0, 10.0]],
                    "color": pdf2kicad.BODY_COLOR,
                    "fill": None,
                    "width": 0.1,
                },
            ],
            "texts": [reference, value],
            "decoded": {
                "components": [],
                "wires": [],
            },
        }

        components, consumed_texts, consumed_lines = (
            pdf2kicad.decode_components(page)
        )
        pdf2kicad.assign_values(page, components, consumed_texts)

        self.assertEqual(len(components), 1)
        recovered = components[0]
        self.assertEqual(recovered["reference"], "L17")
        self.assertEqual(recovered["value"], "2R2")
        self.assertEqual(len(recovered["pins"]), 2)
        self.assertEqual(len(recovered["body_curves"]), 2)
        self.assertEqual(recovered["curve_indexes"], {0, 1})
        self.assertEqual(consumed_lines, {0, 1})

    def test_visible_numbers_replace_generated_nonrectangular_pin_numbers(self):
        reference = self.semantic_text("U51", 20.0, 20.0, 22.0, 21.0)
        number_texts = [
            self.semantic_text("4", 8.5, 13.5, 9.5, 14.5),
            self.semantic_text("2", 20.5, 13.5, 21.5, 14.5),
            self.semantic_text("1", 14.5, 7.5, 15.5, 8.5, 90),
            self.semantic_text("5", 16.5, 7.5, 17.5, 8.5, 90),
            self.semantic_text("3", 16.5, 21.5, 17.5, 22.5, 90),
        ]
        page = {
            "lines": [
                line(pdf2kicad.BODY_COLOR, 10.0, 15.0, 20.0, 10.0),
                line(pdf2kicad.BODY_COLOR, 20.0, 10.0, 20.0, 20.0),
                line(pdf2kicad.BODY_COLOR, 20.0, 20.0, 10.0, 15.0),
                line(pdf2kicad.PIN_COLOR, 7.0, 15.0, 10.0, 15.0),
                line(pdf2kicad.PIN_COLOR, 20.0, 15.0, 23.0, 15.0),
                line(pdf2kicad.PIN_COLOR, 16.0, 7.0, 16.0, 12.0),
                line(pdf2kicad.PIN_COLOR, 18.0, 7.0, 18.0, 11.0),
                line(pdf2kicad.PIN_COLOR, 18.0, 23.0, 18.0, 19.0),
            ],
            "rectangles": [],
            "curves": [],
            "texts": [reference, *number_texts],
            "decoded": {
                "components": [],
                "wires": [],
            },
        }

        components, consumed_texts, _consumed_lines = (
            pdf2kicad.decode_components(page)
        )
        recovered = components[0]
        numbers_by_hotpoint = {
            (pin["hot"]["x"], pin["hot"]["y"]): pin["number"]
            for pin in recovered["pins"]
        }

        self.assertEqual(
            numbers_by_hotpoint,
            {
                (7.0, 15.0): "4",
                (23.0, 15.0): "2",
                (16.0, 7.0): "1",
                (18.0, 7.0): "5",
                (18.0, 23.0): "3",
            },
        )
        for number_text in number_texts:
            self.assertIn(pdf2kicad._text_key(number_text), consumed_texts)

    def test_matching_maximizes_cardinality_before_minimizing_cost(self):
        pairs = pdf2kicad._maximum_cardinality_pairs([
            (1.0, 0, 0),
            (2.0, 0, 1),
            (2.0, 1, 0),
            (100.0, 1, 1),
        ])

        self.assertEqual(
            {(cluster, text) for _score, cluster, text in pairs},
            {(0, 1), (1, 0)},
        )

    def test_spacer_pin_stub_connects_to_its_wire(self):
        reference = self.semantic_text("SP1", 10.0, 7.0, 12.0, 8.0)
        number = self.semantic_text("1", 14.0, 11.0, 15.0, 12.0)
        source_component = {
            "reference": "SP1",
            "reference_text": reference,
            "bbox": {
                "x0": 10.0,
                "y0": 10.0,
                "x1": 13.0,
                "y1": 13.0,
            },
            "pins": [],
        }
        page = {
            "lines": [
                line(pdf2kicad.BODY_COLOR, 10.0, 11.5, 13.0, 11.5),
                line(pdf2kicad.PIN_COLOR, 14.0, 11.5, 15.0, 11.5),
            ],
            "rectangles": [],
            "curves": [],
            "texts": [reference, number],
            "decoded": {
                "components": [source_component],
                "wires": [{
                    "start": {"x": 15.0, "y": 11.5},
                    "end": {"x": 18.0, "y": 11.5},
                    "length": 3.0,
                }],
            },
        }

        components, _consumed_texts, consumed_lines = (
            pdf2kicad.decode_components(page)
        )
        recovered = components[0]

        self.assertEqual(
            recovered["pins"],
            [{
                "hot": {"x": 15.0, "y": 11.5},
                "other": {"x": 14.0, "y": 11.5},
                "length": 1.0,
                "number": "1",
                "line_index": 1,
            }],
        )
        self.assertEqual(consumed_lines, {0, 1})

    def test_passive_value_can_be_a_rotated_vendor_part_number(self):
        reference = self.semantic_text("R1", 8.0, 9.0, 9.0, 12.0, 90)
        value = self.semantic_text(
            "CRF0805-FZ-R001ELF",
            10.0,
            13.5,
            19.0,
            14.5,
        )
        source_component = component("R1")
        source_component["reference_text"] = reference
        source_component["bbox"] = {
            "x0": 10.0,
            "y0": 10.0,
            "x1": 12.0,
            "y1": 12.0,
        }
        consumed_texts = {pdf2kicad._text_key(reference)}
        page = {
            "texts": [reference, value],
            "decoded": {"wires": []},
        }

        pdf2kicad.assign_values(
            page,
            [source_component],
            consumed_texts,
        )

        self.assertEqual(source_component["value"], value["text"])
        self.assertIs(source_component["value_text"], value)
        self.assertIn(pdf2kicad._text_key(value), consumed_texts)

    def test_pin_name_overline_becomes_native_negation(self):
        name_text = {
            **self.semantic_text("CTS", 10.5, 11.5, 13.0, 12.5),
            "color": pdf2kicad.PIN_NAME_COLOR,
        }
        source_component = component("U1")
        source_component["pins"][0]["name"] = "CTS"
        source_component["pins"][0]["name_text"] = name_text
        source_component["line_indexes"] = set()
        page = {
            "lines": [
                line(
                    pdf2kicad.PIN_NAME_COLOR,
                    10.5,
                    11.43,
                    13.0,
                    11.43,
                ),
            ],
        }

        consumed = pdf2kicad._recover_negated_pin_names(
            page,
            source_component,
        )
        rendered = pdf2kicad._symbol_definition(
            pdf2kicad.UuidFactory(b"negated-pin-name"),
            pdf2kicad.coordinate_transform("A4"),
            source_component,
            "pdf2kicad:U1",
        )

        self.assertEqual(source_component["pins"][0]["name"], "~{CTS}")
        self.assertEqual(consumed, {0})
        self.assertEqual(source_component["line_indexes"], {0})
        self.assertIn('(name "~{CTS}"', rendered)

    def test_bubbled_overlined_pin_becomes_native_inverted_pin(self):
        reference = self.semantic_text("U28", 10.45, 7.0, 12.5, 8.0)
        number = self.semantic_text("15", 9.0, 14.0, 10.2, 15.0)
        name = {
            **self.semantic_text("1OE", 11.0, 14.5, 13.5, 15.5),
            "color": pdf2kicad.PIN_NAME_COLOR,
        }
        page = {
            "width": 100.0,
            "height": 80.0,
            "lines": [
                line(pdf2kicad.PIN_COLOR, 7.0, 15.0, 9.6, 15.0),
                line(
                    pdf2kicad.PIN_NAME_COLOR,
                    11.0,
                    14.43,
                    13.5,
                    14.43,
                ),
            ],
            "rectangles": [{
                "x0": 10.45,
                "y0": 10.0,
                "x1": 20.0,
                "y1": 20.0,
                "color": pdf2kicad.BODY_COLOR,
                "fill": None,
                "width": 0.1,
            }],
            "curves": [
                {
                    "points": points,
                    "color": pdf2kicad.PIN_COLOR,
                    "fill": "#ffffff",
                    "width": 0.1,
                }
                for points in (
                    [[9.6, 15.0], [10.0, 14.6]],
                    [[10.0, 14.6], [10.4, 15.0]],
                    [[10.4, 15.0], [10.0, 15.4]],
                    [[10.0, 15.4], [9.6, 15.0]],
                )
            ],
            "texts": [reference, number, name],
        }

        components, consumed_texts, consumed_lines = (
            pdf2kicad.decode_components(page)
        )
        recovered = components[0]
        pin = recovered["pins"][0]
        recovered["value"] = "Example"
        rendered = pdf2kicad._symbol_definition(
            pdf2kicad.UuidFactory(b"inverted-pin"),
            pdf2kicad.coordinate_transform("A4"),
            recovered,
            "pdf2kicad:U28",
        )

        self.assertEqual(pin["number"], "15")
        self.assertEqual(pin["name"], "~{1OE}")
        self.assertEqual(pin["graphic_style"], "inverted")
        self.assertEqual(pin["hot"], {"x": 7.0, "y": 15.0})
        self.assertEqual(pin["other"], {"x": 10.45, "y": 15.0})
        self.assertEqual(recovered["curve_indexes"], {0, 1, 2, 3})
        self.assertEqual(consumed_lines, {0, 1})
        self.assertIn(pdf2kicad._text_key(number), consumed_texts)
        self.assertIn(pdf2kicad._text_key(name), consumed_texts)
        self.assertIn("(pin passive inverted", rendered)
        self.assertIn('(name "~{1OE}"', rendered)

    def test_rotated_reference_bank_prefers_body_after_own_text(self):
        references = [
            self.semantic_text(value, x, 10.0, x + 1.0, 13.0, 90)
            for value, x in (
                ("R304", 6.3),
                ("R305", 11.3),
                ("C648", 16.3),
                ("C649", 21.3),
            )
        ]
        page_lines = []
        for x in (10.0, 15.0, 20.0, 25.0):
            page_lines.extend([
                line(pdf2kicad.BODY_COLOR, x, 10.0, x, 12.0),
                line(pdf2kicad.PIN_COLOR, x, 8.0, x, 10.0),
                line(pdf2kicad.PIN_COLOR, x, 12.0, x, 14.0),
            ])
        page = {
            "lines": page_lines,
            "rectangles": [],
            "curves": [],
            "texts": references,
            "decoded": {"components": [], "wires": []},
        }

        components, _consumed_texts, _consumed_lines = (
            pdf2kicad.decode_components(page)
        )
        centers = {
            item["reference"]: round(
                (item["bbox"]["x0"] + item["bbox"]["x1"]) / 2,
                1,
            )
            for item in components
        }

        self.assertEqual(
            centers,
            {
                "R304": 10.0,
                "R305": 15.0,
                "C648": 20.0,
                "C649": 25.0,
            },
        )

    def test_jsw_reference_and_value_use_switch_body(self):
        reference = self.semantic_text("JSW1", 10.0, 15.5, 13.0, 16.5)
        duplicate_reference = self.semantic_text(
            "JSW1",
            2.0,
            25.0,
            5.0,
            26.0,
        )
        value = self.semantic_text(
            "CJS-1200A1",
            10.0,
            17.0,
            16.0,
            18.0,
        )
        decoy_reference = self.semantic_text(
            "DSW3",
            25.0,
            10.0,
            28.0,
            11.0,
        )
        page = {
            "width": 100.0,
            "height": 80.0,
            "lines": [
                line(pdf2kicad.PIN_COLOR, 8.0, 11.0, 10.0, 11.0),
                line(pdf2kicad.PIN_COLOR, 8.0, 14.0, 10.0, 14.0),
                line(pdf2kicad.PIN_COLOR, 15.0, 12.5, 17.0, 12.5),
                line(pdf2kicad.WIRE_COLOR, 40.0, 40.0, 50.0, 40.0),
            ],
            "rectangles": [{
                "x0": 10.0,
                "y0": 10.0,
                "x1": 15.0,
                "y1": 15.0,
                "color": pdf2kicad.BODY_COLOR,
                "fill": None,
                "width": 0.1,
            }],
            "curves": [],
            "texts": [
                duplicate_reference,
                decoy_reference,
                value,
                reference,
            ],
        }

        components, consumed_texts, _consumed_lines = (
            pdf2kicad.decode_components(page)
        )
        pdf2kicad.assign_values(page, components, consumed_texts)
        switches = [
            item for item in components if item["reference"] == "JSW1"
        ]

        self.assertIsNotNone(pdf2kicad.REF_RE.fullmatch("JSW1"))
        self.assertIsNotNone(pdf2kicad.REF_RE.fullmatch("PCIE1"))
        self.assertEqual(len(switches), 1)
        self.assertEqual(len(switches[0]["pins"]), 3)
        self.assertEqual(switches[0]["value"], "CJS-1200A1")
        self.assertIs(switches[0]["value_text"], value)

    def test_black_reference_wins_over_blue_pin_name(self):
        body = {"x0": 10.0, "y0": 10.0, "x1": 25.0, "y1": 20.0}
        pin_name = {
            **self.semantic_text("D1", 23.0, 12.0, 24.0, 13.0),
            "color": pdf2kicad.PIN_NAME_COLOR,
        }
        reference = self.semantic_text("Q5", 18.0, 18.0, 20.0, 19.0)

        recovered = pdf2kicad.pdf_dump._nearest_reference(
            body,
            [pin_name, reference],
        )

        self.assertEqual(recovered["text"], "Q5")

    def test_capacitor_plates_are_consumed_into_symbol_body(self):
        reference = {
            "text": "C6",
            "color": "#000000",
            "x": 11.0,
            "y": 13.0,
            "x1": 12.0,
            "y1": 14.0,
            "size": 1.0,
            "angle": 90,
        }
        pins = [
            {
                "hot": {"x": 10.0, "y": 8.0},
                "other": {"x": 10.0, "y": 9.0},
                "length": 1.0,
                "number": "1",
            },
            {
                "hot": {"x": 10.0, "y": 12.0},
                "other": {"x": 10.0, "y": 11.0},
                "length": 1.0,
                "number": "2",
            },
        ]
        page = {
            "lines": [
                line(pdf2kicad.BODY_COLOR, 9.0, 9.5, 11.0, 9.5),
                line(pdf2kicad.BODY_COLOR, 9.0, 10.5, 11.0, 10.5),
                line(pdf2kicad.BODY_COLOR, 10.0, 9.0, 10.0, 9.5),
                line(pdf2kicad.BODY_COLOR, 10.0, 10.5, 10.0, 11.0),
                line(pdf2kicad.PIN_COLOR, 10.0, 8.0, 10.0, 9.0),
                line(pdf2kicad.PIN_COLOR, 10.0, 11.0, 10.0, 12.0),
            ],
            "rectangles": [],
            "curves": [],
            "texts": [reference],
            "decoded": {
                "components": [{
                    "reference": "C6",
                    "reference_text": reference,
                    "bbox": {
                        "x0": 10.0,
                        "y0": 9.0,
                        "x1": 10.0,
                        "y1": 11.0,
                    },
                    "pins": pins,
                }],
                "wires": [],
            },
        }

        components, _consumed_texts, consumed_lines = (
            pdf2kicad.decode_components(page)
        )

        self.assertEqual(len(components[0]["body_lines"]), 4)
        self.assertEqual(consumed_lines, set(range(6)))
        self.assertEqual(
            components[0]["bbox"],
            {"x0": 9.0, "y0": 9.0, "x1": 11.0, "y1": 11.0},
        )

    def test_ferrite_curves_and_second_pin_are_part_of_symbol(self):
        reference = {
            "text": "FB1",
            "color": "#000000",
            "x": 10.0,
            "y": 7.0,
            "x1": 12.0,
            "y1": 8.0,
            "size": 1.0,
            "angle": 0,
        }
        page = {
            "lines": [
                line(pdf2kicad.BODY_COLOR, 10.0, 10.0, 10.5, 10.0),
                line(pdf2kicad.BODY_COLOR, 13.5, 10.0, 14.0, 10.0),
                line(pdf2kicad.PIN_COLOR, 8.0, 10.0, 10.0, 10.0),
                line(pdf2kicad.PIN_COLOR, 14.0, 10.0, 16.0, 10.0),
            ],
            "rectangles": [{
                "x0": 10.0,
                "y0": 9.0,
                "x1": 14.0,
                "y1": 11.0,
                "color": pdf2kicad.BODY_COLOR,
                "fill": None,
                "width": 0.1,
            }],
            "curves": [{
                "points": [[10.5, 10.0], [12.0, 9.2], [13.5, 10.0]],
                "color": pdf2kicad.BODY_COLOR,
                "fill": None,
                "width": 0.1,
            }],
            "texts": [reference],
            "decoded": {
                "components": [{
                    "reference": "FB1",
                    "reference_text": reference,
                    "bbox": {
                        "x0": 10.0,
                        "y0": 10.0,
                        "x1": 10.5,
                        "y1": 10.0,
                    },
                    "pins": [{
                        "hot": {"x": 8.0, "y": 10.0},
                        "other": {"x": 10.0, "y": 10.0},
                        "length": 2.0,
                        "number": "1",
                    }],
                }],
                "wires": [],
            },
        }

        components, _consumed_texts, consumed_lines = (
            pdf2kicad.decode_components(page)
        )
        recovered = components[0]

        self.assertEqual(len(recovered["pins"]), 2)
        self.assertEqual(len(recovered["body_rectangles"]), 1)
        self.assertEqual(len(recovered["body_curves"]), 1)
        self.assertEqual(consumed_lines, {0, 1, 2, 3})
        rendered = pdf2kicad._symbol_definition(
            pdf2kicad.UuidFactory(b"ferrite"),
            pdf2kicad.coordinate_transform("A4"),
            {**recovered, "value": "600R"},
            "pdf2kicad:FB1",
        )
        self.assertIn("(rectangle", rendered)
        self.assertIn("(pts (xy", rendered)

    def test_noise_filter_reference_recovers_rectangular_body(self):
        self.assertIsNotNone(pdf2kicad.REF_RE.fullmatch("FL1"))
        reference = {
            "text": "NF1",
            "color": "#000000",
            "x": 10.0,
            "y": 7.0,
            "x1": 12.0,
            "y1": 8.0,
            "size": 1.0,
            "angle": 0,
        }
        page = {
            "width": 100.0,
            "height": 80.0,
            "lines": [
                line(pdf2kicad.PIN_COLOR, 8.0, 10.0, 10.0, 10.0),
                line(pdf2kicad.PIN_COLOR, 8.0, 12.0, 10.0, 12.0),
                line(pdf2kicad.PIN_COLOR, 14.0, 10.0, 16.0, 10.0),
                line(pdf2kicad.PIN_COLOR, 14.0, 12.0, 16.0, 12.0),
            ],
            "rectangles": [{
                "x0": 10.0,
                "y0": 9.0,
                "x1": 14.0,
                "y1": 13.0,
                "color": pdf2kicad.BODY_COLOR,
                "fill": None,
                "width": 0.1,
            }],
            "curves": [],
            "texts": [reference],
        }

        decoded = pdf2kicad.pdf_dump.decode_page(page)

        self.assertEqual(len(decoded["components"]), 1)
        self.assertEqual(decoded["components"][0]["reference"], "NF1")
        self.assertEqual(len(decoded["components"][0]["pins"]), 4)

    def test_coalesced_passive_reference_and_value_are_split(self):
        merged = {
            "text": "R13110K/0603",
            "color": "#000000",
            "x": 9.0,
            "y": 7.0,
            "x1": 16.0,
            "y1": 8.0,
            "size": 1.0,
            "angle": 0,
        }
        page = {
            "lines": [
                line(pdf2kicad.PIN_COLOR, 7.0, 10.0, 10.0, 10.0),
                line(pdf2kicad.BODY_COLOR, 10.0, 10.0, 14.0, 10.0),
                line(pdf2kicad.PIN_COLOR, 14.0, 10.0, 17.0, 10.0),
            ],
            "rectangles": [],
            "curves": [],
            "texts": [
                merged,
                {
                    **merged,
                    "text": "R133",
                    "x": 30.0,
                    "x1": 33.0,
                },
            ],
            "decoded": {
                "components": [],
                "wires": [],
            },
        }

        components, consumed, _lines = pdf2kicad.decode_components(page)
        pdf2kicad.assign_values(page, components, consumed)

        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["reference"], "R131")
        self.assertEqual(components[0]["value"], "10K/0603")
        self.assertNotIn(
            "R13110K/0603",
            [text["text"] for text in page["texts"]],
        )

    def test_connector_designator_like_pin_labels_stay_on_body(self):
        def text(value, x, y, x1, y1, angle=0):
            return {
                "text": value,
                "color": "#000000",
                "x": x,
                "y": y,
                "x1": x1,
                "y1": y1,
                "size": 1.0,
                "angle": angle,
            }

        numbers = [
            text(str(number), 7.0, 10.5 + number, 8.0, 11.5 + number)
            for number in range(1, 6)
        ]
        fields = {
            "F1": text("F1", 12.2, 21.5, 13.0, 23.0, 90),
            "F2": text("F2", 12.2, 7.0, 13.0, 8.5, 90),
            "F3": text("F3", 15.2, 21.5, 16.0, 23.0, 90),
            "F4": text("F4", 15.2, 7.0, 16.0, 8.5, 90),
        }
        pins = [
            {
                "number": str(number),
                "number_text": numbers[number - 1],
                "hot": {"x": 8.0, "y": 11.0 + number},
                "other": {"x": 10.0, "y": 11.0 + number},
                "length": 2.0,
            }
            for number in range(1, 6)
        ] + [
            {
                "number": str(number),
                "hot": {"x": x, "y": hot_y},
                "other": {"x": x, "y": other_y},
                "length": 2.0,
            }
            for number, x, hot_y, other_y in (
                (6, 13.0, 22.0, 20.0),
                (7, 13.0, 8.0, 10.0),
                (8, 16.0, 22.0, 20.0),
                (9, 16.0, 8.0, 10.0),
            )
        ]
        pin_lines = [
            line(
                pdf2kicad.PIN_COLOR,
                pin["hot"]["x"],
                pin["hot"]["y"],
                pin["other"]["x"],
                pin["other"]["y"],
            )
            for pin in pins
        ]
        cn2 = text("CN2", 20.5, 11.0, 23.0, 12.0)
        page = {
            "lines": pin_lines,
            "rectangles": [],
            "curves": [],
            "texts": [cn2, *numbers, *fields.values()],
            "decoded": {
                "components": [{
                    "reference": "F2",
                    "reference_text": fields["F2"],
                    "bbox": {
                        "x0": 10.0,
                        "y0": 10.0,
                        "x1": 20.0,
                        "y1": 20.0,
                    },
                    "pins": pins,
                }],
                "wires": [{
                    "start": {"x": 0.0, "y": 30.0},
                    "end": {"x": 30.0, "y": 30.0},
                }],
            },
        }

        components, consumed, _lines = pdf2kicad.decode_components(page)

        self.assertEqual([item["reference"] for item in components], ["CN2"])
        recovered = {
            pin["number"]: pin.get("name")
            for pin in components[0]["pins"]
        }
        self.assertEqual(
            recovered,
            {label: label for label in ("1", "2", "3", "4", "5",
                                        "F1", "F2", "F3", "F4")},
        )
        for pin_text in fields.values():
            self.assertIn(pdf2kicad._text_key(pin_text), consumed)

    def test_values_directly_below_rectangular_bodies_are_paired(self):
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

        references = [
            text("CN5", 10.0, 7.0, 12.0, 8.0),
            text("U21", 50.0, 7.0, 52.0, 8.0),
            text("FB22", 87.0, 9.0, 90.0, 10.0),
            text("SP1", 110.0, 7.0, 112.0, 8.0),
        ]
        values = [
            text("JXD0-0019NL", 10.0, 40.1, 18.0, 41.1),
            text("KSZ9131RNXI", 50.0, 40.1, 58.0, 41.1),
            text("BLM15AX121SN1D", 87.0, 12.5, 97.0, 13.5),
            text("2SSB-4.0", 109.0, 14.5, 115.0, 15.5),
        ]
        inside_body_label = text("S1.8V", 50.0, 39.8, 54.0, 40.8)
        components = [
            {
                "reference": reference["text"],
                "reference_text": reference,
                "bbox": bbox,
            }
            for reference, bbox in zip(
                references,
                (
                    {"x0": 10.0, "y0": 10.0, "x1": 30.0, "y1": 40.0},
                    {"x0": 50.0, "y0": 10.0, "x1": 70.0, "y1": 40.0},
                    {"x0": 90.0, "y0": 10.0, "x1": 94.0, "y1": 12.0},
                    {
                        "x0": 110.0,
                        "y0": 10.0,
                        "x1": 114.0,
                        "y1": 14.0,
                    },
                ),
            )
        ]
        consumed = {
            pdf2kicad._text_key(reference)
            for reference in references
        }

        pdf2kicad.assign_values(
            {"texts": [*references, inside_body_label, *values]},
            components,
            consumed,
        )

        self.assertEqual(
            [component["value"] for component in components],
            [value["text"] for value in values],
        )
        self.assertNotIn(
            pdf2kicad._text_key(inside_body_label),
            consumed,
        )

    def test_passive_value_is_not_claimed_by_nearby_connector(self):
        def text(value, x, y, x1, y1):
            return {
                "text": value,
                "color": "#000000",
                "x": x,
                "y": y,
                "x1": x1,
                "y1": y1,
                "size": 1.15,
                "angle": 0,
            }

        c133 = text("C133", 195.575, 97.859, 198.332, 99.135)
        cn15 = text("CN15", 208.91, 99.892, 211.84, 101.167)
        capacitor_value = text(
            "0.1u/10V/0603/X7R",
            195.575,
            99.129,
            205.555,
            100.405,
        )
        connector_value = text(
            "DF40TC(4.0)-30DS-0.4V(58)",
            205.735,
            124.53,
            220.469,
            125.806,
        )
        components = [
            {
                "reference": "C133",
                "reference_text": c133,
                "bbox": {
                    "x0": 193.675,
                    "y0": 97.79,
                    "x1": 194.945,
                    "y1": 99.06,
                },
            },
            {
                "reference": "CN15",
                "reference_text": cn15,
                "bbox": {
                    "x0": 209.55,
                    "y0": 101.6,
                    "x1": 215.9,
                    "y1": 121.92,
                },
            },
        ]
        consumed = {
            pdf2kicad._text_key(c133),
            pdf2kicad._text_key(cn15),
        }

        pdf2kicad.assign_values(
            {
                "texts": [
                    c133,
                    cn15,
                    capacitor_value,
                    connector_value,
                ],
            },
            components,
            consumed,
        )

        self.assertEqual(
            [component["value"] for component in components],
            [
                "0.1u/10V/0603/X7R",
                "DF40TC(4.0)-30DS-0.4V(58)",
            ],
        )

    def test_j_connector_numeric_pdf_names_are_suppressed(self):
        def text(value, x, y, x1, y1, color="#000000"):
            return {
                "text": value,
                "color": color,
                "x": x,
                "y": y,
                "x1": x1,
                "y1": y1,
                "size": 1.0,
                "angle": 0,
            }

        reference = text("J4", 10.0, 7.0, 12.0, 8.0)
        value = text("HIF3H-16DA-2.54DSA", 8.0, 20.5, 16.0, 21.5)
        merged_name = text("1112", 10.5, 14.0, 12.5, 15.0, "#0000cc")
        pins = [
            {
                "number": "11",
                "name": "1112",
                "name_text": merged_name,
                "hot": {"x": 8.0, "y": 14.0},
                "other": {"x": 10.0, "y": 14.0},
                "length": 2.0,
            },
            {
                "number": "12",
                "hot": {"x": 16.0, "y": 14.0},
                "other": {"x": 14.0, "y": 14.0},
                "length": 2.0,
            },
        ]
        page = {
            "lines": [],
            "rectangles": [{
                "x0": 10.0,
                "y0": 10.0,
                "x1": 14.0,
                "y1": 20.0,
                "color": pdf2kicad.BODY_COLOR,
                "fill": None,
                "width": 0.1,
            }],
            "curves": [],
            "texts": [reference, value, merged_name],
            "decoded": {
                "components": [{
                    "reference": "J4",
                    "reference_text": reference,
                    "bbox": {
                        "x0": 10.0,
                        "y0": 10.0,
                        "x1": 14.0,
                        "y1": 20.0,
                    },
                    "pins": pins,
                }],
                "wires": [],
            },
        }

        components, consumed, _lines = pdf2kicad.decode_components(page)
        pdf2kicad.assign_values(page, components, consumed)

        recovered = components[0]
        self.assertEqual(recovered["value"], "HIF3H-16DA-2.54DSA")
        self.assertTrue(all(not pin.get("name") for pin in recovered["pins"]))
        rendered = pdf2kicad._symbol_definition(
            pdf2kicad.UuidFactory(b"j-connector"),
            pdf2kicad.coordinate_transform("A4"),
            recovered,
            "pdf2kicad:J4",
        )
        self.assertIn("(pin_names (offset 1.016) hide)", rendered)
        self.assertNotIn('(name "1112"', rendered)

    def test_gp_reference_and_segmented_circle_become_one_symbol(self):
        reference = {
            "text": "GP1",
            "color": "#000000",
            "x": 9.0,
            "y": 7.0,
            "x1": 11.0,
            "y1": 8.0,
            "size": 1.0,
            "angle": 0,
        }
        value = {
            **reference,
            "text": "HK-2-G",
            "y": 8.2,
            "y1": 9.2,
        }
        page = {
            "lines": [
                line(pdf2kicad.BODY_COLOR, 10.0, 12.0, 10.0, 13.0),
                line(pdf2kicad.PIN_COLOR, 10.0, 13.0, 10.0, 15.0),
            ],
            "rectangles": [],
            "curves": [
                {
                    "points": points,
                    "color": pdf2kicad.BODY_COLOR,
                    "fill": None,
                    "width": 0.1,
                }
                for points in (
                    [[10.0, 10.0], [11.0, 11.0]],
                    [[11.0, 11.0], [10.0, 12.0]],
                    [[10.0, 12.0], [9.0, 11.0]],
                    [[9.0, 11.0], [10.0, 10.0]],
                )
            ],
            "texts": [reference, value],
            "decoded": {
                "components": [],
                "wires": [],
            },
        }

        components, consumed, _lines = pdf2kicad.decode_components(page)
        pdf2kicad.assign_values(page, components, consumed)

        self.assertIsNotNone(pdf2kicad.REF_RE.fullmatch("GP1"))
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["reference"], "GP1")
        self.assertEqual(components[0]["value"], "HK-2-G")
        self.assertEqual(len(components[0]["body_curves"]), 4)
        self.assertEqual(components[0]["curve_indexes"], {0, 1, 2, 3})

    def test_testpoint_segmented_circle_is_absorbed_without_neighbour(self):
        reference = {
            "text": "TP1",
            "color": "#000000",
            "x": 13.5,
            "y": 8.0,
            "x1": 15.5,
            "y1": 9.0,
            "size": 1.0,
            "angle": 0,
        }
        circle = [
            [[12.0, 9.0], [13.0, 10.0]],
            [[13.0, 10.0], [12.0, 11.0]],
            [[12.0, 11.0], [11.0, 10.0]],
            [[11.0, 10.0], [12.0, 9.0]],
        ]
        curves = [
            {
                "points": points,
                "color": pdf2kicad.BODY_COLOR,
                "fill": None,
                "width": 0.1,
            }
            for points in circle
        ]
        curves.append({
            "points": [[11.0, 11.17], [12.0, 12.17]],
            "color": pdf2kicad.BODY_COLOR,
            "fill": None,
            "width": 0.1,
        })
        page = {
            "lines": [
                line(pdf2kicad.PIN_COLOR, 8.0, 10.0, 10.0, 10.0),
                line(pdf2kicad.BODY_COLOR, 10.0, 10.0, 11.0, 10.0),
            ],
            "rectangles": [],
            "curves": curves,
            "texts": [reference],
            "decoded": {
                "components": [],
                "wires": [],
            },
        }

        components, _consumed, _lines = pdf2kicad.decode_components(page)

        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["reference"], "TP1")
        self.assertEqual(len(components[0]["body_curves"]), 4)
        self.assertEqual(components[0]["curve_indexes"], {0, 1, 2, 3})

    def test_passive_reference_is_reassigned_from_multi_pin_body(self):
        r383 = {
            "text": "R383",
            "color": "#000000",
            "x": 20.0,
            "y": 11.0,
            "x1": 22.0,
            "y1": 12.0,
            "size": 1.0,
            "angle": 0,
        }
        u7 = {
            "text": "U7",
            "color": "#000000",
            "x": 10.0,
            "y": 3.0,
            "x1": 12.0,
            "y1": 4.0,
            "size": 1.0,
            "angle": 0,
        }
        wrong_component = component("R383")
        wrong_component["reference_text"] = r383
        wrong_component["pins"] = [
            {
                "number": str(number),
                "hot": {"x": 8.0, "y": 10.0 + number},
                "other": {"x": 10.0, "y": 10.0 + number},
                "length": 2.0,
            }
            for number in range(1, 4)
        ]
        page = {
            "lines": [
                line(pdf2kicad.BODY_COLOR, 20.0, 10.0, 22.0, 10.0),
                line(pdf2kicad.PIN_COLOR, 18.0, 10.0, 20.0, 10.0),
                line(pdf2kicad.PIN_COLOR, 22.0, 10.0, 24.0, 10.0),
            ],
            "rectangles": [],
            "curves": [],
            "texts": [r383, u7],
            "decoded": {
                "components": [wrong_component],
                "wires": [],
            },
        }

        components, _consumed_texts, _consumed_lines = (
            pdf2kicad.decode_components(page)
        )
        by_reference = {item["reference"]: item for item in components}

        self.assertEqual(len(by_reference["U7"]["pins"]), 3)
        self.assertEqual(len(by_reference["R383"]["pins"]), 2)

    def test_dense_rotated_capacitor_values_are_paired_globally(self):
        def text(value, x, y, x1, y1):
            return {
                "text": value,
                "color": "#000000",
                "x": x,
                "y": y,
                "x1": x1,
                "y1": y1,
                "size": 1.15,
                "angle": 90,
            }

        c34_reference = text("C34", 147.349, 110.522, 148.624, 112.646)
        c35_reference = text("C35", 151.158, 110.522, 152.434, 112.646)
        c34_value = text(
            "0.1u/10V/0603/X7R",
            148.618,
            105.507,
            149.894,
            115.567,
        )
        c35_value = text(
            "0.47u/6.3V/0603",
            152.429,
            106.0,
            153.704,
            115.0,
        )
        components = [
            {"reference": "C34", "reference_text": c34_reference},
            {"reference": "C35", "reference_text": c35_reference},
        ]
        consumed = {
            pdf2kicad._text_key(c34_reference),
            pdf2kicad._text_key(c35_reference),
        }

        pdf2kicad.assign_values(
            {
                "texts": [
                    c35_value,
                    c34_reference,
                    c34_value,
                    c35_reference,
                ],
            },
            components,
            consumed,
        )

        self.assertEqual(components[0]["value"], c34_value["text"])
        self.assertEqual(components[1]["value"], c35_value["text"])
        self.assertIn(pdf2kicad._text_key(c34_value), consumed)
        self.assertIn(pdf2kicad._text_key(c35_value), consumed)

    def test_adjacent_capacitor_does_not_steal_previous_value(self):
        def text(value, x, y, x1, y1):
            return {
                "text": value,
                "color": "#000000",
                "x": x,
                "y": y,
                "x1": x1,
                "y1": y1,
                "size": 1.15,
                "angle": 90,
            }

        c6_reference = text("C6", 41.935, 31.782, 43.211, 33.273)
        c6_value = text(
            "0.1u/10V/0603/X7R",
            43.205,
            28.038,
            44.481,
            38.099,
        )
        c7_reference = text("C7", 43.205, 41.941, 44.481, 43.433)
        c7_value = text(
            "0.1u/10V/0603/X7R",
            44.476,
            39.468,
            45.751,
            49.529,
        )
        components = [
            {
                "reference": "C6",
                "reference_text": c6_reference,
                "bbox": {
                    "x0": 41.275,
                    "y0": 30.48,
                    "x1": 42.545,
                    "y1": 31.75,
                },
            },
            {
                "reference": "C7",
                "reference_text": c7_reference,
                "bbox": {
                    "x0": 41.275,
                    "y0": 41.91,
                    "x1": 42.545,
                    "y1": 43.18,
                },
            },
        ]
        consumed = {
            pdf2kicad._text_key(c6_reference),
            pdf2kicad._text_key(c7_reference),
        }

        pdf2kicad.assign_values(
            {
                "texts": [
                    c7_reference,
                    c6_value,
                    c6_reference,
                    c7_value,
                ],
            },
            components,
            consumed,
        )

        self.assertIs(components[0]["value_text"], c6_value)
        self.assertIs(components[1]["value_text"], c7_value)

    def test_stacked_capacitors_prefer_value_after_own_reference(self):
        c531_reference = self.semantic_text(
            "C531", 158.746, 57.218, 161.503, 58.493
        )
        c531_value = self.semantic_text(
            "22u/10V/1608 *DNP",
            158.746,
            58.488,
            169.076,
            59.764,
        )
        c535_reference = self.semantic_text(
            "C535", 158.746, 54.678, 161.503, 55.954
        )
        c535_value = self.semantic_text(
            "22u/10V/1608 *DNP",
            158.746,
            55.948,
            169.076,
            57.224,
        )
        components = [
            {
                "reference": "C531",
                "reference_text": c531_reference,
                "bbox": {
                    "x0": 156.845,
                    "y0": 57.15,
                    "x1": 158.115,
                    "y1": 58.42,
                },
            },
            {
                "reference": "C535",
                "reference_text": c535_reference,
                "bbox": {
                    "x0": 154.305,
                    "y0": 55.88,
                    "x1": 155.575,
                    "y1": 57.15,
                },
            },
        ]
        consumed = {
            pdf2kicad._text_key(c531_reference),
            pdf2kicad._text_key(c535_reference),
        }

        pdf2kicad.assign_values(
            {
                "texts": [
                    c531_reference,
                    c535_value,
                    c535_reference,
                    c531_value,
                ],
                "decoded": {"wires": []},
            },
            components,
            consumed,
        )

        self.assertIs(components[0]["value_text"], c531_value)
        self.assertIs(components[1]["value_text"], c535_value)

    def test_passive_value_does_not_consume_nearby_net_label(self):
        reference = {
            "text": "R61",
            "color": "#000000",
            "x": 20.0,
            "y": 10.0,
            "x1": 22.0,
            "y1": 11.0,
            "size": 1.0,
            "angle": 0,
        }
        net_label = {
            "text": "XSPI0_ECS0_N",
            "color": "#000000",
            "x": 20.0,
            "y": 11.0,
            "x1": 28.0,
            "y1": 12.0,
            "size": 1.0,
            "angle": 0,
        }
        value = {
            "text": "10K/0603",
            "color": "#000000",
            "x": 24.0,
            "y": 10.0,
            "x1": 29.0,
            "y1": 11.0,
            "size": 1.0,
            "angle": 0,
        }
        component_data = {
            "reference": "R61",
            "reference_text": reference,
            "bbox": {"x0": 21.0, "y0": 9.5, "x1": 24.0, "y1": 10.5},
        }
        consumed = {pdf2kicad._text_key(reference)}
        page = {
            "texts": [reference, net_label, value],
            "decoded": {
                "wires": [{
                    "start": {"x": 18.0, "y": 12.16},
                    "end": {"x": 30.0, "y": 12.16},
                }],
            },
        }

        pdf2kicad.assign_values(page, [component_data], consumed)

        self.assertEqual(component_data["value"], "10K/0603")
        self.assertNotIn(pdf2kicad._text_key(net_label), consumed)

    def test_symmetric_two_pin_parts_hide_pin_numbers(self):
        symmetric = {
            "reference": "R1",
            "value": "10k",
            "bbox": {"x0": 10.0, "y0": 9.0, "x1": 12.0, "y1": 11.0},
            "body_lines": [],
            "pins": [
                {
                    "number": "1",
                    "hot": {"x": 8.0, "y": 10.0},
                    "other": {"x": 10.0, "y": 10.0},
                    "length": 2.0,
                },
                {
                    "number": "2",
                    "hot": {"x": 14.0, "y": 10.0},
                    "other": {"x": 12.0, "y": 10.0},
                    "length": 2.0,
                },
            ],
        }
        asymmetric = copy.deepcopy(symmetric)
        asymmetric["reference"] = "SW1"
        asymmetric["pins"][1]["hot"]["y"] = 12.0
        asymmetric["pins"][1]["other"]["y"] = 12.0

        for reference in ("R1", "C1", "L1"):
            with self.subTest(reference=reference):
                symmetric["reference"] = reference
                hidden = pdf2kicad._symbol_definition(
                    pdf2kicad.UuidFactory(reference.encode()),
                    pdf2kicad.coordinate_transform("A4"),
                    symmetric,
                    f"pdf2kicad:{reference}",
                )
                self.assertIn("(pin_numbers hide)", hidden)
        visible = pdf2kicad._symbol_definition(
            pdf2kicad.UuidFactory(b"asymmetric"),
            pdf2kicad.coordinate_transform("A4"),
            asymmetric,
            "pdf2kicad:SW1",
        )

        self.assertNotIn("(pin_numbers hide)", visible)

    def test_one_pin_part_hides_pin_number(self):
        one_pin = component("TP1")
        rendered = pdf2kicad._symbol_definition(
            pdf2kicad.UuidFactory(b"one-pin"),
            pdf2kicad.coordinate_transform("A4"),
            one_pin,
            "pdf2kicad:TP1",
        )

        self.assertIn("(pin_numbers hide)", rendered)

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
        self.assertEqual(project["meta"]["version"], 3)
        self.assertNotIn(
            "page_layout_descr_file",
            project["schematic"],
        )

    def test_project_file_references_generated_worksheet(self):
        project = json.loads(
            pdf2kicad.project_file("example", "example.kicad_wks")
        )
        self.assertEqual(
            project["schematic"]["page_layout_descr_file"],
            "example.kicad_wks",
        )

    def test_root_sheets_have_complete_project_instances(self):
        root = pdf2kicad.render_root(
            pdf2kicad.UuidFactory(b"root"),
            "example",
            ["01_A.kicad_sch", "02_B.kicad_sch"],
            ["01_A", "02_B"],
            "root-uuid",
            ["sheet-a", "sheet-b"],
        )

        self.assertIn('(uuid "root-uuid")', root)
        self.assertIn('(uuid "sheet-a")', root)
        self.assertIn('(uuid "sheet-b")', root)
        self.assertEqual(root.count('(project "example"'), 2)
        self.assertEqual(root.count('(path "/root-uuid"'), 2)
        self.assertIn('(page "2")', root)
        self.assertIn('(page "3")', root)


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
            "example",
            "/root/sheet",
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
        self.assertIn('(project "example"', placement_a)
        self.assertIn('(path "/root/sheet"', placement_a)
        self.assertIn('(property "Reference" "U7"', placement_b)
        self.assertIn("\t\t(unit 2)\n", placement_b)


class PowerSymbolTests(unittest.TestCase):
    def test_supply_bar_is_consumed_and_becomes_native_power_symbol(self):
        power_text = {
            "text": "VDD_CORE",
            "color": "#000000",
            "x": 7.0,
            "y": 6.0,
            "x1": 13.0,
            "y1": 7.2,
            "size": 1.15,
            "angle": 0,
        }
        page = {
            "lines": [
                line("#000000", 10.0, 8.73, 10.0, 10.0),
                line("#000000", 9.365, 8.73, 10.635, 8.73),
            ],
            "texts": [power_text],
            "decoded": {
                "components": [{}],
                "worksheet": None,
            },
        }
        consumed_texts = set()
        consumed_lines = set()

        ports = pdf2kicad.decode_power_ports(
            page,
            [],
            consumed_texts,
            consumed_lines,
        )

        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0]["name"], "VDD_CORE")
        self.assertEqual(ports[0]["glyph"], "supply")
        self.assertEqual(ports[0]["angle"], 0)
        self.assertEqual(consumed_lines, {0, 1})
        self.assertEqual(
            consumed_texts,
            {pdf2kicad._text_key(power_text)},
        )

        ports[0]["reference"] = "#PWR0001"
        definition = pdf2kicad._power_symbol_definition(ports[0])
        placement = pdf2kicad._power_symbol_instance(
            pdf2kicad.UuidFactory(b"supply"),
            pdf2kicad.coordinate_transform("A4"),
            ports[0],
            "example",
            "/root/sheet",
        )
        self.assertIn('(symbol "power:VDD_CORE"', definition)
        self.assertIn("(power)", definition)
        self.assertIn("(pin power_in line", definition)
        self.assertIn('(lib_id "power:VDD_CORE")', placement)
        self.assertIn('(reference "#PWR0001")', placement)
        self.assertIn('(project "example"', placement)
        self.assertIn('(path "/root/sheet"', placement)
        self.assertNotIn("(global_label", placement)

        rendered = pdf2kicad.render_page(
            pdf2kicad.UuidFactory(b"supply-page"),
            {
                **page,
                "rectangles": [],
                "curves": [],
            },
            {
                "components": [],
                "wires": [],
                "power_ports": ports,
                "global_labels": [],
                "local_labels": [],
                "consumed_texts": consumed_texts,
                "semantic_lines": consumed_lines,
                "semantic_rectangles": set(),
                "semantic_curves": set(),
                "worksheet": None,
            },
            pdf2kicad.coordinate_transform("A4"),
            "Power symbol",
            True,
        )
        self.assertNotIn("(global_label", rendered)
        self.assertNotIn('(text "VDD_CORE"', rendered)
        self.assertNotIn("(color 0 0 0 1)", rendered)

    def test_ground_triangle_is_consumed_without_a_wire(self):
        page = {
            "lines": [
                line("#000000", 9.0, 10.0, 11.0, 10.0),
                line("#000000", 11.0, 10.0, 10.0, 11.0),
                line("#000000", 10.0, 11.0, 9.0, 10.0),
            ],
            "texts": [],
            "decoded": {
                "components": [{}],
                "worksheet": None,
            },
        }
        consumed_lines = set()

        ports = pdf2kicad.decode_power_ports(
            page,
            [],
            set(),
            consumed_lines,
        )

        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0]["name"], "GND")
        self.assertEqual(ports[0]["glyph"], "ground")
        self.assertEqual(ports[0]["point"], (10.0, 10.0))
        self.assertEqual(ports[0]["angle"], 0)
        self.assertEqual(consumed_lines, {0, 1, 2})

    def test_named_ground_uses_ground_net_text(self):
        ground_text = {
            "text": "ADAVSS",
            "color": "#000000",
            "x": 7.5,
            "y": 11.1,
            "x1": 12.5,
            "y1": 12.1,
            "size": 1.15,
            "angle": 0,
        }
        page = {
            "lines": [
                line("#000000", 9.0, 10.0, 11.0, 10.0),
                line("#000000", 11.0, 10.0, 10.0, 11.0),
                line("#000000", 10.0, 11.0, 9.0, 10.0),
            ],
            "texts": [ground_text],
            "decoded": {
                "components": [{}],
                "worksheet": None,
            },
        }
        consumed_texts = set()

        ports = pdf2kicad.decode_power_ports(
            page,
            [],
            consumed_texts,
            set(),
        )

        self.assertEqual(ports[0]["name"], "ADAVSS")
        self.assertEqual(ports[0]["glyph"], "ground")
        self.assertEqual(
            consumed_texts,
            {pdf2kicad._text_key(ground_text)},
        )

    def test_worksheet_coordinate_triangle_is_not_ground(self):
        page = {
            "lines": [
                line("#000000", 9.0, 10.0, 11.0, 10.0),
                line("#000000", 11.0, 10.0, 10.0, 11.0),
                line("#000000", 10.0, 11.0, 9.0, 10.0),
            ],
            "texts": [],
            "decoded": {
                "components": [{}],
                "worksheet": {"line_indexes": [0, 1, 2]},
            },
        }

        self.assertEqual(
            pdf2kicad.decode_power_ports(page, [], set(), set()),
            [],
        )


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
            [],
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
            [],
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
                        [],
                        [],
                        consumed_texts,
                        consumed_lines,
                    )

                self.assertEqual(labels[0]["angle"], expected_angle)
                self.assertEqual(labels[0]["direction"], direction)
                self.assertEqual(consumed_lines, {2, 3})
                self.assertEqual(len(consumed_texts), 1)

    def test_text_side_selects_opposite_glyph_extreme_as_hotpoint(self):
        label_text = {
            "text": "PWEN1",
            "color": "#ff0000",
            "x": 10.0,
            "y": 9.0,
            "x1": 15.0,
            "y1": 11.0,
            "size": 2.0,
            "angle": 0,
        }
        page = {
            "lines": [
                line("#000000", 8.0, 10.0, 9.0, 9.0),
                line("#000000", 8.0, 10.0, 9.0, 11.0),
                line("#000000", 9.0, 9.0, 10.0, 10.0),
                line("#000000", 9.0, 11.0, 10.0, 10.0),
            ],
            "texts": [label_text],
            "decoded": {"components": [{}]},
        }
        wires = [{
            "start": {"x": 5.0, "y": 10.0},
            "end": {"x": 8.0, "y": 10.0},
        }]
        decoded_label = {
            "name": "PWEN1",
            # The selected chevron points left even though the source text,
            # and therefore the native label body, is on the right.
            "direction": "left",
            "apex": {"x": 9.0, "y": 10.0},
            "base": {"x": 10.0, "y": 10.0},
            "hotpoint": {"x": 10.0, "y": 10.0},
            "line_indexes": [0, 1, 2, 3],
            "text": label_text,
        }

        with mock.patch.object(
            pdf2kicad.pdf_dump,
            "decode_global_labels",
            return_value=[decoded_label],
        ):
            labels = pdf2kicad.decode_global_labels(
                page,
                wires,
                [],
                [],
                set(),
                set(),
            )

        self.assertEqual(labels[0]["point"], (8.0, 10.0))
        self.assertEqual(labels[0]["direction"], "right")
        self.assertEqual(labels[0]["angle"], 0)

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


class LocalLabelTests(unittest.TestCase):
    def test_hash_suffix_is_valid_in_a_local_label(self):
        label = {
            "text": "PMIC_INT#",
            "x": 10.0,
            "y": 8.5,
            "x1": 15.0,
            "y1": 9.5,
            "size": 1.0,
            "angle": 0,
            "color": "#000000",
        }
        wire = {
            "start": {"x": 9.0, "y": 9.66},
            "end": {"x": 18.0, "y": 9.66},
        }

        labels = pdf2kicad.decode_local_labels(
            {"texts": [label]},
            [wire],
            set(),
        )

        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["name"], "PMIC_INT#")

    def test_baseline_wire_wins_over_nearby_diagonal_bus_entry(self):
        label = {
            "text": "DDR0_DQB15",
            "x": 138.68,
            "y": 42.951,
            "x1": 146.26,
            "y1": 44.227,
            "size": 1.15,
            "angle": 0,
            "color": "#000000",
        }
        horizontal_wire = {
            "start": {"x": 134.62, "y": 44.45},
            "end": {"x": 146.05, "y": 44.45},
        }
        diagonal_entry = {
            "start": {"x": 147.32, "y": 43.18},
            "end": {"x": 146.05, "y": 44.45},
        }

        labels = pdf2kicad.decode_local_labels(
            {"texts": [label]},
            [diagonal_entry, horizontal_wire],
            set(),
        )

        self.assertEqual(len(labels), 1)
        self.assertAlmostEqual(labels[0]["point"][0], 138.4293)
        self.assertEqual(labels[0]["point"][1], 44.45)


class JunctionTests(unittest.TestCase):
    @staticmethod
    def quarter(x0, y0, x1, y1):
        return {
            "points": [[x0, y0], [x1, y0], [x1, y1]],
            "color": "#ff0000",
            "fill": "#ff0000",
            "width": 0,
        }

    def test_red_pdf_dot_becomes_native_junction(self):
        page = {
            "curves": [
                self.quarter(9.75, 9.75, 10.0, 10.0),
                self.quarter(10.0, 9.75, 10.25, 10.0),
                self.quarter(9.75, 10.0, 10.0, 10.25),
                self.quarter(10.0, 10.0, 10.25, 10.25),
            ],
        }
        wires = [
            {
                "start": {"x": 5.0, "y": 10.0},
                "end": {"x": 10.0, "y": 10.0},
            },
            {
                "start": {"x": 10.0, "y": 10.0},
                "end": {"x": 15.0, "y": 10.0},
            },
            {
                "start": {"x": 10.0, "y": 10.0},
                "end": {"x": 10.0, "y": 15.0},
            },
        ]
        consumed_curves = set()

        junctions = pdf2kicad.decode_junctions(
            page,
            wires,
            consumed_curves,
        )

        self.assertEqual(junctions, [(10.0, 10.0)])
        self.assertEqual(consumed_curves, {0, 1, 2, 3})
        rendered = pdf2kicad._junction(
            pdf2kicad.UuidFactory(b"junction"),
            pdf2kicad.coordinate_transform("A4"),
            junctions[0],
        )
        self.assertIn("(junction", rendered)
        self.assertIn("(at 7.46 7.46)", rendered)
        self.assertIn("(diameter 0)", rendered)

    def test_red_circle_without_three_wire_branches_stays_graphic(self):
        page = {
            "curves": [
                self.quarter(9.75, 9.75, 10.0, 10.0),
                self.quarter(10.0, 9.75, 10.25, 10.0),
                self.quarter(9.75, 10.0, 10.0, 10.25),
                self.quarter(10.0, 10.0, 10.25, 10.25),
            ],
        }
        wires = [{
            "start": {"x": 5.0, "y": 10.0},
            "end": {"x": 15.0, "y": 10.0},
        }]
        consumed_curves = set()

        self.assertEqual(
            pdf2kicad.decode_junctions(page, wires, consumed_curves),
            [],
        )
        self.assertEqual(consumed_curves, set())


class BusTests(unittest.TestCase):
    def test_blue_capture_segment_becomes_native_bus(self):
        buses = pdf2kicad.decode_buses({
            "lines": [
                line("#0000ff", 5.0, 10.0, 15.0, 10.0),
                line("#4200ff", 5.0, 12.0, 15.0, 12.0),
            ],
        })

        self.assertEqual(
            buses,
            [{
                "start": {"x": 5.0, "y": 10.0},
                "end": {"x": 15.0, "y": 10.0},
            }],
        )
        rendered = pdf2kicad._bus(
            pdf2kicad.UuidFactory(b"bus"),
            pdf2kicad.coordinate_transform("A4"),
            buses[0],
        )
        self.assertIn("(bus", rendered)
        self.assertIn("(xy 2.46 7.46)", rendered)
        self.assertIn("(xy 12.46 7.46)", rendered)

    def test_diagonal_wire_touching_bus_becomes_native_entry(self):
        buses = [{
            "start": {"x": 15.0, "y": 5.0},
            "end": {"x": 15.0, "y": 15.0},
        }]
        diagonal = {
            "start": {"x": 15.0, "y": 10.0},
            "end": {"x": 14.0, "y": 11.0},
        }
        horizontal = {
            "start": {"x": 14.0, "y": 11.0},
            "end": {"x": 8.0, "y": 11.0},
        }

        wires, entries = pdf2kicad.decode_bus_entries(
            [diagonal, horizontal],
            buses,
        )

        self.assertEqual(wires, [horizontal])
        self.assertEqual(
            entries,
            [{
                "start": {"x": 14.0, "y": 11.0},
                "end": {"x": 15.0, "y": 10.0},
            }],
        )
        rendered = pdf2kicad._bus_entry(
            pdf2kicad.UuidFactory(b"bus-entry"),
            pdf2kicad.coordinate_transform("A4"),
            entries[0],
        )
        self.assertIn("(bus_entry", rendered)
        self.assertIn("(at 11.46 8.46)", rendered)
        self.assertIn("(size 1.00 -1.00)", rendered)

    def test_bus_range_text_does_not_become_wire_label(self):
        buses = [{
            "start": {"x": 15.0, "y": 5.0},
            "end": {"x": 15.0, "y": 15.0},
        }]
        diagonal = {
            "start": {"x": 15.0, "y": 10.0},
            "end": {"x": 14.0, "y": 11.0},
        }
        bus_text = {
            "text": "DDR0_CAB[5..0]",
            "x": 14.718,
            "y": 9.34,
            "x1": 17.0,
            "y1": 10.34,
            "size": 1.0,
            "angle": 0,
            "color": "#000000",
        }

        self.assertEqual(
            len(pdf2kicad.decode_local_labels(
                {"texts": [bus_text]},
                [diagonal],
                set(),
            )),
            1,
        )
        wires, _entries = pdf2kicad.decode_bus_entries(
            [diagonal],
            buses,
        )
        labels = pdf2kicad.decode_local_labels(
            {"texts": [bus_text]},
            wires,
            set(),
        )

        self.assertEqual(labels, [])


class NoConnectTests(unittest.TestCase):
    def test_brown_cross_at_pin_becomes_native_no_connect(self):
        page = {
            "lines": [
                line(pdf2kicad.NO_CONNECT_COLOR, 7.6, 9.6, 8.4, 10.4),
                line(pdf2kicad.NO_CONNECT_COLOR, 8.4, 9.6, 7.6, 10.4),
            ],
        }
        part = component("U1")
        part["pins"][0]["hot"] = {"x": 8.0, "y": 10.0}
        part["pins"][0]["name"] = "NC"
        consumed_lines = set()

        no_connects = pdf2kicad.decode_no_connects(
            page,
            [part],
            consumed_lines,
        )

        self.assertEqual(no_connects, [(8.0, 10.0)])
        self.assertEqual(consumed_lines, {0, 1})
        rendered = pdf2kicad._no_connect(
            pdf2kicad.UuidFactory(b"no-connect"),
            pdf2kicad.coordinate_transform("A4"),
            no_connects[0],
        )
        self.assertIn("(no_connect", rendered)
        self.assertIn("(at 5.46 7.46)", rendered)
        symbol = pdf2kicad._symbol_definition(
            pdf2kicad.UuidFactory(b"nc-pin"),
            pdf2kicad.coordinate_transform("A4"),
            part,
            "pdf2kicad:U1",
        )
        self.assertIn("(pin passive line", symbol)
        self.assertNotIn("(pin no_connect line", symbol)


class TextRenderingTests(unittest.TestCase):
    def test_pdf_outline_font_is_scaled_and_style_is_preserved(self):
        rendered = pdf2kicad._graphic_text(
            pdf2kicad.UuidFactory(b"styled-text"),
            pdf2kicad.coordinate_transform("A4"),
            {
                "text": "Heading",
                "x": 10.0,
                "y": 10.0,
                "x1": 30.0,
                "y1": 14.0,
                "size": 2.0,
                "angle": 0,
                "color": "#008000",
                "bold": True,
                "italic": True,
            },
        )

        self.assertIn('(face "Arial")', rendered)
        self.assertIn("(size 1.43 1.43)", rendered)
        self.assertIn("(bold yes)", rendered)
        self.assertIn("(italic yes)", rendered)

    def test_component_fields_use_compensated_outline_font(self):
        source_component = component("R1")
        source_component["reference_text"] = {
            "x": 10.0,
            "y": 7.0,
            "x1": 12.0,
            "y1": 8.15,
            "size": 1.15,
            "angle": 0,
            "bold": True,
        }
        source_component["value_text"] = {
            "x": 10.0,
            "y": 17.0,
            "x1": 17.0,
            "y1": 18.15,
            "size": 1.15,
            "angle": 0,
        }

        rendered = pdf2kicad._component_instance(
            pdf2kicad.UuidFactory(b"component-text"),
            pdf2kicad.coordinate_transform("A2"),
            source_component,
            "pdf2kicad:R",
        )

        self.assertEqual(rendered.count('(face "Arial")'), 2)
        self.assertEqual(rendered.count("(size 1.64 1.64)"), 2)
        self.assertEqual(rendered.count("(bold yes)"), 1)


if __name__ == "__main__":
    unittest.main()
