"use strict";

// Node's built-in test runner and assertions only - no framework, no
// dependency install, matching tests/chatbot.test.js. Run with:
//   node --test wordpress/le-global-chatbot/tests/admin.test.js
//
// admin.js is a browser IIFE with no module system of its own; its
// tail adds a test-only `module.exports` hook (skipped entirely in a
// real browser, where `module` is never defined) that exposes the
// pure, DOM-free parsing/classification functions plus a small
// __queueForTests seam onto the real multi-file queue engine -
// exercised here through a minimal fake DOM/fetch/FormData, never a
// real browser (that is the Playwright E2E suite's job, mission
// "ORDER 4" - these tests complement it, they do not replace it).

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

class FakeElement {
    constructor() {
        this.innerHTML = "";
        this.dataset = {};
    }

    querySelectorAll() {
        return [];
    }

    querySelector() {
        return null;
    }

    addEventListener() {}
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

function makeFakeFile(name) {
    return { name, size: 1024 };
}

function installFakeDom({ submitHandlerHolder, initialFiles = [] } = {}) {
    const fakeButton = { disabled: false, textContent: "Upload and index" };
    const fakeFileInput = { id: "le-global-document", files: initialFiles };

    const fakeActionInput = {
        name: "action",
        value: "le_global_chatbot_upload_document",
    };

    const fakeQueueContainer = new FakeElement();
    const fakeDocumentsContainer = new FakeElement();
    const fakeSummaryContainer = new FakeElement();

    const fakeForm = {
        // Deliberately NOT a URL string, exactly like a real browser:
        // a form containing <input name="action"> (WordPress's own
        // admin-post.php dispatch convention) shadows the .action IDL
        // property with that named control, turning fetch(form.action)
        // into fetch("[object HTMLInputElement]") - this is the real,
        // confirmed root cause of the mission's historical upload bug
        // (mission "ORDER 4", section 33). Any code that regresses to
        // reading form.action instead of form.getAttribute("action")
        // must fail these tests loudly, not silently pass against an
        // unrealistically-clean fake.
        action: fakeActionInput,
        dataset: {
            refreshAction: "le_global_chatbot_refresh_state",
            refreshNonce: "test-refresh-nonce",
            reindexAction: "le_global_chatbot_reindex_document",
            deleteAction: "le_global_chatbot_delete_document",
        },
        getAttribute(name) {
            if (name === "action") {
                return "https://example.test/wp-admin/admin-post.php";
            }

            return null;
        },
        addEventListener(eventName, handler) {
            if (eventName === "submit" && submitHandlerHolder) {
                submitHandlerHolder.handler = handler;
            }
        },
        querySelector(selector) {
            if (selector === 'button[type="submit"]') {
                return fakeButton;
            }

            if (selector === 'input[name="action"]') {
                return fakeActionInput;
            }

            return null;
        },
        querySelectorAll(_selector) {
            return [];
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
        getElementById: (id) => {
            if (id === "le-global-document") {
                return fakeFileInput;
            }

            if (id === "le-global-chatbot-queue") {
                return fakeQueueContainer;
            }

            if (id === "le-global-chatbot-documents") {
                return fakeDocumentsContainer;
            }

            if (id === "le-global-chatbot-summary") {
                return fakeSummaryContainer;
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

    return {
        fakeButton,
        fakeForm,
        fakeFileInput,
        fakeQueueContainer,
        fakeDocumentsContainer,
        fakeSummaryContainer,
    };
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
    classifyUploadResponse,
    MAX_CONCURRENT_UPLOADS,
    getSelectedFiles,
} = loadFreshAdminModule();

delete global.document;
delete global.window;
delete global.FormData;

// --- Pure parsing/decision function tests (unchanged contract) ----

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
    // neither a usable detail nor message string falls back.
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

    assert.equal(
        errorMessage({
            success: false,
            data: { message: "Request entity too large." },
        }),
        "Request entity too large."
    );
});

test("a FastAPI RequestValidationError's list-shaped detail surfaces a useful message, never the generic fallback", () => {
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

    assert.equal(
        isReplacementRequiredResponse(409, {
            success: false,
            data: { detail: { code: "document_already_current" } },
        }),
        false
    );

    assert.equal(
        isReplacementRequiredResponse(422, replacementPayload),
        false
    );

    assert.equal(isReplacementRequiredResponse(409, null), false);
});

// --- classifyUploadResponse: the new decision-routing logic --------

describe("classifyUploadResponse", () => {
    test("a fresh 201 classifies as success", () => {
        const outcome = classifyUploadResponse(201, {
            success: true,
            data: { message: "Chile.docx was indexed successfully.", status: "uploaded" },
        });

        assert.equal(outcome.kind, "success");
    });

    test("document_already_current never requires a decision", () => {
        const outcome = classifyUploadResponse(409, {
            success: false,
            data: {
                message: "Chile.docx is already the current source.",
                detail: { code: "document_already_current" },
            },
        });

        assert.equal(outcome.kind, "already_current");
    });

    test("document_replacement_required routes to replacement_required", () => {
        const outcome = classifyUploadResponse(409, {
            success: false,
            data: {
                detail: {
                    code: "document_replacement_required",
                    country: "Argentina",
                },
            },
        });

        assert.equal(outcome.kind, "replacement_required");
        assert.equal(outcome.detail.country, "Argentina");
    });

    test("document_warning_confirmation_required with replacement_required=false routes to warning_required, never combined", () => {
        const outcome = classifyUploadResponse(409, {
            success: false,
            data: {
                detail: {
                    code: "document_warning_confirmation_required",
                    replacement_required: false,
                    warnings: [{ code: "STRUCTURE_WARNING" }],
                },
            },
        });

        assert.equal(outcome.kind, "warning_required");
    });

    test("document_warning_confirmation_required with replacement_required=true routes to combined_required", () => {
        const outcome = classifyUploadResponse(409, {
            success: false,
            data: {
                detail: {
                    code: "document_warning_confirmation_required",
                    replacement_required: true,
                    country_name: "Peru",
                },
            },
        });

        assert.equal(outcome.kind, "combined_required");
    });

    test("any other error status/payload routes to error, with the real backend message", () => {
        const outcome = classifyUploadResponse(422, {
            success: false,
            data: { message: "Only DOCX documents are accepted." },
        });

        assert.equal(outcome.kind, "error");
        assert.equal(outcome.message, "Only DOCX documents are accepted.");
    });
});

// --- getSelectedFiles / MAX_CONCURRENT_UPLOADS ----------------------

test("getSelectedFiles reads from input.files, never input.value", () => {
    const files = [makeFakeFile("a.docx"), makeFakeFile("b.docx")];

    assert.deepEqual(
        getSelectedFiles({ files, value: "C:\\fakepath\\a.docx" }).map((f) => f.name),
        ["a.docx", "b.docx"]
    );

    assert.deepEqual(getSelectedFiles(null), []);
    assert.deepEqual(getSelectedFiles({ files: null }), []);
});

test("MAX_CONCURRENT_UPLOADS is exactly 2 (mission ORDER 4, section 12)", () => {
    assert.equal(MAX_CONCURRENT_UPLOADS, 2);
});

// --- Fetch-count / queue flow (mocked DOM + fetch) ------------------

describe("upload queue flow", () => {
    let fakeButton;
    let submitHandlerHolder;
    let fetchCalls;
    let moduleExports;

    function installFilesAndLoad(files) {
        submitHandlerHolder = {};

        const dom = installFakeDom({ submitHandlerHolder, initialFiles: files });
        fakeButton = dom.fakeButton;

        global.window.confirm = () => true;
        global.window.alert = () => {};

        moduleExports = loadFreshAdminModule();

        return dom;
    }

    beforeEach(() => {
        fetchCalls = [];
    });

    afterEach(() => {
        delete global.document;
        delete global.window;
        delete global.FormData;
        delete require.cache[require.resolve(ADMIN_JS_PATH)];
    });

    function queueFetchResponses(responses) {
        let call = 0;

        global.fetch = async (_url, options) => {
            fetchCalls.push({
                replaceExisting: options.body.get("replace_existing"),
                confirmWarnings: options.body.get("confirm_warnings"),
            });

            const response = responses[Math.min(call, responses.length - 1)];
            call += 1;

            return makeFakeResponse(response);
        };
    }

    async function submit() {
        const event = { preventDefault: () => {} };

        return submitHandlerHolder.handler(event);
    }

    function snapshot() {
        return moduleExports.__queueForTests.getQueueSnapshot();
    }

    test(
        "a fresh single-file upload (HTTP 201) makes exactly one fetch and ends indexed",
        async () => {
            installFilesAndLoad([makeFakeFile("Argentina.docx")]);

            queueFetchResponses([
                {
                    ok: true,
                    status: 201,
                    payload: {
                        success: true,
                        data: { message: "Argentina.docx was indexed successfully with 2 chunks.", status: "uploaded" },
                    },
                },
            ]);

            await submit();

            assert.equal(fetchCalls.length, 1);
            assert.equal(snapshot()[0].status, "indexed");
            assert.equal(fakeButton.disabled, false);
        }
    );

    test(
        "409 document_already_current never makes a second request and never shows a replace decision",
        async () => {
            installFilesAndLoad([makeFakeFile("Chile.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            message: "Chile.docx is already the current source.",
                            detail: { code: "document_already_current" },
                        },
                    },
                },
            ]);

            await submit();

            assert.equal(fetchCalls.length, 1);
            assert.equal(snapshot()[0].status, "already_current");
        }
    );

    test(
        "409 replacement_required then Cancel makes exactly one fetch total, zero mutation",
        async () => {
            installFilesAndLoad([makeFakeFile("Argentina.docx")]);

            queueFetchResponses([
                {
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
                },
            ]);

            await submit();

            assert.equal(fetchCalls.length, 1);
            assert.equal(
                snapshot()[0].status,
                "awaiting_replacement_confirmation"
            );

            moduleExports.__queueForTests.resolveDecision(0, "cancel");

            assert.equal(fetchCalls.length, 1);
            assert.equal(snapshot()[0].status, "cancelled");
        }
    );

    test(
        "409 replacement_required then Replace makes exactly two fetches, replace=false then replace=true",
        async () => {
            installFilesAndLoad([makeFakeFile("Argentina.docx")]);

            queueFetchResponses([
                {
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
                },
                {
                    ok: true,
                    status: 201,
                    payload: {
                        success: true,
                        data: {
                            message: "Argentina.docx replaced the previous country document successfully with 2 chunks.",
                            status: "replaced",
                        },
                    },
                },
            ]);

            await submit();

            assert.equal(fetchCalls.length, 1);

            moduleExports.__queueForTests.resolveDecision(0, "replace");

            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(fetchCalls.length, 2);
            assert.equal(fetchCalls[0].replaceExisting, null);
            assert.equal(fetchCalls[1].replaceExisting, "1");
            assert.equal(snapshot()[0].status, "indexed");
        }
    );

    test(
        "409 warning_confirmation_required (replacement_required=false) then Continue sends confirm_warnings=1, replace_existing unset",
        async () => {
            installFilesAndLoad([makeFakeFile("Peru.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            message: "Confirmation required.",
                            detail: {
                                code: "document_warning_confirmation_required",
                                replacement_required: false,
                                country_name: "Peru",
                                warnings: [{ code: "STRUCTURE_WARNING", message: "Low topic coverage." }],
                            },
                        },
                    },
                },
                {
                    ok: true,
                    status: 201,
                    payload: {
                        success: true,
                        data: { message: "Peru.docx was indexed successfully.", status: "uploaded" },
                    },
                },
            ]);

            await submit();

            assert.equal(
                snapshot()[0].status,
                "awaiting_warning_confirmation"
            );

            moduleExports.__queueForTests.resolveDecision(0, "continue");

            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(fetchCalls.length, 2);
            assert.equal(fetchCalls[1].confirmWarnings, "1");
            assert.equal(fetchCalls[1].replaceExisting, null);
            assert.equal(snapshot()[0].status, "indexed");
        }
    );

