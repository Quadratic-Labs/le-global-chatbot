"use strict";

// Node's built-in test runner and assertions only - no framework, no
// dependency install, matching chatbot.test.js/chatbot-stream.test.js's
// own convention. Run with:
//   node --test wordpress/le-global-chatbot/tests/chatbot-progressive-render.test.js
//
// GATE S8-LITE: covers createStreamingBubbleController (the progressive
// /chat/stream UI added to chatbot.js) and findPendingBubbleElement.
// Both are DOM-manipulation functions, but every DOM/scheduling
// dependency is explicitly injected (bubbleElement/conversationElement/
// documentRef/scheduleWork), so a minimal hand-rolled fake element
// (no jsdom, no real browser) is enough to exercise them precisely -
// no full initializeWidget/DOM harness needed. What is NOT covered
// here, by design and matching the same scoping decision the other two
// test files already document: initializeWidget's own wiring (reading
// chatTransport, constructing the real controller with the real
// messageListElement/conversationElement, passing
// handleStreamProtocolEvent into requestStream) - covered by
// `node --check` plus direct source review instead.

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

/**
 * A minimal fake DOM element - just enough of className/classList/
 * textContent/hidden/appendChild/querySelector/lastElementChild for
 * createStreamingBubbleController and findPendingBubbleElement to
 * operate on. Deliberately NOT a full DOM emulation (no jsdom
 * dependency, matching this project's "no dependency install"
 * convention) - two properties are made faithful specifically because
 * an S8-LITE adversarial review found tests relying on unfaithful
 * versions of them gave false confidence:
 *
 *  - textContent is a live getter/setter, exactly like a real
 *    Element's: with no children it is plain own text; once any
 *    child exists, the getter recursively concatenates every
 *    descendant's own textContent (this fake never mixes a bare text
 *    value with child elements at the same node, which matches every
 *    pattern chatbot.js's own code actually uses - always
 *    `.textContent = ""` fully BEFORE any appendChild calls).
 *  - querySelector recurses into descendants, not just direct
 *    children, matching real Element.querySelector.
 */
function createFakeElement(tagName) {
    let ownText = "";

    const element = {
        tagName,
        className: "",
        hidden: false,
        children: [],
        lastElementChild: null,
        get textContent() {
            if (element.children.length === 0) {
                return ownText;
            }

            return element.children
                .map((child) => child.textContent)
                .join("");
        },
        set textContent(value) {
            ownText = value;
            element.children = [];
            element.lastElementChild = null;
        },
        appendChild(child) {
            element.children.push(child);
            element.lastElementChild = child;
            return child;
        },
        querySelector(selector) {
            const wanted = selector.replace(/^\./, "");

            function search(candidates) {
                for (const child of candidates) {
                    if (
                        (child.className || "")
                            .split(/\s+/)
                            .filter(Boolean)
                            .includes(wanted)
                    ) {
                        return child;
                    }

                    if (child.children && child.children.length > 0) {
                        const found = search(child.children);

                        if (found) {
                            return found;
                        }
                    }
                }

                return null;
            }

            return search(element.children);
        },
    };

    element.classList = {
        add(cls) {
            const parts = new Set(
                element.className.split(/\s+/).filter(Boolean)
            );
            parts.add(cls);
            element.className = Array.from(parts).join(" ");
        },
        remove(cls) {
            element.className = element.className
                .split(/\s+/)
                .filter(Boolean)
                .filter((part) => part !== cls)
                .join(" ");
        },
        contains(cls) {
            return element.className
                .split(/\s+/)
                .filter(Boolean)
                .includes(cls);
        },
    };

    return element;
}

const fakeDocument = {
    createElement: (tagName) => createFakeElement(tagName),
};

function createPendingBubble() {
    const bubble = createFakeElement("div");

    bubble.className = (
        "le-global-chatbot__message "
        + "le-global-chatbot__message--assistant "
        + "le-global-chatbot__message--pending"
    );

    bubble.textContent = "Searching validated legal documents…";

    return bubble;
}

