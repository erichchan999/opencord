import json

from state import Store, set_setting, settings_of

BASE = "/work/sandbox"


def write(path, data):
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def load(path):
    store = Store(str(path), BASE)
    store.load()
    return store


def test_missing_file_starts_empty(tmp_path):
    store = load(tmp_path / "threads.json")
    assert store.threads == {} and store.defaults == {}


def test_file_from_before_settings_loads_unchanged(tmp_path):
    path = tmp_path / "threads.json"
    entry = {"sessionId": "ses_1", "directory": BASE, "createdBy": "42", "createdAt": "2026-11-01T00:00:00.000Z"}
    write(path, {"threads": {"100": entry}})
    store = load(path)
    assert store.threads == {"100": entry}
    assert store.defaults == {}
    assert settings_of(store.threads["100"]) == {}


def test_settings_and_defaults_survive_a_save(tmp_path):
    path = tmp_path / "threads.json"
    store = load(path)
    set_setting(store.defaults, "model", "deepseek-proxy/deepseek-v4-pro")
    entry = store.register_thread(100, 42)
    set_setting(entry, "effort", "max")
    store.save()
    again = load(path)
    assert again.defaults == {"model": "deepseek-proxy/deepseek-v4-pro"}
    assert settings_of(again.threads["100"]) == {"model": "deepseek-proxy/deepseek-v4-pro", "effort": "max"}


def test_new_thread_copies_the_defaults_and_then_stands_alone(tmp_path):
    store = load(tmp_path / "threads.json")
    set_setting(store.defaults, "agent", "plan")
    entry = store.register_thread(100, 42)
    set_setting(store.defaults, "agent", "build")
    assert entry["agent"] == "plan"
    assert entry["sessionId"] is None and entry["directory"] == BASE and entry["createdBy"] == "42"


def test_threads_from_another_directory_move_to_the_sandbox(tmp_path):
    path = tmp_path / "threads.json"
    threads = {
        "100": {"sessionId": "ses_1", "directory": "/work/repo", "model": "a/b"},
        # Moved from a worktree to the repo clone, with no message since.
        "101": {"sessionId": None, "directory": "/work/repo", "movedToSharedClone": True},
        "102": {"sessionId": "ses_2", "directory": BASE},
    }
    write(path, {"threads": threads})
    loaded = load(path).threads
    for key in ("100", "101"):
        entry = loaded[key]
        assert entry["directory"] == BASE and entry["sessionId"] is None and entry["movedFolder"] is True
        assert "movedToSharedClone" not in entry
    assert loaded["100"]["model"] == "a/b"
    assert loaded["102"] == {"sessionId": "ses_2", "directory": BASE}


def test_invalid_json_is_set_aside_not_overwritten(tmp_path):
    path = tmp_path / "threads.json"
    write(path, "{not json")
    store = load(path)
    assert store.threads == {}
    assert not path.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("threads.json.bad-")]


def test_file_without_a_thread_map_is_set_aside(tmp_path):
    path = tmp_path / "threads.json"
    write(path, {"something": "else"})
    assert load(path).threads == {}
    assert not path.exists()


def test_junk_entries_and_junk_defaults_are_ignored(tmp_path):
    path = tmp_path / "threads.json"
    write(path, {"threads": {"100": "oops", "200": {"directory": BASE}}, "defaults": {"model": 5, "effort": "low", "x": "y"}})
    store = load(path)
    assert list(store.threads) == ["200"]
    assert store.defaults == {"effort": "low"}


def test_set_setting_clears_with_none():
    entry = {"model": "a/b"}
    set_setting(entry, "model", None)
    set_setting(entry, "effort", None)
    assert entry == {}
