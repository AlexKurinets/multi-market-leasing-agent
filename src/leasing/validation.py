"""Number grounding checks for model-written text.

The model may phrase things however it likes, but every figure it states has to come from data
it was given. Briefs are checked against their input payload; conversation replies are checked
against the tool results in the transcript.

The check is by value: it proves each figure exists somewhere in the source data, not that it
is attached to the right label ("3%" passes if any source value is 3). Misattribution is what
the eval set in ``evals/`` is for.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

Kind = Literal["currency", "percent", "number"]

_NUMBER = re.compile(
    r"(?<![\w.])(?P<cur>\$)?\s?(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<frac>\d+))?(?P<pct>\s?%)?"
)


@dataclass(frozen=True)
class NumberToken:
    raw: str
    value: Decimal
    kind: Kind


def extract_numbers(text: str) -> list[NumberToken]:
    tokens: list[NumberToken] = []
    for m in _NUMBER.finditer(text):
        digits = m.group("int").replace(",", "")
        if m.group("frac"):
            digits += "." + m.group("frac")
        try:
            value = Decimal(digits)
        except InvalidOperation:
            continue
        kind: Kind = "currency" if m.group("cur") else "percent" if m.group("pct") else "number"
        tokens.append(NumberToken(m.group(0).strip(), value, kind))
    return tokens


def unsupported_numbers(
    text: str, sources: Iterable[str], kinds: Iterable[Kind] = ("currency", "percent", "number")
) -> list[NumberToken]:
    """Numbers in ``text`` (of the given kinds) whose value appears in none of ``sources``."""
    allowed = {tok.value for source in sources for tok in extract_numbers(source)}
    wanted = set(kinds)
    return [t for t in extract_numbers(text) if t.kind in wanted and t.value not in allowed]
