from datetime import datetime, timezone
from trials.config import Config


def make_cfg(tmp_path, **kw):
    base = dict(seerr_url="http://seerr", seerr_key="k", sonarr_url="http://sonarr", sonarr_key="k",
                jellyfin_url="http://jf", jellyfin_key="k", jellyfin_public_url="https://jf.example",
                state_path=str(tmp_path / "state.json"), enforce=True)
    base.update(kw)
    return Config(**base)


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)   # a Monday


class FakeSonarr:
    def __init__(self, free=5e12):
        self.free, self.tags, self.next_id = free, {"trial": 1}, 100
        self.series_db, self.eps, self.lookups, self.calls = {}, {}, {}, []
        self.remove_tag_fails = False

    def tag_id(self, label):
        return self.tags.setdefault(label, len(self.tags) + 1)

    def quality_profile_id(self, name):
        return 7

    def series(self):
        return [dict(s) for s in self.series_db.values()]

    def lookup_tvdb(self, tvdb):
        return self.lookups.get(tvdb)

    def add_series(self, lookup, profile_id, root, tag_id):
        sid, self.next_id = self.next_id, self.next_id + 1
        s = dict(lookup, id=sid, path=f"{root}/{lookup['title']}", tags=[tag_id], rootFolderPath=root)
        self.series_db[sid] = s
        self.eps[sid] = [{"id": sid * 100 + i, "seasonNumber": 1, "episodeNumber": i,
                          "monitored": False, "hasFile": False} for i in range(1, 11)]
        self.calls.append(("add", sid))
        return dict(s)

    def add_existing(self, sid, tvdb, title, tags=(), path=None):
        self.series_db[sid] = {"id": sid, "tvdbId": tvdb, "title": title, "tags": list(tags),
                               "path": path or f"/data/media/tv/{title}"}
        self.eps[sid] = []

    def episodes(self, sid):
        return [dict(e) for e in self.eps.get(sid, [])]

    def set_monitored(self, ids, monitored):
        for eps in self.eps.values():
            for e in eps:
                if e["id"] in ids:
                    e["monitored"] = monitored

    def search_episodes(self, ids):
        self.calls.append(("search", tuple(ids)))

    def free_bytes(self, path):
        return self.free

    def move_series(self, sid, root):
        s = self.series_db[sid]
        s["path"] = root + "/" + s["path"].rsplit("/", 1)[-1]
        self.calls.append(("move", sid, root))
        return s["path"]

    def monitor_all_and_search(self, sid):
        self.calls.append(("monitor_all", sid))

    def remove_tag(self, sid, tag_id):
        if self.remove_tag_fails:
            self.remove_tag_fails = False
            raise RuntimeError("remove_tag failed")
        self.series_db[sid]["tags"].remove(tag_id)
        self.calls.append(("untag", sid))

    def delete_series(self, sid, exclude):
        self.series_db.pop(sid)
        self.calls.append(("delete", sid, exclude))


class FakeJellyfin:
    def __init__(self):
        self.users_list = [{"Id": "u1", "Name": "adriel"}, {"Id": "u2", "Name": "bobby"}]
        self.index, self.eps, self.like, self.calls = {}, {}, {}, []
        self.notify_fails = False

    def users(self):
        return self.users_list

    def series_index(self):
        return dict(self.index)

    def season1_episodes(self, series_id, user_id):
        return self.eps.get((series_id, user_id), [])

    def likes(self, item_id, user_id):
        return self.like.get((item_id, user_id))

    def set_like(self, item_id, user_id, likes):
        self.like[(item_id, user_id)] = likes
        self.calls.append(("like", item_id, user_id, likes))

    def mark_played(self, item_id, user_id):
        self.calls.append(("played", item_id, user_id))

    def set_position(self, item_id, user_id, ticks):
        self.calls.append(("pos", item_id, user_id, ticks))

    def notify_paths(self, created=(), deleted=()):
        if self.notify_fails:
            raise Exception("Jellyfin notify_paths failed")
        self.calls.append(("notify", tuple(created), tuple(deleted)))

    def authenticate(self, username, password):
        users = {("adriel", "pw"): {"id": "u1", "name": "adriel", "admin": True},
                 ("bobby", "pw"): {"id": "u2", "name": "bobby", "admin": False}}
        return users.get((username, password))


def ep(item_id, n, played=False, ticks=0):
    return {"Id": item_id, "ParentIndexNumber": 1, "IndexNumber": n,
            "UserData": {"Played": played, "PlaybackPositionTicks": ticks}}


class FakeSeerr:
    def __init__(self):
        self.trending, self.details, self.requested = [], {}, set()

    def add_show(self, tmdb, tvdb, name, last=(1, 8), genres=("Drama",), origin=("US",)):
        self.trending.append({"id": tmdb, "mediaType": "tv", "name": name})
        self.details[tmdb] = {"id": tmdb, "name": name, "externalIds": {"tvdbId": tvdb},
                              "lastEpisodeToAir": {"seasonNumber": last[0], "episodeNumber": last[1]},
                              "genres": [{"name": g} for g in genres], "originCountry": list(origin)}

    def trending_tv(self, pages=3):
        return list(self.trending)

    def tv(self, tmdb):
        return self.details[tmdb]

    def requested_since(self, tmdb, since_iso):
        return tmdb in self.requested


class FakeNtfy:
    def __init__(self):
        self.sent = []

    def send(self, title, message):
        self.sent.append((title, message))
