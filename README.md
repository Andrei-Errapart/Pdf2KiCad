# pdf2kicad

`pdf2kicad` reconstructs an editable KiCad schematic project from an
OrCAD/Capture- or Altium Designer-generated PDF. It builds on
`scripts/pdf_dump.py` and recovers the PDF's colored schematic geometry as
KiCad wires and buses, native junctions, generic symbols and pins,
local/global labels, power ports, and sequential multi-unit designators.

PDF is a presentation format, not a schematic database. Library identities,
hidden fields, multi-unit relationships, electrical pin types, and some pin
names may not be present in the file. The converter therefore creates
deterministic, project-local generic symbols. By default it also retains
unconsumed PDF vectors and text as KiCad graphics, so tables, notes, and
unsupported glyphs remain visible. A detected source worksheet is the
exception: its PDF frame and corner block are replaced by a project-local copy
of KiCad's standard worksheet—coordinate divisions, title block, fields, and
styling included—with zero margins so its frame follows the paper edge. The
source title, company, date, revision, document number, page name, and source
page count are parsed when present and transferred to that title block.

Multi-unit components are inferred project-wide when designators with one
letter-prefixed base (`U1`, `D3`, `J1`, ...) have an uninterrupted suffix
sequence beginning with `A`, for example `U1A`, `U1B`, and `U1C`. They are
emitted as units 1, 2, and 3 of the shared KiCad reference `U1`. Gapped,
duplicated, or otherwise ambiguous sequences remain separate components; a
bare same-base component with pins also vetoes the merge.

OrCAD off-page ports become native KiCad global labels. Their connector
direction controls the KiCad orientation so the label body extends away from
and attaches to its wire or bus, and the consumed PDF text/chevron is omitted
from residual graphics. Compact filled connection dots become native KiCad
junctions. Recovered PDF text uses its source bold/italic style and an
Arial-compatible outline font with KiCad's font-size compensation.
Local net labels attach through their Capture text-baseline anchor, avoiding
nearby bus-entry diagonals in dense breakouts. Reference/value fields are
paired globally across compact component banks, and symmetric opposed two-pin
symbols omit their generated pin numbers. Diagonal bus breakouts become native
KiCad bus entries, and Capture no-connect crosses become native KiCad
no-connect markers. Disconnected passive strokes, rectangles, and curves are
absorbed into their symbols; one-pin symbols also hide generated pin numbers.
Adjacent reference/value fields coalesced into one PDF text span are split
before recovery, values printed directly below rectangular IC bodies are
associated with those bodies, and connector pin labels that resemble
designators remain pins rather than becoming bodyless components. Segmented
round outlines used by ground points and testpoints are absorbed into their
symbols instead of being emitted as standalone graphics.

## Altium Designer PDFs

Altium PDFs (llPDFLib producer) use a different drawing language, detected
automatically from the PDF metadata or forced with `--flavor altium`.
Normalization maps the Altium conventions onto the OrCAD decode pipeline:
navy wires, black axis-aligned pin strokes, pale-yellow body fills, blue
symbol art, navy or blue-gray junction dots, red no-ERC crosses, and maroon
net labels and power-port glyphs (bar, circle, and earth styles). Colors a
PDF/A re-encoder has moved a few steps off this palette (for example
`#000080` to `#000081`) are snapped back onto it. Fully transparent marker
text is dropped, coalesced designator/value spans are split, and
coordinates are snapped to a 0.05 mm grid to absorb the generator's float
noise; coordinates within 0.02 mm of each other snap to the same grid point.
A net label attaches to the wire under its baseline. Power ports drawn edge
to edge, such as GND bars on adjacent pins, each become their own port, and
port names are shared out nearest first; a text whose baseline lies on a
wire is a net label, never a port name. Because such projects use flat
net-identifier scope, every net label becomes a KiCad global label. Titles,
page numbers,
and sheet names are read from the sheet-template title block; the template
itself stays residual graphics. Not-fitted variant parts print dimmed gray
and are recovered with KiCad's native do-not-populate state. Multi-channel
designs can print one logical designator on several pages; duplicates are
suffixed (`R13_2`) to keep the flat project annotation-unique. Buses, bus
entries, and Altium ports (none appear in the validation design) are not yet
recovered; per-object color overrides fall back to residual graphics.

Values ending in `*DNP` are cleaned and emitted with KiCad's native
do-not-populate state and BOM exclusion. Optional passive enrichment can infer
generic KiCad SMD footprints from delimited imperial or metric package codes
such as `0603` or `1608`. A separate rendering option replaces recovered
two-terminal resistor, capacitor, inductor, and ferrite-bead bodies with the
standard `Device:R`, `Device:C`, `Device:L`, and `Device:FerriteBead` symbols.
Each required library definition is embedded once per sheet and shared by all
matching instances. Four-pin filters, nonstandard pin-number pairs, and other
multi-terminal magnetics are deliberately left unchanged.

## Usage

```sh
scripts/pdf2kicad input.pdf [output_directory]
```

The wrapper creates a cached virtual environment and installs PyMuPDF when
needed. Set `PDF2KICAD_VENV` to use an existing virtual environment.

Useful options:

