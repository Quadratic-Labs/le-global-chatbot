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

if ($failures > 0) {
    fwrite(STDERR, "\n{$failures} check(s) FAILED\n");
    exit(1);
}

fwrite(STDOUT, "\nAll admin-documents checks passed.\n");
exit(0);
