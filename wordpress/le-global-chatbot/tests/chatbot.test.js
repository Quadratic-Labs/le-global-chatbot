"use strict";

// Node's built-in test runner and assertions only - no framework, no
// dependency install. Run with:
//   node --test wordpress/le-global-chatbot/tests/chatbot.test.js
//
// chatbot.js is a browser IIFE with no module system of its own; its
// tail adds a test-only `module.exports` hook (skipped entirely in a
// real browser, where `module` is never defined) that exposes only
// the pure, DOM-free functions this file exercises. The DOM-wired
// widget internals (initializeWidget's own event handlers - reading
// widget.dataset, constructing sendChatRequest/submitChatRequest, the
// requestInFlight/AbortController plumbing) are not reachable this
// way and are instead covered by `node --check` plus direct source
// review - see the mission report for that scoping decision. The
// exactly-once retry-on-invalid-conversation_state rule itself is
// NOT in that untested category: GATE S7-LITE extracted it into
// performChatTransportRequest(), which IS exercised directly - see
// chatbot-stream.test.js.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test, beforeEach } = require("node:test");

const CHATBOT_JS_PATH = path.join(
    __dirname,
    "..",
    "assets",
    "chatbot.js"
);

function createFakeSessionStorage() {
    const store = new Map();

    return {
        getItem(key) {
            return store.has(key) ? store.get(key) : null;
        },
        setItem(key, value) {
            store.set(key, String(value));
        },
        removeItem(key) {
            store.delete(key);
        },
    };
}

// document.querySelectorAll runs once, at the top level of the IIFE,
// when the module is first required - it must exist before that
// first require. window.sessionStorage is read fresh on every call,
// so reassigning global.window in beforeEach gives each test its own
// isolated storage without needing to bust Node's module cache.
global.document = { querySelectorAll: () => [] };

let chatbot;
let fakeSessionStorage;

beforeEach(() => {
    fakeSessionStorage = createFakeSessionStorage();
    global.window = { sessionStorage: fakeSessionStorage };
    chatbot = require(CHATBOT_JS_PATH);
});

function successTurn(overrides) {
    return Object.assign(
        {
            id: 1,
            question: "Can an employee work remotely in Spain?",
            status: "success",
            answer: "Yes, subject to a written agreement. [1]",
            sources: [
                {
                    citation: 1,
                    country: "Spain",
                    section: "Remote Work",
                },
            ],
            hasDisclaimer: true,
            errorMessage: null,
        },
        overrides
    );
}

test("saveConversation/loadStoredConversation round-trip messages and conversationState", () => {
    const conversationState = {
        version: 1,
        actions: [
            {
                type: "legal_information",
                country_codes: ["ES"],
                legal_topics: ["Remote Work"],
            },
        ],
        focus_action_index: 0,
        ordered_country_codes: [],
        pending_clarification: null,
    };

    chatbot.saveConversation(
        [successTurn({})],
        20,
        conversationState
    );

    const raw = JSON.parse(
        fakeSessionStorage.getItem(
            chatbot.CONVERSATION_STORAGE_KEY
        )
    );

    assert.equal(raw.version, chatbot.CONVERSATION_STORAGE_VERSION);

    const restored = chatbot.loadStoredConversation(20);

    assert.equal(restored.messages.length, 2);
    assert.equal(restored.messages[0].role, "user");
    assert.equal(restored.messages[1].role, "assistant");
    assert.deepEqual(restored.conversationState, conversationState);
});

test("hasDisclaimer is carried per-message, not globally", () => {
    const turns = [
        successTurn({
            id: 1,
            question: "What is the notice period in France?",
            hasDisclaimer: true,
        }),
        successTurn({
            id: 2,
            question: "And in Germany?",
            hasDisclaimer: false,
        }),
    ];

    chatbot.saveConversation(turns, 20, null);

    const restored = chatbot.loadStoredConversation(20);
    const rebuilt = chatbot.rebuildTurnsFromMessages(
        restored.messages,
        1
    );

    assert.equal(rebuilt.turns.length, 2);
    assert.equal(rebuilt.turns[0].hasDisclaimer, true);
    assert.equal(rebuilt.turns[1].hasDisclaimer, false);
});

test("a turn without hasDisclaimer defaults to false rather than throwing", () => {
    const turn = successTurn({});
    delete turn.hasDisclaimer;

    chatbot.saveConversation([turn], 20, null);

    const restored = chatbot.loadStoredConversation(20);

    assert.equal(restored.messages[1].hasDisclaimer, false);
});

test("only success turns are persisted - pending and error turns are dropped", () => {
    const turns = [
        successTurn({ id: 1, question: "First question" }),
        {
            id: 2,
            question: "Still pending",
            status: "pending",
            answer: null,
            sources: [],
            hasDisclaimer: false,
            errorMessage: null,
        },
        {
            id: 3,
            question: "Failed question",
            status: "error",
            answer: null,
            sources: [],
            hasDisclaimer: false,
            errorMessage: "boom",
        },
    ];

    chatbot.saveConversation(turns, 20, null);

    const restored = chatbot.loadStoredConversation(20);

    assert.equal(restored.messages.length, 2);
    assert.equal(restored.messages[0].content, "First question");
});

