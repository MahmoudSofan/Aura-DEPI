"""Aura benchmarks — manual smoke harnesses, not run in default CI.

Each module in this package is invokable as a script (``python -m
eval.benchmarks.<name>``) and prints a PASS/WARN/FAIL summary against
one of the spec's measurable success criteria. They submit briefs
against a running API instance whose external service clients have
already been replaced by the standard test fixtures (offline, no token
spend), and report wall-clock latencies / quality metrics so the team
can monitor regressions outside of CI.

Modules:

* ``concurrency_smoke`` — SC-008 (5 concurrent runs within 1.5x baseline).
* ``repeatability_smoke`` — SC-007 (overall score within ±0.1 across 5 reruns).
* ``e2e_latency_smoke`` — SC-001 (p50 ≤ 60s end-to-end).
* ``brand_grounding_smoke`` — SC-003 (≥ 80% of campaigns reference a fact
  from the brand's documents).
"""
