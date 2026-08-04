"""
Canonical employment-law topic taxonomy - pure data, no dependency on
any request/response model, so it can be imported by both
legal_topic_detection.py (deterministic phrase detection) and
app/models/conversation_state.py (validating a client-supplied
legal_topics value) without a circular import.
"""

from __future__ import annotations

from typing import Final


TOPIC_RULES: Final[
    tuple[
        tuple[tuple[str, ...], tuple[str, ...]],
        ...,
    ]
] = (
    (
        (
            "hiring practices",
            "recruitment",
            "background check",
            "interview questions",
            "work permit",
            "employment visa",
            "pre-employment screening",
            "local entity",
        ),
        (
            "Hiring Practices",
        ),
    ),
    (
        (
            "notice period",
            "notice periods",
            "statutory notice",
            "termination notice",
            "notice of termination",
        ),
        (
            "Employment Contracts",
            "Termination of Employment Contracts",
        ),
    ),
    (
        (
            "termination",
            "dismissal",
            "dismissed",
            "redundancy",
            "severance",
            "wrongful termination",
            "unfair dismissal",
        ),
        (
            "Termination of Employment Contracts",
        ),
    ),
    (
        (
            "employment contract",
            "employment contracts",
            "fixed term contract",
            "fixed-term contract",
            "probation",
            "probation period",
            "probation periods",
            "probationary period",
            "probationary periods",
            "trial period",
        ),
        (
            "Employment Contracts",
        ),
    ),
    (
        (
            "working time",
            "working hours",
            "working week",
            "overtime",
            "rest period",
            "night work",
        ),
        (
            "Working Conditions",
        ),
    ),
    (
        (
            "discrimination",
            "harassment",
            "reasonable accommodation",
            "protected characteristic",
        ),
        (
            "Anti-Discrimination Laws",
        ),
    ),
    (
        (
            "equal pay",
            "pay equity",
            "gender pay gap",
        ),
        (
            "Pay Equity Laws",
        ),
    ),
    (
        (
            "employee monitoring",
            "monitoring employees",
            "monitor employee",
            "monitor employees",
            "electronic communications",
            "data privacy",
            "personal data",
            "social media",
        ),
        (
            "Social Media and Data Privacy",
        ),
    ),
    (
        (
            "non compete",
            "non-compete",
            "restrictive covenant",
            "restrictive covenants",
            "non solicitation",
            "non-solicitation",
        ),
        (
            "Restrictive Covenants",
        ),
    ),
    (
        (
            "transfer of undertaking",
            "transfer of undertakings",
            "business transfer",
            "tupe",
        ),
        (
            "Transfer of Undertakings",
        ),
    ),
    (
        (
            "trade union",
            "trade unions",
            "works council",
            "collective bargaining",
            "employee representative",
        ),
        (
            "Trade Unions and Employers Associations",
        ),
    ),
    (
        (
            "annual leave",
            "paid leave",
            "sick leave",
            "maternity leave",
            "paternity leave",
            "parental leave",
            "employee benefits",
            "social security",
        ),
        (
            "Employee Benefits",
        ),
    ),
)


CANONICAL_LEGAL_TOPICS: Final[tuple[str, ...]] = tuple(
    sorted(
        {
            legal_topic
            for _, legal_topics in TOPIC_RULES
            for legal_topic in legal_topics
        }
    )
)
