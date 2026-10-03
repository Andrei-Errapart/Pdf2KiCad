// Copyright (C) 2026 Andrei Errapart
// SPDX-License-Identifier: GPL-2.0-or-later
//
// web/session.js: which request is current.  The identity of a request
// covers reading the file and waiting for start-up as well as the worker's
// part, so Cancel works at every stage and the latest selection wins.

import assert from "node:assert/strict";
import { test } from "node:test";
import { createSession } from "../../web/session.js";

/** A File stand-in whose read finishes when the test says so. */
function fakeFile(name, size = 10) {
    let resolve;
    let reject;
    const read = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return {
        name,
        size,
        arrayBuffer: () => read,
        finishRead: () => resolve(new TextEncoder().encode(name).buffer),
        failRead: (message) => reject(new Error(message)),
    };
}

function harness({ ready = Promise.resolve() } = {}) {
    const h = { posts: [], shown: [], restarts: 0, options: {} };
    const show = (kind) => (...args) => h.shown.push([kind, ...args]);
    h.session = createSession({
        maxBytes: 100,
        ready,
        readOptions: () => ({ ...h.options }),
        post: (message) => h.posts.push(message),
        restartWorker: () => { h.restarts += 1; },
        view: {
            busy: show("busy"),
            progress: show("progress"),
            result: show("result"),
            error: show("error"),
            cancelled: show("cancelled"),
        },
    });
    h.last = () => h.shown[h.shown.length - 1];
    h.names = () => h.posts.map((message) => message.fileName);
    return h;
}

// Lets every promise continuation that is already runnable run.
const settle = () => new Promise((resolve) => setImmediate(resolve));

/** Select a file and let its read and the post complete. */
async function load(h, name) {
    const file = fakeFile(name);
    h.session.select(file);
    file.finishRead();
    await settle();
    return h.posts[h.posts.length - 1];
}

test("a selected file is read and posted to the worker with the options", async () => {
    const h = harness();
    h.options = { kicadRcl: true };
    const request = await load(h, "a.pdf");

    assert.equal(h.posts.length, 1);
    assert.equal(request.type, "convert");
    assert.equal(request.fileName, "a.pdf");
    assert.deepEqual(request.options, { kicadRcl: true });
    assert.equal(new TextDecoder().decode(request.bytes), "a.pdf");
    assert.equal(h.restarts, 0);
});

test("Cancel during a file read posts nothing when the read resolves", async () => {
    const h = harness();
    const file = fakeFile("a.pdf");
    h.session.select(file);
    h.session.cancel();
    file.finishRead();
    await settle();

    assert.deepEqual(h.posts, []);
    assert.deepEqual(h.last(), ["cancelled"]);
    // Nothing was loaded either: an option change has no file to convert.
    h.session.optionsChanged();
    await settle();
    assert.deepEqual(h.posts, []);
});

test("of two selections the later one is converted, whichever read finishes first", async () => {
    const h = harness();
    const a = fakeFile("a.pdf");
    const b = fakeFile("b.pdf");
    h.session.select(a);
    h.session.select(b);
    b.finishRead();
    await settle();
    a.finishRead();
    await settle();
    assert.deepEqual(h.names(), ["b.pdf"]);

    // The loaded file is B, not the A whose read finished last.
    h.session.optionsChanged();
    await settle();
    assert.deepEqual(h.names(), ["b.pdf", "b.pdf"]);
});

test("a superseded read that fails says nothing", async () => {
    const h = harness();
    const a = fakeFile("a.pdf");
    h.session.select(a);
    await load(h, "b.pdf");
    const shownBefore = h.shown.length;
    a.failRead("disk went away");
    await settle();
    assert.equal(h.shown.length, shownBefore);
});

test("a failed read of the current file is reported", async () => {
    const h = harness();
    const a = fakeFile("a.pdf");
    h.session.select(a);
    a.failRead("disk went away");
    await settle();
    assert.deepEqual(h.posts, []);
    assert.equal(h.last()[0], "error");
    assert.match(h.last()[1], /“a\.pdf” could not be read \(disk went away\)/);
});

test("a file over the size limit is refused without being read", async () => {
    const h = harness();
    const big = fakeFile("big.pdf", 101);
    big.arrayBuffer = () => assert.fail("read a file that is too large");
    h.session.select(big);
    await settle();
    assert.deepEqual(h.posts, []);
    assert.equal(h.last()[0], "error");
    assert.match(h.last()[1], /“big\.pdf” is larger than/);
});

