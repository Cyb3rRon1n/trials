import json
import pytest
from trials.clients import ApiError, Http, Jellyfin, Ntfy, Seerr, Sonarr


class FakeTransport:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, data):
        body = json.loads(data) if data and data[:1] in (b"{", b"[") else data
        self.calls.append((method, url, headers, body))
        for (m, prefix), (status, body) in self.routes.items():
            if m == method and url.startswith(prefix):
                return status, (body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b"")
        return 404, b"{}"


def test_http_raises_on_error_status():
    t = FakeTransport({("GET", "http://x/boom"): (500, {"e": 1})})
    with pytest.raises(ApiError, match="HTTP 500"):
        Http("http://x", {}, t).call("GET", "/boom")


def test_sonarr_sends_api_key_and_free_bytes_longest_prefix():
    t = FakeTransport({("GET", "http://s/api/v3/diskspace"): (200, [
        {"path": "/", "freeSpace": 1}, {"path": "/data", "freeSpace": 5}, {"path": "/data/media/", "freeSpace": 9}])})
    s = Sonarr("http://s", "KEY", t)
    assert s.free_bytes("/data/media/trials") == 9
    assert t.calls[0][2]["X-Api-Key"] == "KEY"


def test_sonarr_move_series_sets_path_and_movefiles():
    t = FakeTransport({("GET", "http://s/api/v3/series/5"): (200, {"id": 5, "path": "/data/media/trials/Show A", "rootFolderPath": "/data/media/trials"}),
                       ("PUT", "http://s/api/v3/series/5"): (202, {})})
    new = Sonarr("http://s", "k", t).move_series(5, "/data/media/tv")
    method, url, _, body = t.calls[-1]
    assert new == "/data/media/tv/Show A" and method == "PUT" and "moveFiles=true" in url
    assert body["path"] == "/data/media/tv/Show A" and body["rootFolderPath"] == "/data/media/tv"


def test_sonarr_tag_id_creates_when_missing():
    t = FakeTransport({("GET", "http://s/api/v3/tag"): (200, [{"id": 1, "label": "anime"}]),
                       ("POST", "http://s/api/v3/tag"): (201, {"id": 4, "label": "trial"})})
    assert Sonarr("http://s", "k", t).tag_id("trial") == 4


def test_jellyfin_auth_header_and_authenticate():
    ok = FakeTransport({("POST", "http://j/Users/AuthenticateByName"): (200, {"User": {"Id": "u1", "Name": "adriel", "Policy": {"IsAdministrator": True}}})})
    assert Jellyfin("http://j", "K", ok).authenticate("adriel", "pw") == {"id": "u1", "name": "adriel", "admin": True}
    assert 'Client="trials"' in ok.calls[0][2]["Authorization"]
    bad = FakeTransport({("POST", "http://j/Users/AuthenticateByName"): (401, None)})
    assert Jellyfin("http://j", "K", bad).authenticate("x", "y") is None


def test_jellyfin_set_like_and_clear():
    t = FakeTransport({("POST", "http://j/UserItems/i1/Rating"): (200, {}), ("DELETE", "http://j/UserItems/i1/Rating"): (200, {})})
    j = Jellyfin("http://j", "K", t)
    j.set_like("i1", "u1", False)
    j.set_like("i1", "u1", None)
    assert t.calls[0][0] == "POST" and "likes=false" in t.calls[0][1] and "userId=u1" in t.calls[0][1]
    assert t.calls[1][0] == "DELETE"
    assert t.calls[0][2]["Authorization"] == 'MediaBrowser Token="K"'


def test_seerr_trending_filters_tv():
    t = FakeTransport({("GET", "http://e/api/v1/discover/trending"): (200, {"results": [
        {"id": 1, "mediaType": "movie"}, {"id": 2, "mediaType": "tv"}]})})
    assert [r["id"] for r in Seerr("http://e", "k", t).trending_tv(pages=1)] == [2]


