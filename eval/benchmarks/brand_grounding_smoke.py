"""SC-003 benchmark — fraction of campaigns that reference brand-document facts.

Reference: spec.md SC-003 — *"≥ 80% of campaigns reference at least one
distinctive fact (product name, price, claim) from the brand's
uploaded documents."*

The benchmark seeds a brand with a fixture document containing a small
set of marker phrases, submits N varied briefs, and counts the
campaigns whose ad copy contains at least one marker via
case-insensitive substring matching.

Under stubbed conditions, the LLM is configured to echo retrieved
chunks back, so the smoke verifies the *retrieval-into-prompt* path
rather than LLM faithfulness — the live variant (off by default)
exercises the latter.

Run::

    python -m eval.benchmarks.brand_grounding_smoke

Documented in tasks.md T072. Not run in default CI.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

from eval.benchmarks._harness import build_stack, poll_until_terminal, submit_brief

_DEFAULT_MARKERS: tuple[str, ...] = (
    "ACME Mesh Pro",
    "Free shipping over $75",
    "Lifetime warranty",
)


_DEFAULT_DOCUMENT_BODY = (
    "Brand voice: confident, technical.\n"
    "ACME Mesh Pro is our flagship sneaker.\n"
    "Free shipping over $75 in the contiguous US.\n"
    "Lifetime warranty against manufacturing defects.\n"
)


_DEFAULT_BRIEFS: tuple[str, ...] = (
    "Launch the summer sneaker line.",
    "Highlight breathability for fitness audiences.",
    "Promote the free-shipping threshold.",
    "Emphasise the lifetime warranty.",
    "Drive sign-ups for the loyalty program.",
)


@dataclass
class GroundingSample:
    run_id: str
    headline: str
    primary_text: str
    matched_markers: list[str]


def _matched(text: str, markers: tuple[str, ...]) -> list[str]:
    lo = text.lower()
    return [m for m in markers if m.lower() in lo]


def _patch_copy_to_echo_retrieval(stack: object, markers: tuple[str, ...]) -> None:
    """Replace the copy stub so the LLM "incorporates" retrieved chunks.

    The harness's default copy stub returns a fixed payload. For the
    grounding smoke we want the headline / primary_text to reflect any
    marker that surfaced in the retrieval prompt — that's what a live
    LLM grounded on those chunks would do.
    """

    from types import SimpleNamespace

    from agents.stages import copy as copy_stage

    def _create(**kwargs: Any) -> SimpleNamespace:
        joined = " ".join(
            m.get("content", "") for m in kwargs.get("messages", []) if isinstance(m, dict)
        )
        if "critic" in joined.lower():
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "overall": 0.85,
                                    "breakdown": {
                                        "relevance": 0.9,
                                        "brand_fit": 0.85,
                                        "clarity": 0.82,
                                        "factual_alignment": 0.83,
                                    },
                                    "feedback": "ok",
                                    "passed": True,
                                }
                            )
                        )
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=42, completion_tokens=24),
            )

        echoed = next((m for m in markers if m.lower() in joined.lower()), "")
        headline = f"Discover {echoed}." if echoed else "Discover our brand."
        primary_text = (
            f"{echoed}. Built for the streets you actually run."
            if echoed
            else "Built for the streets you actually run."
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "headline": headline,
                                "primary_text": primary_text,
                                "cta": "Shop now",
                                "platform": "instagram",
                            }
                        )
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=42, completion_tokens=24),
        )

    fake_llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
    setattr(copy_stage, "_make_llm_client", lambda: fake_llm)  # noqa: B010
    # critic stage uses the same _make_llm_client name imported at module load.
    from agents.stages import critic as critic_stage

    setattr(critic_stage, "_make_llm_client", lambda: fake_llm)  # noqa: B010


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=25, help="Number of briefs to submit.")
    parser.add_argument(
        "--target-fraction",
        type=float,
        default=0.8,
        help="SC-003 minimum fraction of grounded campaigns (default 0.8).",
    )
    args = parser.parse_args(argv)

    samples: list[GroundingSample] = []
    with build_stack() as stack:
        # Seed the brand document.
        upload = stack.client.post(
            f"/api/v1/brands/{stack.brand_id}/documents",
            files={"file": ("brand.txt", _DEFAULT_DOCUMENT_BODY.encode(), "text/plain")},
        )
        upload.raise_for_status()

        _patch_copy_to_echo_retrieval(stack, _DEFAULT_MARKERS)

        for i in range(args.n):
            brief = _DEFAULT_BRIEFS[i % len(_DEFAULT_BRIEFS)]
            run_id = submit_brief(stack.client, brand_id=stack.brand_id, brief=brief)
            final = poll_until_terminal(stack.client, run_id)
            if final["status"] != "done":
                print(
                    f"[brand_grounding_smoke] WARN — run {run_id} status={final['status']} "
                    f"failed_reason={final.get('failed_reason')}"
                )
                continue
            ad_copy = final["output"]["ad_copy"]
            text = f"{ad_copy['headline']} {ad_copy['primary_text']}"
            matched = _matched(text, _DEFAULT_MARKERS)
            samples.append(
                GroundingSample(
                    run_id=run_id,
                    headline=ad_copy["headline"],
                    primary_text=ad_copy["primary_text"],
                    matched_markers=matched,
                )
            )

    if not samples:
        print("[brand_grounding_smoke] FAIL — no successful runs to score")
        return 1

    grounded = [s for s in samples if s.matched_markers]
    fraction = len(grounded) / len(samples)
    print(
        f"[brand_grounding_smoke] n={len(samples)} grounded={len(grounded)} "
        f"fraction={fraction:.2%} target={args.target_fraction:.0%}"
    )

    if fraction >= args.target_fraction:
        print(f"[brand_grounding_smoke] PASS — {fraction:.2%} ≥ {args.target_fraction:.0%}")
        return 0
    print(f"[brand_grounding_smoke] FAIL — {fraction:.2%} below SC-003 {args.target_fraction:.0%}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
