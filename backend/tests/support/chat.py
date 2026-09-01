"""
Shared fixtures for legal-chat and streaming router tests: fake catalog/
document-topic providers, fake generation/understanding/streaming OpenAI
clients (including fail-fast "must not be called" doubles), JSON-payload
builders for RequestUnderstanding action/result/delta shapes, and a
helper that splits text into DELTA events the way a real provider
dribbles tokens out.
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.clients.openai_responses import GeneratedText, OpenAIResponseError
from app.clients.openai_responses_stream import StreamEvent, StreamEventType
from app.core.country_registry import COUNTRIES
from app.models.catalog import LegalCatalogCountry, LegalCatalogResponse
from app.models.search import LegalSearchHit, LegalSearchResponse


def _document_topic_provider(
    country_codes: list[str],
) -> dict[str, list[str]]:
    """Fake DocumentLegalTopicsProvider - no document legal topics for
    any country, matching every test that doesn't concern that
    concept."""

    return {}


# country_registry.COUNTRIES answers "can this country be detected/
# named at all" - some countries are registered there (so an admin
# upload for them resolves to "detected but not allowed"/"detected
# and allowed" rather than "undetermined") without yet having real
# indexed content. It is deliberately NOT mirrored 1:1 into this fake
# catalog: doing so would silently claim every registered country is
# indexed, which is exactly the France/Germany-shaped bug this test
# suite exists to catch. France and Germany are excluded here to
# represent their real, current production state - registered and
# admin-upload-allowed, but not (yet) indexed - which is also why
# they remain this suite's two go-to examples of "recognized but
# unavailable" rather than "unregistered" (Kenya/Nigeria cover that
# different case instead).
_NOT_YET_INDEXED_CODES: frozenset[str] = frozenset({"FR", "DE"})


def _build_catalog() -> LegalCatalogResponse:
    """Build a catalog covering every actually-indexed real country."""

    return LegalCatalogResponse(
        countries=[
            LegalCatalogCountry(
                country_code=country.code,
                country=country.display_name,
                chunk_count=42,
            )
            for country in COUNTRIES
            if country.code not in _NOT_YET_INDEXED_CODES
        ],
        legal_topics=[],
        subsections=[],
    )


def _catalog_provider() -> LegalCatalogResponse:
    """Return the test catalog."""

    return _build_catalog()


def _catalog_provider_with_france() -> LegalCatalogResponse:
    """Return the test catalog with France explicitly supported."""

    return LegalCatalogResponse(
        countries=[
            *_build_catalog().countries,
            LegalCatalogCountry(
                country_code="FR",
                country="France",
                chunk_count=29,
            ),
        ],
        legal_topics=[],
        subsections=[],
    )


def _catalog_provider_with_germany() -> LegalCatalogResponse:
    """Return the test catalog with Germany explicitly supported."""

    return LegalCatalogResponse(
        countries=[
            *_build_catalog().countries,
            LegalCatalogCountry(
                country_code="DE",
                country="Germany",
                chunk_count=29,
            ),
        ],
        legal_topics=[],
        subsections=[],
    )


def _build_hit(
    *,
    country_code: str,
    country: str,
    content: str = "Overtime legal content.",
) -> LegalSearchHit:
    """Build one valid legal search hit."""

    return LegalSearchHit(
        score=10.0,
        document_id=f"document-{country_code.lower()}",
        chunk_id=f"chunk-{country_code.lower()}",
        country=country,
        country_code=country_code,
        legal_topic="Working Conditions",
        document_type="comparator",
        language="en",
        section="03. Working Conditions",
        subsection="Overtime",
        content=content,
        source_filename=(
            f"Labour and Employment Law in {country} 2026.docx"
        ),
        source_format="docx",
        reference_year=2026,
    )


def _build_contact_hit(
    *,
    country_code: str,
    country: str,
    content: str = (
        "Member firm: Test Firm\nEmail: contact@test-firm.example"
    ),
) -> LegalSearchHit:
    """Build one valid Contact-subsection search hit."""

    return LegalSearchHit(
        score=10.0,
        document_id=f"document-{country_code.lower()}",
        chunk_id=f"chunk-{country_code.lower()}-contact",
        country=country,
        country_code=country_code,
        legal_topic=None,
        document_type="overview",
        language="en",
        section=f"Employment Law Overview {country}",
        subsection="Contact",
        content=content,
        source_filename=(
            f"Labour and Employment Law in {country} 2026.docx"
        ),
        source_format="docx",
        reference_year=2026,
    )


class FakeGenerationClient:
    """Test text-generation client."""

    model = "test-model"

    def __init__(
        self,
        answer: str,
        raise_error: bool = False,
        delay_seconds: float = 0.0,
    ) -> None:
        self.answer = answer
        self.raise_error = raise_error
        self.delay_seconds = delay_seconds

    def generate(
        self,
        instructions: str,
        input_text: str,
    ) -> GeneratedText:
        if self.delay_seconds:
            time.sleep(
                self.delay_seconds
            )

        if self.raise_error:
            raise OpenAIResponseError(
                "boom"
            )

        return GeneratedText(
            text=self.answer,
            model=self.model,
        )


class _RepairOnlyClient:
    """
    Sync generation client double used ONLY for the hidden repair
    call in the streaming architecture.

    Deliberately NOT FakeGenerationClient: that fake's "second call
    returns repair_answer" logic assumes ONE client serves both the
    first AND the repair generate() call - true for
    answer_legal_question(), but never true for streaming, where the
    first generation always goes through a separate
    FakeStreamGenerationClient and this sync client's own generate()
    is called for repair only (its first-ever call IS the repair
    call) - so it always returns the one configured answer, no
    call-count logic needed.
    """

    model = "test-model"

    def __init__(self, answer: str, *, raise_error: bool = False) -> None:
        self.answer = answer
        self.raise_error = raise_error
        self.calls: list[tuple[str, str]] = []

    def generate(self, instructions: str, input_text: str) -> GeneratedText:
        self.calls.append((instructions, input_text))

        if self.raise_error:
            raise OpenAIResponseError("boom")

        return GeneratedText(text=self.answer, model=self.model)


class FakeStreamGenerationClient:
    """Scripted streaming test double for the final-answer generation
    stage only - satisfies TextStreamGenerationClient (model + async
    stream())."""

    model = "test-stream-model"

    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = events
        self.calls: list[tuple[str, str]] = []

    async def stream(self, instructions: str, input_text: str):
        self.calls.append((instructions, input_text))

        for event in self._events:
            yield event


def _delta_events(text: str, *, chunk_size: int = 1) -> list[StreamEvent]:
    """Split `text` into `chunk_size`-character DELTA events, then a
    COMPLETED event - simulating a real provider dribbling out an
    answer token by token."""

    events = [
        StreamEvent(type=StreamEventType.DELTA, text=text[i:i + chunk_size])
        for i in range(0, len(text), chunk_size)
    ]
    events.append(StreamEvent(type=StreamEventType.COMPLETED))
    return events


def _unexpected_search(
    request: Any,
) -> LegalSearchResponse:
    """Fail the test if OpenSearch is called for an unsupported request."""

    raise AssertionError(
        "OpenSearch must not be called "
        "for an unsupported request."
    )


def _empty_contact_search(
    country_codes: list[str],
    client: Any = None,
) -> LegalSearchResponse:
    """Return a deterministic no-contact result for fallback tests."""

    return LegalSearchResponse(
        query="",
        total=0,
        limit=20,
        offset=0,
        took_ms=0,
        hits=[],
    )


def _understanding_action(
    action_type: str,
    *,
    country_codes: list[str] | None = None,
    legal_topics: list[str] | None = None,
    topic_text: str | None = None,
    resolved_question: str | None = None,
    subject_text: str | None = None,
    search_concepts: list[dict[str, Any]] | None = None,
    subject_specificity: str | None = None,
    evidence_mode: str | None = None,
) -> dict[str, Any]:
    """
    Build one RequestUnderstandingAction JSON payload.

    Mirrors app.services.request_understanding.RequestUnderstandingAction
    exactly - see that module's model_validator for which fields are
    required for which type/status combination. subject_text/
    search_concepts/subject_specificity/evidence_mode default to the
    same absent values every pre-existing call site already relied on.
    """

    return {
        "type": action_type,
        "country_codes": country_codes or [],
        "legal_topics": legal_topics or [],
        "topic_text": topic_text,
        "resolved_question": resolved_question,
        "subject_text": subject_text,
        "search_concepts": search_concepts or [],
        "subject_specificity": subject_specificity,
        "evidence_mode": evidence_mode,
    }


def _current_message_delta(
    *,
    explicit_action_types: list[str] | None = None,
    explicit_country_codes: list[str] | None = None,
    explicit_legal_topics: list[str] | None = None,
    explicit_subject_text: str | None = None,
    context_operation: str = "independent",
) -> dict[str, Any]:
    """Build one CurrentMessageDelta JSON payload."""

    return {
        "explicit_action_types": explicit_action_types or [],
        "explicit_country_codes": explicit_country_codes or [],
        "explicit_legal_topics": explicit_legal_topics or [],
        "explicit_subject_text": explicit_subject_text,
        "context_operation": context_operation,
    }


def _understanding_result(
    *,
    status: str = "resolved",
    actions: list[dict[str, Any]] | None = None,
    is_follow_up: bool = False,
    confidence: float = 0.9,
    clarification_reason: str | None = None,
    current_message_delta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build one RequestUnderstandingResult JSON payload.

    Mirrors app.services.request_understanding.RequestUnderstandingResult
    exactly, so every fake understanding response used below is a
    genuinely valid payload the real model_validator would accept.
    """

    return {
        "status": status,
        "actions": actions or [],
        "is_follow_up": is_follow_up,
        "confidence": confidence,
        "current_message_delta": (
            current_message_delta
            if current_message_delta is not None
            else _current_message_delta(
                context_operation=(
                    "continue" if is_follow_up else "independent"
                ),
            )
        ),
        "clarification_reason": clarification_reason,
    }


