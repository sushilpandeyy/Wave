"""Runtime settings, all env-overridable.

Budget is a single number — W_MAX, the max total concurrent workers across all pools.
The autoscaler ramps toward demand within that ceiling, so cost stays in control.
"""

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # --- Budget (the one cost dial) -------------------------------------------
    w_max: int = _int("W_MAX", 32)            # max total concurrent workers
    enterprise_floor: int = _int("ENTERPRISE_FLOOR", 4)  # enterprise-only reserve
    standard_floor: int = _int("STANDARD_FLOOR", 4)      # always-warm general workers

    # --- Autoscaler -----------------------------------------------------------
    monitor_interval_s: float = _float("MONITOR_INTERVAL_S", 1.0)
    backlog_per_worker: float = _float("BACKLOG_PER_WORKER", 4.0)
    ramp_step: int = _int("RAMP_STEP", 4)     # max worker change per tick (gentle)

    # --- Pressure thresholds (a "how far over the line" ratio -> level 0..3) ---
    # Applied to both backlog/capacity and latency/target, so load and latency feed
    # the same pressure signal.
    pressure_l1: float = _float("PRESSURE_L1", 1.0)   # free starts degrading
    pressure_l2: float = _float("PRESSURE_L2", 2.0)   # premium degrades too
    pressure_l3: float = _float("PRESSURE_L3", 4.0)   # free shed at admission
    free_hard_cap: int = _int("FREE_HARD_CAP", 5000)  # memory bound on the free lane

    # --- Pool health inputs ---------------------------------------------------
    latency_target_s: float = _float("LATENCY_TARGET_S", 2.0)  # latency "line"
    error_circuit: float = _float("ERROR_CIRCUIT", 0.5)        # err rate that trips breaker

    # --- Worker ---------------------------------------------------------------
    worker_poll_timeout_s: float = _float("WORKER_POLL_TIMEOUT_S", 2.0)
    heartbeat_ttl_s: float = _float("HEARTBEAT_TTL_S", 10.0)
    workers_per_container: int = _int("WORKERS_PER_CONTAINER", 16)
    # Chance a general worker scans the lowest lane first, so it never starves.
    fairness: float = _float("FAIRNESS", 0.15)


settings = Settings()
