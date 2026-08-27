"use strict";

// Node's built-in test runner and assertions only - no framework, no
// dependency install, matching tests/chatbot.test.js. Run with:
//   node --test wordpress/le-global-chatbot/tests/admin.test.js
//
// admin.js is a browser IIFE with no module system of its own; its
// tail adds a test-only `module.exports` hook (skipped entirely in a
// real browser, where `module` is never defined) that exposes the
// pure, DOM-free parsing/classification/formatting functions plus a
// small __queueForTests seam onto the real multi-file queue engine -
// exercised here through a minimal fake DOM/fetch/FormData, never a
// real browser (that is the Playwright E2E suite's job, mission
// "ORDER 8B" - these tests complement it, they do not replace it,
// especially for DOM-interaction-heavy behavior like the documents
// search/filter/menu, which is verified end-to-end in the browser).

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
        this.hidden = false;
        this._listeners = {};

        const classes = new Set();

        this.classList = {
            add: (cls) => classes.add(cls),
            remove: (cls) => classes.delete(cls),
            toggle: (cls, force) => {
                const shouldHave = (
                    force === undefined ? !classes.has(cls) : Boolean(force)
                );

                if (shouldHave) {
                    classes.add(cls);
                } else {
                    classes.delete(cls);
                }

                return shouldHave;
            },
            contains: (cls) => classes.has(cls),
        };
    }

    querySelectorAll() {
        return [];
    }

    querySelector() {
        return null;
    }

    addEventListener(eventName, handler) {
        this._listeners[eventName] = handler;
    }
}

function makeFakeButton() {
    const classes = new Set();
    const attributes = {};

    const button = {
        disabled: false,
        hidden: false,
        textContent: "",
        dataset: {},
        _listeners: {},
        classList: {
            add: (cls) => classes.add(cls),
            remove: (cls) => classes.delete(cls),
            toggle: (cls, force) => {
                const shouldHave = (
                    force === undefined ? !classes.has(cls) : Boolean(force)
                );

                if (shouldHave) {
                    classes.add(cls);
                } else {
                    classes.delete(cls);
                }

                return shouldHave;
            },
            contains: (cls) => classes.has(cls),
        },
        setAttribute(name, value) {
            attributes[name] = String(value);
        },
        getAttribute(name) {
            return (
                Object.prototype.hasOwnProperty.call(attributes, name)
                    ? attributes[name]
                    : null
            );
        },
        addEventListener(eventName, handler) {
            button._listeners[eventName] = handler;
        },
    };

    return button;
}

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

function makeFakeTextarea() {
    const element = {
        value: "",
        disabled: false,
        _listeners: {},
        addEventListener(eventName, handler) {
            element._listeners[eventName] = handler;
        },
    };

    return element;
}

function makeFakeMessageElement() {
    return { textContent: "", className: "" };
}

function makeFakeContainer() {
    const container = {
        hidden: false,
        innerHTML: "",
        _children: [],
        appendChild(child) {
            container._children.push(child);
        },
    };

    return container;
}

function makeFakeGenericElement() {
    const element = {
        value: "",
        textContent: "",
        type: "",
        className: "",
        _listeners: {},
        addEventListener(eventName, handler) {
            element._listeners[eventName] = handler;
        },
    };

    return element;
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

function installFakeDom({
    submitHandlerHolder,
    changeHandlerHolder,
    initialFiles = [],
} = {}) {
    const fakeButton = { disabled: false, textContent: "Upload documents" };
    const fakeFallbackSubmit = makeFakeButton();

    const fakeFileInput = {
        id: "le-global-document",
        files: initialFiles,
        _listeners: {},
        addEventListener(eventName, handler) {
            fakeFileInput._listeners[eventName] = handler;

            if (eventName === "change" && changeHandlerHolder) {
                changeHandlerHolder.handler = handler;
            }
        },
    };

    const fakeActionInput = {
        name: "action",
        value: "le_global_chatbot_upload_document",
    };

    const fakeQueueContainer = new FakeElement();
    const fakeDocumentsContainer = new FakeElement();
    const fakeSummaryContainer = new FakeElement();
    const fakeDocumentsMessage = new FakeElement();
    const fakeDocumentCount = { textContent: "" };
    const fakeDropzone = new FakeElement();
    const fakeSearchInput = makeFakeGenericElement();
    const fakeStatusFilter = makeFakeGenericElement();
    const fakeConflictReviewContainer = new FakeElement();

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
            conflictReviewAction: "le_global_chatbot_conflict_review",
            conflictReviewNonce: "test-conflict-review-nonce",
            resolveConflictAction: "le_global_chatbot_resolve_conflict",
            resolveConflictNonce: "test-resolve-conflict-nonce",
            resolveConflictReplaceAction: (
                "le_global_chatbot_resolve_conflict_replace"
            ),
            resolveConflictReplaceNonce: (
                "test-resolve-conflict-replace-nonce"
            ),
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

            if (selector === ".le-global-chatbot-admin__upload-fallback-submit") {
                return fakeFallbackSubmit;
            }

            return null;
        },
        querySelectorAll(_selector) {
            return [];
        },
    };

    const byId = {
        "le-global-document": fakeFileInput,
        "le-global-chatbot-queue": fakeQueueContainer,
        "le-global-chatbot-documents": fakeDocumentsContainer,
        "le-global-chatbot-summary": fakeSummaryContainer,
        "le-global-documents-message": fakeDocumentsMessage,
        "le-global-document-count": fakeDocumentCount,
        "le-global-dropzone": fakeDropzone,
        "le-global-documents-search": fakeSearchInput,
        "le-global-documents-status-filter": fakeStatusFilter,
        "le-global-conflict-review": fakeConflictReviewContainer,
    };

    global.document = {
        querySelectorAll: () => [],
        querySelector: (selector) => {
            if (selector === ".le-global-chatbot-admin__upload-form") {
                return fakeForm;
            }

            return null;
        },
        getElementById: (id) => byId[id] || null,
        addEventListener() {},
        removeEventListener() {},
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
        fakeFallbackSubmit,
        fakeQueueContainer,
        fakeDocumentsContainer,
        fakeSummaryContainer,
        fakeDocumentsMessage,
        fakeDocumentCount,
        fakeDropzone,
        fakeSearchInput,
        fakeStatusFilter,
        fakeConflictReviewContainer,
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
    businessMessage,
    detectConflictedCountryCodes,
    computeDisplayStatus,
    formatLastUpdated,
    normalizeTitle,
    findDuplicateSectionIn,
    buildPositionOptions,
    summarizeQueue,
    rowMatchesFilter,
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
        "The document could not be added."
    );
    assert.equal(errorMessage(null), "The document could not be added.");
    assert.equal(
        errorMessage({ success: false, data: { message: "   " } }),
        "The document could not be added."
    );
});

test("errorMessage accepts a caller-supplied fallback (ORDER 8B, business-friendly generic errors)", () => {
    assert.equal(
        errorMessage({ success: false, data: {} }, "Custom fallback."),
        "Custom fallback."
    );
});

