from trials import state
from trials.jobs import Clients, iso, weekly_add
from fakes import FakeJellyfin, FakeNtfy, FakeSeerr, FakeSonarr, NOW, make_cfg


def setup(tmp_path, **kw):
    cfg = make_cfg(tmp_path, **kw)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy())
    return cfg, c, state.empty()


def test_adds_top_n_eligible_and_skips_the_rest(tmp_path):
    cfg, c, st = setup(tmp_path, trials_per_week=2)
    c.seerr.add_show(1, 1001, "Already Have")          # already in Sonarr
    c.seerr.add_show(2, 1002, "Was Rejected")
    c.seerr.add_show(3, 1003, "Too New", last=(1, 2))   # only 2 aired
    c.seerr.add_show(4, 1004, "Anime One", genres=("Animation",), origin=("JP",))
    c.seerr.add_show(5, 1005, "Plain Show")
    c.seerr.add_show(6, 1006, "Third Eligible")
    for tvdb, t in [(1004, "Anime One"), (1005, "Plain Show"), (1006, "Third Eligible")]:
        c.sonarr.lookups[tvdb] = {"title": t, "tvdbId": tvdb}
    c.sonarr.add_existing(1, 1001, "Already Have")
    st["rejected"] = [1002]

    lines = weekly_add(cfg, c, st, NOW)

    assert sorted(st["shows"]) == ["1004", "1005"]
    rec = st["shows"]["1004"]
    assert rec["status"] == "active" and rec["dest"] == "/data/media/anime" and rec["window_start"] is None
    assert rec["path"] == "/data/media/trials/Anime One" and rec["setup_done"] is True
    assert st["shows"]["1005"]["dest"] == "/data/media/tv"
    sid = rec["sonarr_id"]
    mon = {e["episodeNumber"]: e["monitored"] for e in c.sonarr.eps[sid]}
    assert [n for n, m in mon.items() if m] == [1, 2, 3]
    assert ("search", (sid * 100 + 1, sid * 100 + 2, sid * 100 + 3)) in c.sonarr.calls
    assert len(lines) == 2 and "Anime One" in lines[0]


def test_skips_shows_already_in_state(tmp_path):
    cfg, c, st = setup(tmp_path)
    c.seerr.add_show(5, 1005, "Old Trial")
    c.sonarr.lookups[1005] = {"title": "Old Trial", "tvdbId": 1005}
    st["shows"]["1005"] = {"status": "kept"}
    assert weekly_add(cfg, c, st, NOW) == []


def test_low_disk_space_adds_nothing(tmp_path):
    cfg, c, st = setup(tmp_path)
    c.sonarr.free = 0.5e12
    c.seerr.add_show(5, 1005, "Plain Show")
    c.sonarr.lookups[1005] = {"title": "Plain Show", "tvdbId": 1005}
    lines = weekly_add(cfg, c, st, NOW)
    assert st["shows"] == {} and "skipped" in lines[0]


def test_weekly_add_respects_already_added_this_week(tmp_path):
    cfg, c, st = setup(tmp_path, trials_per_week=3)
    st["shows"]["9001"] = {"tvdb": 9001, "added_at": iso(NOW), "status": "active"}
    st["shows"]["9002"] = {"tvdb": 9002, "added_at": iso(NOW), "status": "active"}
    c.seerr.add_show(5, 1005, "Plain Show")
    c.seerr.add_show(6, 1006, "Second Show")
    c.sonarr.lookups[1005] = {"title": "Plain Show", "tvdbId": 1005}
    c.sonarr.lookups[1006] = {"title": "Second Show", "tvdbId": 1006}

    weekly_add(cfg, c, st, NOW)

    added = [k for k in st["shows"] if k not in ("9001", "9002")]
    assert len(added) == 1


def test_setup_deferred_when_sonarr_has_no_episodes_yet(tmp_path):
    cfg, c, st = setup(tmp_path)
    c.seerr.add_show(5, 1005, "Plain Show")
    c.sonarr.lookups[1005] = {"title": "Plain Show", "tvdbId": 1005}
    real_add = c.sonarr.add_series

    def add_without_eps(*a, **kw):
        s = real_add(*a, **kw)
        c.sonarr.eps[s["id"]] = []
        return s
    c.sonarr.add_series = add_without_eps
    weekly_add(cfg, c, st, NOW)
    assert st["shows"]["1005"]["setup_done"] is False


def test_anime_destination_adds_series_as_anime_type(tmp_path):
    cfg, c, st = setup(tmp_path)
    c.seerr.add_show(4, 1004, "Anime One", genres=("Animation",), origin=("JP",))
    c.seerr.add_show(5, 1005, "Plain Show")
    for tvdb, t in [(1004, "Anime One"), (1005, "Plain Show")]:
        c.sonarr.lookups[tvdb] = {"title": t, "tvdbId": tvdb}
    weekly_add(cfg, c, st, NOW)
    types = {s["title"]: s["seriesType"] for s in c.sonarr.series_db.values()}
    assert types == {"Anime One": "anime", "Plain Show": "standard"}


def test_picks_follow_what_users_watch_like_and_favorite(tmp_path):
    from trials.jobs import Clients, weekly_add
    from trials import state
    from fakes import FakeJellyfin, FakeNtfy, FakeSeerr, FakeSonarr, NOW, make_cfg
    cfg = make_cfg(tmp_path, trials_per_week=1)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy())
    c.seerr.add_show(1, 101, "Trending Sitcom", genres=("Comedy",))            # #1 trending
    c.seerr.add_show(2, 102, "Crime Drama", genres=("Crime", "Drama"))
    for tvdb, name in ((101, "Trending Sitcom"), (102, "Crime Drama")):
        c.sonarr.lookups[tvdb] = {"title": name, "tvdbId": tvdb}
    c.jellyfin.taste = {"u1": [{"Type": "Series", "Genres": ["Crime"], "UserData": {"IsFavorite": True}},
                               {"Type": "Series", "Genres": ["Comedy"], "UserData": {"Likes": False, "Played": True}}]}
    lines = weekly_add(cfg, c, state.empty(), NOW)
    assert lines == ["trial added: Crime Drama (S01E01-E03) -> Trials library"]


def test_genre_names_normalise():
    from trials.jobs import genres_of
    assert genres_of(["Sci-Fi & Fantasy", "Action & Adventure"]) == {"science fiction", "fantasy", "action", "adventure"}


def test_trial_is_first_half_of_season_1_rounded_up():
    from trials.jobs import aired_enough, half_season
    assert [half_season(n) for n in (1, 2, 7, 12, 24)] == [1, 1, 4, 6, 12]
    det = {"seasons": [{"seasonNumber": 1, "episodeCount": 12}], "lastEpisodeToAir": {"seasonNumber": 1, "episodeNumber": 5}}
    assert not aired_enough(det)
    det["lastEpisodeToAir"]["episodeNumber"] = 6
    assert aired_enough(det)
    assert not aired_enough({"lastEpisodeToAir": {"seasonNumber": 2, "episodeNumber": 1}})   # no season info
