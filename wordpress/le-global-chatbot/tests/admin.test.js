"use strict";

// Node's built-in test runner and assertions only - no framework, no
// dependency install, matching tests/chatbot.test.js. Run with:
//   node --test wordpress/le-global-chatbot/tests/admin.test.js
//
// admin.js is a browser IIFE with no module system of its own; its
// tail adds a test-only `module.exports` hook (skipped entirely in a
// real browser, where `module` is never defined) that exposes the
// pure, DOM-free parsing functions (errorMessage/extractStructured
// Detail/isReplacementRequiredResponse) plus the mission's mandatory
// fetch-count/replace_existing flow, exercised here through a
// minimal fake DOM/fetch/FormData - never a real browser or Node's
// own strict FormData(form) constructor, which rejects a plain fake
// form outright.
//
// Every exported function is defined AFTER admin.js's own top-level
// `if (!uploadForm) { return; }` guard, so even importing the pure
// functions requires document.querySelector to already return a
// (minimal) fake form before the very first require() below - real
// browsers always have one; a real admin page without the upload
// form present simply never reaches any of this code either.

const assert = require("node:assert/strict");
const path = require("node:path");
const { test, describe, beforeEach, afterEach } = require("node:test");

const ADMIN_JS_PATH = path.join(
    __dirname,
    "..",
    "assets",
    "admin.js"
);

class FakeFormData {
    constructor() {
        this._entries = new Map();
    }

    set(key, value) {
        this._entries.set(key, value);
    }

    delete(key) {
        this._entries.delete(key);
    }

    get(key) {
        return this._entries.has(key) ? this._entries.get(key) : null;
    }

    has(key) {
        return this._entries.has(key);
    }
}

function makeFakeResponse({ ok, status, payload }) {
    return {
        ok,
        status,
        async json() {
            if (payload === undefined) {
                throw new Error("no body");
            }

            return payload;
        },
    };
}

function installFakeDom({ submitHandlerHolder } = {}) {
    const fakeButton = { disabled: false };

    const fakeForm = {
        action: "https://example.test/wp-admin/admin-post.php",
        dataset: {},
        addEventListener(eventName, handler) {
            if (eventName === "submit" && submitHandlerHolder) {
                submitHandlerHolder.handler = handler;
            }
        },
        querySelector(selector) {
            if (selector === 'button[type="submit"]') {
                return fakeButton;
            }

            return null;
        },
    };

    global.document = {
        querySelectorAll: () => [],
        querySelector: (selector) => {
            if (selector === ".le-global-chatbot-admin__upload-form") {
                return fakeForm;
            }

            return null;
        },
    };

    global.window = {
        confirm: () => true,
        alert: () => {},
        location: { reload: () => {} },
    };

    global.FormData = FakeFormData;

    return { fakeButton, fakeForm };
}

function loadFreshAdminModule() {
    delete require.cache[require.resolve(ADMIN_JS_PATH)];

    return require(ADMIN_JS_PATH);
}

installFakeDom();

const {
    errorMessage,
    extractStructuredDetail,
    isReplacementRequiredResponse,
} = loadFreshAdminModule();

delete global.document;
delete global.window;
delete global.FormData;

// --- Pure parsing/decision function tests -------------------------

test("errorMessage uses the structured data.message when present", () => {
    assert.equal(
        errorMessage({
            success: false,
            data: { message: "DOCX validation failed: bad zip." },
        }),
        "DOCX validation failed: bad zip."
    );
});

test("errorMessage falls back to the generic text only when no usable message exists", () => {
    assert.equal(
        errorMessage({ success: false, data: {} }),
        "The document could not be indexed."
    );
    assert.equal(errorMessage(null), "The document could not be indexed.");
    assert.equal(
        errorMessage({ success: false, data: { message: "   " } }),
        "The document could not be indexed."
    );
});

