"""Render *already validated* legal bullets as human-readable paragraphs.

The RAG generator, citation validation, evidence gates, uncertainty
suppression and repair mechanism continue to use canonical country
headings and citations on individual bullets. This module changes only
the final user-facing *display* string. On an unexpected shape, it
returns the validated original verbatim (fail-safe).
"""

from __future__ import annotations

import re

_BULLET = re.compile(r"^\s*[-*•]\s+(.+?)\s*$")
_SPACE = re.compile(r"\s+")


def _first_clause_text(text: str) -> str:
    """Start the first sentence grammatically after 'Under X law,'."""
    if len(text) >= 2 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def present_validated_legal_answer_as_prose(
    answer: str,
    *,
    law_labels: dict[str, str],
    split_themes: bool = False,
) -> str:
    """Convert canonical headings + cited bullets into readable prose.

    law_labels maps the validated display heading (e.g. 'Spain' or
    'Under Spanish law:') to a prefix WITHOUT colon ('Under Spanish law').
    Country sections become paragraphs; the optional comparison section
    retains its heading. Multi-theme single-country questions may use
    separate paragraphs for each already-validated bullet.

    No legal propositions or citations are changed, generated or removed.
    """
    if not answer or not law_labels:
        return answer

    heading_map = {key.casefold(): value for key, value in law_labels.items()}
    lines = answer.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_heading: str | None = None
    current_bullets: list[str] = []

    def finish() -> None:
        if current_heading is not None:
            sections.append((current_heading, list(current_bullets)))

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        clean_heading = stripped.lstrip('#').strip().strip('*').strip()
        heading_key = clean_heading.casefold()
        if heading_key in heading_map or heading_key == 'comparison':
            finish()
            current_heading = clean_heading
            current_bullets = []
            continue
        match = _BULLET.match(line)
        if match and current_heading is not None:
            current_bullets.append(_SPACE.sub(' ', match.group(1).strip()))
            continue
        if line[:1].isspace() and current_bullets:
            current_bullets[-1] += ' ' + _SPACE.sub(' ', stripped)
            continue
        # Non-bullet text, unknown heading, or malformed section.
        # Never improvise a legal display from an unrecognized structure.
        return answer

    finish()
    if not sections or any(not bullets for _, bullets in sections):
        return answer
    if not any(heading.casefold() in heading_map for heading, _ in sections):
        return answer

    rendered: list[str] = []
    # Country titles are part of the public comparison contract.
    # Aliases in law_labels do not count as separate country sections.
    country_section_count = sum(
        heading.casefold() in heading_map
        for heading, _ in sections
    )
    for heading, bullets in sections:
        if heading.casefold() == 'comparison':
            rendered.append('Comparison\n' + ' '.join(bullets))
            continue
        label = heading_map[heading.casefold()]
        intro = label + ', ' + _first_clause_text(bullets[0])
        if split_themes and len(sections) == 1 and len(bullets) > 1:
            rendered.append('\n\n'.join([intro, *bullets[1:]]))
        else:
            paragraph = ' '.join([intro, *bullets[1:]])
            if country_section_count > 1:
                # Keep the country name above the paragraph: exactly
                # Country name / Under [Country] law, ...
                paragraph = heading + '\n' + paragraph
            rendered.append(paragraph)
    return '\n\n'.join(rendered)
