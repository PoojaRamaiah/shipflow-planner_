"""
etl/db.py
Database connection pool and config loader.
All other modules import from here.
"""

import os
import re
import logging
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

load_dotenv()

logger = logging.getLogger(__name__)

_config: dict | None = None
_engine = None
_Session = None


def _resolve_env(value: Any) -> Any:
    """Replace ${VAR:default} placeholders with env values."""
    if not isinstance(value, str):
        return value
    pattern = r"\$\{(\w+)(?::([^}]*))?\}"
    def replace(m):
        var, default = m.group(1), m.group(2) or ""
        return os.environ.get(var, default)
    return re.sub(pattern, replace, value)


def _resolve_dict(d: dict) -> dict:
    """Recursively resolve env placeholders in a config dict."""
    return {
        k: _resolve_dict(v) if isinstance(v, dict) else _resolve_env(v)
        for k, v in d.items()
    }


def load_config(path: str | None = None) -> dict:
    """Load and cache settings.yaml with env substitution."""
    global _config
    if _config is not None:
        return _config
    cfg_path = path or Path(__file__).parents[1] / "config" / "settings.yaml"
    with open(cfg_path) as f:
        raw = yaml.safe_load(f)
    _config = _resolve_dict(raw)
    return _config


def get_engine():
    """Return (and cache) a SQLAlchemy engine."""
    global _engine
    if _engine is not None:
        return _engine
    cfg = load_config()["database"]
    url = (
        f"postgresql+psycopg2://{cfg['user']}:{cfg['password']}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['name']}"
    )
    _engine = create_engine(
        url,
        poolclass=QueuePool,
        pool_size=int(cfg.get("pool_size", 5)),
        max_overflow=int(cfg.get("max_overflow", 10)),
        connect_args={"connect_timeout": int(cfg.get("connect_timeout", 30))},
        echo=False,
    )
    logger.info("Database engine created: %s:%s/%s", cfg["host"], cfg["port"], cfg["name"])
    return _engine


def get_session():
    """Return a new SQLAlchemy session."""
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=get_engine())
    return _Session()


def test_connection() -> bool:
    """Quick connectivity check. Returns True if DB is reachable."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error("DB connection failed: %s", e)
        return False
