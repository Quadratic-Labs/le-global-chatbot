"use strict";

// Node's built-in test runner and assertions only - no framework, no
// dependency install, matching chatbot.test.js's own convention. Run
// with:
//   node --test wordpress/le-global-chatbot/tests/chatbot-stream.test.js
//
// GATE S7-LITE: covers the /chat/stream browser consumer added to
// chatbot.js - the NDJSON line parser, the protocol v1 state machine,
// response-shape reconstruction, transport selection, and the no-
// double-request retry guarantee. Everything here is a pure, DOM-free
// function exported via chatbot.js's own test-only module.exports
// hook (see that file). requestStream() itself is still DOM-free (it
// only takes a url/options pair, like requestJson()) but depends on
// the global fetch/Response/ReadableStream - this file supplies fake
// implementations per test rather than making any real network call.
//
// What is NOT covered here, by design: initializeWidget's own DOM
// wiring (reading widget.dataset.chatStream*, constructing
// sendChatRequest/submitChatRequest, the requestInFlight/
// AbortController plumbing). That thin wiring calls straight into the
// functions tested below and is covered by `node --check` plus direct
// source review instead - the same scoping decision chatbot.test.js
// already documents for submitChatRequest's retry logic, which this
// gate extracted into the now directly-testable
// performChatTransportRequest().

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

/**
 * Installs a fake global fetch resolving to a Response whose body is
 * a real ReadableStream fed from `bytes` - in chunkSize-sized pieces
 * when given (deliberately small/arbitrary, to force splits mid-
 * record and mid-character), or as one single chunk otherwise.
 */
function installFakeStreamFetch(
    bytes,
    {
        status = 200,
        contentType = "application/x-ndjson; charset=utf-8",
        chunkSize = null,
    } = {}
) {
    global.fetch = async () => {
        let offset = 0;

        const stream = new ReadableStream({
            pull(controller) {
                if (offset >= bytes.length) {
                    controller.close();
                    return;
                }

                const end = chunkSize
                    ? Math.min(offset + chunkSize, bytes.length)
                    : bytes.length;

                controller.enqueue(bytes.slice(offset, end));
                offset = end;
            },
        });

        return new Response(
            stream,
            {
                status,
                headers: { "content-type": contentType },
            }
        );
    };
}

// =====================================================================
// Parser (cases 1-7)
// =====================================================================

test("NDJSON parser: one complete record per chunk", () => {
    const parser = chatbot.createNdjsonLineParser();

    const records = [
        utf8('{"type":"start","protocol_version":1,"request_id":"r1"}\n'),
        utf8('{"type":"delta","text":"Hello"}\n'),
    ].flatMap((chunk) => parser.push(chunk));

    parser.flush();

    assert.deepEqual(
        records,
        [
            { type: "start", protocol_version: 1, request_id: "r1" },
            { type: "delta", text: "Hello" },
        ]
    );
});

test("NDJSON parser: one record split across multiple chunks, including a mid-record split", () => {
    const parser = chatbot.createNdjsonLineParser();

    const line = (
        '{"type":"delta","text":"across multiple network chunks"}\n'
    );
    const bytes = utf8(line);

    // Deliberately mirrors the mission's own example: one record
    // arriving as four separate network chunks.
    const boundaries = [5, 17, 33, bytes.length];
    let start = 0;
    let allRecords = [];

    boundaries.forEach((end, index) => {
        const emitted = parser.push(bytes.slice(start, end));
        start = end;

        if (index < boundaries.length - 1) {
            assert.deepEqual(
                emitted,
                [],
                "nothing should be complete before the final piece"
            );
        }

        allRecords = allRecords.concat(emitted);
    });

    assert.deepEqual(
        allRecords,
        [{ type: "delta", text: "across multiple network chunks" }]
    );
});

test("NDJSON parser: multiple complete records arriving in a single chunk", () => {
    const parser = chatbot.createNdjsonLineParser();

    const chunk = bytesFromLines(
        ndjsonLines(
            [
                { type: "delta", text: "a" },
                { type: "delta", text: "b" },
                { type: "delta", text: "c" },
            ]
        )
    );

    const records = parser.push(chunk);

    assert.deepEqual(
        records.map((record) => record.text),
        ["a", "b", "c"]
    );
});

