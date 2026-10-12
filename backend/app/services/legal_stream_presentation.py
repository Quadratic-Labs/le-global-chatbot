"""Append-only projection of canonical legal-answer deltas into prose.

Wire-layer display projection only: legal validation, evidence/citation
checks, streaming repair and persistence retain the canonical model output.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

_SPACE = re.compile(r"\s+")
_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")


class ProgressiveLegalProse:
    """Accumulate arbitrary token splits without exposing canonical bullets.

    feed() returns an append-only delta. If the raw answer is not canonical,
    display text is passed through (no legal content is invented).
    replace() implements NDJSON replacement with the same projection.
    """

    def __init__(
        self,
        *,
        law_labels: Mapping[str, str],
        show_country_titles: bool = False,
        split_themes: bool = False,
    ) -> None:
        self._labels = {key.casefold(): value for key, value in law_labels.items()}
        self._titles = show_country_titles
        self._split_themes = split_themes
        self._raw = ""
        self._shown = ""

    @property
    def displayed(self) -> str:
        return self._shown

    def reset(self) -> None:
        self._raw = ""
        self._shown = ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._raw += text
        preview = self._preview()
        # Never retract visible legal text in a delta. Full replacements
        # remain reserved for real repair/final answer reconciliation.
        if not preview.startswith(self._shown):
            return ""
        delta = preview[len(self._shown):]
        self._shown = preview
        return delta

    def replace(self, text: str) -> str:
        self._raw = text
        self._shown = self._preview(final=True)
        return self._shown

    def finish(self) -> str:
        """Flush any final incomplete token as an append-only delta."""
        preview = self._preview(final=True)
        if not preview.startswith(self._shown):
            return ""
        delta = preview[len(self._shown):]
        self._shown = preview
        return delta

    def _preview(self, *, final: bool = False) -> str:
        raw = self._raw
        if not raw:
            return ""
        lines = raw.split("\n")
        chunks: list[tuple[str, list[str]]] = []
        heading: str | None = None
        bullets: list[str] = []

        def finish_section() -> None:
            nonlocal bullets
            if heading is not None:
                chunks.append((heading, bullets))
                bullets = []

        recognized = False
        for i, line in enumerate(lines):
            complete = i < len(lines) - 1 or final
            trimmed = line.strip()
            candidate = trimmed.lstrip("#").strip().strip("*").strip()
            normalized = candidate.casefold()
            if complete and (normalized in self._labels or normalized == "comparison"):
                finish_section()
                heading = candidate
                recognized = True
                continue

            if not complete and not recognized:
                # Never leak a partial country heading ("Germ"), which
                # would cause the unwanted visible rewrite a moment later.
                normalized_initial = raw.lstrip().casefold()
                if any(k.startswith(normalized_initial) for k in self._labels) or (
                    "comparison".startswith(normalized_initial)
                ):
                    return ""
                return raw

            if not recognized:
                # Noncanonical text keeps existing streaming semantics.
                return raw

            if not trimmed:
                continue
            bullet = _BULLET.match(line)
            if bullet and heading is not None:
                bullets.append(_SPACE.sub(" ", bullet.group(1).strip()))
                continue
            if line[:1].isspace() and bullets:
                bullets[-1] += " " + _SPACE.sub(" ", trimmed)
                continue
            # During a split next-heading token, do not print partial
            # country names or accidentally turn them into a bullet.
            if not complete:
                break
            # Unknown/malformed source lines are not interpreted into
            # new legal claims; the final authoritative answer reconciles.
            break

        finish_section()
        pieces: list[str] = []
        for header, body in chunks:
            if not body:
                # emit title only when followed by first bullet; avoids
                # heading-only transient frames and misclassification.
                continue
            if header.casefold() == "comparison":
                pieces.append("Comparison\n" + " ".join(body))
                continue
            label = self._labels.get(header.casefold())
            if not label:
                continue
            first = body[0]
            # If leading capital might be an acronym, wait for a second
            # character rather than displaying the wrong case then
            # replacing it in the next streamed chunk.
            if len(first) == 1 and first.isupper() and not final:
                continue
            if len(first) > 1 and first[0].isupper() and first[1].islower():
                first = first[0].lower() + first[1:]
            paragraph = label + ", " + first
            if self._split_themes and not self._titles:
                paragraph += "\n\n".join(["", *body[1:]]) if len(body) > 1 else ""
            elif len(body) > 1:
                paragraph += " " + " ".join(body[1:])
            if self._titles:
                # Country title is intentionally distinct from the law prefix.
                paragraph = header + "\n" + paragraph
            pieces.append(paragraph)
        return "\n\n".join(pieces)
