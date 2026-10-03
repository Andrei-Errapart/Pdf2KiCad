# Browser-only web converter on GitHub Pages

Status: approved 2026-10-03; implemented, not yet deployed
Date: 2026-10-03
Tracking: beads epic `Pdf2KiCad-gna`

Modelled on the sibling converter's page; its spec is
`Orcad2Kicad/doc/specs/2026-10-03-web-converter-design.md` and its site is
<https://andrei-errapart.github.io/Orcad2Kicad/>. Where this document says
"as in Orcad2Kicad", that spec's reasoning applies unchanged and is not
repeated here.

## Goal

A public web page where someone drops, picks, or pastes a schematic PDF — or
a ZIP archive containing exactly one PDF — sees how much of it was recovered,
looks at the converted sheets, and downloads the KiCad project as a ZIP.

Constraints, from the owner:

- **No server-side component.** The conversion runs in the visitor's browser
  and the file never leaves it. The site is static, hosted on GitHub Pages,
  and redeployed by a GitHub Actions workflow on every push to `main`.
- **PyMuPDF and MuPDF are distributed from the site** under AGPL-3.0, with
  source links in the page footer. Approved by the owner on 2026-10-03.

## Non-goals

- No backend, accounts, analytics, telemetry, or error reporting.
- No JavaScript framework, bundler, or npm build step.
- No port of the converter to another language and no second PDF extractor:
  the page runs `scripts/pdf2kicad.py` and `scripts/pdf_dump.py` themselves.
- No new recovery features. The Python changes are the three listed under
  Python changes.
- No scanned-PDF or OCR support, no batch conversion.
- No side-by-side view of the original PDF.
- No offline mode or service worker.

## Evidence from the spike

Done on 2026-10-03 in a scratch directory (`Pdf2KiCad-2ex`); none of it is
kept.

- Pyodide 0.29.5 (Python 3.13) with
  `pymupdf-1.28.0-cp313-abi3-pyemscripten_2025_0_wasm32.whl` from PyPI runs
  both scripts unmodified.
- 40 PDFs (36 on the OrCAD path, 4 Altium), each with default options and
  with `--kicad-rcl`: 812 output files, byte-identical between Pyodide under
  Node and native PyMuPDF 1.28.0 on Python 3.14.
- Headless Chromium, Firefox and WebKit, under Orcad2Kicad's
  Content-Security-Policy with the worker started from a `blob:` URL: a
  19-page PDF converts in 4.1–6.0 s from page load on localhost (conversion
  itself 2.2–3.6 s), byte-identical to native. A cross-origin `fetch` from the
  worker is refused in all three; no policy violations are reported.
- Orcad2Kicad's vendored KiCanvas renders the output; both converters emit
  schematic file version `20260306`.
- Conversion under Pyodide takes about 1.8 times as long as native.
- 31.4 MB of files, of which the PyMuPDF wheel is 18.4 MB and does not
  compress. About 24 MB transferred on first load if the host gzips
  JavaScript and wasm, which was not measured on GitHub Pages.
- Corpus pages that are schematics have at most 5,321 drawings; the
  placement maps and layout plots that stall the converter have 14,318 or
  more.

## Architecture

```
            main thread                          Web Worker
  ┌────────────────────────────┐         ┌──────────────────────────────┐
  │ app.js     state + DOM,    │         │ worker.js  (thin wrapper)    │
  │            size gate       │──bytes─▶│ input.js   file/ZIP → PDF    │
  │ summary.js recovery table  │◀─files──│ runner.js  Pyodide + PyMuPDF │
  │ preview.js sheet list +    │ summary │            + pdf2kicad.py    │
  │            KiCanvas        │ progress└──────────────────────────────┘
  │ archive.js files → ZIP     │
  └────────────────────────────┘
```

Everything that parses the visitor's file runs in the worker, so the page
stays responsive and one Cancel button stops extraction or conversion by
terminating the worker.

The converter in the browser is the Python source in `scripts/`, copied into
the site at build time. There is one implementation, and the parity test
holds the browser build to the native one.

## Layout

