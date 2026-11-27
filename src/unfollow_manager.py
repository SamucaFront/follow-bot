"""Encontra quem não seguiu de volta e deixa de seguir só depois da sua aprovação.

Fluxo:
1. Passado o prazo (`unfollow.wait_days`), quem não retribuiu entra numa issue
   de revisão, um por linha, todos marcados.
2. Você desmarca quem quer continuar seguindo e comenta `/aprovar`
   (ou `/cancelar` para descartar a lista).
3. O bot deixa de seguir os marcados, respeitando o limite por execução e o
   limite diário. Se sobrar alguém, continua nas próximas execuções.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from config import Config
from scheduler import operations_left, record_operation, today
from social_api import GitHubAPIError, SocialAPI
from storage import add_event, from_iso, to_iso

log = logging.getLogger("follow-bot.unfollow")

REVIEW_LABEL = "revisao-unfollow"
APPROVE_COMMANDS = ("/aprovar", "/approve")
CANCEL_COMMANDS = ("/cancelar", "/cancel")
CHECKBOX_RE = re.compile(r"^\s*[-*] \[([ xX])\] \[?`?([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))`?", re.MULTILINE)


def find_non_reciprocal(state: dict[str, Any], config: Config, now: datetime) -> list[str]:
    """Quem você segue há mais de `wait_days` e não te segue, mais antigos primeiro."""
    cfg = config.unfollow
    followers = {x.lower() for x in state.get("followers", [])}
    cutoff = now - timedelta(days=cfg.wait_days)
    result = []
    for login, info in state.get("following", {}).items():
        if login.lower() in followers or login.lower() in cfg.keep:
            continue
        if info.get("source") == "preexisting" and not cfg.include_preexisting:
            continue
        if from_iso(info["followed_at"]) <= cutoff:
            result.append((info["followed_at"], login))
    result.sort()
    return [login for _, login in result][: cfg.max_per_review]


def parse_checked(body: str) -> list[str]:
    return [m.group(2) for m in CHECKBOX_RE.finditer(body) if m.group(1).lower() == "x"]


def render_review_issue(logins: list[str], state: dict[str, Any], config: Config, now: datetime) -> tuple[str, str]:
    prefix = "[SIMULAÇÃO] " if config.dry_run else ""
    title = f"{prefix}Revisar unfollow: {today(now)} ({len(logins)} perfis)"
    lines = [
        f"Estes perfis não seguiram você de volta em {config.unfollow.wait_days} dia(s).",
        "",
        "**Como aprovar:**",
        "1. Desmarque quem você quer **continuar seguindo** (edite esta issue).",
        "2. Comente `/aprovar`. Para descartar a lista, comente `/cancelar`.",
        "",
        f"O bot deixa de seguir no máximo {config.limits.max_accounts_per_run} por execução e "
        f"{config.limits.max_operations_per_day} por dia. Quem seguir você de volta antes disso é pulado.",
        "",
    ]
    for login in logins:
        info = state["following"][login]
        days = int((now - from_iso(info["followed_at"])).total_seconds() // 86400)
        origin = {"suggestion": "sugestão do bot", "manual": "seguido manualmente",
                  "preexisting": "já seguia antes do bot"}.get(info.get("source"), "")
        lines.append(f"- [x] [`{login}`](https://github.com/{login}): seguindo há {days} dia(s) · {origin}")
    lines += ["", "---", "_Gerado automaticamente pelo follow-bot. Nada acontece sem o seu `/aprovar`._"]
    return title, "\n".join(lines)


def open_review_if_needed(api: SocialAPI, state: dict[str, Any], config: Config, now: datetime) -> int | None:
    state["last_review_date"] = today(now)
    logins = find_non_reciprocal(state, config, now)
    if not logins:
        log.info("Ninguém para revisar hoje.")
        return None
    title, body = render_review_issue(logins, state, config, now)
    api.ensure_label(REVIEW_LABEL, "d93f0b", "Lista de unfollow aguardando sua aprovação")
    number = api.create_issue(title, body, [REVIEW_LABEL])
    state["pending_review"] = {
        "issue": number, "created_at": to_iso(now), "logins": logins, "approved_at": None, "done": [],
    }
    add_event(state, now, "review_opened", issue=number, count=len(logins))
    log.info("Issue de revisão #%d criada com %d perfis", number, len(logins))
    return number


def _find_command(api: SocialAPI, review: dict[str, Any], me: str) -> str | None:
    """Último comando válido, só do dono da conta e depois da criação da issue."""
    created = from_iso(review["created_at"])
    command = None
    for comment in api.list_issue_comments(review["issue"]):
        if comment.author.lower() != me.lower() or comment.created_at < created:
            continue
        text = comment.body.strip().lower()
        if text.startswith(APPROVE_COMMANDS):
            command = "approve"
        elif text.startswith(CANCEL_COMMANDS):
            command = "cancel"
    return command


def process_review(api: SocialAPI, state: dict[str, Any], config: Config, me: str, now: datetime) -> list[str]:
    """Executa a revisão pendente, se aprovada. Retorna quem deixou de ser seguido."""
    review = state.get("pending_review")
    if not review:
        return []

    issue = api.get_issue(review["issue"])
    if not review.get("approved_at"):
        command = _find_command(api, review, me)
        if command == "cancel" or (command is None and issue.state == "closed"):
            if issue.state != "closed":
                api.comment_issue(review["issue"], "Lista cancelada. Ninguém foi removido.")
                api.update_issue(review["issue"], state="closed")
            add_event(state, now, "review_cancelled", issue=review["issue"])
            state["pending_review"] = None
            return []
        if command != "approve":
            log.info("Revisão #%d aguardando /aprovar", review["issue"])
            return []
        review["approved_at"] = to_iso(now)
        add_event(state, now, "review_approved", issue=review["issue"])
    elif issue.state == "closed":
        # Você fechou a issue no meio do caminho: paramos por aqui.
        add_event(state, now, "review_stopped", issue=review["issue"], done=len(review.get("done", [])))
        state["pending_review"] = None
        return []

    # Só quem estava na lista original E continua marcado. Isso impede que
    # alguém edite a issue e acrescente perfis que o bot não sugeriu.
    allowed = {x.lower(): x for x in review["logins"]}
    checked = [allowed[x.lower()] for x in parse_checked(issue.body) if x.lower() in allowed]
    done = set(review.get("done", []))
    followers = {x.lower() for x in state.get("followers", [])}
    queue = [x for x in checked if x not in done]

    budget = min(config.limits.max_accounts_per_run, operations_left(state, config.limits, now))
    removed: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for login in queue:
        if login.lower() in followers or login not in state.get("following", {}) or login.lower() in config.unfollow.keep:
            skipped.append(login)  # seguiu de volta, você já deixou de seguir, ou está protegido
            done.add(login)
            continue
        if len(removed) >= budget:
            break
        try:
            api.unfollow(login)
        except GitHubAPIError as exc:
            if exc.status == 404:  # conta apagada ou renomeada
                skipped.append(login)
                done.add(login)
                continue
            log.warning("Falha ao deixar de seguir %s: %s", login, exc)
            errors.append(login)
            continue
        removed.append(login)
        done.add(login)
        record_operation(state, now)
        if not config.dry_run:
            state["following"].pop(login, None)
            state.setdefault("unfollowed", {})[login] = {"unfollowed_at": to_iso(now), "by": "bot"}
        add_event(state, now, "unfollowed", login=login, dry_run=config.dry_run)

    review["done"] = sorted(done)
    remaining = [x for x in checked if x not in done]

    if removed or skipped or errors:
        verb = "Simulado (nada foi feito de verdade)" if config.dry_run else "Deixou de seguir"
        parts = []
        if removed:
            parts.append(f"{verb}: " + ", ".join(f"`{x}`" for x in removed))
        if skipped:
            parts.append("Pulados (seguiram de volta ou já não eram seguidos): " + ", ".join(f"`{x}`" for x in skipped))
        if errors:
            parts.append("Com erro, tento de novo depois: " + ", ".join(f"`{x}`" for x in errors))
        if remaining:
            parts.append(f"Faltam {len(remaining)}; continuo nas próximas execuções (limite diário).")
        api.comment_issue(review["issue"], "\n\n".join(parts))

    if not remaining:
        api.update_issue(review["issue"], state="closed")
        add_event(state, now, "review_completed", issue=review["issue"])
        state["pending_review"] = None
    return removed