def test_seerr_requested_since():
    t = FakeTransport({("GET", "http://e/api/v1/request"): (200, {"results": [
        {"type": "tv", "createdAt": "2026-10-02T10:00:00.000Z", "media": {"tmdbId": 77}}]})})
    s = Seerr("http://e", "k", t)
    assert s.requested_since(77, "2026-10-01T00:00:00Z") is True
    assert s.requested_since(77, "2026-10-03T00:00:00Z") is False
    assert s.requested_since(78, "2026-10-01T00:00:00Z") is False


def test_ntfy_noop_without_topic_and_posts_with_topic():
    t = FakeTransport({("POST", "https://n/topic"): (200, None)})
    Ntfy("https://n", "", t).send("T", "m")
    assert t.calls == []
    Ntfy("https://n", "topic", t).send("T", "m")
    assert t.calls[0][1] == "https://n/topic" and t.calls[0][2]["Title"] == "T"


# Request-shape tests for write/destructive calls

def test_sonarr_add_series_request_shape():
    t = FakeTransport({("POST", "http://s/api/v3/series"): (201, {"id": 99})})
    lookup = {"title": "Show", "tvdbId": 123}
    Sonarr("http://s", "k", t).add_series(lookup, profile_id=1, root="/data", tag_id=5)
    method, url, _, body = t.calls[0]
    assert method == "POST" and url == "http://s/api/v3/series"
    assert body["title"] == "Show" and body["tvdbId"] == 123
    assert body["qualityProfileId"] == 1
    assert body["rootFolderPath"] == "/data"
    assert body["tags"] == [5]
    assert body["monitored"] is True
    assert body["seasonFolder"] is True
    assert body["monitorNewItems"] == "none"
    assert body["addOptions"]["monitor"] == "none"
    assert body["addOptions"]["searchForMissingEpisodes"] is False
    assert body["addOptions"]["searchForCutoffUnmetEpisodes"] is False


def test_sonarr_delete_series_exclude_true():
    t = FakeTransport({("DELETE", "http://s/api/v3/series/10"): (200, {})})
    Sonarr("http://s", "k", t).delete_series(10, exclude=True)
    method, url, _, _ = t.calls[0]
    assert method == "DELETE"
    assert "deleteFiles=true" in url
    assert "addImportListExclusion=true" in url


def test_sonarr_delete_series_exclude_false():
    t = FakeTransport({("DELETE", "http://s/api/v3/series/10"): (200, {})})
    Sonarr("http://s", "k", t).delete_series(10, exclude=False)
    method, url, _, _ = t.calls[0]
    assert method == "DELETE"
    assert "deleteFiles=true" in url
    assert "addImportListExclusion=false" in url


def test_sonarr_remove_tag_keeps_other_tags():
    t = FakeTransport({("GET", "http://s/api/v3/series/7"): (200, {"id": 7, "tags": [1, 2, 3]}),
                       ("PUT", "http://s/api/v3/series/7"): (202, {})})
    Sonarr("http://s", "k", t).remove_tag(7, 2)
    method, url, _, body = t.calls[-1]
    assert method == "PUT"
    assert body["tags"] == [1, 3]


def test_sonarr_set_monitored_empty_list_no_call():
    t = FakeTransport({})
    Sonarr("http://s", "k", t).set_monitored([], True)
    assert t.calls == []


def test_sonarr_set_monitored_request_shape():
    t = FakeTransport({("PUT", "http://s/api/v3/episode/monitor"): (202, {})})
    Sonarr("http://s", "k", t).set_monitored([10, 20, 30], True)
    method, url, _, body = t.calls[0]
    assert method == "PUT" and url == "http://s/api/v3/episode/monitor"
    assert body == {"episodeIds": [10, 20, 30], "monitored": True}


