"""Environment-driven settings shared by both apps.

Values come from environment variables, optionally loaded from a `.env`
file in the repo root. See `.env.example` for every option.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional
    load_dotenv = None

REPO_ROOT = Path(__file__).resolve().parent.parent

if load_dotenv is not None:
    load_dotenv(REPO_ROOT / ".env", override=False)


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip()


def env_bool(name: str, default: bool = False) -> bool:
    v = env(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except (TypeError, ValueError):
        return default


def data_dir() -> Path:
    p = Path(env("SAAKSHI_DATA_DIR", str(REPO_ROOT / "data")))
    p.mkdir(parents=True, exist_ok=True)
    return p