class FakeUnderstandingClient:
    """
    Test double for the semantic-understanding OpenAI client.

    RequestUnderstanding is the primary router for every free-text
    request (see app/services/request_understanding.py), so every test
    that calls resolve_legal_chat_response with a free-text question
    must supply one of these, returning exactly the JSON a correct,
    well-behaved semantic-understanding call would have produced for
    that test's scenario. Every call is captured (instructions/
    input_text) so a test can assert what the model actually received
    - in particular that the full conversation history reaches it,
    which matters since there is no separate, smaller history window:
    the model gets the whole validated history directly and decides
    itself.
    """

    def __init__(
        self,
        payload: dict[str, Any],
    ) -> None:
        self.payload = payload
        self.call_count = 0
        self.captured_instructions: list[str] = []
        self.captured_input_texts: list[str] = []

    def generate(
        self,
        instructions: str,
        input_text: str,
        text_format: dict[str, Any] | None = None,
    ) -> GeneratedText:
        self.call_count += 1
        self.captured_instructions.append(instructions)
        self.captured_input_texts.append(input_text)

        return GeneratedText(
            text=json.dumps(self.payload),
            model="test-model",
        )


class _FailingUnderstandingClient:
    """
    Forces resolve_legal_chat_response's conservative deterministic
    fallback (_resolve_conservative_fallback) by making the one
    semantic-understanding call fail outright.

    Used only for the handful of scenarios a single resolved/
    clarification RequestUnderstanding plan cannot express at all -
    see the docstring on each test that uses this for why.
    """

    def generate(
        self,
        instructions: str,
        input_text: str,
        text_format: dict[str, Any] | None = None,
    ) -> GeneratedText:
        raise OpenAIResponseError(
            "boom",
            retryable=False,
        )


