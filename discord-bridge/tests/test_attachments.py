"""Files the agent marks with discord_attach, gathered and posted to the thread."""

import asyncio
import types

import discord

import attachments
from events import Turn, attached_path, turn_steps
from fakes import Channel
from opencode import FileTooLarge, OpenCodeError

DIR = "/work/sandbox"


def attach(path, status="completed", call="call_1"):
    """A discord_attach tool part, as the agent stores it."""
    state = {"status": status, "input": {"path": path}}
    if status == "completed":
        state["metadata"] = {"attach": path, "size": 3}
    else:
        state["error"] = "There is no file at that path."
    return {"type": "tool", "tool": "discord_attach", "callID": call, "id": f"prt_{call}", "messageID": "msg_2", "state": state}


def task(child):
    return {"type": "tool", "tool": "task", "state": {"status": "completed", "metadata": {"sessionId": child}}}


def step(*parts, message_id="msg_2"):
    return {"info": {"id": message_id, "role": "assistant"}, "parts": list(parts)}


class Agent:
    def __init__(self, files=None, children=None):
        self.files = files or {}  # path -> bytes, or an exception to raise
        self.children = children or {}  # session id -> its messages
        self.read = []

    async def read_file(self, directory, path, limit):
        self.read.append((directory, path, limit))
        found = self.files.get(path, OpenCodeError(f"reading {path} -> 500", 500))
        if isinstance(found, Exception):
            raise found
        return found

    async def messages(self, session_id, directory):
        return self.children[session_id]


def test_only_finished_calls_mark_a_file():
    assert attached_path(attach("out/report.md")) == "out/report.md"
    assert attached_path(attach("/etc/hostname", status="error")) is None
    assert attached_path({"type": "tool", "tool": "write", "state": {"status": "completed", "metadata": {"attach": "x"}}}) is None
    assert attached_path({"type": "text", "text": "out/report.md"}) is None


def test_a_turn_notes_each_call_once_as_it_completes():
    turn = Turn("ses_1", 600)
    running = attach("a.png")
    running["state"] = {"status": "running", "input": {"path": "a.png"}}
    turn.apply("message.part.updated", {"part": running})
    assert turn.attached == []
    turn.apply("message.part.updated", {"part": attach("a.png")})
    turn.apply("message.part.updated", {"part": attach("a.png")})  # the same part sent again
    turn.apply("message.part.updated", {"part": attach("b.png", call="call_2")}, child=True)  # from a subagent
    assert turn.attached == ["a.png", "b.png"]


def test_files_are_found_in_the_requests_messages_and_its_subagents():
    messages = [
        {"info": {"id": "msg_0", "role": "user"}, "parts": [{"type": "text", "text": "an older request"}]},
        step(attach("old.txt"), message_id="msg_00"),
        {"info": {"id": "msg_1", "role": "user"}, "parts": [{"type": "text", "text": "make the charts"}]},
        step(attach("one.png"), task("ses_child")),
        step(attach("missing.png", status="error", call="call_3"), attach("two.png", call="call_4"), message_id="msg_3"),
    ]
    child = [step(attach("from-sub.csv", call="call_5"), message_id="msg_9")]
    agent = Agent(children={"ses_child": child})
    found = asyncio.run(attachments.marked_in(turn_steps(messages, "msg_1"), agent, DIR))
    assert found == ["one.png", "from-sub.csv", "two.png"]


def test_a_subagent_that_cannot_be_read_is_skipped():
    agent = Agent()  # knows no subagent sessions
    assert asyncio.run(attachments.marked_in([step(task("ses_gone"), attach("a.txt"))], agent, DIR)) == ["a.txt"]


def test_repeats_are_dropped_keeping_the_last_call():
    assert attachments.last_of_each(["a", "b", "a", "c", "b"]) == ["a", "c", "b"]


def posted_groups(channel):
    """The names of the files posted, one list per message."""
    groups = {}
    for message_id, name, _ in channel.files:
        groups.setdefault(message_id, []).append(name)
    return list(groups.values())


def test_files_are_grouped_by_count_and_size():
    channel = Channel(600)
    files = {f"f{n}": b"x" for n in range(23)}
    asyncio.run(attachments.post(channel, Agent(files), DIR, list(files)))
    assert [len(group) for group in posted_groups(channel)] == [10, 10, 3]

    channel = Channel(600)
    big = {"a": b"x" * 6, "b": b"x" * 6, "c": b"x" * 3}
    asyncio.run(attachments.post(channel, Agent(big), DIR, list(big), size=10))
    assert posted_groups(channel) == [["a"], ["b", "c"]]


def test_one_messages_worth_is_fetched_at_a_time():
    channel = Channel(600)

    class Watching(Agent):
        async def read_file(self, directory, path, limit):
            self.read.append(len(channel.files))  # files posted before this one was fetched
            return b"x"

    agent = Watching()
    asyncio.run(attachments.post(channel, agent, DIR, [f"f{n}" for n in range(12)]))
    assert agent.read == [0] * 11 + [10]  # the 11th is fetched, then the first ten go out


def test_posting_sends_the_files_and_names_the_ones_left_out():
    channel = Channel(600)
    files = {f"out/{n}.txt": f"file {n}".encode() for n in range(12)}
    files["big.zip"] = FileTooLarge(9_500_000)
    agent = Agent(files)
    paths = [*files, "gone.txt", "out/0.txt"]
    asyncio.run(attachments.post(channel, agent, DIR, paths))

    assert [(directory, limit) for directory, _, limit in agent.read][:1] == [(DIR, attachments.FILE_LIMIT)]
    posted = [(mid, name, data) for mid, name, data in channel.files]
    assert len({mid for mid, _, _ in posted}) == 2  # 12 files: 10, then 2
    assert sorted(name for _, name, _ in posted) == sorted(f"{n}.txt" for n in range(12))
    assert ("0.txt", b"file 0") in [(name, data) for _, name, data in posted]
    assert posted[-1][1] == "0.txt"  # attached again last, so posted last
    assert channel.texts()[-1] == (
        "Not attached: `big.zip` is 9.5 MB, over the 8.0 MB Discord takes; "
        "`gone.txt` could not be fetched from the agent."
    )


def test_a_group_discord_refuses_is_named_and_nothing_raises():
    class Refusing(Channel):
        async def send(self, content=None, *, files=None, **kwargs):
            if files:
                raise discord.HTTPException(types.SimpleNamespace(status=413, reason="too large"), "Request entity too large")
            return await super().send(content, **kwargs)

    channel = Refusing(600)
    asyncio.run(attachments.post(channel, Agent({"a.bin": b"123"}), DIR, ["a.bin"]))
    assert channel.files == [] and channel.texts()[0].startswith("Not attached: `a.bin`: Discord refused them")
