// Copyright (C) 2026 Andrei Errapart
// SPDX-License-Identifier: GPL-2.0-or-later
//
// The converter under Pyodide: scripts/pdf2kicad.py itself, with PyMuPDF
// built for WebAssembly, in an interpreter that serves every conversion of
// its worker's lifetime.  scripts/build-web provides what is loaded here:
// runtime/ (Pyodide, the PyMuPDF wheel, runtime.json) and py/ (the Python
// sources and files.json).
//
// No DOM API is used, so the same module runs in the page's worker and, with
// the two hooks below, under Node in the tests.

const decoder = new TextDecoder();
const encoder = new TextEncoder();

// The page's options, by name, onto convert_pdf_bytes' keywords.  A
// ConversionError is the converter refusing the input, with a message
// written for the visitor; anything else propagates as a PythonError.
const GLUE = `
import json
import sys

sys.path.insert(0, "/app")
import pdf2kicad


def convert_for_page(pdf, name, options_json, progress):
    options = json.loads(options_json)
    try:
        files, summary = pdf2kicad.convert_pdf_bytes(
            pdf.to_bytes(),
            name,
            paper=options.get("paper") or "auto",
            keep_graphics=not options.get("noGraphics"),
            infer_footprints=bool(options.get("inferFootprints")),
            use_kicad_rcl=bool(options.get("kicadRcl")),
            flavor=options.get("flavor") or "auto",
            progress=progress,
        )
    except pdf2kicad.ConversionError as error:
        return json.dumps({"ok": False, "message": str(error)})
    return json.dumps({"ok": True, "files": files, "summary": summary})
`;

async function fetchBytes(url) {
    const response = await fetch(url);
    if (!response.ok) {
        throw new Error(`${url} could not be loaded (HTTP ${response.status})`);
    }
    return new Uint8Array(await response.arrayBuffer());
}

/** The last line of a Python traceback: the exception and its message. */
function lastLine(text) {
    const lines = String(text).split("\n").filter((line) => line.trim() !== "");
    return lines.length > 0 ? lines[lines.length - 1].trim() : "unknown error";
}

/**
 * Start the interpreter and load the converter into it.
 *
 * @param {object} where
 * @param {string} where.runtimeUrl  URL of runtime/, ending in a slash
 * @param {string} where.pyUrl       URL of py/, ending in a slash
 * @param {string} [where.query]     appended to the py/ URLs, to bust caches
 * @param {(url: string) => string} [where.toLocation]  a URL as Pyodide
 *     wants it: unchanged in a browser, a file-system path under Node
 * @param {(url: string) => Promise<Uint8Array>} [where.readBytes]
 * @returns {Promise<{convert: Function}>}
 */
export async function createRunner({
    runtimeUrl,
    pyUrl,
    query = "",
    toLocation = (url) => url,
    readBytes = fetchBytes,
}) {
    const readJson = async (url) => JSON.parse(decoder.decode(await readBytes(url)));
    const runtime = await readJson(`${runtimeUrl}runtime.json`);
    const { loadPyodide } = await import(`${runtimeUrl}pyodide/pyodide.mjs`);
    const pyodide = await loadPyodide({ indexURL: toLocation(`${runtimeUrl}pyodide/`) });
    await pyodide.loadPackage(toLocation(`${runtimeUrl}${runtime.wheel}`), {
        messageCallback() {},
    });

    pyodide.FS.mkdirTree("/app/kicad_symbols");
    for (const name of await readJson(`${pyUrl}files.json${query}`)) {
        pyodide.FS.writeFile(`/app/${name}`, await readBytes(`${pyUrl}${name}${query}`));
    }
    pyodide.runPython(GLUE);
    const convertForPage = pyodide.globals.get("convert_for_page");

    /**
     * Convert one PDF.  Synchronous: the worker is busy until it returns.
     *
     * @param {Uint8Array} pdfBytes
     * @param {string} fileName  the PDF's file name; the project is named after it
     * @param {{kicadRcl?: boolean, inferFootprints?: boolean, noGraphics?: boolean,
     *          paper?: string, flavor?: string}} options
     * @param {(page: number, pages: number) => void} [onProgress]
     * @returns {{ok: true, files: Map<string, Uint8Array>, summary: object}
     *           | {ok: false, message: string, unexpected?: true}}
     */
    function convert(pdfBytes, fileName, options, onProgress) {
        let reply;
        try {
            reply = JSON.parse(convertForPage(
                pdfBytes,
                fileName,
                JSON.stringify(options ?? {}),
                (page, pages) => onProgress?.(page, pages),
            ));
        } catch (error) {
            // Not a refusal: the interpreter itself may be unusable now.
            return {
                ok: false,
                unexpected: true,
                message: `The converter failed unexpectedly (${lastLine(error.message)}).`,
            };
        }
        if (!reply.ok) return reply;
        const files = new Map();
        for (const [name, text] of Object.entries(reply.files)) {
            files.set(name, encoder.encode(text));
        }
        return { ok: true, files, summary: reply.summary };
    }

    return { convert };
}
