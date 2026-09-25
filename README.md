# trials

Weekly "trial shows" for a Jellyfin + Sonarr + Seerr stack. Every Monday it adds the first
3 episodes of 3 TMDb-trending shows to a separate **Trials** library. Users vote 👍/👎 on a
small page. After 21 days, shows most people liked are moved to their permanent library
and completed; the rest are deleted. It only ever touches shows it added itself.

Design: `docs/superpowers/specs/2026-09-25-trial-shows-design.md`.

## Run
    pip install -e ".[dev]"
    python -m pytest tests/
    python -m trials probe      # read-only API check
    python -m trials serve      # web page + scheduler

Configuration is by environment variable (see `trials/config.py`). `TRIALS_ENFORCE=0`
(the default) means decisions are only reported, never carried out.