    test(
        "409 warning_confirmation_required (replacement_required=false) then Cancel makes zero further requests",
        async () => {
            installFilesAndLoad([makeFakeFile("Peru.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            detail: {
                                code: "document_warning_confirmation_required",
                                replacement_required: false,
                            },
                        },
                    },
                },
            ]);

            await submit();

            moduleExports.__queueForTests.resolveDecision(0, "cancel");

            assert.equal(fetchCalls.length, 1);
            assert.equal(snapshot()[0].status, "cancelled");
        }
    );

    test(
        "409 warning_confirmation_required (replacement_required=true) is the combined decision, never confused with either single decision",
        async () => {
            installFilesAndLoad([makeFakeFile("Peru.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            detail: {
                                code: "document_warning_confirmation_required",
                                replacement_required: true,
                                country_name: "Peru",
                            },
                        },
                    },
                },
                {
                    ok: true,
                    status: 201,
                    payload: {
                        success: true,
                        data: { message: "Peru.docx replaced the previous country document successfully.", status: "replaced" },
                    },
                },
            ]);

            await submit();

            assert.equal(
                snapshot()[0].status,
                "awaiting_combined_confirmation"
            );

            moduleExports.__queueForTests.resolveDecision(
                0,
                "continue-and-replace"
            );

            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(fetchCalls.length, 2);
            assert.equal(fetchCalls[1].confirmWarnings, "1");
            assert.equal(fetchCalls[1].replaceExisting, "1");
            assert.equal(snapshot()[0].status, "indexed");
        }
    );

    test(
        "a 422 with a structured message never shows the generic fallback",
        async () => {
            installFilesAndLoad([makeFakeFile("bad.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 422,
                    payload: {
                        success: false,
                        data: { message: "Only DOCX documents are accepted.", detail: [] },
                    },
                },
            ]);

            await submit();

            assert.equal(fetchCalls.length, 1);
            assert.equal(snapshot()[0].status, "failed");
            assert.equal(snapshot()[0].message, "Only DOCX documents are accepted.");
        }
    );

    test(
        "a 500 with no usable body falls back to the generic message, never a raw crash",
        async () => {
            installFilesAndLoad([makeFakeFile("x.docx")]);

            global.fetch = async (_url, options) => {
                fetchCalls.push({
                    replaceExisting: options.body.get("replace_existing"),
                });

                return makeFakeResponse({ ok: false, status: 500 });
            };

            await submit();

            assert.equal(fetchCalls.length, 1);
            assert.equal(snapshot()[0].status, "failed");
            assert.equal(
                snapshot()[0].message,
                "The document could not be indexed."
            );
        }
    );

    test(
        "the submit button is disabled while the initial round trip is in flight and re-enabled after",
        async () => {
            installFilesAndLoad([makeFakeFile("x.docx")]);

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
        "a second submit while the button is disabled never doubles the fetch count",
        async () => {
            installFilesAndLoad([makeFakeFile("x.docx")]);
            fakeButton.disabled = true;

            const event = { preventDefault: () => {} };
            await submitHandlerHolder.handler(event);

            assert.equal(fetchCalls.length, 0);
        }
    );

    test(
        "submitting with no file selected never fetches (required-input guard, not a crash)",
        async () => {
            installFilesAndLoad([]);

            const event = { preventDefault: () => {} };
            await submitHandlerHolder.handler(event);

            assert.equal(fetchCalls.length, 0);
        }
    );

    test(
        "multi-file selection: at most 2 requests are ever in flight at once (mission ORDER 4, section 12)",
        async () => {
            installFilesAndLoad([
                makeFakeFile("a.docx"),
                makeFakeFile("b.docx"),
                makeFakeFile("c.docx"),
                makeFakeFile("d.docx"),
                makeFakeFile("e.docx"),
            ]);

            let inFlight = 0;
            let maxInFlight = 0;
            const releasers = [];

            global.fetch = async (_url, options) => {
                fetchCalls.push({
                    replaceExisting: options.body.get("replace_existing"),
                });

                inFlight += 1;
                maxInFlight = Math.max(maxInFlight, inFlight);

                await new Promise((resolve) => {
                    releasers.push(resolve);
                });

                inFlight -= 1;

                return makeFakeResponse({
                    ok: true,
                    status: 201,
                    payload: {
                        success: true,
                        data: { message: "ok", status: "uploaded" },
                    },
                });
            };

            const submitPromise = submit();

            // Let the microtask queue drain enough for the queue
            // engine to start its first batch of requests.
            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(
                fetchCalls.length,
                2,
                "only 2 of the 5 files should have started a request"
            );
            assert.equal(maxInFlight, 2);

            while (releasers.length > 0) {
                releasers.shift()();
                // eslint-disable-next-line no-await-in-loop
                await new Promise((resolve) => setTimeout(resolve, 20));
            }

            await submitPromise;

            assert.equal(fetchCalls.length, 5);
            assert.equal(maxInFlight, 2);
            assert.ok(
                snapshot().every((item) => item.status === "indexed")
            );
        }
    );

    test(
        "one file's failure never blocks or rolls back the others (mission ORDER 4, section 13)",
        async () => {
            installFilesAndLoad([
                makeFakeFile("good-a.docx"),
                makeFakeFile("corrupt.docx"),
                makeFakeFile("good-b.docx"),
            ]);

            queueFetchResponses([
                {
                    ok: true,
                    status: 201,
                    payload: { success: true, data: { message: "ok", status: "uploaded" } },
                },
                {
                    ok: false,
                    status: 422,
                    payload: { success: false, data: { message: "DOCX validation failed: bad zip." } },
                },
                {
                    ok: true,
                    status: 201,
                    payload: { success: true, data: { message: "ok", status: "uploaded" } },
                },
            ]);

            await submit();

            const statuses = snapshot().map((item) => item.status);

            assert.equal(fetchCalls.length, 3);
            assert.deepEqual(statuses, ["indexed", "failed", "indexed"]);
        }
    );

    test(
        "a multi-file batch triggers exactly one list/stats refresh, never one per file (mission ORDER 5D, section 4)",
        async () => {
            installFilesAndLoad([
                makeFakeFile("Argentina.docx"),
                makeFakeFile("Brazil.docx"),
                makeFakeFile("Chile.docx"),
            ]);

            let uploadCalls = 0;
            let refreshCalls = 0;

            // Unlike queueFetchResponses above, this mock handles
            // BOTH request shapes for real: the upload POSTs (a
            // FormData body) and the refresh GET (no body at all) -
            // needed here specifically to prove the refresh really
            // only fires once, not merely that it fails silently.
            global.fetch = async (_url, options) => {
                if (options && options.body && typeof options.body.get === "function") {
                    uploadCalls += 1;

                    return makeFakeResponse({
                        ok: true,
                        status: 201,
                        payload: { success: true, data: { message: "ok", status: "uploaded" } },
                    });
                }

                refreshCalls += 1;

                return makeFakeResponse({
                    ok: true,
                    status: 200,
                    payload: { success: true, data: { documents: [], stats: {} } },
                });
            };

            await submit();

            assert.equal(uploadCalls, 3);
            assert.equal(
                refreshCalls,
                1,
                "a 3-file batch must collapse into exactly one refresh, not three"
            );
        }
    );

    test(
        "resolveDecision on a parked item never pushes concurrency past MAX_CONCURRENT_UPLOADS",
        async () => {
            installFilesAndLoad([makeFakeFile("Denmark.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            message: "A document already exists for Denmark.",
                            detail: {
                                code: "document_replacement_required",
                                country: "Denmark",
                                country_code: "DK",
                                existing_document_ids: ["doc_dk"],
                            },
                        },
                    },
                },
            ]);

            await submit();

            assert.equal(
                snapshot()[0].status,
                "awaiting_replacement_confirmation"
            );

            // Denmark's own 409 is settled - now switch to a fetch
            // mock whose upload calls hang until released, so two
            // freshly-enqueued files can be driven to exactly the
            // MAX_CONCURRENT_UPLOADS=2 cap and held there.
            const resolvers = [];

            global.fetch = () => new Promise((resolve) => {
                resolvers.push(() => resolve(makeFakeResponse({
                    ok: true,
                    status: 201,
                    payload: { success: true, data: { message: "ok", status: "uploaded" } },
                })));
            });

            moduleExports.__queueForTests.enqueueFiles([
                makeFakeFile("Argentina.docx"),
                makeFakeFile("Belgium.docx"),
            ]);

            assert.equal(
                resolvers.length,
                2,
                "the fresh 2-file batch alone reaches the cap"
            );
            assert.equal(
                moduleExports.__queueForTests.activeUploadCount(),
                2
            );

            // While both cap slots are held, resolve Denmark's parked
            // decision - it must be queued behind the cap, never
            // started immediately alongside the two already in flight.
            moduleExports.__queueForTests.resolveDecision(0, "replace");

            assert.equal(
                resolvers.length,
                2,
                "Denmark's replace must not fire a third concurrent request while the cap is held"
            );
            assert.equal(
                moduleExports.__queueForTests.activeUploadCount(),
                2,
                "active concurrency must stay at the cap, never 3"
            );

            // Free exactly one slot - Denmark's queued decision should
            // now be picked up by the same pump that serves the batch.
            resolvers[0]();
            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(
                resolvers.length,
                3,
                "Denmark's replace request fires only once a slot actually frees up"
            );

            // Drain everything else so no pending promise leaks into
            // a later test.
            resolvers[1]();
            resolvers[2]();
            await new Promise((resolve) => setTimeout(resolve, 20));
        }
    );
});

