# trials

Weekly "trial shows" for a Jellyfin + Sonarr + Seerr (+ Radarr) stack. It only ever touches what it
added itself (or what an admin approved into the trials folder).

**Selection.** Every Monday it picks from Seerr's trending + popular lists, ranked half by how well a
title matches what users watch, 👍 and ♥ and half by trending position:
- up to `TRIALS_PER_WEEK` (3) **brand-new shows**: exactly one season (specials aside), premiered in the
  last `NEW_DAYS` (30) days, at least one episode out;
- with Radarr configured, up to `MOVIES_PER_WEEK` (2) **new movies**: digital release (physical if
  there is none) in the last `NEW_DAYS` days.
Nothing already in Sonarr/Radarr, rejected before, or trialled before is picked.

**Trials.** A show's trial is the whole of season 1: all of it is monitored, so episodes airing
during the trial download too. Everything sits in one mixed **On Trial** library. Voting opens once
the movie, or every aired episode, is in; a show also opens after `ARRIVAL_DAYS` (14) with whatever
arrived, and is dropped if nothing has. Users vote 👍/👎 on a small page for `WINDOW_DAYS` (21) days:
keep it permanently (a kept show follows future seasons too) or drop it. ♥ counts double; no vote but
finished it (3 episodes, or all there were) counts as a keep. Kept titles move to their permanent
library with everyone's watch history; dropped ones are deleted.

Design: `docs/superpowers/specs/2026-09-25-trial-shows-design.md`.

## Run
    pip install -e ".[dev]"
    python -m pytest tests/
    python -m trials probe      # read-only API check
    python -m trials serve      # web page + scheduler

Configuration is by environment variable (see `trials/config.py`). `TRIALS_ENFORCE=0`
(the default) means decisions are only reported, never carried out, and new movie trials are only
listed, not added (new show trials still are).

## Operating it

- Dry-run first. Turn on real deletions by setting `TRIALS_ENFORCE=1` in `.env` and recreating
  the container (`docker compose up -d trials`).
- **SAFETY STOP:** when more than `MAX_DELETES_PER_RUN` deletions are due (common right after
  enabling enforce, or after an outage), nothing is deleted and an alert is sent. To clear a
  backlog you've reviewed on the voting page, temporarily set `MAX_DELETES_PER_RUN` higher,
  recreate the container, run `docker exec trials python -m trials decide`, then set it back.
- Manual `add`/`decide` via `docker exec` are safe while the service runs (state is file-locked).
- Removing the `trial` tag from a series in Sonarr (or deleting the tag) hands that show back to
  you: it's never judged again.
- Every Jellyfin user needs access to the Trials library, including users created later.
