# Data Sources

This project is based on publicly available and fully synthetic data only;
the rule is the Data Policy in [AGENTS.md](AGENTS.md). This file lists every
public document whose data appears anywhere in the repository — tests,
fixtures, documentation, code comments, commit messages or beads — and where
anyone can download it. A public source is added here before any data from
it is used.

## Synthetic data

Everything not attributed below is invented:

- the unit-test fixtures in `tests/test_pdf2kicad.py`, written by hand;
- the OrCAD-style and Altium-style schematic PDFs that
  `tests/synthetic_schematics.py` draws and
  `tests/test_synthetic_schematics.py` converts end to end
  (`python3 tests/synthetic_schematics.py OUT_DIR` writes them with their
  reference netlists);
- the small schematic that `tests/test_web_parity.py` draws, and the web
  test fixtures that `tests/web/make_fixtures.py` writes to
  `tests/web/fixtures/`.

## Public schematics

### TE0725LP FPGA module, Trenz Electronic

- Product page, "Documents" tab:
  <https://www.trenz-electronic.de/FPGA-Modul-mit-AMD-Artix-7A100T-2I-2-x-50-Pin-Header-1-8V-Versorgung/TE0725LP-01-72I-1T?path=Trenz_Electronic/Modules_and_Module_Carriers/3.5x7.3/TE0725LP/REV02/Documents>
- Schematics of every variant, per revision:
  `https://www.trenz-electronic.de/trenzdownloads/Trenz_Electronic/Modules_and_Module_Carriers/3.5x7.3/TE0725LP/REV01/Documents/`
  (for example `SCH-TE0725LP-01-100-2C.PDF`, `SCH-TE0725LP-01-100-2D.PDF`,
  `SCH-TE0725LP-01-100-2L.PDF`) and the same under `REV02/Documents/`. The
  TE0725LP-01-72I-1T module's is
  <https://www.trenz-electronic.de/trenzdownloads/Trenz_Electronic/Modules_and_Module_Carriers/3.5x7.3/TE0725LP/REV01/Documents/AD-TE0725LP-01-72I-1T.PDF>.
- File used: `SCH-TE0725LP-02-72I-AT.PDF` (REV02, variant 72I-AT; Altium
  Designer, re-encoded by a PDF/A converter), SHA-256
  `f7923a9aacd78947f5af2b2d4e81b345d92292019b75cbcb1783126777a1a382`.
  On 2026-11-28 the download at
  <https://www.trenz-electronic.de/trenzdownloads/Trenz_Electronic/Modules_and_Module_Carriers/3.5x7.3/TE0725LP/REV02/Documents/SCH-TE0725LP-02-72I-AT.PDF>
  was byte-identical to the copy the fixtures were drawn from.
- Used in: the `tests/test_pdf2kicad.py` fixtures whose comments name
  TE0725LP (the V_P/V_N label stack, the GND bars on U2 A0–A2, the U1 pin P4
  grid-midpoint snap) and the PDF/A palette tests; the commit "pdf2kicad:
  recover Altium PDFs re-encoded to PDF/A"; beads Pdf2KiCad-q7x, -3rk, -hec
  and -01x; the drawing counts quoted in `README.md`.

### TE0725 FPGA module, Trenz Electronic

- File used: `SCH-TE0725-04-72C-1-A.PDF` (REV04, variant 72C-1-A),
  <https://www.trenz-electronic.de/trenzdownloads/Trenz_Electronic/Modules_and_Module_Carriers/3.5x7.3/TE0725/REV04/Documents/SCH-TE0725-04-72C-1-A.PDF>.
- Used in: beads Pdf2KiCad-90z and -01x (the not-fitted R23/R24 on page 9)
  and -9i6 (the J3 jumper, D1.1/D4.2 and RD_C_P notes).

### WeAct STM32G474 core board V10, WeAct Studio

- Repository: <https://github.com/WeActStudio/WeActStudio.STM32G474CoreBoard>
- File used: `Hardware/QFN48/WeAct-STM32G474CoreBoard_V10_SchDoc.pdf`
  (Altium Designer, llPDFLib), git blob
  `09117c1a0ed8dcabf0b17a1ba6e48c9568a6d7e8`, SHA-256
  `865d54528686365fcf76291796ba599464dcb8f6431815bac40b5608878a3a9f`.
- Used in: the commits "pdf2kicad: recover Altium PDFs re-encoded to
  PDF/A" and "pdf_dump: read pin numbers printed inside the body on the
  pin's row" (headers P1, P2 and P3); bead Pdf2KiCad-4ic (dual-row header
  numbering); the drawing counts quoted in `README.md`.

### ESP32-C3 0.42" OLED board, 01Space

- Repository: <https://github.com/01Space/ESP32-C3-0.42LCD>
- File used: `Schematic/ESP32-C3-0.42OED Schematic.pdf`,
  <https://github.com/01Space/ESP32-C3-0.42LCD/blob/main/Schematic/ESP32-C3-0.42OED%20Schematic.pdf>,
  git blob `dffa791bb873d0c6420986f44ecd2ff5ae1327ae`, SHA-256
  `7be47a67c62b214e4954681fcfc195ad275532ba8e831e67387c4b3c17de1203`.
- Used in: the commit "pdf2kicad: recover Altium PDFs re-encoded to PDF/A";
  the drawing counts quoted in `README.md`.

### ESP32-C3 ABrobot OLED board, zhuhai-esp

- Repository: <https://github.com/zhuhai-esp/ESP32-C3-ABrobot-OLED>
- File used: `Document/ESP32C3 OLED原理图.pdf` (Altium Designer, llPDFLib;
  kept locally as `esp32-c3-abrobot-oled_schematic.pdf`), git blob
  `4abb5b316d6282475c3d2213472d152d853d1ebe`, SHA-256
  `b9c3eab4e10bbf386855f358b2fa5ed137b8896fa8153f56c5c5375dc1d73317`.
- Used in: the commits "pdf2kicad: recover Altium PDFs re-encoded to
  PDF/A" and "pdf_dump: read pin numbers printed inside the body on the
  pin's row" (header JP1); the drawing counts quoted in `README.md`.
