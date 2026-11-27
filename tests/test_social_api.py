import pytest

from social_api import DryRunAPI, GitHubAPI, GitHubAPIError
from fakes import FakeAPI

TOKEN = "ghp_user_secret_value_1234"
REPO_TOKEN = "ghs_repo_secret_value_5678"


class FakeResponse:
    def __init__(self, status=200, json_data=None, headers=None, next_url=None):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}
        self.links = {"next": {"url": next_url}} if next_url else {}

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, **kwargs):
        self.calls.append((method, url, headers, kwargs))
        return self.responses.pop(0)


def make_api(responses):
    session = FakeSession(responses)
    return GitHubAPI(TOKEN, REPO_TOKEN, "me/follow-bot", session=session), session


def test_requires_tokens():
    with pytest.raises(GitHubAPIError):
        GitHubAPI("", REPO_TOKEN, "me/x")
    with pytest.raises(GitHubAPIError):
        GitHubAPI(TOKEN, REPO_TOKEN, "no-slash")


def test_paginates_followers_with_user_token():
    api, session = make_api([
        FakeResponse(json_data=[{"login": "a"}, {"login": "b"}], next_url="https://api.github.com/user/followers?page=2"),
        FakeResponse(json_data=[{"login": "c"}]),
    ])
    assert api.list_followers() == {"a", "b", "c"}
    assert session.calls[0][2]["Authorization"] == f"Bearer {TOKEN}"
    assert session.calls[1][3]["params"] is None


def test_issues_use_repo_token():
    api, session = make_api([FakeResponse(201, {"number": 7})])
    assert api.create_issue("t", "b", ["x"]) == 7
    method, url, headers, _ = session.calls[0]
    assert method == "POST" and url.endswith("/repos/me/follow-bot/issues")
    assert headers["Authorization"] == f"Bearer {REPO_TOKEN}"


def test_error_message_never_contains_token():
    api, _ = make_api([FakeResponse(401, {"message": "Bad credentials"})])
    with pytest.raises(GitHubAPIError) as exc:
        api.get_authenticated_login()
    assert exc.value.status == 401
    assert TOKEN not in str(exc.value) and "Bad credentials" in str(exc.value)


def test_long_rate_limit_stops_instead_of_waiting(monkeypatch):
    monkeypatch.setattr("social_api.time.time", lambda: 1000)
    api, _ = make_api([FakeResponse(403, {}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "5000"})])
    with pytest.raises(GitHubAPIError, match="Limite"):
        api.list_following()


def test_retries_server_errors(monkeypatch):
    monkeypatch.setattr("social_api.time.sleep", lambda s: None)
    api, session = make_api([FakeResponse(502), FakeResponse(200, {"login": "me"})])
    assert api.get_authenticated_login() == "me"
    assert len(session.calls) == 2


def test_dry_run_wrapper_blocks_unfollow_but_reads():
    inner = FakeAPI()
    inner.following = {"x"}
    api = DryRunAPI(inner)
    api.unfollow("x")
    assert inner.unfollow_calls == [] and api.simulated_unfollows == ["x"]
    assert api.list_following() == {"x"}