// --- Edit a section (mission "ORDER 5D") ----------------------------
//
// A dedicated, self-contained fake DOM/fetch - the Edit UI has its
// own set of elements (country/section selects, textarea, message,
// Cancel/Save) never touched by the upload-queue tests above.

describe("edit a section", () => {
    function makeFakeSelect() {
        const select = {
            value: "",
            disabled: false,
            options: [],
            _listeners: {},
            addEventListener(eventName, handler) {
                select._listeners[eventName] = handler;
            },
            appendChild(option) {
                select.options.push(option);
            },
        };

        Object.defineProperty(select, "textContent", {
            get() {
                return "";
            },
            set() {
                select.options = [];
            },
        });

        return select;
    }

    function makeFakeButton() {
        return {
            disabled: false,
            _listeners: {},
            addEventListener(eventName, handler) {
                this._listeners[eventName] = handler;
            },
        };
    }

    function makeFakeMessageElement() {
        return { textContent: "", className: "" };
    }

    function makeFakeTextarea() {
        return { value: "", disabled: false };
    }

    function installEditSectionFakeDom() {
        const countrySelect = makeFakeSelect();
        const sectionSelect = makeFakeSelect();
        const textarea = makeFakeTextarea();
        const messageEl = makeFakeMessageElement();
        const cancelButton = makeFakeButton();
        const saveButton = makeFakeButton();
        const restoreButton = makeFakeButton();

        const editContainer = {
            dataset: {
                adminPostUrl: "https://example.test/wp-admin/admin-post.php",
                sectionsListAction: "le_global_chatbot_list_sections",
                sectionsListNonce: "sections-list-nonce",
                sectionGetAction: "le_global_chatbot_get_section",
                sectionGetNonce: "section-get-nonce",
                sectionUpdateAction: "le_global_chatbot_update_section",
                sectionUpdateNonce: "section-update-nonce",
                sectionRestoreAction: "le_global_chatbot_restore_section",
                sectionRestoreNonce: "section-restore-nonce",
            },
        };

        const byId = {
            "le-global-chatbot-edit": editContainer,
            "le-global-edit-country": countrySelect,
            "le-global-edit-section": sectionSelect,
            "le-global-edit-content": textarea,
            "le-global-chatbot-edit-message": messageEl,
            "le-global-edit-cancel": cancelButton,
            "le-global-edit-save": saveButton,
            "le-global-edit-restore": restoreButton,
        };

        global.document = {
            querySelectorAll: () => [],
            querySelector: () => null,
            getElementById: (id) => byId[id] || null,
            createElement: () => ({ value: "", textContent: "" }),
        };

        global.window = {
            confirm: () => true,
            alert: () => {},
            location: { reload: () => {} },
        };

        global.FormData = FakeFormData;

        return {
            countrySelect,
            sectionSelect,
            textarea,
            messageEl,
            cancelButton,
            saveButton,
            restoreButton,
        };
    }

    let dom;
    let fetchCalls;

    beforeEach(() => {
        dom = installEditSectionFakeDom();
        fetchCalls = [];
        loadFreshAdminModule();
    });

    afterEach(() => {
        delete global.document;
        delete global.window;
        delete global.FormData;
        delete global.fetch;
        delete require.cache[require.resolve(ADMIN_JS_PATH)];
    });

    function queueFetchResponses(responses) {
        let call = 0;

        global.fetch = async (url, options) => {
            fetchCalls.push({ url, options });

            const response = responses[Math.min(call, responses.length - 1)];
            call += 1;

            return makeFakeResponse(response);
        };
    }

    async function changeCountry(value) {
        dom.countrySelect.value = value;
        await dom.countrySelect._listeners.change();
    }

    async function changeSection(value) {
        dom.sectionSelect.value = value;
        await dom.sectionSelect._listeners.change();
    }

    async function clickSave() {
        return dom.saveButton._listeners.click();
    }

    async function clickRestore() {
        return dom.restoreButton._listeners.click();
    }

    function clickCancel() {
        return dom.cancelButton._listeners.click();
    }

    test("selecting a country fetches its sections and populates the dropdown", async () => {
        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: {
                        document_id: "doc_aaa",
                        sections: [
                            { section_id: "sec-1", legal_topic: "Working Time" },
                            { section_id: "sec-2", legal_topic: "Termination" },
                        ],
                    },
                },
            },
        ]);

        await changeCountry("doc_aaa");

        assert.equal(fetchCalls.length, 1);
        assert.match(fetchCalls[0].url, /action=le_global_chatbot_list_sections/);
        assert.match(fetchCalls[0].url, /document_id=doc_aaa/);
        assert.equal(dom.sectionSelect.disabled, false);
        // Placeholder + 2 real sections, never a "Create section" entry.
        assert.equal(dom.sectionSelect.options.length, 3);
        assert.equal(dom.sectionSelect.options[1].value, "sec-1");
        assert.equal(dom.sectionSelect.options[1].textContent, "Working Time");
        assert.equal(dom.cancelButton.disabled, false);
        assert.equal(dom.saveButton.disabled, true);
    });

    test("a country with zero sections disables the section dropdown and never offers to create one", async () => {
        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_empty", sections: [] } },
            },
        ]);

        await changeCountry("doc_empty");

        assert.equal(dom.sectionSelect.disabled, true);
        assert.equal(dom.sectionSelect.options.length, 1);
        assert.equal(dom.messageEl.textContent, "This country has no editable section yet.");
    });

    test("clearing the country selection resets section/textarea and makes zero network calls", async () => {
        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: { document_id: "doc_aaa", sections: [{ section_id: "sec-1", legal_topic: "Working Time" }] },
                },
            },
        ]);

        await changeCountry("doc_aaa");
        await changeCountry("");

        assert.equal(fetchCalls.length, 1);
        assert.equal(dom.sectionSelect.disabled, true);
        assert.equal(dom.textarea.value, "");
        assert.equal(dom.textarea.disabled, true);
        assert.equal(dom.cancelButton.disabled, true);
        assert.equal(dom.saveButton.disabled, true);
    });

    test("selecting a section fetches its effective content and enables Save", async () => {
        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: {
                        document_id: "doc_aaa",
                        section_id: "sec-1",
                        legal_topic: "Working Time",
                        content: "Employees are entitled to 25 days of paid leave.",
                    },
                },
            },
        ]);

        dom.countrySelect.value = "doc_aaa";
        await changeSection("sec-1");

        assert.match(fetchCalls[0].url, /action=le_global_chatbot_get_section/);
        assert.match(fetchCalls[0].url, /section_id=sec-1/);
        assert.equal(dom.textarea.value, "Employees are entitled to 25 days of paid leave.");
        assert.equal(dom.textarea.disabled, false);
        assert.equal(dom.saveButton.disabled, false);
    });

    test("Save posts the edited content and re-fetches to show the value really persisted", async () => {
        dom.countrySelect.value = "doc_aaa";
        dom.sectionSelect.value = "sec-1";
        dom.textarea.value = "Draft text the admin just typed.";
        dom.saveButton.disabled = false;

        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "Working Time", indexed_chunks: 4 } },
            },
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "Working Time", content: "Draft text the admin just typed." },
                },
            },
        ]);

        await clickSave();

        assert.equal(fetchCalls.length, 2);
        // First call: the mutation itself, exactly once.
        assert.equal(fetchCalls[0].options.method, "POST");
        assert.equal(fetchCalls[0].options.body.get("content"), "Draft text the admin just typed.");
        assert.equal(fetchCalls[0].options.body.get("document_id"), "doc_aaa");
        assert.equal(fetchCalls[0].options.body.get("section_id"), "sec-1");
        // Second call: the re-fetch, never trusting the sent value blindly.
        assert.match(fetchCalls[1].url, /action=le_global_chatbot_get_section/);
        assert.equal(dom.textarea.value, "Draft text the admin just typed.");
        assert.equal(dom.messageEl.className.includes("is-success"), true);
        assert.equal(dom.saveButton.disabled, false);
    });

    test("Save is a single mutation even under a double click", async () => {
        dom.countrySelect.value = "doc_aaa";
        dom.sectionSelect.value = "sec-1";
        dom.textarea.value = "Some content.";
        dom.saveButton.disabled = false;

        let resolveFirstFetch;
        let callCount = 0;

        global.fetch = (url, options) => {
            fetchCalls.push({ url, options });
            callCount += 1;

            if (callCount === 1) {
                return new Promise((resolve) => {
                    resolveFirstFetch = () => resolve(makeFakeResponse({
                        ok: true,
                        status: 200,
                        payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x", indexed_chunks: 1 } },
                    }));
                });
            }

            // The automatic re-fetch after a successful save - this
            // test only cares about the MUTATION call count, not the
            // re-fetch's own timing, so it resolves immediately.
            return Promise.resolve(makeFakeResponse({
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x", content: "Some content." } },
            }));
        };

        const firstSave = clickSave();
        // The button is disabled synchronously before the first await
        // yields - a second click while still in flight must be a
        // true no-op, never a second mutation.
        const secondSave = clickSave();

        resolveFirstFetch();
        await firstSave;
        await secondSave;

        assert.equal(
            fetchCalls.filter((call) => call.options.method === "POST").length,
            1
        );
    });

    test("Cancel resets everything to empty and makes zero network calls", async () => {
        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: {
                        document_id: "doc_aaa",
                        section_id: "sec-1",
                        legal_topic: "Working Time",
                        content: "Some loaded content.",
                    },
                },
            },
        ]);

        dom.countrySelect.value = "doc_aaa";
        await changeSection("sec-1");

        assert.equal(fetchCalls.length, 1);

        clickCancel();

        assert.equal(fetchCalls.length, 1, "Cancel must never itself call fetch");
        assert.equal(dom.countrySelect.value, "");
        assert.equal(dom.sectionSelect.disabled, true);
        assert.equal(dom.textarea.value, "");
        assert.equal(dom.textarea.disabled, true);
        assert.equal(dom.messageEl.textContent, "");
        assert.equal(dom.cancelButton.disabled, true);
        assert.equal(dom.saveButton.disabled, true);
    });

    test("changing the country again before the first sections fetch resolves discards the stale response", async () => {
        let resolveFirst;
        let secondResolved = false;

        global.fetch = (url) => {
            fetchCalls.push({ url });

            if (fetchCalls.length === 1) {
                return new Promise((resolve) => {
                    resolveFirst = () => resolve(makeFakeResponse({
                        ok: true,
                        status: 200,
                        payload: {
                            success: true,
                            data: { document_id: "doc_stale", sections: [{ section_id: "old-sec", legal_topic: "Stale" }] },
                        },
                    }));
                });
            }

            secondResolved = true;

            return Promise.resolve(makeFakeResponse({
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: { document_id: "doc_fresh", sections: [{ section_id: "new-sec", legal_topic: "Fresh" }] },
                },
            }));
        };

        dom.countrySelect.value = "doc_stale";
        const firstChange = changeCountry("doc_stale");

        dom.countrySelect.value = "doc_fresh";
        await changeCountry("doc_fresh");

        assert.equal(secondResolved, true);
        // The second (newer) selection's own sections must win.
        assert.equal(dom.sectionSelect.options[1].value, "new-sec");

        resolveFirst();
        await firstChange;

        // The stale first response must never overwrite the second,
        // newer one once it has already arrived and been applied.
        assert.equal(dom.sectionSelect.options[1].value, "new-sec");
    });

    test("a structured backend error is surfaced verbatim, never the generic fallback", async () => {
        queueFetchResponses([
            {
                ok: false,
                status: 404,
                payload: { success: false, data: { message: "No indexed document was found for this country." } },
            },
        ]);

        await changeCountry("doc_missing");

        assert.equal(dom.messageEl.textContent, "No indexed document was found for this country.");
        assert.equal(dom.messageEl.className.includes("is-error"), true);
    });

    test("exotic content (script tags, ampersands, quotes) round-trips through .value untouched, never innerHTML", async () => {
        const exotic = "<Company>\n<script>alert(1)</script>\n& \"quoted\" text";

        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x", content: exotic },
                },
            },
        ]);

        dom.countrySelect.value = "doc_aaa";
        await changeSection("sec-1");

        // Exact byte-for-byte preservation - a DOM textarea's .value
        // is never parsed as markup, so nothing here is "escaped"
        // (there is no HTML entity anywhere in this string); it is
        // reproduced exactly as the backend sent it.
        assert.equal(dom.textarea.value, exotic);
    });

    test("the update request is sent to the real admin-post URL from config, not a form.action lookalike", async () => {
        dom.countrySelect.value = "doc_aaa";
        dom.sectionSelect.value = "sec-1";
        dom.textarea.value = "content";
        dom.saveButton.disabled = false;

        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x", indexed_chunks: 1 } },
            },
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x", content: "content" } },
            },
        ]);

        await clickSave();

        assert.equal(
            fetchCalls[0].url,
            "https://example.test/wp-admin/admin-post.php"
        );
    });

    test("Cancel while a Save is still in flight leaves the country dropdown usable, not stuck disabled", async () => {
        dom.countrySelect.value = "doc_aaa";
        dom.sectionSelect.value = "sec-1";
        dom.textarea.value = "content";
        dom.saveButton.disabled = false;

        let resolveSave;

        global.fetch = (url) => {
            fetchCalls.push({ url });

            return new Promise((resolve) => {
                resolveSave = () => resolve(makeFakeResponse({
                    ok: true,
                    status: 200,
                    payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x", indexed_chunks: 1 } },
                }));
            });
        };

        const savePromise = clickSave();

        // The save's own first await is still pending - Save
        // disables the country dropdown while it works.
        assert.equal(dom.countrySelect.disabled, true);

        clickCancel();

        // Cancel must restore the country dropdown to usable
        // immediately, even though the save it interrupted has not
        // resolved yet - the dropdown must never stay stuck disabled
        // with no way to pick a country again.
        assert.equal(dom.countrySelect.disabled, false);
        assert.equal(dom.countrySelect.value, "");

        resolveSave();
        await savePromise;

        // The now-stale save's own continuation must not re-disable
        // (or otherwise touch) what Cancel already reset.
        assert.equal(dom.countrySelect.disabled, false);
        assert.equal(dom.countrySelect.value, "");
    });

    test("Restore posts to the real admin-post URL with the restore action/nonce, then re-fetches the persisted value", async () => {
        dom.countrySelect.value = "doc_aaa";
        dom.sectionSelect.value = "sec-1";
        dom.textarea.value = "A manually edited draft the admin wants to discard.";
        dom.saveButton.disabled = false;
        dom.restoreButton.disabled = false;

        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "Working Time", indexed_chunks: 4 } },
            },
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "Working Time", content: "The document's own real content." },
                },
            },
        ]);

        await clickRestore();

        assert.equal(fetchCalls.length, 2);
        assert.equal(fetchCalls[0].url, "https://example.test/wp-admin/admin-post.php");
        assert.equal(fetchCalls[0].options.method, "POST");
        assert.equal(fetchCalls[0].options.body.get("action"), "le_global_chatbot_restore_section");
        assert.equal(fetchCalls[0].options.body.get("nonce"), "section-restore-nonce");
        assert.equal(fetchCalls[0].options.body.get("document_id"), "doc_aaa");
        assert.equal(fetchCalls[0].options.body.get("section_id"), "sec-1");
        assert.equal(fetchCalls[0].options.body.get("content"), null);
        // Second call: the re-fetch, showing the value really persisted.
        assert.match(fetchCalls[1].url, /action=le_global_chatbot_get_section/);
        assert.equal(dom.textarea.value, "The document's own real content.");
        assert.equal(dom.messageEl.className.includes("is-success"), true);
        assert.equal(dom.saveButton.disabled, false);
        assert.equal(dom.restoreButton.disabled, false);
    });

    test("Restore's confirmation dialog declined makes zero network calls and zero mutation", async () => {
        dom.countrySelect.value = "doc_aaa";
        dom.sectionSelect.value = "sec-1";
        dom.textarea.value = "content the admin typed";
        dom.saveButton.disabled = false;
        dom.restoreButton.disabled = false;

        global.window.confirm = () => false;

        await clickRestore();

        assert.equal(fetchCalls.length, 0);
        assert.equal(dom.textarea.value, "content the admin typed");
        assert.equal(dom.restoreButton.disabled, false);
    });

    test("Restore is a single mutation even under a double click", async () => {
        dom.countrySelect.value = "doc_aaa";
        dom.sectionSelect.value = "sec-1";
        dom.textarea.value = "Some content.";
        dom.saveButton.disabled = false;
        dom.restoreButton.disabled = false;

        let resolveFirstFetch;
        let callCount = 0;

        global.fetch = (url, options) => {
            fetchCalls.push({ url, options });
            callCount += 1;

            if (callCount === 1) {
                return new Promise((resolve) => {
                    resolveFirstFetch = () => resolve(makeFakeResponse({
                        ok: true,
                        status: 200,
                        payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x", indexed_chunks: 4 } },
                    }));
                });
            }

            return Promise.resolve(makeFakeResponse({
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x", content: "Restored content." } },
            }));
        };

        const firstRestore = clickRestore();
        // The button is disabled synchronously before the first await
        // yields - a second click while still in flight must be a
        // true no-op, never a second mutation.
        const secondRestore = clickRestore();

        resolveFirstFetch();
        await firstRestore;
        await secondRestore;

        assert.equal(
            fetchCalls.filter((call) => call.options.method === "POST").length,
            1
        );
    });

    test("a structured backend error from Restore is surfaced verbatim, never the generic fallback", async () => {
        dom.countrySelect.value = "doc_aaa";
        dom.sectionSelect.value = "sec-1";
        dom.textarea.value = "content";
        dom.saveButton.disabled = false;
        dom.restoreButton.disabled = false;

        queueFetchResponses([
            {
                ok: false,
                status: 502,
                payload: { success: false, data: { message: "The section could not be restored: the source DOCX is missing." } },
            },
        ]);

        await clickRestore();

        assert.equal(dom.messageEl.textContent, "The section could not be restored: the source DOCX is missing.");
        assert.equal(dom.messageEl.className.includes("is-error"), true);
        assert.equal(dom.saveButton.disabled, false);
        assert.equal(dom.restoreButton.disabled, false);
    });

    test("Cancel while a Restore is still in flight leaves the country dropdown usable, not stuck disabled", async () => {
        dom.countrySelect.value = "doc_aaa";
        dom.sectionSelect.value = "sec-1";
        dom.textarea.value = "content";
        dom.saveButton.disabled = false;
        dom.restoreButton.disabled = false;

        let resolveRestore;

        global.fetch = (url) => {
            fetchCalls.push({ url });

            return new Promise((resolve) => {
                resolveRestore = () => resolve(makeFakeResponse({
                    ok: true,
                    status: 200,
                    payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x", indexed_chunks: 4 } },
                }));
            });
        };

        const restorePromise = clickRestore();

        assert.equal(dom.countrySelect.disabled, true);

        clickCancel();

        assert.equal(dom.countrySelect.disabled, false);
        assert.equal(dom.countrySelect.value, "");

        resolveRestore();
        await restorePromise;

        assert.equal(dom.countrySelect.disabled, false);
        assert.equal(dom.countrySelect.value, "");
    });
});