function createConversationElement(overrides) {
    return Object.assign(
        { scrollHeight: 200, scrollTop: 200, clientHeight: 200 },
        overrides
    );
}

/** Synchronous scheduler - flushes are deterministic in tests. */
function immediateSchedule(callback) {
    callback();
}

function createController(overrides) {
    return chatbot.createStreamingBubbleController(
        Object.assign(
            {
                bubbleElement: createPendingBubble(),
                conversationElement: createConversationElement(),
                isStale: () => false,
                documentRef: fakeDocument,
                scheduleWork: immediateSchedule,
            },
            overrides
        )
    );
}

function utf8(text) {
    return new TextEncoder().encode(text);
}

function ndjsonLines(records) {
    return records.map(
        (record) => JSON.stringify(record) + "\n"
    );
}

function bytesFromLines(lines) {
    return utf8(lines.join(""));
}

function installFakeStreamFetch(bytes) {
    global.fetch = async () => {
        let offset = 0;

        const stream = new ReadableStream(
            {
                pull(controller) {
                    if (offset >= bytes.length) {
                        controller.close();
                        return;
                    }

                    controller.enqueue(bytes.slice(offset));
                    offset = bytes.length;
                },
            }
        );

        return new Response(
            stream,
            {
                status: 200,
                headers: {
                    "content-type":
                        "application/x-ndjson; charset=utf-8",
                },
            }
        );
    };
}

// =====================================================================
// 1. first delta replaces loading state
// =====================================================================

test("progressive rendering: the first delta replaces the loading state - pending class removed, the original 'Searching...' text cleared, an answer element created", () => {
    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });

    controller.handleEvent({ type: "delta", text: "In France, " });

    assert.equal(
        bubble.classList.contains(
            "le-global-chatbot__message--pending"
        ),
        false
    );

    const answerElement = bubble.querySelector(
        ".le-global-chatbot__answer"
    );

    assert.ok(answerElement);
    assert.equal(answerElement.textContent, "In France, ");

    // The original "Searching..." text is gone - the bubble's own
    // (recursive) textContent now reflects only its two real
    // children's own text (the hidden status element is empty).
    assert.equal(bubble.textContent, "In France, ");
});

// =====================================================================
// 2. multiple deltas append into one bubble
// =====================================================================

test("progressive rendering: multiple deltas append into the SAME bubble - never one DOM element per delta", () => {
    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });

    controller.handleEvent({ type: "delta", text: "In France, " });
    const answerElementAfterFirst = bubble.querySelector(
        ".le-global-chatbot__answer"
    );

    controller.handleEvent(
        { type: "delta", text: "an employer must give notice." }
    );
    const answerElementAfterSecond = bubble.querySelector(
        ".le-global-chatbot__answer"
    );

    assert.equal(
        answerElementAfterFirst,
        answerElementAfterSecond,
        "the same element must be reused, never recreated"
    );

    assert.equal(
        answerElementAfterSecond.textContent,
        "In France, an employer must give notice."
    );

    assert.equal(
        bubble.children.filter(
            (child) => child.className.includes(
                "le-global-chatbot__answer"
            )
        ).length,
        1
    );
});

test("progressive rendering: a numeric citation split across deltas is never visible", () => {
    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });

    controller.handleEvent(
        { type: "delta", text: "An employer must provide notice [" }
    );

    const answerElement = bubble.querySelector(
        ".le-global-chatbot__answer"
    );

    assert.equal(
        answerElement.textContent,
        "An employer must provide notice"
    );

    controller.handleEvent({ type: "delta", text: "12]." });

    assert.equal(
        answerElement.textContent,
        "An employer must provide notice."
    );
});

// =====================================================================
// 3. validating shows transient state
// =====================================================================