test("errorMessage surfaces the real backend reason for every HTTP error code the mission lists", () => {
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

// --- businessMessage: error-code -> business-friendly text (ORDER 8B, section 38) -

describe("businessMessage", () => {
    test("maps section_already_exists to the Edit-a-section guidance", () => {
        assert.equal(
            businessMessage(
                {
                    success: false,
                    data: { detail: { code: "section_already_exists" } },
                },
                "fallback"
            ),
            "This section already exists. Use \"Edit a section\" to update it."
        );
    });

    test("maps country_document_conflict to the support-contact message", () => {
        assert.equal(
            businessMessage(
                {
                    success: false,
                    data: { detail: { code: "country_document_conflict" } },
                },
                "fallback"
            ),
            "This country has conflicting document records. Please contact support before making changes."
        );
    });

    test("maps rollback_failed to the nothing-was-confirmed message", () => {
        assert.equal(
            businessMessage(
                { success: false, data: { detail: { code: "rollback_failed" } } },
                "fallback"
            ),
            "We couldn't save your changes. Nothing has been confirmed as completed. Please try again or contact support."
        );
    });

    test("maps document_country_not_allowed to the unsupported-country message", () => {
        assert.equal(
            businessMessage(
                {
                    success: false,
                    data: { detail: { code: "document_country_not_allowed" } },
                },
                "fallback"
            ),
            "This country is not currently supported for document uploads."
        );
    });

    test("an unmapped code falls back to the backend's own message, then the caller fallback", () => {
        assert.equal(
            businessMessage(
                {
                    success: false,
                    data: {
                        message: "Only DOCX documents are accepted.",
                        detail: { code: "something_unmapped" },
                    },
                },
                "fallback"
            ),
            "Only DOCX documents are accepted."
        );

        assert.equal(
            businessMessage({ success: false, data: {} }, "fallback"),
            "fallback"
        );
    });

    test("never leaks the raw technical code itself as the displayed text", () => {
        const message = businessMessage(
            {
                success: false,
                data: { detail: { code: "country_document_conflict" } },
            },
            "fallback"
        );

        assert.equal(message.includes("country_document_conflict"), false);
    });
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

    // Mission "ORDER 8G-B2", section 14 - byte-identical bytes, but
    // this country's document has Admin (contact/section) changes
    // since it was last accepted; this is deliberately never folded
    // into "already_current" above - it needs its own confirmable
    // decision, not a silent no-op.
    test("document_identical_but_admin_modified routes to its own identical_but_admin_modified kind, distinct from already_current", () => {
        const outcome = classifyUploadResponse(409, {
            success: false,
            data: {
                message: "The uploaded DOCX is identical to the current Germany source, but this document has changes made in the Admin.",
                detail: {
                    code: "document_identical_but_admin_modified",
                    country: "Germany",
                    country_code: "DE",
                    document_id: "doc_de",
                    admin_modified: true,
                },
            },
        });

        assert.equal(outcome.kind, "identical_but_admin_modified");
        assert.equal(outcome.detail.country, "Germany");
        assert.equal(outcome.detail.country_code, "DE");
        assert.equal(outcome.detail.document_id, "doc_de");
        assert.equal(outcome.detail.admin_modified, true);
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

    test("document_country_not_allowed (a plain 422 error) is mapped to the business-friendly text (ORDER 8B, section 38)", () => {
        const outcome = classifyUploadResponse(422, {
            success: false,
            data: {
                message: "Narnia (NA) is not currently accepted for new document uploads.",
                detail: { code: "document_country_not_allowed" },
            },
        });

        assert.equal(outcome.kind, "error");
        assert.equal(
            outcome.message,
            "This country is not currently supported for document uploads."
        );
    });
});

// --- documents table: pure status/conflict/date/filter helpers ------
//
// Mission "ORDER 8B", sections 22-27, 31-32 - these mirror the equally
// pure PHP helpers of the same name so the table renders identically
// on first load (server-rendered) and after an AJAX refresh.

describe("detectConflictedCountryCodes", () => {
    test("flags only country codes appearing more than once", () => {
        const codes = detectConflictedCountryCodes([
            { country_code: "IT" },
            { country_code: "IT" },
            { country_code: "FR" },
        ]);

        assert.equal(codes.has("IT"), true);
        assert.equal(codes.has("FR"), false);
    });

    test("ignores documents with no country_code", () => {
        const codes = detectConflictedCountryCodes([{}, { country_code: "" }]);

        assert.equal(codes.size, 0);
    });
});

describe("computeDisplayStatus", () => {
    test("a country conflict always wins, regardless of the document's own status", () => {
        // Mission "ORDER 8E-A2", section 20 - superseded from "Needs
        // attention" to the more actionable "Action required" wording,
        // since a conflict always has a Review path (unlike a
        // generically missing/unreadable source).
        const status = computeDisplayStatus({ status: "indexed" }, true);

        assert.equal(status.value, "needs_attention");
        assert.equal(status.label, "Action required");
    });

    test("status 'indexed' with no conflict is Ready", () => {
        const status = computeDisplayStatus({ status: "indexed" }, false);

        assert.equal(status.value, "ready");
        assert.equal(status.label, "Ready");
        assert.equal(status.icon, "✓");
    });

    test("any other status with no conflict is Needs attention, never a fabricated status", () => {
        const status = computeDisplayStatus(
            { status: "indexed_source_missing" },
            false
        );

        assert.equal(status.value, "needs_attention");
        assert.equal(status.label, "Needs attention");
    });

    test("status is never conveyed by color alone: label and icon are always both present", () => {
        [true, false].forEach((conflict) => {
            const status = computeDisplayStatus({ status: "indexed" }, conflict);

            assert.ok(status.icon.length > 0);
            assert.ok(status.label.length > 0);
        });
    });
});

describe("formatLastUpdated", () => {
    test("renders a business-friendly date, never a raw ISO string", () => {
        const formatted = formatLastUpdated("2026-08-14T14:32:18.921Z");

        assert.equal(formatted.includes("T"), false);
        assert.equal(formatted.includes("Z"), false);
        assert.match(formatted, /^\d{1,2} \w{3} \d{4}, \d{2}:\d{2}$/);
    });

    test("returns an em dash placeholder for a missing or invalid value", () => {
        assert.equal(formatLastUpdated(null), "—");
        assert.equal(formatLastUpdated(""), "—");
        assert.equal(formatLastUpdated("not-a-date"), "—");
    });
});

describe("rowMatchesFilter", () => {
    const row = { country: "italy", filename: "labour law italy.docx", status: "ready" };

    test("an empty query and empty status match everything", () => {
        assert.equal(rowMatchesFilter(row, "", ""), true);
    });

    test("matches by country or by filename, case-insensitively (caller lowercases)", () => {
        assert.equal(rowMatchesFilter(row, "italy", ""), true);
        assert.equal(rowMatchesFilter(row, "labour", ""), true);
        assert.equal(rowMatchesFilter(row, "france", ""), false);
    });

    test("the status filter narrows independently of the search query", () => {
        assert.equal(rowMatchesFilter(row, "", "ready"), true);
        assert.equal(rowMatchesFilter(row, "", "needs_attention"), false);
    });
});

// --- Add-a-section: pure title-matching / position-mapping helpers --

describe("normalizeTitle / findDuplicateSectionIn", () => {
    test("normalizes case and collapses whitespace before comparing", () => {
        assert.equal(normalizeTitle("  Hiring   Practices "), "hiring practices");
    });

    test("finds a duplicate regardless of case/whitespace differences", () => {
        const sections = [{ section_id: "sec-1", legal_topic: "Hiring Practices" }];

        const duplicate = findDuplicateSectionIn(sections, "  hiring   practices");

        assert.equal(duplicate.section_id, "sec-1");
    });

    test("an empty or non-matching title finds nothing", () => {
        const sections = [{ section_id: "sec-1", legal_topic: "Hiring Practices" }];

        assert.equal(findDuplicateSectionIn(sections, ""), null);
        assert.equal(findDuplicateSectionIn(sections, "Remote Working"), null);
    });
});

describe("buildPositionOptions", () => {
    test("always offers beginning/end, plus one 'After X' entry per existing section, never a raw section_id in the label", () => {
        const options = buildPositionOptions([
            { section_id: "sec-1", legal_topic: "Hiring Practices" },
            { section_id: "sec-2", legal_topic: "Termination" },
        ]);

        assert.deepEqual(
            options.map((option) => option.value),
            ["beginning", "after:sec-1", "after:sec-2", "end"]
        );
        assert.equal(options[0].label, "At the beginning");
        assert.equal(options[1].label, "After \"Hiring Practices\"");
        assert.equal(options[3].label, "At the end");
    });

    test("still offers beginning/end for a country with zero sections", () => {
        const options = buildPositionOptions([]);

        assert.deepEqual(options.map((option) => option.value), ["beginning", "end"]);
    });
});

describe("summarizeQueue", () => {
    test("zero-count categories are never included (ORDER 8B, section 9)", () => {
        const { categories } = summarizeQueue([
            { status: "indexed" },
            { status: "indexed" },
        ]);

        assert.deepEqual(categories, [{ key: "added", count: 2, icon: "✓" }]);
    });

    test("is not 'allSettled' while anything is still queued/uploading", () => {
        const summary = summarizeQueue([
            { status: "indexed" },
            { status: "uploading" },
        ]);

        assert.equal(summary.allSettled, false);
    });

    test("is 'allSettled' once every item has left queued/uploading, including awaiting-decision items", () => {
        const summary = summarizeQueue([
            { status: "indexed" },
            { status: "awaiting_replacement_confirmation" },
        ]);

        assert.equal(summary.allSettled, true);
    });

    test("awaiting_* states of all three kinds collapse into one 'needs confirmation' category", () => {
        const { categories } = summarizeQueue([
            { status: "awaiting_replacement_confirmation" },
            { status: "awaiting_warning_confirmation" },
            { status: "awaiting_combined_confirmation" },
        ]);

        assert.deepEqual(categories, [
            { key: "needs confirmation", count: 3, icon: "⚠" },
        ]);
    });

    test("a mixed batch lists only its non-zero categories, in a stable order", () => {
        const { categories } = summarizeQueue([
            { status: "indexed" },
            { status: "already_current" },
            { status: "failed" },
        ]);

        assert.deepEqual(categories, [
            { key: "added", count: 1, icon: "✓" },
            { key: "already up to date", count: 1, icon: "✓" },
            { key: "failed", count: 1, icon: "✕" },
        ]);
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
                countryConfirmed: options.body.get("country_confirmed"),
                selectedCountryCode: options.body.get(
                    "selected_country_code"
                ),
                action: options.body.get("action"),
                nonce: options.body.get("_wpnonce"),
                countryCode: options.body.get("country_code"),
                confirmContactReseed: options.body.get(
                    "confirm_contact_reseed"
                ),
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
        "409 replacement_required then Replace makes exactly two fetches, replace=false then replace=true, and ends 'replaced' (ORDER 8B terminology)",
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
            assert.equal(snapshot()[0].status, "replaced");
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
            assert.equal(snapshot()[0].status, "replaced");
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
                "The document could not be added."
            );
        }
    );

    test(
        "a queue item reads 'Uploading…' while the request is in flight (business-friendly, no technical status)",
        async () => {
            installFilesAndLoad([makeFakeFile("x.docx")]);

            let resolveFetch;

            // Only the upload POST is held open - the batch's own
            // trailing refresh (a GET, no FormData body) must resolve
            // immediately or this test's own final `await
            // submitPromise` would hang forever waiting on a second,
            // never-released promise from the same mock.
            global.fetch = (_url, options) => {
                if (options && options.body && typeof options.body.get === "function") {
                    return new Promise((resolve) => {
                        resolveFetch = () => resolve(makeFakeResponse({
                            ok: true,
                            status: 201,
                            payload: { success: true, data: { message: "ok", status: "uploaded" } },
                        }));
                    });
                }

                return Promise.resolve(makeFakeResponse({
                    ok: true,
                    status: 200,
                    payload: { success: true, data: { documents: [], stats: {} } },
                }));
            };

            const submitPromise = submit();

            await new Promise((resolve) => setTimeout(resolve, 10));

            assert.equal(snapshot()[0].status, "uploading");

            resolveFetch();
            await submitPromise;

            assert.equal(snapshot()[0].status, "indexed");
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

            resolvers[0]();
            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(
                resolvers.length,
                3,
                "Denmark's replace request fires only once a slot actually frees up"
            );

            resolvers[1]();
            resolvers[2]();
            await new Promise((resolve) => setTimeout(resolve, 20));
        }
    );

    // --- ORDER 8B, section 7: the batch-reset bug fix -------------------

    test(
        "selecting a brand-new batch after the previous one finished clears the old summary/results entirely",
        async () => {
            const dom = installFilesAndLoad([makeFakeFile("a.docx"), makeFakeFile("b.docx")]);

            queueFetchResponses([
                { ok: true, status: 201, payload: { success: true, data: { message: "ok", status: "uploaded" } } },
                { ok: true, status: 201, payload: { success: true, data: { message: "ok", status: "uploaded" } } },
            ]);

            await submit();

            assert.match(dom.fakeQueueContainer.innerHTML, /2 documents processed/);
            assert.match(dom.fakeQueueContainer.innerHTML, /2 added/);

            // The exact same files are re-selected as a brand-new batch.
            // The OLD summary/result list must be gone entirely, not
            // accumulated alongside the new one.
            moduleExports.__queueForTests.startNewBatch([
                makeFakeFile("c.docx"),
            ]);

            await new Promise((resolve) => setTimeout(resolve, 10));

            assert.equal(
                (dom.fakeQueueContainer.innerHTML.match(/document/g) || []).length
                    < 6,
                true,
                "the old batch's own summary text must not still be present"
            );
            assert.equal(
                dom.fakeQueueContainer.innerHTML.includes("2 documents processed"),
                false,
                "the previous batch's total must never leak into the new one"
            );
        }
    );

    test(
        "starting a new batch never issues a delete/removal request - it only resets the batch display",
        async () => {
            installFilesAndLoad([]);

            const methodsUsed = [];

            global.fetch = async (_url, options) => {
                methodsUsed.push((options && options.method) || "GET");

                if (options && options.body && typeof options.body.get === "function") {
                    return makeFakeResponse({
                        ok: true,
                        status: 201,
                        payload: { success: true, data: { message: "ok", status: "uploaded" } },
                    });
                }

                return makeFakeResponse({
                    ok: true,
                    status: 200,
                    payload: { success: true, data: { documents: [], stats: {} } },
                });
            };

            // startNewBatch only ever talks to the upload/refresh
            // endpoints - it has no delete/removal code path at all,
            // so there is nothing here that could remove a document.
            await moduleExports.__queueForTests.startNewBatch([makeFakeFile("a.docx")]);
            await moduleExports.__queueForTests.startNewBatch([makeFakeFile("b.docx")]);

            assert.ok(methodsUsed.length > 0);
            assert.equal(methodsUsed.includes("DELETE"), false);
        }
    );

    // --- ORDER 8B, section 8/9/10: terminology + zero-count hiding ------

    test(
        "the rendered summary never shows a zero-count category (ORDER 8B, section 9)",
        async () => {
            const dom = installFilesAndLoad([makeFakeFile("a.docx")]);

            queueFetchResponses([
                { ok: true, status: 201, payload: { success: true, data: { message: "ok", status: "uploaded" } } },
            ]);

            await submit();

            const html = dom.fakeQueueContainer.innerHTML;

            assert.match(html, /1 document processed/);
            assert.match(html, /1 added/);
            assert.equal(html.includes("Already up to date: 0"), false);
            assert.equal(html.includes("Cancelled: 0"), false);
            assert.equal(html.includes("Failed: 0"), false);
        }
    );

    test(
        "individual results render as structured lines, never 'filename.docxAdded' run together (ORDER 8B, section 10)",
        async () => {
            const dom = installFilesAndLoad([makeFakeFile("Employment Law Overview Indonesia.docx")]);

            queueFetchResponses([
                { ok: true, status: 201, payload: { success: true, data: { message: "ok", status: "uploaded" } } },
            ]);

            await submit();

            const html = dom.fakeQueueContainer.innerHTML;

            assert.equal(
                html.includes("Employment Law Overview Indonesia.docxAdded"),
                false
            );
            assert.match(
                html,
                /Employment Law Overview Indonesia\.docx<\/span><span class="le-global-chatbot-admin__queue-status">Added/
            );
        }
    );

    test(
        "upload terminology never leaks internal words like 'Indexed'/'Awaiting decision' (ORDER 8B, section 8)",
        async () => {
            const dom = installFilesAndLoad([makeFakeFile("a.docx")]);

            queueFetchResponses([
                { ok: true, status: 201, payload: { success: true, data: { message: "ok", status: "uploaded" } } },
            ]);

            await submit();

            const html = dom.fakeQueueContainer.innerHTML;

            assert.equal(html.includes(">Indexed<"), false);
            assert.equal(html.includes("Awaiting decision"), false);
            assert.match(html, />Added</);
        }
    );

    test(
        "a country-replacement decision shows the country, and the existing document's own filename when it is already known (ORDER 8B, section 11)",
        async () => {
            const dom = installFilesAndLoad([makeFakeFile("Italy-new.docx")]);

            // The catalog already knows about Italy's current document -
            // exactly what a real page load/refresh would have primed.
            global.fetch = async () => makeFakeResponse({
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: {
                        documents: [
                            {
                                document_id: "doc_a",
                                country: "Italy",
                                country_code: "IT",
                                source_filename: "Italy-old.docx",
                                status: "indexed",
                                source_file_present: true,
                            },
                        ],
                        stats: { total_documents: 1, total_countries: 1 },
                    },
                },
            });

            await moduleExports.__queueForTests.refresh();

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            message: "A document already exists for Italy.",
                            detail: {
                                code: "document_replacement_required",
                                country: "Italy",
                                country_code: "IT",
                                existing_document_ids: ["doc_a"],
                            },
                        },
                    },
                },
            ]);

            await submit();

            const html = dom.fakeQueueContainer.innerHTML;

            assert.match(html, /Italy already has a document/);
            assert.match(html, /Current document: Italy-old\.docx/);
            assert.match(html, /New document: Italy-new\.docx/);
            assert.equal(html.includes("document_id"), false);
            assert.equal(html.includes("chunk"), false);
        }
    );

    // --- ORDER 8B, section 5: dropzone / fallback submit -----------------

    test(
        "the no-JS fallback submit button is hidden once the JS enhancement wires up",
        () => {
            const dom = installFilesAndLoad([]);

            assert.equal(dom.fakeFallbackSubmit.hidden, true);
        }
    );

    test(
        "dropping files on the dropzone runs the exact same upload flow as picking them (ORDER 8B, section 5)",
        async () => {
            const dom = installFilesAndLoad([]);

            queueFetchResponses([
                { ok: true, status: 201, payload: { success: true, data: { message: "ok", status: "uploaded" } } },
            ]);

            await dom.fakeDropzone._listeners.drop({
                preventDefault: () => {},
                dataTransfer: { files: [makeFakeFile("dropped.docx")] },
            });

            assert.equal(fetchCalls.length, 1);
            assert.equal(snapshot()[0].file.name, "dropped.docx");
            assert.equal(snapshot()[0].status, "indexed");
        }
    );

    test(
        "selecting files via the real file input's change event starts the upload automatically",
        async () => {
            const changeHandlerHolder = {};
            submitHandlerHolder = {};

            const dom = installFakeDom({ submitHandlerHolder, changeHandlerHolder });
            fakeButton = dom.fakeButton;
            global.window.confirm = () => true;

            moduleExports = loadFreshAdminModule();

            dom.fakeFileInput.files = [makeFakeFile("auto.docx")];

            queueFetchResponses([
                { ok: true, status: 201, payload: { success: true, data: { message: "ok", status: "uploaded" } } },
            ]);

            await changeHandlerHolder.handler();

            assert.equal(fetchCalls.length, 1);
            assert.equal(snapshot()[0].status, "indexed");
        }
    );

    // --- ORDER 8E-A2: country confirmation / selection / conflict-replace ---

    test(
        "409 country_confirmation_required -> awaiting_country_confirmation with country name+code, Cancel makes zero further requests",
        async () => {
            installFilesAndLoad([makeFakeFile("belgium.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            message: "This document was detected as Belgium (BE). Confirm the country before it is processed further.",
                            detail: {
                                code: "document_country_confirmation_required",
                                country_code: "BE",
                                country_name: "Belgium",
                                allowed_countries: [
                                    { code: "BE", name: "Belgium" },
                                    { code: "FR", name: "France" },
                                ],
                            },
                        },
                    },
                },
            ]);

            await submit();

            assert.equal(fetchCalls.length, 1);
            assert.equal(
                snapshot()[0].status,
                "awaiting_country_confirmation"
            );
            assert.equal(snapshot()[0].detail.country_name, "Belgium");
            assert.equal(snapshot()[0].detail.country_code, "BE");

            moduleExports.__queueForTests.resolveDecision(0, "cancel");

            assert.equal(fetchCalls.length, 1);
            assert.equal(snapshot()[0].status, "cancelled");
        }
    );

    test(
        "confirm-country resubmits with country_confirmed=1 and reaches Ready",
        async () => {
            installFilesAndLoad([makeFakeFile("belgium.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            detail: {
                                code: "document_country_confirmation_required",
                                country_code: "BE",
                                country_name: "Belgium",
                            },
                        },
                    },
                },
                {
                    ok: true,
                    status: 201,
                    payload: {
                        success: true,
                        data: { message: "ok", status: "uploaded" },
                    },
                },
            ]);

            await submit();
            moduleExports.__queueForTests.resolveDecision(0, "confirm-country");

            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(fetchCalls.length, 2);
            assert.equal(fetchCalls[0].countryConfirmed, null);
            assert.equal(fetchCalls[1].countryConfirmed, "1");
            assert.equal(snapshot()[0].status, "indexed");
        }
    );

    test(
        "'Choose a different country' switches to the selection panel with no request fired, and Back restores the confirmation panel",
        async () => {
            installFilesAndLoad([makeFakeFile("belgium.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            detail: {
                                code: "document_country_confirmation_required",
                                country_code: "BE",
                                country_name: "Belgium",
                                allowed_countries: [
                                    { code: "BE", name: "Belgium" },
                                    { code: "FR", name: "France" },
                                ],
                            },
                        },
                    },
                },
            ]);

            await submit();

            moduleExports.__queueForTests.resolveDecision(0, "change-country");

            assert.equal(
                fetchCalls.length,
                1,
                "switching to manual selection must never itself submit anything"
            );
            assert.equal(
                snapshot()[0].status,
                "awaiting_country_selection"
            );
            assert.deepEqual(
                snapshot()[0].allowedCountries.map((c) => c.code),
                ["BE", "FR"]
            );

            moduleExports.__queueForTests.resolveDecision(
                0,
                "back-to-confirmation"
            );

            assert.equal(fetchCalls.length, 1);
            assert.equal(
                snapshot()[0].status,
                "awaiting_country_confirmation"
            );
            assert.equal(snapshot()[0].detail.country_name, "Belgium");
        }
    );

    test(
        "409 document_country_selection_required (no country detected) shows friendly copy and a select decision, empty selection never submits",
        async () => {
            installFilesAndLoad([makeFakeFile("mystery.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            message: (
                                "We couldn't identify the country automatically."
                            ),
                            detail: {
                                code: "document_country_selection_required",
                                allowed_countries: [
                                    { code: "FR", name: "France" },
                                    { code: "DE", name: "Germany" },
                                ],
                            },
                        },
                    },
                },
            ]);

            await submit();

            assert.equal(fetchCalls.length, 1);
            assert.equal(
                snapshot()[0].status,
                "awaiting_country_selection"
            );

            moduleExports.__queueForTests.resolveDecision(
                0,
                "select-country",
                ""
            );

            assert.equal(
                fetchCalls.length,
                1,
                "an empty selection must never be submitted to the backend"
            );
            assert.equal(
                snapshot()[0].status,
                "awaiting_country_selection"
            );
            assert.ok(snapshot()[0].selectionError);
        }
    );

    test(
        "selecting a country resubmits with selected_country_code and reaches Ready",
        async () => {
            installFilesAndLoad([makeFakeFile("mystery.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            detail: {
                                code: "document_country_selection_required",
                                allowed_countries: [
                                    { code: "FR", name: "France" },
                                ],
                            },
                        },
                    },
                },
                {
                    ok: true,
                    status: 201,
                    payload: {
                        success: true,
                        data: { message: "ok", status: "uploaded" },
                    },
                },
            ]);

            await submit();
            moduleExports.__queueForTests.resolveDecision(
                0,
                "select-country",
                "FR"
            );

            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(fetchCalls.length, 2);
            assert.equal(fetchCalls[1].selectedCountryCode, "FR");
            assert.equal(snapshot()[0].status, "indexed");
        }
    );

    test(
        "422 document_country_selection_invalid on retry keeps the selection panel open with the real backend reason",
        async () => {
            installFilesAndLoad([makeFakeFile("mystery.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            detail: {
                                code: "document_country_selection_required",
                                allowed_countries: [
                                    { code: "ZZ", name: "Nowhere" },
                                ],
                            },
                        },
                    },
                },
                {
                    ok: false,
                    status: 422,
                    payload: {
                        success: false,
                        data: {
                            message: "Nowhere (ZZ) is not currently accepted for new document uploads.",
                            detail: {
                                code: "document_country_selection_invalid",
                                allowed_countries: [
                                    { code: "ZZ", name: "Nowhere" },
                                ],
                            },
                        },
                    },
                },
            ]);

            await submit();
            moduleExports.__queueForTests.resolveDecision(
                0,
                "select-country",
                "ZZ"
            );

            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(fetchCalls.length, 2);
            assert.equal(
                snapshot()[0].status,
                "awaiting_country_selection"
            );
            assert.equal(
                snapshot()[0].selectionError,
                "That country is not supported. Please choose another one."
            );
        }
    );

    test(
        "a confirmed country is carried forward on every later resubmission, not only the round that first produced it (regression test - country confirmation was previously lost on the following decision)",
        async () => {
            installFilesAndLoad([makeFakeFile("belgium.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            detail: {
                                code: "document_country_confirmation_required",
                                country_code: "BE",
                                country_name: "Belgium",
                            },
                        },
                    },
                },
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
                {
                    ok: true,
                    status: 201,
                    payload: {
                        success: true,
                        data: { message: "ok", status: "uploaded" },
                    },
                },
            ]);

            await submit();
            moduleExports.__queueForTests.resolveDecision(0, "confirm-country");

            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(
                snapshot()[0].status,
                "awaiting_warning_confirmation"
            );

            moduleExports.__queueForTests.resolveDecision(0, "continue");

            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(fetchCalls.length, 3);
            assert.equal(
                fetchCalls[2].countryConfirmed,
                "1",
                "the earlier country confirmation must still be present on this later resubmission"
            );
            assert.equal(fetchCalls[2].confirmWarnings, "1");
            assert.equal(snapshot()[0].status, "indexed");
        }
    );

    test(
        "a hard technical failure never offers a Continue option, distinct from a content warning",
        async () => {
            const dom = installFilesAndLoad([makeFakeFile("corrupt.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 500,
                    payload: undefined,
                },
            ]);

            await submit();

            assert.equal(snapshot()[0].status, "failed");
            assert.doesNotMatch(
                dom.fakeQueueContainer.innerHTML,
                /Continue anyway/
            );
            assert.doesNotMatch(
                dom.fakeQueueContainer.innerHTML,
                /data-decision="continue"/
            );
        }
    );

    test(
        "independent per-file decisions: resolving one file's country confirmation never touches a sibling file's own request or status",
        async () => {
            installFilesAndLoad([
                makeFakeFile("belgium.docx"),
                makeFakeFile("chile.docx"),
            ]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            detail: {
                                code: "document_country_confirmation_required",
                                country_code: "BE",
                                country_name: "Belgium",
                            },
                        },
                    },
                },
                {
                    ok: true,
                    status: 201,
                    payload: {
                        success: true,
                        data: { message: "ok", status: "uploaded" },
                    },
                },
                {
                    ok: true,
                    status: 201,
                    payload: {
                        success: true,
                        data: { message: "ok", status: "uploaded" },
                    },
                },
            ]);

            await submit();

            assert.equal(fetchCalls.length, 2);
            assert.equal(
                snapshot()[0].status,
                "awaiting_country_confirmation"
            );
            assert.equal(snapshot()[1].status, "indexed");

            moduleExports.__queueForTests.resolveDecision(0, "confirm-country");

            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(
                fetchCalls.length,
                3,
                "only the resolved file may fire a new request"
            );
            assert.equal(snapshot()[0].status, "indexed");
            assert.equal(
                snapshot()[1].status,
                "indexed",
                "the sibling file's already-settled status must be untouched"
            );
        }
    );

    test(
        "REPLACE_WITH_DOCUMENT: the very first attempt already targets the conflict-resolution proxy action, never the plain upload action (regression test for a race between enqueueFiles's synchronous pumpQueue and setting resolveConflictCountryCode)",
        async () => {
            installFilesAndLoad([]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            detail: {
                                code: "document_country_confirmation_required",
                                country_code: "CZ",
                                country_name: "Czech Republic",
                            },
                        },
                    },
                },
            ]);

            moduleExports.__queueForTests.enqueueConflictReplacement(
                "CZ",
                makeFakeFile("czech-authoritative.docx")
            );

            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(fetchCalls.length, 1);
            assert.equal(
                fetchCalls[0].action,
                "le_global_chatbot_resolve_conflict_replace",
                "must never fall back to the plain upload action"
            );
            assert.equal(
                fetchCalls[0].nonce,
                "test-resolve-conflict-replace-nonce",
                "must read the nonce fresh from the DOM, never a still-null cached config"
            );
            assert.equal(fetchCalls[0].countryCode, "CZ");
            assert.equal(
                snapshot()[0].status,
                "awaiting_country_confirmation"
            );
        }
    );

    test(
        "REPLACE_WITH_DOCUMENT: confirming the country resubmits to the same proxy action with country_confirmed=1 and reaches Ready",
        async () => {
            installFilesAndLoad([]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            detail: {
                                code: "document_country_confirmation_required",
                                country_code: "CZ",
                                country_name: "Czech Republic",
                            },
                        },
                    },
                },
                {
                    ok: true,
                    status: 200,
                    payload: {
                        success: true,
                        data: { status: "replaced", country_code: "CZ" },
                    },
                },
            ]);

            moduleExports.__queueForTests.enqueueConflictReplacement(
                "CZ",
                makeFakeFile("czech-authoritative.docx")
            );

            await new Promise((resolve) => setTimeout(resolve, 20));

            moduleExports.__queueForTests.resolveDecision(0, "confirm-country");

            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(fetchCalls.length, 2);
            assert.equal(
                fetchCalls[1].action,
                "le_global_chatbot_resolve_conflict_replace"
            );
            assert.equal(fetchCalls[1].countryConfirmed, "1");
            assert.equal(fetchCalls[1].countryCode, "CZ");
            assert.equal(snapshot()[0].status, "indexed");
        }
    );

    // --- ORDER 8G-B2: identical-bytes-but-Admin-modified confirmation --
    //
    // Distinct from "document_already_current" above - the DOCX bytes
    // are unchanged, but this country's contacts/sections were edited
    // in the Admin since that source was last accepted, so the upload
    // must still offer an explicit reseed-or-cancel decision rather
    // than silently ending as "already up to date".

    test(
        "409 identical_but_admin_modified then reseed-contacts sends confirm_contact_reseed=1 and applies the resulting contact_count",
        async () => {
            installFilesAndLoad([makeFakeFile("Germany.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            message: "The uploaded DOCX is identical to the current Germany source, but this document has changes made in the Admin.",
                            detail: {
                                code: "document_identical_but_admin_modified",
                                country: "Germany",
                                country_code: "DE",
                                document_id: "doc_de",
                                admin_modified: true,
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
                            message: "Germany.docx's contacts were reseeded from the current document; Admin changes to them were discarded.",
                            status: "contacts_reseeded",
                            contact_count: 3,
                        },
                    },
                },
            ]);

            await submit();

            assert.equal(fetchCalls.length, 1);
            assert.equal(
                snapshot()[0].status,
                "awaiting_contact_reseed_confirmation"
            );

            moduleExports.__queueForTests.resolveDecision(0, "reseed-contacts");

            await new Promise((resolve) => setTimeout(resolve, 20));

            assert.equal(fetchCalls.length, 2);
            assert.equal(fetchCalls[0].confirmContactReseed, null);
            assert.equal(fetchCalls[1].confirmContactReseed, "1");
            assert.equal(snapshot()[0].status, "indexed");
            assert.equal(snapshot()[0].contactCount, 3);
        }
    );

    test(
        "409 identical_but_admin_modified then Cancel makes exactly one fetch total, zero mutation",
        async () => {
            installFilesAndLoad([makeFakeFile("Germany.docx")]);

            queueFetchResponses([
                {
                    ok: false,
                    status: 409,
                    payload: {
                        success: false,
                        data: {
                            message: "The uploaded DOCX is identical to the current Germany source, but this document has changes made in the Admin.",
                            detail: {
                                code: "document_identical_but_admin_modified",
                                country: "Germany",
                                country_code: "DE",
                                document_id: "doc_de",
                                admin_modified: true,
                            },
                        },
                    },
                },
            ]);

            await submit();

            assert.equal(fetchCalls.length, 1);
            assert.equal(
                snapshot()[0].status,
                "awaiting_contact_reseed_confirmation"
            );

            moduleExports.__queueForTests.resolveDecision(0, "cancel");

            assert.equal(fetchCalls.length, 1);
            assert.equal(snapshot()[0].status, "cancelled");
        }
    );

    test(
        "a successful upload whose resulting contact_count is exactly 0 carries that zero count in the snapshot (non-blocking informational banner, never dropped as falsy)",
        async () => {
            installFilesAndLoad([makeFakeFile("Malta.docx")]);

            queueFetchResponses([
                {
                    ok: true,
                    status: 201,
                    payload: {
                        success: true,
                        data: {
                            message: "Malta.docx was added successfully.",
                            status: "indexed",
                            contact_count: 0,
                        },
                    },
                },
            ]);

            await submit();

            assert.equal(fetchCalls.length, 1);
            assert.equal(snapshot()[0].status, "indexed");
            assert.equal(snapshot()[0].contactCount, 0);
        }
    );
});

