// Copyright (C) 2026 Andrei Errapart
// SPDX-License-Identifier: GPL-2.0-or-later
//
// web/summary.js: the converter's per-page recovery counts as the table the
// page shows.  Only the rows and the headline are tested here; putting them
// into the DOM is covered by the browser checklist.

import assert from "node:assert/strict";
import { test } from "node:test";
import { COLUMNS, summaryHeadline, summaryRows, summaryTotals } from "../../web/summary.js";

function page(number, counts = {}) {
    return {
        page: number,
        sheet_name: `0${number}_SHEET`,
        sheet_file: `0${number}_SHEET.kicad_sch`,
        paper: "A3",
        wires: 0, buses: 0, bus_entries: 0, junctions: 0, no_connects: 0,
        components: 0, dnp_components: 0, pins: 0,
        local_labels: 0, global_labels: 0, power_ports: 0,
        inferred_footprints: [], standardized_passives: 0, worksheet: null,
        ...counts,
    };
}

const summary = {
    project: "board",
    flavor: "orcad",
    paper: "A3",
    multi_units: {},
    pages: [
        page(1),
        page(2, {
            wires: 171, junctions: 54, components: 25, pins: 226,
            local_labels: 27, global_labels: 3, power_ports: 23, buses: 2,
        }),
        page(3, { wires: 4 }),
    ],
};

test("one row per page, in order, named by sheet", () => {
    const rows = summaryRows(summary);
    assert.deepEqual(rows.map((row) => row.page), [1, 2, 3]);
    assert.deepEqual(rows.map((row) => row.sheet), ["01_SHEET", "02_SHEET", "03_SHEET"]);
});

test("the columns are the spec's counts, with both kinds of label added up", () => {
    assert.deepEqual(
        COLUMNS.map((column) => column.title),
        ["Components", "Pins", "Wires", "Junctions", "Labels", "Power ports", "Buses"],
    );
    assert.deepEqual(summaryRows(summary)[1].counts, [25, 226, 171, 54, 30, 23, 2]);
});

test("a page with no components and no wires is marked as empty", () => {
    assert.deepEqual(summaryRows(summary).map((row) => row.empty), [true, false, false]);
});

test("totals add the pages up", () => {
    assert.deepEqual(summaryTotals(summary), [25, 226, 175, 54, 30, 23, 2]);
});

test("the headline names the source tool, the sheet size and what was recovered", () => {
    assert.equal(
        summaryHeadline(summary),
        "Read as OrCAD Capture output, sheet size A3. "
        + "2 of 3 pages have recovered schematic content.",
    );
    assert.equal(
        summaryHeadline({ ...summary, flavor: "altium", paper: "mixed", pages: [page(1, { wires: 1 })] }),
        "Read as Altium Designer output, mixed sheet sizes. "
        + "The page has recovered schematic content.",
    );
});

test("a conversion that recovered nothing says so plainly", () => {
    assert.equal(
        summaryHeadline({ ...summary, pages: [page(1), page(2)] }),
        "Read as OrCAD Capture output, sheet size A3. "
        + "No schematic content was recovered: the PDF's drawings are kept as plain "
        + "graphics only. It may not come from a supported tool.",
    );
});
