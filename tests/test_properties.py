from argparse import Namespace

import pytest

from translator.cli import _api_key, _api_key_name
from translator.epub_processor import EpubError
from translator.properties import load_properties


def _args(tmp_path, *, provider="generic", api_key_env=None):
    return Namespace(
        api_key_env=api_key_env,
        properties_file=tmp_path / "secrets.properties",
        provider_profile=provider,
        tuning_profile="generic",
        fast_model="test",
        quality_model=None,
    )


def test_properties_support_comments_whitespace_and_separators(tmp_path):
    path = tmp_path / "secrets.properties"
    path.write_text(
        "# credentials\n ZAI_API_KEY = zai=value \nDASHSCOPE_API_KEY: bailian:value\n",
        encoding="utf-8",
    )

    assert load_properties(path) == {
        "ZAI_API_KEY": "zai=value",
        "DASHSCOPE_API_KEY": "bailian:value",
    }


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("zhipu", "ZAI_API_KEY"),
        ("qwen-bailian", "DASHSCOPE_API_KEY"),
        ("bailian", "DASHSCOPE_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
        ("generic", "OPENAI_API_KEY"),
    ],
)
def test_provider_selects_default_key_name(tmp_path, provider, expected):
    assert _api_key_name(_args(tmp_path, provider=provider)) == expected


def test_properties_supply_key_and_environment_has_priority(tmp_path, monkeypatch):
    args = _args(tmp_path, provider="zhipu")
    args.properties_file.write_text("ZAI_API_KEY=from-properties\n", encoding="utf-8")
    assert _api_key(args) == "from-properties"

    monkeypatch.setenv("ZAI_API_KEY", "from-environment")
    assert _api_key(args) == "from-environment"


def test_explicit_key_name_overrides_provider_default(tmp_path):
    args = _args(tmp_path, provider="zhipu", api_key_env="CUSTOM_KEY")
    args.properties_file.write_text("CUSTOM_KEY=custom\n", encoding="utf-8")
    assert _api_key(args) == "custom"


def test_invalid_properties_is_reported_without_leaking_contents(tmp_path):
    args = _args(tmp_path)
    args.properties_file.write_text("not-an-assignment\n", encoding="utf-8")
    with pytest.raises(EpubError, match="无法读取密钥配置"):
        _api_key(args)
