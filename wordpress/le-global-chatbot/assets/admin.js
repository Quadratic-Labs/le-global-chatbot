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

    function errorMessage(payload, fallback = "The document could not be indexed.") {
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
    const BUSINESS_ERROR_MESSAGES = {
        section_already_exists: (
            "This section already exists. Use \"Edit a section\" to "
            + "update it."
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
    function classifyUploadResponse(statusCode, payload) {
        const detail = extractStructuredDetail(payload);
        const fallback = "The document could not be indexed.";

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

            if (detail.code === "document_warning_confirmation_required") {
                return {
                    kind: detail.replacement_required
                        ? "combined_required"
                        : "warning_required",
                    detail,
                    message: businessMessage(payload, fallback),
                };
            }
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

    function computeDisplayStatus(document, hasCountryConflict) {
        if (hasCountryConflict) {
            return {
                value: "needs_attention",
                label: "Needs attention",
                icon: "⚠",
                cls: "is-warning",
                title: "This country has conflicting document records.",
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

        const conflictedCodes = detectConflictedCountryCodes(documents);
        const needsAttention = documents.filter((document) => {
            const code = document.country_code || "";
            const hasConflict = code !== "" && conflictedCodes.has(code);
            return computeDisplayStatus(document, hasConflict).value !== "ready";
        }).length;

        container.innerHTML = (
            summaryCardHtml("Documents", totalDocuments)
            + summaryCardHtml("Countries", totalCountries)
            + summaryCardHtml("Documents needing attention", needsAttention)
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
        const rows = lastKnownDocuments
            .map((item) => documentRowHtml(item, conflictedCodes))
            .join("");

        container.innerHTML = (
            '<div class="le-global-chatbot-admin__table-container">'
            + '<table class="widefat striped le-global-chatbot-admin__table" id="le-global-documents-table">'
            + "<thead><tr>"
            + "<th scope=\"col\">Country</th>"
            + "<th scope=\"col\">Document</th>"
            + "<th scope=\"col\">Year</th>"
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
        applyDocumentsFilter();
    }

    function updateDocumentCount(count) {
        const element = document.getElementById("le-global-document-count");

        if (!element) {
            return;
        }

        element.textContent = `${count} document${count === 1 ? "" : "s"}`;
    }

    function documentRowHtml(item, conflictedCodes) {
        const country = item.country || "";
        const countryCode = item.country_code || "";
        const filename = item.source_filename || "";
        const hasConflict = (
            countryCode !== "" && conflictedCodes.has(countryCode)
        );
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
            + `<td>${item.reference_year ? escapeHtml(item.reference_year) : "—"}</td>`
            + `<td>${statusBadgeHtml(displayStatus)}</td>`
            + `<td>${escapeHtml(formatLastUpdated(item.updated_at))}</td>`
            + `<td>${rowActionsHtml(item, hasConflict)}</td>`
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

    function rowActionsHtml(item, hasConflict) {
        const parts = [];

        if (item.download_url) {
            parts.push(
                `<a class="button" href="${escapeHtml(item.download_url)}">Download</a>`
            );
        } else {
            parts.push(
                '<button type="button" class="button" disabled '
                + 'title="No unambiguous source document is available to download.">'
                + "Download</button>"
            );
        }

        let refreshItem;

        if (adminFormConfig && item.reindex_nonce && !hasConflict) {
            refreshItem = actionFormHtml({
                action: adminFormConfig.reindexAction,
                nonce: item.reindex_nonce,
                documentId: item.document_id,
                buttonClass: "le-global-chatbot-admin__menu-item",
                buttonLabel: "Refresh chatbot data",
                markerAttribute: "data-reindex-form",
                buttonAttributes: 'role="menuitem"',
            });
        } else {
            const title = hasConflict
                ? "This country has conflicting document records."
                : "The source document is unavailable.";

            refreshItem = (
                '<button type="button" class="le-global-chatbot-admin__menu-item" '
                + `role="menuitem" disabled title="${escapeHtml(title)}">`
                + "Refresh chatbot data</button>"
            );
        }

        let deleteItem = "";

        if (adminFormConfig && item.delete_nonce) {
            deleteItem = actionFormHtml({
                action: adminFormConfig.deleteAction,
                nonce: item.delete_nonce,
                documentId: item.document_id,
                buttonClass: "le-global-chatbot-admin__menu-item is-destructive",
                buttonLabel: "Delete document",
                markerAttribute: "data-confirm-delete",
                formAttributes: (
                    `data-document-name="${escapeHtml(item.source_filename || "")}" `
                    + `data-country-name="${escapeHtml(item.country || "")}"`
                ),
                buttonAttributes: 'role="menuitem"',
            });
        }

        parts.push(
            '<div class="le-global-chatbot-admin__menu">'
            + '<button type="button" class="button le-global-chatbot-admin__menu-toggle" '
            + 'aria-haspopup="true" aria-expanded="false" '
            + `aria-label="${escapeHtml("More actions for " + (item.source_filename || ""))}">`
            + "&hellip;</button>"
            + '<div class="le-global-chatbot-admin__menu-list" role="menu" hidden>'
            + refreshItem
            + '<div class="le-global-chatbot-admin__menu-separator"></div>'
            + deleteItem
            + "</div></div>"
        );

        return (
            '<div class="le-global-chatbot-admin__actions">'
            + parts.join("")
            + "</div>"
        );
    }

    function actionFormHtml({
        action,
        nonce,
        documentId,
        buttonClass,
        buttonLabel,
        markerAttribute,
        formAttributes = "",
        buttonAttributes = "",
    }) {
        return (
            `<form method="post" action="${escapeHtml(adminFormConfig.adminPostUrl || "")}" ${markerAttribute} ${formAttributes}>`
            + `<input type="hidden" name="action" value="${escapeHtml(action)}">`
            + `<input type="hidden" name="document_id" value="${escapeHtml(documentId)}">`
            + `<input type="hidden" name="_wpnonce" value="${escapeHtml(nonce)}">`
            + `<button type="submit" class="${buttonClass}" ${buttonAttributes}>${escapeHtml(buttonLabel)}</button>`
            + "</form>"
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
        const textarea = document.getElementById(
            "le-global-edit-content"
        );
        const editHintEl = document.getElementById("le-global-edit-hint");
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

        if (
            !modeEditButton || !modeAddButton || !countrySelect
            || !editOnlyFields || !sectionSelect || !textarea || !editHintEl
            || !addOnlyFields || !addTitleInput || !addPositionSelect
            || !duplicateWarningEl || !addContentTextarea || !messageEl
            || !cancelButton || !saveButton || !addSubmitButton
        ) {
            return;
        }

        const config = container.dataset;

        let mode = "edit";
        let saving = false;
        let adding = false;
        let previousCountryValue = "";
        let previousSectionValue = "";
        let currentSections = [];
        let editBaselineContent = null;

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
                && textarea.value !== editBaselineContent
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
            const ready = (
                !saving
                && editBaselineContent !== null
                && textarea.value !== editBaselineContent
                && textarea.value.trim() !== ""
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
            if (mode === nextMode || saving || adding) {
                return;
            }

            if (isDirty() && !window.confirm(UNSAVED_CHANGES_PROMPT)) {
                return;
            }

            mode = nextMode;
            setMessage("", null);
            renderModeUI();
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
            textarea.value = "";
            textarea.disabled = true;
            editBaselineContent = null;
            editHintEl.textContent = "";

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
            textarea.value = "";
            textarea.disabled = true;
            editBaselineContent = null;
            editHintEl.textContent = "";

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

            textarea.value = "";
            textarea.disabled = true;
            editBaselineContent = null;
            editHintEl.textContent = "";
            setMessage("", null);
            saveButton.disabled = true;

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

            textarea.value = content;
            textarea.disabled = false;
            editBaselineContent = content;
            saveButton.disabled = true;

            const sectionTitle = sectionSelect.options[
                sectionSelect.selectedIndex
            ]
                ? sectionSelect.options[sectionSelect.selectedIndex].textContent
                : "this section";

            editHintEl.textContent = (
                `Saving will replace the current content of "${sectionTitle}" `
                + `in the ${selectedCountryName()} document.`
            );
        }

        async function onSave() {
            if (saving || adding || saveButton.disabled) {
                return;
            }

            const documentId = countrySelect.value;
            const sectionId = sectionSelect.value;

            if (documentId === "" || sectionId === "") {
                return;
            }

            const sectionTitle = sectionSelect.options[
                sectionSelect.selectedIndex
            ]
                ? sectionSelect.options[sectionSelect.selectedIndex].textContent
                : "This section";
            const countryName = selectedCountryName();

            saving = true;
            saveButton.disabled = true;
            saveButton.textContent = "Saving…";
            cancelButton.disabled = true;
            countrySelect.disabled = true;
            sectionSelect.disabled = true;
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

            // Re-fetch the section so the UI always shows the value
            // really persisted, never just the value that was sent
            // (mission "ORDER 5D", section 2).
            const url = buildQueryUrl(
                config.sectionGetAction,
                config.sectionGetNonce,
                { document_id: documentId, section_id: sectionId }
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
            modeEditButton.disabled = false;
            modeAddButton.disabled = false;

            if (
                isSuccessful(refetch)
                && refetch.payload.data
                && typeof refetch.payload.data.content === "string"
            ) {
                textarea.value = refetch.payload.data.content;
                editBaselineContent = refetch.payload.data.content;
                setMessage(
                    `✓ ${sectionTitle} was updated successfully. The `
                    + `${countryName} document and chatbot content `
                    + "are now up to date.",
                    "is-success"
                );
            } else {
                editBaselineContent = textarea.value;
                setMessage(
                    "Saved, but the updated content could not be "
                    + "re-loaded for confirmation.",
                    "is-error"
                );
            }

            updateSaveAvailability();
        }

        async function onAddSubmit() {
            if (saving || adding || addSubmitButton.disabled) {
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

        textarea.addEventListener("input", updateSaveAvailability);
        saveButton.addEventListener("click", onSave);

        addTitleInput.addEventListener("input", () => {
            checkDuplicateTitle();
            updateAddSubmitAvailability();
        });
        addContentTextarea.addEventListener("input", updateAddSubmitAvailability);
        addPositionSelect.addEventListener("change", updateAddSubmitAvailability);
        addSubmitButton.addEventListener("click", onAddSubmit);

        cancelButton.addEventListener("click", onCancel);
        modeEditButton.addEventListener("click", () => setMode("edit"));
        modeAddButton.addEventListener("click", () => setMode("add"));

        renderModeUI();
    }

    wireEditSection();

    wireDocumentsToolbar();
    wireDeleteForms();
    wireReindexForms();
    wireDocumentMenus();

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

    function buildUploadFormData(file, { replaceExisting, confirmWarnings }) {
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

        return formData;
    }

    async function sendUpload(file, options) {
        // uploadForm.action is shadowed by this very form's own
        // <input name="action"> in every real browser - see the note
        // in runFormAsAjax.
        const response = await fetch(uploadForm.getAttribute("action"), {
            method: "POST",
            body: buildUploadFormData(file, options),
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
                resolveDecision(
                    Number(button.dataset.itemId),
                    button.dataset.decision
                );
            });
        });
    }

    // Mission "ORDER 8B", section 8 - user-facing terminology only;
    // the backend's own internal status strings never change.
    const STATUS_TEXT = {
        queued: "Queued",
        uploading: "Uploading…",
        indexed: "Added",
        replaced: "Replaced",
        already_current: "Already up to date",
        awaiting_replacement_confirmation: "Needs confirmation",
        awaiting_warning_confirmation: "Needs confirmation",
        awaiting_combined_confirmation: "Needs confirmation",
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
                + decisionButtonsHtml(item.id, "Cancel", "cancel", "Continue and replace", "continue-and-replace")
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

    function decisionButtonsHtml(itemId, cancelLabel, cancelValue, continueLabel, continueValue) {
        return (
            `<span class="le-global-chatbot-admin__queue-decisions">`
            + `<button type="button" class="button" data-decision="${cancelValue}" data-item-id="${itemId}">${escapeHtml(cancelLabel)}</button>`
            + `<button type="button" class="button button-primary" data-decision="${continueValue}" data-item-id="${itemId}">${escapeHtml(continueLabel)}</button>`
            + "</span>"
        );
    }

    function findItem(itemId) {
        return queue.find((candidate) => candidate.id === itemId) || null;
    }

    function resolveDecision(itemId, decision) {
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

        const replaceExisting = (
            decision === "replace" || decision === "continue-and-replace"
        );

        const confirmWarnings = (
            decision === "continue" || decision === "continue-and-replace"
        );

        item.status = "queued";
        item.forcedOptions = { replaceExisting, confirmWarnings };

        // A single, deliberate admin decision (Replace/Continue) on
        // one file - refreshed on its own, exactly once, distinct
        // from the batch-wide single refresh below. Routed through
        // the same pumpQueue() cap as every other upload rather than
        // started directly, so a decision resolved while a batch is
        // already at MAX_CONCURRENT_UPLOADS can never push the real
        // concurrency past that cap - it simply waits its turn.
        item.refreshOnComplete = true;
        renderQueue();

        pumpQueue();
    }

    async function runUpload(item) {
        item.status = "uploading";
        renderQueue();

        const options = item.forcedOptions || {
            replaceExisting: false,
            confirmWarnings: false,
        };

        let result;

        try {
            result = await sendUpload(item.file, options);
        } catch (error) {
            item.status = "failed";
            item.message = (
                (error && typeof error.message === "string" && error.message)
                || "The document could not be indexed."
            );
            renderQueue();
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

        if (outcome.kind === "error") {
            item.status = "failed";
            renderQueue();
            return;
        }

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

    function enqueueFiles(files) {
        const newItems = files.map((file) => {
            const item = {
                id: nextItemId,
                file,
                status: "queued",
                message: "",
                detail: null,
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
            computeDisplayStatus,
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
            },
        };
    }
})();
