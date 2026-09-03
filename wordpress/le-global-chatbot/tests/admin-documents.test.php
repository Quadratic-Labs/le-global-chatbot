<?php

declare(strict_types=1);

/**
 * Permanent, framework-free CLI test for the document-upload and
 * country-conflict-resolution proxy handlers on LE_Global_Chatbot_Admin
 * (mission "ORDER 8E-A2") - handle_upload, handle_conflict_review,
 * handle_resolve_conflict, handle_resolve_conflict_replace. Matches
 * tests/admin-sections.test.php's own "no PHPUnit, no autoloader,
 * plain PHP reflection over the real plugin file" convention exactly
 * (a fresh, self-contained stub set - not shared with that file).
 *
 * A real HTTP multipart upload is not reproducible in a CLI script:
 * validate_and_read_uploaded_docx() calls PHP's own is_uploaded_file(),
 * which unconditionally returns false outside of a genuine SAPI file
 * upload, and this file (like the plugin itself) declares no
 * namespace to shadow it in. The gates reachable before that point
 * (capability, nonce, missing file, wrong extension) are fully
 * covered here; the full success round-trip through handle_upload and
 * handle_resolve_conflict_replace (country confirmation, content
 * warning, replacement, exact-current-file, REPLACE_WITH_DOCUMENT) is
 * verified instead by the real Chromium E2E suite against a live
 * WordPress+PHP+backend stack (Scenarios 1, 2, 4, 5, 7, 8, 11).
 *
 * Run with:
 *   php wordpress/le-global-chatbot/tests/admin-documents.test.php
 *
 * Exits 0 when every check passes, 1 otherwise (with a message on
 * stderr for whichever check failed).
 */

// --- Minimal WordPress function/class stubs -------------------------

final class TestHaltException extends \Exception
{
    public function __construct(
        public readonly ?bool $jsonSuccess,
        public readonly mixed $jsonData,
        public readonly int $statusCode
    ) {
        parent::__construct('halt');
    }
}

final class WP_Error
{
    public function __construct(private string $code, private string $message)
    {
    }

    public function get_error_message(): string
    {
        return $this->message;
    }

    public function get_error_code(): string
    {
        return $this->code;
    }
}

function is_wp_error($value): bool
{
    return $value instanceof WP_Error;
}

function add_action(...$args): void {}
function register_activation_hook(...$args): void {}
function register_deactivation_hook(...$args): void {}
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

$GLOBALS['__test_current_user_can'] = true;
$GLOBALS['__test_nonce_valid'] = true;
$GLOBALS['__test_fake_backend_response'] = null;
$GLOBALS['__test_backend_calls'] = [];

function current_user_can(string $capability): bool
{
    return $GLOBALS['__test_current_user_can'];
}

function wp_die($message = '', $title = '', $args = []): void
{
    throw new TestHaltException(
        null,
        is_string($message) ? $message : '',
        is_array($args) && isset($args['response']) ? (int) $args['response'] : 500
    );
}

// Both nonce-check functions collapse to the same halt shape here -
// real WordPress's own check_ajax_referer()/check_admin_referer() die
// with a bare "-1" body and HTTP 200 on failure (confirmed against a
// real WordPress instance), never a distinct status code of their
// own. The checks below only assert the halt itself and the
// zero-backend-calls property, never this exact status number.
function check_ajax_referer(string $action, $query_arg = false, bool $die = true): bool
{
    if (!$GLOBALS['__test_nonce_valid']) {
        throw new TestHaltException(null, '-1', 200);
    }

    return true;
}

function check_admin_referer(string $action, string $query_arg = '_wpnonce'): bool
{
    if (!$GLOBALS['__test_nonce_valid']) {
        throw new TestHaltException(null, '-1', 200);
    }

    return true;
}

function wp_send_json_success($data = null, ?int $status_code = null): void
{
    throw new TestHaltException(true, $data, $status_code ?? 200);
}

function wp_send_json_error($data = null, ?int $status_code = null): void
{
    throw new TestHaltException(false, $data, $status_code ?? 400);
}

function sanitize_text_field(string $value): string
{
    return trim($value);
}

function wp_unslash($value)
{
    if (is_array($value)) {
        return array_map('wp_unslash', $value);
    }

    return is_string($value) ? stripslashes($value) : $value;
}

function wp_json_encode($data)
{
    return json_encode($data);
}

function untrailingslashit(string $value): string
{
    return rtrim($value, '/');
}

function esc_url_raw(string $value): string
{
    return $value;
}

function esc_html__(string $text, string $domain = 'default'): string
{
    return $text;
}

function esc_html(string $text): string
{
    return $text;
}

function esc_attr(string $text): string
{
    return $text;
}

/**
 * Mirrors the ONE property every check below depends on: WordPress's
 * real esc_url() turns a bare "&" into the HTML entity "&#038;" for
 * safe HTML-attribute display, and ALSO normalizes an
 * already-present "&amp;" (exactly what wp_nonce_url() itself used to
 * produce) to that same "&#038;" - via wp_kses_normalize_entities()
 * plus a dedicated str_replace('&amp;', '&#038;', ...) in real
 * WordPress. This is WHY the double-escaping incident this file
 * guards against did not literally show "&amp;amp;" in the PHP-
 * rendered <a href> path (a bare "&" and an already-escaped "&amp;"
 * both collapse to the same single "&#038;" here) - the incident
 * instead reached production through the JSON/JS rendering path
 * (assets/admin.js's own escapeHtml(), which has no such
 * normalization and single-escapes whatever string it is given,
 * verbatim).
 */
function esc_url(string $url): string
{
    $url = preg_replace(
        '/&(?!(?:amp|lt|gt|quot|#0*39|#0*38);)/',
        '&#038;',
        $url
    );

    return str_replace('&amp;', '&#038;', $url);
}

function admin_url(string $path = ''): string
{
    return 'https://example.test/wp-admin/' . ltrim($path, '/');
}

