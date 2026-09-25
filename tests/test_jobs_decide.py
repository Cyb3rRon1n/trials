from datetime import timedelta
from trials import state
from trials.jobs import Clients, daily_decide, iso, weekly_add
from fakes import FakeJellyfin, FakeNtfy, FakeSeerr, FakeSonarr, NOW, ep, make_cfg


def world(tmp_path, added_days_ago=30, **kw):
    cfg = make_cfg(tmp_path, **kw)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy())
    st = state.empty()
    c.seerr.add_show(5, 1005, "Plain Show")
    c.sonarr.lookups[1005] = {"title": "Plain Show", "tvdbId": 1005}
    weekly_add(cfg, c, st, NOW - timedelta(days=added_days_ago))
    rec = st["shows"]["1005"]
    return cfg, c, st, rec, rec["sonarr_id"]


def arrive(c, sid):
    for e in c.sonarr.eps[sid][:3]:
        e["hasFile"] = True


def open_window_ended(c, st, rec, sid):
    arrive(c, sid)
    rec["window_start"] = iso(NOW - timedelta(days=22))
    c.jellyfin.index[rec["path"]] = {"Id": "jf1"}


def test_window_starts_when_all_trial_eps_arrive(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path, added_days_ago=1)
    daily_decide(cfg, c, st, NOW)
    assert rec["window_start"] is None
    arrive(c, sid)
    # Window doesn't open without Jellyfin path
    lines = daily_decide(cfg, c, st, NOW)
    assert rec["window_start"] is None
    # Window opens once path is in Jellyfin
    c.jellyfin.index[rec["path"]] = {"Id": "jf1"}
    lines = daily_decide(cfg, c, st, NOW)
    assert rec["window_start"] == iso(NOW) and "voting open" in lines[0]


def test_unavailable_after_arrival_days_is_deleted_without_exclusion(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    daily_decide(cfg, c, st, NOW)          # added 30 days ago, never arrived
    assert ("delete", sid, False) in c.sonarr.calls
    assert rec["status"] == "unavailable" and 1005 not in st["rejected"]


def test_majority_like_keeps_moves_then_restores_watched(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    open_window_ended(c, st, rec, sid)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1, True), ep("e2", 2, True), ep("e3", 3, True)]
    c.jellyfin.eps[("jf1", "u2")] = [ep("e1", 1, False, 900)]
    c.jellyfin.like[("jf1", "u2")] = True           # watched a bit, voted 👍
    lines = daily_decide(cfg, c, st, NOW)
    assert ("move", sid, "/data/media/tv") in c.sonarr.calls and ("monitor_all", sid) in c.sonarr.calls
    assert ("untag", sid) in c.sonarr.calls and rec["status"] == "moving"
    assert any("KEPT" in l for l in lines)
    # next day Jellyfin has scanned the new location
    c.jellyfin.index = {"/data/media/tv/Plain Show": {"Id": "jf2"}}
    c.jellyfin.eps[("jf2", "u1")] = [ep("n1", 1), ep("n2", 2), ep("n3", 3)]
    daily_decide(cfg, c, st, NOW + timedelta(days=1))
    played = sorted(x for x in c.jellyfin.calls if x[0] in ("played", "pos"))
    assert played == [("played", "n1", "u1"), ("played", "n2", "u1"), ("played", "n3", "u1"), ("pos", "n1", "u2", 900)]
    assert rec["status"] == "kept" and rec["played"] is None


def test_majority_dislike_rejects_with_exclusion(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    open_window_ended(c, st, rec, sid)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1, True), ep("e2", 2), ep("e3", 3)]      # watched 1, quit -> dislike
    lines = daily_decide(cfg, c, st, NOW)
    assert ("delete", sid, True) in c.sonarr.calls and rec["status"] == "rejected"
    assert st["rejected"] == [1005] and any("REJECTED" in l for l in lines)


def test_nobody_engaged_rejects(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    open_window_ended(c, st, rec, sid)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1), ep("e2", 2), ep("e3", 3)]  # no engagement
    daily_decide(cfg, c, st, NOW)
    assert ("delete", sid, True) in c.sonarr.calls and rec["status"] == "rejected"
    assert st["rejected"] == [1005]


def test_dry_run_reports_once_and_changes_nothing(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path, enforce=False)
    open_window_ended(c, st, rec, sid)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1), ep("e2", 2), ep("e3", 3)]  # no engagement
    first = daily_decide(cfg, c, st, NOW)
    second = daily_decide(cfg, c, st, NOW)
    assert any("[dry-run] would DELETE" in l for l in first) and second == []
    assert not [x for x in c.sonarr.calls if x[0] in ("delete", "move", "monitor_all", "untag")]
    assert rec["status"] == "active" and rec["dry_run"].startswith("would DELETE")


