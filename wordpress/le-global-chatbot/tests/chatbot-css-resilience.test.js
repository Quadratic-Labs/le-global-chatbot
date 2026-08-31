"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");

const stylesheet = fs.readFileSync(
    path.join(__dirname, "..", "assets", "chatbot.css"),
    "utf8"
);

function normalized(value) {
    return value.replace(/\s+/g, " ").trim();
}

function rules() {
    const withoutComments = stylesheet.replace(/\/\*[\s\S]*?\*\//g, "");
    const matches = withoutComments.matchAll(/([^{}]+)\{([^{}]*)\}/g);

    return Array.from(matches, (match) => ({
        selector: normalized(match[1]),
        declarations: normalized(match[2]),
    }));
}

function ruleFor(selector) {
    return rules().find((rule) => rule.selector === normalized(selector));
}

test("message and contact wrappers neutralize theme article spacing", () => {
    const turnRule = ruleFor(
        ".le-global-chatbot .le-global-chatbot__message-list "
        + "> article.le-global-chatbot__turn"
    );
    const contactSectionRule = ruleFor(
        ".le-global-chatbot .le-global-chatbot__message--contact-only "
        + "> .le-global-chatbot__contact-cards"
    );
    const contactCardRule = ruleFor(
        ".le-global-chatbot .le-global-chatbot__contact-cards "
        + "> article.le-global-chatbot__contact-card"
    );

    assert.ok(turnRule);
    assert.match(turnRule.declarations, /display: block;/);
    assert.match(turnRule.declarations, /height: auto;/);
    assert.match(turnRule.declarations, /min-height: 0;/);
    assert.match(turnRule.declarations, /margin: 0;/);
    assert.match(turnRule.declarations, /padding: 0;/);

    assert.ok(contactSectionRule);
    assert.match(contactSectionRule.declarations, /height: auto;/);
    assert.match(contactSectionRule.declarations, /min-height: 0;/);
    assert.match(contactSectionRule.declarations, /margin: 0;/);
    assert.match(contactSectionRule.declarations, /padding: 0;/);

    assert.ok(contactCardRule);
    assert.match(contactCardRule.declarations, /height: auto;/);
    assert.match(contactCardRule.declarations, /min-height: 0;/);
    assert.match(contactCardRule.declarations, /margin: 0;/);
});

test("header title has an explicit root-scoped client color", () => {
    const titleRule = ruleFor(
        ".le-global-chatbot .le-global-chatbot__panel-header "
        + ".le-global-chatbot__panel-heading > h2.le-global-chatbot__title"
    );

    assert.ok(titleRule);
    assert.match(titleRule.declarations, /color: #200e32;/);
});

test("launcher keeps its pre-generic-hover color and shadow behavior", () => {
    const launcherHoverRules = rules().filter(
        (rule) => rule.selector.includes(
            ".le-global-chatbot-floating__launcher:hover"
        )
    );
    const launcherRules = rules().filter(
        (rule) => rule.selector.includes(
            ".le-global-chatbot-floating__launcher"
        )
    );

    assert.equal(launcherHoverRules.length, 1);
    assert.match(
        launcherHoverRules[0].declarations,
        /background: var\(--le-global-primary-hover, #0b5ed7\);/
    );
    assert.equal(
        launcherHoverRules[0].declarations.includes("#000a21"),
        false
    );
    assert.ok(
        launcherRules.some((rule) => (
            rule.declarations.includes(
                "box-shadow: 0 10px 30px rgba(13, 110, 253, 0.35);"
            )
        ))
    );
    launcherRules.forEach((rule) => {
        assert.equal(rule.declarations.includes("translateY"), false);
        assert.equal(
            rule.declarations.includes("0 4px 10px rgba(0, 0, 0, 0.18)"),
            false
        );
    });
});

test("generic lightweight hover remains scoped to buttons inside the panel", () => {
    const raisedHoverRules = rules().filter(
        (rule) => rule.declarations.includes("transform: translateY(-1px);")
    );

    assert.equal(raisedHoverRules.length, 1);
    assert.equal(
        raisedHoverRules[0].selector,
        ".le-global-chatbot button:hover:not(:disabled)"
    );
    assert.equal(
        raisedHoverRules[0].selector.includes(
            ".le-global-chatbot-floating__launcher"
        ),
        false
    );
});
