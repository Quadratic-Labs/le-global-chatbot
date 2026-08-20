"""API models for grounded legal answers."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.conversation_state import ConversationState


HISTORY_MAX_MESSAGES = 20
HISTORY_MESSAGE_MAX_CHARACTERS = 4000
# Scaled proportionally with HISTORY_MAX_MESSAGES (previously 10000 for
# 6 messages) - the per-message limit above is unchanged.
HISTORY_TOTAL_MAX_CHARACTERS = (
    HISTORY_MAX_MESSAGES * 10000 // 6
)


class LegalChatHistoryMessage(BaseModel):
    """One prior turn of a conversation, supplied by the client."""

    @model_validator(mode="before")
    @classmethod
    def _bound_oversized_assistant_content(
        cls,
        value: object,
    ) -> object:
        """
        Bound only oversized historical assistant answers before
        field-level length validation.

        The API itself can legitimately generate answers longer than
        HISTORY_MESSAGE_MAX_CHARACTERS. Reusing such an answer as the
        next request's history must therefore not make that next
        request invalid.

        User messages remain strict: an oversized user history entry
        is still rejected by the existing Field(max_length=...).

        Valid assistant content at or below the existing API limit is
        returned byte-for-byte unchanged. Extra fields, invalid roles,
        non-string content and every other malformed input are left
        untouched so the existing Pydantic validation still rejects
        them normally.
        """

        if not isinstance(value, dict):
            return value

        if value.get("role") != "assistant":
            return value

        content = value.get("content")

        if not isinstance(content, str):
            return value

        if len(content) <= HISTORY_MESSAGE_MAX_CHARACTERS:
            return value

        bounded = dict(value)
        bounded["content"] = content[
            :HISTORY_MESSAGE_MAX_CHARACTERS
        ]

        return bounded

    role: str = Field(
        pattern=r"^(user|assistant)$",
        description="Either 'user' or 'assistant'.",
    )

    content: str = Field(
        min_length=1,
        max_length=HISTORY_MESSAGE_MAX_CHARACTERS,
    )

    class Config:
        extra = "forbid"

    @field_validator("content")
    @classmethod
    def _reject_whitespace_only_content(
        cls,
        value: str,
    ) -> str:
        """
        Reject content that is empty or whitespace-only once
        stripped, independently of the WordPress proxy - the backend
        must not accept a history entry that has no real content of
        its own even when called directly. A valid value's internal
        whitespace and line breaks are never altered.
        """

        if not value.strip():
            raise ValueError(
                "history content must not be empty or "
                "whitespace-only."
            )

        return value


class LegalChatRequest(BaseModel):
    """One independent legal question."""

    question: str = Field(
        min_length=2,
        max_length=2000,
        description="Employment law question.",
    )

    history: list[LegalChatHistoryMessage] = Field(
        default_factory=list,
        max_length=HISTORY_MAX_MESSAGES,
        description=(
            "Optional recent conversation turns, oldest first. "
            "Used only to disambiguate follow-up questions - never "
            "treated as a legal source."
        ),
    )

    country_codes: list[str] = Field(
        default_factory=list,
        description=(
            "Optional ISO alpha-2 country filters. "
            "Several countries may be supplied for comparisons."
        ),
    )

    legal_topics: list[str] = Field(
        default_factory=list,
        description=(
            "Optional canonical legal topic filters."
        ),
    )

    subsections: list[str] = Field(
        default_factory=list,
        description=(
            "Optional canonical subsection filters."
        ),
    )

    language: str = Field(
        default="en",
        min_length=2,
        max_length=10,
    )

    reference_year: int | None = Field(
        default=None,
        ge=1900,
        le=2100,
    )

    max_sources: int = Field(
        default=6,
        ge=1,
        le=10,
        description=(
            "Maximum number of retrieved chunks "
            "used to generate the answer."
        ),
    )

    conversation_state: ConversationState | None = Field(
        default=None,
        description=(
            "Structured routing state from the last successful turn, "
            "as returned by that turn's own response. Untrusted "
            "client-supplied data, used only to disambiguate a "
            "follow-up's action/country/subject - never a legal "
            "source. A client that omits it falls back to using "
            "history alone, exactly as before this field existed."
        ),
    )

    class Config:
        extra = "forbid"

    @model_validator(mode="after")
    def _validate_history_shape(self) -> "LegalChatRequest":
        """
        Enforce a strictly alternating, user-first, assistant-last
        history - the shape a real turn-by-turn conversation always
        produces - and a total character budget across all messages
        combined, independent of each message's own limit.
        """

        if not self.history:
            return self

        total_characters = sum(
            len(message.content)
            for message in self.history
        )

        if total_characters > HISTORY_TOTAL_MAX_CHARACTERS:
            raise ValueError(
                "history total content length must not exceed "
                f"{HISTORY_TOTAL_MAX_CHARACTERS} characters."
            )

        if self.history[0].role != "user":
            raise ValueError(
                "history must start with a 'user' message."
            )

        if self.history[-1].role != "assistant":
            raise ValueError(
                "history must end with an 'assistant' message."
            )

        expected_role = "user"

        for message in self.history:
            if message.role != expected_role:
                raise ValueError(
                    "history roles must strictly alternate "
                    "between 'user' and 'assistant'."
                )

            expected_role = (
                "assistant"
                if expected_role == "user"
                else "user"
            )

        return self


class LegalAnswerSource(BaseModel):
    """Source actually cited by the generated answer."""

    citation: int

    document_id: str
    chunk_id: str

    country: str
    country_code: str
    legal_topic: str | None

    section: str
    subsection: str | None

    source_filename: str
    reference_year: int | None = None

    score: float

    class Config:
        extra = "forbid"


class LegalChatResponse(BaseModel):
    """Grounded legal answer and its cited sources."""

    question: str
    answer: str

    grounded: bool
    model: str | None

    retrieval_total: int
    sources: list[LegalAnswerSource]

    conversation_state: ConversationState | None = None

    class Config:
        extra = "forbid"