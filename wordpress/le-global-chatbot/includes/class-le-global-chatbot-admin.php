<?php
/**
 * WordPress administration interface for L&E Global documents.
 */

if (!defined('ABSPATH')) {
    exit;
}

final class LE_Global_Chatbot_Admin
{
    private const VERSION = '0.7.2';

    private const PAGE_SLUG = 'le-global-chatbot';

    private const CAPABILITY = 'manage_options';

    private const DOCUMENTS_PATH = '/api/v1/admin/documents';

    // A backend operation on a large document has been measured at
    // ~30s (mission "ORDER 3B"), which sits exactly at PHP's own
    // apache2handler max_execution_time (also 30s, unmodified - see
    // timeout-audit.txt). wp_remote_request's own per-call timeout
    // (120s/120s/90s below) only bounds how long PHP waits for cURL;
    // it does nothing to extend PHP's own script execution budget, so
    // the three handlers proxying a genuinely long operation each
    // raise it explicitly (mission "ORDER 4", section 8).
    private const LONG_OPERATION_TIME_LIMIT_SECONDS = 150;

    private const UPLOAD_ACTION = (
        'le_global_chatbot_upload_document'
    );

    private const REINDEX_ACTION = (
        'le_global_chatbot_reindex_document'
    );

    private const DELETE_ACTION = (
        'le_global_chatbot_delete_document'
    );

    private const DOWNLOAD_ACTION = (
        'le_global_chatbot_download_document'
    );

    private const REFRESH_ACTION = (
        'le_global_chatbot_refresh_state'
    );

    private const SECTIONS_LIST_ACTION = (
        'le_global_chatbot_list_sections'
    );

    private const SECTION_GET_ACTION = (
        'le_global_chatbot_get_section'
    );

    private const SECTION_UPDATE_ACTION = (
        'le_global_chatbot_update_section'
    );

    private const SECTION_ADD_ACTION = (
        'le_global_chatbot_add_section'
    );

    private const SECTION_DELETE_ACTION = (
        'le_global_chatbot_delete_section'
    );

    // Mission "ORDER 8E-A2" - the country-conflict review/resolution
    // proxy actions. CONFLICT_REVIEW_ACTION is a read-only GET;
    // RESOLVE_CONFLICT_ACTION handles AUTO_DEDUPLICATE/CHOOSE_DOCUMENT
    // (no file); RESOLVE_CONFLICT_REPLACE_ACTION handles
    // REPLACE_WITH_DOCUMENT (a file, reusing the exact same upload
    // decision flow as handle_upload).
    private const CONFLICT_REVIEW_ACTION = (
        'le_global_chatbot_conflict_review'
    );

    private const RESOLVE_CONFLICT_ACTION = (
        'le_global_chatbot_resolve_conflict'
    );

    private const RESOLVE_CONFLICT_REPLACE_ACTION = (
        'le_global_chatbot_resolve_conflict_replace'
    );

    private static ?string $page_hook = null;

    public static function init(): void
    {
        add_action(
            'admin_menu',
            [self::class, 'register_menu']
        );

        add_action(
            'admin_enqueue_scripts',
            [self::class, 'enqueue_assets']
        );

        add_action(
            'admin_post_' . self::UPLOAD_ACTION,
            [self::class, 'handle_upload']
        );

        add_action(
            'admin_post_' . self::REINDEX_ACTION,
            [self::class, 'handle_reindex']
        );

        add_action(
            'admin_post_' . self::DELETE_ACTION,
            [self::class, 'handle_delete']
        );

        add_action(
            'admin_post_' . self::DOWNLOAD_ACTION,
            [self::class, 'handle_download']
        );

        add_action(
            'admin_post_' . self::REFRESH_ACTION,
            [self::class, 'handle_refresh']
        );

        add_action(
            'admin_post_' . self::SECTIONS_LIST_ACTION,
            [self::class, 'handle_list_sections']
        );

        add_action(
            'admin_post_' . self::SECTION_GET_ACTION,
            [self::class, 'handle_get_section']
        );

        add_action(
            'admin_post_' . self::SECTION_UPDATE_ACTION,
            [self::class, 'handle_update_section']
        );

        add_action(
            'admin_post_' . self::SECTION_ADD_ACTION,
            [self::class, 'handle_add_section']
        );

        add_action(
            'admin_post_' . self::SECTION_DELETE_ACTION,
            [self::class, 'handle_delete_section']
        );

        add_action(
            'admin_post_' . self::CONFLICT_REVIEW_ACTION,
            [self::class, 'handle_conflict_review']
        );

        add_action(
            'admin_post_' . self::RESOLVE_CONFLICT_ACTION,
            [self::class, 'handle_resolve_conflict']
        );

        add_action(
            'admin_post_' . self::RESOLVE_CONFLICT_REPLACE_ACTION,
            [self::class, 'handle_resolve_conflict_replace']
        );
    }

    public static function register_menu(): void
    {
        $page_hook = add_menu_page(
            'L&E Global Chatbot',
            'L&E Chatbot',
            self::CAPABILITY,
            self::PAGE_SLUG,
            [self::class, 'render_page'],
            'dashicons-media-document',
            58
        );

        if (is_string($page_hook)) {
            self::$page_hook = $page_hook;
        }
    }

    public static function enqueue_assets(
        string $hook_suffix
    ): void {
        if (
            self::$page_hook === null
            || $hook_suffix !== self::$page_hook
        ) {
            return;
        }

        wp_enqueue_style(
            'le-global-chatbot-admin',
            plugins_url(
                '../assets/admin.css',
                __FILE__
            ),
            [],
            self::VERSION
        );

        wp_enqueue_script(
            'le-global-chatbot-admin',
            plugins_url(
                '../assets/admin.js',
                __FILE__
            ),
            [],
            self::VERSION,
            true
        );
    }

    public static function render_page(): void
    {
        if (
            !current_user_can(
                self::CAPABILITY
            )
        ) {
            wp_die(
                esc_html__(
                    'You are not allowed to access this page.',
                    'le-global-chatbot'
                )
            );
        }

        self::render_notice();

        [$documents, $catalog_error] = self::fetch_document_catalog();
        $stats = self::fetch_document_stats();

        $total_documents = (
            $stats !== null
            ? (int) $stats['total_documents']
            : count($documents)
        );

        $total_countries = (
            $stats !== null
            ? (int) $stats['total_countries']
            : 0
        );

        $conflicted_country_codes = self::detect_conflicted_country_codes(
            $documents
        );

        // Mission "ORDER 8E-A2", section 28 - the backend's own
        // deduplicated, country-level stat is the source of truth;
        // count($conflicted_country_codes) (never
        // count_needs_attention's raw per-document count) is only ever
        // a fallback for a stats response that could not be loaded.
        $countries_requiring_action_count = (
            $stats !== null
            ? (int) $stats['countries_requiring_action']
            : count($conflicted_country_codes)
        );

        ?>
        <div class="wrap le-global-chatbot-admin">
            <header class="le-global-chatbot-admin__header">
                <div>
                    <p class="le-global-chatbot-admin__eyebrow">
                        L&amp;E Global
                    </p>

                    <h1>
                        Document management
                    </h1>

                    <p class="le-global-chatbot-admin__description">
                        Upload, edit and maintain the employment-law
                        documents the chatbot uses to answer questions.
                    </p>
                </div>
            </header>

            <?php self::render_upload_panel(); ?>

            <?php
            self::render_section_editor_panel(
                $documents,
                $conflicted_country_codes
            );
            ?>

            <?php
            self::render_documents_panel(
                $documents,
                $catalog_error,
                $conflicted_country_codes
            );
            ?>

            <?php
            self::render_overview_panel(
                $total_documents,
                $total_countries,
                $countries_requiring_action_count
            );
            ?>
        </div>
        <?php
    }

    /**
     * ORDER 8B, section 5 - a business-friendly drop zone replaces the
     * bare native file input as the primary visual affordance. The
     * real <input type="file"> stays in the DOM (just visually
     * de-emphasized, never display:none, so it remains reachable by
     * assistive tech and keyboard) - "Choose documents" is a plain
     * button that forwards its click to it, and drag-and-drop is a
     * purely additive convenience admin.js wires on top; neither path
     * is the only way in, so the feature keeps working with JS
     * disabled via the real (if visually secondary) submit button.
     */
    private static function render_upload_panel(): void
    {
        ?>
        <section class="le-global-chatbot-admin__panel">
            <div class="le-global-chatbot-admin__panel-header">
                <div>
                    <h2>Upload documents</h2>

                    <p>
                        Upload one or more Word documents (.docx).
                        Maximum file size: 25 MB each.
                    </p>
                </div>
            </div>

            <form
                class="le-global-chatbot-admin__upload-form"
                method="post"
                action="<?php
                    echo esc_url(
                        admin_url(
                            'admin-post.php'
                        )
                    );
                ?>"
                enctype="multipart/form-data"
                data-refresh-action="<?php
                    echo esc_attr(self::REFRESH_ACTION);
                ?>"
                data-refresh-nonce="<?php
                    echo esc_attr(
                        wp_create_nonce(self::REFRESH_ACTION)
                    );
                ?>"
                data-reindex-action="<?php
                    echo esc_attr(self::REINDEX_ACTION);
                ?>"
                data-delete-action="<?php
                    echo esc_attr(self::DELETE_ACTION);
                ?>"
                data-conflict-review-action="<?php
                    echo esc_attr(self::CONFLICT_REVIEW_ACTION);
                ?>"
                data-conflict-review-nonce="<?php
                    echo esc_attr(
                        wp_create_nonce(self::CONFLICT_REVIEW_ACTION)
                    );
                ?>"
                data-resolve-conflict-action="<?php
                    echo esc_attr(self::RESOLVE_CONFLICT_ACTION);
                ?>"
                data-resolve-conflict-nonce="<?php
                    echo esc_attr(
                        wp_create_nonce(self::RESOLVE_CONFLICT_ACTION)
                    );
                ?>"
                data-resolve-conflict-replace-action="<?php
                    echo esc_attr(self::RESOLVE_CONFLICT_REPLACE_ACTION);
                ?>"
                data-resolve-conflict-replace-nonce="<?php
                    echo esc_attr(
                        wp_create_nonce(
                            self::RESOLVE_CONFLICT_REPLACE_ACTION
                        )
                    );
                ?>"
            >
                <input
                    type="hidden"
                    name="action"
                    value="<?php
                        echo esc_attr(
                            self::UPLOAD_ACTION
                        );
                    ?>"
                >

                <?php
                wp_nonce_field(
                    self::UPLOAD_ACTION
                );
                ?>

                <div
                    id="le-global-dropzone"
                    class="le-global-chatbot-admin__dropzone"
                >
                    <p class="le-global-chatbot-admin__dropzone-title">
                        Drag and drop Word documents here
                    </p>

                    <p class="le-global-chatbot-admin__dropzone-or">
                        or
                    </p>

                    <label
                        for="le-global-document"
                        class="button button-primary"
                        id="le-global-choose-documents"
                    >
                        Choose documents
                    </label>

                    <input
                        id="le-global-document"
                        class="le-global-chatbot-admin__file-input"
                        type="file"
                        name="document"
                        accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                        multiple
                    >

                    <p class="le-global-chatbot-admin__dropzone-hint">
                        DOCX &middot; Maximum 25 MB per file
                    </p>
                </div>

                <p class="description">
                    Documents are checked automatically and made
                    available to the chatbot once ready.
                </p>

                <button
                    type="submit"
                    class="button button-primary le-global-chatbot-admin__upload-fallback-submit"
                >
                    Upload documents
                </button>
            </form>

            <div
                id="le-global-chatbot-queue"
                class="le-global-chatbot-admin__queue"
                aria-live="polite"
            ></div>
        </section>
        <?php
    }

