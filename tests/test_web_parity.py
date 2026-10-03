#!/usr/bin/env python3
# Copyright (C) 2026 Andrei Errapart
# SPDX-License-Identifier: GPL-2.0-or-later
"""The web converter must produce byte-identical output to the native one.

The page runs scripts/pdf2kicad.py under Pyodide, with PyMuPDF built for
WebAssembly.  A small schematic-like PDF is drawn here in memory, converted
natively and through web/runner.js -- the code the page runs in its worker --
under Node, and the two results must match file for file and byte for byte.

Needs Node and the runtime that scripts/build-web puts in web/runtime and
web/py.  Skipped when either is absent, so the plain unit suite still
passes; PDF2KICAD_REQUIRE_WEB=1 (set in CI) turns the skip into a failure.
The comparison is exact only when the native PyMuPDF is the release pinned
in scripts/build-web.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fitz  # PyMuPDF

REPO_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_DIR / "scripts"
WEB_DIR = REPO_DIR / "web"
RUN_CONVERTER = REPO_DIR / "tests" / "web" / "run_converter.mjs"
sys.path.insert(0, str(SCRIPTS))

import pdf2kicad  # noqa: E402
import pdf_dump  # noqa: E402

MM = 72 / 25.4


def _rgb(color: str) -> tuple[float, float, float]:
    return tuple(int(color[index:index + 2], 16) / 255 for index in (1, 3, 5))


def schematic_pdf_bytes() -> bytes:
    """Two A4 sheets, each with a resistor, three wires and a net label."""
    doc = fitz.open()

    def stroke(page, color, x1, y1, x2, y2):
        page.draw_line(
            (x1 * MM, y1 * MM), (x2 * MM, y2 * MM),
            color=_rgb(color), width=0.15 * MM,
        )

    def text(page, value, x, y, size=1.8):
        page.insert_text((x * MM, y * MM), value, fontsize=size * MM)

    for number in (1, 2):
        page = doc.new_page(width=297 * MM, height=210 * MM)
        stroke(page, pdf_dump.PIN_COLOR, 50, 60, 55, 60)
        stroke(page, pdf_dump.PIN_COLOR, 65, 60, 70, 60)
        body = [(55, 58.5), (65, 58.5), (65, 61.5), (55, 61.5), (55, 58.5)]
        for start, end in zip(body, body[1:]):
            stroke(page, pdf_dump.BODY_COLOR, *start, *end)
        text(page, f"R{number}", 57, 57)
        text(page, "10K", 57, 65)
        stroke(page, pdf_dump.WIRE_COLOR, 30, 60, 50, 60)
        stroke(page, pdf_dump.WIRE_COLOR, 70, 60, 90, 60)
        stroke(page, pdf_dump.WIRE_COLOR, 90, 60, 90, 80)
        text(page, "NET_A", 32, 59.3, size=1.5)
        text(page, "A4", 270, 200)
    return doc.tobytes(no_new_id=True)


class WebParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = []
        if shutil.which("node") is None:
            missing.append("node is not on PATH")
        if not (WEB_DIR / "runtime" / "runtime.json").is_file():
            missing.append("web/runtime is not built (run scripts/build-web)")
        if missing:
            reason = "; ".join(missing)
            if os.environ.get("PDF2KICAD_REQUIRE_WEB") == "1":
                raise AssertionError(f"PDF2KICAD_REQUIRE_WEB=1 but {reason}")
            raise unittest.SkipTest(reason)
        cls.pdf_bytes = schematic_pdf_bytes()

    def convert_in_node(self, flags):
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "parity.pdf"
            pdf.write_bytes(self.pdf_bytes)
            out_dir = Path(directory) / "out"
            result = subprocess.run(
                ["node", str(RUN_CONVERTER), str(pdf), str(out_dir), *flags],
                capture_output=True, text=True, timeout=300,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return {
                path.name: path.read_bytes()
                for path in sorted(out_dir.iterdir())
            }

    def assert_parity(self, flags, **options):
        native, summary = pdf2kicad.convert_pdf_bytes(
            self.pdf_bytes, "parity.pdf", **options
        )
        web = self.convert_in_node(flags)
        self.assertEqual(sorted(web), sorted(native))
        for name, text in native.items():
            self.assertEqual(
                web[name], text.encode("utf-8"), f"{name} differs"
            )
        return summary

    def test_web_copy_of_the_converter_is_current(self):
        for name in ("pdf2kicad.py", "pdf_dump.py"):
            self.assertEqual(
                (WEB_DIR / "py" / name).read_bytes(),
                (SCRIPTS / name).read_bytes(),
                f"web/py/{name} is stale; run scripts/build-web",
            )

    def test_the_fixture_exercises_semantic_recovery(self):
        _files, summary = pdf2kicad.convert_pdf_bytes(
            self.pdf_bytes, "parity.pdf", use_kicad_rcl=True
        )
        for page in summary["pages"]:
            self.assertEqual(page["components"], 1)
            self.assertEqual(page["pins"], 2)
            self.assertEqual(page["wires"], 3)
            self.assertEqual(page["local_labels"], 1)
            self.assertEqual(page["standardized_passives"], 1)

    def test_default_options(self):
        self.assert_parity([])

    def test_kicad_passives_and_inferred_footprints(self):
        self.assert_parity(
            ["--kicad-rcl", "--infer-footprints"],
            use_kicad_rcl=True,
            infer_footprints=True,
        )

    def test_no_graphics_with_forced_paper_and_flavor(self):
        self.assert_parity(
            ["--no-graphics", "--paper=A3", "--flavor=altium"],
            keep_graphics=False,
            paper="A3",
            flavor="altium",
        )


if __name__ == "__main__":
    unittest.main()
