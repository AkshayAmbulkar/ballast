"""Runtime configuration.

Secrets live OUTSIDE the project folder, in ~/.config/ballast/secrets.env
(override the folder with BALLAST_CONFIG_DIR). Keeping them out of the folder
means they can never be committed, drag-and-dropped into a GitHub upload,
zipped, or shared along with the code. Ballast deliberately ignores any .env
file inside the project folder and warns if one exists.

Environment variables take priority over the secrets file.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "db" / "migrations"
SEED_DIR = PROJECT_ROOT / "data" / "seed"
PRIVATE_DATA_DIR = PROJECT_ROOT / "data" / "private"   # git-ignored: for your own real trades
PROJECT_ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_DATABASE_URL = "postgresql+psycopg://ballast:ballast@localhost:5432/ballast"
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://ballast:ballast@localhost:5432/ballast_test"


def config_dir() -> Path:
    return Path(os.getenv("BALLAST_CONFIG_DIR") or Path.home() / ".config" / "ballast")


def secrets_file() -> Path:
    return config_dir() / "secrets.env"


@dataclass(frozen=True)
class Settings:
    database_url: str
    test_database_url: str
    fred_api_key: str | None

    def __repr__(self) -> str:  # never print the key, even when debugging
        key = "set" if self.fred_api_key else "not set"
        return f"Settings(database_url=<hidden>, test_database_url=<hidden>, fred_api_key=<{key}>)"


def get_settings() -> Settings:
    path = secrets_file()
    file_values = dotenv_values(path) if path.is_file() else {}

    def pick(name: str, default: str | None = None) -> str | None:
        return os.getenv(name) or file_values.get(name) or default

    return Settings(
        database_url=pick("DATABASE_URL", DEFAULT_DATABASE_URL),
        test_database_url=pick("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL),
        fred_api_key=pick("FRED_API_KEY"),
    )


def security_warnings() -> list[str]:
    """Problems with how secrets are stored on this computer (empty list = all good)."""
    warnings: list[str] = []
    if PROJECT_ENV_FILE.exists():
        warnings.append(
            f"A .env file exists inside the project folder ({PROJECT_ENV_FILE}). Ballast ignores it, "
            "because files in the project folder can end up on GitHub. Move it with:\n"
            f"    mkdir -p {config_dir()} && mv {PROJECT_ENV_FILE} {secrets_file()} && chmod 600 {secrets_file()}"
        )
    path = secrets_file()
    if path.is_file() and os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            warnings.append(
                f"{path} can be read by other users of this computer (permissions {oct(mode)}). "
                f"Fix with:\n    chmod 600 {path}"
            )
    return warnings
