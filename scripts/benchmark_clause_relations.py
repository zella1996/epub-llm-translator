"""Evaluate source-grounded clause links against a small public-domain corpus."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from translator.sentence_analyzer import (
    Sentence,
    SyntaxAnalyzerUnavailable,
    create_syntax_analyzer,
)


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
    parser.add_argument("--analyzer", choices=("spacy", "stanza"), required=True)
    parser.add_argument("--spacy-model", default="en_core_web_sm")
    parser.add_argument("--stanza-package", default="default_accurate")
    parser.add_argument("--stanza-model-dir")
    parser.add_argument("--stanza-hf-cache-dir")
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    cases = corpus["cases"]
    try:
        analyzer = create_syntax_analyzer(
            args.analyzer,
            model=args.spacy_model,
            stanza_package=args.stanza_package,
            stanza_model_dir=args.stanza_model_dir,
            stanza_hf_cache_dir=args.stanza_hf_cache_dir,
        )
    except SyntaxAnalyzerUnavailable as exc:
        raise SystemExit(f"preflight failed: {exc}") from exc
    assert analyzer is not None

    sentences = [
        Sentence(index, case["sentence"])
        for index, case in enumerate(cases, start=1)
    ]
    results = analyzer.analyze(sentences)
    expected_total = matched_total = predicted_total = 0
    report_cases: list[dict[str, object]] = []
    for index, case in enumerate(cases, start=1):
        expected = case["links"]
        predicted = tuple(
            hint
            for hint in results[index].hints
            if hint.relation in {"acl", "relcl", "acl:relcl"}
        )
        expected_total += len(expected)
        predicted_total += len(predicted)
        report_cases.append(
            {
                "id": case["id"],
                "predicted": [
                    {
                        "clause": hint.clause,
                        "target": hint.target,
                        "relation": hint.relation,
                        "depth": hint.depth,
                    }
                    for hint in predicted
                ],
            }
        )
        unmatched = list(predicted)
        missing: list[str] = []
        for link in expected:
            clause = _normalized(link["clause"])
            targets = {_normalized(value) for value in link["targets"]}
            match = next(
                (
                    hint
                    for hint in unmatched
                    if _normalized(hint.clause) == clause
                    and _normalized(hint.target) in targets
                ),
                None,
            )
            if match is None:
                missing.append(f'{link["clause"]} -> {link["targets"]}')
            else:
                matched_total += 1
                unmatched.remove(match)
        status = "PASS" if not missing and not unmatched else "CHECK"
        print(f'{status} {case["id"]}')
        for value in missing:
            print(f"  missing: {value}")
        for hint in unmatched:
            print(f"  extra: {hint.clause} -> {hint.target} ({hint.relation})")

    precision = matched_total / predicted_total if predicted_total else 0.0
    recall = matched_total / expected_total if expected_total else 1.0
    print(
        f"summary cases={len(cases)} expected={expected_total} "
        f"predicted={predicted_total} matched={matched_total} "
        f"precision={precision:.1%} recall={recall:.1%}"
    )
    if args.report is not None:
        args.report.write_text(
            json.dumps(
                {
                    "version": 1,
                    "analyzer": args.analyzer,
                    "stanza_package": args.stanza_package,
                    "cases": report_cases,
                    "summary": {
                        "expected": expected_total,
                        "predicted": predicted_total,
                        "matched": matched_total,
                        "precision": precision,
                        "recall": recall,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