    /**
     * ORDER 8B, section 12 - "Add / Edit a section" as one container
     * with a segmented Edit/Add mode control, Edit as the default
     * visible mode. Every field an existing test/JS module already
     * depends on (le-global-edit-country/section/content/cancel/save,
     * le-global-chatbot-edit-message) keeps its exact id - only the
     * now-moot Restore button is gone, and a parallel set of Add-only
     * fields is added alongside.
     */
    private static function render_section_editor_panel(
        array $documents,
        array $conflicted_country_codes
    ): void {
        ?>
        <section class="le-global-chatbot-admin__panel">
            <div class="le-global-chatbot-admin__panel-header">
                <div>
                    <h2>Add / Edit a section</h2>

                    <p>
                        Update existing legal information or add a new
                        section.
                    </p>
                </div>

                <button
                    type="button"
                    id="le-global-edit-collapse"
                    class="button le-global-chatbot-admin__collapse-button"
                    aria-label="Collapse section form"
                    title="Collapse section form"
                    hidden
                >
                    <span aria-hidden="true">&#9650;</span>
                </button>
            </div>

            <div
                class="le-global-chatbot-admin__segmented"
                role="tablist"
                aria-label="Section editor mode"
            >
                <button
                    type="button"
                    id="le-global-mode-edit"
                    class="le-global-chatbot-admin__segmented-option is-active"
                    role="tab"
                    aria-selected="true"
                >
                    Edit a section
                </button>

                <button
                    type="button"
                    id="le-global-mode-add"
                    class="le-global-chatbot-admin__segmented-option"
                    role="tab"
                    aria-selected="false"
                >
                    + Add a new section
                </button>
            </div>

            <div
                id="le-global-chatbot-edit"
                class="le-global-chatbot-admin__edit"
                hidden
                data-admin-post-url="<?php
                    echo esc_url(
                        admin_url('admin-post.php')
                    );
                ?>"
                data-sections-list-action="<?php
                    echo esc_attr(self::SECTIONS_LIST_ACTION);
                ?>"
                data-sections-list-nonce="<?php
                    echo esc_attr(
                        wp_create_nonce(
                            self::SECTIONS_LIST_ACTION
                        )
                    );
                ?>"
                data-section-get-action="<?php
                    echo esc_attr(self::SECTION_GET_ACTION);
                ?>"
                data-section-get-nonce="<?php
                    echo esc_attr(
                        wp_create_nonce(
                            self::SECTION_GET_ACTION
                        )
                    );
                ?>"
                data-section-update-action="<?php
                    echo esc_attr(self::SECTION_UPDATE_ACTION);
                ?>"
                data-section-update-nonce="<?php
                    echo esc_attr(
                        wp_create_nonce(
                            self::SECTION_UPDATE_ACTION
                        )
                    );
                ?>"
                data-section-delete-action="<?php
                    echo esc_attr(self::SECTION_DELETE_ACTION);
                ?>"
                data-section-delete-nonce="<?php
                    echo esc_attr(
                        wp_create_nonce(
                            self::SECTION_DELETE_ACTION
                        )
                    );
                ?>"
                data-section-add-action="<?php
                    echo esc_attr(self::SECTION_ADD_ACTION);
                ?>"
                data-section-add-nonce="<?php
                    echo esc_attr(
                        wp_create_nonce(
                            self::SECTION_ADD_ACTION
                        )
                    );
                ?>"
            >
                <?php if ($conflicted_country_codes) : ?>
                    <div class="le-global-chatbot-admin__duplicate-warning">
                        <?php
                        $conflict_count = count($conflicted_country_codes);

                        echo esc_html(
                            $conflict_count === 1
                                ? '1 country requires action before '
                                    . 'its content can be edited.'
                                : sprintf(
                                    '%d countries require action '
                                        . 'before their content can be '
                                        . 'edited.',
                                    $conflict_count
                                )
                        );
                        ?>

                        <?php if ($conflict_count === 1) : ?>
                            <?php
                            $conflicted_document = self::find_document_by_country_code(
                                $documents,
                                $conflicted_country_codes[0]
                            );
                            ?>
                            <button
                                type="button"
                                class="button"
                                data-review-country-code="<?php
                                    echo esc_attr(
                                        $conflicted_country_codes[0]
                                    );
                                ?>"
                                data-review-country-name="<?php
                                    echo esc_attr(
                                        $conflicted_document['country']
                                        ?? 'This country'
                                    );
                                ?>"
                            >
                                Review
                            </button>
                        <?php else : ?>
                            <a
                                class="button"
                                href="#le-global-chatbot-documents"
                            >
                                Review in Documents
                            </a>
                        <?php endif; ?>
                    </div>
                <?php endif; ?>

                <div class="le-global-chatbot-admin__edit-field">
                    <label for="le-global-edit-country">
                        Country
                    </label>

                    <select id="le-global-edit-country">
                        <option value="">
                            Select a country…
                        </option>

                        <?php
                        foreach (
                            self::sorted_documents_for_edit(
                                $documents,
                                $conflicted_country_codes
                            ) as $edit_document
                        ) :
                            ?>
                            <option
                                value="<?php
                                    echo esc_attr(
                                        $edit_document[
                                            'document_id'
                                        ]
                                    );
                                ?>"
                            >
                                <?php
                                echo esc_html(
                                    $edit_document['country']
                                    . ' ('
                                    . $edit_document[
                                        'country_code'
                                    ]
                                    . ')'
                                );
                                ?>
                            </option>
                        <?php endforeach; ?>
                    </select>
                </div>

                <div
                    id="le-global-edit-only-fields"
                    class="le-global-chatbot-admin__mode-fields"
                >
                    <div class="le-global-chatbot-admin__edit-field">
                        <label for="le-global-edit-section">
                            Section to edit
                        </label>

                        <select
                            id="le-global-edit-section"
                            disabled
                        >
                            <option value="">
                                Select a country first…
                            </option>
                        </select>
                    </div>

                    <div class="le-global-chatbot-admin__edit-field">
                        <label for="le-global-edit-title">
                            Section title
                        </label>

                        <input
                            type="text"
                            id="le-global-edit-title"
                            disabled
                        >
                    </div>

                    <div class="le-global-chatbot-admin__edit-field">
                        <label for="le-global-edit-content">
                            Section content
                        </label>

                        <textarea
                            id="le-global-edit-content"
                            class="le-global-chatbot-admin__edit-textarea"
                            disabled
                        ></textarea>
                    </div>

                    <p
                        id="le-global-edit-hint"
                        class="le-global-chatbot-admin__edit-hint"
                    ></p>

                    <div class="le-global-chatbot-admin__edit-delete">
                        <button
                            type="button"
                            id="le-global-edit-delete"
                            class="button le-global-chatbot-admin__delete-button is-destructive"
                            disabled
                        >
                            Delete section
                        </button>
                    </div>
                </div>

                <div
                    id="le-global-add-only-fields"
                    class="le-global-chatbot-admin__mode-fields"
                    hidden
                >
                    <div class="le-global-chatbot-admin__edit-field">
                        <label for="le-global-add-title">
                            New section title
                        </label>

                        <input
                            type="text"
                            id="le-global-add-title"
                            disabled
                        >
                    </div>

                    <div class="le-global-chatbot-admin__edit-field">
                        <label for="le-global-add-position">
                            Where should this section appear?
                        </label>

                        <select
                            id="le-global-add-position"
                            disabled
                        >
                            <option value="">
                                Select a country first…
                            </option>
                        </select>
                    </div>

                    <div
                        id="le-global-add-duplicate-warning"
                        class="le-global-chatbot-admin__duplicate-warning"
                        hidden
                    ></div>

                    <div class="le-global-chatbot-admin__edit-field">
                        <label for="le-global-add-content">
                            Section content
                        </label>

                        <textarea
                            id="le-global-add-content"
                            class="le-global-chatbot-admin__edit-textarea"
                            placeholder="Enter the legal information for this section…"
                            disabled
                        ></textarea>
                    </div>
                </div>

                <div
                    id="le-global-chatbot-edit-message"
                    class="le-global-chatbot-admin__edit-message"
                    aria-live="polite"
                ></div>

                <div class="le-global-chatbot-admin__edit-actions">
                    <button
                        type="button"
                        id="le-global-edit-cancel"
                        class="button"
                        disabled
                    >
                        Cancel
                    </button>

                    <button
                        type="button"
                        id="le-global-edit-save"
                        class="button button-primary"
                        disabled
                    >
                        Save changes
                    </button>

                    <button
                        type="button"
                        id="le-global-add-submit"
                        class="button button-primary"
                        disabled
                        hidden
                    >
                        + Add section
                    </button>
                </div>
            </div>
        </section>
        <?php
    }

