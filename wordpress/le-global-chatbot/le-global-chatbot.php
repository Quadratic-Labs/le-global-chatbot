<?php
/**
 * Plugin Name: L&E Global Chatbot
 * Description: Secure WordPress integration for the L&E Global employment law chatbot.
 * Version: 0.8.10
 * Author: Quadratic Labs
 * Text Domain: le-global-chatbot
 */

if (!defined('ABSPATH')) {
    exit;
}

final class LE_Global_Chatbot_Plugin
{
    private const VERSION = '0.8.10';

    private const REST_NAMESPACE = 'le-global-chatbot/v1';

    private const SHORTCODE = 'le_global_chatbot';

    private const BACKEND_CONFIG_PATH = (
        '/api/v1/frontend-config'
    );

    private const BACKEND_CHAT_PATH = (
        '/api/v1/chat'
    );

    private const BACKEND_CHAT_STREAM_PATH = (
        '/api/v1/chat/stream'
    );

    // GATE S9B: explicit cancellation, independent of passive
    // connection_aborted()-based disconnect detection (found
    // unreliable under real Apache/mod_php - see the S9-LITE report).
    private const BACKEND_CHAT_STREAM_CANCEL_PATH = (
        '/api/v1/chat/stream/cancel'
    );

    private const STREAM_ROUTE = (
        '/' . self::REST_NAMESPACE . '/chat/stream'
    );

    // GATE S7-LITE: the smallest possible switch for exposing
    // /chat/stream to the browser widget - one wp-config.php-style
    // constant, matching LE_GLOBAL_CHATBOT_API_URL/_API_KEY's own
    // defined()-or-default convention (see get_backend_configuration()
    // below). Undefined - the default - means OFF: render_shortcode()
    // still advertises the /chat/stream URL (so it is never a secret),
    // but chatbot.js will not use it until this constant is defined
    // truthy. Deliberately not a general feature-flag framework - no
    // options table row, no admin UI, no per-request evaluation.
    private const STREAMING_ENABLED_CONSTANT = (
        'LE_GLOBAL_CHATBOT_STREAMING_ENABLED'
    );

    // GATE S6C: the S6B derivation (200s) covered only retrieval +
    // the streamed answer + repair - it omitted request understanding,
    // which runs BEFORE any byte streams and so also counts against
    // this route's total request/response window. Full stage-by-stage
    // trace (backend source, not assumption):
    //
    //   UNDERSTANDING: request_understanding.py's understand_request()
    //   makes "at most two network attempts (a single retry, only for
    //   a transient failure)", no backoff sleep between them, each via
    //   get_openai_understanding_client() -> the same OPENAI_TIMEOUT_
    //   SECONDS-bounded (60s default) sync client every OpenAI call in
    //   this codebase shares (_get_configured_openai_client() - one
    //   shared timeout, no per-flavor override). Worst case: 2x60=120s.
    //
    //   RETRIEVAL: one _retrieve_search_hits() call per action_spec,
    //   one OpenSearch call per country within it (opensearch-py's own
    //   un-overridden default: 10s/call, no application-level retry).
    //   action_specs/actions has no max_length in the Pydantic model -
    //   not hard-bounded - but OPENAI_UNDERSTANDING_MAX_OUTPUT_TOKENS's
    //   own docstring records real-API verification up to "mixed
    //   3-action" requests as the realistic/tested envelope. Budgeted
    //   at ~30s for that many calls at healthy-cluster (not
    //   all-timing-out) latency, consistent with S6B's own reasoning.
    //
    //   RERANK: RERANK_ENABLED defaults to false (infra/compose.yml) -
    //   0s in the CURRENT runtime. If ever enabled, rerank shares
    //   retrieval's own call-count profile (one call per retrieval
    //   call) via the same OPENAI_TIMEOUT_SECONDS-bounded sync client -
    //   budgeted at ~30s for the same reasoning as retrieval, since
    //   this route's ceiling must not need re-deriving the moment
    //   reranking is turned on.
    //
    //   STREAM: OpenAIResponsesStreamClient's own self-enforced
    //   total_stream_timeout_seconds=120s (class default, never
    //   overridden by get_openai_answer_stream_client() - confirmed
    //   OPENAI_TIMEOUT_SECONDS has zero effect on this axis).
    //
    //   REPAIR: at most one repair attempt (stream_answer_legal_
    //   question's own generation_attempts=2 ceiling - a single
    //   asyncio.to_thread() call, no loop), via the same 60s-bounded
    //   sync client, no retry. Worst case: 60s.
    //
    // CURRENT_RUNTIME_MAX  = 120 + 30 + 0  + 120 + 60 = 330s
    // SUPPORTED_CONFIG_MAX = 120 + 30 + 30 + 120 + 60 = 360s (rerank on)
    //
    // This route's own ceilings are sized against SUPPORTED_CONFIG_MAX
    // (360s), not the current-runtime figure, so enabling rerank later
    // never silently reopens this same gap.
    private const STREAM_CONNECT_TIMEOUT_SECONDS = 10;

