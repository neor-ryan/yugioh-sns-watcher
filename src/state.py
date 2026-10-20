"""state/state.json の読み書き。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "state.json"

DEFAULT_STATE = {
    "x_accounts": {},  # {ユーザー名: {"user_id": ..., "since_id": ...}}
    "site_seen_ids": [],
    "last_run": None,
}


def load(path: Path = STATE_PATH) -> dict:
    state = dict(DEFAULT_STATE)
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            state.update(json.loads(text))
    return state


def save(state: dict, path: Path = STATE_PATH) -> None:
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