def test_sonarr_monitor_all_and_search_request_shapes():
    t = FakeTransport({
        ("GET", "http://s/api/v3/series/5"): (200, {
            "id": 5, "monitored": False,
            "seasons": [{"seasonNumber": 0, "monitored": False}, {"seasonNumber": 1, "monitored": False}]
        }),
        ("GET", "http://s/api/v3/episode"): (200, [
            {"id": 100, "seasonNumber": 0}, {"id": 101, "seasonNumber": 1}, {"id": 102, "seasonNumber": 1}
        ]),
        ("PUT", "http://s/api/v3/series/5"): (202, {}),
        ("PUT", "http://s/api/v3/episode/monitor"): (202, {}),
        ("POST", "http://s/api/v3/command"): (201, {})
    })
    Sonarr("http://s", "k", t).monitor_all_and_search(5)

    # First call: GET series
    assert t.calls[0][0] == "GET"

    # Second call: PUT series (monitored=True, monitorNewItems="all", season 0 unchanged, season 1 monitored)
    put_series_call = [c for c in t.calls if c[0] == "PUT" and "/series/" in c[1]][0]
    assert put_series_call[3]["monitored"] is True
    assert put_series_call[3]["monitorNewItems"] == "all"
    assert put_series_call[3]["seasons"][0]["monitored"] is False  # season 0 unchanged
    assert put_series_call[3]["seasons"][1]["monitored"] is True   # season 1 monitored

    # Third call: GET episodes
    assert t.calls[2][0] == "GET" and "/episode" in t.calls[2][1]

    # Fourth call: PUT episode/monitor with only season >0 episodes
    put_episode_call = [c for c in t.calls if c[0] == "PUT" and "/episode/monitor" in c[1]][0]
    assert put_episode_call[3]["episodeIds"] == [101, 102]
    assert put_episode_call[3]["monitored"] is True

    # Last call: POST command SeriesSearch
    assert t.calls[-1][0] == "POST" and "/command" in t.calls[-1][1]
    assert t.calls[-1][3]["name"] == "SeriesSearch"
    assert t.calls[-1][3]["seriesId"] == 5


def test_jellyfin_season1_episodes_excludes_missing():
    t = FakeTransport({("GET", "http://j/Shows/s1/Episodes"): (200, {"Items": []})})
    Jellyfin("http://j", "K", t).season1_episodes("s1", "u1")
    method, url, _, _ = t.calls[0]
    assert method == "GET" and "IsMissing=false" in url


def test_jellyfin_notify_paths_empty_no_call():
    t = FakeTransport({})
    Jellyfin("http://j", "K", t).notify_paths()
    assert t.calls == []


def test_jellyfin_notify_paths_request_shape():
    t = FakeTransport({("POST", "http://j/Library/Media/Updated"): (200, {})})
    Jellyfin("http://j", "K", t).notify_paths(created=["/data/show1"], deleted=["/data/show2"])
    method, url, _, body = t.calls[0]
    assert method == "POST" and url == "http://j/Library/Media/Updated"
    assert len(body["Updates"]) == 2
    assert body["Updates"][0] == {"Path": "/data/show1", "UpdateType": "Created"}
    assert body["Updates"][1] == {"Path": "/data/show2", "UpdateType": "Deleted"}


def test_sonarr_add_series_series_type():
    t = FakeTransport({("POST", "http://s/api/v3/series"): (201, {"id": 99})})
    s = Sonarr("http://s", "k", t)
    s.add_series({"title": "A", "tvdbId": 1}, profile_id=1, root="/data", tag_id=5)
    s.add_series({"title": "B", "tvdbId": 2}, profile_id=1, root="/data", tag_id=5, series_type="anime")
    assert t.calls[0][3]["seriesType"] == "standard" and t.calls[1][3]["seriesType"] == "anime"


