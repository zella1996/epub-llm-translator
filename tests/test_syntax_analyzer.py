from __future__ import annotations

import os
import sys
import threading
from types import SimpleNamespace

import pytest

from translator.sentence_analyzer import (
    ClauseHint,
    LocalSyntaxResult,
    Sentence,
    SpacySyntaxAnalyzer,
    StanzaSyntaxAnalyzer,
    SyntaxAnalyzerUnavailable,
    create_syntax_analyzer,
)


@pytest.fixture(scope="module")
def analyzer():
    pytest.importorskip("spacy")
    try:
        return SpacySyntaxAnalyzer()
    except RuntimeError as exc:
        pytest.skip(str(exc))


def test_spacy_syntax_gate_distinguishes_simple_and_nested_clauses(analyzer):
    results = analyzer.analyze(
        [
            Sentence(1, "She laughed."),
            Sentence(
                2,
                "The letter which the woman whom I met yesterday had written was never delivered.",
            ),
        ]
    )

    assert not results[1].needs_structure
    assert results[2].needs_structure
    assert results[2].max_clause_depth == 2
    assert "nested-clauses" in results[2].reasons
    assert {hint.target for hint in results[2].hints} == {"The letter", "the woman"}


@pytest.mark.parametrize(
    "text",
    [
        "Never had she imagined that he would leave.",
        "Had he known the truth, he would have stayed.",
    ],
)
def test_spacy_records_but_does_not_render_inversions_as_clause_links(analyzer, text):
    result = analyzer.analyze([Sentence(1, text)])[1]

    assert result.has_inversion
    assert not result.needs_structure
    assert result.hints == ()


def test_spacy_omits_non_relative_subtrees_inside_a_relative_clause(analyzer):
    result = analyzer.analyze(
        [
            Sentence(
                1,
                "A man who has nothing to do with his own time has no conscience in his intrusion on that of others.",
            )
        ]
    )[1]

    assert result.needs_structure
    assert [(hint.clause, hint.target) for hint in result.hints] == [
        ("who has nothing to do with his own time", "A man")
    ]


def test_spacy_keeps_explicit_that_relative_clauses(analyzer):
    result = analyzer.analyze(
        [
            Sentence(
                1,
                "Marianne had been given a horse, one that he had bred himself.",
            )
        ]
    )[1]

    assert [(hint.clause, hint.target) for hint in result.hints] == [
        ("that he had bred himself", "one")
    ]


def test_spacy_omits_content_clauses_without_an_explicit_relative_marker(analyzer):
    result = analyzer.analyze([Sentence(1, "That she left surprised him.")])[1]

    assert not result.needs_structure
    assert result.hints == ()


def test_spacy_keeps_chained_relative_clauses_for_same_appositive_antecedent(analyzer):
    text = (
        "Though aware, before she began it, that it must bring a confession of "
        "his inconstancy, and confirm their separation for ever, she was not aware "
        "that such language could be suffered to announce it; nor could she have "
        "supposed Willoughby capable of departing so far from the appearance of "
        "every honourable and delicate feeling—so far from the common decorum of a "
        "gentleman, as to send a letter so impudently cruel: a letter which, instead "
        "of bringing with his desire of a release any professions of regret, "
        "acknowledged no breach of faith, denied all peculiar affection whatever—a "
        "letter of which every line was an insult, and which proclaimed its writer "
        "to be deep in hardened villainy."
    )

    result = analyzer.analyze([Sentence(1, text)])[1]

    assert {(hint.clause, hint.target) for hint in result.hints} >= {
        (
            "which, instead of bringing with his desire of a release any "
            "professions of regret, acknowledged no breach of faith, denied all "
            "peculiar affection whatever",
            "a letter",
        ),
        ("of which every line was an insult", "a letter"),
        ("which proclaimed its writer to be deep in hardened villainy", "a letter"),
    }


def test_spacy_omits_relative_candidates_with_truncated_or_main_clause_spans(analyzer):
    text = (
        "In her earnest meditations on the contents of the letter, on the depravity "
        "of that mind which could dictate it, and probably, on the very different "
        "mind of a very different person, who had no other connection whatever with "
        "the affair than what her heart gave him with every thing that passed, Elinor "
        "forgot the immediate distress of her sister, forgot that she had three "
        "letters on her lap yet unread, and so entirely forgot how long she had been "
        "in the room, that when on hearing a carriage drive up to the door, she went "
        "to the window to see who could be coming so unreasonably early, she was all "
        "astonishment to perceive Mrs. Jennings’s chariot, which she knew had not "
        "been ordered till one."
    )

    result = analyzer.analyze([Sentence(1, text)])[1]

    assert not any(hint.clause.endswith("and probably") for hint in result.hints)
    assert not any("Elinor forgot" in hint.clause for hint in result.hints)


