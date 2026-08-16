<?php

declare(strict_types=1);

/**
 * Permanent, framework-free CLI test for the "Add / Edit a section"
 * proxy handlers on LE_Global_Chatbot_Admin - list_sections/
 * get_section/update_section (mission "ORDER 5D") and add_section
 * (mission "ORDER 8A-C"/"ORDER 8B") - matching tests/extract-
 * message.test.php's own "no PHPUnit, no autoloader, plain PHP
 * reflection over the real plugin file" convention.
 *
 * Run with:
 *   php wordpress/le-global-chatbot/tests/admin-sections.test.php
 *
 * Exits 0 when every check passes, 1 otherwise (with a message on
 * stderr for whichever check failed).
 */

// --- Minimal WordPress function/class stubs -------------------------
//
// check_ajax_referer/wp_send_json_success/wp_send_json_error/wp_die
// each normally end the request (die()/exit) - here they throw a
// sentinel exception instead, carrying whatever the real handler
// would have sent, so the test can inspect it without the PHP
// process itself exiting.

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

// Controllable via $GLOBALS before each check - the real functions'
// behavior this test cares about (capability/nonce gates), never the
// full WordPress implementation.
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

function check_ajax_referer(string $action, $query_arg = false, bool $die = true): bool
{
    if (!$GLOBALS['__test_nonce_valid']) {
        // Real WordPress's own check_ajax_referer() dies with a bare
        // "-1" body and HTTP 200 on failure (confirmed against a real
        // WordPress instance, mission "ORDER 5D" browser E2E) - never
        // a distinct status code of its own. The checks below only
        // assert the halt itself and the zero-backend-calls property,
        // never this exact status number.
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
 * whatever $GLOBALS['__test_fake_backend_response'] currently holds -
 * the same shape request_backend() itself expects back from the real
 * function (a WP_Error, or an array with a numeric-status
 * 'response'/'code' and a 'body' string).
 */
function wp_remote_request(string $url, array $args = [])
{
    $GLOBALS['__test_backend_calls'][] = [
        'url' => $url,
        'method' => $args['method'] ?? 'GET',
        'body' => $args['body'] ?? null,
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

$reflection = new ReflectionClass('LE_Global_Chatbot_Admin');

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

function reset_state(array $request = [], array $post = []): void
{
    $GLOBALS['__test_current_user_can'] = true;
    $GLOBALS['__test_nonce_valid'] = true;
    $GLOBALS['__test_backend_calls'] = [];
    $_REQUEST = $request;
    $_POST = $post;
    $_GET = [];
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

$VALID_DOCUMENT_ID = 'doc_' . str_repeat('a', 64);

// --- Capability gate (all three handlers share assert_capability) --

reset_state(['document_id' => $VALID_DOCUMENT_ID]);
$GLOBALS['__test_current_user_can'] = false;
$halt = invoke_handler('handle_list_sections');
check(
    'unauthorized user gets a 403 and zero backend calls (list_sections)',
    [$halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [403, 0]
);

// --- Nonce gate ------------------------------------------------------

reset_state(['document_id' => $VALID_DOCUMENT_ID]);
$GLOBALS['__test_nonce_valid'] = false;
$halt = invoke_handler('handle_get_section');
check(
    'an invalid nonce halts before any backend call (get_section)',
    [$halt !== null, count($GLOBALS['__test_backend_calls'])],
    [true, 0]
);

// --- Invalid document_id / section_id --------------------------------

reset_state(['document_id' => 'not-a-real-id']);
$halt = invoke_handler('handle_list_sections');
check(
    'an invalid document_id is rejected with 422 and zero backend calls',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID, 'section_id' => '']);
$halt = invoke_handler('handle_get_section');
check(
    'an empty section_id is rejected with 422 and zero backend calls',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

// --- list_sections success/error propagation ------------------------

reset_state(['document_id' => $VALID_DOCUMENT_ID]);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'document_id' => $VALID_DOCUMENT_ID,
    'sections' => [['section_id' => 'sec-1', 'legal_topic' => 'Overtime']],
]);
$halt = invoke_handler('handle_list_sections');
check(
    'a successful sections list is relayed verbatim',
    [$halt?->jsonSuccess, $halt?->jsonData],
    [true, ['document_id' => $VALID_DOCUMENT_ID, 'sections' => [['section_id' => 'sec-1', 'legal_topic' => 'Overtime']]]]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID]);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(404, [
    'detail' => ['code' => 'document_not_found', 'message' => 'No indexed document was found for this identifier.'],
]);
$halt = invoke_handler('handle_list_sections');
check(
    'a 404 backend error is propagated with the real message and status',
    [$halt?->jsonSuccess, $halt?->statusCode, $halt?->jsonData['message'] ?? null],
    [false, 404, 'No indexed document was found for this identifier.']
);

// --- get_section success ---------------------------------------------

reset_state(['document_id' => $VALID_DOCUMENT_ID, 'section_id' => 'sec-1']);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'document_id' => $VALID_DOCUMENT_ID,
    'country_code' => 'CA',
    'country_name' => 'Canada',
    'section_id' => 'sec-1',
    'legal_topic' => 'Overtime',
    'content' => "Line one.\nLine two, with \"quotes\" and Unicode: café, 日本語.",
]);
$halt = invoke_handler('handle_get_section');
check(
    'a successful section fetch relays the exact effective content, unicode and newlines intact',
    [$halt?->jsonSuccess, $halt?->jsonData['content'] ?? null],
    [true, "Line one.\nLine two, with \"quotes\" and Unicode: café, 日本語."]
);

// --- update_section validation + content preservation ---------------

reset_state(['document_id' => $VALID_DOCUMENT_ID, 'section_id' => 'sec-1'], ['content' => '']);
$halt = invoke_handler('handle_update_section');
check(
    'empty content is rejected with 422 and zero backend calls',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

$exotic_content = "Paragraph one.\n\nParagraph two with a quote: \"employer's obligation\" & café.";
reset_state(
    ['document_id' => $VALID_DOCUMENT_ID, 'section_id' => 'sec-1'],
    ['content' => $exotic_content]
);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'document_id' => $VALID_DOCUMENT_ID,
    'section_id' => 'sec-1',
    'legal_topic' => 'Overtime',
    'indexed_chunks' => 3,
]);
$halt = invoke_handler('handle_update_section');
$sent_body = json_decode($GLOBALS['__test_backend_calls'][0]['body'] ?? '', true);
check(
    'the exact content (paragraphs, quotes, unicode) is forwarded to the backend unmodified',
    $sent_body['content'] ?? null,
    $exotic_content
);
check(
    'a successful update is relayed via wp_send_json_success',
    [$halt?->jsonSuccess, $halt?->jsonData['indexed_chunks'] ?? null],
    [true, 3]
);
check(
    'the update is sent as a PUT to the backend, exactly once',
    [$GLOBALS['__test_backend_calls'][0]['method'] ?? null, count($GLOBALS['__test_backend_calls'])],
    ['PUT', 1]
);

