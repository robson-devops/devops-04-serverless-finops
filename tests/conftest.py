import importlib.util
import os
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def load_function(name):
    """Carrega src/<name>/app.py com nome próprio; todas as funções usam app.py."""
    spec = importlib.util.spec_from_file_location(f"{name}_app", SRC / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    os.environ.pop("AWS_PROFILE", None)
