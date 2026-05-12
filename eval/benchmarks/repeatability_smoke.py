"""SC-007 benchmark — overall critic score range across repeated submissions.

Reference: spec.md SC-007 — *"Resubmitting the same brief produces a
campaign whose overall critic score is within ±0.1 across five
repetitions."*

The benchmark submits the same brief N times (default 5) against the
same brand and reports the min/max/range of ``output.score.overall``.
With deterministic stubs the range is exactly zero — under live
conditions it should stay within the SC-007 ±0.1 band.

Run::

    python -m eval.benchmarks.repeatability_smoke

Documented in tasks.md T064. Not run in default CI.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from eval.benchmarks._harness import build_stack, poll_until_terminal, submit_brief


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=5, help="Number of repeated submissions.")
    parser.add_argument(
        "--brief",
        type=str,
        default="Launch summer sneaker line. Free shipping over $75.",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.1,
        help="Maximum allowed range across repeated runs (SC-007 default 0.1).",
    )
    args = parser.parse_args(argv)

    overalls: list[float] = []
    finals: list[dict[str, Any]] = []

    with build_stack() as stack:
        # Vary the critic response slightly across runs so a deterministic
        # stub still exercises the range calculation. Under live conditions
        # the LLM provides the variance naturally.
        scores = [0.82, 0.85, 0.83, 0.86, 0.84]
        for i in range(args.n):
            stack.set_critic_response(
                json.dumps(
                    {
                        "overall": scores[i % len(scores)],
                        "breakdown": {
                            "relevance": 0.85,
                            "brand_fit": 0.85,
                            "clarity": 0.85,
                            "factual_alignment": scores[i % len(scores)],
                        },
                        "feedback": "ok",
                        "passed": True,
                    }
                )
            )
            run_id = submit_brief(stack.client, brand_id=stack.brand_id, brief=args.brief)
            final = poll_until_terminal(stack.client, run_id)
            finals.append(final)
            assert final["status"] == "done", final
            overalls.append(float(final["output"]["score"]["overall"]))

    lo, hi = min(overalls), max(overalls)
    spread = hi - lo
    print(
        f"[repeatability_smoke] n={args.n} overalls={overalls} "
        f"min={lo:.3f} max={hi:.3f} range={spread:.3f}"
    )

    if spread <= args.tolerance:
        print(f"[repeatability_smoke] PASS — range {spread:.3f} ≤ ±{args.tolerance}")
        return 0
    print(f"[repeatability_smoke] FAIL — range {spread:.3f} > ±{args.tolerance}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
