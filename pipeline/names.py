from __future__ import annotations

import re


def firm_aliases(firm: str) -> list[str]:
    """'Peak XV Partners (formerly Sequoia Capital India)' -> ['Peak XV Partners', 'Sequoia Capital India'].

    Old names are kept: pages written before a rebrand still refer to the firm by it.
    """
    aliases: list[str] = []
    main = re.sub(r"\([^)]*\)", "", firm).strip()
    if main:
        aliases.append(main)
    for inner in re.findall(r"\(([^)]*)\)", firm):
        inner = re.sub(r"^\s*(formerly|previously|fka|f/k/a)\s+", "", inner, flags=re.I).strip()
        if inner:
            aliases.append(inner)
    return aliases


def mentions(text: str, needle: str) -> bool:
    """Whole-word, case-insensitive match. 'Accel' must not match 'accelerate'."""
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", text, flags=re.I) is not None
