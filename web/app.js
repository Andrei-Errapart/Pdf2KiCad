// Copyright (C) 2026 Andrei Errapart
// SPDX-License-Identifier: GPL-2.0-or-later
//
// Page wiring: take a file from drop, picker or paste, hand it to the
// session, show what the conversion worker reports and offer the download.
// The visitor's file is never sent anywhere: it goes from the browser's File
// API to the worker and back as converted files, all in this tab.
//
// Which request is current, and what Cancel means at each stage, is decided
// in session.js; this module only connects it to the DOM and the worker.

import { archiveName, buildProjectArchive } from "./archive.js";
import { MAX_INPUT_BYTES } from "./input.js";
import { showPreview } from "./preview.js";
import { createSession } from "./session.js";
import { renderSummary } from "./summary.js";

const REPOSITORY = "https://github.com/Andrei-Errapart/Pdf2KiCad";

const $ = (id) => document.getElementById(id);
const ui = {
    drop: $("drop"),
    picker: $("picker"),
    options: $("options"),
    runtime: $("runtime"),
    status: $("status"),
    statusText: $("status-text"),
    cancel: $("cancel"),
    result: $("result"),
    resultName: $("result-name"),
    resultDetail: $("result-detail"),
    download: $("download"),
    summaryHeadline: $("summary-headline"),
    summary: $("summary"),
    sheets: $("sheets"),
    viewer: $("viewer"),
    previewNote: $("preview-note"),
    version: $("version"),
};

let version = "development build";
let query = ""; // cache-busting suffix for the Python sources
let worker = null;
let workerReady = false; // this worker's interpreter has loaded
let result = null; // {projectName, files} of the last successful conversion
let busyText = "";

// --- Worker -----------------------------------------------------------------

// A worker loaded from a network URL gets its policy from that script's own
// response headers -- and GitHub Pages sends none we control -- so it would
// run without the page's Content-Security-Policy.  A worker loaded from a
// blob: URL inherits the page's policy instead; its one line imports the
// real module from this origin.
function startWorker() {
    const moduleUrl = new URL("worker.js", import.meta.url).href;
    const bootstrap = new Blob(
        [`import ${JSON.stringify(moduleUrl)};\n`],
        { type: "text/javascript" },
    );
    workerReady = false;
    worker = new Worker(URL.createObjectURL(bootstrap), { type: "module" });
    worker.addEventListener("message", onWorkerMessage);
    worker.addEventListener("error", (event) => {
        event.preventDefault();
        // A worker that had been running is replaced; one that never started
        // is not retried, or a broken deployment would restart it for ever.
        const wasReady = workerReady;
        session.workerFailed(
            `The converter stopped unexpectedly (${event.message || "worker error"}).`,
        );
        if (wasReady) restartWorker();
        else showRuntime("failed", "The converter could not start. Reload the page to try again.");
    });
    showRuntime("loading");
    worker.postMessage({ type: "warm", query });
}

function restartWorker() {
    worker?.terminate();
    startWorker();
}

function showRuntime(state, message) {
    ui.runtime.dataset.state = state;
    if (message) ui.runtime.textContent = message;
}

function onWorkerMessage({ data }) {
    if (data.type === "runtime") {
        workerReady = data.state === "ready";
        showRuntime(
            data.state,
            data.state === "failed"
                ? `The converter could not be loaded (${data.message}). Reload the page to try again.`
                : undefined,
        );
        return;
    }
    session.onWorkerMessage(data);
    // The interpreter raised something other than a refusal and may be
    // unusable; the next request gets a fresh one.
    if (data.fatal) restartWorker();
}

// --- Status and result --------------------------------------------------------

function setBusy(text) {
    busyText = text;
    ui.status.dataset.state = "busy";
    ui.statusText.textContent = text;
    ui.cancel.hidden = false;
}

function showError(message) {
    ui.status.dataset.state = "error";
    ui.statusText.textContent = message;
    ui.cancel.hidden = true;
    // The download on show would be of an earlier file.
    result = null;
    ui.result.hidden = true;
}

