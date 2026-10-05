from datetime import datetime, timedelta, timezone
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

    def add_series(self, lookup, profile_id, root, tag_id, series_type="standard"):
        sid, self.next_id = self.next_id, self.next_id + 1
        s = dict(lookup, id=sid, path=f"{root}/{lookup['title']}", tags=[tag_id], rootFolderPath=root,
                 seriesType=series_type)
        self.series_db[sid] = s
        # E01-E04 have aired (E04 today), E05-E06 air over the next two weeks
        self.eps[sid] = [{"id": sid * 100 + i, "seasonNumber": 1, "episodeNumber": i, "monitored": False, "hasFile": False,
                          "airDateUtc": (NOW + timedelta(days=7 * (i - 4))).strftime("%Y-%m-%dT%H:%M:%SZ")}
                         for i in range(1, 7)]
        self.calls.append(("add", sid))
        return dict(s)

    def add_existing(self, sid, tvdb, title, tags=(), path=None):
        self.series_db[sid] = {"id": sid, "tvdbId": tvdb, "title": title, "tags": list(tags),
                               "path": path or f"/data/media/tv/{title}"}
        self.eps[sid] = []

    def episodes(self, sid):
        return [dict(e) for e in self.eps.get(sid, [])]

    def monitor_season(self, sid, season):
        self.calls.append(("monitor_season", sid, season))

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

    def add_tag(self, sid, tag_id):
        self.series_db[sid]["tags"].append(tag_id)
        self.calls.append(("tag", sid))

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
        self.movies, self.udata, self.recent = {}, {}, {}
        self.notify_fails = False
        self.note_fails = False
        self.notes = {}

    def set_trial_note(self, item_id, user_id, note):
        if self.note_fails:
            raise RuntimeError("jellyfin item update failed")
        self.notes[item_id] = note
        self.calls.append(("note", item_id, note))

    def users(self):
        return self.users_list

    def series_index(self):
        return dict(self.index)

    def season1_episodes(self, series_id, user_id):
        return self.eps.get((series_id, user_id), [])

    def all_episodes(self, series_id, user_id):
        return self.eps.get((series_id, user_id), [])

    def movie_index(self):
        return dict(self.movies)

    def user_data(self, item_id, user_id):
        return self.udata.get((item_id, user_id), {})

    def taste_items(self, user_id):
        return getattr(self, "taste", {}).get(user_id, [])

    def recently_played(self, user_id, limit=40):
        return self.recent.get(user_id, [])

    def likes(self, item_id, user_id):
        return self.like.get((item_id, user_id))

    def favorite(self, item_id, user_id):
        return bool(self.udata.get((item_id, user_id), {}).get("IsFavorite"))

    def set_like(self, item_id, user_id, likes):
        self.like[(item_id, user_id)] = likes
        self.calls.append(("like", item_id, user_id, likes))

    def restore_played(self, item_id, user_id, played, ticks, count=0, date=None):
        self.calls.append(("played", item_id, user_id) if played else ("pos", item_id, user_id, ticks))
        self.restored = getattr(self, "restored", []) + [(item_id, user_id, played, ticks, count, date)]

    def set_favorite(self, item_id, user_id, fav):
        self.calls.append(("fav", item_id, user_id, fav))

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
                 ("bobby", "pw"): {"id": "u2", "name": "bobby", "admin": False},   # admin via cfg.admins, not Jellyfin
                 ("palma", "pw"): {"id": "u3", "name": "palma", "admin": False}}
        return users.get((username, password))


def ep(item_id, n, played=False, ticks=0):
    return {"Id": item_id, "ParentIndexNumber": 1, "IndexNumber": n,
            "UserData": {"Played": played, "PlaybackPositionTicks": ticks}}


