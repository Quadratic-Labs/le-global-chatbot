"""
Shared fakes for stream_answer_legal_question() tests: a sync repair-only
generation client, a scripted async streaming client, and a helper that
splits text into DELTA events the way a real provider dribbles tokens out.

Extracted from test_stream_answer_legal_question.py, which previously
defined these while test_chat_stream.py and
test_stream_answer_legal_question_evidence_gating.py imported them from it
directly.
"""

from __future__ import annotations

from app.clients.openai_responses import GeneratedText, OpenAIResponseError
from app.clients.openai_responses_stream import StreamEvent, StreamEventType


class _RepairOnlyClient:
    """
    Sync generation client double used ONLY for the hidden repair
    call in the streaming architecture.

    Deliberately NOT FakeGenerationClient (tests/support/rag_fixtures.py):
    that fake's "second call returns repair_answer" logic assumes ONE
    client serves both the first AND the repair generate() call - true
    for answer_legal_question(), but never true here, where the first
    generation always goes through a separate FakeStreamGenerationClient
    and this sync client's own generate() is called for repair only
    (its first-ever call IS the repair call) - so it always returns
    the one configured answer, no call-count logic needed.
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