```
web/
  index.html
  style.css
  app.js            DOM wiring; starts the worker
  session.js        which request is current; cancellation                 (no DOM)
  input.js          bytes + filename -> { projectName, pdfBytes } | error  (no DOM)
  runner.js         Pyodide + PDF + options -> files + summary | error     (no DOM)
  worker.js         postMessage wrapper around input.js and runner.js
  summary.js        conversion summary -> recovery table
  archive.js        files -> ZIP bytes
  preview.js        sheet list + one KiCanvas embed
  vendor/           fflate, KiCanvas: committed, see Vendored code
  runtime/          Pyodide and the PyMuPDF wheel: build product, git-ignored
  py/               copies of scripts/*.py and kicad_symbols: build product, git-ignored
  version.json      build product, git-ignored
scripts/build-web   fetches runtime/, fills py/; used locally and in CI
tests/web/          node --test suites
tests/test_web_parity.py
.github/workflows/pages.yml
```

`input.js`, `archive.js`, `preview.js`, `style.css` and the shape of `app.js`
and `worker.js` are adapted from Orcad2Kicad's `web/`. They are copies, not a
shared package: two small sites do not justify one.

They are not copied as they stand. A review of Orcad2Kicad's page found three
defects in the parts reused here — `Orcad2Kicad-3o4`, `Orcad2Kicad-id6` and
`Orcad2Kicad-1m3` — and this design specifies the corrected behaviour under
Request lifecycle and Input.

## Components

### Build — `scripts/build-web`

A POSIX shell script, like the existing wrappers.

- Downloads the npm tarball `pyodide-0.29.5.tgz` and the PyMuPDF wheel named
  above, verifies each against a SHA-256 recorded in the script, and unpacks
  the five files Pyodide needs (`pyodide.mjs`, `pyodide.asm.js`,
  `pyodide.asm.wasm`, `python_stdlib.zip`, `pyodide-lock.json`) plus the wheel
  into `web/runtime/`. A download whose hash does not match fails the build.
  Downloads are cached outside the tree and skipped when already present.
- Copies `scripts/pdf2kicad.py`, `scripts/pdf_dump.py` and
  `scripts/kicad_symbols/` into `web/py/`.

The 31 MB of runtime binaries are not committed. The two pins and their
hashes live in this one script, and `web/vendor/README.md` records origin and
licence.

The wheel's `pyemscripten_2025_0` tag ties it to Pyodide 0.28/0.29. Pyodide
314.x uses a different ABI, so the two pins move together or not at all.

### Runner — `web/runner.js`

`createRunner(runtimeUrl, pyUrl)` loads Pyodide, loads the wheel with
`loadPackage`, writes the files from `py/` into Pyodide's in-memory
filesystem, and imports `pdf2kicad`. It returns
`convert(pdfBytes, projectName, options, onProgress)`, which resolves to
`{ ok: true, files: Map<name, Uint8Array>, summary }` or
`{ ok: false, message }`.

- One interpreter serves every conversion in the worker's lifetime; start-up
  costs about 1.5 s and is paid once.
- The Python glue is a few lines calling `pdf2kicad.convert_pdf_bytes`.
- A `ConversionError` returns its message; any other exception returns a
  generic message followed by the exception's last line.

It touches no DOM API, so the same module runs in the worker and under Node.

### Worker — `web/worker.js`

Started from a `blob:` URL so that it inherits the page's policy, as in
Orcad2Kicad. It creates the runner on a `warm` message sent at page load, so
the 24 MB download starts before the visitor has chosen a file.

```
in:   {type: "warm", runtimeUrl, pyUrl}
      {type: "convert", id, fileName, bytes, options}
out:  {stage: "loading"}
      {id, stage: "converting", page, pages}
      {id, ok: true, projectName, member, files, summary}
      {id, ok: false, message}
```

Cancel terminates the worker and starts a new one. The runtime then reloads
from the browser cache.

### Request lifecycle — `web/session.js`

One request is current at a time, and the most recent selection wins. The
identity of a request covers everything asynchronous that happens for it —
reading the file, waiting for start-up, extraction, conversion — not only
the worker's part.

- A generation counter advances, synchronously and before any `await`, when
  a file is accepted, when an option changes on a loaded file, and on Cancel.
- Every asynchronous step captures the generation it started under and
  drops its result if the generation has moved on: the file read, the wait
  for start-up, and each progress, result and error message from the worker.
