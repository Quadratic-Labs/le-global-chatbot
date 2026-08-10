<?php
/**
 * Plugin Name: L&E Global Chatbot
 * Description: Secure WordPress integration for the L&E Global employment law chatbot.
 * Version: 0.4.9
 * Author: Quadratic Labs
 * Text Domain: le-global-chatbot
 */

if (!defined('ABSPATH')) {
    exit;
}

final class LE_Global_Chatbot_Plugin
{
    private const VERSION = '0.4.9';

    private const REST_NAMESPACE = 'le-global-chatbot/v1';

    private const SHORTCODE = 'le_global_chatbot';

    private const BACKEND_CONFIG_PATH = (
        '/api/v1/frontend-config'
    );

    private const BACKEND_CHAT_PATH = (
        '/api/v1/chat'
    );

    private const CHAT_LANGUAGE = 'en';

    private const MAX_SOURCES_DEFAULT = 6;

    private const MAX_SOURCES_MIN = 1;

    private const MAX_SOURCES_MAX = 10;

    private const HISTORY_MAX_MESSAGES = 20;

    private const HISTORY_MESSAGE_MAX_CHARACTERS = 4000;

    // Scaled proportionally with HISTORY_MAX_MESSAGES (previously
    // 10000 for 6 messages), matching the backend's
    // HISTORY_TOTAL_MAX_CHARACTERS exactly - the per-message limit
    // above is unchanged.
    private const HISTORY_TOTAL_MAX_CHARACTERS = 33333;

    private const HISTORY_ALLOWED_ROLES = [
        'user',
        'assistant',
    ];

