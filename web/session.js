// Copyright (C) 2026 Andrei Errapart
// SPDX-License-Identifier: GPL-2.0-or-later
//
// Which request is current.  One request is current at a time and the most
// recent selection wins.  A request's identity covers everything
// asynchronous done for it -- reading the file, waiting for start-up,
// extraction and conversion in the worker -- so Cancel works at every stage
// and a slow step of an old request can never overtake a newer one.
//
// The rule: `generation` advances, synchronously and before any await, when
// a file is selected, when an option changes on a loaded file, and on
// Cancel.  Every asynchronous step remembers the generation it started
// under and drops its result if the generation has moved on.
//
// No DOM API is used; app.js connects this to the page.

/**
 * @param {object} page
 * @param {number} page.maxBytes  the largest file that is read at all
 * @param {Promise<void>} page.ready  resolved once the worker exists
 * @param {() => object} page.readOptions  the options as they are now
 * @param {(message: object) => void} page.post  send to the worker
 * @param {() => void} page.restartWorker  terminate the worker, start another
 * @param {{busy: Function, progress: Function, result: Function,
 *          error: Function, cancelled: Function}} page.view
 */
export function createSession({ maxBytes, ready, readOptions, post, restartWorker, view }) {
    let generation = 0;
    let loaded = null; // {name, bytes} of the file last read
    let reading = false; // the current request is still reading its file
    let inFlight = false; // the current request is with the worker

    /** Make every earlier request stale, and stop the worker if it has one. */
    function supersede() {
        generation += 1;
        reading = false;
        if (inFlight) {
            restartWorker();
            inFlight = false;
        }
        return generation;
    }

    async function select(file) {
        if (!file) return;
        const mine = supersede();
        if (file.size > maxBytes) {
            view.error(
                `“${file.name}” is larger than ${Math.round(maxBytes / (1024 * 1024))} MB, `
                + "the most this page converts.",
            );
            return;
        }
        reading = true;
        view.busy(`Reading ${file.name}…`);
        let bytes;
        try {
            bytes = await file.arrayBuffer();
        } catch (error) {
            if (mine !== generation) return;
            reading = false;
            view.error(`“${file.name}” could not be read (${error.message}).`);
            return;
        }
        if (mine !== generation) return;
        reading = false;
        loaded = { name: file.name, bytes };
        await convert(mine);
    }

    async function convert(mine) {
        view.busy(`Converting ${loaded.name}…`);
        // The worker starts once the build's version is known; a file chosen
        // before then waits here.
        await ready;
        if (mine !== generation) return;
        inFlight = true;
        post({
            type: "convert",
            id: mine,
            fileName: loaded.name,
            // Copied by postMessage, not transferred: the page keeps the
            // original so that an option change can convert it again.
            bytes: loaded.bytes,
            // Read now, not when the file was chosen.
            options: readOptions(),
        });
    }

    function optionsChanged() {
        // A pending read will post with the new options when it completes.
        if (!loaded || reading) return;
        convert(supersede());
    }

    function cancel() {
        supersede();
        view.cancelled();
    }

    function onWorkerMessage(data) {
        if (data.id !== generation) return;
        if (data.stage) {
            view.progress(data.page, data.pages);
            return;
        }
        inFlight = false;
        if (data.ok) view.result(data, loaded.name);
        else view.error(data.message);
    }

    /** The worker died; app.js has already started another. */
    function workerFailed(message) {
        inFlight = false;
        supersede();
        view.error(message);
    }

    return { select, optionsChanged, cancel, onWorkerMessage, workerFailed };
}
