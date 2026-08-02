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

    const CONVERSATION_STORAGE_VERSION = 1;

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

            if (
                role === "assistant"
                && Array.isArray(entry.sources)
            ) {
                message.sources = entry.sources.filter(
                    (source) => (
                        source
                        && typeof source === "object"
                    )
                );
            }

            normalizedMessages.push(message);
        });

        return normalizedMessages;
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
        let rawText;

        try {
            rawText = window.sessionStorage.getItem(
                CONVERSATION_STORAGE_KEY
            );
        } catch {
            return [];
        }

        if (!rawText) {
            return [];
        }

        let parsedValue;

        try {
            parsedValue = JSON.parse(rawText);
        } catch {
            clearStoredConversation();
            return [];
        }

        const normalizedMessages = (
            normalizeStoredConversation(parsedValue)
        );

        if (normalizedMessages === null) {
            clearStoredConversation();
            return [];
        }

        const trimmedMessages = (
            trimConversationToCompletePairs(
                normalizedMessages,
                maxHistoryMessages
            )
        );

        if (trimmedMessages.length === 0) {
            clearStoredConversation();
            return [];
        }

        return trimmedMessages;
    }

    /**
     * Persist only the complete, successful turns - never a pending
     * question awaiting its answer, never an error turn - capped to
     * the same complete-pairs limit used everywhere else. Silently
     * gives up on any storage failure (quota exceeded, storage
     * disabled in private browsing): persistence is a convenience,
     * never a requirement for the widget to keep working.
     */
    function saveConversation(turns, maxHistoryMessages) {
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

        // Restore any conversation persisted earlier in this same
        // browser session (survives a reload, never a full browser
        // session close, since sessionStorage is used - never
        // localStorage). Corrupted or unreadable storage silently
        // yields an empty list, so the widget always opens normally.
        const restoredMessages = loadStoredConversation(
            maxHistoryMessages
        );

        if (restoredMessages.length > 0) {
            const rebuilt = rebuildTurnsFromMessages(
                restoredMessages,
                nextTurnId
            );

            turns = rebuilt.turns;
            nextTurnId = rebuilt.nextTurnId;

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

                try {
                    const response = await requestJson(
                        chatEndpoint,
                        {
                            method: "POST",
                            headers: {
                                "Content-Type": "application/json",
                            },
                            credentials: "same-origin",
                            signal: controller.signal,
                            body: JSON.stringify({
                                question,
                                history: historyPayload,
                                language: "en",
                                max_sources: defaultMaximumSources,
                            }),
                        }
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

                    turn.status = "success";
                    turn.answer = renumbered.answer;
                    turn.sources = renumbered.sources;
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
                            maxHistoryMessages
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
            assistantBubble.appendChild(
                answerElement
            );

            const sources = Array.isArray(turn.sources)
                ? turn.sources
                : [];

            if (sources.length > 0) {
                assistantBubble.appendChild(
                    buildSourcesSection(
                        sources
                    )
                );
            }

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

            return assistantBubble;
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
            throw new Error(
                getErrorMessage(
                    payload,
                    response.status
                )
            );
        }

        return payload || {};
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
})();
