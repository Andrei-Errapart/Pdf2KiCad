// Copyright (C) 2026 Andrei Errapart
// SPDX-License-Identifier: GPL-2.0-or-later
//
// web/runner.js: the converter under Pyodide, as the page's worker runs it.

import assert from "node:assert/strict";
import { test } from "node:test";
import { fixture, nodeRunner, skipWithoutRuntime } from "./node_runner.mjs";

const skip = skipWithoutRuntime();
const decoder = new TextDecoder();

test("a vector PDF converts to a KiCad project named after the file", { skip }, async () => {
    const runner = await nodeRunner();
    const result = runner.convert(await fixture("vector.pdf"), "Demo Board.pdf", {});

    assert.equal(result.ok, true, result.message);
    assert.equal(result.summary.project, "Demo_Board");
    assert.equal(result.summary.pages.length, 2);
    assert.ok(result.files instanceof Map);
    assert.ok(result.files.has("Demo_Board.kicad_pro"));
    const root = decoder.decode(result.files.get("Demo_Board.kicad_sch"));
    assert.match(root, /^\(kicad_sch/);
    for (const data of result.files.values()) {
        assert.ok(data instanceof Uint8Array);
    }
});

test("progress is reported for every page, in order", { skip }, async () => {
    const runner = await nodeRunner();
    const calls = [];
    const result = runner.convert(
        await fixture("vector.pdf"), "demo.pdf", {}, (page, pages) => calls.push([page, pages]),
    );
    assert.equal(result.ok, true, result.message);
    assert.deepEqual(calls, [[1, 2], [2, 2]]);
});

test("options reach the converter", { skip }, async () => {
    const runner = await nodeRunner();
    const bytes = await fixture("vector.pdf");
    const a3 = runner.convert(bytes, "demo.pdf", { paper: "A3" });
    const a1 = runner.convert(bytes, "demo.pdf", { paper: "A1", flavor: "altium" });
    assert.equal(a3.summary.paper, "A3");
    assert.equal(a3.summary.flavor, "orcad");
    assert.equal(a1.summary.paper, "A1");
    assert.equal(a1.summary.flavor, "altium");

    const traced = runner.convert(bytes, "demo.pdf", {});
    const bare = runner.convert(bytes, "demo.pdf", { noGraphics: true });
    const page = [...traced.files.keys()].find((name) => /\.kicad_sch$/.test(name) && name !== "demo.kicad_sch");
    assert.ok(bare.files.get(page).length < traced.files.get(page).length);
});

test("a refused input returns the converter's own message", { skip }, async () => {
    const runner = await nodeRunner();
    const result = runner.convert(new TextEncoder().encode("plain text"), "notes.pdf", {});
    assert.deepEqual(result, { ok: false, message: '"notes.pdf" is not a readable PDF.' });
});

test("an unexpected failure is reported without a traceback", { skip }, async () => {
    const runner = await nodeRunner();
    const result = runner.convert(await fixture("vector.pdf"), "demo.pdf", { paper: "Z9" });
    assert.equal(result.ok, false);
    // Marked, so that the page replaces a worker whose interpreter may be broken.
    assert.equal(result.unexpected, true);
    assert.match(result.message, /^The converter failed unexpectedly \(.*Z9.*\)\.$/);
    assert.doesNotMatch(result.message, /Traceback|File "/);
    // The interpreter is still usable afterwards.
    assert.equal(runner.convert(await fixture("vector.pdf"), "demo.pdf", {}).ok, true);
});
