<?php
/**
 * Plugin Name: L&E Global Chatbot
 * Description: Secure WordPress integration for the L&E Global employment law chatbot.
 * Version: 0.2.0
 * Author: Quadratic Labs
 * Text Domain: le-global-chatbot
 */

if (!defined('ABSPATH')) {
    exit;
}

final class LE_Global_Chatbot_Plugin
{
    private const VERSION = '0.2.0';

    private const REST_NAMESPACE = 'le-global-chatbot/v1';

    private const SHORTCODE = 'le_global_chatbot';

    private const BACKEND_CONFIG_PATH = (
        '/api/v1/frontend-config'
    );

    private const BACKEND_CHAT_PATH = (
        '/api/v1/chat'
    );

    public static function init(): void
    {
        add_action(
            'rest_api_init',
            [self::class, 'register_rest_routes']
        );

        add_action(
            'wp_enqueue_scripts',
            [self::class, 'register_assets']
        );

        add_shortcode(
            self::SHORTCODE,
            [self::class, 'render_shortcode']
        );
    }

    public static function register_rest_routes(): void
    {
        register_rest_route(
            self::REST_NAMESPACE,
            '/config',
            [
                'methods' => WP_REST_Server::READABLE,
                'callback' => [
                    self::class,
                    'get_frontend_config',
                ],
                'permission_callback' => (
                    '__return_true'
                ),
            ]
        );

        register_rest_route(
            self::REST_NAMESPACE,
            '/chat',
            [
                'methods' => WP_REST_Server::CREATABLE,
                'callback' => [
                    self::class,
                    'submit_chat_question',
                ],
                'permission_callback' => (
                    '__return_true'
                ),
                'args' => [
                    'question' => [
                        'required' => true,
                        'type' => 'string',
                    ],
                    'country_codes' => [
                        'required' => false,
                        'type' => 'array',
                    ],
                    'legal_topics' => [
                        'required' => false,
                        'type' => 'array',
                    ],
                    'subsections' => [
                        'required' => false,
                        'type' => 'array',
                    ],
                    'language' => [
                        'required' => false,
                        'type' => 'string',
                    ],
                    'reference_year' => [
                        'required' => false,
                        'type' => 'integer',
                    ],
                    'max_sources' => [
                        'required' => false,
                        'type' => 'integer',
                    ],
                ],
            ]
        );
    }

    public static function register_assets(): void
    {
        wp_register_style(
            'le-global-chatbot',
            plugins_url(
                'assets/chatbot.css',
                __FILE__
            ),
            [],
            self::VERSION
        );

        wp_register_script(
            'le-global-chatbot',
            plugins_url(
                'assets/chatbot.js',
                __FILE__
            ),
            [],
            self::VERSION,
            true
        );
    }

    public static function render_shortcode(): string
    {
        wp_enqueue_style(
            'le-global-chatbot'
        );

        wp_enqueue_script(
            'le-global-chatbot'
        );

        $instance_id = wp_unique_id(
            'le-global-chatbot-'
        );

        $rest_base = untrailingslashit(
            rest_url(
                self::REST_NAMESPACE
            )
        );

        ob_start();
        ?>
        <section
            id="<?php
                echo esc_attr(
                    $instance_id
                );
            ?>"
            class="le-global-chatbot"
            data-config-endpoint="<?php
                echo esc_url(
                    $rest_base . '/config'
                );
            ?>"
            data-chat-endpoint="<?php
                echo esc_url(
                    $rest_base . '/chat'
                );
            ?>"
        >
            <header class="le-global-chatbot__header">
                <p class="le-global-chatbot__eyebrow">
                    L&amp;E Global
                </p>

                <h2 class="le-global-chatbot__title">
                    Employment Law Assistant
                </h2>

                <p class="le-global-chatbot__introduction">
                    Ask a question about employment law.
                    Answers are generated exclusively from
                    validated L&amp;E Global documents.
                </p>
            </header>

            <form class="le-global-chatbot__form">
                <div class="le-global-chatbot__field">
                    <label
                        class="le-global-chatbot__label"
                        for="<?php
                            echo esc_attr(
                                $instance_id
                                . '-question'
                            );
                        ?>"
                    >
                        Your question
                    </label>

                    <textarea
                        id="<?php
                            echo esc_attr(
                                $instance_id
                                . '-question'
                            );
                        ?>"
                        class="le-global-chatbot__question"
                        name="question"
                        rows="5"
                        minlength="2"
                        maxlength="2000"
                        required
                        placeholder="Example: What is the statutory notice period in the United Kingdom?"
                    ></textarea>

                    <div class="le-global-chatbot__counter">
                        <span data-character-count>0</span>
                        / 2000
                    </div>
                </div>

                <fieldset class="le-global-chatbot__fieldset">
                    <legend class="le-global-chatbot__label">
                        Countries
                    </legend>

                    <p class="le-global-chatbot__help">
                        Optional. Leave empty to detect countries
                        automatically from your question.
                    </p>

                    <div
                        class="le-global-chatbot__countries"
                        data-countries
                    >
                        <p class="le-global-chatbot__loading">
                            Loading available countries…
                        </p>
                    </div>
                </fieldset>

                <div class="le-global-chatbot__actions">
                    <button
                        class="le-global-chatbot__submit"
                        type="submit"
                    >
                        Ask the assistant
                    </button>
                </div>
            </form>

            <div
                class="le-global-chatbot__status"
                data-status
                role="status"
                aria-live="polite"
            ></div>

            <div
                class="le-global-chatbot__error"
                data-error
                role="alert"
                hidden
            ></div>

            <article
                class="le-global-chatbot__response"
                data-response
                hidden
            >
                <h3 class="le-global-chatbot__response-title">
                    Answer
                </h3>

                <div
                    class="le-global-chatbot__answer"
                    data-answer
                ></div>

                <section
                    class="le-global-chatbot__sources-section"
                    data-sources-section
                    hidden
                >
                    <h4 class="le-global-chatbot__sources-title">
                        Sources
                    </h4>

                    <ol
                        class="le-global-chatbot__sources"
                        data-sources
                    ></ol>
                </section>

                <p class="le-global-chatbot__disclaimer">
                    This information is based on the available
                    L&amp;E Global documents and does not constitute
                    legal advice.
                </p>
            </article>
        </section>
        <?php

        return (string) ob_get_clean();
    }

