"""Простые print-логи в консоль (uvicorn)."""
from __future__ import annotations

from datetime import datetime


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[NefrLearn {ts}] {msg}", flush=True)