test("NDJSON parser: a UTF-8 multibyte character split across chunks decodes correctly", () => {
    const parser = chatbot.createNdjsonLineParser();

    const line = '{"type":"delta","text":"before🎉after"}\n';
    const bytes = utf8(line);

    const prefixBytes = utf8(
        '{"type":"delta","text":"before'
    );

    // Splits inside the 4-byte 🎉 sequence itself.
    const splitPoint = prefixBytes.length + 2;

    const records = parser
        .push(bytes.slice(0, splitPoint))
        .concat(parser.push(bytes.slice(splitPoint)));

    assert.deepEqual(
        records,
        [{ type: "delta", text: "before🎉after" }]
    );
});

test("NDJSON parser: an embedded (escaped) newline inside a JSON string value is never mistaken for a record boundary", () => {
    const parser = chatbot.createNdjsonLineParser();

    const line = JSON.stringify(
        { type: "delta", text: "line one\nline two" }
    ) + "\n";
    const bytes = utf8(line);
    const mid = Math.floor(bytes.length / 2);

    const records = parser
        .push(bytes.slice(0, mid))
        .concat(parser.push(bytes.slice(mid)));

    assert.deepEqual(
        records,
        [{ type: "delta", text: "line one\nline two" }]
    );
});

test("NDJSON parser: an incomplete trailing record (no terminating newline) is rejected at flush, not silently accepted", () => {
    const parser = chatbot.createNdjsonLineParser();

    const records = parser.push(
        utf8(
            '{"type":"delta","text":"complete"}\n'
            + '{"type":"delta","text":"truncated"'
        )
    );

    assert.deepEqual(records, [{ type: "delta", text: "complete" }]);

    assert.throws(
        () => parser.flush(),
        (error) => error instanceof Error
            && error.code === "truncated_stream"
    );
});

test("NDJSON parser: a malformed JSON line is rejected as a controlled protocol error, never a raw SyntaxError", () => {
    const parser = chatbot.createNdjsonLineParser();

    assert.throws(
        () => parser.push(utf8('{"type":"delta",\n')),
        (error) => error instanceof Error
            && error.code === "malformed_record"
    );
});

// =====================================================================
// Reconstruction (cases 8-13) - through requestStream(), end to end.
// =====================================================================

test("requestStream reconstruction: normal success (start, delta, delta, metadata, done)", async () => {
    const records = [
        { type: "start", protocol_version: 1, request_id: "r1" },
        { type: "delta", text: "Employees " },
        {
            type: "delta",
            text: "in Spain are entitled to notice. [1]",
        },
        {
            type: "metadata",
            question: "What is the notice period in Spain?",
            grounded: true,
            model: "gpt-test",
            retrieval_total: 3,
            sources: [
                { citation: 1, country: "Spain", section: "Notice" },
            ],
            contacts: [],
            conversation_state: { version: 1 },
        },
        { type: "done", request_id: "r1" },
    ];

    installFakeStreamFetch(bytesFromLines(ndjsonLines(records)));

    const response = await chatbot.requestStream(
        "https://example.test/chat/stream",
        {}
    );

    assert.equal(
        response.answer,
        "Employees in Spain are entitled to notice. [1]"
    );
    assert.equal(response.question, records[3].question);
    assert.equal(response.grounded, true);
    assert.equal(response.model, "gpt-test");
    assert.equal(response.retrieval_total, 3);
    assert.deepEqual(response.sources, records[3].sources);
    assert.deepEqual(response.contacts, []);
    assert.deepEqual(response.conversation_state, { version: 1 });
});

test("requestStream reconstruction: repair sequence (validating, discard, replacement) yields only the final replacement text", async () => {
    const records = [
        { type: "start", protocol_version: 1, request_id: "r2" },
        { type: "delta", text: "Draft answer that fails validation." },
        { type: "validating" },
        { type: "discard" },
        { type: "replacement", text: "Corrected, validated answer. [1]" },
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
        { type: "done", request_id: "r2" },
    ];

    installFakeStreamFetch(bytesFromLines(ndjsonLines(records)));

    const response = await chatbot.requestStream(
        "https://example.test/chat/stream",
        {}
    );

    assert.equal(response.answer, "Corrected, validated answer. [1]");
});