// --- ORDER 8E-A2: country-conflict review panel -----------------------
//
// openConflictReview/handleConflictAction/closeConflictReview always
// read their action/nonce fresh from the DOM (getAdminFormConfig), so
// Review can be resolved before any AJAX refresh has ever populated
// the module-level adminFormConfig cache - see the regression tests
// above for the upload-side version of that same bug.

describe("conflict review panel", () => {
    afterEach(() => {
        delete global.document;
        delete global.window;
        delete global.FormData;
        delete global.fetch;
        delete require.cache[require.resolve(ADMIN_JS_PATH)];
    });

    function load() {
        const dom = installFakeDom({});
        global.window.confirm = () => true;
        global.window.alert = () => {};

        return { dom, moduleExports: loadFreshAdminModule() };
    }

    test("open() with a successful GET populates the review and moves to the list stage", async () => {
        const { moduleExports } = load();

        global.fetch = async (url) => {
            assert.match(url, /action=le_global_chatbot_conflict_review/);
            assert.match(url, /country_code=NO/);

            return makeFakeResponse({
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: {
                        resolution_mode: "AUTO_DEDUPLICATE",
                        candidates: [],
                    },
                },
            });
        };

        await moduleExports.__conflictReviewForTests.open("NO", "Norway");

        const state = moduleExports.__conflictReviewForTests.getState();

        assert.equal(state.stage, "list");
        assert.equal(state.review.resolution_mode, "AUTO_DEDUPLICATE");
    });

    test("open() when the GET fails moves to the error stage with a business-friendly message, never a raw backend error", async () => {
        const { moduleExports } = load();

        global.fetch = async () => makeFakeResponse({
            ok: false,
            status: 503,
            payload: undefined,
        });

        await moduleExports.__conflictReviewForTests.open("NO", "Norway");

        const state = moduleExports.__conflictReviewForTests.getState();

        assert.equal(state.stage, "error");
        assert.ok(state.error);
    });

    test("close() clears the review state entirely", async () => {
        const { moduleExports } = load();

        global.fetch = async () => makeFakeResponse({
            ok: true,
            status: 200,
            payload: { success: true, data: { candidates: [] } },
        });

        await moduleExports.__conflictReviewForTests.open("NO", "Norway");
        moduleExports.__conflictReviewForTests.close();

        assert.equal(moduleExports.__conflictReviewForTests.getState(), null);
    });

    test("'Fix duplicate' (AUTO_DEDUPLICATE) posts the resolution mode and moves to resolved on success", async () => {
        const { moduleExports } = load();

        global.fetch = async (url, options) => {
            if (!options) {
                return makeFakeResponse({
                    ok: true,
                    status: 200,
                    payload: { success: true, data: { candidates: [] } },
                });
            }

            assert.equal(
                options.body.toString(),
                "action=le_global_chatbot_resolve_conflict"
                    + "&nonce=test-resolve-conflict-nonce"
                    + "&country_code=NO"
                    + "&resolution_mode=AUTO_DEDUPLICATE"
            );

            return makeFakeResponse({
                ok: true,
                status: 200,
                payload: { success: true, data: { status: "resolved" } },
            });
        };

        await moduleExports.__conflictReviewForTests.open("NO", "Norway");
        moduleExports.__conflictReviewForTests.handleAction(
            "auto-deduplicate"
        );

        // handleConflictAction is deliberately fire-and-forget for this
        // branch (it does not return submitConflictResolution's own
        // promise), so the test waits for that in-flight mutation the
        // same way a real Admin's click-then-wait would.
        await new Promise((resolve) => setTimeout(resolve, 20));

        assert.equal(
            moduleExports.__conflictReviewForTests.getState().stage,
            "resolved"
        );
    });

    test("'Upload the correct document' (REPLACE_WITH_DOCUMENT fallback) only switches the panel to the replace stage - never 'contact developer'", async () => {
        const { moduleExports } = load();

        global.fetch = async () => makeFakeResponse({
            ok: true,
            status: 200,
            payload: { success: true, data: { candidates: [] } },
        });

        await moduleExports.__conflictReviewForTests.open("CZ", "Czech Republic");
        moduleExports.__conflictReviewForTests.handleAction("show-replace");

        assert.equal(
            moduleExports.__conflictReviewForTests.getState().stage,
            "replace"
        );
    });
});

// --- Documents table rendering (AJAX-refresh integration) -----------
//
// Mission "ORDER 8B", sections 22-30 - exercised through the same
// refresh path a real upload triggers, asserting on
// the rendered HTML string (this harness has no real DOM parser, so
// assertions are deliberately scoped to precise substrings/regexes
// rather than full markup structure - the Chromium E2E suite is the
// authority for genuine DOM interaction).

describe("documents table rendering", () => {
    let dom;
    let moduleExports;

    beforeEach(() => {
        dom = installFakeDom();
        global.window.confirm = () => true;
        moduleExports = loadFreshAdminModule();
    });

    afterEach(() => {
        delete global.document;
        delete global.window;
        delete global.FormData;
        delete global.fetch;
        delete require.cache[require.resolve(ADMIN_JS_PATH)];
    });

    function mockRefresh(documents, stats) {
        global.fetch = async () => makeFakeResponse({
            ok: true,
            status: 200,
            payload: { success: true, data: { documents, stats } },
        });

        return moduleExports.__queueForTests.refresh();
    }

    test("chunk counts are never shown, even though they remain present in the underlying data", async () => {
        await mockRefresh(
            [
                {
                    document_id: `doc_${"a".repeat(64)}`,
                    country: "Italy",
                    country_code: "IT",
                    source_filename: "Italy.docx",
                    reference_year: 2026,
                    status: "indexed",
                    source_file_present: true,
                    updated_at: "2026-08-14T14:32:00.000Z",
                    chunk_count: 42,
                    download_url: "https://example.test/download",
                    reindex_nonce: "n1",
                    delete_nonce: "n2",
                },
            ],
            { total_documents: 1, total_countries: 1 }
        );

        const html = dom.fakeDocumentsContainer.innerHTML;

        assert.equal(/chunk/i.test(html), false);
    });

    test("document_id is never shown as visible text, only inside the data-document-id attribute", async () => {
        const documentId = `doc_${"b".repeat(64)}`;

        await mockRefresh(
            [
                {
                    document_id: documentId,
                    country: "France",
                    country_code: "FR",
                    source_filename: "France.docx",
                    reference_year: 2025,
                    status: "indexed",
                    source_file_present: true,
                    updated_at: "2026-08-10T09:00:00.000Z",
                    download_url: "https://example.test/download",
                    reindex_nonce: "n1",
                    delete_nonce: "n2",
                },
            ],
            { total_documents: 1, total_countries: 1 }
        );

        const html = dom.fakeDocumentsContainer.innerHTML;

        assert.equal(html.includes(`data-document-id="${documentId}"`), true);
        // The id never appears as element text content (immediately
        // after a ">"), only inside the attribute value above.
        assert.equal(html.includes(`>${documentId}<`), false);
    });

    test("status 'indexed' with no conflict renders as Ready, not the raw backend status word", async () => {
        await mockRefresh(
            [
                {
                    document_id: "doc_ready",
                    country: "Spain",
                    country_code: "ES",
                    source_filename: "Spain.docx",
                    status: "indexed",
                    source_file_present: true,
                    updated_at: "2026-08-01T00:00:00.000Z",
                },
            ],
            { total_documents: 1, total_countries: 1 }
        );

        const html = dom.fakeDocumentsContainer.innerHTML;

        assert.match(html, / Ready<\/span>/);
        assert.equal(html.includes(">Indexed<"), false);
    });

    test("Last updated renders the business-friendly formatted date, never the raw ISO timestamp", async () => {
        const iso = "2026-08-14T14:32:18.921Z";

        await mockRefresh(
            [
                {
                    document_id: "doc_dates",
                    country: "Germany",
                    country_code: "DE",
                    source_filename: "Germany.docx",
                    status: "indexed",
                    source_file_present: true,
                    updated_at: iso,
                },
            ],
            { total_documents: 1, total_countries: 1 }
        );

        const html = dom.fakeDocumentsContainer.innerHTML;

        assert.equal(html.includes(iso), false);
        assert.equal(html.includes(formatLastUpdated(iso)), true);
    });

    test("the refreshed table omits Year/Refresh/Delete and exposes only Download for a document", async () => {
        await mockRefresh(
            [
                {
                    document_id: "doc_refresh",
                    country: "Belgium",
                    country_code: "BE",
                    source_filename: "Belgium.docx",
                    reference_year: 2026,
                    status: "indexed",
                    source_file_present: true,
                    updated_at: "2026-08-01T00:00:00.000Z",
                    download_url: "https://example.test/download",
                    reindex_nonce: "n1",
                    delete_nonce: "n2",
                },
            ],
            { total_documents: 1, total_countries: 1 }
        );

        const html = dom.fakeDocumentsContainer.innerHTML;

        assert.match(html, />Download<\/a>/);
        assert.equal(html.includes("<th scope=\"col\">Year</th>"), false);
        assert.equal(html.includes(">2026<"), false);
        assert.equal(html.includes("Refresh chatbot data"), false);
        assert.equal(html.includes("Delete document"), false);
        assert.equal(html.includes(">Reindex<"), false);
        assert.equal(html.includes("le-global-chatbot-admin__menu"), false);
    });

    test("a country conflict collapses into one Action required row with a Review button, never per-record raw rows", async () => {
        // Mission "ORDER 8E-A2", section 21 - superseded from "every
        // affected row shows Needs attention" (two raw Italy rows) to
        // exactly one grouped row per conflicted country, since the
        // Admin has no use for seeing the same country twice with no
        // way to tell the records apart.
        await mockRefresh(
            [
                {
                    document_id: "doc_it1",
                    country: "Italy",
                    country_code: "IT",
                    source_filename: "Italy-1.docx",
                    status: "indexed",
                    source_file_present: true,
                    reindex_nonce: "n1",
                    delete_nonce: "n2",
                },
                {
                    document_id: "doc_it2",
                    country: "Italy",
                    country_code: "IT",
                    source_filename: "Italy-2.docx",
                    status: "indexed",
                    source_file_present: true,
                    reindex_nonce: "n3",
                    delete_nonce: "n4",
                },
                {
                    document_id: "doc_fr",
                    country: "France",
                    country_code: "FR",
                    source_filename: "France.docx",
                    status: "indexed",
                    source_file_present: true,
                    reindex_nonce: "n5",
                    delete_nonce: "n6",
                },
            ],
            { total_documents: 3, total_countries: 2 }
        );

        const html = dom.fakeDocumentsContainer.innerHTML;

        assert.equal(
            (html.match(/ Action required<\/span>/g) || []).length,
            1,
            "Italy must collapse into exactly one Action required row"
        );
        assert.equal(
            (html.match(/ Ready<\/span>/g) || []).length,
            1,
            "the unaffected France row must still show Ready"
        );
        assert.equal(
            (html.match(/data-reindex-form/g) || []).length,
            0,
            "Refresh must not render for either regular or conflicted rows"
        );
        assert.equal(
            (html.match(/data-review-country-code="IT"/g) || []).length,
            1,
            "Italy's grouped row must offer exactly one Review action"
        );
        assert.equal(
            html.includes(
                "More than one document record is linked to this country."
            ),
            true
        );
        // document_id may still exist technically (e.g. the hidden
        // <input name="document_id"> the Refresh/Delete forms POST) -
        // only its VALUE must never render as visible text.
        assert.equal(html.includes(">doc_it1<"), false);
        assert.equal(html.includes(">doc_it2<"), false);
        assert.equal(html.includes(">doc_fr<"), false);
        assert.equal(html.includes("chunk"), false);
    });

    test("the document count updates after a refresh", async () => {
        await mockRefresh(
            [
                { document_id: "doc_1", country: "Italy", country_code: "IT", source_filename: "a.docx", status: "indexed", source_file_present: true },
                { document_id: "doc_2", country: "France", country_code: "FR", source_filename: "b.docx", status: "indexed", source_file_present: true },
            ],
            { total_documents: 2, total_countries: 2 }
        );

        assert.equal(dom.fakeDocumentCount.textContent, "2 documents");
    });

    test("Overview shows Documents/Countries/Countries requiring action only - no chunk or index metrics", async () => {
        // Mission "ORDER 8E-A2", section 28 - superseded from
        // "Documents needing attention" (a raw-record count) to
        // "Countries requiring action" (a deduplicated, country-level
        // count) - one conflicted country never counts once per extra
        // record.
        await mockRefresh(
            [
                { document_id: "doc_1", country: "Italy", country_code: "IT", source_filename: "a.docx", status: "indexed", source_file_present: true },
                { document_id: "doc_2", country: "Spain", country_code: "ES", source_filename: "b.docx", status: "indexed_source_missing", source_file_present: false },
            ],
            { total_documents: 2, total_countries: 2, countries_requiring_action: 1 }
        );

        const html = dom.fakeSummaryContainer.innerHTML;

        assert.match(html, /<span>Documents<\/span>/);
        assert.match(html, /<span>Countries<\/span>/);
        assert.match(html, /<span>Countries requiring action<\/span>/);
        assert.equal(/chunk/i.test(html), false);
        assert.equal(/opensearch/i.test(html), false);

        const values = [...html.matchAll(/<strong>(\d+)<\/strong>/g)].map(
            (match) => Number(match[1])
        );

        assert.deepEqual(values, [2, 2, 1]);
    });

    test("the empty-documents state never fetches search/filter data and shows no table", async () => {
        await mockRefresh([], { total_documents: 0, total_countries: 0 });

        assert.match(
            dom.fakeDocumentsContainer.innerHTML,
            /No document is currently available\./
        );
        assert.equal(dom.fakeDocumentCount.textContent, "0 documents");
    });
});