/**
 * The one call shape this plugin actually uses: add_query_arg(array
 * $args, string $url). Real WordPress's own add_query_arg() returns a
 * RAW url with bare "&" separators (never HTML-escaped) - this stub
 * matches that exactly via http_build_query()'s own default "&"
 * separator.
 */
function add_query_arg(array $args, string $url): string
{
    $parts = parse_url($url);
    $existing_query = [];

    if (!empty($parts['query'])) {
        parse_str($parts['query'], $existing_query);
    }

    $query = array_merge($existing_query, $args);
    $base = (
        ($parts['scheme'] ?? 'https') . '://'
        . ($parts['host'] ?? '')
        . ($parts['path'] ?? '')
    );

    return $base . '?' . http_build_query($query, '', '&');
}

/**
 * A deterministic, action-bound test nonce - real WordPress salts
 * this with a rotating per-install/per-session secret; the ONE
 * property every check here actually depends on is that the SAME
 * action string always produces the SAME token and a DIFFERENT
 * action string always produces a DIFFERENT one, which this
 * reproduces exactly.
 */
function wp_create_nonce($action = -1): string
{
    return substr(md5('test-nonce-secret:' . $action), 0, 10);
}

function wp_verify_nonce(string $nonce, $action = -1): bool
{
    return hash_equals(wp_create_nonce($action), $nonce);
}

/**
 * Other admin rendering paths call wp_nonce_field() directly. A
 * minimal, real-shaped hidden input is enough for this framework-free
 * rendering harness; no test here inspects its contents.
 */
function wp_nonce_field(
    $action = -1,
    string $name = '_wpnonce',
    bool $referer = true,
    bool $echo = true
) {
    $field = (
        '<input type="hidden" name="' . $name . '" '
        . 'value="' . wp_create_nonce($action) . '">'
    );

    if ($echo) {
        echo $field;
    }

    return $field;
}

function sanitize_key(string $value): string
{
    return strtolower(preg_replace('/[^a-z0-9_\-]/', '', strtolower($value)) ?? '');
}

/**
 * A fake wp_remote_request that records every call and returns
 * whatever $GLOBALS['__test_fake_backend_response'] currently holds.
 */
function wp_remote_request(string $url, array $args = [])
{
    $GLOBALS['__test_backend_calls'][] = [
        'url' => $url,
        'method' => $args['method'] ?? 'GET',
        'body' => $args['body'] ?? null,
        'headers' => $args['headers'] ?? [],
    ];

    return $GLOBALS['__test_fake_backend_response'];
}

function wp_remote_retrieve_response_code($response)
{
    return $response['response']['code'] ?? 0;
}

function wp_remote_retrieve_body($response)
{
    return $response['body'] ?? '';
}

require __DIR__ . '/../includes/class-le-global-chatbot-admin.php';

function invoke_handler(string $method_name)
{
    $reflection = new ReflectionClass('LE_Global_Chatbot_Admin');
    $handler = $reflection->getMethod($method_name);
    $handler->setAccessible(true);

    try {
        $handler->invoke(null);

        return null;
    } catch (TestHaltException $halt) {
        return $halt;
    }
}

function reset_state(array $request = [], array $post = [], array $files = []): void
{
    $GLOBALS['__test_current_user_can'] = true;
    $GLOBALS['__test_nonce_valid'] = true;
    $GLOBALS['__test_backend_calls'] = [];
    $_REQUEST = $request;
    $_POST = $post;
    $_GET = [];
    $_FILES = $files;
}

function fake_backend_json_response(int $status, array $body): array
{
    return [
        'response' => ['code' => $status, 'message' => ''],
        'body' => json_encode($body),
    ];
}

if (!defined('LE_GLOBAL_CHATBOT_API_URL')) {
    define('LE_GLOBAL_CHATBOT_API_URL', 'https://backend.test');
    define('LE_GLOBAL_CHATBOT_API_KEY', 'test-api-key');
    define('LE_GLOBAL_CHATBOT_ADMIN_API_KEY', 'test-admin-key');
}

$failures = 0;

function check(string $label, $actual, $expected): void
{
    global $failures;

    if ($actual === $expected) {
        fwrite(STDOUT, "PASS  {$label}\n");
        return;
    }

    $failures++;
    fwrite(
        STDERR,
        "FAIL  {$label}\n"
        . "      expected: " . var_export($expected, true) . "\n"
        . "      actual:   " . var_export($actual, true) . "\n"
    );
}

$DOCX_FIELD_OK = [
    'error' => UPLOAD_ERR_OK,
    'tmp_name' => '/tmp/does-not-matter.docx',
    'name' => 'mystery.docx',
];

// =====================================================================
// handle_conflict_review (read-only GET)
// =====================================================================

reset_state(['country_code' => 'NO']);
$GLOBALS['__test_current_user_can'] = false;
$halt = invoke_handler('handle_conflict_review');
check(
    'unauthorized user gets a 403 and zero backend calls (conflict_review)',
    [$halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [403, 0]
);

reset_state(['country_code' => 'NO']);
$GLOBALS['__test_nonce_valid'] = false;
$halt = invoke_handler('handle_conflict_review');
check(
    'an invalid nonce halts before any backend call (conflict_review)',
    [$halt !== null, count($GLOBALS['__test_backend_calls'])],
    [true, 0]
);

reset_state(['country_code' => 'not-a-code']);
$halt = invoke_handler('handle_conflict_review');
check(
    'a malformed country_code is rejected with 422 and zero backend calls (conflict_review)',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

reset_state(['country_code' => 'no']);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'country_code' => 'NO',
    'resolution_mode' => 'AUTO_DEDUPLICATE',
    'auto_deduplicate_available' => true,
    'candidates' => [
        ['document_id' => 'doc_no_a', 'source_filename' => 'norway.docx'],
        ['document_id' => 'doc_no_b', 'source_filename' => 'norway.docx'],
    ],
]);
$halt = invoke_handler('handle_conflict_review');
check(
    'a successful conflict review is relayed verbatim, country_code lower-cased input still resolved',
    [$halt?->jsonSuccess, $halt?->jsonData['resolution_mode'] ?? null, count($halt?->jsonData['candidates'] ?? [])],
    [true, 'AUTO_DEDUPLICATE', 2]
);
check(
    'the request targets the countries/{code}/conflict-review path with the upper-cased code',
    [
        $GLOBALS['__test_backend_calls'][0]['method'] ?? null,
        str_ends_with($GLOBALS['__test_backend_calls'][0]['url'] ?? '', '/documents/countries/NO/conflict-review'),
    ],
    ['GET', true]
);

