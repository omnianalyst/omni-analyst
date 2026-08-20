import pytest
from pydantic import ValidationError

from omni.config import Settings


def test_target_hit_rate_default_is_preserved(monkeypatch):
    monkeypatch.delenv("TARGET_HIT_RATE", raising=False)

    assert Settings(_env_file=None).target_hit_rate == 0.6


def test_target_hit_rate_is_loaded_from_the_environment(monkeypatch):
    monkeypatch.setenv("TARGET_HIT_RATE", "0.73")

    assert Settings(_env_file=None).target_hit_rate == 0.73


@pytest.mark.parametrize("value", ["0", "1"])
def test_target_hit_rate_accepts_inclusive_boundaries(monkeypatch, value):
    monkeypatch.setenv("TARGET_HIT_RATE", value)

    assert Settings(_env_file=None).target_hit_rate == float(value)


@pytest.mark.parametrize(
    ("value", "error_type"),
    [
        pytest.param("-0.01", "greater_than_equal", id="negative"),
        pytest.param("1.01", "less_than_equal", id="above-one"),
        pytest.param("NaN", "finite_number", id="nan"),
        pytest.param("inf", "finite_number", id="infinity"),
    ],
)
def test_target_hit_rate_rejects_invalid_environment_values(
    monkeypatch, value, error_type
):
    monkeypatch.setenv("TARGET_HIT_RATE", value)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    assert exc_info.value.errors()[0]["loc"] == ("target_hit_rate",)
    assert exc_info.value.errors()[0]["type"] == error_type


def test_profile_defaults_to_solo_depth(monkeypatch):
    monkeypatch.delenv("OMNI_PROFILE", raising=False)

    profile = Settings(_env_file=None)

    assert profile.omni_profile == "solo"
    assert profile.backfill_lookback_days == 365
    assert profile.polygon_history_days == 365
    assert profile.autonomous_top_sectors == 4


def test_full_profile_is_the_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("OMNI_PROFILE", "full")

    profile = Settings(_env_file=None)

    assert profile.omni_profile == "full"
    assert profile.backfill_lookback_days == 730
    assert profile.polygon_history_days == 730
    assert profile.autonomous_top_sectors == 11


def test_profile_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("OMNI_PROFILE", "enterprise")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    assert exc_info.value.errors()[0]["loc"] == ("omni_profile",)
