import os
from dataclasses import dataclass

from .secrets import load_project_environment

load_project_environment()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("NAVIGATE_DATABASE_URL", "sqlite:///./data/navigate.db")
    cors_origins: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv(
            "NAVIGATE_CORS_ORIGINS", "http://127.0.0.1:3000,http://localhost:3000"
        ).split(",")
        if item.strip()
    )


settings = Settings()
