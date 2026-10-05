"""rec["tally"]: an anonymous running count during the vote, read from state.json by the announcer"""
from datetime import timedelta
from trials import state
from trials.jobs import Clients, daily_decide, iso, weekly_add, weekly_add_movies
from fakes import FakeJellyfin, FakeNtfy, FakeRadarr, FakeSeerr, FakeSonarr, NOW, ep, make_cfg


def world(tmp_path, **kw):
    cfg = make_cfg(tmp_path, **kw)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy(), FakeRadarr())
    st = state.empty()
    c.seerr.add_show(5, 1005, "New Show")
    c.sonarr.lookups[1005] = {"title": "New Show", "tvdbId": 1005}
    weekly_add(cfg, c, st, NOW)
    rec = st["shows"]["1005"]
    for e in c.sonarr.eps[rec["sonarr_id"]][:4]:
        e["hasFile"] = True
    c.jellyfin.index[rec["path"]] = {"Id": "jf1"}
    c.jellyfin.users_list.append({"Id": "u3", "Name": "palma"})
    c.jellyfin.udata[("jf1", "u1")] = {"IsFavorite": True}                          # ♥ = 2 keep
    c.jellyfin.like[("jf1", "u2")] = False                                         # 👎
    c.jellyfin.eps[("jf1", "u3")] = [ep(f"e{i}", i, played=True) for i in (1, 2, 3)]  # finished 3, no vote: keep
    return cfg, c, st, rec


def test_tally_during_the_window_counts_only(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    daily_decide(cfg, c, st, NOW)                     # voting opens today
    daily_decide(cfg, c, st, NOW + timedelta(days=1))
    assert rec["tally"] == {"keep": 3, "drop": 1, "voters": 3, "at": iso(NOW + timedelta(days=1))}
    assert rec["played"] is None                      # no snapshot taken during the window
    assert not any(u in str(rec["tally"]) for u in ("u1", "u2", "u3", "adriel", "bobby", "palma"))


def test_no_tally_before_voting_opens_or_after_it_ends(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    c.jellyfin.index = {}                             # not indexed: window can't open
    daily_decide(cfg, c, st, NOW)
    assert "tally" not in rec and rec["window_start"] is None
    rec["window_start"] = iso(NOW - timedelta(days=22))
    c.jellyfin.index[rec["path"]] = {"Id": "jf1"}
    daily_decide(cfg, c, st, NOW)                     # ended: decided, not tallied
    assert "tally" not in rec and rec["status"] == "moving"


def test_jellyfin_error_skips_the_tally_without_aborting(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    daily_decide(cfg, c, st, NOW)

    def boom(*a):
        raise RuntimeError("jellyfin down")
    c.jellyfin.season1_episodes = boom
    daily_decide(cfg, c, st, NOW + timedelta(days=1))
    assert "tally" not in rec and rec["status"] == "active"


def test_movies_are_tallied(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    c.seerr.add_film(1, "Fresh")
    weekly_add_movies(cfg, c, st, NOW)
    mrec = st["shows"]["movie:1"]
    c.radarr.movies_db[mrec["radarr_id"]]["hasFile"] = True
    c.jellyfin.movies[mrec["path"]] = {"Id": "m1"}
    c.jellyfin.like[("m1", "u2")] = True
    c.jellyfin.udata[("m1", "u3")] = {"PlaybackPositionTicks": 50}                # started, gave up: drop
    daily_decide(cfg, c, st, NOW)
    daily_decide(cfg, c, st, NOW + timedelta(days=1))
    assert mrec["tally"] == {"keep": 1, "drop": 1, "voters": 2, "at": iso(NOW + timedelta(days=1))}


def test_legacy_trials_use_their_fixed_episode_count(tmp_path):
    from test_jobs_decide import legacy_trial
    cfg = make_cfg(tmp_path)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy())
    st = state.empty()
    rec = legacy_trial(cfg, c, st, 5, 1005, "Old Trial", NOW - timedelta(days=5))
    rec["window_start"] = iso(NOW - timedelta(days=1))
    c.jellyfin.index[rec["path"]] = {"Id": "jf1"}
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1, True), ep("e2", 2, True), ep("e3", 3)]   # 2 of 3: drop
    daily_decide(cfg, c, st, NOW)
    assert rec["tally"] == {"keep": 0, "drop": 1, "voters": 1, "at": iso(NOW)}
