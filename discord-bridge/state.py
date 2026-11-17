"""The bridge's own state: which Discord thread belongs to which OpenCode
session, and the settings people chose with slash commands.

threads.json:
  {"threads": {"<thread id>": {"sessionId", "directory", "createdBy", "createdAt",
                                "model", "effort", "agent", "turn"}},
   "defaults": {"model", "effort", "agent"},
   "questions": {"<request id>": {...}}}

The three settings are optional everywhere. A missing one means "whatever the
agent's own config says". `defaults` is what a new thread starts with; it is
copied into the thread when the thread is opened, so changing it later leaves
existing threads alone.

`turn` is present only while a request is being worked on. It is how a
restarted bridge knows which requests to pick back up. `questions` holds the
agent's questions that people have not answered yet (see questions.py).
"""

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("bridge")

# model is "<provider id>/<model id>", effort is an OpenCode variant name,
# agent is an OpenCode agent name.
SETTINGS = ("model", "effort", "agent")


def settings_of(entry):
    """The settings stored on a thread entry (or on the defaults)."""
    return {name: entry[name] for name in SETTINGS if isinstance(entry.get(name), str) and entry[name]}


def set_setting(entry, name, value):
    """Stores a setting, or clears it when value is None."""
    if value:
        entry[name] = value
    else:
        entry.pop(name, None)


def usable_question(entry):
    """True for a stored question request that has everything the buttons need."""
    if not isinstance(entry, dict) or not isinstance(entry.get("thread"), str):
        return False
    lists = [entry.get("questions"), entry.get("answers"), entry.get("messages")]
    return all(isinstance(item, list) for item in lists) and len(lists[0]) == len(lists[1])


class Store:
    def __init__(self, path, base_dir):
        self.path = path
        self.base_dir = base_dir
        self.threads = {}
        self.defaults = {}
        self.questions = {}

    def _set_aside(self, problem):
        # Moved rather than left in place, where the next save would overwrite
        # it, so the thread map can still be recovered by hand.
        bad = f"{self.path}.bad-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        try:
            os.replace(self.path, bad)
            log.error("%s %s; moved it to %s and started with no threads", self.path, problem, bad)
        except OSError as err:
            log.error("%s %s, and could not move it aside: %s", self.path, problem, err)

    def load(self):
        loaded = None
        try:
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
        except FileNotFoundError:
            pass
        except ValueError as err:  # includes undecodable bytes
            self._set_aside(f"is not valid JSON ({err})")
        except OSError as err:
            log.error("could not read %s: %s", self.path, err)
        threads = loaded.get("threads") if isinstance(loaded, dict) else None
        if loaded not in (None, {}) and not isinstance(threads, dict):
            self._set_aside("has no thread map")
        if not isinstance(threads, dict):
            threads = {}
        self.threads = {key: entry for key, entry in threads.items() if isinstance(entry, dict)}
        defaults = loaded.get("defaults") if isinstance(loaded, dict) else None
        self.defaults = settings_of(defaults) if isinstance(defaults, dict) else {}
        questions = loaded.get("questions") if isinstance(loaded, dict) else None
        questions = questions if isinstance(questions, dict) else {}
        self.questions = {key: entry for key, entry in questions.items() if usable_question(entry)}
        # Threads from an earlier layout (a worktree per thread, then the repo
        # clone) move to the sandbox and get a fresh session on their next message.
        for entry in self.threads.values():
            entry.pop("movedToSharedClone", None)  # the old name of movedFolder
            if entry.get("directory") != self.base_dir:
                entry.update(directory=self.base_dir, sessionId=None, movedFolder=True)

    def save(self):
        # Synchronous on purpose: the file is tiny, and nothing else runs on
        # the event loop while it is written, so saves never interleave.
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"threads": self.threads, "defaults": self.defaults, "questions": self.questions}, f, indent=2)
            os.replace(tmp, self.path)
        except OSError as err:
            log.error("could not save state: %s", err)

    def register_thread(self, thread_id, author_id):
        """Adds a new thread, starting from the channel defaults. Its session is created later."""
        entry = {
            "sessionId": None,
            "directory": self.base_dir,
            "createdBy": str(author_id),
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            **self.defaults,
        }
        self.threads[str(thread_id)] = entry
        self.save()
        return entry