class NoCallGenerationClient:
    """Fails the test if generate() is ever called."""

    model = "test-model"

    def generate(
        self,
        instructions: str,
        input_text: str,
    ) -> GeneratedText:
        raise AssertionError(
            "OpenAI must not be called for a "
            "deterministic contact response."
        )


class NoCallUnderstandingClient:
    """Fails the test if RequestUnderstanding is ever called."""

    model = "test-model"

    def generate(
        self,
        instructions: str,
        input_text: str,
        text_format: dict[str, Any] | None = None,
    ) -> GeneratedText:
        raise AssertionError(
            "OpenAI must not be called for a deterministic "
            "assistant-help response."
        )


# FOLDED_FROM: backend/tests/support/conversation.py
from app.models.conversation_state import ConversationActionState, ConversationState
from app.services.request_understanding import CurrentMessageDelta, DeterministicHints, RequestUnderstandingAction, RequestUnderstandingResult

def _delta(*, context_operation: str='independent', explicit_action_types: list[str] | None=None, explicit_country_codes: list[str] | None=None, explicit_legal_topics: list[str] | None=None, explicit_subject_text: str | None=None) -> CurrentMessageDelta:
    return CurrentMessageDelta(explicit_action_types=explicit_action_types or [], explicit_country_codes=explicit_country_codes or [], explicit_legal_topics=explicit_legal_topics or [], explicit_subject_text=explicit_subject_text, context_operation=context_operation)

def _hints(*, strong_contact_signal: bool=False, comparison_signal: bool=False) -> DeterministicHints:
    return DeterministicHints(strong_contact_signal=strong_contact_signal, comparison_signal=comparison_signal)

def _ru_action(action_type: str, country_codes: list[str], **kwargs) -> RequestUnderstandingAction:
    return RequestUnderstandingAction(type=action_type, country_codes=country_codes, **kwargs)

def _result(*, status: str='clarification', actions: list[RequestUnderstandingAction] | None=None, clarification_reason: str | None='ambiguous_request', delta: CurrentMessageDelta | None=None, is_follow_up: bool=True) -> RequestUnderstandingResult:
    """
    A stand-in "classifier's own result" - defaults to a generic,
    contentless clarification so that any test asserting an override
    can tell the engine's own inheritance/clarification apart from
    whatever the classifier alone produced.
    """
    return RequestUnderstandingResult(status=status, actions=actions or [], is_follow_up=is_follow_up, confidence=0.5, clarification_reason=clarification_reason, current_message_delta=delta or _delta(context_operation='ambiguous'))

def _action_state(action_type: str, country_codes: list[str], **kwargs) -> ConversationActionState:
    return ConversationActionState(type=action_type, country_codes=country_codes, **kwargs)

def _state(actions: list[ConversationActionState], *, focus_action_index: int | None=None, ordered_country_codes: list[str] | None=None) -> ConversationState:
    return ConversationState(actions=actions, focus_action_index=focus_action_index, ordered_country_codes=ordered_country_codes or [])
