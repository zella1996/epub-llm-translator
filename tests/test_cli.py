from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from tests.helpers import build_epub
import translator.cli as cli
from translator.cli import main
from translator.languages import GLM52_MEANING_ONLY_COMPACT_ENVELOPE
from translator.llm_api import Usage
from translator.translator import TranslationEstimate, TranslationProgress


def test_reading_eval_offline_never_loads_clients(tmp_path, monkeypatch, capsys):
    def unexpected(*args):
        pytest.fail("offline experiment must not read credentials or construct clients")
    monkeypatch.setattr(cli, "_api_key", unexpected)
    monkeypatch.setattr(cli, "_quality_client", unexpected)
    source, responses = tmp_path / "input.json", tmp_path / "responses.json"
    source.write_text(json.dumps({"paragraph": "He gave up."}))
    payload = {"sentences": [{"index": 1, "translation": "他放弃了。", "difficulty": "fluent"}], "aids": []}
    responses.write_text(json.dumps([payload, payload, payload]))
    assert main(["reading-eval", str(source), "--responses", str(responses),
                 "--trace-dir", str(tmp_path / "traces")]) == 0
    assert "调用记录：3" in capsys.readouterr().out
    assert not list(tmp_path.rglob("*.epub"))
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_reading_eval_offline_core_repair_is_explicit_and_bounded(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_quality_client", lambda args: pytest.fail("offline repair must not construct client"))
    source, responses = tmp_path / "input.json", tmp_path / "responses.json"
    source.write_text(json.dumps({"paragraph": "He left it unopened."}))
    invalid = {"sentences": [{"index": 1, "translation": "他 unopened。", "difficulty": "effortful"}], "aids": []}
    valid = {"sentences": [{"index": 1, "translation": "他把它留着没有打开。", "difficulty": "effortful"}], "aids": []}
    responses.write_text(json.dumps([invalid, valid]))

    assert main([
        "reading-eval", str(source), "--pipeline", "single", "--repair-core",
        "--responses", str(responses), "--trace-dir", str(tmp_path / "traces"),
    ]) == 0
    assert "调用记录：2" in capsys.readouterr().out


def test_reading_eval_offline_directed_review_repairs_partial_aids(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_quality_client", lambda args: pytest.fail("offline review must not construct client"))
    source, responses = tmp_path / "input.json", tmp_path / "responses.json"
    source.write_text(json.dumps({
        "paragraph": "Unless she called, he would not leave.",
        "mode": "generation",
        "expected_aids": [{"scope": "sentence", "sentence_indices": [1]}],
    }))
    partial = {
        "sentences": [{"index": 1, "translation": "除非她来电，否则他不会离开。", "difficulty": "effortful"}],
        "aids": [{"scope": "sentence", "sentence_indices": [1], "text": "条件范围。",
                  "show_translation": "true"}],
    }
    final = {
        "sentences": partial["sentences"],
        "aids": [{"scope": "sentence", "sentence_indices": [1], "text": "Unless 引出否定条件。"}],
    }
    # Three responses are the declared maximum; only two should be consumed here.
    responses.write_text(json.dumps([partial, final, final]))

    assert main([
        "reading-eval", str(source), "--pipeline", "single", "--directed-review",
        "--responses", str(responses), "--trace-dir", str(tmp_path / "traces"),
    ]) == 0
    assert "调用记录：2" in capsys.readouterr().out


@pytest.mark.parametrize("extra", [[], ["--input-token-reserve", "10000", "--response-max-tokens", "1000", "--max-tokens", "1"]])
def test_reading_eval_live_requires_budget_before_client(tmp_path, monkeypatch, extra):
    monkeypatch.setattr(cli, "_quality_client", lambda args: pytest.fail("no client before preflight"))
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"paragraph": "He left."}))
    assert main(["reading-eval", str(source), "--execute", "--trace-dir", str(tmp_path / "traces"),
                 "--base-url", "https://example.invalid/v1", "--quality-model", "fake", *extra]) == 2


def test_reading_eval_frozen_sentence_mismatch_stops_before_client(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_quality_client", lambda args: pytest.fail("no client before validation"))
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"paragraph": "He left.", "sentences": [],
                                 "syntax": {"1": {}}, "stanza_identity": "frozen"}))
    assert main(["reading-eval", str(source), "--execute", "--trace-dir", str(tmp_path / "traces")]) == 2