- The page's loaded file is replaced only by a read that is still current
  when it completes.
- Cancel therefore stops a request at any stage. During a read, nothing is
  posted when the read resolves; during extraction or conversion, the worker
  is also terminated and restarted.
- Selecting A and then B converts B, whichever read finishes first. A
  worker busy with A is terminated when B is posted.
- An option change while a read is pending starts nothing; options are read
  when the conversion is posted, so the pending selection picks them up.

This logic lives in `session.js`, with no DOM access, so that it is tested
under Node; `app.js` only connects it to the page.

### Input — `web/input.js`

As in Orcad2Kicad — drop, file picker and paste; identification by magic
bytes; our own central-directory reader with 16 KiB bounded inflation through
fflate — with these differences:

| Starts with | Treated as |
|---|---|
| `%PDF-` | a PDF; passed through |
| `PK\x03\x04` | a user archive; unwrapped here |
| anything else | error: not a PDF or a ZIP |

Archive members are matched on a case-insensitive `.pdf` suffix, exactly one
is required, and the extracted member must itself start with `%PDF-`.

**Limit.** 32 MiB, for the dropped file and for the extracted PDF alike,
instead of Orcad2Kicad's 100 MB. The largest schematic PDF in the corpus is
2.5 MB, and every figure under Memory below scales with the limit.

**Checksum.** The selected member's CRC-32 is read from the central
directory and verified against the extracted bytes, for stored and deflated
members alike, before anything is handed to the converter. A mismatch is
reported as a damaged archive. Length and the `%PDF-` prefix cannot detect
corruption elsewhere in the member. Only the selected member is checked;
its neighbours are still never inflated. fflate does not export its CRC
routine, so `input.js` carries a small table-driven one.

**Memory.** The limit bounds the *decompressed bytes produced* — at most the
limit plus about 17 MB of overshoot from one 16 KiB push, as in Orcad2Kicad.
It is not a bound on memory, and this design does not claim one by
derivation. What is held, for a file at the limit:

- on the page, the dropped file, kept so that an option change can convert
  it again;
- in the worker, a copy of it;
- during extraction, the output and fflate's own buffers;
- during conversion, a copy of the PDF inside Pyodide's heap, plus whatever
  MuPDF builds from it.

Extraction writes into a single output buffer, sized from the member's
declared size capped at the limit and grown if the declaration was low. It
does not collect chunks and then join them, which is what doubled the output
in Orcad2Kicad. The peak during extraction and during conversion of a
limit-sized file is measured, not estimated, and added to this document as
an implementation note; if it is unreasonable for a browser tab, the limit
is lowered rather than the claim stretched.

### Options

| Control | CLI flag |
|---|---|
| Use KiCad resistor, capacitor and inductor symbols | `--kicad-rcl` |
| Infer footprints for two-terminal SMD passives | `--infer-footprints` |
| Omit leftover PDF graphics | `--no-graphics` |
| Sheet size (detect, A0–A4) | `--paper` |
| Source tool (detect, OrCAD, Altium) | `--flavor` |

Changing an option re-runs the conversion on the file already loaded.

### Summary — `web/summary.js`

A table with one row per page from the converter's summary — the same counts
`--summary-json` prints: wires, buses, junctions, components, pins, labels,
power ports. A page with no components and no wires is marked as carrying no
recovered schematic content. The detected source tool and sheet size are
shown above the table.

This is what tells a visitor whether the conversion understood their PDF, so
it is shown before the preview.

### Preview and download — `web/preview.js`, `web/archive.js`

As in Orcad2Kicad: a sheet list and one `<kicanvas-embed>` in the KiCad
colour scheme, fed from memory, whose failure never disables the download;
and one ZIP named `<projectName>_kicad.zip` holding `<projectName>_kicad/`.

### Page text

The page states, above the drop area:

- what is supported: vector schematic PDFs printed from OrCAD Capture or
  Altium Designer;
- what is not: scanned or image-only PDFs, PCB layout plots, other tools;
- that the result is a reconstruction from drawing primitives and must be
  checked in KiCad;
- that the first visit downloads about 24 MB and the file is not uploaded.

## Python changes

Three changes to the converter, each useful to the CLI on its own.

