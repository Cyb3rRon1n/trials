import base64
import json
import urllib.error
import urllib.parse
import urllib.request

TRIAL_MARK = "🗳 ON TRIAL"
AUTH_CLIENT = 'MediaBrowser Client="trials", Device="trials-web", DeviceId="trials-web", Version="0.1.0"'


class ApiError(Exception):
    pass


def urllib_transport(method, url, headers, data):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ApiError(f"{method} {url}: {e}") from e


class Http:
    def __init__(self, base, headers, transport=urllib_transport):
        self.base, self.headers, self.transport = base.rstrip("/"), headers, transport

    def call(self, method, path, params=None, body=None, ok=(200, 201, 202, 204)):
        data = None if body is None else json.dumps(body).encode()
        _, raw = self.raw(method, path, params, data, None if body is None else "application/json", ok)
        return json.loads(raw) if raw else None

    def raw(self, method, path, params=None, data=None, ctype=None, ok=(200, 201, 202, 204)):
        """-> (status, body bytes); for non-JSON bodies like images"""
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = dict(self.headers, **({"Content-Type": ctype} if ctype else {}))
        status, raw = self.transport(method, url, headers, data)
        if status not in ok:
            raise ApiError(f"{method} {path} -> HTTP {status}: {raw[:200]!r}")
        return status, raw


def _covers(root, path):
    root = root.rstrip("/")
    return root == "" or path == root or path.startswith(root + "/")


class Arr:
    """what Sonarr and Radarr share (same v3 API shapes)"""
    def __init__(self, url, key, transport=urllib_transport):
        self.http = Http(url.rstrip("/") + "/api/v3", {"X-Api-Key": key}, transport)

    def tag_id(self, label):
        for t in self.http.call("GET", "/tag"):
            if t["label"] == label:
                return t["id"]
        return self.http.call("POST", "/tag", body={"label": label})["id"]

    def quality_profile_id(self, name):
        for p in self.http.call("GET", "/qualityprofile"):
            if p["name"] == name:
                return p["id"]
        raise ApiError(f"{type(self).__name__} quality profile {name!r} not found")

    def free_bytes(self, path):
        best = None
        for d in self.http.call("GET", "/diskspace"):
            if _covers(d["path"], path) and (best is None or len(d["path"].rstrip("/")) > len(best["path"].rstrip("/"))):
                best = d
        if best is None:
            raise ApiError(f"no {type(self).__name__} diskspace entry covers {path}")
        return int(best["freeSpace"])