test("requestStream reconstruction: a partial-stream in-band error discards provisional text and rejects (routes to the existing error UI)", async () => {
    const records = [
        { type: "start", protocol_version: 1, request_id: "r3" },
        { type: "delta", text: "Partial answer before failure." },
        { type: "discard" },
        {
            type: "error",
            code: "stream_generation_failed",
            message: "Answer generation failed.",
            retryable: false,
        },
    ];

    installFakeStreamFetch(bytesFromLines(ndjsonLines(records)));

    await assert.rejects(
        () => chatbot.requestStream(
            "https://example.test/chat/stream",
            {}
        ),
        (error) => error instanceof Error
            && error.message === "Answer generation failed."
            && error.statusCode === undefined
    );
});

test("requestStream reconstruction: contacts are preserved from metadata", async () => {
    const contacts = [
        {
            contact_id: "c1",
            country_code: "ES",
            member_firm: "Firm",
            contact_person: "Jane Doe",
            email: "jane@example.test",
            phone: null,
            address: null,
            website: null,
            photo_url: null,
        },
    ];

    const records = [
        { type: "start", protocol_version: 1, request_id: "r4" },
        { type: "delta", text: "Contact answer." },
        {
            type: "metadata",
            question: "q",
            grounded: false,
            model: null,
            retrieval_total: 0,
            sources: [],
            contacts,
            conversation_state: null,
        },
        { type: "done", request_id: "r4" },
    ];

    installFakeStreamFetch(bytesFromLines(ndjsonLines(records)));

    const response = await chatbot.requestStream(
        "https://example.test/chat/stream",
        {}
    );

    assert.deepEqual(response.contacts, contacts);
});

test("requestStream reconstruction: conversation_state is preserved from metadata", async () => {
    const conversationState = {
        version: 1,
        actions: [],
        focus_action_index: null,
        ordered_country_codes: [],
        pending_clarification: null,
    };

    const records = [
        { type: "start", protocol_version: 1, request_id: "r5" },
        { type: "delta", text: "Answer." },
        {
            type: "metadata",
            question: "q",
            grounded: true,
            model: "m",
            retrieval_total: 2,
            sources: [],
            contacts: [],
            conversation_state: conversationState,
        },
        { type: "done", request_id: "r5" },
    ];

    installFakeStreamFetch(bytesFromLines(ndjsonLines(records)));

    const response = await chatbot.requestStream(
        "https://example.test/chat/stream",
        {}
    );

    assert.deepEqual(response.conversation_state, conversationState);
});

test("requestStream reconstruction: sources are preserved in their original order", async () => {
    const sources = [
        { citation: 1, country: "Spain", section: "A" },
        { citation: 2, country: "France", section: "B" },
        { citation: 3, country: "Germany", section: "C" },
    ];

    const records = [
        { type: "start", protocol_version: 1, request_id: "r6" },
        { type: "delta", text: "Answer [1][2][3]." },
        {
            type: "metadata",
            question: "q",
            grounded: true,
            model: "m",
            retrieval_total: 3,
            sources,
            contacts: [],
            conversation_state: null,
        },
        { type: "done", request_id: "r6" },
    ];

    installFakeStreamFetch(bytesFromLines(ndjsonLines(records)));

    const response = await chatbot.requestStream(
        "https://example.test/chat/stream",
        {}
    );

    assert.deepEqual(response.sources, sources);
});

test("requestStream: a full response delivered in small arbitrary-sized chunks (forcing splits mid-record and mid-character) still reconstructs correctly", async () => {
    const records = [
        { type: "start", protocol_version: 1, request_id: "r7" },
        {
            type: "delta",
            text: "Le délai de préavis est de trois mois. 🎉",
        },
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
        { type: "done", request_id: "r7" },
    ];

    installFakeStreamFetch(
        bytesFromLines(ndjsonLines(records)),
        { chunkSize: 3 }
    );

    const response = await chatbot.requestStream(
        "https://example.test/chat/stream",
        {}
    );

    assert.equal(
        response.answer,
        "Le délai de préavis est de trois mois. 🎉"
    );
});

// =====================================================================
// Protocol errors (cases 14-20, plus two closely related MUST rules
// from mission section 8 that are not otherwise covered above)
// =====================================================================

