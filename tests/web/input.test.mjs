// Copyright (C) 2026 Andrei Errapart
// SPDX-License-Identifier: GPL-2.0-or-later
//
// web/input.js: from the bytes a visitor supplied to the PDF the converter
// gets.  Fixtures are written by tests/web/make_fixtures.py with Python's
// zipfile, an implementation independent of the page's.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { zipSync } from "../../web/vendor/fflate/fflate.js";
import { INFLATE_CHUNK, MAX_INPUT_BYTES, crc32, prepareInput } from "../../web/input.js";

const fixture = (name) =>
    new Uint8Array(readFileSync(new URL(`fixtures/${name}`, import.meta.url)));
const PDF = fixture("vector.pdf");

// The spec's chunk size, written out rather than imported so that a change to
// the module's constant fails here.  DEFLATE expands at most about 1032:1, so
// one pushed chunk can produce at most MAX_PUSH_OUTPUT bytes.
const CHUNK = 16 * 1024;
const MAX_PUSH_OUTPUT = CHUNK * 1032;
const MIB = 1024 * 1024;

function assertPdf(result, pdfName, member = null) {
    assert.equal(result.ok, true, result.message);
    assert.equal(result.pdfName, pdfName);
    assert.equal(result.member, member);
    assert.deepEqual(result.pdfBytes, PDF);
}

function assertRefused(result, pattern) {
    assert.equal(result.ok, false);
    assert.match(result.message, pattern);
    assert.equal(result.pdfBytes, undefined);
}

// --- Selection ---------------------------------------------------------------

test("a PDF passes through", () => {
    assertPdf(prepareInput(PDF, "board.pdf"), "board.pdf");
});

test("a path in the file name is dropped", () => {
    assertPdf(prepareInput(PDF, "C:\\work\\board.pdf"), "board.pdf");
    assertPdf(prepareInput(PDF, "work/rev b/board.pdf"), "board.pdf");
});

test("the container is recognised by content, not by extension", () => {
    assertPdf(prepareInput(PDF, "board.txt"), "board.txt");
    assertPdf(prepareInput(fixture("one-pdf.zip"), "design.pdf"), "vector.pdf", "vector.pdf");
});

test("a PDF header after a little leading junk is accepted, as readers do", () => {
    const withBom = new Uint8Array(PDF.length + 3);
    withBom.set([0xef, 0xbb, 0xbf]);
    withBom.set(PDF, 3);
    assert.equal(prepareInput(withBom, "bom.pdf").ok, true);

    const buried = new Uint8Array(4096);
    buried.set(PDF.subarray(0, 8), 2000);
    assertRefused(prepareInput(buried, "late.pdf"), /not a PDF file or a ZIP archive/);
});

test("bytes that are neither a PDF nor a ZIP are refused", () => {
    assertRefused(
        prepareInput(new TextEncoder().encode("plain text"), "notes.pdf"),
        /“notes\.pdf” is not a PDF file or a ZIP archive/,
    );
    assertRefused(prepareInput(new Uint8Array(0), "empty.pdf"), /not a PDF file or a ZIP archive/);
});

test("a PDF over the limit is refused", () => {
    assertRefused(prepareInput(PDF, "board.pdf", { limit: PDF.length - 1 }), /larger than/);
    assertPdf(prepareInput(PDF, "board.pdf", { limit: PDF.length }), "board.pdf");
});

test("a zipped design folder yields its one PDF", () => {
    assertPdf(
        prepareInput(fixture("design-folder.zip"), "board.zip"),
        "Schematic Rev B.PDF",
        "board/Schematic Rev B.PDF",
    );
});

test("a stored member is read", () => {
    assertPdf(prepareInput(fixture("stored.zip"), "s.zip"), "vector.pdf", "vector.pdf");
});

test("an archive written with data descriptors is read", () => {
    assertPdf(prepareInput(fixture("data-descriptor.zip"), "d.zip"), "vector.pdf", "vector.pdf");
});

test("a UTF-8 member name is kept", () => {
    assertPdf(prepareInput(fixture("utf8-name.zip"), "u.zip"), "基板 Rev2.pdf", "基板 Rev2.pdf");
});