// --- Document row actions: Refresh chatbot data / Delete -------------
//
// Mission "ORDER 8B", sections 29-30, 38 - jargon-free confirmation
// text and a business-friendly success/error message rendered into
// the documents panel's own aria-live area (never window.alert).

describe("document row actions: Refresh chatbot data / Delete", () => {
    let dom;
    let moduleExports;
    let confirmCalls;
    let reindexForm;
    let deleteForm;

    function makeFakeRowForm(dataset = {}) {
        const button = makeFakeButton();

        return {
            method: "post",
            dataset,
            _listeners: {},
            addEventListener(eventName, handler) {
                this._listeners[eventName] = handler;
            },
            getAttribute(name) {
                if (name === "action") {
                    return "https://example.test/wp-admin/admin-post.php";
                }

                return null;
            },
            querySelector(selector) {
                if (selector === 'button[type="submit"]') {
                    return button;
                }

                return null;
            },
        };
    }

    beforeEach(() => {
        confirmCalls = [];
        dom = installFakeDom();

        reindexForm = makeFakeRowForm();
        deleteForm = makeFakeRowForm({
            documentName: "Italy.docx",
            countryName: "Italy",
        });

        global.document.querySelectorAll = (selector) => {
            if (selector === "[data-reindex-form]") {
                return [reindexForm];
            }

            if (selector === "[data-confirm-delete]") {
                return [deleteForm];
            }

            return [];
        };

        global.window.confirm = (message) => {
            confirmCalls.push(message);
            return true;
        };

        moduleExports = loadFreshAdminModule();
    });

    afterEach(() => {
        delete global.document;
        delete global.window;
        delete global.FormData;
        delete global.fetch;
        delete require.cache[require.resolve(ADMIN_JS_PATH)];
    });

    test("Refresh chatbot data confirms with jargon-free wording naming the document, not the index", async () => {
        global.fetch = async () => makeFakeResponse({
            ok: true,
            status: 200,
            payload: { success: true, data: { documents: [], stats: {} } },
        });

        await reindexForm._listeners.submit({ preventDefault: () => {} });

        assert.equal(confirmCalls.length, 1);
        assert.match(confirmCalls[0], /Refresh chatbot data/);
        assert.match(confirmCalls[0], /does not change the document/);
        assert.equal(/reindex/i.test(confirmCalls[0]), false);
        assert.match(dom.fakeDocumentsMessage.textContent, /Chatbot data refreshed successfully/);
        assert.equal(/chunk/i.test(dom.fakeDocumentsMessage.textContent), false);
    });

    test("declining the Refresh confirmation makes zero network calls", async () => {
        global.window.confirm = (message) => {
            confirmCalls.push(message);
            return false;
        };

        let fetchCalled = false;

        global.fetch = async () => {
            fetchCalled = true;
            return makeFakeResponse({ ok: true, status: 200, payload: { success: true, data: {} } });
        };

        await reindexForm._listeners.submit({ preventDefault: () => {} });

        assert.equal(fetchCalled, false);
    });

    test("a country_document_conflict error from Refresh maps to the business-friendly support message", async () => {
        global.fetch = async () => makeFakeResponse({
            ok: false,
            status: 409,
            payload: {
                success: false,
                data: {
                    message: "This country has conflicting document records.",
                    detail: { code: "country_document_conflict" },
                },
            },
        });

        await reindexForm._listeners.submit({ preventDefault: () => {} });

        assert.equal(
            dom.fakeDocumentsMessage.textContent,
            "This country has conflicting document records. Please contact support before making changes."
        );
        assert.equal(dom.fakeDocumentsMessage.className.includes("is-error"), true);
    });

    test("Delete confirms with the country/document name and never mentions chunks or the index", async () => {
        global.fetch = async () => makeFakeResponse({
            ok: true,
            status: 200,
            payload: { success: true, data: { documents: [], stats: {} } },
        });

        await deleteForm._listeners.submit({ preventDefault: () => {} });

        assert.equal(confirmCalls.length, 1);
        assert.match(confirmCalls[0], /Delete Italy document\?/);
        assert.match(confirmCalls[0], /'Italy\.docx' will be removed from the chatbot\./);
        assert.equal(/chunk/i.test(confirmCalls[0]), false);
        assert.match(
            dom.fakeDocumentsMessage.textContent,
            /Italy\.docx was deleted successfully\./
        );
    });

    test("a generic Delete failure maps to the 'nothing was confirmed' business message", async () => {
        global.fetch = async () => makeFakeResponse({
            ok: false,
            status: 500,
            payload: { success: false, data: { detail: { code: "rollback_failed" } } },
        });

        await deleteForm._listeners.submit({ preventDefault: () => {} });

        assert.equal(
            dom.fakeDocumentsMessage.textContent,
            "We couldn't save your changes. Nothing has been confirmed as completed. Please try again or contact support."
        );
    });
});

// --- Add / Edit a section (mission "ORDER 5D" + "ORDER 8B") ---------
//
// A dedicated, self-contained fake DOM/fetch - the Edit/Add UI has its
// own set of elements (segmented control, country/section selects,
// textareas, message, Cancel/Save/Add) never touched by the upload-
// queue or documents-table tests above.

