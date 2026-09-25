import contextlib
import json
import os
import threading

_LOCK = threading.Lock()


def empty():
    return {"shows": {}, "rejected": [], "last_add_week": None, "last_decide_date": None}


def load(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty()
    st = empty()
    st.update(data)
    return st


def save(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


@contextlib.contextmanager
def locked(path):
    with _LOCK:
        st = load(path)
        try:
            yield st
        finally:
            save(path, st)
