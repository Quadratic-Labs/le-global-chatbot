<?php

declare(strict_types=1);

/**
 * Permanent, framework-free CLI test for GATE S7-LITE's render_shortcode()
 * change - the ONE piece of PHP/config code this gate touched: exposing
 * /chat/stream's URL and the LE_GLOBAL_CHATBOT_STREAMING_ENABLED feature
 * flag to chatbot.js via the same data-* attribute mechanism the widget
 * already uses for data-config-endpoint/data-chat-endpoint. Matches
 * chat-stream.test.php's own "no PHPUnit, no autoloader, plain PHP
 * reflection over the real plugin file" convention.
 *
 * PHP constants cannot be redefined within one process, so the flag's
 * OFF (default) and ON paths are exercised as two separate process runs
 * of this same file:
 *
 *   php wordpress/le-global-chatbot/tests/shortcode-stream-config.test.php
 *   LE_TEST_STREAMING_ENABLED=1 \
 *     php wordpress/le-global-chatbot/tests/shortcode-stream-config.test.php
 *
 * Exits 0 when every check in that run passes, 1 otherwise (with a
 * message on stderr for whichever check failed).
 */

final class TestHaltException extends \Exception
{
}

final class WP_Error
{
    public function __construct(
        private string $code,
        private string $message,
        private array $errorData = []
    ) {
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

function wp_enqueue_style(...$args): void
{
}

function wp_enqueue_script(...$args): void
{
}

function untrailingslashit(string $value): string
{
    return rtrim($value, '/');
}

function rest_url(string $path = ''): string
{
    return 'https://example.test/wp-json/' . ltrim($path, '/');
}

function admin_url(string $path = ''): string
{
    return 'https://example.test/wp-admin/' . ltrim($path, '/');
}

function wp_unique_id(string $prefix = ''): string
{
    return $prefix . '1';
}

/**
 * Real shortcode_atts() semantics: only keys present in $pairs survive,
 * each overridden by $atts when present there.
 */
function shortcode_atts(array $pairs, $atts, string $shortcode = ''): array
{
    $atts = (array) $atts;
    $out = [];

    foreach ($pairs as $name => $default) {
        $out[$name] = array_key_exists($name, $atts)
            ? $atts[$name]
            : $default;
    }

    return $out;
}

function esc_attr(string $text): string
{
    return $text;
}

/**
 * Mirrors the one property every check below depends on: a bare "&"
 * becomes the HTML entity "&#038;" - none of this gate's URLs contain
 * one, so this never actually fires, but it matches the real function's
 * contract rather than a pure passthrough.
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

if (!defined('ABSPATH')) {
    define('ABSPATH', sys_get_temp_dir() . '/');
}

// The one env var this file's two process-run invocations use to
// choose which side of the OFF-by-default flag they exercise - see
// this file's own docblock.
$streamingEnabledForThisRun = getenv('LE_TEST_STREAMING_ENABLED') === '1';

if ($streamingEnabledForThisRun) {
    define('LE_GLOBAL_CHATBOT_STREAMING_ENABLED', true);
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

// --- is_chat_streaming_enabled() itself --------------------------------

check(
    $streamingEnabledForThisRun
        ? 'is_chat_streaming_enabled() is true once the constant is defined truthy'
        : 'is_chat_streaming_enabled() defaults to false when the constant is undefined',
    call_private_static(
        'LE_Global_Chatbot_Plugin',
        'is_chat_streaming_enabled'
    ) === $streamingEnabledForThisRun,
    $failures
);

// --- render_shortcode() output -----------------------------------------

$html = LE_Global_Chatbot_Plugin::render_shortcode([]);

check(
    'render_shortcode() always advertises the /chat/stream URL, '
    . 'regardless of the flag - the endpoint itself is never a secret',
    str_contains(
        $html,
        'data-chat-stream-endpoint="https://example.test/wp-json/'
        . 'le-global-chatbot/v1/chat/stream"'
    ),
    $failures
);

check(
    'render_shortcode() still advertises the existing /chat URL '
    . 'unchanged',
    str_contains(
        $html,
        'data-chat-endpoint="https://example.test/wp-json/'
        . 'le-global-chatbot/v1/chat"'
    ),
    $failures
);

check(
    $streamingEnabledForThisRun
        ? 'data-chat-streaming-enabled="1" once the constant is defined truthy'
        : 'data-chat-streaming-enabled="0" by default (constant undefined)',
    str_contains(
        $html,
        'data-chat-streaming-enabled="'
        . ($streamingEnabledForThisRun ? '1' : '0')
        . '"'
    ),
    $failures
);

check(
    'exactly one data-chat-streaming-enabled attribute is emitted '
    . '(never both "0" and "1")',
    substr_count($html, 'data-chat-streaming-enabled="') === 1,
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

echo "All shortcode-stream-config.test.php checks passed "
    . "(LE_TEST_STREAMING_ENABLED="
    . ($streamingEnabledForThisRun ? '1' : '0')
    . ").\n";
exit(0);
