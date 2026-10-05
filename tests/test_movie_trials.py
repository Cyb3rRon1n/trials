"""weekly new-movie trials: digitally released in the last new_days, judged like movie requests"""
from datetime import timedelta
from trials import state
from trials.jobs import Clients, daily_decide, iso, movie_release, recent, weekly_add_movies
from fakes import FakeJellyfin, FakeNtfy, FakeRadarr, FakeSeerr, FakeSonarr, NOW, make_cfg


def world(tmp_path, **kw):
    cfg = make_cfg(tmp_path, **kw)
    return cfg, Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy(), FakeRadarr()), state.empty()


def test_release_is_digital_else_physical_earliest_anywhere():
    rel = lambda *ds: {"releases": {"results": [{"release_dates": [{"type": t, "release_date": d} for t, d in ds]}]}}
    assert movie_release(rel((3, "2026-07-01T00:00:00.000Z"), (4, "2026-09-25T00:00:00.000Z"),
                             (4, "2026-09-20T00:00:00.000Z"), (5, "2026-09-01T00:00:00.000Z"))) == "2026-09-20T00:00:00.000Z"
    assert movie_release(rel((5, "2026-09-01T00:00:00.000Z"))) == "2026-09-01T00:00:00.000Z"
    assert movie_release(rel((3, "2026-09-01T00:00:00.000Z"))) is None and movie_release({}) is None
    assert recent("2026-09-25T00:00:00.000Z", NOW, 30) and recent("2026-09-25", NOW, 30)
    assert not recent("2026-08-01T00:00:00.000Z", NOW, 30) and not recent("2026-10-09", NOW, 30) and not recent(None, NOW, 30)


def test_only_newly_released_movies_are_added(tmp_path):
    cfg, c, st = world(tmp_path, movies_per_week=10)
    c.seerr.add_film(1, "Fresh")                                      # digital 10 days ago
    c.seerr.add_film(2, "Old", digital_days_ago=60)
    c.seerr.add_film(3, "Not Yet", digital_days_ago=-5)
    c.seerr.add_film(4, "Cinemas Only", digital_days_ago=None)
    c.radarr.lookups[4] = {"tmdbId": 4, "title": "Cinemas Only", "year": 2026}  # Radarr knows no date either
    c.seerr.add_film(5, "Radarr Knows", digital_days_ago=None, popular=True)
    c.radarr.lookups[5] = {"tmdbId": 5, "title": "Radarr Knows", "year": 2026,
                           "digitalRelease": iso(NOW - timedelta(days=3))}
    c.seerr.add_film(6, "Blu-ray Only", digital_days_ago=None, physical_days_ago=5)
    lines = weekly_add_movies(cfg, c, st, NOW)
    assert sorted(st["shows"]) == ["movie:1", "movie:5", "movie:6"]
    rec = st["shows"]["movie:1"]
    assert {k: rec[k] for k in ("tmdb", "title", "media", "kind", "dest", "status", "window_start")} == \
        {"tmdb": 1, "title": "Film 1 (2026)", "media": "movie", "kind": "auto", "dest": cfg.movies_root,
         "status": "active", "window_start": None}
    assert ("add", rec["radarr_id"], 1, 4, cfg.trials_movies_root) in c.radarr.calls
    assert c.radarr.movies_db[rec["radarr_id"]]["tags"] == [1]
    assert "movie trial added: Film 1 (2026) -> Trials library" in lines


def test_weekly_movie_limit_counts_movies_only(tmp_path):
    cfg, c, st = world(tmp_path)                                      # movies_per_week = 2
    st["shows"]["1005"] = {"tvdb": 1005, "added_at": iso(NOW), "status": "active"}            # a show: not counted
    st["shows"]["movie:9"] = {"tmdb": 9, "media": "movie", "added_at": iso(NOW), "status": "active"}
    for i in (1, 2, 3):
        c.seerr.add_film(i, f"F{i}")
    weekly_add_movies(cfg, c, st, NOW)
    assert len([k for k in st["shows"] if k.startswith("movie:")]) == 2


def test_skips_movies_in_radarr_rejected_or_already_trialled(tmp_path):
    cfg, c, st = world(tmp_path, movies_per_week=10)
    for i in (1, 2, 3, 4):
        c.seerr.add_film(i, f"F{i}")
    c.radarr.add(99, 1, "F1", "/data/media/movies/F1 (2026)")          # already have it
    st["rejected_movies"] = [2]
    st["shows"]["movie:3"] = {"tmdb": 3, "media": "movie", "status": "kept", "added_at": iso(NOW - timedelta(days=40))}
    weekly_add_movies(cfg, c, st, NOW)
    assert [k for k in st["shows"] if k != "movie:3"] == ["movie:4"]


def test_dry_run_adds_nothing(tmp_path):
    cfg, c, st = world(tmp_path, enforce=False)
    c.seerr.add_film(1, "Fresh")
    lines = weekly_add_movies(cfg, c, st, NOW)
    assert st["shows"] == {} and c.radarr.movies_db == {} and not [x for x in c.radarr.calls if x[0] == "add"]
    assert lines == ["[dry-run] would add movie trial: Film 1 (2026)"]


def test_low_disk_space_adds_no_movies(tmp_path):
    cfg, c, st = world(tmp_path)
    c.radarr.free = 0.5e12
    c.seerr.add_film(1, "Fresh")
    lines = weekly_add_movies(cfg, c, st, NOW)
    assert st["shows"] == {} and "skipped weekly movie add" in lines[0]


def test_no_radarr_no_movies(tmp_path):
    cfg, c, st = world(tmp_path)
    c.radarr = None
    c.seerr.add_film(1, "Fresh")
    assert weekly_add_movies(cfg, c, st, NOW) == [] and st["shows"] == {}


def test_auto_movie_is_voted_on_then_kept_or_dropped(tmp_path):
    cfg, c, st = world(tmp_path)
    c.seerr.add_film(1, "Good")
    c.seerr.add_film(2, "Bad")
    weekly_add_movies(cfg, c, st, NOW)
    good, bad = st["shows"]["movie:1"], st["shows"]["movie:2"]
    for rec, jid in ((good, "m1"), (bad, "m2")):
        c.radarr.movies_db[rec["radarr_id"]]["hasFile"] = True
        c.jellyfin.movies[rec["path"]] = {"Id": jid}
    daily_decide(cfg, c, st, NOW)
    assert good["window_start"] == iso(NOW) and good["kind"] == "auto"      # not re-adopted as a request
    c.jellyfin.udata[("m1", "u1")] = {"Played": True}
    c.jellyfin.like[("m2", "u1")] = False
    lines = daily_decide(cfg, c, st, NOW + timedelta(days=22))
    assert ("move", good["radarr_id"], cfg.movies_root) in c.radarr.calls and good["status"] == "moving"
    assert ("delete", bad["radarr_id"], True) in c.radarr.calls and st["rejected_movies"] == [2]
    assert any(l.startswith("KEPT Film 1 (2026)") for l in lines)
