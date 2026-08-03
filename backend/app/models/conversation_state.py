"""
Structured conversation-state models exchanged with the client.

ConversationState represents exclusively the actions actually executed
at the last successful user turn, and the subject actually used for
them - never an accumulation of the whole conversation. Every turn
that resolves successfully replaces it outright; an action that is no
longer active is dropped, not merged with the new one. The one
exception is a pending clarification, which keeps only the candidates
needed to resolve it.

Sent by the backend after each turn, stored client-side in
sessionStorage, and returned by the client on the next turn as
untrusted context - see app/services/conversation_transition.py for
how it is validated and applied. It is never used as a legal source:
it carries routing metadata only (action type, country, topic), never
legal content or contact coordinates.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.core.country_registry import COUNTRIES
from app.services.legal_topic_taxonomy import CANONICAL_LEGAL_TOPICS


CONVERSATION_STATE_VERSION: int = 1

ACTION_TYPES: tuple[str, ...] = (
    "contact",
    "legal_information",
    "comparison",
)

EVIDENCE_MODES: tuple[str, ...] = (
    "broad_topic",
    "direct_topic",
    "relation_required",
)

CLARIFICATION_REASONS: tuple[str, ...] = (
    "select_action",
    "select_country",
    "missing_country",
    "missing_topic",
    "ambiguous_reference",
)

MAX_ACTIONS = 3
# Not capped to a small fixed number: an existing, already-supported
# product capability compares as many countries as a single request
# names (e.g. a 6+ country comparison) - conversation_state must be
# able to carry forward any comparison the product itself allows,
# never a narrower cap invented for this mission alone.
MAX_COUNTRY_CODES_PER_ACTION = len(COUNTRIES)
MAX_SEARCH_CONCEPT_GROUPS = 4
MAX_CONCEPT_TERMS = 5
MIN_CONCEPT_TERM_CHARACTERS = 2
MAX_CONCEPT_TERM_CHARACTERS = 80
MAX_SUBJECT_TEXT_CHARACTERS = 300
MAX_RESOLVED_QUESTION_CHARACTERS = 500
MAX_CONVERSATION_STATE_JSON_CHARACTERS = 8000

_SUPPORTED_COUNTRY_CODES: frozenset[str] = frozenset(
    country.code for country in COUNTRIES
)


class ConversationSearchConcept(BaseModel):
    """
    One synonym group for a single concept the user's subject depends
    on - never a legal answer, never a contact, never an adjacent
    theme the user did not ask about.
    """

    terms: list[str] = Field(
        min_length=1,
        max_length=MAX_CONCEPT_TERMS,
    )

    class Config:
        extra = "forbid"

    @model_validator(mode="after")
    def _validate_terms(self) -> "ConversationSearchConcept":
        normalized_terms: list[str] = []
        seen_terms: set[str] = set()

        for term in self.terms:
            stripped_term = term.strip()

            if not (
                MIN_CONCEPT_TERM_CHARACTERS
                <= len(stripped_term)
                <= MAX_CONCEPT_TERM_CHARACTERS
            ):
                raise ValueError(
                    "Each search concept term must be between "
                    f"{MIN_CONCEPT_TERM_CHARACTERS} and "
                    f"{MAX_CONCEPT_TERM_CHARACTERS} characters."
                )

            casefolded_term = stripped_term.casefold()

            if casefolded_term in seen_terms:
                raise ValueError(
                    "Search concept terms must be unique."
                )

            seen_terms.add(casefolded_term)
            normalized_terms.append(stripped_term)

        self.terms[:] = normalized_terms

        return self


class ConversationActionState(BaseModel):
    """One action actually executed at the last successful turn."""

    type: Literal[
        "contact",
        "legal_information",
        "comparison",
    ]

    country_codes: list[str] = Field(default_factory=list)
    legal_topics: list[str] = Field(default_factory=list)

    subject_text: str | None = Field(
        default=None,
        max_length=MAX_SUBJECT_TEXT_CHARACTERS,
    )

    search_concepts: list[ConversationSearchConcept] = Field(
        default_factory=list,
        max_length=MAX_SEARCH_CONCEPT_GROUPS,
    )

    subject_specificity: Literal["broad", "specific"] | None = None

    resolved_question: str | None = Field(
        default=None,
        max_length=MAX_RESOLVED_QUESTION_CHARACTERS,
    )

    evidence_mode: Literal[
        "broad_topic",
        "direct_topic",
        "relation_required",
    ] | None = None

    class Config:
        extra = "forbid"

    @model_validator(mode="after")
    def _validate_action(self) -> "ConversationActionState":
        if len(self.country_codes) > MAX_COUNTRY_CODES_PER_ACTION:
            raise ValueError(
                "At most "
                f"{MAX_COUNTRY_CODES_PER_ACTION} countries are "
                "supported per action."
            )

        normalized_codes: list[str] = []

        for code in self.country_codes:
            upper_code = code.strip().upper()

            if upper_code not in _SUPPORTED_COUNTRY_CODES:
                raise ValueError(
                    f"Unsupported country code: {code!r}"
                )

            if upper_code not in normalized_codes:
                normalized_codes.append(upper_code)

        self.country_codes[:] = normalized_codes

        for topic in self.legal_topics:
            if topic not in CANONICAL_LEGAL_TOPICS:
                raise ValueError(
                    f"Unsupported legal topic: {topic!r}"
                )

        if self.type == "contact":
            if (
                self.legal_topics
                or self.subject_text
                or self.search_concepts
                or self.subject_specificity is not None
                or self.evidence_mode is not None
            ):
                raise ValueError(
                    "A contact action must not carry legal "
                    "subject matter."
                )

        else:
            if not self.legal_topics and not self.subject_text:
                raise ValueError(
                    "A legal_information or comparison action "
                    "requires legal_topics or subject_text."
                )

            if (
                self.type == "comparison"
                and len(self.country_codes) < 2
            ):
                raise ValueError(
                    "A comparison action requires at least "
                    "two countries."
                )

        return self


class ConversationPendingClarification(BaseModel):
    """
    The clarification currently awaiting the user's answer, and the
    exact candidates it was built from - resolving a short follow-up
    answer ("both", "the contact", "Peru") means picking among these
    candidates only, never guessing new ones.
    """

    reason: Literal[
        "select_action",
        "select_country",
        "missing_country",
        "missing_topic",
        "ambiguous_reference",
    ]

    candidate_action_types: list[
        Literal[
            "contact",
            "legal_information",
            "comparison",
        ]
    ] = Field(default_factory=list)

    candidate_country_codes: list[str] = Field(
        default_factory=list
    )

    class Config:
        extra = "forbid"

    @model_validator(mode="after")
    def _validate_candidate_country_codes(
        self,
    ) -> "ConversationPendingClarification":
        normalized_codes: list[str] = []

        for code in self.candidate_country_codes:
            upper_code = code.strip().upper()

            if upper_code not in _SUPPORTED_COUNTRY_CODES:
                raise ValueError(
                    f"Unsupported country code: {code!r}"
                )

            if upper_code not in normalized_codes:
                normalized_codes.append(upper_code)

        self.candidate_country_codes[:] = normalized_codes

        return self


class ConversationState(BaseModel):
    """
    The routing-relevant residue of the last successful turn.

    Never a legal source, never a store of contact coordinates, and
    never an accumulation of every action ever executed in the
    conversation - see the module docstring.
    """

    version: Literal[1] = 1

    actions: list[ConversationActionState] = Field(
        default_factory=list,
        max_length=MAX_ACTIONS,
    )

    focus_action_index: int | None = None

    ordered_country_codes: list[str] = Field(
        default_factory=list
    )

    pending_clarification: (
        ConversationPendingClarification | None
    ) = None

    class Config:
        extra = "forbid"

    @model_validator(mode="after")
    def _validate_state(self) -> "ConversationState":
        seen_scopes: set[tuple[str, frozenset[str]]] = set()

        for action in self.actions:
            scope = (
                action.type,
                frozenset(action.country_codes),
            )

            if scope in seen_scopes:
                raise ValueError(
                    "Duplicate action for the same type and "
                    "country scope."
                )

            seen_scopes.add(scope)

        action_count = len(self.actions)

        if action_count == 0:
            if self.focus_action_index is not None:
                raise ValueError(
                    "focus_action_index must be null when "
                    "there are no actions."
                )
        elif action_count == 1:
            if self.focus_action_index != 0:
                raise ValueError(
                    "focus_action_index must be 0 when exactly "
                    "one action is present."
                )
        else:
            if self.focus_action_index is not None and not (
                0 <= self.focus_action_index < action_count
            ):
                raise ValueError(
                    "focus_action_index is out of range."
                )

        comparison_actions = [
            action
            for action in self.actions
            if action.type == "comparison"
        ]

        if self.ordered_country_codes:
            if len(comparison_actions) != 1:
                raise ValueError(
                    "ordered_country_codes requires exactly "
                    "one active comparison action."
                )

            if set(self.ordered_country_codes) != set(
                comparison_actions[0].country_codes
            ):
                raise ValueError(
                    "ordered_country_codes must match the "
                    "comparison action's own countries."
                )

        elif comparison_actions:
            raise ValueError(
                "A comparison action requires "
                "ordered_country_codes."
            )

        serialized_length = len(
            json.dumps(
                self.model_dump(mode="json"),
                separators=(",", ":"),
            )
        )

        if (
            serialized_length
            > MAX_CONVERSATION_STATE_JSON_CHARACTERS
        ):
            raise ValueError(
                "conversation_state is too large "
                f"({serialized_length} characters, maximum "
                f"{MAX_CONVERSATION_STATE_JSON_CHARACTERS})."
            )

        return self
