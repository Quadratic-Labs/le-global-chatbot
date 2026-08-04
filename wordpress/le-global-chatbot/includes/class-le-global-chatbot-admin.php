<?php
/**
 * WordPress administration interface for L&E Global documents.
 */

if (!defined('ABSPATH')) {
    exit;
}

final class LE_Global_Chatbot_Admin
{
    private const VERSION = '0.4.2';

    private const PAGE_SLUG = 'le-global-chatbot';

    private const CAPABILITY = 'manage_options';

    private const DOCUMENTS_PATH = '/api/v1/admin/documents';

    private const UPLOAD_ACTION = (
        'le_global_chatbot_upload_document'
    );

    private const REINDEX_ACTION = (
        'le_global_chatbot_reindex_document'
    );

    private const DELETE_ACTION = (
        'le_global_chatbot_delete_document'
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

        $result = self::request_backend(
            'GET',
            self::DOCUMENTS_PATH,
            null,
            30
        );

        $documents = [];
        $catalog_error = null;

        if (is_wp_error($result)) {
            $catalog_error = (
                $result->get_error_message()
            );
        } elseif (
            (int) $result['status_code'] !== 200
        ) {
            $catalog_error = self::extract_message(
                $result['body'],
                'The document catalog could not be loaded.'
            );
        } elseif (
            isset($result['body']['documents'])
            && is_array(
                $result['body']['documents']
            )
        ) {
            $documents = (
                $result['body']['documents']
            );
        }

        $total_documents = count(
            $documents
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
                    <span>Missing source files</span>

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
                            Legal document
                        </label>

                        <input
                            id="le-global-document"
                            type="file"
                            name="document"
                            accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                            required
                        >

                        <p class="description">
                            DOCX format only. The backend controls the
                            maximum permitted file size.
                        </p>
                    </div>

                    <button
                        type="submit"
                        class="button button-primary"
                    >
                        Upload and index
                    </button>
                </form>
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

                                    $status_label = (
                                        $source_present
                                        ? 'Indexed'
                                        : 'Source missing'
                                    );

                                    $status_class = (
                                        $source_present
                                        ? 'is-success'
                                        : 'is-warning'
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
                                                        title="The source DOCX is missing."
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

        $file = $_FILES['document'] ?? null;

        if (!is_array($file)) {
            self::redirect_with_notice(
                'error',
                'No DOCX document was received.'
            );
        }

        $upload_error = isset(
            $file['error']
        )
            ? (int) $file['error']
            : UPLOAD_ERR_NO_FILE;

        if ($upload_error !== UPLOAD_ERR_OK) {
            self::redirect_with_notice(
                'error',
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
            self::redirect_with_notice(
                'error',
                'Only DOCX documents are accepted.'
            );
        }

        if (
            $temporary_path === ''
            || !is_uploaded_file(
                $temporary_path
            )
        ) {
            self::redirect_with_notice(
                'error',
                'The uploaded file could not be validated.'
            );
        }

        $file_content = file_get_contents(
            $temporary_path
        );

        if ($file_content === false) {
            self::redirect_with_notice(
                'error',
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

        // The arbitrary original filename (accents, spaces, parentheses,
        // dashes, underscores all included) is sent to the backend
        // exactly as received - never WordPress's own sanitize_file_name(),
        // which would strip parentheses and collapse spaces into hyphens.
        // The backend is the sole authority on filename safety and on
        // country/year/document identity, all derived from the DOCX
        // content itself (mission "CONTINUATION PATCH 0.4.3"). Only the
        // three characters that would break this HTTP header's own
        // syntax are removed here - a transport-encoding concern, not a
        // business-format one.
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
            self::redirect_with_notice(
                'error',
                $result->get_error_message()
            );
        }

        if (
            (int) $result['status_code'] !== 201
        ) {
            self::redirect_with_notice(
                'error',
                self::extract_message(
                    $result['body'],
                    'The document could not be indexed.'
                )
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

        self::redirect_with_notice(
            'success',
            sprintf(
                '%s was indexed successfully with %s chunks.',
                $indexed_filename,
                number_format_i18n(
                    $indexed_chunks
                )
            )
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
            self::redirect_with_notice(
                'error',
                $result->get_error_message()
            );
        }

        if (
            (int) $result['status_code'] !== 200
        ) {
            self::redirect_with_notice(
                'error',
                self::extract_message(
                    $result['body'],
                    'The document could not be reindexed.'
                )
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

        self::redirect_with_notice(
            'success',
            sprintf(
                '%s was reindexed successfully with %s chunks.',
                $filename,
                number_format_i18n(
                    $indexed_chunks
                )
            )
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
            self::redirect_with_notice(
                'error',
                $result->get_error_message()
            );
        }

        if (
            (int) $result['status_code'] !== 200
        ) {
            self::redirect_with_notice(
                'error',
                self::extract_message(
                    $result['body'],
                    'The document could not be deleted.'
                )
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

        self::redirect_with_notice(
            'success',
            sprintf(
                '%s was deleted successfully. %s indexed chunks were removed.',
                $filename,
                number_format_i18n(
                    $deleted_chunks
                )
            )
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
            $_POST['document_id']
        )
            ? sanitize_text_field(
                wp_unslash(
                    (string) $_POST['document_id']
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