// Copyright (C) 2026 Andrei Errapart
// SPDX-License-Identifier: GPL-2.0-or-later
//
// From the bytes a visitor dropped, picked or pasted to the one PDF the
// converter gets.  Runs in the worker (no DOM API), so a slow or hostile
// archive never blocks the page and Cancel can always stop it.
//
// A ZIP is unwrapped here.  The member is chosen from the archive's central
// directory without inflating anything; only that member is inflated, into
// one buffer of the size the archive declares for it, and its CRC-32 is
// verified before it is handed on.

import { Inflate } from "./vendor/fflate/fflate.js";

/** The largest file, and the largest PDF inside an archive, the page accepts. */
export const MAX_INPUT_BYTES = 32 * 1024 * 1024;

/**
 * Compressed bytes pushed to the inflater at a time.  fflate's Inflate
 * decompresses everything pushed in one call before reporting output, so a
 * member that turns out larger than declared is noticed only after the push:
 * DEFLATE expands at most about 1032:1, so at most about 17 MB too late.
 */
export const INFLATE_CHUNK = 16 * 1024;

const PDF_MAGIC = [0x25, 0x50, 0x44, 0x46, 0x2d]; // %PDF-
// Readers accept the header anywhere in the first 1024 bytes; some
// generators put a byte-order mark or a line of junk before it.
const PDF_MAGIC_WINDOW = 1024;
const LOCAL_HEADER = 0x04034b50;
const CENTRAL_HEADER = 0x02014b50;
const END_OF_DIRECTORY = 0x06054b50;
const FLAG_ENCRYPTED = 0x0001;
const FLAG_UTF8 = 0x0800;
const STORED = 0;
const DEFLATED = 8;

class InputError extends Error {
    constructor(message, extra = {}) {
        super(message);
        this.extra = extra;
    }
}

const megabytes = (n) => `${Math.round(n / (1024 * 1024))} MB`;

function tooLarge(limit) {
    return new InputError(
        `The PDF is larger than ${megabytes(limit)}, the most this page converts.`,
        { inflatedBytes: 0 },
    );
}

function damaged(fileName, extra) {
    return new InputError(`The archive “${fileName}” is damaged or incomplete.`, extra);
}

function looksLikePdf(bytes) {
    const last = Math.min(bytes.length, PDF_MAGIC_WINDOW) - PDF_MAGIC.length;
    for (let at = 0; at <= last; at++) {
        if (PDF_MAGIC.every((byte, i) => bytes[at + i] === byte)) return true;
    }
    return false;
}

function startsWithZip(bytes) {
    // A local header, or the end record of an empty archive.
    return bytes.length >= 4 && bytes[0] === 0x50 && bytes[1] === 0x4b
        && ((bytes[2] === 3 && bytes[3] === 4) || (bytes[2] === 5 && bytes[3] === 6));
}

const baseName = (name) => String(name).split(/[\\/]/).pop();