def test_spacy_normalizes_epub_line_wrapping_in_reader_facing_targets(analyzer):
    result = analyzer.analyze(
        [Sentence(1, "The imprudence\nwhich had hazarded such proofs was regretted.")]
    )[1]

    assert len(result.hints) == 1
    assert result.hints[0].target == "The imprudence"


def test_stanza_keeps_coordinated_overlapping_link_for_same_target():
    hints = [
        ClauseHint("acl:relcl", "whom she found attempting to rise", "Marianne", 1),
        ClauseHint(
            "acl:relcl",
            "whom she found attempting to rise, and whom she reached in time",
            "Marianne",
            1,
        ),
        ClauseHint("acl:relcl", "which was kind", "the letter", 1),
    ]

    assert StanzaSyntaxAnalyzer._dedupe_reader_hints(hints) == [
        hints[1],
        hints[2],
    ]


def test_stanza_batches_sentences_in_one_pipeline_call():
    observed = []

    class FakePipeline:
        def bulk_process(self, texts):
            observed.append(list(texts))
            return [
                SimpleNamespace(sentences=[SimpleNamespace(id=index)])
                for index, _ in enumerate(texts, start=1)
            ]

    analyzer = object.__new__(StanzaSyntaxAnalyzer)
    analyzer._lock = threading.Lock()
    analyzer._nlp = FakePipeline()
    analyzer._analyze_sentence = lambda text, sentence: LocalSyntaxResult(
        reasons=(f"{text}:{sentence.id}",)
    )

    results = analyzer.analyze(
        [Sentence(1, "First sentence."), Sentence(2, "Second sentence.")]
    )

    assert observed == [["First sentence.", "Second sentence."]]
    assert results[1].reasons == ("First sentence.:1",)
    assert results[2].reasons == ("Second sentence.:2",)


def _obsolete_reader_guide_policy_gates_by_length_and_caps_modification_relations():
    modifiers = (
        ReaderModifier("interruption", "who watched …", "the reader", "回到主干"),
        ReaderModifier("time", "while they spoke", "watched", "说明时间"),
        ReaderModifier("reason", "because it mattered", "spoke", "说明原因"),
    )
    result = LocalSyntaxResult(
        reasons=("interrupted-main-clause",),
        skeleton="The reader understood the sentence.",
        reader_modifiers=modifiers,
    )
    short = "The reader who watched them speak understood the sentence."
    long = " ".join(["word"] * 40)

    assert select_reader_guide(short, result) is None
    guide = select_reader_guide(long, result)
    assert guide is not None
    assert guide.skeleton == "The reader understood the sentence."
    assert guide.modifiers == modifiers[:2]


