<?php

declare(strict_types=1);

/**
 * Permanent, framework-free CLI test for the /chat/stream WordPress
 * proxy route (GATE S6) - matching tests/extract-message.test.php's
 * own "no PHPUnit, no autoloader, plain PHP reflection over the real
 * plugin file" convention.
 *
 * Scope: this file exercises the pure-logic pieces of the streaming
 * route that are safely testable without a real HTTP server/cURL
 * target - shared request validation/payload-building parity between
 * /chat and /chat/stream, and maybe_stream_backend_response()'s
 * routing decision (the actual rest_pre_serve_request security
 * boundary). The cURL byte-relay/header-forwarding/buffering
 * behavior itself can only be meaningfully proven over a real socket
 * (see the GATE S6 report for that proof) - PHP's header()/echo
 * output functions cannot be intercepted from a CLI test the way
 * WordPress's OWN functions can be stubbed below.
 *
 * Run with:
 *   php wordpress/le-global-chatbot/tests/chat-stream.test.php
 *
 * Exits 0 when every check passes, 1 otherwise (with a message on
 * stderr for whichever check failed).
 */

// --- Minimal WordPress function/class stubs -------------------------

final class TestHaltException extends \Exception
{
}

final class WP_Error
{
    private array $errorData;

    public function __construct(
        private string $code,
        private string $message,
        array $data = []
    ) {
        $this->errorData = $data;
    }

    public function get_error_code(): string
    {
        return $this->code;
    }

    public function get_error_message(): string
    {
        return $this->message;
    }

    public function get_error_data(): array
    {
        return $this->errorData;
    }
}

function is_wp_error($value): bool
{
    return $value instanceof WP_Error;
}

final class WP_REST_Request
{
    private array $jsonParams;

    private array $headers = [];

    public function __construct(
        array $jsonParams,
        private string $route = '/le-global-chatbot/v1/chat/stream',
        private string $method = 'POST'
    ) {
        $this->jsonParams = $jsonParams;
    }

    public function get_json_params(): array
    {
        return $this->jsonParams;
    }

    public function set_header(string $name, string $value): void
    {
        $this->headers[strtolower($name)] = $value;
    }

    public function get_header(string $name): ?string
    {
        return $this->headers[strtolower($name)] ?? null;
    }

    public function get_route(): string
    {
        return $this->route;
    }

    public function get_method(): string
    {
        return $this->method;
    }
}

final class WP_REST_Response
{
    public function __construct(
        private mixed $data,
        private int $status = 200
    ) {
    }

    public function get_data(): mixed
    {
        return $this->data;
    }

    public function get_status(): int
    {
        return $this->status;
    }
}

final class WP_REST_Server
{
    public const CREATABLE = 'POST';
    public const READABLE = 'GET';
}

function add_action(...$args): void
{
}

function add_filter(...$args): void
{
}

function add_shortcode(...$args): void
{
}

function register_rest_route(...$args): void
{
}

function sanitize_textarea_field(string $value): string
{
    return trim($value);
}

function sanitize_text_field(string $value): string
{
    return trim($value);
}

function sanitize_key(string $value): string
{
    return strtolower(
        preg_replace('/[^a-z0-9_\-]/', '', strtolower($value)) ?? ''
    );
}

function absint($value): int
{
    return abs((int) $value);
}

function wp_json_encode($value)
{
    return json_encode($value);
}

function untrailingslashit(string $value): string
{
    return rtrim($value, '/');
}

function esc_url_raw(string $value): string
{
    return $value;
}

function status_header(int $code): void
{
}

function plugin_dir_path(string $file): string
{
    return dirname($file) . '/';
}

function plugin_dir_url(string $file): string
{
    return '';
}

if (!defined('ABSPATH')) {
    define('ABSPATH', sys_get_temp_dir() . '/');
}

if (!defined('LE_GLOBAL_CHATBOT_API_URL')) {
    // Deliberately unroutable/refused so a positive-path
    // maybe_stream_backend_response() test can exercise the REAL
    // stream_backend_response()/cURL call path without ever
    // depending on network availability - curl_exec() fails fast
    // and deterministically (connection refused) rather than the
    // test needing a real backend.
    define('LE_GLOBAL_CHATBOT_API_URL', 'http://127.0.0.1:1');
}