class Sonarr(Arr):

    def series(self):
        return self.http.call("GET", "/series")

    def get_series(self, sid):
        return self.http.call("GET", f"/series/{sid}")

    def lookup_tvdb(self, tvdb):
        found = self.http.call("GET", "/series/lookup", {"term": f"tvdb:{tvdb}"})
        return found[0] if found else None

    def add_series(self, lookup, profile_id, root, tag_id, series_type="standard"):
        body = dict(lookup, qualityProfileId=profile_id, rootFolderPath=root, tags=[tag_id],
                    monitored=True, seasonFolder=True, monitorNewItems="none", seriesType=series_type,
                    addOptions={"monitor": "none", "searchForMissingEpisodes": False,
                                "searchForCutoffUnmetEpisodes": False})
        return self.http.call("POST", "/series", body=body)

    def episodes(self, sid):
        return self.http.call("GET", "/episode", {"seriesId": sid})

    def monitor_season(self, sid, season):
        """monitor a whole season at series level - Sonarr then monitors its episodes, including ones listed later"""
        s = self.get_series(sid)
        for x in s["seasons"]:
            if x["seasonNumber"] == season:
                x["monitored"] = True
        self.http.call("PUT", f"/series/{sid}", body=s)

    def set_monitored(self, ids, monitored):
        if ids:
            self.http.call("PUT", "/episode/monitor", body={"episodeIds": list(ids), "monitored": monitored})

    def search_episodes(self, ids):
        if ids:
            self.http.call("POST", "/command", body={"name": "EpisodeSearch", "episodeIds": list(ids)})

    def move_series(self, sid, root):
        s = self.get_series(sid)
        s["rootFolderPath"] = root
        s["path"] = root.rstrip("/") + "/" + s["path"].rstrip("/").rsplit("/", 1)[-1]
        self.http.call("PUT", f"/series/{sid}", {"moveFiles": "true"}, body=s)
        return s["path"]

    def monitor_all_and_search(self, sid):
        s = self.get_series(sid)
        s["monitored"] = True
        s["monitorNewItems"] = "all"
        for x in s["seasons"]:
            if x["seasonNumber"] > 0:
                x["monitored"] = True
        self.http.call("PUT", f"/series/{sid}", body=s)
        self.set_monitored([e["id"] for e in self.episodes(sid) if e["seasonNumber"] > 0], True)
        self.http.call("POST", "/command", body={"name": "SeriesSearch", "seriesId": sid})

    def add_tag(self, sid, tag_id):
        s = self.get_series(sid)
        if tag_id not in s["tags"]:
            s["tags"].append(tag_id)
            self.http.call("PUT", f"/series/{sid}", body=s)

    def remove_tag(self, sid, tag_id):
        s = self.get_series(sid)
        s["tags"] = [t for t in s["tags"] if t != tag_id]
        self.http.call("PUT", f"/series/{sid}", body=s)

    def delete_series(self, sid, exclude):
        self.http.call("DELETE", f"/series/{sid}",
                       {"deleteFiles": "true", "addImportListExclusion": "true" if exclude else "false"})


class Radarr(Arr):
    def movies(self):
        return self.http.call("GET", "/movie")

    def get_movie(self, mid):
        return self.http.call("GET", f"/movie/{mid}")

    def lookup_tmdb(self, tmdb):
        return self.http.call("GET", "/movie/lookup/tmdb", {"tmdbId": tmdb})

    def add_movie(self, lookup, profile_id, root, tag_id):
        body = dict(lookup, qualityProfileId=profile_id, rootFolderPath=root, tags=[tag_id], monitored=True,
                    minimumAvailability="released", addOptions={"searchForMovie": True})
        return self.http.call("POST", "/movie", body=body)

    def add_tag(self, mid, tag_id):
        m = self.get_movie(mid)
        if tag_id not in m["tags"]:
            m["tags"].append(tag_id)
            self.http.call("PUT", f"/movie/{mid}", body=m)

    def remove_tag(self, mid, tag_id):
        m = self.get_movie(mid)
        m["tags"] = [t for t in m["tags"] if t != tag_id]
        self.http.call("PUT", f"/movie/{mid}", body=m)

    def move_movie(self, mid, root):
        m = self.get_movie(mid)
        m["rootFolderPath"] = root
        m["path"] = root.rstrip("/") + "/" + m["path"].rstrip("/").rsplit("/", 1)[-1]
        self.http.call("PUT", f"/movie/{mid}", {"moveFiles": "true"}, body=m)
        return m["path"]

    def delete_movie(self, mid, exclude):
        self.http.call("DELETE", f"/movie/{mid}",
                       {"deleteFiles": "true", "addImportExclusion": "true" if exclude else "false"})