describe("add / edit a section", () => {
    function installEditSectionFakeDom() {
        const modeEditButton = makeFakeButton();
        const modeAddButton = makeFakeButton();
        const countrySelect = makeFakeSelect();
        const editOnlyFields = { hidden: false };
        const sectionSelect = makeFakeSelect();
        const titleInput = makeFakeTextarea();
        const textarea = makeFakeTextarea();
        const editHintEl = { textContent: "" };
        const deleteButton = makeFakeButton();
        const addOnlyFields = { hidden: true };
        const addTitleInput = makeFakeTextarea();
        const addPositionSelect = makeFakeSelect();
        const duplicateWarningEl = makeFakeContainer();
        const addContentTextarea = makeFakeTextarea();
        const messageEl = makeFakeMessageElement();
        const cancelButton = makeFakeButton();
        const saveButton = makeFakeButton();
        const addSubmitButton = makeFakeButton();
        const collapseButton = makeFakeButton();
        // Server-rendered `hidden` on the real button, mirroring
        // #le-global-chatbot-edit's own default-collapsed state.
        collapseButton.hidden = true;

        const editContainer = {
            hidden: true,
            dataset: {
                adminPostUrl: "https://example.test/wp-admin/admin-post.php",
                sectionsListAction: "le_global_chatbot_list_sections",
                sectionsListNonce: "sections-list-nonce",
                sectionGetAction: "le_global_chatbot_get_section",
                sectionGetNonce: "section-get-nonce",
                sectionUpdateAction: "le_global_chatbot_update_section",
                sectionUpdateNonce: "section-update-nonce",
                sectionDeleteAction: "le_global_chatbot_delete_section",
                sectionDeleteNonce: "section-delete-nonce",
                sectionAddAction: "le_global_chatbot_add_section",
                sectionAddNonce: "section-add-nonce",
            },
        };

        const byId = {
            "le-global-chatbot-edit": editContainer,
            "le-global-mode-edit": modeEditButton,
            "le-global-mode-add": modeAddButton,
            "le-global-edit-country": countrySelect,
            "le-global-edit-only-fields": editOnlyFields,
            "le-global-edit-section": sectionSelect,
            "le-global-edit-title": titleInput,
            "le-global-edit-content": textarea,
            "le-global-edit-hint": editHintEl,
            "le-global-edit-delete": deleteButton,
            "le-global-add-only-fields": addOnlyFields,
            "le-global-add-title": addTitleInput,
            "le-global-add-position": addPositionSelect,
            "le-global-add-duplicate-warning": duplicateWarningEl,
            "le-global-add-content": addContentTextarea,
            "le-global-chatbot-edit-message": messageEl,
            "le-global-edit-cancel": cancelButton,
            "le-global-edit-save": saveButton,
            "le-global-add-submit": addSubmitButton,
            "le-global-edit-collapse": collapseButton,
        };

        global.document = {
            querySelectorAll: () => [],
            querySelector: () => null,
            getElementById: (id) => byId[id] || null,
            createElement: () => makeFakeGenericElement(),
            addEventListener() {},
            removeEventListener() {},
        };

        global.window = {
            confirm: () => true,
            alert: () => {},
            location: { reload: () => {} },
        };

        global.FormData = FakeFormData;

        return {
            modeEditButton,
            modeAddButton,
            countrySelect,
            editOnlyFields,
            editContainer,
            sectionSelect,
            titleInput,
            textarea,
            editHintEl,
            deleteButton,
            addOnlyFields,
            addTitleInput,
            addPositionSelect,
            duplicateWarningEl,
            addContentTextarea,
            messageEl,
            cancelButton,
            saveButton,
            addSubmitButton,
            collapseButton,
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
        return dom.countrySelect._listeners.change();
    }

    async function changeSection(value) {
        dom.sectionSelect.value = value;
        return dom.sectionSelect._listeners.change();
    }

    async function clickSave() {
        return dom.saveButton._listeners.click();
    }

    async function clickAdd() {
        return dom.addSubmitButton._listeners.click();
    }

    function clickCancel() {
        return dom.cancelButton._listeners.click();
    }

    function clickModeAdd() {
        return dom.modeAddButton._listeners.click();
    }

    function clickModeEdit() {
        return dom.modeEditButton._listeners.click();
    }

    function clickCollapse() {
        return dom.collapseButton._listeners.click();
    }

    const TWO_SECTIONS_RESPONSE = {
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
    };

    // --- Edit mode (unchanged contract from mission "ORDER 5D") --------

    test("selecting a country fetches its sections and populates both the Edit dropdown and the Add position dropdown", async () => {
        queueFetchResponses([TWO_SECTIONS_RESPONSE]);

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

        // Add's position dropdown: beginning / after each section / end.
        assert.deepEqual(
            dom.addPositionSelect.options.map((option) => option.value),
            ["beginning", "after:sec-1", "after:sec-2", "end"]
        );
        assert.equal(dom.addTitleInput.disabled, false);
        assert.equal(dom.addContentTextarea.disabled, false);
    });

    test("a country with zero sections disables the Edit dropdown but Add can still be used", async () => {
        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_empty", sections: [] } },
            },
        ]);

        await changeCountry("doc_empty");

        assert.equal(dom.sectionSelect.disabled, true);
        assert.equal(dom.messageEl.textContent, "This country has no editable section yet.");
        assert.deepEqual(
            dom.addPositionSelect.options.map((option) => option.value),
            ["beginning", "end"]
        );
        assert.equal(dom.addTitleInput.disabled, false);
    });

    test("clearing the country selection resets both modes' fields and makes zero network calls", async () => {
        queueFetchResponses([TWO_SECTIONS_RESPONSE]);

        await changeCountry("doc_aaa");
        await changeCountry("");

        assert.equal(fetchCalls.length, 1);
        assert.equal(dom.sectionSelect.disabled, true);
        assert.equal(dom.textarea.value, "");
        assert.equal(dom.textarea.disabled, true);
        assert.equal(dom.cancelButton.disabled, true);
        assert.equal(dom.saveButton.disabled, true);
        assert.equal(dom.addTitleInput.disabled, true);
        assert.equal(dom.addSubmitButton.disabled, true);
    });

    test("selecting a section fetches its effective content and enables Save only once the content actually changes", async () => {
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
        assert.equal(dom.textarea.value, "Employees are entitled to 25 days of paid leave.");
        assert.equal(dom.textarea.disabled, false);
        // Save is disabled until the admin actually edits the content
        // (ORDER 8B, section 14: "disabled if no real change").
        assert.equal(dom.saveButton.disabled, true);

        dom.textarea.value = "Employees are entitled to 30 days of paid leave.";
        dom.textarea._listeners.input();

        assert.equal(dom.saveButton.disabled, false);
    });

    test("re-typing the exact original content disables Save again", async () => {
        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x", content: "Original." } },
            },
        ]);

        dom.countrySelect.value = "doc_aaa";
        await changeSection("sec-1");

        dom.textarea.value = "Edited.";
        dom.textarea._listeners.input();
        assert.equal(dom.saveButton.disabled, false);

        dom.textarea.value = "Original.";
        dom.textarea._listeners.input();
        assert.equal(dom.saveButton.disabled, true);
    });

    test("Save posts the edited content and re-fetches to show the value really persisted, with a business-friendly success message", async () => {
        dom.countrySelect.value = "doc_aaa";
        dom.countrySelect.options = [{ value: "doc_aaa", textContent: "Italy (IT)" }];
        dom.countrySelect.selectedIndex = 0;
        dom.sectionSelect.value = "sec-1";
        dom.sectionSelect.options = [
            { value: "", textContent: "Select a section…" },
            { value: "sec-1", textContent: "Hiring Practices" },
        ];
        dom.sectionSelect.selectedIndex = 1;
        dom.textarea.value = "Draft text the admin just typed.";
        dom.saveButton.disabled = false;

        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "Hiring Practices" } },
            },
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: {
                        document_id: "doc_aaa",
                        sections: [{ section_id: "sec-1", legal_topic: "Hiring Practices" }],
                    },
                },
            },
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "Hiring Practices", content: "Draft text the admin just typed." },
                },
            },
        ]);

        await clickSave();

        assert.equal(fetchCalls.length, 3);
        assert.equal(fetchCalls[0].options.method, "POST");
        assert.equal(fetchCalls[0].options.body.get("content"), "Draft text the admin just typed.");
        assert.equal(fetchCalls[0].options.body.get("document_id"), "doc_aaa");
        assert.equal(fetchCalls[0].options.body.get("section_id"), "sec-1");
        assert.match(fetchCalls[1].url, /action=le_global_chatbot_list_sections/);
        assert.match(fetchCalls[2].url, /action=le_global_chatbot_get_section/);
        assert.equal(dom.textarea.value, "Draft text the admin just typed.");
        assert.equal(
            dom.messageEl.textContent,
            "✓ \"Hiring Practices\" was updated successfully. The Italy document and chatbot content are now up to date."
        );
        assert.equal(dom.messageEl.className.includes("is-success"), true);
        assert.equal(/chunk/i.test(dom.messageEl.textContent), false);
        assert.equal(/index/i.test(dom.messageEl.textContent), false);
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
                        payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x" } },
                    }));
                });
            }

            return Promise.resolve(makeFakeResponse({
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x", content: "Some content." } },
            }));
        };

        const firstSave = clickSave();
        const secondSave = clickSave();

        resolveFirstFetch();
        await firstSave;
        await secondSave;

        assert.equal(
            fetchCalls.filter((call) => call.options.method === "POST").length,
            1
        );
    });

    test("Cancel resets everything (Edit and Add) to empty and makes zero network calls", async () => {
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
        assert.equal(dom.addSubmitButton.disabled, true);
        assert.equal(dom.duplicateWarningEl.hidden, true);
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
        assert.equal(dom.sectionSelect.options[1].value, "new-sec");

        resolveFirst();
        await firstChange;

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
                payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x" } },
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
                    payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x" } },
                }));
            });
        };

        const savePromise = clickSave();

        assert.equal(dom.countrySelect.disabled, true);

        clickCancel();

        assert.equal(dom.countrySelect.disabled, false);
        assert.equal(dom.countrySelect.value, "");

        resolveSave();
        await savePromise;

        assert.equal(dom.countrySelect.disabled, false);
        assert.equal(dom.countrySelect.value, "");
    });

    // --- Mode toggle (ORDER 8B, section 12) ------------------------------

    test("the segmented control defaults to Edit mode and toggling shows/hides the right fields", () => {
        assert.equal(dom.editOnlyFields.hidden, false);
        assert.equal(dom.addOnlyFields.hidden, true);
        assert.equal(dom.saveButton.hidden, false);
        assert.equal(dom.addSubmitButton.hidden, true);

        clickModeAdd();

        assert.equal(dom.editOnlyFields.hidden, true);
        assert.equal(dom.addOnlyFields.hidden, false);
        assert.equal(dom.saveButton.hidden, true);
        assert.equal(dom.addSubmitButton.hidden, false);

        clickModeEdit();

        assert.equal(dom.editOnlyFields.hidden, false);
        assert.equal(dom.addOnlyFields.hidden, true);
    });

    test("switching to Add mode with unsaved Edit content asks for confirmation; declining keeps the content and stays in Edit mode", async () => {
        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "x", content: "Original." } },
            },
        ]);

        dom.countrySelect.value = "doc_aaa";
        await changeSection("sec-1");

        dom.textarea.value = "Edited but not saved.";
        dom.textarea._listeners.input();

        global.window.confirm = () => false;

        clickModeAdd();

        assert.equal(dom.editOnlyFields.hidden, false, "must stay in Edit mode");
        assert.equal(dom.textarea.value, "Edited but not saved.", "content must be preserved");
    });

    test("switching modes with no unsaved changes never prompts", async () => {
        let confirmCalled = false;
        global.window.confirm = () => {
            confirmCalled = true;
            return true;
        };

        clickModeAdd();

        assert.equal(confirmCalled, false);
    });

    // --- Add flow (ORDER 8B, sections 15-18) -----------------------------

    test("Add posts title/content/position, then re-fetches sections so the new one is immediately usable in Edit mode", async () => {
        queueFetchResponses([TWO_SECTIONS_RESPONSE]);
        await changeCountry("doc_aaa");

        dom.countrySelect.options = [{ value: "doc_aaa", textContent: "Italy (IT)" }];
        dom.countrySelect.selectedIndex = 0;

        clickModeAdd();

        dom.addTitleInput.value = "Remote Working";
        dom.addTitleInput._listeners.input();
        dom.addContentTextarea.value = "Employees may work remotely up to 2 days a week.";
        dom.addContentTextarea._listeners.input();
        dom.addPositionSelect.value = "after:sec-1";
        dom.addPositionSelect._listeners.change();

        assert.equal(dom.addSubmitButton.disabled, false);

        fetchCalls.length = 0;
        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_aaa", legal_topic: "Remote Working" } },
            },
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: {
                        sections: [
                            { section_id: "sec-1", legal_topic: "Working Time" },
                            { section_id: "sec-3", legal_topic: "Remote Working" },
                            { section_id: "sec-2", legal_topic: "Termination" },
                        ],
                    },
                },
            },
        ]);

        await clickAdd();

        assert.equal(fetchCalls.length, 2);
        assert.equal(fetchCalls[0].options.method, "POST");
        assert.equal(fetchCalls[0].options.body.get("title"), "Remote Working");
        assert.equal(
            fetchCalls[0].options.body.get("content"),
            "Employees may work remotely up to 2 days a week."
        );
        assert.equal(fetchCalls[0].options.body.get("position"), "after:sec-1");
        assert.match(fetchCalls[1].url, /action=le_global_chatbot_list_sections/);

        assert.equal(
            dom.messageEl.textContent,
            "✓ \"Remote Working\" was added successfully. The Italy document and chatbot content are now up to date."
        );
        assert.equal(/parser/i.test(dom.messageEl.textContent), false);
        assert.equal(/index/i.test(dom.messageEl.textContent), false);

        // The new section is immediately present for Edit mode.
        assert.equal(dom.sectionSelect.options.length, 4);
        assert.ok(
            dom.sectionSelect.options.some((option) => option.value === "sec-3")
        );

        // The Add form itself clears for the next entry.
        assert.equal(dom.addTitleInput.value, "");
        assert.equal(dom.addContentTextarea.value, "");
    });

    test("a duplicate section title disables Add and shows the business-friendly warning, without ever calling the backend", async () => {
        queueFetchResponses([TWO_SECTIONS_RESPONSE]);
        await changeCountry("doc_aaa");

        clickModeAdd();

        dom.addTitleInput.value = "working time";
        dom.addTitleInput._listeners.input();
        dom.addContentTextarea.value = "Some content.";
        dom.addContentTextarea._listeners.input();
        dom.addPositionSelect.value = "end";
        dom.addPositionSelect._listeners.change();

        assert.equal(dom.duplicateWarningEl.hidden, false);
        assert.equal(dom.addSubmitButton.disabled, true);
        assert.match(
            dom.duplicateWarningEl._children[0].textContent,
            /"Working Time" already exists for this country/
        );

        const fetchCallsBefore = fetchCalls.length;
        await clickAdd();

        assert.equal(fetchCalls.length, fetchCallsBefore, "the backend must never be called for a known duplicate");
    });

    test("the duplicate warning's inline switch button jumps to Edit mode with that section selected", async () => {
        queueFetchResponses([TWO_SECTIONS_RESPONSE]);
        await changeCountry("doc_aaa");

        clickModeAdd();

        dom.addTitleInput.value = "Termination";
        dom.addTitleInput._listeners.input();

        const switchButton = dom.duplicateWarningEl._children[1];

        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: { document_id: "doc_aaa", section_id: "sec-2", legal_topic: "Termination", content: "Existing content." },
                },
            },
        ]);

        await switchButton._listeners.click();

        assert.equal(dom.editOnlyFields.hidden, false);
        assert.equal(dom.sectionSelect.value, "sec-2");
        assert.equal(dom.textarea.value, "Existing content.");
    });

    test("Add is a single mutation even under a double click", async () => {
        queueFetchResponses([TWO_SECTIONS_RESPONSE]);
        await changeCountry("doc_aaa");

        clickModeAdd();

        dom.addTitleInput.value = "Remote Working";
        dom.addTitleInput._listeners.input();
        dom.addContentTextarea.value = "Content.";
        dom.addContentTextarea._listeners.input();
        dom.addPositionSelect.value = "end";
        dom.addPositionSelect._listeners.change();

        let resolveFirst;
        let callCount = 0;

        global.fetch = (url, options) => {
            fetchCalls.push({ url, options });
            callCount += 1;

            if (callCount === 1) {
                return new Promise((resolve) => {
                    resolveFirst = () => resolve(makeFakeResponse({
                        ok: true,
                        status: 200,
                        payload: { success: true, data: { document_id: "doc_aaa", legal_topic: "Remote Working" } },
                    }));
                });
            }

            return Promise.resolve(makeFakeResponse({
                ok: true,
                status: 200,
                payload: { success: true, data: { sections: [] } },
            }));
        };

        const firstAdd = clickAdd();
        const secondAdd = clickAdd();

        resolveFirst();
        await firstAdd;
        await secondAdd;

        assert.equal(
            fetchCalls.filter((call) => call.options.method === "POST").length,
            1
        );
    });

    test("a section_already_exists error from the backend maps to the business-friendly message, never the raw code", async () => {
        queueFetchResponses([TWO_SECTIONS_RESPONSE]);
        await changeCountry("doc_aaa");

        clickModeAdd();

        dom.addTitleInput.value = "Something New";
        dom.addTitleInput._listeners.input();
        dom.addContentTextarea.value = "Content.";
        dom.addContentTextarea._listeners.input();
        dom.addPositionSelect.value = "end";
        dom.addPositionSelect._listeners.change();

        global.fetch = async () => makeFakeResponse({
            ok: false,
            status: 409,
            payload: {
                success: false,
                data: { detail: { code: "section_already_exists" } },
            },
        });

        await clickAdd();

        assert.equal(
            dom.messageEl.textContent,
            "This section already exists. Use \"Edit a section\" to update it."
        );
        assert.equal(dom.messageEl.textContent.includes("section_already_exists"), false);
    });

    test("changing country while Add has unsaved input asks for confirmation; accepting clears Add's fields", async () => {
        queueFetchResponses([TWO_SECTIONS_RESPONSE]);
        await changeCountry("doc_aaa");

        clickModeAdd();

        dom.addTitleInput.value = "Draft title";
        dom.addTitleInput._listeners.input();

        global.window.confirm = () => true;

        queueFetchResponses([TWO_SECTIONS_RESPONSE]);
        dom.countrySelect.value = "";
        await dom.countrySelect._listeners.change();

        assert.equal(dom.addTitleInput.value, "");
    });

    test("Cancel with unsaved Add input asks for confirmation; declining leaves the draft untouched", () => {
        dom.countrySelect.value = "doc_aaa";
        clickModeAdd();

        dom.addTitleInput.value = "Draft";
        dom.addTitleInput._listeners.input();

        global.window.confirm = () => false;

        clickCancel();

        assert.equal(dom.addTitleInput.value, "Draft", "declining must never discard the draft");
    });

    // --- Collapsed-by-default panel (mission "ORDER 8G-A", section 9) -

    test("A: fresh page load - form hidden, both mode buttons and the collapse control usable/hidden as expected", () => {
        assert.equal(dom.editContainer.hidden, true);
        assert.equal(dom.collapseButton.hidden, true);
        assert.equal(dom.modeEditButton.hidden, false);
        assert.equal(dom.modeAddButton.hidden, false);
    });

    test("B: clicking Edit a section shows the Edit form and reveals the dedicated collapse control", () => {
        clickModeEdit();

        assert.equal(dom.editContainer.hidden, false);
        assert.equal(dom.collapseButton.hidden, false);
    });

    test("F: clicking + Add a new section shows the Add form and reveals the dedicated collapse control", () => {
        clickModeAdd();

        assert.equal(dom.editContainer.hidden, false);
        assert.equal(dom.collapseButton.hidden, false);
    });

    test("expand() does not depend on setMode's own no-op guard - Edit is already the default mode, but the panel still starts collapsed", () => {
        assert.equal(dom.editContainer.hidden, true);

        clickModeEdit();

        assert.equal(dom.editContainer.hidden, false);
    });

    // --- ORDER 8G-A.2: Collapse is presentation-only, Cancel resets only -

    test("C/D: Collapse hides only the form (country/section/content preserved), and reopening Edit shows the same state", async () => {
        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "Hiring Practices", content: "Original content." } },
            },
        ]);

        clickModeEdit();
        dom.countrySelect.value = "doc_aaa";
        await changeSection("sec-1");
        dom.textarea.value = "Edited but not yet saved.";
        dom.textarea._listeners.input();

        clickCollapse();

        assert.equal(dom.editContainer.hidden, true, "the form itself must be hidden");
        assert.equal(dom.collapseButton.hidden, true);
        assert.equal(dom.countrySelect.value, "doc_aaa", "collapse must not reset the country");
        assert.equal(dom.sectionSelect.value, "sec-1", "collapse must not reset the section");
        assert.equal(dom.textarea.value, "Edited but not yet saved.", "collapse must not reset the content");

        clickModeEdit();

        assert.equal(dom.editContainer.hidden, false, "reopening Edit must show the form again");
        assert.equal(dom.countrySelect.value, "doc_aaa");
        assert.equal(dom.sectionSelect.value, "sec-1");
        assert.equal(dom.textarea.value, "Edited but not yet saved.", "unsaved content must survive collapse/reopen");
    });

    test("G: entering Add content, collapsing, then reopening Add preserves the draft", () => {
        clickModeAdd();
        dom.addTitleInput.value = "Remote Working";
        dom.addTitleInput._listeners.input();
        dom.addContentTextarea.value = "Employees may work remotely.";
        dom.addContentTextarea._listeners.input();

        clickCollapse();

        assert.equal(dom.editContainer.hidden, true);
        assert.equal(dom.addTitleInput.value, "Remote Working", "collapse must not reset the Add title");
        assert.equal(dom.addContentTextarea.value, "Employees may work remotely.", "collapse must not reset the Add content");

        clickModeAdd();

        assert.equal(dom.editContainer.hidden, false);
        assert.equal(dom.addTitleInput.value, "Remote Working");
        assert.equal(dom.addContentTextarea.value, "Employees may work remotely.");
    });

    test("COLLAPSE_NO_DISCARD_WARNING: collapsing with dirty Add content never prompts, unlike a real mode switch", () => {
        clickModeAdd();
        dom.addTitleInput.value = "Draft";
        dom.addTitleInput._listeners.input();

        let confirmCalled = false;
        global.window.confirm = () => {
            confirmCalled = true;
            return true;
        };

        clickCollapse();

        assert.equal(confirmCalled, false, "Collapse is non-destructive and must never ask to discard");
        assert.equal(dom.addTitleInput.value, "Draft", "the draft must still be there after collapsing");
    });

    test("E/H: Cancel resets the current form's fields but leaves the panel open (does not collapse)", async () => {
        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "Hiring Practices", content: "Original content." } },
            },
        ]);

        clickModeEdit();
        dom.countrySelect.value = "doc_aaa";
        await changeSection("sec-1");
        dom.textarea.value = "Edited but not yet saved.";
        dom.textarea._listeners.input();

        clickCancel();

        assert.equal(dom.editContainer.hidden, false, "Cancel must NOT collapse the panel");
        assert.equal(dom.collapseButton.hidden, false, "the collapse control stays visible - the panel is still open");
        assert.equal(dom.countrySelect.value, "", "Cancel must reset the country selection");
        assert.equal(dom.cancelButton.disabled, true, "nothing left to reset");
    });

    test("H: Cancel on a dirty Add form clears the fields but keeps the Add panel open", () => {
        clickModeAdd();
        dom.countrySelect.value = "doc_aaa";
        dom.addTitleInput.value = "Test";
        dom.addTitleInput._listeners.input();
        dom.addContentTextarea.value = "Some content.";
        dom.addContentTextarea._listeners.input();

        clickCancel();

        assert.equal(dom.editContainer.hidden, false, "Cancel must NOT collapse the Add panel");
        assert.equal(dom.addOnlyFields.hidden, false, "Add mode itself must stay selected");
        assert.equal(dom.addTitleInput.value, "", "Add title must be cleared");
        assert.equal(dom.addContentTextarea.value, "", "Add content must be cleared");
    });

    test("J: switching to Edit mode with unsaved Add content asks for confirmation; declining keeps the draft and stays in Add mode", () => {
        clickModeAdd();
        dom.countrySelect.value = "doc_aaa";
        dom.addTitleInput.value = "Unsaved draft";
        dom.addTitleInput._listeners.input();

        global.window.confirm = () => false;

        clickModeEdit();

        assert.equal(dom.addOnlyFields.hidden, false, "must stay in Add mode");
        assert.equal(dom.addTitleInput.value, "Unsaved draft", "the draft must be preserved");
    });

    // --- Section title / Rename (mission "ORDER 8G-A", sections 2-5) --

    test("selecting a section loads its current title, and changing only the title enables Save", async () => {
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
                        content: "Some content.",
                    },
                },
            },
        ]);

        dom.countrySelect.value = "doc_aaa";
        await changeSection("sec-1");

        assert.equal(dom.titleInput.value, "Working Time");
        assert.equal(dom.titleInput.disabled, false);
        assert.equal(dom.saveButton.disabled, true);

        dom.titleInput.value = "Working Hours";
        dom.titleInput._listeners.input();

        assert.equal(dom.saveButton.disabled, false);
    });

    test("Save is disabled if the title is cleared to empty, even with edited content", async () => {
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
                        content: "Some content.",
                    },
                },
            },
        ]);

        dom.countrySelect.value = "doc_aaa";
        await changeSection("sec-1");

        dom.textarea.value = "Edited content.";
        dom.textarea._listeners.input();
        assert.equal(dom.saveButton.disabled, false);

        dom.titleInput.value = "   ";
        dom.titleInput._listeners.input();
        assert.equal(dom.saveButton.disabled, true);
    });

    test("Rename: Save posts the changed title, and the response's own new section_id/title drive the re-selection", async () => {
        dom.countrySelect.value = "doc_aaa";
        dom.countrySelect.options = [{ value: "doc_aaa", textContent: "Australia (AU)" }];
        dom.countrySelect.selectedIndex = 0;
        dom.sectionSelect.value = "sec-hiring";
        dom.titleInput.value = "Remote Work Equipment Requirements";
        dom.textarea.value = "Some content.";
        dom.saveButton.disabled = false;

        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: {
                        document_id: "doc_aaa",
                        section_id: "sec-remote-work",
                        legal_topic: "Remote Work Equipment Requirements",
                    },
                },
            },
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: {
                        document_id: "doc_aaa",
                        sections: [
                            { section_id: "sec-remote-work", legal_topic: "Remote Work Equipment Requirements" },
                        ],
                    },
                },
            },
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: {
                        document_id: "doc_aaa",
                        section_id: "sec-remote-work",
                        legal_topic: "Remote Work Equipment Requirements",
                        content: "Some content.",
                    },
                },
            },
        ]);

        await clickSave();

        assert.equal(
            fetchCalls[0].options.body.get("title"),
            "Remote Work Equipment Requirements"
        );
        assert.equal(dom.sectionSelect.value, "sec-remote-work");
        assert.equal(dom.titleInput.value, "Remote Work Equipment Requirements");
        assert.match(
            dom.messageEl.textContent,
            /"Remote Work Equipment Requirements" was updated successfully/
        );
    });

    test("a duplicate title from a rename attempt maps to the business-friendly message, not the raw code", async () => {
        dom.countrySelect.value = "doc_aaa";
        dom.sectionSelect.value = "sec-1";
        dom.titleInput.value = "Hiring Practices";
        dom.textarea.value = "Some content.";
        dom.saveButton.disabled = false;

        global.fetch = async () => makeFakeResponse({
            ok: false,
            status: 409,
            payload: {
                success: false,
                data: { detail: { code: "section_already_exists" } },
            },
        });

        await clickSave();

        assert.equal(
            dom.messageEl.textContent,
            "This section already exists. Use \"Edit a section\" to update it."
        );
        assert.equal(dom.messageEl.className.includes("is-error"), true);
    });

    test("Cancel prompts for confirmation when only the title changed (content unchanged), and declining keeps the panel expanded", async () => {
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
                        content: "Same content.",
                    },
                },
            },
        ]);

        dom.countrySelect.value = "doc_aaa";
        await changeSection("sec-1");

        dom.titleInput.value = "Working Hours";
        dom.titleInput._listeners.input();

        let confirmCalled = false;
        global.window.confirm = () => {
            confirmCalled = true;
            return false;
        };

        clickCancel();

        assert.equal(confirmCalled, true);
        assert.equal(dom.titleInput.value, "Working Hours", "declining must never discard the edit");
    });

    // --- Delete section (mission "ORDER 8G-A", sections 6-8) ----------

    test("Delete: declining the confirmation makes zero network calls", async () => {
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
                        content: "x",
                    },
                },
            },
        ]);

        dom.countrySelect.value = "doc_aaa";
        await changeSection("sec-1");

        assert.equal(dom.deleteButton.disabled, false);
        assert.equal(fetchCalls.length, 1);

        global.window.confirm = () => false;

        await dom.deleteButton._listeners.click();

        assert.equal(fetchCalls.length, 1, "Delete must never call fetch when declined");
    });

    test("Delete: confirming shows the exact required wording, removes the section, and shows a success message", async () => {
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
                        content: "x",
                    },
                },
            },
        ]);

        dom.countrySelect.value = "doc_aaa";
        dom.countrySelect.options = [{ value: "doc_aaa", textContent: "Italy (IT)" }];
        dom.countrySelect.selectedIndex = 0;
        await changeSection("sec-1");

        let confirmMessage = null;
        global.window.confirm = (message) => {
            confirmMessage = message;
            return true;
        };

        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: { document_id: "doc_aaa", section_id: "sec-1", legal_topic: "Working Time" },
                },
            },
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { document_id: "doc_aaa", sections: [] } },
            },
        ]);

        await dom.deleteButton._listeners.click();

        assert.match(confirmMessage, /Delete "Working Time"\?/);
        assert.match(
            confirmMessage,
            /This section will be removed from the Italy document and will no longer be available to the chatbot\./
        );
        assert.equal(dom.sectionSelect.value, "");
        assert.equal(dom.titleInput.value, "");
        assert.equal(dom.deleteButton.disabled, true);
        assert.match(dom.messageEl.textContent, /"Working Time" was deleted successfully/);
    });

    test("Delete: a section_is_last_remaining error maps to the friendly business message and re-enables Delete", async () => {
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
                        content: "x",
                    },
                },
            },
        ]);

        dom.countrySelect.value = "doc_aaa";
        await changeSection("sec-1");

        global.fetch = async () => makeFakeResponse({
            ok: false,
            status: 409,
            payload: {
                success: false,
                data: { detail: { code: "section_is_last_remaining" } },
            },
        });

        await dom.deleteButton._listeners.click();

        assert.equal(
            dom.messageEl.textContent,
            "This section cannot be deleted because it is the only remaining section in this document."
        );
        assert.equal(dom.messageEl.className.includes("is-error"), true);
        assert.equal(dom.deleteButton.disabled, false, "must re-enable Delete after a failed attempt");
        // The section must not have been discarded from the UI on a
        // failed delete - the admin can still see/edit it.
        assert.equal(dom.sectionSelect.value, "sec-1");
    });
});

