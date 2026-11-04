"""Single responsibility: typed runtime settings loaded from the environment.

Values come from environment variables or a local `.env` file. Model names are
never hard coded elsewhere; every module reads them from here.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for one agent-gate process."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    agent_model: str = Field(default="claude-haiku-5-5", alias="AGENT_MODEL")
    improver_model: str = Field(default="claude-sonnet-5-5", alias="IMPROVER_MODEL")
    judge_model: str = Field(default="claude-haiku-5-5", alias="JUDGE_MODEL")

    agent_temperature: float = Field(default=0.7, alias="AGENT_TEMPERATURE")
    improver_temperature: float = Field(default=0.3, alias="IMPROVER_TEMPERATURE")
    judge_temperature: float = Field(default=0.0, alias="JUDGE_TEMPERATURE")

    max_spend_usd: float = Field(default=15.0, alias="MAX_SPEND_USD")
    runs_per_case: int = Field(default=3, alias="RUNS_PER_CASE")
    gate_runs_per_case: int = Field(default=5, alias="GATE_RUNS_PER_CASE")
    max_concurrency: int = Field(default=8, alias="MAX_CONCURRENCY")

    attempt_timeout_s: float = Field(default=60.0, alias="ATTEMPT_TIMEOUT_S")
    max_tokens: int = Field(default=1024, alias="AGENT_MAX_TOKENS")