reset_state(['country_code' => 'NO']);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(404, [
    'detail' => ['code' => 'document_not_found', 'message' => 'No conflict is currently active for this country.'],
]);
$halt = invoke_handler('handle_conflict_review');
check(
    'a structured 404 backend error is propagated with its real message and code',
    [$halt?->jsonSuccess, $halt?->statusCode, $halt?->jsonData['message'] ?? null, $halt?->jsonData['detail']['code'] ?? null],
    [false, 404, 'No conflict is currently active for this country.', 'document_not_found']
);

reset_state(['country_code' => 'NO']);
$GLOBALS['__test_fake_backend_response'] = new WP_Error('http_request_failed', 'cURL error 7: Failed to connect');
$halt = invoke_handler('handle_conflict_review');
check(
    'a true backend outage (WP_Error, e.g. connection refused) is a generic 503, never the raw transport error',
    [$halt?->jsonSuccess, $halt?->statusCode],
    [false, 503]
);
check(
    'the outage message is business-friendly, not the raw cURL text',
    str_contains((string) ($halt?->jsonData['message'] ?? ''), 'cURL'),
    false
);

// =====================================================================
// handle_resolve_conflict (AUTO_DEDUPLICATE / CHOOSE_DOCUMENT, no file)
// =====================================================================

reset_state(['country_code' => 'NO'], ['resolution_mode' => 'AUTO_DEDUPLICATE']);
$GLOBALS['__test_current_user_can'] = false;
$halt = invoke_handler('handle_resolve_conflict');
check(
    'unauthorized user gets a 403 and zero backend calls (resolve_conflict)',
    [$halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [403, 0]
);

reset_state(['country_code' => 'NO'], ['resolution_mode' => 'AUTO_DEDUPLICATE']);
$GLOBALS['__test_nonce_valid'] = false;
$halt = invoke_handler('handle_resolve_conflict');
check(
    'an invalid nonce halts before any backend call (resolve_conflict)',
    [$halt !== null, count($GLOBALS['__test_backend_calls'])],
    [true, 0]
);

reset_state(['country_code' => 'NO'], []);
$halt = invoke_handler('handle_resolve_conflict');
check(
    'a missing resolution_mode is rejected with 422 and zero backend calls',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

reset_state(['country_code' => 'NO'], ['resolution_mode' => 'AUTO_DEDUPLICATE']);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'status' => 'resolved',
    'country_code' => 'NO',
    'remaining_document_id' => 'doc_no_b',
]);
$halt = invoke_handler('handle_resolve_conflict');
check(
    'AUTO_DEDUPLICATE is forwarded as a plain url-encoded POST with no keep_document_id key at all',
    [
        $GLOBALS['__test_backend_calls'][0]['method'] ?? null,
        $GLOBALS['__test_backend_calls'][0]['body'] ?? null,
        $GLOBALS['__test_backend_calls'][0]['headers']['Content-Type'] ?? null,
    ],
    [
        'POST',
        'resolution_mode=AUTO_DEDUPLICATE',
        'application/x-www-form-urlencoded; charset=UTF-8',
    ]
);
check(
    'the request targets the countries/{code}/resolve-conflict path, exactly once',
    [
        str_ends_with($GLOBALS['__test_backend_calls'][0]['url'] ?? '', '/documents/countries/NO/resolve-conflict'),
        count($GLOBALS['__test_backend_calls']),
    ],
    [true, 1]
);
check(
    'a successful AUTO_DEDUPLICATE resolution is relayed via wp_send_json_success',
    [$halt?->jsonSuccess, $halt?->jsonData['status'] ?? null],
    [true, 'resolved']
);

reset_state(['country_code' => 'CH'], [
    'resolution_mode' => 'CHOOSE_DOCUMENT',
    'keep_document_id' => 'doc_ch_current',
]);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'status' => 'resolved',
    'country_code' => 'CH',
]);
$halt = invoke_handler('handle_resolve_conflict');
check(
    'CHOOSE_DOCUMENT forwards both resolution_mode and keep_document_id',
    $GLOBALS['__test_backend_calls'][0]['body'] ?? null,
    'resolution_mode=CHOOSE_DOCUMENT&keep_document_id=doc_ch_current'
);
check(
    'a successful CHOOSE_DOCUMENT resolution is relayed via wp_send_json_success',
    $halt?->jsonSuccess,
    true
);

reset_state(['country_code' => 'IT'], ['resolution_mode' => 'AUTO_DEDUPLICATE']);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(409, [
    'detail' => [
        'code' => 'country_document_conflict',
        'message' => 'This resolution is no longer valid - the conflict for Italy has already changed.',
    ],
]);
$halt = invoke_handler('handle_resolve_conflict');
check(
    'a structured business error (e.g. the conflict already changed) is propagated with its real code and message, never a generic fallback',
    [$halt?->jsonSuccess, $halt?->statusCode, $halt?->jsonData['detail']['code'] ?? null, $halt?->jsonData['message'] ?? null],
    [false, 409, 'country_document_conflict', 'This resolution is no longer valid - the conflict for Italy has already changed.']
);

