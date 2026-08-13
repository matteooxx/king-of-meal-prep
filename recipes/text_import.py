"""Deterministic parser for recipes pasted from notes, email, or chat."""
from __future__ import annotations

import re

_INGREDIENT_HEADINGS = {
    "ingredient",
    "ingredients",
    "what you need",
}
_STEP_HEADINGS = {
    "direction",
    "directions",
    "instruction",
    "instructions",
    "method",
    "steps",
    "preparation",
}
_ALL_HEADINGS = _INGREDIENT_HEADINGS | _STEP_HEADINGS


def _clean_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^[\-\*\u2022]\s*", "", line)
    line = re.sub(r"^\d{1,3}[\.\)]\s+", "", line)
    return line.strip()


def _heading(line: str) -> str:
    return re.sub(r"[^a-z ]", "", line.lower()).strip()


def _blocks(text: str) -> list[list[str]]:
    return [
        [line.strip() for line in block.splitlines() if line.strip()]
        for block in re.split(r"\n\s*\n", text.strip())
        if block.strip()
    ]


def parse(text: str) -> dict:
    lines = [line.strip() for line in text.replace("\r", "\n").splitlines()]
    nonempty = [line for line in lines if line]
    if not nonempty:
        raise ValueError("recipe text is empty")

    title = _clean_line(nonempty[0])
    servings = 1
    servings_match = re.search(
        r"\b(?:serves|servings?|yield)\s*[:\-]?\s*(\d{1,2})\b",
        text,
        flags=re.IGNORECASE,
    )
    if servings_match:
        servings = max(1, min(99, int(servings_match.group(1))))

    ingredients: list[str] = []
    steps: list[str] = []
    section: str | None = None
    saw_heading = False
    for raw in nonempty[1:]:
        heading = _heading(raw.rstrip(":"))
        if heading in _INGREDIENT_HEADINGS:
            section = "ingredients"
            saw_heading = True
            continue
        if heading in _STEP_HEADINGS:
            section = "steps"
            saw_heading = True
            continue
        cleaned = _clean_line(raw)
        if not cleaned:
            continue
        if section == "ingredients":
            ingredients.append(cleaned)
        elif section == "steps":
            steps.append(cleaned)

    if not saw_heading:
        blocks = _blocks(text)
        if len(blocks) >= 3 or (
            len(blocks) >= 2 and len(blocks[1]) >= 2
        ):
            first = blocks[0]
            title = _clean_line(first[0])
            ingredients = [_clean_line(line) for line in blocks[1]]
            steps = [
                _clean_line(line)
                for block in blocks[2:]
                for line in block
            ]

    ingredients = [
        line for line in ingredients
        if line and _heading(line.rstrip(":")) not in _ALL_HEADINGS
    ]
    steps = [line for line in steps if line]
    if not ingredients:
        raise ValueError(
            "could not find an Ingredients section; add that heading and retry"
        )

    time_match = re.search(
        r"\b(?:total\s+time|time)\s*[:\-]?\s*(\d{1,3})\s*(?:min|minutes?)\b",
        text,
        flags=re.IGNORECASE,
    )
    total_time = int(time_match.group(1)) if time_match else None
    return {
        "name": title[:200],
        "servings": servings,
        "total_time_min": total_time,
        "ingredients": ingredients[:200],
        "steps": steps[:100],
    }