class Jellyfin:
    def __init__(self, url, key, transport=urllib_transport):
        self.http = Http(url, {"Authorization": f'MediaBrowser Token="{key}"'}, transport)
        self.anon = Http(url, {"Authorization": AUTH_CLIENT}, transport)

    def users(self):
        return [{"Id": u["Id"], "Name": u["Name"]} for u in self.http.call("GET", "/Users")]

    def series_index(self):
        r = self.http.call("GET", "/Items", {"Recursive": "true", "IncludeItemTypes": "Series", "Fields": "Path"})
        return {i["Path"].rstrip("/"): i for i in r["Items"] if i.get("Path")}

    def taste_items(self, user_id):
        """every show/movie with this user's data + genres (for the taste profile)"""
        return self.http.call("GET", "/Items", {"userId": user_id, "Recursive": "true",
                                                "IncludeItemTypes": "Series,Movie", "Fields": "Genres"})["Items"]

    def movie_index(self):
        """movie folder -> item (a movie's Path is its file; Radarr knows the folder)"""
        r = self.http.call("GET", "/Items", {"Recursive": "true", "IncludeItemTypes": "Movie", "Fields": "Path"})
        return {i["Path"].rsplit("/", 1)[0]: i for i in r["Items"] if i.get("Path")}

    def all_episodes(self, series_id, user_id):
        return self.http.call("GET", f"/Shows/{series_id}/Episodes", {"userId": user_id, "IsMissing": "false"})["Items"]

    def user_data(self, item_id, user_id):
        return self.http.call("GET", f"/Items/{item_id}", {"userId": user_id}).get("UserData") or {}

    def recently_played(self, user_id, limit=40):
        """the user's latest watched movies and episodes, newest first"""
        return self.http.call("GET", "/Items", {"userId": user_id, "Recursive": "true", "Filters": "IsPlayed",
                                                "IncludeItemTypes": "Movie,Episode", "SortBy": "DatePlayed",
                                                "SortOrder": "Descending", "Limit": limit,
                                                "Fields": "SeriesId,SeriesName,ProductionYear"})["Items"]

    def season1_episodes(self, series_id, user_id):
        return self.http.call("GET", f"/Shows/{series_id}/Episodes",
                              {"userId": user_id, "season": 1, "IsMissing": "false"})["Items"]

    def likes(self, item_id, user_id):
        return (self.http.call("GET", f"/Items/{item_id}", {"userId": user_id}).get("UserData") or {}).get("Likes")

    def favorite(self, item_id, user_id):
        return bool(self.user_data(item_id, user_id).get("IsFavorite"))

    def set_like(self, item_id, user_id, likes):
        if likes is None:
            self.http.call("DELETE", f"/UserItems/{item_id}/Rating", {"userId": user_id})
        else:
            self.http.call("POST", f"/UserItems/{item_id}/Rating",
                           {"userId": user_id, "likes": "true" if likes else "false"})

    def mark_played(self, item_id, user_id):
        self.http.call("POST", f"/UserPlayedItems/{item_id}", {"userId": user_id})

    def set_position(self, item_id, user_id, ticks):
        self.http.call("POST", f"/UserItems/{item_id}/UserData", {"userId": user_id},
                       body={"PlaybackPositionTicks": ticks})

    def restore_played(self, item_id, user_id, played, ticks, count=0, date=None):
        """put back watched status, resume point, play count and last-played date in one go"""
        body = {"Played": played, "PlaybackPositionTicks": ticks}
        if count:
            body["PlayCount"] = count
        if date:
            body["LastPlayedDate"] = date
        self.http.call("POST", f"/UserItems/{item_id}/UserData", {"userId": user_id}, body=body)

    def set_favorite(self, item_id, user_id, fav):
        self.http.call("POST" if fav else "DELETE", f"/UserFavoriteItems/{item_id}", {"userId": user_id})

    def set_trial_note(self, item_id, user_id, note):
        """Put `note` at the top of the item's overview (locked so metadata refreshes keep it); None removes it."""
        item = self.http.call("GET", f"/Items/{item_id}", {"userId": user_id})
        overview = item.get("Overview") or ""
        if overview.startswith(TRIAL_MARK):
            overview = overview.split("\n\n", 1)[1] if "\n\n" in overview else ""
        locked = [f for f in (item.get("LockedFields") or []) if f != "Overview"]
        if note:
            item["Overview"] = note + ("\n\n" + overview if overview else "")
            locked.append("Overview")
        else:
            item["Overview"] = overview
        item["LockedFields"] = locked
        self.http.call("POST", f"/Items/{item_id}", body=item)

    def get_image(self, item_id, image_type, index=0):
        """the item's stored image as bytes, or None if it has none of this type"""
        status, data = self.http.raw("GET", f"/Items/{item_id}/Images/{image_type}/{index}", ok=(200, 404))
        return data if status == 200 else None

    def set_image(self, item_id, image_type, data, ctype="image/jpeg"):
        """make `data` the item's first image of this type (body is base64, Jellyfin's quirk). An upload
        APPENDS a backdrop (ImageSaver: index = count), so it's moved to the front and the old first one
        (now index 1) deleted - in that order, so a failure part-way leaves an extra backdrop, never none.
        Other backdrops stay as they were."""
        body = base64.b64encode(data)
        if image_type != "Backdrop":
            self.http.raw("POST", f"/Items/{item_id}/Images/{image_type}", data=body, ctype=ctype)
            return
        n = len(self.http.call("GET", f"/Items/{item_id}").get("BackdropImageTags") or [])
        self.http.raw("POST", f"/Items/{item_id}/Images/Backdrop", data=body, ctype=ctype)
        if n:
            self.http.call("POST", f"/Items/{item_id}/Images/Backdrop/{n}/Index", {"newIndex": 0})
            self.http.call("DELETE", f"/Items/{item_id}/Images/Backdrop/1")

    def notify_paths(self, created=(), deleted=()):
        ups = [{"Path": p, "UpdateType": "Created"} for p in created] + \
              [{"Path": p, "UpdateType": "Deleted"} for p in deleted]
        if ups:
            self.http.call("POST", "/Library/Media/Updated", body={"Updates": ups})

    def authenticate(self, username, password):
        r = self.anon.call("POST", "/Users/AuthenticateByName",
                           body={"Username": username, "Pw": password}, ok=(200, 401))
        if not r or "User" not in r:
            return None
        u = r["User"]
        return {"id": u["Id"], "name": u["Name"], "admin": bool((u.get("Policy") or {}).get("IsAdministrator"))}


