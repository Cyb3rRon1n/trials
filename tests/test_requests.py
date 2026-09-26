from datetime import timedelta
from trials import state
from trials.jobs import Clients, daily_decide, iso
from fakes import FakeJellyfin, FakeNtfy, FakeRadarr, FakeSeerr, FakeSonarr, NOW, ep, make_cfg

T = "/data/media/trials"


def world(tmp_path, **kw):
    cfg = make_cfg(tmp_path, **kw)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy(), FakeRadarr())
    return cfg, c, state.empty()


def requested_show(c, sid=300, tvdb=7001, tmdb=71, aired=3, extra_future=1):
    c.sonarr.add_existing(sid, tvdb, "Asked For", path=f"{T}/Asked For")
    c.sonarr.series_db[sid]["tmdbId"] = tmdb
    c.seerr.details[tmdb] = {"genres": [{"name": "Drama"}], "originCountry": ["US"]}
    c.sonarr.eps[sid] = [{"id": sid * 10 + i, "seasonNumber": 1, "episodeNumber": i, "monitored": True, "hasFile": False,
                          "airDateUtc": iso(NOW - timedelta(days=30 - i) if i <= aired else NOW + timedelta(days=7))}
                         for i in range(1, aired + extra_future + 1)]
    return sid


def arrive_all(c, sid):
    for e in c.sonarr.eps[sid]:
        if e["airDateUtc"] <= iso(NOW):
            e["hasFile"] = True


def test_only_things_in_the_trials_folder_are_adopted(tmp_path):
    cfg, c, st = world(tmp_path)
    requested_show(c)
    c.sonarr.add_existing(301, 7002, "Normal Request")            # /data/media/tv - never judged
    daily_decide(cfg, c, st, NOW)
    assert st["shows"]["7001"]["kind"] == "request" and "7002" not in st["shows"]
    assert ("tag", 300) in c.sonarr.calls


def test_request_opens_when_every_aired_episode_is_in_then_keeps_without_touching_monitoring(tmp_path):
    cfg, c, st = world(tmp_path)
    sid = requested_show(c)
    c.seerr.requested.add(71)                                    # it IS a request - must not auto-keep
    daily_decide(cfg, c, st, NOW)
    rec = st["shows"]["7001"]
    assert rec["window_start"] is None and ("search", (3001, 3002, 3003)) in c.sonarr.calls
    arrive_all(c, sid)
    c.jellyfin.index[f"{T}/Asked For"] = {"Id": "jf7"}
    daily_decide(cfg, c, st, NOW)
    assert rec["window_start"] == iso(NOW)
    c.jellyfin.like[("jf7", "u1")] = True
    daily_decide(cfg, c, st, NOW + timedelta(days=22))
    assert ("move", sid, "/data/media/tv") in c.sonarr.calls
    assert ("monitor_all", sid) not in c.sonarr.calls and rec["status"] == "moving"


def test_slow_request_is_never_dropped(tmp_path):
    cfg, c, st = world(tmp_path)
    sid = requested_show(c)
    for d in (0, 20, 40):
        daily_decide(cfg, c, st, NOW + timedelta(days=d))
    assert sid in c.sonarr.series_db and st["shows"]["7001"]["status"] == "active"


def test_whole_request_votes_count_any_episode(tmp_path):
    cfg, c, st = world(tmp_path)
    sid = requested_show(c, aired=6, extra_future=0)
    arrive_all(c, sid)
    c.jellyfin.index[f"{T}/Asked For"] = {"Id": "jf7"}
    daily_decide(cfg, c, st, NOW)
    c.jellyfin.eps[("jf7", "u1")] = [ep(f"e{i}", i, played=True) for i in range(1, 4)]    # finished 3 of 6
    c.jellyfin.eps[("jf7", "u2")] = [ep("e1", 1, ticks=500)]                           # gave up in ep 1
    for u in ("u1", "u2"):
        c.jellyfin.eps.setdefault(("jf7", u), [])
    c.jellyfin.eps[("jf7", "u2")] += [ep(f"x{i}", i) for i in range(2, 7)]
    lines = daily_decide(cfg, c, st, NOW + timedelta(days=22))
    assert any("KEPT Asked For (1 like / 1 dislike)" in l for l in lines)             # tie keeps


def test_admin_override_decides_immediately(tmp_path):
    cfg, c, st = world(tmp_path)
    sid = requested_show(c)
    daily_decide(cfg, c, st, NOW)
    st["shows"]["7001"]["override"] = {"verdict": "drop", "by": "bobby"}
    lines = daily_decide(cfg, c, st, NOW)
    assert ("delete", sid, True) in c.sonarr.calls and any("overruled by bobby" in l for l in lines)


