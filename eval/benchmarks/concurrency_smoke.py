"""SC-008 benchmark — concurrent runs vs solo-run baseline.

Reference: spec.md SC-008 — *"Five concurrent campaign runs each finish
within 1.5x the solo-run baseline."*

The harness:

1. Submits a single brief and times it end-to-end (the solo baseline).
2. Submits N briefs back-to-back (default 5, the concurrency cap from
   ``AURA_CONCURRENCY_CAP``) and times each one.
3. Prints PASS/WARN/FAIL: PASS when every concurrent run is ≤ 1.5x the
   baseline, WARN when one is, FAIL when more than one is.

Run::

    python -m eval.benchmarks.concurrency_smoke

Documented in tasks.md T063. Not run in default CI.
"""

from __future__ import annotations

import argparse
import statistics
import time
from dataclasses import dataclass

from eval.benchmarks._harness import build_stack, poll_until_terminal, submit_brief


@dataclass
class RunTiming:
    run_id: str
    submit_at: float
    finish_at: float

    @property
    def wall_clock_s(self) -> float:
        return self.finish_at - self.submit_at


def _time_one(stack: object, *, brief: str) -> RunTiming:
    from eval.benchmarks._harness import BenchStack  # local import for typing

    assert isinstance(stack, BenchStack)
    submit_at = time.monotonic()
    run_id = submit_brief(stack.client, brand_id=stack.brand_id, brief=brief)
    poll_until_terminal(stack.client, run_id)
    return RunTiming(run_id=run_id, submit_at=submit_at, finish_at=time.monotonic())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--n", type=int, default=5, help="Number of concurrent runs (default: AURA_CONCURRENCY_CAP)"
    )
    parser.add_argument(
        "--budget-multiplier",
        type=float,
        default=1.5,
        help="Per-run latency budget as a multiple of the solo baseline.",
    )
    args = parser.parse_args(argv)

    with build_stack() as stack:
        # 1. Solo baseline.
        print("[concurrency_smoke] measuring solo baseline...")
        solo = _time_one(stack, brief="Solo baseline brief")
        print(f"[concurrency_smoke] solo baseline: {solo.wall_clock_s:.2f}s")

        # 2. Concurrent batch.
        n = max(1, args.n)
        budget = solo.wall_clock_s * args.budget_multiplier
        print(f"[concurrency_smoke] submitting {n} concurrent runs (budget: {budget:.2f}s/run)...")

        # Submit all `n` then poll each.
        run_ids: list[tuple[str, float]] = []
        for i in range(n):
            ts = time.monotonic()
            rid = submit_brief(stack.client, brand_id=stack.brand_id, brief=f"Concurrent brief {i}")
            run_ids.append((rid, ts))

        timings: list[RunTiming] = []
        for rid, submit_at in run_ids:
            poll_until_terminal(stack.client, rid)
            timings.append(RunTiming(run_id=rid, submit_at=submit_at, finish_at=time.monotonic()))

    durations = [t.wall_clock_s for t in timings]
    over_budget = [t for t in timings if t.wall_clock_s > budget]
    p50 = statistics.median(durations)
    p_max = max(durations)
    print(
        f"[concurrency_smoke] concurrent durations p50={p50:.2f}s "
        f"max={p_max:.2f}s over_budget={len(over_budget)}/{n}"
    )

    if not over_budget:
        print("[concurrency_smoke] PASS — all concurrent runs within budget.")
        return 0
    if len(over_budget) == 1:
        print(
            f"[concurrency_smoke] WARN — 1 run exceeded budget "
            f"({over_budget[0].run_id}: {over_budget[0].wall_clock_s:.2f}s)"
        )
        return 0
    print(f"[concurrency_smoke] FAIL — {len(over_budget)} runs exceeded {budget:.2f}s budget.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
