"""Validated service limits and explicit local launch modes."""
from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    mode: str = "stub"
    host: str = "127.0.0.1"
    port: int = 8791
    body_limit: int = 131072
    queue_capacity: int = 8
    owner_waiting: int = 2
    bucket_capacity: float = 2
    refill_per_minute: float = 5
    queue_seconds: float = 180
    prompt_cap: int = 6144
    output_cap: int = 1024
    safety_tokens: int = 512
    idle_seconds: float = 900
    event_retention: int = 128
    shutdown_token: str = ""
    pairing_dir: Path | None = None

    @classmethod
    def from_environment(cls, mode: str):
        if mode not in {"stub", "live", "local", "network"}:
            raise ValueError("invalid_mode")
        host = os.environ.get("APP_BIND_HOST", "0.0.0.0" if mode == "network" else "127.0.0.1")
        if host not in ({"127.0.0.1", "0.0.0.0"} if mode == "network" else {"127.0.0.1"}):
            raise ValueError("invalid_bind_host")
        def number(key, default, minimum, maximum):
            value = int(os.environ.get(key, str(default)))
            if not minimum <= value <= maximum:
                raise ValueError("invalid_service_limit")
            return value
        root = Path(__file__).resolve().parents[2]
        data = Path(os.environ.get("APP_DATA_DIR", str(root / ".runtime" / ("stub" if mode == "stub" else "service")))).resolve()
        return cls(data, mode, host, number("APP_PORT", 8791, 1024, 65535),
                   queue_capacity=number("APP_QUEUE_CAPACITY", 8, 1, 64),
                   owner_waiting=number("APP_OWNER_WAITING", 2, 1, 8),
                   prompt_cap=number("APP_PROMPT_CAP", 6144, 128, 6144),
                   output_cap=number("APP_OUTPUT_CAP", 1024, 1, 1024),
                   shutdown_token=os.environ.get("APP_SHUTDOWN_TOKEN", ""),
                   pairing_dir=Path(os.environ.get("APP_PAIRING_DIR",str(data/"pairing" if mode=="stub" else root/".runtime/pairing"))).resolve())
