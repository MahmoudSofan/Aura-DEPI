"""Smoke test: log a fake run to the MLflow server and print the run_id.

Usage (from repo root, with the compose stack up):
    python -m eval.smoke_mlflow
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from eval.tracking import log_run

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        artifact = Path(tmp) / "hello.txt"
        artifact.write_text("hello from smoke_mlflow\n")

        run_id = log_run(
            params={"model": "gpt-4o-mini", "temperature": 0.7},
            metrics={"latency_ms": 123.4, "score": 0.84},
            artifacts=[artifact],
            run_name="smoke-test",
            tags={"kind": "smoke"},
        )

    print(f"OK: logged run {run_id}")


if __name__ == "__main__":
    main()