test("protocol error: a stream that does not begin with start is rejected", async () => {
    installFakeStreamFetch(
        bytesFromLines(ndjsonLines([{ type: "delta", text: "x" }]))
    );

    await assert.rejects(
        () => chatbot.requestStream(
            "https://example.test/chat/stream",
            {}
        ),
        (error) => error.code === "missing_start"
    );
});

test("protocol error: a second start event is rejected", async () => {
    installFakeStreamFetch(
        bytesFromLines(
            ndjsonLines(
                [
                    {
                        type: "start",
                        protocol_version: 1,
                        request_id: "r",
                    },
                    {
                        type: "start",
                        protocol_version: 1,
                        request_id: "r",
                    },
                ]
            )
        )
    );

    await assert.rejects(
        () => chatbot.requestStream(
            "https://example.test/chat/stream",
            {}
        ),
        (error) => error.code === "duplicate_start"
    );
});

test("protocol note (deliberate deviation from the literal spec wording - see the S7-LITE report): a replacement WITHOUT a preceding discard is accepted, matching real backend behavior", async () => {
    // chat_stream.py's own post-generation reconciliation branch
    // emits a bare `replacement` (no preceding `discard`) whenever the
    // final assembled answer differs from what was already streamed -
    // e.g. an unavailable-countries note or a contact fallback
    // appended after RAG generation finishes. Requiring a prior
    // discard here, as the gate spec's own minimum-rule list literally
    // states, would reject that legitimate, routine success response
    // as a protocol error - see applyStreamProtocolEvent's own
    // docstring in chatbot.js.
    const records = [
        { type: "start", protocol_version: 1, request_id: "r" },
        { type: "delta", text: "Streamed answer." },
        {
            type: "replacement",
            text: "Streamed answer. Note: no data for CH.",
        },
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
    ];

    installFakeStreamFetch(bytesFromLines(ndjsonLines(records)));

    const response = await chatbot.requestStream(
        "https://example.test/chat/stream",
        {}
    );

    assert.equal(
        response.answer,
        "Streamed answer. Note: no data for CH."
    );
});

test("protocol error: done without a preceding metadata event is rejected", async () => {
    installFakeStreamFetch(
        bytesFromLines(
            ndjsonLines(
                [
                    {
                        type: "start",
                        protocol_version: 1,
                        request_id: "r",
                    },
                    { type: "delta", text: "x" },
                    { type: "done", request_id: "r" },
                ]
            )
        )
    );

    await assert.rejects(
        () => chatbot.requestStream(
            "https://example.test/chat/stream",
            {}
        ),
        (error) => error.code === "missing_metadata"
    );
});

test("protocol error: any event after done is rejected", async () => {
    installFakeStreamFetch(
        bytesFromLines(
            ndjsonLines(
                [
                    {
                        type: "start",
                        protocol_version: 1,
                        request_id: "r",
                    },
                    { type: "delta", text: "x" },
                    {
                        type: "metadata",
                        question: "q",
                        grounded: true,
                        model: "m",
                        retrieval_total: 0,
                        sources: [],
                        contacts: [],
                        conversation_state: null,
                    },
                    { type: "done", request_id: "r" },
                    { type: "delta", text: "late" },
                ]
            )
        )
    );

    await assert.rejects(
        () => chatbot.requestStream(
            "https://example.test/chat/stream",
            {}
        ),
        (error) => error.code === "event_after_terminal"
    );
});

test("protocol error: an unsupported protocol_version in start is rejected", async () => {
    installFakeStreamFetch(
        bytesFromLines(
            ndjsonLines(
                [
                    {
                        type: "start",
                        protocol_version: 2,
                        request_id: "r",
                    },
                ]
            )
        )
    );

    await assert.rejects(
        () => chatbot.requestStream(
            "https://example.test/chat/stream",
            {}
        ),
        (error) => error.code === "unsupported_protocol_version"
    );
});

test("protocol error: an unrecognized event type is rejected", async () => {
    installFakeStreamFetch(
        bytesFromLines(
            ndjsonLines(
                [
                    {
                        type: "start",
                        protocol_version: 1,
                        request_id: "r",
                    },
                    { type: "sparkle" },
                ]
            )
        )
    );

    await assert.rejects(
        () => chatbot.requestStream(
            "https://example.test/chat/stream",
            {}
        ),
        (error) => error.code === "unknown_event_type"
    );
});

