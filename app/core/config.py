from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_name: str = "wave"
    environment: str = "development"
    debug: bool = True

    # Postgres
    postgres_dsn: str = "postgresql+asyncpg://wave:wave@localhost:5432/wave"
    db_pool_size: int = 20
    db_max_overflow: int = 10

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Message queue keys
    queue_prefix: str = "wave:queue"

    # Worker pool + autoscaling (knobs hardcoded here for now)
    min_workers: int = 2
    max_workers: int = 32              # hard ceiling on spun-up workers
    scale_up_queue_depth: int = 20     # high-lane backlog that triggers scale-up
    scale_up_age_s: float = 2.0        # oldest-job age (s) that triggers scale-up
    scale_down_idle_s: float = 30.0    # idle duration before a worker is retired
    monitor_interval_s: float = 1.0    # how often the manager evaluates scaling
    worker_poll_timeout_s: float = 2.0 # blocking dequeue timeout per worker loop

    # Worker heartbeats
    heartbeat_interval_s: float = 3.0
    heartbeat_ttl_s: float = 10.0      # workers stale past this are pruned


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
