from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str | None = None) -> str | None:
    """Le uma variavel de ambiente tratando string vazia como ausente."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def env_float(name: str, default: float) -> float:
    raw = env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


DATA_DIR = Path(env("DATA_DIR", str(ROOT / "data")) or (ROOT / "data"))
DB_PATH = Path(env("DB_PATH", str(DATA_DIR / "analytics.db")) or (DATA_DIR / "analytics.db"))
SECRETS_DIR = Path(env("SECRETS_DIR", str(ROOT / ".secrets")) or (ROOT / ".secrets"))
TZ = env("ANALYTICS_TZ", "America/Sao_Paulo") or "America/Sao_Paulo"