def test_movie_request_trial_keep_and_drop(tmp_path):
    cfg, c, st = world(tmp_path)
    c.radarr.add(40, 555, "Good Film", f"{cfg.trials_movies_root}/Good Film (2026)")
    c.radarr.add(41, 556, "Bad Film", f"{cfg.trials_movies_root}/Bad Film (2026)")
    c.radarr.add(42, 557, "Normal Film", "/data/media/movies/Normal Film (2026)")
    daily_decide(cfg, c, st, NOW)
    assert "movie:557" not in st["shows"] and st["shows"]["movie:555"]["media"] == "movie"
    c.jellyfin.movies = {f"{cfg.trials_movies_root}/Good Film (2026)": {"Id": "m1"},
                         f"{cfg.trials_movies_root}/Bad Film (2026)": {"Id": "m2"}}
    daily_decide(cfg, c, st, NOW)
    assert st["shows"]["movie:555"]["window_start"] == iso(NOW)
    c.jellyfin.udata[("m1", "u1")] = {"Played": True}                  # watched to the end, no vote -> keep
    c.jellyfin.like[("m2", "u1")] = False                              # explicit 👎
    daily_decide(cfg, c, st, NOW + timedelta(days=22))
    assert ("move", 40, cfg.movies_root) in c.radarr.calls and ("untag", 40) in c.radarr.calls
    assert ("delete", 41, True) in c.radarr.calls and 556 in st["rejected_movies"]
    assert st["shows"]["movie:555"]["status"] == "moving"


def test_kept_movie_gets_watched_status_back_after_the_move(tmp_path):
    cfg, c, st = world(tmp_path)
    c.radarr.add(40, 555, "Good Film", f"{cfg.trials_movies_root}/Good Film (2026)")
    daily_decide(cfg, c, st, NOW)
    c.jellyfin.movies = {f"{cfg.trials_movies_root}/Good Film (2026)": {"Id": "m1"}}
    daily_decide(cfg, c, st, NOW)
    c.jellyfin.udata[("m1", "u1")] = {"Played": True}
    c.jellyfin.udata[("m1", "u2")] = {"PlaybackPositionTicks": 900}
    c.jellyfin.like[("m1", "u2")] = True
    daily_decide(cfg, c, st, NOW + timedelta(days=22))
    rec = st["shows"]["movie:555"]
    assert rec["status"] == "moving"
    c.jellyfin.movies = {f"{cfg.movies_root}/Good Film (2026)": {"Id": "m9"}}      # Jellyfin re-found it
    lines = daily_decide(cfg, c, st, NOW + timedelta(days=23))
    assert ("played", "m9", "u1") in c.jellyfin.calls and ("pos", "m9", "u2", 900) in c.jellyfin.calls
    assert rec["status"] == "kept" and any("watched marks restored" in l for l in lines)


def test_movie_favorite_outvotes_two_thumbs_down(tmp_path):
    cfg, c, st = world(tmp_path)
    c.jellyfin.users_list.append({"Id": "u3", "Name": "palma"})
    c.radarr.add(40, 555, "Loved Film", f"{cfg.trials_movies_root}/Loved Film (2026)")
    daily_decide(cfg, c, st, NOW)
    c.jellyfin.movies = {f"{cfg.trials_movies_root}/Loved Film (2026)": {"Id": "m1"}}
    daily_decide(cfg, c, st, NOW)
    c.jellyfin.udata[("m1", "u1")] = {"IsFavorite": True}
    c.jellyfin.like[("m1", "u2")] = False
    c.jellyfin.like[("m1", "u3")] = False
    lines = daily_decide(cfg, c, st, NOW + timedelta(days=22))
    assert any("KEPT Loved Film (2026) (2 like / 2 dislike)" in l for l in lines)


def test_kept_title_keeps_likes_favorites_play_count_and_date(tmp_path):
    cfg, c, st = world(tmp_path)
    c.radarr.add(40, 555, "Good Film", f"{cfg.trials_movies_root}/Good Film (2026)")
    daily_decide(cfg, c, st, NOW)
    c.jellyfin.movies = {f"{cfg.trials_movies_root}/Good Film (2026)": {"Id": "m1"}}
    daily_decide(cfg, c, st, NOW)
    c.jellyfin.udata[("m1", "u1")] = {"Played": True, "PlayCount": 2, "LastPlayedDate": "2026-10-01T20:00:00Z", "IsFavorite": True}
    c.jellyfin.like[("m1", "u2")] = True
    daily_decide(cfg, c, st, NOW + timedelta(days=22))
    c.jellyfin.movies = {f"{cfg.movies_root}/Good Film (2026)": {"Id": "m9"}}
    daily_decide(cfg, c, st, NOW + timedelta(days=23))
    assert ("m9", "u1", True, 0, 2, "2026-10-01T20:00:00Z") in c.jellyfin.restored     # count + date back
    assert ("fav", "m9", "u1", True) in c.jellyfin.calls                                  # ♥ back
    assert ("like", "m9", "u2", True) in c.jellyfin.calls                                 # 👍 back
    assert st["shows"]["movie:555"]["status"] == "kept" and st["shows"]["movie:555"]["taste"] is None
