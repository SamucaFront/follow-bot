from dataclasses import replace
from datetime import timedelta

from config import Config, LimitsConfig, UnfollowConfig
from fakes import NOW, FakeAPI
from social_api import GitHubAPIError
from storage import empty_state, to_iso
from unfollow_manager import find_non_reciprocal, open_review_if_needed, parse_checked, process_review

CONFIG = Config(unfollow=UnfollowConfig(wait_days=7), limits=LimitsConfig(max_accounts_per_run=20, max_operations_per_day=20))


def make_state(api: FakeAPI, entries: dict[str, tuple[int, str]]) -> dict:
    """entries: login -> (dias desde que segui, origem)."""
    state = empty_state()
    state["initialized"] = True
    for login, (days, source) in entries.items():
        state["following"][login] = {"followed_at": to_iso(NOW - timedelta(days=days)), "source": source}
        api.following.add(login)
    state["followers"] = sorted(api.followers)
    return state


def test_find_non_reciprocal_rules():
    api = FakeAPI()
    api.followers = {"mutual"}
    state = make_state(api, {
        "old": (10, "suggestion"), "older": (20, "manual"), "fresh": (2, "suggestion"),
        "mutual": (30, "suggestion"), "legacy": (99, "preexisting"), "keepme": (30, "manual"),
    })
    config = replace(CONFIG, unfollow=UnfollowConfig(wait_days=7, keep=frozenset({"keepme"})))
    assert find_non_reciprocal(state, config, NOW) == ["older", "old"]  # mais antigos primeiro

    config = replace(config, unfollow=replace(config.unfollow, include_preexisting=True))
    assert find_non_reciprocal(state, config, NOW)[0] == "legacy"


def test_parse_checked():
    body = "- [x] [`alice`](https://github.com/alice): x\n- [ ] [`bob`](u)\n- [X] [`carol-1`](u)\ntexto"
    assert parse_checked(body) == ["alice", "carol-1"]


def test_full_approval_flow():
    api = FakeAPI()
    state = make_state(api, {"a": (10, "suggestion"), "b": (10, "suggestion"), "c": (10, "suggestion")})
    number = open_review_if_needed(api, state, CONFIG, NOW)
    assert number is not None and state["pending_review"]["logins"] == ["a", "b", "c"]

    # Sem aprovação nada acontece.
    assert process_review(api, state, CONFIG, "me", NOW) == []
    assert api.unfollow_calls == []

    # Desmarca "b" e aprova.
    api.issues[number]["body"] = api.issues[number]["body"].replace("- [x] [`b`]", "- [ ] [`b`]")
    api.user_comments(number, "/aprovar")
    removed = process_review(api, state, CONFIG, "me", NOW)

    assert removed == ["a", "c"]
    assert api.unfollow_calls == ["a", "c"]
    assert "b" in state["following"] and "a" not in state["following"]
    assert state["unfollowed"]["a"]["by"] == "bot"
    assert api.issues[number]["state"] == "closed"
    assert state["pending_review"] is None


def test_only_owner_comments_count_and_only_after_issue_creation():
    api = FakeAPI()
    state = make_state(api, {"a": (10, "manual")})
    number = open_review_if_needed(api, state, CONFIG, NOW)
    api.user_comments(number, "/aprovar", author="stranger")
    api.user_comments(number, "/aprovar", at=NOW - timedelta(days=1))
    assert process_review(api, state, CONFIG, "me", NOW) == []
    assert api.unfollow_calls == []


def test_cannot_inject_accounts_by_editing_issue():
    api = FakeAPI()
    state = make_state(api, {"a": (10, "manual")})
    api.following.add("victim")
    state["following"]["victim"] = {"followed_at": to_iso(NOW), "source": "manual"}
    number = open_review_if_needed(api, state, CONFIG, NOW)
    api.issues[number]["body"] += "\n- [x] [`victim`](u)"
    api.user_comments(number, "/aprovar")
    assert process_review(api, state, CONFIG, "me", NOW) == ["a"]
    assert "victim" in api.following


def test_skips_people_who_followed_back_meanwhile():
    api = FakeAPI()
    state = make_state(api, {"a": (10, "manual"), "b": (10, "manual")})
    number = open_review_if_needed(api, state, CONFIG, NOW)
    state["followers"] = ["b"]  # b seguiu de volta antes da aprovação
    api.user_comments(number, "/aprovar")
    assert process_review(api, state, CONFIG, "me", NOW) == ["a"]
    assert "b" in api.following


def test_daily_limit_spreads_work_over_runs():
    api = FakeAPI()
    state = make_state(api, {f"u{i}": (10 + i, "manual") for i in range(5)})
    config = replace(CONFIG, limits=LimitsConfig(max_accounts_per_run=2, max_operations_per_day=3))
    number = open_review_if_needed(api, state, config, NOW)
    api.user_comments(number, "/aprovar")

    assert len(process_review(api, state, config, "me", NOW)) == 2              # limite por execução
    assert len(process_review(api, state, config, "me", NOW + timedelta(hours=1))) == 1  # limite diário
    assert process_review(api, state, config, "me", NOW + timedelta(hours=2)) == []
    assert api.issues[number]["state"] == "open"
    assert len(process_review(api, state, config, "me", NOW + timedelta(days=1))) == 2   # dia seguinte
    assert api.issues[number]["state"] == "closed"
    assert state["daily_ops"]["count"] == 2


def test_cancel_closes_without_unfollowing():
    api = FakeAPI()
    state = make_state(api, {"a": (10, "manual")})
    number = open_review_if_needed(api, state, CONFIG, NOW)
    api.user_comments(number, "/cancelar")
    assert process_review(api, state, CONFIG, "me", NOW) == []
    assert api.issues[number]["state"] == "closed"
    assert state["pending_review"] is None and api.unfollow_calls == []


def test_closing_issue_manually_discards_review():
    api = FakeAPI()
    state = make_state(api, {"a": (10, "manual")})
    number = open_review_if_needed(api, state, CONFIG, NOW)
    api.issues[number]["state"] = "closed"
    process_review(api, state, CONFIG, "me", NOW)
    assert state["pending_review"] is None and api.unfollow_calls == []


def test_deleted_account_is_skipped_and_other_errors_retry():
    api = FakeAPI()
    state = make_state(api, {"gone": (10, "manual"), "flaky": (11, "manual")})
    api.unfollow_errors = {"gone": GitHubAPIError("nf", 404), "flaky": GitHubAPIError("boom", 502)}
    number = open_review_if_needed(api, state, CONFIG, NOW)
    api.user_comments(number, "/aprovar")
    process_review(api, state, CONFIG, "me", NOW)
    assert state["pending_review"]["done"] == ["gone"]
    assert api.issues[number]["state"] == "open"

    del api.unfollow_errors["flaky"]
    assert process_review(api, state, CONFIG, "me", NOW + timedelta(hours=1)) == ["flaky"]
    assert state["pending_review"] is None


def test_no_review_when_nobody_qualifies():
    api = FakeAPI()
    state = make_state(api, {"fresh": (1, "manual")})
    assert open_review_if_needed(api, state, CONFIG, NOW) is None
    assert api.issues == {}
