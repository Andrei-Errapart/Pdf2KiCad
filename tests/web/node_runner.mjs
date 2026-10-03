// Copyright (C) 2026 Andrei Errapart
// SPDX-License-Identifier: GPL-2.0-or-later
//
// Test helper: web/runner.js over the runtime that scripts/build-web put in
// web/runtime and web/py, read from disk instead of fetched.
//
// The tests that need the runtime skip when it has not been built, so that
// `node --test` passes on a fresh checkout; PDF2KICAD_REQUIRE_WEB=1 (set in
// CI) turns the skip into a failure.

import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createRunner } from "../../web/runner.js";

const webUrl = new URL("../../web/", import.meta.url);

export const runtimeBuilt = existsSync(new URL("runtime/runtime.json", webUrl))
    && existsSync(new URL("py/pdf2kicad.py", webUrl));
export const runtimeRequired = process.env.PDF2KICAD_REQUIRE_WEB === "1";

/** The reason to skip a runtime test, or false to run it. */
export function skipWithoutRuntime() {
    if (runtimeBuilt) return false;
    if (runtimeRequired) {
        throw new Error("PDF2KICAD_REQUIRE_WEB=1 but web/runtime is not built; run scripts/build-web");
    }
    return "web/runtime is not built; run scripts/build-web";
}

let runner = null;

/** One runner for the whole test process: starting Pyodide takes a second. */
export function nodeRunner() {
    runner ??= createRunner({
        runtimeUrl: new URL("runtime/", webUrl).href,
        pyUrl: new URL("py/", webUrl).href,
        // Under Node, Pyodide takes file-system paths, not file: URLs.
        toLocation: fileURLToPath,
        readBytes: async (url) => new Uint8Array(await readFile(new URL(url))),
    });
    return runner;
}

/** A fixture as a plain Uint8Array, which is what the page handles. */
export async function fixture(name) {
    return new Uint8Array(await readFile(new URL(`fixtures/${name}`, import.meta.url)));
}