class Seerr:
    def __init__(self, url, key, transport=urllib_transport):
        self.http = Http(url.rstrip("/") + "/api/v1", {"X-Api-Key": key}, transport)

    def trending_tv(self, pages=3):
        out = []
        for page in range(1, pages + 1):
            out += [r for r in self.http.call("GET", "/discover/trending", {"page": page})["results"]
                    if r.get("mediaType") == "tv"]
        return out

    def popular_tv(self, pages=2):
        out = []
        for page in range(1, pages + 1):
            out += [dict(r, mediaType="tv") for r in self.http.call("GET", "/discover/tv", {"page": page})["results"]]
        return out

    def tv(self, tmdb):
        return self.http.call("GET", f"/tv/{tmdb}")

    def trending_movies(self, pages=3):
        out = []
        for page in range(1, pages + 1):
            out += [r for r in self.http.call("GET", "/discover/trending", {"page": page})["results"]
                    if r.get("mediaType") == "movie"]
        return out

    def popular_movies(self, pages=2):
        out = []
        for page in range(1, pages + 1):
            out += [dict(r, mediaType="movie") for r in self.http.call("GET", "/discover/movies", {"page": page})["results"]]
        return out

    def movie(self, tmdb):
        """details incl. `releases` = TMDb release_dates: results[].release_dates[] {type, release_date}"""
        return self.http.call("GET", f"/movie/{tmdb}")

    def requested_since(self, tmdb, since_iso):
        r = self.http.call("GET", "/request", {"take": 100, "skip": 0, "sort": "added", "filter": "all"})
        return any(x.get("type") == "tv" and (x.get("media") or {}).get("tmdbId") == tmdb
                   and x.get("createdAt", "")[:19] > since_iso[:19] for x in r["results"])


class Ntfy:
    def __init__(self, url, topic, transport=urllib_transport):
        self.url, self.topic, self.transport = url.rstrip("/"), topic, transport

    def send(self, title, message):
        if not self.topic:
            return
        status, raw = self.transport("POST", f"{self.url}/{self.topic}", {"Title": title}, message.encode())
        if status >= 300:
            raise ApiError(f"ntfy -> HTTP {status}: {raw[:200]!r}")
