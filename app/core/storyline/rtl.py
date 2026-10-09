"""Right-to-left neutralisation for Arabic (`ar`) decks. Ported from Darwin `_shared/rtlText.ts`.

The layout libraries (the archetype directives and purposes, the framework hints) are written in
left-to-right terms: "left rail", "left-to-right chevron stages", "originating from the bottom-left".
Injected verbatim into a prompt for an Arabic slide they contradict the RTL instruction, and a model
resolves that contradiction only partly (a real Darwin render came out half-mirrored). So for `ar`
only, the directional words are rewritten to their mirror before the text reaches a prompt.

* Only for `ar`: English text is never touched, so English prompts stay byte-for-byte identical.
* One single-pass alternation, longest phrase first, so nothing is flipped twice ("right-hand" never
  decays back) and "left-to-right" wins over a bare "left".
* Case-insensitive, whole words only. Purely lexical: the RTL instruction in the prompt stays the
  semantic backstop for anything this misses.

Full RTL in the export engine is Phase 6; this is the prompt side.
"""

from __future__ import annotations

import re

_SWAP: dict[str, str] = {
    "left-to-right": "right-to-left",
    "right-to-left": "right-to-left",  # identity: never re-flip text that is already RTL
    "left-hand": "right-hand",
    "right-hand": "left-hand",
    "bottom-left": "bottom-right",
    "bottom-right": "bottom-left",
    "top-left": "top-right",
    "top-right": "top-left",
    "left-aligned": "right-aligned",
    "right-aligned": "left-aligned",
    "leftward": "rightward",
    "rightward": "leftward",
    "left": "right",
    "right": "left",
}

_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_SWAP, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def mirror_directional_text(text: str) -> str:
    """`text` with its left-to-right direction words mirrored. Single pass; other words untouched."""
    return _PATTERN.sub(lambda m: _SWAP.get(m.group(0).lower(), m.group(0)), text)


def is_rtl(language: str | None) -> bool:
    return language == "ar"


def for_language(text: str, language: str | None) -> str:
    """`text` mirrored for an RTL language, unchanged otherwise."""
    return mirror_directional_text(text) if is_rtl(language) else text