test("progressive rendering: validating keeps the provisional answer visible and shows a neutral transient status", () => {
    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });

    controller.handleEvent({ type: "delta", text: "Draft answer." });
    controller.handleEvent({ type: "validating" });

    const answerElement = bubble.querySelector(
        ".le-global-chatbot__answer"
    );
    const statusElement = bubble.querySelector(
        ".le-global-chatbot__stream-status"
    );

    assert.equal(answerElement.textContent, "Draft answer.");
    assert.equal(statusElement.hidden, false);
    assert.equal(
        statusElement.textContent,
        chatbot.STREAM_VALIDATING_STATUS_TEXT
    );

    // Never described as unsafe/hallucinated/invalid/repaired.
    assert.equal(
        /unsafe|hallucinat|invalid|repaired/i.test(
            statusElement.textContent
        ),
        false
    );
});

// =====================================================================
// 4. done removes transient state
// =====================================================================

test("progressive rendering: metadata/done/start are safe no-ops for the bubble controller - transient-state removal at settle time comes from the EXISTING, unmodified renderMessageList() full rebuild (the whole bubble is replaced once turn.status becomes success/error), never from new controller logic", () => {
    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });

    controller.handleEvent({ type: "delta", text: "Draft answer." });
    controller.handleEvent({ type: "validating" });

    const statusBefore = bubble.querySelector(
        ".le-global-chatbot__stream-status"
    );
    assert.equal(statusBefore.hidden, false);

    assert.doesNotThrow(
        () => {
            controller.handleEvent(
                {
                    type: "metadata",
                    question: "q",
                    grounded: true,
                    model: "m",
                    retrieval_total: 1,
                    sources: [],
                    contacts: [],
                    conversation_state: null,
                }
            );
            controller.handleEvent(
                { type: "done", request_id: "r" }
            );
        }
    );

    const statusAfter = bubble.querySelector(
        ".le-global-chatbot__stream-status"
    );

    // The controller itself never clears this - it is the CALLER's
    // job (via the existing renderMessageList() rebuild) once its
    // await settles. See this test's own name/docstring.
    assert.equal(statusAfter, statusBefore);
    assert.equal(statusAfter.hidden, false);
});

// =====================================================================
// 5. discard removes provisional answer
// =====================================================================

test("progressive rendering: discard clears the provisional answer immediately and shows a neutral transient status", () => {
    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });

    controller.handleEvent(
        { type: "delta", text: "Draft answer that fails validation." }
    );
    controller.handleEvent({ type: "discard" });

    const answerElement = bubble.querySelector(
        ".le-global-chatbot__answer"
    );
    const statusElement = bubble.querySelector(
        ".le-global-chatbot__stream-status"
    );

    assert.equal(answerElement.textContent, "");
    assert.equal(statusElement.hidden, false);
    assert.equal(
        statusElement.textContent,
        chatbot.STREAM_FINALIZING_STATUS_TEXT
    );
});

// =====================================================================
// 6. repair discard -> replacement
// =====================================================================

test("progressive rendering: repair sequence (discard then replacement) shows only the final replacement text atomically, never token-by-token, and clears the transient status", () => {
    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });

    controller.handleEvent(
        { type: "delta", text: "Draft answer that fails validation." }
    );
    controller.handleEvent({ type: "discard" });
    controller.handleEvent(
        { type: "replacement", text: "Corrected, validated answer [12]." }
    );

    const answerElement = bubble.querySelector(
        ".le-global-chatbot__answer"
    );
    const statusElement = bubble.querySelector(
        ".le-global-chatbot__stream-status"
    );

    assert.equal(
        answerElement.textContent,
        "Corrected, validated answer."
    );
    assert.equal(statusElement.hidden, true);
});

// =====================================================================
// 7. reconciliation replacement WITHOUT discard
// =====================================================================

test("progressive rendering: a bare replacement (no preceding discard) still atomically replaces the answer - matches the real backend's post-generation reconciliation behavior (see the S7-LITE report's disclosed deviation)", () => {
    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });

    controller.handleEvent(
        { type: "delta", text: "Streamed answer." }
    );
    controller.handleEvent(
        {
            type: "replacement",
            text: "Streamed answer [2]. Note: no data for CH.",
        }
    );

    const answerElement = bubble.querySelector(
        ".le-global-chatbot__answer"
    );

    assert.equal(
        answerElement.textContent,
        "Streamed answer. Note: no data for CH."
    );
});

