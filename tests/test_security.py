"""Security and privacy guarantees, checked on every CI run.

Each test pins down one promise made in SECURITY.md, so a future change that
breaks a promise fails the build instead of silently leaking something.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import traceback
from datetime import date
from pathlib import Path

import pytest
import requests

from ballast import config
from ballast.market_data import fred
from conftest import is_safe_test_database

ROOT = Path(__file__).resolve().parents[1]
KEY = "0123456789abcdef0123456789abcdef"


# ---------- the FRED key never appears in errors ----------

class _RaisingSession:
    def get(self, url, params, timeout):
        # Mimic requests: its exceptions embed the full URL, query string included.
        query = "&".join(f"{k}={v}" for k, v in params.items())
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}?{query}")


class _ErrorBodySession:
    def get(self, url, params, timeout):
        class R:
            status_code = 400
            text = f"Bad Request. api_key {params['api_key']} is not registered."
        return R()


def _full_report(exc: BaseException) -> str:
    """Everything Python would print for this exception, including chained causes."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def test_network_error_does_not_leak_key():
    with pytest.raises(fred.FredError) as info:
        fred.fetch_series("DGS10", KEY, date(2024, 1, 1), session=_RaisingSession())
    assert KEY not in _full_report(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_error_body_is_redacted():
    with pytest.raises(fred.FredError) as info:
        fred.fetch_series("DGS10", KEY, date(2024, 1, 1), session=_ErrorBodySession())
    assert KEY not in _full_report(info.value)
    assert "***" in str(info.value)


def test_settings_repr_hides_secrets(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", KEY)
    text = repr(config.get_settings())
    assert KEY not in text and "ballast:ballast" not in text


# ---------- secrets live outside the project folder ----------

def test_secrets_are_read_from_user_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BALLAST_CONFIG_DIR", str(tmp_path))
    (tmp_path / "secrets.env").write_text(f"FRED_API_KEY={KEY}\n")
    assert config.get_settings().fred_api_key == KEY


def test_environment_variable_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BALLAST_CONFIG_DIR", str(tmp_path))
    (tmp_path / "secrets.env").write_text("FRED_API_KEY=fromfile\n")
    monkeypatch.setenv("FRED_API_KEY", "fromenv")
    assert config.get_settings().fred_api_key == "fromenv"


def test_project_env_file_is_ignored_and_flagged(tmp_path, monkeypatch):
    fake_project_env = tmp_path / ".env"
    fake_project_env.write_text(f"FRED_API_KEY={KEY}\n")
    monkeypatch.setattr(config, "PROJECT_ENV_FILE", fake_project_env)
    assert config.get_settings().fred_api_key is None
    assert any("inside the project folder" in w for w in config.security_warnings())


@pytest.mark.skipif(os.name != "posix", reason="file permissions are POSIX-only")
def test_world_readable_secrets_file_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setenv("BALLAST_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "PROJECT_ENV_FILE", tmp_path / "absent")
    path = tmp_path / "secrets.env"
    path.write_text("FRED_API_KEY=x\n")
    path.chmod(0o644)
    assert any("chmod 600" in w for w in config.security_warnings())
    path.chmod(0o600)
    assert config.security_warnings() == []


# ---------- nothing secret or private gets committed ----------

@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_gitignore_blocks_secrets_and_private_data(tmp_path):
    shutil.copy(ROOT / ".gitignore", tmp_path / ".gitignore")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    def ignored(path: str) -> bool:
        return subprocess.run(["git", "check-ignore", "-q", path], cwd=tmp_path).returncode == 0

    for path in [".env", ".env.local", "secrets.env", "data/private/trades.csv", "backup.dump", "id.pem"]:
        assert ignored(path), f"{path} should be git-ignored"
    for path in ["secrets.env.example", "data/seed/trades.csv", "db/migrations/001_instruments.sql", "README.md"]:
        assert not ignored(path), f"{path} should be committed"


def test_no_secrets_or_email_addresses_in_repository():
    key_like = re.compile(r"\b[a-f0-9]{32}\b")
    email = re.compile(r"[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    skip_dirs = {".git", ".venv", "venv", "__pycache__", ".pytest_cache"}
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or skip_dirs & set(path.parts) or path.suffix in {".pyc"}:
            continue
        if path == Path(__file__) or "egg-info" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if key_like.search(text) or email.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


# ---------- the database is not exposed to the network ----------

def test_database_port_bound_to_localhost_only():
    compose = (ROOT / "docker-compose.yml").read_text()
    mappings = re.findall(r'-\s*"([^"]*5432[^"]*)"', compose)
    assert mappings, "no port mapping found"
    assert all(m.startswith("127.0.0.1:") for m in mappings), mappings


def test_compose_mounts_no_host_folders():
    """Only a Docker-managed named volume: the container cannot see your files."""
    compose = (ROOT / "docker-compose.yml").read_text()
    volume_lines = re.findall(r"^\s*-\s*(\S+):/var/lib/postgresql/data", compose, flags=re.M)
    assert volume_lines == ["ballast-pgdata"]
    assert not re.search(r"^\s*-\s*[./~]", compose, flags=re.M)


# ---------- tests can never wipe the real database ----------

@pytest.mark.parametrize(
    "url, safe",
    [
        ("postgresql+psycopg://ballast:ballast@localhost:5432/ballast_test", True),
        ("postgresql+psycopg://ballast:ballast@localhost:5432/ballast", False),
        ("postgresql+psycopg://ballast:ballast@localhost:5432/test_ballast", False),
    ],
)
def test_test_database_guard(url, safe):
    assert is_safe_test_database(url) is safe
