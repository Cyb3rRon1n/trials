from datetime import datetime, timedelta, timezone
import threading
from trials import state
from trials.jobs import Clients, iso
from trials.main import due, run_job, scheduler
from fakes import FakeJellyfin, FakeNtfy, FakeSeerr, FakeSonarr, make_cfg, NOW, ep
from trials.jobs import daily_decide, weekly_add


MON_0930 = datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)
MON_1130 = datetime(2026, 10, 5, 11, 30, tzinfo=timezone.utc)
TUE_1130 = MON_1130 + timedelta(days=1)


def test_due_schedule():
    st = state.empty()
    assert due(MON_0930, st) == []
    assert due(MON_1130, st) == ["add", "decide"]
    st.update(last_add_week="2026-W41", last_decide_date="2026-10-05")
    assert due(MON_1130, st) == []
    assert due(TUE_1130, st) == ["decide"]


def test_due_respects_retry_after():
    st = state.empty()
    st["retry_after_decide"] = iso(MON_1130 + timedelta(minutes=30))
    assert due(MON_1130, st) == ["add"]


def test_run_job_success_marks_run_and_notifies(tmp_path):
    cfg = make_cfg(tmp_path)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy())
    c.seerr.add_show(5, 1005, "Plain Show")
    c.sonarr.lookups[1005] = {"title": "Plain Show", "tvdbId": 1005}
    lines = run_job("add", cfg, c, MON_1130)
    st = state.load(cfg.state_path)
    assert st["last_add_week"] == "2026-W41" and "1005" in st["shows"]
    assert c.ntfy.sent and "Plain Show" in c.ntfy.sent[0][1] and lines


def test_run_job_failure_sets_retry_and_keeps_partial_state(tmp_path):
    cfg = make_cfg(tmp_path)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy())

    def boom():
        raise RuntimeError("jellyfin down")
    c.jellyfin.users = boom
    lines = run_job("decide", cfg, c, MON_1130)
    st = state.load(cfg.state_path)
    assert st["last_decide_date"] is None and st["retry_after_decide"] == iso(MON_1130 + timedelta(hours=1))
    assert "aborted" in lines[-1]


def test_run_job_failure_still_reports_completed_actions(tmp_path):
    # Set up two trials: one due to keep (like), one due to reject (dislike)
    # The keep should succeed and report "KEPT", the reject should fail on delete
    cfg = make_cfg(tmp_path)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy())
    st = state.empty()

    # Add two shows
    c.seerr.add_show(5, 1005, "Keep Show")
    c.sonarr.lookups[1005] = {"title": "Keep Show", "tvdbId": 1005}
    c.seerr.add_show(6, 1006, "Reject Show")
    c.sonarr.lookups[1006] = {"title": "Reject Show", "tvdbId": 1006}

    # Add both via weekly_add
    weekly_add(cfg, c, st, NOW - timedelta(days=30))

    rec1 = st["shows"]["1005"]
    sid1 = rec1["sonarr_id"]
    rec2 = st["shows"]["1006"]
    sid2 = rec2["sonarr_id"]

    # Arrive and open voting windows for both
    for e in c.sonarr.eps[sid1][:3]:
        e["hasFile"] = True
    for e in c.sonarr.eps[sid2][:3]:
        e["hasFile"] = True

    rec1["window_start"] = iso(NOW - timedelta(days=22))
    rec2["window_start"] = iso(NOW - timedelta(days=22))

    c.jellyfin.index[rec1["path"]] = {"Id": "jf1"}
    c.jellyfin.index[rec2["path"]] = {"Id": "jf2"}

    # Set up votes: Keep Show gets a like (keep), Reject Show gets dislike (delete)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1, True), ep("e2", 2, True), ep("e3", 3, True)]
    c.jellyfin.like[("jf1", "u1")] = True

    c.jellyfin.eps[("jf2", "u1")] = [ep("f1", 1, True), ep("f2", 2), ep("f3", 3)]
    # No like = dislike

    # Save state to file so run_job can load it
    state.save(cfg.state_path, st)

    # Make delete_series raise on the first call (for Reject Show)
    def delete_with_fail(sid, exclude):
        raise RuntimeError("sonarr down")

    c.sonarr.delete_series = delete_with_fail

    lines = run_job("decide", cfg, c, NOW)

    # Check that we have both a "KEPT" line and an "aborted" line
    assert any("KEPT" in l for l in lines), f"No KEPT line in {lines}"
    assert "aborted" in lines[-1], f"No aborted line at end. Last line: {lines[-1]}"

    # Check that ntfy received the KEPT message
    assert c.ntfy.sent, "ntfy should have been called"
    ntfy_message = c.ntfy.sent[0][1]
    assert "KEPT" in ntfy_message, f"ntfy message should contain KEPT: {ntfy_message}"


def test_scheduler_survives_corrupt_state(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy())

    # Write invalid JSON to state file
    with open(cfg.state_path, 'w') as f:
        f.write("{ invalid json }")

    # Create a stop event that will trigger after one iteration
    stop = threading.Event()

    # Monkeypatch stop.wait to set the event and return so loop runs exactly once
    original_wait = stop.wait
    iterations = [0]

    def wait_once(timeout=None):
        iterations[0] += 1
        if iterations[0] >= 1:
            stop.set()
        return False

    stop.wait = wait_once

    # Call scheduler directly - should not raise despite corrupt state
    scheduler(cfg, c, stop)

    # Capture output and verify error was logged
    captured = capsys.readouterr()
    assert "trials scheduler error:" in captured.out
    assert "JSONDecodeError" in captured.out
