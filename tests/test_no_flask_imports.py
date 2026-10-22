"""Nothing under this service imports the Flask world (``minty_shared.testing.guards``): a
verbatim port carries its imports, and a function-local one fails only when first run."""

from __future__ import annotations

from pathlib import Path

from minty_shared.testing import guards

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("onboarding", "core", "shared_models", "config")


def test_no_flask_world_import():
    assert guards.forbidden_imports(ROOT, PACKAGES) == []


def test_the_walk_saw_the_service():
    files = {p.relative_to(ROOT).as_posix() for p in guards.python_files(ROOT, PACKAGES)}
    assert "core/auth.py" in files and "onboarding/api_entity.py" in files
