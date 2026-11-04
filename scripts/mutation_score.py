#!/usr/bin/env python3
"""Fail when the mutmut mutation score is below a threshold.

    uv run --group mutation mutmut run "mizzle.core.*"
    uv run --group mutation mutmut export-cicd-stats
    uv run python scripts/mutation_score.py --min 60

``mutmut export-cicd-stats`` writes ``mutants/mutmut-cicd-stats.json``; this reads it and
prints the score (killed over every mutant that was actually detected as good or bad).
Exit status is 1 when the score is below ``--min`` or the stats file is missing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DETECTED = ("killed", "survived", "timeout", "suspicious", "no_tests")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stats", nargs="?", default="mutants/mutmut-cicd-stats.json")
    parser.add_argument("--min", type=float, default=0.0, help="minimum score, in percent")
    args = parser.parse_args()

    path = Path(args.stats)
    if not path.is_file():
        print(f"no mutation stats at {path}; run `mutmut run` first", file=sys.stderr)
        return 1
    stats: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    counts = {name: int(stats.get(name, 0) or 0) for name in DETECTED}
    detected = sum(counts.values())
    detail = ", ".join(f"{name} {count}" for name, count in counts.items())
    if not detected:
        # A filter that matches nothing would otherwise report a perfect score.
        print(f"no mutants ran ({detail}); the filter matched nothing", file=sys.stderr)
        return 1
    score = 100.0 * counts["killed"] / detected
    print(f"mutation score {score:.1f}% ({detail})")
    if score < args.min:
        print(f"below the {args.min:.1f}% threshold", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