    /**
     * ORDER 8B, sections 22-34 - the documents table no longer shows
     * Chunks or document_id (both remain available technically as
     * dataset/hidden values elsewhere, never as visible text), adds
     * Last updated, and folds Reindex/Delete into a single "⋯" menu
     * next to Download. A client-side search box and status filter
     * sit above the table; both operate only on the rows already
     * rendered here (or by the JS refresh path), never a new server
     * round-trip.
     */
    private static function render_documents_panel(
        array $documents,
        ?string $catalog_error,
        array $conflicted_country_codes
    ): void {
        ?>
        <section class="le-global-chatbot-admin__panel">
            <div class="le-global-chatbot-admin__panel-header">
                <div>
                    <h2>
                        Documents
                        <span
                            id="le-global-document-count"
                            class="le-global-chatbot-admin__count"
                        >
                            <?php
                            echo esc_html(
                                self::document_count_label(
                                    count($documents)
                                )
                            );
                            ?>
                        </span>
                    </h2>

                    <p>
                        Each row represents one document currently
                        available to the chatbot.
                    </p>
                </div>
            </div>

            <div class="le-global-chatbot-admin__toolbar">
                <div class="le-global-chatbot-admin__toolbar-field">
                    <label for="le-global-documents-search">
                        Search documents
                    </label>

                    <input
                        type="search"
                        id="le-global-documents-search"
                        placeholder="Search by country or document name"
                    >
                </div>

                <div class="le-global-chatbot-admin__toolbar-field">
                    <label for="le-global-documents-status-filter">
                        Status
                    </label>

                    <select id="le-global-documents-status-filter">
                        <option value="">All</option>
                        <option value="ready">Ready</option>
                        <option value="needs_attention">
                            Needs attention
                        </option>
                    </select>
                </div>
            </div>

            <div
                id="le-global-documents-message"
                class="le-global-chatbot-admin__edit-message"
                aria-live="polite"
            ></div>

            <div
                id="le-global-conflict-review"
                class="le-global-chatbot-admin__conflict-review"
                aria-live="polite"
                hidden
            ></div>

            <div id="le-global-chatbot-documents">
                <?php
                self::render_documents_table_body(
                    $documents,
                    $catalog_error,
                    $conflicted_country_codes
                );
                ?>
            </div>
        </section>
        <?php
    }

    private static function render_documents_table_body(
        array $documents,
        ?string $catalog_error,
        array $conflicted_country_codes
    ): void {
        if ($catalog_error !== null) :
            ?>
            <div class="notice notice-error inline">
                <p><?php echo esc_html($catalog_error); ?></p>
            </div>
            <?php
            return;
        endif;

        if (!$documents) :
            ?>
            <div class="le-global-chatbot-admin__empty">
                No document is currently available.
            </div>
            <?php
            return;
        endif;
        ?>
        <div class="le-global-chatbot-admin__table-container">
            <table
                class="widefat striped le-global-chatbot-admin__table"
                id="le-global-documents-table"
            >
                <thead>
                    <tr>
                        <th scope="col">Country</th>
                        <th scope="col">Document</th>
                        <th scope="col">Year</th>
                        <th scope="col">Status</th>
                        <th scope="col">Last updated</th>
                        <th scope="col">Actions</th>
                    </tr>
                </thead>

                <tbody>
                    <?php
                    foreach (
                        self::group_documents_by_country_code($documents)
                        as $group
                    ) :
                        $requires_action = false;

                        foreach ($group as $grouped_document) {
                            $code = isset($grouped_document['country_code'])
                                ? (string) $grouped_document['country_code']
                                : '';

                            if (
                                isset($grouped_document['requires_action'])
                                ? (bool) $grouped_document['requires_action']
                                : (
                                    $code !== ''
                                    && in_array(
                                        $code,
                                        $conflicted_country_codes,
                                        true
                                    )
                                )
                            ) {
                                $requires_action = true;
                                break;
                            }
                        }

                        if ($requires_action && count($group) > 1) :
                            self::render_conflict_row($group);
                        else :
                            foreach ($group as $grouped_document) {
                                self::render_document_row(
                                    $grouped_document,
                                    $conflicted_country_codes
                                );
                            }
                        endif;
                    endforeach;
                    ?>
                </tbody>
            </table>
        </div>
        <?php
    }

    /**
     * Mission "ORDER 8E-A2", section 21 - groups the catalog purely by
     * country_code, preserving each country's own first-seen order -
     * the one place both the initial server render and the later JS
     * refresh must agree on which rows collapse into a single "Action
     * required" row.
     *
     * @param array<int, array<string, mixed>> $documents
     * @return array<int, array<int, array<string, mixed>>>
     */
    private static function group_documents_by_country_code(
        array $documents
    ): array {
        $groups = [];
        $index_by_code = [];

        foreach ($documents as $document) {
            if (!is_array($document)) {
                continue;
            }

            $code = isset($document['country_code'])
                ? (string) $document['country_code']
                : '';

            $key = $code === '' ? ' no-code-' . count($groups) : $code;

            if (!isset($index_by_code[$key])) {
                $index_by_code[$key] = count($groups);
                $groups[] = [];
            }

            $groups[$index_by_code[$key]][] = $document;
        }

        return $groups;
    }

    /**
     * One synthetic row for an entire conflicted country - never
     * exposes document_id/document_type/chunk counts; "Review" is the
     * only way forward (mirrors admin.js's conflictRowHtml exactly).
     *
     * @param array<int, array<string, mixed>> $documents_for_country
     */
    private static function render_conflict_row(
        array $documents_for_country
    ): void {
        $first = $documents_for_country[0] ?? [];
        $country = isset($first['country']) ? (string) $first['country'] : '';
        $country_code = isset($first['country_code'])
            ? (string) $first['country_code']
            : '';
        ?>
        <tr
            data-country="<?php echo esc_attr(strtolower($country)); ?>"
            data-filename=""
            data-status="needs_attention"
        >
            <td>
                <strong><?php echo esc_html($country); ?></strong>

                <?php if ($country_code !== '') : ?>
                    <span class="le-global-chatbot-admin__country-code">
                        <?php echo esc_html($country_code); ?>
                    </span>
                <?php endif; ?>
            </td>

            <td colspan="3">
                <span
                    class="le-global-chatbot-admin__status is-warning"
                    title="More than one document record is linked to this country."
                >
                    <span aria-hidden="true">⚠</span> Action required
                </span>
                <span class="le-global-chatbot-admin__conflict-note">
                    More than one document record is linked to this
                    country.
                </span>
            </td>

            <td>—</td>

            <td>
                <button
                    type="button"
                    class="button button-primary"
                    data-review-country-code="<?php
                        echo esc_attr($country_code);
                    ?>"
                    data-review-country-name="<?php
                        echo esc_attr($country);
                    ?>"
                >
                    Review
                </button>
            </td>
        </tr>
        <?php
    }

    private static function render_document_row(
        array $document,
        array $conflicted_country_codes
    ): void {
        $document_id = isset($document['document_id'])
            ? (string) $document['document_id']
            : '';

        $country = isset($document['country'])
            ? (string) $document['country']
            : '';

        $country_code = isset($document['country_code'])
            ? (string) $document['country_code']
            : '';

        $source_filename = isset($document['source_filename'])
            ? (string) $document['source_filename']
            : '';

        $reference_year = isset($document['reference_year'])
            ? (int) $document['reference_year']
            : 0;

        $source_present = !empty($document['source_file_present']);

        $has_country_conflict = (
            $country_code !== ''
            && in_array(
                $country_code,
                $conflicted_country_codes,
                true
            )
        );

        $display_status = self::compute_display_status(
            $document,
            $has_country_conflict
        );

        $updated_at_raw = isset($document['updated_at'])
            && is_string($document['updated_at'])
            ? $document['updated_at']
            : null;
        ?>
        <tr
            data-country="<?php echo esc_attr(strtolower($country)); ?>"
            data-filename="<?php
                echo esc_attr(strtolower($source_filename));
            ?>"
            data-status="<?php echo esc_attr($display_status['value']); ?>"
        >
            <td>
                <strong><?php echo esc_html($country); ?></strong>

                <?php if ($country_code !== '') : ?>
                    <span class="le-global-chatbot-admin__country-code">
                        <?php echo esc_html($country_code); ?>
                    </span>
                <?php endif; ?>
            </td>

            <td>
                <span
                    class="le-global-chatbot-admin__filename"
                    data-document-id="<?php
                        echo esc_attr($document_id);
                    ?>"
                >
                    <?php echo esc_html($source_filename); ?>
                </span>
            </td>

            <td>
                <?php
                echo $reference_year > 0
                    ? esc_html((string) $reference_year)
                    : '—';
                ?>
            </td>

            <td>
                <?php self::render_status_badge($display_status); ?>
            </td>

            <td>
                <?php
                echo esc_html(
                    self::format_last_updated($updated_at_raw)
                );
                ?>
            </td>

            <td>
                <?php
                self::render_document_actions(
                    $document,
                    $document_id,
                    $source_filename,
                    $source_present,
                    $has_country_conflict
                );
                ?>
            </td>
        </tr>
        <?php
    }

    /**
     * ORDER 8B, section 36 - status is never conveyed by color alone:
     * every badge pairs its color with both an icon and a text label.
     *
     * @param array{value: string, label: string, icon: string, class: string, title: string} $display_status
     */
    private static function render_status_badge(array $display_status): void
    {
        ?>
        <span
            class="le-global-chatbot-admin__status <?php
                echo esc_attr($display_status['class']);
            ?>"
            title="<?php echo esc_attr($display_status['title']); ?>"
        >
            <span aria-hidden="true"><?php
                echo esc_html($display_status['icon']);
            ?></span>
            <?php echo esc_html($display_status['label']); ?>
        </span>
        <?php
    }

