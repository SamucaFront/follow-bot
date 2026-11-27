from datetime import timedelta

import pytest

from config import ConfigError, LimitsConfig, SuggestionConfig, load_config
from fakes import NOW
from scheduler import is_run_due, is_suggestion_due, operations_left, record_operation
from storage import Storage, empty_state, to_iso


# ---------------------------------------------------------------- config
def test_defaults_without_file(tmp_path):
    cfg = load_config(tmp_path / "missing.toml", env={})
    assert cfg.enabled and not cfg.dry_run
    assert cfg.suggestions.per_day == 20
    assert cfg.limits.max_accounts_per_run == 20


def test_shipped_config_is_valid():
    from pathlib import Path
    cfg = load_config(Path(__file__).resolve().parent.parent / "config.toml", env={})
    assert cfg.suggestions.per_day == 20 and cfg.suggestions.languages == ()


def test_reads_file_and_env_overrides(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[bot]\nenabled = true\n[suggestions]\nper_day = 10\nlanguages = ["Python"]\n'
        '[unfollow]\nkeep = ["@Torvalds"]\n',
        encoding="utf-8",
    )
    cfg = load_config(path, env={"BOT_ENABLED": "false", "DRY_RUN": "true"})
    assert cfg.enabled is False
    assert cfg.dry_run is True
    assert cfg.suggestions.per_day == 10
    assert cfg.suggestions.languages == ("Python",)
    assert cfg.unfollow.keep == frozenset({"torvalds"})


def test_empty_env_values_are_ignored(tmp_path):
    cfg = load_config(tmp_path / "x.toml", env={"BOT_ENABLED": "", "DRY_RUN": ""})
    assert cfg.enabled is True and cfg.dry_run is False


@pytest.mark.parametrize("snippet", [
    "[suggestions]\nper_day = 500",          # acima do teto
    "[limits]\nmax_operations_per_day = 999",
    "[limits]\nmax_accounts_per_run = 0",
    "[suggestions]\nmin_ratio = 4.0\nmax_ratio = 1.0",
])
def test_hard_limits_are_enforced(tmp_path, snippet):
    path = tmp_path / "config.toml"
    path.write_text(snippet, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, env={})


def test_invalid_env_bool(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "x.toml", env={"BOT_ENABLED": "talvez"})


# --------------------------------------------------------------- storage
def test_storage_roundtrip_and_defaults(tmp_path):
    storage = Storage(tmp_path / "data")
    assert storage.load() == empty_state()
    state = empty_state()
    state["following"]["alice"] = {"followed_at": to_iso(NOW), "source": "manual"}
    storage.save(state)
    loaded = storage.load()
    assert loaded["following"]["alice"]["source"] == "manual"
    assert list((tmp_path / "data").glob(".state-*")) == []  # sem temporários esquecidos


def test_storage_adds_new_keys_to_old_files(tmp_path):
    (tmp_path / "state.json").write_text('{"version": 1, "following": {}}', encoding="utf-8")
    loaded = Storage(tmp_path).load()
    assert "daily_ops" in loaded and loaded["history"] == []


def test_storage_caps_history(tmp_path):
    storage = Storage(tmp_path)
    state = empty_state()
    state["history"] = [{"n": i} for i in range(1500)]
    storage.save(state)
    assert len(storage.load()["history"]) == 1000


# ------------------------------------------------------------- scheduler
def test_run_due_respects_interval():
    limits = LimitsConfig(run_interval_hours=1)
    state = {"last_run_at": to_iso(NOW - timedelta(minutes=30))}
    assert not is_run_due(state, limits, NOW, manual=False)
    assert is_run_due(state, limits, NOW, manual=True)
    state["last_run_at"] = to_iso(NOW - timedelta(minutes=55))  # dentro da tolerância do cron
    assert is_run_due(state, limits, NOW, manual=False)
    assert is_run_due({}, limits, NOW, manual=False)


def test_suggestion_due_once_per_day_after_hour():
    cfg = SuggestionConfig(hour_utc=12)
    assert is_suggestion_due({}, cfg, NOW)
    assert not is_suggestion_due({"last_suggestion_date": "2026-11-27"}, cfg, NOW)
    assert not is_suggestion_due({}, cfg, NOW.replace(hour=11))


def test_daily_operations_reset_each_day():
    limits = LimitsConfig(max_operations_per_day=5)
    state = {}
    record_operation(state, NOW, 3)
    assert operations_left(state, limits, NOW) == 2
    assert operations_left(state, limits, NOW + timedelta(days=1)) == 5
