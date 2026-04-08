import os
from typing import Optional

from dotenv import load_dotenv


load_dotenv()


def env_str(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def env_int(name: str, default: int = 0) -> int:
    raw = env_str(name, str(default))
    try:
        return int(raw)
    except Exception:
        return default


def env_optional_int(name: str) -> Optional[int]:
    value = env_int(name, 0)
    return value or None

