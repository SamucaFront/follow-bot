"""Persistência do estado do bot em JSON.

No GitHub Actions o arquivo fica no branch `bot-data` do próprio repositório,
que o workflow baixa antes de rodar e salva (commit + push) no final. Assim o
histórico sobrevive entre execuções sem servidor externo.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_HISTORY = 1000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def empty_state() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "initialized": False,
        "me": None,
        "last_run_at": None,
        # login -> {"followed_at": iso, "source": "suggestion"|"manual"|"preexisting"}
        "following": {},
        "followers": [],
        # login -> {"suggested_at": iso, "issue": int|None, "score": float}
        "suggested": {},
        # login -> {"unfollowed_at": iso, "by": "bot"|"manual"}
        "unfollowed": {},
        "last_suggestion_date": None,
        "last_suggestion_issue": None,
        "last_review_date": None,
        # {"issue": int, "created_at": iso, "logins": [...], "approved_at": iso|None, "done": [...]}
        "pending_review": None,
        "daily_ops": {"date": None, "count": 0},
        "history": [],
    }


class Storage:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.state_path = self.data_dir / "state.json"

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return empty_state()
        with self.state_path.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
        # Garante que chaves novas existam quando o formato evolui.
        merged = empty_state()
        merged.update(state)
        if merged.get("version", 0) > SCHEMA_VERSION:
            raise RuntimeError(
                f"state.json usa a versão {merged['version']}, mais nova que a suportada ({SCHEMA_VERSION})"
            )
        merged["version"] = SCHEMA_VERSION
        return merged

    def save(self, state: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        state["history"] = state.get("history", [])[-MAX_HISTORY:]
        # Escrita atômica: grava num temporário e troca, para nunca deixar JSON pela metade.
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.state_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def add_event(state: dict[str, Any], now: datetime, kind: str, **details: Any) -> None:
    state.setdefault("history", []).append({"at": to_iso(now), "event": kind, **details})