test("a version 1 (pre-conversationState) payload is discarded, not misread", () => {
    fakeSessionStorage.setItem(
        chatbot.CONVERSATION_STORAGE_KEY,
        JSON.stringify({
            version: 1,
            messages: [
                { role: "user", content: "Old format question" },
                { role: "assistant", content: "Old format answer" },
            ],
        })
    );

    const restored = chatbot.loadStoredConversation(20);

    assert.deepEqual(restored.messages, []);
    assert.equal(restored.conversationState, null);
    assert.equal(
        fakeSessionStorage.getItem(chatbot.CONVERSATION_STORAGE_KEY),
        null
    );
});

test("corrupted JSON is discarded and the storage key is cleared", () => {
    fakeSessionStorage.setItem(
        chatbot.CONVERSATION_STORAGE_KEY,
        "not valid json {"
    );

    const restored = chatbot.loadStoredConversation(20);

    assert.deepEqual(restored.messages, []);
    assert.equal(restored.conversationState, null);
    assert.equal(
        fakeSessionStorage.getItem(chatbot.CONVERSATION_STORAGE_KEY),
        null
    );
});

test("a non-object top-level payload (e.g. a bare array) is discarded", () => {
    fakeSessionStorage.setItem(
        chatbot.CONVERSATION_STORAGE_KEY,
        JSON.stringify([1, 2, 3])
    );

    const restored = chatbot.loadStoredConversation(20);

    assert.deepEqual(restored.messages, []);
    assert.equal(restored.conversationState, null);
});

test("a malformed nested conversationState degrades to null without losing the messages", () => {
    fakeSessionStorage.setItem(
        chatbot.CONVERSATION_STORAGE_KEY,
        JSON.stringify({
            version: chatbot.CONVERSATION_STORAGE_VERSION,
            messages: [
                { role: "user", content: "Question" },
                {
                    role: "assistant",
                    content: "Answer",
                    hasDisclaimer: true,
                },
            ],
            conversationState: "not an object",
        })
    );

    const restored = chatbot.loadStoredConversation(20);

    assert.equal(restored.messages.length, 2);
    assert.equal(restored.conversationState, null);
});

test("clearStoredConversation wipes a previously saved conversation entirely", () => {
    chatbot.saveConversation([successTurn({})], 20, { any: "state" });

    assert.notEqual(
        fakeSessionStorage.getItem(chatbot.CONVERSATION_STORAGE_KEY),
        null
    );

    chatbot.clearStoredConversation();

    assert.equal(
        fakeSessionStorage.getItem(chatbot.CONVERSATION_STORAGE_KEY),
        null
    );

    const restored = chatbot.loadStoredConversation(20);
    assert.deepEqual(restored.messages, []);
    assert.equal(restored.conversationState, null);
});

test("isConversationStateValidationError is true only for a 422 naming conversation_state", () => {
    assert.equal(
        chatbot.isConversationStateValidationError({
            statusCode: 422,
            payload: {
                detail: [
                    {
                        loc: [
                            "body",
                            "conversation_state",
                            "actions",
                            0,
                            "type",
                        ],
                        msg: "Unsupported action type",
                        type: "value_error",
                    },
                ],
            },
        }),
        true
    );
});

test("isConversationStateValidationError is false for a 422 on a different field", () => {
    assert.equal(
        chatbot.isConversationStateValidationError({
            statusCode: 422,
            payload: {
                detail: [
                    {
                        loc: ["body", "question"],
                        msg: "field required",
                        type: "missing",
                    },
                ],
            },
        }),
        false
    );
});

test("isConversationStateValidationError is false for a non-422 status even naming conversation_state", () => {
    assert.equal(
        chatbot.isConversationStateValidationError({
            statusCode: 400,
            payload: {
                detail: [{ loc: ["body", "conversation_state"] }],
            },
        }),
        false
    );
});

test("isConversationStateValidationError is false for a missing or malformed payload", () => {
    assert.equal(chatbot.isConversationStateValidationError(null), false);
    assert.equal(
        chatbot.isConversationStateValidationError({ statusCode: 422 }),
        false
    );
    assert.equal(
        chatbot.isConversationStateValidationError({
            statusCode: 422,
            payload: { detail: "conversation_state is invalid" },
        }),
        false
    );
});

test("the widget never touches localStorage, cookies, or IndexedDB", () => {
    // Checks for actual API usage, not the bare words - the file's
    // own comments legitimately mention "localStorage"/"cookies"/
    // "IndexedDB" in prose precisely to document that they are never
    // used (see the CONVERSATION_STORAGE_KEY comment).
    const source = fs.readFileSync(CHATBOT_JS_PATH, "utf8");

    assert.equal(/\blocalStorage\s*\./.test(source), false);
    assert.equal(/\bdocument\.cookie\b/.test(source), false);
    assert.equal(/\bindexedDB\s*\./.test(source), false);
});