```text
--paper auto|A0|A1|A2|A3|A4  select the source Capture sheet size
--flavor auto|orcad|altium     select the authoring EDA tool (auto reads the
                               PDF metadata)
--no-graphics                  omit the residual PDF vector/text trace
--infer-footprints             infer SMD footprints for two-terminal passives
--kicad-rcl                    use standard KiCad Device R/C/L/ferrite symbols
--summary-json                 print machine-readable recovery counts
```

Automatic paper detection reads each page's title block, so mixed-size PDFs
are supported. The output directory contains a root `.kicad_sch`, one child
schematic per PDF page, and a `.kicad_pro` file. When the PDF contains a
worksheet, the output also contains a project-local `.kicad_wks`; PDFs without
a detected worksheet continue to use KiCad's configured drawing sheet. Child
sheet filenames use
the source page number and a reconstructed, sanitized page heading, matching
the `dsn2kicad.hs` convention (for example,
`03_Clock_Sys_Config_PWR_on_cnt.kicad_sch`). An object-free first page is
named `01_NOTE.kicad_sch`.

The converter refuses input it cannot do anything useful with, printing a
one-line reason and exiting with status 1: a file that is not a readable PDF,
a password-protected PDF, a PDF with no vector drawings on any page (a scan),
and a PDF with a page of more than 12,000 drawing objects. The last is how
PCB layout plots and placement maps are told from schematic sheets — the
densest schematic page in the validation corpus has 5,321 — and it replaces
a decode that would otherwise run for many minutes.

## Web converter

The same converter runs in a browser at
<https://andrei-errapart.github.io/Pdf2KiCad/>: drop a schematic PDF, or a
ZIP archive containing exactly one, look at what was recovered from each
page and at a preview of the sheets, and download the KiCad project as a
ZIP. The conversion happens in the browser tab and the file is not uploaded
anywhere; the site is static and has no server-side component.

The page does not contain a second implementation. It runs
`scripts/pdf2kicad.py` and `scripts/pdf_dump.py` themselves under
[Pyodide](https://pyodide.org), with PyMuPDF's WebAssembly build, and a test
holds its output byte-identical to the native converter's. Expect a first
visit to download about 24 MB, and a conversion to take roughly twice as
long as on the command line. Files up to 32 MiB are accepted.

`web/` is the site as served. Its own files are plain HTML and ES modules
with no build step; two things are assembled beside them by
`scripts/build-web`, which CI also runs:

```sh
scripts/build-web                      # fills web/runtime/ and web/py/
python3 -m http.server -d web 8000     # then open http://localhost:8000/
```

`web/runtime/` receives Pyodide and the PyMuPDF wheel, downloaded at pinned
versions and checked against SHA-256 hashes recorded in the script;
`web/py/` receives copies of the Python sources. Neither is committed. Run
the script again after changing anything under `scripts/`.
`.github/workflows/pages.yml` runs all the tests and redeploys the site on
every push to `main`. Third-party code and its licences are recorded in
`web/vendor/README.md`, and the design in
`doc/specs/2026-10-03-web-converter-design.md`.

## Validation

The converter is exercised against the PDFs in the parent corpus cases
`tests/0001`, `tests/0002`, and `tests/0003`. Their DSN files are converted
with `orcad2kicad/scripts/dsn2kicad`, and KiCad-exported netlists are compared
at reference-connectivity level. The two compact fixtures reproduce that
connectivity exactly; the larger multi-unit design is a recovery test because
some topology exists only in the DSN.

Run the self-contained unit tests with:

```sh
python3 -m unittest discover -s tests -v
node --test tests/web/*.test.mjs
```

The first also runs `tests/test_web_parity.py`, which converts a synthetic
schematic natively and through the web converter's runner under Node and
requires identical output; the second covers the page's own modules. The
tests that need `web/runtime/` are skipped until `scripts/build-web` has
been run, and fail instead when `PDF2KICAD_REQUIRE_WEB=1` is set, as it is in
CI.

For corpus comparisons, export both generated schematics with
`kicad-cli sch export netlist` before comparing connectivity.

## License

`pdf2kicad` is licensed under **GPL-2.0-or-later** — see [LICENSE](LICENSE).
This is the same license as the sibling `orcad2kicad` converter and is
compatible with KiCad's own GPL-3.0-or-later application license, so the code
can be reused or upstreamed within the KiCad ecosystem. Every source file
carries an `SPDX-License-Identifier: GPL-2.0-or-later` header.

The only runtime dependency, PyMuPDF, is installed by the wrapper rather than
bundled in this repository; it is dual-licensed under AGPL-3.0-or-later or an
Artifex commercial license. The "or later" in this project's own license is
what keeps a combined distribution possible — a recipient may take
`pdf2kicad` under GPL-3.0, whose section 13 permits combination with AGPL-3.0
code. Anyone redistributing the two together must still satisfy PyMuPDF's
terms, or hold a commercial license for it.

The web converter is such a combined distribution: the site serves PyMuPDF's
WebAssembly wheel, which contains MuPDF, beside this project's code. It does
so under the AGPL, and the page footer links the source of both at the
released versions it serves. The other third-party components of the site
are listed with their licences in `web/vendor/README.md`.