def test_stanza_accurate_load_uses_explicit_local_transformer_cache(
    tmp_path, monkeypatch
):
    hf_cache = tmp_path / "huggingface"
    snapshot = (
        hf_cache
        / "hub"
        / "models--google--electra-large-discriminator"
        / "snapshots"
        / "test-revision"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    (snapshot / "pytorch_model.bin").write_bytes(b"test")
    observed = {}

    def pipeline(language, **options):
        observed.update(language=language, hf_home=os.environ.get("HF_HOME"), **options)
        return object()

    monkeypatch.setitem(sys.modules, "stanza", SimpleNamespace(Pipeline=pipeline))
    monkeypatch.setenv("HF_HOME", "before-test")

    analyzer = StanzaSyntaxAnalyzer(
        "default_accurate",
        use_gpu=False,
        hf_cache_dir=str(hf_cache),
    )

    assert analyzer.package == "default_accurate"
    assert observed == {
        "language": "en",
        "hf_home": str(hf_cache),
        "package": "default_accurate",
        "processors": "tokenize,pos,lemma,depparse,constituency",
        "use_gpu": False,
        "download_method": None,
        "verbose": False,
    }


def test_stanza_factory_defaults_to_the_accurate_package(tmp_path, monkeypatch):
    snapshot = (
        tmp_path
        / "hub"
        / "models--google--electra-large-discriminator"
        / "snapshots"
        / "test-revision"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    (snapshot / "pytorch_model.bin").write_bytes(b"test")
    observed = {}

    def pipeline(_, **options):
        observed.update(options)
        return object()

    monkeypatch.setitem(sys.modules, "stanza", SimpleNamespace(Pipeline=pipeline))
    monkeypatch.setenv("HF_HOME", "before-test")

    create_syntax_analyzer("stanza", stanza_hf_cache_dir=str(tmp_path))

    assert observed["package"] == "default_accurate"


def test_stanza_accurate_fails_before_loading_when_transformer_cache_is_missing(
    tmp_path, monkeypatch
):
    called = False

    def pipeline(*_, **__):
        nonlocal called
        called = True
        return object()

    monkeypatch.setitem(sys.modules, "stanza", SimpleNamespace(Pipeline=pipeline))
    missing = tmp_path / "missing-huggingface-cache"

    with pytest.raises(
        SyntaxAnalyzerUnavailable, match="Hugging Face 缓存目录不存在"
    ):
        StanzaSyntaxAnalyzer(
            "default_accurate",
            use_gpu=False,
            hf_cache_dir=str(missing),
        )

    assert called is False


def test_stanza_accurate_fails_before_loading_when_electra_weights_are_missing(
    tmp_path, monkeypatch
):
    called = False

    def pipeline(*_, **__):
        nonlocal called
        called = True
        return object()

    monkeypatch.setitem(sys.modules, "stanza", SimpleNamespace(Pipeline=pipeline))
    empty_cache = tmp_path / "huggingface"
    empty_cache.mkdir()

    with pytest.raises(
        SyntaxAnalyzerUnavailable, match="缺少 default_accurate 所需的 Electra-large"
    ):
        StanzaSyntaxAnalyzer(
            "default_accurate",
            use_gpu=False,
            hf_cache_dir=str(empty_cache),
        )

    assert called is False


@pytest.fixture(scope="module")
def stanza_analyzer():
    pytest.importorskip("stanza")
    try:
        return StanzaSyntaxAnalyzer(
            use_gpu=False,
            model_dir=os.environ.get("STANZA_TEST_MODEL_DIR"),
            hf_cache_dir=os.environ.get("STANZA_TEST_HF_CACHE_DIR"),
        )
    except SyntaxAnalyzerUnavailable as exc:
        pytest.skip(f"{exc}; cause={exc.__cause__!r}")


def test_stanza_extracts_nested_relative_clause_targets(stanza_analyzer):
    text = "The letter which the woman whom I met yesterday had written was never delivered."
    result = stanza_analyzer.analyze([Sentence(1, text)])[1]

    assert result.needs_structure
    assert result.max_clause_depth == 2
    assert {(hint.clause, hint.target) for hint in result.hints} == {
        ("which the woman whom I met yesterday had written", "The letter"),
        ("whom I met yesterday", "the woman"),
    }


def test_stanza_skips_comma_before_a_nonrestrictive_relative_clause(stanza_analyzer):
    result = stanza_analyzer.analyze(
        [Sentence(1, "The letter, which the woman wrote, was lost.")]
    )[1]

    assert [(hint.clause, hint.target) for hint in result.hints] == [
        ("which the woman wrote", "The letter")
    ]


def test_stanza_omits_non_relative_sbar_candidates(stanza_analyzer):
    text = (
        "The grounds were declared to be highly beautiful, and Sir John, who was "
        "particularly warm in their praise, might be allowed to be a tolerable "
        "judge, for he had formed parties to visit them, at least, twice every "
        "summer for the last ten years."
    )
    result = stanza_analyzer.analyze([Sentence(1, text)])[1]

    assert result.needs_structure
    assert [(hint.clause, hint.target) for hint in result.hints] == [
        ("who was particularly warm in their praise", "Sir John")
    ]


def _obsolete_stanza_builds_reader_mainline_and_modification_relations(stanza_analyzer):
    text = (
        "Mrs. Jennings, who had watched them with pleasure while they were talking, "
        "and who expected to see the effect of Miss Dashwood’s communication, in such "
        "an instantaneous gaiety on Colonel Brandon’s side, as might have become a man "
        "in the bloom of youth, of hope and happiness, saw him, with amazement, remain "
        "the whole evening more serious and thoughtful than usual."
    )

    result = stanza_analyzer.analyze([Sentence(1, text)])[1]

    assert result.skeleton == (
        "Mrs. Jennings saw him remain the whole evening more serious and thoughtful "
        "than usual."
    )
    assert [modifier.kind for modifier in result.reader_modifiers] == [
        "interruption",
        "time",
    ]
    assert result.reader_modifiers[0].clause == (
        "who had watched … and who expected …"
    )
    assert result.reader_modifiers[0].target == "Mrs. Jennings"
    assert result.reader_modifiers[0].explanation == (
        "补充说明 Mrs. Jennings；读到 saw him 时回到主干"
    )
    assert result.reader_modifiers[1].clause == "while they were talking"
    assert result.reader_modifiers[1].target == "watched"
    assert result.reader_modifiers[1].explanation == "说明 watched 发生的时间"


def _obsolete_stanza_reader_guide_keeps_negated_auxiliary_in_mainline(stanza_analyzer):
    text = (
        "Well, it don’t signify talking; but when a young man, be who he will, "
        "comes and makes love to a pretty girl, and promises marriage, he has no "
        "business to fly off from his word only because he grows poor, and a richer "
        "girl is ready to have him."
    )

    result = stanza_analyzer.analyze([Sentence(1, text)])[1]
    guide = select_reader_guide(text, result)

    assert guide is not None
    assert guide.skeleton == (
        "Well, it don’t signify talking; but when a young man, be who he will, "
        "comes and makes love to a pretty girl, and promises marriage, he has no "
        "business to fly off from his word."
    )


def test_stanza_prefers_nominal_antecedent_over_trailing_reflexive_pronoun(
    stanza_analyzer,
):
    text = (
        "The Miss Dashwoods had no greater reason to be dissatisfied with Mrs. "
        "Jennings’s style of living, and set of acquaintance, than with her "
        "behaviour to themselves, which was invariably kind."
    )

    result = stanza_analyzer.analyze([Sentence(1, text)])[1]

    assert [(hint.clause, hint.target) for hint in result.hints] == [
        ("which was invariably kind", "her behaviour")
    ]


def test_stanza_keeps_a_coordinated_relative_clause_together(stanza_analyzer):
    text = (
        "The late owner of this estate was a single man, who lived to a very "
        "advanced age, and who for many years of his life, had a constant "
        "companion and housekeeper in his sister."
    )

    result = stanza_analyzer.analyze([Sentence(1, text)])[1]

    assert [(hint.clause, hint.target) for hint in result.hints] == [
        (
            "who lived to a very advanced age, and who for many years of his "
            "life, had a constant companion and housekeeper in his sister",
            "a single man",
        )
    ]


def test_stanza_keeps_explicit_that_relative_clause_for_an_appositive(
    stanza_analyzer,
):
    text = (
        "Marianne told her, with the greatest delight, that Willoughby had given "
        "her a horse, one that he had bred himself on his estate in Somersetshire."
    )

    result = stanza_analyzer.analyze([Sentence(1, text)])[1]

    assert [(hint.clause, hint.target) for hint in result.hints] == [
        ("that he had bred himself on his estate in Somersetshire", "one")
    ]


def test_stanza_omits_result_that_clause_without_a_nominal_antecedent(
    stanza_analyzer,
):
    text = (
        "Elinor and her mother rose up in amazement at their entrance, and while "
        "the eyes of both were fixed on him with an evident wonder and a secret "
        "admiration which equally sprung from his appearance, he apologized for "
        "his intrusion by relating its cause, in a manner so frank and so graceful "
        "that his person, which was uncommonly handsome, received additional charms "
        "from his voice and expression."
    )

    result = stanza_analyzer.analyze([Sentence(1, text)])[1]

    assert not any(hint.clause.startswith("that his person") for hint in result.hints)


def test_stanza_omits_unhelpful_than_correlative(stanza_analyzer):
    text = (
        "No sooner was her answer dispatched, than Mrs. Dashwood indulged herself "
        "in the pleasure of announcing to her son-in-law and his wife that she was "
        "provided with a house, and should incommode them no longer than till every "
        "thing were ready for her inhabiting it."
    )
    result = stanza_analyzer.analyze([Sentence(1, text)])[1]

    assert not any(hint.clause.startswith("than Mrs. Dashwood") for hint in result.hints)
    assert not any(hint.target == "was" for hint in result.hints)
