"""Stable offline prompt-size proxy used by regression tests.

This is intentionally not a provider tokenizer.  It keeps token-budget tests
deterministic when the exact GLM tokenizer is unavailable locally; provider
``usage.prompt_tokens`` remains the billing source of truth.
"""

from __future__ import annotations

import math
import re


_CJK = re.compile(r"[\u3400-\u9fff]")
_ASCII_ALNUM = re.compile(r"[A-Za-z0-9]")


def approximate_prompt_tokens(*parts: str) -> int:
    """Estimate prompt size from Han characters, ASCII text, and punctuation."""
    total = 0
    for text in parts:
        han = len(_CJK.findall(text))
        ascii_alnum = len(_ASCII_ALNUM.findall(text))
        whitespace = sum(character.isspace() for character in text)
        punctuation = len(text) - han - ascii_alnum - whitespace
        total += math.ceil(han + ascii_alnum / 4 + punctuation / 2)
    return total
