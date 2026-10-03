# Repository Guidelines

## Project Structure & Module Organization

Core Python code lives in `scripts/`. `pdf_dump.py` extracts and decodes PDF
geometry; `pdf2kicad.py` reconstructs KiCad projects. The extensionless
`scripts/pdf_dump` and `scripts/pdf2kicad` files are user-facing shell wrappers
that manage the PyMuPDF environment. `scripts/kicad_symbols/` holds reduced
third-party copies of KiCad's `Device` and `Connector` symbol libraries, used by
`--kicad-rcl` so standard-symbol substitution works without a local KiCad
installation. Unit tests are consolidated in `tests/test_pdf2kicad.py`. Keep
generated `.kicad_sch`, `.kicad_pro`, and `.kicad_wks` files in a separate
output directory, not alongside source files, and do not commit them.

`web/` is the browser-only converter, published to GitHub Pages by
`.github/workflows/pages.yml`. It runs the two Python scripts unmodified
under Pyodide; there is no second implementation. Its modules are plain ES
modules with no build step: `app.js` (DOM wiring), `session.js` (which
request is current, cancellation), `worker.js`, `input.js` (PDF or ZIP to
PDF bytes), `runner.js` (Pyodide and the converter), `summary.js`,
`preview.js`, and `archive.js`. Only `app.js` and the rendering halves of
`summary.js` and `preview.js` touch the DOM. `web/vendor/` holds committed
third-party code, recorded in `web/vendor/README.md`. `web/runtime/` and
`web/py/` are build products of `scripts/build-web` and are not committed.
JavaScript tests and their fixtures live in `tests/web/`, outside the served
directory. The design is `doc/specs/2026-10-03-web-converter-design.md`.

## Build, Test, and Development Commands

- `python3 -m unittest discover -s tests -v` runs the complete self-contained
  unit suite. The tests import `scripts/pdf2kicad.py`, which requires PyMuPDF,
  so use an interpreter that has it — for example the wrapper's cached virtual
  environment (`$XDG_CACHE_HOME/pdf2kicad/venv`, or
  `~/Library/Caches/pdf2kicad/venv` on macOS), the parent repository's `.venv`,
  or whatever `$PDF2KICAD_VENV` points at.
- `scripts/pdf2kicad input.pdf output_dir` converts a schematic PDF. The wrapper
  creates a cached virtual environment and installs PyMuPDF if needed.
- `scripts/pdf2kicad input.pdf output_dir --summary-json` prints machine-readable
  per-page recovery counts, which is the quickest way to compare runs.
- `scripts/pdf_dump input.pdf --decode` prints decoded PDF primitives as JSON,
  which is useful when diagnosing recovery failures.
- `python3 -m py_compile scripts/pdf2kicad.py scripts/pdf_dump.py` performs a
  quick syntax check.
- `scripts/build-web` downloads the pinned Pyodide and PyMuPDF wheel into
  `web/runtime/` and copies the Python sources into `web/py/`. Run it again
  after changing anything under `scripts/`, or the parity test reports the
  copy as stale.
- `node --test tests/web/*.test.mjs` runs the web converter's JavaScript
  tests. Those that need `web/runtime/`, and `tests/test_web_parity.py` in
  the Python suite, are skipped until `scripts/build-web` has run;
  `PDF2KICAD_REQUIRE_WEB=1` turns the skip into a failure, as in CI.
- `python3 -m http.server -d web 8000` serves the page locally.
- `python3 tests/web/make_fixtures.py` regenerates the committed web test
  fixtures; it needs PyMuPDF.
- `kicad-cli sch export netlist output_dir/project.kicad_sch` exports a netlist
  for corpus-level connectivity comparison.

Conversion behavior is also gated by `--paper`, `--no-graphics`,
`--infer-footprints`, and `--kicad-rcl`; see `README.md` for what each affects.

## Coding Style & Naming Conventions

Use four-space indentation and conventional PEP 8 naming: `snake_case` for
functions and variables, `PascalCase` for classes, and uppercase names for
constants. Add type hints where they clarify geometry-heavy data flow. Private
helpers should begin with `_`. Preserve the GPL-2.0-or-later SPDX header in
source files. No formatter or linter is configured, so match nearby code and
keep changes focused.

The Python sources must keep running on Python 3.13, the version Pyodide
provides in the browser, even though development may use a newer one; CI
runs the suite on 3.13. Report input the converter cannot handle by raising
`ConversionError` with a message written for the user: the CLI prints it and
exits with status 1, and the web page shows it as is.