function showIdle(message = "") {
    ui.status.dataset.state = message ? "info" : "idle";
    ui.statusText.textContent = message;
    ui.cancel.hidden = true;
}

function readOptions() {
    const form = new FormData(ui.options);
    return {
        kicadRcl: form.has("kicadRcl"),
        inferFootprints: form.has("inferFootprints"),
        noGraphics: form.has("noGraphics"),
        paper: form.get("paper") || "",
        flavor: form.get("flavor") || "",
    };
}

function showResult(data) {
    result = { projectName: data.projectName, files: new Map(data.files) };
    const { projectName, files } = result;
    showIdle();
    ui.result.hidden = false;
    ui.resultName.textContent = projectName;
    const sheetCount = data.summary.pages.length;
    ui.resultDetail.textContent =
        `${sheetCount} sheet${sheetCount === 1 ? "" : "s"}, ${files.size} files`
        + (data.member ? ` · from ${data.member}` : "");
    ui.download.textContent = `Download ${archiveName(projectName)}`;
    renderSummary({ headline: ui.summaryHeadline, table: ui.summary }, data.summary);
    ui.previewNote.hidden = true;
    showPreview(
        {
            list: ui.sheets,
            viewer: ui.viewer,
            onError: (message) => {
                ui.previewNote.textContent = message;
                ui.previewNote.hidden = false;
            },
        },
        projectName,
        files,
    );
}

function download() {
    if (!result) return;
    const blob = new Blob(
        [buildProjectArchive(result.projectName, result.files)],
        { type: "application/zip" },
    );
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = archiveName(result.projectName);
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

// --- Session ------------------------------------------------------------------

// Event handlers are registered before start-up finishes; a file chosen
// before then waits for `started` inside the session.
const started = Promise.resolve().then(() => start());

const session = createSession({
    maxBytes: MAX_INPUT_BYTES,
    ready: started,
    readOptions,
    post: (message) => worker.postMessage({ ...message, query }),
    restartWorker,
    view: {
        busy: setBusy,
        progress: (page, pages) => {
            ui.statusText.textContent = `${busyText} page ${page} of ${pages}`;
        },
        result: showResult,
        error: showError,
        cancelled: () => showIdle("Cancelled."),
    },
});

// --- Input routes -------------------------------------------------------------

ui.picker.addEventListener("change", () => {
    session.select(ui.picker.files[0]);
    ui.picker.value = "";
});

for (const type of ["dragenter", "dragover"]) {
    document.addEventListener(type, (event) => {
        event.preventDefault();
        ui.drop.dataset.over = "true";
    });
}
for (const type of ["dragleave", "drop"]) {
    document.addEventListener(type, (event) => {
        event.preventDefault();
        if (type === "dragleave" && event.relatedTarget) return;
        ui.drop.dataset.over = "false";
    });
}
document.addEventListener("drop", (event) => {
    session.select(event.dataTransfer?.files?.[0]);
});

document.addEventListener("paste", (event) => {
    const file = event.clipboardData?.files?.[0];
    if (file) {
        event.preventDefault();
        session.select(file);
    } else if (!(event.target instanceof HTMLInputElement)) {
        showIdle(
            "The clipboard holds no file. Copy the PDF or .zip itself "
            + "(not its contents), or drop or choose it instead.",
        );
    }
});

ui.options.addEventListener("change", () => session.optionsChanged());
ui.cancel.addEventListener("click", () => session.cancel());
ui.download.addEventListener("click", download);

// --- Start-up -----------------------------------------------------------------

// The deploy workflow writes the commit into version.json; the commit also
// busts caches of the Python sources.  Never rejects.
async function start() {
    try {
        const response = await fetch(new URL("version.json", import.meta.url));
        if (response.ok) {
            const info = await response.json();
            if (info.commit) {
                version = info.commit;
                query = `?v=${encodeURIComponent(info.commit)}`;
            }
        }
    } catch {
        // Local development: no version.json.
    }
    ui.version.textContent = version;
    if (version !== "development build") {
        ui.version.href = `${REPOSITORY}/tree/${version}`;
    }
    startWorker();
}
