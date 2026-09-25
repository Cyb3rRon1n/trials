from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .library import choose_destination

TAG = "trial"
UNAVAILABLE_COOLDOWN = timedelta(days=30)


@dataclass
class Clients:
    sonarr: object
    jellyfin: object
    seerr: object
    ntfy: object


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def aired_enough(details, n):
    last = details.get("lastEpisodeToAir") or {}
    season, episode = last.get("seasonNumber") or 0, last.get("episodeNumber") or 0
    return season > 1 or (season == 1 and episode >= n)


def trial_eps(episodes, n):
    return [e for e in episodes if e["seasonNumber"] == 1 and 1 <= e["episodeNumber"] <= n]


def setup_trial(c, rec, episodes, n):
    trial = [e["id"] for e in trial_eps(episodes, n)]
    if not trial:
        rec["setup_done"] = False
        return False
    sid = rec["sonarr_id"]
    c.sonarr.set_season_monitored(sid, 1, True)
    c.sonarr.set_monitored([e["id"] for e in episodes if e["id"] not in trial], False)
    c.sonarr.set_monitored(trial, True)
    c.sonarr.search_episodes(trial)
    rec["setup_done"] = True
    return True


def _skip_set(st, now):
    skip = set(st["rejected"])
    for key, rec in st["shows"].items():
        dropped = rec.get("dropped_at")
        if rec.get("status") == "unavailable" and dropped and now - parse(dropped) > UNAVAILABLE_COOLDOWN:
            continue
        skip.add(int(key))
    return skip


def weekly_add(cfg, c, st, now):
    free = c.sonarr.free_bytes(cfg.trials_root)
    if free < cfg.min_free_tb * 1e12:
        return [f"skipped weekly add: only {free / 1e12:.2f} TB free (< {cfg.min_free_tb} TB)"]
    skip = _skip_set(st, now) | {s["tvdbId"] for s in c.sonarr.series()}
    tag = c.sonarr.tag_id(TAG)
    profile = c.sonarr.quality_profile_id(cfg.quality_profile)
    lines = []
    for item in c.seerr.trending_tv():
        if len(lines) >= cfg.trials_per_week:
            break
        det = c.seerr.tv(item["id"])
        tvdb = (det.get("externalIds") or {}).get("tvdbId")
        if not tvdb or tvdb in skip or not aired_enough(det, cfg.trial_episodes):
            continue
        lookup = c.sonarr.lookup_tvdb(tvdb)
        if not lookup:
            continue
        s = c.sonarr.add_series(lookup, profile, cfg.trials_root, tag)
        rec = st["shows"][str(tvdb)] = {
            "tvdb": tvdb, "tmdb": item["id"], "title": s["title"], "sonarr_id": s["id"], "path": s["path"],
            "added_at": iso(now), "window_start": None, "status": "active",
            "dest": choose_destination([g["name"] for g in det.get("genres") or []], det.get("originCountry") or [],
                                       cfg.tv_root, cfg.anime_root, cfg.drama_root),
            "played": None, "reported": False, "setup_done": False, "dry_run": None}
        skip.add(tvdb)
        setup_trial(c, rec, c.sonarr.episodes(s["id"]), cfg.trial_episodes)
        lines.append(f"trial added: {s['title']} (S01E01-E{cfg.trial_episodes:02d}) -> Trials library")
    return lines