    /**
     * ORDER 8B, section 28 - Download stays its own, always-visible
     * primary action; Refresh chatbot data (internally: Reindex) and
     * the visually-destructive Delete move into one "⋯" menu, so the
     * row never presents three same-weight buttons. A country in
     * conflict can still be downloaded when its own source resolves
     * unambiguously, but never refreshed/edited - see section 26.
     */
    private static function render_document_actions(
        array $document,
        string $document_id,
        string $source_filename,
        bool $source_present,
        bool $has_country_conflict
    ): void {
        $country = isset($document['country'])
            ? (string) $document['country']
            : '';
        ?>
        <div class="le-global-chatbot-admin__actions">
            <?php if ($source_present) : ?>
                <a
                    class="button"
                    href="<?php
                        echo esc_url(
                            self::build_download_url($document_id)
                        );
                    ?>"
                >
                    Download
                </a>
            <?php else : ?>
                <button
                    type="button"
                    class="button"
                    disabled
                    title="No unambiguous source document is available to download."
                >
                    Download
                </button>
            <?php endif; ?>

            <div class="le-global-chatbot-admin__menu">
                <button
                    type="button"
                    class="button le-global-chatbot-admin__menu-toggle"
                    aria-haspopup="true"
                    aria-expanded="false"
                    aria-label="<?php
                        echo esc_attr(
                            'More actions for '
                            . $source_filename
                        );
                    ?>"
                >
                    &hellip;
                </button>

                <div
                    class="le-global-chatbot-admin__menu-list"
                    role="menu"
                    hidden
                >
                    <?php if ($source_present && !$has_country_conflict) : ?>
                        <form
                            method="post"
                            action="<?php
                                echo esc_url(
                                    admin_url('admin-post.php')
                                );
                            ?>"
                            data-reindex-form
                        >
                            <input
                                type="hidden"
                                name="action"
                                value="<?php
                                    echo esc_attr(self::REINDEX_ACTION);
                                ?>"
                            >

                            <input
                                type="hidden"
                                name="document_id"
                                value="<?php echo esc_attr($document_id); ?>"
                            >

                            <?php
                            wp_nonce_field(
                                self::REINDEX_ACTION . ':' . $document_id
                            );
                            ?>

                            <button
                                type="submit"
                                class="le-global-chatbot-admin__menu-item"
                                role="menuitem"
                            >
                                Refresh chatbot data
                            </button>
                        </form>
                    <?php else : ?>
                        <button
                            type="button"
                            class="le-global-chatbot-admin__menu-item"
                            role="menuitem"
                            disabled
                            title="<?php
                                echo esc_attr(
                                    $has_country_conflict
                                        ? 'This country has conflicting document records.'
                                        : 'The source document is unavailable.'
                                );
                            ?>"
                        >
                            Refresh chatbot data
                        </button>
                    <?php endif; ?>

                    <div class="le-global-chatbot-admin__menu-separator"></div>

                    <form
                        method="post"
                        action="<?php
                            echo esc_url(admin_url('admin-post.php'));
                        ?>"
                        data-confirm-delete
                        data-document-name="<?php
                            echo esc_attr($source_filename);
                        ?>"
                        data-country-name="<?php
                            echo esc_attr($country);
                        ?>"
                    >
                        <input
                            type="hidden"
                            name="action"
                            value="<?php
                                echo esc_attr(self::DELETE_ACTION);
                            ?>"
                        >

                        <input
                            type="hidden"
                            name="document_id"
                            value="<?php echo esc_attr($document_id); ?>"
                        >

                        <?php
                        wp_nonce_field(
                            self::DELETE_ACTION . ':' . $document_id
                        );
                        ?>

                        <button
                            type="submit"
                            class="le-global-chatbot-admin__menu-item is-destructive"
                            role="menuitem"
                        >
                            Delete document
                        </button>
                    </form>
                </div>
            </div>
        </div>
        <?php
    }

    /**
     * ORDER 8B, section 35 - Overview replaces the old Statistics
     * cards with business-only numbers; chunk counts/index health
     * never appear here (they remain available to developers via
     * logs/tests only).
     */
    private static function render_overview_panel(
        int $total_documents,
        int $total_countries,
        int $countries_requiring_action_count
    ): void {
        ?>
        <section class="le-global-chatbot-admin__panel">
            <div class="le-global-chatbot-admin__panel-header">
                <div>
                    <h2>Overview</h2>
                </div>
            </div>

            <div
                id="le-global-chatbot-summary"
                class="le-global-chatbot-admin__summary"
                aria-label="Document overview"
            >
                <article class="le-global-chatbot-admin__summary-card">
                    <span>Documents</span>

                    <strong>
                        <?php
                        echo esc_html(
                            number_format_i18n($total_documents)
                        );
                        ?>
                    </strong>
                </article>

                <article class="le-global-chatbot-admin__summary-card">
                    <span>Countries</span>

                    <strong>
                        <?php
                        echo esc_html(
                            number_format_i18n($total_countries)
                        );
                        ?>
                    </strong>
                </article>

                <article class="le-global-chatbot-admin__summary-card">
                    <span>Countries requiring action</span>

                    <strong>
                        <?php
                        echo esc_html(
                            number_format_i18n(
                                $countries_requiring_action_count
                            )
                        );
                        ?>
                    </strong>
                </article>
            </div>
        </section>
        <?php
    }

    private static function document_count_label(int $count): string
    {
        return sprintf(
            '%s document%s',
            number_format_i18n($count),
            $count === 1 ? '' : 's'
        );
    }

    /**
     * ORDER 8B, section 26 - the backend itself has no per-document
     * "this country is in conflict" flag; the documents list is
     * scanned for country_codes appearing more than once, exactly the
     * shape a legacy duplicate like Italy's takes today. Used both to
     * badge affected rows "Needs attention" and to keep those
     * countries out of the Edit/Add country dropdown entirely, rather
     * than letting an admin pick one arbitrarily and hit a backend
     * error.
     *
     * @param array<int, array<string, mixed>> $documents
     * @return array<int, string>
     */
    private static function detect_conflicted_country_codes(
        array $documents
    ): array {
        $counts = [];

        foreach ($documents as $document) {
            if (!is_array($document) || !isset($document['country_code'])) {
                continue;
            }

            $country_code = (string) $document['country_code'];

            if ($country_code === '') {
                continue;
            }

            $counts[$country_code] = ($counts[$country_code] ?? 0) + 1;
        }

        return array_keys(
            array_filter(
                $counts,
                static fn (int $count): bool => $count > 1
            )
        );
    }

    /**
     * @param array<string, mixed> $document
     * @return array{value: string, label: string, icon: string, class: string, title: string}
     */
    private static function compute_display_status(
        array $document,
        bool $has_country_conflict
    ): array {
        if ($has_country_conflict) {
            return [
                'value' => 'needs_attention',
                'label' => 'Needs attention',
                'icon' => '⚠',
                'class' => 'is-warning',
                'title' => (
                    'This country has conflicting document records.'
                ),
            ];
        }

        $status_value = isset($document['status'])
            ? (string) $document['status']
            : 'unknown';

        if ($status_value === 'indexed') {
            return [
                'value' => 'ready',
                'label' => 'Ready',
                'icon' => '✓',
                'class' => 'is-success',
                'title' => 'This document is available to the chatbot.',
            ];
        }

        return [
            'value' => 'needs_attention',
            'label' => 'Needs attention',
            'icon' => '⚠',
            'class' => 'is-warning',
            'title' => (
                $status_value === 'indexed_source_conflict'
                    ? 'Multiple source documents resolve for this country.'
                    : 'The source document is missing.'
            ),
        ];
    }

    /**
     * ORDER 8B, section 27 - a business-readable "14 Aug 2026, 14:32"
     * rendering of updated_at, never a raw ISO 8601 timestamp. Uses
     * the site's own configured timezone so it matches what an admin
     * would expect from WordPress elsewhere.
     */
    private static function format_last_updated(?string $iso): string
    {
        if ($iso === null || trim($iso) === '') {
            return '—';
        }

        try {
            $date = new DateTimeImmutable($iso);
        } catch (\Exception $exception) {
            return '—';
        }

        $timezone = wp_timezone();

        if ($timezone instanceof \DateTimeZone) {
            $date = $date->setTimezone($timezone);
        }

        return $date->format('j M Y, H:i');
    }

    public static function handle_upload(): void
    {
        self::assert_capability();

        check_admin_referer(
            self::UPLOAD_ACTION
        );

        self::raise_execution_time_limit();

        $is_ajax = (
            isset($_POST['le_global_ajax'])
            && sanitize_text_field(
                wp_unslash(
                    (string) $_POST['le_global_ajax']
                )
            ) === '1'
        );

        $replace_existing = (
            isset($_POST['replace_existing'])
            && sanitize_text_field(
                wp_unslash(
                    (string) $_POST['replace_existing']
                )
            ) === '1'
        );

        $confirm_warnings = (
            isset($_POST['confirm_warnings'])
            && sanitize_text_field(
                wp_unslash(
                    (string) $_POST['confirm_warnings']
                )
            ) === '1'
        );

        // Mission "ORDER 8E-A1"/"ORDER 8E-A2" - the two newer upload
        // decision flags, forwarded exactly like replace_existing/
        // confirm_warnings above: a fresh full resubmission of the
        // same file, never a token/session.
        $country_confirmed = (
            isset($_POST['country_confirmed'])
            && sanitize_text_field(
                wp_unslash(
                    (string) $_POST['country_confirmed']
                )
            ) === '1'
        );

        $selected_country_code = isset($_POST['selected_country_code'])
            ? sanitize_text_field(
                wp_unslash(
                    (string) $_POST['selected_country_code']
                )
            )
            : '';

        $fail = static function (
            string $message,
            int $status_code = 400,
            array $detail = []
        ) use ($is_ajax): void {
            if ($is_ajax) {
                wp_send_json_error(
                    [
                        'message' => $message,
                        'detail' => $detail,
                    ],
                    $status_code
                );
            }

            self::redirect_with_notice(
                'error',
                $message
            );
        };

        $file = $_FILES['document'] ?? null;

        [$original_filename, $file_content] = (
            self::validate_and_read_uploaded_docx($file, $fail)
        );

        [$multipart_body, $boundary] = self::build_docx_multipart_body(
            [
                'replace_existing' => $replace_existing ? 'true' : 'false',
                'confirm_warnings' => $confirm_warnings ? 'true' : 'false',
                'country_confirmed' => $country_confirmed ? 'true' : 'false',
                'selected_country_code' => $selected_country_code,
            ],
            $original_filename,
            $file_content
        );

        $result = self::request_backend(
            'POST',
            self::DOCUMENTS_PATH,
            null,
            120,
            $multipart_body,
            [
                'Content-Type' => (
                    'multipart/form-data; boundary='
                    . $boundary
                ),
            ]
        );

        if (is_wp_error($result)) {
            $fail(
                $result->get_error_message(),
                503
            );
        }

        if (
            (int) $result['status_code'] !== 201
        ) {
            $detail = (
                isset($result['body']['detail'])
                && is_array(
                    $result['body']['detail']
                )
            )
                ? $result['body']['detail']
                : [];

            $fail(
                self::extract_message(
                    $result['body'],
                    'The document could not be added.'
                ),
                (int) $result['status_code'],
                $detail
            );
        }

        $indexed_filename = isset(
            $result['body']['source_filename']
        )
            ? (string) (
                $result['body']['source_filename']
            )
            : $original_filename;

        $indexed_chunks = isset(
            $result['body']['indexed_chunks']
        )
            ? (int) (
                $result['body']['indexed_chunks']
            )
            : 0;

        $result_status = isset(
            $result['body']['status']
        )
            ? (string) $result['body']['status']
            : 'indexed';

        $success_message = sprintf(
            (
                $result_status === 'replaced'
                ? '%s replaced the previous country document '
                    . 'successfully.'
                : '%s was added successfully.'
            ),
            $indexed_filename
        );

        if ($is_ajax) {
            wp_send_json_success(
                [
                    'message' => $success_message,
                    'status' => $result_status,
                    'source_filename' => $indexed_filename,
                    'indexed_chunks' => $indexed_chunks,
                ],
                201
            );
        }

        self::redirect_with_notice(
            'success',
            $success_message
        );
    }

