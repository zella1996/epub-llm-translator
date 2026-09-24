from __future__ import annotations

import json
import sys

from scripts import compare_clause_benchmark_reports as compare
from scripts import benchmark_clause_relations as benchmark
from translator.sentence_analyzer import ClauseHint, LocalSyntaxResult


class _Analyzer:
    def analyze(self, sentences):
        assert [sentence.index for sentence in sentences] == [1]
        return {
            1: LocalSyntaxResult(
                needs_structure=True,
                clause_count=1,
                hints=(
                    ClauseHint("acl:relcl", "which she wrote", "the letter", 1),
                ),
            )
        }


def test_benchmark_writes_a_machine_readable_report(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "case-001",
                        "sentence": "The letter which she wrote was lost.",
                        "links": [
                            {
                                "clause": "which she wrote",
                                "targets": ["the letter"],
                            }
                        ],
                    }
                ]
            }
        )
    )
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        benchmark, "create_syntax_analyzer", lambda *_, **__: _Analyzer()
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_clause_relations.py",
            "--analyzer",
            "stanza",
            "--corpus",
            str(corpus),
            "--report",
            str(report),
        ],
    )

    assert benchmark.main() == 0
    assert json.loads(report.read_text()) == {
        "version": 1,
        "analyzer": "stanza",
        "stanza_package": "default_accurate",
        "cases": [
            {
                "id": "case-001",
                "predicted": [
                    {
                        "clause": "which she wrote",
                        "target": "the letter",
                        "relation": "acl:relcl",
                        "depth": 1,
                    }
                ],
            }
        ],
        "summary": {
            "expected": 1,
            "predicted": 1,
            "matched": 1,
            "precision": 1.0,
            "recall": 1.0,
        },
    }


def test_report_comparison_rejects_any_clause_relation_difference(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    payload = {
        "version": 1,
        "analyzer": "stanza",
        "stanza_package": "default_accurate",
        "cases": [
            {
                "id": "case-001",
                "predicted": [
                    {
                        "clause": "which she wrote",
                        "target": "the letter",
                        "relation": "acl:relcl",
                        "depth": 1,
                    }
                ],
            }
        ],
        "summary": {
            "expected": 1,
            "predicted": 1,
            "matched": 1,
            "precision": 1.0,
            "recall": 1.0,
        },
    }
    baseline.write_text(json.dumps(payload))
    candidate.write_text(json.dumps(payload))
    monkeypatch.setattr(sys, "argv", ["compare.py", str(baseline), str(candidate)])
    assert compare.main() == 0

    payload["cases"][0]["predicted"][0]["target"] = "the woman"
    candidate.write_text(json.dumps(payload))
    assert compare.main() == 1