test("protocol error: a stream that ends without done or error is treated as a failure, not a silent success", async () => {
    installFakeStreamFetch(
        bytesFromLines(
            ndjsonLines(
                [
                    {
                        type: "start",
                        protocol_version: 1,
                        request_id: "r",
                    },
                    { type: "delta", text: "x" },
                ]
            )
        )
    );

    await assert.rejects(
        () => chatbot.requestStream(
            "https://example.test/chat/stream",
            {}
        ),
        /unexpectedly/
    );
});

// =====================================================================
// HTTP error handling (mission section 11) - a pre-NDJSON HTTP failure
// must never be parsed as NDJSON, and must throw the same
// statusCode/payload shape requestJson() does.
// =====================================================================

test("requestStream: a pre-NDJSON HTTP error (e.g. 422) is surfaced with requestJson()'s own error shape, never parsed as NDJSON", async () => {
    global.fetch = async () => new Response(
        JSON.stringify({ detail: "Question is required." }),
        {
            status: 422,
            headers: { "content-type": "application/json" },
        }
    );

    await assert.rejects(
        () => chatbot.requestStream(
            "https://example.test/chat/stream",
            {}
        ),
        (error) => error.statusCode === 422
            && error.payload.detail === "Question is required."
            && error.message === "Question is required."
    );
});

test("requestStream: a 503 with no JSON body still produces the existing generic error message, not a crash", async () => {
    global.fetch = async () => new Response(
        "",
        {
            status: 503,
            headers: { "content-type": "text/plain" },
        }
    );

    await assert.rejects(
        () => chatbot.requestStream(
            "https://example.test/chat/stream",
            {}
        ),
        (error) => error.statusCode === 503
            && /could not process/.test(error.message)
    );
});

test("a /chat/stream conversation_state 422 is detected by isConversationStateValidationError exactly like /chat's own", async () => {
    global.fetch = async () => new Response(
        JSON.stringify(
            {
                detail: [
                    {
                        loc: ["body", "conversation_state"],
                        msg: "invalid",
                    },
                ],
            }
        ),
        {
            status: 422,
            headers: { "content-type": "application/json" },
        }
    );

    try {
        await chatbot.requestStream(
            "https://example.test/chat/stream",
            {}
        );
        assert.fail("expected requestStream to reject");
    } catch (error) {
        assert.equal(
            chatbot.isConversationStateValidationError(error),
            true
        );
    }
});

// =====================================================================
// Request behavior (cases 21-25)
// =====================================================================

test("resolveChatTransport: feature flag OFF always resolves to json, regardless of endpoint or browser capability", () => {
    assert.equal(
        chatbot.resolveChatTransport(
            {
                chatStreamingEnabled: false,
                chatStreamEndpoint: "https://example.test/chat/stream",
                capabilitySource: {
                    fetch: () => {},
                    ReadableStream: function () {},
                    TextDecoder: function () {},
                },
            }
        ),
        "json"
    );
});

test("resolveChatTransport: feature flag ON with a supported browser resolves to stream", () => {
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
});

test("resolveChatTransport: feature flag ON but an unsupported browser falls back to json BEFORE any request is sent", () => {
    assert.equal(
        chatbot.resolveChatTransport(
            {
                chatStreamingEnabled: true,
                chatStreamEndpoint: "https://example.test/chat/stream",
                capabilitySource: {},
            }
        ),
        "json"
    );
});

test("resolveChatTransport: an empty stream endpoint falls back to json even with the flag ON", () => {
    assert.equal(
        chatbot.resolveChatTransport(
            {
                chatStreamingEnabled: true,
                chatStreamEndpoint: "",
                capabilitySource: {
                    fetch: () => {},
                    ReadableStream: function () {},
                    TextDecoder: function () {},
                },
            }
        ),
        "json"
    );
});

test("isStreamResponseSupported: true only when fetch, ReadableStream, and TextDecoder are all present", () => {
    assert.equal(
        chatbot.isStreamResponseSupported(
            {
                fetch: () => {},
                ReadableStream: function () {},
                TextDecoder: function () {},
            }
        ),
        true
    );

    assert.equal(
        chatbot.isStreamResponseSupported({ fetch: () => {} }),
        false
    );

    assert.equal(chatbot.isStreamResponseSupported({}), false);
});

