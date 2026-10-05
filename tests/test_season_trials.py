"""weekly picks since whole-season trials: brand-new shows on trial for all of season 1"""
from datetime import timedelta
from trials import state
from trials.jobs import Clients, daily_decide, iso, like_threshold, weekly_add
from fakes import FakeJellyfin, FakeNtfy, FakeSeerr, FakeSonarr, NOW, ep, make_cfg


def world(tmp_path, **kw):
    cfg = make_cfg(tmp_path, **kw)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy())
    st = state.empty()
    c.seerr.add_show(5, 1005, "New Show")
    c.sonarr.lookups[1005] = {"title": "New Show", "tvdbId": 1005}
    weekly_add(cfg, c, st, NOW)                  # E01-E04 aired, E05-E06 still to come
    rec = st["shows"]["1005"]
    return cfg, c, st, rec, rec["sonarr_id"]


def have(c, sid, *numbers):
    for e in c.sonarr.eps[sid]:
        if e["episodeNumber"] in numbers:
            e["hasFile"] = True


def voting_over(c, rec, sid):
    have(c, sid, 1, 2, 3, 4)
    rec["window_start"] = iso(NOW - timedelta(days=22))
    c.jellyfin.index[rec["path"]] = {"Id": "jf1"}


def test_window_opens_once_every_aired_episode_is_in_and_indexed(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    have(c, sid, 1, 2, 3)
    c.sonarr.calls.clear()
    daily_decide(cfg, c, st, NOW)
    assert rec["window_start"] is None and ("search", (sid * 100 + 4,)) in c.sonarr.calls
    have(c, sid, 4)                               # E05/E06 haven't aired: not waited for
    daily_decide(cfg, c, st, NOW)
    assert rec["window_start"] is None            # not indexed yet
    c.jellyfin.index[rec["path"]] = {"Id": "jf1"}
    lines = daily_decide(cfg, c, st, NOW)
    assert rec["window_start"] == iso(NOW) and "voting open for 21 days" in lines[0]


def test_slow_season_opens_after_arrival_days_with_what_arrived(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    have(c, sid, 1)
    c.jellyfin.index[rec["path"]] = {"Id": "jf1"}
    lines = daily_decide(cfg, c, st, NOW + timedelta(days=15))
    assert rec["window_start"] == iso(NOW + timedelta(days=15)) and "1/6 episodes arrived" in lines[0]


def test_nothing_arrived_after_arrival_days_is_dropped(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    lines = daily_decide(cfg, c, st, NOW + timedelta(days=15))
    assert ("delete", sid, False) in c.sonarr.calls and rec["status"] == "unavailable"
    assert any("never arrived" in l for l in lines)


def test_finished_threshold_is_three_or_everything_available():
    assert [like_threshold(3, n) for n in (0, 1, 2, 3, 10)] == [1, 1, 2, 3, 3]


def test_verdict_counts_any_season_1_episode(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    voting_over(c, rec, sid)
    eps = lambda *played: [ep(f"e{i}", i, played=i in played) for i in range(1, 5)]
    c.jellyfin.users_list.append({"Id": "u3", "Name": "palma"})
    c.jellyfin.eps[("jf1", "u1")] = eps(2, 3, 4)        # finished 3 (not E01-03): like
    c.jellyfin.eps[("jf1", "u2")] = eps(4)              # only finished 1: dislike
    c.jellyfin.eps[("jf1", "u3")] = eps()
    c.jellyfin.eps[("jf1", "u3")][3]["UserData"]["PlaybackPositionTicks"] = 5    # gave up in E04: dislike
    lines = daily_decide(cfg, c, st, NOW)
    assert any("REJECTED New Show (1 like / 2 dislike)" in l for l in lines)


def test_short_season_finished_counts_as_like(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    voting_over(c, rec, sid)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1, True), ep("e2", 2, True)]     # all that's in Jellyfin
    lines = daily_decide(cfg, c, st, NOW)
    assert any("KEPT New Show (1 like / 0 dislike)" in l for l in lines)


def test_keep_follows_future_seasons(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    voting_over(c, rec, sid)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1)]
    c.jellyfin.like[("jf1", "u1")] = True
    daily_decide(cfg, c, st, NOW)
    # monitor_all_and_search: every season monitored, monitorNewItems "all", searched (see test_clients)
    assert ("move", sid, "/data/media/tv") in c.sonarr.calls and ("monitor_all", sid) in c.sonarr.calls
    assert rec["status"] == "moving" and rec["played"]["u1"] == {"S01E01": {"played": False, "ticks": 0, "count": 0, "date": None}}


def test_drop_deletes_and_rejects(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    voting_over(c, rec, sid)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1)]
    c.jellyfin.like[("jf1", "u1")] = False
    daily_decide(cfg, c, st, NOW)
    assert ("delete", sid, True) in c.sonarr.calls and st["rejected"] == [1005]


def test_no_episodes_in_jellyfin_postpones(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    voting_over(c, rec, sid)
    lines = daily_decide(cfg, c, st, NOW)
    assert rec["status"] == "active" and any("decision postponed" in l for l in lines)


def test_episodes_monitored_by_the_trial_are_not_a_user_extension(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    have(c, sid, 1, 2, 3, 4)
    rec["window_start"] = iso(NOW)
    daily_decide(cfg, c, st, NOW)
    assert rec["status"] == "active"
    c.sonarr.eps[sid].append({"id": 7777, "seasonNumber": 2, "episodeNumber": 1, "monitored": True, "hasFile": False})
    rec["known_episode_ids"].append(7777)        # a known S2 episode someone switched on
    daily_decide(cfg, c, st, NOW)
    assert rec["status"] == "moving"
