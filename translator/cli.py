"""Command-line interface for inspecting books and generating a one-paragraph preview."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Mapping

from translator.epub_processor import (
    DEFAULT_ANALYSIS_LINK_TEXT, EpubBook, EpubError, run_epubcheck,
)
from translator.epub_normalizer import normalize_epub
from translator.cache import LearningCache
from translator.llm_api import (
    LLMError,
    OpenAICompatibleClient,
    OpenAIConfig,
    RateLimiter,
    Usage,
)
from translator.openrouter_client import OpenRouterClient, OpenRouterConfig
from translator.profile import CalibrationExample, ReadingProfile, calibration_sentences
from translator.properties import DEFAULT_PROPERTIES_FILE, load_properties
from translator.languages import (
    EffectiveLanguageConfiguration, LanguageConfigurationError, language_profile,
    japanese_short_text_gate,
    resolve_effective_language_configuration,
)
from translator.provider_profiles import PROVIDER_PROFILES, provider_profile
from translator.tuning_profiles import TUNING_PROFILES, ModelTuning, tuning_profile
from translator.sentence_analyzer import (
    STANZA_ACCURATE_TRANSFORMER_CACHE,
    SyntaxAnalyzerUnavailable,
    create_syntax_analyzer,
    local_model_gate,
)
from translator.trace import TraceRun
from translator.reading_assistance import AssistanceError
from translator.translator import (
    TranslationEstimate,
    TranslationProgress,
    generate_preview,
    generate_reading_preview,
    replay_quality,
    translate_reading_book,
    translate_book,
)


QUALITY_PAYLOAD_VERSION = "quality-candidates-v6-sentence-meaning"


def _chapter_paragraph(value: str) -> tuple[int, int]:
    try:
        chapter_text, paragraph_text = value.split(":", 1)
        chapter, paragraph = int(chapter_text), int(paragraph_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("段落坐标必须使用 CHAPTER:PARAGRAPH") from exc
    if chapter < 1 or paragraph < 1:
        raise argparse.ArgumentTypeError("章节号和段落号必须大于 0")
    return chapter, paragraph


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="epub-llm-translator",
        description="生成保留英文原文的 Kindle 友好型 EPUB 学习版。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="预检 EPUB 并列出可选择的正文段落"
    )
    inspect_parser.add_argument("input", type=Path)
    inspect_parser.add_argument("--chapter", type=int)
    inspect_parser.add_argument("--limit", type=int, default=30)
    inspect_parser.add_argument("--profile", type=Path)
    _local_gate_arguments(inspect_parser)
    _japanese_gate_argument(inspect_parser)
    _validation_arguments(inspect_parser)

    normalize_parser = subparsers.add_parser(
        "normalize", help="离线修复少数前置条件明确的 EPUB 结构错误"
    )
    normalize_parser.add_argument("input", type=Path)
    normalize_parser.add_argument("output", type=Path)
    normalize_parser.add_argument(
        "--dry-run", action="store_true", help="仅列出可安全应用的规则，不创建输出文件"
    )

    preview_parser = subparsers.add_parser(
        "preview", help="为一个指定段落生成 PREVIEW EPUB"
    )
    preview_parser.add_argument("input", type=Path)
    preview_parser.add_argument("output", type=Path)
    preview_parser.add_argument("--chapter", type=int, required=True)
    preview_parser.add_argument("--paragraph", type=int, required=True)
    preview_parser.add_argument("--pipeline", choices=("legacy", "reading-v1"), default="legacy")
    _analysis_link_text_argument(preview_parser)
    _japanese_gate_argument(preview_parser)
    preview_parser.add_argument("--cache", type=Path, default=Path(".epub-llm-translator/cache.sqlite3"))
    preview_parser.add_argument("--cache-profile-key")
    preview_parser.add_argument("--manifest", type=Path, help="reading-v1 可携带冻结结果清单")
    preview_parser.add_argument(
        "--reading-layout", choices=("translation-first", "aids-first"),
        default="translation-first", help="reading-v1 辅助页展示顺序",
    )
    preview_parser.add_argument(
        "--reading-inline-phrases", action="store_true",
        help="实验：将可安全定位的短语变成普通单向链接",
    )
    preview_parser.add_argument(
        "--reading-paragraph-aids", action="store_true",
        help="reading-v1 生成并渲染段落级跨句解析；默认不生成",
    )
    preview_parser.add_argument("--base-url")
    preview_parser.add_argument("--fast-model")
    preview_parser.add_argument(
        "--quality-model",
        help="默认与 --fast-model 相同",
    )
    _credential_arguments(preview_parser)
    preview_parser.add_argument("--timeout", type=float, default=120.0)
    preview_parser.add_argument("--max-retries", type=int, default=2)
    preview_parser.add_argument("--schema-retries", type=int, default=1)
    preview_parser.add_argument("--requests-per-minute", type=float)
    _generation_arguments(preview_parser)
    _provider_argument(preview_parser)
    _thinking_argument(preview_parser)
    _book_context_arguments(preview_parser)
    preview_parser.add_argument("--profile", type=Path)
    preview_parser.add_argument(
        "--density", choices=("low", "medium", "high"), default="medium"
    )
    _syntax_arguments(preview_parser)
    _validation_arguments(preview_parser)
    preview_parser.add_argument(
        "--trace-dir", type=Path, help="开发调试：记录无密钥的完整模型请求与响应"
    )

    replay_parser = subparsers.add_parser(
        "quality-replay", help="开发：使用冻结 fast trace 仅重放 quality 阶段"
    )
    replay_parser.add_argument("input", type=Path, help="quality-replay-input.json")
    replay_parser.add_argument("--base-url", required=True)
    replay_parser.add_argument("--quality-model", required=True)
    _credential_arguments(replay_parser)
    replay_parser.add_argument("--timeout", type=float, default=120.0)
    replay_parser.add_argument("--max-retries", type=int, default=2)
    replay_parser.add_argument("--requests-per-minute", type=float)
    _generation_arguments(replay_parser)
    _provider_argument(replay_parser)
    _thinking_argument(replay_parser)
    replay_parser.add_argument(
        "--trace-dir", type=Path, required=True, help="replay 请求与响应的输出 trace 目录"
    )

    reading_parser = subparsers.add_parser(
        "reading-eval", help="开发：纯文本阅读辅助实验，不生成 EPUB 或写入生产缓存"
    )
    reading_parser.add_argument("input", type=Path, help="单段 JSON，含 paragraph 与可选冻结证据")
    reading_parser.add_argument("--pipeline", choices=("single", "two-stage", "both"), default="both")
    reading_parser.add_argument(
        "--repair-core", action="store_true",
        help="single 首次核心结果无效时，最多执行一次完整替换修订",
    )
    reading_parser.add_argument(
        "--directed-review", action="store_true",
        help="single 启用一次核心替换，并仅在结果降级或遗漏固定目标时再复核一次",
    )
    reading_parser.add_argument("--trace-dir", type=Path, required=True)
    mode = reading_parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--responses", type=Path, help="离线 mock 响应数组，不读取凭据或联网")
    mode.add_argument("--execute", action="store_true", help="显式允许本次输入外发；需指定请求与 token 预算")
    reading_parser.add_argument("--base-url")
    reading_parser.add_argument("--quality-model")
    reading_parser.add_argument("--input-token-reserve", type=int)
    reading_parser.add_argument("--max-tokens", type=int)
    reading_parser.add_argument("--timeout", type=float, default=120.0)
    reading_parser.set_defaults(max_retries=0, requests_per_minute=None)
    _credential_arguments(reading_parser)
    _generation_arguments(reading_parser, legacy_payload=False)
    _provider_argument(reading_parser)
    _thinking_argument(reading_parser)

    translate_parser = subparsers.add_parser(
        "translate", help="处理所选章节或整本书；成功前不会创建正式成品"
    )
    translate_parser.add_argument("input", type=Path)
    translate_parser.add_argument("output", type=Path)
    translate_parser.add_argument(
        "--pipeline", choices=("legacy", "reading-v1"), default="legacy",
        help="生成路径；默认 legacy，reading-v1 使用共享句译和阅读辅助",
    )
    _analysis_link_text_argument(translate_parser)
    _model_arguments(translate_parser)
    translate_parser.add_argument(
        "--chapter", type=int, action="append", help="可重复；默认处理所有正文章节"
    )
    translate_parser.add_argument("--workers", type=int, default=2)
    translate_parser.add_argument(
        "--progress",
        choices=("auto", "verbose", "off"),
        default="auto",
        help="控制台进度：auto 按时间/百分比节流，verbose 显示每个阶段，off 关闭",
    )
    translate_parser.add_argument(
        "--progress-interval",
        type=float,
        default=10.0,
        help="auto 模式静默运行时的最长进度间隔秒数（默认 10）",
    )
    translate_parser.add_argument(
        "--cache",
        type=Path,
        default=Path(".epub-llm-translator/cache.sqlite3"),
    )
    translate_parser.add_argument("--dry-run", action="store_true")
    translate_parser.add_argument("--max-tokens", type=int)
    translate_parser.add_argument(
        "--prior-tokens", type=int, default=0,
        help="已计入本次授权预算的历史 API token；续跑时从 --max-tokens 运行额度扣除（默认 0）",
    )
    translate_parser.add_argument("--max-cost", type=float)
    translate_parser.add_argument("--input-price", type=float)
    translate_parser.add_argument("--output-price", type=float)
    _local_gate_arguments(translate_parser, default=None)
    _japanese_gate_argument(translate_parser)
    _validation_arguments(translate_parser)
    translate_parser.add_argument(
        "--trace-dir", type=Path, help="开发调试：记录无密钥的完整模型请求与响应"
    )
    translate_parser.add_argument(
        "--frozen-manifest", type=Path,
        help="reading-v1 成功后导出的可携带冻结结果清单"
    )
    translate_parser.add_argument(
        "--reading-prior-context", action="store_true",
        help="reading-v1 将同章前一正文段作为有界背景并纳入缓存身份",
    )
    translate_parser.add_argument(
        "--reading-generation-pipeline", choices=("single", "two-stage"), default="single",
        help="reading-v1 内容生成：single 单次完成；two-stage 固定 draft 后完整复核",
    )
    translate_parser.add_argument(
        "--reading-directed-review", action="store_true",
        help="reading-v1 对核心失败执行至多一次替换修订，并对降级或固定目标遗漏执行一次定向复核",
    )
    translate_parser.add_argument(
        "--reading-inline-phrases", action="store_true",
        help="实验：将可安全定位的短语变成普通单向链接",
    )
    translate_parser.add_argument(
        "--reading-paragraph-aids", action="store_true",
        help="reading-v1 生成并渲染段落级跨句解析；默认不生成",
    )
    translate_parser.add_argument(
        "--reading-short-text-words", type=int, default=15,
        help="reading-v1 在访问模型前跳过总词数不超过该值的短文本；0 表示关闭（默认 15）",
    )
    translate_parser.add_argument(
        "--reading-skip-paragraph", type=_chapter_paragraph, action="append", default=[],
        metavar="CHAPTER:PARAGRAPH",
        help="reading-v1 显式跳过一个段落，不调用模型也不生成阅读辅助；可重复",
    )
    translate_parser.add_argument(
        "--reading-reprocess-paragraph", type=_chapter_paragraph, action="append", default=[],
        metavar="CHAPTER:PARAGRAPH",
        help="reading-v1 忽略指定段落的现有缓存并重新生成；新结果校验成功后才覆盖缓存；可重复",
    )
    translate_parser.add_argument(
        "--reading-output-token-floor", type=int, default=20000,
        help="reading-v1 执行余量：首次模型请求的最小输出 token 上限（默认 20000）",
    )
    translate_parser.add_argument(
        "--reading-length-retry-max-tokens", type=int, default=30000,
        help="reading-v1 正文为空且长度耗尽时的一次升额上限（默认 30000）",
    )

    clear_parser = subparsers.add_parser(
        "cache-clear", help="删除一本源书的全部本地模型缓存"
    )
    clear_parser.add_argument("input", type=Path)
    clear_parser.add_argument(
        "--cache",
        type=Path,
        default=Path(".epub-llm-translator/cache.sqlite3"),
    )

    calibrate_parser = subparsers.add_parser(
        "calibrate", help="交互标注真实句子并保存个人阅读画像"
    )
    calibrate_parser.add_argument("input", type=Path)
    calibrate_parser.add_argument(
        "--profile",
        type=Path,
        default=Path(".epub-llm-translator/profile.json"),
    )
    calibrate_parser.add_argument("--samples", type=int, default=24)
    calibrate_parser.add_argument(
        "--base-profile",
        type=Path,
        help="从长期画像派生书籍画像；输出仍写入 --profile，不修改基础文件",
    )
    calibrate_parser.add_argument("--overwrite", action="store_true")
    _validation_arguments(calibrate_parser)
    return parser


def _validation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--skip-epubcheck",
        action="store_true",
        help="仅供开发诊断；跳过外部 EPUBCheck，内部安全预检仍会运行",
    )
    parser.add_argument(
        "--allow-invalid-source",
        action="store_true",
        help="允许 EPUBCheck 报错的旧书继续内部严格预检；输出不得新增错误",
    )


def _model_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--fast-model", required=True)
    parser.add_argument("--quality-model", help="默认与 --fast-model 相同")
    _credential_arguments(parser)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--schema-retries", type=int, default=1)
    parser.add_argument("--requests-per-minute", type=float)
    _generation_arguments(parser)
    _provider_argument(parser)
    _thinking_argument(parser)
    _book_context_arguments(parser)
    parser.add_argument("--profile", type=Path)
    parser.add_argument(
        "--density", choices=("low", "medium", "high"), default="medium"
    )
    _syntax_arguments(parser)


def _analysis_link_text_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--analysis-link-text", default=DEFAULT_ANALYSIS_LINK_TEXT,
        help=f"段末解析链接的可见文字（默认 {DEFAULT_ANALYSIS_LINK_TEXT}；仅纯文本）",
    )


def _credential_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--properties-file",
        type=Path,
        default=DEFAULT_PROPERTIES_FILE,
        help="本地密钥配置文件；默认 secrets.properties（环境变量优先）",
    )
    parser.add_argument(
        "--api-key-env",
        help=(
            "API key 的环境变量/properties 键名；默认按供应商选择 "
            "ZAI_API_KEY、DASHSCOPE_API_KEY 或 OPENAI_API_KEY"
        ),
    )


def _thinking_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--thinking",
        choices=("default", "on", "off"),
        default="default",
        help="思考模式；default 使用供应商预设，generic 预设不发送扩展参数",
    )


def _generation_arguments(parser: argparse.ArgumentParser, *, legacy_payload: bool = True) -> None:
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument(
        "--tuning-profile",
        choices=tuple(TUNING_PROFILES),
        default="generic",
        help="模型组合的版本化提示词与请求参数预设；显式 CLI 参数优先",
    )
    parser.add_argument(
        "--sampling",
        choices=("default", "on", "off"),
        default="default",
        help="采样开关；zhipu 预设默认关闭以提高翻译可复现性",
    )
    parser.add_argument(
        "--response-max-tokens",
        type=int,
        help="每次模型响应的最大 token 数；zhipu 预设默认 2048",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("max", "xhigh", "high", "medium", "low", "minimal", "none"),
        help="智谱直连 GLM 开启思考时使用；百炼不支持此参数",
    )
    parser.add_argument(
        "--stream-response",
        action="store_true",
        help="使用 SSE 流式接收模型响应；仅影响传输，不改变缓存身份",
    )
    if legacy_payload:
        parser.add_argument(
            "--quality-payload-mode",
            choices=("compact-v5",),
            default="compact-v5",
            help="quality 请求结构；仅保留紧凑的句意卡格式",
        )


def _thinking_value(mode: str) -> bool | None:
    return {"default": None, "on": True, "off": False}[mode]


def _provider_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider-profile",
        choices=tuple(PROVIDER_PROFILES),
        default=None,
        help="不含密钥和 endpoint 的版本化供应商适配预设",
    )


def _effective_thinking(args: argparse.Namespace) -> bool | None:
    explicit = _thinking_value(args.thinking)
    if explicit is not None:
        return explicit
    tuning = _tuning(args)
    if tuning.thinking is not None:
        return tuning.thinking
    return provider_profile(_provider_name(args)).default_thinking


def _effective_sampling(args: argparse.Namespace) -> bool | None:
    explicit = {"default": None, "on": True, "off": False}[args.sampling]
    if explicit is not None:
        return explicit
    tuning = _tuning(args)
    if tuning.do_sample is not None:
        return tuning.do_sample
    return provider_profile(_provider_name(args)).default_do_sample


def _effective_response_max_tokens(args: argparse.Namespace) -> int | None:
    return (
        args.response_max_tokens
        or _tuning(args).max_output_tokens
        or provider_profile(_provider_name(args)).default_max_output_tokens
    )


def _effective_request_extras(
    args: argparse.Namespace, provider
) -> dict[str, object] | None:
    extras = dict(provider.request_extras or {})
    return extras or None


def _provider_name(args: argparse.Namespace) -> str:
    return args.provider_profile or _tuning(args).provider_profile or "generic"


def _api_key_name(args: argparse.Namespace) -> str:
    if args.api_key_env:
        return args.api_key_env
    return {
        "zhipu": "ZAI_API_KEY",
        "zhipu-glm53": "ZAI_API_KEY",
        "bailian": "DASHSCOPE_API_KEY",
        "qwen-bailian": "DASHSCOPE_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }.get(_provider_name(args), "OPENAI_API_KEY")


def _api_key(args: argparse.Namespace) -> str:
    name = _api_key_name(args)
    environment_value = os.environ.get(name)
    if environment_value is not None:
        return environment_value
    try:
        return load_properties(args.properties_file).get(name, "")
    except (OSError, UnicodeError, ValueError) as exc:
        raise EpubError(f"无法读取密钥配置 {args.properties_file}: {exc}") from exc


def _openrouter_client(
    config: OpenAIConfig,
    *,
    rate_limiter: RateLimiter | None = None,
):
    extras = dict(config.request_extras or {})
    unexpected = sorted(set(extras) - {"provider"})
    if unexpected:
        raise LLMError(
            "OpenRouter 模型参数必须由 model profile 声明，不能通过 request_extras 传入: "
            + ", ".join(unexpected)
        )
    provider_preferences = extras.get("provider")
    if provider_preferences is not None and not isinstance(
        provider_preferences, Mapping
    ):
        raise LLMError("OpenRouter provider preferences 必须是对象")
    return OpenRouterClient(
        OpenRouterConfig(
            base_url=config.base_url,
            model=config.model,
            api_key=config.api_key,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            max_output_tokens=config.max_output_tokens,
            provider_preferences=provider_preferences,
        ),
        rate_limiter=rate_limiter,
    )


_PROVIDER_CLIENT_FACTORIES: Mapping[
    str, Callable[..., OpenAICompatibleClient]
] = {"openrouter": _openrouter_client}


def _model_client(
    config: OpenAIConfig,
    provider_name: str,
    *,
    rate_limiter: RateLimiter | None = None,
):
    factory = _PROVIDER_CLIENT_FACTORIES.get(provider_name)
    if factory is not None:
        return factory(config, rate_limiter=rate_limiter)
    return OpenAICompatibleClient(config, rate_limiter=rate_limiter)


def _effective_temperature(args: argparse.Namespace) -> float:
    if args.temperature is not None:
        return args.temperature
    return _tuning(args).temperature if _tuning(args).temperature is not None else 0.1


def _effective_top_p(args: argparse.Namespace) -> float | None:
    if args.top_p is not None:
        return args.top_p
    return _tuning(args).top_p


def _tuning(args: argparse.Namespace) -> ModelTuning:
    tuning = tuning_profile(args.tuning_profile)
    fast_model = getattr(args, "fast_model", None)
    quality_model = args.quality_model or fast_model
    if tuning.fast_model and fast_model is not None and tuning.fast_model != fast_model:
        raise EpubError(f"调优预设 {tuning.name} 仅适用于 fast 模型 {tuning.fast_model}")
    if tuning.quality_model and tuning.quality_model != quality_model:
        raise EpubError(f"调优预设 {tuning.name} 仅适用于 quality 模型 {tuning.quality_model}")
    return tuning


def _validate_provider_generation_args(
    args: argparse.Namespace, provider_name: str
) -> None:
    if provider_name == "zhipu-glm53" and args.thinking == "off":
        raise EpubError(
            "GLM-5.3-Flash 不支持关闭思考；"
            "请移除 --thinking off 或使用 --thinking on/default"
        )
    if provider_name == "bailian" and args.reasoning_effort is not None:
        raise EpubError(
            "百炼 Chat Completion 不支持 --reasoning-effort；"
            "请关闭该参数，思考长度后续使用百炼 thinking_budget 适配"
        )
    if provider_name == "openrouter" and args.reasoning_effort is not None:
        raise EpubError(
            "OpenRouter reasoning 必须由对应 model profile 声明；"
            "不能通过通用 --reasoning-effort 注入"
        )
    if provider_name == "openrouter" and args.thinking != "default":
        raise EpubError("OpenRouter --thinking/reasoning 必须由对应 model profile 声明")
    if provider_name == "openrouter" and args.sampling != "default":
        raise EpubError("OpenRouter --sampling 参数必须由对应 model profile 声明")


def _book_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--book-title", help="覆盖 EPUB 书名 metadata")
    parser.add_argument(
        "--book-author",
        action="append",
        default=[],
        help="覆盖 EPUB 作者 metadata；可重复",
    )
    parser.add_argument(
        "--work-year",
        type=int,
        help="作品成书或首版年代；不自动使用 EPUB 电子版日期",
    )


def _local_gate_arguments(parser: argparse.ArgumentParser, *, default: str | None = "conservative") -> None:
    parser.add_argument(
        "--local-gate",
        choices=("off", "conservative", "balanced"),
        default=default,
        help=("模型调用前的本地难度门控；默认 conservative" if default else "reading-v1 未指定时为 off；legacy 默认为 conservative"),
    )
    parser.add_argument(
        "--short-dialogue-words",
        type=int,
        default=16,
        help="短简单对话的最大词数；0 表示不跳过此类段落",
    )
    parser.add_argument(
        "--short-prose-words",
        type=int,
        default=10,
        help="其他短简单单句的最大词数；0 表示不跳过此类段落",
    )


def _japanese_gate_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--japanese-short-text-gate",
        choices=("auto", "off", "safe-v1"),
        default="auto",
        help=(
            "日文固定短应答门控；safe-v1 仅跳过保守白名单，"
            "跳过段落不会生成译文或兜底内容（默认 auto：日文 safe-v1，其他语言 off）"
        ),
    )
    parser.add_argument(
        "--japanese-short-text-chars",
        type=int,
        default=30,
        help="日文短文本无条件跳过的最大 Unicode 字符数；0 表示关闭（默认 30）",
    )


def _effective_japanese_gate_mode(
    configuration: EffectiveLanguageConfiguration, requested: str, short_text_chars: int = 30,
) -> str:
    effective = (
        ("safe-v1" if configuration.source.source_code == "ja" else "off")
        if requested == "auto" else requested
    )
    try:
        configuration.run_identity(effective, short_text_chars)
    except LanguageConfigurationError as exc:
        raise EpubError(str(exc)) from exc
    return effective


def _syntax_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--syntax-analyzer",
        choices=("off", "spacy", "stanza"),
        default=None,
        help="本地句法分析；meaning-only 调优预设强制使用 Stanza",
    )
    parser.add_argument(
        "--spacy-model",
        default="en_core_web_sm",
        help="--syntax-analyzer spacy 使用的已安装 spaCy 模型",
    )
    parser.add_argument(
        "--stanza-package",
        help="Stanza 英文模型包；默认使用最高精度 default_accurate",
    )
    parser.add_argument(
        "--stanza-model-dir",
        help="Stanza 模型资源目录；默认使用 Stanza 自身配置",
    )
    parser.add_argument(
        "--stanza-hf-cache-dir",
        help="default_accurate 使用的本地 Hugging Face 缓存根目录（HF_HOME）",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            return _inspect(args)
        if args.command == "normalize":
            return _normalize(args)
        if args.command == "preview":
            return _preview(args)
        if args.command == "quality-replay":
            return _quality_replay(args)
        if args.command == "reading-eval":
            return _reading_eval(args)
        if args.command == "translate":
            return _translate(args)
        if args.command == "cache-clear":
            return _cache_clear(args)
        if args.command == "calibrate":
            return _calibrate(args)
        raise AssertionError(f"unknown command: {args.command}")
    except (EpubError, LLMError, OSError, SyntaxAnalyzerUnavailable, AssistanceError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


def _inspect(args: argparse.Namespace) -> int:
    language_configuration, _ = _language_configuration_for_book(args, args.input)
    japanese_gate_mode = _effective_japanese_gate_mode(
        language_configuration, args.japanese_short_text_gate, args.japanese_short_text_chars
    )
    _validate_local_gate_args(args)
    check = run_epubcheck(
        args.input,
        required=not args.skip_epubcheck,
        allow_invalid=args.allow_invalid_source,
    )
    if check.ran:
        print("EPUBCheck：已通过" if check.returncode == 0 else "EPUBCheck：源书存在已确认错误")
        _print_warnings("输入 EPUBCheck warning", check.warnings)
    else:
        print("EPUBCheck：已显式跳过")
    with EpubBook(args.input) as book:
        profile = ReadingProfile.load(args.profile) if args.profile else None
        hard_profile_texts = tuple(
            example.text
            for example in (profile.examples if profile else ())
            if example.label != "fluent"
        )
        refs = [
            ref
            for ref in book.paragraphs()
            if args.chapter is None or ref.chapter == args.chapter
        ]
        skipped_dialogue = 0
        skipped_prose = 0
        skipped_local_easy = 0
        skipped_japanese: list[tuple[int, int]] = []
        skipped_japanese_chars: list[tuple[int, int]] = []
        skipped_japanese_fixed: list[tuple[int, int]] = []
        displayed = []
        for ref in refs:
            if language_configuration.source.source_code == "ja":
                gate = japanese_short_text_gate(
                    ref.text,
                    mode=japanese_gate_mode,
                    short_text_chars=args.japanese_short_text_chars,
                    has_ruby=ref.has_ruby,
                )
            else:
                gate = local_model_gate(
                    ref.text, mode=args.local_gate,
                    dialogue_words=args.short_dialogue_words,
                    prose_words=args.short_prose_words,
                    hard_profile_texts=hard_profile_texts,
                )
            if not gate.needs_model and gate.reason == "short-dialogue":
                skipped_dialogue += 1
            elif not gate.needs_model and gate.reason == "short-prose":
                skipped_prose += 1
            elif not gate.needs_model:
                if language_configuration.source.source_code == "ja":
                    target = (ref.chapter, ref.paragraph)
                    skipped_japanese.append(target)
                    if gate.reason == "japanese-short-text-chars":
                        skipped_japanese_chars.append(target)
                    else:
                        skipped_japanese_fixed.append(target)
                else:
                    skipped_local_easy += 1
            displayed.append((ref, gate))
        for ref, gate in displayed[: max(args.limit, 0)]:
            preview = " ".join(ref.text.split())
            if len(preview) > 120:
                preview = preview[:117] + "..."
            status = (
                f"\t[本地简单：{gate.reason}，score={gate.score}，"
                f"threshold={gate.threshold}]"
                if not gate.needs_model
                else f"\t[需要模型：{gate.reason}，score={gate.score}]"
            )
            print(f"{ref.chapter}:{ref.paragraph}\t<{ref.tag}>\t{preview}{status}")
        print(f"共找到 {len(refs)} 个正文候选段落")
        print("有效语言配置：" + json.dumps(
            language_configuration.run_identity(
                japanese_gate_mode, args.japanese_short_text_chars
            ),
            ensure_ascii=False, sort_keys=True
        ))
        print(
            f"预计生成解析 {len(refs) - skipped_dialogue - skipped_prose - skipped_local_easy - len(skipped_japanese)}；"
            f"跳过短简单对话 {skipped_dialogue}；"
            f"跳过其他短简单单句 {skipped_prose}；"
            f"跳过其他本地简单段落 {skipped_local_easy}"
        )
        coordinates = ", ".join(f"{c}:{p}" for c, p in skipped_japanese) or "无"
        print(f"日文短文本跳过 {len(skipped_japanese)}（{coordinates}）；跳过即无译文兜底")
        fixed_coordinates = ", ".join(
            f"{c}:{p}" for c, p in skipped_japanese_fixed
        ) or "无"
        print(f"日文固定应答跳过 {len(skipped_japanese_fixed)}（{fixed_coordinates}）")
        char_coordinates = ", ".join(
            f"{c}:{p}" for c, p in skipped_japanese_chars
        ) or "无"
        print(
            f"日文字数保底阈值 {args.japanese_short_text_chars}；"
            f"跳过 {len(skipped_japanese_chars)}（{char_coordinates}）"
        )
    return 0


def _normalize(args: argparse.Namespace) -> int:
    result = normalize_epub(args.input, args.output, dry_run=args.dry_run)
    if not result.changes:
        print("没有匹配到可安全应用的规范化规则")
        return 0
    for change in result.changes:
        print(f"已应用 {change.rule}：{change.detail} [{change.path}]")
    if args.dry_run:
        print("dry-run：未创建输出文件")
    else:
        print(f"规范化 EPUB：{result.output}")
        print("EPUBCheck：已通过")
    return 0


def _preview(args: argparse.Namespace) -> int:
    if args.pipeline == "reading-v1":
        return _preview_reading(args)
    if not args.base_url or not args.fast_model:
        raise EpubError("legacy preview requires --base-url and --fast-model")
    language_configuration, tuning = _language_configuration_for_book(args, args.input)
    japanese_gate_mode = _effective_japanese_gate_mode(
        language_configuration, args.japanese_short_text_gate, args.japanese_short_text_chars
    )
    print("有效语言配置：" + json.dumps(
        language_configuration.run_identity(
            japanese_gate_mode, args.japanese_short_text_chars
        ),
        ensure_ascii=False, sort_keys=True
    ))
    if args.max_retries < 0:
        raise EpubError("--max-retries 不能小于 0")
    api_key = _api_key(args)
    provider_name = _provider_name(args)
    _validate_provider_generation_args(args, provider_name)
    provider = provider_profile(provider_name)
    common = dict(
        base_url=args.base_url,
        api_key=api_key,
        timeout_seconds=args.timeout,
        max_retries=args.max_retries,
        enable_thinking=_effective_thinking(args),
        thinking_parameter=provider.thinking_parameter,
        thinking_clear_thinking=provider.thinking_clear_thinking,
        temperature=_effective_temperature(args),
        top_p=_effective_top_p(args),
        do_sample=_effective_sampling(args),
        max_output_tokens=_effective_response_max_tokens(args),
        reasoning_effort=(args.reasoning_effort or tuning.reasoning_effort) if _effective_thinking(args) else None,
        response_format_type=provider.response_format_type,
        request_extras=_effective_request_extras(args, provider),
        stream_response=args.stream_response,
    )
    limiter = _rate_limiter(args.requests_per_minute)
    fast = _model_client(
        OpenAIConfig(model=args.fast_model, **common),
        provider_name,
        rate_limiter=limiter,
    )
    quality = _model_client(
        OpenAIConfig(model=args.quality_model or args.fast_model, **common),
        provider_name,
        rate_limiter=limiter,
    )
    profile = ReadingProfile.load(args.profile) if args.profile else None
    syntax_analyzer = create_syntax_analyzer(
        _effective_syntax_analyzer(args, tuning),
        model=args.spacy_model,
        stanza_package=_effective_stanza_package(args, tuning),
        stanza_model_dir=args.stanza_model_dir,
        stanza_hf_cache_dir=args.stanza_hf_cache_dir,
    )
    result = generate_preview(
        args.input,
        args.output,
        chapter=args.chapter,
        paragraph=args.paragraph,
        fast_model=fast,
        quality_model=quality,
        require_epubcheck=not args.skip_epubcheck,
        allow_invalid_source=args.allow_invalid_source,
        profile=profile,
        density=args.density,
        schema_retries=args.schema_retries,
        book_title=args.book_title,
        book_authors=tuple(args.book_author),
        work_year=args.work_year,
        language=language_profile(language_configuration.language_profile),
        source_language=language_configuration.source,
        syntax_analyzer=syntax_analyzer,
        quality_payload_mode=args.quality_payload_mode,
        trace_dir=args.trace_dir,
        analysis_link_text=args.analysis_link_text,
    )
    print(f"原文：{result.paragraph.text}")
    print(f"段译：{result.learning.paragraph_translation}")
    for card in result.learning.cards:
        print(f"\n[{card.difficulty}] {card.sentence}")
        print(f"句意：{card.meaning}")
    print(f"PREVIEW EPUB：{result.output}")
    _print_actual_usage(fast.usage, quality.usage)
    _print_warnings("输入 EPUBCheck warning", result.epubcheck_input_warnings)
    _print_warnings("输出 EPUBCheck warning", result.epubcheck_output_warnings)
    return 0


def _preview_reading(args: argparse.Namespace) -> int:
    """Offline renderer for one cache-frozen reading-v1 paragraph."""
    if bool(args.manifest) == bool(args.cache_profile_key):
        raise EpubError("reading-v1 preview requires exactly one of --manifest or --cache-profile-key")
    from translator.reading_assistance import ParagraphAssistance
    from translator.translator import reading_assistance_from_manifest
    language_configuration, _ = _language_configuration_for_book(args, args.input)
    japanese_gate_mode = _effective_japanese_gate_mode(
        language_configuration, args.japanese_short_text_gate, args.japanese_short_text_chars
    )
    print("有效语言配置：" + json.dumps(
        language_configuration.run_identity(
            japanese_gate_mode, args.japanese_short_text_chars
        ),
        ensure_ascii=False, sort_keys=True
    ))
    with EpubBook(args.input, reject_generated=True) as book:
        ref = book.paragraph(args.chapter, args.paragraph)
        book_key = LearningCache.book_key(args.input)
    if args.manifest:
        assistance = reading_assistance_from_manifest(
            args.manifest, source=args.input, ref=ref,
            language_identity=language_configuration.run_identity(
                japanese_gate_mode, args.japanese_short_text_chars
            ),
            source_language=language_configuration.source,
        )
    else:
        key = LearningCache.key(book_key, args.cache_profile_key, ref)
        with LearningCache(args.cache) as cache:
            payload = cache.get(key)
        if payload is None:
            raise EpubError("未找到该段的 reading-v1 冻结缓存；请使用匹配的 --cache 与 --cache-profile-key")
        try:
            assistance = ParagraphAssistance.from_dict(
                payload, ref.text, source_language=language_configuration.source,
            )
        except (KeyError, TypeError, ValueError, AssistanceError) as exc:
            raise EpubError(f"reading-v1 冻结缓存无效: {exc}") from exc
    result = generate_reading_preview(
        args.input, args.output, chapter=args.chapter, paragraph=args.paragraph,
        assistance=assistance, layout=args.reading_layout,
        inline_phrases=args.reading_inline_phrases,
        paragraph_aids=args.reading_paragraph_aids,
        require_epubcheck=not args.skip_epubcheck,
        allow_invalid_source=args.allow_invalid_source,
        analysis_link_text=args.analysis_link_text,
    )
    print(f"reading-v1 PREVIEW EPUB：{result.output}")
    _print_warnings("输入 EPUBCheck warning", result.epubcheck_input_warnings)
    _print_warnings("输出 EPUBCheck warning", result.epubcheck_output_warnings)
    return 0


def _reading_eval(args: argparse.Namespace) -> int:
    from translator.reading_eval import (
        FrozenResponseModel, ReadingBudgetModel, evaluate_reading, prepare_reading_input,
    )
    try:
        prepared = prepare_reading_input(json.loads(args.input.read_text(encoding="utf-8")))
        if args.repair_core and args.pipeline != "single":
            raise AssistanceError("--repair-core currently requires --pipeline single")
        if args.directed_review and args.pipeline == "both":
            raise AssistanceError("--directed-review requires one selected pipeline")
        if args.repair_core and args.directed_review:
            raise AssistanceError("choose either --repair-core or --directed-review")
        count = {"single": 1, "two-stage": 2, "both": 3}[args.pipeline]
        count += ((2 if args.pipeline == "single" else 1) if args.directed_review
                  else int(args.repair_core))
        if args.responses:
            responses = json.loads(args.responses.read_text(encoding="utf-8"))
            if not isinstance(responses, list) or len(responses) != count:
                raise AssistanceError(f"offline responses must contain exactly {count} responses")
            model = FrozenResponseModel(responses)
        else:
            if not args.base_url or not args.quality_model:
                raise AssistanceError("live experiment requires --base-url and --quality-model")
            if any(value is None or value <= 0 for value in (
                args.input_token_reserve, args.response_max_tokens, args.max_tokens
            )):
                raise AssistanceError("live experiment requires positive explicit input/output/total token limits")
            if count * (args.input_token_reserve + args.response_max_tokens) > args.max_tokens:
                raise AssistanceError("planned requests exceed total token reservation")
            model = ReadingBudgetModel(_quality_client(args), args.input_token_reserve,
                                       args.response_max_tokens, args.max_tokens)
        report = evaluate_reading(args.input, args.trace_dir, model, pipeline=args.pipeline,
                                  prepared=prepared, offline=bool(args.responses),
                                  repair_core=args.repair_core,
                                  directed_review=args.directed_review)
    except (ValueError, UnicodeError) as exc:
        raise AssistanceError(str(exc)) from exc
    print(f"READING trace：{report['trace_directory']}")
    for result in report["runs"]:
        print(f"{result['pipeline']}: {result['status']}")
    print(f"调用记录：{report['call_count']}；usage：{report['usage_status']}")
    return 2 if any(result["status"] == "failure" for result in report["runs"]) else 0


def _quality_client(args: argparse.Namespace):
    if args.max_retries < 0:
        raise EpubError("--max-retries 不能小于 0")
    tuning = _tuning(args)
    provider_name = _provider_name(args)
    _validate_provider_generation_args(args, provider_name)
    provider = provider_profile(provider_name)
    return _model_client(
        OpenAIConfig(
            base_url=args.base_url,
            api_key=_api_key(args),
            model=args.quality_model,
            timeout_seconds=args.timeout,
            max_retries=args.max_retries,
            enable_thinking=_effective_thinking(args),
            thinking_parameter=provider.thinking_parameter,
            thinking_clear_thinking=provider.thinking_clear_thinking,
            temperature=_effective_temperature(args),
            top_p=_effective_top_p(args),
            do_sample=_effective_sampling(args),
            max_output_tokens=_effective_response_max_tokens(args),
            reasoning_effort=(args.reasoning_effort or tuning.reasoning_effort)
            if _effective_thinking(args)
            else None,
            response_format_type=provider.response_format_type,
            request_extras=_effective_request_extras(args, provider),
            stream_response=args.stream_response,
        ),
        provider_name,
        rate_limiter=_rate_limiter(args.requests_per_minute),
    )


def _quality_replay(args: argparse.Namespace) -> int:
    # Replay has no EPUB metadata.  Its frozen, versioned language envelope is
    # the only authority; never infer Japanese from text characters.
    try:
        frozen = json.loads(args.input.read_text(encoding="utf-8"))
        frozen_language = frozen["language"]["key"]
        language = language_profile(frozen_language)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise EpubError(f"无法读取 quality replay 语言身份: {exc}") from exc
    if language.source_code == "ja" and args.tuning_profile == "generic":
        args.tuning_profile = "generic-ja"
    tuning = _tuning(args)
    if tuning.source_language != language.source_code or tuning.language_profile != language.key:
        raise EpubError("quality replay 冻结语言身份与调优配置不匹配")
    quality = _quality_client(args)
    trace_run = TraceRun.create(
        args.trace_dir,
        command="quality-replay",
        source=args.input,
        quality_payload_mode=args.quality_payload_mode,
    )
    result = replay_quality(
        args.input,
        quality,
        quality_payload_mode=args.quality_payload_mode,
        language=language,
        trace=trace_run.paragraph(0, 0),
    )
    print(f"冻结候选索引：{list(result.candidate_indices)}")
    for card in result.cards:
        print(f"\n[{card.difficulty}] {card.sentence}")
        print(f"句意：{card.meaning}")
    print(f"REPLAY trace：{trace_run.directory}")
    _print_actual_usage(Usage(), quality.usage)
    return 0


def _language_configuration_for_book(
    args: argparse.Namespace, source: Path,
) -> tuple[EffectiveLanguageConfiguration, ModelTuning]:
    """Resolve EPUB language once, before credentials or a transport client."""
    if not source.exists():  # preserves parser/unit seams; real commands always open EPUB.
        fallback = resolve_effective_language_configuration(())
        return fallback, tuning_profile("generic")
    with EpubBook(source, reject_generated=True) as book:
        metadata_languages = book.original_languages
    requested = tuning_profile(getattr(args, "tuning_profile", "generic"))
    # ``generic`` is the historical parser default.  It means automatic
    # selection, not an instruction to force English onto a Japanese EPUB.
    if requested.name == "generic" and metadata_languages:
        preliminary = resolve_effective_language_configuration(metadata_languages)
        if preliminary.source.source_code == "ja":
            requested = tuning_profile("generic-ja")
    try:
        resolved = resolve_effective_language_configuration(
            metadata_languages, tuning_name=requested.name,
            tuning_source_code=requested.source_language,
            language_profile_key=requested.language_profile,
        )
        if resolved.source.syntax_identity == "off-v1" and getattr(args, "syntax_analyzer", None) not in (None, "off"):
            raise EpubError("日文句法分析固定为 off；不能启用英文 Stanza 或 spaCy")
        return resolved, requested
    except LanguageConfigurationError as exc:
        raise EpubError(str(exc)) from exc


def _clients(
    args: argparse.Namespace,
    *,
    tuning: ModelTuning | None = None,
    language_configuration: EffectiveLanguageConfiguration | None = None,
):
    if args.max_retries < 0:
        raise EpubError("--max-retries 不能小于 0")
    api_key = _api_key(args)
    tuning = tuning or getattr(args, "_effective_tuning", None) or _tuning(args)
    syntax_analyzer_name = _effective_syntax_analyzer(args, tuning)
    stanza_package = _effective_stanza_package(args, tuning)
    provider_name = _provider_name(args)
    _validate_provider_generation_args(args, provider_name)
    provider = provider_profile(provider_name)
    configured_output_max = _effective_response_max_tokens(args)
    reading_execution = (
        getattr(args, "command", None) == "translate"
        and getattr(args, "pipeline", None) == "reading-v1"
    )
    length_retry_max = None
    request_output_max = configured_output_max
    if reading_execution:
        output_floor = args.reading_output_token_floor
        retry_floor = args.reading_length_retry_max_tokens
        if output_floor <= 0 or retry_floor <= 0:
            raise EpubError("reading-v1 输出 token 执行余量必须大于 0")
        request_output_max = max(configured_output_max or 0, output_floor)
        length_retry_max = max(request_output_max, retry_floor)
    common = dict(
        base_url=args.base_url,
        api_key=api_key,
        timeout_seconds=args.timeout,
        max_retries=args.max_retries,
        enable_thinking=_effective_thinking(args),
        thinking_parameter=provider.thinking_parameter,
        thinking_clear_thinking=provider.thinking_clear_thinking,
        temperature=_effective_temperature(args),
        top_p=_effective_top_p(args),
        do_sample=_effective_sampling(args),
        # Reading headroom is an execution-only recovery policy. It does not
        # change the identity of already validated cached content.
        max_output_tokens=request_output_max,
        length_retry_max_output_tokens=length_retry_max,
        reasoning_effort=(args.reasoning_effort or tuning.reasoning_effort) if _effective_thinking(args) else None,
        response_format_type=provider.response_format_type,
        request_extras=_effective_request_extras(args, provider),
        stream_response=args.stream_response,
    )
    limiter = _rate_limiter(args.requests_per_minute)
    fast = _model_client(
        OpenAIConfig(model=args.fast_model, **common),
        provider_name,
        rate_limiter=limiter,
    )
    quality_name = args.quality_model or args.fast_model
    quality = _model_client(
        OpenAIConfig(model=quality_name, **common),
        provider_name,
        rate_limiter=limiter,
    )
    reading_profile = ReadingProfile.load(args.profile) if args.profile else None
    language = language_profile(
        (language_configuration or getattr(args, "_effective_language_configuration", None)).language_profile
        if (language_configuration or getattr(args, "_effective_language_configuration", None))
        else tuning.language_profile
    )
    profile_data = json.dumps(
        {
            "prompt": "learning-v27-reader-guide-safe-projection",
            "quality_payload": QUALITY_PAYLOAD_VERSION,
            "quality_payload_mode": args.quality_payload_mode,
            "language": language.key,
            "effective_language": (
                (language_configuration or getattr(args, "_effective_language_configuration", None)).generation_identity
                if (language_configuration or getattr(args, "_effective_language_configuration", None)) else None
            ),
            "task_prompt": language.task_prompt_key,
            "prompt_variant": language.prompt_variant_key,
            "base_url": args.base_url.rstrip("/"),
            "fast_model": args.fast_model,
            "quality_model": quality_name,
            "provider_profile": provider.fingerprint,
            "tuning_profile": tuning.fingerprint,
            "thinking": _effective_thinking(args),
            "thinking_clear_thinking": provider.thinking_clear_thinking,
            "temperature": _effective_temperature(args),
            "top_p": _effective_top_p(args),
            "sampling": _effective_sampling(args),
            "response_max_tokens": _effective_response_max_tokens(args),
            "reasoning_effort": (
                (args.reasoning_effort or tuning.reasoning_effort)
                if _effective_thinking(args) else None
            ),
            "response_format_type": provider.response_format_type,
            "request_extras": _effective_request_extras(args, provider),
            "fast_request_policy": getattr(
                fast, "request_policy_fingerprint", None
            ),
            "quality_request_policy": getattr(
                quality, "request_policy_fingerprint", None
            ),
            "density": args.density,
            "reading_profile": (
                reading_profile.fingerprint_payload() if reading_profile else None
            ),
            # Execution policy does not change a validated cached result.
            # Retain the legacy zero value to preserve zero-retry cache keys.
            "schema_retries": 0,
            "syntax_analyzer": syntax_analyzer_name,
            "spacy_model": args.spacy_model if syntax_analyzer_name == "spacy" else None,
            "stanza_package": (
                stanza_package if syntax_analyzer_name == "stanza" else None
            ),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    profile_key = hashlib.sha256(profile_data.encode("utf-8")).hexdigest()
    return fast, quality, profile_key, reading_profile


def _rate_limiter(value: float | None) -> RateLimiter | None:
    if value is None:
        return None
    if value <= 0:
        raise EpubError("--requests-per-minute 必须大于 0")
    return RateLimiter(value)


def _effective_syntax_analyzer(
    args: argparse.Namespace, tuning: ModelTuning | None = None
) -> str:
    resolved_tuning = tuning or _tuning(args)
    required = resolved_tuning.required_syntax_analyzer
    explicit = args.syntax_analyzer
    if required is not None:
        if explicit is not None and explicit != required:
            raise EpubError(
                f"调优预设 {resolved_tuning.name} 必须使用 Stanza 从句解析，"
                f"不能使用 --syntax-analyzer {explicit}"
            )
        return required
    return explicit or "off"


def _effective_stanza_package(
    args: argparse.Namespace, tuning: ModelTuning | None = None
) -> str:
    resolved_tuning = tuning or _tuning(args)
    required = resolved_tuning.required_stanza_package
    explicit = args.stanza_package
    if required is not None:
        if explicit is not None and explicit != required:
            raise EpubError(
                f"调优预设 {resolved_tuning.name} 必须使用 Stanza {required}，"
                f"不能使用 --stanza-package {explicit}"
            )
        return required
    return explicit or "default_accurate"


def _syntax_profile_key(args: argparse.Namespace, tuning: ModelTuning) -> str:
    analyzer = _effective_syntax_analyzer(args, tuning)
    payload = json.dumps(
        {
            "adapter": "syntax-cache-v5-reader-guide-safe-projection",
            "analyzer": analyzer,
            "spacy_model": args.spacy_model if analyzer == "spacy" else None,
            "stanza_package": (
                _effective_stanza_package(args, tuning) if analyzer == "stanza" else None
            ),
            "stanza_runtime": (
                _stanza_runtime_identity(args) if analyzer == "stanza" else None
            ),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stanza_runtime_identity(args: argparse.Namespace) -> dict[str, str | None]:
    try:
        stanza_version = importlib.metadata.version("stanza")
    except importlib.metadata.PackageNotFoundError:
        stanza_version = None
    stanza_root = (
        Path(args.stanza_model_dir)
        if args.stanza_model_dir
        else Path.home() / "stanza_resources"
    )
    hf_root = (
        Path(args.stanza_hf_cache_dir)
        if args.stanza_hf_cache_dir
        else Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    )
    resources = stanza_root / "resources.json"
    revision = (
        hf_root
        / "hub"
        / STANZA_ACCURATE_TRANSFORMER_CACHE
        / "refs"
        / "main"
    )
    return {
        "stanza_version": stanza_version,
        "resources_sha256": (
            hashlib.sha256(resources.read_bytes()).hexdigest()
            if resources.is_file()
            else None
        ),
        "transformer_revision": (
            revision.read_text(encoding="utf-8").strip()
            if revision.is_file()
            else None
        ),
    }


def _translate(args: argparse.Namespace) -> int:
    if args.pipeline == "reading-v1":
        return _translate_reading(args)
    _validate_local_gate_args(args)
    if args.prior_tokens < 0:
        raise EpubError("--prior-tokens 不能小于 0")
    if args.prior_tokens and (
        args.max_tokens is None or args.prior_tokens >= args.max_tokens
    ):
        raise EpubError("--prior-tokens 必须小于已设置的 --max-tokens")
    if args.progress_interval <= 0:
        raise EpubError("进度间隔必须大于 0")
    language_configuration, tuning = _language_configuration_for_book(args, args.input)
    japanese_gate_mode = _effective_japanese_gate_mode(
        language_configuration, args.japanese_short_text_gate, args.japanese_short_text_chars
    )
    print("有效语言配置：" + json.dumps(
        language_configuration.run_identity(
            japanese_gate_mode, args.japanese_short_text_chars
        ),
        ensure_ascii=False, sort_keys=True
    ))
    args._effective_tuning = tuning
    args._effective_language_configuration = language_configuration
    fast, quality, profile_key, reading_profile = _clients(args)
    syntax_analyzer = create_syntax_analyzer(
        _effective_syntax_analyzer(args, tuning),
        model=args.spacy_model,
        stanza_package=_effective_stanza_package(args, tuning),
        stanza_model_dir=args.stanza_model_dir,
        stanza_hf_cache_dir=args.stanza_hf_cache_dir,
    )
    progress = _ConsoleProgress(
        mode=args.progress,
        interval=args.progress_interval,
        fast=fast,
        quality=quality,
        token_ceiling=args.max_tokens,
        prior_tokens=args.prior_tokens,
    )
    try:
        result = translate_book(
            args.input,
            args.output,
            fast_model=fast,
            quality_model=quality,
            profile_key=profile_key,
            cache_path=args.cache,
            syntax_profile_key=_syntax_profile_key(args, tuning),
            chapters=set(args.chapter) if args.chapter else None,
            max_workers=args.workers,
            require_epubcheck=not args.skip_epubcheck,
            allow_invalid_source=args.allow_invalid_source,
            input_price_per_million=args.input_price,
            output_price_per_million=args.output_price,
            requests_per_minute=args.requests_per_minute,
            max_tokens=args.max_tokens,
            max_cost=args.max_cost,
            dry_run=args.dry_run,
            profile=reading_profile,
            density=args.density,
            schema_retries=args.schema_retries,
            on_estimate=_print_estimate,
            on_progress=progress,
            local_gate=args.local_gate or "conservative",
            short_dialogue_words=args.short_dialogue_words,
            short_prose_words=args.short_prose_words,
            book_title=args.book_title,
            book_authors=tuple(args.book_author),
            work_year=args.work_year,
            language=language_profile(language_configuration.language_profile),
            syntax_analyzer=syntax_analyzer,
            quality_payload_mode=args.quality_payload_mode,
            trace_dir=args.trace_dir,
            analysis_link_text=args.analysis_link_text,
            source_language=language_configuration.source,
            japanese_short_text_gate_mode=japanese_gate_mode,
            japanese_short_text_chars=args.japanese_short_text_chars,
        )
    finally:
        progress.close()
    if isinstance(result, TranslationEstimate):
        print("dry-run：未调用模型，未创建输出文件")
    else:
        print(
            f"完成：{result.output}（段落 {result.paragraphs}，"
            f"缓存命中 {result.cached_paragraphs}，新生成 {result.generated_paragraphs}，"
            f"本地跳过 {result.estimate.skipped_short_dialogue + result.estimate.skipped_short_prose + result.estimate.skipped_local_easy + result.estimate.skipped_japanese_short_text}）"
        )
        _print_warnings("输出 EPUBCheck warning", result.epubcheck_output_warnings)
        _print_actual_usage(fast.usage, quality.usage)
    return 0


def _translate_reading(args: argparse.Namespace) -> int:
    if args.prior_tokens:
        raise EpubError("reading-v1 暂不支持 --prior-tokens；请使用新的独立授权预算")
    if args.max_cost is not None or args.input_price is not None or args.output_price is not None:
        raise EpubError("reading-v1 暂不支持费用上限；供应商费用字段必须先完成对账")
    if args.workers < 1:
        raise EpubError("--workers 必须大于 0")
    if args.reading_short_text_words < 0:
        raise EpubError("--reading-short-text-words 不能小于 0")
    language_configuration, tuning = _language_configuration_for_book(args, args.input)
    japanese_gate_mode = _effective_japanese_gate_mode(
        language_configuration, args.japanese_short_text_gate, args.japanese_short_text_chars
    )
    print("有效语言配置：" + json.dumps(
        language_configuration.run_identity(
            japanese_gate_mode, args.japanese_short_text_chars
        ),
        ensure_ascii=False, sort_keys=True
    ))
    args._effective_tuning = tuning
    args._effective_language_configuration = language_configuration
    fast, quality, legacy_profile_key, _ = _clients(args)
    progress = _ConsoleProgress(
        mode=args.progress, interval=args.progress_interval, fast=fast, quality=quality,
        token_ceiling=args.max_tokens,
    )
    from translator.reading_assistance import SCHEMA_VERSION, VALIDATION_VERSION
    from translator.reading_prompts import PROMPT_VERSION
    profile = {
        "pipeline": "reading-v1", "schema": SCHEMA_VERSION,
        "validation": VALIDATION_VERSION, "prompt": PROMPT_VERSION,
        "legacy_request_identity": legacy_profile_key,
        "effective_language": language_configuration.generation_identity,
        "single_call": args.reading_generation_pipeline == "single", "stanza": "off",
        "generation_pipeline": args.reading_generation_pipeline,
        "prior_context": args.reading_prior_context,
        "directed_review": args.reading_directed_review,
        "inline_phrases": args.reading_inline_phrases,
    }
    # Preserve the historical/default cache identity. The opt-in mode needs its
    # own identity because its generated result intentionally has extra aids.
    if args.reading_paragraph_aids:
        profile["paragraph_aids"] = True
    if args.reading_directed_review:
        profile["core_repair_policy"] = "completed-json-repair-v1"
    profile_key = hashlib.sha256(
        json.dumps(profile, sort_keys=True).encode("utf-8")
    ).hexdigest()
    try:
        result = translate_reading_book(
            args.input, args.output, model=quality, profile_key=profile_key,
            cache_path=args.cache, chapters=set(args.chapter) if args.chapter else None,
            max_workers=args.workers,
            require_epubcheck=not args.skip_epubcheck,
            allow_invalid_source=args.allow_invalid_source, max_tokens=args.max_tokens,
        dry_run=args.dry_run, trace_dir=args.trace_dir,
        local_gate=args.local_gate or "off", on_estimate=_print_estimate,
            on_progress=progress, frozen_manifest=args.frozen_manifest,
            prior_context=args.reading_prior_context,
            directed_review=args.reading_directed_review,
            inline_phrases=args.reading_inline_phrases,
            pipeline=args.reading_generation_pipeline,
            short_text_words=args.reading_short_text_words,
            short_sentence_words=args.reading_short_text_words,
            skip_paragraphs=set(args.reading_skip_paragraph),
            reprocess_paragraphs=set(args.reading_reprocess_paragraph),
            paragraph_aids=args.reading_paragraph_aids,
            source_language=language_configuration.source,
            language_identity=language_configuration.run_identity(
                japanese_gate_mode, args.japanese_short_text_chars
            ),
            analysis_link_text=args.analysis_link_text,
            japanese_short_text_gate_mode=japanese_gate_mode,
            japanese_short_text_chars=args.japanese_short_text_chars,
        )
    finally:
        progress.close()
    if isinstance(result, TranslationEstimate):
        print("dry-run：reading-v1 未调用模型，未创建输出文件")
    else:
        print(
            f"完成 reading-v1：{result.output}（段落 {result.paragraphs}，"
            f"缓存命中 {result.cached_paragraphs}，新生成 {result.generated_paragraphs}，"
            f"日文短文本跳过 {result.estimate.skipped_japanese_short_text}）"
        )
        _print_warnings("输出 EPUBCheck warning", result.epubcheck_output_warnings)
        if result.frozen_manifest:
            print(f"冻结结果清单：{result.frozen_manifest}")
        _print_actual_usage(Usage(), quality.usage)
    return 0


class _ConsoleProgress:
    """Rate-limited progress suitable for chapters and whole books."""

    _ALWAYS = {"cache", "retry", "render", "epubcheck", "complete"}

    def __init__(
        self,
        *,
        mode: str,
        interval: float,
        fast: OpenAICompatibleClient,
        quality: OpenAICompatibleClient,
        token_ceiling: int | None = None,
        prior_tokens: int = 0,
    ) -> None:
        self.mode = mode
        self.interval = interval
        self.fast = fast
        self.quality = quality
        self.token_ceiling = token_ceiling
        self.prior_tokens = prior_tokens
        self.started = time.monotonic()
        self.last_printed = self.started
        self.last_completed = -1
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.current = TranslationProgress("prepare", 0, 0, 0, 0)
        self.heartbeat: threading.Thread | None = None
        if self.mode != "off":
            self.heartbeat = threading.Thread(
                target=self._heartbeat,
                name="epub-translation-progress",
                daemon=True,
            )
            self.heartbeat.start()
            with self.lock:
                self._print(self.current, self.started)

    def __call__(self, event: TranslationProgress) -> None:
        with self.lock:
            self.current = event
            total_usage = (
                self.prior_tokens + self.fast.usage.total_tokens
                + self.quality.usage.total_tokens
            )
            if self.token_ceiling is not None and total_usage > self.token_ceiling:
                self.stop.set()
                raise EpubError(
                    f"实际 API token {total_usage} 超过运行上限 {self.token_ceiling}"
                )
            if self.mode == "off":
                return
            now = time.monotonic()
            percent_step = max(1, event.total_paragraphs // 100)
            completed_step = event.completed_paragraphs - self.last_completed
            due = now - self.last_printed >= self.interval
            should_print = (
                self.mode == "verbose"
                or event.phase in self._ALWAYS
                or due
                or (
                    event.phase == "paragraph"
                    and (
                        event.total_paragraphs <= 100
                        or completed_step >= percent_step
                    )
                )
            )
            if not should_print:
                return
            self._print(event, now)
            if event.phase == "complete":
                self.stop.set()

    def close(self) -> None:
        self.stop.set()
        heartbeat = self.heartbeat
        if heartbeat is not None and heartbeat is not threading.current_thread():
            heartbeat.join(timeout=1)

    def _heartbeat(self) -> None:
        while not self.stop.wait(self.interval):
            with self.lock:
                if self.current is not None:
                    self._print(self.current, time.monotonic())

    def _print(self, event: TranslationProgress, now: float) -> None:
        elapsed = int(now - self.started)
        percent = (
            100.0 * event.completed_paragraphs / event.total_paragraphs
            if event.total_paragraphs
            else 0.0
        )
        location = (
            f" | 当前 {event.chapter}:{event.paragraph}"
            if event.chapter is not None and event.paragraph is not None
            else ""
        )
        retry = f" | schema 重试 {event.retry}" if event.retry else ""
        fast_usage = self.fast.usage.total_tokens
        quality_usage = self.quality.usage.total_tokens
        total_usage = fast_usage + quality_usage
        budget_usage = (
            f" | 预算累计 {self.prior_tokens + total_usage}/{self.token_ceiling}"
            f"（历史 {self.prior_tokens}）" if self.prior_tokens else ""
        )
        scope = (
            f" | 所选正文 {event.source_paragraphs} | 本地跳过 {event.skipped_paragraphs}"
            if event.source_paragraphs
            else ""
        )
        print(
            f"进度 [{event.phase}] 模型段落 {event.completed_paragraphs}/{event.total_paragraphs} "
            f"({percent:.1f}%) | 缓存 {event.cached_paragraphs} | "
            f"新生成 {event.generated_paragraphs}{scope}{location}{retry} | "
            f"API tokens fast {fast_usage} / quality {quality_usage} / total {total_usage} "
            f"{budget_usage}| 已用时 {elapsed}s",
            flush=True,
        )
        self.last_printed = now
        self.last_completed = event.completed_paragraphs


def _print_estimate(estimate: TranslationEstimate) -> None:
    print(f"正文候选段落：{estimate.source_paragraphs}")
    print(f"模型段落：{estimate.paragraphs}")
    print(f"预计生成解析：{estimate.paragraphs}")
    coordinates = ", ".join(
        f"{chapter}:{paragraph}"
        for chapter, paragraph in estimate.japanese_short_text_coordinates
    ) or "无"
    print(
        f"日文短文本跳过：{estimate.skipped_japanese_short_text}"
        f"（{coordinates}）"
    )
    char_coordinates = ", ".join(
        f"{chapter}:{paragraph}"
        for chapter, paragraph in estimate.japanese_short_text_char_coordinates
    ) or "无"
    print(f"日文字数保底阈值：{estimate.japanese_short_text_chars}")
    print(
        f"日文字数保底跳过：{estimate.skipped_japanese_short_text_chars}"
        f"（{char_coordinates}）"
    )
    fixed_coordinates = ", ".join(
        f"{chapter}:{paragraph}"
        for chapter, paragraph in estimate.japanese_fixed_response_coordinates
    ) or "无"
    print(
        f"日文固定应答跳过：{estimate.skipped_japanese_fixed_responses}"
        f"（{fixed_coordinates}）"
    )
    if estimate.skipped_japanese_short_text:
        print("注意：跳过段落不会生成译文或兜底内容。")
    print(f"本地跳过短简单对话：{estimate.skipped_short_dialogue}")
    print(f"本地跳过其他短简单单句：{estimate.skipped_short_prose}")
    print(f"本地跳过其他简单段落：{estimate.skipped_local_easy}")
    print(f"预计请求：最多 {estimate.approximate_requests}")
    if estimate.syntax_requests:
        print(f"其中专用结构映射请求：最多 {estimate.syntax_requests}")
    print(
        f"预计 token：输入 {estimate.approximate_input_tokens}，"
        f"输出 {estimate.approximate_output_tokens}，"
        f"合计 {estimate.approximate_total_tokens}"
    )
    if estimate.approximate_cost is not None:
        print(f"预计费用：{estimate.approximate_cost:.4f}（单位取决于所填价格）")
    if estimate.approximate_seconds is not None:
        print(
            f"预计最低调用时间：{estimate.approximate_seconds / 60:.1f} 分钟"
            "（按请求速率上限；不含供应商排队和模型延迟）"
        )
    else:
        print("预计时间：未提供 --requests-per-minute，无法估算")
    _print_warnings("输入 EPUBCheck warning", estimate.epubcheck_input_warnings)


def _print_actual_usage(fast: Usage, quality: Usage) -> None:
    total = Usage(
        fast.prompt_tokens + quality.prompt_tokens,
        fast.completion_tokens + quality.completion_tokens,
        fast.total_tokens + quality.total_tokens,
        fast.cached_prompt_tokens + quality.cached_prompt_tokens,
        fast.cost_usd + quality.cost_usd,
        tuple(dict.fromkeys(fast.providers + quality.providers)),
    )
    print(
        "实际 API usage："
        f"输入 {total.prompt_tokens}，输出 {total.completion_tokens}，"
        f"合计 {total.total_tokens}"
    )
    print(
        "  阶段："
        f"fast（输入 {fast.prompt_tokens}，输出 {fast.completion_tokens}，合计 {fast.total_tokens}）；"
        f"quality（输入 {quality.prompt_tokens}，输出 {quality.completion_tokens}，合计 {quality.total_tokens}）"
    )
    if total.cached_prompt_tokens or total.cost_usd or total.providers:
        print(
            "  "
            f"缓存输入 {total.cached_prompt_tokens}；"
            f"供应商费用 ${total.cost_usd:.8f}；"
            f"实际 provider {', '.join(total.providers) or '未提供'}"
        )


def _print_warnings(label: str, warnings: tuple[str, ...]) -> None:
    for warning in warnings:
        print(f"{label}：{warning}", file=sys.stderr)


def _validate_local_gate_args(args: argparse.Namespace) -> None:
    if args.short_dialogue_words < 0 or args.short_prose_words < 0:
        raise EpubError("短句跳过词数不能为负数")


def _cache_clear(args: argparse.Namespace) -> int:
    if not args.input.is_file():
        raise EpubError(f"输入文件不存在: {args.input}")
    book_key = LearningCache.book_key(args.input)
    with LearningCache(args.cache) as cache:
        removed = cache.delete_book(book_key)
    print(f"已删除 {removed} 条缓存")
    return 0


def _calibrate(args: argparse.Namespace) -> int:
    run_epubcheck(
        args.input,
        required=not args.skip_epubcheck,
        allow_invalid=args.allow_invalid_source,
    )
    with EpubBook(args.input) as book:
        samples = calibration_sentences(book.paragraphs(), args.samples)
    print("请标注：f=顺畅，e=费力但能理解，b=无法可靠理解，s=跳过，q=结束")
    labels = {"f": "fluent", "e": "effortful", "b": "blocking"}
    base = ReadingProfile.load(args.base_profile) if args.base_profile else ReadingProfile()
    examples = list(base.examples)
    added = 0
    for index, sentence in enumerate(samples, 1):
        print(f"\n[{index}/{len(samples)}] {sentence.text}")
        while True:
            answer = input("标注 [f/e/b/s/q]: ").strip().lower()
            if answer in labels:
                examples.append(CalibrationExample(sentence.text, labels[answer]))
                added += 1
                break
            if answer == "s":
                break
            if answer == "q":
                profile = ReadingProfile(tuple(examples))
                if added == 0:
                    raise EpubError("没有完成任何校准样本，未保存画像")
                profile.save(args.profile, overwrite=args.overwrite)
                print(
                    f"已新增 {added} 条、共保存 {len(examples)} 条校准样本：{args.profile}"
                )
                return 0
            print("请输入 f、e、b、s 或 q")
    profile = ReadingProfile(tuple(examples))
    if added == 0:
        raise EpubError("没有完成任何校准样本，未保存画像")
    profile.save(args.profile, overwrite=args.overwrite)
    print(f"已新增 {added} 条、共保存 {len(examples)} 条校准样本：{args.profile}")
    return 0