test("a message from a superseded request is ignored", async () => {
    const h = harness();
    const first = await load(h, "a.pdf");
    const second = await load(h, "b.pdf");
    const shownBefore = h.shown.length;

    h.session.onWorkerMessage({ id: first.id, stage: "converting", page: 1, pages: 2 });
    h.session.onWorkerMessage({ id: first.id, ok: true, files: [], summary: {} });
    h.session.onWorkerMessage({ id: first.id, ok: false, message: "late" });
    assert.equal(h.shown.length, shownBefore);

    h.session.onWorkerMessage({ id: second.id, stage: "converting", page: 1, pages: 2 });
    assert.deepEqual(h.last(), ["progress", 1, 2]);
    const reply = { id: second.id, ok: true, files: [], summary: {} };
    h.session.onWorkerMessage(reply);
    assert.deepEqual(h.last(), ["result", reply, "b.pdf"]);
});

test("a new selection stops the worker that is still converting the old one", async () => {
    const h = harness();
    await load(h, "a.pdf");
    assert.equal(h.restarts, 0);
    await load(h, "b.pdf");
    assert.equal(h.restarts, 1);
});

test("a finished conversion leaves the worker alone on the next request", async () => {
    const h = harness();
    const request = await load(h, "a.pdf");
    h.session.onWorkerMessage({ id: request.id, ok: false, message: "refused" });
    assert.deepEqual(h.last(), ["error", "refused"]);
    await load(h, "b.pdf");
    assert.equal(h.restarts, 0);
});

test("Cancel during conversion restarts the worker and drops its reply", async () => {
    const h = harness();
    const request = await load(h, "a.pdf");
    h.session.cancel();
    assert.equal(h.restarts, 1);
    assert.deepEqual(h.last(), ["cancelled"]);

    h.session.onWorkerMessage({ id: request.id, ok: true, files: [], summary: {} });
    assert.deepEqual(h.last(), ["cancelled"]);
    // Cancel with nothing running does not restart the worker again.
    h.session.cancel();
    assert.equal(h.restarts, 1);
});

test("an option change converts the loaded file again as a new request", async () => {
    const h = harness();
    const first = await load(h, "a.pdf");
    h.session.onWorkerMessage({ id: first.id, ok: true, files: [], summary: {} });
    h.options = { noGraphics: true };
    h.session.optionsChanged();
    await settle();

    assert.deepEqual(h.names(), ["a.pdf", "a.pdf"]);
    assert.notEqual(h.posts[1].id, first.id);
    assert.deepEqual(h.posts[1].options, { noGraphics: true });
});

test("an option change during a pending read starts nothing, and the read picks it up", async () => {
    const h = harness();
    await load(h, "a.pdf");
    const b = fakeFile("b.pdf");
    h.session.select(b);
    h.options = { paper: "A3" };
    h.session.optionsChanged();
    await settle();
    assert.deepEqual(h.names(), ["a.pdf"]);

    b.finishRead();
    await settle();
    assert.deepEqual(h.names(), ["a.pdf", "b.pdf"]);
    assert.deepEqual(h.posts[1].options, { paper: "A3" });
});

test("an option change with no file loaded does nothing", async () => {
    const h = harness();
    h.session.optionsChanged();
    await settle();
    assert.deepEqual(h.posts, []);
    assert.deepEqual(h.shown, []);
});

test("Cancel while waiting for start-up posts nothing once the page is ready", async () => {
    let becomeReady;
    const h = harness({ ready: new Promise((resolve) => { becomeReady = resolve; }) });
    const file = fakeFile("a.pdf");
    h.session.select(file);
    file.finishRead();
    await settle();
    assert.deepEqual(h.posts, []);

    h.session.cancel();
    becomeReady();
    await settle();
    assert.deepEqual(h.posts, []);
    assert.equal(h.restarts, 0);
});

test("a worker failure ends the current request", async () => {
    const h = harness();
    const request = await load(h, "a.pdf");
    h.session.workerFailed("out of memory");
    assert.deepEqual(h.last(), ["error", "out of memory"]);
    h.session.onWorkerMessage({ id: request.id, ok: true, files: [], summary: {} });
    assert.deepEqual(h.last(), ["error", "out of memory"]);
    // The failed worker is not "still converting": the next request does not
    // restart it a second time.
    await load(h, "b.pdf");
    assert.equal(h.restarts, 0);
});