// =====================================================================
// handle_resolve_conflict_replace (REPLACE_WITH_DOCUMENT, has a file)
// =====================================================================
//
// Only the gates reachable before is_uploaded_file() are testable
// here (see the file header) - capability, nonce, country_code, and
// the file-shape checks (missing file / wrong extension). The full
// accepted-file round trip is covered by the Chromium E2E suite
// (Scenario 11).

reset_state(['country_code' => 'CZ'], [], ['document' => $DOCX_FIELD_OK]);
$GLOBALS['__test_current_user_can'] = false;
$halt = invoke_handler('handle_resolve_conflict_replace');
check(
    'unauthorized user gets a 403 and zero backend calls (resolve_conflict_replace)',
    [$halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [403, 0]
);

reset_state(['country_code' => 'CZ'], [], ['document' => $DOCX_FIELD_OK]);
$GLOBALS['__test_nonce_valid'] = false;
$halt = invoke_handler('handle_resolve_conflict_replace');
check(
    'an invalid nonce halts before any backend call (resolve_conflict_replace)',
    [$halt !== null, count($GLOBALS['__test_backend_calls'])],
    [true, 0]
);

reset_state(['country_code' => 'not-a-code'], [], ['document' => $DOCX_FIELD_OK]);
$halt = invoke_handler('handle_resolve_conflict_replace');
check(
    'a malformed country_code is rejected with 422 and zero backend calls (resolve_conflict_replace)',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

reset_state(['country_code' => 'CZ'], [], []);
$halt = invoke_handler('handle_resolve_conflict_replace');
check(
    'no file at all is rejected with a business-friendly message and zero backend calls',
    [$halt?->jsonSuccess, count($GLOBALS['__test_backend_calls']), $halt?->jsonData['message'] ?? null],
    [false, 0, 'No DOCX document was received.']
);

reset_state(['country_code' => 'CZ'], [], [
    'document' => [
        'error' => UPLOAD_ERR_OK,
        'tmp_name' => '/tmp/does-not-matter.txt',
        'name' => 'not-a-docx.txt',
    ],
]);
$halt = invoke_handler('handle_resolve_conflict_replace');
check(
    'a non-DOCX file extension is rejected with a business-friendly message and zero backend calls',
    [$halt?->jsonSuccess, count($GLOBALS['__test_backend_calls']), $halt?->jsonData['message'] ?? null],
    [false, 0, 'Only DOCX documents are accepted.']
);

// =====================================================================
// handle_upload - the same capability/nonce/file-shape gates, shared
// with handle_resolve_conflict_replace via validate_and_read_uploaded_
// docx (never a second, parallel implementation of those checks).
// =====================================================================

reset_state([], ['le_global_ajax' => '1'], ['document' => $DOCX_FIELD_OK]);
$GLOBALS['__test_current_user_can'] = false;
$halt = invoke_handler('handle_upload');
check(
    'unauthorized user gets a 403 and zero backend calls (upload)',
    [$halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [403, 0]
);

reset_state([], ['le_global_ajax' => '1'], ['document' => $DOCX_FIELD_OK]);
$GLOBALS['__test_nonce_valid'] = false;
$halt = invoke_handler('handle_upload');
check(
    'an invalid nonce halts before any backend call (upload)',
    [$halt !== null, count($GLOBALS['__test_backend_calls'])],
    [true, 0]
);

reset_state([], ['le_global_ajax' => '1'], []);
$halt = invoke_handler('handle_upload');
check(
    'no file at all is rejected with a business-friendly message and zero backend calls (upload)',
    [$halt?->jsonSuccess, count($GLOBALS['__test_backend_calls']), $halt?->jsonData['message'] ?? null],
    [false, 0, 'No DOCX document was received.']
);

reset_state([], ['le_global_ajax' => '1'], [
    'document' => [
        'error' => UPLOAD_ERR_OK,
        'tmp_name' => '/tmp/does-not-matter.pdf',
        'name' => 'contract.pdf',
    ],
]);
$halt = invoke_handler('handle_upload');
check(
    'a non-DOCX file extension is rejected with a business-friendly message and zero backend calls (upload)',
    [$halt?->jsonSuccess, count($GLOBALS['__test_backend_calls']), $halt?->jsonData['message'] ?? null],
    [false, 0, 'Only DOCX documents are accepted.']
);

// =====================================================================
// Mission "ORDER 8E-A2C" - user-facing jargon regression on the
// private message-builder methods. The JS-driven success paths for
// delete/reindex never render these PHP strings at all (they show
// their own canned "✓ ... successfully." text instead - see
// admin.js's runFormAsAjax callers), so this is the only place that
// actually exercises build_delete_success_message()'s two branches
// and confirms neither contains "chunk(s)"/"indexed" - the only
// caller-reachable path for that text is the no-JS fallback notice.
// =====================================================================

function invoke_private_static(string $method_name, array $args)
{
    $reflection = new ReflectionClass('LE_Global_Chatbot_Admin');
    $method = $reflection->getMethod($method_name);
    $method->setAccessible(true);

    return $method->invokeArgs(null, $args);
}

$delete_message_normal = invoke_private_static(
    'build_delete_success_message',
    ['mystery.docx', 12, false]
);
check(
    'build_delete_success_message (no deferred cleanup) contains no chunk/indexed jargon',
    (bool) preg_match('/chunk|indexed/i', $delete_message_normal),
    false
);
check(
    'build_delete_success_message (no deferred cleanup) is still business-friendly and names the file',
    $delete_message_normal,
    'mystery.docx was deleted successfully.'
);

$delete_message_deferred = invoke_private_static(
    'build_delete_success_message',
    ['mystery.docx', 12, true]
);
check(
    'build_delete_success_message (deferred cleanup) contains no chunk/indexed jargon',
    (bool) preg_match('/chunk|indexed/i', $delete_message_deferred),
    false
);
check(
    'build_delete_success_message (deferred cleanup) still communicates the deferred state in business terms',
    $delete_message_deferred,
    'mystery.docx was deleted successfully. Cleanup of some related files is still in progress.'
);

$VALID_DOCUMENT_ID = 'doc_' . str_repeat('a', 64);

reset_state(
    ['document_id' => $VALID_DOCUMENT_ID, 'le_global_ajax' => '1'],
    ['le_global_ajax' => '1']
);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'source_filename' => 'mystery.docx',
    'indexed_chunks' => 42,
]);
$halt = invoke_handler('handle_reindex');
check(
    'handle_reindex success message contains no chunk/indexed/reindex jargon',
    (bool) preg_match('/chunk|indexed|reindex/i', $halt?->jsonData['message'] ?? ''),
    false
);
check(
    'handle_reindex success message is business-friendly and names the file',
    [$halt?->jsonSuccess, $halt?->jsonData['message'] ?? null],
    [true, 'mystery.docx was refreshed successfully.']
);

reset_state(
    ['document_id' => $VALID_DOCUMENT_ID, 'le_global_ajax' => '1'],
    ['le_global_ajax' => '1']
);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(503, []);
$halt = invoke_handler('handle_reindex');
check(
    'handle_reindex generic failure fallback contains no reindex/chunk jargon',
    (bool) preg_match('/chunk|reindex/i', $halt?->jsonData['message'] ?? ''),
    false
);
check(
    'handle_reindex generic failure fallback is business-friendly',
    $halt?->jsonData['message'] ?? null,
    'The chatbot data could not be refreshed.'
);

// --- handle_download (mission "ORDER 8G-A", section 10) ---------------
//
// A real download response is not reproducible in this CLI script:
// success falls through to a bare `exit;` after streaming binary
// bytes (never wp_die/wp_send_json_*, so nothing here can intercept
// it without terminating the test script itself), and the real fix
// (removing the previously-declared Content-Length header, discarding
// any active output buffer, and asking Apache to skip compressing
// this one response) is about the EXACT BYTES/headers a real HTTP
// response carries - only observable end-to-end. This file's own
// upload tests already establish the same precedent for exactly this
// class of limitation (see the file docstring). What IS verified
// here: the two gates that fire before any of that (capability,
// nonce) - both real, reachable, wp_die()-based halts - plus a
// structural check (via reflection over the real method source) that
// the fix is actually present. The full real success-with-no-false-
// error / real-failure-shows-error contract is proven by the mission's
// own real Chromium canary pass instead.

reset_state(['document_id' => $VALID_DOCUMENT_ID]);
$GLOBALS['__test_current_user_can'] = false;
$halt = invoke_handler('handle_download');
check(
    'unauthorized user gets a wp_die halt and zero backend calls (handle_download)',
    [$halt !== null, count($GLOBALS['__test_backend_calls'])],
    [true, 0]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID]);