def test_tag_removed_by_user_releases_without_touching(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    open_window_ended(c, st, rec, sid)
    c.sonarr.series_db[sid]["tags"] = []
    daily_decide(cfg, c, st, NOW)
    assert rec["status"] == "released"
    assert not [x for x in c.sonarr.calls if x[0] in ("delete", "move")]


def test_seerr_request_mid_trial_keeps_immediately(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    arrive(c, sid)
    rec["window_start"] = iso(NOW)               # window far from over
    c.seerr.requested.add(5)
    lines = daily_decide(cfg, c, st, NOW)
    assert ("move", sid, "/data/media/tv") in c.sonarr.calls and "requested" in lines[0]


def test_user_monitoring_more_episodes_keeps(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    arrive(c, sid)
    rec["window_start"] = iso(NOW)
    c.sonarr.eps[sid][5]["monitored"] = True      # someone monitored E06 in Sonarr
    daily_decide(cfg, c, st, NOW)
    assert rec["status"] == "moving"


def test_trial_tagged_series_not_in_state_is_ignored(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    c.sonarr.add_existing(999, 4242, "Hand Tagged", tags=[1])
    open_window_ended(c, st, rec, sid)
    daily_decide(cfg, c, st, NOW)
    assert 999 in c.sonarr.series_db and not [x for x in c.sonarr.calls if x[1:2] == (999,)]


def test_deletion_ceiling_deletes_nothing(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path, max_deletes_per_run=0)
    open_window_ended(c, st, rec, sid)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1), ep("e2", 2), ep("e3", 3)]  # no engagement -> delete
    lines = daily_decide(cfg, c, st, NOW)
    assert "SAFETY STOP" in lines[0] and sid in c.sonarr.series_db and rec["status"] == "active"


def test_new_episode_listed_after_setup_does_not_keep(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    arrive(c, sid)
    c.jellyfin.index[rec["path"]] = {"Id": "jf1"}
    daily_decide(cfg, c, st, NOW)  # Window opens
    # New episode listed after setup
    c.sonarr.eps[sid].append({"id": 99999, "seasonNumber": 1, "episodeNumber": 11, "monitored": True, "hasFile": False})
    rec["window_start"] = iso(NOW - timedelta(days=5))  # Window not ended yet
    daily_decide(cfg, c, st, NOW)
    assert rec["status"] == "active"
    assert not [x for x in c.sonarr.calls if x[0] == "move"]


def test_missing_jellyfin_episodes_postpones_instead_of_rejecting(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    open_window_ended(c, st, rec, sid)
    # No episodes for any user
    lines = daily_decide(cfg, c, st, NOW)
    assert rec["status"] == "active"
    assert not [x for x in c.sonarr.calls if x[0] == "delete"]
    assert any("decision postponed" in l for l in lines)


def test_drop_is_recorded_even_if_jellyfin_notify_fails(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    open_window_ended(c, st, rec, sid)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1, True), ep("e2", 2), ep("e3", 3)]  # dislike
    c.jellyfin.notify_fails = True
    lines = daily_decide(cfg, c, st, NOW)
    assert rec["status"] == "rejected"
    assert 1005 in st["rejected"]
    assert any("Jellyfin not notified" in l for l in lines)


def test_keep_interrupted_resumes_next_run(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    open_window_ended(c, st, rec, sid)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1, True), ep("e2", 2, True), ep("e3", 3, True)]  # like
    c.jellyfin.like[("jf1", "u1")] = True
    # First run: make remove_tag raise on first call only
    c.sonarr.remove_tag_fails = True
    try:
        daily_decide(cfg, c, st, NOW)
    except RuntimeError:
        pass
    assert rec["status"] == "moving" and rec.get("completed") and not rec.get("untagged")
    # Second run: remove_tag succeeds
    daily_decide(cfg, c, st, NOW + timedelta(days=1))
    move_calls = [x for x in c.sonarr.calls if x[0] == "move"]
    assert len(move_calls) == 1  # Only one move call total
    assert ("untag", sid) in c.sonarr.calls


def test_finish_move_waits_for_all_episodes_then_gives_up_honestly(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    open_window_ended(c, st, rec, sid)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1, True), ep("e2", 2, True), ep("e3", 3, True)]  # like
    daily_decide(cfg, c, st, NOW)
    # Jellyfin scans new location but only has first episode
    c.jellyfin.index = {"/data/media/tv/Plain Show": {"Id": "jf2"}}
    c.jellyfin.eps[("jf2", "u1")] = [ep("n1", 1)]
    lines = daily_decide(cfg, c, st, NOW + timedelta(days=1))
    assert rec["status"] == "moving"  # Still waiting
    # After 7 days, give up
    lines = daily_decide(cfg, c, st, NOW + timedelta(days=8))
    assert rec["status"] == "kept"
    assert any("except" in l and "7 days" in l for l in lines)


def test_request_before_window_with_setup_pending_keeps(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    # Setup pending: Sonarr has no episodes yet
    c.sonarr.eps[sid] = []
    rec["setup_done"] = False
    arrive(c, sid)
    c.jellyfin.index[rec["path"]] = {"Id": "jf1"}
    # Give episodes and add to requested
    c.sonarr.eps[sid] = [{"id": i, "seasonNumber": 1, "episodeNumber": i, "monitored": False, "hasFile": True}
                         for i in range(1, 4)]
    c.seerr.requested.add(5)
    lines = daily_decide(cfg, c, st, NOW)
    assert ("move", sid, "/data/media/tv") in c.sonarr.calls


def test_dry_run_safety_stop_and_label_change(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path, enforce=False, max_deletes_per_run=0)
    open_window_ended(c, st, rec, sid)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1), ep("e2", 2), ep("e3", 3)]  # dislike -> delete
    first = daily_decide(cfg, c, st, NOW)
    assert any("would hit SAFETY STOP" in l for l in first)
    assert any("[dry-run] would DELETE" in l for l in first)
    # Change verdict: give user a like
    c.jellyfin.like[("jf1", "u1")] = True
    second = daily_decide(cfg, c, st, NOW)
    assert any("[dry-run] would KEEP" in l for l in second)