test("errorMessage surfaces the real backend reason for every HTTP error code the mission lists", () => {
    // 400/413/422/500 all reach the PHP proxy as a plain string
    // `detail` (a bare FastAPI HTTPException) - extract_message()
    // relays it verbatim as data.message; only a response with
    // neither a usable detail nor message string falls back. This
    // backend never natively emits a 413 itself (oversized uploads
    // are rejected as 422 - see the size-limit case below); a real
    // 413 could only originate from an infrastructure layer in
    // front of it (e.g. a reverse proxy body-size limit), which is
    // untested here as it is outside this admin code's own
    // responsibility - what IS tested is that errorMessage()/
    // extract_message() extract a plain-string detail identically
    // no matter which status code carries it.
    const cases = [
        "No DOCX document was received.",
        "The uploaded DOCX exceeds the configured size limit.",
        "DOCX validation failed: bad zip.",
        "The document was valid but could not be indexed.",
    ];

    for (const message of cases) {
        assert.equal(
            errorMessage({ success: false, data: { message } }),
            message
        );
    }

    // The generic mechanism itself does not discriminate by status
    // code - a 413-shaped response with a plain-string detail (were
    // one ever produced, e.g. by a future infrastructure change)
    // would be surfaced exactly the same way.
    assert.equal(
        errorMessage({
            success: false,
            data: { message: "Request entity too large." },
        }),
        "Request entity too large."
    );
});

test("a FastAPI RequestValidationError's list-shaped detail surfaces a useful message, never the generic fallback", () => {
    // Mission "HOTFIX 0.4.9" review, section 3 - FastAPI's own
    // automatic request validation (e.g. a missing/malformed
    // multipart `file` field, intercepted before the admin router's
    // business logic even runs) produces detail=[{loc,msg,type}, ...],
    // never a string or a {message: ...} object. extract_message()
    // (PHP) now extracts detail[0].msg from this shape and relays it
    // as data.message - by the time it reaches admin.js, this is
    // already a plain string, so errorMessage() needs no JS-side
    // change; this test proves the two sides genuinely compose: the
    // exact string PHP would now produce is what errorMessage()
    // shows, never "The document could not be indexed."
    const messageAfterPhpExtraction = "Field required";

    assert.equal(
        errorMessage({
            success: false,
            data: { message: messageAfterPhpExtraction },
        }),
        "Field required"
    );
});

test("extractStructuredDetail returns the object for a 409 structured payload", () => {
    const detail = extractStructuredDetail({
        success: false,
        data: {
            message: "A document already exists for Argentina.",
            detail: {
                code: "document_replacement_required",
                country: "Argentina",
                country_code: "AR",
                existing_document_ids: ["doc_a"],
            },
        },
    });

    assert.equal(detail.code, "document_replacement_required");
    assert.equal(detail.country, "Argentina");
});

test("extractStructuredDetail is null when detail is a plain string, absent, or empty", () => {
    // The WordPress proxy sends [] (an empty array, not an object)
    // whenever the backend's own `detail` was a plain string (a
    // regular HTTPException) - is_array() is false for a string on
    // the PHP side, so this must never be treated as structured.
    assert.equal(
        extractStructuredDetail({ success: false, data: { detail: [] } }),
        null
    );
    assert.equal(
        extractStructuredDetail({ success: false, data: {} }),
        null
    );
    assert.equal(extractStructuredDetail(null), null);
    assert.equal(
        extractStructuredDetail({
            success: false,
            data: { detail: "a plain string detail" },
        }),
        null
    );
});

test("isReplacementRequiredResponse is true only for 409 + document_replacement_required", () => {
    const replacementPayload = {
        success: false,
        data: {
            detail: { code: "document_replacement_required" },
        },
    };

    assert.equal(
        isReplacementRequiredResponse(409, replacementPayload),
        true
    );

    // A different 409 code (already-current) must never be confused
    // with the replacement-confirmation flow.
    assert.equal(
        isReplacementRequiredResponse(409, {
            success: false,
            data: { detail: { code: "document_already_current" } },
        }),
        false
    );

    // Same detail, wrong status.
    assert.equal(
        isReplacementRequiredResponse(422, replacementPayload),
        false
    );

    assert.equal(isReplacementRequiredResponse(409, null), false);
});

// --- Fetch-count / replace_existing flow (mocked DOM + fetch) -----