// =====================================================================
// 8. metadata/sources remain internal and never alter the bubble
// =====================================================================

test("progressive rendering: metadata never touches the bubble or exposes visitor-facing sources", () => {
    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });

    controller.handleEvent({ type: "delta", text: "Answer. [1]" });

    controller.handleEvent(
        {
            type: "metadata",
            question: "q",
            grounded: true,
            model: "m",
            retrieval_total: 1,
            sources: [
                { citation: 1, country: "France", section: "A" },
            ],
            contacts: [],
            conversation_state: null,
        }
    );

    // Only the answer element and the (hidden) status element -
    // nothing source-card-shaped.
    assert.equal(bubble.children.length, 2);
    assert.equal(
        bubble.querySelector(
            ".le-global-chatbot__sources-section"
        ),
        null
    );
    assert.equal(
        bubble.querySelector(
            ".le-global-chatbot__answer"
        ).textContent,
        "Answer."
    );
});

// =====================================================================
// 9. contacts appear correctly at finalization
// =====================================================================

test("progressive rendering: contacts in metadata create no DOM during streaming - contact cards are built only by the existing final-render path once the reconstructed response is available (see chatbot-stream.test.js's own contacts-preserved-in-metadata test)", () => {
    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });

    controller.handleEvent(
        { type: "delta", text: "Contact answer." }
    );

    controller.handleEvent(
        {
            type: "metadata",
            question: "q",
            grounded: false,
            model: null,
            retrieval_total: 0,
            sources: [],
            contacts: [{ contact_id: "c1", country_code: "FR" }],
            conversation_state: null,
        }
    );

    assert.equal(bubble.children.length, 2);
    assert.equal(
        bubble.querySelector(
            ".le-global-chatbot__contact-cards"
        ),
        null
    );
});

// =====================================================================
// 10. error before delta
// =====================================================================

test("progressive rendering: an error before any delta leaves the bubble genuinely untouched - the existing normal chat error experience, not a repurposed bubble (mission section 10's explicit before/after-delta split; found and fixed via an S8-LITE adversarial review)", () => {
    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });

    assert.doesNotThrow(
        () => controller.handleEvent(
            {
                type: "error",
                code: "stream_generation_failed",
                message: "Answer generation failed.",
                retryable: false,
            }
        )
    );

    // Genuinely pristine, not merely "empty": no answer element was
    // ever created, the --pending class survives, and the bubble's
    // own original text is untouched - the controller took no action
    // at all, exactly matching "keep the existing normal chat error
    // experience" for this case.
    assert.equal(
        bubble.querySelector(".le-global-chatbot__answer"),
        null
    );
    assert.equal(
        bubble.querySelector(".le-global-chatbot__stream-status"),
        null
    );
    assert.equal(
        bubble.classList.contains(
            "le-global-chatbot__message--pending"
        ),
        true
    );
    assert.equal(
        bubble.textContent,
        "Searching validated legal documents…"
    );
});

// =====================================================================
// 11. delta -> discard -> error
// =====================================================================

test("progressive rendering: delta, then discard, then error - never leaves partial legal text presented as complete", () => {
    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });

    controller.handleEvent(
        { type: "delta", text: "Partial answer before failure." }
    );
    controller.handleEvent({ type: "discard" });
    controller.handleEvent(
        {
            type: "error",
            code: "stream_generation_failed",
            message: "Answer generation failed.",
            retryable: false,
        }
    );

    const answerElement = bubble.querySelector(
        ".le-global-chatbot__answer"
    );
    const statusElement = bubble.querySelector(
        ".le-global-chatbot__stream-status"
    );

    assert.equal(answerElement.textContent, "");
    assert.equal(statusElement.hidden, false);
});