if (!defined('LE_GLOBAL_CHATBOT_API_KEY')) {
    define('LE_GLOBAL_CHATBOT_API_KEY', 'test-key-not-a-real-secret');
}

// class-le-global-chatbot-admin.php is require_once'd by the plugin
// file itself - stub it out so loading the plugin doesn't also need
// the entire admin surface's own dependencies.
$adminStubPath = sys_get_temp_dir() . '/le-global-chatbot-admin-stub-' . getmypid() . '.php';
file_put_contents(
    $adminStubPath,
    "<?php\nfinal class LE_Global_Chatbot_Admin { public static function init(): void {} }\n"
);

$pluginSource = file_get_contents(
    dirname(__DIR__) . '/le-global-chatbot.php'
);

$pluginSource = str_replace(
    "require_once plugin_dir_path(\n    __FILE__\n) . 'includes/class-le-global-chatbot-admin.php';",
    "require_once " . var_export($adminStubPath, true) . ';',
    $pluginSource
);

$pluginSource = str_replace(
    "LE_Global_Chatbot_Plugin::init();\nLE_Global_Chatbot_Admin::init();",
    '',
    $pluginSource
);

$pluginSource = preg_replace('/^<\?php\s*/', '', $pluginSource, 1);

eval($pluginSource);

unlink($adminStubPath);

// --- Test helpers -----------------------------------------------------

$failures = [];

function check(string $label, bool $condition, array &$failures): void
{
    if (!$condition) {
        $failures[] = $label;
        fwrite(STDERR, "FAIL: {$label}\n");
    }
}

function call_private_static(string $class, string $method, array $args = []): mixed
{
    $reflectionMethod = new \ReflectionMethod($class, $method);
    $reflectionMethod->setAccessible(true);

    return $reflectionMethod->invokeArgs(null, $args);
}

// --- Shared validation parity (mission section 5) ----------------------

$validRequest = new WP_REST_Request([
    'question' => 'What is the notice period in Spain?',
    'country_codes' => ['es', ' ES '],
    'max_sources' => 4,
]);

$payloadFromShared = call_private_static(
    'LE_Global_Chatbot_Plugin',
    'build_chat_payload_from_request',
    [$validRequest]
);

check(
    'valid request builds a payload array, not a WP_Error',
    is_array($payloadFromShared),
    $failures
);

check(
    'valid payload question is sanitized/trimmed',
    ($payloadFromShared['question'] ?? null)
        === 'What is the notice period in Spain?',
    $failures
);

check(
    'valid payload country_codes are uppercased and deduplicated',
    ($payloadFromShared['country_codes'] ?? null) === ['ES'],
    $failures
);

check(
    'valid payload max_sources reflects the requested value',
    ($payloadFromShared['max_sources'] ?? null) === 4,
    $failures
);

check(
    'valid payload language is always the server-fixed value, '
    . 'never client-controlled',
    ($payloadFromShared['language'] ?? null) === 'en',
    $failures
);

$tooShortQuestionRequest = new WP_REST_Request([
    'question' => 'x',
]);

$invalidPayload = call_private_static(
    'LE_Global_Chatbot_Plugin',
    'build_chat_payload_from_request',
    [$tooShortQuestionRequest]
);

check(
    'too-short question is rejected as a WP_Error',
    is_wp_error($invalidPayload),
    $failures
);

check(
    'too-short question WP_Error carries a 422 status',
    is_wp_error($invalidPayload)
        && ($invalidPayload->get_error_data()['status'] ?? null) === 422,
    $failures
);

$badMaxSourcesRequest = new WP_REST_Request([
    'question' => 'A valid question here.',
    'max_sources' => 999,
]);

$badMaxSourcesPayload = call_private_static(
    'LE_Global_Chatbot_Plugin',
    'build_chat_payload_from_request',
    [$badMaxSourcesRequest]
);

check(
    'out-of-range max_sources is rejected as a WP_Error',
    is_wp_error($badMaxSourcesPayload),
    $failures
);

// Both public route callbacks must reject the SAME invalid request
// identically (both call the shared builder and never proceed to a
// network call for a validation failure).

$streamResultForInvalid = LE_Global_Chatbot_Plugin::submit_chat_question_stream(
    $tooShortQuestionRequest
);

check(
    'submit_chat_question_stream() rejects an invalid request as a '
    . 'plain WP_Error (never a stream marker)',
    is_wp_error($streamResultForInvalid),
    $failures
);