    private const STREAM_TOTAL_TIMEOUT_SECONDS = 400;

    // A few seconds of headroom over STREAM_TOTAL_TIMEOUT_SECONDS -
    // this route must never be cut short by PHP's own execution
    // ceiling before the backend's own (longer) stream timeout has a
    // chance to fire and be relayed to the client normally.
    private const STREAM_PHP_EXECUTION_TIME_SECONDS = 420;

    // Only these backend response headers are ever relayed to the
    // client for the streaming route - never a blind passthrough.
    // Framing/hop-by-hop headers (Server, Date, Connection,
    // Transfer-Encoding, backend Content-Length) and anything
    // WordPress/PHP must own itself (Set-Cookie, CORS) are
    // deliberately excluded.
    private const STREAM_RESPONSE_HEADER_ALLOWLIST = [
        'content-type',
        'cache-control',
        'x-accel-buffering',
        'x-request-id',
        'x-ratelimit-limit',
        'x-ratelimit-remaining',
        'x-ratelimit-reset',
        'retry-after',
    ];

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

        // Narrowly scoped: maybe_stream_backend_response() itself
        // re-checks both the exact route AND an unforgeable marker
        // object identity before ever taking over serving - see that
        // method's own docblock. This does not alter REST serving
        // for any other route.
        add_filter(
            'rest_pre_serve_request',
            [self::class, 'maybe_stream_backend_response'],
            20,
            4
        );

        add_action(
            'wp_ajax_le_global_contact_photo',
            [self::class, 'proxy_contact_photo']
        );

