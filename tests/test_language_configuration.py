import pytest

from translator.languages import (
    LanguageConfigurationError,
    resolve_effective_language_configuration,
    resolve_source_language,
)
from translator.tuning_profiles import tuning_profile


@pytest.mark.parametrize("label", ["ja", "ja-JP", "JA-jp", "ja_JP"])
def test_japanese_labels_normalize_to_one_registered_source(label):
    source, originals = resolve_source_language([label])

    assert source.source_code == "ja"
    assert originals == (label,)
    assert source.gate_version == "japanese-short-text-safe-v1"
    assert source.syntax_identity == "off-v1"


def test_missing_book_language_retains_english_compatibility():
    source, originals = resolve_source_language([])

    assert source.source_code == "en"
    assert originals == ()


def test_multiple_equivalent_labels_are_not_ambiguous():
    source, originals = resolve_source_language(["JA-jp", "ja"])

    assert source.source_code == "ja"
    assert originals == ("JA-jp", "ja")


@pytest.mark.parametrize("labels", [("en", "ja"), ("en-US", "ja-JP")])
def test_multiple_distinct_languages_fail_explicitly(labels):
    with pytest.raises(LanguageConfigurationError, match="多个互不相同"):
        resolve_source_language(labels)


def test_unknown_language_fails_explicitly():
    with pytest.raises(LanguageConfigurationError, match="不支持的 EPUB 源语言: fr"):
        resolve_source_language(["fr-FR"])


def test_japanese_selects_its_own_task_and_versioned_cache_identity():
    tuning = tuning_profile("generic-ja")
    resolved = resolve_effective_language_configuration(
        ["ja-JP"],
        tuning_name=tuning.name,
        tuning_source_code=tuning.source_language,
        language_profile_key=tuning.language_profile,
    )

    assert resolved.task.key == "ja-zh-Hans"
    assert resolved.task.implemented
    assert resolved.original_languages == ("ja-JP",)
    assert resolved.cache_identity == {
        "source_language": "ja",
        "translation_task": "ja-zh-Hans",
        "splitter": "japanese-exact-v3",
        "gate": "japanese-short-text-safe-v1",
        "validator": "japanese-output-v2",
        "syntax": "off-v1",
        "tuning_profile": "generic-ja",
        "language_profile": "ja-zh-Hans-v2-japanese-reading-meaning",
        "prompt_fingerprint": "japanese-reading-meaning-v2-e5fec3247464",
    }


def test_mismatched_japanese_task_or_english_tuning_fails_before_transport():
    english = tuning_profile("generic")
    with pytest.raises(LanguageConfigurationError, match="仅适用于 en 源语言"):
        resolve_effective_language_configuration(
            ["ja"],
            tuning_name=english.name,
            tuning_source_code=english.source_language,
            language_profile_key=english.language_profile,
        )

    japanese = tuning_profile("generic-ja")
    with pytest.raises(LanguageConfigurationError, match="必须使用任务 ja-zh-Hans"):
        resolve_effective_language_configuration(
            ["ja"],
            tuning_name=japanese.name,
            tuning_source_code=japanese.source_language,
            language_profile_key=japanese.language_profile,
            explicit_task_key="en-zh-Hans",
        )


def test_existing_english_profile_resolves_without_changing_profile_key():
    tuning = tuning_profile("glm52-grounded")
    resolved = resolve_effective_language_configuration(
        ["EN-us"],
        tuning_name=tuning.name,
        tuning_source_code=tuning.source_language,
        language_profile_key=tuning.language_profile,
    )

    assert resolved.source.source_code == "en"
    assert resolved.task.key == "en-zh-Hans"
    assert resolved.task.implemented
    assert resolved.language_profile == "en-zh-Hans-v16-compact-envelope"
