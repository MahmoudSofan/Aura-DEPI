from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import mlflow

logger = logging.getLogger(__name__)

_DEFAULT_TRACKING_URI = "http://localhost:5000"
_DEFAULT_EXPERIMENT = "aura"


def log_run(
    params: Mapping[str, Any],
    metrics: Mapping[str, float],
    artifacts: Iterable[str | Path] = (),
    *,
    run_name: str | None = None,
    experiment: str | None = None,
    tags: Mapping[str, str] | None = None,
) -> str:
    """Log a single MLflow run and return its run_id.

    Tracking URI and experiment come from env (`MLFLOW_TRACKING_URI`,
    `MLFLOW_EXPERIMENT`); pass `experiment=` to override per call.
    `artifacts` is an iterable of local file paths to upload.
    """
    tracking_uri = os.getenv("MLFLOW_TRACKING_URI", _DEFAULT_TRACKING_URI)
    exp_name = experiment or os.getenv("MLFLOW_EXPERIMENT", _DEFAULT_EXPERIMENT)

    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(exp_name)

    with mlflow.start_run(run_name=run_name) as run:
        if tags:
            mlflow.set_tags(dict(tags))
        if params:
            mlflow.log_params(dict(params))
        if metrics:
            mlflow.log_metrics(dict(metrics))
        for path in artifacts:
            p = Path(path)
            if not p.exists():
                logger.warning("artifact path does not exist, skipping: %s", p)
                continue
            mlflow.log_artifact(str(p))

        run_id: str = run.info.run_id
        logger.info("logged mlflow run %s to %s/%s", run_id, tracking_uri, exp_name)
        return run_id