    // Coarse defense-in-depth ceiling only, mirroring the backend's
    // own authoritative limit (app/models/conversation_state.py,
    // MAX_CONVERSATION_STATE_JSON_CHARACTERS). The backend's Pydantic
    // model (extra="forbid") remains the sole source of truth for
    // conversation_state's shape and content.
    private const CONVERSATION_STATE_MAX_JSON_CHARACTERS = 8000;

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
                    'history' => [
                        'required' => false,
                        'type' => 'array',
                    ],
                    'conversation_state' => [
                        'required' => false,
                        'type' => 'object',
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

    public static function render_shortcode(
        $attributes = []
    ): string {
        wp_enqueue_style(
            'le-global-chatbot'
        );

        wp_enqueue_script(
            'le-global-chatbot'
        );

        $normalized_attributes = shortcode_atts(
            [
                'mode' => 'inline',
            ],
            is_array($attributes)
                ? $attributes
                : [],
            self::SHORTCODE
        );

        $mode = (
            strtolower(
                trim(
                    (string) $normalized_attributes['mode']
                )
            ) === 'floating'
                ? 'floating'
                : 'inline'
        );

        $is_floating = ($mode === 'floating');

        $instance_id = wp_unique_id(
            'le-global-chatbot-'
        );

        $title_id = $instance_id . '-title';

        $rest_base = untrailingslashit(
            rest_url(
                self::REST_NAMESPACE
            )
        );

        ob_start();
        ?>
        <?php if ($is_floating) : ?>
        <div
            class="le-global-chatbot-floating"
            data-floating-wrapper
        >
            <button
                type="button"
                class="le-global-chatbot-floating__launcher"
                aria-expanded="false"
                aria-controls="<?php
                    echo esc_attr(
                        $instance_id
                    );
                ?>"
                aria-label="Open the employment law assistant"
                data-launcher
            >
                <svg
                    xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 24 24"
                    width="28"
                    height="28"
                    aria-hidden="true"
                    focusable="false"
                >
                    <path
                        fill="currentColor"
                        d="M12 3C7.03 3 3 6.58 3 11c0 2.36 1.14 4.47 2.94 5.94L5 21l4.24-1.7c.87.18 1.79.28 2.76.28 4.97 0 9-3.58 9-8s-4.03-8-9-8z"
                    />
                </svg>
            </button>
        <?php endif; ?>
        <section
            id="<?php
                echo esc_attr(
                    $instance_id
                );
            ?>"
            class="le-global-chatbot<?php
                echo $is_floating
                    ? ' le-global-chatbot--floating'
                    : '';
            ?>"
            data-mode="<?php
                echo esc_attr(
                    $mode
                );
            ?>"
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
            <?php if ($is_floating) : ?>
            role="dialog"
            aria-labelledby="<?php
                echo esc_attr(
                    $title_id
                );
            ?>"
            hidden
            <?php endif; ?>
        >
            <header class="le-global-chatbot__panel-header">
                <div class="le-global-chatbot__panel-heading">
                    <p class="le-global-chatbot__eyebrow">
                        L&amp;E Global
                    </p>

                    <h2
                        class="le-global-chatbot__title"
                        id="<?php
                            echo esc_attr(
                                $title_id
                            );
                        ?>"
                    >
                        Employment Law Assistant
                    </h2>
                </div>

                <div class="le-global-chatbot__panel-actions">
                    <button
                        type="button"
                        class="le-global-chatbot__new-conversation"
                        aria-label="Start a new conversation"
                        data-new-conversation
                    >
                        New conversation
                    </button>

                    <?php if ($is_floating) : ?>
                    <button
                        type="button"
                        class="le-global-chatbot-floating__close"
                        aria-label="Close the employment law assistant"
                        data-close
                    >
                        <span aria-hidden="true">&times;</span>
                    </button>
                    <?php endif; ?>
                </div>
            </header>

            <div
                class="le-global-chatbot__conversation"
                data-conversation
            >
                <div
                    class="le-global-chatbot__message le-global-chatbot__message--assistant"
                    data-welcome-message
                >
                    Ask an employment law question. Countries
                    and legal topics are detected automatically.
                    Answers use validated L&amp;E Global documents.
                </div>

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

                <div
                    class="le-global-chatbot__message-list"
                    data-message-list
                ></div>
            </div>

            <form class="le-global-chatbot__composer">
                <label
                    class="le-global-chatbot__sr-only"
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
                    rows="3"
                    minlength="2"
                    maxlength="2000"
                    required
                    placeholder="Ask an employment law question…"
                ></textarea>

                <div class="le-global-chatbot__composer-footer">
                    <div class="le-global-chatbot__counter">
                        <span data-character-count>0</span>
                        / 2000
                    </div>

                    <button
                        class="le-global-chatbot__submit"
                        type="submit"
                    >
                        Send
                    </button>
                </div>
            </form>
        </section>
        <?php if ($is_floating) : ?>
        </div>
        <?php endif; ?>
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

        $max_sources = self::MAX_SOURCES_DEFAULT;

        if (
            isset(
                $parameters['max_sources']
            )
        ) {
            $requested_max_sources = (int) (
                $parameters['max_sources']
            );

            if (
                $requested_max_sources
                < self::MAX_SOURCES_MIN
                || $requested_max_sources
                > self::MAX_SOURCES_MAX
            ) {
                return new WP_Error(
                    'le_global_invalid_max_sources',
                    sprintf(
                        'max_sources must be between '
                        . '%d and %d.',
                        self::MAX_SOURCES_MIN,
                        self::MAX_SOURCES_MAX
                    ),
                    [
                        'status' => 422,
                    ]
                );
            }

            $max_sources = $requested_max_sources;
        }

        $history = self::sanitize_history(
            $parameters['history'] ?? []
        );

        if (is_wp_error($history)) {
            return $history;
        }

        $conversation_state = self::sanitize_conversation_state(
            $parameters['conversation_state'] ?? null
        );

        if (is_wp_error($conversation_state)) {
            return $conversation_state;
        }

        $payload = [
            'question' => $question,
            'history' => $history,
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
            // Public clients may never choose the response
            // language: any client-supplied 'language' value is
            // ignored, since the product only ever answers in
            // English.
            'language' => self::CHAT_LANGUAGE,
            'max_sources' => $max_sources,
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

        if ($conversation_state !== null) {
            $payload['conversation_state'] = $conversation_state;
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

    /**
     * Count the Unicode characters (not bytes) of a string.
     *
     * Falls back to strlen() only when mbstring is unavailable, since
     * the character budgets below must match the backend's Python
     * len() semantics, not a raw byte count.
     */
    private static function string_length(string $value): int
    {
        if (function_exists('mb_strlen')) {
            return mb_strlen($value, 'UTF-8');
        }

        return strlen($value);
    }

    /**
     * Validate and sanitize the optional conversation history.
     *
     * Mirrors every shape rule LegalChatHistoryMessage enforces on
     * the backend (message count, per-message and total character
     * budgets, first=user/last=assistant, strict alternation), so a
     * malformed history is rejected here with a generic 422 instead
     * of being forwarded to FastAPI only to fail there. This
     * duplication is intentional defense in depth - the Pydantic
     * model remains the final authority and is not relaxed by it.
     * Never logs or echoes rejected content - only a generic message
     * is returned for any structural problem.
     *
     * @return array|WP_Error
     */
    private static function sanitize_history(
        mixed $raw_history
    ) {
        if (!is_array($raw_history)) {
            return new WP_Error(
                'le_global_invalid_history',
                'history must be a list of messages.',
                [
                    'status' => 422,
                ]
            );
        }

        if (count($raw_history) > self::HISTORY_MAX_MESSAGES) {
            return new WP_Error(
                'le_global_invalid_history',
                sprintf(
                    'history must contain at most %d messages.',
                    self::HISTORY_MAX_MESSAGES
                ),
                [
                    'status' => 422,
                ]
            );
        }

        $sanitized_history = [];

        foreach ($raw_history as $entry) {
            if (!is_array($entry)) {
                return new WP_Error(
                    'le_global_invalid_history',
                    'Each history entry must be an object '
                    . 'with role and content.',
                    [
                        'status' => 422,
                    ]
                );
            }

            $role = isset($entry['role'])
                ? sanitize_key(
                    (string) $entry['role']
                )
                : '';

            if (
                !in_array(
                    $role,
                    self::HISTORY_ALLOWED_ROLES,
                    true
                )
            ) {
                return new WP_Error(
                    'le_global_invalid_history',
                    'history role must be "user" or '
                    . '"assistant".',
                    [
                        'status' => 422,
                    ]
                );
            }

            $content = isset($entry['content'])
                ? sanitize_textarea_field(
                    (string) $entry['content']
                )
                : '';

            if (trim($content) === '') {
                return new WP_Error(
                    'le_global_invalid_history',
                    'Each history entry must have '
                    . 'non-empty content.',
                    [
                        'status' => 422,
                    ]
                );
            }

            if (
                self::string_length($content)
                > self::HISTORY_MESSAGE_MAX_CHARACTERS
            ) {
                return new WP_Error(
                    'le_global_invalid_history',
                    sprintf(
                        'history content must be at most '
                        . '%d characters.',
                        self::HISTORY_MESSAGE_MAX_CHARACTERS
                    ),
                    [
                        'status' => 422,
                    ]
                );
            }

            $sanitized_history[] = [
                'role' => $role,
                'content' => $content,
            ];
        }

        if (empty($sanitized_history)) {
            return $sanitized_history;
        }

        if ($sanitized_history[0]['role'] !== 'user') {
            return new WP_Error(
                'le_global_invalid_history',
                'history must start with a "user" message.',
                [
                    'status' => 422,
                ]
            );
        }

        $last_index = count($sanitized_history) - 1;

        if ($sanitized_history[$last_index]['role'] !== 'assistant') {
            return new WP_Error(
                'le_global_invalid_history',
                'history must end with an "assistant" message.',
                [
                    'status' => 422,
                ]
            );
        }

        $total_characters = 0;
        $previous_role = null;

        foreach ($sanitized_history as $entry) {
            if (
                $previous_role !== null
                && $entry['role'] === $previous_role
            ) {
                return new WP_Error(
                    'le_global_invalid_history',
                    'history roles must strictly alternate '
                    . 'between "user" and "assistant".',
                    [
                        'status' => 422,
                    ]
                );
            }

            $previous_role = $entry['role'];

            $total_characters += self::string_length(
                $entry['content']
            );
        }

        if ($total_characters > self::HISTORY_TOTAL_MAX_CHARACTERS) {
            return new WP_Error(
                'le_global_invalid_history',
                sprintf(
                    'history total content length must not '
                    . 'exceed %d characters.',
                    self::HISTORY_TOTAL_MAX_CHARACTERS
                ),
                [
                    'status' => 422,
                ]
            );
        }

        return $sanitized_history;
    }

    /**
     * Validates the client-supplied conversation_state envelope
     * before it is forwarded to the backend. This is a coarse,
     * defense-in-depth check only: the proxy never inspects,
     * rewrites, or injects any of its inner fields - it either
     * forwards the object exactly as received or rejects it
     * outright. The backend's Pydantic model (extra="forbid") is
     * the sole authority on conversation_state's shape and content,
     * and a 422 it raises is relayed back to the client unchanged
     * via proxy_backend_request().
     */
    private static function sanitize_conversation_state(
        mixed $raw_conversation_state
    ) {
        if ($raw_conversation_state === null) {
            return null;
        }

        if (!is_array($raw_conversation_state)) {
            return new WP_Error(
                'le_global_invalid_conversation_state',
                'conversation_state must be an object.',
                [
                    'status' => 422,
                ]
            );
        }

        $encoded_conversation_state = wp_json_encode(
            $raw_conversation_state
        );

        if (
            $encoded_conversation_state === false
            || self::string_length(
                $encoded_conversation_state
            ) > self::CONVERSATION_STATE_MAX_JSON_CHARACTERS
        ) {
            return new WP_Error(
                'le_global_invalid_conversation_state',
                'conversation_state is too large.',
                [
                    'status' => 422,
                ]
            );
        }

        return $raw_conversation_state;
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
                'le_global_backend_unavailable',
                'The legal assistant is temporarily unavailable.',
                [
                    'status' => 502,
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