// --- update_section error propagation (section-specific error code) -

reset_state(
    ['document_id' => $VALID_DOCUMENT_ID, 'section_id' => 'sec-missing'],
    ['content' => 'New content.']
);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(404, [
    'detail' => ['code' => 'document_section_not_found', 'message' => 'No section "sec-missing" exists for this document.'],
]);
$halt = invoke_handler('handle_update_section');
check(
    'a section-not-found backend error is propagated with its real message',
    [$halt?->jsonSuccess, $halt?->statusCode, $halt?->jsonData['message'] ?? null],
    [false, 404, 'No section "sec-missing" exists for this document.']
);

// --- add_section (mission "ORDER 8A-C" backend / "ORDER 8B" proxy) ---

reset_state(['document_id' => $VALID_DOCUMENT_ID], [
    'title' => 'Remote Working',
    'content' => 'Employees may work remotely.',
    'position' => 'end',
]);
$GLOBALS['__test_current_user_can'] = false;
$halt = invoke_handler('handle_add_section');
check(
    'unauthorized user gets a 403 and zero backend calls (add_section)',
    [$halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [403, 0]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID], [
    'title' => 'Remote Working',
    'content' => 'Employees may work remotely.',
    'position' => 'end',
]);
$GLOBALS['__test_nonce_valid'] = false;
$halt = invoke_handler('handle_add_section');
check(
    'an invalid nonce halts before any backend call (add_section)',
    [$halt !== null, count($GLOBALS['__test_backend_calls'])],
    [true, 0]
);