test("an archive without a PDF is refused", () => {
    assertRefused(prepareInput(fixture("no-pdf.zip"), "n.zip"), /“n\.zip” contains no PDF file/);
});

test("an archive with several PDFs is refused, naming them", () => {
    assertRefused(
        prepareInput(fixture("two-pdfs.zip"), "t.zip"),
        /several PDF files \(a\.pdf, sub\/b\.pdf\); it must contain exactly one/,
    );
});

test("an encrypted member is refused", () => {
    assertRefused(prepareInput(fixture("encrypted.zip"), "e.zip"), /“vector\.pdf” .* is encrypted/);
});

test("a zipped file that is not a PDF is refused", () => {
    assertRefused(
        prepareInput(fixture("not-a-pdf-inside.zip"), "f.zip"),
        /“fake\.pdf” in the archive is not a PDF file/,
    );
});

test("a truncated archive is refused, not thrown", () => {
    const zip = fixture("one-pdf.zip");
    for (const cut of [zip.length - 1, zip.length - 30, 40, 8]) {
        assertRefused(prepareInput(zip.subarray(0, cut), "cut.zip"), /damaged or incomplete/);
    }
});

// --- Checksum ----------------------------------------------------------------

test("CRC-32 is the ZIP polynomial", () => {
    assert.equal(crc32(new TextEncoder().encode("123456789")), 0xcbf43926);
    assert.equal(crc32(new Uint8Array(0)), 0);
});

test("a stored member that does not match its checksum is refused", () => {
    assertRefused(
        prepareInput(fixture("stored-corrupt.zip"), "sc.zip"),
        /“sc\.zip” is damaged or incomplete/,
    );
});

test("a deflated member that does not match its checksum is refused", () => {
    assertRefused(
        prepareInput(fixture("deflated-corrupt.zip"), "dc.zip"),
        /“dc\.zip” is damaged or incomplete/,
    );
});

// --- Bounds ------------------------------------------------------------------

test("a large neighbour of the PDF is never inflated", () => {
    // big.bin inflates to 40 MiB; with a limit far below that the PDF beside
    // it is still extracted, which it could not be if big.bin were inflated.
    const result = prepareInput(fixture("large-neighbour.zip"), "l.zip", { limit: MIB });
    assertPdf(result, "vector.pdf", "vector.pdf");
});

test("a member declared larger than the limit is refused before inflating", () => {
    const result = prepareInput(fixture("bomb.zip"), "b.zip", { limit: MIB });
    assertRefused(result, /larger than 1 MB/);
    assert.equal(result.inflatedBytes, 0);
});

test("a member larger than it declares is stopped within one push", () => {
    // The archive says 1024 bytes; the member really inflates to 40 MiB.
    const result = prepareInput(fixture("understated-size.zip"), "u.zip", { limit: 8 * MIB });
    assertRefused(result, /damaged or incomplete/);
    assert.ok(result.inflatedBytes > 1024);
    assert.ok(
        result.inflatedBytes <= 1024 + MAX_PUSH_OUTPUT,
        `inflated ${result.inflatedBytes} bytes of a member declared as 1024`,
    );
});

test("extraction fills one buffer of exactly the member's size", () => {
    // A member at the limit: the result is its own buffer, not a view into a
    // larger one and not a join of collected chunks.
    const limit = 4 * MIB;
    const member = new Uint8Array(limit);
    member.set(PDF);
    for (const level of [0, 6]) {
        const zip = zipSync({ "at-limit.pdf": member }, { level });
        const result = prepareInput(zip, "z.zip", { limit });
        assert.equal(result.ok, true, result.message);
        assert.equal(result.pdfBytes.length, limit);
        assert.equal(result.pdfBytes.byteOffset, 0);
        assert.equal(result.pdfBytes.buffer.byteLength, limit);
        assert.notEqual(result.pdfBytes.buffer, zip.buffer);
        assert.deepEqual(result.pdfBytes, member);
    }
});

test("the limits are the spec's: 32 MiB, inflated 16 KiB at a time", () => {
    assert.equal(MAX_INPUT_BYTES, 32 * 1024 * 1024);
    assert.equal(INFLATE_CHUNK, CHUNK);
});
