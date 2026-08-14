<?php
/**
 * WordPress administration interface for L&E Global documents.
 */

if (!defined('ABSPATH')) {
    exit;
}

final class LE_Global_Chatbot_Admin
{
    private const VERSION = '0.4.11';

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

    private const SECTION_RESTORE_ACTION = (
        'le_global_chatbot_restore_section'
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
            'admin_post_' . self::SECTION_RESTORE_ACTION,
            [self::class, 'handle_restore_section']
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

        $total_chunks = 0;
        $missing_sources = 0;

        foreach ($documents as $document) {
            if (!is_array($document)) {
                continue;
            }

            $total_chunks += isset(
                $document['chunk_count']
            )
                ? (int) $document['chunk_count']
                : 0;

            if (
                empty(
                    $document['source_file_present']
                )
            ) {
                $missing_sources++;
            }
        }

        ?>
        <div class="wrap le-global-chatbot-admin">
            <header class="le-global-chatbot-admin__header">
                <div>
                    <p class="le-global-chatbot-admin__eyebrow">
                        L&amp;E Global
                    </p>

                    <h1>
                        Legal document administration
                    </h1>

                    <p class="le-global-chatbot-admin__description">
                        Upload, validate, index and maintain the
                        employment-law documents used by the chatbot.
                    </p>
                </div>

                <a
                    class="button"
                    href="<?php
                        echo esc_url(
                            admin_url(
                                'admin.php?page='
                                . self::PAGE_SLUG
                            )
                        );
                    ?>"
                >
                    Refresh
                </a>
            </header>

            <section
                id="le-global-chatbot-summary"
                class="le-global-chatbot-admin__summary"
                aria-label="Document summary"
            >
                <article
                    class="le-global-chatbot-admin__summary-card"
                >
                    <span>Indexed documents</span>

                    <strong>
                        <?php
                        echo esc_html(
                            number_format_i18n(
                                $total_documents
                            )
                        );
                        ?>
                    </strong>
                </article>

                <article
                    class="le-global-chatbot-admin__summary-card"
                >
                    <span>Countries</span>

                    <strong>
                        <?php
                        echo esc_html(
                            number_format_i18n(
                                $total_countries
                            )
                        );
                        ?>
                    </strong>
                </article>

                <article
                    class="le-global-chatbot-admin__summary-card"
                >
                    <span>Indexed chunks</span>

                    <strong>
                        <?php
                        echo esc_html(
                            number_format_i18n(
                                $total_chunks
                            )
                        );
                        ?>
                    </strong>
                </article>

                <article
                    class="le-global-chatbot-admin__summary-card"
                >
                    <span>Source issues</span>

                    <strong>
                        <?php
                        echo esc_html(
                            number_format_i18n(
                                $missing_sources
                            )
                        );
                        ?>
                    </strong>
                </article>
            </section>

            <section class="le-global-chatbot-admin__panel">
                <div class="le-global-chatbot-admin__panel-header">
                    <div>
                        <h2>Upload a DOCX document</h2>

                        <p>
                            The document will be validated, split into
                            legal chunks and indexed immediately.
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

                    <div class="le-global-chatbot-admin__upload-field">
                        <label for="le-global-document">
                            Legal document(s)
                        </label>

                        <input
                            id="le-global-document"
                            type="file"
                            name="document"
                            accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                            multiple
                            required
                        >

                        <p class="description">
                            DOCX format only. Maximum upload size: 25 MB
                            per file. Select multiple files to queue
                            them; at most 2 are uploaded at a time.
                            Replacing an existing country requires
                            confirmation.
                        </p>
                    </div>

                    <button
                        type="submit"
                        class="button button-primary"
                    >
                        Upload and index
                    </button>
                </form>

                <div
                    id="le-global-chatbot-queue"
                    class="le-global-chatbot-admin__queue"
                    aria-live="polite"
                ></div>
            </section>

            <section class="le-global-chatbot-admin__panel">
                <div class="le-global-chatbot-admin__panel-header">
                    <div>
                        <h2>Edit a section</h2>

                        <p>
                            Edit the current effective content of one
                            legal topic for an already-indexed country.
                            This never creates a new section.
                        </p>
                    </div>
                </div>

                <div
                    id="le-global-chatbot-edit"
                    class="le-global-chatbot-admin__edit"
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
                    data-section-restore-action="<?php
                        echo esc_attr(self::SECTION_RESTORE_ACTION);
                    ?>"
                    data-section-restore-nonce="<?php
                        echo esc_attr(
                            wp_create_nonce(
                                self::SECTION_RESTORE_ACTION
                            )
                        );
                    ?>"
                >
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
                                    $documents
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

                    <div class="le-global-chatbot-admin__edit-field">
                        <label for="le-global-edit-section">
                            Section
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
                        <label for="le-global-edit-content">
                            Content
                        </label>

                        <textarea
                            id="le-global-edit-content"
                            class="le-global-chatbot-admin__edit-textarea"
                            disabled
                        ></textarea>
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
                            id="le-global-edit-restore"
                            class="button"
                            disabled
                        >
                            Restore from document
                        </button>

                        <button
                            type="button"
                            id="le-global-edit-save"
                            class="button button-primary"
                            disabled
                        >
                            Save changes
                        </button>
                    </div>
                </div>
            </section>

            <section class="le-global-chatbot-admin__panel">
                <div class="le-global-chatbot-admin__panel-header">
                    <div>
                        <h2>Indexed documents</h2>

                        <p>
                            Each row represents one source document
                            currently available to the chatbot.
                        </p>
                    </div>
                </div>

                <div id="le-global-chatbot-documents">
                <?php if ($catalog_error !== null) : ?>
                    <div class="notice notice-error inline">
                        <p>
                            <?php
                            echo esc_html(
                                $catalog_error
                            );
                            ?>
                        </p>
                    </div>
                <?php elseif (!$documents) : ?>
                    <div
                        class="le-global-chatbot-admin__empty"
                    >
                        No indexed document is currently available.
                    </div>
                <?php else : ?>
                    <div
                        class="le-global-chatbot-admin__table-container"
                    >
                        <table
                            class="widefat striped le-global-chatbot-admin__table"
                        >
                            <thead>
                                <tr>
                                    <th scope="col">Country</th>
                                    <th scope="col">Source file</th>
                                    <th scope="col">Year</th>
                                    <th scope="col">Chunks</th>
                                    <th scope="col">Status</th>
                                    <th scope="col">Actions</th>
                                </tr>
                            </thead>

                            <tbody>
                                <?php
                                foreach (
                                    $documents as $document
                                ) :
                                    if (
                                        !is_array(
                                            $document
                                        )
                                    ) {
                                        continue;
                                    }

                                    $document_id = isset(
                                        $document['document_id']
                                    )
                                        ? (string) (
                                            $document['document_id']
                                        )
                                        : '';

                                    $country = isset(
                                        $document['country']
                                    )
                                        ? (string) (
                                            $document['country']
                                        )
                                        : '';

                                    $country_code = isset(
                                        $document['country_code']
                                    )
                                        ? (string) (
                                            $document['country_code']
                                        )
                                        : '';

                                    $source_filename = isset(
                                        $document['source_filename']
                                    )
                                        ? (string) (
                                            $document['source_filename']
                                        )
                                        : '';

                                    $reference_year = isset(
                                        $document['reference_year']
                                    )
                                        ? (int) (
                                            $document['reference_year']
                                        )
                                        : 0;

                                    $chunk_count = isset(
                                        $document['chunk_count']
                                    )
                                        ? (int) (
                                            $document['chunk_count']
                                        )
                                        : 0;

                                    $source_present = !empty(
                                        $document[
                                            'source_file_present'
                                        ]
                                    );

                                    $status_value = isset(
                                        $document['status']
                                    )
                                        ? (string) (
                                            $document['status']
                                        )
                                        : 'unknown';

                                    $status_label = match (
                                        $status_value
                                    ) {
                                        'indexed' => 'Indexed',
                                        'indexed_source_conflict' => (
                                            'Source conflict'
                                        ),
                                        'indexed_source_missing' => (
                                            'Source missing'
                                        ),
                                        default => (
                                            $source_present
                                            ? 'Indexed'
                                            : 'Source unavailable'
                                        ),
                                    };

                                    $status_class = (
                                        $status_value === 'indexed'
                                        ? 'is-success'
                                        : 'is-warning'
                                    );

                                    $source_problem_title = (
                                        $status_value
                                        === 'indexed_source_conflict'
                                        ? (
                                            'Multiple source DOCX files '
                                            . 'resolve for this country.'
                                        )
                                        : 'The source DOCX is missing.'
                                    );
                                    ?>
                                    <tr>
                                        <td>
                                            <strong>
                                                <?php
                                                echo esc_html(
                                                    $country
                                                );
                                                ?>
                                            </strong>

                                            <?php
                                            if (
                                                $country_code !== ''
                                            ) :
                                                ?>
                                                <span
                                                    class="le-global-chatbot-admin__country-code"
                                                >
                                                    <?php
                                                    echo esc_html(
                                                        $country_code
                                                    );
                                                    ?>
                                                </span>
                                            <?php endif; ?>
                                        </td>

                                        <td>
                                            <span
                                                class="le-global-chatbot-admin__filename"
                                            >
                                                <?php
                                                echo esc_html(
                                                    $source_filename
                                                );
                                                ?>
                                            </span>

                                            <code
                                                class="le-global-chatbot-admin__document-id"
                                                title="<?php
                                                    echo esc_attr(
                                                        $document_id
                                                    );
                                                ?>"
                                            >
                                                <?php
                                                echo esc_html(
                                                    self::shorten_identifier(
                                                        $document_id
                                                    )
                                                );
                                                ?>
                                            </code>
                                        </td>

                                        <td>
                                            <?php
                                            echo $reference_year > 0
                                                ? esc_html(
                                                    (string) (
                                                        $reference_year
                                                    )
                                                )
                                                : '—';
                                            ?>
                                        </td>

                                        <td>
                                            <?php
                                            echo esc_html(
                                                number_format_i18n(
                                                    $chunk_count
                                                )
                                            );
                                            ?>
                                        </td>

                                        <td>
                                            <span
                                                class="le-global-chatbot-admin__status <?php
                                                    echo esc_attr(
                                                        $status_class
                                                    );
                                                ?>"
                                                title="<?php
                                                    echo esc_attr(
                                                        $status_value
                                                    );
                                                ?>"
                                            >
                                                <?php
                                                echo esc_html(
                                                    $status_label
                                                );
                                                ?>
                                            </span>
                                        </td>

                                        <td>
                                            <div
                                                class="le-global-chatbot-admin__actions"
                                            >
                                                <?php if ($source_present) : ?>
                                                    <a
                                                        class="button"
                                                        href="<?php
                                                            echo esc_url(
                                                                self::build_download_url(
                                                                    $document_id
                                                                )
                                                            );
                                                        ?>"
                                                    >
                                                        Download
                                                    </a>
                                                <?php endif; ?>

                                                <?php
                                                if (
                                                    $source_present
                                                ) :
                                                    ?>
                                                    <form
                                                        method="post"
                                                        action="<?php
                                                            echo esc_url(
                                                                admin_url(
                                                                    'admin-post.php'
                                                                )
                                                            );
                                                        ?>"
                                                        data-reindex-form
                                                    >
                                                        <input
                                                            type="hidden"
                                                            name="action"
                                                            value="<?php
                                                                echo esc_attr(
                                                                    self::REINDEX_ACTION
                                                                );
                                                            ?>"
                                                        >

                                                        <input
                                                            type="hidden"
                                                            name="document_id"
                                                            value="<?php
                                                                echo esc_attr(
                                                                    $document_id
                                                                );
                                                            ?>"
                                                        >

                                                        <?php
                                                        wp_nonce_field(
                                                            self::REINDEX_ACTION
                                                            . ':'
                                                            . $document_id
                                                        );
                                                        ?>

                                                        <button
                                                            type="submit"
                                                            class="button"
                                                        >
                                                            Reindex
                                                        </button>
                                                    </form>
                                                <?php else : ?>
                                                    <button
                                                        type="button"
                                                        class="button"
                                                        disabled
                                                        title="<?php
                                                    echo esc_attr(
                                                        $source_problem_title
                                                    );
                                                ?>"
                                                    >
                                                        Reindex
                                                    </button>
                                                <?php endif; ?>

                                                <form
                                                    method="post"
                                                    action="<?php
                                                        echo esc_url(
                                                            admin_url(
                                                                'admin-post.php'
                                                            )
                                                        );
                                                    ?>"
                                                    data-confirm-delete
                                                    data-document-name="<?php
                                                        echo esc_attr(
                                                            $source_filename
                                                        );
                                                    ?>"
                                                >
                                                    <input
                                                        type="hidden"
                                                        name="action"
                                                        value="<?php
                                                            echo esc_attr(
                                                                self::DELETE_ACTION
                                                            );
                                                        ?>"
                                                    >

                                                    <input
                                                        type="hidden"
                                                        name="document_id"
                                                        value="<?php
                                                            echo esc_attr(
                                                                $document_id
                                                            );
                                                        ?>"
                                                    >

                                                    <?php
                                                    wp_nonce_field(
                                                        self::DELETE_ACTION
                                                        . ':'
                                                        . $document_id
                                                    );
                                                    ?>

                                                    <button
                                                        type="submit"
                                                        class="button button-link-delete"
                                                    >
                                                        Delete
                                                    </button>
                                                </form>
                                            </div>
                                        </td>
                                    </tr>
                                <?php endforeach; ?>
                            </tbody>
                        </table>
                    </div>
                <?php endif; ?>
                </div>
            </section>
        </div>
        <?php
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

        if (!is_array($file)) {
            $fail(
                'No DOCX document was received.'
            );
        }

        $upload_error = isset(
            $file['error']
        )
            ? (int) $file['error']
            : UPLOAD_ERR_NO_FILE;

        if ($upload_error !== UPLOAD_ERR_OK) {
            $fail(
                self::upload_error_message(
                    $upload_error
                )
            );
        }

        $temporary_path = isset(
            $file['tmp_name']
        )
            ? (string) $file['tmp_name']
            : '';

        $original_filename = isset(
            $file['name']
        )
            ? (string) $file['name']
            : '';

        if (
            $original_filename === ''
            || strtolower(
                pathinfo(
                    $original_filename,
                    PATHINFO_EXTENSION
                )
            ) !== 'docx'
        ) {
            $fail(
                'Only DOCX documents are accepted.'
            );
        }

        if (
            $temporary_path === ''
            || !is_uploaded_file(
                $temporary_path
            )
        ) {
            $fail(
                'The uploaded file could not be validated.'
            );
        }

        $file_content = file_get_contents(
            $temporary_path
        );

        if ($file_content === false) {
            $fail(
                'The uploaded document could not be read.'
            );
        }

        $boundary = (
            'LEGlobalBoundary'
            . str_replace(
                '-',
                '',
                wp_generate_uuid4()
            )
        );

        $line_break = "\r\n";

        $header_filename = str_replace(
            [
                '"',
                "\r",
                "\n",
            ],
            '',
            $original_filename
        );

        $multipart_body = (
            '--'
            . $boundary
            . $line_break
        );

        $multipart_body .= (
            'Content-Disposition: form-data; '
            . 'name="replace_existing"'
            . $line_break
            . $line_break
            . (
                $replace_existing
                ? 'true'
                : 'false'
            )
            . $line_break
        );

        $multipart_body .= (
            '--'
            . $boundary
            . $line_break
        );

        $multipart_body .= (
            'Content-Disposition: form-data; '
            . 'name="confirm_warnings"'
            . $line_break
            . $line_break
            . (
                $confirm_warnings
                ? 'true'
                : 'false'
            )
            . $line_break
        );

        $multipart_body .= (
            '--'
            . $boundary
            . $line_break
        );

        $multipart_body .= (
            'Content-Disposition: form-data; '
            . 'name="file"; filename="'
            . $header_filename
            . '"'
            . $line_break
        );

        $multipart_body .= (
            'Content-Type: '
            . 'application/vnd.openxmlformats-officedocument.'
            . 'wordprocessingml.document'
            . $line_break
            . $line_break
        );

        $multipart_body .= (
            $file_content
            . $line_break
            . '--'
            . $boundary
            . '--'
            . $line_break
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
                    'The document could not be indexed.'
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
                    . 'successfully with %s chunks.'
                : '%s was indexed successfully with %s chunks.'
            ),
            $indexed_filename,
            number_format_i18n(
                $indexed_chunks
            )
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
                    'The document could not be reindexed.'
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
            '%s was reindexed successfully with %s chunks.',
            $filename,
            number_format_i18n(
                $indexed_chunks
            )
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

        header(
            'Content-Length: ' . strlen($body)
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

        $result = self::request_backend(
            'PUT',
            self::DOCUMENTS_PATH
            . '/'
            . rawurlencode($document_id)
            . '/sections/'
            . rawurlencode($section_id),
            ['content' => $content],
            60
        );

        self::relay_json_result(
            $result,
            'The section could not be saved.'
        );
    }

    /**
     * Discard any persisted Edit for one section and restore it to
     * the current source DOCX's own content (mission "ORDER 7C") -
     * the only supported path back to the DOCX for a single section,
     * never a full document Replace/Delete (which would also discard
     * every OTHER section's own edits). Takes no content - the
     * backend derives the restored content entirely from the
     * document's own current source file.
     */
    public static function handle_restore_section(): void
    {
        self::assert_capability();

        check_ajax_referer(
            self::SECTION_RESTORE_ACTION,
            'nonce'
        );

        self::raise_execution_time_limit();

        $document_id = self::read_document_id_for_json();
        $section_id = self::read_section_id_for_json();

        $result = self::request_backend(
            'POST',
            self::DOCUMENTS_PATH
            . '/'
            . rawurlencode($document_id)
            . '/sections/'
            . rawurlencode($section_id)
            . '/restore',
            null,
            60
        );

        self::relay_json_result(
            $result,
            'The section could not be restored.'
        );
    }

    /**
     * Shared JSON relay for the four section endpoints above - the
     * same is_wp_error/status_code/extract_message pattern
     * handle_refresh and the older form-based handlers each already
     * used, factored out once these four needed it identically.
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
            wp_send_json_error(
                [
                    'message' => self::extract_message(
                        $result['body'],
                        $fallback_message
                    ),
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

        if (is_wp_error($result)) {
            return [[], $result->get_error_message()];
        }

        if ((int) $result['status_code'] !== 200) {
            return [
                [],
                self::extract_message(
                    $result['body'],
                    'The document catalog could not be loaded.'
                ),
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
     * @return ?array{total_documents: int, total_countries: int, status_counts: array<string, int>}
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
                ? '%s was deleted successfully. %s indexed '
                    . 'chunks were removed. Source-file cleanup '
                    . 'is deferred.'
                : '%s was deleted successfully. %s indexed '
                    . 'chunks were removed.'
            ),
            $filename,
            number_format_i18n($deleted_chunks)
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
     * The Edit UI's country dropdown, built exclusively from the real
     * indexed catalog (mission "ORDER 5D", section 2) - never the
     * static 34-country allowlist, and never a country the catalog
     * does not currently have a document for. Sorted stably by
     * display name (PHP's usort has been a stable sort since 8.0, so
     * this needs no manual tie-break) - two countries never swap
     * places between renders just because the catalog happened to
     * list them in a different order.
     *
     * @param array<int, array<string, mixed>> $documents
     * @return array<int, array{document_id: string, country: string, country_code: string}>
     */
    private static function sorted_documents_for_edit(
        array $documents
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

            $entries[] = [
                'document_id' => $document_id,
                'country' => isset($document['country'])
                    ? (string) $document['country']
                    : '',
                'country_code' => isset(
                    $document['country_code']
                )
                    ? (string) $document['country_code']
                    : '',
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

    private static function shorten_identifier(
        string $identifier
    ): string {
        if (strlen($identifier) <= 22) {
            return $identifier;
        }

        return (
            substr(
                $identifier,
                0,
                12
            )
            . '…'
            . substr(
                $identifier,
                -8
            )
        );
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