JavaScript under `web/` uses four-space indentation, double quotes, and ES
modules, with no framework, bundler, or npm dependency. Keep modules that
can run without a DOM free of DOM access so that Node can test them, and
never add a request to another origin: the page's Content-Security-Policy
forbids it and the privacy statement depends on it. The files under
`web/vendor/` are third-party code; change them only from upstream, and keep
`web/vendor/README.md` in sync, as with `scripts/kicad_symbols/`. The
Pyodide and PyMuPDF versions are pinned, with hashes, in `scripts/build-web`
alone, and move together.

The files under `scripts/kicad_symbols/` are third-party data, not project
source: do not hand-edit them or add SPDX headers to them. Take any change from
upstream KiCad and keep `scripts/kicad_symbols/NOTICE` — which records the
extracted symbols, their source, and their CC-BY-SA 4.0 with KiCad Library
Exception terms — in sync.

## Testing Guidelines

Tests use `unittest`; name classes `*Tests` and methods `test_*`, grouped by
recovered feature (worksheet, power symbols, global labels, buses, and so on).
Add a focused regression fixture for every recovery bug, using small in-memory
page, component, line, or text dictionaries when possible. Assert both the
recovered semantic object and consumption of source graphics where relevant.
Keep tests self-contained: no corpus PDFs, no network, no real KiCad
installation. Tests that need a PDF draw one in memory with PyMuPDF. Web
tests use `node:test`, one `tests/web/<module>.test.mjs` per module, with
small synthetic fixtures written by `tests/web/make_fixtures.py`. Run both
suites before submitting. A change to the page's behavior in a browser is
additionally checked by hand against the checklist in the web converter
spec. Corpus validation
additionally uses the parent repository’s `tests/0001`, `tests/0002`, and
`tests/0003` PDFs and DSN-derived reference connectivity.

## Commit & Pull Request Guidelines

Include screenshots or generated KiCad/PDF comparisons when visual schematic output changes.

Use commit messages that help reviewers understand the observable effect of the change without inventing unsupported context.

### Subject

Write a concise, imperative subject line that is understandable in `git log --oneline`.

A prefix is optional. Add one, formatted as `prefix: subject`, only when at least one of these applies:

- A ticket ID is available from the user request, branch name, issue, or surrounding commits (for example `SEI-2196:`).
- The commit mostly concerns one project or component (for example `pdf2kicad:` or `pdf_dump:`).
- A topic clearly describes the nature of the change (for example `CI`, `Docs`, `Cleanup`, `Typo`, `Fix`).

Multiple prefixes are permitted when more than one applies; chain them with colons, for example `SEI-2196: CI: ...`. Omit the prefix when none of these reasons applies. Do not invent ticket IDs, scopes, or prefixes.

Examples:

```text
Fix login redirect after session expiry
```

```text
ABC-123: Update device-tree overlay generation
```

```text
CI: Fix MSI copy exit code
```

```text
SEI-2196: CI: Fix MSI copy exit code
```

### Body decision

After writing the subject, decide whether the subject alone is sufficient to understand the observable effect of the commit.

Use a subject-only commit when the subject is enough.

Add a body after a blank line when the subject would leave important context unclear, such as what behavior changed, what limitation was addressed, or what notable files, flows, or interfaces were affected.

### Body contents

When a body is needed, summarize the relevant context and important changes. Focus on information that helps a reviewer understand the commit.

Prefer explaining the user-visible, reviewer-relevant, or operational effect of the change over restating low-level implementation details that are obvious from the diff.

### Rationale

Include rationale only when it is directly supported by explicit evidence, such as the user request, issue text, failing test, error message, design note, code comment, or reviewed source material.

If the rationale is not clear from the available context, do not infer it.

### Tests, docs, and verification

Mention tests, documentation updates, setup commands, screenshots, manual checks, or other verification only when they were actually performed, reviewed, or explicitly provided.

Do not invent test results, claim verification that was not done, or imply that documentation was updated when it was not.



<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:970c3bf2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

<!-- BEGIN BEADS CODEX SETUP: generated by bd setup codex -->
## Beads Issue Tracker

Use Beads (`bd`) for durable task tracking in repositories that include it. Use the `beads` skill at `.agents/skills/beads/SKILL.md` (project install) or `~/.agents/skills/beads/SKILL.md` (global install) for Beads workflow guidance, then use the `bd` CLI for issue operations.

### Quick Reference

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Claim work
bd close <id>           # Complete work
bd prime                # Refresh Beads context
```

### Rules

- Use `bd` for all task tracking; do not create markdown TODO lists.
- Run `bd prime` when Beads context is missing or stale. Codex 0.129.0+ can load Beads context automatically through native hooks; use `/hooks` to inspect or toggle them.
- Keep persistent project memory in Beads via `bd remember`; do not create ad hoc memory files.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.
<!-- END BEADS CODEX SETUP -->