def test_jellyfin_set_trial_note_prepends_locks_and_is_idempotent():
    item = {"Id": "i1", "Overview": "Real synopsis.", "LockedFields": ["Name"]}
    t = FakeTransport({("GET", "http://j/Items/i1"): (200, item), ("POST", "http://j/Items/i1"): (204, None)})
    Jellyfin("http://j", "K", t).set_trial_note("i1", "u1", "🗳 ON TRIAL until Fri 16 Oct – vote at https://t")
    body = t.calls[-1][3]
    assert t.calls[-1][0] == "POST"
    assert body["Overview"] == "🗳 ON TRIAL until Fri 16 Oct – vote at https://t\n\nReal synopsis."
    assert sorted(body["LockedFields"]) == ["Name", "Overview"]
    # re-noting an already-noted item replaces the note instead of stacking it
    item2 = dict(item, Overview=body["Overview"], LockedFields=body["LockedFields"])
    t2 = FakeTransport({("GET", "http://j/Items/i1"): (200, item2), ("POST", "http://j/Items/i1"): (204, None)})
    Jellyfin("http://j", "K", t2).set_trial_note("i1", "u1", "🗳 ON TRIAL until Sat 17 Oct – vote at https://t")
    assert t2.calls[-1][3]["Overview"] == "🗳 ON TRIAL until Sat 17 Oct – vote at https://t\n\nReal synopsis."


def test_jellyfin_clear_trial_note_restores_and_unlocks():
    item = {"Id": "i1", "Overview": "🗳 ON TRIAL until Fri 16 Oct – vote\n\nReal synopsis.", "LockedFields": ["Overview", "Name"]}
    t = FakeTransport({("GET", "http://j/Items/i1"): (200, item), ("POST", "http://j/Items/i1"): (204, None)})
    Jellyfin("http://j", "K", t).set_trial_note("i1", "u1", None)
    body = t.calls[-1][3]
    assert body["Overview"] == "Real synopsis." and body["LockedFields"] == ["Name"]


def test_sonarr_monitor_season_puts_only_that_season():
    t = FakeTransport({("GET", "http://s/api/v3/series/5"): (200, {"id": 5, "seasons": [
                           {"seasonNumber": 0, "monitored": False}, {"seasonNumber": 1, "monitored": False},
                           {"seasonNumber": 2, "monitored": False}]}),
                       ("PUT", "http://s/api/v3/series/5"): (202, {})})
    Sonarr("http://s", "k", t).monitor_season(5, 1)
    assert [x["monitored"] for x in t.calls[-1][3]["seasons"]] == [False, True, False]


def test_sonarr_add_tag_appends_once():
    t = FakeTransport({("GET", "http://s/api/v3/series/5"): (200, {"id": 5, "tags": [3]}),
                       ("PUT", "http://s/api/v3/series/5"): (202, {})})
    sonarr = Sonarr("http://s", "k", t)
    sonarr.add_tag(5, 1)
    assert t.calls[-1][0] == "PUT" and t.calls[-1][3]["tags"] == [3, 1]
    t.routes[("GET", "http://s/api/v3/series/5")] = (200, {"id": 5, "tags": [3, 1]})
    sonarr.add_tag(5, 1)
    assert [c[0] for c in t.calls] == ["GET", "PUT", "GET"]       # already tagged: no write


def test_fakes_only_fake_methods_the_real_clients_have():
    # FakeSonarr.add_tag existed while Sonarr.add_tag didn't, so a production crash passed every test
    from trials.clients import Radarr
    import fakes
    helpers = {"add_existing", "add", "add_show", "add_film"}
    for fake, real in ((fakes.FakeSonarr, Sonarr), (fakes.FakeRadarr, Radarr), (fakes.FakeSeerr, Seerr),
                       (fakes.FakeJellyfin, Jellyfin), (fakes.FakeNtfy, Ntfy)):
        missing = {m for m in vars(fake) if not m.startswith("_") and callable(getattr(fake, m))} - helpers - set(dir(real))
        assert not missing, f"{fake.__name__} fakes {missing}, which {real.__name__} lacks"


def test_seerr_movie_candidates_and_details():
    t = FakeTransport({("GET", "http://e/api/v1/discover/trending"): (200, {"results": [
                           {"id": 1, "mediaType": "movie"}, {"id": 2, "mediaType": "tv"}]}),
                       ("GET", "http://e/api/v1/discover/movies"): (200, {"results": [{"id": 3}]}),
                       ("GET", "http://e/api/v1/movie/3"): (200, {"id": 3, "releases": {"results": []}})})
    s = Seerr("http://e", "k", t)
    assert [r["id"] for r in s.trending_movies(pages=1)] == [1]
    assert s.popular_movies(pages=1) == [{"id": 3, "mediaType": "movie"}]
    assert s.movie(3)["releases"] == {"results": []}