    /**
     * Validate one $_FILES['document']-shaped entry and return its
     * [original_filename, file_content] - shared by every handler
     * that proxies a DOCX upload to the backend. $fail is called (and
     * never returns - every real caller's $fail always ends the
     * request) for any technical problem.
     *
     * @param mixed $file
     * @return array{0: string, 1: string}
     */
    private static function validate_and_read_uploaded_docx(
        $file,
        callable $fail
    ): array {
        if (!is_array($file)) {
            $fail('No DOCX document was received.');
        }

        $upload_error = isset($file['error'])
            ? (int) $file['error']
            : UPLOAD_ERR_NO_FILE;

        if ($upload_error !== UPLOAD_ERR_OK) {
            $fail(self::upload_error_message($upload_error));
        }

        $temporary_path = isset($file['tmp_name'])
            ? (string) $file['tmp_name']
            : '';

        $original_filename = isset($file['name'])
            ? (string) $file['name']
            : '';

        if (
            $original_filename === ''
            || strtolower(
                pathinfo($original_filename, PATHINFO_EXTENSION)
            ) !== 'docx'
        ) {
            $fail('Only DOCX documents are accepted.');
        }

        if (
            $temporary_path === ''
            || !is_uploaded_file($temporary_path)
        ) {
            $fail('The uploaded file could not be validated.');
        }

        $file_content = file_get_contents($temporary_path);

        if ($file_content === false) {
            $fail('The uploaded document could not be read.');
        }

        return [$original_filename, $file_content];
    }

    /**
     * Build a multipart/form-data body from simple string fields plus
     * one "file" part - shared by every handler that proxies a DOCX
     * upload to the backend (handle_upload,
     * handle_resolve_conflict_replace), so the exact wire format is
     * defined in exactly one place.
     *
     * @param array<string, string> $fields
     * @return array{0: string, 1: string} [$body, $boundary]
     */
    private static function build_docx_multipart_body(
        array $fields,
        string $original_filename,
        string $file_content
    ): array {
        $boundary = (
            'LEGlobalBoundary'
            . str_replace('-', '', wp_generate_uuid4())
        );

        $line_break = "\r\n";

        $header_filename = str_replace(
            ['"', "\r", "\n"],
            '',
            $original_filename
        );

        $body = '';

        foreach ($fields as $name => $value) {
            $body .= '--' . $boundary . $line_break;
            $body .= (
                'Content-Disposition: form-data; name="'
                . $name
                . '"'
                . $line_break
                . $line_break
                . $value
                . $line_break
            );
        }

        $body .= '--' . $boundary . $line_break;
        $body .= (
            'Content-Disposition: form-data; name="file"; filename="'
            . $header_filename
            . '"'
            . $line_break
        );
        $body .= (
            'Content-Type: application/vnd.openxmlformats-officedocument.'
            . 'wordprocessingml.document'
            . $line_break
            . $line_break
        );
        $body .= (
            $file_content
            . $line_break
            . '--'
            . $boundary
            . '--'
            . $line_break
        );

        return [$body, $boundary];
    }

    /**
     * Mission "ORDER 8E-A2", sections 20-27 - a read-only, safe review
     * of one country's active conflict, relayed verbatim (the
     * backend's own response already excludes document_type/chunk
     * counts/SHA - only filename/year/last-updated/file size per
     * candidate, plus whether AUTO_DEDUPLICATE is available).
     */
    public static function handle_conflict_review(): void
    {
        self::assert_capability();

        check_ajax_referer(
            self::CONFLICT_REVIEW_ACTION,
            'nonce'
        );

        $country_code = self::read_country_code_for_json();

        $result = self::request_backend(
            'GET',
            self::DOCUMENTS_PATH
            . '/countries/'
            . rawurlencode($country_code)
            . '/conflict-review',
            null,
            30
        );

        self::relay_json_result(
            $result,
            'The conflict review could not be loaded.'
        );
    }

    /**
     * Mission "ORDER 8E-A2", sections 22-26 - AUTO_DEDUPLICATE and
     * CHOOSE_DOCUMENT both act directly on the existing indexed
     * records, with no file to upload - a simple url-encoded POST,
     * never multipart. REPLACE_WITH_DOCUMENT has its own dedicated
     * handler below, since it needs a file.
     */
    public static function handle_resolve_conflict(): void
    {
        self::assert_capability();

        check_ajax_referer(
            self::RESOLVE_CONFLICT_ACTION,
            'nonce'
        );

        self::raise_execution_time_limit();

        $country_code = self::read_country_code_for_json();

        $resolution_mode = isset($_POST['resolution_mode'])
            ? sanitize_text_field(
                wp_unslash((string) $_POST['resolution_mode'])
            )
            : '';

        $keep_document_id = isset($_POST['keep_document_id'])
            ? sanitize_text_field(
                wp_unslash((string) $_POST['keep_document_id'])
            )
            : '';

        if ($resolution_mode === '') {
            wp_send_json_error(
                ['message' => 'The resolution mode is required.'],
                422
            );
        }

        $fields = ['resolution_mode' => $resolution_mode];

        if ($keep_document_id !== '') {
            $fields['keep_document_id'] = $keep_document_id;
        }

        $result = self::request_backend(
            'POST',
            self::DOCUMENTS_PATH
            . '/countries/'
            . rawurlencode($country_code)
            . '/resolve-conflict',
            null,
            60,
            self::build_url_encoded_body($fields),
            [
                'Content-Type' => (
                    'application/x-www-form-urlencoded; charset=UTF-8'
                ),
            ]
        );

        self::relay_json_result(
            $result,
            'This issue could not be resolved.'
        );
    }

    /**
     * Mission "ORDER 8E-A2", section 27 - "we couldn't determine which
     * document should be used" is never a dead end: the Admin supplies
     * an authoritative DOCX, which goes through the exact same upload
     * validation flow as an ordinary upload (technical validation,
     * country confirmation, content warning if applicable) - this
     * handler only pins resolution_mode=REPLACE_WITH_DOCUMENT and the
     * target country_code server-side, reusing
     * build_docx_multipart_body/validate_and_read_uploaded_docx rather
     * than a second, parallel upload implementation.
     */
    public static function handle_resolve_conflict_replace(): void
    {
        self::assert_capability();

        check_ajax_referer(
            self::RESOLVE_CONFLICT_REPLACE_ACTION,
            'nonce'
        );

        self::raise_execution_time_limit();

        $country_code = self::read_country_code_for_json();

        $country_confirmed = (
            isset($_POST['country_confirmed'])
            && sanitize_text_field(
                wp_unslash((string) $_POST['country_confirmed'])
            ) === '1'
        );

        $confirm_warnings = (
            isset($_POST['confirm_warnings'])
            && sanitize_text_field(
                wp_unslash((string) $_POST['confirm_warnings'])
            ) === '1'
        );

        $selected_country_code = isset($_POST['selected_country_code'])
            ? sanitize_text_field(
                wp_unslash((string) $_POST['selected_country_code'])
            )
            : '';

        $fail = static function (
            string $message,
            int $status_code = 400,
            array $detail = []
        ): void {
            wp_send_json_error(
                [
                    'message' => $message,
                    'detail' => $detail,
                ],
                $status_code
            );
        };

        $file = $_FILES['document'] ?? null;

        [$original_filename, $file_content] = (
            self::validate_and_read_uploaded_docx($file, $fail)
        );

        [$multipart_body, $boundary] = self::build_docx_multipart_body(
            [
                'resolution_mode' => 'REPLACE_WITH_DOCUMENT',
                'country_confirmed' => $country_confirmed ? 'true' : 'false',
                'confirm_warnings' => $confirm_warnings ? 'true' : 'false',
                'selected_country_code' => $selected_country_code,
            ],
            $original_filename,
            $file_content
        );

        $result = self::request_backend(
            'POST',
            self::DOCUMENTS_PATH
            . '/countries/'
            . rawurlencode($country_code)
            . '/resolve-conflict',
            null,
            120,
            $multipart_body,
            [
                'Content-Type' => (
                    'multipart/form-data; boundary='
                    . $boundary
                ),
            ]
        );

        self::relay_json_result(
            $result,
            'The document could not be processed.'
        );
    }

    public static function handle_reindex(): void
    {
        self::assert_capability();

        $document_id = self::read_document_id();

        check_admin_referer(
            self::REINDEX_ACTION
            . ':'
            . $document_id
        );

        self::raise_execution_time_limit();

        $is_ajax = self::is_ajax_request();

        $result = self::request_backend(
            'POST',
            self::DOCUMENTS_PATH
            . '/'
            . rawurlencode(
                $document_id
            )
            . '/reindex',
            null,
            120
        );

        if (is_wp_error($result)) {
            self::fail_reindex_or_delete(
                $is_ajax,
                $result->get_error_message(),
                503
            );
        }

        if (
            (int) $result['status_code'] !== 200
        ) {
            self::fail_reindex_or_delete(
                $is_ajax,
                self::extract_message(
                    $result['body'],
                    'The chatbot data could not be refreshed.'
                ),
                (int) $result['status_code']
            );
        }

        $filename = isset(
            $result['body']['source_filename']
        )
            ? (string) (
                $result['body']['source_filename']
            )
            : 'The document';

        $indexed_chunks = isset(
            $result['body']['indexed_chunks']
        )
            ? (int) (
                $result['body']['indexed_chunks']
            )
            : 0;

        $success_message = sprintf(
            '%s was refreshed successfully.',
            $filename
        );

        if ($is_ajax) {
            wp_send_json_success(
                [
                    'message' => $success_message,
                    'source_filename' => $filename,
                    'indexed_chunks' => $indexed_chunks,
                ]
            );
        }

        self::redirect_with_notice(
            'success',
            $success_message
        );
    }

    public static function handle_delete(): void
    {
        self::assert_capability();

        $document_id = self::read_document_id();

        check_admin_referer(
            self::DELETE_ACTION
            . ':'
            . $document_id
        );

        self::raise_execution_time_limit();

        $is_ajax = self::is_ajax_request();

        $result = self::request_backend(
            'DELETE',
            self::DOCUMENTS_PATH
            . '/'
            . rawurlencode(
                $document_id
            ),
            null,
            90
        );

        if (is_wp_error($result)) {
            self::fail_reindex_or_delete(
                $is_ajax,
                $result->get_error_message(),
                503
            );
        }

        if (
            (int) $result['status_code'] !== 200
        ) {
            self::fail_reindex_or_delete(
                $is_ajax,
                self::extract_message(
                    $result['body'],
                    'The document could not be deleted.'
                ),
                (int) $result['status_code']
            );
        }

        $filename = isset(
            $result['body']['source_filename']
        )
            ? (string) (
                $result['body']['source_filename']
            )
            : 'The document';

        $deleted_chunks = isset(
            $result['body']['deleted_chunks']
        )
            ? (int) (
                $result['body']['deleted_chunks']
            )
            : 0;

        $success_message = self::build_delete_success_message(
            $filename,
            $deleted_chunks,
            isset($result['body']['source_cleanup_deferred'])
            && $result['body']['source_cleanup_deferred'] === true
        );

        if ($is_ajax) {
            wp_send_json_success(
                [
                    'message' => $success_message,
                    'source_filename' => $filename,
                    'deleted_chunks' => $deleted_chunks,
                ]
            );
        }

        self::redirect_with_notice(
            'success',
            $success_message
        );
    }

