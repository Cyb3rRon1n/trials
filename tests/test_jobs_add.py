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
    c.seerr.add_show(3, 1003, "Old News", premiered_days_ago=400)
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
    assert rec["trial_mode"] == "season" and "trial_n" not in rec
    assert st["shows"]["1005"]["dest"] == "/data/media/tv"
    assert len(lines) == 2 and lines[0] == "trial added: Anime One (season 1) -> Trials library"


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
    assert lines == ["trial added: Crime Drama (season 1) -> Trials library"]


def test_genre_names_normalise():
    from trials.jobs import genres_of
    assert genres_of(["Sci-Fi & Fantasy", "Action & Adventure"]) == {"science fiction", "fantasy", "action", "adventure"}


def test_only_brand_new_shows_are_picked(tmp_path):
    cfg, c, st = setup(tmp_path, trials_per_week=10)
    c.seerr.add_show(1, 1001, "Old Show", premiered_days_ago=45)
    c.seerr.add_show(2, 1002, "Second Season", seasons=2)
    c.seerr.add_show(3, 1003, "Not Out Yet", premiered_days_ago=-3, last=None)
    c.seerr.add_show(4, 1004, "Premiere Pending", last=None)         # dated, but no episode has aired
    c.seerr.add_show(5, 1005, "Ten Days Old")
    for tvdb in range(1001, 1006):
        c.sonarr.lookups[tvdb] = {"title": f"show {tvdb}", "tvdbId": tvdb}
    weekly_add(cfg, c, st, NOW)
    assert sorted(st["shows"]) == ["1005"]


def test_whole_season_1_is_monitored_and_aired_episodes_searched(tmp_path):
    cfg, c, st = setup(tmp_path)
    c.seerr.add_show(5, 1005, "Plain Show")
    c.sonarr.lookups[1005] = {"title": "Plain Show", "tvdbId": 1005}
    real_add = c.sonarr.add_series

    def add_with_s2(*a, **kw):
        s = real_add(*a, **kw)
        c.sonarr.eps[s["id"]].append({"id": 9999, "seasonNumber": 2, "episodeNumber": 1, "monitored": True, "hasFile": False})
        return s
    c.sonarr.add_series = add_with_s2
    weekly_add(cfg, c, st, NOW)
    sid = st["shows"]["1005"]["sonarr_id"]
    mon = {(e["seasonNumber"], e["episodeNumber"]): e["monitored"] for e in c.sonarr.eps[sid]}
    assert [k for k, m in mon.items() if m] == [(1, n) for n in range(1, 7)]       # S2 stays off
    assert ("monitor_season", sid, 1) in c.sonarr.calls                           # later S1 listings too
    assert ("search", tuple(sid * 100 + i for i in range(1, 5))) in c.sonarr.calls   # E05-06 not aired yet


def test_movie_trial_in_state_does_not_break_the_show_add(tmp_path):
    cfg, c, st = setup(tmp_path)
    st["shows"]["movie:555"] = {"tmdb": 555, "media": "movie", "status": "active"}
    c.seerr.add_show(5, 1005, "Plain Show")
    c.sonarr.lookups[1005] = {"title": "Plain Show", "tvdbId": 1005}
    weekly_add(cfg, c, st, NOW)
    assert "1005" in st["shows"]


def test_renewed_show_qualifies_until_its_next_season_airs(tmp_path):
    cfg, c, st = setup(tmp_path, trials_per_week=10)
    c.seerr.add_show(1, 1001, "Renewed, S2 Dated", seasons=2)
    c.seerr.details[1]["seasons"][2]["airDate"] = "2027-03-01"
    c.seerr.add_show(2, 1002, "Renewed, S2 Undated", seasons=2)
    c.seerr.details[2]["seasons"][2]["airDate"] = None
    c.seerr.add_show(3, 1003, "S2 Already Aired", seasons=2)      # both seasons dated 10 days ago
    for tvdb in (1001, 1002, 1003):
        c.sonarr.lookups[tvdb] = {"title": f"show {tvdb}", "tvdbId": tvdb}
    weekly_add(cfg, c, st, NOW)
    assert sorted(st["shows"]) == ["1001", "1002"]


def test_a_special_as_last_aired_episode_still_qualifies(tmp_path):
    cfg, c, st = setup(tmp_path, trials_per_week=10)
    c.seerr.add_show(1, 1001, "Christmas Special Out", last=(0, 1))
    c.seerr.add_show(2, 1002, "Second Season Out", last=(2, 1))
    for tvdb in (1001, 1002):
        c.sonarr.lookups[tvdb] = {"title": f"show {tvdb}", "tvdbId": tvdb}
    weekly_add(cfg, c, st, NOW)
    assert sorted(st["shows"]) == ["1001"]
