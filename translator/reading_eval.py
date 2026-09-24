"""Pure-text experiments with frozen evidence, no EPUB or production cache access."""

from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path
import time

from translator.reading_assistance import (
    AssistanceError, SCHEMA_VERSION, SPLITTER_VERSION, VALIDATION_VERSION, _aid, fingerprint,
)
from translator.reading_prompts import PROMPT_VERSION
from translator.languages import ENGLISH_SOURCE, SourceLanguage, split_source_sentences
from translator.trace import TraceRun


def prepare_reading_input(
    payload: object, *, source_language: SourceLanguage = ENGLISH_SOURCE,
) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("paragraph"), str):
        raise AssistanceError("input requires a paragraph string")
    paragraph = payload["paragraph"]
    sentences = [asdict(s) for s in split_source_sentences(source_language, paragraph)]
    if not sentences:
        raise AssistanceError("input paragraph is empty")
    if "sentences" in payload and fingerprint(payload["sentences"]) != fingerprint(sentences):
        raise AssistanceError("frozen sentence table differs from current splitter")
    syntax = payload.get("syntax", {})
    if not isinstance(syntax, dict):
        raise AssistanceError("syntax must be frozen JSON object")
    if any(key not in {str(s["index"]) for s in sentences} or not isinstance(value, dict)
           for key, value in syntax.items()):
        raise AssistanceError("frozen syntax must map current sentence indices to evidence objects")
    if source_language.syntax_identity == "off-v1":
        if syntax:
            raise AssistanceError("Japanese frozen syntax must be off")
        if payload.get("stanza_identity") != "off-v1":
            raise AssistanceError("Japanese frozen syntax requires off-v1 identity")
    if syntax and ("sentences" not in payload or not isinstance(payload.get("stanza_identity"), str)
                   or not payload["stanza_identity"].strip()):
        raise AssistanceError("frozen syntax requires sentence table and stanza_identity")
    mode = payload.get("mode", "selection")
    if mode not in ("selection", "generation"):
        raise AssistanceError("mode must be selection or generation")
    targets = payload.get("expected_aids", [])
    if not isinstance(targets, list):
        raise AssistanceError("expected_aids must be an array")
    table = {s["index"]: s["text"] for s in sentences}
    normalized_targets = []
    for target in targets:
        if not isinstance(target, dict) or set(target) - {"scope", "sentence_indices", "quote"}:
            raise AssistanceError("invalid expected aid target")
        aid = _aid({**target, "text": "location validation"}, table)
        normalized_targets.append({"scope": aid.scope,
                                   "sentence_indices": list(aid.sentence_indices),
                                   **({"quote": aid.quote} if aid.quote else {})})
    if mode == "generation" and not normalized_targets:
        raise AssistanceError("generation mode requires expected_aids locations")
    paragraph_aids = payload.get("paragraph_aids", True)
    if type(paragraph_aids) is not bool:
        raise AssistanceError("paragraph_aids must be boolean")
    if not paragraph_aids and any(target["scope"] == "paragraph" for target in normalized_targets):
        raise AssistanceError("paragraph aid target requires paragraph_aids")
    excluded = payload.get("excluded_aid_sentence_indices", [])
    sentence_indices = {sentence["index"] for sentence in sentences}
    if (
        not isinstance(excluded, list)
        or any(type(index) is not int or index not in sentence_indices for index in excluded)
        or len(set(excluded)) != len(excluded)
    ):
        raise AssistanceError("excluded aid sentence indices must be unique current sentence indices")
    prior = payload.get("prior_paragraph")
    if prior is not None and (not isinstance(prior, str) or not prior.strip() or len(prior) > 12000):
        raise AssistanceError("prior_paragraph must be a bounded nonempty string")
    result = {"paragraph": paragraph, "sentences": sentences, "syntax": syntax,
              "stanza_identity": payload.get("stanza_identity") if syntax else (
                  "off-v1" if source_language.syntax_identity == "off-v1" else "off"
              ),
              "mode": mode, "expected_aids": normalized_targets,
              "paragraph_aids": paragraph_aids,
              "excluded_aid_sentence_indices": sorted(excluded),
              "prior_paragraph": prior}
    # Reject NaN/non-JSON values before any model call.
    fingerprint(result)
    return result


class FrozenResponseModel:
    """Offline responses only; never constructs a transport or reads credentials."""

    def __init__(self, responses: list):
        self.responses = iter(responses)
        self.exchange = None

    def complete_json(self, system: str, user: str) -> dict:
        try:
            result = next(self.responses)
        except StopIteration as exc:
            raise AssistanceError("offline responses exhausted") from exc
        self.exchange = {"provider_responses": [{"offline_content": result}]}
        if isinstance(result, str):
            return json.loads(result)
        return result

    def consume_trace_exchange(self):
        exchange, self.exchange = self.exchange, None
        return exchange


