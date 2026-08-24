<?php

declare(strict_types=1);

/**
 * Permanent, framework-free CLI test for the "Contacts" proxy
 * handlers on LE_Global_Chatbot_Admin - handle_list_contacts/
 * handle_add_contact/handle_update_contact/handle_delete_contact
 * (mission "ORDER 8G-B2") - matching tests/extract-message.test.php's
 * own "no PHPUnit, no autoloader, plain PHP reflection over the real
 * plugin file" convention, and tests/admin-sections.test.php's own
 * shape exactly, since the contact endpoints are proxies of the
 * identical shape (assert_capability -> check_ajax_referer ->
 * [raise_execution_time_limit] -> read_*_for_json -> request_backend
 * -> relay_json_result).
 *
 * Run with:
 *   php wordpress/le-global-chatbot/tests/admin-contacts.test.php
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

// contact_id is an opaque uuid4-hex-style string (mission "ORDER
// 8G-B1"); read_contact_id_for_json() only checks non-emptiness (see
// its docblock), so any non-empty opaque string is a valid fixture -
// this one is deliberately alphanumeric/underscore only so
// rawurlencode() never transforms it, keeping the URL assertions
// below simple substring checks.
$VALID_CONTACT_ID = 'contact_' . str_repeat('b', 32);

// A representative set of the six real business fields, each padded
// with outer whitespace and carrying an internal space, an apostrophe
// and non-ASCII characters - proving read_contact_fields_for_json()
// only ever strips the outer whitespace (trim()) and never mangles
// legitimate internal content the way sanitize_text_field() would.
$POSTED_CONTACT_FIELDS = [
    'member_firm' => "  Müller & Fils SARL  ",
    'contact_person' => "  Marie O'Brien Müller  ",
    'email' => "  marie.obrien@example.com  ",
    'phone' => "  +41 22 123 45 67  ",
    'address' => "  10 Rue de la Paix, 1204 Genève  ",
    'website' => "  https://example.com/geneva  ",
];

$EXPECTED_TRIMMED_CONTACT_FIELDS = [
    'member_firm' => "Müller & Fils SARL",
    'contact_person' => "Marie O'Brien Müller",
    'email' => "marie.obrien@example.com",
    'phone' => "+41 22 123 45 67",
    'address' => "10 Rue de la Paix, 1204 Genève",
    'website' => "https://example.com/geneva",
];

// --- Capability gate (all four handlers share assert_capability) ---

reset_state(['document_id' => $VALID_DOCUMENT_ID]);
$GLOBALS['__test_current_user_can'] = false;
$halt = invoke_handler('handle_list_contacts');
check(
    'unauthorized user gets a 403 and zero backend calls (list_contacts)',
    [$halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [403, 0]
);

// --- Nonce gate ------------------------------------------------------

reset_state(['document_id' => $VALID_DOCUMENT_ID], $POSTED_CONTACT_FIELDS);
$GLOBALS['__test_nonce_valid'] = false;
$halt = invoke_handler('handle_add_contact');
check(
    'an invalid nonce halts before any backend call (add_contact)',
    [$halt !== null, count($GLOBALS['__test_backend_calls'])],
    [true, 0]
);

// --- Invalid document_id, for each of the four handlers --------------

reset_state(['document_id' => 'not-a-real-id']);
$halt = invoke_handler('handle_list_contacts');
check(
    'an invalid document_id is rejected with 422 and zero backend calls (list_contacts)',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

reset_state(['document_id' => 'not-a-real-id'], $POSTED_CONTACT_FIELDS);
$halt = invoke_handler('handle_add_contact');
check(
    'an invalid document_id is rejected with 422 and zero backend calls (add_contact)',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

reset_state(
    ['document_id' => 'not-a-real-id', 'contact_id' => $VALID_CONTACT_ID],
    $POSTED_CONTACT_FIELDS
);
$halt = invoke_handler('handle_update_contact');
check(
    'an invalid document_id is rejected with 422 and zero backend calls (update_contact)',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

reset_state(['document_id' => 'not-a-real-id', 'contact_id' => $VALID_CONTACT_ID]);
$halt = invoke_handler('handle_delete_contact');
check(
    'an invalid document_id is rejected with 422 and zero backend calls (delete_contact)',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

// --- Empty contact_id (update_contact / delete_contact only) ---------

reset_state(
    ['document_id' => $VALID_DOCUMENT_ID, 'contact_id' => ''],
    $POSTED_CONTACT_FIELDS
);
$halt = invoke_handler('handle_update_contact');
check(
    'an empty contact_id is rejected with 422 and zero backend calls (update_contact)',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID, 'contact_id' => '']);
$halt = invoke_handler('handle_delete_contact');
check(
    'an empty contact_id is rejected with 422 and zero backend calls (delete_contact)',
    [$halt?->jsonSuccess, $halt?->statusCode, count($GLOBALS['__test_backend_calls'])],
    [false, 422, 0]
);

// --- list_contacts success/error propagation --------------------------

reset_state(['document_id' => $VALID_DOCUMENT_ID]);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'document_id' => $VALID_DOCUMENT_ID,
    'contacts' => [
        [
            'contact_id' => $VALID_CONTACT_ID,
            'member_firm' => 'Müller & Fils SARL',
            'contact_person' => "Marie O'Brien Müller",
            'email' => 'marie.obrien@example.com',
            'phone' => '+41 22 123 45 67',
            'address' => '10 Rue de la Paix, 1204 Genève',
            'website' => 'https://example.com/geneva',
        ],
    ],
]);
$halt = invoke_handler('handle_list_contacts');
check(
    'a successful contacts list is relayed verbatim',
    [$halt?->jsonSuccess, $halt?->jsonData],
    [
        true,
        [
            'document_id' => $VALID_DOCUMENT_ID,
            'contacts' => [
                [
                    'contact_id' => $VALID_CONTACT_ID,
                    'member_firm' => 'Müller & Fils SARL',
                    'contact_person' => "Marie O'Brien Müller",
                    'email' => 'marie.obrien@example.com',
                    'phone' => '+41 22 123 45 67',
                    'address' => '10 Rue de la Paix, 1204 Genève',
                    'website' => 'https://example.com/geneva',
                ],
            ],
        ],
    ]
);
check(
    'the list is sent as a GET to the backend contacts collection path, exactly once',
    [
        $GLOBALS['__test_backend_calls'][0]['method'] ?? null,
        str_ends_with(
            $GLOBALS['__test_backend_calls'][0]['url'] ?? '',
            '/documents/' . $VALID_DOCUMENT_ID . '/contacts'
        ),
        count($GLOBALS['__test_backend_calls']),
    ],
    ['GET', true, 1]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID]);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(404, [
    'detail' => ['code' => 'document_not_found', 'message' => 'No indexed document was found for this identifier.'],
]);
$halt = invoke_handler('handle_list_contacts');
check(
    'a 404 backend error is propagated with the real message and status (list_contacts)',
    [$halt?->jsonSuccess, $halt?->statusCode, $halt?->jsonData['message'] ?? null],
    [false, 404, 'No indexed document was found for this identifier.']
);

// --- observability: request_backend() must log enough to distinguish
// a real backend 400/401/403/404/500/502 after the fact - the exact
// gap that turned a real, already-captured production 400 into a
// many-hour forensic reconstruction (docs/RELEASE_COMPATIBILITY.md's
// "Contact 400 investigation"), because relay_json_result() itself
// only ever shows the admin a single generic fallback message and
// request_backend() previously never logged a successful-but-error
// HTTP round trip at all - only a hard transport failure.

function capture_error_log(callable $body): string
{
    $log_path = tempnam(sys_get_temp_dir(), 'le-global-test-error-log-');
    $previous_error_log = ini_set('error_log', $log_path);

    try {
        $body();

        return file_get_contents($log_path) ?: '';
    } finally {
        ini_set('error_log', $previous_error_log);
        @unlink($log_path);
    }
}

reset_state(['document_id' => $VALID_DOCUMENT_ID]);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(400, [
    'detail' => ['code' => 'some_backend_error', 'message' => 'Bad request.'],
]);
$logged = capture_error_log(function () {
    invoke_handler('handle_list_contacts');
});
check(
    'a real backend 400 is logged with its exact status code, method, and path',
    [
        str_contains($logged, '400'),
        str_contains($logged, 'GET'),
        str_contains($logged, '/documents/' . $VALID_DOCUMENT_ID . '/contacts'),
    ],
    [true, true, true]
);
check(
    'the non-2xx log line never contains the configured admin/API keys',
    [
        str_contains($logged, 'test-admin-key'),
        str_contains($logged, 'test-api-key'),
    ],
    [false, false]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID]);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(401, [
    'detail' => 'Invalid or missing administration key.',
]);
$logged = capture_error_log(function () {
    invoke_handler('handle_list_contacts');
});
check(
    'a real backend 401 is logged distinctly from a 400 (exact status code present)',
    str_contains($logged, '401'),
    true
);

reset_state(['document_id' => $VALID_DOCUMENT_ID]);
$GLOBALS['__test_fake_backend_response'] = [
    'response' => ['code' => 400, 'message' => ''],
    'body' => 'Invalid HTTP request received.',
];
$logged = capture_error_log(function () {
    invoke_handler('handle_list_contacts');
});
check(
    'a non-JSON backend body is logged as invalid JSON, with its real status code',
    [
        str_contains($logged, 'not valid JSON'),
        str_contains($logged, '400'),
    ],
    [true, true]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID]);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'document_id' => $VALID_DOCUMENT_ID,
    'contacts' => [],
]);
$logged = capture_error_log(function () {
    invoke_handler('handle_list_contacts');
});
check(
    'a normal 200 response logs nothing at all (no noise on the success path)',
    $logged,
    ''
);

// --- add_contact forwarding + success/error propagation ---------------

reset_state(['document_id' => $VALID_DOCUMENT_ID], $POSTED_CONTACT_FIELDS);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'document_id' => $VALID_DOCUMENT_ID,
    'contact_id' => $VALID_CONTACT_ID,
    'member_firm' => $EXPECTED_TRIMMED_CONTACT_FIELDS['member_firm'],
]);
$halt = invoke_handler('handle_add_contact');
$sent_body = json_decode($GLOBALS['__test_backend_calls'][0]['body'] ?? '', true);
check(
    'the exact six posted fields are forwarded to the backend outer-trimmed only, internal content/apostrophe/unicode untouched',
    $sent_body,
    $EXPECTED_TRIMMED_CONTACT_FIELDS
);
check(
    'a successful add is relayed via wp_send_json_success',
    [$halt?->jsonSuccess, $halt?->jsonData['contact_id'] ?? null],
    [true, $VALID_CONTACT_ID]
);
check(
    'the add is sent as a POST to the backend contacts collection path, exactly once',
    [
        $GLOBALS['__test_backend_calls'][0]['method'] ?? null,
        str_ends_with(
            $GLOBALS['__test_backend_calls'][0]['url'] ?? '',
            '/documents/' . $VALID_DOCUMENT_ID . '/contacts'
        ),
        count($GLOBALS['__test_backend_calls']),
    ],
    ['POST', true, 1]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID], $POSTED_CONTACT_FIELDS);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(422, [
    'detail' => [
        'code' => 'contact_validation_failed',
        'message' => 'The contact person is required.',
    ],
]);
$halt = invoke_handler('handle_add_contact');
check(
    'a backend validation error is propagated with its structured detail (add_contact)',
    [
        $halt?->jsonSuccess,
        $halt?->statusCode,
        $halt?->jsonData['message'] ?? null,
        $halt?->jsonData['detail']['code'] ?? null,
    ],
    [false, 422, 'The contact person is required.', 'contact_validation_failed']
);

// --- update_contact forwarding + success/error propagation -------------

reset_state(
    ['document_id' => $VALID_DOCUMENT_ID, 'contact_id' => $VALID_CONTACT_ID],
    $POSTED_CONTACT_FIELDS
);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'document_id' => $VALID_DOCUMENT_ID,
    'contact_id' => $VALID_CONTACT_ID,
    'member_firm' => $EXPECTED_TRIMMED_CONTACT_FIELDS['member_firm'],
]);
$halt = invoke_handler('handle_update_contact');
$sent_body = json_decode($GLOBALS['__test_backend_calls'][0]['body'] ?? '', true);
check(
    'the exact six posted fields are forwarded to the backend outer-trimmed only (update_contact)',
    $sent_body,
    $EXPECTED_TRIMMED_CONTACT_FIELDS
);
check(
    'a successful save is relayed via wp_send_json_success',
    [$halt?->jsonSuccess, $halt?->jsonData['contact_id'] ?? null],
    [true, $VALID_CONTACT_ID]
);
check(
    'the update is sent as a PUT to the backend contact path (with the exact contact_id), exactly once',
    [
        $GLOBALS['__test_backend_calls'][0]['method'] ?? null,
        str_ends_with(
            $GLOBALS['__test_backend_calls'][0]['url'] ?? '',
            '/documents/' . $VALID_DOCUMENT_ID . '/contacts/' . $VALID_CONTACT_ID
        ),
        count($GLOBALS['__test_backend_calls']),
    ],
    ['PUT', true, 1]
);

reset_state(
    ['document_id' => $VALID_DOCUMENT_ID, 'contact_id' => $VALID_CONTACT_ID],
    $POSTED_CONTACT_FIELDS
);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(404, [
    'detail' => [
        'code' => 'contact_not_found',
        'message' => 'No contact exists with this identifier for this document.',
    ],
]);
$halt = invoke_handler('handle_update_contact');
check(
    'a contact-not-found backend error is propagated with its real message (update_contact)',
    [$halt?->jsonSuccess, $halt?->statusCode, $halt?->jsonData['message'] ?? null],
    [false, 404, 'No contact exists with this identifier for this document.']
);

// --- delete_contact: no body + success/error propagation ---------------

reset_state(['document_id' => $VALID_DOCUMENT_ID, 'contact_id' => $VALID_CONTACT_ID]);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(200, [
    'document_id' => $VALID_DOCUMENT_ID,
    'contact_id' => $VALID_CONTACT_ID,
    'member_firm' => 'Müller & Fils SARL',
]);
$halt = invoke_handler('handle_delete_contact');
check(
    'the delete is sent as a DELETE to the backend contact path (with the exact contact_id), with no body, exactly once',
    [
        $GLOBALS['__test_backend_calls'][0]['method'] ?? null,
        str_ends_with(
            $GLOBALS['__test_backend_calls'][0]['url'] ?? '',
            '/documents/' . $VALID_DOCUMENT_ID . '/contacts/' . $VALID_CONTACT_ID
        ),
        $GLOBALS['__test_backend_calls'][0]['body'],
        count($GLOBALS['__test_backend_calls']),
    ],
    ['DELETE', true, null, 1]
);
check(
    'a successful delete is relayed via wp_send_json_success',
    [$halt?->jsonSuccess, $halt?->jsonData['contact_id'] ?? null],
    [true, $VALID_CONTACT_ID]
);

reset_state(['document_id' => $VALID_DOCUMENT_ID, 'contact_id' => $VALID_CONTACT_ID]);
$GLOBALS['__test_fake_backend_response'] = fake_backend_json_response(404, [
    'detail' => [
        'code' => 'contact_not_found',
        'message' => 'No contact exists with this identifier for this document.',
    ],
]);
$halt = invoke_handler('handle_delete_contact');
check(
    'a contact-not-found backend error is propagated with its real message (delete_contact)',
    [$halt?->jsonSuccess, $halt?->statusCode, $halt?->jsonData['message'] ?? null],
    [false, 404, 'No contact exists with this identifier for this document.']
);

if ($failures > 0) {
    fwrite(STDERR, "\n{$failures} check(s) FAILED\n");
    exit(1);
}

fwrite(STDOUT, "\nAll admin-contacts checks passed.\n");
exit(0);
