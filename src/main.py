"""Ponto de entrada do follow-bot (roda a cada hora no GitHub Actions)."""
from __future__ import annotations

import argparse
import logging
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from config import Config, ConfigError, load_config
from follow_manager import publish_daily_suggestions, sync_relationships
from scheduler import is_review_due, is_run_due, is_suggestion_due, mark_run
from social_api import DryRunAPI, GitHubAPI, SocialAPI
from storage import Storage, utcnow
from unfollow_manager import open_review_if_needed, process_review

log = logging.getLogger("follow-bot")

TASKS = ("all", "sync", "suggest", "review")
SECRET_ENV_VARS = ("GH_USER_TOKEN", "GITHUB_TOKEN")


class RedactSecrets(logging.Filter):
    """Troca qualquer token que apareça numa mensagem de log por ***."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "***")
        record.msg, record.args = message, None
        return True


def setup_logging(log_file: str | None, secrets: list[str]) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    redact = RedactSecrets(secrets)
    for handler in handlers:
        handler.addFilter(redact)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


@dataclass
class RunSummary:
    status: str = "ok"  # ok | skipped | disabled | partial
    me: str | None = None
    dry_run: bool = False
    new_following: list[str] = field(default_factory=list)
    new_followers: list[str] = field(default_factory=list)
    lost_followers: list[str] = field(default_factory=list)
    unfollowed: list[str] = field(default_factory=list)
    suggestion_issue: int | None = None
    review_issue: int | None = None
    errors: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        mode = " (simulação)" if self.dry_run else ""
        lines = [f"## follow-bot: {self.status}{mode}", ""]
        if self.me:
            lines.append(f"- Conta: `{self.me}`")
        lines += [
            f"- Começou a seguir (detectado): {len(self.new_following)}",
            f"- Novos seguidores: {len(self.new_followers)}",
            f"- Deixaram de seguir você: {len(self.lost_followers)}",
            f"- Unfollows aprovados executados: {len(self.unfollowed)}",
        ]
        if self.suggestion_issue:
            lines.append(f"- Sugestões do dia: #{self.suggestion_issue}")
        if self.review_issue:
            lines.append(f"- Nova revisão de unfollow: #{self.review_issue}")
        for err in self.errors:
            lines.append(f"- ⚠️ {err}")
        return "\n".join(lines) + "\n"


def run(
    config: Config,
    api: SocialAPI,
    storage: Storage,
    *,
    task: str = "all",
    manual: bool = False,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> RunSummary:
    now = now or utcnow()
    rng = rng or random.Random()
    summary = RunSummary(dry_run=config.dry_run)

    if not config.enabled:
        log.info("Bot desativado (bot.enabled = false ou BOT_ENABLED=false). Nada a fazer.")
        summary.status = "disabled"
        return summary

    state = storage.load()
    if not is_run_due(state, config.limits, now, manual):
        log.info("Ainda não deu o intervalo de %sh desde a última execução.", config.limits.run_interval_hours)
        summary.status = "skipped"
        return summary

    if config.dry_run:
        api = DryRunAPI(api)

    me = api.get_authenticated_login()
    summary.me = me
    if state.get("me") and state["me"].lower() != me.lower():
        raise RuntimeError(
            f"O token é da conta {me}, mas o histórico é de {state['me']}. "
            "Use o token da conta certa ou apague o branch bot-data para recomeçar."
        )
    state["me"] = me

    def phase(name: str, fn: Callable[[], Any]) -> Any:
        """Uma etapa que falha não derruba as outras; o estado é salvo depois de cada uma."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - registramos e seguimos
            log.exception("Etapa '%s' falhou", name)
            summary.errors.append(f"{name}: {exc}")
            return None
        finally:
            storage.save(state)

    sync = phase("sincronização", lambda: sync_relationships(api, state, now))
    if sync is None:
        summary.status = "partial"
        return summary  # sem dados atualizados não dá para revisar nem sugerir com segurança
    summary.new_following = sync.new_following if not sync.first_run else []
    summary.new_followers = sync.new_followers
    summary.lost_followers = sync.lost_followers
    if sync.first_run:
        log.info("Primeira execução: %d seguindo, %d seguidores registrados.",
                 len(state["following"]), len(state["followers"]))

    if task in ("all", "review"):
        summary.unfollowed = phase("unfollow aprovado", lambda: process_review(api, state, config, me, now)) or []
        if is_review_due(state, now) and not sync.first_run:
            summary.review_issue = phase("abrir revisão", lambda: open_review_if_needed(api, state, config, now))

    if task in ("all", "suggest") and (task == "suggest" or is_suggestion_due(state, config.suggestions, now)):
        summary.suggestion_issue = phase(
            "sugestões",
            lambda: publish_daily_suggestions(api, state, config.suggestions, me, now, rng, config.dry_run),
        )

    mark_run(state, now)
    storage.save(state)
    if summary.errors:
        summary.status = "partial"
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="follow-bot: sugestões diárias e unfollow com aprovação")
    parser.add_argument("--task", choices=TASKS, default=os.environ.get("BOT_TASK") or "all")
    parser.add_argument("--manual", action="store_true", help="ignora o intervalo mínimo entre execuções")
    parser.add_argument("--config", default=os.environ.get("BOT_CONFIG", "config.toml"))
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data"))
    parser.add_argument("--log-file", default=os.environ.get("LOG_FILE", "logs/bot.log"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_file, [os.environ.get(name, "") for name in SECRET_ENV_VARS])

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("Configuração inválida: %s", exc)
        return 2

    try:
        api = GitHubAPI(
            user_token=os.environ.get("GH_USER_TOKEN", ""),
            repo_token=os.environ.get("GITHUB_TOKEN", ""),
            repo=os.environ.get("GITHUB_REPOSITORY", ""),
        )
        summary = run(config, api, Storage(args.data_dir), task=args.task, manual=args.manual)
    except Exception as exc:  # noqa: BLE001
        log.error("Execução falhou: %s", exc)
        return 1

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(summary.to_markdown())
    log.info("Resultado: %s", summary.status)
    return 1 if summary.status == "partial" else 0


if __name__ == "__main__":
    sys.exit(main())
