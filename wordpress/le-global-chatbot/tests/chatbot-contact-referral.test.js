"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const path = require("node:path");
global.document = { querySelectorAll: () => [] };
const chatbot = require(path.join(__dirname, "..", "assets", "chatbot.js"));

test("single country: validated contact fields are normalized", () => {
    assert.deepEqual(chatbot.normalizeContactReferral({
        contact_person: " Example Lawyer ", email: " lawyer@example.org ",
    }), {contact_person: "Example Lawyer", email: "lawyer@example.org"});
});

test("comparison and cards: generic referral has no person or email", () => {
    assert.deepEqual(chatbot.normalizeContactReferral({}), {
        contact_person: null, email: null,
    });
});

test("missing contact email gracefully avoids displaying a name alone", () => {
    assert.deepEqual(chatbot.normalizeContactReferral({contact_person:"Example"}), {
        contact_person:null,email:null,
    });
    assert.equal(chatbot.normalizeContactReferral(undefined),null);
});

test("stream: reconstructed metadata preserves structured referral", () => {
    const reconstructed = chatbot.buildReconstructedChatResponse({
        metadata: {contact_referral: {contact_person:"Example",email:"example@test.org"}},
        answerText: "Legal answer.",
    });
    assert.deepEqual(reconstructed.contact_referral, {
        contact_person:"Example",email:"example@test.org",
    });
});

test("referral renders literal names safely in an em element", () => {
    const oldDocument = global.document;
    const makeNode = (tag) => ({
        tagName: tag,
        children: [],
        textContent: "",
        appendChild(child) { this.children.push(child); },
    });
    global.document = {
        querySelectorAll:()=>[],
        createElement: makeNode,
        createTextNode: (value) => ({textContent:value}),
    };
    try {
        const paragraph = chatbot.buildContactReferralElement({
            contact_person:"<script>alert(1)</script>",
            email:"lawyer@example.org",
        });
        assert.equal(
            paragraph.textContent,
            "There are nuances and exceptions that might impact the answer "
            + "based on facts not provided so we recommend that you contact "
            + "the L&E Global member firm lawyer ",
        );
        assert.equal(paragraph.children[0].tagName, "em");
        assert.equal(paragraph.children[0].textContent,
            "<script>alert(1)</script> (lawyer@example.org)");
        assert.equal(paragraph.children[1].textContent, ".");
    } finally {
        global.document=oldDocument;
    }
});
