import os
import re
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ENV_FILE = PROJECT_ROOT / ".env"
MANAGED_SECRET_NAMES = ("DEEPSEEK_API_KEY", "REDFOX_API_KEY", "FIRECRAWL_API_KEY")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class MissingSecretError(RuntimeError):
    pass


def load_project_environment(path: Path = PROJECT_ENV_FILE) -> bool:
    """Load the project-local env file without overriding injected runtime secrets."""
    if not path.is_file():
        return False
    return bool(load_dotenv(dotenv_path=path, override=False))


def get_secret(name: str) -> str:
    if not _ENV_NAME.fullmatch(name):
        raise ValueError("invalid environment variable name")
    load_project_environment()
    return os.getenv(name, "").strip()


def require_secret(name: str) -> str:
    value = get_secret(name)
    if not value:
        raise MissingSecretError(f"missing required secret environment variable: {name}")
    return value


def managed_secret_status() -> dict[str, bool]:
    return {name: bool(get_secret(name)) for name in MANAGED_SECRET_NAMES}