$GLOBALS['__test_nonce_valid'] = false;
$halt = invoke_handler('handle_download');
check(
    'an invalid nonce halts handle_download before any backend call',
    [$halt !== null, count($GLOBALS['__test_backend_calls'])],
    [true, 0]
);

$download_reflection = new ReflectionMethod('LE_Global_Chatbot_Admin', 'handle_download');
$download_source_lines = array_slice(
    file($download_reflection->getFileName()),
    $download_reflection->getStartLine() - 1,
    $download_reflection->getEndLine() - $download_reflection->getStartLine() + 1
);
$download_source = implode('', $download_source_lines);

check(
    'handle_download no longer declares its own (potentially stale) Content-Length header',
    str_contains($download_source, "'Content-Length:"),
    false
);
check(
    'handle_download discards any active output buffering before streaming the file',
    str_contains($download_source, 'ob_end_clean'),
    true
);
check(
    'handle_download asks Apache to skip compressing this one response',
    str_contains($download_source, "apache_setenv('no-gzip'"),
    true
);
check(
    'handle_download still declares the correct DOCX Content-Type',
    str_contains(
        $download_source,
        'application/vnd.openxmlformats-'
    ),
    true
);

// --- handle_download byte-transparency (mission "DOCX HARDENING",
// 2026-08-24, section 9/10): live-proven byte-for-byte identical
// (backend-direct vs WordPress-admin-post) as long as this function's
// body never gains a SECOND output statement between the backend
// fetch and the raw echo - any HTML wrapper, debug dump, or stray
// whitespace outside PHP tags placed there would silently corrupt
// every real download. Token-aware (like the wp_nonce_url guard
// above): a doc-comment mentioning "echo" in prose must never
// false-fail this.

$download_tokens = token_get_all('<?php ' . $download_source);
$download_code_only = '';

foreach ($download_tokens as $token) {
    if (is_array($token)) {
        if (in_array($token[0], [T_COMMENT, T_DOC_COMMENT], true)) {
            continue;
        }
        $download_code_only .= $token[1];
    } else {
        $download_code_only .= $token;
    }
}

$echo_statement_count = preg_match_all(
    '/\becho\b/',
    $download_code_only
);

check(
    'handle_download contains exactly one echo statement (the raw body)',
    $echo_statement_count,
    1
);
check(
    'handle_download\'s one echo is $body itself, not a wrapped/concatenated value',
    (bool) preg_match('/echo\s+\$body\s*;/', $download_code_only),
    true
);
check(
    'handle_download never calls print/print_r/var_dump/var_export/printf '
    . '(any of which would contaminate the binary response body)',
    (bool) preg_match(
        '/\b(print|print_r|var_dump|var_export|printf)\s*\(/',
        $download_code_only
    ),
    false
);
check(
    'the echo of $body is the statement immediately before exit',
    (bool) preg_match('/echo\s+\$body\s*;\s*exit\s*;/', $download_code_only),
    true
);