reset_state(['document_id' => 'not-a-real-id'], [
    'title' => 'Remote Working',
    'content' => 'Employees may work remotely.',
    'position' => 'end',
]);
$halt = invoke_handler('handle_add_section');
check(
    'an invalid document_id is rejected with 422 and zero backend calls (add_section)',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID], [
    'title' => '   ',
    'content' => 'Employees may work remotely.',
    'position' => 'end',
]);
$halt = invoke_handler('handle_add_section');
check(
    'an empty title is rejected with 422 and zero backend calls (add_section)',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID], [
    'title' => 'Remote Working',
    'content' => '   ',
    'position' => 'end',
]);
$halt = invoke_handler('handle_add_section');
check(
    'empty content is rejected with 422 and zero backend calls (add_section)',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID], [
    'title' => 'Remote Working',
    'content' => 'Employees may work remotely.',
    'position' => '',
]);
$halt = invoke_handler('handle_add_section');
check(
    'an empty position is rejected with 422 and zero backend calls (add_section)',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID], [
    'title' => 'Remote Working',
    'content' => "Paragraph one.\n\nParagraph two with café.",
    'position' => 'after:hiring_practices',
]);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'document_id' => $VALID_DOCUMENT_ID,
    'section_id' => 'remote_working',
    'legal_topic' => 'Remote Working',
    'indexed_chunks' => 1,
]);
$halt = invoke_handler('handle_add_section');
$sent_body = json_decode($GLOBALS['__test_backend_calls'][0]['body'] ?? '', true);
check(
    'the exact title/content/position are forwarded to the backend unmodified',
    $sent_body,
    [
        'title' => 'Remote Working',
        'content' => "Paragraph one.\n\nParagraph two with café.",
        'position' => 'after:hiring_practices',
    ]
);
check(
    'a successful add is relayed via wp_send_json_success',
    [$halt?->jsonSuccess, $halt?->jsonData['legal_topic'] ?? null],
    [true, 'Remote Working']
);
check(
    'the add is sent as a POST to the backend sections collection path, exactly once',
    [
        $GLOBALS['__test_backend_calls'][0]['method'] ?? null,
        str_ends_with($GLOBALS['__test_backend_calls'][0]['url'] ?? '', '/documents/' . $VALID_DOCUMENT_ID . '/sections'),
        count($GLOBALS['__test_backend_calls']),
    ],
    ['POST', true, 1]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID], [
    'title' => 'Hiring Practices',
    'content' => 'Duplicate attempt.',
    'position' => 'end',
]);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(409, [
    'detail' => [
        'code' => 'section_already_exists',
        'message' => "A section already exists with this title: 'Hiring Practices'. Use Edit instead.",
        'title' => 'Hiring Practices',
    ],
]);
$halt = invoke_handler('handle_add_section');
check(
    'a section_already_exists backend error is propagated with its structured detail',
    [
        $halt?->jsonSuccess,
        $halt?->statusCode,
        $halt?->jsonData['detail']['code'] ?? null,
    ],
    [false, 409, 'section_already_exists']
);

// --- update_section: title forwarding (Rename, mission "ORDER 8G-A") -

reset_state(
    ['document_id' => $VALID_DOCUMENT_ID, 'section_id' => 'sec-1'],
    ['content' => 'Some content.', 'title' => 'Remote Work Equipment Requirements']
);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'document_id' => $VALID_DOCUMENT_ID,
    'section_id' => 'sec-remote-work',
    'legal_topic' => 'Remote Work Equipment Requirements',
    'indexed_chunks' => 1,
]);
$halt = invoke_handler('handle_update_section');
$sent_body = json_decode($GLOBALS['__test_backend_calls'][0]['body'] ?? '', true);
check(
    'a non-empty title is forwarded to the backend as-is',
    $sent_body['title'] ?? null,
    'Remote Work Equipment Requirements'
);
check(
    'a rename response (new section_id/legal_topic) is relayed verbatim',
    [$halt?->jsonSuccess, $halt?->jsonData['section_id'] ?? null, $halt?->jsonData['legal_topic'] ?? null],
    [true, 'sec-remote-work', 'Remote Work Equipment Requirements']
);

reset_state(
    ['document_id' => $VALID_DOCUMENT_ID, 'section_id' => 'sec-1'],
    ['content' => 'Some content.']
);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'document_id' => $VALID_DOCUMENT_ID,
    'section_id' => 'sec-1',
    'legal_topic' => 'Overtime',
    'indexed_chunks' => 1,
]);
invoke_handler('handle_update_section');
$sent_body = json_decode($GLOBALS['__test_backend_calls'][0]['body'] ?? '', true);
check(
    'an omitted title is never forwarded to the backend at all (content-only edit, unchanged from before Rename existed)',
    array_key_exists('title', $sent_body ?? ['title' => null]),
    false
);

