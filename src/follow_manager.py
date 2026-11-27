"""Sincroniza quem você segue e quem te segue, e monta as sugestões diárias.

O bot não segue ninguém: ele escolhe perfis engajados de qualquer lugar do
mundo, publica a lista numa issue e você segue manualmente. Na execução
seguinte a sincronização percebe quem você seguiu e começa a contar o prazo de
reciprocidade.
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from config import SuggestionConfig
from scheduler import today
from social_api import GitHubAPIError, SocialAPI, UserProfile
from storage import add_event, to_iso

log = logging.getLogger("follow-bot.follow")

SUGGESTION_LABEL = "sugestoes"
SORT_OPTIONS = ("joined", "followers", "repositories")
FIRST_SEARCH_YEAR = 2010


@dataclass
class SyncResult:
    new_following: list[str] = field(default_factory=list)
    removed_following: list[str] = field(default_factory=list)
    new_followers: list[str] = field(default_factory=list)
    lost_followers: list[str] = field(default_factory=list)
    first_run: bool = False


@dataclass(frozen=True)
class Candidate:
    profile: UserProfile
    last_active: datetime
    score: float


# ------------------------------------------------------------------ sincronia
def sync_relationships(api: SocialAPI, state: dict[str, Any], now: datetime) -> SyncResult:
    following = api.list_following()
    followers = api.list_followers()
    first_run = not state.get("initialized")
    result = SyncResult(first_run=first_run)

    known: dict[str, dict[str, Any]] = state.setdefault("following", {})
    for login in sorted(following - known.keys()):
        if first_run:
            source = "preexisting"  # já seguia antes de instalar o bot
        elif login in state.get("suggested", {}):
            source = "suggestion"
        else:
            source = "manual"
        known[login] = {"followed_at": to_iso(now), "source": source}
        result.new_following.append(login)

    for login in sorted(known.keys() - following):
        del known[login]
        result.removed_following.append(login)
        unfollowed = state.setdefault("unfollowed", {})
        if login not in unfollowed:
            unfollowed[login] = {"unfollowed_at": to_iso(now), "by": "manual"}

    previous = set(state.get("followers", []))
    if not first_run:
        result.new_followers = sorted(followers - previous)
        result.lost_followers = sorted(previous - followers)
    state["followers"] = sorted(followers)
    state["initialized"] = True

    if not first_run:
        for login in result.new_following:
            add_event(state, now, "followed", login=login, source=known[login]["source"])
        for login in result.new_followers:
            add_event(state, now, "new_follower", login=login)
        for login in result.lost_followers:
            add_event(state, now, "lost_follower", login=login)
    else:
        add_event(state, now, "initialized", following=len(following), followers=len(followers))
    return result


# ----------------------------------------------------------------- descoberta
def build_queries(cfg: SuggestionConfig, rng: random.Random, now: datetime, count: int = 4) -> list[tuple[str, str, int]]:
    """Gera buscas variadas. Sem filtro de localização: perfis do mundo inteiro.

    Variar o ano de criação da conta e a ordenação evita receber sempre os
    mesmos perfis famosos no topo da busca.
    """
    queries = []
    last_year = max(FIRST_SEARCH_YEAR, now.year - 1)
    for _ in range(count):
        parts = [
            "type:user",
            f"repos:>={cfg.min_public_repos}",
            f"followers:{cfg.min_followers}..{cfg.max_followers}",
        ]
        if cfg.languages:
            parts.append(f"language:{rng.choice(cfg.languages)}")
        year = rng.randint(FIRST_SEARCH_YEAR, last_year)
        parts.append(f"created:{year}-01-01..{year}-12-31")
        queries.append((" ".join(parts), rng.choice(SORT_OPTIONS), rng.randint(1, 5)))
    return queries


def evaluate(profile: UserProfile, last_active: datetime | None, cfg: SuggestionConfig, now: datetime) -> float | None:
    """Retorna uma nota de 0 a 100, ou None se o perfil não serve.

    "Engajado e que segue de volta" vira três sinais:
    - proporção seguindo/seguidores perto de 1 (costuma retribuir);
    - atividade pública recente (está usando o GitHub);
    - um mínimo de seguidores e repositórios (não é conta vazia).
    """
    if profile.type != "User":
        return None
    if not cfg.min_followers <= profile.followers <= cfg.max_followers:
        return None
    if profile.following < cfg.min_following or profile.public_repos < cfg.min_public_repos:
        return None
    ratio = profile.following / max(profile.followers, 1)
    if not cfg.min_ratio <= ratio <= cfg.max_ratio:
        return None
    if last_active is None:
        return None
    days_idle = (now - last_active).total_seconds() / 86400
    if days_idle > cfg.active_within_days:
        return None

    reciprocity = max(0.0, 1.0 - abs(math.log(ratio)))  # 1.0 quando segue tanto quanto é seguido
    recency = max(0.0, 1.0 - days_idle / cfg.active_within_days)
    size = min(1.0, math.log10(profile.followers + 1) / 3)
    return round(45 * reciprocity + 35 * recency + 20 * size, 2)


def _looks_like_bot(login: str) -> bool:
    lowered = login.lower()
    return lowered.endswith("bot") or lowered.endswith("[bot]") or "-bot-" in lowered


def discover_candidates(
    api: SocialAPI,
    state: dict[str, Any],
    cfg: SuggestionConfig,
    me: str,
    now: datetime,
    rng: random.Random,
) -> list[Candidate]:
    excluded = (
        {me.lower()}
        | {x.lower() for x in state.get("following", {})}
        | {x.lower() for x in state.get("suggested", {})}
        | {x.lower() for x in state.get("unfollowed", {})}
    )
    goal = cfg.per_day + cfg.per_day // 2  # junta um pouco a mais para escolher os melhores
    seen: set[str] = set()
    inspected = 0
    candidates: list[Candidate] = []

    for query, sort, page in build_queries(cfg, rng, now):
        if inspected >= cfg.candidates_to_inspect or len(candidates) >= goal:
            break
        try:
            logins = api.search_users(query, sort=sort, page=page, per_page=30)
        except GitHubAPIError as exc:
            log.warning("Busca falhou (%s); seguindo com as próximas", exc)
            continue
        rng.shuffle(logins)
        for login in logins:
            key = login.lower()
            if key in excluded or key in seen or _looks_like_bot(login):
                continue
            seen.add(key)
            if inspected >= cfg.candidates_to_inspect or len(candidates) >= goal:
                break
            inspected += 1
            try:
                profile = api.get_user(login)
                # Filtra antes de gastar uma chamada com eventos.
                if evaluate(profile, now, cfg, now) is None:
                    continue
                last_active = api.last_public_activity(login)
            except GitHubAPIError as exc:
                log.debug("Ignorando %s: %s", login, exc)
                continue
            score = evaluate(profile, last_active, cfg, now)
            if score is not None and last_active is not None:
                candidates.append(Candidate(profile, last_active, score))

    log.info("Perfis analisados: %d, aprovados: %d", inspected, len(candidates))
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[: cfg.per_day]


# ------------------------------------------------------------------- issues
def _escape(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text.replace("|", "\\|").replace("@", "@​")  # não notifica ninguém


def render_suggestion_issue(candidates: list[Candidate], now: datetime, dry_run: bool) -> tuple[str, str]:
    prefix = "[SIMULAÇÃO] " if dry_run else ""
    title = f"{prefix}Sugestões para seguir: {today(now)} ({len(candidates)} perfis)"
    lines = [
        "Perfis ativos, de qualquer lugar do mundo, que costumam seguir de volta.",
        "",
        "**Como usar:** abra cada perfil e clique em *Follow* se fizer sentido para você. "
        "Marque a caixa só para se organizar: o bot percebe sozinho quem você seguiu na próxima execução.",
        "",
    ]
    for c in candidates:
        p = c.profile
        idle = max(0, int((now - c.last_active).total_seconds() // 86400))
        active = "ativo hoje" if idle == 0 else f"ativo há {idle} dia(s)"
        place = f" · {_escape(p.location, 40)}" if p.location else ""
        bio = f"\n  > {_escape(p.bio)}" if p.bio else ""
        # Link sem @ para não mencionar (e notificar) a pessoa.
        lines.append(
            f"- [ ] [`{p.login}`]({p.html_url}): {p.followers} seguidores · segue {p.following} · "
            f"{p.public_repos} repos · {active}{place} · nota {c.score:.0f}{bio}"
        )
    lines += [
        "",
        "---",
        "_Gerado automaticamente pelo follow-bot. O bot não segue ninguém por você: "
        "seguir de forma automatizada viola as políticas do GitHub._",
    ]
    return title, "\n".join(lines)


def publish_daily_suggestions(
    api: SocialAPI,
    state: dict[str, Any],
    cfg: SuggestionConfig,
    me: str,
    now: datetime,
    rng: random.Random,
    dry_run: bool,
) -> int | None:
    candidates = discover_candidates(api, state, cfg, me, now, rng)
    state["last_suggestion_date"] = today(now)
    if not candidates:
        log.info("Nenhum perfil novo passou nos filtros hoje.")
        add_event(state, now, "suggestions_empty")
        return None

    previous = state.get("last_suggestion_issue")
    title, body = render_suggestion_issue(candidates, now, dry_run)
    api.ensure_label(SUGGESTION_LABEL, "0e8a16", "Perfis sugeridos para seguir")
    number = api.create_issue(title, body, [SUGGESTION_LABEL])

    if previous:
        try:
            api.comment_issue(previous, f"Substituída pela lista de hoje: #{number}")
            api.update_issue(previous, state="closed")
        except GitHubAPIError as exc:
            log.warning("Não consegui fechar a issue anterior #%s: %s", previous, exc)

    suggested = state.setdefault("suggested", {})
    for c in candidates:
        suggested[c.profile.login] = {"suggested_at": to_iso(now), "issue": number, "score": c.score}
    state["last_suggestion_issue"] = number
    add_event(state, now, "suggestions_published", issue=number, count=len(candidates))
    log.info("Issue #%d criada com %d sugestões", number, len(candidates))
    return number