def test_reading_budget_stops_without_repeating_model_calls():
    from types import SimpleNamespace
    from translator.reading_assistance import AssistanceError
    from translator.reading_eval import ReadingBudgetModel
    model = SimpleNamespace(usage=Usage())
    calls = []
    def complete(system, user):
        calls.append(user)
        model.usage = Usage(total_tokens=900)
        return {}
    model.complete_json = complete
    wrapper = ReadingBudgetModel(model, input_reserve=500, output_limit=500, ceiling=1500)
    wrapper.complete_json("s", "u")
    with pytest.raises(AssistanceError, match="reservation"):
        wrapper.complete_json("s", "u")
    assert calls == ["u"]


def test_reading_budget_rejects_oversized_input_before_request():
    from types import SimpleNamespace
    from translator.reading_assistance import AssistanceError
    from translator.reading_eval import ReadingBudgetModel
    model = SimpleNamespace(complete_json=lambda *args: pytest.fail("must not call model"))
    with pytest.raises(AssistanceError, match="input token"):
        ReadingBudgetModel(model, 300, 100, 400).complete_json("s", "中" * 100)


@pytest.mark.parametrize("reported", [0, 1501])
def test_reading_budget_unknown_or_over_limit_stops(reported):
    from types import SimpleNamespace
    from translator.reading_assistance import AssistanceError
    from translator.reading_eval import ReadingBudgetModel
    calls = []
    model = SimpleNamespace(usage=Usage())
    def complete(system, user):
        calls.append(user)
        model.usage = Usage(total_tokens=reported)
        return {}
    model.complete_json = complete
    wrapper = ReadingBudgetModel(model, 500, 500, 1500)
    if reported:
        with pytest.raises(AssistanceError, match="actual usage"):
            wrapper.complete_json("s", "u")
    else:
        wrapper.complete_json("s", "u")
    with pytest.raises(AssistanceError, match="reservation"):
        wrapper.complete_json("s", "u")
    assert len(calls) == 1