reset_state(
    ['document_id' => $VALID_DOCUMENT_ID, 'section_id' => 'sec-1'],
    ['content' => 'Some content.', 'title' => '   ']
);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'document_id' => $VALID_DOCUMENT_ID,
    'section_id' => 'sec-1',
    'legal_topic' => 'Overtime',
    'indexed_chunks' => 1,
]);
invoke_handler('handle_update_section');
$sent_body = json_decode($GLOBALS['__test_backend_calls'][0]['body'] ?? '', true);
check(
    'a whitespace-only title is treated the same as omitted - never forwarded',
    array_key_exists('title', $sent_body ?? ['title' => null]),
    false
);

reset_state(
    ['document_id' => $VALID_DOCUMENT_ID, 'section_id' => 'sec-1'],
    ['content' => 'Duplicate attempt.', 'title' => 'Hiring Practices']
);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(409, [
    'detail' => [
        'code' => 'section_already_exists',
        'message' => "A section already exists with this title: 'Hiring Practices'.",
        'operation' => 'section_update',
        'title' => 'Hiring Practices',
    ],
]);
$halt = invoke_handler('handle_update_section');
check(
    'a duplicate-title rename error is propagated with its structured detail (operation=section_update)',
    [
        $halt?->jsonSuccess,
        $halt?->statusCode,
        $halt?->jsonData['detail']['code'] ?? null,
        $halt?->jsonData['detail']['operation'] ?? null,
    ],
    [false, 409, 'section_already_exists', 'section_update']
);

// --- delete_section (mission "ORDER 8G-A", section 7) -----------------

reset_state(['document_id' => $VALID_DOCUMENT_ID, 'section_id' => 'sec-1']);
$GLOBALS['__test_current_user_can'] = false;
$halt = invoke_handler('handle_delete_section');
check(
    'unauthorized user gets a 403 and zero backend calls (delete_section)',
    [$halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [403, 0]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID, 'section_id' => 'sec-1']);
$GLOBALS['__test_nonce_valid'] = false;
$halt = invoke_handler('handle_delete_section');
check(
    'an invalid nonce halts before any backend call (delete_section)',
    [$halt !== null, count($GLOBALS['__test_backend_calls'])],
    [true, 0]
);

reset_state(['document_id' => 'not-a-real-id', 'section_id' => 'sec-1']);
$halt = invoke_handler('handle_delete_section');
check(
    'an invalid document_id is rejected with 422 and zero backend calls (delete_section)',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID, 'section_id' => 'sec-1']);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'document_id' => $VALID_DOCUMENT_ID,
    'section_id' => 'sec-1',
    'legal_topic' => 'Remote Working',
]);
$halt = invoke_handler('handle_delete_section');
check(
    'the delete is sent as a DELETE to the backend section path, exactly once',
    [
        $GLOBALS['__test_backend_calls'][0]['method'] ?? null,
        str_ends_with(
            $GLOBALS['__test_backend_calls'][0]['url'] ?? '',
            '/documents/' . $VALID_DOCUMENT_ID . '/sections/sec-1'
        ),
        count($GLOBALS['__test_backend_calls']),
    ],
    ['DELETE', true, 1]
);
check(
    'a successful delete is relayed via wp_send_json_success',
    [$halt?->jsonSuccess, $halt?->jsonData['legal_topic'] ?? null],
    [true, 'Remote Working']
);

reset_state(['document_id' => $VALID_DOCUMENT_ID, 'section_id' => 'sec-only']);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(409, [
    'detail' => [
        'code' => 'section_is_last_remaining',
        'message' => 'This section cannot be deleted because it is the only remaining section in this document.',
        'operation' => 'section_delete',
    ],
]);
$halt = invoke_handler('handle_delete_section');
check(
    'a section_is_last_remaining backend error is propagated with its structured detail',
    [$halt?->jsonSuccess, $halt?->statusCode, $halt?->jsonData['detail']['code'] ?? null],
    [false, 409, 'section_is_last_remaining']
);

reset_state(['document_id' => $VALID_DOCUMENT_ID, 'section_id' => 'sec-missing']);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(404, [
    'detail' => ['code' => 'document_section_not_found', 'message' => 'No section "sec-missing" exists for this document.'],
]);
$halt = invoke_handler('handle_delete_section');
check(
    'a section-not-found backend error is propagated with its real message (delete_section)',
    [$halt?->jsonSuccess, $halt?->statusCode, $halt?->jsonData['message'] ?? null],
    [false, 404, 'No section "sec-missing" exists for this document.']
);

if ($failures > 0) {
    fwrite(STDERR, "\n{$failures} check(s) FAILED\n");
    exit(1);
}

fwrite(STDOUT, "\nAll admin-sections checks passed.\n");
exit(0);
