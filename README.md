# pdf2kicad

`pdf2kicad` reconstructs an editable KiCad schematic project from an
OrCAD/Capture-generated PDF. It builds on `scripts/pdf_dump.py` and recovers
the PDF's colored schematic geometry as KiCad wires and buses, native
junctions, generic symbols and pins, local/global labels, power ports, and
sequential multi-unit designators.

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
`U<number>` prefix have an uninterrupted suffix sequence beginning with `A`,
for example `U1A`, `U1B`, and `U1C`. They are emitted as units 1, 2, and 3 of
the shared KiCad reference `U1`. Gapped, duplicated, or otherwise ambiguous
sequences remain separate components.

OrCAD off-page ports become native KiCad global labels. Their connector
direction controls the KiCad orientation so the label body extends away from
and attaches to its wire or bus, and the consumed PDF text/chevron is omitted
from residual graphics. Compact filled connection dots become native KiCad
junctions. Recovered PDF text uses its source bold/italic style and an
Arial-compatible outline font with KiCad's font-size compensation.

## Usage

```sh
scripts/pdf2kicad input.pdf [output_directory]
```

The wrapper creates a cached virtual environment and installs PyMuPDF when
needed. Set `PDF2KICAD_VENV` to use an existing virtual environment.

Useful options:

```text
--paper auto|A0|A1|A2|A3|A4  select the source Capture sheet size
--no-graphics                  omit the residual PDF vector/text trace
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
```

For corpus comparisons, export both generated schematics with
`kicad-cli sch export netlist` before comparing connectivity.

## License

`pdf2kicad` is licensed under **GPL-2.0-or-later** — see [LICENSE](LICENSE).
This is the same license as the sibling `orcad2kicad` converter and is
compatible with KiCad's own GPL-3.0-or-later application license, so the code
can be reused or upstreamed within the KiCad ecosystem. Every source file
carries an `SPDX-License-Identifier: GPL-2.0-or-later` header.

The only runtime dependency, PyMuPDF, is installed by the wrapper rather than
bundled here; it is dual-licensed under AGPL-3.0-or-later or an Artifex
commercial license. The "or later" in this project's own license is what keeps
a combined distribution possible — a recipient may take `pdf2kicad` under
GPL-3.0, whose section 13 permits combination with AGPL-3.0 code. Anyone
redistributing the two together must still satisfy PyMuPDF's terms, or hold a
commercial license for it.