class ReadingBudgetModel:
    """Serial request reservation, not a provider-enforced billing limit."""

    def __init__(self, model: object, input_reserve: int, output_limit: int, ceiling: int):
        self.model = model
        self.input_reserve = input_reserve
        self.output_limit = output_limit
        self.ceiling = ceiling
        self.spent = 0
        self.unknown_usage = False

    def __getattr__(self, name):
        return getattr(self.model, name)

    def complete_json(self, system: str, user: str) -> dict:
        reservation = self.input_reserve + self.output_limit
        if self.unknown_usage or self.spent + reservation > self.ceiling:
            raise AssistanceError("remaining token reservation unavailable")
        # Conservative byte-based preflight including message overhead; not a tokenizer.
        if len((system + user).encode("utf-8")) + 256 > self.input_reserve:
            raise AssistanceError("request exceeds input token reservation estimate")
        before = self.model.usage.total_tokens
        try:
            result = self.model.complete_json(system, user)
        finally:
            used = self.model.usage.total_tokens - before
            self.spent += used
            self.unknown_usage = used <= 0
        if self.spent > self.ceiling:
            raise AssistanceError("actual usage exceeded token ceiling; stopping")
        return result


def evaluate_reading(source: Path, trace_root: Path, model: object, *,
                     pipeline: str, prepared: dict, offline: bool,
                     repair_core: bool = False, directed_review: bool = False) -> dict:
    # Import here to keep generation orchestration in translator.py.
    from translator.translator import (
        generate_reading_assistance, needs_reading_review, review_reading_assistance,
    )

    if pipeline not in ("single", "two-stage", "both"):
        raise AssistanceError("invalid reading pipeline")
    run = TraceRun.create(trace_root, command="reading-eval", source=source,
                          quality_payload_mode=SCHEMA_VERSION,
                          metadata={"pipeline": pipeline, "offline": offline,
                                    "repair_core": repair_core,
                                    "directed_review": directed_review,
                                    "prompt_version": PROMPT_VERSION,
                                    "splitter_version": SPLITTER_VERSION,
                                    "validation_version": VALIDATION_VERSION})
    trace = run.paragraph(0, 0)
    trace.write("reading-input", prepared)
    report = {"trace_directory": str(run.directory), "offline": offline,
              "schema_version": SCHEMA_VERSION, "runs": [], "calls": []}
    candidates = ("single", "two-stage") if pipeline == "both" else (pipeline,)
    for candidate in candidates:
        started = time.monotonic()
        try:
            result = generate_reading_assistance(
                prepared, model, pipeline=candidate, trace=trace,
                repair_core=repair_core or directed_review,
            )
            if directed_review and needs_reading_review(result, prepared):
                result = review_reading_assistance(prepared, result, model, trace=trace)
            result_payload = result.to_dict()
            missing = [target for target in prepared["expected_aids"] if not any(
                aid.scope == target["scope"]
                and list(aid.sentence_indices) == target["sentence_indices"]
                and aid.quote == target.get("quote") for aid in result.aids)]
            report["runs"].append({"pipeline": candidate, "status": result.status,
                                   "result": result_payload, "missing_locations": missing,
                                   "semantic_review": "not_performed"})
        except Exception as exc:
            report["runs"].append({"pipeline": candidate, "status": "failure",
                                   "error": {"type": type(exc).__name__, "message": str(exc)}})
        report["runs"][-1]["elapsed_seconds"] = time.monotonic() - started
        # These records include actual provider responses, failed requests and usage,
        # already redacted by the existing trace infrastructure. Missing cost is unknown.
        report["calls"] = [json.loads(path.read_text(encoding="utf-8")) for path in
                           sorted(trace.directory.glob("reading-*-response.json"))]
        report["call_count"] = len(report["calls"])
        report["provider_response_count"] = sum(len(call["provider_responses"]) for call in report["calls"])
        report["usage_records"] = [usage for call in report["calls"] for usage in call["usage"]]
        report["usage_status"] = "offline" if offline else (
            "reported" if all(call["usage"] for call in report["calls"]) else "incomplete")
        for key in ("total_tokens", "cost"):
            values = [usage.get(key) for usage in report["usage_records"]]
            report[key] = sum(values) if (
                report["usage_status"] == "reported" and values
                and all(type(value) in (int, float) and math.isfinite(value) for value in values)
            ) else None
        trace.write("reading-report", report)
        if report["runs"][-1]["status"] == "failure":
            break  # Never silently retry a failed draft/review or start more paid work.
    # Return the same redacted payload that is persisted.
    return json.loads((trace.directory / "reading-report.json").read_text(encoding="utf-8"))
