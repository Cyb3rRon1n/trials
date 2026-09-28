# trials

Weekly "trial shows" for a Jellyfin + Sonarr + Seerr stack. Every Monday it adds the first
half of season 1 of 3 TMDb-trending shows to a separate **Trials** library. Users vote 👍/👎 on a
small page. After 14 days, shows most people liked are moved to their permanent library
and completed; the rest are deleted. It only ever touches shows it added itself.

Design: `docs/superpowers/specs/2026-09-25-trial-shows-design.md`.

## Run
    pip install -e ".[dev]"
    python -m pytest tests/
    python -m trials probe      # read-only API check
    python -m trials serve      # web page + scheduler

Configuration is by environment variable (see `trials/config.py`). `TRIALS_ENFORCE=0`
(the default) means decisions are only reported, never carried out.

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