    /**
     * Mission "ORDER 4", section 21/22: reindex/delete gain a fetch-
     * based JSON response mode as pure progressive enhancement - the
     * native admin-post.php form submit (no le_global_ajax field,
     * e.g. JS disabled/failed) must keep working exactly as before,
     * via the unchanged redirect_with_notice() fallback.
     */
    private static function is_ajax_request(): bool
    {
        return (
            isset($_REQUEST['le_global_ajax'])
            && sanitize_text_field(
                wp_unslash(
                    (string) $_REQUEST['le_global_ajax']
                )
            ) === '1'
        );
    }

    private static function fail_reindex_or_delete(
        bool $is_ajax,
        string $message,
        int $status_code
    ): void {
        if ($is_ajax) {
            wp_send_json_error(
                [
                    'message' => $message,
                    'detail' => [],
                ],
                $status_code
            );
        }

        self::redirect_with_notice(
            'error',
            $message
        );
    }

    /**
     * A backend reindex/delete on a large document has been measured
     * at ~30s (mission "ORDER 3B"), which sits exactly at PHP's own
     * unmodified apache2handler max_execution_time (also 30s - see
     * timeout-audit.txt, measured via a real HTTP request, not the
     * CLI SAPI's misleading max_execution_time=0). Raised only inside
     * the three handlers that proxy a genuinely long operation, never
     * globally - GET-only reads (list/stats/download) keep PHP's
     * default budget since they never approach it.
     */
    private static function raise_execution_time_limit(): void
    {
        if (function_exists('set_time_limit')) {
            @set_time_limit(
                self::LONG_OPERATION_TIME_LIMIT_SECONDS
            );
        }
    }

    /**
     * Mission "ORDER 4", section 23: the client supplies only
     * document_id, never a path or key - PHP resolves the backend
     * URL server-side (X-API-Key/X-Admin-Key never reach the
     * browser) and streams the real DOCX bytes back with the
     * correct Content-Type/Content-Disposition, never as JSON/
     * base64. A plain <a href> link (no JS required) is the only
     * caller - this keeps download working with JS disabled too.
     */
    public static function handle_download(): void
    {
        self::assert_capability();

        $document_id = self::read_document_id();

        check_admin_referer(
            self::DOWNLOAD_ACTION . ':' . $document_id
        );

        $configuration = self::get_backend_configuration();

        if (is_wp_error($configuration)) {
            wp_die(
                esc_html(
                    $configuration->get_error_message()
                ),
                '',
                ['response' => 503]
            );
        }

        $backend_url = (
            untrailingslashit($configuration['url'])
            . self::DOCUMENTS_PATH
            . '/'
            . rawurlencode($document_id)
            . '/download'
        );

        $response = wp_remote_get(
            $backend_url,
            [
                'timeout' => 60,
                'headers' => [
                    'Accept' => (
                        'application/vnd.openxmlformats-'
                        . 'officedocument.wordprocessingml.document'
                    ),
                    'X-API-Key' => $configuration['api_key'],
                    'X-Admin-Key' => (
                        $configuration['admin_api_key']
                    ),
                ],
            ]
        );

        if (is_wp_error($response)) {
            wp_die(
                esc_html__(
                    'The document could not be downloaded.',
                    'le-global-chatbot'
                ),
                '',
                ['response' => 503]
            );
        }

        $status_code = wp_remote_retrieve_response_code(
            $response
        );

        if ((int) $status_code !== 200) {
            wp_die(
                esc_html__(
                    'The document could not be downloaded.',
                    'le-global-chatbot'
                ),
                '',
                ['response' => $status_code ?: 502]
            );
        }

        $body = wp_remote_retrieve_body($response);

        $content_disposition = (string) wp_remote_retrieve_header(
            $response,
            'content-disposition'
        );

        $filename = self::filename_from_content_disposition(
            $content_disposition,
            $document_id . '.docx'
        );

        // Mission "ORDER 8G-A", section 10 - the reported bug ("the
        // file downloads correctly, but a red failure notice also
        // appears") matches the classic browser-level symptom of a
        // declared Content-Length no longer matching the bytes
        // actually placed on the wire (an active output buffer
        // appending bytes after this point, or a compression layer
        // altering the byte count post-hoc) - the browser still
        // reconstructs the complete file, but flags the transfer as
        // failed. Never self-compute Content-Length for this
        // response: discard any active output buffering first, ask
        // Apache to skip compressing this one response, and let the
        // web server determine and send the real length itself.
        while (ob_get_level() > 0) {
            ob_end_clean();
        }

        if (function_exists('apache_setenv')) {
            @apache_setenv('no-gzip', '1');
        }

        nocache_headers();

        header(
            'Content-Type: application/vnd.openxmlformats-'
            . 'officedocument.wordprocessingml.document'
        );

        header(
            'Content-Disposition: attachment; filename="'
            . $filename
            . '"'
        );

        // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped
        // -- this is the raw DOCX binary body, never HTML; escaping
        // it would corrupt the file.
        echo $body;

        exit;
    }

    private static function filename_from_content_disposition(
        string $header,
        string $fallback
    ): string {
        if (
            preg_match(
                '/filename="?([^";]+)"?/i',
                $header,
                $matches
            )
            && trim($matches[1]) !== ''
        ) {
            return trim($matches[1]);
        }

        return $fallback;
    }

    /**
     * Mission "ORDER 4", section 26: a small JSON-only read endpoint
     * so the enhanced (JS) path can refresh the documents table and
     * summary cards in place after an upload/reindex/delete settles,
     * instead of a full page reload. Purely additive - render_page()
     * still renders the exact same data server-side on a normal page
     * load, via the same fetch_document_catalog()/fetch_document_
     * stats() helpers.
     */
    public static function handle_refresh(): void
    {
        self::assert_capability();

        check_ajax_referer(self::REFRESH_ACTION, 'nonce');

        [$documents, $catalog_error] = (
            self::fetch_document_catalog()
        );

        if ($catalog_error !== null) {
            wp_send_json_error(
                ['message' => $catalog_error],
                502
            );
        }

        wp_send_json_success(
            [
                'documents' => $documents,
                'stats' => self::fetch_document_stats(),
            ]
        );
    }

    /**
     * Mission "ORDER 5D": every section that really exists in one
     * document's current effective state - the Edit UI's section
     * dropdown is populated from this, never a "Create section"
     * option, since only an already-existing section may be edited
     * (see handle_update_section, and the backend's own
     * update_admin_document_section, which never creates a new
     * legal_topic).
     */
    public static function handle_list_sections(): void
    {
        self::assert_capability();

        check_ajax_referer(
            self::SECTIONS_LIST_ACTION,
            'nonce'
        );

        $document_id = self::read_document_id_for_json();

        $result = self::request_backend(
            'GET',
            self::DOCUMENTS_PATH
            . '/'
            . rawurlencode($document_id)
            . '/sections',
            null,
            30
        );

        self::relay_json_result(
            $result,
            'The sections could not be loaded.'
        );
    }

    /**
     * Mission "ORDER 5D": the current EFFECTIVE content of one
     * section - never the original DOCX paragraph text, and never
     * annotated with any DOCX/manual-edit indicator (the textarea
     * shows exactly what the chatbot would actually answer with,
     * indistinguishable from unedited content by design).
     */
    public static function handle_get_section(): void
    {
        self::assert_capability();

        check_ajax_referer(
            self::SECTION_GET_ACTION,
            'nonce'
        );

        $document_id = self::read_document_id_for_json();
        $section_id = self::read_section_id_for_json();

        $result = self::request_backend(
            'GET',
            self::DOCUMENTS_PATH
            . '/'
            . rawurlencode($document_id)
            . '/sections/'
            . rawurlencode($section_id),
            null,
            30
        );

        self::relay_json_result(
            $result,
            'The section could not be loaded.'
        );
    }

    /**
     * Mission "ORDER 5D": save a new effective content for one
     * already-existing section - a single backend mutation per call,
     * the browser's own double-click/double-submit protection is
     * enforced client-side (admin.js disables Save for the duration
     * of the request), never here, since PHP has no way to tell a
     * genuine retry from a second, distinct click. Content is
     * unslashed only, deliberately never sanitize_text_field()'d -
     * that WordPress helper strips tags AND collapses line breaks,
     * which would silently destroy the paragraph structure the
     * mission explicitly requires preserving; this is plain text
     * relayed to a JSON API, never rendered as HTML on either side of
     * this proxy, so there is nothing here for sanitize_text_field to
     * usefully protect against.
     */
    public static function handle_update_section(): void
    {
        self::assert_capability();

        check_ajax_referer(
            self::SECTION_UPDATE_ACTION,
            'nonce'
        );

        self::raise_execution_time_limit();

        $document_id = self::read_document_id_for_json();
        $section_id = self::read_section_id_for_json();

        $content = isset($_POST['content'])
            ? wp_unslash((string) $_POST['content'])
            : '';

        if (trim($content) === '') {
            wp_send_json_error(
                [
                    'message' => (
                        'The section content must not be empty.'
                    ),
                ],
                422
            );
        }

        // Mission "ORDER 8G-A" - one Save now supports content only,
        // title only, or both: an omitted (or blank) title simply
        // means no rename was requested, exactly the pre-existing
        // content-only behavior. Title, like content, is unslashed
        // only - never sanitize_text_field()'d - the backend itself
        // is the sole authority on trimming/validating it.
        $title = isset($_POST['title'])
            ? wp_unslash((string) $_POST['title'])
            : '';

        $payload = ['content' => $content];

        if (trim($title) !== '') {
            $payload['title'] = $title;
        }

        $result = self::request_backend(
            'PUT',
            self::DOCUMENTS_PATH
            . '/'
            . rawurlencode($document_id)
            . '/sections/'
            . rawurlencode($section_id),
            $payload,
            60
        );

        self::relay_json_result(
            $result,
            'The section could not be saved.'
        );
    }

