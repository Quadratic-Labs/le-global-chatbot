(() => {
    "use strict";

    const MAX_CONCURRENT_UPLOADS = 2;

    const FILE_INPUT_ID = "le-global-document";
    const QUEUE_CONTAINER_ID = "le-global-chatbot-queue";
    const DOCUMENTS_CONTAINER_ID = "le-global-chatbot-documents";
    const SUMMARY_CONTAINER_ID = "le-global-chatbot-summary";

    // Populated from the real upload form's data-* attributes the
    // first time a refresh runs - the single source of truth for the
    // action name strings stays server-side (PHP constants), JS only
    // ever reads them, never hardcodes them (mission "ORDER 4",
    // section 6: no client-invented endpoint name).
    let adminFormConfig = null;

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

    // --- pure response-classification helpers (unchanged contract) -

    function errorMessage(payload) {
        if (
            payload
            && payload.data
            && typeof payload.data.message === "string"
            && payload.data.message.trim() !== ""
        ) {
            return payload.data.message.trim();
        }

        return "The document could not be indexed.";
    }

    // The backend's own structured 409/4xx payload, as the WordPress
    // AJAX proxy relays it: wp_send_json_error wraps whatever the
    // proxy passed as {success:false, data:{message, detail}} -
    // detail is only ever a plain object for the backend's own
    // structured codes, never a string itself (the proxy sends []
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

    // Classifies one upload response into exactly one outcome kind -
    // the single place that decides which queue-item status a
    // response maps to, kept pure/DOM-free so it is directly
    // testable (mission "ORDER 4", section 15: warning, replacement,
    // combined and already-current are four distinct, never-
    // confused outcomes).
    function classifyUploadResponse(statusCode, payload) {
        const detail = extractStructuredDetail(payload);

        if (statusCode === 409 && detail) {
            if (detail.code === "document_already_current") {
                return { kind: "already_current", detail, message: errorMessage(payload) };
            }

            if (detail.code === "document_replacement_required") {
                return { kind: "replacement_required", detail, message: errorMessage(payload) };
            }

            if (detail.code === "document_warning_confirmation_required") {
                return {
                    kind: detail.replacement_required
                        ? "combined_required"
                        : "warning_required",
                    detail,
                    message: errorMessage(payload),
                };
            }
        }

        if (!payload || payload.success !== true) {
            return { kind: "error", detail, message: errorMessage(payload) };
        }

        return { kind: "success", detail: null, message: errorMessage(payload) };
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
                const documentName = (
                    form.dataset.documentName || "this document"
                );

                const confirmed = window.confirm(
                    `Delete ${documentName}? `
                    + "The source DOCX and all indexed chunks "
                    + "will be removed."
                );

                if (!confirmed) {
                    event.preventDefault();
                    return;
                }

                event.preventDefault();
                runFormAsAjax(form, {
                    disable: form.querySelector('button[type="submit"]'),
                    busyText: "Deleting…",
                });
            });
        });
    }

    // --- reindex forms: progressive enhancement (new capability) ---

    function wireReindexForms() {
        const reindexForms = document.querySelectorAll(
            "[data-reindex-form]"
        );

        reindexForms.forEach((form) => {
            form.addEventListener("submit", (event) => {
                event.preventDefault();
                runFormAsAjax(form, {
                    disable: form.querySelector('button[type="submit"]'),
                    busyText: "Reindexing…",
                });
            });
        });
    }

    // Shared enhancement for the native per-row forms (reindex,
    // delete): never a double click = two mutations (mission
    // "ORDER 4", section 54) - the button is disabled for the whole
    // request and only re-enabled by a full refresh (success) or
    // explicitly on failure, so a user cannot fire it twice while a
    // reindex/delete is genuinely still in flight.
    async function runFormAsAjax(form, { disable, busyText }) {
        if (disable && disable.disabled) {
            return;
        }

        if (disable) {
            disable.disabled = true;
            disable.dataset.originalText = disable.textContent;
            disable.textContent = busyText;
        }

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
                throw new Error(errorMessage(payload));
            }

            await refreshAdminState();

            if (disable) {
                disable.disabled = false;
                disable.textContent = disable.dataset.originalText || busyText;
            }
        } catch (error) {
            if (disable) {
                disable.disabled = false;
                disable.textContent = disable.dataset.originalText || busyText;
            }

            window.alert(
                (error && typeof error.message === "string" && error.message)
                    || "The request could not be completed."
            );
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

        const totalChunks = documents.reduce(
            (sum, item) => sum + (Number(item.chunk_count) || 0),
            0
        );

        renderSummary(payload.data.stats, totalChunks);
        renderDocuments(documents);
    }

    function renderSummary(stats, totalChunks) {
        const container = document.getElementById(SUMMARY_CONTAINER_ID);

        if (!container || !stats) {
            return;
        }

        container.innerHTML = (
            summaryCardHtml("Indexed documents", stats.total_documents)
            + summaryCardHtml("Countries", stats.total_countries)
            + summaryCardHtml("Indexed chunks", totalChunks)
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

    const STATUS_LABELS = {
        indexed: "Indexed",
        indexed_source_conflict: "Source conflict",
        indexed_source_missing: "Source missing",
    };

    function renderDocuments(documents) {
        const container = document.getElementById(
            DOCUMENTS_CONTAINER_ID
        );

        if (!container) {
            return;
        }

        if (!Array.isArray(documents) || documents.length === 0) {
            container.innerHTML = (
                '<div class="le-global-chatbot-admin__empty">'
                + "No indexed document is currently available."
                + "</div>"
            );
            return;
        }

        const rows = documents.map(documentRowHtml).join("");

        container.innerHTML = (
            '<div class="le-global-chatbot-admin__table-container">'
            + '<table class="widefat striped le-global-chatbot-admin__table">'
            + "<thead><tr>"
            + "<th scope=\"col\">Country</th>"
            + "<th scope=\"col\">Source file</th>"
            + "<th scope=\"col\">Year</th>"
            + "<th scope=\"col\">Chunks</th>"
            + "<th scope=\"col\">Status</th>"
            + "<th scope=\"col\">Actions</th>"
            + "</tr></thead>"
            + `<tbody>${rows}</tbody>`
            + "</table></div>"
        );

        wireReindexForms();
        wireDeleteForms();
    }

    function documentRowHtml(item) {
        const statusValue = item.status || "unknown";
        const statusLabel = (
            STATUS_LABELS[statusValue]
            || (item.source_file_present ? "Indexed" : "Source unavailable")
        );

        return (
            "<tr>"
            + `<td><strong>${escapeHtml(item.country)}</strong> `
            + `<span class="le-global-chatbot-admin__country-code">${escapeHtml(item.country_code)}</span></td>`
            + `<td>${escapeHtml(item.source_filename)}</td>`
            + `<td>${escapeHtml(item.reference_year || "—")}</td>`
            + `<td>${escapeHtml(item.chunk_count || 0)}</td>`
            + `<td>${escapeHtml(statusLabel)}</td>`
            + `<td>${rowActionsHtml(item)}</td>`
            + "</tr>"
        );
    }

    function rowActionsHtml(item) {
        const parts = [];

        if (item.download_url) {
            parts.push(
                `<a class="button" href="${escapeHtml(item.download_url)}">Download</a>`
            );
        }

        if (adminFormConfig && item.reindex_nonce) {
            parts.push(actionFormHtml({
                action: adminFormConfig.reindexAction,
                nonce: item.reindex_nonce,
                documentId: item.document_id,
                buttonClass: "button",
                buttonLabel: "Reindex",
                markerAttribute: "data-reindex-form",
            }));
        }

        if (adminFormConfig && item.delete_nonce) {
            parts.push(actionFormHtml({
                action: adminFormConfig.deleteAction,
                nonce: item.delete_nonce,
                documentId: item.document_id,
                buttonClass: "button button-link-delete",
                buttonLabel: "Delete",
                markerAttribute: "data-confirm-delete",
                extraAttributes: `data-document-name="${escapeHtml(item.source_filename)}"`,
            }));
        }

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
        extraAttributes = "",
    }) {
        return (
            `<form method="post" action="${escapeHtml(adminFormConfig.adminPostUrl || "")}" ${markerAttribute} ${extraAttributes}>`
            + `<input type="hidden" name="action" value="${escapeHtml(action)}">`
            + `<input type="hidden" name="document_id" value="${escapeHtml(documentId)}">`
            + `<input type="hidden" name="_wpnonce" value="${escapeHtml(nonce)}">`
            + `<button type="submit" class="${buttonClass}">${escapeHtml(buttonLabel)}</button>`
            + "</form>"
        );
    }

    // --- Edit a section ---------------------------------------------
    //
    // Mission "ORDER 5D": country dropdown lists only real indexed
    // documents (rendered server-side from the same catalog the
    // documents table uses, never the static 34-country allowlist -
    // this module never re-derives that list itself). Section
    // dropdown loads only once a country is chosen, and only ever
    // lists sections that really exist (never a "Create section"
    // option). Every async step carries a generation token, bumped
    // on every country change, section change, and Cancel - a
    // response that arrives after a newer one has already
    // superseded it is discarded outright, never applied on top of
    // whatever the admin is looking at now.

    let editSectionGeneration = 0;

    function wireEditSection() {
        const container = document.getElementById(
            "le-global-chatbot-edit"
        );

        if (!container) {
            return;
        }

        const countrySelect = document.getElementById(
            "le-global-edit-country"
        );
        const sectionSelect = document.getElementById(
            "le-global-edit-section"
        );
        const textarea = document.getElementById(
            "le-global-edit-content"
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

        if (
            !countrySelect
            || !sectionSelect
            || !textarea
            || !messageEl
            || !cancelButton
            || !saveButton
        ) {
            return;
        }

        const config = container.dataset;
        let saving = false;

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
            setSectionOptions("Select a country first…", []);
            sectionSelect.disabled = true;
            textarea.value = "";
            textarea.disabled = true;
            setMessage("", null);
            cancelButton.disabled = true;
            saveButton.disabled = true;
        }

        async function onCountryChange() {
            const documentId = countrySelect.value;

            editSectionGeneration += 1;
            const generation = editSectionGeneration;

            setSectionOptions("Loading sections…", []);
            sectionSelect.disabled = true;
            textarea.value = "";
            textarea.disabled = true;
            setMessage("", null);
            cancelButton.disabled = documentId === "";
            saveButton.disabled = true;

            if (documentId === "") {
                setSectionOptions("Select a country first…", []);
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
                setMessage(
                    errorMessage(result ? result.payload : null),
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

            setSectionOptions("Select a section…", sections);
            sectionSelect.disabled = sections.length === 0;

            if (sections.length === 0) {
                setMessage(
                    "This country has no editable section yet.",
                    null
                );
            }
        }

        async function onSectionChange() {
            const documentId = countrySelect.value;
            const sectionId = sectionSelect.value;

            editSectionGeneration += 1;
            const generation = editSectionGeneration;

            textarea.value = "";
            textarea.disabled = true;
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
                    errorMessage(result ? result.payload : null),
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
            saveButton.disabled = false;
        }

        async function onSave() {
            if (saving || saveButton.disabled) {
                return;
            }

            const documentId = countrySelect.value;
            const sectionId = sectionSelect.value;

            if (documentId === "" || sectionId === "") {
                return;
            }

            saving = true;
            saveButton.disabled = true;
            countrySelect.disabled = true;
            sectionSelect.disabled = true;
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
                saveButton.disabled = false;
                countrySelect.disabled = false;
                sectionSelect.disabled = false;
                setMessage(
                    errorMessage(result ? result.payload : null),
                    "is-error"
                );
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

            if (generation !== editSectionGeneration) {
                return;
            }

            countrySelect.disabled = false;
            sectionSelect.disabled = false;
            saveButton.disabled = false;

            if (
                isSuccessful(refetch)
                && refetch.payload.data
                && typeof refetch.payload.data.content === "string"
            ) {
                textarea.value = refetch.payload.data.content;
                setMessage("Section saved successfully.", "is-success");
            } else {
                setMessage(
                    "Saved, but the updated content could not be "
                    + "re-loaded for confirmation.",
                    "is-error"
                );
            }
        }

        function onCancel() {
            resetToEmpty();
        }

        countrySelect.addEventListener("change", onCountryChange);
        sectionSelect.addEventListener("change", onSectionChange);
        saveButton.addEventListener("click", onSave);
        cancelButton.addEventListener("click", onCancel);
    }

    wireEditSection();

    // --- upload form: multi-file queue ------------------------------

    const uploadForm = document.querySelector(
        ".le-global-chatbot-admin__upload-form"
    );

    wireDeleteForms();
    wireReindexForms();

    if (!uploadForm) {
        return;
    }

    const submitButton = uploadForm.querySelector(
        'button[type="submit"]'
    );

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

    function renderQueue() {
        const container = document.getElementById(QUEUE_CONTAINER_ID);

        if (!container) {
            return;
        }

        if (queue.length === 0) {
            container.innerHTML = "";
            return;
        }

        const counts = queue.reduce((accumulator, item) => {
            accumulator[item.status] = (accumulator[item.status] || 0) + 1;
            return accumulator;
        }, {});

        const summary = (
            `Indexed: ${counts.indexed || 0} · `
            + `Already current: ${counts.already_current || 0} · `
            + `Awaiting decision: ${
                (counts.awaiting_replacement_confirmation || 0)
                + (counts.awaiting_warning_confirmation || 0)
                + (counts.awaiting_combined_confirmation || 0)
            } · `
            + `Cancelled: ${counts.cancelled || 0} · `
            + `Failed: ${counts.failed || 0}`
        );

        container.innerHTML = (
            `<p class="le-global-chatbot-admin__queue-summary">${escapeHtml(summary)}</p>`
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

    const STATUS_TEXT = {
        queued: "Queued",
        uploading: "Uploading…",
        indexed: "Indexed",
        already_current: "Already current",
        cancelled: "Cancelled",
        failed: "Failed",
    };

    function queueItemHtml(item) {
        const filename = escapeHtml(item.file.name);
        const statusText = STATUS_TEXT[item.status] || item.status;

        let extra = "";

        if (item.status === "failed") {
            extra = `<span class="le-global-chatbot-admin__queue-message">${escapeHtml(item.message)}</span>`;
        } else if (item.status === "awaiting_replacement_confirmation") {
            const country = (
                (item.detail && item.detail.country) || "this country"
            );

            extra = (
                `<span class="le-global-chatbot-admin__queue-message">A document already exists for ${escapeHtml(country)}.</span>`
                + decisionButtonsHtml(item.id, "Cancel", "cancel", "Replace", "replace")
            );
        } else if (item.status === "awaiting_warning_confirmation") {
            extra = (
                `<span class="le-global-chatbot-admin__queue-message">${escapeHtml(item.message)}</span>`
                + decisionButtonsHtml(item.id, "Cancel", "cancel", "Continue", "continue")
            );
        } else if (item.status === "awaiting_combined_confirmation") {
            const country = (
                (item.detail && item.detail.country_name) || "this country"
            );

            extra = (
                `<span class="le-global-chatbot-admin__queue-message">${escapeHtml(item.message)} A document already exists for ${escapeHtml(country)}.</span>`
                + decisionButtonsHtml(item.id, "Cancel", "cancel", "Continue and replace", "continue-and-replace")
            );
        }

        return (
            '<li class="le-global-chatbot-admin__queue-item" '
            + `data-status="${escapeHtml(item.status)}">`
            + `<span class="le-global-chatbot-admin__queue-filename">${filename}</span>`
            + `<span class="le-global-chatbot-admin__queue-status">${escapeHtml(statusText)}</span>`
            + extra
            + "</li>"
        );
    }

    function decisionButtonsHtml(itemId, cancelLabel, cancelValue, continueLabel, continueValue) {
        return (
            `<button type="button" class="button" data-decision="${cancelValue}" data-item-id="${itemId}">${escapeHtml(cancelLabel)}</button>`
            + `<button type="button" class="button button-primary" data-decision="${continueValue}" data-item-id="${itemId}">${escapeHtml(continueLabel)}</button>`
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

        item.status = "indexed";
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

    uploadForm.addEventListener(
        "submit",
        async (event) => {
            event.preventDefault();

            if (submitButton && submitButton.disabled) {
                return;
            }

            const files = getSelectedFiles(fileInput);

            if (files.length === 0) {
                return;
            }

            if (submitButton) {
                submitButton.disabled = true;
            }

            try {
                const items = enqueueFiles(files);
                await Promise.all(items.map(settledPromise));

                // One refresh for the whole batch, never one per
                // file (mission "ORDER 5D", section 4: the 33-file
                // corpus campaign must not amplify into 33 separate
                // list/stats refreshes) - a file still awaiting a
                // replacement/warning decision at this point is
                // refreshed later, on its own, once the admin
                // actually resolves it (see resolveDecision above).
                await refreshAdminState();
            } finally {
                if (submitButton) {
                    submitButton.disabled = false;
                }
            }
        }
    );

    // Test-only hook: absent in the browser (module is never defined
    // there), so this changes nothing about how the admin page itself
    // loads or runs - see assets/chatbot.js for the identical pattern.
    if (typeof module !== "undefined" && module.exports) {
        module.exports = {
            errorMessage,
            extractStructuredDetail,
            isReplacementRequiredResponse,
            classifyUploadResponse,
            MAX_CONCURRENT_UPLOADS,
            getSelectedFiles,
            __queueForTests: {
                enqueueFiles,
                resolveDecision,
                getQueueSnapshot: () => queue.map((item) => ({ ...item })),
                activeUploadCount: () => activeUploadCount,
            },
        };
    }
})();