1. **A bytes entry point with progress.** `convert_pdf_bytes(pdf_bytes,
   name, *, paper, keep_graphics, infer_footprints, use_kicad_rcl, flavor,
   progress=None)` holds today's body of `convert_pdf`; `convert_pdf(path,
   …)` becomes a wrapper that reads the file. `progress(page, pages)` is
   called once per page.
2. **`ConversionError` and an input gate** (`Pdf2KiCad-q6g`). Raised with a
   message meant for the user when the PDF cannot be opened, is encrypted,
   has no vector drawings on any page, or has a page with more than 12,000
   drawings — above every schematic page in the corpus and below every
   document that stalls the converter. The CLI prints the message and exits
   with status 1.
3. **`assign_values` performance** (`Pdf2KiCad-3tz`). An 18-page schematic
   takes 31 s natively and 55 s in the browser, most of it in
   `_nearest_wire_point` testing every text against every wire segment. A
   bounding-box prefilter over the segments, with output required to stay
   byte-identical on the corpus.

The source must keep running on Python 3.13, the version Pyodide 0.29
provides.

## Privacy

The two properties and their mechanisms are those of Orcad2Kicad: no code
path sends the file anywhere, and a `<meta>` Content-Security-Policy —
`default-src 'none'`, `'self'` for scripts, styles, fonts, images and
`connect-src`, `'wasm-unsafe-eval'`, and `worker-src 'self' blob:` — confines
requests to the hosting origin. The spike ran Pyodide and PyMuPDF under
exactly that policy without a violation.

One difference: Orcad2Kicad's converter is a WASI module that cannot reach
the network at all, whereas Pyodide can call `fetch`. Here the policy, not
the binary, is what stops a request to another origin.

## Vendored code

| Component | Use | Licence | Delivered |
|---|---|---|---|
| Pyodide 0.29.5 | Python in the worker | MPL-2.0 | fetched by `build-web`, hash-pinned |
| PyMuPDF 1.28.0 with MuPDF | PDF extraction | AGPL-3.0 | fetched by `build-web`, hash-pinned |
| KiCanvas | preview | MIT | committed under `web/vendor/`, with Orcad2Kicad's patch |
| fflate 0.8.3 | ZIP in and out | MIT | committed under `web/vendor/` |

`web/vendor/README.md` records origin, version, SHA-256, licence and patches
for all four. The page footer credits them, links the PyMuPDF and MuPDF
source for the pinned release, credits the bundled KiCad symbols (CC-BY-SA
4.0 with the KiCad Library Exception), and links this converter's source at
the deployed commit (GPL-2.0-or-later).

## Testing

- **Unit tests** in `tests/test_pdf2kicad.py` for the three Python changes:
  the bytes entry point and its progress calls; each `ConversionError` case;
  and a regression fixture for the prefilter showing the same value
  assignment as the exhaustive search.
- **Parity — `tests/test_web_parity.py`.** Builds a small schematic-like PDF
  in memory with PyMuPDF, converts it natively and under Pyodide through
  `web/runner.js` in Node, and requires byte-identical files. Skipped when
  `web/runtime/` or Node is absent; `PDF2KICAD_REQUIRE_WEB=1`, set in CI,
  turns the skip into a failure. No corpus PDF is used, in keeping with the
  repository's testing rule.
- **JavaScript — `node --test tests/web/*.test.mjs`.**
  - Input: selection and bounds, ported from Orcad2Kicad with PDF fixtures.
  - Input, checksum: a stored member and a deflated member, each with one
    payload byte altered and the recorded CRC left alone, are refused as
    damaged; an intact archive with a large neighbour passes without the
    neighbour being inflated.
  - Input, memory: after extracting a member at the limit, the ArrayBuffer
    memory retained is the archive plus one output, not two.
  - Session: Cancel during a file read posts nothing when the read resolves;
    A then B with A's read finishing last converts B only; a worker message
    from a superseded request is ignored; Cancel during conversion restarts
    the worker; an option change during a pending read starts nothing.
  - The archive round trip; the sheet list; the summary table's rows and its
    empty-page marking; the runner returning a `ConversionError` message for
    a non-PDF.
- **Corpus check, manual.** Before the first deploy and after a runtime
  bump, the spike's 40-PDF comparison is repeated with the parent
  repository's corpus.
- **Browser checklist, manual,** run headless with Playwright in Chromium,
  Firefox and WebKit before the first deploy and after any vendored or
  runtime update: each input route; each option; a failing file; Cancel
  during a file read, a large extraction and a long conversion; choosing a
  second file while the first is converting; the summary and preview of a multi-sheet design;
  the download opens in KiCad; no request to another origin; a cross-origin
  `fetch` from the worker is refused; phone width.

## Deployment — `.github/workflows/pages.yml`

The first workflow in this repository; Orcad2Kicad's is the template.

- **Triggers:** push to `main`, pull requests, manual dispatch.
- **Build job:** Python 3.13 with `pymupdf==1.28.0`, matching the browser
  runtime, and Node 22; `scripts/build-web` with its download cache; the unit
  suite and the parity test with `PDF2KICAD_REQUIRE_WEB=1`; the Node tests;
  write the commit hash into `web/version.json`, which the page shows in its
  footer and uses as a cache-busting query on the `py/` files; upload `web/`
  as the Pages artifact.
- **Deploy job:** only on `main`, only after the build job passes, with
  `pages: write` and `id-token: write`.

Pages is not yet enabled on the repository, which is public. It is switched
on once with `gh api -X POST repos/Andrei-Errapart/Pdf2KiCad/pages -f
build_type=workflow`, when the workflow is ready to be pushed.

## Implementation order

Each step becomes a child of the epic.

1. `ConversionError` and the input gate (`Pdf2KiCad-q6g`).
2. The `assign_values` prefilter (`Pdf2KiCad-3tz`).
3. `convert_pdf_bytes` with progress.
4. `scripts/build-web`; `runner.js`, `worker.js`; the parity test.
5. `input.js` with the checksum and single-buffer extraction; `archive.js`;
   `session.js`; the page, options and per-page progress.
6. `summary.js`.
7. Vendor KiCanvas with its patch; `preview.js`.
8. Workflow, enabling Pages, first deploy; footer attributions.
9. README, `AGENTS.md` and `CLAUDE.md`, mirrored.

## Acceptance

- A visitor converts a real OrCAD PDF and a real Altium PDF, and a ZIP
  containing one, entirely in the browser; the network panel shows no request
  to another origin and none carrying file content.
- The recovery table and the sheets can be inspected before downloading, and
  the downloaded ZIP opens as a project in KiCad.
- A PCB placement map, a scanned PDF and a non-PDF are each refused with a
  specific message within a few seconds.
- Cancel stops a request at any stage, and of two files chosen in quick
  succession only the second is converted.
- A ZIP whose PDF does not match its recorded CRC-32 is refused.
- Browser output is byte-identical to native output on the parity fixture,
  enforced in CI.
- A push to `main` redeploys the site with no manual step; a failing test
  blocks the deploy.

## Risks

- **First load is about 24 MB.** Acceptable on a desktop connection, slow on
  a phone. The page says so, shows that the converter is still loading, and
  starts the download at page load. The figure assumes GitHub Pages gzips wasm; if it does not,
  it is nearer 30 MB.
- **Runtime pins move together.** A PyMuPDF release without a wasm wheel for
  the pinned Pyodide ABI blocks an upgrade of either. The pins are explicit
  and nothing forces a bump.
- **Python version skew.** Development runs 3.14, the browser 3.13. CI runs
  the suite on 3.13; a 3.14-only construct fails there rather than on the
  site.
- **Memory is not bounded by derivation.** A file is held several times
  over between the page, the worker and Pyodide (see Input, Memory), and
  none of it was measured in the spike. The 32 MiB limit keeps the
  multiples small, and the peak is measured during implementation. Pyodide's
  heap is limited to what a 32-bit wasm module can address; exhausting it
  surfaces as a conversion error, not a hang.
- **Slow conversions remain possible** after the prefilter, at 1.8 times
  native. Per-page progress and Cancel cover them.
- **The drawing budget can refuse a dense real schematic.** 12,000 is more
  than twice the densest corpus page; the message names the limit so a report
  is actionable.
- **KiCanvas** is alpha, pinned by hash and isolated, as in Orcad2Kicad.
- **No automated browser test.** UI regressions are caught by the manual
  checklist only.

## Implementation notes

Recorded after implementation (2026-10-03), where the build differs from or
adds to the design above.

- **The worker was written with the page, not with the runner.** It imports
  `input.js`, so it belongs to step 5 rather than step 4.
- **The runner is `createRunner({ runtimeUrl, pyUrl, query, toLocation,
  readBytes })`, and `convert` is synchronous.** Under Node, Pyodide takes
  file-system paths where a browser gives it URLs, so the two hooks let the
  tests run the shipped module unchanged. `scripts/build-web` writes
  `runtime/runtime.json` (the wheel's name) and `py/files.json` (the sources
  to load), so the pins and the file list live in that script alone.
- **`input.js` returns the PDF's file name, not a project name.** The
  converter derives the project name from it, as the CLI does, so there is
  one rule for both.
- **The PDF header is accepted anywhere in the first 1024 bytes,** as PDF
  readers do; some generators put a byte-order mark before it. ZIP is tested
  first, since a stored PDF puts `%PDF-` near the start of its archive.
- **Extraction fills a buffer of exactly the declared size and never grows
  it.** A member that inflates past its declared size is refused as damaged
  after the push that showed it, so growing the buffer would only postpone
  the refusal. The bound on decompressed bytes produced is therefore the
  declared size, itself at most the limit, plus one push.
- **There is no automated memory test.** What a process holds at its peak
  depends on when the garbage collector runs, so no assertion on it is
  deterministic. The test instead checks the structure: the result is its
  own buffer of exactly the member's size. Peaks were measured by hand, as
  maximum resident set size of a Node 26 process on macOS arm64, with a
  32 MiB PDF of incompressible data:

  | Step | Baseline | Peak |
  |---|---|---|
  | Extracting it from a stored ZIP | 109 MiB | 142 MiB |
  | Extracting it from a deflated ZIP | 106 MiB | 261 MiB |
  | Converting it | 735 MiB | 798 MiB |

  The deflated case costs about five times the output rather than one: each
  16 KiB push makes fflate allocate and discard buffers that carry its 32 KiB
  window, and they pile up until collected. They are garbage, not retained
  data. The interpreter with PyMuPDF loaded is the large fixed cost; a
  limit-sized PDF adds about twice its size. None of this was measured in a
  browser, and the 32 MiB limit stands.
- **Worker messages.** Loading of the runtime is reported apart from any
  request, as `{type: "runtime", state: "loading" | "ready" | "failed"}`,
  because it happens once per worker. A failed reply carries `fatal: true`
  when the interpreter raised something other than a refusal; the page then
  replaces the worker, since Pyodide may be unusable after such an error.
- **A new selection stops a busy worker at once,** not when the new file is
  posted. The old request is stale from that moment either way.
- **An error hides the previous result,** so that the Download button never
  offers an earlier file under a message about the current one.
- **The native and the WebAssembly PyMuPDF 1.28.0 contain different MuPDF
  releases:** 1.29.0 and 1.28.0. Output was byte-identical all the same on
  every file compared, but "the same release" holds for PyMuPDF only.
- **After the `assign_values` change** the 40-PDF corpus run fell from 117 s
  to 65 s natively with identical output; the slowest schematic, 18 pages,
  takes about 10 s natively and 17–29 s in the three browser engines.
- **The 19-page PDF used in the spike recovers nothing,** natively or in the
  browser: it is a recoloured print, so every page is leftover graphics. The
  page reports that plainly. Byte-identity on it therefore exercised only
  graphics; the corpus comparison is what covered semantic recovery.
- **Browser verification** of the checklist ran headless with Playwright in
  Chromium, Firefox and WebKit, served from localhost: the file picker with a
  PDF and with ZIPs; an option change; refusals of a non-PDF, a placement map
  and a corrupt ZIP; an OrCAD and an Altium PDF with recovered content;
  Cancel during a conversion and a conversion after it; a second file chosen
  while the first converts; the download, read back as a ZIP; phone width
  without horizontal scrolling; no request to another origin. Not checked:
  drag-and-drop, paste, opening the download in KiCad, a real phone, and the
  deployed site on GitHub Pages. The worker's refusal of a cross-origin
  `fetch` was checked in the spike, under the same policy, not again here.