test("progressive rendering: an error WITHOUT a preceding discard (the backend's own mid-generation ERROR path never sends one) still clears any visible provisional text - defensive, disclosed in the report", () => {
    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });

    controller.handleEvent(
        { type: "delta", text: "Partial answer before failure." }
    );

    controller.handleEvent(
        {
            type: "error",
            code: "stream_generation_failed",
            message: "Answer generation failed.",
            retryable: false,
        }
    );

    const answerElement = bubble.querySelector(
        ".le-global-chatbot__answer"
    );

    assert.equal(answerElement.textContent, "");
});

// =====================================================================
// 12. no automatic /chat retry
// =====================================================================

test("progressive rendering: a mid-stream failure observed through the controller still never triggers an automatic /chat retry - S7-LITE's no-double-request guarantee is unaffected by S8-LITE's onProtocolEvent wiring", async () => {
    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });
    const observedTypes = [];

    installFakeStreamFetch(
        bytesFromLines(
            ndjsonLines(
                [
                    {
                        type: "start",
                        protocol_version: 1,
                        request_id: "r",
                    },
                    { type: "delta", text: "Partial." },
                    { type: "discard" },
                    {
                        type: "error",
                        code: "stream_generation_failed",
                        message: "Answer generation failed.",
                        retryable: false,
                    },
                ]
            )
        )
    );

    let sendCallCount = 0;

    const send = async () => {
        sendCallCount += 1;

        return chatbot.requestStream(
            "https://example.test/chat/stream",
            {},
            (record) => {
                observedTypes.push(record.type);
                controller.handleEvent(record);
            }
        );
    };

    await assert.rejects(
        () => chatbot.performChatTransportRequest(
            {
                send,
                includeConversationState: true,
                onConversationStateRejected: () => {
                    throw new Error(
                        "must not retry a mid-stream failure"
                    );
                },
            }
        ),
        /Answer generation failed/
    );

    assert.equal(sendCallCount, 1);
    assert.deepEqual(
        observedTypes,
        ["start", "delta", "discard", "error"]
    );
    assert.equal(
        bubble.querySelector(".le-global-chatbot__answer")
            .textContent,
        ""
    );
});

// =====================================================================
// 13. abort stops further UI updates
// =====================================================================

test("progressive rendering: once the request is stale (e.g. New Conversation aborted it), handleEvent stops updating the bubble", () => {
    const bubble = createPendingBubble();
    let stale = false;

    const controller = createController(
        { bubbleElement: bubble, isStale: () => stale }
    );

    controller.handleEvent({ type: "delta", text: "Before abort." });
    stale = true;
    controller.handleEvent(
        { type: "delta", text: " After abort." }
    );

    assert.equal(
        bubble.querySelector(".le-global-chatbot__answer")
            .textContent,
        "Before abort."
    );
});

test("progressive rendering: a scheduled (RAF-deferred) flush that fires after the request went stale never writes to the DOM", () => {
    const bubble = createPendingBubble();
    let stale = false;
    let deferredCallback = null;

    const controller = createController(
        {
            bubbleElement: bubble,
            isStale: () => stale,
            scheduleWork: (callback) => {
                deferredCallback = callback;
            },
        }
    );

    controller.handleEvent({ type: "delta", text: "Text." });

    // The flush is deferred - nothing written yet.
    assert.equal(
        bubble.querySelector(".le-global-chatbot__answer")
            .textContent,
        ""
    );

    stale = true;
    deferredCallback();

    assert.equal(
        bubble.querySelector(".le-global-chatbot__answer")
            .textContent,
        "",
        "a stale flush must never write"
    );
});

// =====================================================================
// 14. feature OFF keeps exact old rendering
// =====================================================================

test("feature OFF: chatTransport resolves to json, so requestStream/the streaming bubble controller are structurally never invoked (see sendChatRequest in chatbot.js - the ternary only ever calls requestStream when chatTransport === \"stream\")", () => {
    assert.equal(
        chatbot.resolveChatTransport(
            {
                chatStreamingEnabled: false,
                chatStreamEndpoint: "https://example.test/chat/stream",
            }
        ),
        "json"
    );
});