        add_action(
            'wp_ajax_nopriv_le_global_contact_photo',
            [self::class, 'proxy_contact_photo']
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
                'args' => self::chat_route_args(),
            ]
        );

        register_rest_route(
            self::REST_NAMESPACE,
            '/chat/stream',
            [
                'methods' => WP_REST_Server::CREATABLE,
                'callback' => [
                    self::class,
                    'submit_chat_question_stream',
                ],
                'permission_callback' => (
                    '__return_true'
                ),
                'args' => self::chat_route_args(),
            ]
        );

        // GATE S9B.
        register_rest_route(
            self::REST_NAMESPACE,
            '/chat/stream/cancel',
            [
                'methods' => WP_REST_Server::CREATABLE,
                'callback' => [
                    self::class,
                    'cancel_chat_stream',
                ],
                'permission_callback' => (
                    '__return_true'
                ),
                'args' => [
                    'request_id' => [
                        'required' => true,
                        'type' => 'string',
                    ],
                ],
            ]
        );
    }

    /**
     * The request-argument schema shared by /chat and /chat/stream -
     * a single definition so the two routes can never silently drift
     * apart on which top-level fields WordPress itself validates the
     * shape/type of before either route callback ever runs.
     */
    private static function chat_route_args(): array
    {
        return [
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
        ];
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
            data-contact-photo-endpoint="<?php
                echo esc_url(
                    admin_url(
                        'admin-ajax.php?action=le_global_contact_photo'
                    )
                );
            ?>"
            data-chat-endpoint="<?php
                echo esc_url(
                    $rest_base . '/chat'
                );
            ?>"
            data-chat-stream-endpoint="<?php
                echo esc_url(
                    $rest_base . '/chat/stream'
                );
            ?>"
            data-chat-streaming-enabled="<?php
                echo esc_attr(
                    self::is_chat_streaming_enabled()
                        ? '1'
                        : '0'
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
                    rows="1"
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
        $payload = self::build_chat_payload_from_request(
            $request
        );

        if (is_wp_error($payload)) {
            return $payload;
        }

        return self::proxy_backend_request(
            'POST',
            self::BACKEND_CHAT_PATH,
            $payload,
            75
        );
    }

    /**
     * Streaming counterpart to submit_chat_question() - reuses the
     * EXACT same validation/sanitization/payload-building primitive
     * (build_chat_payload_from_request()), so /chat and /chat/stream
     * can never accept/reject different client payloads.
     *
     * On success this callback does NOT write any body bytes itself:
     * it returns a WP_REST_Response wrapping an
     * LE_Global_Chatbot_Stream_Marker, which
     * maybe_stream_backend_response() (registered on
     * rest_pre_serve_request) recognizes and takes over from - see
     * that method's own docblock for why a plain WP_Error is safe to
     * return here unchanged (normal WordPress REST error handling
     * serves it, exactly as for /chat).
     */
    public static function submit_chat_question_stream(
        WP_REST_Request $request
    ) {
        $payload = self::build_chat_payload_from_request(
            $request
        );

        if (is_wp_error($payload)) {
            return $payload;
        }

        $request_id = self::sanitize_request_id(
            $request->get_header('X-Request-ID')
        );

        return new WP_REST_Response(
            new LE_Global_Chatbot_Stream_Marker(
                $payload,
                $request_id
            )
        );
    }

    /**
     * GATE S9B: explicit cancellation - the normal (non-streaming)
     * proxy mechanism, never the raw cURL relay stream_backend_
     * response() uses for /chat/stream itself: this response is one
     * small JSON object, not a stream. request_id comes from the
     * request BODY here (unlike submit_chat_question_stream's own
     * X-Request-ID HEADER usage) because it names an EXISTING, already
     * in-flight stream the client learned from that stream's own
     * `start` NDJSON record - it is not this request's own identity.
     */
    public static function cancel_chat_stream(
        WP_REST_Request $request
    ) {
        $request_id = self::sanitize_request_id(
            $request->get_param('request_id')
        );

        if ($request_id === null) {
            return new WP_Error(
                'le_global_chatbot_invalid_request_id',
                'A valid request_id is required.',
                [
                    'status' => 422,
                ]
            );
        }

        return self::proxy_backend_request(
            'POST',
            self::BACKEND_CHAT_STREAM_CANCEL_PATH,
            ['request_id' => $request_id],
            10
        );
    }

    /**
     * Validate and sanitize one /chat or /chat/stream request body
     * into the exact payload shape the backend expects - shared by
     * both routes so they can never independently drift. Returns the
     * payload array on success, or a WP_Error (with the correct HTTP
     * status already attached) on any validation failure.
     *
     * @return array|WP_Error
     */
    private static function build_chat_payload_from_request(
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

        return $payload;
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

    /**
     * rest_pre_serve_request handler - takes over serving ONLY when
     * BOTH of these hold:
     *   (a) this is exactly POST /le-global-chatbot/v1/chat/stream;
     *   (b) $result (the dispatched route's own return value, already
     *       normalized by WordPress core into a WP_REST_Response
     *       EVEN WHEN THE ROUTE RETURNED A WP_Error - rest_ensure_
     *       response() converts errors before this filter ever runs)
     *       carries an LE_Global_Chatbot_Stream_Marker as its data.
     *
     * Condition (b) is the actual security boundary, not (a): a
     * converted WP_Error's data is an array (code/message/data
     * shape), never an instance of this specific internal class, so
     * a validation failure - or ANY other response this route could
     * ever return - can never be mistaken for the one true success
     * marker. The marker is a fresh, request-local PHP object created
     * exactly once inside submit_chat_question_stream() and never
     * touches any global/static state, so there is nothing to leak
     * between requests (PHP's own per-request lifecycle already
     * discards all userland object/variable state at the end of every
     * request, under both mod_php and PHP-FPM alike) and nothing an
     * attacker can forge via request parameters - only this plugin's
     * own code can ever construct one.
     *
     * When this returns true, WordPress skips its own default
     * wp_json_encode() body serialization entirely - by that point
     * this method has already written the real body itself (see
     * stream_backend_response()).
     */
    public static function maybe_stream_backend_response(
        bool $served,
        $result,
        WP_REST_Request $request,
        $server
    ): bool {
        unset($server);

        if (
            $request->get_method() !== 'POST'
            || $request->get_route() !== self::STREAM_ROUTE
            || !($result instanceof WP_REST_Response)
        ) {
            return $served;
        }

        $marker = $result->get_data();

        if (!($marker instanceof LE_Global_Chatbot_Stream_Marker)) {
            return $served;
        }

        self::stream_backend_response(
            $marker->payload,
            $marker->request_id
        );

        return true;
    }

    /**
     * The PHP cURL streaming proxy itself - deliberately NOT
     * wp_remote_request()/wp_remote_post() (they buffer the entire
     * response before returning, which is the whole reason this
     * route exists). CURLOPT_WRITEFUNCTION relays each chunk to the
     * client as it arrives; CURLOPT_HEADERFUNCTION relays the
     * backend's own status code and an explicit header allowlist
     * BEFORE any body byte is echoed, so both a streamed NDJSON
     * success and a pre-stream JSON error (422/429/502/503) are
     * forwarded with their real status/content-type intact - this
     * function never assumes or forces either shape, it only relays
     * whatever the backend actually sent.
     */
    private static function stream_backend_response(
        array $payload,
        ?string $request_id
    ): void {
        $configuration = self::get_backend_configuration();

        if (is_wp_error($configuration)) {
            self::emit_proxy_error(503, 'chatbot_not_configured');

            return;
        }

        // This request-local override does not touch php.ini or any
        // other request's execution ceiling - see the class-constant
        // docblock for why this specific value was chosen.
        set_time_limit(
            self::STREAM_PHP_EXECUTION_TIME_SECONDS
        );

        $backend_url = untrailingslashit(
            $configuration['url']
        ) . self::BACKEND_CHAT_STREAM_PATH;

        $request_headers = [
            'Content-Type: application/json',
            'Accept: application/x-ndjson',
            'X-API-Key: ' . $configuration['api_key'],
        ];

        $client_ip = self::get_client_ip();

        if ($client_ip !== null) {
            $request_headers[] = 'X-Forwarded-For: ' . $client_ip;
            $request_headers[] = 'X-Real-IP: ' . $client_ip;
        }

        if ($request_id !== null) {
            $request_headers[] = 'X-Request-ID: ' . $request_id;
        }

        $body_started = false;

        $curl = curl_init();

        curl_setopt_array(
            $curl,
            [
                CURLOPT_URL => $backend_url,
                CURLOPT_POST => true,
                CURLOPT_POSTFIELDS => wp_json_encode($payload),
                CURLOPT_HTTPHEADER => $request_headers,
                CURLOPT_CONNECTTIMEOUT => (
                    self::STREAM_CONNECT_TIMEOUT_SECONDS
                ),
                CURLOPT_TIMEOUT => (
                    self::STREAM_TOTAL_TIMEOUT_SECONDS
                ),
                CURLOPT_FOLLOWLOCATION => false,
                CURLOPT_RETURNTRANSFER => false,
                CURLOPT_HEADERFUNCTION => (
                    static function ($ch, $header_line) {
                        unset($ch);

                        self::relay_one_backend_header(
                            $header_line
                        );

                        return strlen($header_line);
                    }
                ),
                CURLOPT_WRITEFUNCTION => (
                    static function ($ch, $chunk) use (
                        &$body_started
                    ) {
                        unset($ch);

                        if (connection_aborted()) {
                            // A non-zero, non-strlen() return tells
                            // cURL to abort the transfer immediately.
                            return -1;
                        }

                        $body_started = true;

                        echo $chunk;

                        if (function_exists('flush')) {
                            flush();
                        }

                        // GATE S9-LITE: connection_aborted() only ever
                        // becomes true once a write has actually
                        // FAILED at the OS level - it does not
                        // proactively poll the socket, so checking
                        // only BEFORE the next chunk (as this code did
                        // before this gate) can never notice a failure
                        // that happened on THIS chunk's own write until
                        // a further chunk arrives. Checking again
                        // immediately after closes that specific gap.
                        // It is NOT a complete fix, and is disclosed as
                        // such in the S9-LITE report: real testing
                        // against a real Apache mod_php + real TCP
                        // client found that small, infrequent NDJSON
                        // chunks can be absorbed into the OS socket
                        // send buffer without the kernel attempting
                        // real transmission (and so without any write
                        // ever actually failing) for a materially long
                        // time after the browser has already gone -
                        // this is a genuine platform characteristic of
                        // connection_aborted()'s write-failure-based
                        // detection, not fixable from application code
                        // alone. WordPress's own outer ceilings
                        // (CURLOPT_TIMEOUT/set_time_limit) remain the
                        // bound of last resort when this happens.
                        if (connection_aborted()) {
                            return -1;
                        }

                        return strlen($chunk);
                    }
                ),
            ]
        );

        $succeeded = curl_exec($curl);
        $curl_errno = curl_errno($curl);

        curl_close($curl);

        if ($succeeded === false && !$body_started) {
            self::emit_proxy_error(502, 'backend_unavailable');

            return;
        }

        if ($succeeded === false && $body_started && $curl_errno !== 0) {
            // The transfer broke off ABNORMALLY (a clean end of a
            // real NDJSON stream is $succeeded === true - cURL only
            // returns false/sets an errno for a genuine connection
            // failure), so the backend's own terminal record (done/
            // error) was never sent. One well-formed, clearly-marked
            // NDJSON error line lets an already-connected client
            // detect this without ever corrupting the wire format
            // with PHP-generated warning/notice/HTML text.
            echo wp_json_encode(
                [
                    'type' => 'error',
                    'code' => 'wordpress_proxy_connection_lost',
                    'message' => (
                        'The connection to the legal assistant '
                        . 'was lost while streaming the response.'
                    ),
                    'retryable' => true,
                ]
            ) . "\n";

            if (function_exists('flush')) {
                flush();
            }
        }
    }

    /**
     * Parse and relay exactly one raw HTTP header LINE received from
     * the backend (CURLOPT_HEADERFUNCTION is called once per line,
     * starting with the status line, ending with the blank line that
     * terminates the header block) - called strictly before any body
     * byte is ever echoed, so status_header()/header() calls here
     * still take effect (PHP headers stay mutable until the first
     * actual output).
     */
    private static function relay_one_backend_header(
        string $header_line
    ): void {
        $trimmed_line = trim($header_line);

        if ($trimmed_line === '') {
            return;
        }

        if (
            preg_match(
                '#^HTTP/\d(?:\.\d)?\s+(\d{3})#',
                $trimmed_line,
                $matches
            )
        ) {
            status_header((int) $matches[1]);

            return;
        }

        $colon_position = strpos($trimmed_line, ':');

        if ($colon_position === false) {
            return;
        }

        $header_name = strtolower(
            trim(substr($trimmed_line, 0, $colon_position))
        );

        if (
            !in_array(
                $header_name,
                self::STREAM_RESPONSE_HEADER_ALLOWLIST,
                true
            )
        ) {
            return;
        }

        $header_value = trim(
            substr($trimmed_line, $colon_position + 1)
        );

        header(
            self::header_name_for_display($header_name)
            . ': ' . $header_value,
            true
        );
    }

    /**
     * Restore conventional header-name casing for display only (the
     * allowlist itself is matched case-insensitively) - purely
     * cosmetic, HTTP header names are case-insensitive on the wire.
     */
    private static function header_name_for_display(
        string $lowercase_header_name
    ): string {
        return implode(
            '-',
            array_map(
                'ucfirst',
                explode('-', $lowercase_header_name)
            )
        );
    }

    /**
     * A safe, generic pre-body error for the streaming route only -
     * used when the backend could not be reached/configured at all,
     * mirroring proxy_backend_request()'s own user-facing philosophy
     * (never expose hostnames, cURL error strings, or credentials).
     * Only reachable before any body byte has been echoed, so a
     * normal status/JSON response is still possible here.
     */
    private static function emit_proxy_error(
        int $status,
        string $code
    ): void {
        status_header($status);

        header('Content-Type: application/json', true);

        echo wp_json_encode(
            [
                'code' => 'le_global_' . $code,
                'message' => (
                    'The legal assistant is temporarily unavailable.'
                ),
                'data' => [
                    'status' => $status,
                ],
            ]
        );
    }

    /**
     * A conservative allowlist for a client-supplied X-Request-ID:
     * never reflected/forwarded unless it is short and made only of
     * characters safe to place directly into an outbound HTTP header
     * and to log - anything else is silently dropped (the backend
     * generates its own request_id when none is supplied, which is
     * an entirely safe, expected fallback, never an error).
     */
    private static function sanitize_request_id(
        mixed $raw_request_id
    ): ?string {
        if (!is_string($raw_request_id)) {
            return null;
        }

        $trimmed_request_id = trim($raw_request_id);

        if (
            $trimmed_request_id === ''
            || strlen($trimmed_request_id) > 100
            || !preg_match(
                '/^[A-Za-z0-9_-]+$/D',
                $trimmed_request_id
            )
        ) {
            return null;
        }

        return $trimmed_request_id;
    }

    public static function proxy_contact_photo(): void
    {
        $contact_id = isset($_GET['contact_id'])
            ? trim((string) wp_unslash($_GET['contact_id']))
            : '';

        $sha256 = isset($_GET['sha256'])
            ? trim((string) wp_unslash($_GET['sha256']))
            : '';

        if (
            !preg_match('/^[A-Za-z0-9._-]{1,200}$/D', $contact_id)
            || !preg_match('/^[0-9a-f]{64}$/D', $sha256)
        ) {
            status_header(404);
            exit;
        }

        $configuration = self::get_backend_configuration();

        if (is_wp_error($configuration)) {
            status_header(503);
            exit;
        }

        $backend_url = (
            untrailingslashit($configuration['url'])
            . '/api/v1/contact-photos/'
            . rawurlencode($contact_id)
            . '/'
            . $sha256
        );

        $response = wp_remote_get(
            $backend_url,
            [
                'timeout' => 20,
                'headers' => [
                    'X-API-Key' => $configuration['api_key'],
                ],
            ]
        );

        if (is_wp_error($response)) {
            status_header(502);
            exit;
        }

        $status = wp_remote_retrieve_response_code($response);

        if ($status !== 200) {
            status_header($status > 0 ? $status : 502);
            exit;
        }

        $content_type = strtolower(
            trim(
                (string) wp_remote_retrieve_header(
                    $response,
                    'content-type'
                )
            )
        );

        if (
            !in_array(
                $content_type,
                ['image/jpeg', 'image/png', 'image/webp'],
                true
            )
        ) {
            status_header(502);
            exit;
        }

        status_header(200);
        header('Content-Type: ' . $content_type);
        header('X-Content-Type-Options: nosniff');

        $etag = wp_remote_retrieve_header($response, 'etag');
        $cache = wp_remote_retrieve_header(
            $response,
            'cache-control'
        );

        if ($etag !== '') {
            header('ETag: ' . $etag);
        }

        if ($cache !== '') {
            header('Cache-Control: ' . $cache);
        }

        echo wp_remote_retrieve_body($response);
        exit;
    }


    /**
     * GATE S7-LITE: the one boolean render_shortcode() exposes as
     * data-chat-streaming-enabled. Defined-and-truthy is the only way
     * to turn this on; anything else (undefined, "", "0", false) is
     * OFF, matching the mission's "default MUST remain OFF" rule
     * without needing a truthiness table.
     */
    private static function is_chat_streaming_enabled(): bool
    {
        return (
            defined(self::STREAMING_ENABLED_CONSTANT)
            && (bool) constant(self::STREAMING_ENABLED_CONSTANT)
        );
    }

    private static function get_backend_configuration()
    {
        $backend_url = 'http://57.130.29.41';

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

/**
 * Internal-only success marker for POST /le-global-chatbot/v1/chat/
 * stream - see LE_Global_Chatbot_Plugin::maybe_stream_backend_
 * response() for why this class's identity (not any array shape or
 * string token) is the actual unforgeable security boundary between
 * "stream the backend response" and "let WordPress serve this
 * response normally". Holds only the already-validated/sanitized
 * payload to forward and a pre-sanitized request id - never a
 * credential of any kind.
 */
final class LE_Global_Chatbot_Stream_Marker
{
    public function __construct(
        public readonly array $payload,
        public readonly ?string $request_id
    ) {
    }
}

require_once plugin_dir_path(
    __FILE__
) . 'includes/class-le-global-chatbot-admin.php';

LE_Global_Chatbot_Plugin::init();
LE_Global_Chatbot_Admin::init();
