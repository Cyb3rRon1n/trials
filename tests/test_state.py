import json
import pytest
from trials import state


def test_load_missing_returns_empty(tmp_path):
    assert state.load(str(tmp_path / "s.json")) == state.empty()


def test_save_load_roundtrip_and_defaults_merged(tmp_path):
    p = str(tmp_path / "s.json")
    state.save(p, {"shows": {"1": {"title": "X"}}})
    st = state.load(p)
    assert st["shows"]["1"]["title"] == "X" and st["rejected"] == [] and st["last_add_week"] is None


def test_save_is_atomic_no_tmp_left(tmp_path):
    p = str(tmp_path / "s.json")
    state.save(p, state.empty())
    assert sorted(x.name for x in tmp_path.iterdir()) == ["s.json"]


def test_locked_saves_even_on_error(tmp_path):
    p = str(tmp_path / "s.json")
    with pytest.raises(RuntimeError):
        with state.locked(p) as st:
            st["rejected"].append(42)
            raise RuntimeError("boom")
    assert json.load(open(p))["rejected"] == [42]