test("performChatTransportRequest: a failure with no statusCode (e.g. a mid-stream failure) is never retried", async () => {
    let callCount = 0;

    const send = async () => {
        callCount += 1;
        throw new Error("Answer generation failed.");
    };

    await assert.rejects(
        () => chatbot.performChatTransportRequest(
            {
                send,
                includeConversationState: true,
                onConversationStateRejected: () => {
                    throw new Error(
                        "must not be called for a non-422 failure"
                    );
                },
            }
        ),
        /Answer generation failed/
    );

    assert.equal(
        callCount,
        1,
        "send must be called exactly once - no retry"
    );
});

test("performChatTransportRequest: an aborted request is never retried", async () => {
    let callCount = 0;

    const abortError = new Error("The operation was aborted.");
    abortError.name = "AbortError";

    const send = async () => {
        callCount += 1;
        throw abortError;
    };

    await assert.rejects(
        () => chatbot.performChatTransportRequest(
            {
                send,
                includeConversationState: true,
                onConversationStateRejected: () => {
                    throw new Error(
                        "must not be called on an aborted request"
                    );
                },
            }
        ),
        (error) => error.name === "AbortError"
    );

    assert.equal(callCount, 1);
});

test("performChatTransportRequest: a genuine pre-stream conversation_state 422 (requestJson/requestStream's own error shape) is retried exactly once, via the SAME send function, never a second one", async () => {
    let callCount = 0;
    let rejectedCalled = false;

    const send = async (includeConversationState) => {
        callCount += 1;

        if (includeConversationState) {
            const error = new Error("conversation_state is invalid");
            error.statusCode = 422;
            error.payload = {
                detail: [
                    {
                        loc: ["body", "conversation_state"],
                        msg: "invalid",
                    },
                ],
            };

            throw error;
        }

        return { answer: "ok" };
    };

    const result = await chatbot.performChatTransportRequest(
        {
            send,
            includeConversationState: true,
            onConversationStateRejected: () => {
                rejectedCalled = true;
            },
        }
    );

    assert.equal(result.answer, "ok");
    assert.equal(callCount, 2);
    assert.equal(rejectedCalled, true);
});

test("requestStream: aborting before/while the request is in flight rejects with AbortError (no NDJSON parsing begins)", async () => {
    const controller = new AbortController();

    global.fetch = (url, options) => new Promise(
        (resolve, reject) => {
            options.signal.addEventListener(
                "abort",
                () => {
                    const abortError = new Error(
                        "The operation was aborted."
                    );
                    abortError.name = "AbortError";
                    reject(abortError);
                }
            );
        }
    );

    const pending = chatbot.requestStream(
        "https://example.test/chat/stream",
        { signal: controller.signal }
    );

    controller.abort();

    await assert.rejects(
        () => pending,
        (error) => error.name === "AbortError"
    );
});

test("requestStream: an abort mid-body causes the reader to be cancelled and the rejection to propagate, never a completed response", async () => {
    const abortError = new Error("The operation was aborted.");
    abortError.name = "AbortError";

    const stream = new ReadableStream(
        {
            pull() {
                return Promise.reject(abortError);
            },
        }
    );

    global.fetch = async () => new Response(
        stream,
        {
            status: 200,
            headers: {
                "content-type": "application/x-ndjson; charset=utf-8",
            },
        }
    );

    await assert.rejects(
        () => chatbot.requestStream(
            "https://example.test/chat/stream",
            {}
        ),
        (error) => error.name === "AbortError"
    );
});

// =====================================================================
// Existing non-stream path (mission section 3/14) - confirms the
// module still exposes requestJson-independent behavior unchanged;
// the full regression proof is running chatbot.test.js unmodified.
// =====================================================================

test("the feature flag defaults OFF: an unset chatStreamingEnabled always resolves to json", () => {
    assert.equal(
        chatbot.resolveChatTransport(
            {
                chatStreamingEnabled: undefined,
                chatStreamEndpoint: "https://example.test/chat/stream",
            }
        ),
        "json"
    );
});
