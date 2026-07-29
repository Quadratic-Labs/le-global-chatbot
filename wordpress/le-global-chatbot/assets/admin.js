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
})();