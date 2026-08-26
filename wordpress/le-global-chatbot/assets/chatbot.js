(() => {
    "use strict";

    // Default cap on total conversation messages (user + assistant
    // turns combined) kept in memory, persisted to sessionStorage, and
    // sent to the backend as history - overridden by the backend's own
    // frontend-config limit when available (see loadConfiguration()).
    const DEFAULT_MAX_HISTORY_MESSAGES = 20;

    const DISCLAIMER_TEXT = (
        "This information is based on the available L&E "
        + "Global documents and does not constitute legal advice."
    );

    const CITATION_GROUP_PATTERN = (
        /\[(\d+(?:\s*,\s*\d+)*)\]/g
    );

    const HISTORY_QUESTION_MAX_CHARACTERS = 2000;
    const HISTORY_ANSWER_MAX_CHARACTERS = 3000;

    // Scaled proportionally with DEFAULT_MAX_HISTORY_MESSAGES
    // (previously 10000 for 6 messages), matching the backend's own
    // HISTORY_TOTAL_MAX_CHARACTERS exactly - the per-message limits
    // above are unchanged.
    const HISTORY_TOTAL_MAX_CHARACTERS = 33333;

    // sessionStorage only - never localStorage/cookies/IndexedDB - so
    // the conversation never survives beyond the browser session. The
    // version segment lets a future format change discard older data
    // safely instead of misreading it.
    const CONVERSATION_STORAGE_KEY = (
        "le_global_chatbot_conversation_v1"
    );

    // Version 2 adds conversationState (structured routing context
    // for the next turn) and a per-message hasDisclaimer flag
    // alongside the existing messages array - a version 1 payload
    // (messages only) is detected by normalizeStoredConversation()
    // and discarded rather than misread.
    const CONVERSATION_STORAGE_VERSION = 2;

    // GATE S7-LITE: the only /chat/stream NDJSON protocol version this
    // client understands - matches the backend's own
    // NDJSON_PROTOCOL_VERSION (chat_stream.py). A start record naming
    // any other version is rejected as a protocol error rather than
    // parsed optimistically.
    const STREAM_PROTOCOL_VERSION = 1;

    /**
     * Build the history payload sent alongside a new question, from
     * this widget's own turns array - a pure function of its input so
     * it can be exercised directly, without any DOM.
     *
     * Only "success" turns are eligible (a pending or error turn is
     * never included). Turns are walked from the most recent to the
     * oldest, each contributing one complete user+assistant pair -
     * never just one side of it - clipped to
     * HISTORY_QUESTION_MAX_CHARACTERS / HISTORY_ANSWER_MAX_CHARACTERS
     * first. A pair is kept only while the running total of the kept
     * pairs' characters stays within HISTORY_TOTAL_MAX_CHARACTERS;
     * the walk stops at the first older pair that would not fit,
     * rather than skipping it and considering even older ones. The
     * result is returned in chronological order.
     */
    function buildBoundedHistoryPayload(
        turns,
        maxHistoryMessages
    ) {
        const successTurns = turns.filter(
            (turn) => turn.status === "success"
        );

        const selectedPairs = [];
        let totalCharacters = 0;

        for (
            let index = successTurns.length - 1;
            index >= 0;
            index -= 1
        ) {
            const turn = successTurns[index];

            const questionContent = turn.question.slice(
                0,
                HISTORY_QUESTION_MAX_CHARACTERS
            );

            const answerContent = (turn.answer || "").slice(
                0,
                HISTORY_ANSWER_MAX_CHARACTERS
            );

            const pairCharacters = (
                questionContent.length
                + answerContent.length
            );

            if (
                totalCharacters + pairCharacters
                > HISTORY_TOTAL_MAX_CHARACTERS
            ) {
                break;
            }

            totalCharacters += pairCharacters;

            selectedPairs.unshift(
                {
                    question: questionContent,
                    answer: answerContent,
                }
            );

            if (
                selectedPairs.length * 2
                >= maxHistoryMessages
            ) {
                break;
            }
        }

        const history = [];

        selectedPairs.forEach((pair) => {
            history.push(
                {
                    role: "user",
                    content: pair.question,
                }
            );

            history.push(
                {
                    role: "assistant",
                    content: pair.answer,
                }
            );
        });

        return history;
    }

    /**
     * Validate one parsed sessionStorage payload and return a flat,
     * cleaned message list - never the raw value itself.
     *
     * Returns null - never throws, never returns a partially-trusted
     * object - whenever the top-level shape is wrong (not a plain
     * object, wrong version, "messages" not an array). Within an
     * otherwise valid payload, individual malformed entries (wrong
     * role, non-string or empty content) are dropped rather than
     * invalidating the whole conversation - trimConversationToPairs()
     * is what reduces the remaining entries to a safely alternating,
     * complete-pairs-only list.
     */
    function normalizePublicContacts(rawContacts) {
        if (!Array.isArray(rawContacts)) {
            return [];
        }

        return rawContacts
            .filter((item) => (
                item
                && typeof item === "object"
                && typeof item.contact_id === "string"
                && item.contact_id.trim() !== ""
            ))
            .map((item) => {
                const clean = (name) => (
                    typeof item[name] === "string"
                        && item[name].trim() !== ""
                        ? item[name].trim()
                        : null
                );

                return {
                    contact_id: item.contact_id.trim(),
                    country_code: clean("country_code"),
                    member_firm: clean("member_firm"),
                    contact_person: clean("contact_person"),
                    email: clean("email"),
                    phone: clean("phone"),
                    address: clean("address"),
                    website: clean("website"),
                    photo_url: clean("photo_url"),
                };
            });
    }

    function normalizeStoredConversationState(rawConversationState) {
        if (
            !rawConversationState
            || typeof rawConversationState !== "object"
            || Array.isArray(rawConversationState)
        ) {
            return null;
        }

        return rawConversationState;
    }

    function normalizeStoredConversation(rawValue) {
        if (
            !rawValue
            || typeof rawValue !== "object"
            || Array.isArray(rawValue)
        ) {
            return null;
        }

        if (
            rawValue.version
            !== CONVERSATION_STORAGE_VERSION
        ) {
            return null;
        }

        if (!Array.isArray(rawValue.messages)) {
            return null;
        }

        const normalizedMessages = [];

        rawValue.messages.forEach((entry) => {
            if (!entry || typeof entry !== "object") {
                return;
            }

            const role = entry.role;

            if (
                role !== "user"
                && role !== "assistant"
            ) {
                return;
            }

            if (typeof entry.content !== "string") {
                return;
            }

            const content = entry.content.trim();

            if (!content) {
                return;
            }

            const message = {
                role,
                content,
            };

            if (role === "assistant") {
                if (Array.isArray(entry.sources)) {
                    message.sources = entry.sources.filter(
                        (source) => (
                            source
                            && typeof source === "object"
                        )
                    );
                }

                message.contacts = normalizePublicContacts(
                    entry.contacts
                );

                message.hasDisclaimer = Boolean(
                    entry.hasDisclaimer
                );
            }

            normalizedMessages.push(message);
        });

        return {
            messages: normalizedMessages,
            conversationState: normalizeStoredConversationState(
                rawValue.conversationState
            ),
        };
    }

    /**
     * Reduce a flat message list to complete, alternating
     * user-then-assistant pairs only, most recent pairs kept first.
     *
     * Walks from the start expecting strict user/assistant
     * alternation - stops at the first break (a wrong role, or a
     * trailing question with no answer yet) rather than trying to
     * resynchronize, since a broken pairing this deep is never
     * trustworthy. The result is capped to at most
     * floor(maxHistoryMessages / 2) pairs, dropping the oldest ones
     * first.
     */
    function trimConversationToCompletePairs(
        messages,
        maxHistoryMessages
    ) {
        const pairs = [];
        let index = 0;

        while (index + 1 < messages.length) {
            const userMessage = messages[index];
            const assistantMessage = messages[index + 1];

            if (
                userMessage.role !== "user"
                || assistantMessage.role !== "assistant"
            ) {
                break;
            }

            pairs.push(
                [
                    userMessage,
                    assistantMessage,
                ]
            );

            index += 2;
        }

        const maxPairs = Math.max(
            0,
            Math.floor(
                maxHistoryMessages / 2
            )
        );

        return pairs.slice(-maxPairs).flat();
    }

    /**
     * Read and validate the persisted conversation, if any.
     *
     * Any corruption at all - invalid JSON, wrong version, a
     * non-object payload, or a message list that normalizes/trims
     * down to nothing - deletes the stored key and returns an empty
     * list, so the widget always starts normally rather than ever
     * failing to open over bad storage content.
     */
    function loadStoredConversation(maxHistoryMessages) {
        const empty = {
            messages: [],
            conversationState: null,
        };

        let rawText;

        try {
            rawText = window.sessionStorage.getItem(
                CONVERSATION_STORAGE_KEY
            );
        } catch {
            return empty;
        }

        if (!rawText) {
            return empty;
        }

        let parsedValue;

        try {
            parsedValue = JSON.parse(rawText);
        } catch {
            clearStoredConversation();
            return empty;
        }

        const normalized = (
            normalizeStoredConversation(parsedValue)
        );

        if (normalized === null) {
            clearStoredConversation();
            return empty;
        }

        const trimmedMessages = (
            trimConversationToCompletePairs(
                normalized.messages,
                maxHistoryMessages
            )
        );

        if (trimmedMessages.length === 0) {
            clearStoredConversation();
            return empty;
        }

        return {
            messages: trimmedMessages,
            conversationState: normalized.conversationState,
        };
    }

    /**
     * Persist only the complete, successful turns - never a pending
     * question awaiting its answer, never an error turn - capped to
     * the same complete-pairs limit used everywhere else. Silently
     * gives up on any storage failure (quota exceeded, storage
     * disabled in private browsing): persistence is a convenience,
     * never a requirement for the widget to keep working.
     */
    function saveConversation(
        turns,
        maxHistoryMessages,
        conversationState
    ) {
        const messages = [];

        turns
            .filter(
                (turn) => turn.status === "success"
            )
            .forEach((turn) => {
                messages.push(
                    {
                        role: "user",
                        content: turn.question,
                    }
                );

                messages.push(
                    {
                        role: "assistant",
                        content: turn.answer || "",
                        sources: Array.isArray(turn.sources)
                            ? turn.sources
                            : [],
                        contacts: normalizePublicContacts(
                            turn.contacts
                        ),
                        hasDisclaimer: Boolean(
                            turn.hasDisclaimer
                        ),
                    }
                );
            });

        const trimmedMessages = (
            trimConversationToCompletePairs(
                messages,
                maxHistoryMessages
            )
        );

        if (trimmedMessages.length === 0) {
            clearStoredConversation();
            return;
        }

        try {
            window.sessionStorage.setItem(
                CONVERSATION_STORAGE_KEY,
                JSON.stringify(
                    {
                        version: CONVERSATION_STORAGE_VERSION,
                        messages: trimmedMessages,
                        conversationState: (
                            conversationState || null
                        ),
                    }
                )
            );
        } catch {
            // Storage may be full or unavailable - never break the
            // widget over a persistence failure.
        }
    }

    /** Delete the persisted conversation, if any. */
    function clearStoredConversation() {
        try {
            window.sessionStorage.removeItem(
                CONVERSATION_STORAGE_KEY
            );
        } catch {
            // sessionStorage may be unavailable (private browsing,
            // disabled storage) - nothing to clean up in that case.
        }
    }

    /**
     * Rebuild in-memory turns (the widget's own rendering/history
     * model) from a validated, complete-pairs-only message list -
     * every restored turn is already "success", since only successful
     * turns are ever persisted.
     */
    function rebuildTurnsFromMessages(
        messages,
        nextTurnId
    ) {
        const turns = [];
        let currentId = nextTurnId;

        for (
            let index = 0;
            index + 1 < messages.length;
            index += 2
        ) {
            const userMessage = messages[index];
            const assistantMessage = messages[index + 1];

            turns.push(
                {
                    id: currentId,
                    question: userMessage.content,
                    status: "success",
                    answer: assistantMessage.content,
                    sources: Array.isArray(
                        assistantMessage.sources
                    )
                        ? assistantMessage.sources
                        : [],
                    contacts: normalizePublicContacts(
                        assistantMessage.contacts
                    ),
                    hasDisclaimer: Boolean(
                        assistantMessage.hasDisclaimer
                    ),
                    errorMessage: null,
                }
            );

            currentId += 1;
        }

        return {
            turns,
            nextTurnId: currentId,
        };
    }

    const widgets = document.querySelectorAll(
        ".le-global-chatbot"
    );

    widgets.forEach((widget) => {
        initializeWidget(widget);
    });

    function initializeWidget(widget) {
        const configEndpoint = widget.dataset.configEndpoint;
        const chatEndpoint = widget.dataset.chatEndpoint;
        const contactPhotoEndpoint = (
            widget.dataset.contactPhotoEndpoint || ""
        );

        // GATE S7-LITE: same server-generated data-attribute mechanism
        // as chatEndpoint/configEndpoint above - never hard-coded, and
        // still always same-origin WordPress, never the backend
        // directly. chatStreamingEnabled defaults OFF whenever the
        // attribute is absent or not exactly "1" (an older cached
        // shortcode render, or the PHP-side constant left undefined).
        const chatStreamEndpoint = (
            widget.dataset.chatStreamEndpoint || ""
        );

        const chatStreamingEnabled = (
            widget.dataset.chatStreamingEnabled === "1"
        );

        // Fixed for this widget's entire lifetime (browser capability
        // and the server-provided flag/endpoint never change at
        // runtime) - captured once here, then closure-captured by
        // every submitChatRequest() call including its own
        // conversation_state retry, so a request that ever dispatches
        // through /chat/stream can structurally never fall through to
        // /chat instead (mission section 5's no-double-request rule).
        const chatTransport = resolveChatTransport(
            {
                chatStreamingEnabled,
                chatStreamEndpoint,
            }
        );

        const form = widget.querySelector(
            ".le-global-chatbot__composer"
        );

        const questionInput = widget.querySelector(
            ".le-global-chatbot__question"
        );

        const characterCount = widget.querySelector(
            "[data-character-count]"
        );

        const conversationElement = widget.querySelector(
            "[data-conversation]"
        );

        const welcomeMessageElement = widget.querySelector(
            "[data-welcome-message]"
        );

        const messageListElement = widget.querySelector(
            "[data-message-list]"
        );

        const newConversationButton = widget.querySelector(
            "[data-new-conversation]"
        );

        const submitButton = widget.querySelector(
            ".le-global-chatbot__submit"
        );

        const statusElement = widget.querySelector(
            "[data-status]"
        );

        const errorElement = widget.querySelector(
            "[data-error]"
        );

        if (
            !form
            || !questionInput
            || !characterCount
            || !conversationElement
            || !welcomeMessageElement
            || !messageListElement
            || !newConversationButton
            || !submitButton
            || !statusElement
            || !errorElement
        ) {
            return;
        }

        let maximumQuestionLength = 2000;
        let defaultMaximumSources = 6;
        let maxHistoryMessages = DEFAULT_MAX_HISTORY_MESSAGES;
        let requestInFlight = false;
        let configurationInFlight = false;

        // Identifies which submit() call a still-pending /chat
        // request belongs to, and which AbortController it used.
        // startNewConversation() bumps the generation and clears the
        // controller immediately - any mutation, error handling, or
        // finally cleanup from an older request compares its own
        // captured values against these before touching anything, so
        // a request abandoned by "New conversation" can never affect
        // the conversation the user is now looking at, whether or not
        // the browser actually manages to cancel it in time.
        let activeChatController = null;
        let conversationGeneration = 0;

        /**
         * Conversation turns, oldest first. At most
         * floor(maxHistoryMessages / 2) are ever kept - adding one
         * more removes the oldest one, whole, before it is rendered.
         * A turn is one of:
         * { id, question, status: "pending", answer: null,
         *   sources: [], errorMessage: null }
         * { id, question, status: "success", answer, sources }
         * { id, question, status: "error", answer: null,
         *   sources: [], errorMessage }
         * Once a turn reaches "success" or "error" it is never
         * mutated again - a later render always reproduces the same
         * markup for it.
         */
        let turns = [];
        let nextTurnId = 1;

        // Structured routing state returned with the most recent
        // successful turn - sent back with the next question so the
        // backend can resolve a follow-up ("Peru?", "the contact")
        // without re-deriving it from raw history text alone. Never
        // a legal source, never rendered - purely routing metadata
        // the backend itself produced and validated.
        let conversationState = null;

        // Restore any conversation persisted earlier in this same
        // browser session (survives a reload, never a full browser
        // session close, since sessionStorage is used - never
        // localStorage). Corrupted or unreadable storage silently
        // yields an empty list, so the widget always opens normally.
        const restored = loadStoredConversation(
            maxHistoryMessages
        );

        if (restored.messages.length > 0) {
            const rebuilt = rebuildTurnsFromMessages(
                restored.messages,
                nextTurnId
            );

            turns = rebuilt.turns;
            nextTurnId = rebuilt.nextTurnId;
            conversationState = restored.conversationState;

            renderMessageList();
            scrollConversationToBottom();
        }

        questionInput.addEventListener(
            "input",
            () => {
                characterCount.textContent = String(
                    questionInput.value.length
                );
            }
        );

        questionInput.addEventListener(
            "keydown",
            (event) => {
                if (
                    event.key !== "Enter"
                    || event.shiftKey
                    || event.isComposing
                    || event.keyCode === 229
                ) {
                    return;
                }

                event.preventDefault();

                if (requestInFlight || configurationInFlight) {
                    return;
                }

                form.requestSubmit();
            }
        );

        newConversationButton.addEventListener(
            "click",
            startNewConversation
        );

        form.addEventListener(
            "submit",
            async (event) => {
                event.preventDefault();

                if (requestInFlight || configurationInFlight) {
                    return;
                }

                const question = questionInput.value.trim();

                if (question.length < 2) {
                    showError(
                        "Please enter a legal question."
                    );

                    questionInput.focus();
                    return;
                }

                clearError();

                const historyPayload = buildBoundedHistoryPayload(
                    turns,
                    maxHistoryMessages
                );

                const turn = {
                    id: nextTurnId,
                    question,
                    status: "pending",
                    answer: null,
                    sources: [],
                    contacts: [],
                    errorMessage: null,
                };

                nextTurnId += 1;

                turns.push(
                    turn
                );

                const maxConversationPairs = Math.max(
                    1,
                    Math.floor(
                        maxHistoryMessages / 2
                    )
                );

                if (turns.length > maxConversationPairs) {
                    turns.shift();
                }

                questionInput.value = "";
                characterCount.textContent = "0";
                questionInput.focus();

                renderMessageList();
                scrollConversationToBottom();

                const requestGeneration = conversationGeneration;
                const controller = new AbortController();
                activeChatController = controller;

                function isStaleRequest() {
                    return (
                        requestGeneration !== conversationGeneration
                        || activeChatController !== controller
                    );
                }

                requestInFlight = true;
                conversationElement.setAttribute(
                    "aria-busy",
                    "true"
                );

                refreshLoadingState();

                function sendChatRequest(
                    includeConversationState
                ) {
                    const requestBody = {
                        question,
                        history: historyPayload,
                        language: "en",
                        max_sources: defaultMaximumSources,
                    };

                    if (
                        includeConversationState
                        && conversationState
                    ) {
                        requestBody.conversation_state = (
                            conversationState
                        );
                    }

                    const requestInit = {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                        },
                        credentials: "same-origin",
                        signal: controller.signal,
                        body: JSON.stringify(
                            requestBody
                        ),
                    };

                    // chatTransport is fixed for this whole submit -
                    // see its own definition above for why a retry
                    // below can never cross from stream to json.
                    return chatTransport === "stream"
                        ? requestStream(
                            chatStreamEndpoint,
                            requestInit
                        )
                        : requestJson(
                            chatEndpoint,
                            requestInit
                        );
                }

                function submitChatRequest(
                    includeConversationState
                ) {
                    return performChatTransportRequest(
                        {
                            send: sendChatRequest,
                            includeConversationState,
                            onConversationStateRejected: () => {
                                // Recover from a conversation_state
                                // the backend rejected (RECTIFICATIF
                                // §D): drop only conversationState,
                                // keep every persisted message - the
                                // retry itself (exactly once, same
                                // transport, never a second copy of
                                // the user's question) is
                                // performChatTransportRequest's own
                                // job.
                                conversationState = null;

                                saveConversation(
                                    turns.filter(
                                        (existingTurn) => (
                                            existingTurn.id
                                            !== turn.id
                                        )
                                    ),
                                    maxHistoryMessages,
                                    null
                                );
                            },
                        }
                    );
                }

                try {
                    const response = await submitChatRequest(
                        true
                    );

                    if (isStaleRequest()) {
                        return;
                    }

                    const rawSources = Array.isArray(
                        response.sources
                    )
                        ? response.sources
                        : [];

                    const renumbered = applyCitationRenumbering(
                        response.answer || "",
                        rawSources
                    );

                    conversationState = (
                        response.conversation_state || null
                    );

                    turn.status = "success";
                    turn.answer = renumbered.answer;
                    turn.sources = renumbered.sources;
                    turn.contacts = normalizePublicContacts(response.contacts);
                    // Routing state may survive a clarification.
                    // Grounding is the authoritative disclaimer signal.
                    turn.hasDisclaimer = Boolean(
                        response.grounded
                    );
                } catch (error) {
                    if (
                        error
                        && error.name === "AbortError"
                    ) {
                        return;
                    }

                    if (isStaleRequest()) {
                        return;
                    }

                    turn.status = "error";
                    turn.errorMessage = (
                        error instanceof Error
                            ? error.message
                            : (
                                "The legal assistant is "
                                + "unavailable."
                            )
                    );
                } finally {
                    if (!isStaleRequest()) {
                        requestInFlight = false;
                        activeChatController = null;

                        conversationElement.setAttribute(
                            "aria-busy",
                            "false"
                        );

                        refreshLoadingState();

                        renderMessageList();
                        scrollConversationToBottom();

                        // Persists only the turns that reached
                        // "success" - an error turn, or the pending
                        // turn of a request that is still in flight,
                        // is never written to storage.
                        saveConversation(
                            turns,
                            maxHistoryMessages,
                            conversationState
                        );
                    }
                }
            }
        );

        const mode = (
            widget.dataset.mode === "floating"
                ? "floating"
                : "inline"
        );

        let configurationLoaded = false;
        let configurationPromise = null;

        function ensureConfigurationLoaded() {
            if (configurationLoaded) {
                return Promise.resolve(true);
            }

            if (configurationPromise) {
                return configurationPromise;
            }

            configurationPromise = loadConfiguration()
                .then((succeeded) => {
                    configurationLoaded = succeeded;
                    return succeeded;
                })
                .finally(() => {
                    configurationPromise = null;
                });

            return configurationPromise;
        }

        if (mode === "floating") {
            initializeFloatingMode();
        } else {
            void ensureConfigurationLoaded();
        }

        function initializeFloatingMode() {
            const floatingWrapper = widget.closest(
                "[data-floating-wrapper]"
            );

            const launcher = (
                floatingWrapper
                    ? floatingWrapper.querySelector(
                        "[data-launcher]"
                    )
                    : null
            );

            const closeButton = widget.querySelector(
                "[data-close]"
            );

            if (!floatingWrapper || !launcher || !closeButton) {
                void ensureConfigurationLoaded();
                return;
            }

            launcher.addEventListener(
                "click",
                togglePanel
            );

            closeButton.addEventListener(
                "click",
                closePanel
            );

            document.addEventListener(
                "keydown",
                (event) => {
                    if (
                        event.key === "Escape"
                        && !widget.hidden
                    ) {
                        closePanel();
                    }
                }
            );

            function togglePanel() {
                if (widget.hidden) {
                    openPanel();
                } else {
                    closePanel();
                }
            }

            function openPanel() {
                void ensureConfigurationLoaded();

                widget.hidden = false;
                floatingWrapper.classList.add(
                    "le-global-chatbot-floating--open"
                );

                launcher.setAttribute(
                    "aria-expanded",
                    "true"
                );

                launcher.setAttribute(
                    "aria-label",
                    "Close the employment law assistant"
                );

                questionInput.focus();
            }

            function closePanel() {
                if (widget.hidden) {
                    return;
                }

                widget.hidden = true;
                floatingWrapper.classList.remove(
                    "le-global-chatbot-floating--open"
                );

                launcher.setAttribute(
                    "aria-expanded",
                    "false"
                );

                launcher.setAttribute(
                    "aria-label",
                    "Open the employment law assistant"
                );

                launcher.focus();
            }
        }

        async function loadConfiguration() {
            clearError();

            configurationInFlight = true;
            refreshLoadingState();

            try {
                const configuration = await requestJson(
                    configEndpoint,
                    {
                        method: "GET",
                        credentials: "same-origin",
                    }
                );

                const limits = configuration.limits || {};

                if (
                    Number.isInteger(
                        limits.question_max_length
                    )
                ) {
                    maximumQuestionLength = (
                        limits.question_max_length
                    );

                    questionInput.maxLength = (
                        maximumQuestionLength
                    );
                }

                if (
                    Number.isInteger(
                        limits.max_sources_default
                    )
                ) {
                    defaultMaximumSources = (
                        limits.max_sources_default
                    );
                }

                if (
                    Number.isInteger(
                        limits.max_history_messages
                    )
                    && limits.max_history_messages > 0
                ) {
                    maxHistoryMessages = (
                        limits.max_history_messages
                    );
                }

                return true;
            } catch (error) {
                showError(
                    error instanceof Error
                        ? error.message
                        : "The legal catalog could not be loaded."
                );

                return false;
            } finally {
                configurationInFlight = false;
                refreshLoadingState();
            }
        }

        function startNewConversation() {
            conversationGeneration += 1;

            if (activeChatController) {
                activeChatController.abort();
            }

            activeChatController = null;
            requestInFlight = false;

            conversationElement.setAttribute(
                "aria-busy",
                "false"
            );

            refreshLoadingState();

            clearError();

            turns = [];
            conversationState = null;
            renderMessageList();

            clearStoredConversation();

            questionInput.value = "";
            characterCount.textContent = "0";
            questionInput.focus();
        }

        function renderMessageList() {
            welcomeMessageElement.hidden = (
                turns.length > 0
            );

            const fragment = document.createDocumentFragment();

            turns.forEach((turn) => {
                fragment.appendChild(
                    buildTurnElement(
                        turn
                    )
                );
            });

            messageListElement.replaceChildren(
                fragment
            );
        }

        function buildTurnElement(turn) {
            const turnElement = document.createElement(
                "article"
            );

            turnElement.className = (
                "le-global-chatbot__turn"
            );

            const userBubble = document.createElement(
                "div"
            );

            userBubble.className = (
                "le-global-chatbot__message "
                + "le-global-chatbot__message--user"
            );

            const userText = document.createElement(
                "p"
            );

            userText.textContent = turn.question;
            userBubble.appendChild(
                userText
            );
            turnElement.appendChild(
                userBubble
            );

            if (turn.status === "pending") {
                const pendingBubble = document.createElement(
                    "div"
                );

                pendingBubble.className = (
                    "le-global-chatbot__message "
                    + "le-global-chatbot__message--assistant "
                    + "le-global-chatbot__message--pending"
                );

                pendingBubble.setAttribute(
                    "aria-live",
                    "polite"
                );

                pendingBubble.textContent = (
                    "Searching validated legal documents…"
                );

                turnElement.appendChild(
                    pendingBubble
                );
            } else if (turn.status === "error") {
                const errorBubble = document.createElement(
                    "div"
                );

                errorBubble.className = (
                    "le-global-chatbot__message "
                    + "le-global-chatbot__message--assistant "
                    + "le-global-chatbot__message--error"
                );

                errorBubble.setAttribute(
                    "role",
                    "alert"
                );

                errorBubble.textContent = (
                    turn.errorMessage
                    || "The legal assistant could not "
                    + "process the request."
                );

                turnElement.appendChild(
                    errorBubble
                );
            } else {
                turnElement.appendChild(
                    buildAssistantBubble(
                        turn
                    )
                );
            }

            return turnElement;
        }

        function buildAssistantBubble(turn) {
            const assistantBubble = document.createElement(
                "div"
            );

            assistantBubble.className = (
                "le-global-chatbot__message "
                + "le-global-chatbot__message--assistant"
            );

            const answerElement = document.createElement(
                "div"
            );

            answerElement.className = (
                "le-global-chatbot__answer"
            );

            answerElement.textContent = turn.answer || "";

            const contacts = normalizePublicContacts(
                turn.contacts
            );

            const sources = Array.isArray(turn.sources)
                ? turn.sources
                : [];

            const contactOnly = (
                contacts.length > 0
                && sources.length > 0
                && sources.every((source) => (
                    String(
                        source.subsection
                        || source.section
                        || ""
                    )
                        .trim()
                        .toLowerCase() === "contact"
                ))
            );

            if (contactOnly) {
                assistantBubble.classList.add(
                    "le-global-chatbot__message--contact-only"
                );
            } else {
                assistantBubble.appendChild(
                    answerElement
                );
            }

            if (contacts.length > 0) {
                assistantBubble.appendChild(
                    buildContactCardsSection(contacts)
                );
            }

            if (sources.length > 0 && !contactOnly) {
                assistantBubble.appendChild(
                    buildSourcesSection(
                        sources
                    )
                );
            }

            // Never shown for a clarification, an out-of-scope
            // refusal, or any other turn that resolved no real
            // action - only for a genuine legal/contact/comparison/
            // mixed answer (see hasDisclaimer, set from whether the
            // response carried a conversation_state with at least
            // one resolved action).
            if (turn.hasDisclaimer) {
                const disclaimer = document.createElement(
                    "p"
                );

                disclaimer.className = (
                    "le-global-chatbot__disclaimer"
                );

                disclaimer.textContent = DISCLAIMER_TEXT;
                assistantBubble.appendChild(
                    disclaimer
                );
            }

            return assistantBubble;
        }

        function buildContactCardsSection(contacts) {
            const section = document.createElement("section");
            section.className = "le-global-chatbot__contact-cards";

            contacts.forEach((contact) => {
                const card = document.createElement("article");
                card.className = "le-global-chatbot__contact-card";

                if (contact.photo_url && contactPhotoEndpoint) {
                    const match = contact.photo_url.match(
                        /\/([0-9a-f]{64})$/
                    );

                    if (match) {
                        const image = document.createElement("img");
                        const separator = contactPhotoEndpoint.includes("?")
                            ? "&"
                            : "?";

                        image.className = "le-global-chatbot__contact-photo";
                        card.classList.add(
                            "le-global-chatbot__contact-card--with-photo"
                        );
                        image.alt = contact.contact_person
                            ? contact.contact_person
                            : "Contact";
                        image.loading = "lazy";
                        image.src = (
                            contactPhotoEndpoint
                            + separator
                            + "contact_id="
                            + encodeURIComponent(contact.contact_id)
                            + "&sha256="
                            + encodeURIComponent(match[1])
                        );

                        card.appendChild(image);
                    }
                }

                const body = document.createElement("div");
                body.className = "le-global-chatbot__contact-body";

                const addText = (value, className) => {
                    if (!value) {
                        return;
                    }

                    const line = document.createElement("div");
                    line.className = className;
                    line.textContent = value;
                    body.appendChild(line);
                };

                addText(
                    contact.contact_person,
                    "le-global-chatbot__contact-name"
                );
                addText(
                    contact.member_firm,
                    "le-global-chatbot__contact-firm"
                );

                if (contact.email) {
                    const link = document.createElement("a");
                    link.textContent = contact.email;

                    if (/^[^@\s]+@[^@\s]+$/.test(contact.email)) {
                        link.href = "mailto:" + contact.email;
                    }

                    body.appendChild(link);
                }

                if (contact.phone) {
                    const link = document.createElement("a");
                    link.textContent = contact.phone;
                    link.href = "tel:" + contact.phone.replace(
                        /[^+0-9]/g,
                        ""
                    );
                    body.appendChild(link);
                }

                addText(
                    contact.address,
                    "le-global-chatbot__contact-address"
                );

                if (contact.website) {
                    try {
                        const url = new URL(contact.website);

                        if (
                            url.protocol === "http:"
                            || url.protocol === "https:"
                        ) {
                            const link = document.createElement("a");
                            link.textContent = contact.website;
                            link.href = url.href;
                            link.target = "_blank";
                            link.rel = "noopener noreferrer";
                            body.appendChild(link);
                        }
                    } catch {
                        addText(
                            contact.website,
                            "le-global-chatbot__contact-website"
                        );
                    }
                }

                card.appendChild(body);
                section.appendChild(card);
            });

            return section;
        }

        function buildSourcesSection(sources) {
            const sourcesSection = document.createElement(
                "section"
            );

            sourcesSection.className = (
                "le-global-chatbot__sources-section"
            );

            const sourcesTitle = document.createElement(
                "h4"
            );

            sourcesTitle.className = (
                "le-global-chatbot__sources-title"
            );

            sourcesTitle.textContent = "Sources";
            sourcesSection.appendChild(
                sourcesTitle
            );

            const sourcesList = document.createElement(
                "ol"
            );

            sourcesList.className = (
                "le-global-chatbot__sources"
            );

            sources.forEach((source) => {
                const item = document.createElement(
                    "li"
                );

                item.className = (
                    "le-global-chatbot__source"
                );

                const title = document.createElement(
                    "strong"
                );

                title.textContent = [
                    `[${source.citation}]`,
                    source.country,
                    source.section,
                ]
                    .filter(Boolean)
                    .join(" — ");

                const metadata = document.createElement(
                    "span"
                );

                metadata.className = (
                    "le-global-chatbot__source-metadata"
                );

                metadata.textContent = [
                    source.subsection,
                    source.source_filename,
                    source.reference_year,
                ]
                    .filter(Boolean)
                    .join(" · ");

                item.appendChild(
                    title
                );

                if (metadata.textContent) {
                    item.appendChild(
                        metadata
                    );
                }

                sourcesList.appendChild(
                    item
                );
            });

            sourcesSection.appendChild(
                sourcesList
            );

            return sourcesSection;
        }

        function scrollConversationToBottom() {
            conversationElement.scrollTo({
                top: conversationElement.scrollHeight,
                behavior: "smooth",
            });
        }

        function showError(message) {
            errorElement.textContent = message;
            errorElement.hidden = false;
        }

        function clearError() {
            errorElement.textContent = "";
            errorElement.hidden = true;
        }

        function refreshLoadingState() {
            const isBusy = (
                requestInFlight
                || configurationInFlight
            );

            submitButton.disabled = isBusy;

            if (requestInFlight) {
                statusElement.textContent = (
                    "Searching validated legal documents…"
                );
            } else if (configurationInFlight) {
                statusElement.textContent = (
                    "Loading the legal catalog…"
                );
            } else {
                statusElement.textContent = "";
            }
        }
    }

    /**
     * Build a table from each source's original backend citation
     * number to a dense, sequential display number, in the order the
     * sources were returned. Returns null - never renumber - unless
     * every id is a unique positive integer.
     */
    function buildCitationDisplayMap(sources) {
        const seenIds = new Set();
        const displayNumberById = new Map();
        let nextDisplayNumber = 1;

        for (const source of sources) {
            const originalId = source.citation;

            if (
                !Number.isInteger(originalId)
                || originalId <= 0
            ) {
                return null;
            }

            if (seenIds.has(originalId)) {
                return null;
            }

            seenIds.add(originalId);
            displayNumberById.set(
                originalId,
                nextDisplayNumber
            );
            nextDisplayNumber += 1;
        }

        return displayNumberById;
    }

    /**
     * Parse every "[n]" / "[n, m, ...]" citation group in one answer,
     * in a single pass, without touching any other bracketed text
     * (only digits and commas are ever treated as a citation group).
     */
    function findCitationGroups(answer) {
        const groups = [];
        const pattern = new RegExp(
            CITATION_GROUP_PATTERN.source,
            "g"
        );

        let match = pattern.exec(answer);

        while (match !== null) {
            groups.push(
                {
                    fullMatch: match[0],
                    index: match.index,
                    ids: match[1]
                        .split(",")
                        .map(
                            (piece) => parseInt(
                                piece.trim(),
                                10
                            )
                        ),
                }
            );

            match = pattern.exec(answer);
        }

        return groups;
    }

    /**
     * Renumber every citation group in one answer using a display
     * map already built for this response's sources.
     *
     * Validates every group against the map first, then rewrites the
     * text in one reconstruction pass - never a naive sequence of
     * String.replace calls, which could corrupt an already-rewritten
     * number. If any group cites an id the map does not have, no
     * group is renumbered: the answer and its sources are returned
     * exactly as received.
     */
    function renumberCitationMarkers(answer, citationMap) {
        const groups = findCitationGroups(
            answer
        );

        const allGroupsKnown = groups.every(
            (group) => group.ids.every(
                (id) => citationMap.has(id)
            )
        );

        if (!allGroupsKnown) {
            return {
                text: answer,
                ok: false,
            };
        }

        let rebuilt = "";
        let cursor = 0;

        groups.forEach((group) => {
            rebuilt += answer.slice(
                cursor,
                group.index
            );

            const displayIds = group.ids.map(
                (id) => citationMap.get(id)
            );

            rebuilt += `[${displayIds.join(", ")}]`;

            cursor = (
                group.index
                + group.fullMatch.length
            );
        });

        rebuilt += answer.slice(
            cursor
        );

        return {
            text: rebuilt,
            ok: true,
        };
    }

    /**
     * Apply this response's own local citation renumbering.
     *
     * Falls back to the original answer and sources untouched -
     * never a partial renumbering - whenever the sources carry
     * invalid or duplicate ids, or the answer cites an id the
     * sources do not have.
     */
    function applyCitationRenumbering(answer, sources) {
        const citationMap = buildCitationDisplayMap(
            sources
        );

        if (!citationMap) {
            return {
                answer,
                sources,
            };
        }

        const renumbered = renumberCitationMarkers(
            answer,
            citationMap
        );

        if (!renumbered.ok) {
            return {
                answer,
                sources,
            };
        }

        const renumberedSources = sources.map(
            (source) => (
                {
                    ...source,
                    citation: citationMap.get(
                        source.citation
                    ),
                }
            )
        );

        return {
            answer: renumbered.text,
            sources: renumberedSources,
        };
    }

    async function requestJson(url, options) {
        const response = await fetch(
            url,
            options
        );

        let payload = null;

        try {
            payload = await response.json();
        } catch {
            payload = null;
        }

        if (!response.ok) {
            const error = new Error(
                getErrorMessage(
                    payload,
                    response.status
                )
            );

            // Exposed only so a caller can react to a specific
            // backend validation failure (see
            // isConversationStateValidationError) - never displayed
            // or logged as-is.
            error.statusCode = response.status;
            error.payload = payload;

            throw error;
        }

        return payload || {};
    }

    /**
     * True only for a 422 whose FastAPI validation detail names
     * conversation_state - never for any other 422 (e.g. an invalid
     * question), which must surface as a normal error instead.
     */
    function isConversationStateValidationError(error) {
        if (
            !error
            || error.statusCode !== 422
            || !error.payload
        ) {
            return false;
        }

        const detail = error.payload.detail;

        if (!Array.isArray(detail)) {
            return false;
        }

        return detail.some(
            (item) => (
                item
                && Array.isArray(item.loc)
                && item.loc.includes(
                    "conversation_state"
                )
            )
        );
    }

    function getErrorMessage(payload, statusCode) {
        if (
            payload
            && typeof payload.detail === "string"
        ) {
            return payload.detail;
        }

        if (
            payload
            && typeof payload.message === "string"
        ) {
            return payload.message;
        }

        if (
            payload
            && payload.data
            && typeof payload.data.message === "string"
        ) {
            return payload.data.message;
        }

        if (statusCode === 429) {
            return (
                "Too many questions have been submitted. "
                + "Please try again shortly."
            );
        }

        return (
            "The legal assistant could not process "
            + "the request."
        );
    }

    // =====================================================================
    // GATE S7-LITE: /chat/stream consumption - fetch + ReadableStream +
    // TextDecoder, a robust NDJSON parser, protocol v1 state validation,
    // and reconstruction of an object shaped exactly like /chat's own
    // JSON response, so the EXISTING renderer (buildAssistantBubble and
    // everything upstream of it) never needs to know which endpoint a
    // turn's answer came from. No progressive UI here - deltas/
    // validating/discard/replacement only update internal state; the
    // caller sees one complete result once (and only once) `done`
    // arrives, exactly like a resolved requestJson() call.
    // =====================================================================

    /**
     * True only when this browser can plausibly support requestStream()
     * - fetch, a real ReadableStream body, and TextDecoder. Takes an
     * optional capability source purely so tests can simulate an
     * unsupported browser without touching real globals; production
     * code always calls this with no argument.
     */
    function isStreamResponseSupported(capabilitySource) {
        const source = (
            capabilitySource
            || (
                typeof globalThis !== "undefined"
                    ? globalThis
                    : {}
            )
        );

        return Boolean(
            source
            && typeof source.fetch === "function"
            && typeof source.ReadableStream === "function"
            && typeof source.TextDecoder === "function"
        );
    }

    /**
     * The ONE place that decides /chat vs /chat/stream for a widget -
     * called once per widget (mission section 3/5): OFF by default,
     * and never chosen at all unless this browser can actually support
     * it. Never re-evaluated mid-request, so a retry can never migrate
     * from one transport to the other.
     */
    function resolveChatTransport(
        {
            chatStreamingEnabled,
            chatStreamEndpoint,
            capabilitySource,
        }
    ) {
        if (
            !chatStreamingEnabled
            || !chatStreamEndpoint
        ) {
            return "json";
        }

        return isStreamResponseSupported(capabilitySource)
            ? "stream"
            : "json";
    }

    /**
     * The retry-on-invalid-conversation_state wrapper shared by /chat
     * and /chat/stream alike (RECTIFICATIF §D) - `send` already has
     * its transport fixed by the caller (see chatTransport in
     * initializeWidget), so this never has an opinion about which
     * endpoint is used; it only ever calls `send` again, never a
     * different function. That is what makes "no automatic retry
     * against /chat after a stream request has been sent" (mission
     * section 5) hold structurally rather than by convention: there is
     * no second `send` implementation for this to accidentally reach
     * for.
     */
    async function performChatTransportRequest(
        {
            send,
            includeConversationState,
            onConversationStateRejected,
        }
    ) {
        try {
            return await send(
                includeConversationState
            );
        } catch (error) {
            if (
                includeConversationState
                && isConversationStateValidationError(
                    error
                )
            ) {
                onConversationStateRejected();

                return performChatTransportRequest(
                    {
                        send,
                        includeConversationState: false,
                        onConversationStateRejected,
                    }
                );
            }

            throw error;
        }
    }

    /**
     * Incrementally decodes arbitrary byte chunks into complete
     * newline-terminated NDJSON records. Network chunk boundaries are
     * arbitrary - a chunk may end mid-multibyte-character or mid-JSON-
     * object - so this buffers raw text (never raw bytes are handed to
     * JSON.parse) and only ever yields complete lines. A raw newline
     * byte can only ever be a record separator: JSON string values
     * with an embedded newline always carry it escaped ("\n", two
     * characters), never a literal 0x0A, so this never mis-splits a
     * record's own text.
     */
    function createNdjsonLineParser() {
        const decoder = new TextDecoder("utf-8");
        let buffer = "";

        function push(chunk) {
            buffer += decoder.decode(
                chunk,
                { stream: true }
            );

            const records = [];
            let newlineIndex = buffer.indexOf("\n");

            while (newlineIndex !== -1) {
                const rawLine = buffer.slice(
                    0,
                    newlineIndex
                );

                buffer = buffer.slice(
                    newlineIndex + 1
                );

                if (rawLine.length > 0) {
                    records.push(
                        parseNdjsonLine(rawLine)
                    );
                }

                newlineIndex = buffer.indexOf("\n");
            }

            return records;
        }

        /**
         * Call once at end-of-stream. Flushes any pending decoder
         * state and rejects a non-empty, newline-less remainder - a
         * truncated record is never silently accepted as complete.
         */
        function flush() {
            buffer += decoder.decode();

            const hadTrailingContent = (
                buffer.trim().length > 0
            );

            buffer = "";

            if (hadTrailingContent) {
                throw createStreamProtocolError(
                    "truncated_stream",
                    "The response stream ended with an "
                    + "incomplete record."
                );
            }
        }

        return { push, flush };
    }

    function parseNdjsonLine(rawLine) {
        try {
            return JSON.parse(rawLine);
        } catch {
            throw createStreamProtocolError(
                "malformed_record",
                "The response stream contained a "
                + "malformed record."
            );
        }
    }

    function createStreamProtocolError(code, message) {
        const error = new Error(message);
        error.code = code;
        return error;
    }

    function createStreamReconstructionState() {
        return {
            started: false,
            terminal: null,
            answerText: "",
            metadata: null,
            errorCode: null,
            errorMessage: null,
            errorRetryable: false,
        };
    }

    /**
     * Validates and applies one already-JSON-parsed NDJSON record to
     * the running reconstruction state (mission sections 7/8). Mutates
     * and returns the same state object. Throws a StreamProtocolError-
     * shaped Error (a plain Error with a .code) for any sequence this
     * client does not recognize as valid protocol v1 - never silently
     * ignored.
     *
     * `replacement` is accepted any time after `start` and before a
     * terminal event, WITH or WITHOUT a preceding `discard`: besides
     * the discard/replacement repair pair, the backend also emits a
     * bare replacement to reconcile the streamed text with the final
     * assembled answer whenever generation is followed by appended
     * content it could not have streamed in advance (an unavailable-
     * countries note, or an ungrounded-answer contact fallback - see
     * chat_stream.py's own post-generation reconciliation branch).
     * Requiring a prior discard in every case would reject that
     * legitimate, routine success response as a protocol error.
     */
    function applyStreamProtocolEvent(state, record) {
        if (
            !record
            || typeof record !== "object"
            || Array.isArray(record)
        ) {
            throw createStreamProtocolError(
                "invalid_record",
                "The response stream contained an "
                + "invalid record."
            );
        }

        if (state.terminal) {
            throw createStreamProtocolError(
                "event_after_terminal",
                "The response stream continued after it "
                + "had already ended."
            );
        }

        const type = record.type;

        if (!state.started) {
            if (type !== "start") {
                throw createStreamProtocolError(
                    "missing_start",
                    "The response stream did not begin "
                    + "with a start event."
                );
            }

            if (
                record.protocol_version
                !== STREAM_PROTOCOL_VERSION
            ) {
                throw createStreamProtocolError(
                    "unsupported_protocol_version",
                    "The response stream used an "
                    + "unsupported protocol version."
                );
            }

            state.started = true;
            return state;
        }

        if (type === "start") {
            throw createStreamProtocolError(
                "duplicate_start",
                "The response stream sent more than one "
                + "start event."
            );
        }

        if (type === "delta") {
            if (typeof record.text !== "string") {
                throw createStreamProtocolError(
                    "invalid_delta",
                    "The response stream sent a malformed "
                    + "delta event."
                );
            }

            state.answerText += record.text;
            return state;
        }

        if (type === "validating") {
            return state;
        }

        if (type === "discard") {
            state.answerText = "";
            return state;
        }

        if (type === "replacement") {
            if (typeof record.text !== "string") {
                throw createStreamProtocolError(
                    "invalid_replacement",
                    "The response stream sent a malformed "
                    + "replacement event."
                );
            }

            state.answerText = record.text;
            return state;
        }

        if (type === "metadata") {
            state.metadata = record;
            return state;
        }

        if (type === "done") {
            if (!state.metadata) {
                throw createStreamProtocolError(
                    "missing_metadata",
                    "The response stream reached done "
                    + "without metadata."
                );
            }

            state.terminal = "done";
            return state;
        }

        if (type === "error") {
            state.terminal = "error";
            state.errorCode = record.code || null;
            state.errorMessage = (
                typeof record.message === "string"
                    ? record.message
                    : (
                        "The legal assistant could not "
                        + "process the request."
                    )
            );
            state.errorRetryable = Boolean(
                record.retryable
            );
            return state;
        }

        throw createStreamProtocolError(
            "unknown_event_type",
            "The response stream contained an "
            + "unrecognized event."
        );
    }

    /** Mirrors requestJson()'s own thrown-Error shape/philosophy. */
    function buildStreamTerminalError(state) {
        const error = new Error(
            state.errorMessage
            || "The legal assistant could not process "
            + "the request."
        );

        error.code = state.errorCode;
        error.retryable = state.errorRetryable;
        return error;
    }

    /**
     * Builds an object shaped exactly like /chat's own LegalChatResponse
     * JSON (mission section 9) from a state that reached `done` -
     * answer from the accumulated delta/replacement text, everything
     * else from the one `metadata` record (itself every LegalChatResponse
     * field except answer - see chat_stream.py's _metadata_record).
     */
    function buildReconstructedChatResponse(state) {
        const metadata = state.metadata || {};

        return {
            question: metadata.question,
            answer: state.answerText,
            grounded: Boolean(metadata.grounded),
            model: (
                metadata.model !== undefined
                    ? metadata.model
                    : null
            ),
            retrieval_total: (
                Number.isInteger(metadata.retrieval_total)
                    ? metadata.retrieval_total
                    : 0
            ),
            sources: (
                Array.isArray(metadata.sources)
                    ? metadata.sources
                    : []
            ),
            contacts: (
                Array.isArray(metadata.contacts)
                    ? metadata.contacts
                    : []
            ),
            conversation_state: (
                metadata.conversation_state !== undefined
                    ? metadata.conversation_state
                    : null
            ),
        };
    }

    /**
     * Consumes one /chat/stream response end to end and returns an
     * object shaped exactly like requestJson(chatEndpoint, ...) would -
     * so a caller can await either interchangeably. A pre-NDJSON HTTP
     * failure (401/422/429/502/503 - mission section 11) throws with
     * the SAME .statusCode/.payload shape requestJson() uses, so
     * isConversationStateValidationError() and the existing error UI
     * both work unchanged. Any failure once the NDJSON body has begun -
     * a malformed/truncated record, a protocol violation, an in-band
     * `error` record, or the request being aborted - throws a plain
     * Error with no .statusCode, so it is never mistaken for that one
     * retryable pre-stream case (see performChatTransportRequest).
     */
    async function requestStream(url, options) {
        const response = await fetch(
            url,
            options
        );

        const contentType = (
            response.headers.get("content-type") || ""
        );

        const isNdjson = contentType.includes(
            "application/x-ndjson"
        );

        if (!response.ok || !isNdjson) {
            let payload = null;

            try {
                payload = await response.json();
            } catch {
                payload = null;
            }

            const error = new Error(
                getErrorMessage(
                    payload,
                    response.status
                )
            );

            error.statusCode = response.status;
            error.payload = payload;
            throw error;
        }

        if (
            !response.body
            || typeof response.body.getReader !== "function"
        ) {
            throw new Error(
                "The legal assistant could not process "
                + "the request."
            );
        }

        const reader = response.body.getReader();
        const parser = createNdjsonLineParser();
        let state = createStreamReconstructionState();

        try {
            while (true) {
                const { value, done } = await reader.read();

                if (done) {
                    parser.flush();
                    break;
                }

                const records = parser.push(value);

                for (const record of records) {
                    state = applyStreamProtocolEvent(
                        state,
                        record
                    );
                }
            }
        } catch (error) {
            try {
                await reader.cancel();
            } catch {
                // Best-effort only - the stream is already
                // being abandoned over the original error.
            }

            throw error;
        }

        if (state.terminal === "error") {
            throw buildStreamTerminalError(state);
        }

        if (state.terminal !== "done") {
            throw new Error(
                "The response stream ended unexpectedly."
            );
        }

        return buildReconstructedChatResponse(state);
    }

    // Test-only hook: absent in the browser (module is never defined
    // there), so this changes nothing about how the widget itself
    // loads or runs. Exposes only the pure, DOM-free functions the
    // test suite exercises directly - never the DOM-wired widget
    // internals (initializeWidget's own event handlers), which are
    // covered by node --check plus direct source review instead.
    if (typeof module !== "undefined" && module.exports) {
        module.exports = {
            CONVERSATION_STORAGE_KEY,
            CONVERSATION_STORAGE_VERSION,
            normalizeStoredConversation,
            loadStoredConversation,
            saveConversation,
            clearStoredConversation,
            rebuildTurnsFromMessages,
            isConversationStateValidationError,
            STREAM_PROTOCOL_VERSION,
            isStreamResponseSupported,
            resolveChatTransport,
            performChatTransportRequest,
            createNdjsonLineParser,
            createStreamReconstructionState,
            applyStreamProtocolEvent,
            buildStreamTerminalError,
            buildReconstructedChatResponse,
            requestStream,
        };
    }
})();