    public static function get_frontend_config(
        WP_REST_Request $request
    ) {
        unset($request);

        return self::proxy_backend_request(
            'GET',
            self::BACKEND_CONFIG_PATH,
            null,
            20
        );
    }

    public static function submit_chat_question(
        WP_REST_Request $request
    ) {
        $parameters = (
            $request->get_json_params()
        );

        if (!is_array($parameters)) {
            return new WP_Error(
                'le_global_invalid_request',
                'The request body must contain valid JSON.',
                [
                    'status' => 400,
                ]
            );
        }

        $question = isset(
            $parameters['question']
        )
            ? sanitize_textarea_field(
                (string) (
                    $parameters['question']
                )
            )
            : '';

        if (strlen(trim($question)) < 2) {
            return new WP_Error(
                'le_global_invalid_question',
                'Please enter a legal question.',
                [
                    'status' => 422,
                ]
            );
        }

        $payload = [
            'question' => $question,
            'country_codes' => (
                self::sanitize_string_list(
                    $parameters[
                        'country_codes'
                    ] ?? [],
                    true
                )
            ),
            'legal_topics' => (
                self::sanitize_string_list(
                    $parameters[
                        'legal_topics'
                    ] ?? []
                )
            ),
            'subsections' => (
                self::sanitize_string_list(
                    $parameters[
                        'subsections'
                    ] ?? []
                )
            ),
            'language' => isset(
                $parameters['language']
            )
                ? sanitize_key(
                    (string) (
                        $parameters['language']
                    )
                )
                : 'en',
            'max_sources' => isset(
                $parameters['max_sources']
            )
                ? absint(
                    $parameters['max_sources']
                )
                : 6,
        ];

        if (
            isset(
                $parameters['reference_year']
            )
            && absint(
                $parameters['reference_year']
            ) > 0
        ) {
            $payload['reference_year'] = absint(
                $parameters['reference_year']
            );
        }

        return self::proxy_backend_request(
            'POST',
            self::BACKEND_CHAT_PATH,
            $payload,
            75
        );
    }

    private static function sanitize_string_list(
        mixed $values,
        bool $uppercase = false
    ): array {
        if (!is_array($values)) {
            return [];
        }

        $sanitized_values = [];

        foreach ($values as $value) {
            if (!is_scalar($value)) {
                continue;
            }

            $sanitized_value = trim(
                sanitize_text_field(
                    (string) $value
                )
            );

            if ($uppercase) {
                $sanitized_value = strtoupper(
                    $sanitized_value
                );
            }

            if ($sanitized_value === '') {
                continue;
            }

            $sanitized_values[
                $sanitized_value
            ] = $sanitized_value;
        }

        return array_values(
            $sanitized_values
        );
    }

    private static function proxy_backend_request(
        string $method,
        string $path,
        ?array $payload,
        int $timeout
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

        $arguments = [
            'method' => $method,
            'timeout' => $timeout,
            'redirection' => 2,
            'headers' => $headers,
        ];

        if ($payload !== null) {
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
            return new WP_Error(
                'le_global_backend_unavailable',
                'The legal assistant is temporarily unavailable.',
                [
                    'status' => 502,
                    'backend_error' => (
                        $response->get_error_message()
                    ),
                ]
            );
        }

        $status_code = (
            wp_remote_retrieve_response_code(
                $response
            )
        );

        $raw_body = wp_remote_retrieve_body(
            $response
        );

        $decoded_body = json_decode(
            $raw_body,
            true
        );

        if (
            $raw_body !== ''
            && json_last_error()
            !== JSON_ERROR_NONE
        ) {
            return new WP_Error(
                'le_global_invalid_backend_response',
                'The legal assistant returned an invalid response.',
                [
                    'status' => 502,
                ]
            );
        }

        if (!is_array($decoded_body)) {
            $decoded_body = [
                'detail' => (
                    'The legal assistant returned '
                    . 'an empty response.'
                ),
            ];
        }

        return new WP_REST_Response(
            $decoded_body,
            $status_code > 0
                ? $status_code
                : 502
        );
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

        if (
            $backend_url === ''
            || $api_key === ''
        ) {
            return new WP_Error(
                'le_global_chatbot_not_configured',
                'The L&E Global chatbot is not configured.',
                [
                    'status' => 503,
                ]
            );
        }

        return [
            'url' => esc_url_raw(
                $backend_url
            ),
            'api_key' => $api_key,
        ];
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
}

require_once plugin_dir_path(
    __FILE__
) . 'includes/class-le-global-chatbot-admin.php';

LE_Global_Chatbot_Plugin::init();
LE_Global_Chatbot_Admin::init();