// --- maybe_stream_backend_response() routing (mission sections 7/8) ---

function fresh_marker_response(): WP_REST_Response
{
    return new WP_REST_Response(
        new LE_Global_Chatbot_Stream_Marker(
            ['question' => 'irrelevant for this test'],
            null
        )
    );
}

$wrongRouteRequest = new WP_REST_Request(
    [],
    '/le-global-chatbot/v1/chat',
    'POST'
);

check(
    'a marker response on the WRONG route is never intercepted',
    LE_Global_Chatbot_Plugin::maybe_stream_backend_response(
        false,
        fresh_marker_response(),
        $wrongRouteRequest,
        null
    ) === false,
    $failures
);

$wrongMethodRequest = new WP_REST_Request(
    [],
    '/le-global-chatbot/v1/chat/stream',
    'GET'
);

check(
    'a marker response on the wrong HTTP method is never intercepted',
    LE_Global_Chatbot_Plugin::maybe_stream_backend_response(
        false,
        fresh_marker_response(),
        $wrongMethodRequest,
        null
    ) === false,
    $failures
);

$correctStreamRequest = new WP_REST_Request(
    [],
    '/le-global-chatbot/v1/chat/stream',
    'POST'
);

// This is the critical negative-security-boundary test (mission
// section 7): a route-callback validation failure, once normalized
// by WordPress core into a WP_REST_Response (rest_ensure_response()
// converts EVERY WP_Error before rest_pre_serve_request ever runs -
// see maybe_stream_backend_response()'s own docblock), must NEVER be
// mistaken for the one true stream marker merely because the route
// matches - route-name matching alone must never be sufficient.
$errorAsResponse = new WP_REST_Response(
    [
        'code' => 'le_global_invalid_question',
        'message' => 'Please enter a legal question.',
        'data' => ['status' => 422],
    ],
    422
);

check(
    'a WP_Error-shaped response on the correct route/method is '
    . 'still never intercepted (route match alone is insufficient)',
    LE_Global_Chatbot_Plugin::maybe_stream_backend_response(
        false,
        $errorAsResponse,
        $correctStreamRequest,
        null
    ) === false,
    $failures
);

check(
    'a non-WP_REST_Response result is never intercepted',
    LE_Global_Chatbot_Plugin::maybe_stream_backend_response(
        false,
        new WP_Error('whatever', 'whatever'),
        $correctStreamRequest,
        null
    ) === false,
    $failures
);

// Positive path: a genuine marker, on the correct route/method,
// takes over serving - proven by observing it returns true (meaning
// "I already served this, skip default JSON encoding") even though
// the configured backend URL is deliberately unroutable, so this
// exercises the REAL stream_backend_response()/cURL call path
// without depending on any live network target.
ob_start();
$positiveResult = LE_Global_Chatbot_Plugin::maybe_stream_backend_response(
    false,
    fresh_marker_response(),
    $correctStreamRequest,
    null
);
ob_end_clean();

check(
    'a genuine marker on the exact route/method takes over serving '
    . '(returns true)',
    $positiveResult === true,
    $failures
);

// --- sanitize_request_id() ---------------------------------------------

check(
    'a normal alphanumeric request id is preserved',
    call_private_static(
        'LE_Global_Chatbot_Plugin',
        'sanitize_request_id',
        ['s6-test-request-1']
    ) === 's6-test-request-1',
    $failures
);

check(
    'a request id with unsafe characters is dropped, not reflected',
    call_private_static(
        'LE_Global_Chatbot_Plugin',
        'sanitize_request_id',
        ["evil\r\nX-Injected: yes"]
    ) === null,
    $failures
);

check(
    'a null/absent request id yields null (backend auto-generates)',
    call_private_static(
        'LE_Global_Chatbot_Plugin',
        'sanitize_request_id',
        [null]
    ) === null,
    $failures
);

check(
    'an overly long request id is dropped',
    call_private_static(
        'LE_Global_Chatbot_Plugin',
        'sanitize_request_id',
        [str_repeat('a', 200)]
    ) === null,
    $failures
);

// --- Result -------------------------------------------------------------

if (!empty($failures)) {
    fwrite(
        STDERR,
        sprintf("\n%d check(s) failed.\n", count($failures))
    );

    exit(1);
}

echo "All chat-stream.test.php checks passed.\n";
exit(0);
