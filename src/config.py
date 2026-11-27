"""Carrega e valida a configuração do bot (config.toml + variáveis de ambiente)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

# Tetos fixos: mesmo que alguém aumente os valores no config.toml, o bot não
# passa destes limites. Eles mantêm o volume num nível humano e dentro das
# políticas do GitHub.
HARD_MAX_SUGGESTIONS_PER_DAY = 30
HARD_MAX_OPERATIONS_PER_DAY = 50
HARD_MAX_ACCOUNTS_PER_RUN = 20


class ConfigError(ValueError):
    """Configuração inválida."""


@dataclass(frozen=True)
class SuggestionConfig:
    per_day: int = 20
    hour_utc: int = 12
    languages: tuple[str, ...] = ()
    min_followers: int = 10
    max_followers: int = 5000
    min_following: int = 10
    min_ratio: float = 0.5
    max_ratio: float = 3.0
    active_within_days: int = 14
    min_public_repos: int = 3
    candidates_to_inspect: int = 80


@dataclass(frozen=True)
class UnfollowConfig:
    wait_days: int = 7
    max_per_review: int = 40
    include_preexisting: bool = False
    keep: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class LimitsConfig:
    max_accounts_per_run: int = 20
    max_operations_per_day: int = 20
    run_interval_hours: float = 1.0


@dataclass(frozen=True)
class Config:
    enabled: bool = True
    dry_run: bool = False
    suggestions: SuggestionConfig = field(default_factory=SuggestionConfig)
    unfollow: UnfollowConfig = field(default_factory=UnfollowConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)


def _env_bool(value: str | None) -> bool | None:
    if value is None or value.strip() == "":
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "sim"}:
        return True
    if normalized in {"0", "false", "no", "off", "nao", "não"}:
        return False
    raise ConfigError(f"Valor booleano inválido: {value!r}")


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _build(raw: Mapping[str, Any]) -> Config:
    bot = raw.get("bot", {})
    sug = raw.get("suggestions", {})
    unf = raw.get("unfollow", {})
    lim = raw.get("limits", {})

    suggestions = SuggestionConfig(
        per_day=int(sug.get("per_day", 20)),
        hour_utc=int(sug.get("hour_utc", 12)),
        languages=tuple(str(x) for x in sug.get("languages", [])),
        min_followers=int(sug.get("min_followers", 10)),
        max_followers=int(sug.get("max_followers", 5000)),
        min_following=int(sug.get("min_following", 10)),
        min_ratio=float(sug.get("min_ratio", 0.5)),
        max_ratio=float(sug.get("max_ratio", 3.0)),
        active_within_days=int(sug.get("active_within_days", 14)),
        min_public_repos=int(sug.get("min_public_repos", 3)),
        candidates_to_inspect=int(sug.get("candidates_to_inspect", 80)),
    )
    unfollow = UnfollowConfig(
        wait_days=int(unf.get("wait_days", 7)),
        max_per_review=int(unf.get("max_per_review", 40)),
        include_preexisting=bool(unf.get("include_preexisting", False)),
        keep=frozenset(str(x).lower().lstrip("@") for x in unf.get("keep", [])),
    )
    limits = LimitsConfig(
        max_accounts_per_run=int(lim.get("max_accounts_per_run", 20)),
        max_operations_per_day=int(lim.get("max_operations_per_day", 20)),
        run_interval_hours=float(lim.get("run_interval_hours", 1.0)),
    )

    _check(1 <= suggestions.per_day <= HARD_MAX_SUGGESTIONS_PER_DAY,
           f"suggestions.per_day deve estar entre 1 e {HARD_MAX_SUGGESTIONS_PER_DAY}")
    _check(0 <= suggestions.hour_utc <= 23, "suggestions.hour_utc deve estar entre 0 e 23")
    _check(0 <= suggestions.min_followers <= suggestions.max_followers,
           "suggestions.min_followers deve ser <= max_followers")
    _check(0 < suggestions.min_ratio <= suggestions.max_ratio,
           "suggestions.min_ratio deve ser > 0 e <= max_ratio")
    _check(suggestions.active_within_days >= 1, "suggestions.active_within_days deve ser >= 1")
    _check(10 <= suggestions.candidates_to_inspect <= 200,
           "suggestions.candidates_to_inspect deve estar entre 10 e 200")
    _check(unfollow.wait_days >= 1, "unfollow.wait_days deve ser >= 1")
    _check(1 <= unfollow.max_per_review <= 100, "unfollow.max_per_review deve estar entre 1 e 100")
    _check(1 <= limits.max_accounts_per_run <= HARD_MAX_ACCOUNTS_PER_RUN,
           f"limits.max_accounts_per_run deve estar entre 1 e {HARD_MAX_ACCOUNTS_PER_RUN}")
    _check(1 <= limits.max_operations_per_day <= HARD_MAX_OPERATIONS_PER_DAY,
           f"limits.max_operations_per_day deve estar entre 1 e {HARD_MAX_OPERATIONS_PER_DAY}")
    _check(limits.run_interval_hours > 0, "limits.run_interval_hours deve ser > 0")

    return Config(
        enabled=bool(bot.get("enabled", True)),
        dry_run=bool(bot.get("dry_run", False)),
        suggestions=suggestions,
        unfollow=unfollow,
        limits=limits,
    )


def load_config(path: str | Path = "config.toml", env: Mapping[str, str] | None = None) -> Config:
    """Lê o config.toml e aplica as variáveis BOT_ENABLED e DRY_RUN por cima."""
    env = os.environ if env is None else env
    path = Path(path)
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    config = _build(raw)

    enabled = _env_bool(env.get("BOT_ENABLED"))
    dry_run = _env_bool(env.get("DRY_RUN"))
    if enabled is not None or dry_run is not None:
        config = Config(
            enabled=config.enabled if enabled is None else enabled,
            dry_run=config.dry_run if dry_run is None else dry_run,
            suggestions=config.suggestions,
            unfollow=config.unfollow,
            limits=config.limits,
        )
    return config