class FakeSeerr:
    def __init__(self):
        self.trending, self.details, self.requested = [], {}, set()

    def add_show(self, tmdb, tvdb, name, last=(1, 4), genres=("Drama",), origin=("US",), s1_eps=6,
                 premiered_days_ago=10, seasons=1):
        """defaults: a brand-new show - one season, premiered 10 days ago, 4 episodes out"""
        self.trending.append({"id": tmdb, "mediaType": "tv", "name": name})
        self.details[tmdb] = {"id": tmdb, "name": name, "externalIds": {"tvdbId": tvdb},
                              "firstAirDate": (NOW - timedelta(days=premiered_days_ago)).strftime("%Y-%m-%d"),
                              "lastEpisodeToAir": {"seasonNumber": last[0], "episodeNumber": last[1]} if last else None,
                              "seasons": [{"seasonNumber": 0, "episodeCount": 2}] +
                                         [{"seasonNumber": n, "episodeCount": s1_eps} for n in range(1, seasons + 1)],
                              "genres": [{"name": g} for g in genres], "originCountry": list(origin)}

    def add_film(self, tmdb, title, digital_days_ago=10, physical_days_ago=None, genres=("Drama",), popular=False):
        """a movie candidate; None = no such release date known to TMDb"""
        dates = [{"type": t, "release_date": (NOW - timedelta(days=d)).strftime("%Y-%m-%dT00:00:00.000Z")}
                 for t, d in ((4, digital_days_ago), (5, physical_days_ago)) if d is not None]
        dates.append({"type": 3, "release_date": (NOW - timedelta(days=90)).strftime("%Y-%m-%dT00:00:00.000Z")})  # cinemas
        item = {"id": tmdb, "mediaType": "movie", "title": title}
        self.__dict__.setdefault("popular_m" if popular else "trending_m", []).append(item)
        self.__dict__.setdefault("movie_details", {})[tmdb] = {
            "id": tmdb, "title": title, "genres": [{"name": g} for g in genres],
            "releases": {"results": [{"iso_3166_1": "US", "release_dates": dates}]}}

    def trending_movies(self, pages=3):
        return list(getattr(self, "trending_m", []))

    def popular_movies(self, pages=2):
        return list(getattr(self, "popular_m", []))

    def movie(self, tmdb):
        return self.movie_details[tmdb]

    def trending_tv(self, pages=3):
        return list(self.trending)

    def popular_tv(self, pages=2):
        return list(getattr(self, "popular", []))

    def tv(self, tmdb):
        return self.details[tmdb]

    def requested_since(self, tmdb, since_iso):
        return tmdb in self.requested


class FakeNtfy:
    def __init__(self):
        self.sent = []

    def send(self, title, message):
        self.sent.append((title, message))


class FakeRadarr:
    def __init__(self, free=5e12):
        self.tags, self.movies_db, self.calls, self.lookups, self.free = {"trial": 1}, {}, [], {}, free

    def quality_profile_id(self, name):
        return 4

    def free_bytes(self, path):
        return self.free

    def lookup_tmdb(self, tmdb):
        self.calls.append(("lookup", tmdb))
        return self.lookups.get(tmdb, {"tmdbId": tmdb, "title": f"Film {tmdb}", "year": 2026})

    def add_movie(self, lookup, profile_id, root, tag_id):
        mid = 40 + len(self.movies_db)
        self.add(mid, lookup["tmdbId"], lookup["title"], f"{root}/{lookup['title']} ({lookup['year']})",
                 year=lookup["year"], has_file=False, tags=[tag_id])
        self.calls.append(("add", mid, lookup["tmdbId"], profile_id, root))
        return self.get_movie(mid)

    def tag_id(self, label):
        return self.tags.setdefault(label, len(self.tags) + 1)

    def add(self, mid, tmdb, title, path, year=2026, has_file=True, tags=()):
        self.movies_db[mid] = {"id": mid, "tmdbId": tmdb, "title": title, "year": year, "path": path,
                               "hasFile": has_file, "tags": list(tags)}

    def movies(self):
        return [dict(m) for m in self.movies_db.values()]

    def get_movie(self, mid):
        return dict(self.movies_db[mid])

    def add_tag(self, mid, tag_id):
        self.movies_db[mid]["tags"].append(tag_id)

    def remove_tag(self, mid, tag_id):
        self.movies_db[mid]["tags"].remove(tag_id)
        self.calls.append(("untag", mid))

    def move_movie(self, mid, root):
        m = self.movies_db[mid]
        m["path"] = root + "/" + m["path"].rsplit("/", 1)[-1]
        self.calls.append(("move", mid, root))
        return m["path"]

    def delete_movie(self, mid, exclude):
        self.movies_db.pop(mid)
        self.calls.append(("delete", mid, exclude))
