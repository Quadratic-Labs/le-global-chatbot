(() => {
    "use strict";

    const MAX_CONCURRENT_UPLOADS = 2;

    const FILE_INPUT_ID = "le-global-document";
    const QUEUE_CONTAINER_ID = "le-global-chatbot-queue";
    const DOCUMENTS_CONTAINER_ID = "le-global-chatbot-documents";
    const SUMMARY_CONTAINER_ID = "le-global-chatbot-summary";

    const UNSAVED_CHANGES_PROMPT = (
        "You have unsaved changes. Discard them and continue?"
    );

    // Populated from the real upload form's data-* attributes the
    // first time a refresh runs - the single source of truth for the
    // action name strings stays server-side (PHP constants), JS only
    // ever reads them, never hardcodes them (mission "ORDER 4",
    // section 6: no client-invented endpoint name).
    let adminFormConfig = null;

    // The most recently known document catalog - kept purely so the
    // upload queue can resolve a country's *existing* filename from
    // the replacement-required error's existing_document_ids (mission
    // "ORDER 8B", section 11), and so the Overview/Documents panels
    // can compute business-facing status/conflict info without a
    // second round trip.
    let lastKnownDocuments = [];

    // --- tiny DOM/string helpers -----------------------------------

    function escapeHtml(value) {
        return String(value == null ? "" : value).replace(
            /[&<>"']/g,
            (character) => (
                {
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#39;",
                }[character]
            )
        );
    }

    // Never read a file's *contents* from anything but input.files,
    // and never let an HTMLElement itself be used as a URL/filename -
    // this is the one place a File is ever pulled off the DOM
    // (mission "ORDER 4", sections 6/9/33).
    function getSelectedFiles(fileInput) {
        if (!fileInput || !fileInput.files) {
            return [];
        }

        return Array.from(fileInput.files);
    }

    function findDocumentById(documentId) {
        return (
            lastKnownDocuments.find(
                (document) => document.document_id === documentId
            ) || null
        );
    }

    // --- pure response-classification helpers (unchanged contract) -

    function errorMessage(payload, fallback = "The document could not be added.") {
        if (
            payload
            && payload.data
            && typeof payload.data.message === "string"
            && payload.data.message.trim() !== ""
        ) {
            return payload.data.message.trim();
        }

        return fallback;
    }

    // The backend's own structured 409/4xx payload, as the WordPress
    // AJAX proxy relays it: wp_send_json_error wraps whatever the
    // proxy passed as {success:false, data:{message, detail}} -
    // detail is only ever a plain object for the backend's own
    // structured codes, never a string itself (the proxy sends [] //
    // whenever the backend's own `detail` was a plain string).
    function extractStructuredDetail(payload) {
        if (
            payload
            && payload.data
            && payload.data.detail
            && typeof payload.data.detail === "object"
            && !Array.isArray(payload.data.detail)
        ) {
            return payload.data.detail;
        }

        return null;
    }

    function isReplacementRequiredResponse(statusCode, payload) {
        const detail = extractStructuredDetail(payload);

        return (
            statusCode === 409
            && detail !== null
            && detail.code === "document_replacement_required"
        );
    }

    // Mission "ORDER 8B", section 38 - a small, explicit map from the
    // backend's own structured error codes to the business-facing
    // sentence an admin should see. Never the technical code itself,
    // never a stack trace. Any code not listed here simply falls back
    // to the backend/proxy's own `message` (already free of internal
    // identifiers for every code that reaches this layer today), then
    // to a caller-supplied fallback.
    //
    // Mission "ORDER 8E-A2", section 32 - every new decision code the
    // upload queue now recognizes gets its own dedicated panel (see
    // queueItemHtml) rather than a generic message, so this table only
    // grows with codes that still resolve to a single sentence.
    const BUSINESS_ERROR_MESSAGES = {
        section_already_exists: (
            "This section already exists. Use \"Edit a section\" to "
            + "update it."
        ),
        section_is_last_remaining: (
            "This section cannot be deleted because it is the only "
            + "remaining section in this document."
        ),
        country_document_conflict: (
            "This country has conflicting document records. Please "
            + "contact support before making changes."
        ),
        rollback_failed: (
            "We couldn't save your changes. Nothing has been "
            + "confirmed as completed. Please try again or contact "
            + "support."
        ),
        document_country_not_allowed: (
            "This country is not currently supported for document "
            + "uploads."
        ),
        document_operation_in_progress: (
            "Another change for this country is already being "
            + "processed. Please try again in a moment."
        ),
        document_country_selection_invalid: (
            "That country is not supported. Please choose another one."
        ),
    };

    function businessMessage(payload, fallback) {
        const detail = extractStructuredDetail(payload);

        if (
            detail
            && typeof detail.code === "string"
            && Object.prototype.hasOwnProperty.call(
                BUSINESS_ERROR_MESSAGES,
                detail.code
            )
        ) {
            return BUSINESS_ERROR_MESSAGES[detail.code];
        }

        return errorMessage(payload, fallback);
    }

    // Classifies one upload response into exactly one outcome kind -
    // the single place that decides which queue-item status a
    // response maps to, kept pure/DOM-free so it is directly
    // testable (mission "ORDER 4", section 15: warning, replacement,
    // combined and already-current are four distinct, never-
    // confused outcomes).
    //
    // Mission "ORDER 8E-A2", sections 5-9/16-21 - extended with the
    // backend's newer decision codes (mission "ORDER 8E-A1"): a
    // detected country always needs an explicit confirmation, an
    // undetected-but-processable document offers a country picker
    // instead of a hard failure, and a country already in conflict is
    // never guessed through by an ordinary upload. Every new kind gets
    // its own dedicated panel (see queueItemHtml) - dispatch is always
    // by this classifier's `kind` field, never by re-parsing `message`.
    function classifyUploadResponse(statusCode, payload) {
        const detail = extractStructuredDetail(payload);
        const fallback = "The document could not be added.";

        if (statusCode === 409 && detail) {
            if (detail.code === "document_already_current") {
                return {
                    kind: "already_current",
                    detail,
                    message: businessMessage(payload, fallback),
                };
            }

            if (detail.code === "document_replacement_required") {
                return {
                    kind: "replacement_required",
                    detail,
                    message: businessMessage(payload, fallback),
                };
            }

            // Mission "ORDER 8G-B2", section 14 - the uploaded bytes
            // are already byte-identical to what is active, but this
            // country's document has Admin changes (a section or
            // contact mutation) since that source was last accepted.
            // The ordinary "already up to date" short-circuit must
            // never silently end the workflow here - this is a
            // distinct, separately confirmable outcome.
            if (
                detail.code === "document_identical_but_admin_modified"
            ) {
                return {
                    kind: "identical_but_admin_modified",
                    detail,
                    message: businessMessage(payload, fallback),
                };
            }

            if (detail.code === "document_warning_confirmation_required") {
                return {
                    kind: detail.replacement_required
                        ? "combined_required"
                        : "warning_required",
                    detail,
                    message: businessMessage(payload, fallback),
                };
            }

            if (detail.code === "document_country_confirmation_required") {
                return {
                    kind: "country_confirmation_required",
                    detail,
                    message: businessMessage(payload, fallback),
                };
            }

            if (detail.code === "document_country_selection_required") {
                return {
                    kind: "country_selection_required",
                    detail,
                    message: businessMessage(payload, fallback),
                };
            }

            if (detail.code === "document_country_conflict_review_required") {
                return {
                    kind: "conflict_review_required",
                    detail,
                    message: businessMessage(payload, fallback),
                };
            }
        }

        if (
            statusCode === 422
            && detail
            && detail.code === "document_country_selection_invalid"
        ) {
            return {
                kind: "country_selection_invalid",
                detail,
                message: businessMessage(payload, fallback),
            };
        }

        if (!payload || payload.success !== true) {
            return {
                kind: "error",
                detail,
                message: businessMessage(payload, fallback),
            };
        }

        return { kind: "success", detail: null, message: businessMessage(payload, fallback) };
    }

    // --- documents: status/conflict/date helpers (pure, DOM-free) ---
    //
    // Mission "ORDER 8B", sections 25-27 - mirrors the equally-pure
    // PHP helpers of the same name (detect_conflicted_country_codes,
    // compute_display_status, format_last_updated) so the documents
    // table renders identically whether it came from the initial
    // server-side page load or a later AJAX refresh.

    function detectConflictedCountryCodes(documents) {
        const counts = {};

        (documents || []).forEach((document) => {
            const code = document && document.country_code;

            if (!code) {
                return;
            }

            counts[code] = (counts[code] || 0) + 1;
        });

        return new Set(
            Object.keys(counts).filter((code) => counts[code] > 1)
        );
    }

    // Mission "ORDER 8E-A1" already computes requires_action per
    // document (country-conflict-count based, never derived from
    // document_type - a lone "comparator" document is never itself a
    // problem); hasCountryConflict is passed through unchanged for
    // callers, but its own source of truth is now that backend field
    // first (see requiresActionFor below), the client-side count-based
    // detectConflictedCountryCodes only as a fallback for a catalog
    // response that somehow omits it.
    function requiresActionFor(document, conflictedCountryCodes) {
        if (document && typeof document.requires_action === "boolean") {
            return document.requires_action;
        }

        const code = (document && document.country_code) || "";

        return code !== "" && conflictedCountryCodes.has(code);
    }

    function computeDisplayStatus(document, hasCountryConflict) {
        if (hasCountryConflict) {
            return {
                value: "needs_attention",
                label: "Action required",
                icon: "⚠",
                cls: "is-warning",
                title: "More than one document record is linked to this country.",
            };
        }

        const statusValue = (document && document.status) || "unknown";

        if (statusValue === "indexed") {
            return {
                value: "ready",
                label: "Ready",
                icon: "✓",
                cls: "is-success",
                title: "This document is available to the chatbot.",
            };
        }

        return {
            value: "needs_attention",
            label: "Needs attention",
            icon: "⚠",
            cls: "is-warning",
            title: (
                statusValue === "indexed_source_conflict"
                    ? "Multiple source documents resolve for this country."
                    : "The source document is missing."
            ),
        };
    }

    const MONTH_NAMES = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];

    function formatLastUpdated(iso) {
        if (!iso) {
            return "—";
        }

        const date = new Date(iso);

        if (Number.isNaN(date.getTime())) {
            return "—";
        }

        const day = date.getDate();
        const month = MONTH_NAMES[date.getMonth()];
        const year = date.getFullYear();
        const hours = String(date.getHours()).padStart(2, "0");
        const minutes = String(date.getMinutes()).padStart(2, "0");

        return `${day} ${month} ${year}, ${hours}:${minutes}`;
    }

    // --- Add-a-section: pure helpers (title matching, position map) -
    //
    // Mission "ORDER 8B", sections 16-17 - the position dropdown and
    // duplicate-title detection are both plain data transforms, kept
    // DOM-free so they are directly unit-testable.

    function normalizeTitle(value) {
        return String(value || "")
            .trim()
            .toLowerCase()
            .replace(/\s+/g, " ");
    }

    function findDuplicateSectionIn(sections, title) {
        const normalized = normalizeTitle(title);

        if (normalized === "") {
            return null;
        }

        return (
            (sections || []).find(
                (section) => normalizeTitle(section.legal_topic) === normalized
            ) || null
        );
    }

    function buildPositionOptions(sections) {
        const options = [{ value: "beginning", label: "At the beginning" }];

        (sections || []).forEach((section) => {
            options.push({
                value: `after:${section.section_id}`,
                label: `After "${section.legal_topic}"`,
            });
        });

        options.push({ value: "end", label: "At the end" });

        return options;
    }

    // --- upload queue: pure batch-summary helper --------------------
    //
    // Mission "ORDER 8B", section 9 - a zero-count category is simply
    // never listed, and the running total only reads "processed" once
    // every item has left the queued/uploading state (kept pure so
    // the batch-reset fix and the summary wording are both directly
    // testable without a DOM).

    function summarizeQueue(queue) {
        const total = queue.length;

        const settledCount = queue.filter(
            (item) => item.status !== "queued" && item.status !== "uploading"
        ).length;

        const allSettled = total > 0 && settledCount === total;

        const counts = queue.reduce((accumulator, item) => {
            accumulator[item.status] = (accumulator[item.status] || 0) + 1;
            return accumulator;
        }, {});

        const needsConfirmation = (
            (counts.awaiting_replacement_confirmation || 0)
            + (counts.awaiting_warning_confirmation || 0)
            + (counts.awaiting_combined_confirmation || 0)
        );

        const categories = [
            { key: "added", count: counts.indexed || 0, icon: "✓" },
            { key: "replaced", count: counts.replaced || 0, icon: "✓" },
            {
                key: "already up to date",
                count: counts.already_current || 0,
                icon: "✓",
            },
            { key: "needs confirmation", count: needsConfirmation, icon: "⚠" },
            { key: "cancelled", count: counts.cancelled || 0, icon: "—" },
            { key: "failed", count: counts.failed || 0, icon: "✕" },
        ].filter((category) => category.count > 0);

        return { total, allSettled, categories };
    }

    // --- delete forms: progressive enhancement ----------------------
    //
    // The native <form method="post" action="admin-post.php" data-
    // confirm-delete> submit (no JS) still works exactly as before -
    // a full page POST + redirect-with-notice. When JS runs, the
    // very same form is instead sent via fetch with le_global_ajax=1
    // added, so the row can update in place without a full reload
    // (mission "ORDER 4", section 2: never a destructive rewrite of
    // the no-JS path).

    function wireDeleteForms() {
        const deleteForms = document.querySelectorAll(
            "[data-confirm-delete]"
        );

        deleteForms.forEach((form) => {
            form.addEventListener("submit", (event) => {
                const countryName = form.dataset.countryName || "";
                const documentName = (
                    form.dataset.documentName || "this document"
                );

                const confirmed = window.confirm(
                    `Delete ${countryName ? countryName + " document" : "this document"}? `
                    + `'${documentName}' will be removed from the `
                    + "chatbot."
                );

                if (!confirmed) {
                    event.preventDefault();
                    return undefined;
                }

                event.preventDefault();
                closeOpenMenu();
                return runFormAsAjax(form, {
                    disable: form.querySelector('button[type="submit"]'),
                    busyText: "Deleting…",
                    successMessage: `✓ ${documentName} was deleted successfully.`,
                    errorFallback: "The document could not be deleted.",
                });
            });
        });
    }

    // --- reindex forms: progressive enhancement ---------------------
    //
    // Mission "ORDER 8B", section 29 - "Reindex" never appears to the
    // admin: the very same backend reindex endpoint is now presented
    // as "Refresh chatbot data", with wording that makes clear the
    // document itself is not changed.

    function wireReindexForms() {
        const reindexForms = document.querySelectorAll(
            "[data-reindex-form]"
        );

        reindexForms.forEach((form) => {
            form.addEventListener("submit", (event) => {
                event.preventDefault();

                const confirmed = window.confirm(
                    "Refresh chatbot data from the current Word "
                    + "document? This does not change the document."
                );

                if (!confirmed) {
                    return undefined;
                }

                closeOpenMenu();
                return runFormAsAjax(form, {
                    disable: form.querySelector('button[type="submit"]'),
                    busyText: "Refreshing…",
                    successMessage: "✓ Chatbot data refreshed successfully.",
                    errorFallback: "The chatbot data could not be refreshed.",
                });
            });
        });
    }

    // Shared enhancement for the native per-row forms (reindex,
    // delete): never a double click = two mutations (mission
    // "ORDER 4", section 54) - the button is disabled for the whole
    // request and only re-enabled by a full refresh (success) or
    // explicitly on failure, so a user cannot fire it twice while a
    // reindex/delete is genuinely still in flight. Success/error text
    // renders into the documents panel's own aria-live message area,
    // never a window.alert (mission "ORDER 8B", section 36).
    async function runFormAsAjax(form, { disable, busyText, successMessage, errorFallback }) {
        if (disable && disable.disabled) {
            return;
        }

        if (disable) {
            disable.disabled = true;
            disable.dataset.originalText = disable.textContent;
            disable.textContent = busyText;
        }

        setDocumentsMessage("", null);

        const formData = new FormData(form);
        formData.set("le_global_ajax", "1");

        try {
            // form.action, NOT form.getAttribute("action") here, would
            // resolve to the <input name="action"> WordPress itself
            // requires inside this very form (admin-post.php's own
            // dispatch convention) - a named form control shadows the
            // .action IDL property in every real browser, silently
            // turning the URL into the input ELEMENT, then into the
            // literal string "[object HTMLInputElement]" once fetch()
            // stringifies it. This is the real, root cause of the
            // mission's long-standing historical upload bug (mission
            // "ORDER 4", section 33) - confirmed by a minimal, real-
            // Chromium reproduction, not assumed.
            const response = await fetch(form.getAttribute("action"), {
                method: form.method || "POST",
                body: formData,
                credentials: "same-origin",
            });

            let payload = null;

            try {
                payload = await response.json();
            } catch {
                payload = null;
            }

            if (!response.ok || !payload || payload.success !== true) {
                throw new Error(
                    businessMessage(
                        payload,
                        errorFallback || "The request could not be completed."
                    )
                );
            }

            await refreshAdminState();
            setDocumentsMessage(successMessage || "✓ Done.", "is-success");

            if (disable) {
                disable.disabled = false;
                disable.textContent = disable.dataset.originalText || busyText;
            }
        } catch (error) {
            if (disable) {
                disable.disabled = false;
                disable.textContent = disable.dataset.originalText || busyText;
            }

            setDocumentsMessage(
                (error && typeof error.message === "string" && error.message)
                    || "The request could not be completed.",
                "is-error"
            );
        }
    }

    function setDocumentsMessage(text, kind) {
        const element = document.getElementById(
            "le-global-documents-message"
        );

        if (!element) {
            return;
        }

        element.textContent = text || "";
        element.className = (
            "le-global-chatbot-admin__edit-message"
            + (kind ? ` ${kind}` : "")
        );
    }

    // --- documents actions menu ("⋯") --------------------------------

    let openMenu = null;

    function closeOpenMenu() {
        if (!openMenu) {
            return;
        }

        openMenu.list.hidden = true;
        openMenu.toggle.setAttribute("aria-expanded", "false");
        openMenu = null;
    }

    function wireDocumentMenus() {
        const container = document.getElementById(DOCUMENTS_CONTAINER_ID);

        if (!container) {
            return;
        }

        container
            .querySelectorAll(".le-global-chatbot-admin__menu")
            .forEach((menu) => {
                const toggle = menu.querySelector(
                    ".le-global-chatbot-admin__menu-toggle"
                );
                const list = menu.querySelector(
                    ".le-global-chatbot-admin__menu-list"
                );

                if (!toggle || !list) {
                    return;
                }

                toggle.addEventListener("click", (event) => {
                    event.stopPropagation();

                    const wasOpen = Boolean(
                        openMenu && openMenu.list === list
                    );

                    closeOpenMenu();

                    if (!wasOpen) {
                        list.hidden = false;
                        toggle.setAttribute("aria-expanded", "true");
                        openMenu = { toggle, list };

                        const firstItem = list.querySelector(
                            ".le-global-chatbot-admin__menu-item:not(:disabled)"
                        );

                        if (firstItem) {
                            firstItem.focus();
                        }
                    }
                });

                list.addEventListener("keydown", (event) => {
                    if (event.key === "Escape") {
                        closeOpenMenu();
                        toggle.focus();
                    }
                });
            });
    }

    document.addEventListener("click", () => closeOpenMenu());
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            closeOpenMenu();
        }
    });

    // --- documents search + status filter ---------------------------
    //
    // Mission "ORDER 8B", sections 31-32 - purely client-side, over
    // whatever rows are already rendered (server-side or via the AJAX
    // refresh below); never a new server round-trip.

    function applyDocumentsFilter() {
        const table = document.getElementById("le-global-documents-table");

        if (!table) {
            return;
        }

        const searchInput = document.getElementById(
            "le-global-documents-search"
        );
        const statusFilter = document.getElementById(
            "le-global-documents-status-filter"
        );

        const query = (
            (searchInput && searchInput.value) || ""
        ).trim().toLowerCase();
        const status = (statusFilter && statusFilter.value) || "";

        table.querySelectorAll("tbody tr").forEach((row) => {
            row.hidden = !rowMatchesFilter(row.dataset, query, status);
        });
    }

    function rowMatchesFilter(rowData, query, status) {
        const matchesQuery = (
            query === ""
            || ((rowData && rowData.country) || "").includes(query)
            || ((rowData && rowData.filename) || "").includes(query)
        );
        const matchesStatus = (
            status === "" || (rowData && rowData.status) === status
        );

        return matchesQuery && matchesStatus;
    }

    function wireDocumentsToolbar() {
        const searchInput = document.getElementById(
            "le-global-documents-search"
        );
        const statusFilter = document.getElementById(
            "le-global-documents-status-filter"
        );

        if (searchInput) {
            searchInput.addEventListener("input", applyDocumentsFilter);
        }

        if (statusFilter) {
            statusFilter.addEventListener("change", applyDocumentsFilter);
        }
    }

    // --- country-conflict review/resolution --------------------------
    //
    // Mission "ORDER 8E-A2", sections 20-27 - a country in conflict is
    // never a dead end: "Review" always opens exactly one of two
    // paths - a same-source duplicate that can be fixed with one
    // click (AUTO_DEDUPLICATE), or a choice between keeping one of the
    // existing documents (CHOOSE_DOCUMENT) and uploading a fresh,
    // authoritative one through the ordinary upload decision flow
    // (REPLACE_WITH_DOCUMENT) - never a "contact developer" dead end.
    // No document_id/document_type/chunk count is ever shown; a
    // candidate's document_id survives only as a data attribute the
    // Admin never has to read.

    let conflictReviewState = null;

    // Always reads fresh from the DOM rather than the cached
    // adminFormConfig (which stays null until the first AJAX refresh
    // runs) - Review can be the very first thing an Admin clicks on a
    // freshly loaded page.
    function getAdminFormConfig() {
        const form = document.querySelector(
            ".le-global-chatbot-admin__upload-form"
        );

        if (!form) {
            return null;
        }

        return { ...form.dataset, adminPostUrl: form.getAttribute("action") };
    }

    function wireReviewButtons(container) {
        (container || document)
            .querySelectorAll("[data-review-country-code]")
            .forEach((button) => {
                button.addEventListener("click", () => {
                    openConflictReview(
                        button.dataset.reviewCountryCode,
                        button.dataset.reviewCountryName || "This country"
                    );
                });
            });
    }

    function conflictReviewContainer() {
        return document.getElementById("le-global-conflict-review");
    }

    async function openConflictReview(countryCode, countryName) {
        const container = conflictReviewContainer();

        if (!container || !countryCode) {
            return;
        }

        conflictReviewState = {
            countryCode,
            countryName,
            stage: "loading",
            review: null,
            error: null,
            pendingKeepDocumentId: null,
        };

        renderConflictReviewPanel();

        const config = getAdminFormConfig();

        if (!config || !config.conflictReviewAction) {
            conflictReviewState.stage = "error";
            conflictReviewState.error = (
                "The conflict review could not be loaded. Please "
                + "refresh the page and try again."
            );
            renderConflictReviewPanel();
            return;
        }

        const url = (
            config.adminPostUrl
            + "?action="
            + encodeURIComponent(config.conflictReviewAction)
            + "&nonce="
            + encodeURIComponent(config.conflictReviewNonce)
            + "&country_code="
            + encodeURIComponent(countryCode)
        );

        let payload = null;

        try {
            const response = await fetch(url, {
                method: "GET",
                credentials: "same-origin",
            });

            payload = await response.json();

            if (!response.ok || !payload || payload.success !== true) {
                throw new Error(
                    businessMessage(
                        payload,
                        "The conflict review could not be loaded."
                    )
                );
            }
        } catch (error) {
            if (
                !conflictReviewState
                || conflictReviewState.countryCode !== countryCode
            ) {
                return;
            }

            conflictReviewState.stage = "error";
            conflictReviewState.error = (
                (error && typeof error.message === "string" && error.message)
                || "The conflict review could not be loaded."
            );
            renderConflictReviewPanel();
            return;
        }

        if (
            !conflictReviewState
            || conflictReviewState.countryCode !== countryCode
        ) {
            // The Admin closed this review (or opened a different
            // country's) before the response arrived - discard it.
            return;
        }

        conflictReviewState.review = payload.data;
        conflictReviewState.stage = "list";
        renderConflictReviewPanel();
    }

    function closeConflictReview() {
        conflictReviewState = null;
        renderConflictReviewPanel();
    }

    function conflictReviewCandidateHtml(candidate) {
        return (
            '<li class="le-global-chatbot-admin__conflict-candidate" '
            + `data-document-id="${escapeHtml(candidate.document_id || "")}">`
            + '<div class="le-global-chatbot-admin__conflict-candidate-info">'
            + `<strong>${escapeHtml(candidate.source_filename || "This document")}</strong>`
            + '<span class="le-global-chatbot-admin__conflict-candidate-meta">'
            + (
                candidate.reference_year
                    ? `Year: ${escapeHtml(candidate.reference_year)} · `
                    : ""
            )
            + `Updated: ${escapeHtml(formatLastUpdated(candidate.updated_at))}`
            + "</span>"
            + "</div>"
            + '<button type="button" class="button" '
            + `data-keep-document-id="${escapeHtml(candidate.document_id || "")}">`
            + "Keep this document</button>"
            + "</li>"
        );
    }

    function renderConflictReviewPanel() {
        const container = conflictReviewContainer();

        if (!container) {
            return;
        }

        if (!conflictReviewState) {
            container.hidden = true;
            container.innerHTML = "";
            return;
        }

        container.hidden = false;

        const { countryName, stage, review, error } = conflictReviewState;

        if (stage === "loading") {
            container.innerHTML = (
                '<div class="le-global-chatbot-admin__conflict-review-panel">'
                + `<p>Loading the review for ${escapeHtml(countryName)}…</p>`
                + "</div>"
            );
            return;
        }

        if (stage === "error") {
            container.innerHTML = (
                '<div class="le-global-chatbot-admin__conflict-review-panel">'
                + `<p class="le-global-chatbot-admin__queue-error">${escapeHtml(error || "This could not be loaded.")}</p>`
                + '<button type="button" class="button" data-conflict-action="close">Close</button>'
                + "</div>"
            );
            wireConflictReviewPanel();
            return;
        }

        if (stage === "resolving") {
            container.innerHTML = (
                '<div class="le-global-chatbot-admin__conflict-review-panel">'
                + "<p>Resolving…</p>"
                + "</div>"
            );
            return;
        }

        if (stage === "resolve-error") {
            container.innerHTML = (
                '<div class="le-global-chatbot-admin__conflict-review-panel">'
                + "<p>We couldn't resolve this issue.</p>"
                + "<p>Nothing was changed.</p>"
                + '<button type="button" class="button" data-conflict-action="back-to-list">Back</button>'
                + "</div>"
            );
            wireConflictReviewPanel();
            return;
        }

        if (stage === "confirm-choose") {
            const candidate = (
                (review && review.candidates) || []
            ).find((item) => (
                item.document_id
                === conflictReviewState.pendingKeepDocumentId
            ));

            container.innerHTML = (
                '<div class="le-global-chatbot-admin__conflict-review-panel">'
                + `<h3>Keep this document for ${escapeHtml(countryName)}?</h3>`
                + (
                    candidate
                        ? `<p>${escapeHtml(candidate.source_filename || "")}</p>`
                        : ""
                )
                + "<p>The other conflicting document will be removed.</p>"
                + '<div class="le-global-chatbot-admin__conflict-review-actions">'
                + '<button type="button" class="button" data-conflict-action="back-to-list">Cancel</button>'
                + '<button type="button" class="button button-primary" data-conflict-action="confirm-choose">Keep document</button>'
                + "</div></div>"
            );
            wireConflictReviewPanel();
            return;
        }

        if (stage === "resolved") {
            container.innerHTML = (
                '<div class="le-global-chatbot-admin__conflict-review-panel">'
                + '<p class="le-global-chatbot-admin__queue-message">✓ Document issue resolved.</p>'
                + '<button type="button" class="button" data-conflict-action="close">Close</button>'
                + "</div>"
            );
            wireConflictReviewPanel();
            return;
        }

        if (stage === "replace") {
            container.innerHTML = (
                '<div class="le-global-chatbot-admin__conflict-review-panel">'
                + "<p>We couldn't determine which document should be "
                + `used for ${escapeHtml(countryName)}.</p>`
                + "<p>Upload the current document you want to use.</p>"
                + '<input type="file" id="le-global-conflict-replace-file" '
                + 'class="le-global-chatbot-admin__visually-hidden-input" '
                + 'accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document">'
                + '<label class="button button-primary" '
                + 'for="le-global-conflict-replace-file">Choose document</label>'
                + '<div class="le-global-chatbot-admin__conflict-review-actions">'
                + '<button type="button" class="button" data-conflict-action="back-to-list">Back</button>'
                + "</div></div>"
            );
            wireConflictReviewPanel();
            return;
        }

        // stage === "list"
        const candidates = (review && review.candidates) || [];
        const autoDeduplicateAvailable = Boolean(
            review && review.auto_deduplicate_available
        );

        let body;

        if (autoDeduplicateAvailable) {
            body = (
                "<h3>Duplicate document detected</h3>"
                + `<p>We found duplicate records for the same ${escapeHtml(countryName)} document.</p>`
                + "<p>Your document content is safe. We can remove the "
                + "duplicate record without changing the document "
                + "itself.</p>"
                + '<div class="le-global-chatbot-admin__conflict-review-actions">'
                + '<button type="button" class="button" data-conflict-action="close">Cancel</button>'
                + '<button type="button" class="button button-primary" data-conflict-action="auto-deduplicate">Fix duplicate</button>'
                + "</div>"
            );
        } else {
            body = (
                "<h3>More than one document is available for this "
                + "country</h3>"
                + "<p>Choose the document that should remain active, "
                + "or upload the correct one.</p>"
                + '<ul class="le-global-chatbot-admin__conflict-candidates">'
                + candidates.map(conflictReviewCandidateHtml).join("")
                + "</ul>"
                + "<p>Not sure which one is right?</p>"
                + '<div class="le-global-chatbot-admin__conflict-review-actions">'
                + '<button type="button" class="button" data-conflict-action="close">Cancel</button>'
                + '<button type="button" class="button" data-conflict-action="show-replace">Upload the correct document</button>'
                + "</div>"
            );
        }

        container.innerHTML = (
            '<div class="le-global-chatbot-admin__conflict-review-panel">'
            + body
            + "</div>"
        );

        wireConflictReviewPanel();
    }

    function wireConflictReviewPanel() {
        const container = conflictReviewContainer();

        if (!container) {
            return;
        }

        container
            .querySelectorAll("[data-conflict-action]")
            .forEach((button) => {
                button.addEventListener("click", () => {
                    handleConflictAction(button.dataset.conflictAction);
                });
            });

        container
            .querySelectorAll("[data-keep-document-id]")
            .forEach((button) => {
                button.addEventListener("click", () => {
                    if (!conflictReviewState) {
                        return;
                    }

                    conflictReviewState.pendingKeepDocumentId = (
                        button.dataset.keepDocumentId
                    );
                    conflictReviewState.stage = "confirm-choose";
                    renderConflictReviewPanel();
                });
            });

        const replaceFileInput = document.getElementById(
            "le-global-conflict-replace-file"
        );

        if (replaceFileInput) {
            replaceFileInput.addEventListener("change", () => {
                const files = getSelectedFiles(replaceFileInput);
                replaceFileInput.value = "";

                if (files.length === 0 || !conflictReviewState) {
                    return;
                }

                const { countryCode } = conflictReviewState;
                closeConflictReview();
                enqueueConflictReplacement(countryCode, files[0]);
            });
        }
    }

    function handleConflictAction(action) {
        if (!conflictReviewState) {
            return;
        }

        if (action === "close") {
            closeConflictReview();
            return;
        }

        if (action === "back-to-list") {
            conflictReviewState.stage = "list";
            renderConflictReviewPanel();
            return;
        }

        if (action === "show-replace") {
            conflictReviewState.stage = "replace";
            renderConflictReviewPanel();
            return;
        }

        if (action === "auto-deduplicate") {
            submitConflictResolution("AUTO_DEDUPLICATE", null);
            return;
        }

        if (action === "confirm-choose") {
            submitConflictResolution(
                "CHOOSE_DOCUMENT",
                conflictReviewState.pendingKeepDocumentId
            );
        }
    }

    async function submitConflictResolution(resolutionMode, keepDocumentId) {
        if (!conflictReviewState) {
            return;
        }

        const { countryCode } = conflictReviewState;
        conflictReviewState.stage = "resolving";
        renderConflictReviewPanel();

        const config = getAdminFormConfig();

        if (!config || !config.resolveConflictAction) {
            conflictReviewState.stage = "resolve-error";
            renderConflictReviewPanel();
            return;
        }

        const body = new URLSearchParams();
        body.set("action", config.resolveConflictAction);
        body.set("nonce", config.resolveConflictNonce);
        body.set("country_code", countryCode);
        body.set("resolution_mode", resolutionMode);

        if (keepDocumentId) {
            body.set("keep_document_id", keepDocumentId);
        }

        let payload = null;

        try {
            const response = await fetch(config.adminPostUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": (
                        "application/x-www-form-urlencoded; charset=UTF-8"
                    ),
                },
                body: body.toString(),
            });

            payload = await response.json();

            if (!response.ok || !payload || payload.success !== true) {
                throw new Error("resolution failed");
            }
        } catch {
            if (
                conflictReviewState
                && conflictReviewState.countryCode === countryCode
            ) {
                conflictReviewState.stage = "resolve-error";
                renderConflictReviewPanel();
            }
            return;
        }

        if (
            !conflictReviewState
            || conflictReviewState.countryCode !== countryCode
        ) {
            return;
        }

        conflictReviewState.stage = "resolved";
        renderConflictReviewPanel();
        refreshAdminState();
    }

    // Mission "ORDER 8E-A2", section 27 - REPLACE_WITH_DOCUMENT reuses
    // the exact same upload queue/decision UI as an ordinary upload
    // (Checking document… -> country confirmation -> content warning
    // if applicable -> Ready), the only difference being the item
    // carries which country's conflict it is resolving so sendUpload
    // targets the dedicated proxy action instead of the plain one.
    function enqueueConflictReplacement(countryCode, file) {
        // resolveConflictCountryCode must be present on the item BEFORE
        // enqueueFiles's own synchronous pumpQueue() call can start its
        // first upload attempt - enqueueFiles already runs the queue
        // pump synchronously (mission "ORDER 8B" batching behaviour),
        // so setting this field only after enqueueFiles returns is too
        // late and lets that very first attempt go out through the
        // plain upload endpoint instead of the conflict-resolution one.
        enqueueFiles([file], { resolveConflictCountryCode: countryCode });
    }

    // --- refreshAdminState: partial refresh, no full page reload ---
    //
    // Mission "ORDER 4", section 26: a single function re-fetches the
    // real catalog+stats and re-renders just the summary cards and
    // documents table - never a full page reload, and debounced so a
    // batch of near-simultaneous completions collapses into one GET.

    let refreshPending = null;

    function refreshAdminState() {
        if (refreshPending) {
            return refreshPending;
        }

        refreshPending = performRefresh().finally(() => {
            refreshPending = null;
        });

        return refreshPending;
    }

    async function performRefresh() {
        const uploadForm = document.querySelector(
            ".le-global-chatbot-admin__upload-form"
        );

        if (!uploadForm) {
            return;
        }

        // uploadForm.action would resolve to the form's own
        // <input name="action">, not the URL string - see the note
        // in runFormAsAjax for the full explanation.
        const adminPostUrl = uploadForm.getAttribute("action");

        adminFormConfig = {
            ...uploadForm.dataset,
            adminPostUrl,
        };

        const refreshAction = uploadForm.dataset.refreshAction;
        const refreshNonce = uploadForm.dataset.refreshNonce;

        if (!refreshAction || !refreshNonce) {
            return;
        }

        const url = (
            adminPostUrl
            + "?action="
            + encodeURIComponent(refreshAction)
            + "&nonce="
            + encodeURIComponent(refreshNonce)
        );

        let response;
        let payload = null;

        try {
            response = await fetch(url, {
                method: "GET",
                credentials: "same-origin",
            });

            payload = await response.json();
        } catch {
            return;
        }

        if (!response.ok || !payload || payload.success !== true) {
            return;
        }

        const documents = Array.isArray(payload.data.documents)
            ? payload.data.documents
            : [];

        renderSummary(payload.data.stats, documents);
        renderDocuments(documents);
    }

    // Mission "ORDER 8B", section 35 - Overview shows only the three
    // business-useful numbers; chunk counts/index health never render
    // here (they stay available to developers via logs/tests only).
    function renderSummary(stats, documents) {
        const container = document.getElementById(SUMMARY_CONTAINER_ID);

        if (!container) {
            return;
        }

        const totalDocuments = (
            stats ? stats.total_documents : documents.length
        );
        const totalCountries = stats ? stats.total_countries : 0;

        // Mission "ORDER 8E-A2", section 28 - one country conflict
        // (however many raw document records it involves) counts as
        // exactly one "country requiring action", never one count per
        // extra record - the backend's own deduplicated stat is the
        // source of truth; the client-side count only ever backs it
        // up if a catalog response somehow omits it.
        const conflictedCodes = detectConflictedCountryCodes(documents);
        const countriesRequiringAction = (
            stats && typeof stats.countries_requiring_action === "number"
        )
            ? stats.countries_requiring_action
            : conflictedCodes.size;

        container.innerHTML = (
            summaryCardHtml("Documents", totalDocuments)
            + summaryCardHtml("Countries", totalCountries)
            + summaryCardHtml(
                "Countries requiring action",
                countriesRequiringAction
            )
        );
    }

    function summaryCardHtml(label, value) {
        return (
            '<article class="le-global-chatbot-admin__summary-card">'
            + `<span>${escapeHtml(label)}</span>`
            + `<strong>${escapeHtml(value == null ? 0 : value)}</strong>`
            + "</article>"
        );
    }

    // --- documents table ---------------------------------------------
    //
    // Mission "ORDER 8B", sections 22-34 - no Chunks column, no
    // visible document_id (it survives only as a data attribute),
    // Ready/Needs attention status, a readable Last updated, and
    // Download + a single "⋯" menu instead of three same-weight
    // buttons.

    function renderDocuments(documents) {
        lastKnownDocuments = Array.isArray(documents) ? documents : [];

        const container = document.getElementById(DOCUMENTS_CONTAINER_ID);

        if (!container) {
            return;
        }

        updateDocumentCount(lastKnownDocuments.length);

        if (lastKnownDocuments.length === 0) {
            container.innerHTML = (
                '<div class="le-global-chatbot-admin__empty">'
                + "No document is currently available."
                + "</div>"
            );
            return;
        }

        const conflictedCodes = detectConflictedCountryCodes(
            lastKnownDocuments
        );
        const rows = documentsAndConflictRows(
            lastKnownDocuments,
            conflictedCodes
        )
            .join("");

        container.innerHTML = (
            '<div class="le-global-chatbot-admin__table-container">'
            + '<table class="widefat striped le-global-chatbot-admin__table" id="le-global-documents-table">'
            + "<thead><tr>"
            + "<th scope=\"col\">Country</th>"
            + "<th scope=\"col\">Document</th>"
            + "<th scope=\"col\">Status</th>"
            + "<th scope=\"col\">Last updated</th>"
            + "<th scope=\"col\">Actions</th>"
            + "</tr></thead>"
            + `<tbody>${rows}</tbody>`
            + "</table></div>"
        );

        wireReindexForms();
        wireDeleteForms();
        wireDocumentMenus();
        wireReviewButtons(container);
        applyDocumentsFilter();
    }

    function updateDocumentCount(count) {
        const element = document.getElementById("le-global-document-count");

        if (!element) {
            return;
        }

        element.textContent = `${count} document${count === 1 ? "" : "s"}`;
    }

    // Mission "ORDER 8E-A2", section 21 - a country in conflict is
    // never shown as several same-weight raw rows (the Admin has no
    // use for document_type/document_id/chunk counts to tell them
    // apart); it collapses into exactly one row pointing at Review.
    // Grouping is purely by country_code - a country with only one
    // active document, however it counts internally, always renders
    // as a normal single row.
    function groupDocumentsByCountryCode(documents) {
        const groups = [];
        const indexByCode = new Map();

        (documents || []).forEach((document) => {
            const code = (document && document.country_code) || "";
            const key = code === "" ? ` no-code-${groups.length}` : code;

            if (!indexByCode.has(key)) {
                indexByCode.set(key, groups.length);
                groups.push({ countryCode: code, documents: [] });
            }

            groups[indexByCode.get(key)].documents.push(document);
        });

        return groups;
    }

    function documentsAndConflictRows(documents, conflictedCodes) {
        const groups = groupDocumentsByCountryCode(documents);
        const rows = [];

        groups.forEach((group) => {
            const requiresAction = group.documents.some((document) => (
                requiresActionFor(document, conflictedCodes)
            ));

            if (requiresAction && group.documents.length > 1) {
                rows.push(conflictRowHtml(group.documents));
                return;
            }

            group.documents.forEach((document) => {
                rows.push(
                    documentRowHtml(document, conflictedCodes)
                );
            });
        });

        return rows;
    }

    function documentRowHtml(item, conflictedCodes) {
        const country = item.country || "";
        const countryCode = item.country_code || "";
        const filename = item.source_filename || "";
        const hasConflict = requiresActionFor(item, conflictedCodes);
        const displayStatus = computeDisplayStatus(item, hasConflict);

        return (
            "<tr "
            + `data-country="${escapeHtml(country.toLowerCase())}" `
            + `data-filename="${escapeHtml(filename.toLowerCase())}" `
            + `data-status="${escapeHtml(displayStatus.value)}">`
            + `<td><strong>${escapeHtml(country)}</strong> `
            + (
                countryCode
                    ? `<span class="le-global-chatbot-admin__country-code">${escapeHtml(countryCode)}</span>`
                    : ""
            )
            + "</td>"
            + '<td><span class="le-global-chatbot-admin__filename" '
            + `data-document-id="${escapeHtml(item.document_id || "")}">`
            + `${escapeHtml(filename)}</span></td>`
            + `<td>${statusBadgeHtml(displayStatus)}</td>`
            + `<td>${escapeHtml(formatLastUpdated(item.updated_at))}</td>`
            + `<td>${rowActionsHtml(item)}</td>`
            + "</tr>"
        );
    }

    // One synthetic row for an entire conflicted country - never
    // exposes document_id/document_type/chunk counts to the Admin;
    // "Review" is the only way forward, wired in wireConflictReview().
    function conflictRowHtml(documentsForCountry) {
        const first = documentsForCountry[0] || {};
        const country = first.country || "";
        const countryCode = first.country_code || "";

        return (
            "<tr "
            + `data-country="${escapeHtml(country.toLowerCase())}" `
            + 'data-filename="" '
            + 'data-status="needs_attention">'
            + `<td><strong>${escapeHtml(country)}</strong> `
            + (
                countryCode
                    ? `<span class="le-global-chatbot-admin__country-code">${escapeHtml(countryCode)}</span>`
                    : ""
            )
            + "</td>"
            + '<td colspan="2">'
            + statusBadgeHtml({
                label: "Action required",
                icon: "⚠",
                cls: "is-warning",
                title: "More than one document record is linked to this country.",
            })
            + ' <span class="le-global-chatbot-admin__conflict-note">'
            + "More than one document record is linked to this country."
            + "</span></td>"
            + "<td>—</td>"
            + "<td>"
            + '<button type="button" class="button button-primary" '
            + `data-review-country-code="${escapeHtml(countryCode)}" `
            + `data-review-country-name="${escapeHtml(country)}">`
            + "Review</button>"
            + "</td>"
            + "</tr>"
        );
    }

    function statusBadgeHtml(displayStatus) {
        return (
            `<span class="le-global-chatbot-admin__status ${displayStatus.cls}" `
            + `title="${escapeHtml(displayStatus.title)}">`
            + `<span aria-hidden="true">${displayStatus.icon}</span> `
            + `${escapeHtml(displayStatus.label)}`
            + "</span>"
        );
    }

    function rowActionsHtml(item) {
        if (item.download_url) {
            return (
                '<div class="le-global-chatbot-admin__actions">'
                + `<a class="button" href="${escapeHtml(item.download_url)}">Download</a>`
                + "</div>"
            );
        }

        return (
            '<div class="le-global-chatbot-admin__actions">'
            + '<button type="button" class="button" disabled '
            + 'title="No unambiguous source document is available to download.">'
            + "Download</button>"
            + "</div>"
        );
    }

    // --- Add / Edit a section -----------------------------------------
    //
    // Mission "ORDER 8B", sections 12-21: one segmented control picks
    // between Edit and Add, Edit stays the default visible mode.
    // Country dropdown lists only real documents (rendered server-
    // side from the same catalog the documents table uses, excluding
    // any country in conflict - never a static allowlist). Section/
    // position lists load only once a country is chosen, and only
    // ever list sections that really exist (never a "Create section"
    // option in Edit, never a fabricated position in Add). Every
    // async step carries a generation token, bumped on every country
    // change and section change - a response that arrives after a
    // newer one has already superseded it is discarded outright.

    let editSectionGeneration = 0;

    function wireEditSection() {
        const container = document.getElementById(
            "le-global-chatbot-edit"
        );

        if (!container) {
            return;
        }

        const modeEditButton = document.getElementById("le-global-mode-edit");
        const modeAddButton = document.getElementById("le-global-mode-add");
        const countrySelect = document.getElementById(
            "le-global-edit-country"
        );
        const editOnlyFields = document.getElementById(
            "le-global-edit-only-fields"
        );
        const sectionSelect = document.getElementById(
            "le-global-edit-section"
        );
        const titleInput = document.getElementById(
            "le-global-edit-title"
        );
        const textarea = document.getElementById(
            "le-global-edit-content"
        );
        const editHintEl = document.getElementById("le-global-edit-hint");
        const deleteButton = document.getElementById(
            "le-global-edit-delete"
        );
        const addOnlyFields = document.getElementById(
            "le-global-add-only-fields"
        );
        const addTitleInput = document.getElementById("le-global-add-title");
        const addPositionSelect = document.getElementById(
            "le-global-add-position"
        );
        const duplicateWarningEl = document.getElementById(
            "le-global-add-duplicate-warning"
        );
        const addContentTextarea = document.getElementById(
            "le-global-add-content"
        );
        const messageEl = document.getElementById(
            "le-global-chatbot-edit-message"
        );
        const cancelButton = document.getElementById(
            "le-global-edit-cancel"
        );
        const saveButton = document.getElementById(
            "le-global-edit-save"
        );
        const addSubmitButton = document.getElementById(
            "le-global-add-submit"
        );
        const collapseButton = document.getElementById(
            "le-global-edit-collapse"
        );

        if (
            !modeEditButton || !modeAddButton || !countrySelect
            || !editOnlyFields || !sectionSelect || !titleInput || !textarea
            || !editHintEl || !deleteButton
            || !addOnlyFields || !addTitleInput || !addPositionSelect
            || !duplicateWarningEl || !addContentTextarea || !messageEl
            || !cancelButton || !saveButton || !addSubmitButton
            || !collapseButton
        ) {
            return;
        }

        const config = container.dataset;

        let mode = "edit";
        let saving = false;
        let adding = false;
        let deleting = false;
        let previousCountryValue = "";
        let previousSectionValue = "";
        let currentSections = [];
        let editBaselineContent = null;
        let editBaselineTitle = null;

        // Messages/textarea content only ever go through .textContent
        // or .value - never innerHTML - so nothing rendered here can
        // ever be interpreted as markup, whatever text a legal
        // document or an error message happens to contain (mission
        // "ORDER 5D", sections 3/8).
        function setMessage(text, kind) {
            messageEl.textContent = text || "";
            messageEl.className = (
                "le-global-chatbot-admin__edit-message"
                + (kind ? ` ${kind}` : "")
            );
        }

        function selectedCountryName() {
            const option = countrySelect.options[countrySelect.selectedIndex];

            if (!option) {
                return "the";
            }

            return option.textContent.replace(/\s*\([^)]*\)\s*$/, "").trim();
        }

        function setSectionOptions(placeholderText, sections) {
            sectionSelect.textContent = "";

            const placeholder = document.createElement("option");
            placeholder.value = "";
            placeholder.textContent = placeholderText;
            sectionSelect.appendChild(placeholder);

            (sections || []).forEach((section) => {
                const option = document.createElement("option");
                option.value = section.section_id;
                option.textContent = section.legal_topic;
                sectionSelect.appendChild(option);
            });
        }

        function setPositionPlaceholder(text) {
            addPositionSelect.textContent = "";

            const option = document.createElement("option");
            option.value = "";
            option.textContent = text;
            addPositionSelect.appendChild(option);
        }

        function populatePositionOptions(sections) {
            addPositionSelect.textContent = "";

            buildPositionOptions(sections).forEach(({ value, label }) => {
                const option = document.createElement("option");
                option.value = value;
                option.textContent = label;
                addPositionSelect.appendChild(option);
            });

            addPositionSelect.value = "end";
        }

        function findDuplicateSection(title) {
            return findDuplicateSectionIn(currentSections, title);
        }

        function hideDuplicateWarning() {
            duplicateWarningEl.hidden = true;
            duplicateWarningEl.innerHTML = "";
            updateAddSubmitAvailability();
        }

        function showDuplicateWarning(duplicateSection) {
            duplicateWarningEl.hidden = false;
            duplicateWarningEl.innerHTML = "";

            const message = document.createElement("p");
            message.textContent = (
                `"${duplicateSection.legal_topic}" already exists for `
                + "this country. To change its content, use \"Edit a "
                + "section\"."
            );
            duplicateWarningEl.appendChild(message);

            const switchButton = document.createElement("button");
            switchButton.type = "button";
            switchButton.className = "button";
            switchButton.textContent = `Edit "${duplicateSection.legal_topic}"`;
            switchButton.addEventListener("click", () => {
                if (isDirty() && !window.confirm(UNSAVED_CHANGES_PROMPT)) {
                    return undefined;
                }

                setMode("edit");
                sectionSelect.value = duplicateSection.section_id;
                previousSectionValue = duplicateSection.section_id;
                return onSectionChange();
            });
            duplicateWarningEl.appendChild(switchButton);

            updateAddSubmitAvailability();
        }

        function checkDuplicateTitle() {
            const duplicate = findDuplicateSection(addTitleInput.value);

            if (duplicate) {
                showDuplicateWarning(duplicate);
                return true;
            }

            hideDuplicateWarning();
            return false;
        }

        function isEditDirty() {
            return (
                editBaselineContent !== null
                && (
                    textarea.value !== editBaselineContent
                    || titleInput.value !== editBaselineTitle
                )
            );
        }

        function isAddDirty() {
            return (
                addTitleInput.value.trim() !== ""
                || addContentTextarea.value.trim() !== ""
            );
        }

        function isDirty() {
            return mode === "edit" ? isEditDirty() : isAddDirty();
        }

        function updateSaveAvailability() {
            // Mission "ORDER 8G-A" - one Save now supports content
            // only, title only, or both: ready whenever either
            // differs from its own loaded baseline, never only content
            // as before. Title must never be saved empty even if
            // content alone changed.
            const contentChanged = textarea.value !== editBaselineContent;
            const titleChanged = titleInput.value !== editBaselineTitle;

            const ready = (
                !saving
                && !deleting
                && editBaselineContent !== null
                && textarea.value.trim() !== ""
                && titleInput.value.trim() !== ""
                && (contentChanged || titleChanged)
            );

            saveButton.disabled = !ready;
        }

        function updateAddSubmitAvailability() {
            const ready = (
                !adding
                && countrySelect.value !== ""
                && addTitleInput.value.trim() !== ""
                && addContentTextarea.value.trim() !== ""
                && addPositionSelect.value !== ""
                && duplicateWarningEl.hidden
            );

            addSubmitButton.disabled = !ready;
        }

        function renderModeUI() {
            const isEdit = mode === "edit";

            modeEditButton.classList.toggle("is-active", isEdit);
            modeAddButton.classList.toggle("is-active", !isEdit);
            modeEditButton.setAttribute("aria-selected", String(isEdit));
            modeAddButton.setAttribute("aria-selected", String(!isEdit));

            editOnlyFields.hidden = !isEdit;
            addOnlyFields.hidden = isEdit;
            saveButton.hidden = !isEdit;
            addSubmitButton.hidden = isEdit;
        }

        function setMode(nextMode) {
            if (saving || adding || deleting) {
                return;
            }

            if (mode === nextMode) {
                return;
            }

            if (isDirty() && !window.confirm(UNSAVED_CHANGES_PROMPT)) {
                return;
            }

            mode = nextMode;
            setMessage("", null);
            renderModeUI();
        }

        // Mission "ORDER 8G-A", section 9 - the panel is collapsed by
        // default (server-rendered `hidden` on #le-global-chatbot-edit
        // itself); expand()/collapse() are deliberately independent of
        // setMode()'s own no-op guard (mode === nextMode), since
        // clicking "Edit a section" while already in edit mode must
        // still reveal the panel the very first time.
        //
        // Mission "ORDER 8G-A.2" - collapse() is a pure presentation-
        // state toggle: it never resets form fields/selections, so it
        // is safe to call directly from the dedicated collapse button
        // with no dirty check/confirm and no call to resetToEmpty().
        // Reopening (clicking "Edit a section"/"+ Add a new section"
        // again for the SAME mode) only calls expand() - since
        // collapse() never touched any field value, everything the
        // admin had selected/typed is exactly where they left it.
        function expand() {
            container.hidden = false;
            collapseButton.hidden = false;
        }

        function collapse() {
            container.hidden = true;
            collapseButton.hidden = true;
        }

        function buildQueryUrl(action, nonce, params) {
            let url = (
                config.adminPostUrl
                + "?action=" + encodeURIComponent(action)
                + "&nonce=" + encodeURIComponent(nonce)
            );

            Object.keys(params || {}).forEach((key) => {
                url += (
                    "&" + encodeURIComponent(key)
                    + "=" + encodeURIComponent(params[key])
                );
            });

            return url;
        }

        async function fetchJson(url, options) {
            const response = await fetch(url, {
                credentials: "same-origin",
                ...options,
            });

            let payload = null;

            try {
                payload = await response.json();
            } catch {
                payload = null;
            }

            return { response, payload };
        }

        function isSuccessful(result) {
            return Boolean(
                result
                && result.response.ok
                && result.payload
                && result.payload.success === true
            );
        }

        function resetToEmpty() {
            editSectionGeneration += 1;

            countrySelect.disabled = false;
            countrySelect.value = "";
            previousCountryValue = "";
            currentSections = [];

            setSectionOptions("Select a country first…", []);
            sectionSelect.disabled = true;
            previousSectionValue = "";
            titleInput.value = "";
            titleInput.disabled = true;
            editBaselineTitle = null;
            textarea.value = "";
            textarea.disabled = true;
            editBaselineContent = null;
            editHintEl.textContent = "";
            deleteButton.disabled = true;

            addTitleInput.value = "";
            addTitleInput.disabled = true;
            setPositionPlaceholder("Select a country first…");
            addPositionSelect.disabled = true;
            addContentTextarea.value = "";
            addContentTextarea.disabled = true;
            hideDuplicateWarning();

            setMessage("", null);
            cancelButton.disabled = true;
            saveButton.disabled = true;
            addSubmitButton.disabled = true;
        }

        async function onCountryChange() {
            const documentId = countrySelect.value;

            editSectionGeneration += 1;
            const generation = editSectionGeneration;

            currentSections = [];
            setSectionOptions("Loading sections…", []);
            sectionSelect.disabled = true;
            titleInput.value = "";
            titleInput.disabled = true;
            editBaselineTitle = null;
            textarea.value = "";
            textarea.disabled = true;
            editBaselineContent = null;
            editHintEl.textContent = "";
            deleteButton.disabled = true;

            addTitleInput.value = "";
            addTitleInput.disabled = true;
            setPositionPlaceholder("Loading sections…");
            addPositionSelect.disabled = true;
            addContentTextarea.value = "";
            addContentTextarea.disabled = true;
            hideDuplicateWarning();

            setMessage("", null);
            cancelButton.disabled = documentId === "";
            saveButton.disabled = true;
            addSubmitButton.disabled = true;

            if (documentId === "") {
                setSectionOptions("Select a country first…", []);
                setPositionPlaceholder("Select a country first…");
                return;
            }

            const url = buildQueryUrl(
                config.sectionsListAction,
                config.sectionsListNonce,
                { document_id: documentId }
            );

            let result;

            try {
                result = await fetchJson(url, { method: "GET" });
            } catch {
                result = null;
            }

            if (generation !== editSectionGeneration) {
                return;
            }

            if (!isSuccessful(result)) {
                setSectionOptions("Select a country…", []);
                setPositionPlaceholder("Select a country…");
                setMessage(
                    businessMessage(
                        result ? result.payload : null,
                        "The sections could not be loaded."
                    ),
                    "is-error"
                );
                return;
            }

            const sections = (
                result.payload.data
                && Array.isArray(result.payload.data.sections)
            )
                ? result.payload.data.sections
                : [];

            currentSections = sections;

            setSectionOptions("Select a section…", sections);
            sectionSelect.disabled = sections.length === 0;

            populatePositionOptions(sections);
            addPositionSelect.disabled = false;
            addTitleInput.disabled = false;
            addContentTextarea.disabled = false;

            if (sections.length === 0) {
                setMessage(
                    "This country has no editable section yet.",
                    null
                );
            }

            checkDuplicateTitle();
            updateAddSubmitAvailability();
        }

        async function onSectionChange() {
            const documentId = countrySelect.value;
            const sectionId = sectionSelect.value;

            editSectionGeneration += 1;
            const generation = editSectionGeneration;

            titleInput.value = "";
            titleInput.disabled = true;
            editBaselineTitle = null;
            textarea.value = "";
            textarea.disabled = true;
            editBaselineContent = null;
            editHintEl.textContent = "";
            setMessage("", null);
            saveButton.disabled = true;
            deleteButton.disabled = true;

            if (documentId === "" || sectionId === "") {
                return;
            }

            const url = buildQueryUrl(
                config.sectionGetAction,
                config.sectionGetNonce,
                { document_id: documentId, section_id: sectionId }
            );

            let result;

            try {
                result = await fetchJson(url, { method: "GET" });
            } catch {
                result = null;
            }

            if (generation !== editSectionGeneration) {
                return;
            }

            if (!isSuccessful(result)) {
                setMessage(
                    businessMessage(
                        result ? result.payload : null,
                        "The section could not be loaded."
                    ),
                    "is-error"
                );
                return;
            }

            const content = (
                result.payload.data
                && typeof result.payload.data.content === "string"
            )
                ? result.payload.data.content
                : "";

            const sectionTitle = (
                result.payload.data
                && typeof result.payload.data.legal_topic === "string"
            )
                ? result.payload.data.legal_topic
                : (
                    sectionSelect.options[sectionSelect.selectedIndex]
                        ? sectionSelect.options[
                            sectionSelect.selectedIndex
                        ].textContent
                        : "this section"
                );

            textarea.value = content;
            textarea.disabled = false;
            editBaselineContent = content;
            titleInput.value = sectionTitle;
            titleInput.disabled = false;
            editBaselineTitle = sectionTitle;
            saveButton.disabled = true;
            deleteButton.disabled = false;

            editHintEl.textContent = (
                `Saving will replace the current content of "${sectionTitle}" `
                + `in the ${selectedCountryName()} document.`
            );
        }

        async function onSave() {
            if (saving || adding || deleting || saveButton.disabled) {
                return;
            }

            const documentId = countrySelect.value;
            const sectionId = sectionSelect.value;

            if (documentId === "" || sectionId === "") {
                return;
            }

            const submittedTitle = titleInput.value.trim();
            const countryName = selectedCountryName();

            saving = true;
            saveButton.disabled = true;
            saveButton.textContent = "Saving…";
            cancelButton.disabled = true;
            countrySelect.disabled = true;
            sectionSelect.disabled = true;
            titleInput.disabled = true;
            deleteButton.disabled = true;
            modeEditButton.disabled = true;
            modeAddButton.disabled = true;
            setMessage("Saving…", null);

            const generation = editSectionGeneration;

            const formData = new FormData();
            formData.set("action", config.sectionUpdateAction);
            formData.set("nonce", config.sectionUpdateNonce);
            formData.set("document_id", documentId);
            formData.set("section_id", sectionId);
            formData.set("content", textarea.value);
            formData.set("title", submittedTitle);

            let result;

            try {
                result = await fetchJson(config.adminPostUrl, {
                    method: "POST",
                    body: formData,
                });
            } catch {
                result = null;
            }

            if (generation !== editSectionGeneration) {
                // Cancel (or a fresh country/section pick) already
                // reset the UI while this save was in flight - never
                // touch controls it already reset or disabled.
                saving = false;
                return;
            }

            if (!isSuccessful(result)) {
                saving = false;
                saveButton.textContent = "Save changes";
                cancelButton.disabled = false;
                countrySelect.disabled = false;
                sectionSelect.disabled = false;
                titleInput.disabled = false;
                deleteButton.disabled = false;
                modeEditButton.disabled = false;
                modeAddButton.disabled = false;
                setMessage(
                    businessMessage(
                        result ? result.payload : null,
                        "We couldn't save your changes. Nothing has "
                        + "been confirmed as completed. Please try "
                        + "again or contact support."
                    ),
                    "is-error"
                );
                updateSaveAvailability();
                return;
            }

            // A rename changes the section's own identity - the
            // update response's own document_id/section_id/legal_topic
            // are the one authoritative source for what to refetch and
            // re-select next, never the (possibly now-stale) sectionId
            // this save was originally submitted against.
            const savedSectionId = (
                result.payload.data
                && typeof result.payload.data.section_id === "string"
            )
                ? result.payload.data.section_id
                : sectionId;
            const savedTitle = (
                result.payload.data
                && typeof result.payload.data.legal_topic === "string"
            )
                ? result.payload.data.legal_topic
                : submittedTitle;

            // Refresh the full sections list so a renamed section's
            // new title/section_id is immediately reflected in the
            // dropdown too (mirrors onAddSubmit's own confirmation
            // refetch).
            const listUrl = buildQueryUrl(
                config.sectionsListAction,
                config.sectionsListNonce,
                { document_id: documentId }
            );

            let listRefetch;

            try {
                listRefetch = await fetchJson(listUrl, { method: "GET" });
            } catch {
                listRefetch = null;
            }

            if (
                generation === editSectionGeneration
                && isSuccessful(listRefetch)
                && listRefetch.payload.data
                && Array.isArray(listRefetch.payload.data.sections)
            ) {
                currentSections = listRefetch.payload.data.sections;
                setSectionOptions("Select a section…", currentSections);
                sectionSelect.value = savedSectionId;
                previousSectionValue = savedSectionId;
                populatePositionOptions(currentSections);
            }

            // Re-fetch the section itself so the UI always shows the
            // value really persisted, never just the value that was
            // sent (mission "ORDER 5D", section 2).
            const url = buildQueryUrl(
                config.sectionGetAction,
                config.sectionGetNonce,
                { document_id: documentId, section_id: savedSectionId }
            );

            let refetch;

            try {
                refetch = await fetchJson(url, { method: "GET" });
            } catch {
                refetch = null;
            }

            saving = false;
            saveButton.textContent = "Save changes";

            if (generation !== editSectionGeneration) {
                return;
            }

            cancelButton.disabled = false;
            countrySelect.disabled = false;
            sectionSelect.disabled = false;
            titleInput.disabled = false;
            deleteButton.disabled = false;
            modeEditButton.disabled = false;
            modeAddButton.disabled = false;

            if (
                isSuccessful(refetch)
                && refetch.payload.data
                && typeof refetch.payload.data.content === "string"
            ) {
                textarea.value = refetch.payload.data.content;
                editBaselineContent = refetch.payload.data.content;
                titleInput.value = (
                    typeof refetch.payload.data.legal_topic === "string"
                        ? refetch.payload.data.legal_topic
                        : savedTitle
                );
                editBaselineTitle = titleInput.value;
                setMessage(
                    `✓ "${savedTitle}" was updated successfully. The `
                    + `${countryName} document and chatbot content `
                    + "are now up to date.",
                    "is-success"
                );
            } else {
                editBaselineContent = textarea.value;
                editBaselineTitle = titleInput.value;
                setMessage(
                    "Saved, but the updated content could not be "
                    + "re-loaded for confirmation.",
                    "is-error"
                );
            }

            updateSaveAvailability();
        }

        // Mission "ORDER 8G-A", section 6/7 - a confirmation dialog
        // stands between the button click and the real delete, using
        // the exact same "confirm first, only proceed if accepted"
        // shape wireDeleteForms() already establishes for whole-
        // document delete elsewhere on this page.
        async function onDelete() {
            if (saving || adding || deleting || deleteButton.disabled) {
                return;
            }

            const documentId = countrySelect.value;
            const sectionId = sectionSelect.value;

            if (documentId === "" || sectionId === "") {
                return;
            }

            const sectionTitle = titleInput.value.trim() || "this section";
            const countryName = selectedCountryName();

            const confirmed = window.confirm(
                `Delete "${sectionTitle}"?\n\nThis section will be `
                + `removed from the ${countryName} document and will `
                + "no longer be available to the chatbot."
            );

            if (!confirmed) {
                return;
            }

            deleting = true;
            deleteButton.disabled = true;
            deleteButton.textContent = "Deleting…";
            cancelButton.disabled = true;
            saveButton.disabled = true;
            countrySelect.disabled = true;
            sectionSelect.disabled = true;
            titleInput.disabled = true;
            textarea.disabled = true;
            modeEditButton.disabled = true;
            modeAddButton.disabled = true;
            setMessage("Deleting…", null);

            const generation = editSectionGeneration;

            const formData = new FormData();
            formData.set("action", config.sectionDeleteAction);
            formData.set("nonce", config.sectionDeleteNonce);
            formData.set("document_id", documentId);
            formData.set("section_id", sectionId);

            let result;

            try {
                result = await fetchJson(config.adminPostUrl, {
                    method: "POST",
                    body: formData,
                });
            } catch {
                result = null;
            }

            deleting = false;
            deleteButton.textContent = "Delete section";

            if (generation !== editSectionGeneration) {
                return;
            }

            if (!isSuccessful(result)) {
                cancelButton.disabled = false;
                countrySelect.disabled = false;
                sectionSelect.disabled = false;
                titleInput.disabled = false;
                textarea.disabled = false;
                deleteButton.disabled = false;
                modeEditButton.disabled = false;
                modeAddButton.disabled = false;
                setMessage(
                    businessMessage(
                        result ? result.payload : null,
                        "We couldn't delete this section. Nothing has "
                        + "been confirmed as completed. Please try "
                        + "again or contact support."
                    ),
                    "is-error"
                );
                updateSaveAvailability();
                return;
            }

            // Refresh the sections list so the deleted section
            // disappears immediately (mirrors onAddSubmit's own
            // confirmation refetch).
            const url = buildQueryUrl(
                config.sectionsListAction,
                config.sectionsListNonce,
                { document_id: documentId }
            );

            let refetch;

            try {
                refetch = await fetchJson(url, { method: "GET" });
            } catch {
                refetch = null;
            }

            cancelButton.disabled = false;
            countrySelect.disabled = false;
            modeEditButton.disabled = false;
            modeAddButton.disabled = false;

            if (
                isSuccessful(refetch)
                && refetch.payload.data
                && Array.isArray(refetch.payload.data.sections)
            ) {
                currentSections = refetch.payload.data.sections;
                setSectionOptions("Select a section…", currentSections);
                sectionSelect.disabled = currentSections.length === 0;
                populatePositionOptions(currentSections);
            }

            sectionSelect.value = "";
            previousSectionValue = "";
            textarea.value = "";
            textarea.disabled = true;
            editBaselineContent = null;
            titleInput.value = "";
            titleInput.disabled = true;
            editBaselineTitle = null;
            editHintEl.textContent = "";
            deleteButton.disabled = true;
            saveButton.disabled = true;

            setMessage(
                `✓ "${sectionTitle}" was deleted successfully. The `
                + `${countryName} document and chatbot content are `
                + "now up to date.",
                "is-success"
            );
        }

        async function onAddSubmit() {
            if (saving || adding || deleting || addSubmitButton.disabled) {
                return;
            }

            const documentId = countrySelect.value;
            const title = addTitleInput.value.trim();
            const content = addContentTextarea.value;
            const position = addPositionSelect.value;

            if (
                documentId === ""
                || title === ""
                || content.trim() === ""
                || position === ""
                || findDuplicateSection(title)
            ) {
                checkDuplicateTitle();
                return;
            }

            const countryName = selectedCountryName();

            adding = true;
            addSubmitButton.disabled = true;
            addSubmitButton.textContent = "Adding section…";
            cancelButton.disabled = true;
            countrySelect.disabled = true;
            addTitleInput.disabled = true;
            addPositionSelect.disabled = true;
            addContentTextarea.disabled = true;
            modeEditButton.disabled = true;
            modeAddButton.disabled = true;
            setMessage("Adding section…", null);

            const generation = editSectionGeneration;

            const formData = new FormData();
            formData.set("action", config.sectionAddAction);
            formData.set("nonce", config.sectionAddNonce);
            formData.set("document_id", documentId);
            formData.set("title", title);
            formData.set("content", content);
            formData.set("position", position);

            let result;

            try {
                result = await fetchJson(config.adminPostUrl, {
                    method: "POST",
                    body: formData,
                });
            } catch {
                result = null;
            }

            if (generation !== editSectionGeneration) {
                adding = false;
                return;
            }

            if (!isSuccessful(result)) {
                adding = false;
                addSubmitButton.textContent = "+ Add section";
                cancelButton.disabled = false;
                countrySelect.disabled = false;
                addTitleInput.disabled = false;
                addPositionSelect.disabled = false;
                addContentTextarea.disabled = false;
                modeEditButton.disabled = false;
                modeAddButton.disabled = false;
                setMessage(
                    businessMessage(
                        result ? result.payload : null,
                        "We couldn't add the section. Nothing has "
                        + "been confirmed as completed. Please try "
                        + "again or contact support."
                    ),
                    "is-error"
                );
                updateAddSubmitAvailability();
                return;
            }

            // Re-fetch the sections list so the new section is
            // immediately available in Edit mode (mission "ORDER 8B",
            // section 18) - never trust the bare success response as
            // final proof.
            const url = buildQueryUrl(
                config.sectionsListAction,
                config.sectionsListNonce,
                { document_id: documentId }
            );

            let refetch;

            try {
                refetch = await fetchJson(url, { method: "GET" });
            } catch {
                refetch = null;
            }

            adding = false;
            addSubmitButton.textContent = "+ Add section";

            if (generation !== editSectionGeneration) {
                return;
            }

            cancelButton.disabled = false;
            countrySelect.disabled = false;
            modeEditButton.disabled = false;
            modeAddButton.disabled = false;

            if (
                isSuccessful(refetch)
                && refetch.payload.data
                && Array.isArray(refetch.payload.data.sections)
            ) {
                currentSections = refetch.payload.data.sections;
                setSectionOptions("Select a section…", currentSections);
                sectionSelect.disabled = currentSections.length === 0;
                populatePositionOptions(currentSections);
            }

            addTitleInput.value = "";
            addTitleInput.disabled = false;
            addPositionSelect.disabled = false;
            addContentTextarea.value = "";
            addContentTextarea.disabled = false;
            hideDuplicateWarning();

            setMessage(
                `✓ "${title}" was added successfully. The `
                + `${countryName} document and chatbot content are `
                + "now up to date.",
                "is-success"
            );

            updateAddSubmitAvailability();
        }

        // Mission "ORDER 8G-A.2", section 6 - Cancel resets the current
        // form back to its normal initial state but must NOT collapse
        // the panel; collapsing is exclusively the dedicated ▲
        // control's job now.
        function onCancel() {
            if (isDirty() && !window.confirm(UNSAVED_CHANGES_PROMPT)) {
                return;
            }

            resetToEmpty();
        }

        countrySelect.addEventListener("change", () => {
            if (isDirty() && !window.confirm(UNSAVED_CHANGES_PROMPT)) {
                countrySelect.value = previousCountryValue;
                return undefined;
            }

            previousCountryValue = countrySelect.value;
            return onCountryChange();
        });

        sectionSelect.addEventListener("change", () => {
            if (isDirty() && !window.confirm(UNSAVED_CHANGES_PROMPT)) {
                sectionSelect.value = previousSectionValue;
                return undefined;
            }

            previousSectionValue = sectionSelect.value;
            return onSectionChange();
        });

        titleInput.addEventListener("input", updateSaveAvailability);
        textarea.addEventListener("input", updateSaveAvailability);
        saveButton.addEventListener("click", onSave);
        deleteButton.addEventListener("click", onDelete);

        addTitleInput.addEventListener("input", () => {
            checkDuplicateTitle();
            updateAddSubmitAvailability();
        });
        addContentTextarea.addEventListener("input", updateAddSubmitAvailability);
        addPositionSelect.addEventListener("change", updateAddSubmitAvailability);
        addSubmitButton.addEventListener("click", onAddSubmit);

        cancelButton.addEventListener("click", onCancel);
        modeEditButton.addEventListener("click", () => {
            setMode("edit");
            expand();
        });
        modeAddButton.addEventListener("click", () => {
            setMode("add");
            expand();
        });
        // Mission "ORDER 8G-A.2", section 4 - a pure presentation-state
        // action: no dirty check, no reset, no backend request.
        collapseButton.addEventListener("click", collapse);

        renderModeUI();
    }

    wireEditSection();

    // --- Contacts panel (mission "ORDER 8G-B2") -----------------------
    //
    // Reuses the exact same collapsed-by-default segmented View/Add
    // pattern wireEditSection() above already establishes - dedicated
    // ▲ collapse control (pure presentation, no backend request, state
    // preserved), Cancel resets only (never collapses), mode-switch
    // dirty-change protection via the same UNSAVED_CHANGES_PROMPT.
    //
    // View mode has one further, purely client-side sub-state
    // (list vs a single contact's edit form) that Section editing has
    // no equivalent of - toggled by viewSubState, completely
    // independent of the top-level mode/collapse machinery.
    function wireContactsPanel() {
        const container = document.getElementById(
            "le-global-chatbot-contacts"
        );

        if (!container) {
            return;
        }

        const modeViewButton = document.getElementById(
            "le-global-contact-mode-view"
        );
        const modeAddButton = document.getElementById(
            "le-global-contact-mode-add"
        );
        const countrySelect = document.getElementById(
            "le-global-contact-country"
        );
        const viewOnlyFields = document.getElementById(
            "le-global-contact-view-only-fields"
        );
        const zeroWarningEl = document.getElementById(
            "le-global-contact-zero-warning"
        );
        const listEl = document.getElementById(
            "le-global-contact-list"
        );
        const addAnotherButton = document.getElementById(
            "le-global-contact-add-another"
        );
        const editFieldsEl = document.getElementById(
            "le-global-contact-edit-fields"
        );
        const editIdInput = document.getElementById(
            "le-global-contact-edit-id"
        );
        const editMemberFirmInput = document.getElementById(
            "le-global-contact-edit-member-firm"
        );
        const editContactPersonInput = document.getElementById(
            "le-global-contact-edit-contact-person"
        );
        const editEmailInput = document.getElementById(
            "le-global-contact-edit-email"
        );
        const editPhoneInput = document.getElementById(
            "le-global-contact-edit-phone"
        );
        const editAddressInput = document.getElementById(
            "le-global-contact-edit-address"
        );
        const editWebsiteInput = document.getElementById(
            "le-global-contact-edit-website"
        );
        const backToListButton = document.getElementById(
            "le-global-contact-back-to-list"
        );
        const addBackToListButton = document.getElementById(
            "le-global-contact-add-back-to-list"
        );
        const addOnlyFields = document.getElementById(
            "le-global-contact-add-only-fields"
        );
        const addMemberFirmInput = document.getElementById(
            "le-global-contact-add-member-firm"
        );
        const addContactPersonInput = document.getElementById(
            "le-global-contact-add-contact-person"
        );
        const addEmailInput = document.getElementById(
            "le-global-contact-add-email"
        );
        const addPhoneInput = document.getElementById(
            "le-global-contact-add-phone"
        );
        const addAddressInput = document.getElementById(
            "le-global-contact-add-address"
        );
        const addWebsiteInput = document.getElementById(
            "le-global-contact-add-website"
        );
        const messageEl = document.getElementById(
            "le-global-chatbot-contact-message"
        );
        const cancelButton = document.getElementById(
            "le-global-contact-cancel"
        );
        const saveButton = document.getElementById(
            "le-global-contact-save"
        );
        const addSubmitButton = document.getElementById(
            "le-global-contact-add-submit"
        );
        const collapseButton = document.getElementById(
            "le-global-contact-collapse"
        );

        if (
            !modeViewButton || !modeAddButton || !countrySelect
            || !viewOnlyFields || !zeroWarningEl || !listEl
            || !addAnotherButton || !editFieldsEl || !editIdInput
            || !editMemberFirmInput || !editContactPersonInput
            || !editEmailInput || !editPhoneInput || !editAddressInput
            || !editWebsiteInput || !backToListButton
            || !addBackToListButton
            || !addOnlyFields || !addMemberFirmInput
            || !addContactPersonInput || !addEmailInput
            || !addPhoneInput || !addAddressInput || !addWebsiteInput
            || !messageEl || !cancelButton || !saveButton
            || !addSubmitButton || !collapseButton
        ) {
            return;
        }

        const config = container.dataset;

        let mode = "view";
        let viewSubState = "list";
        let saving = false;
        let adding = false;
        let deleting = false;
        let previousCountryValue = "";
        let currentContacts = [];
        let editBaseline = null;
        let contactGeneration = 0;

        const editFields = [
            ["member_firm", editMemberFirmInput],
            ["contact_person", editContactPersonInput],
            ["email", editEmailInput],
            ["phone", editPhoneInput],
            ["address", editAddressInput],
            ["website", editWebsiteInput],
        ];

        const addFields = [
            ["member_firm", addMemberFirmInput],
            ["contact_person", addContactPersonInput],
            ["email", addEmailInput],
            ["phone", addPhoneInput],
            ["address", addAddressInput],
            ["website", addWebsiteInput],
        ];

        function setMessage(text, kind) {
            messageEl.textContent = text || "";
            messageEl.className = (
                "le-global-chatbot-admin__edit-message"
                + (kind ? ` ${kind}` : "")
            );
        }

        function selectedCountryName() {
            const option = countrySelect.options[countrySelect.selectedIndex];

            if (!option) {
                return "this country";
            }

            return option.textContent.replace(/\s*\([^)]*\)\s*$/, "").trim();
        }

        function buildQueryUrl(action, nonce, params) {
            let url = (
                config.adminPostUrl
                + "?action=" + encodeURIComponent(action)
                + "&nonce=" + encodeURIComponent(nonce)
            );

            Object.keys(params || {}).forEach((key) => {
                url += (
                    "&" + encodeURIComponent(key)
                    + "=" + encodeURIComponent(params[key])
                );
            });

            return url;
        }

        async function fetchJson(url, options) {
            const response = await fetch(url, {
                credentials: "same-origin",
                ...options,
            });

            let payload = null;

            try {
                payload = await response.json();
            } catch {
                payload = null;
            }

            return { response, payload };
        }

        function isSuccessful(result) {
            return Boolean(
                result
                && result.response.ok
                && result.payload
                && result.payload.success === true
            );
        }

        function hasAtLeastOneFieldFilled(fields, inputs) {
            // Every field is individually optional (member_firm,
            // contact_person, email, phone, address, website) - a
            // real member-firm contact can genuinely have some of
            // them empty (e.g. France's own contact has always had
            // address/website empty). The only requirement, matching
            // the backend's own AdminContactWriteRequest validator, is
            // that at least one field carries a value.
            return inputs.some(([, input]) => input.value.trim() !== "");
        }

        function isEditDirty() {
            if (viewSubState !== "edit-one" || editBaseline === null) {
                return false;
            }

            if (editPhotoControl.input.files.length > 0) {
                return true;
            }

            return editFields.some(
                ([key, input]) => input.value !== editBaseline[key]
            );
        }

        function isAddDirty() {
            if (addPhotoControl.input.files.length > 0) {
                return true;
            }

            return addFields.some(
                ([, input]) => input.value.trim() !== ""
            );
        }

        function isDirty() {
            if (mode === "add") {
                return isAddDirty();
            }

            return isEditDirty();
        }

        function updateSaveAvailability() {
            const ready = (
                !saving
                && viewSubState === "edit-one"
                && editBaseline !== null
                && hasAtLeastOneFieldFilled(null, editFields)
                && isEditDirty()
            );

            saveButton.disabled = !ready;
        }

        function updateAddSubmitAvailability() {
            const ready = (
                !adding
                && countrySelect.value !== ""
                && hasAtLeastOneFieldFilled(null, addFields)
            );

            addSubmitButton.disabled = !ready;
        }

        function updateCancelAvailability() {
            cancelButton.disabled = !isDirty();
        }

        [...editFields, ...addFields].forEach(([, input]) => {
            input.addEventListener("input", () => {
                updateSaveAvailability();
                updateAddSubmitAvailability();
                updateCancelAvailability();
            });
        });

        function renderModeUI() {
            const isView = mode === "view";

            modeViewButton.classList.toggle("is-active", isView);
            modeAddButton.classList.toggle("is-active", !isView);
            modeViewButton.setAttribute("aria-selected", String(isView));
            modeAddButton.setAttribute("aria-selected", String(!isView));

            viewOnlyFields.hidden = !isView;
            addOnlyFields.hidden = isView;
            saveButton.hidden = !isView;
            addSubmitButton.hidden = isView;

            updateCancelAvailability();
        }

        function renderViewSubStateUI() {
            const isEditingOne = viewSubState === "edit-one";

            // Guard on a real country selection too, not just an empty
            // contacts array - otherwise this warning (styled with a
            // visible border/background) would show up empty the
            // moment View mode opens, before any country was ever
            // chosen and before currentContacts has been populated by
            // a real fetch.
            zeroWarningEl.hidden = (
                isEditingOne
                || currentContacts.length > 0
                || countrySelect.value === ""
            );
            listEl.hidden = isEditingOne;
            addAnotherButton.hidden = (
                isEditingOne || currentContacts.length === 0
            );
            editFieldsEl.hidden = !isEditingOne;

            updateCancelAvailability();
        }

        function setMode(nextMode) {
            if (saving || adding || deleting) {
                return;
            }

            if (mode === nextMode) {
                return;
            }

            if (isDirty() && !window.confirm(UNSAVED_CHANGES_PROMPT)) {
                return;
            }

            mode = nextMode;
            setMessage("", null);
            renderModeUI();
        }

        // Mirrors wireEditSection()'s own expand()/collapse() exactly
        // (mission "ORDER 8G-A", section 9 / "ORDER 8G-A.2", section
        // 4) - a pure presentation-state toggle, safe to call directly
        // from the dedicated collapse button with no dirty check.
        function expand() {
            container.hidden = false;
            collapseButton.hidden = false;
        }

        function collapse() {
            container.hidden = true;
            collapseButton.hidden = true;
        }

        function clearContactList() {
            listEl.textContent = "";
        }


        const CONTACT_PHOTO_MAX_BYTES = 10 * 1024 * 1024;
        let photoBusy = false;

        function contactPhotoUrl(documentId, contactId) {
            const url = new URL(
                config.adminPostUrl,
                window.location.href
            );

            url.searchParams.set(
                "action",
                "le_global_admin_contact_photo_get"
            );
            url.searchParams.set(
                "nonce",
                config.contactUpdateNonce
            );
            url.searchParams.set("document_id", documentId);
            url.searchParams.set("contact_id", contactId);
            url.searchParams.set("_", String(Date.now()));

            return url.toString();
        }

        function validatePhotoFile(file) {
            if (!file) {
                return null;
            }

            if (
                ![
                    "image/jpeg",
                    "image/png",
                    "image/webp",
                ].includes(file.type)
            ) {
                return "Choose a JPEG, PNG or WebP image.";
            }

            if (file.size > CONTACT_PHOTO_MAX_BYTES) {
                return "The photo must be smaller than 10 MiB.";
            }

            return null;
        }

        function clearPhotoControl(control) {
            if (control.objectUrl) {
                URL.revokeObjectURL(control.objectUrl);
                control.objectUrl = null;
            }

            control.input.value = "";
            control.image.removeAttribute("src");
            control.image.hidden = true;

            if (control.removeButton) {
                control.removeButton.hidden = true;
            }
        }

        function showLocalPhoto(control, file) {
            if (control.objectUrl) {
                URL.revokeObjectURL(control.objectUrl);
            }

            control.objectUrl = URL.createObjectURL(file);
            control.image.src = control.objectUrl;
            control.image.hidden = false;
        }

        function showStoredPhoto(
            control,
            documentId,
            contactId,
            hasPhoto
        ) {
            clearPhotoControl(control);

            if (!hasPhoto || !documentId || !contactId) {
                return;
            }

            control.image.onload = () => {
                control.image.hidden = false;

                if (control.removeButton) {
                    control.removeButton.hidden = false;
                }
            };

            control.image.onerror = () => {
                control.image.hidden = true;

                if (control.removeButton) {
                    control.removeButton.hidden = true;
                }
            };

            control.image.src = contactPhotoUrl(
                documentId,
                contactId
            );
        }

        function mountPhotoControl(anchorInput, kind) {
            const host = (
                anchorInput.closest("td")
                || anchorInput.parentElement
            );

            const root = document.createElement("div");
            root.className = (
                "le-global-chatbot-admin__contact-photo-control"
            );

            const title = document.createElement("strong");
            title.textContent = kind === "edit"
                ? "Contact photo"
                : "Photo (optional)";
            root.appendChild(title);

            const image = document.createElement("img");
            image.className = (
                "le-global-chatbot-admin__contact-photo-preview"
            );
            image.alt = "Contact photo preview";
            image.hidden = true;
            root.appendChild(image);

            const input = document.createElement("input");
            input.type = "file";
            input.accept = "image/jpeg,image/png,image/webp";
            input.className = (
                "le-global-chatbot-admin__contact-photo-input"
            );
            root.appendChild(input);

            let removeButton = null;

            if (kind === "edit") {
                removeButton = document.createElement("button");
                removeButton.type = "button";
                removeButton.className = (
                    "button le-global-chatbot-admin__contact-photo-remove"
                );
                removeButton.textContent = "Remove current photo";
                removeButton.hidden = true;
                root.appendChild(removeButton);
            }

            const note = document.createElement("small");
            note.textContent = (
                "JPEG, PNG or WebP. Maximum 10 MiB."
            );
            root.appendChild(note);

            const control = {
                root,
                image,
                input,
                removeButton,
                objectUrl: null,
            };

            input.addEventListener("change", () => {
                const file = input.files[0] || null;
                const error = validatePhotoFile(file);

                if (error) {
                    window.alert(error);
                    clearPhotoControl(control);
                    updateSaveAvailability();
                    return;
                }

                if (file) {
                    showLocalPhoto(control, file);
                }

                updateSaveAvailability();
            });

            host.appendChild(root);

            return control;
        }

        const editPhotoControl = mountPhotoControl(
            editWebsiteInput,
            "edit"
        );

        const addPhotoControl = mountPhotoControl(
            addWebsiteInput,
            "add"
        );

        async function uploadContactPhoto(
            documentId,
            contactId,
            file
        ) {
            const validationError = validatePhotoFile(file);

            if (validationError) {
                window.alert(validationError);
                return false;
            }

            const formData = new FormData();
            formData.set(
                "action",
                "le_global_admin_contact_photo_replace"
            );
            formData.set(
                "nonce",
                config.contactUpdateNonce
            );
            formData.set("document_id", documentId);
            formData.set("contact_id", contactId);
            formData.set("photo", file);

            try {
                const result = await fetchJson(
                    config.adminPostUrl,
                    {
                        method: "POST",
                        body: formData,
                    }
                );

                return isSuccessful(result);
            } catch {
                return false;
            }
        }

        async function removeCurrentContactPhoto() {
            if (
                photoBusy
                || saving
                || adding
                || deleting
            ) {
                return;
            }

            const documentId = countrySelect.value;
            const contactId = editIdInput.value;

            if (!documentId || !contactId) {
                return;
            }

            if (
                !window.confirm(
                    "Remove this contact's current photo?"
                )
            ) {
                return;
            }

            photoBusy = true;
            editPhotoControl.removeButton.disabled = true;

            const formData = new FormData();
            formData.set(
                "action",
                "le_global_admin_contact_photo_remove"
            );
            formData.set(
                "nonce",
                config.contactUpdateNonce
            );
            formData.set("document_id", documentId);
            formData.set("contact_id", contactId);

            let result;

            try {
                result = await fetchJson(
                    config.adminPostUrl,
                    {
                        method: "POST",
                        body: formData,
                    }
                );
            } catch {
                result = null;
            }

            photoBusy = false;
            editPhotoControl.removeButton.disabled = false;

            if (!isSuccessful(result)) {
                setMessage(
                    "The photo could not be removed.",
                    "is-error"
                );
                return;
            }

            clearPhotoControl(editPhotoControl);

            setMessage(
                "✓ The contact photo was removed successfully.",
                "is-success"
            );
        }

        editPhotoControl.removeButton.addEventListener(
            "click",
            removeCurrentContactPhoto
        );


        // Never innerHTML for anything derived from real contact data
        // (mission "ORDER 5D"'s own discipline, reused here) - every
        // node is built with createElement/textContent.
        function buildContactCard(contact) {
            const card = document.createElement("div");
            card.className = "le-global-chatbot-admin__contact-card";

            if (contact.has_photo) {
                const photo = document.createElement("img");
                photo.className = (
                    "le-global-chatbot-admin__contact-card-photo"
                );
                photo.alt = contact.contact_person
                    ? `${contact.contact_person} photo`
                    : "Contact photo";
                photo.src = contactPhotoUrl(
                    countrySelect.value,
                    contact.contact_id
                );
                photo.onerror = () => {
                    photo.remove();
                };
                card.appendChild(photo);
            }

            const fieldLabels = [
                ["Member firm", contact.member_firm],
                ["Contact person", contact.contact_person],
                ["Email", contact.email],
                ["Phone", contact.phone],
                ["Address", contact.address],
                ["Website", contact.website],
            ];

            fieldLabels.forEach(([label, value]) => {
                const row = document.createElement("p");
                row.className = "le-global-chatbot-admin__contact-field";

                const labelEl = document.createElement("strong");
                labelEl.textContent = `${label}: `;
                row.appendChild(labelEl);

                const valueEl = document.createElement("span");
                valueEl.textContent = value || "—";
                row.appendChild(valueEl);

                card.appendChild(row);
            });

            const actions = document.createElement("div");
            actions.className = (
                "le-global-chatbot-admin__contact-card-actions"
            );

            const editButton = document.createElement("button");
            editButton.type = "button";
            editButton.className = "button";
            editButton.textContent = "Edit contact";
            editButton.addEventListener(
                "click",
                () => beginEditContact(contact)
            );
            actions.appendChild(editButton);

            const deleteButton = document.createElement("button");
            deleteButton.type = "button";
            deleteButton.className = (
                "button le-global-chatbot-admin__delete-button"
                + " is-destructive"
            );
            deleteButton.textContent = "Delete contact";
            deleteButton.addEventListener(
                "click",
                () => confirmDeleteContact(contact)
            );
            actions.appendChild(deleteButton);

            card.appendChild(actions);

            return card;
        }

        function renderContactList() {
            clearContactList();

            currentContacts.forEach((contact) => {
                listEl.appendChild(buildContactCard(contact));
            });

            const countryName = selectedCountryName();

            zeroWarningEl.textContent = "";

            if (currentContacts.length === 0) {
                const warning = document.createElement("p");
                warning.textContent = (
                    `⚠ No L&E Global contact is currently configured `
                    + `for ${countryName}.`
                );
                zeroWarningEl.appendChild(warning);
            }

            renderViewSubStateUI();

            // Mission "ORDER 8G-B2", section 5 - a zero-contact country
            // reveals the Add form directly, no second click on
            // "+ Add a contact" required.
            if (currentContacts.length === 0 && mode !== "add") {
                setMode("add");
                expand();
            }
        }

        function resetEditOnlyFields() {
            editIdInput.value = "";
            editFields.forEach(([, input]) => {
                input.value = "";
                input.disabled = true;
            });
            editBaseline = null;
            clearPhotoControl(editPhotoControl);
        }

        function resetAddOnlyFields() {
            addFields.forEach(([, input]) => {
                input.value = "";
            });
            clearPhotoControl(addPhotoControl);
        }

        function resetToEmpty() {
            contactGeneration += 1;

            countrySelect.disabled = false;
            countrySelect.value = "";
            previousCountryValue = "";
            currentContacts = [];

            clearContactList();
            zeroWarningEl.hidden = true;
            zeroWarningEl.textContent = "";
            addAnotherButton.hidden = true;

            viewSubState = "list";
            resetEditOnlyFields();

            addFields.forEach(([, input]) => {
                input.value = "";
                input.disabled = true;
            });

            setMessage("", null);
            saveButton.disabled = true;
            addSubmitButton.disabled = true;
            updateCancelAvailability();
        }

        function beginEditContact(contact) {
            if (
                viewSubState === "edit-one"
                && isEditDirty()
                && !window.confirm(UNSAVED_CHANGES_PROMPT)
            ) {
                return;
            }

            editIdInput.value = contact.contact_id;

            editBaseline = {
                member_firm: contact.member_firm || "",
                contact_person: contact.contact_person || "",
                email: contact.email || "",
                phone: contact.phone || "",
                address: contact.address || "",
                website: contact.website || "",
                has_photo: Boolean(contact.has_photo),
            };

            editFields.forEach(([key, input]) => {
                input.value = editBaseline[key];
                input.disabled = false;
            });

            showStoredPhoto(
                editPhotoControl,
                countrySelect.value,
                contact.contact_id,
                contact.has_photo
            );

            viewSubState = "edit-one";
            setMessage("", null);
            renderViewSubStateUI();
            updateSaveAvailability();
        }

        function backToList() {
            if (isEditDirty() && !window.confirm(UNSAVED_CHANGES_PROMPT)) {
                return;
            }

            viewSubState = "list";
            resetEditOnlyFields();
            setMessage("", null);
            renderViewSubStateUI();
        }

        // "← Back to contacts" from Add mode (mission "ORDER 8G-B2.1",
        // sections 8-9) - navigation, not Cancel/Collapse/Reset. Mirrors
        // backToList()'s own shape rather than delegating to setMode(),
        // since viewSubState may already be "edit-one" if Add mode was
        // entered by clicking the "+ Add a contact" tab directly while
        // mid-edit (that tab switch itself never resets viewSubState,
        // matching its own established behavior) - explicitly forcing
        // "list" here guarantees this button always lands on the
        // current country's contact list, never a stale edit-one form.
        function backToListFromAdd() {
            if (isAddDirty() && !window.confirm(UNSAVED_CHANGES_PROMPT)) {
                return;
            }

            resetAddOnlyFields();
            viewSubState = "list";
            mode = "view";
            setMessage("", null);
            renderModeUI();
            renderViewSubStateUI();
        }

        async function loadContacts(documentId) {
            contactGeneration += 1;
            const generation = contactGeneration;

            currentContacts = [];
            clearContactList();
            viewSubState = "list";
            resetEditOnlyFields();
            setMessage("Loading contacts…", null);

            const url = buildQueryUrl(
                config.contactsListAction,
                config.contactsListNonce,
                { document_id: documentId }
            );

            let result;

            try {
                result = await fetchJson(url, { method: "GET" });
            } catch {
                result = null;
            }

            if (generation !== contactGeneration) {
                return;
            }

            if (!isSuccessful(result)) {
                setMessage(
                    businessMessage(
                        result ? result.payload : null,
                        "The contacts could not be loaded."
                    ),
                    "is-error"
                );
                return;
            }

            currentContacts = (
                result.payload.data
                && Array.isArray(result.payload.data.contacts)
            )
                ? result.payload.data.contacts
                : [];

            setMessage("", null);
            renderContactList();
        }

        async function onCountryChange() {
            const documentId = countrySelect.value;

            if (documentId === "") {
                resetToEmpty();
                return;
            }

            countrySelect.disabled = false;
            addFields.forEach(([, input]) => {
                input.disabled = false;
            });
            updateAddSubmitAvailability();
            cancelButton.disabled = true;

            await loadContacts(documentId);
        }

        function confirmDeleteContact(contact) {
            const label = contact.contact_person || "this contact";
            const countryName = selectedCountryName();

            const confirmed = window.confirm(
                `Delete "${label}"?\n\nThis contact will be removed `
                + `from ${countryName} and will no longer be `
                + "available to the chatbot."
            );

            if (!confirmed) {
                return;
            }

            deleteContact(contact);
        }

        async function deleteContact(contact) {
            if (saving || adding || deleting) {
                return;
            }

            const documentId = countrySelect.value;

            if (documentId === "") {
                return;
            }

            deleting = true;
            setMessage("Deleting…", null);

            const generation = contactGeneration;

            const formData = new FormData();
            formData.set("action", config.contactDeleteAction);
            formData.set("nonce", config.contactDeleteNonce);
            formData.set("document_id", documentId);
            formData.set("contact_id", contact.contact_id);

            let result;

            try {
                result = await fetchJson(config.adminPostUrl, {
                    method: "POST",
                    body: formData,
                });
            } catch {
                result = null;
            }

            deleting = false;

            if (generation !== contactGeneration) {
                return;
            }

            if (!isSuccessful(result)) {
                setMessage(
                    businessMessage(
                        result ? result.payload : null,
                        "We couldn't delete this contact. Nothing has "
                        + "been confirmed as completed. Please try "
                        + "again or contact support."
                    ),
                    "is-error"
                );
                return;
            }

            if (
                viewSubState === "edit-one"
                && editIdInput.value === contact.contact_id
            ) {
                viewSubState = "list";
                resetEditOnlyFields();
            }

            await loadContacts(documentId);

            setMessage(
                `✓ "${contact.contact_person || "Contact"}" was `
                + "deleted successfully. The chatbot content is now "
                + "up to date.",
                "is-success"
            );
        }

        // A silent, internal counterpart to deleteContact() above -
        // used only to roll back a contact this SAME Add operation
        // just created, when its required photo failed to save
        // (mission "FINAL BLOCKER", section 8). Never guarded by the
        // saving/adding/deleting flags (onAddSubmit is still mid-flow
        // and already holds them), never shows its own message or
        // confirmation prompt - the caller reports one single, honest
        // outcome for the whole Add attempt.
        async function rollbackContact(documentId, contactId) {
            const formData = new FormData();
            formData.set("action", config.contactDeleteAction);
            formData.set("nonce", config.contactDeleteNonce);
            formData.set("document_id", documentId);
            formData.set("contact_id", contactId);

            try {
                const result = await fetchJson(config.adminPostUrl, {
                    method: "POST",
                    body: formData,
                });

                return isSuccessful(result);
            } catch {
                return false;
            }
        }

        async function onSave() {
            if (
                saving || adding || deleting || saveButton.disabled
            ) {
                return;
            }

            const documentId = countrySelect.value;
            const contactId = editIdInput.value;

            if (documentId === "" || contactId === "") {
                return;
            }

            saving = true;
            saveButton.disabled = true;
            saveButton.textContent = "Saving…";
            cancelButton.disabled = true;
            countrySelect.disabled = true;
            setMessage("Saving…", null);

            const generation = contactGeneration;

            const formData = new FormData();
            formData.set("action", config.contactUpdateAction);
            formData.set("nonce", config.contactUpdateNonce);
            formData.set("document_id", documentId);
            formData.set("contact_id", contactId);

            editFields.forEach(([key, input]) => {
                formData.set(key, input.value.trim());
            });

            let result;

            try {
                result = await fetchJson(config.adminPostUrl, {
                    method: "POST",
                    body: formData,
                });
            } catch {
                result = null;
            }

            if (generation !== contactGeneration) {
                // Cancel (or a fresh country pick) already reset the
                // UI while this save was in flight - never touch
                // controls it already reset or disabled.
                saving = false;
                return;
            }

            if (!isSuccessful(result)) {
                saving = false;
                saveButton.textContent = "Save changes";
                countrySelect.disabled = false;
                cancelButton.disabled = false;
                setMessage(
                    businessMessage(
                        result ? result.payload : null,
                        "We couldn't save your changes. Nothing has "
                        + "been confirmed as completed. Please try "
                        + "again or contact support."
                    ),
                    "is-error"
                );
                updateSaveAvailability();
                return;
            }

            // Business fields are now committed - one Save click is
            // one logical edit operation that may also carry a
            // pending photo (mission "COMPLETE CONTACT PHOTO CRUD +
            // DOCX SOURCE SYNCHRONIZATION", section 11); each backend
            // mutation is independently atomic with the source DOCX,
            // sequenced here rather than merged into one request. The
            // baseline is refreshed immediately so a photo failure
            // below never re-asks the user to redo the part that
            // already succeeded.
            editBaseline = {
                has_photo: editBaseline ? editBaseline.has_photo : false,
            };
            editFields.forEach(([key, input]) => {
                editBaseline[key] = input.value.trim();
            });

            const pendingPhoto = editPhotoControl.input.files[0] || null;
            let photoFailed = false;

            if (pendingPhoto) {
                photoFailed = !(await uploadContactPhoto(
                    documentId,
                    contactId,
                    pendingPhoto
                ));
            }

            saving = false;
            saveButton.textContent = "Save changes";

            if (generation !== contactGeneration) {
                return;
            }

            countrySelect.disabled = false;
            cancelButton.disabled = false;

            if (photoFailed) {
                setMessage(
                    "The contact details were saved, but the photo "
                    + "could not be saved. You can retry the photo "
                    + "from this Edit contact screen.",
                    "is-error"
                );
                updateSaveAvailability();
                return;
            }

            await loadContacts(documentId);

            const savedContact = currentContacts.find(
                (contact) => contact.contact_id === contactId
            );

            if (savedContact) {
                beginEditContact(savedContact);
            }

            setMessage(
                "✓ The contact was updated successfully. The "
                + "chatbot content is now up to date.",
                "is-success"
            );
        }

        async function onAddSubmit() {
            if (
                saving || adding || deleting
                || addSubmitButton.disabled
            ) {
                return;
            }

            const documentId = countrySelect.value;

            if (documentId === "") {
                return;
            }

            const countryName = selectedCountryName();

            adding = true;
            addSubmitButton.disabled = true;
            addSubmitButton.textContent = "Adding contact…";
            cancelButton.disabled = true;
            countrySelect.disabled = true;
            setMessage("Adding contact…", null);

            const generation = contactGeneration;

            const formData = new FormData();
            formData.set("action", config.contactAddAction);
            formData.set("nonce", config.contactAddNonce);
            formData.set("document_id", documentId);

            addFields.forEach(([key, input]) => {
                formData.set(key, input.value.trim());
            });

            let result;

            try {
                result = await fetchJson(config.adminPostUrl, {
                    method: "POST",
                    body: formData,
                });
            } catch {
                result = null;
            }

            if (generation !== contactGeneration) {
                adding = false;
                return;
            }

            if (!isSuccessful(result)) {
                adding = false;
                addSubmitButton.textContent = "Add contact";
                countrySelect.disabled = false;
                cancelButton.disabled = false;
                setMessage(
                    businessMessage(
                        result ? result.payload : null,
                        "We couldn't add the contact. Nothing has "
                        + "been confirmed as completed. Please try "
                        + "again or contact support."
                    ),
                    "is-error"
                );
                updateAddSubmitAvailability();
                return;
            }

            // The contact now exists - a pending photo is a SEPARATE,
            // independently atomic mutation against the new
            // contact_id (mission section 13): its own failure must
            // never be reported as the contact itself failing to be
            // added.
            const pendingPhoto = addPhotoControl.input.files[0] || null;
            const createdContactId = (
                result.payload.data
                && typeof result.payload.data.contact_id === "string"
            )
                ? result.payload.data.contact_id
                : "";

            let photoFailed = false;

            if (pendingPhoto) {
                photoFailed = !(
                    createdContactId
                    && await uploadContactPhoto(
                        documentId,
                        createdContactId,
                        pendingPhoto
                    )
                );
            }

            adding = false;
            addSubmitButton.textContent = "Add contact";

            if (generation !== contactGeneration) {
                return;
            }

            countrySelect.disabled = false;
            cancelButton.disabled = false;

            if (photoFailed) {
                // One logical Add: a contact that exists only because
                // its own required photo failed to save is a
                // partially-created contact, which must never remain
                // (mission "FINAL BLOCKER", section 8) - roll it back
                // rather than leaving it dangling, and report an
                // honest full failure, never a partial success.
                const rolledBack = Boolean(
                    createdContactId
                    && await rollbackContact(
                        documentId,
                        createdContactId
                    )
                );

                await loadContacts(documentId);
                updateAddSubmitAvailability();

                setMessage(
                    rolledBack
                        ? "The contact could not be added because "
                            + "its photo could not be saved. Please "
                            + "try again."
                        : "The contact's photo could not be saved, "
                            + "and it could not be automatically "
                            + "removed either. Please open Edit "
                            + "contact and remove it, or retry the "
                            + "photo there.",
                    "is-error"
                );
                return;
            }

            resetAddOnlyFields();
            updateAddSubmitAvailability();

            await loadContacts(documentId);

            setMessage(
                `✓ The contact was added successfully for `
                + `${countryName}. The chatbot content is now up to `
                + "date.",
                "is-success"
            );
        }

        // Mission "ORDER 8G-B2", section 8 - Cancel resets only the
        // CURRENT mode's own field values (never the country/contact
        // selection, never collapses); this is the one deliberate
        // difference from wireEditSection()'s own Cancel (which also
        // clears country) - the mission's own wording for Contacts is
        // explicit: "restore persisted contact values, remain in
        // current contact mode/panel".
        function onCancel() {
            if (isDirty() && !window.confirm(UNSAVED_CHANGES_PROMPT)) {
                return;
            }

            if (mode === "add") {
                resetAddOnlyFields();
                updateAddSubmitAvailability();
            } else if (viewSubState === "edit-one" && editBaseline) {
                editFields.forEach(([key, input]) => {
                    input.value = editBaseline[key];
                });
                showStoredPhoto(
                    editPhotoControl,
                    countrySelect.value,
                    editIdInput.value,
                    editBaseline.has_photo
                );
                updateSaveAvailability();
            }

            setMessage("", null);
            updateCancelAvailability();
        }

        countrySelect.addEventListener("change", () => {
            if (isDirty() && !window.confirm(UNSAVED_CHANGES_PROMPT)) {
                countrySelect.value = previousCountryValue;
                return undefined;
            }

            previousCountryValue = countrySelect.value;
            return onCountryChange();
        });

        saveButton.addEventListener("click", onSave);
        addSubmitButton.addEventListener("click", onAddSubmit);
        cancelButton.addEventListener("click", onCancel);
        backToListButton.addEventListener("click", backToList);
        addBackToListButton.addEventListener(
            "click",
            backToListFromAdd
        );

        addAnotherButton.addEventListener("click", () => {
            viewSubState = "list";
            resetEditOnlyFields();
            setMode("add");
            expand();
        });

        modeViewButton.addEventListener("click", () => {
            setMode("view");
            expand();
        });
        modeAddButton.addEventListener("click", () => {
            setMode("add");
            expand();
        });

        // Mission "ORDER 8G-A.2", section 4 - a pure presentation-state
        // action: no dirty check, no reset, no backend request.
        collapseButton.addEventListener("click", collapse);

        renderModeUI();
        renderViewSubStateUI();
    }

    wireContactsPanel();

    wireDocumentsToolbar();
    wireDeleteForms();
    wireReindexForms();
    wireDocumentMenus();
    wireReviewButtons(document);

    // --- upload form: dropzone + multi-file queue --------------------

    const uploadForm = document.querySelector(
        ".le-global-chatbot-admin__upload-form"
    );

    if (!uploadForm) {
        return;
    }

    const fileInput = document.getElementById(FILE_INPUT_ID);

    const queue = [];
    let activeUploadCount = 0;
    let nextItemId = 0;

    // Mission "ORDER 8E-A1"/"ORDER 8E-A2", sections 5-9 - the exact
    // same full-resubmission pattern the backend itself was designed
    // around (see safe_upload_and_index_document's own docstring):
    // every decision round-trip just resends the whole file again
    // with different flags, never a token/session. countryConfirmed
    // and selectedCountryCode are the two new flags this mission adds
    // alongside the existing replaceExisting/confirmWarnings.
    function buildUploadFormData(file, {
        replaceExisting,
        confirmWarnings,
        countryConfirmed,
        selectedCountryCode,
        confirmContactReseed,
    }) {
        const formData = new FormData();

        const actionInput = uploadForm.querySelector(
            'input[name="action"]'
        );

        if (actionInput) {
            formData.set("action", actionInput.value);
        }

        uploadForm
            .querySelectorAll(
                'input[name="_wpnonce"], input[name="_wp_http_referer"]'
            )
            .forEach((input) => {
                formData.set(input.name, input.value);
            });

        formData.set("le_global_ajax", "1");
        formData.set("document", file);

        if (replaceExisting) {
            formData.set("replace_existing", "1");
        }

        if (confirmWarnings) {
            formData.set("confirm_warnings", "1");
        }

        if (countryConfirmed) {
            formData.set("country_confirmed", "1");
        }

        if (selectedCountryCode) {
            formData.set("selected_country_code", selectedCountryCode);
        }

        // Mission "ORDER 8G-B2", section 14 - confirming the Admin
        // wants to discard this country's Admin contact changes and
        // reseed them from the (possibly byte-identical) DOCX.
        if (confirmContactReseed) {
            formData.set("confirm_contact_reseed", "1");
        }

        return formData;
    }

    // Mission "ORDER 8E-A2", section 27 - REPLACE_WITH_DOCUMENT reuses
    // this exact same upload form data, just posted to the dedicated
    // conflict-resolution proxy action instead of the ordinary upload
    // one, with the target country_code attached - never a second,
    // parallel upload implementation.
    function buildResolveConflictReplaceFormData(file, countryCode, options) {
        const formData = buildUploadFormData(file, options);

        // Always reads fresh from the DOM via getAdminFormConfig(),
        // never the module-level adminFormConfig cache - that cache
        // stays null until the first AJAX refresh, and Review (like
        // any other decision in this flow) can be resolved before any
        // refresh has ever run on a freshly loaded page.
        const config = getAdminFormConfig();

        formData.set(
            "action",
            config ? config.resolveConflictReplaceAction : ""
        );
        formData.set(
            "_wpnonce",
            config ? config.resolveConflictReplaceNonce : ""
        );
        formData.set("country_code", countryCode);

        return formData;
    }

    async function sendUpload(file, options, item) {
        // uploadForm.action is shadowed by this very form's own
        // <input name="action"> in every real browser - see the note
        // in runFormAsAjax.
        const isConflictReplace = Boolean(
            item && item.resolveConflictCountryCode
        );

        const body = isConflictReplace
            ? buildResolveConflictReplaceFormData(
                file,
                item.resolveConflictCountryCode,
                options
            )
            : buildUploadFormData(file, options);

        // admin-post.php always dispatches on the "action" POST field
        // itself, never the request URL - the same endpoint URL is
        // correct for every admin_post_* action this form ever submits
        // to (upload, or the conflict-replace proxy action above).
        const response = await fetch(uploadForm.getAttribute("action"), {
            method: "POST",
            body,
            credentials: "same-origin",
        });

        let payload = null;

        try {
            payload = await response.json();
        } catch {
            payload = null;
        }

        return { response, payload };
    }

    // Mission "ORDER 8B", section 9 - zero-count categories are never
    // shown; while any file is still queued/uploading, the summary
    // reads as an in-progress count rather than a final tally.
    function renderQueue() {
        const container = document.getElementById(QUEUE_CONTAINER_ID);

        if (!container) {
            return;
        }

        if (queue.length === 0) {
            container.innerHTML = "";
            return;
        }

        const { total, allSettled, categories } = summarizeQueue(queue);

        const summaryLine = allSettled
            ? `${total} document${total === 1 ? "" : "s"} processed`
            : `Processing ${total} document${total === 1 ? "" : "s"}…`;

        const summaryDetails = categories
            .map((category) => `${category.icon} ${category.count} ${category.key}`)
            .join(" · ");

        container.innerHTML = (
            '<p class="le-global-chatbot-admin__queue-summary">'
            + escapeHtml(summaryLine)
            + (allSettled && summaryDetails ? ` — ${escapeHtml(summaryDetails)}` : "")
            + "</p>"
            + '<ul class="le-global-chatbot-admin__queue-list">'
            + queue.map(queueItemHtml).join("")
            + "</ul>"
        );

        container.querySelectorAll("[data-decision]").forEach((button) => {
            button.addEventListener("click", () => {
                const selectId = button.dataset.selectId;
                const selectedValue = selectId
                    ? (
                        document.getElementById(selectId)
                        && document.getElementById(selectId).value
                    ) || ""
                    : undefined;

                resolveDecision(
                    Number(button.dataset.itemId),
                    button.dataset.decision,
                    selectedValue
                );
            });
        });

        wireReviewButtons(container);
    }

    // Mission "ORDER 8B", section 8 - user-facing terminology only;
    // the backend's own internal status strings never change.
    //
    // Mission "ORDER 8E-A2", section 4 - a pending Admin decision
    // (awaiting_*) always reads as a distinct, business-worded state,
    // never "Queued"/"Indexed"/"Processing chunks" - those technical-
    // sounding words are deliberately never used anywhere here.
    const STATUS_TEXT = {
        queued: "Checking document…",
        uploading: "Checking document…",
        indexed: "Added",
        replaced: "Replaced",
        already_current: "Already up to date",
        awaiting_replacement_confirmation: "Waiting for confirmation",
        awaiting_warning_confirmation: "Waiting for confirmation",
        awaiting_combined_confirmation: "Waiting for confirmation",
        awaiting_country_confirmation: "Waiting for confirmation",
        awaiting_country_selection: "Waiting for confirmation",
        awaiting_contact_reseed_confirmation: "Waiting for confirmation",
        needs_conflict_resolution: "Action required",
        cancelled: "Cancelled",
        failed: "Failed",
    };

    function queueIconFor(status) {
        if (
            status === "indexed"
            || status === "replaced"
            || status === "already_current"
        ) {
            return { icon: "✓", cls: "is-success" };
        }

        if (status === "failed") {
            return { icon: "✕", cls: "is-error" };
        }

        if (
            status === "awaiting_replacement_confirmation"
            || status === "awaiting_warning_confirmation"
            || status === "awaiting_combined_confirmation"
            || status === "awaiting_country_confirmation"
            || status === "awaiting_country_selection"
            || status === "needs_conflict_resolution"
            || status === "awaiting_contact_reseed_confirmation"
        ) {
            return { icon: "⚠", cls: "is-warning" };
        }

        return { icon: "…", cls: "" };
    }

    function queueItemHtml(item) {
        const filename = escapeHtml(item.file.name);
        const statusText = STATUS_TEXT[item.status] || item.status;
        const { icon, cls } = queueIconFor(item.status);

        let extra = "";

        if (item.status === "failed") {
            extra = `<span class="le-global-chatbot-admin__queue-message">${escapeHtml(item.message)}</span>`;
        } else if (item.status === "awaiting_replacement_confirmation") {
            const country = (
                (item.detail && item.detail.country) || "This country"
            );
            const existingIds = (
                item.detail && Array.isArray(item.detail.existing_document_ids)
            )
                ? item.detail.existing_document_ids
                : [];
            const existingDocument = existingIds.length > 0
                ? findDocumentById(existingIds[0])
                : null;

            extra = (
                `<span class="le-global-chatbot-admin__queue-message">${escapeHtml(country)} already has a document.</span>`
                + (
                    existingDocument && existingDocument.source_filename
                        ? `<span class="le-global-chatbot-admin__queue-message">Current document: ${escapeHtml(existingDocument.source_filename)}</span>`
                        : ""
                )
                + `<span class="le-global-chatbot-admin__queue-message">New document: ${filename}</span>`
                + adminModifiedWarningHtml(item.detail)
                + decisionButtonsHtml(item.id, "Cancel", "cancel", "Replace document", "replace")
            );
        } else if (item.status === "awaiting_warning_confirmation") {
            extra = (
                warningMessagesHtml(item.detail)
                + decisionButtonsHtml(item.id, "Cancel", "cancel", "Continue", "continue")
            );
        } else if (item.status === "awaiting_combined_confirmation") {
            const country = (
                (item.detail && item.detail.country_name) || "this country"
            );

            extra = (
                warningMessagesHtml(item.detail)
                + `<span class="le-global-chatbot-admin__queue-message">${escapeHtml(country)} already has a document.</span>`
                + adminModifiedWarningHtml(item.detail)
                + decisionButtonsHtml(item.id, "Cancel", "cancel", "Continue and replace", "continue-and-replace")
            );
        } else if (
            item.status === "awaiting_contact_reseed_confirmation"
        ) {
            const country = (
                (item.detail && item.detail.country) || "This country"
            );

            extra = (
                `<span class="le-global-chatbot-admin__queue-message">${escapeHtml(country)} has changes made in the Admin.</span>`
                + '<span class="le-global-chatbot-admin__queue-message">'
                + "This document is identical to the one already "
                + "active, but confirming will discard those Admin "
                + "changes and regenerate contacts from it."
                + "</span>"
                + decisionButtonsHtml(
                    item.id,
                    "Cancel",
                    "cancel",
                    "Discard changes and reseed",
                    "reseed-contacts"
                )
            );
        } else if (item.status === "awaiting_country_confirmation") {
            extra = countryConfirmationHtml(item);
        } else if (item.status === "awaiting_country_selection") {
            extra = countrySelectionHtml(item);
        } else if (item.status === "needs_conflict_resolution") {
            const countryName = (
                (item.detail && (item.detail.country || item.detail.country_name))
                || "This country"
            );
            const countryCode = (
                (item.detail && item.detail.country_code) || ""
            );

            extra = (
                `<span class="le-global-chatbot-admin__queue-message">${escapeHtml(countryName)} already needs attention before a new document can be added.</span>`
                + '<span class="le-global-chatbot-admin__queue-decisions">'
                + '<button type="button" class="button button-primary" '
                + `data-review-country-code="${escapeHtml(countryCode)}" `
                + `data-review-country-name="${escapeHtml(countryName)}">`
                + "Review</button>"
                + "</span>"
            );
        } else if (
            (item.status === "indexed" || item.status === "replaced")
            && item.contactCount === 0
        ) {
            // Mission "ORDER 8G-B2", section 18 - informational and
            // actionable, never a red processing error: the document
            // itself is still exactly as successful as the status
            // above already says.
            extra = (
                '<span class="le-global-chatbot-admin__queue-message '
                + 'le-global-chatbot-admin__queue-warning">'
                + "⚠ Contact missing for this document. It does not "
                + "currently contain an L&amp;E Global contact — use "
                + "the Contacts panel to add one."
                + "</span>"
            );
        }

        return (
            '<li class="le-global-chatbot-admin__queue-item" '
            + `data-status="${escapeHtml(item.status)}">`
            + `<span class="le-global-chatbot-admin__queue-icon ${cls}" aria-hidden="true">${icon}</span>`
            + '<span class="le-global-chatbot-admin__queue-body">'
            + `<span class="le-global-chatbot-admin__queue-filename">${filename}</span>`
            + `<span class="le-global-chatbot-admin__queue-status">${escapeHtml(statusText)}</span>`
            + extra
            + "</span></li>"
        );
    }

    // The raw top-level error message on a warning/combined outcome
    // mentions confirm_warnings/indexing internals - each individual
    // warning's own .message field is written in business language
    // instead, so that is what renders here (mission "ORDER 8B",
    // section 6).
    function warningMessagesHtml(detail) {
        const warnings = (detail && Array.isArray(detail.warnings))
            ? detail.warnings
            : [];

        if (warnings.length === 0) {
            return (
                '<span class="le-global-chatbot-admin__queue-message">'
                + "This document may need a quick review before it is added."
                + "</span>"
            );
        }

        return warnings
            .map((warning) => (
                `<span class="le-global-chatbot-admin__queue-message">${escapeHtml(warning.message)}</span>`
            ))
            .join("");
    }

    // Mission "ORDER 8G-B2", section 12/13 - composed into the SAME
    // existing replacement-confirmation dialog (never a second,
    // separate modal) whenever this country's document has Admin
    // changes (a section or contact mutation) recorded since its last
    // accepted upload. Never mentions the marker/state/index by name -
    // purely business wording, matching the mission's own conceptual
    // text.
    function adminModifiedWarningHtml(detail) {
        if (!detail || !detail.admin_modified) {
            return "";
        }

        return (
            '<span class="le-global-chatbot-admin__queue-message '
            + 'le-global-chatbot-admin__queue-warning">'
            + "This country has changes made in the Admin. Uploading "
            + "this document will replace the current document and "
            + "discard those changes, including section and contact "
            + "information."
            + "</span>"
        );
    }

    function decisionButtonsHtml(itemId, cancelLabel, cancelValue, continueLabel, continueValue) {
        return (
            `<span class="le-global-chatbot-admin__queue-decisions">`
            + `<button type="button" class="button" data-decision="${cancelValue}" data-item-id="${itemId}">${escapeHtml(cancelLabel)}</button>`
            + `<button type="button" class="button button-primary" data-decision="${continueValue}" data-item-id="${itemId}">${escapeHtml(continueLabel)}</button>`
            + "</span>"
        );
    }

    // Mission "ORDER 8E-A2", section 5 - a detected country is always
    // shown with its human-friendly name and code (never a bare
    // code), with a prominent Confirm plus a discreet, always-
    // available correction path - a wrong automatic detection must
    // never need IT support to fix.
    function countryConfirmationHtml(item) {
        const countryName = (
            (item.detail && item.detail.country_name) || "Unknown"
        );
        const countryCode = (
            (item.detail && item.detail.country_code) || ""
        );

        return (
            '<span class="le-global-chatbot-admin__queue-message '
            + 'le-global-chatbot-admin__queue-country-heading">'
            + "Country detected</span>"
            + '<span class="le-global-chatbot-admin__queue-message '
            + 'le-global-chatbot-admin__queue-country-value">'
            + `<strong>${escapeHtml(countryName)}</strong>`
            + (countryCode ? ` — ${escapeHtml(countryCode)}` : "")
            + "</span>"
            + '<span class="le-global-chatbot-admin__queue-message">'
            + "Please confirm that this is the correct country for "
            + "this document."
            + "</span>"
            + decisionButtonsHtml(
                item.id,
                "Cancel",
                "cancel",
                "Confirm country",
                "confirm-country"
            )
            + '<button type="button" '
            + 'class="le-global-chatbot-admin__link-button" '
            + `data-decision="change-country" data-item-id="${item.id}">`
            + "Choose a different country</button>"
        );
    }

    // Mission "ORDER 8E-A2", sections 6/7 - the same select-a-country
    // panel serves two origins: the backend genuinely could not
    // identify a country at all (Cancel/Continue), or the Admin is
    // deliberately correcting an automatic detection they clicked
    // "Choose a different country" on (Back/Continue instead). Either
    // way the option list is always the server's own authoritative
    // allowed-country list carried on the decision itself - never a
    // second, client-invented copy of it.
    function countrySelectionHtml(item) {
        const isCorrection = item.countrySelectionOrigin === "correction";
        const heading = isCorrection
            ? "Select the correct country"
            : "We couldn't identify the country automatically.";
        const options = (item.allowedCountries || [])
            .map((option) => (
                `<option value="${escapeHtml(option.code)}">`
                + `${escapeHtml(option.name)} (${escapeHtml(option.code)})`
                + "</option>"
            ))
            .join("");
        const selectId = `le-global-queue-select-${item.id}`;
        const backOrCancelLabel = isCorrection ? "Back" : "Cancel";
        const backOrCancelDecision = isCorrection
            ? "back-to-confirmation"
            : "cancel";

        return (
            '<span class="le-global-chatbot-admin__queue-message '
            + 'le-global-chatbot-admin__queue-country-heading">'
            + `${escapeHtml(heading)}</span>`
            + (
                isCorrection
                    ? ""
                    : '<span class="le-global-chatbot-admin__queue-message">'
                        + "Please select the country this document "
                        + "belongs to."
                        + "</span>"
            )
            + (
                item.selectionError
                    ? '<span class="le-global-chatbot-admin__queue-message '
                        + 'le-global-chatbot-admin__queue-error">'
                        + `${escapeHtml(item.selectionError)}</span>`
                    : ""
            )
            + '<span class="le-global-chatbot-admin__queue-field">'
            + `<label for="${selectId}">Country</label>`
            + `<select id="${selectId}">`
            + '<option value="">Select a country…</option>'
            + options
            + "</select>"
            + "</span>"
            + '<span class="le-global-chatbot-admin__queue-decisions">'
            + '<button type="button" class="button" '
            + `data-decision="${backOrCancelDecision}" `
            + `data-item-id="${item.id}">`
            + `${escapeHtml(backOrCancelLabel)}</button>`
            + '<button type="button" class="button button-primary" '
            + 'data-decision="select-country" '
            + `data-item-id="${item.id}" data-select-id="${selectId}">`
            + "Continue</button>"
            + "</span>"
        );
    }

    function findItem(itemId) {
        return queue.find((candidate) => candidate.id === itemId) || null;
    }

    // Mission "ORDER 8E-A2", section 33 - two client-only transitions
    // (change-country/back-to-confirmation) never touch the network at
    // all; every other decision triggers exactly one fresh
    // resubmission of the same file, guarded by requestToken (see
    // runUpload) against a reply that arrives after the item has since
    // moved on.
    function resolveDecision(itemId, decision, selectedValue) {
        const item = findItem(itemId);

        if (!item) {
            return;
        }

        if (decision === "cancel") {
            item.status = "cancelled";
            item.message = "Cancelled by the administrator.";
            renderQueue();
            return;
        }

        if (decision === "change-country") {
            item.originalCountryDetail = item.detail;
            item.countrySelectionOrigin = "correction";
            item.allowedCountries = (
                (item.detail && item.detail.allowed_countries) || []
            );
            item.selectionError = null;
            item.status = "awaiting_country_selection";
            renderQueue();
            return;
        }

        if (decision === "back-to-confirmation") {
            item.detail = item.originalCountryDetail || item.detail;
            item.selectionError = null;
            item.status = "awaiting_country_confirmation";
            renderQueue();
            return;
        }

        const replaceExisting = (
            decision === "replace" || decision === "continue-and-replace"
        );

        const confirmWarnings = (
            decision === "continue" || decision === "continue-and-replace"
        );

        // Mission "ORDER 8G-B2", section 14 - "Confirm" on the
        // identical-bytes-but-Admin-modified dialog resubmits with
        // this one additional flag; the DOCX bytes themselves never
        // change, only the structured contact state is reseeded.
        const confirmContactReseed = decision === "reseed-contacts";

        if (decision === "select-country" && !(selectedValue || "")) {
            // Nothing selected yet - stay on the same panel rather
            // than submitting an empty choice the server would only
            // reject.
            item.selectionError = "Select a country to continue.";
            renderQueue();
            return;
        }

        // Mission "ORDER 8E-A2", section 33 - every resubmission is a
        // completely fresh, independent request with no server-side
        // memory of earlier ones (the backend's own documented
        // design - see safe_upload_and_index_document's docstring).
        // Once a country has been confirmed or selected in ANY round
        // for this item, that fact must be carried forward on EVERY
        // later resubmission (continue/replace/continue-and-replace),
        // never only on the one decision that first produced it -
        // otherwise the backend's own country-confirms-before-content
        // gate correctly (and confusingly, from the UI's perspective)
        // asks again.
        if (decision === "confirm-country") {
            item.countryConfirmed = true;
        }

        if (decision === "select-country") {
            item.selectedCountryCode = selectedValue;
        }

        item.status = "queued";
        item.selectionError = null;
        item.forcedOptions = {
            replaceExisting,
            confirmWarnings,
            countryConfirmed: Boolean(item.countryConfirmed),
            selectedCountryCode: item.selectedCountryCode || "",
            confirmContactReseed,
        };

        // A single, deliberate admin decision (Replace/Continue/
        // Confirm/Select) on one file - refreshed on its own, exactly
        // once, distinct from the batch-wide single refresh below.
        // Routed through the same pumpQueue() cap as every other
        // upload rather than started directly, so a decision resolved
        // while a batch is already at MAX_CONCURRENT_UPLOADS can never
        // push the real concurrency past that cap - it simply waits
        // its turn.
        item.refreshOnComplete = true;
        renderQueue();

        pumpQueue();
    }

    async function runUpload(item) {
        item.status = "uploading";

        // Mission "ORDER 8E-A2", section 33 - a per-item token, bumped
        // on every fresh attempt: if a second attempt somehow starts
        // for this same item before this one's response arrives (the
        // UI itself already prevents this by hiding decision buttons
        // once a file is "uploading", but this is the explicit guard
        // the mission asks for rather than relying on that alone), the
        // stale reply is discarded instead of overwriting newer state.
        item.requestToken = (item.requestToken || 0) + 1;
        const requestToken = item.requestToken;

        renderQueue();

        const options = item.forcedOptions || {
            replaceExisting: false,
            confirmWarnings: false,
            countryConfirmed: false,
            selectedCountryCode: "",
            confirmContactReseed: false,
        };

        let result;

        try {
            result = await sendUpload(item.file, options, item);
        } catch (error) {
            if (item.requestToken !== requestToken) {
                return;
            }

            item.status = "failed";
            item.message = (
                (error && typeof error.message === "string" && error.message)
                || "The document could not be added."
            );
            renderQueue();
            return;
        }

        if (item.requestToken !== requestToken) {
            return;
        }

        const outcome = classifyUploadResponse(
            result.response.status,
            result.payload
        );

        item.detail = outcome.detail;
        item.message = outcome.message;

        if (outcome.kind === "already_current") {
            item.status = "already_current";
            renderQueue();
            return;
        }

        if (outcome.kind === "identical_but_admin_modified") {
            item.status = "awaiting_contact_reseed_confirmation";
            renderQueue();
            return;
        }

        if (outcome.kind === "replacement_required") {
            item.status = "awaiting_replacement_confirmation";
            renderQueue();
            return;
        }

        if (outcome.kind === "warning_required") {
            item.status = "awaiting_warning_confirmation";
            renderQueue();
            return;
        }

        if (outcome.kind === "combined_required") {
            item.status = "awaiting_combined_confirmation";
            renderQueue();
            return;
        }

        if (outcome.kind === "country_confirmation_required") {
            item.status = "awaiting_country_confirmation";
            renderQueue();
            return;
        }

        if (outcome.kind === "country_selection_required") {
            item.status = "awaiting_country_selection";
            item.countrySelectionOrigin = "backend";
            item.allowedCountries = (
                (item.detail && item.detail.allowed_countries) || []
            );
            item.selectionError = null;
            renderQueue();
            return;
        }

        if (outcome.kind === "country_selection_invalid") {
            item.status = "awaiting_country_selection";
            item.selectionError = outcome.message;
            item.allowedCountries = (
                (item.detail && item.detail.allowed_countries)
                || item.allowedCountries
                || []
            );
            renderQueue();
            return;
        }

        if (outcome.kind === "conflict_review_required") {
            item.status = "needs_conflict_resolution";
            renderQueue();
            return;
        }

        if (outcome.kind === "error") {
            item.status = "failed";
            renderQueue();
            return;
        }

        // Mission "ORDER 8G-B2", section 18 - a successful upload/
        // replace/reseed whose resulting structured contact count is
        // exactly 0 gets a non-blocking, actionable warning alongside
        // its normal success status - the document itself still
        // finishes exactly as before (Ready), this is purely
        // informational.
        item.contactCount = (
            result.payload
            && result.payload.data
            && typeof result.payload.data.contact_count === "number"
        )
            ? result.payload.data.contact_count
            : null;

        item.status = (
            item.forcedOptions && item.forcedOptions.replaceExisting
        )
            ? "replaced"
            : "indexed";
        renderQueue();
    }

    function pumpQueue() {
        for (const item of queue) {
            if (activeUploadCount >= MAX_CONCURRENT_UPLOADS) {
                break;
            }

            if (item.status === "queued" && !item.started) {
                item.started = true;
                activeUploadCount += 1;

                runUpload(item).finally(() => {
                    activeUploadCount -= 1;
                    item.started = false;

                    if (item.refreshOnComplete) {
                        item.refreshOnComplete = false;
                        refreshAdminState();
                    }

                    pumpQueue();
                });
            }
        }
    }

    function settledPromise(item) {
        return new Promise((resolve) => {
            const check = () => {
                if (item.status !== "queued" && item.status !== "uploading") {
                    resolve();
                    return;
                }

                setTimeout(check, 10);
            };

            check();
        });
    }

    function enqueueFiles(files, extraFields) {
        const newItems = files.map((file) => {
            const item = {
                id: nextItemId,
                file,
                status: "queued",
                message: "",
                detail: null,
                ...extraFields,
            };

            nextItemId += 1;
            queue.push(item);

            return item;
        });

        renderQueue();
        pumpQueue();

        return newItems;
    }

    // Mission "ORDER 8B", section 7 - a brand-new file selection is
    // always its own batch: the previous batch's summary and result
    // list never bleed into the next one. This clears only the
    // in-page batch DISPLAY - it never deletes an already-indexed
    // document. Returns a promise that settles once the whole batch
    // has left the queued/uploading state and the single shared
    // refresh for it has run - never awaited by the live change/drop
    // handlers below (genuinely fire-and-forget there), but useful
    // for anything (including tests) that needs to know a batch is
    // done.
    function startNewBatch(files) {
        queue.length = 0;
        renderQueue();

        const newItems = enqueueFiles(files);

        // One refresh for the whole batch, never one per file
        // (mission "ORDER 5D", section 4) - a file still awaiting a
        // replacement/warning decision at this point is refreshed
        // later, on its own, once the admin actually resolves it
        // (see resolveDecision above).
        return Promise.all(newItems.map(settledPromise)).then(() => (
            refreshAdminState()
        ));
    }

    function handleFilesSelected(files) {
        const fileArray = Array.from(files || []);

        if (fileArray.length === 0) {
            return Promise.resolve();
        }

        return startNewBatch(fileArray);
    }

    // Mission "ORDER 8B", section 5 - the real <input type="file">
    // stays the one true source of files (reachable by keyboard/
    // assistive tech via the "Choose documents" label's native
    // for=/id= relationship, no JS required for that part); drag-and-
    // drop on the dropzone is a purely additive convenience that
    // funnels into the exact same entry point.
    function wireUploadDropzone() {
        const dropzone = document.getElementById("le-global-dropzone");
        const fallbackSubmit = uploadForm.querySelector(
            ".le-global-chatbot-admin__upload-fallback-submit"
        );

        // Hiding the no-JS fallback submit button only once this
        // script actually runs means a genuinely no-JS visitor still
        // sees and can use it - it is never removed from the DOM,
        // only taken out of the accessible/tab order once the
        // enhanced flow above has taken over (mission "ORDER 8B",
        // section 5).
        if (fallbackSubmit) {
            fallbackSubmit.hidden = true;
        }

        if (fileInput) {
            fileInput.addEventListener("change", () => {
                const files = getSelectedFiles(fileInput);

                // A real browser never fires "change" a second time for
                // an unchanged selection (confirmed against real
                // Chromium, mission "ORDER 8B") - clearing the input's
                // own value immediately after reading it means picking
                // the exact same file(s) again still registers as a
                // fresh change, so a repeat upload of an identical
                // batch works exactly like the first one.
                fileInput.value = "";

                return handleFilesSelected(files);
            });
        }

        if (!dropzone) {
            return;
        }

        ["dragenter", "dragover"].forEach((eventName) => {
            dropzone.addEventListener(eventName, (event) => {
                event.preventDefault();
                dropzone.classList.add("is-dragover");
            });
        });

        ["dragleave", "dragend"].forEach((eventName) => {
            dropzone.addEventListener(eventName, (event) => {
                event.preventDefault();
                dropzone.classList.remove("is-dragover");
            });
        });

        dropzone.addEventListener("drop", (event) => {
            event.preventDefault();
            dropzone.classList.remove("is-dragover");

            const files = (event.dataTransfer && event.dataTransfer.files)
                ? Array.from(event.dataTransfer.files)
                : [];

            return handleFilesSelected(files);
        });
    }

    wireUploadDropzone();

    // Defensive no-JS-submit fallback: routes through the exact same
    // entry point as the change/drop handlers above, so even if this
    // ever fires (it shouldn't - the fallback submit button is hidden
    // the moment this script runs) it can never start a second,
    // duplicate batch alongside one already in progress. Returns the
    // batch-completion promise (ignored by a real browser's dispatch,
    // useful for a caller - e.g. a test - that invokes this handler
    // directly).
    uploadForm.addEventListener("submit", (event) => {
        event.preventDefault();
        return handleFilesSelected(getSelectedFiles(fileInput));
    });

    // Test-only hook: absent in the browser (module is never defined
    // there), so this changes nothing about how the admin page itself
    // loads or runs - see assets/chatbot.js for the identical pattern.
    if (typeof module !== "undefined" && module.exports) {
        module.exports = {
            errorMessage,
            extractStructuredDetail,
            isReplacementRequiredResponse,
            classifyUploadResponse,
            businessMessage,
            detectConflictedCountryCodes,
            requiresActionFor,
            computeDisplayStatus,
            groupDocumentsByCountryCode,
            formatLastUpdated,
            rowMatchesFilter,
            normalizeTitle,
            findDuplicateSectionIn,
            buildPositionOptions,
            summarizeQueue,
            MAX_CONCURRENT_UPLOADS,
            getSelectedFiles,
            __queueForTests: {
                enqueueFiles,
                resolveDecision,
                startNewBatch,
                refresh: refreshAdminState,
                getQueueSnapshot: () => queue.map((item) => ({ ...item })),
                activeUploadCount: () => activeUploadCount,
                enqueueConflictReplacement,
            },
            __conflictReviewForTests: {
                open: openConflictReview,
                close: closeConflictReview,
                handleAction: handleConflictAction,
                getState: () => (
                    conflictReviewState
                        ? { ...conflictReviewState }
                        : null
                ),
            },
        };
    }
})();
