# Trial Shows — design

**Date:** 2026-09-25 · **Host:** cyberpac (192.168.10.157) · **Status:** approved design, not built

## Goal

Every week, automatically add the first 3 episodes of a few trending TV shows to Jellyfin. Let users vote 👍/👎. After 3 weeks, keep and complete the shows that most of the people who tried them liked, and delete the rest.

## Scope: the rule that matters most

- The system **only ever judges or deletes shows it added itself.** Existing library shows and anything a user requests (through Seerr, Sonarr, or by hand) are never judged, however long they sit unwatched.
- Two checks enforce this. A show must be **both** tagged `trial` in Sonarr **and** listed in the app's own state file. Only the app's weekly job applies the tag.
- If a user requests a show that is currently on trial (a Seerr request, or manually monitoring more of it in Sonarr), the request counts as keeping it: the show leaves the trial immediately and is treated as a normal requested show. No vote is taken.

## Components

One small Python service (`trials`), a single container in the cyberpac stack. It has three parts:

1. **Weekly add job** (Mondays):
   - Fetch TMDb trending TV through Seerr's API (`/api/v1/discover/trending`, filtered to `mediaType == "tv"`). No new accounts are needed.
   - Skip a show if it is already in Sonarr, on the rejected list, has fewer than 3 aired episodes, or has no TVDB id.
   - Add the top **3** to Sonarr with tag `trial` under the root folder **`/data/media/trials`**. Monitor only **S01E01–E03**, and search for them.
   - Record each show in the state file with `added_at`.
   - Skip the whole week if `/mnt/media` has less than **1 TB** free.
2. **Voting page**: `trials.totallylegitmedia.us`, behind Authelia, with a Homepage tile.
   - Users sign in with their **Jellyfin username and password** (`/Users/AuthenticateByName`), so there is one vote per real account.
   - For each trial it lists the poster, days left, **your own** current vote, 👍/👎 buttons (no live tally: it would cost ~80 Jellyfin calls per page load), and a "watch in Jellyfin" link.
   - A vote writes Jellyfin's per-user `Likes` field (`POST /UserItems/{itemId}/Rating?likes=true|false`). There is no separate vote database.
   - It also shows what dry-run would decide, and lets someone take a show off the rejected list.
3. **Daily decide job:** for every trial whose voting window has ended, tally the votes and act on the result (see below).

## Trials library (so temporary shows are obvious)

- Trial shows live in their own folder, `/data/media/trials`, added as a Sonarr root folder. That folder is its own Jellyfin **"Trials"** library (type: tvshows). It shows up as a separate home-screen tile and "Recently Added in Trials" row in every Jellyfin app. The permanent libraries (Shows, Anime, Drama) never contain trial shows.
- The voting page and its Homepage tile say (Jellyfin libraries have no visible description field) that shows here are temporary and link to the voting page. The voting page shows each show's "vote by <date>".
- **On keep**, the show moves out of Trials into its permanent library:
  - Destination: **Anime** (`/data/media/anime`) if TMDb genre includes Animation and origin country is JP. **Drama** (`/data/media/drama`) if origin is KR/CN/TW/JP and it isn't animation. Otherwise **Shows** (`/data/media/tv`).
  - Sonarr series edit with the new `rootFolderPath` and `moveFiles=true`. It's the same filesystem (md0), so the move is an instant rename.
  - **Watched status carried over:** before the move, record each user's played flag and position for the trial episodes, keyed by S/E. After Jellyfin has scanned the new location (poll until the series appears there, up to 30 min), write the same played state onto the new items (`POST /UserPlayedItems/{id}` or a position update). Votes don't need carrying over, because the decision is already made.
  - The show then gets full monitoring and a search, as described under Decision rules.
- **On reject**, the show is deleted as usual. Its files are in `trials/`, so nothing in a permanent library is involved.

## State

- **Sonarr:** the `trial` tag and the series monitoring settings.
- **Jellyfin:** votes in `UserData.Likes`, and viewing in played status and play position.
- **App state file** (`/data/state.json`): `{tvdbId: {sonarrId, added_at, window_start, status}}` plus a `rejected: [tvdbId]` list. This file is the second half of the safety check. It is small and easy to back up.

