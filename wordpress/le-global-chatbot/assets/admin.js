(() => {
    "use strict";

    const deleteForms = document.querySelectorAll(
        "[data-confirm-delete]"
    );

    deleteForms.forEach((form) => {
        form.addEventListener(
            "submit",
            (event) => {
                const documentName = (
                    form.dataset.documentName
                    || "this document"
                );

                const confirmed = window.confirm(
                    `Delete ${documentName}? `
                    + "The source DOCX and all indexed chunks "
                    + "will be removed."
                );

                if (!confirmed) {
                    event.preventDefault();
                }
            }
        );
    });

    const uploadForm = document.querySelector(
        ".le-global-chatbot-admin__upload-form"
    );

    if (!uploadForm) {
        return;
    }

    const submitButton = uploadForm.querySelector(
        'button[type="submit"]'
    );

    async function sendUpload(replaceExisting) {
        const formData = new FormData(
            uploadForm
        );

        formData.set(
            "le_global_ajax",
            "1"
        );

        if (replaceExisting) {
            formData.set(
                "replace_existing",
                "1"
            );
        } else {
            formData.delete(
                "replace_existing"
            );
        }

        const response = await fetch(
            uploadForm.action,
            {
                method: "POST",
                body: formData,
                credentials: "same-origin",
            }
        );

        let payload = null;

        try {
            payload = await response.json();
        } catch {
            payload = null;
        }

        return {
            response,
            payload,
        };
    }

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

    uploadForm.addEventListener(
        "submit",
        async (event) => {
            event.preventDefault();

            if (
                submitButton
                && submitButton.disabled
            ) {
                return;
            }

            if (submitButton) {
                submitButton.disabled = true;
            }

            try {
                let result = await sendUpload(
                    false
                );

                const detail = (
                    result.payload
                    && result.payload.data
                    && result.payload.data.detail
                    && typeof result.payload.data.detail === "object"
                )
                    ? result.payload.data.detail
                    : null;

                if (
                    result.response.status === 409
                    && detail
                    && detail.code
                        === "document_replacement_required"
                ) {
                    const country = (
                        typeof detail.country === "string"
                        && detail.country.trim() !== ""
                    )
                        ? detail.country.trim()
                        : "this country";

                    const confirmed = window.confirm(
                        `A document already exists for ${country}. `
                        + "Replace it with the uploaded DOCX? "
                        + "The previous source file and all previous "
                        + "indexed chunks for this country will be "
                        + "replaced."
                    );

                    if (!confirmed) {
                        return;
                    }

                    result = await sendUpload(
                        true
                    );
                }

                if (
                    !result.response.ok
                    || !result.payload
                    || result.payload.success !== true
                ) {
                    throw new Error(
                        errorMessage(
                            result.payload
                        )
                    );
                }

                const message = (
                    result.payload.data
                    && typeof result.payload.data.message
                        === "string"
                )
                    ? result.payload.data.message
                    : "The document was indexed successfully.";

                window.alert(
                    message
                );

                window.location.reload();
            } catch (error) {
                window.alert(
                    (
                        error
                        && typeof error.message === "string"
                        && error.message !== ""
                    )
                        ? error.message
                        : "The document could not be indexed."
                );
            } finally {
                if (submitButton) {
                    submitButton.disabled = false;
                }
            }
        }
    );
})();