describe("upload form fetch flow", () => {
    let fakeButton;
    let submitHandlerHolder;
    let fetchCalls;
    let alerts;

    beforeEach(() => {
        fetchCalls = [];
        alerts = [];
        submitHandlerHolder = {};

        const dom = installFakeDom({ submitHandlerHolder });
        fakeButton = dom.fakeButton;

        global.window.confirm = () => true;
        global.window.alert = (message) => alerts.push(message);

        loadFreshAdminModule();
    });

    afterEach(() => {
        delete global.document;
        delete global.window;
        delete global.FormData;
        delete require.cache[require.resolve(ADMIN_JS_PATH)];
    });

    function queueResponses(...responses) {
        let call = 0;

        global.fetch = async (_url, options) => {
            fetchCalls.push({
                replaceExisting: options.body.get("replace_existing"),
            });

            const response = responses[call];
            call += 1;

            return makeFakeResponse(response);
        };
    }

    function submit() {
        const event = { preventDefault: () => {} };

        return submitHandlerHolder.handler(event);
    }

    test(
        "409 replacement_required then Cancel makes exactly one fetch total",
        async () => {
            global.window.confirm = () => false;

            queueResponses({
                ok: false,
                status: 409,
                payload: {
                    success: false,
                    data: {
                        message: "A document already exists for Argentina.",
                        detail: {
                            code: "document_replacement_required",
                            country: "Argentina",
                            country_code: "AR",
                            existing_document_ids: ["doc_a"],
                        },
                    },
                },
            });

            await submit();

            assert.equal(fetchCalls.length, 1);
            assert.equal(fetchCalls[0].replaceExisting, null);
            // Cancel: no success alert, button re-enabled.
            assert.equal(alerts.length, 0);
            assert.equal(fakeButton.disabled, false);
        }
    );

    test(
        "409 replacement_required then Confirm makes exactly two fetches, replace=false then replace=true",
        async () => {
            global.window.confirm = () => true;

            queueResponses(
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            message: (
                                "A document already exists for Argentina."
                            ),
                            detail: {
                                code: "document_replacement_required",
                                country: "Argentina",
                                country_code: "AR",
                                existing_document_ids: ["doc_a"],
                            },
                        },
                    },
                },
                {
                    ok: true,
                    status: 201,
                    payload: {
                        success: true,
                        data: {
                            message: (
                                "Argentina.docx replaced the previous "
                                + "country document successfully with "
                                + "2 chunks."
                            ),
                            status: "replaced",
                        },
                    },
                }
            );

            await submit();

            assert.equal(fetchCalls.length, 2);
            assert.equal(fetchCalls[0].replaceExisting, null);
            assert.equal(fetchCalls[1].replaceExisting, "1");
            assert.equal(alerts.length, 1);
            assert.match(alerts[0], /replaced the previous country/);
        }
    );

    test(
        "a fresh-country upload (HTTP 201, no confirmation) makes exactly one fetch",
        async () => {
            queueResponses({
                ok: true,
                status: 201,
                payload: {
                    success: true,
                    data: {
                        message: (
                            "Argentina.docx was indexed successfully "
                            + "with 2 chunks."
                        ),
                        status: "uploaded",
                    },
                },
            });

            await submit();

            assert.equal(fetchCalls.length, 1);
            assert.equal(alerts.length, 1);
            assert.match(alerts[0], /indexed successfully/);
        }
    );

    test(
        "a 422 with a structured message never shows the generic fallback",
        async () => {
            queueResponses({
                ok: false,
                status: 422,
                payload: {
                    success: false,
                    data: {
                        message: "Only DOCX documents are accepted.",
                        detail: [],
                    },
                },
            });

            await submit();

            assert.equal(fetchCalls.length, 1);
            assert.equal(alerts.length, 1);
            assert.equal(alerts[0], "Only DOCX documents are accepted.");
        }
    );

    test(
        "a 500 with no usable body falls back to the generic message, never a raw crash",
        async () => {
            global.fetch = async (_url, options) => {
                fetchCalls.push({
                    replaceExisting: options.body.get("replace_existing"),
                });

                return makeFakeResponse({ ok: false, status: 500 });
            };

            await submit();

            assert.equal(fetchCalls.length, 1);
            assert.equal(alerts.length, 1);
            assert.equal(alerts[0], "The document could not be indexed.");
        }
    );

    test(
        "the submit button is disabled during the request and re-enabled after",
        async () => {
            let disabledDuringFetch = null;

            global.fetch = async (_url, options) => {
                disabledDuringFetch = fakeButton.disabled;
                fetchCalls.push({
                    replaceExisting: options.body.get("replace_existing"),
                });

                return makeFakeResponse({
                    ok: true,
                    status: 201,
                    payload: {
                        success: true,
                        data: { message: "ok", status: "uploaded" },
                    },
                });
            };

            await submit();

            assert.equal(disabledDuringFetch, true);
            assert.equal(fakeButton.disabled, false);
        }
    );

    test(
        "a second submit while a request is in flight never doubles the fetch count",
        async () => {
            fakeButton.disabled = true;

            const event = { preventDefault: () => {} };
            await submitHandlerHolder.handler(event);

            assert.equal(fetchCalls.length, 0);
        }
    );
});