// =====================================================================
// build_download_url() raw-url contract (production double-escaping
// incident: wp_nonce_url() returns an HTML-escaped url; a SECOND
// esc_url() at render time - or, worse, shipping that pre-escaped
// string as if it were raw JSON data for assets/admin.js's own
// escapeHtml() to escape a SECOND time - corrupted document_id/
// _wpnonce into unparseable query keys and produced "The document
// identifier is invalid.").
//
// build_download_url() must now return a RAW url; every consumer
// escapes exactly once, for its OWN boundary.
// =====================================================================

$download_action_constant = (
    new ReflectionClass('LE_Global_Chatbot_Admin')
)->getConstant('DOWNLOAD_ACTION');

$REALISTIC_DOCUMENT_IDS = [
    'doc_' . str_repeat('a1b2c3d4e5f60718', 4),
    'doc_' . str_repeat('9f8e7d6c5b4a3021', 4),
    'doc_' . str_repeat('0123456789abcdef', 4),
];

foreach ($REALISTIC_DOCUMENT_IDS as $document_id) {
    check(
        "build_download_url produces a well-formed 64-hex document id fixture ({$document_id})",
        (bool) preg_match('/^doc_[0-9a-f]{64}$/', $document_id),
        true
    );

    $raw_url = invoke_private_static(
        'build_download_url',
        [$document_id]
    );

    // 1. Contains the three required pieces.
    check(
        "raw url contains action=le_global_chatbot_download_document ({$document_id})",
        str_contains(
            $raw_url,
            'action=' . $download_action_constant
        ),
        true
    );
    check(
        "raw url contains document_id=<exact id> ({$document_id})",
        str_contains($raw_url, 'document_id=' . $document_id),
        true
    );
    check(
        "raw url contains _wpnonce= ({$document_id})",
        (bool) preg_match('/(?:^|&)_wpnonce=/', $raw_url),
        true
    );

    // 2. Never contains any double-escaping artifact - the exact
    // permanent regression guard: a future change that reintroduces
    // wp_nonce_url() (or any other pre-escaped producer) inside
    // build_download_url() would fail these checks immediately.
    foreach (['&amp;', '&#038;', '&amp;amp;', '%26amp%3B'] as $artifact) {
        check(
            "raw url does not contain \"{$artifact}\" ({$document_id})",
            str_contains($raw_url, $artifact),
            false
        );
    }

    // 3. Parsed as a real query string, exactly the three expected
    // keys are present, each exactly once.
    $query_string = (string) parse_url($raw_url, PHP_URL_QUERY);
    parse_str($query_string, $parsed_query);

    check(
        "parsed query has exactly action/document_id/_wpnonce, nothing else ({$document_id})",
        array_keys($parsed_query),
        ['action', 'document_id', '_wpnonce']
    );

    $key_occurrences = array_count_values(
        array_map(
            static fn ($pair) => explode('=', $pair, 2)[0],
            explode('&', $query_string)
        )
    );
    check(
        "each of action/document_id/_wpnonce appears exactly once in the raw query ({$document_id})",
        $key_occurrences,
        ['action' => 1, 'document_id' => 1, '_wpnonce' => 1]
    );

    // 4. document_id survives byte-for-byte.
    check(
        "document_id is byte-for-byte unchanged after the round trip ({$document_id})",
        $parsed_query['document_id'] ?? null,
        $document_id
    );

    // 5. The generated nonce validates against the EXACT action
    // handle_download() itself checks (self::DOWNLOAD_ACTION . ':' .
    // $document_id) - and does NOT validate against a different
    // document_id's action, proving it is genuinely bound to this
    // one document, not a generic/shared token.
    check(
        "the generated nonce validates against the exact download-handler action ({$document_id})",
        wp_verify_nonce(
            $parsed_query['_wpnonce'] ?? '',
            $download_action_constant . ':' . $document_id
        ),
        true
    );
    check(
        "the generated nonce does NOT validate against a different document_id's action ({$document_id})",
        wp_verify_nonce(
            $parsed_query['_wpnonce'] ?? '',
            $download_action_constant . ':' . $document_id . '-tampered'
        ),
        false
    );
}

// --- 6. Existing download-handler nonce verification still passes -----
//
// handle_download() itself calls check_admin_referer(DOWNLOAD_ACTION
// . ':' . $document_id) - this file's own stub collapses that to the
// $GLOBALS['__test_nonce_valid'] toggle (see the file docstring for
// why a real per-action simulation is not needed for THAT gate); what
// matters here is that the fix did not disturb this call in any way.
$download_reflection_for_nonce = new ReflectionMethod(
    'LE_Global_Chatbot_Admin',
    'handle_download'
);
$download_source_for_nonce = implode(
    '',
    array_slice(
        file($download_reflection_for_nonce->getFileName()),
        $download_reflection_for_nonce->getStartLine() - 1,
        (
            $download_reflection_for_nonce->getEndLine()
            - $download_reflection_for_nonce->getStartLine() + 1
        )
    )
);
check(
    'handle_download still verifies the nonce against DOWNLOAD_ACTION . ":" . $document_id',
    str_contains(
        $download_source_for_nonce,
        'check_admin_referer('
    )
    && str_contains(
        $download_source_for_nonce,
        "self::DOWNLOAD_ACTION . ':' . \$document_id"
    ),
    true
);

// Not re-invoking handle_download() end-to-end here on purpose: past
// its nonce gate it calls wp_remote_get() directly against a real
// backend configuration (get_backend_configuration()), neither of
// which this file stubs (see the file's own docstring for why a full
// success round-trip through handle_download is proven by the real
// Chromium canary instead, never this CLI harness). The nonce
// contract itself - a VALID, correctly-bound nonce - is already fully
// proven above via wp_verify_nonce() directly against the exact
// action string check_admin_referer() uses.

// =====================================================================
// Rendered HTML download link contract - the ACTUAL markup a browser
// receives, parsed with a real HTML parser (never fragile string
// replacement), for the row render_document_row() itself produces.
// =====================================================================

