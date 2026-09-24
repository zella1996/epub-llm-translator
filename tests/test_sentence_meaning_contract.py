from __future__ import annotations

from translator.sentence_analyzer import Sentence
from translator.translator import _parse_cards


def test_quality_mock_discards_retired_understanding_card_fields():
    sentences = [Sentence(1, "Although it is long, its meaning remains testable.")]

    cards = _parse_cards(
        [
            {
                "index": 1,
                "difficulty": "effortful",
                "meaning": "尽管它很长，但意思仍可验证。",
                "basis": "structure",
                "cues": "先让步。",
            }
        ],
        sentences,
        {1},
    )

    assert set(cards[0].__dict__) == {"sentence", "difficulty", "meaning"}


def test_quality_mock_keeps_only_sentence_meaning_card_fields():
    cards = _parse_cards(
        [
            {
                "index": 1,
                "difficulty": "effortful",
                "meaning": "尽管它很长，但意思仍可验证。",
            }
        ],
        [Sentence(1, "Although it is long, its meaning remains testable.")],
        {1},
    )

    assert cards[0].__dict__ == {
        "sentence": "Although it is long, its meaning remains testable.",
        "difficulty": "effortful",
        "meaning": "尽管它很长，但意思仍可验证。",
    }
