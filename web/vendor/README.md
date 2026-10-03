# Third-party code used by the web converter

Everything the page loads is served from this site; nothing comes from a CDN.
Two components are committed here. Two more are too large to commit and are
downloaded by `scripts/build-web`, at pinned versions and checked against
hashes recorded in that script.

To update a committed component, replace the files from the stated source,
re-apply the listed patches, update the hashes, and run the browser checklist
in `doc/specs/2026-10-03-web-converter-design.md`. To update a downloaded
one, change its pin and hash in `scripts/build-web`, and the versions and
links in the page footer and in this file; then repeat the corpus comparison
and the browser checklist described in that spec.

## Committed

### fflate

- Source: npm `fflate` 0.8.3, `esm/browser.js` (renamed `fflate.js`) and
  `LICENSE` (<https://github.com/101arrowz/fflate>).
- Tarball SHA-256: `38c2cd824402407b43153c782274aec2ea83ea688e4aa0b743c5f2c305857d92`
- Licence: MIT (`LICENSE`).
- Used by `web/input.js` (the synchronous `Inflate`, fed 16 KiB at a time)
  and `web/archive.js` (`zipSync`). Its async APIs start workers of their own
  and are deliberately not used.
- No local changes. `fflate.js` SHA-256:
  `b7ca4450b19559a1d50eb381adcee94b82449674be4cd17789d9beba7e6122a1`

### KiCanvas

- Source: the published bundle <https://kicanvas.org/kicanvas/kicanvas.js>,
  downloaded 2026-10-03 (unversioned; upstream `main` was at `b031159`), and
  `LICENSE.md` from <https://github.com/theacodes/kicanvas>. The files here
  are the copy the sibling Orcad2Kicad converter vendors, patch included.
- Downloaded bundle SHA-256: `ca910f25276c3efb9aacb3a5d6341d4d9af4736d4c875fb0440d2cc856865ab7`
- Licence: MIT (`LICENSE.md`).
- Used by `web/preview.js`, loaded on first preview.

Local patch:

- At load, the bundle appended a `<link>` to a `fonts.googleapis.com`
  stylesheet (Material Symbols icons and the Nunito face) -- a request to a
  third party, which the page's policy forbids anyway. The statement is
  replaced by a comment. Nothing is lost in the page's configuration
  (`controls="basic" controlslist="nodownload"`): the only toolbar icons left
  are SVG ones, and text falls back to the system font.

| File | SHA-256 |
|---|---|
| `kicanvas.js` (patched) | `65b2a537b07fc420a3b0123bb8a5d0a5fa0bc3da989852a9c3c7bbc0e2fd2f6e` |

Known upstream quirks, harmless here:

- Each embed requests the literal URL `$$:0:$$` from this site and gets a
  404: its SVG icon template is parsed with a placeholder `xlink:href` before
  the real value is filled in.
- Replacing an embed while it is still initialising (switching sheets or
  files quickly) can log "DisposableStack ... already been disposed" or a
  `setTransform` error from the discarded embed. The new embed is unaffected.
- The theme is chosen through KiCanvas's own `localStorage` key
  `kc:prefs:theme`; if a future bundle renames it, the preview falls back to
  KiCanvas's dark default rather than breaking.

## Downloaded by `scripts/build-web` into `web/runtime/`

### Pyodide

- Source: npm `pyodide` 0.29.5 (<https://github.com/pyodide/pyodide>); the
  files `pyodide.mjs`, `pyodide.asm.js`, `pyodide.asm.wasm`,
  `python_stdlib.zip` and `pyodide-lock.json` from the tarball.
- Tarball SHA-256: `6749b10ee515a1458ecea36d860c8ce2a48366e85b3f92d749ad1840e4aff181`
- Licence: MPL-2.0.
- Used by `web/runner.js`: CPython 3.13 compiled to WebAssembly.
- No local changes.

### PyMuPDF, with MuPDF

- Source: PyPI `pymupdf` 1.28.0, the wheel
  `pymupdf-1.28.0-cp313-abi3-pyemscripten_2025_0_wasm32.whl`
  (<https://github.com/pymupdf/PyMuPDF>). It contains MuPDF 1.28.0
  (<https://github.com/ArtifexSoftware/mupdf>) built for WebAssembly.
- Wheel SHA-256: `74c6d00ba2a9aad3a635db73b07c15db462b480741d831a34a75a56535ebc22b`
- Licence: GNU AGPL, version 3, for both. The site distributes the wheel as
  published; the page footer links the source of both, which is how the
  licence's source offer is met.
- Used by `scripts/pdf_dump.py` to read drawing objects and text spans.
- No local changes.

The wheel's `pyemscripten_2025_0` tag is the ABI of Pyodide 0.28 and 0.29.
The two pins move together: a newer Pyodide needs a wheel built for its ABI.