def test_translate_routes_reading_v1_only_when_explicit(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_translate_reading", lambda args: called.append(args.pipeline) or 0)
    argv = ["translate", "in.epub", "out.epub", "--base-url", "https://example.invalid/v1", "--fast-model", "test", "--pipeline", "reading-v1"]
    assert main(argv) == 0
    assert called == ["reading-v1"]


def test_analysis_link_text_option_is_available_for_preview_and_translate():
    preview = cli.build_parser().parse_args([
        "preview", "in.epub", "out-PREVIEW.epub", "--chapter", "1", "--paragraph", "1",
        "--analysis-link-text", "〔译〕",
    ])
    translate = cli.build_parser().parse_args([
        "translate", "in.epub", "out.epub", "--base-url", "https://example.invalid/v1",
        "--fast-model", "fake", "--analysis-link-text", "〔译〕",
    ])
    assert preview.analysis_link_text == translate.analysis_link_text == "〔译〕"


def test_reading_v1_dry_run_reports_its_conservative_token_bound(monkeypatch, capsys):
    estimate = TranslationEstimate(
        source_paragraphs=8, paragraphs=8, skipped_short_dialogue=0,
        skipped_short_prose=0, skipped_local_easy=0, approximate_requests=8,
        approximate_input_tokens=1200, approximate_output_tokens=800,
        approximate_cost=None, approximate_seconds=None,
    )
    client = SimpleNamespace(usage=Usage())
    monkeypatch.setattr(cli, "_clients", lambda args: (client, client, "legacy-profile", None))
    def fake_translate(*args, **kwargs):
        kwargs["on_estimate"](estimate)
        return estimate
    monkeypatch.setattr(cli, "translate_reading_book", fake_translate)

    assert main([
        "translate", "source.epub", "out.epub", "--pipeline", "reading-v1",
        "--base-url", "https://example.invalid/v1", "--fast-model", "fake", "--dry-run",
    ]) == 0

    output = capsys.readouterr().out
    assert "预计请求：最多 8" in output
    assert "预计 token：输入 1200，输出 800，合计 2000" in output
    assert "dry-run：reading-v1 未调用模型，未创建输出文件" in output


def test_reading_v1_passes_workers_to_translation(monkeypatch):
    estimate = TranslationEstimate(
        source_paragraphs=2, paragraphs=2, skipped_short_dialogue=0,
        skipped_short_prose=0, skipped_local_easy=0, approximate_requests=2,
        approximate_input_tokens=200, approximate_output_tokens=100,
        approximate_cost=None, approximate_seconds=None,
    )
    client = SimpleNamespace(usage=Usage())
    captured = {}
    monkeypatch.setattr(cli, "_clients", lambda args: (client, client, "legacy-profile", None))

    def fake_translate(*args, **kwargs):
        captured.update(kwargs)
        return estimate

    monkeypatch.setattr(cli, "translate_reading_book", fake_translate)
    assert main([
        "translate", "source.epub", "out.epub", "--pipeline", "reading-v1",
        "--base-url", "https://example.invalid/v1", "--fast-model", "fake",
        "--workers", "3", "--reading-skip-paragraph", "4:10",
        "--reading-reprocess-paragraph", "4:11", "--dry-run",
    ]) == 0
    assert captured["max_workers"] == 3
    assert captured["short_text_words"] == 15
    assert captured["short_sentence_words"] == 15
    assert captured["skip_paragraphs"] == {(4, 10)}
    assert captured["reprocess_paragraphs"] == {(4, 11)}
    assert captured["paragraph_aids"] is False


def test_reading_short_text_threshold_is_execution_only_for_cache_identity(monkeypatch):
    estimate = TranslationEstimate(
        source_paragraphs=2, paragraphs=1, skipped_short_dialogue=0,
        skipped_short_prose=1, skipped_local_easy=0, approximate_requests=1,
        approximate_input_tokens=100, approximate_output_tokens=50,
        approximate_cost=None, approximate_seconds=None,
    )
    client = SimpleNamespace(usage=Usage())
    calls = []
    monkeypatch.setattr(
        cli, "_clients", lambda args: (client, client, "legacy-profile", None)
    )

    def fake_translate(*args, **kwargs):
        calls.append(kwargs)
        return estimate

    monkeypatch.setattr(cli, "translate_reading_book", fake_translate)
    common = [
        "translate", "source.epub", "out.epub", "--pipeline", "reading-v1",
        "--base-url", "https://example.invalid/v1", "--fast-model", "fake",
        "--dry-run",
    ]
    assert main(common) == 0
    assert main(common + ["--reading-short-text-words", "0"]) == 0
    assert main(common + ["--reading-paragraph-aids"]) == 0
    assert main(common + ["--reading-reprocess-paragraph", "1:1"]) == 0
    assert main(common + ["--analysis-link-text", "〔译〕"]) == 0

    assert calls[0]["profile_key"] == calls[1]["profile_key"]
    assert calls[2]["profile_key"] != calls[0]["profile_key"]
    assert calls[3]["profile_key"] == calls[0]["profile_key"]
    assert calls[4]["profile_key"] == calls[0]["profile_key"]
    assert [call["short_text_words"] for call in calls] == [15, 0, 15, 15, 15]
    assert [call["short_sentence_words"] for call in calls] == [15, 0, 15, 15, 15]
    assert [call["paragraph_aids"] for call in calls] == [False, False, True, False, False]
    assert calls[3]["reprocess_paragraphs"] == {(1, 1)}
    assert [call["analysis_link_text"] for call in calls] == ["›››"] * 4 + ["〔译〕"]


def test_completed_cache_survives_schema_retry_policy_change(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from translator.cache import LearningCache

    monkeypatch.setattr(cli, "_api_key", lambda args: "offline-test-key")
    common = [
        "translate", "unused-source.epub", "unused-output.epub",
        "--base-url", "https://open.bigmodel.cn/api/paas/v4",
        "--fast-model", "glm-5.3-flash", "--quality-model", "glm-5.3-flash",
        "--tuning-profile", "glm53flash-literal-baseline", "--chapter", "32",
    ]
    original_args = cli.build_parser().parse_args(common + ["--schema-retries", "0"])
    resumed_args = cli.build_parser().parse_args(common + ["--schema-retries", "1"])
    original_fast, original_quality, original_profile, _ = cli._clients(original_args)
    resumed_fast, resumed_quality, resumed_profile, _ = cli._clients(resumed_args)
    assert original_fast.trace_request_payload("system", "user") == resumed_fast.trace_request_payload("system", "user")
    assert original_quality.trace_request_payload("system", "user") == resumed_quality.trace_request_payload("system", "user")
    ref = SimpleNamespace(href="chapter.xhtml", paragraph=1, text="An offline cache test.")
    completed = {"completed": True}
    with LearningCache(tmp_path / "cache.sqlite3") as cache:
        cache.put(LearningCache.key("same-book", original_profile, ref), "same-book", original_profile, completed)
        assert cache.get(LearningCache.key("same-book", resumed_profile, ref)) == completed
    assert resumed_args.schema_retries == 1


def test_streaming_transport_preserves_completed_cache_identity(monkeypatch):
    monkeypatch.setattr(cli, "_api_key", lambda args: "offline-test-key")
    common = [
        "translate", "unused-source.epub", "unused-output.epub",
        "--base-url", "https://open.bigmodel.cn/api/paas/v4",
        "--fast-model", "glm-5.3-flash", "--quality-model", "glm-5.3-flash",
        "--tuning-profile", "glm53flash-japanese-reading", "--chapter", "5",
    ]
    regular = cli.build_parser().parse_args(common)
    streaming = cli.build_parser().parse_args(common + ["--stream-response"])

    regular_fast, _, regular_profile, _ = cli._clients(regular)
    streaming_fast, _, streaming_profile, _ = cli._clients(streaming)

    assert "stream" not in regular_fast.trace_request_payload("system", "user")
    assert streaming_fast.trace_request_payload("system", "user")["stream"] is True
    assert streaming_profile == regular_profile


def test_cache_identity_still_tracks_reasoning(monkeypatch):
    monkeypatch.setattr(cli, "_api_key", lambda args: "offline-test-key")
    common = ["translate", "in.epub", "out.epub", "--base-url", "https://open.bigmodel.cn/api/paas/v4", "--fast-model", "glm-5.3-flash",
              "--tuning-profile", "glm53flash-literal-baseline"]
    high = cli.build_parser().parse_args(common + ["--reasoning-effort", "high"])
    maximum = cli.build_parser().parse_args(common + ["--reasoning-effort", "max"])
    assert cli._clients(high)[2] != cli._clients(maximum)[2]


def test_resume_budget_counts_prior_usage_without_mutating_provider_usage():
    from types import SimpleNamespace
    fast = SimpleNamespace(usage=Usage(0, 0, 60))
    quality = SimpleNamespace(usage=Usage(0, 0, 10))
    progress = cli._ConsoleProgress(
        mode="off", interval=10, fast=fast, quality=quality,
        token_ceiling=100, prior_tokens=40,
    )
    try:
        with pytest.raises(cli.EpubError, match="实际 API token 110 超过运行上限 100"):
            progress(TranslationProgress("fast", 0, 2, 0, 0))
        assert fast.usage.total_tokens == 60
        assert quality.usage.total_tokens == 10
    finally:
        progress.close()


@pytest.mark.parametrize("prior, ceiling", [(-1, 100), (100, 100), (101, 100), (1, None)])
def test_invalid_prior_budget_is_rejected_before_clients(monkeypatch, prior, ceiling):
    def unexpected_clients(args):
        pytest.fail("invalid budget must fail before client creation")
    monkeypatch.setattr(cli, "_clients", unexpected_clients)
    argv = ["translate", "in.epub", "out.epub", "--base-url", "https://example.invalid/v1", "--fast-model", "test",
            "--prior-tokens", str(prior)]
    if ceiling is not None:
        argv += ["--max-tokens", str(ceiling)]
    assert main(argv) != 0


def test_translate_forwards_resume_budget_and_retry_policy(monkeypatch, capsys):
    from types import SimpleNamespace
    fast = SimpleNamespace(usage=Usage(0, 0, 60))
    quality = SimpleNamespace(usage=Usage(0, 0, 10))
    monkeypatch.setattr(cli, "_clients", lambda args: (fast, quality, "test-profile", None))
    monkeypatch.setattr(cli, "create_syntax_analyzer", lambda *a, **kw: None)
    def fake_translate(*args, **kwargs):
        assert kwargs["max_tokens"] == 100
        assert kwargs["schema_retries"] == 1
        kwargs["on_progress"](TranslationProgress("fast", 0, 2, 0, 0))
        pytest.fail("prior usage must stop the run")
    monkeypatch.setattr(cli, "translate_book", fake_translate)
    assert main([
        "translate", "in.epub", "out.epub", "--base-url", "https://example.invalid/v1",
        "--fast-model", "test", "--max-tokens", "100", "--prior-tokens", "40",
        "--schema-retries", "1", "--progress", "off",
    ]) != 0
    assert "实际 API token 110 超过运行上限 100" in capsys.readouterr().err


def test_reading_output_headroom_is_execution_only_and_preserves_profile_key():
    base = [
        "translate", "in.epub", "out.epub", "--pipeline", "reading-v1",
        "--base-url", "https://example.invalid/v1", "--fast-model", "fake",
        "--response-max-tokens", "12288",
    ]
    original = cli.build_parser().parse_args(base)
    raised = cli.build_parser().parse_args(base + [
        "--reading-output-token-floor", "21000",
        "--reading-length-retry-max-tokens", "31000",
    ])

    original_fast, _, original_key, _ = cli._clients(original)
    raised_fast, _, raised_key, _ = cli._clients(raised)

    assert original_fast.config.max_output_tokens == 20000
    assert original_fast.config.length_retry_max_output_tokens == 30000
    assert raised_fast.config.max_output_tokens == 21000
    assert raised_fast.config.length_retry_max_output_tokens == 31000
    assert raised_key == original_key


def test_inspect_lists_paragraphs_without_epubcheck(tmp_path, capsys):
    source = build_epub(tmp_path / "source.epub")
    assert main(["inspect", str(source), "--skip-epubcheck"]) == 0
    output = capsys.readouterr().out
    assert "EPUBCheck：已显式跳过" in output
    assert "1:1" in output
    assert "共找到 2 个正文候选段落" in output
    assert "预计生成解析 1" in output
    assert "跳过其他短简单单句 1" in output


def test_normalize_cli_is_offline_and_reports_dry_run(tmp_path, capsys, monkeypatch):
    source = build_epub(tmp_path / "source.epub")
    output = tmp_path / "normalized.epub"

    class _Result:
        changes = ()

    calls = []

    def fake_normalize(input_path, output_path, *, dry_run):
        calls.append((input_path, output_path, dry_run))
        return _Result()

    monkeypatch.setattr(cli, "normalize_epub", fake_normalize)
    assert main(["normalize", str(source), str(output), "--dry-run"]) == 0

    assert calls == [(source, output, True)]
    assert "没有匹配到可安全应用的规范化规则" in capsys.readouterr().out


def test_quality_replay_cli_only_calls_quality_and_writes_a_new_trace(tmp_path, capsys, monkeypatch):
    text = "This is the original paragraph. Although it is long, its meaning remains testable."
    context = {"title": None, "authors": [], "work_year": None}
    digest = lambda payload: hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    replay_input = tmp_path / "quality-replay-input.json"
    replay_input.write_text(
        json.dumps(
            {
                "version": 1,
                "language": {
                    "key": GLM52_MEANING_ONLY_COMPACT_ENVELOPE.key,
                    "card_format": "sentence-meaning-v1",
                    "quality_system_sha256": hashlib.sha256(
                        GLM52_MEANING_ONLY_COMPACT_ENVELOPE.quality_system.encode()
                    ).hexdigest(),
                },
                "paragraph": text,
                "sentences": [
                    {"index": 1, "text": "This is the original paragraph."},
                    {"index": 2, "text": "Although it is long, its meaning remains testable."},
                ],
                "assessments": [
                    {"index": 1, "text": "This is the original paragraph.", "difficulty": "fluent", "reason": "", "confidence": 0.5},
                    {"index": 2, "text": "Although it is long, its meaning remains testable.", "difficulty": "effortful", "reason": "", "confidence": 0.5},
                ],
                "candidate_indices": [2],
                "stanza": {"version": 3, "results": []},
                "book_context": context,
                "book_context_sha256": digest(context),
                "profile": None,
                "profile_sha256": digest(None),
                "density": "medium",
            },
            ensure_ascii=False,
        )
    )

    clients = []

    class _QualityOnlyClient:
        usage = Usage()

        def __init__(self, config, **_):
            self.config = config
            self.calls = []
            clients.append(self)

        def complete_json(self, system, user):
            self.calls.append((system, user))
            return {"sentences": [{"index": 2, "difficulty": "blocking", "meaning": "冻结输入的结果。"}]}

    monkeypatch.setattr(cli, "OpenAICompatibleClient", _QualityOnlyClient)
    trace_root = tmp_path / "replay-traces"

    assert main(
        [
            "quality-replay",
            str(replay_input),
            "--base-url", "https://offline.example/v1",
            "--quality-model", "glm-5.2",
            "--tuning-profile", "glm52-grounded",
            "--trace-dir", str(trace_root),
        ]
    ) == 0

    assert len(clients) == 1
    assert len(clients[0].calls) == 1
    assert "冻结输入的结果。" in capsys.readouterr().out
    run_dir, = trace_root.iterdir()
    assert (run_dir / "chapter-000" / "paragraph-000" / "quality-compact-v5-attempt-01-request.json").is_file()


def test_console_progress_prints_counts_location_usage_and_flushes(capsys):
    class _Client:
        def __init__(self, usage):
            self.usage = usage

    progress = cli._ConsoleProgress(
        mode="verbose",
        interval=10,
        fast=_Client(Usage(100, 20, 120)),
        quality=_Client(Usage(50, 10, 60)),
    )
    progress(
        TranslationProgress(
            phase="quality",
            completed_paragraphs=3,
            total_paragraphs=42,
            cached_paragraphs=1,
            generated_paragraphs=2,
            chapter=32,
            paragraph=4,
        )
    )

    output = capsys.readouterr().out
    assert "进度 [quality] 模型段落 3/42 (7.1%)" in output
    assert "缓存 1 | 新生成 2" in output
    assert "当前 32:4" in output
    assert "API tokens fast 120 / quality 60 / total 180" in output
    progress.close()


def test_console_progress_heartbeats_during_a_long_stage(capsys):
    class _Client:
        usage = Usage()

    progress = cli._ConsoleProgress(
        mode="auto",
        interval=0.01,
        fast=_Client(),
        quality=_Client(),
    )
    progress(
        TranslationProgress(
            phase="fast",
            completed_paragraphs=0,
            total_paragraphs=200,
            cached_paragraphs=0,
            generated_paragraphs=0,
            chapter=1,
            paragraph=1,
        )
    )
    time.sleep(0.03)
    progress.close()

    assert "进度 [fast] 模型段落 0/200" in capsys.readouterr().out


def test_console_progress_enforces_actual_token_ceiling():
    class _Client:
        def __init__(self, total):
            self.usage = Usage(0, 0, total)

    progress = cli._ConsoleProgress(
        mode="auto",
        interval=10,
        fast=_Client(90),
        quality=_Client(20),
        token_ceiling=100,
    )
    with pytest.raises(cli.EpubError, match="实际 API token 110 超过运行上限 100"):
        progress(
            TranslationProgress(
                phase="paragraph",
                completed_paragraphs=1,
                total_paragraphs=2,
                cached_paragraphs=0,
                generated_paragraphs=1,
            )
        )
    progress.close()


def test_progress_off_is_silent_but_keeps_actual_token_ceiling(capsys):
    class _Client:
        def __init__(self, total):
            self.usage = Usage(0, 0, total)

    progress = cli._ConsoleProgress(
        mode="off",
        interval=10,
        fast=_Client(60),
        quality=_Client(50),
        token_ceiling=100,
    )
    with pytest.raises(cli.EpubError, match="实际 API token 110 超过运行上限 100"):
        progress(
            TranslationProgress(
                phase="paragraph",
                completed_paragraphs=1,
                total_paragraphs=2,
                cached_paragraphs=0,
                generated_paragraphs=1,
            )
        )
    progress.close()
    assert capsys.readouterr().out == ""