function invoke_render_document_row(
    array $document,
    array $conflicted_country_codes = []
): string {
    $reflection = new ReflectionClass('LE_Global_Chatbot_Admin');
    $method = $reflection->getMethod('render_document_row');
    $method->setAccessible(true);

    ob_start();
    $method->invoke(null, $document, $conflicted_country_codes);

    return ob_get_clean();
}

function invoke_render_documents_table_body(array $documents): string
{
    $reflection = new ReflectionClass('LE_Global_Chatbot_Admin');
    $method = $reflection->getMethod('render_documents_table_body');
    $method->setAccessible(true);

    ob_start();
    $method->invoke(null, $documents, null, []);

    return ob_get_clean();
}

check(
    'a real HTML parser (ext-dom) is available to verify the rendered download link',
    class_exists('DOMDocument'),
    true
);

$table_html = invoke_render_documents_table_body([
    [
        'document_id' => $REALISTIC_DOCUMENT_IDS[0],
        'country' => 'Australia',
        'country_code' => 'AU',
        'source_filename' => 'AU.docx',
        'reference_year' => 2026,
        'source_file_present' => true,
        'status' => 'indexed',
    ],
]);

$table_dom = new DOMDocument();
libxml_use_internal_errors(true);
$table_dom->loadHTML(
    '<!DOCTYPE html><html><body>' . $table_html . '</body></html>'
);
libxml_clear_errors();

$header_labels = [];

foreach ($table_dom->getElementsByTagName('th') as $header) {
    $header_labels[] = trim($header->textContent);
}

check(
    'the Documents table has no Year column and preserves the remaining headers',
    $header_labels,
    ['Country', 'Document', 'Status', 'Last updated', 'Actions']
);

foreach ($REALISTIC_DOCUMENT_IDS as $document_id) {
    $row_html = invoke_render_document_row([
        'document_id' => $document_id,
        'country' => 'Australia',
        'country_code' => 'AU',
        'source_filename' => 'AU.docx',
        'reference_year' => 2026,
        'source_file_present' => true,
        'status' => 'indexed',
    ]);

    $document_dom = new DOMDocument();
    libxml_use_internal_errors(true);
    $document_dom->loadHTML(
        '<!DOCTYPE html><html><body><table><tbody>'
        . $row_html
        . '</tbody></table></body></html>'
    );
    libxml_clear_errors();

    $download_href = null;

    foreach ($document_dom->getElementsByTagName('a') as $anchor) {
        if (trim($anchor->textContent) === 'Download') {
            $download_href = $anchor->getAttribute('href');
            break;
        }
    }

    check(
        "the rendered row has a Download link with an href ({$document_id})",
        $download_href !== null,
        true
    );
    check(
        "the rendered row contains exactly five cells and no Year value ({$document_id})",
        $document_dom->getElementsByTagName('td')->length,
        5
    );
    check(
        "the rendered row does not expose Refresh ({$document_id})",
        str_contains($row_html, 'Refresh chatbot data'),
        false
    );
    check(
        "the rendered row does not expose Delete ({$document_id})",
        str_contains($row_html, 'Delete document'),
        false
    );
    check(
        "the rendered row keeps reference_year out of visible text ({$document_id})",
        str_contains(
            preg_replace('/\s+/', ' ', strip_tags($row_html)),
            '2026'
        ),
        false
    );

    // DOMDocument::getAttribute() already returns the attribute value
    // exactly as a browser would (fully entity-decoded) - this is the
    // real browser-parsing semantics the mandatory test requires,
    // never a manual string replace.
    $decoded_query = (string) parse_url($download_href ?? '', PHP_URL_QUERY);
    parse_str($decoded_query, $decoded_params);

    check(
        "the parsed href query contains exactly action/document_id/_wpnonce ({$document_id})",
        array_keys($decoded_params),
        ['action', 'document_id', '_wpnonce']
    );
    check(
        "the parsed href has no \"amp;document_id\" or \"amp;_wpnonce\" key ({$document_id})",
        (
            isset($decoded_params['amp;document_id'])
            || isset($decoded_params['amp;_wpnonce'])
        ),
        false
    );
    check(
        "document_id in the rendered href is identical to the original ({$document_id})",
        $decoded_params['document_id'] ?? null,
        $document_id
    );

    // The href, once decoded exactly as the browser would, must be
    // usable by the download handler without reaching "The document
    // identifier is invalid." - simulate read_document_id()'s own
    // validation directly against the DECODED document_id.
    check(
        "the decoded document_id from the rendered link passes read_document_id()'s own validation ({$document_id})",
        (bool) preg_match(
            '/^doc_[0-9a-f]{64}$/',
            $decoded_params['document_id'] ?? ''
        ),
        true
    );
}

// =====================================================================
// The failure mode itself: a malformed ("&amp;"-joined, as literal
// text) navigation url does NOT parse to the same request parameters
// as the correct ("&"-joined) form - this is preserved as a permanent
// record of the incident, never as something the application (or the
// backend) is made to tolerate.
// =====================================================================

$malformed_literal_url = (
    '?action=le_global_chatbot_download_document'
    . '&amp;document_id=doc_' . str_repeat('a', 64)
    . '&amp;_wpnonce=abc123'
);
$wellformed_url = (
    '?action=le_global_chatbot_download_document'
    . '&document_id=doc_' . str_repeat('a', 64)
    . '&_wpnonce=abc123'
);

parse_str((string) parse_url($malformed_literal_url, PHP_URL_QUERY), $malformed_parsed);
parse_str((string) parse_url($wellformed_url, PHP_URL_QUERY), $wellformed_parsed);