// --- Contacts panel (mission "ORDER 8G-B2") --------------------------
//
// A dedicated, self-contained fake DOM/fetch for wireContactsPanel() -
// mirrors "add / edit a section" above (segmented View/Add control,
// its own dedicated Collapse control, Cancel-resets-only-never-
// collapses, the same UNSAVED_CHANGES_PROMPT dirty-guard on mode
// switches) plus one extra, purely client-side sub-state View mode
// alone has: "list" vs "edit-one" (editing exactly one contact).
//
// admin.js dispatches every write (Add/Update/Delete) through the
// ordinary admin-post.php POST convention, exactly like every other
// action in this file - "PUT"/"DELETE" only describe the WordPress
// proxy's own *internal* request to the backend (see
// class-le-global-chatbot-admin.php's handle_update_contact/
// handle_delete_contact), never the browser-side fetch method, which
// is always POST here.

describe("contacts panel", () => {
    // The shared makeFakeGenericElement()/makeFakeButton() etc. (top
    // of file) do not implement appendChild/track children -
    // wireContactsPanel() builds each contact card via repeated
    // createElement()+appendChild() calls, so document.createElement
    // needs its own small node here that supports exactly that.
    function makeFakeCardNode() {
        const node = {
            textContent: "",
            className: "",
            type: "",
            hidden: false,
            files: [],
            _children: [],
            _listeners: {},
            appendChild(child) {
                node._children.push(child);
                return child;
            },
            addEventListener(eventName, handler) {
                node._listeners[eventName] = handler;
            },
            removeAttribute() {},
        };

        // A real <input type="file">.value = "" also clears its own
        // .files (the one browser behavior clearPhotoControl()'s
        // input.value = "" relies on to actually discard a pending
        // selection, not merely hide it) - modeled here since these
        // two properties are otherwise unrelated on a plain fake node.
        let value = "";

        Object.defineProperty(node, "value", {
            get() {
                return value;
            },
            set(next) {
                value = next;

                if (next === "") {
                    node.files = [];
                }
            },
        });

        return node;
    }

    // For the two byId, pre-existing containers (#le-global-contact-
    // list, #le-global-contact-zero-warning) the real code appends
    // into and clears via `.textContent = ""` (never innerHTML) -
    // mirrors makeFakeSelect()'s own textContent-clears-children
    // convention above.
    function makeFakeAppendableContainer() {
        const container = {
            hidden: false,
            _children: [],
            appendChild(child) {
                container._children.push(child);
                return child;
            },
        };

        Object.defineProperty(container, "textContent", {
            get() {
                return "";
            },
            set() {
                container._children = [];
            },
        });

        return container;
    }

    // Recursively gathers every textContent under a rendered card -
    // used only for the one "contact_id/document_id never leaks into
    // user-facing text" check below, never for asserting on the
    // card's full rendered markup (that is the E2E suite's job).
    function collectTextContent(node) {
        let text = node.textContent || "";

        (node._children || []).forEach((child) => {
            text += " " + collectTextContent(child);
        });

        return text;
    }

    function installContactsFakeDom() {
        const modeViewButton = makeFakeButton();
        const modeAddButton = makeFakeButton();
        const countrySelect = makeFakeSelect();
        const viewOnlyFields = { hidden: false };
        const zeroWarningEl = makeFakeAppendableContainer();
        zeroWarningEl.hidden = true;
        const listEl = makeFakeAppendableContainer();
        const addAnotherButton = makeFakeButton();
        addAnotherButton.hidden = true;
        const editFieldsEl = { hidden: true };
        const editIdInput = makeFakeGenericElement();
        const editMemberFirmInput = makeFakeGenericElement();
        const editContactPersonInput = makeFakeGenericElement();
        const editEmailInput = makeFakeGenericElement();
        const editPhoneInput = makeFakeGenericElement();
        const editAddressInput = makeFakeGenericElement();
        const editWebsiteInput = makeFakeGenericElement();

        // mountPhotoControl() anchors the photo control off this
        // input via anchorInput.closest("td") || anchorInput.
        // parentElement - the shared makeFakeGenericElement() has
        // neither, so this test-only host stands in for the real
        // <td> the actual admin page markup provides.
        editWebsiteInput.closest = () => null;
        editWebsiteInput.parentElement = makeFakeCardNode();

        [
            editMemberFirmInput,
            editContactPersonInput,
            editEmailInput,
            editPhoneInput,
            editAddressInput,
            editWebsiteInput,
        ].forEach((input) => {
            input.disabled = true;
        });

        const backToListButton = makeFakeButton();
        const addBackToListButton = makeFakeButton();
        const addOnlyFields = { hidden: true };
        const addMemberFirmInput = makeFakeGenericElement();
        const addContactPersonInput = makeFakeGenericElement();
        const addEmailInput = makeFakeGenericElement();
        const addPhoneInput = makeFakeGenericElement();
        const addAddressInput = makeFakeGenericElement();
        const addWebsiteInput = makeFakeGenericElement();

        addWebsiteInput.closest = () => null;
        addWebsiteInput.parentElement = makeFakeCardNode();

        [
            addMemberFirmInput,
            addContactPersonInput,
            addEmailInput,
            addPhoneInput,
            addAddressInput,
            addWebsiteInput,
        ].forEach((input) => {
            input.disabled = true;
        });

        const messageEl = makeFakeMessageElement();
        const cancelButton = makeFakeButton();
        cancelButton.disabled = true;
        const saveButton = makeFakeButton();
        saveButton.disabled = true;
        const addSubmitButton = makeFakeButton();
        addSubmitButton.disabled = true;
        const collapseButton = makeFakeButton();
        collapseButton.hidden = true;

        const container = {
            hidden: true,
            dataset: {
                adminPostUrl: "https://example.test/wp-admin/admin-post.php",
                contactsListAction: "le_global_chatbot_list_contacts",
                contactsListNonce: "contacts-list-nonce",
                contactAddAction: "le_global_chatbot_add_contact",
                contactAddNonce: "contact-add-nonce",
                contactUpdateAction: "le_global_chatbot_update_contact",
                contactUpdateNonce: "contact-update-nonce",
                contactDeleteAction: "le_global_chatbot_delete_contact",
                contactDeleteNonce: "contact-delete-nonce",
            },
        };

        const byId = {
            "le-global-chatbot-contacts": container,
            "le-global-contact-mode-view": modeViewButton,
            "le-global-contact-mode-add": modeAddButton,
            "le-global-contact-country": countrySelect,
            "le-global-contact-view-only-fields": viewOnlyFields,
            "le-global-contact-zero-warning": zeroWarningEl,
            "le-global-contact-list": listEl,
            "le-global-contact-add-another": addAnotherButton,
            "le-global-contact-edit-fields": editFieldsEl,
            "le-global-contact-edit-id": editIdInput,
            "le-global-contact-edit-member-firm": editMemberFirmInput,
            "le-global-contact-edit-contact-person": editContactPersonInput,
            "le-global-contact-edit-email": editEmailInput,
            "le-global-contact-edit-phone": editPhoneInput,
            "le-global-contact-edit-address": editAddressInput,
            "le-global-contact-edit-website": editWebsiteInput,
            "le-global-contact-back-to-list": backToListButton,
            "le-global-contact-add-back-to-list": addBackToListButton,
            "le-global-contact-add-only-fields": addOnlyFields,
            "le-global-contact-add-member-firm": addMemberFirmInput,
            "le-global-contact-add-contact-person": addContactPersonInput,
            "le-global-contact-add-email": addEmailInput,
            "le-global-contact-add-phone": addPhoneInput,
            "le-global-contact-add-address": addAddressInput,
            "le-global-contact-add-website": addWebsiteInput,
            "le-global-chatbot-contact-message": messageEl,
            "le-global-contact-cancel": cancelButton,
            "le-global-contact-save": saveButton,
            "le-global-contact-add-submit": addSubmitButton,
            "le-global-contact-collapse": collapseButton,
        };

        global.document = {
            querySelectorAll: () => [],
            querySelector: () => null,
            getElementById: (id) => byId[id] || null,
            createElement: () => makeFakeCardNode(),
            addEventListener() {},
            removeEventListener() {},
        };

        global.window = {
            confirm: () => true,
            alert: () => {},
            location: { reload: () => {} },
        };

        global.FormData = FakeFormData;

        // mountPhotoControl()'s showLocalPhoto() calls the browser's
        // URL.createObjectURL/revokeObjectURL - Node's own built-in
        // URL class has neither, so these are added (never replacing
        // the class itself, which new URL(...) elsewhere in this
        // file's code under test still needs) and removed again in
        // afterEach.
        global.URL.createObjectURL = (file) => `blob:fake/${file.name}`;
        global.URL.revokeObjectURL = () => {};

        return {
            container,
            modeViewButton,
            modeAddButton,
            countrySelect,
            viewOnlyFields,
            zeroWarningEl,
            listEl,
            addAnotherButton,
            editFieldsEl,
            editIdInput,
            editMemberFirmInput,
            editContactPersonInput,
            editEmailInput,
            editPhoneInput,
            editAddressInput,
            editWebsiteInput,
            backToListButton,
            addBackToListButton,
            addOnlyFields,
            addMemberFirmInput,
            addContactPersonInput,
            addEmailInput,
            addPhoneInput,
            addAddressInput,
            addWebsiteInput,
            messageEl,
            cancelButton,
            saveButton,
            addSubmitButton,
            collapseButton,
        };
    }

    let dom;
    let fetchCalls;
    let confirmCalls;
    let confirmReturnValue;

    beforeEach(() => {
        dom = installContactsFakeDom();
        fetchCalls = [];
        confirmCalls = [];
        confirmReturnValue = true;

        global.window.confirm = (message) => {
            confirmCalls.push(message);
            return confirmReturnValue;
        };

        loadFreshAdminModule();
    });

    afterEach(() => {
        delete global.document;
        delete global.window;
        delete global.FormData;
        delete global.fetch;
        delete global.URL.createObjectURL;
        delete global.URL.revokeObjectURL;
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

    function selectCountry(value) {
        dom.countrySelect.value = value;
        return dom.countrySelect._listeners.change();
    }

    function setField(input, value) {
        input.value = value;
        input._listeners.input();
    }

    function clickModeView() {
        return dom.modeViewButton._listeners.click();
    }

    function clickModeAdd() {
        return dom.modeAddButton._listeners.click();
    }

    function clickCollapse() {
        return dom.collapseButton._listeners.click();
    }

    function clickCancel() {
        return dom.cancelButton._listeners.click();
    }

    function clickSave() {
        return dom.saveButton._listeners.click();
    }

    function clickAddSubmit() {
        return dom.addSubmitButton._listeners.click();
    }

    // Reaches one level into a rendered card's own children to fire
    // its Edit/Delete button - unavoidable since there is no test-only
    // hook onto beginEditContact/confirmDeleteContact, but deliberately
    // never used to assert on the card's rendered field text (that
    // stays the E2E/canary suite's job - see the file's own header).
    function clickCardAction(index, actionIndex) {
        const card = dom.listEl._children[index];
        const actions = card._children[card._children.length - 1];

        return actions._children[actionIndex]._listeners.click();
    }

    function clickEditOnCard(index) {
        return clickCardAction(index, 0);
    }

    // mountPhotoControl() builds its own DOM subtree off the anchor
    // input's host (anchorInput.parentElement here, since these fake
    // inputs have no .closest("td")) - these dig back into that
    // subtree by the exact className the real code assigns, the only
    // way test code can reach a control never exposed via byId/dom.
    function findPhotoControlRoot(anchorInput) {
        return anchorInput.parentElement._children.find(
            (child) => child.className
                === "le-global-chatbot-admin__contact-photo-control"
        );
    }

    function findPhotoControlInput(anchorInput) {
        return findPhotoControlRoot(anchorInput)._children.find(
            (child) => child.className
                === "le-global-chatbot-admin__contact-photo-input"
        );
    }

    function makeFakePhotoFile(name = "photo.jpg", type = "image/jpeg") {
        return { name, size: 2048, type };
    }

    function selectPhotoFile(anchorInput, file) {
        const input = findPhotoControlInput(anchorInput);
        input.files = [file];
        return input._listeners.change();
    }

    function clickDeleteOnCard(index) {
        return clickCardAction(index, 1);
    }

    const CONTACT_1 = {
        contact_id: "contact-1",
        member_firm: "Acme Legal SARL",
        contact_person: "Jane Doe",
        email: "jane@example.test",
        phone: "+33 1 23 45 67 89",
        address: "1 Rue de Paris, 75001 Paris",
        website: "https://acme-legal.example",
    };

    const CONTACT_2 = {
        contact_id: "contact-2",
        member_firm: "Beta Law LLP",
        contact_person: "John Roe",
        email: "john@example.test",
        phone: "+1 555 0100",
        address: "2 Main St, Springfield",
        website: "https://beta-law.example",
    };

    const LEGACY_CONTACT = {
        contact_id: "legacy-1",
        member_firm: "Legacy Firm",
        contact_person: "Old Contact",
        email: "",
        phone: "",
        address: "",
        website: "",
    };

    const ZERO_CONTACTS_RESPONSE = {
        ok: true,
        status: 200,
        payload: { success: true, data: { contacts: [] } },
    };

    const ONE_CONTACT_RESPONSE = {
        ok: true,
        status: 200,
        payload: { success: true, data: { contacts: [CONTACT_1] } },
    };

    const TWO_CONTACTS_RESPONSE = {
        ok: true,
        status: 200,
        payload: { success: true, data: { contacts: [CONTACT_1, CONTACT_2] } },
    };

    const LEGACY_CONTACT_RESPONSE = {
        ok: true,
        status: 200,
        payload: { success: true, data: { contacts: [LEGACY_CONTACT] } },
    };

    function fillAddFields(values) {
        setField(dom.addMemberFirmInput, values.member_firm);
        setField(dom.addContactPersonInput, values.contact_person);
        setField(dom.addEmailInput, values.email);
        setField(dom.addPhoneInput, values.phone);
        setField(dom.addAddressInput, values.address);
        setField(dom.addWebsiteInput, values.website);
    }

    // --- Panel: default state, mode switches, collapse, cancel --------

    test("default state: panel collapsed, View mode active, Add-only fields hidden", () => {
        assert.equal(dom.container.hidden, true);
        assert.equal(dom.collapseButton.hidden, true);
        assert.equal(dom.viewOnlyFields.hidden, false);
        assert.equal(dom.addOnlyFields.hidden, true);
    });

    test("clicking 'View contacts' expands the panel and shows the view-only fields", () => {
        clickModeView();

        assert.equal(dom.container.hidden, false);
        assert.equal(dom.viewOnlyFields.hidden, false);
        assert.equal(dom.addOnlyFields.hidden, true);
    });

    test("the zero-contact warning stays hidden until a country is actually selected", () => {
        clickModeView();

        assert.equal(
            dom.zeroWarningEl.hidden,
            true,
            "an empty warning box must never appear before any country has been chosen"
        );
    });

    test("clicking '+ Add a contact' expands the panel and shows the add-only fields", () => {
        clickModeAdd();

        assert.equal(dom.container.hidden, false);
        assert.equal(dom.addOnlyFields.hidden, false);
        assert.equal(dom.viewOnlyFields.hidden, true);
        assert.equal(dom.saveButton.hidden, true);
        assert.equal(dom.addSubmitButton.hidden, false);
    });

    test("Collapse hides the panel while preserving in-memory field values", () => {
        clickModeAdd();
        setField(dom.addMemberFirmInput, "Draft firm name");

        clickCollapse();

        assert.equal(dom.container.hidden, true);
        assert.equal(dom.collapseButton.hidden, true);
        assert.equal(
            dom.addMemberFirmInput.value,
            "Draft firm name",
            "collapse must never clear in-progress Add field values"
        );

        // "Re-expand" the same way a real collapse/expand round trip
        // would - collapse() never touches field state, so clearing
        // the container's own hidden flag back to false is enough to
        // show the value is still there (no re-fetch/re-render needed
        // since Add mode's fields are always live DOM nodes).
        dom.container.hidden = false;

        assert.equal(dom.addMemberFirmInput.value, "Draft firm name");
    });

    test("Cancel resets the current mode's fields without collapsing", () => {
        clickModeAdd();
        setField(dom.addMemberFirmInput, "Draft firm name");

        clickCancel();

        assert.equal(dom.container.hidden, false, "Cancel must not collapse the panel");
        assert.equal(dom.addOnlyFields.hidden, false, "Add mode itself must stay selected");
        assert.equal(dom.addMemberFirmInput.value, "", "Add fields must be cleared");
    });

    test("switching mode with unsaved Add content prompts for confirmation; declining keeps the content and the current mode", () => {
        clickModeAdd();
        setField(dom.addMemberFirmInput, "Draft firm name");

        confirmReturnValue = false;

        clickModeView();

        assert.equal(confirmCalls.length, 1);
        assert.equal(dom.addOnlyFields.hidden, false, "must stay in Add mode");
        assert.equal(dom.addMemberFirmInput.value, "Draft firm name", "content must be preserved");
    });

    // --- View: fetching/rendering contacts -----------------------------

    test("selecting a country with zero contacts shows the warning and auto-switches to Add mode, expanding the panel", async () => {
        queueFetchResponses([ZERO_CONTACTS_RESPONSE]);

        dom.countrySelect.options = [
            { value: "doc_zz", textContent: "Zzedonia (ZZ)" },
        ];
        dom.countrySelect.selectedIndex = 0;

        await selectCountry("doc_zz");

        assert.equal(fetchCalls.length, 1);
        assert.match(fetchCalls[0].url, /action=le_global_chatbot_list_contacts/);
        assert.match(fetchCalls[0].url, /nonce=contacts-list-nonce/);
        assert.match(fetchCalls[0].url, /document_id=doc_zz/);

        assert.equal(dom.zeroWarningEl.hidden, false);
        assert.equal(dom.zeroWarningEl._children.length, 1);
        assert.equal(
            dom.zeroWarningEl._children[0].textContent,
            "⚠ No L&E Global contact is currently configured for Zzedonia."
        );

        // Auto-switched to Add mode, no second click needed.
        assert.equal(dom.container.hidden, false);
        assert.equal(dom.addOnlyFields.hidden, false);
        assert.equal(dom.viewOnlyFields.hidden, true);
    });

    test("one contact renders without throwing", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        await selectCountry("doc_fr");

        assert.equal(fetchCalls.length, 1);
        assert.equal(dom.zeroWarningEl.hidden, true);
        assert.equal(dom.listEl.hidden, false);
        assert.equal(dom.listEl._children.length, 1);
        assert.equal(
            dom.addOnlyFields.hidden,
            true,
            "must stay in View mode - never auto-switch when contacts already exist"
        );
    });

    test("multiple contacts for one country all get fetched and rendered without throwing", async () => {
        queueFetchResponses([TWO_CONTACTS_RESPONSE]);

        await selectCountry("doc_fr");

        assert.equal(fetchCalls.length, 1);
        assert.equal(dom.zeroWarningEl.hidden, true);
        assert.equal(dom.listEl._children.length, 2);
    });

    test("rendered contact cards never surface the internal contact_id or document_id in any user-facing text", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        await selectCountry("doc_fr");

        const cardText = dom.listEl._children
            .map((card) => collectTextContent(card))
            .join(" ");

        assert.equal(cardText.includes("contact-1"), false);
        assert.equal(cardText.includes("doc_fr"), false);
        assert.equal(dom.messageEl.textContent.includes("contact-1"), false);
        assert.equal(dom.messageEl.textContent.includes("doc_fr"), false);
    });

    // --- Add: required fields, submit, duplicates, errors --------------

    test("Add submit stays disabled until a country is selected and at least one field is filled", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        assert.equal(dom.addSubmitButton.disabled, true);

        await selectCountry("doc_fr");
        clickModeAdd();

        assert.equal(dom.addSubmitButton.disabled, true, "still empty");

        // Every field is individually optional - filling in just ONE
        // is already enough (matching the backend's own cross-field
        // "at least one field" rule), not "all six".
        setField(dom.addMemberFirmInput, "Acme Legal SARL");

        assert.equal(
            dom.addSubmitButton.disabled,
            false,
            "one filled field is enough - the other five stay optional"
        );

        setField(dom.addMemberFirmInput, "");

        assert.equal(
            dom.addSubmitButton.disabled,
            true,
            "wholly blank again"
        );

        setField(dom.addWebsiteInput, "https://example.test");

        assert.equal(dom.addSubmitButton.disabled, false);
    });

    test("a successful Add fires the correct POST with all six fields and reloads the contact list", async () => {
        queueFetchResponses([ZERO_CONTACTS_RESPONSE]);

        await selectCountry("doc_fr");
        // Zero contacts already auto-switched to Add mode.
        assert.equal(dom.addOnlyFields.hidden, false);

        fillAddFields(CONTACT_1);

        assert.equal(dom.addSubmitButton.disabled, false);

        fetchCalls.length = 0;
        queueFetchResponses([
            { ok: true, status: 200, payload: { success: true, data: { contact_id: "contact-1" } } },
            ONE_CONTACT_RESPONSE,
        ]);

        await clickAddSubmit();

        assert.equal(fetchCalls.length, 2);
        assert.equal(fetchCalls[0].options.method, "POST");
        assert.equal(fetchCalls[0].options.body.get("action"), "le_global_chatbot_add_contact");
        assert.equal(fetchCalls[0].options.body.get("nonce"), "contact-add-nonce");
        assert.equal(fetchCalls[0].options.body.get("document_id"), "doc_fr");
        assert.equal(fetchCalls[0].options.body.get("member_firm"), "Acme Legal SARL");
        assert.equal(fetchCalls[0].options.body.get("contact_person"), "Jane Doe");
        assert.equal(fetchCalls[0].options.body.get("email"), "jane@example.test");
        assert.equal(fetchCalls[0].options.body.get("phone"), "+33 1 23 45 67 89");
        assert.equal(fetchCalls[0].options.body.get("address"), "1 Rue de Paris, 75001 Paris");
        assert.equal(fetchCalls[0].options.body.get("website"), "https://acme-legal.example");

        assert.match(fetchCalls[1].url, /action=le_global_chatbot_list_contacts/);
        assert.equal(dom.addMemberFirmInput.value, "", "Add fields must be cleared after a successful submit");
        assert.match(dom.messageEl.textContent, /added successfully/);
        assert.equal(dom.messageEl.className.includes("is-success"), true);
    });

    test("selecting a photo alone in Add mode does not enable Add contact (business fields are still required)", async () => {
        queueFetchResponses([ZERO_CONTACTS_RESPONSE]);

        await selectCountry("doc_fr");
        assert.equal(dom.addOnlyFields.hidden, false);

        selectPhotoFile(dom.addWebsiteInput, makeFakePhotoFile());

        assert.equal(
            dom.addSubmitButton.disabled,
            true,
            "a contact record inherently needs its business fields, photo or not"
        );
    });

    test("a successful Add with a pending photo uploads it against the newly created contact_id", async () => {
        queueFetchResponses([ZERO_CONTACTS_RESPONSE]);

        await selectCountry("doc_fr");
        fillAddFields(CONTACT_1);
        selectPhotoFile(dom.addWebsiteInput, makeFakePhotoFile());

        fetchCalls.length = 0;
        queueFetchResponses([
            { ok: true, status: 200, payload: { success: true, data: { contact_id: "contact-1" } } },
            { ok: true, status: 200, payload: { success: true, data: {} } },
            ONE_CONTACT_RESPONSE,
        ]);

        await clickAddSubmit();

        assert.equal(fetchCalls.length, 3, "add contact, photo upload, then the contacts reload");
        assert.equal(
            fetchCalls[1].options.body.get("action"),
            "le_global_admin_contact_photo_replace"
        );
        assert.equal(fetchCalls[1].options.body.get("contact_id"), "contact-1");
        assert.match(dom.messageEl.textContent, /added successfully/);
    });

    test("a failed photo upload after a successful Add rolls back the new contact and reports a full failure", async () => {
        queueFetchResponses([ZERO_CONTACTS_RESPONSE]);

        await selectCountry("doc_fr");
        fillAddFields(CONTACT_1);
        selectPhotoFile(dom.addWebsiteInput, makeFakePhotoFile());

        fetchCalls.length = 0;
        queueFetchResponses([
            { ok: true, status: 200, payload: { success: true, data: { contact_id: "contact-1" } } },
            { ok: true, status: 200, payload: { success: false } },
            { ok: true, status: 200, payload: { success: true, data: {} } },
            ZERO_CONTACTS_RESPONSE,
        ]);

        await clickAddSubmit();

        assert.equal(
            fetchCalls.length,
            4,
            "add, failed photo upload, rollback delete, then reload"
        );
        assert.equal(
            fetchCalls[2].options.body.get("action"),
            "le_global_chatbot_delete_contact",
            "the just-created contact must be rolled back, mission 'FINAL BLOCKER' section 8"
        );
        assert.equal(
            fetchCalls[2].options.body.get("contact_id"),
            "contact-1"
        );

        assert.match(
            dom.messageEl.textContent,
            /could not be added because its photo could not be saved/,
            "a rolled-back Add must report a full failure, never a partial success"
        );
        assert.doesNotMatch(
            dom.messageEl.textContent,
            /was added/,
            "must never claim the contact was added when it was rolled back"
        );
        assert.equal(dom.messageEl.className.includes("is-error"), true);
    });

    test("if the rollback delete itself fails, an honest manual-cleanup message is shown", async () => {
        queueFetchResponses([ZERO_CONTACTS_RESPONSE]);

        await selectCountry("doc_fr");
        fillAddFields(CONTACT_1);
        selectPhotoFile(dom.addWebsiteInput, makeFakePhotoFile());

        fetchCalls.length = 0;
        queueFetchResponses([
            { ok: true, status: 200, payload: { success: true, data: { contact_id: "contact-1" } } },
            { ok: true, status: 200, payload: { success: false } },
            { ok: true, status: 200, payload: { success: false } },
            ONE_CONTACT_RESPONSE,
        ]);

        await clickAddSubmit();

        assert.match(
            dom.messageEl.textContent,
            /open Edit contact and remove it/i
        );
        assert.equal(dom.messageEl.className.includes("is-error"), true);
    });

    test("adding a second contact when one already exists is allowed - never treated as a replace/edit", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        await selectCountry("doc_fr");
        clickModeAdd();

        fillAddFields(CONTACT_2);

        fetchCalls.length = 0;
        queueFetchResponses([
            { ok: true, status: 200, payload: { success: true, data: { contact_id: "contact-2" } } },
            TWO_CONTACTS_RESPONSE,
        ]);

        await clickAddSubmit();

        assert.equal(fetchCalls.length, 2);
        assert.equal(fetchCalls[0].options.body.get("action"), "le_global_chatbot_add_contact");
        assert.equal(
            fetchCalls[0].options.body.has("contact_id"),
            false,
            "Add must never send a contact_id - that would make it an edit"
        );
        assert.equal(dom.listEl._children.length, 2, "the list reloads to show both contacts");
    });

    test("an identical duplicate contact is explicitly permitted - no client-side duplicate rejection", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        await selectCountry("doc_fr");
        clickModeAdd();

        // Exactly the same field values as the already-existing contact-1.
        fillAddFields(CONTACT_1);

        assert.equal(
            dom.addSubmitButton.disabled,
            false,
            "an exact duplicate of an existing contact must never be blocked client-side"
        );

        fetchCalls.length = 0;
        queueFetchResponses([
            { ok: true, status: 200, payload: { success: true, data: { contact_id: "contact-1-dup" } } },
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { contacts: [CONTACT_1, { ...CONTACT_1, contact_id: "contact-1-dup" }] } },
            },
        ]);

        await clickAddSubmit();

        assert.equal(fetchCalls.length, 2, "the duplicate submits normally, exactly like any other Add");
        assert.equal(fetchCalls[0].options.body.get("member_firm"), CONTACT_1.member_firm);
    });

    test("a backend API error on Add is surfaced in the message area and does not clear the entered fields", async () => {
        queueFetchResponses([ZERO_CONTACTS_RESPONSE]);

        await selectCountry("doc_fr");
        // Zero contacts already auto-switched to Add mode.

        fillAddFields(CONTACT_1);

        global.fetch = async () => makeFakeResponse({
            ok: false,
            status: 500,
            payload: { success: false, data: { message: "The contact could not be added." } },
        });

        await clickAddSubmit();

        assert.match(dom.messageEl.textContent, /could not be added/);
        assert.equal(dom.messageEl.className.includes("is-error"), true);
        assert.equal(
            dom.addMemberFirmInput.value,
            "Acme Legal SARL",
            "a failed Add must never silently discard what the admin typed"
        );
        assert.equal(dom.addWebsiteInput.value, "https://acme-legal.example");
    });

    // --- Edit: pre-population, dirty-check, save, legacy blanks, cancel -

    test("selecting Edit on a contact pre-populates all six edit fields with its current values", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        await selectCountry("doc_fr");

        clickEditOnCard(0);

        assert.equal(dom.editFieldsEl.hidden, false);
        assert.equal(dom.listEl.hidden, true);
        assert.equal(dom.editIdInput.value, "contact-1");
        assert.equal(dom.editMemberFirmInput.value, "Acme Legal SARL");
        assert.equal(dom.editContactPersonInput.value, "Jane Doe");
        assert.equal(dom.editEmailInput.value, "jane@example.test");
        assert.equal(dom.editPhoneInput.value, "+33 1 23 45 67 89");
        assert.equal(dom.editAddressInput.value, "1 Rue de Paris, 75001 Paris");
        assert.equal(dom.editWebsiteInput.value, "https://acme-legal.example");
        assert.equal(dom.editMemberFirmInput.disabled, false, "fields must become editable");
    });

    test("Save stays disabled until dirty and at least one field is still filled", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        await selectCountry("doc_fr");
        clickEditOnCard(0);

        assert.equal(dom.saveButton.disabled, true, "unchanged baseline - nothing to save yet");

        setField(dom.editMemberFirmInput, "Acme Legal SARL (renamed)");
        assert.equal(dom.saveButton.disabled, false, "dirty, and still has a filled field");

        // Clearing every field makes the contact wholly blank - a
        // single blank field is legitimately optional and must NOT
        // block Save on its own, but ALL SIX blank must.
        setField(dom.editMemberFirmInput, "");
        setField(dom.editContactPersonInput, "");
        setField(dom.editEmailInput, "");
        setField(dom.editPhoneInput, "");
        setField(dom.editAddressInput, "");
        setField(dom.editWebsiteInput, "");
        assert.equal(
            dom.saveButton.disabled,
            true,
            "wholly blank contact must keep Save disabled"
        );

        setField(dom.editMemberFirmInput, CONTACT_1.member_firm);
        setField(dom.editContactPersonInput, CONTACT_1.contact_person);
        setField(dom.editEmailInput, CONTACT_1.email);
        setField(dom.editPhoneInput, CONTACT_1.phone);
        setField(dom.editAddressInput, CONTACT_1.address);
        setField(dom.editWebsiteInput, CONTACT_1.website);
        assert.equal(dom.saveButton.disabled, true, "back to the baseline - nothing dirty");
    });

    // --- Contact photo: dirty-state, Cancel, combined Save (mission
    // "COMPLETE CONTACT PHOTO CRUD + DOCX SOURCE SYNCHRONIZATION") ------

    test("selecting a photo in Edit mode enables Save even though no text field changed", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        await selectCountry("doc_fr");
        clickEditOnCard(0);

        assert.equal(dom.saveButton.disabled, true, "unchanged baseline - nothing to save yet");

        selectPhotoFile(dom.editWebsiteInput, makeFakePhotoFile());

        assert.equal(
            dom.saveButton.disabled,
            false,
            "a newly selected photo alone must enable Save (Bug A)"
        );
    });

    test("selecting a photo for a contact that has no existing photo still enables Save", async () => {
        const noPhotoContact = { ...CONTACT_1, has_photo: false };

        queueFetchResponses([
            {
                ok: true,
                status: 200,
                payload: { success: true, data: { contacts: [noPhotoContact] } },
            },
        ]);

        await selectCountry("doc_fr");
        clickEditOnCard(0);

        selectPhotoFile(dom.editWebsiteInput, makeFakePhotoFile());

        assert.equal(dom.saveButton.disabled, false);
    });

    test("Cancel after selecting a photo discards the pending file and disables Save again", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        await selectCountry("doc_fr");
        clickEditOnCard(0);

        selectPhotoFile(dom.editWebsiteInput, makeFakePhotoFile());
        assert.equal(dom.saveButton.disabled, false);

        clickCancel();

        assert.equal(
            findPhotoControlInput(dom.editWebsiteInput).files.length,
            0,
            "Cancel must discard the pending file, not merely hide it"
        );
        assert.equal(
            dom.saveButton.disabled,
            true,
            "with the pending file discarded and no text change, Save must be disabled again"
        );
    });

    test("a successful Save with a pending photo uploads it after the text update succeeds", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        await selectCountry("doc_fr");
        clickEditOnCard(0);

        selectPhotoFile(dom.editWebsiteInput, makeFakePhotoFile());

        fetchCalls.length = 0;
        queueFetchResponses([
            { ok: true, status: 200, payload: { success: true, data: {} } },
            { ok: true, status: 200, payload: { success: true, data: {} } },
            ONE_CONTACT_RESPONSE,
        ]);

        await clickSave();

        assert.equal(fetchCalls.length, 3, "text update, photo upload, then the contacts reload");
        assert.equal(
            fetchCalls[0].options.body.get("action"),
            "le_global_chatbot_update_contact"
        );
        assert.equal(
            fetchCalls[1].options.body.get("action"),
            "le_global_admin_contact_photo_replace"
        );
        assert.equal(fetchCalls[1].options.body.get("contact_id"), "contact-1");
        assert.match(dom.messageEl.textContent, /updated successfully/);
    });

    test("a failed photo upload after a successful text save reports an honest partial failure and keeps the pending file", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        await selectCountry("doc_fr");
        clickEditOnCard(0);

        selectPhotoFile(dom.editWebsiteInput, makeFakePhotoFile());

        fetchCalls.length = 0;
        queueFetchResponses([
            { ok: true, status: 200, payload: { success: true, data: {} } },
            { ok: true, status: 200, payload: { success: false } },
        ]);

        await clickSave();

        assert.match(dom.messageEl.textContent, /photo could not be saved/);
        assert.equal(dom.messageEl.className.includes("is-error"), true);
        assert.equal(
            findPhotoControlInput(dom.editWebsiteInput).files.length,
            1,
            "a failed photo save must never silently discard the pending file"
        );
        assert.equal(
            dom.saveButton.disabled,
            false,
            "Save must re-enable so the user can retry the still-pending photo"
        );
    });

    test("a successful Save posts the update with the exact contact_id and reloads the list", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        await selectCountry("doc_fr");
        clickEditOnCard(0);

        setField(dom.editPhoneInput, "+33 9 99 99 99 99");

        fetchCalls.length = 0;
        queueFetchResponses([
            { ok: true, status: 200, payload: { success: true, data: {} } },
            {
                ok: true,
                status: 200,
                payload: {
                    success: true,
                    data: { contacts: [{ ...CONTACT_1, phone: "+33 9 99 99 99 99" }] },
                },
            },
        ]);

        await clickSave();

        assert.equal(fetchCalls.length, 2);
        assert.equal(fetchCalls[0].options.method, "POST");
        assert.equal(fetchCalls[0].options.body.get("action"), "le_global_chatbot_update_contact");
        assert.equal(fetchCalls[0].options.body.get("nonce"), "contact-update-nonce");
        assert.equal(fetchCalls[0].options.body.get("document_id"), "doc_fr");
        assert.equal(fetchCalls[0].options.body.get("contact_id"), "contact-1");
        assert.equal(fetchCalls[0].options.body.get("phone"), "+33 9 99 99 99 99");

        assert.match(fetchCalls[1].url, /action=le_global_chatbot_list_contacts/);
        assert.match(dom.messageEl.textContent, /updated successfully/);
        assert.equal(dom.messageEl.className.includes("is-success"), true);
    });

    test("a legacy contact with blank fields is viewable/editable without throwing; Save enables once dirty with at least one field still filled", async () => {
        queueFetchResponses([LEGACY_CONTACT_RESPONSE]);

        await selectCountry("doc_legacy");

        assert.doesNotThrow(() => clickEditOnCard(0));

        assert.equal(dom.editMemberFirmInput.value, "Legacy Firm");
        assert.equal(dom.editContactPersonInput.value, "Old Contact");
        assert.equal(dom.editEmailInput.value, "");
        assert.equal(dom.editPhoneInput.value, "");
        assert.equal(dom.editAddressInput.value, "");
        assert.equal(dom.editWebsiteInput.value, "");
        assert.equal(
            dom.saveButton.disabled,
            true,
            "unchanged baseline - nothing to save yet"
        );

        // Filling in just ONE previously-blank field is already
        // enough - the other blank fields (and member_firm/
        // contact_person's own already-filled values) are not a
        // reason to block Save.
        setField(dom.editEmailInput, "old@example.test");
        assert.equal(
            dom.saveButton.disabled,
            false,
            "dirty, and member_firm/contact_person are still filled"
        );

        // Clearing every field, including the two that started
        // non-blank, makes the contact wholly blank - Save must go
        // back to disabled.
        setField(dom.editEmailInput, "");
        setField(dom.editMemberFirmInput, "");
        setField(dom.editContactPersonInput, "");
        assert.equal(
            dom.saveButton.disabled,
            true,
            "wholly blank contact must keep Save disabled"
        );

        setField(dom.editPhoneInput, "1");
        assert.equal(
            dom.saveButton.disabled,
            false,
            "phone alone is enough once filled"
        );
    });

    test("Cancel while editing restores the fields to the loaded baseline, not to empty, and stays in edit-one mode", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        clickModeView();
        await selectCountry("doc_fr");
        clickEditOnCard(0);

        setField(dom.editMemberFirmInput, "Something else entirely");

        clickCancel();

        assert.equal(
            dom.editMemberFirmInput.value,
            "Acme Legal SARL",
            "Cancel must restore the loaded baseline, never blank it"
        );
        assert.equal(dom.editFieldsEl.hidden, false, "must stay in edit-one mode");
        assert.equal(dom.container.hidden, false, "Cancel must not collapse the panel");
    });

    // --- Delete: confirm gating -----------------------------------------

    test("clicking Delete shows a confirm dialog and makes zero fetch calls until confirmed", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        await selectCountry("doc_fr");

        fetchCalls.length = 0;

        let fetchCountAtConfirmTime = null;

        global.window.confirm = (message) => {
            confirmCalls.push(message);
            fetchCountAtConfirmTime = fetchCalls.length;
            return true;
        };

        queueFetchResponses([
            { ok: true, status: 200, payload: { success: true, data: {} } },
            ZERO_CONTACTS_RESPONSE,
        ]);

        clickDeleteOnCard(0);

        assert.equal(confirmCalls.length, 1);
        assert.equal(fetchCountAtConfirmTime, 0, "no fetch may happen before the confirm dialog resolves");

        // The confirmed deletion itself fires and completes in the
        // background (fire-and-forget, like every other decision in
        // this panel) - let it settle before the test ends so it
        // never leaks an in-flight request into the next test.
        await new Promise((resolve) => setTimeout(resolve, 20));
    });

    test("declining the Delete confirmation leaves state untouched with zero fetch calls", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        await selectCountry("doc_fr");

        fetchCalls.length = 0;
        confirmReturnValue = false;

        clickDeleteOnCard(0);

        assert.equal(confirmCalls.length, 1);
        assert.equal(fetchCalls.length, 0);
        assert.equal(dom.listEl._children.length, 1, "the contact must still be listed");
    });

    test("confirming Delete fires exactly one delete request for the exact contact_id and reloads the list", async () => {
        queueFetchResponses([ONE_CONTACT_RESPONSE]);

        await selectCountry("doc_fr");

        fetchCalls.length = 0;
        confirmReturnValue = true;

        queueFetchResponses([
            { ok: true, status: 200, payload: { success: true, data: {} } },
            ZERO_CONTACTS_RESPONSE,
        ]);

        clickDeleteOnCard(0);

        await new Promise((resolve) => setTimeout(resolve, 20));

        assert.equal(fetchCalls.length, 2);
        assert.equal(fetchCalls[0].options.method, "POST");
        assert.equal(fetchCalls[0].options.body.get("action"), "le_global_chatbot_delete_contact");
        assert.equal(fetchCalls[0].options.body.get("nonce"), "contact-delete-nonce");
        assert.equal(fetchCalls[0].options.body.get("document_id"), "doc_fr");
        assert.equal(fetchCalls[0].options.body.get("contact_id"), "contact-1");

        assert.match(fetchCalls[1].url, /action=le_global_chatbot_list_contacts/);
        assert.equal(dom.listEl._children.length, 0);
        assert.match(dom.messageEl.textContent, /deleted successfully/);
    });

    // --- "← Back to contacts" (mission "ORDER 8G-B2.1") -----------------

    describe("back to contacts navigation", () => {
        test("the control exists in Add mode", () => {
            clickModeAdd();

            assert.equal(dom.addBackToListButton.hidden, false);
        });

        test("the control exists in Edit mode", async () => {
            queueFetchResponses([ONE_CONTACT_RESPONSE]);
            await selectCountry("doc_fr");
            clickEditOnCard(0);

            assert.equal(dom.editFieldsEl.hidden, false);
            assert.equal(dom.backToListButton.hidden, false);
        });

        test("clean Back from Add mode returns to the list, country preserved, panel not collapsed", async () => {
            queueFetchResponses([ONE_CONTACT_RESPONSE]);
            await selectCountry("doc_fr");
            clickModeAdd();

            dom.addBackToListButton._listeners.click();

            assert.equal(dom.addOnlyFields.hidden, true, "must leave Add mode");
            assert.equal(dom.viewOnlyFields.hidden, false, "must return to View mode");
            assert.equal(dom.editFieldsEl.hidden, true, "must show the list, not an edit form");
            assert.equal(dom.countrySelect.value, "doc_fr", "country selection must be preserved");
            assert.equal(dom.container.hidden, false, "must not collapse the panel");
        });

        test("clean Back from Edit mode returns to the list, country preserved, panel not collapsed", async () => {
            clickModeView();
            queueFetchResponses([ONE_CONTACT_RESPONSE]);
            await selectCountry("doc_fr");
            clickEditOnCard(0);

            dom.backToListButton._listeners.click();

            assert.equal(dom.editFieldsEl.hidden, true, "must leave the edit form");
            assert.equal(dom.listEl.hidden, false, "must show the list");
            assert.equal(dom.countrySelect.value, "doc_fr", "country selection must be preserved");
            assert.equal(dom.container.hidden, false, "must not collapse the panel");
        });

        test("dirty Back from Add mode prompts for confirmation; declining keeps the content and stays in Add mode", async () => {
            clickModeAdd();
            setField(dom.addMemberFirmInput, "Draft firm name");
            confirmReturnValue = false;

            dom.addBackToListButton._listeners.click();

            assert.equal(confirmCalls.length, 1);
            assert.equal(dom.addOnlyFields.hidden, false, "must remain in Add mode");
            assert.equal(
                dom.addMemberFirmInput.value,
                "Draft firm name",
                "declining discard must never clear the field"
            );
        });

        test("dirty Back from Add mode, confirming, discards the draft and returns to the list", async () => {
            queueFetchResponses([ONE_CONTACT_RESPONSE]);
            await selectCountry("doc_fr");
            clickModeAdd();
            setField(dom.addMemberFirmInput, "Draft firm name");
            confirmReturnValue = true;

            dom.addBackToListButton._listeners.click();

            assert.equal(confirmCalls.length, 1);
            assert.equal(dom.viewOnlyFields.hidden, false);
            assert.equal(dom.addMemberFirmInput.value, "");
        });

        test("dirty Back from Edit mode prompts for confirmation; declining keeps the edited value and stays in the form", async () => {
            queueFetchResponses([ONE_CONTACT_RESPONSE]);
            await selectCountry("doc_fr");
            clickEditOnCard(0);
            setField(dom.editMemberFirmInput, "Changed firm name");
            confirmReturnValue = false;

            dom.backToListButton._listeners.click();

            assert.equal(confirmCalls.length, 1);
            assert.equal(dom.editFieldsEl.hidden, false, "must remain in the edit form");
            assert.equal(dom.editMemberFirmInput.value, "Changed firm name");
        });

        test("Back is distinct from Cancel and from Collapse", async () => {
            queueFetchResponses([ONE_CONTACT_RESPONSE]);
            await selectCountry("doc_fr");
            clickModeAdd();
            setField(dom.addMemberFirmInput, "Draft firm name");

            // Cancel: resets fields, stays in Add mode (unchanged
            // semantics - not touched by this mission).
            dom.cancelButton._listeners.click();
            assert.equal(dom.addOnlyFields.hidden, false);
            assert.equal(dom.addMemberFirmInput.value, "");

            // Collapse: hides the panel, preserves field state
            // (unchanged semantics - not touched by this mission).
            setField(dom.addMemberFirmInput, "Another draft");
            clickCollapse();
            assert.equal(dom.container.hidden, true);
            assert.equal(dom.addMemberFirmInput.value, "Another draft");
        });
    });
});
