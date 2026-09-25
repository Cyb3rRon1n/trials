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
                return status, (json.dumps(body).encode() if body is not None else b"")
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

    # Second call: PUT series (monitored=True, season 0 unchanged, season 1 monitored)
    put_series_call = [c for c in t.calls if c[0] == "PUT" and "/series/" in c[1]][0]
    assert put_series_call[3]["monitored"] is True
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
