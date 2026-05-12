"""Repo-root pytest configuration.

Registers the fixtures in :mod:`backend.tests.conftest` as a pytest plugin
so tests anywhere in the tree (e.g. ``agents/tests/``, ``rag/tests/``)
share the same ``api_client`` / ``stub_openai`` / ``stub_hf`` /
``stub_tavily`` / ``engine`` / etc. fixtures without each subtree
duplicating them.
"""

from __future__ import annotations

pytest_plugins = ["backend.tests.conftest"]
