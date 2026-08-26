"use strict";

// Node's built-in test runner and assertions only - no framework, no
// dependency install, matching this project's other chatbot test
// files. Run with:
//   node --test wordpress/le-global-chatbot/tests/chatbot-cancellation.test.js
//
// GATE S9B: covers cancelActiveStream() - the explicit, best-effort
// POST /chat/stream/cancel call, independent of passive connection_
// aborted()-based disconnect detection (found unreliable under real
// Apache/mod_php - see the S9-LITE report). What is NOT covered here,
// by design and matching this project's own established convention:
// startNewConversation()'s own DOM-wired wiring (reading
// activeStreamRequestId, deciding whether to call cancelActiveStream
// at all) - covered by `node --check` plus direct source review
// instead, same as submitChatRequest's own retry wiring before it.

const assert = require("node:assert/strict");
const path = require("node:path");
const { test, beforeEach, afterEach } = require("node:test");

const CHATBOT_JS_PATH = path.join(
    __dirname,
    "..",
    "assets",
    "chatbot.js"
);

global.document = { querySelectorAll: () => [] };

let chatbot;
const originalFetch = global.fetch;

beforeEach(() => {
    global.window = { sessionStorage: null };
    chatbot = require(CHATBOT_JS_PATH);
});

afterEach(() => {
    global.fetch = originalFetch;
});

test("cancelActiveStream: POSTs the request_id as JSON to <chatStreamEndpoint>/cancel", () => {
    const calls = [];

    global.fetch = (url, options) => {
        calls.push({ url, options });
        return Promise.resolve(
            new Response(
                JSON.stringify({ cancelled: true }),
                { status: 200 }
            )
        );
    };

    chatbot.cancelActiveStream(
        "https://example.test/wp-json/le-global-chatbot/v1/chat/stream",
        "req-123"
    );

    assert.equal(calls.length, 1);
    assert.equal(
        calls[0].url,
        "https://example.test/wp-json/le-global-chatbot/v1/chat/stream/cancel"
    );
    assert.equal(calls[0].options.method, "POST");
    assert.equal(
        calls[0].options.headers["Content-Type"], "application/json"
    );
    assert.equal(calls[0].options.credentials, "same-origin");
    assert.equal(
        calls[0].options.body,
        JSON.stringify({ request_id: "req-123" })
    );
});

test("cancelActiveStream: never invents an identifier - a falsy request_id sends nothing", () => {
    let called = false;

    global.fetch = () => {
        called = true;
        return Promise.resolve(new Response("{}", { status: 200 }));
    };

    chatbot.cancelActiveStream(
        "https://example.test/wp-json/le-global-chatbot/v1/chat/stream",
        null
    );
    chatbot.cancelActiveStream(
        "https://example.test/wp-json/le-global-chatbot/v1/chat/stream",
        ""
    );
    chatbot.cancelActiveStream(
        "https://example.test/wp-json/le-global-chatbot/v1/chat/stream",
        undefined
    );

    assert.equal(called, false);
});

test("cancelActiveStream: a missing/empty chatStreamEndpoint sends nothing (feature effectively off)", () => {
    let called = false;

    global.fetch = () => {
        called = true;
        return Promise.resolve(new Response("{}", { status: 200 }));
    };

    chatbot.cancelActiveStream("", "req-123");
    chatbot.cancelActiveStream(null, "req-123");

    assert.equal(called, false);
});

test("cancelActiveStream: is fire-and-forget - it does not return a Promise the caller could accidentally await/block on", () => {
    global.fetch = () => Promise.resolve(
        new Response("{}", { status: 200 })
    );

    const result = chatbot.cancelActiveStream(
        "https://example.test/wp-json/le-global-chatbot/v1/chat/stream",
        "req-123"
    );

    assert.equal(result, undefined);
});

test("cancelActiveStream: a rejected fetch (network failure) never throws or produces an unhandled rejection", async () => {
    global.fetch = () => Promise.reject(new TypeError("network error"));

    assert.doesNotThrow(
        () => chatbot.cancelActiveStream(
            "https://example.test/wp-json/le-global-chatbot/v1/chat/stream",
            "req-123"
        )
    );

    // Let the rejected fetch promise's own .catch() actually run -
    // if cancelActiveStream failed to attach one, Node would report
    // an unhandled rejection for this test.
    await new Promise((resolve) => setTimeout(resolve, 10));
});

test("cancelActiveStream: a synchronously-throwing fetch is also swallowed, never propagated to the caller", () => {
    global.fetch = () => {
        throw new TypeError("synchronous failure");
    };

    assert.doesNotThrow(
        () => chatbot.cancelActiveStream(
            "https://example.test/wp-json/le-global-chatbot/v1/chat/stream",
            "req-123"
        )
    );
});

test("cancelActiveStream: never sends any API key or credential header - only the plain request_id body", () => {
    let capturedHeaders = null;

    global.fetch = (url, options) => {
        capturedHeaders = options.headers;
        return Promise.resolve(new Response("{}", { status: 200 }));
    };

    chatbot.cancelActiveStream(
        "https://example.test/wp-json/le-global-chatbot/v1/chat/stream",
        "req-123"
    );

    const headerNames = Object.keys(capturedHeaders).map(
        (name) => name.toLowerCase()
    );

    assert.deepEqual(headerNames, ["content-type"]);
});
