import pytest
from trials.config import Config

BASE = {"SEERR_URL": "http://seerr:5055", "SEERR_KEY": "sk", "SONARR_URL": "http://sonarr:8989",
        "SONARR_KEY": "k", "JELLYFIN_URL": "http://jellyfin:8096", "JELLYFIN_KEY": "jk",
        "JELLYFIN_PUBLIC_URL": "https://jellyfin.example"}


def test_defaults():
    c = Config.from_env(BASE)
    assert (c.trials_per_week, c.trial_episodes, c.window_days, c.arrival_days) == (3, 3, 21, 14)
    assert c.min_free_tb == 1.0 and c.max_deletes_per_run == 3 and c.enforce is False
    assert c.trials_root == "/data/media/trials" and c.port == 8080


def test_overrides_and_types():
    c = Config.from_env(dict(BASE, TRIALS_PER_WEEK="5", MIN_FREE_TB="0.5", TRIALS_ENFORCE="true"))
    assert c.trials_per_week == 5 and c.min_free_tb == 0.5 and c.enforce is True


def test_enforce_false_values():
    for v in ("0", "false", "no", "off"):
        assert Config.from_env(dict(BASE, TRIALS_ENFORCE=v)).enforce is False


def test_missing_required():
    with pytest.raises(ValueError, match="SONARR_KEY"):
        Config.from_env({k: v for k, v in BASE.items() if k != "SONARR_KEY"})
