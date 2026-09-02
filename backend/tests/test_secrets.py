from pathlib import Path

import pytest

from app.secrets import MissingSecretError, get_secret, load_project_environment, require_secret


def test_project_env_loads_without_overriding_runtime_secret(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_MANAGED_SECRET=from-file\n", encoding="utf-8")
    monkeypatch.setenv("TEST_MANAGED_SECRET", "from-runtime")

    load_project_environment(env_file)

    assert get_secret("TEST_MANAGED_SECRET") == "from-runtime"


def test_project_env_can_supply_missing_secret(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_MANAGED_SECRET=from-file\n", encoding="utf-8")
    monkeypatch.delenv("TEST_MANAGED_SECRET", raising=False)

    load_project_environment(env_file)

    assert get_secret("TEST_MANAGED_SECRET") == "from-file"


def test_required_secret_error_names_variable_without_value(monkeypatch):
    monkeypatch.delenv("TEST_MISSING_SECRET", raising=False)

    with pytest.raises(MissingSecretError, match="TEST_MISSING_SECRET"):
        require_secret("TEST_MISSING_SECRET")
