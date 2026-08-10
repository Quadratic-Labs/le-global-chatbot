<?php

declare(strict_types=1);

/**
 * Permanent, framework-free CLI test for
 * LE_Global_Chatbot_Admin::extract_message() (mission "HOTFIX 0.4.9"
 * review 2, section 16) - no PHPUnit, no autoloader, just plain PHP
 * reflection over the real plugin file, matching this project's own
 * "no heavy infrastructure just for this" instruction.
 *
 * Run with:
 *   php wordpress/le-global-chatbot/tests/extract-message.test.php
 *
 * Exits 0 when every check passes, 1 otherwise (with a message on
 * stderr for whichever check failed).
 */

// Minimal WordPress function stubs so the plugin file loads
// standalone, outside of WordPress itself.
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

require __DIR__ . '/../includes/class-le-global-chatbot-admin.php';

$reflection = new ReflectionClass('LE_Global_Chatbot_Admin');
$extract_message = $reflection->getMethod('extract_message');
$extract_message->setAccessible(true);

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

// The exact shape FastAPI's own RequestValidationError produces for
// a missing/malformed multipart field - a list of {loc, msg, type}
// objects, never a string or a {message: ...} object. This is the
// precise mechanism behind the historical WordPress fallback-message
// defect (mission "HOTFIX 0.4.9" review, section 3): before the fix,
// this shape fell all the way through to the generic
// "The document could not be indexed." fallback.
check(
    'FastAPI RequestValidationError list detail extracts msg',
    $extract_message->invoke(
        null,
        [
            'detail' => [
                [
                    'type' => 'missing',
                    'loc' => ['body', 'file'],
                    'msg' => 'Field required',
                ],
            ],
        ],
        'The document could not be indexed.'
    ),
    'Field required'
);

// A list with more than one error still extracts the first.
check(
    'multiple validation errors extracts the first msg',
    $extract_message->invoke(
        null,
        [
            'detail' => [
                ['type' => 'missing', 'loc' => ['body', 'file'], 'msg' => 'Field required'],
                ['type' => 'bool_parsing', 'loc' => ['body', 'replace_existing'], 'msg' => 'Input should be a valid boolean'],
            ],
        ],
        'fallback'
    ),
    'Field required'
);

// Regression: a plain string detail (a bare FastAPI HTTPException
// from the router's own business logic, e.g. 422/500/502) must
// still be relayed verbatim.
check(
    'plain string detail (422/500/502) is unaffected',
    $extract_message->invoke(
        null,
        ['detail' => 'DOCX validation failed: bad zip.'],
        'fallback'
    ),
    'DOCX validation failed: bad zip.'
);

// Regression: a structured {code, message} object (the 409 cases)
// must still be relayed via its own message field.
check(
    'structured dict-with-message detail (409) is unaffected',
    $extract_message->invoke(
        null,
        [
            'detail' => [
                'code' => 'document_replacement_required',
                'message' => 'A document already exists for Argentina.',
            ],
        ],
        'fallback'
    ),
    'A document already exists for Argentina.'
);

// Regression: an empty list/array (no usable information at all)
// still falls back to the caller's own fallback text.
check(
    'empty detail array falls back to the caller-provided default',
    $extract_message->invoke(
        null,
        ['detail' => []],
        'The document could not be indexed.'
    ),
    'The document could not be indexed.'
);

// A list whose first element has no usable "msg" string also falls
// back rather than returning something empty or malformed.
check(
    'list detail without a usable msg falls back',
    $extract_message->invoke(
        null,
        ['detail' => [['type' => 'missing', 'loc' => ['body', 'file']]]],
        'The document could not be indexed.'
    ),
    'The document could not be indexed.'
);

if ($failures > 0) {
    fwrite(STDERR, "\n{$failures} check(s) FAILED\n");
    exit(1);
}

fwrite(STDOUT, "\nAll extract_message() checks passed.\n");
exit(0);