const CRC_TABLE = new Uint32Array(256);
for (let n = 0; n < 256; n++) {
    let c = n;
    for (let bit = 0; bit < 8; bit++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    CRC_TABLE[n] = c;
}

/** CRC-32 as ZIP records it.  fflate has one, but does not export it. */
export function crc32(bytes) {
    let c = 0xffffffff;
    for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
}

/**
 * @param {Uint8Array} bytes  what the visitor supplied
 * @param {string} fileName  its name, for messages and the project name
 * @param {{limit?: number}} [options]
 * @returns {{ok: true, pdfName: string, pdfBytes: Uint8Array, member: string | null}
 *         | {ok: false, message: string, inflatedBytes?: number}}
 *     pdfName is the PDF's own file name, which the converter turns into the
 *     project name; member is its path inside the archive, if there was one.
 */
export function prepareInput(bytes, fileName, { limit = MAX_INPUT_BYTES } = {}) {
    try {
        // ZIP first: a stored PDF puts "%PDF-" near the start of its archive.
        if (startsWithZip(bytes)) return extractFromZip(bytes, fileName, limit);
        if (looksLikePdf(bytes)) {
            if (bytes.length > limit) throw tooLarge(limit);
            return { ok: true, pdfName: baseName(fileName), pdfBytes: bytes, member: null };
        }
        throw new InputError(`“${fileName}” is not a PDF file or a ZIP archive.`);
    } catch (error) {
        if (error instanceof InputError) {
            return { ok: false, message: error.message, ...error.extra };
        }
        throw error;
    }
}

function extractFromZip(bytes, fileName, limit) {
    const entries = readCentralDirectory(bytes, fileName);
    const candidates = entries.filter((entry) => isPdfMember(entry.name));
    if (candidates.length === 0) {
        throw new InputError(`The archive “${fileName}” contains no PDF file.`);
    }
    if (candidates.length > 1) {
        throw new InputError(
            `The archive “${fileName}” contains several PDF files `
            + `(${candidates.map((entry) => entry.name).join(", ")}); `
            + "it must contain exactly one.",
        );
    }

    const entry = candidates[0];
    if (entry.flags & FLAG_ENCRYPTED) {
        throw new InputError(
            `“${entry.name}” in the archive is encrypted, which is not supported.`,
        );
    }
    if (entry.method !== STORED && entry.method !== DEFLATED) {
        throw new InputError(
            `“${entry.name}” in the archive uses an unsupported compression method.`,
        );
    }
    // The declared size sizes the output buffer, so it is checked first; a
    // member that is really larger is caught while it inflates.
    if (entry.size > limit) throw tooLarge(limit);

    const data = memberData(bytes, entry, fileName);
    const pdfBytes = entry.method === STORED
        ? copyStored(data, entry.size, fileName)
        : inflateInto(data, entry.size, fileName);
    // Length and the PDF header cannot show corruption elsewhere in the member.
    if (crc32(pdfBytes) !== entry.crc) throw damaged(fileName);
    if (!looksLikePdf(pdfBytes)) {
        throw new InputError(`“${entry.name}” in the archive is not a PDF file.`);
    }
    return {
        ok: true,
        pdfName: baseName(entry.nameReliable ? entry.name : fileName),
        pdfBytes,
        member: entry.name,
    };
}

function isPdfMember(name) {
    if (name.endsWith("/") || name.endsWith("\\")) return false;
    const parts = name.split(/[\\/]/);
    if (parts.includes("__MACOSX")) return false;
    const base = parts[parts.length - 1];
    // Dot-files include macOS's ._board.pdf resource forks.
    return !base.startsWith(".") && base.toLowerCase().endsWith(".pdf");
}

/** Every entry's name, flags, method, checksum, sizes and local-header offset. */
function readCentralDirectory(bytes, fileName) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const need = (offset, length) => {
        if (offset < 0 || offset + length > bytes.length) throw damaged(fileName);
    };
    const u16 = (offset) => (need(offset, 2), view.getUint16(offset, true));
    const u32 = (offset) => (need(offset, 4), view.getUint32(offset, true));

    // The end record sits in the last 22 bytes plus at most a 64 KiB comment.
    let end = -1;
    for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 22 - 0xffff); i--) {
        if (view.getUint32(i, true) === END_OF_DIRECTORY) {
            end = i;
            break;
        }
    }
    if (end < 0) throw damaged(fileName);
    const count = u16(end + 10);
    const directorySize = u32(end + 12);
    const directoryOffset = u32(end + 16);
    if (count === 0xffff || directoryOffset === 0xffffffff) {
        throw new InputError(`“${fileName}” is a ZIP64 archive, which is not supported.`);
    }
    if (directoryOffset + directorySize > end) throw damaged(fileName);

    const entries = [];
    let at = directoryOffset;
    for (let i = 0; i < count; i++) {
        if (u32(at) !== CENTRAL_HEADER) throw damaged(fileName);
        const flags = u16(at + 8);
        const nameLength = u16(at + 28);
        need(at + 46, nameLength);
        entries.push({
            flags,
            method: u16(at + 10),
            crc: u32(at + 16),
            compressedSize: u32(at + 20),
            size: u32(at + 24),
            localOffset: u32(at + 42),
            ...decodeName(bytes.subarray(at + 46, at + 46 + nameLength), flags),
        });
        at += 46 + nameLength + u16(at + 30) + u16(at + 32);
    }
    return entries;
}

/**
 * Names are UTF-8 when flagged, and in practice often UTF-8 when not.  Any
 * other legacy encoding (an OEM codepage) is decoded byte for byte, which is
 * enough to match the extension, and is not trusted as the PDF's name.
 */
function decodeName(nameBytes, flags) {
    if (flags & FLAG_UTF8) {
        return { name: new TextDecoder().decode(nameBytes), nameReliable: true };
    }
    try {
        const name = new TextDecoder("utf-8", { fatal: true }).decode(nameBytes);
        return { name, nameReliable: true };
    } catch {
        return { name: String.fromCharCode(...nameBytes), nameReliable: false };
    }
}

/** The member's compressed bytes, located through its local header. */
function memberData(bytes, entry, fileName) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const at = entry.localOffset;
    if (at + 30 > bytes.length || view.getUint32(at, true) !== LOCAL_HEADER) {
        throw damaged(fileName);
    }
    // The local header's own lengths: its extra field can differ from the
    // central directory's.  Sizes come from the central directory, which is
    // correct even when the local header defers them to a data descriptor.
    const start = at + 30 + view.getUint16(at + 26, true) + view.getUint16(at + 28, true);
    const end = start + entry.compressedSize;
    if (end > bytes.length) throw damaged(fileName);
    return bytes.subarray(start, end);
}

/** A copy, so that the result does not keep the whole archive alive. */
function copyStored(data, size, fileName) {
    if (data.length !== size) throw damaged(fileName);
    return data.slice();
}

/**
 * Inflate into one buffer of the declared size.  The inflater's chunks are
 * copied in as they arrive and not kept; bytes past the declared size are
 * counted, not stored, and end the extraction after the push that made them.
 */
function inflateInto(data, size, fileName) {
    const out = new Uint8Array(size);
    let total = 0;
    const inflater = new Inflate((chunk) => {
        const room = size - total;
        if (room > 0) out.set(chunk.length <= room ? chunk : chunk.subarray(0, room), total);
        total += chunk.length;
    });
    try {
        for (let offset = 0; offset < data.length || offset === 0; offset += INFLATE_CHUNK) {
            const end = Math.min(offset + INFLATE_CHUNK, data.length);
            inflater.push(data.subarray(offset, end), end === data.length);
            if (total > size) throw damaged(fileName, { inflatedBytes: total });
            if (end === data.length) break;
        }
    } catch (error) {
        if (error instanceof InputError) throw error;
        throw damaged(fileName);
    }
    if (total !== size) throw damaged(fileName);
    return out;
}