    /**
     * Mission "ORDER 8G-A", section 7 - permanently remove one
     * top-level legal section from the current DOCX. The backend
     * blocks deleting the document's last remaining usable section
     * (surfaced to the browser as the section_is_last_remaining
     * business error, mapped to a friendly message in admin.js).
     */
    public static function handle_delete_section(): void
    {
        self::assert_capability();

        check_ajax_referer(
            self::SECTION_DELETE_ACTION,
            'nonce'
        );

        self::raise_execution_time_limit();

        $document_id = self::read_document_id_for_json();
        $section_id = self::read_section_id_for_json();

        $result = self::request_backend(
            'DELETE',
            self::DOCUMENTS_PATH
            . '/'
            . rawurlencode($document_id)
            . '/sections/'
            . rawurlencode($section_id),
            null,
            60
        );

        self::relay_json_result(
            $result,
            'The section could not be deleted.'
        );
    }

    /**
     * ORDER 8A-C: add a brand-new top-level legal topic to the
     * current DOCX. position is one of "beginning", "end", or
     * "after:<section_id>" - admin.js translates the user-facing
     * dropdown choice into this exact contract, never a raw enum or
     * section_id exposed as-is in the UI copy. Content is unslashed
     * only (never sanitize_text_field()'d), for the same reason
     * handle_update_section leaves it alone - preserving paragraph
     * structure and Unicode exactly as typed.
     */
    public static function handle_add_section(): void
    {
        self::assert_capability();

        check_ajax_referer(
            self::SECTION_ADD_ACTION,
            'nonce'
        );

        self::raise_execution_time_limit();

        $document_id = self::read_document_id_for_json();

        $title = isset($_POST['title'])
            ? trim(wp_unslash((string) $_POST['title']))
            : '';

        $content = isset($_POST['content'])
            ? wp_unslash((string) $_POST['content'])
            : '';

        $position = isset($_POST['position'])
            ? sanitize_text_field(
                wp_unslash((string) $_POST['position'])
            )
            : '';

        if ($title === '') {
            wp_send_json_error(
                ['message' => 'The section title must not be empty.'],
                422
            );
        }

        if (trim($content) === '') {
            wp_send_json_error(
                ['message' => 'The section content must not be empty.'],
                422
            );
        }

        if ($position === '') {
            wp_send_json_error(
                ['message' => 'The section position is invalid.'],
                422
            );
        }

        $result = self::request_backend(
            'POST',
            self::DOCUMENTS_PATH
            . '/'
            . rawurlencode($document_id)
            . '/sections',
            [
                'title' => $title,
                'content' => $content,
                'position' => $position,
            ],
            60
        );

        self::relay_json_result(
            $result,
            'The section could not be added.'
        );
    }

    /**
     * Shared JSON relay for the section endpoints above - the same
     * is_wp_error/status_code/extract_message pattern handle_refresh
     * and the older form-based handlers each already used, factored
     * out once these needed it identically.
     *
     * ORDER 8B: also forwards the backend's own structured `detail`
     * object (its `code` field, e.g. "section_already_exists" or
     * "country_document_conflict") when present, exactly like
     * handle_upload's own $fail closure already did - admin.js maps
     * known codes to a business-friendly message, never showing the
     * technical code itself.
     */
    private static function relay_json_result(
        $result,
        string $fallback_message
    ): void {
        if (is_wp_error($result)) {
            wp_send_json_error(
                ['message' => $result->get_error_message()],
                503
            );
        }

        $status_code = (int) $result['status_code'];

        if ($status_code < 200 || $status_code >= 300) {
            $detail = (
                isset($result['body']['detail'])
                && is_array($result['body']['detail'])
            )
                ? $result['body']['detail']
                : [];

            wp_send_json_error(
                [
                    'message' => self::extract_message(
                        $result['body'],
                        $fallback_message
                    ),
                    'detail' => $detail,
                ],
                $status_code
            );
        }

        wp_send_json_success($result['body']);
    }

    private static function read_document_id_for_json(): string
    {
        $document_id = isset($_REQUEST['document_id'])
            ? sanitize_text_field(
                wp_unslash(
                    (string) $_REQUEST['document_id']
                )
            )
            : '';

        if (
            !preg_match(
                '/^doc_[0-9a-f]{64}$/',
                $document_id
            )
        ) {
            wp_send_json_error(
                [
                    'message' => (
                        'The document identifier is invalid.'
                    ),
                ],
                422
            );
        }

        return $document_id;
    }

    private static function read_section_id_for_json(): string
    {
        $section_id = isset($_REQUEST['section_id'])
            ? sanitize_text_field(
                wp_unslash(
                    (string) $_REQUEST['section_id']
                )
            )
            : '';

        if ($section_id === '') {
            wp_send_json_error(
                [
                    'message' => (
                        'The section identifier is invalid.'
                    ),
                ],
                422
            );
        }

        return $section_id;
    }

    /**
     * Mission "ORDER 8E-A2" - the country-conflict endpoints are keyed
     * by country_code rather than document_id; this is only a basic
     * shape check (two letters), the backend itself is the final
     * authority on which codes are actually recognized/allowed.
     */
    private static function read_country_code_for_json(): string
    {
        $country_code = isset($_REQUEST['country_code'])
            ? sanitize_text_field(
                wp_unslash((string) $_REQUEST['country_code'])
            )
            : '';

        if (!preg_match('/^[A-Za-z]{2}$/', $country_code)) {
            wp_send_json_error(
                ['message' => 'The country code is invalid.'],
                422
            );
        }

        return strtoupper($country_code);
    }

    /**
     * @param array<string, string> $fields
     */
    private static function build_url_encoded_body(array $fields): string
    {
        return http_build_query($fields, '', '&', PHP_QUERY_RFC3986);
    }

    /**
     * @return array{0: array<int, array<string, mixed>>, 1: ?string}
     */
    private static function fetch_document_catalog(): array
    {
        $result = self::request_backend(
            'GET',
            self::DOCUMENTS_PATH,
            null,
            30
        );

        // ORDER 8B, section 6/49 - the catalog's own unavailability is
        // never something the admin can act on (unlike a section-level
        // error), and the backend's own message here has been observed
        // to name its storage technology directly (e.g. "OpenSearch
        // document catalog request failed.") - a fixed, generic
        // message is used instead of relaying it, so no future backend
        // wording change can leak technical jargon into this notice.
        if (is_wp_error($result) || (int) $result['status_code'] !== 200) {
            return [
                [],
                'The document list could not be loaded right now. Please try refreshing the page.',
            ];
        }

        if (
            !isset($result['body']['documents'])
            || !is_array($result['body']['documents'])
        ) {
            return [[], null];
        }

        $documents = array_map(
            static function ($document) {
                if (
                    !is_array($document)
                    || !isset($document['document_id'])
                    || !is_string($document['document_id'])
                ) {
                    return $document;
                }

                $document_id = $document['document_id'];

                $document['download_url'] = (
                    empty($document['source_file_present'])
                    ? null
                    : self::build_download_url($document_id)
                );

                $document['reindex_nonce'] = (
                    empty($document['source_file_present'])
                    ? null
                    : wp_create_nonce(
                        self::REINDEX_ACTION . ':' . $document_id
                    )
                );

                $document['delete_nonce'] = wp_create_nonce(
                    self::DELETE_ACTION . ':' . $document_id
                );

                return $document;
            },
            $result['body']['documents']
        );

        return [$documents, null];
    }

    private static function build_download_url(
        string $document_id
    ): string {
        return wp_nonce_url(
            add_query_arg(
                [
                    'action' => self::DOWNLOAD_ACTION,
                    'document_id' => $document_id,
                ],
                admin_url('admin-post.php')
            ),
            self::DOWNLOAD_ACTION . ':' . $document_id
        );
    }

    /**
     * @return ?array{total_documents: int, total_countries: int, status_counts: array<string, int>, countries_requiring_action: int}
     */
    private static function fetch_document_stats(): ?array
    {
        $result = self::request_backend(
            'GET',
            self::DOCUMENTS_PATH . '/stats',
            null,
            30
        );

        if (
            is_wp_error($result)
            || (int) $result['status_code'] !== 200
        ) {
            return null;
        }

        return [
            'total_documents' => isset(
                $result['body']['total_documents']
            )
                ? (int) $result['body']['total_documents']
                : 0,
            'total_countries' => isset(
                $result['body']['total_countries']
            )
                ? (int) $result['body']['total_countries']
                : 0,
            // Mission "ORDER 8E-A2", section 28 - the backend's own
            // deduplicated, country-level count: one conflict never
            // counts once per extra raw record.
            'countries_requiring_action' => isset(
                $result['body']['countries_requiring_action']
            )
                ? (int) $result['body']['countries_requiring_action']
                : 0,
            'status_counts' => isset(
                $result['body']['status_counts']
            )
            && is_array($result['body']['status_counts'])
                ? $result['body']['status_counts']
                : [],
        ];
    }

    /**
     * The delete itself always succeeded when this is called
     * (status_code=200 was already checked by the caller) -
     * $source_cleanup_deferred=true can mean either that other
     * documents still share this country (no physical file could be
     * safely retired yet) OR that the backend's own best-effort
     * backup-file cleanup failed after an already-successful,
     * committed index delete (mission "HOTFIX 0.4.9" review 2,
     * section 3) - two different causes, so the message stays
     * deliberately generic rather than naming a specific one that
     * would be wrong half the time. Either way this is never an
     * error: the delete itself is done.
     */
    private static function build_delete_success_message(
        string $filename,
        int $deleted_chunks,
        bool $source_cleanup_deferred
    ): string {
        return sprintf(
            (
                $source_cleanup_deferred
                ? '%s was deleted successfully. Cleanup of some '
                    . 'related files is still in progress.'
                : '%s was deleted successfully.'
            ),
            $filename
        );
    }

    private static function assert_capability(): void
    {
        if (
            !current_user_can(
                self::CAPABILITY
            )
        ) {
            wp_die(
                esc_html__(
                    'You are not allowed to perform this action.',
                    'le-global-chatbot'
                ),
                '',
                [
                    'response' => 403,
                ]
            );
        }
    }

    private static function read_document_id(): string
    {
        $document_id = isset(
            $_REQUEST['document_id']
        )
            ? sanitize_text_field(
                wp_unslash(
                    (string) $_REQUEST['document_id']
                )
            )
            : '';

        if (
            !preg_match(
                '/^doc_[0-9a-f]{64}$/',
                $document_id
            )
        ) {
            self::redirect_with_notice(
                'error',
                'The document identifier is invalid.'
            );
        }

        return $document_id;
    }

