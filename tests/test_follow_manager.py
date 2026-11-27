import random
from datetime import timedelta

import social_api
from config import SuggestionConfig
from fakes import NOW, FakeAPI, profile
from follow_manager import (
    build_queries,
    evaluate,
    publish_daily_suggestions,
    render_suggestion_issue,
    sync_relationships,
    Candidate,
)
from storage import empty_state, to_iso

CFG = SuggestionConfig(per_day=3, candidates_to_inspect=50)


def test_module_has_no_follow_function():
    # Seguir automaticamente viola as políticas do GitHub; o cliente não deve oferecer isso.
    assert not hasattr(social_api.GitHubAPI, "follow")


def test_first_sync_marks_existing_follows_as_preexisting():
    api = FakeAPI()
    api.following = {"old1", "old2"}
    api.followers = {"old1"}
    state = empty_state()
    result = sync_relationships(api, state, NOW)
    assert result.first_run
    assert {v["source"] for v in state["following"].values()} == {"preexisting"}
    assert result.new_followers == []  # na primeira vez não "inventa" novos seguidores
    assert state["initialized"]


def test_later_sync_detects_follows_unfollows_and_followers():
    api = FakeAPI()
    state = empty_state()
    api.following = {"a"}
    sync_relationships(api, state, NOW)
    state["suggested"]["sug"] = {"suggested_at": to_iso(NOW), "issue": 1, "score": 50}

    api.following = {"sug", "hand"}       # deixou de seguir "a", seguiu duas
    api.followers = {"fan"}
    result = sync_relationships(api, state, NOW + timedelta(hours=1))
    assert state["following"]["sug"]["source"] == "suggestion"
    assert state["following"]["hand"]["source"] == "manual"
    assert result.removed_following == ["a"]
    assert state["unfollowed"]["a"]["by"] == "manual"
    assert result.new_followers == ["fan"]


def test_queries_have_no_location_and_vary():
    queries = build_queries(SuggestionConfig(languages=("Go",)), random.Random(1), NOW, count=6)
    assert all("location:" not in q for q, _, _ in queries)
    assert all("language:Go" in q and "type:user" in q for q, _, _ in queries)
    assert len({q for q, _, _ in queries}) > 1


def test_evaluate_filters():
    recent = NOW - timedelta(days=1)
    assert evaluate(profile("ok"), recent, CFG, NOW) is not None
    assert evaluate(profile("org", type_="Organization"), recent, CFG, NOW) is None
    assert evaluate(profile("famous", followers=4000, following=20), recent, CFG, NOW) is None  # não segue de volta
    assert evaluate(profile("idle"), NOW - timedelta(days=60), CFG, NOW) is None
    assert evaluate(profile("ok"), None, CFG, NOW) is None
    assert evaluate(profile("empty", repos=0), recent, CFG, NOW) is None


def test_evaluate_prefers_balanced_and_active():
    balanced = evaluate(profile("b", followers=200, following=200), NOW - timedelta(days=1), CFG, NOW)
    skewed = evaluate(profile("s", followers=200, following=550), NOW - timedelta(days=1), CFG, NOW)
    stale = evaluate(profile("t", followers=200, following=200), NOW - timedelta(days=12), CFG, NOW)
    assert balanced > skewed
    assert balanced > stale


def test_publish_creates_issue_without_mentions_and_records_suggestions():
    api = FakeAPI()
    for i in range(6):
        api.add_candidate(profile(f"dev{i}", followers=100 + i, following=100 + i))
    api.add_candidate(profile("me"))                      # eu mesmo
    api.add_candidate(profile("renovate-bot"))            # bot
    api.add_candidate(profile("ghost"), active_days_ago=None)
    state = empty_state()
    state["following"]["dev0"] = {"followed_at": to_iso(NOW), "source": "manual"}  # já sigo

    number = publish_daily_suggestions(api, state, CFG, "me", NOW, random.Random(0), dry_run=False)

    assert number == 1
    issue = api.issues[1]
    assert "sugestoes" in issue["labels"]
    suggested = set(state["suggested"])
    assert len(suggested) == 3
    assert not suggested & {"me", "renovate-bot", "ghost", "dev0"}
    assert "@" not in issue["body"].replace("@​", "")  # ninguém é mencionado
    assert state["last_suggestion_date"] == "2026-11-27"


def test_publish_closes_previous_issue_and_skips_already_suggested():
    api = FakeAPI()
    for i in range(4):
        api.add_candidate(profile(f"dev{i}"))
    state = empty_state()
    first = publish_daily_suggestions(api, state, CFG, "me", NOW, random.Random(0), dry_run=False)
    second = publish_daily_suggestions(api, state, CFG, "me", NOW + timedelta(days=1), random.Random(0), dry_run=False)
    assert api.issues[first]["state"] == "closed"
    assert len(api.issues[second]["body"].splitlines()) < len(api.issues[first]["body"].splitlines())


def test_publish_with_no_candidates_creates_no_issue():
    api = FakeAPI()
    state = empty_state()
    assert publish_daily_suggestions(api, state, CFG, "me", NOW, random.Random(0), dry_run=False) is None
    assert api.issues == {}
    assert state["last_suggestion_date"] == "2026-11-27"


def test_render_marks_simulation_and_escapes_bio():
    c = Candidate(profile("x", bio="hi @someone | pipes"), NOW, 80.0)
    title, body = render_suggestion_issue([c], NOW, dry_run=True)
    assert title.startswith("[SIMULAÇÃO]")
    assert "@​someone" in body and "\\|" in body
