"""SC-001 benchmark — end-to-end campaign latency p50 / p95.

Reference: spec.md SC-001 — *"A typical brief returns a complete
campaign within 60 seconds at the 50th percentile."*

The benchmark submits N briefs sequentially against the in-process
stack from :mod:`eval.benchmarks._harness` (deterministic stubs) and
prints PASS/WARN/FAIL based on the observed p50.

A live-backend variant (gated on ``AURA_RUN_LIVE_TESTS=1``) is left as
follow-up — the stubbed-stack baseline catches code-path regressions
that would push p50 over the budget even before live latency is added
on top.

Run::

    python -m eval.benchmarks.e2e_latency_smoke

Documented in tasks.md T071. Not run in default CI.
"""

from __future__ import annotations

import argparse
import statistics
import time
from dataclasses import dataclass

from eval.benchmarks._harness import build_stack, poll_until_terminal, submit_brief


@dataclass
class LatencySample:
    run_id: str
    duration_s: float


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=20, help="Number of submitted briefs.")
    parser.add_argument(
        "--p50-budget-s",
        type=float,
        default=60.0,
        help="SC-001 p50 budget in seconds (default 60).",
    )
    parser.add_argument(
        "--p95-budget-s",
        type=float,
        default=120.0,
        help="Soft budget for p95 (warn-only above this).",
    )
    args = parser.parse_args(argv)

    samples: list[LatencySample] = []
    with build_stack() as stack:
        for i in range(args.n):
            run_id = submit_brief(
                stack.client,
                brand_id=stack.brand_id,
                brief=f"Latency probe brief {i}",
            )
            t0 = time.monotonic()
            poll_until_terminal(stack.client, run_id)
            samples.append(LatencySample(run_id=run_id, duration_s=time.monotonic() - t0))

    durations = sorted(s.duration_s for s in samples)
    p50 = statistics.median(durations)
    p95 = durations[int(0.95 * (len(durations) - 1))]
    p_max = max(durations)
    print(
        f"[e2e_latency_smoke] n={args.n} p50={p50:.2f}s p95={p95:.2f}s "
        f"max={p_max:.2f}s budget_p50={args.p50_budget_s:.0f}s"
    )

    if p50 > args.p50_budget_s:
        print(
            f"[e2e_latency_smoke] FAIL — p50 {p50:.2f}s exceeded SC-001 budget "
            f"{args.p50_budget_s:.0f}s"
        )
        return 1
    if p95 > args.p95_budget_s:
        print(
            f"[e2e_latency_smoke] WARN — p95 {p95:.2f}s above {args.p95_budget_s:.0f}s "
            f"(p50 still within budget)"
        )
        return 0
    print(f"[e2e_latency_smoke] PASS — p50 {p50:.2f}s within {args.p50_budget_s:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
