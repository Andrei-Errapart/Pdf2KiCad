// Copyright (C) 2026 Andrei Errapart
// SPDX-License-Identifier: GPL-2.0-or-later
//
// Test helper: convert one PDF through web/runner.js -- the code the page
// runs in its worker -- and write the files to a directory, so that
// tests/test_web_parity.py can compare them with the native conversion.
//
//   node tests/web/run_converter.mjs <file.pdf> <out_dir> [flag...]
//
// Flags are the CLI's; they are mapped back to the page's option object.

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";
import { nodeRunner } from "./node_runner.mjs";

const [pdfPath, outDir, ...flags] = process.argv.slice(2);

const options = {};
for (const flag of flags) {
    if (flag === "--kicad-rcl") options.kicadRcl = true;
    else if (flag === "--infer-footprints") options.inferFootprints = true;
    else if (flag === "--no-graphics") options.noGraphics = true;
    else if (flag.startsWith("--paper=")) options.paper = flag.slice("--paper=".length);
    else if (flag.startsWith("--flavor=")) options.flavor = flag.slice("--flavor=".length);
    else {
        console.error(`run_converter: unknown flag ${flag}`);
        process.exit(2);
    }
}

const runner = await nodeRunner();
const result = runner.convert(new Uint8Array(readFileSync(pdfPath)), basename(pdfPath), options);
if (!result.ok) {
    console.error(result.message);
    process.exit(1);
}
mkdirSync(outDir, { recursive: true });
for (const [name, data] of result.files) {
    writeFileSync(join(outDir, name), data);
}