// =====================================================================
// 15. feature ON uses progressive path (also exercises section 16's
// own suggested manual smoke sequence: start, delta, delta,
// validating, metadata, done - as an automated test instead of a
// manual one).
// =====================================================================

test("feature ON + streaming supported: chatTransport resolves to stream, and requestStream's onProtocolEvent wiring drives the bubble controller through a realistic full sequence (start, delta, delta, validating, metadata, done)", async () => {
    assert.equal(
        chatbot.resolveChatTransport(
            {
                chatStreamingEnabled: true,
                chatStreamEndpoint: "https://example.test/chat/stream",
                capabilitySource: {
                    fetch: () => {},
                    ReadableStream: function () {},
                    TextDecoder: function () {},
                },
            }
        ),
        "stream"
    );

    const bubble = createPendingBubble();
    const controller = createController({ bubbleElement: bubble });

    installFakeStreamFetch(
        bytesFromLines(
            ndjsonLines(
                [
                    {
                        type: "start",
                        protocol_version: 1,
                        request_id: "r",
                    },
                    { type: "delta", text: "In France, " },
                    {
                        type: "delta",
                        text: "an employer must give notice.",
                    },
                    { type: "validating" },
                    {
                        type: "metadata",
                        question: "q",
                        grounded: true,
                        model: "m",
                        retrieval_total: 1,
                        sources: [],
                        contacts: [],
                        conversation_state: null,
                    },
                    { type: "done", request_id: "r" },
                ]
            )
        )
    );

    const response = await chatbot.requestStream(
        "https://example.test/chat/stream",
        {},
        (record) => controller.handleEvent(record)
    );

    assert.equal(
        response.answer,
        "In France, an employer must give notice."
    );

    const answerElement = bubble.querySelector(
        ".le-global-chatbot__answer"
    );
    const statusElement = bubble.querySelector(
        ".le-global-chatbot__stream-status"
    );

    assert.equal(
        answerElement.textContent,
        "In France, an employer must give notice."
    );

    // validating was the last bubble-visible event observed - the
    // controller itself never clears it (see test 4 above); the
    // caller's own settle-time renderMessageList() call is what
    // replaces this whole bubble once `done` resolves this promise.
    assert.equal(statusElement.hidden, false);
    assert.equal(
        statusElement.textContent,
        chatbot.STREAM_VALIDATING_STATUS_TEXT
    );
});

// =====================================================================
// findPendingBubbleElement
// =====================================================================

test("findPendingBubbleElement locates the pending bubble inside the most recently appended turn", () => {
    const messageList = createFakeElement("div");

    const settledTurn = createFakeElement("article");
    const streamingTurn = createFakeElement("article");
    const pendingBubble = createPendingBubble();

    streamingTurn.appendChild(pendingBubble);
    messageList.appendChild(settledTurn);
    messageList.appendChild(streamingTurn);

    assert.equal(
        chatbot.findPendingBubbleElement(messageList),
        pendingBubble
    );
});

test("findPendingBubbleElement returns null when the message list is empty", () => {
    const messageList = createFakeElement("div");

    assert.equal(
        chatbot.findPendingBubbleElement(messageList),
        null
    );
});

// =====================================================================
// Scrolling (section 12): keep the latest text visible only when the
// user was already near the bottom - never force-scroll otherwise.
// =====================================================================

test("progressive rendering: a delta scrolls to the new bottom when the user was already near the bottom", () => {
    const bubble = createPendingBubble();
    const conversation = createConversationElement(
        { scrollHeight: 500, scrollTop: 480, clientHeight: 20 }
    );

    const controller = createController(
        { bubbleElement: bubble, conversationElement: conversation }
    );

    controller.handleEvent(
        { type: "delta", text: "Growing answer text." }
    );

    // This fixture's scrollHeight is static (unaffected by the DOM
    // write), so this test only proves the threshold comparison
    // itself, not the before/after-write ORDERING that comparison is
    // read in - see the dedicated ordering test further below, which
    // uses a scrollHeight tied to the bubble's own growing content.
    assert.equal(
        conversation.scrollTop,
        conversation.scrollHeight
    );
});

