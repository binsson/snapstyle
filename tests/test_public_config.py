import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import server


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("setting,expected", [(None, False), ("0", False), ("1", True)])
def test_pinterest_requires_opt_in_even_with_dependency_installed(setting, expected):
    env = os.environ.copy()
    env.update(API_BASE="https://provider.example/v1", API_KEY="test-api-key")
    env.pop("PINTEREST_SCRAPE", None)
    if setting is not None:
        env["PINTEREST_SCRAPE"] = setting
    result = subprocess.run(
        [sys.executable, "-c", "import importlib.util; importlib.util.find_spec = lambda name: object(); import server; print(server.PINTEREST_SCRAPE)"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == str(expected)


def test_missing_api_base_fails_before_any_provider_request():
    env = os.environ.copy()
    env.pop("API_BASE", None)
    env["API_KEY"] = "test-api-key"
    result = subprocess.run(
        [sys.executable, "-c", "import server"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "API_BASE environment variable is required" in result.stderr


def test_pinterest_ignores_personal_config_and_keeps_diagnostics_private(monkeypatch, capsys):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="", stderr="private-cookie-and-proxy-credential", returncode=1)

    monkeypatch.setattr(server.subprocess, "run", run)
    monkeypatch.setattr(server, "PINTEREST_COOKIES_BROWSER", "")
    assert server._gallerydl_pin_urls("test", 1, 24) == (None, False)
    assert "--ignore-config" in commands[0]
    assert "--cookies-from-browser" not in commands[0]
    assert "private-cookie-and-proxy-credential" not in capsys.readouterr().out