check(
    'a literal "&amp;"-joined query string does NOT parse to a usable document_id key',
    array_key_exists('document_id', $malformed_parsed),
    false
);
check(
    'the SAME query, correctly "&"-joined, DOES parse to a usable document_id key',
    $wellformed_parsed['document_id'] ?? null,
    'doc_' . str_repeat('a', 64)
);
check(
    'the malformed form instead produces a nonsensical "amp;document_id" key - exactly what production observed',
    array_key_exists('amp;document_id', $malformed_parsed),
    true
);

// Now prove the application itself never GENERATES the malformed
// form as an actual navigation url - across every fixture above, the
// raw url and the rendered/decoded href both already proved this
// (loop above); this final check locks in that the FIX is what
// prevents generation, not any backend/server-side tolerance for the
// malformed shape (grep the whole plugin, not just this one method,
// for any remaining pre-escaped producer).
// Tokenized, not a bare grep, specifically so this guard survives
// this very file's own doc-comments naming wp_nonce_url() by name to
// explain what NOT to do (a plain substring search would false-fail
// on prose, exactly the "brittle global grep" the fix's own mission
// warns against).
$admin_php_source = file_get_contents(
    __DIR__ . '/../includes/class-le-global-chatbot-admin.php'
);
$render_page_method = new ReflectionMethod(
    'LE_Global_Chatbot_Admin',
    'render_page'
);
$admin_php_lines = file(
    __DIR__ . '/../includes/class-le-global-chatbot-admin.php'
);
$render_page_source = implode(
    '',
    array_slice(
        $admin_php_lines,
        $render_page_method->getStartLine() - 1,
        $render_page_method->getEndLine()
            - $render_page_method->getStartLine()
            + 1
    )
);
$render_upload_method = new ReflectionMethod(
    'LE_Global_Chatbot_Admin',
    'render_upload_panel'
);
$render_upload_source = implode(
    '',
    array_slice(
        $admin_php_lines,
        $render_upload_method->getStartLine() - 1,
        $render_upload_method->getEndLine()
            - $render_upload_method->getStartLine()
            + 1
    )
);
$render_upload_visible_text = preg_replace(
    '/\s+/',
    ' ',
    strip_tags($render_upload_source)
);
$warning_text = 'Upload a country-specific document to replace the existing one (including all information and contact details)';

check(
    'the upload panel renders the exact required warning text',
    str_contains($render_upload_visible_text, $warning_text),
    true
);
$warning_position = strpos(
    $render_upload_source,
    'Upload a country-specific document to replace the'
);
$description_position = strpos(
    $render_upload_source,
    'Maximum file size: 25 MB each.'
);
$dropzone_position = strpos(
    $render_upload_source,
    'le-global-chatbot-admin__dropzone'
);

check(
    'the warning is inside Upload after its description and before the dropzone',
    $description_position !== false
        && $warning_position !== false
        && $dropzone_position !== false
        && $description_position < $warning_position
        && $warning_position < $dropzone_position,
    true
);
check(
    'Overview is rendered once between Contacts and Documents',
    substr_count($render_page_source, 'self::render_overview_panel(') === 1
        && strpos($render_page_source, 'render_contacts_panel')
            < strpos($render_page_source, 'render_overview_panel')
        && strpos($render_page_source, 'render_overview_panel')
            < strpos($render_page_source, 'render_documents_panel'),
    true
);
check(
    'the Documents count element remains in the admin renderer',
    str_contains($admin_php_source, 'id="le-global-document-count"'),
    true
);
$admin_php_code_only = '';

foreach (token_get_all($admin_php_source) as $token) {
    if (is_array($token) && in_array($token[0], [T_COMMENT, T_DOC_COMMENT], true)) {
        continue;
    }

    $admin_php_code_only .= is_array($token) ? $token[1] : $token;
}

check(
    'wp_nonce_url() is never CALLED anywhere in the plugin, outside comments (the one producer of this incident class)',
    str_contains($admin_php_code_only, 'wp_nonce_url('),
    false
);

// =====================================================================
// render_notice (ORDER 8G-A.1 - the PRG notice must not persist across
// a plain reload of the same URL once it has been shown once)
// =====================================================================

function invoke_render_notice(array $get): string
{
    $_GET = $get;

    $reflection = new ReflectionClass('LE_Global_Chatbot_Admin');
    $method = $reflection->getMethod('render_notice');
    $method->setAccessible(true);

    ob_start();
    $method->invoke(null);

    return ob_get_clean();
}

$output = invoke_render_notice([]);
check(
    'a clean page load (no notice query params) renders nothing',
    $output,
    ''
);

$output = invoke_render_notice([
    'le_global_notice' => 'error',
    'le_global_message' => 'The document identifier is invalid.',
]);
check(
    'a genuinely present error notice still shows its message',
    str_contains($output, 'The document identifier is invalid.'),
    true
);
check(
    'the shown notice self-clears its own URL query params via history.replaceState',
    str_contains($output, 'history.replaceState') && str_contains($output, "'le_global_notice'") && str_contains($output, "'le_global_message'"),
    true
);

$output = invoke_render_notice([
    'le_global_notice' => 'success',
    'le_global_message' => 'The section was saved.',
]);
check(
    'a success notice also shows its message',
    str_contains($output, 'The section was saved.'),
    true
);
check(
    'a success notice also self-clears its URL query params',
    str_contains($output, 'history.replaceState'),
    true
);

$output = invoke_render_notice([
    'le_global_notice' => 'not-a-real-type',
    'le_global_message' => 'Should never show.',
]);
check(
    'an unrecognized notice type renders nothing (unchanged behavior)',
    $output,
    ''
);

$output = invoke_render_notice([
    'le_global_notice' => 'error',
    'le_global_message' => '',
]);
check(
    'an empty message renders nothing (unchanged behavior)',
    $output,
    ''
);

if ($failures > 0) {
    fwrite(STDERR, "\n{$failures} check(s) FAILED\n");
    exit(1);
}

fwrite(STDOUT, "\nAll admin-documents checks passed.\n");
exit(0);
