"""Measure an OpenAI-compatible model selecting fixed clause-link candidates."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from translator.llm_api import OpenAICompatibleClient, OpenAIConfig
from translator.sentence_analyzer import Sentence, SpacySyntaxAnalyzer


_RELATION_TYPES = frozenset({"acl", "relcl", "acl:relcl"})


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--corpus",
        type=Path,
        required=True,
        help="Path to a local benchmark corpus JSON file",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--model", default="google/gemma-4-e2b")
    parser.add_argument("--spacy-model", default="en_core_web_sm")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--strict-omit", action="store_true")
    return parser.parse_args()


def _candidate_payload(cases: list[dict[str, object]], analyzer: SpacySyntaxAnalyzer) -> tuple[list[dict[str, object]], dict[int, dict[str, tuple[str, str]]]]:
    sentences = [Sentence(index, str(case["sentence"])) for index, case in enumerate(cases, start=1)]
    results = analyzer.analyze(sentences)
    payload: list[dict[str, object]] = []
    hint_maps: dict[int, dict[str, tuple[str, str]]] = {}
    for index, case in enumerate(cases, start=1):
        hints = [hint for hint in results[index].hints if hint.relation in _RELATION_TYPES]
        if not hints:
            continue
        mapped = {
            f"h{position}": (hint.clause, hint.target)
            for position, hint in enumerate(hints, start=1)
        }
        hint_maps[index] = mapped
        payload.append(
            {
                "index": index,
                "text": case["sentence"],
                "hints": [
                    {"id": hint_id, "clause": clause, "target": target}
                    for hint_id, (clause, target) in mapped.items()
                ],
            }
        )
    return payload, hint_maps


def _selection_prompt(payload: list[dict[str, object]], *, strict_omit: bool) -> str:
    instruction = (
        "你只做英文定语从句指向筛选。每个 hint 都是程序从原句精确取得的固定片段。"
        "只有在该从句确实修饰或说明给出的 target，且这一关系有助于读者理解时，才选择该 hint。"
        "无法确定、目标明显不对、片段不自然或无帮助时不要选择。"
    )
    if strict_omit:
        instruction += (
            "默认省略。不要因为候选出现了 who、which 或 that 就选择；"
            "只有你能明确确认 target 是该定语从句所修饰的自然对象时才保留。"
            "宁可一条也不选，尤其省略目标像地点、抽象词、代词或句法残片的候选。"
        )
    return (
        instruction
        +
        "不得翻译、解释、复制或改写任何英文；只能返回提供的 hint_id。"
        "返回 JSON：{\"sentences\":[{\"index\":1,\"links\":[{\"hint_id\":\"h1\"}]}]}。"
        "可省略整句；links 不得重复。\n句子与候选："
        + json.dumps(payload, ensure_ascii=False)
    )


def _selected_ids(raw: object, hint_maps: dict[int, dict[str, tuple[str, str]]]) -> set[tuple[int, str]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("sentences"), list):
        return set()
    selected: set[tuple[int, str]] = set()
    for item in raw["sentences"]:
        if not isinstance(item, dict) or not isinstance(item.get("index"), int):
            continue
        index = item["index"]
        if index not in hint_maps or not isinstance(item.get("links"), list):
            continue
        for link in item["links"]:
            hint_id = link.get("hint_id") if isinstance(link, dict) else None
            if isinstance(hint_id, str) and hint_id in hint_maps[index]:
                selected.add((index, hint_id))
    return selected


def _matches_gold(clause: str, target: str, gold: list[dict[str, object]]) -> bool:
    normalized_clause = _normalized(clause)
    normalized_target = _normalized(target)
    return any(
        normalized_clause == _normalized(str(link["clause"]))
        and normalized_target in {_normalized(str(value)) for value in link["targets"]}
        for link in gold
    )


def main() -> int:
    args = _arguments()
    if args.repeat <= 0:
        raise SystemExit("--repeat must be positive")
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    cases = corpus["cases"]
    analyzer = SpacySyntaxAnalyzer(args.spacy_model)
    payload, hint_maps = _candidate_payload(cases, analyzer)
    print(f"preflight cases={len(cases)} candidate_sentences={len(payload)}")
    client = OpenAICompatibleClient(
        OpenAIConfig(
            base_url=args.base_url,
            model=args.model,
            response_format_type="json_schema",
            max_output_tokens=2048,
        )
    )
    system = "你帮助中文母语者识别英文定语从句的指向。输出必须是 JSON 对象，不要输出 Markdown。"
    prompt = _selection_prompt(payload, strict_omit=args.strict_omit)
    gold_by_index = {index: case["links"] for index, case in enumerate(cases, start=1)}
    all_runs: list[set[tuple[int, str]]] = []
    for run in range(1, args.repeat + 1):
        selected = _selected_ids(client.complete_json(system, prompt), hint_maps)
        all_runs.append(selected)
        correct = {
            value
            for value in selected
            if _matches_gold(*hint_maps[value[0]][value[1]], gold_by_index[value[0]])
        }
        precision = len(correct) / len(selected) if selected else 1.0
        print(f"run={run} selected={len(selected)} correct={len(correct)} precision={precision:.1%}")
        for index, hint_id in sorted(selected - correct):
            clause, target = hint_maps[index][hint_id]
            print(f"  wrong ss-{index:03d}: {clause} -> {target}")
    stable = len({frozenset(run) for run in all_runs}) == 1
    union = set().union(*all_runs)
    intersection = set.intersection(*all_runs)
    print(
        f"stability exact={stable} union={len(union)} intersection={len(intersection)} "
        f"usage_total_tokens={client.usage.total_tokens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
