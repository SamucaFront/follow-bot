import logging
import random
from dataclasses import replace
from datetime import timedelta

import pytest

from config import Config, SuggestionConfig, UnfollowConfig
from fakes import NOW, FakeAPI, profile
from main import RedactSecrets, run
from storage import Storage

CONFIG = Config(suggestions=SuggestionConfig(per_day=2, hour_utc=12), unfollow=UnfollowConfig(wait_days=7))


def test_disabled_bot_does_nothing(tmp_path):
    api = FakeAPI()
    summary = run(replace(CONFIG, enabled=False), api, Storage(tmp_path), now=NOW)
    assert summary.status == "disabled"
    assert not (tmp_path / "state.json").exists()


def test_end_to_end_week(tmp_path):
    """Dia 1: sugere. Você segue. Dia 9: abre revisão. Você aprova. Bot remove."""
    api = FakeAPI()
    api.following = {"oldfriend"}
    for name in ("ana", "bruno", "chen"):
        api.add_candidate(profile(name))
    storage = Storage(tmp_path)
    rng = random.Random(0)

    s1 = run(CONFIG, api, storage, now=NOW, rng=rng)
    assert s1.status == "ok" and s1.suggestion_issue == 1
    suggested = list(storage.load()["suggested"])
    assert len(suggested) == 2

    # Você segue as duas sugestões manualmente; uma segue de volta.
    api.following |= set(suggested)
    api.followers = {suggested[0]}
    s2 = run(CONFIG, api, storage, now=NOW + timedelta(hours=1), rng=rng)
    assert sorted(s2.new_following) == sorted(suggested)
    assert s2.new_followers == [suggested[0]]

    later = NOW + timedelta(days=9)
    s3 = run(CONFIG, api, storage, now=later, rng=rng)
    review = storage.load()["pending_review"]
    assert s3.review_issue == review["issue"]
    assert review["logins"] == [suggested[1]]  # "oldfriend" é pré-existente, fica de fora

    api.user_comments(review["issue"], "/aprovar", at=later)
    s4 = run(CONFIG, api, storage, now=later + timedelta(minutes=5), rng=rng, manual=True)
    assert s4.unfollowed == [suggested[1]]
    assert suggested[1] not in api.following
    assert "oldfriend" in api.following


def test_respects_interval_unless_manual(tmp_path):
    api = FakeAPI()
    storage = Storage(tmp_path)
    run(CONFIG, api, storage, now=NOW)
    assert run(CONFIG, api, storage, now=NOW + timedelta(minutes=20)).status == "skipped"
    assert run(CONFIG, api, storage, now=NOW + timedelta(minutes=20), manual=True).status == "ok"


def test_dry_run_never_unfollows(tmp_path):
    api = FakeAPI()
    storage = Storage(tmp_path)
    config = replace(CONFIG, dry_run=True)
    run(config, api, storage, now=NOW)
    api.following.add("x")
    run(config, api, storage, now=NOW + timedelta(hours=1))
    later = NOW + timedelta(days=8)
    run(config, api, storage, now=later)
    review = storage.load()["pending_review"]
    assert api.issues[review["issue"]]["title"].startswith("[SIMULAÇÃO]")
    api.user_comments(review["issue"], "/aprovar", at=later)
    summary = run(config, api, storage, now=later, manual=True)
    assert summary.unfollowed == ["x"]   # simulado
    assert api.unfollow_calls == []      # nada de verdade
    assert "x" in api.following


def test_wrong_account_is_rejected(tmp_path):
    storage = Storage(tmp_path)
    run(CONFIG, FakeAPI(me="me"), storage, now=NOW)
    with pytest.raises(RuntimeError, match="conta"):
        run(CONFIG, FakeAPI(me="someone-else"), storage, now=NOW, manual=True)


def test_failing_phase_keeps_sync_and_reports_partial(tmp_path):
    api = FakeAPI()
    api.following = {"a"}

    def broken(*args, **kwargs):
        raise RuntimeError("search down")

    api.search_users = broken
    api.get_user = broken
    api.add_candidate(profile("x"))
    storage = Storage(tmp_path)
    summary = run(CONFIG, api, storage, now=NOW)
    # A busca falhar não quebra a execução: só não há sugestões hoje.
    assert summary.status in {"ok", "partial"}
    assert "a" in storage.load()["following"]


def test_redact_filter_hides_tokens(caplog):
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "token=%s", ("ghp_supersecret123",), None)
    RedactSecrets(["ghp_supersecret123"]).filter(record)
    assert record.getMessage() == "token=***"
