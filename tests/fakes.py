"""API falsa em memória para testes (nenhuma chamada de rede)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from social_api import Comment, GitHubAPIError, Issue, UserProfile

NOW = datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)


def profile(login: str, followers: int = 100, following: int = 110, repos: int = 10,
            type_: str = "User", location: str = "Lisboa", bio: str = "dev") -> UserProfile:
    return UserProfile(login, type_, followers, following, repos, bio, location, f"https://github.com/{login}")


class FakeAPI:
    def __init__(self, me: str = "me"):
        self.me = me
        self.following: set[str] = set()
        self.followers: set[str] = set()
        self.profiles: dict[str, UserProfile] = {}
        self.activity: dict[str, datetime | None] = {}
        self.search_results: list[str] = []
        self.issues: dict[int, dict] = {}
        self.comments: dict[int, list[Comment]] = {}
        self.labels: set[str] = set()
        self.unfollow_calls: list[str] = []
        self.unfollow_errors: dict[str, GitHubAPIError] = {}
        self.search_calls = 0
        self._next_issue = 1

    def add_candidate(self, p: UserProfile, active_days_ago: float | None = 1) -> None:
        self.profiles[p.login] = p
        self.activity[p.login] = None if active_days_ago is None else NOW - timedelta(days=active_days_ago)
        self.search_results.append(p.login)

    # conta
    def get_authenticated_login(self) -> str:
        return self.me

    def list_followers(self) -> set[str]:
        return set(self.followers)

    def list_following(self) -> set[str]:
        return set(self.following)

    def unfollow(self, login: str) -> None:
        if login in self.unfollow_errors:
            raise self.unfollow_errors[login]
        self.unfollow_calls.append(login)
        self.following.discard(login)

    # descoberta
    def search_users(self, query: str, sort: str, page: int, per_page: int) -> list[str]:
        self.search_calls += 1
        return list(self.search_results)

    def get_user(self, login: str) -> UserProfile:
        if login not in self.profiles:
            raise GitHubAPIError("not found", 404)
        return self.profiles[login]

    def last_public_activity(self, login: str) -> datetime | None:
        return self.activity.get(login)

    # issues
    def create_issue(self, title: str, body: str, labels: Iterable[str]) -> int:
        number = self._next_issue
        self._next_issue += 1
        self.issues[number] = {"title": title, "body": body, "labels": list(labels), "state": "open"}
        self.comments[number] = []
        return number

    def get_issue(self, number: int) -> Issue:
        data = self.issues[number]
        return Issue(number, data["state"], data["body"])

    def list_issue_comments(self, number: int) -> list[Comment]:
        return list(self.comments.get(number, []))

    def comment_issue(self, number: int, body: str) -> None:
        self.comments.setdefault(number, []).append(Comment("github-actions[bot]", body, NOW))

    def update_issue(self, number: int, *, body: str | None = None, state: str | None = None) -> None:
        if body is not None:
            self.issues[number]["body"] = body
        if state is not None:
            self.issues[number]["state"] = state

    def ensure_label(self, name: str, color: str, description: str) -> None:
        self.labels.add(name)

    # ajudante de teste
    def user_comments(self, number: int, body: str, author: str | None = None, at: datetime | None = None) -> None:
        self.comments.setdefault(number, []).append(Comment(author or self.me, body, at or NOW))