## Timing

- The voting window **starts when the 3 trial episodes are in Jellyfin**, not when the show is added, so a slow download doesn't use up voting time. The window lasts **21 days**.
- If the episodes never arrive within 14 days of being added, the show is dropped as unavailable. That is recorded as `unavailable`, not `rejected`, so it can be tried again later.

## Decision rules (per trial, at the end of its window)

For each Jellyfin user:
1. An explicit vote (`Likes` = true/false) counts as that vote.
2. With no vote but at least one trial episode watched (played, or position past 0): finishing all 3 trial episodes counts as a like, and stopping after 1–2 counts as a dislike.
3. A user who never watched is not counted.

Then:
- likes ≥ dislikes, with at least one person engaged → **keep**. A tie keeps the show.
- dislikes > likes, **or** nobody engaged → **reject**.

- **Keep:** move the show to its permanent library and carry over watched status (see Trials library). Then remove the `trial` tag, monitor the whole series, and search for missing episodes. Remove the show from the active trials in state.
- **Reject:** delete the series in Sonarr with `deleteFiles=true` and `addImportListExclusion=true`, and add it to `rejected`.

## Safety

- **Dry-run by default.** The first cycle only reports decisions (log, ntfy, voting page). Real deletion needs `TRIALS_ENFORCE=1`.
- **Deletion ceiling.** At most 3 deletions per run. If more are due, delete nothing, send an ntfy alert, and wait for a human.
- **Fail closed.** If Sonarr, Jellyfin or Seerr is unreachable or returns an error, do nothing that run. Missing data is never read as "nobody watched".
- **Double-scope check** before any change: the show must carry the `trial` tag **and** be in the state file.
- **Notifications:** keep/reject decisions and safety alerts go to a **new** ntfy topic `cyberpac-trials-<random suffix>`, with its own Homepage ntfy widget. They are kept separate from cyberbox drive alerts so disk warnings don't get lost among show decisions.

## Config (env)

`SEERR_URL/KEY`, `SONARR_URL/KEY`, `JELLYFIN_URL/KEY`, `NTFY_URL/TOPIC`, `TRIALS_PER_WEEK=3`, `TRIAL_EPISODES=3`, `WINDOW_DAYS=21`, `MIN_FREE_TB=1`, `MAX_DELETES_PER_RUN=3`, `TRIALS_ENFORCE=0`.

## Deployment

- One-time setup:
  - Create `/mnt/media/media/trials`, owned by 1000:1000.
  - Add it as a Sonarr root folder, visible in-container as `/data/media/trials`.
  - Create the Jellyfin **Trials** library pointing to it, with the same metadata settings as Shows.
- Container defined in cyberpac's `~/vulcan/stack/docker-compose.override.yml`, the same pattern as byparr, because vulcan doesn't generate it.
- Traefik labels and Authelia on the stack network. Homepage tile in `services.yaml`.
- Scheduling: an in-process scheduler, so there's no host cron and it all lives in one container.
- Code location: decided in the implementation plan (options: a small new repo, or `~/trials` on cyberpac).

## Testing

- **Self-test** of the tally and decision function with fake data. Cases: tie, nobody engaged, mixed voting and viewing, a vote overriding viewing, a show requested mid-trial, a show missing from the state file, a show missing the tag, and the deletion ceiling being hit. Also the destination-library choice: anime, JP/KR drama, and default. And the watched-status mapping by S/E between old and new item ids.
- **Keep-move rehearsal** in dry-run: log the planned destination and the played-state mapping for each kept show without moving anything. The first real keep is watched end to end: the folder moves, Jellyfin shows the show in the right library, and watched marks are intact.
- **Live dry-run** against the real Sonarr, Jellyfin and Seerr: one weekly add and one decision pass, checking each step. `TRIALS_ENFORCE` stays 0 until the user approves.

## Out of scope (YAGNI)

- Anime charts (AniList/MAL).
- Buttons inside the Jellyfin apps themselves.
- Movies.
- Per-user recommendations.
