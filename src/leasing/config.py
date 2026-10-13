from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/leasing"

    anthropic_model: str = "claude-opus-5-5"
    # Effort is the depth control on Claude Opus 5.5 (thinking is always on). Conversations
    # need reliable tool selection; briefs are short narrations of precomputed numbers.
    agent_effort: Effort = "medium"
    brief_effort: Effort = "low"
    agent_max_iterations: int = 6

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "leasing"
    cadence_hours: list[int] = [24, 72, 168]

    sms_backend: Literal["console", "twilio"] = "console"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_messaging_service_sid: str = ""
    twilio_validate_signatures: bool = True
    public_base_url: str = "http://localhost:8000"

    email_backend: Literal["console", "smtp"] = "console"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    email_from: str = "leasing@example.com"

    crm_backend: Literal["memory", "hubspot"] = "memory"
    hubspot_access_token: str = ""

    dashboard_origin: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