    private static function request_backend(
        string $method,
        string $path,
        ?array $payload,
        int $timeout,
        ?string $raw_body = null,
        array $extra_headers = []
    ) {
        $configuration = (
            self::get_backend_configuration()
        );

        if (is_wp_error($configuration)) {
            return $configuration;
        }

        $backend_url = untrailingslashit(
            $configuration['url']
        ) . $path;

        $headers = [
            'Accept' => 'application/json',
            'X-API-Key' => (
                $configuration['api_key']
            ),
            'X-Admin-Key' => (
                $configuration['admin_api_key']
            ),
        ];

        $client_ip = self::get_client_ip();

        if ($client_ip !== null) {
            $headers['X-Forwarded-For'] = (
                $client_ip
            );

            $headers['X-Real-IP'] = (
                $client_ip
            );
        }

        foreach (
            $extra_headers as $header => $value
        ) {
            $headers[
                (string) $header
            ] = (string) $value;
        }

        $arguments = [
            'method' => strtoupper(
                $method
            ),
            'timeout' => $timeout,
            'redirection' => 2,
            'headers' => $headers,
            'data_format' => 'body',
        ];

        if ($raw_body !== null) {
            $arguments['body'] = (
                $raw_body
            );
        } elseif ($payload !== null) {
            $arguments['headers']['Content-Type'] = (
                'application/json'
            );

            $arguments['body'] = wp_json_encode(
                $payload
            );
        }

        $response = wp_remote_request(
            $backend_url,
            $arguments
        );

        if (is_wp_error($response)) {
            error_log(
                sprintf(
                    '[L&E Global Chatbot] Backend request '
                    . 'failed (%s).',
                    sanitize_key(
                        (string) (
                            $response->get_error_code()
                        )
                    )
                )
            );

            return new WP_Error(
                'le_global_admin_backend_unavailable',
                'The legal document service is temporarily unavailable.'
            );
        }

        $status_code = (
            wp_remote_retrieve_response_code(
                $response
            )
        );

        $raw_response_body = (
            wp_remote_retrieve_body(
                $response
            )
        );

        if ($raw_response_body === '') {
            $decoded_body = [];
        } else {
            $decoded_body = json_decode(
                $raw_response_body,
                true
            );

            if (
                json_last_error()
                !== JSON_ERROR_NONE
                || !is_array(
                    $decoded_body
                )
            ) {
                return new WP_Error(
                    'le_global_admin_invalid_response',
                    'The legal document service returned an invalid response.'
                );
            }
        }

        return [
            'status_code' => (
                $status_code > 0
                ? $status_code
                : 502
            ),
            'body' => $decoded_body,
        ];
    }

    private static function get_backend_configuration()
    {
        $backend_url = defined(
            'LE_GLOBAL_CHATBOT_API_URL'
        )
            ? trim(
                (string) LE_GLOBAL_CHATBOT_API_URL
            )
            : '';

        $api_key = defined(
            'LE_GLOBAL_CHATBOT_API_KEY'
        )
            ? trim(
                (string) LE_GLOBAL_CHATBOT_API_KEY
            )
            : '';

        $admin_api_key = defined(
            'LE_GLOBAL_CHATBOT_ADMIN_API_KEY'
        )
            ? trim(
                (string) (
                    LE_GLOBAL_CHATBOT_ADMIN_API_KEY
                )
            )
            : '';

        if (
            $backend_url === ''
            || $api_key === ''
            || $admin_api_key === ''
        ) {
            return new WP_Error(
                'le_global_admin_not_configured',
                'The WordPress document administration integration is not configured.'
            );
        }

        return [
            'url' => esc_url_raw(
                $backend_url
            ),
            'api_key' => $api_key,
            'admin_api_key' => (
                $admin_api_key
            ),
        ];
    }

    private static function extract_message(
        array $body,
        string $fallback
    ): string {
        if (
            isset($body['detail'])
            && is_array(
                $body['detail']
            )
            && isset(
                $body['detail']['message']
            )
            && is_string(
                $body['detail']['message']
            )
            && trim(
                $body['detail']['message']
            ) !== ''
        ) {
            return trim(
                $body['detail']['message']
            );
        }

        if (
            isset($body['detail'])
            && is_string(
                $body['detail']
            )
            && trim(
                $body['detail']
            ) !== ''
        ) {
            return trim(
                $body['detail']
            );
        }

        // FastAPI's own automatic request-validation errors (a
        // missing/malformed multipart field, caught before the admin
        // router's business logic ever runs) shape `detail` as a
        // list of {loc, msg, type} objects, never a string or a
        // {message: ...} object - silently falling through to the
        // generic fallback here previously discarded a real,
        // structured reason (mission "HOTFIX 0.4.9" review).
        if (
            isset($body['detail'])
            && is_array($body['detail'])
            && isset($body['detail'][0])
            && is_array($body['detail'][0])
            && isset($body['detail'][0]['msg'])
            && is_string($body['detail'][0]['msg'])
            && trim($body['detail'][0]['msg']) !== ''
        ) {
            return trim($body['detail'][0]['msg']);
        }

        if (
            isset($body['message'])
            && is_string(
                $body['message']
            )
            && trim(
                $body['message']
            ) !== ''
        ) {
            return trim(
                $body['message']
            );
        }

        return $fallback;
    }

    private static function get_client_ip(): ?string
    {
        $remote_address = (
            $_SERVER['REMOTE_ADDR']
            ?? ''
        );

        if (!is_string($remote_address)) {
            return null;
        }

        $remote_address = trim(
            $remote_address
        );

        if (
            filter_var(
                $remote_address,
                FILTER_VALIDATE_IP
            ) === false
        ) {
            return null;
        }

        return $remote_address;
    }

    /**
     * The Edit/Add UI's country dropdown, built exclusively from the
     * real indexed catalog (mission "ORDER 5D", section 2) - never
     * the static 34-country allowlist, and never a country the
     * catalog does not currently have a document for. A country in
     * conflict (ORDER 8B, section 26) is excluded entirely here
     * rather than offered and then failing on selection - the
     * Documents table row is the only place that state is explained.
     * Sorted stably by display name (PHP's usort has been a stable
     * sort since 8.0, so this needs no manual tie-break) - two
     * countries never swap places between renders just because the
     * catalog happened to list them in a different order.
     *
     * Mission "ORDER 8E-A2", section 30 - a conflicted country must
     * never silently disappear from Add/Edit with no explanation; this
     * finds any one document for that country purely to display its
     * human-readable name next to the explanatory message and Review
     * button.
     *
     * @param array<int, array<string, mixed>> $documents
     */
    private static function find_document_by_country_code(
        array $documents,
        string $country_code
    ): ?array {
        foreach ($documents as $document) {
            if (
                is_array($document)
                && isset($document['country_code'])
                && (string) $document['country_code'] === $country_code
            ) {
                return $document;
            }
        }

        return null;
    }

    /**
     * @param array<int, array<string, mixed>> $documents
     * @param array<int, string> $conflicted_country_codes
     * @return array<int, array{document_id: string, country: string, country_code: string}>
     */
    private static function sorted_documents_for_edit(
        array $documents,
        array $conflicted_country_codes = []
    ): array {
        $entries = [];

        foreach ($documents as $document) {
            if (!is_array($document)) {
                continue;
            }

            $document_id = isset($document['document_id'])
                ? (string) $document['document_id']
                : '';

            if ($document_id === '') {
                continue;
            }

            $country_code = isset($document['country_code'])
                ? (string) $document['country_code']
                : '';

            if (
                $country_code !== ''
                && in_array(
                    $country_code,
                    $conflicted_country_codes,
                    true
                )
            ) {
                continue;
            }

            $entries[] = [
                'document_id' => $document_id,
                'country' => isset($document['country'])
                    ? (string) $document['country']
                    : '',
                'country_code' => $country_code,
            ];
        }

        usort(
            $entries,
            static fn (array $a, array $b): int => strcasecmp(
                $a['country'],
                $b['country']
            )
        );

        return $entries;
    }

    private static function upload_error_message(
        int $error_code
    ): string {
        return match ($error_code) {
            UPLOAD_ERR_INI_SIZE,
            UPLOAD_ERR_FORM_SIZE => (
                'The uploaded DOCX exceeds the permitted size.'
            ),

            UPLOAD_ERR_PARTIAL => (
                'The document was only partially uploaded.'
            ),

            UPLOAD_ERR_NO_FILE => (
                'No DOCX document was selected.'
            ),

            UPLOAD_ERR_NO_TMP_DIR => (
                'The WordPress temporary upload directory is unavailable.'
            ),

            UPLOAD_ERR_CANT_WRITE => (
                'WordPress could not write the uploaded document.'
            ),

            UPLOAD_ERR_EXTENSION => (
                'A server extension blocked the upload.'
            ),

            default => (
                'The document upload failed.'
            ),
        };
    }

    private static function render_notice(): void
    {
        $notice_type = isset(
            $_GET['le_global_notice']
        )
            ? sanitize_key(
                wp_unslash(
                    (string) (
                        $_GET['le_global_notice']
                    )
                )
            )
            : '';

        $message = isset(
            $_GET['le_global_message']
        )
            ? sanitize_text_field(
                wp_unslash(
                    (string) (
                        $_GET['le_global_message']
                    )
                )
            )
            : '';

        if (
            $message === ''
            || !in_array(
                $notice_type,
                [
                    'success',
                    'error',
                ],
                true
            )
        ) {
            return;
        }

        $notice_class = (
            $notice_type === 'success'
            ? 'notice-success'
            : 'notice-error'
        );

        ?>
        <div
            class="notice <?php
                echo esc_attr(
                    $notice_class
                );
            ?> is-dismissible"
        >
            <p>
                <?php
                echo esc_html(
                    $message
                );
                ?>
            </p>
        </div>
        <script>
            ( function () {
                if ( ! window.history || ! window.history.replaceState ) {
                    return;
                }
                var url = new URL( window.location.href );
                url.searchParams.delete( 'le_global_notice' );
                url.searchParams.delete( 'le_global_message' );
                window.history.replaceState( null, '', url.toString() );
            } )();
        </script>
        <?php
    }

    private static function redirect_with_notice(
        string $type,
        string $message
    ): void {
        $redirect_url = add_query_arg(
            [
                'page' => self::PAGE_SLUG,
                'le_global_notice' => (
                    $type === 'success'
                    ? 'success'
                    : 'error'
                ),
                'le_global_message' => (
                    $message
                ),
            ],
            admin_url(
                'admin.php'
            )
        );

        wp_safe_redirect(
            $redirect_url
        );

        exit;
    }
}