def test_radarr_lookup_add_profile_and_free_space():
    from trials.clients import Radarr
    t = FakeTransport({("GET", "http://r/api/v3/movie/lookup/tmdb"): (200, {"title": "Film", "tmdbId": 9}),
                       ("POST", "http://r/api/v3/movie"): (201, {"id": 40, "title": "Film"}),
                       ("GET", "http://r/api/v3/qualityprofile"): (200, [{"id": 4, "name": "HD-1080p"}]),
                       ("GET", "http://r/api/v3/diskspace"): (200, [{"path": "/data", "freeSpace": 7}])})
    r = Radarr("http://r", "k", t)
    lookup = r.lookup_tmdb(9)
    assert lookup["title"] == "Film" and "tmdbId=9" in t.calls[0][1]
    assert r.quality_profile_id("HD-1080p") == 4 and r.free_bytes("/data/media/trials") == 7
    assert r.add_movie(lookup, 4, "/data/media/trials", 1)["id"] == 40
    body = [c for c in t.calls if c[0] == "POST"][0][3]
    assert (body["tmdbId"], body["qualityProfileId"], body["rootFolderPath"], body["tags"], body["monitored"]) == \
        (9, 4, "/data/media/trials", [1], True)
    assert body["addOptions"] == {"searchForMovie": True} and body["minimumAvailability"] == "released"


def test_jellyfin_get_image_returns_bytes_or_none():
    t = FakeTransport({("GET", "http://j/Items/i1/Images/Backdrop/0"): (200, b"\xff\xd8jpeg"),
                       ("GET", "http://j/Items/i2/Images/Backdrop/0"): (404, b"")})
    j = Jellyfin("http://j", "K", t)
    assert j.get_image("i1", "Backdrop") == b"\xff\xd8jpeg" and j.get_image("i2", "Backdrop") is None


def test_jellyfin_set_image_replaces_the_first_backdrop():
    import base64
    t = FakeTransport({("GET", "http://j/Items/i1"): (200, {"Id": "i1", "BackdropImageTags": ["a", "b"]}),
                       ("DELETE", "http://j/Items/i1/Images/Backdrop/2"): (204, None),
                       ("POST", "http://j/Items/i1/Images/Backdrop"): (204, None)})
    Jellyfin("http://j", "K", t).set_image("i1", "Backdrop", b"NEW")
    calls = [(m, u.split("http://j")[1]) for m, u, _, _ in t.calls]
    # upload appends (Jellyfin ImageSaver: index = count), so: upload, swap it to the front (the Index
    # endpoint calls SwapImagesAsync), then drop the old first one, now last - a failure part-way
    # leaves an extra backdrop, never none
    assert calls == [("GET", "/Items/i1"), ("POST", "/Items/i1/Images/Backdrop"),
                     ("POST", "/Items/i1/Images/Backdrop/2/Index?newIndex=0"), ("DELETE", "/Items/i1/Images/Backdrop/2")]
    upload = t.calls[1]
    assert upload[3] == base64.b64encode(b"NEW") and upload[2]["Content-Type"] == "image/jpeg"


def test_jellyfin_set_image_primary_and_first_backdrop():
    t = FakeTransport({("GET", "http://j/Items/i1"): (200, {"Id": "i1", "BackdropImageTags": []}),
                       ("POST", "http://j/Items/i1/Images/"): (204, None)})
    j = Jellyfin("http://j", "K", t)
    j.set_image("i1", "Primary", b"P", "image/png")
    j.set_image("i1", "Backdrop", b"B")
    calls = [(m, u.split("http://j")[1]) for m, u, _, _ in t.calls]
    assert calls == [("POST", "/Items/i1/Images/Primary"), ("GET", "/Items/i1"), ("POST", "/Items/i1/Images/Backdrop")]
    assert t.calls[0][2]["Content-Type"] == "image/png"
