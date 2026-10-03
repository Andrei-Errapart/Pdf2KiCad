// Copyright (C) 2026 Andrei Errapart
// SPDX-License-Identifier: GPL-2.0-or-later
//
// The conversion worker.  Everything that parses the visitor's file runs
// here -- ZIP extraction and the converter -- so the page stays responsive
// and terminating this worker stops either.
//
// app.js starts it from a blob: URL that imports this module, so that the
// worker inherits the page's Content-Security-Policy; see app.js.
//
// Messages in:  {type: "warm", query}
//               {type: "convert", id, query, fileName, bytes, options}
// Messages out: {type: "runtime", state: "loading" | "ready" | "failed", message?}
//               {id, stage: "converting", page, pages}
//               {id, ok: true, projectName, member, files: [[name, bytes]...], summary}
//               {id, ok: false, message, fatal?}
//
// `fatal` marks a failure after which this worker should be replaced: the
// interpreter raised something other than a refusal and may be unusable.
//
// `query` is appended to the URLs of the Python sources, to bust caches
// when a new build is deployed.

import { prepareInput } from "./input.js";
import { createRunner } from "./runner.js";

let runner = null;

/** Start the interpreter once; a failed start is retried by the next request. */
function startRunner(query) {
    if (!runner) {
        self.postMessage({ type: "runtime", state: "loading" });
        const starting = createRunner({
            runtimeUrl: new URL("runtime/", import.meta.url).href,
            pyUrl: new URL("py/", import.meta.url).href,
            query,
        });
        runner = starting;
        starting.then(
            () => self.postMessage({ type: "runtime", state: "ready" }),
            (error) => {
                if (runner === starting) runner = null;
                self.postMessage({ type: "runtime", state: "failed", message: error.message });
            },
        );
    }
    return runner;
}

self.addEventListener("message", async ({ data }) => {
    if (data.type === "warm") {
        startRunner(data.query).catch(() => {});
        return;
    }
    const { id, query, fileName, bytes, options } = data;
    try {
        const input = prepareInput(new Uint8Array(bytes), fileName);
        if (!input.ok) {
            self.postMessage({ id, ok: false, message: input.message });
            return;
        }
        let convert;
        try {
            ({ convert } = await startRunner(query));
        } catch (error) {
            self.postMessage({
                id, ok: false, message: `The converter could not be loaded (${error.message}).`,
            });
            return;
        }
        const result = convert(input.pdfBytes, input.pdfName, options, (page, pages) => {
            self.postMessage({ id, stage: "converting", page, pages });
        });
        if (!result.ok) {
            self.postMessage({
                id, ok: false, message: result.message, fatal: result.unexpected === true,
            });
            return;
        }
        const files = [...result.files];
        self.postMessage(
            {
                id,
                ok: true,
                projectName: result.summary.project,
                member: input.member,
                files,
                summary: result.summary,
            },
            files.map(([, fileBytes]) => fileBytes.buffer),
        );
    } catch (error) {
        self.postMessage({
            id, ok: false, message: `Conversion failed: ${error.message}.`, fatal: true,
        });
    }
});
