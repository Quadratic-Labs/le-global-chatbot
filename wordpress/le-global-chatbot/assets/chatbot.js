(() => {
    "use strict";

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

        const submitButton = widget.querySelector(
            ".le-global-chatbot__submit"
        );

        const statusElement = widget.querySelector(
            "[data-status]"
        );

        const errorElement = widget.querySelector(
            "[data-error]"
        );

        const responseElement = widget.querySelector(
            "[data-response]"
        );

        const userQuestionElement = widget.querySelector(
            "[data-user-question]"
        );

        const assistantMessageElement = widget.querySelector(
            "[data-assistant-message]"
        );

        const answerElement = widget.querySelector(
            "[data-answer]"
        );

        const sourcesSection = widget.querySelector(
            "[data-sources-section]"
        );

        const sourcesElement = widget.querySelector(
            "[data-sources]"
        );

        if (
            !form
            || !questionInput
            || !characterCount
            || !conversationElement
            || !submitButton
            || !statusElement
            || !errorElement
            || !responseElement
            || !userQuestionElement
            || !assistantMessageElement
            || !answerElement
            || !sourcesSection
            || !sourcesElement
        ) {
            return;
        }

        let maximumQuestionLength = 2000;
        let defaultMaximumSources = 6;

        questionInput.addEventListener(
            "input",
            () => {
                characterCount.textContent = String(
                    questionInput.value.length
                );
            }
        );

        form.addEventListener(
            "submit",
            async (event) => {
                event.preventDefault();

                const question = questionInput.value.trim();

                if (question.length < 2) {
                    showError(
                        "Please enter a legal question."
                    );

                    questionInput.focus();
                    return;
                }

                clearError();

                userQuestionElement.textContent = question;
                assistantMessageElement.hidden = true;
                answerElement.textContent = "";
                sourcesElement.innerHTML = "";
                sourcesSection.hidden = true;
                responseElement.hidden = false;

                scrollConversationToBottom();

                setLoading(
                    true,
                    "Searching validated legal documents…"
                );

                try {
                    const response = await requestJson(
                        chatEndpoint,
                        {
                            method: "POST",
                            headers: {
                                "Content-Type": "application/json",
                            },
                            credentials: "same-origin",
                            body: JSON.stringify({
                                question,
                                language: "en",
                                max_sources: defaultMaximumSources,
                            }),
                        }
                    );

                    renderResponse(response);
                } catch (error) {
                    showError(
                        error instanceof Error
                            ? error.message
                            : "The legal assistant is unavailable."
                    );

                    scrollConversationToBottom();
                } finally {
                    setLoading(false);
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

            setLoading(
                true,
                "Loading the legal catalog…"
            );

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

                return true;
            } catch (error) {
                showError(
                    error instanceof Error
                        ? error.message
                        : "The legal catalog could not be loaded."
                );

                return false;
            } finally {
                setLoading(false);
            }
        }

        function renderResponse(response) {
            answerElement.textContent = response.answer || "";

            sourcesElement.innerHTML = "";

            const sources = Array.isArray(response.sources)
                ? response.sources
                : [];

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

                item.appendChild(title);

                if (metadata.textContent) {
                    item.appendChild(metadata);
                }

                sourcesElement.appendChild(item);
            });

            sourcesSection.hidden = sources.length === 0;
            responseElement.hidden = false;

            assistantMessageElement.hidden = false;

            scrollConversationToBottom();
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

        function setLoading(isLoading, message = "") {
            submitButton.disabled = isLoading;
            questionInput.disabled = isLoading;

            statusElement.textContent = isLoading
                ? message
                : "";
        }
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