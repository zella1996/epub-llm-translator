"""Compare two machine-readable clause benchmark reports exactly."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    return parser.parse_args()


def _read_report(path: Path) -> dict[str, object]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read report {path}: {exc}") from exc
    if not isinstance(report, dict) or report.get("version") != 1:
        raise SystemExit(f"invalid report {path}")
    return report


def _case_map(report: dict[str, object]) -> dict[str, list[object]]:
    cases = report.get("cases")
    if not isinstance(cases, list):
        raise SystemExit("invalid report cases")
    mapped: dict[str, list[object]] = {}
    for case in cases:
        if not isinstance(case, dict):
            raise SystemExit("invalid report case")
        case_id = case.get("id")
        predicted = case.get("predicted")
        if not isinstance(case_id, str) or not isinstance(predicted, list):
            raise SystemExit("invalid report case")
        if case_id in mapped:
            raise SystemExit(f"duplicate report case {case_id}")
        mapped[case_id] = predicted
    return mapped


def main() -> int:
    args = _arguments()
    baseline = _read_report(args.baseline)
    candidate = _read_report(args.candidate)
    differences: list[str] = []
    for key in ("analyzer", "stanza_package"):
        if baseline.get(key) != candidate.get(key):
            differences.append(
                f"configuration {key}: {baseline.get(key)!r} != {candidate.get(key)!r}"
            )
    baseline_cases = _case_map(baseline)
    candidate_cases = _case_map(candidate)
    for case_id in sorted(set(baseline_cases) | set(candidate_cases)):
        if baseline_cases.get(case_id) != candidate_cases.get(case_id):
            differences.append(f"case {case_id}: predicted links differ")
    if differences:
        print("MISMATCH")
        print("\n".join(differences))
        return 1
    print("MATCH")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
