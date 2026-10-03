// Copyright (C) 2026 Andrei Errapart
// SPDX-License-Identifier: GPL-2.0-or-later
//
// The converter's summary as a table: what was recovered from each page.
// This is what tells a visitor whether the conversion understood their PDF,
// so the page shows it before the preview.  The counts are the ones
// `pdf2kicad --summary-json` prints.
//
// Only renderSummary touches the DOM.

/** Table columns: a title and how to read the count from a page's summary. */
export const COLUMNS = [
    { title: "Components", count: (page) => page.components },
    { title: "Pins", count: (page) => page.pins },
    { title: "Wires", count: (page) => page.wires },
    { title: "Junctions", count: (page) => page.junctions },
    { title: "Labels", count: (page) => page.local_labels + page.global_labels },
    { title: "Power ports", count: (page) => page.power_ports },
    { title: "Buses", count: (page) => page.buses },
];

const TOOLS = { orcad: "OrCAD Capture", altium: "Altium Designer" };

/** A page from which nothing electrical was recovered. */
const isEmpty = (page) => page.components === 0 && page.wires === 0;

/**
 * @param {{pages: object[]}} summary  as the converter returns it
 * @returns {{page: number, sheet: string, counts: number[], empty: boolean}[]}
 */
export function summaryRows(summary) {
    return summary.pages.map((page) => ({
        page: page.page,
        sheet: page.sheet_name,
        counts: COLUMNS.map((column) => column.count(page)),
        empty: isEmpty(page),
    }));
}

/** Each column added up over the pages. */
export function summaryTotals(summary) {
    return COLUMNS.map((column) =>
        summary.pages.reduce((total, page) => total + column.count(page), 0));
}

/** One or two sentences: the source tool, the sheet size, how much was recovered. */
export function summaryHeadline(summary) {
    const tool = TOOLS[summary.flavor] ?? summary.flavor;
    const paper = summary.paper === "mixed" ? "mixed sheet sizes" : `sheet size ${summary.paper}`;
    const total = summary.pages.length;
    const recovered = summary.pages.filter((page) => !isEmpty(page)).length;
    let outcome;
    if (recovered === 0) {
        outcome = "No schematic content was recovered: the PDF's drawings are kept as plain "
            + "graphics only. It may not come from a supported tool.";
    } else if (total === 1) {
        outcome = "The page has recovered schematic content.";
    } else {
        outcome = `${recovered} of ${total} pages have recovered schematic content.`;
    }
    return `Read as ${tool} output, ${paper}. ${outcome}`;
}

function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
}

/** Fill `headline` and `table` (a <table>) from the summary. */
export function renderSummary({ headline, table }, summary) {
    headline.textContent = summaryHeadline(summary);

    const head = element("tr");
    head.append(element("th", "Page"), element("th", "Sheet"));
    for (const column of COLUMNS) head.append(element("th", column.title, "number"));
    const thead = element("thead");
    thead.append(head);

    const tbody = element("tbody");
    for (const row of summaryRows(summary)) {
        const tr = element("tr", undefined, row.empty ? "empty" : "");
        tr.append(element("td", String(row.page)), element("td", row.sheet));
        if (row.empty) {
            const note = element("td", "no schematic content recovered");
            note.colSpan = COLUMNS.length;
            tr.append(note);
        } else {
            for (const count of row.counts) tr.append(element("td", String(count), "number"));
        }
        tbody.append(tr);
    }

    const tfoot = element("tfoot");
    if (summary.pages.length > 1) {
        const tr = element("tr");
        const label = element("th", "Total");
        label.colSpan = 2;
        tr.append(label);
        for (const count of summaryTotals(summary)) {
            tr.append(element("td", String(count), "number"));
        }
        tfoot.append(tr);
    }
    table.replaceChildren(thead, tbody, tfoot);
}