test("progressive rendering: a delta does NOT force-scroll when the user had scrolled up away from the bottom", () => {
    const bubble = createPendingBubble();
    const conversation = createConversationElement(
        { scrollHeight: 500, scrollTop: 0, clientHeight: 20 }
    );

    const controller = createController(
        { bubbleElement: bubble, conversationElement: conversation }
    );

    controller.handleEvent(
        { type: "delta", text: "Growing answer text." }
    );

    assert.equal(
        conversation.scrollTop,
        0,
        "scrollTop must be left exactly where the user put it"
    );
});

test("progressive rendering: the near-bottom check is read BEFORE the DOM write, not after - a conversationElement whose scrollHeight genuinely grows with the bubble's content (found missing by an S8-LITE adversarial review, which noted the two tests above use a static scrollHeight that can't distinguish this ordering)", () => {
    const bubble = createPendingBubble();

    // scrollHeight is a live getter tied to the bubble's own current
    // text length, like a real browser's layout would be - so
    // checking isNearBottom() before vs. after the textContent write
    // genuinely produces different answers, unlike a static fixture.
    const baseScrollHeight = 452;
    const clientHeight = 52;

    const conversation = {
        get scrollHeight() {
            return (
                baseScrollHeight
                + (bubble.textContent || "").length
            );
        },
        // Exactly at the bottom relative to the height BEFORE any
        // delta has been applied (452 - 52 = 400).
        scrollTop: 400,
        clientHeight,
    };

    const controller = createController(
        { bubbleElement: bubble, conversationElement: conversation }
    );

    const deltaText = "x".repeat(100);
    controller.handleEvent({ type: "delta", text: deltaText });

    // Correct order (check near-bottom BEFORE writing): reads
    // scrollHeight=452 against scrollTop=400 -> exactly at the
    // bottom -> pins to the NEW bottom (452 + 100 = 552) after the
    // write. A regression that read scrollHeight AFTER the write
    // would see 552 - 400 - 52 = 100 > 48 slack -> wrongly conclude
    // "not near bottom" -> leave scrollTop at 400, force-scrolling a
    // user who never asked for it out of position on the NEXT delta,
    // or under-scrolling here - either way this assertion fails.
    assert.equal(conversation.scrollTop, 552);
});

// =====================================================================
// Performance (section 14): multiple deltas queued behind ONE unfired
// scheduled flush must coalesce into a single DOM write - found
// missing by an S8-LITE adversarial review (the existing "deferred
// flush respects staleness" test above only ever queues one event
// before firing the deferred callback).
// =====================================================================

test("progressive rendering: multiple deltas queued behind one unfired scheduled flush coalesce into a single DOM write containing ALL of them, never one write per delta", () => {
    const bubble = createPendingBubble();
    let scheduleCallCount = 0;
    let deferredCallback = null;

    const controller = createController(
        {
            bubbleElement: bubble,
            scheduleWork: (callback) => {
                scheduleCallCount += 1;
                deferredCallback = callback;
            },
        }
    );

    controller.handleEvent({ type: "delta", text: "A" });
    controller.handleEvent({ type: "delta", text: "B" });
    controller.handleEvent({ type: "delta", text: "C" });

    assert.equal(
        scheduleCallCount,
        1,
        "only one frame may ever be scheduled while a flush is pending"
    );

    assert.equal(
        bubble.querySelector(".le-global-chatbot__answer")
            .textContent,
        "",
        "nothing should be written to the DOM until the flush fires"
    );

    deferredCallback();

    assert.equal(
        bubble.querySelector(".le-global-chatbot__answer")
            .textContent,
        "ABC",
        "the single flush must contain every coalesced delta, "
        + "not just the first"
    );
});
