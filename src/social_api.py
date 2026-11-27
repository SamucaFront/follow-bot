"""Cliente da API oficial do GitHub (REST v3).

Este módulo, de propósito, NÃO tem função para seguir contas. As Políticas de
Uso Aceitável do GitHub proíbem seguir de forma automatizada ("rank abuse, such
as automated starring or following"). O bot só sugere perfis; quem segue é você,
manualmente. A única escrita na rede é deixar de seguir, e só depois da sua
aprovação numa issue.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Protocol

import requests

from storage import from_iso

log = logging.getLogger("follow-bot.api")

API_URL = "https://api.github.com"
USER_AGENT = "follow-bot (+https://github.com/topics/github-actions)"


class GitHubAPIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class UserProfile:
    login: str
    type: str
    followers: int
    following: int
    public_repos: int
    bio: str
    location: str
    html_url: str


@dataclass(frozen=True)
class Comment:
    author: str
    body: str
    created_at: datetime


@dataclass(frozen=True)
class Issue:
    number: int
    state: str
    body: str


class SocialAPI(Protocol):
    """O que o bot precisa da rede social. Facilita testes e simulação."""

    def get_authenticated_login(self) -> str: ...
    def list_followers(self) -> set[str]: ...
    def list_following(self) -> set[str]: ...
    def unfollow(self, login: str) -> None: ...
    def search_users(self, query: str, sort: str, page: int, per_page: int) -> list[str]: ...
    def get_user(self, login: str) -> UserProfile: ...
    def last_public_activity(self, login: str) -> datetime | None: ...
    def create_issue(self, title: str, body: str, labels: Iterable[str]) -> int: ...
    def get_issue(self, number: int) -> Issue: ...
    def list_issue_comments(self, number: int) -> list[Comment]: ...
    def comment_issue(self, number: int, body: str) -> None: ...
    def update_issue(self, number: int, *, body: str | None = None, state: str | None = None) -> None: ...
    def ensure_label(self, name: str, color: str, description: str) -> None: ...


class GitHubAPI:
    """Implementação real.

    - `user_token`: token pessoal (fine-grained) da SUA conta, com permissão
      "Followers: read and write". Usado para ler seguidores, buscar perfis e
      deixar de seguir.
    - `repo_token`: o GITHUB_TOKEN do workflow, usado para issues do repositório.
    """

    def __init__(
        self,
        user_token: str,
        repo_token: str,
        repo: str,
        session: requests.Session | None = None,
        base_url: str = API_URL,
        max_retries: int = 3,
    ):
        if not user_token:
            raise GitHubAPIError("Token pessoal ausente (secret GH_USER_TOKEN).")
        if not repo_token:
            raise GitHubAPIError("Token do repositório ausente (GITHUB_TOKEN).")
        if "/" not in repo:
            raise GitHubAPIError("GITHUB_REPOSITORY deve ter o formato dono/repositorio.")
        self._user_token = user_token
        self._repo_token = repo_token
        self.repo = repo
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.max_retries = max_retries

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, *, repo_scope: bool = False, **kwargs: Any) -> requests.Response:
        token = self._repo_token if repo_scope else self._user_token
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.request(method, url, headers=headers, timeout=30, **kwargs)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise GitHubAPIError(f"Falha de rede em {method} {path}: {exc.__class__.__name__}") from None
                time.sleep(2 ** attempt)
                continue

            if resp.status_code in (403, 429) and self._is_rate_limited(resp):
                wait = self._rate_limit_wait(resp)
                if wait is None or wait > 120 or attempt == self.max_retries:
                    raise GitHubAPIError(
                        f"Limite de requisições da API atingido em {method} {path}. Tentaremos na próxima execução.",
                        resp.status_code,
                    )
                log.warning("Limite da API atingido; aguardando %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < self.max_retries:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                # Nunca incluímos headers na mensagem: eles têm o token.
                message = ""
                try:
                    message = resp.json().get("message", "")
                except ValueError:
                    pass
                raise GitHubAPIError(f"{method} {path} retornou {resp.status_code}: {message}", resp.status_code)
            return resp
        raise GitHubAPIError(f"{method} {path} falhou após {self.max_retries} tentativas")

    @staticmethod
    def _is_rate_limited(resp: requests.Response) -> bool:
        return resp.headers.get("X-RateLimit-Remaining") == "0" or "Retry-After" in resp.headers

    @staticmethod
    def _rate_limit_wait(resp: requests.Response) -> int | None:
        if "Retry-After" in resp.headers:
            try:
                return int(resp.headers["Retry-After"])
            except ValueError:
                return None
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset:
            return max(1, int(reset) - int(time.time()) + 1)
        return None

    def _paginate(self, path: str, *, repo_scope: bool = False, **params: Any) -> list[Any]:
        items: list[Any] = []
        url: str | None = path
        query: dict[str, Any] | None = {"per_page": 100, **params}
        while url:
            resp = self._request("GET", url, repo_scope=repo_scope, params=query)
            items.extend(resp.json())
            url = resp.links.get("next", {}).get("url")
            query = None  # a URL "next" já traz os parâmetros
        return items

    # ----------------------------------------------------------------- conta
    def get_authenticated_login(self) -> str:
        return self._request("GET", "/user").json()["login"]

    def list_followers(self) -> set[str]:
        return {u["login"] for u in self._paginate("/user/followers")}

    def list_following(self) -> set[str]:
        return {u["login"] for u in self._paginate("/user/following")}

    def unfollow(self, login: str) -> None:
        self._request("DELETE", f"/user/following/{login}")

    # ------------------------------------------------------------- descoberta
    def search_users(self, query: str, sort: str, page: int, per_page: int) -> list[str]:
        resp = self._request(
            "GET", "/search/users",
            params={"q": query, "sort": sort, "order": "desc", "page": page, "per_page": per_page},
        )
        return [item["login"] for item in resp.json().get("items", [])]

    def get_user(self, login: str) -> UserProfile:
        data = self._request("GET", f"/users/{login}").json()
        return UserProfile(
            login=data["login"],
            type=data.get("type", "User"),
            followers=int(data.get("followers", 0)),
            following=int(data.get("following", 0)),
            public_repos=int(data.get("public_repos", 0)),
            bio=(data.get("bio") or "").strip(),
            location=(data.get("location") or "").strip(),
            html_url=data.get("html_url", f"https://github.com/{login}"),
        )

    def last_public_activity(self, login: str) -> datetime | None:
        events = self._request("GET", f"/users/{login}/events/public", params={"per_page": 1}).json()
        if not events:
            return None
        return from_iso(events[0]["created_at"].replace("Z", "+00:00"))

    # ---------------------------------------------------------------- issues
    def create_issue(self, title: str, body: str, labels: Iterable[str]) -> int:
        resp = self._request(
            "POST", f"/repos/{self.repo}/issues", repo_scope=True,
            json={"title": title, "body": body, "labels": list(labels)},
        )
        return int(resp.json()["number"])

    def get_issue(self, number: int) -> Issue:
        data = self._request("GET", f"/repos/{self.repo}/issues/{number}", repo_scope=True).json()
        return Issue(number=number, state=data["state"], body=data.get("body") or "")

    def list_issue_comments(self, number: int) -> list[Comment]:
        raw = self._paginate(f"/repos/{self.repo}/issues/{number}/comments", repo_scope=True)
        return [
            Comment(
                author=c["user"]["login"],
                body=c.get("body") or "",
                created_at=from_iso(c["created_at"].replace("Z", "+00:00")),
            )
            for c in raw
        ]

    def comment_issue(self, number: int, body: str) -> None:
        self._request("POST", f"/repos/{self.repo}/issues/{number}/comments", repo_scope=True, json={"body": body})

    def update_issue(self, number: int, *, body: str | None = None, state: str | None = None) -> None:
        payload: dict[str, Any] = {}
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        if payload:
            self._request("PATCH", f"/repos/{self.repo}/issues/{number}", repo_scope=True, json=payload)

    def ensure_label(self, name: str, color: str, description: str) -> None:
        try:
            self._request("GET", f"/repos/{self.repo}/labels/{name}", repo_scope=True)
        except GitHubAPIError as exc:
            if exc.status != 404:
                raise
            self._request(
                "POST", f"/repos/{self.repo}/labels", repo_scope=True,
                json={"name": name, "color": color, "description": description},
            )


class DryRunAPI:
    """Modo simulação: lê dados reais, mas não deixa de seguir ninguém."""

    def __init__(self, inner: SocialAPI):
        self._inner = inner
        self.simulated_unfollows: list[str] = []

    def unfollow(self, login: str) -> None:
        log.info("[SIMULAÇÃO] deixaria de seguir @%s", login)
        self.simulated_unfollows.append(login)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
