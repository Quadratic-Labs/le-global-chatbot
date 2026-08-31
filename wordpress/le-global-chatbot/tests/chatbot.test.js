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

const CHATBOT_CSS_PATH = path.join(
    __dirname,
    "..",
    "assets",
    "chatbot.css"
);

const CHATBOT_PHP_PATH = path.join(
    __dirname,
    "..",
    "le-global-chatbot.php"
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

test("missing-evidence comparison keeps its answer visible above all contact cards", () => {
    const contacts = [
        {
            contact_id: "contact-caroline",
            country_code: "FR",
            contact_person: "Caroline Scherrmann",
        },
        {
            contact_id: "contact-florence",
            country_code: "FR",
            contact_person: "Florence Bacquet",
        },
        {
            contact_id: "contact-robert",
            country_code: "GB",
            contact_person: "Robert Hill",
        },
    ];
    const sources = [
        { country_code: "FR", subsection: "Contact" },
        { country_code: "GB", subsection: "Contact" },
    ];
    const response = {
        answer: "Reliable remote-work information is unavailable.",
        contacts,
        sources,
        conversation_state: {
            actions: [{ type: "comparison" }],
        },
    };

    const contactOnly = chatbot.isContactOnlyResponse(response);

    assert.equal(contactOnly, false);
    assert.notEqual(response.answer, "");
    assert.equal(response.contacts.length, 3);

    chatbot.saveConversation(
        [
            successTurn({
                answer: response.answer,
                contacts,
                sources,
                contactOnly,
            }),
        ],
        20,
        response.conversation_state
    );

    const restored = chatbot.loadStoredConversation(20);
    const rebuilt = chatbot.rebuildTurnsFromMessages(
        restored.messages,
        1
    );

    assert.equal(rebuilt.turns[0].contactOnly, false);
    assert.equal(rebuilt.turns[0].contacts.length, 3);
    assert.equal(
        rebuilt.turns[0].answer,
        "Reliable remote-work information is unavailable."
    );
});

test("direct contact query remains contact-card-only", () => {
    const response = {
        answer: "United Kingdom\nRobert Hill",
        contacts: [
            {
                contact_id: "contact-robert",
                country_code: "GB",
                contact_person: "Robert Hill",
            },
        ],
        sources: [
            { country_code: "GB", subsection: "Contact" },
        ],
        conversation_state: {
            actions: [{ type: "contact" }],
        },
    };

    assert.equal(
        chatbot.isContactOnlyResponse(response),
        true
    );

    const legacyResponse = { ...response };
    delete legacyResponse.conversation_state;

    assert.equal(
        chatbot.isContactOnlyResponse(legacyResponse),
        true
    );
});

test("out-of-scope explanation with a country contact overrides contact-only via contact_only:false", () => {
    const response = {
        answer: (
            "This assistant can only answer employment law "
            + "questions, and related L&E Global contacts, covered "
            + "by the validated documents. Please rephrase your "
            + "question within that scope, or contact our L&E "
            + "Global member firm in France for further assistance."
        ),
        contacts: [
            {
                contact_id: "contact-france",
                country_code: "FR",
                contact_person: "France Contact",
            },
        ],
        sources: [
            { country_code: "FR", subsection: "Contact" },
        ],
        contact_only: false,
        conversation_state: null,
    };

    // Same shape (contacts present, contact-only sources, no
    // conversation_state) as the legacy "direct contact query" case
    // above - only the explicit contact_only:false override tells the
    // client this text is not a duplicate of the cards and must stay
    // visible instead of being suppressed by the legacy heuristic.
    assert.equal(chatbot.isContactOnlyResponse(response), false);
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

test("question textarea starts at one line and keeps the 2000-character limit", () => {
    const markup = fs.readFileSync(CHATBOT_PHP_PATH, "utf8");
    const stylesheet = fs.readFileSync(CHATBOT_CSS_PATH, "utf8");
    const questionMarkup = markup.match(
        /<textarea[\s\S]*?class="le-global-chatbot__question"[\s\S]*?<\/textarea>/
    );
    const questionRule = stylesheet.match(
        /\.le-global-chatbot textarea\.le-global-chatbot__question\s*\{([^}]*)\}/
    );

    assert.ok(questionMarkup);
    assert.match(questionMarkup[0], /rows="1"/);
    assert.match(questionMarkup[0], /maxlength="2000"/);

    assert.ok(questionRule);
    assert.match(questionRule[1], /height:\s*3rem;/);
    assert.match(questionRule[1], /min-height:\s*3rem;/);
    assert.match(questionRule[1], /max-height:\s*10rem;/);
    assert.match(questionRule[1], /overflow-y:\s*hidden;/);
    assert.match(questionRule[1], /resize:\s*none;/);
});

test("autoResizeQuestionInput grows with content below its maximum", () => {
    const questionInput = {
        scrollHeight: 94,
        style: {},
    };

    chatbot.autoResizeQuestionInput(
        questionInput,
        () => ({
            maxHeight: "160px",
            borderTopWidth: "1px",
            borderBottomWidth: "1px",
        })
    );

    assert.equal(questionInput.style.height, "96px");
    assert.equal(questionInput.style.overflowY, "hidden");
});

test("autoResizeQuestionInput caps tall content and enables internal scrolling", () => {
    const questionInput = {
        scrollHeight: 240,
        style: {},
    };

    chatbot.autoResizeQuestionInput(
        questionInput,
        () => ({
            maxHeight: "160px",
            borderTopWidth: "1px",
            borderBottomWidth: "1px",
        })
    );

    assert.equal(questionInput.style.height, "160px");
    assert.equal(questionInput.style.overflowY, "auto");
});

test("autoResizeQuestionInput shrinks again after the textarea is cleared", () => {
    const questionInput = {
        scrollHeight: 240,
        style: {},
    };

    const computedStyle = () => ({
        maxHeight: "160px",
        borderTopWidth: "1px",
        borderBottomWidth: "1px",
    });

    chatbot.autoResizeQuestionInput(
        questionInput,
        computedStyle
    );

    questionInput.scrollHeight = 46;
    chatbot.autoResizeQuestionInput(
        questionInput,
        computedStyle
    );

    assert.equal(questionInput.style.height, "48px");
    assert.equal(questionInput.style.overflowY, "hidden");
});

test("Send and New Conversation both reset the textarea auto-grow height", () => {
    const source = fs.readFileSync(CHATBOT_JS_PATH, "utf8");
    const resetSequence = (
        /questionInput\.value = "";\s*characterCount\.textContent = "0";\s*autoResizeQuestionInput\(questionInput\);/g
    );

    assert.equal(Array.from(source.matchAll(resetSequence)).length, 2);
});

test("visitor display removes only numeric citation groups and preserves the raw answer", () => {
    const rawAnswer = (
        "Notice is required [1]. Grouped support [2, 12]! "
        + "Keep [Article 1] and [2024 Act]."
    );

    assert.equal(
        chatbot.answerTextForDisplay(rawAnswer),
        "Notice is required. Grouped support! Keep [Article 1] and [2024 Act]."
    );
    assert.equal(
        rawAnswer,
        "Notice is required [1]. Grouped support [2, 12]! Keep [Article 1] and [2024 Act]."
    );
});

test("visitor display suppresses only a trailing partial citation while streaming", () => {
    assert.equal(
        chatbot.answerTextForDisplay(
            "An employer must provide notice [12",
            { streaming: true }
        ),
        "An employer must provide notice"
    );
    assert.equal(
        chatbot.answerTextForDisplay("An employer must provide notice [12"),
        "An employer must provide notice [12"
    );
});

test("the production chatbot has no visitor-facing Sources renderer or styles", () => {
    const script = fs.readFileSync(CHATBOT_JS_PATH, "utf8");
    const stylesheet = fs.readFileSync(CHATBOT_CSS_PATH, "utf8");

    assert.equal(script.includes("buildSourcesSection"), false);
    assert.equal(
        script.includes("le-global-chatbot__sources-section"),
        false
    );
    assert.equal(
        stylesheet.includes("le-global-chatbot__sources-section"),
        false
    );
});

test("the chatbot and floating launcher primary color is exactly #0d6efd", () => {
    const stylesheet = fs.readFileSync(CHATBOT_CSS_PATH, "utf8");
    const chatbotRule = stylesheet.match(
        /\.le-global-chatbot\s*\{([^}]*)\}/
    );
    const floatingRule = stylesheet.match(
        /\.le-global-chatbot-floating\s*\{([^}]*)\}/
    );

    assert.ok(chatbotRule);
    assert.ok(floatingRule);
    assert.match(
        chatbotRule[1],
        /--le-global-primary:\s*#0d6efd;/
    );
    assert.match(
        floatingRule[1],
        /--le-global-primary:\s*#0d6efd;/
    );
});
