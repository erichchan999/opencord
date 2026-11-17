"""Whole requests through the bridge, with stand-ins for the agent and Discord.

The agent stand-in plays back an event stream recorded from OpenCode 1.19.5
when it is handed a prompt, the way the real one reports its work.
"""

import asyncio
import time
import types

import aiohttp
import discord
import pytest

import bridge
import events
from fakes import Channel
from opencode import OpenCodeError
from sample import FLASH, messages_from, recorded, root_session, snapshot

CHANNEL = 500  # an enabled channel
THREAD = 600  # a thread the bridge opened in it
STRANGER = 650  # a thread in it the bridge did not open
ELSEWHERE = 999  # a channel that is not enabled
BOT_ID = 1234
AUTHOR = types.SimpleNamespace(name="erich", display_name="Erich", id=42, bot=False)


class Agent:
    def __init__(self, session_id="ses_new"):
        self.session_id = session_id  # what create_session hands out
        self.stream = []  # events to report when a prompt arrives
        self.messages = []  # what the session's message list holds afterwards
        self.working = {}
        self.missing = set()  # sessions the agent has forgotten
        self.prompts = []
        self.created = []
        self.aborted = []
        self.rejected = []
        self.on_abort = None
        self.commands_sent = []
        self.command_answer = None  # what a /command call returns
        self.command_fails = None  # or the error it raises

    async def create_session(self, directory, title):
        self.created.append(title)
        return self.session_id

    async def prompt_async(self, session_id, directory, body):
        if session_id in self.missing:
            raise OpenCodeError(f"session {session_id} not found", 404)
        self.prompts.append((session_id, body))
        for event in self.stream:
            bridge.router.dispatch(event)

    async def command(self, session_id, directory, body, timeout):
        self.commands_sent.append((session_id, body))
        if self.command_fails:
            raise self.command_fails
        for event in self.stream:
            bridge.router.dispatch(event)
        return self.command_answer

    async def session(self, session_id, directory):
        if session_id in self.missing:
            raise OpenCodeError(f"session {session_id} not found", 404)
        return {"id": session_id}

    async def statuses(self, directory):
        return self.working

    async def recent_messages(self, session_id, directory, limit):
        return self.messages

    async def abort(self, session_id, directory):
        self.aborted.append(session_id)
        if self.on_abort:
            self.on_abort()

    async def reject_question(self, request_id, directory):
        self.rejected.append(request_id)


class Catalog:
    async def get(self):
        return snapshot()


class Client:
    def __init__(self, channel):
        self._channel = channel
        self.user = types.SimpleNamespace(id=BOT_ID)

    async def wait_until_ready(self):
        pass

    def get_channel(self, channel_id):
        return self._channel

    def is_ready(self):
        return True


@pytest.fixture
def world(monkeypatch):
    """A bridge with nothing in it, talking to a stand-in agent and one stand-in thread."""
    agent, thread = Agent(), Channel(THREAD)
    bridge.store.threads, bridge.store.defaults, bridge.store.questions = {}, {}, {}
    bridge.router.turns.clear()
    monkeypatch.setattr(bridge, "agent", agent)
    monkeypatch.setattr(bridge.questions, "_agent", agent)
    monkeypatch.setattr(bridge, "catalog", Catalog())
    monkeypatch.setattr(bridge, "client", Client(thread))
    monkeypatch.setattr(bridge, "shutting_down", False)
    monkeypatch.setattr(events, "POLL_SECONDS", 0.01)
    entry = bridge.store.register_thread(THREAD, 42)
    return types.SimpleNamespace(agent=agent, thread=thread, entry=entry)


def play(world, name):
    """Makes the agent answer the next prompt with a recorded stream."""
    stream = recorded(name)
    world.agent.session_id = root_session(stream)
    world.agent.stream = stream
    world.agent.messages = messages_from(stream, world.agent.session_id)
    return world.agent.session_id


def event(kind, session_id, **properties):
    return {"type": kind, "properties": {"sessionID": session_id, **properties}}


def test_a_request_from_prompt_to_reply(world):
    session = play(world, "tools")
    asyncio.run(bridge.run_turn(world.thread, world.entry, AUTHOR, "do the thing"))

    ((sent_to, body),) = world.agent.prompts
    assert sent_to == session == world.entry["sessionId"]
    assert body["parts"] == [{"type": "text", "text": "Discord user Erich says:\n\ndo the thing"}]
    assert body["model"] == {"providerID": "deepseek-proxy", "modelID": "deepseek-flash"}
    assert body["tools"] == {"question": True} and "variant" not in body and "agent" not in body

    status_id, first_status, _ = world.thread.sent[0]
    assert first_status.startswith("⏳ **Working** · started <t:") and "· DeepSeek Flash" in first_status
    final = world.thread.text_of(status_id)
    assert final.startswith("-# ✅ Done in ") and final.endswith(" · 3 tool calls · 8.1k tokens in context")
    assert world.thread.texts()[1:] == ["done."]
    assert "turn" not in world.entry and bridge.router.turns == {}


def test_the_threads_settings_go_out_with_the_prompt_and_show_in_the_status(world):
    play(world, "tools")
    world.entry.update(model=FLASH, effort="max", agent="plan")
    asyncio.run(bridge.run_turn(world.thread, world.entry, AUTHOR, "plan it"))
    body = world.agent.prompts[0][1]
    assert (body["variant"], body["agent"]) == ("max", "plan")
    assert "DeepSeek Flash · effort max · plan agent" in world.thread.texts()[0]


def test_a_stopped_request_says_so_once(world):
    play(world, "abort")
    asyncio.run(bridge.run_turn(world.thread, world.entry, AUTHOR, "sleep"))
    assert world.thread.text_of(world.thread.sent[0][0]).startswith("-# 🛑 Stopped after ")
    assert not any("error" in text.lower() or text.startswith("Done.") for text in world.thread.texts()[1:])


def test_an_error_from_the_agent_is_passed_on(world):
    session = world.agent.session_id
    world.agent.stream = [
        event("session.status", session, status={"type": "busy"}),
        event("session.error", session, error={"name": "UnknownError", "data": {"message": "Model not found: deepseek-proxy/nope."}}),
        event("session.status", session, status={"type": "idle"}),
        event("session.idle", session),
    ]
    world.agent.messages = [{"info": {"id": "msg_u", "role": "user"}, "parts": []}]
    asyncio.run(bridge.run_turn(world.thread, world.entry, AUTHOR, "hi"))
    assert world.thread.text_of(world.thread.sent[0][0]).startswith("-# ⚠️ Ended with an error after ")
    assert world.thread.texts()[1:] == ["The agent stopped with an error: Model not found: deepseek-proxy/nope."]


def test_an_error_before_the_agent_even_starts_is_passed_on(world):
    session = world.agent.session_id
    world.agent.stream = [event("session.error", session, error={"name": "UnknownError", "data": {"message": 'Agent not found: "nope"'}})]
    asyncio.run(asyncio.wait_for(bridge.run_turn(world.thread, world.entry, AUTHOR, "hi"), 2))
    assert world.thread.texts()[1:] == ['The agent stopped with an error: Agent not found: "nope"']


def test_a_reply_without_text_is_acknowledged(world):
    session = world.agent.session_id
    world.agent.stream = [event("session.status", session, status={"type": "busy"}), event("session.idle", session)]
    asyncio.run(bridge.run_turn(world.thread, world.entry, AUTHOR, "hi"))
    assert world.thread.texts()[1:] == ["Done. (The agent finished without a text reply.)"]


def test_a_session_the_agent_forgot_is_replaced(world):
    session = play(world, "tools")
    world.entry["sessionId"] = "ses_forgotten"
    world.agent.missing = {"ses_forgotten"}
    asyncio.run(bridge.run_turn(world.thread, world.entry, AUTHOR, "still there?"))
    assert world.entry["sessionId"] == session and [sid for sid, _ in world.agent.prompts] == [session]
    assert "started a fresh session" in world.thread.texts()[0]
    assert world.thread.texts()[-1] == "done." and bridge.router.turns == {}


def test_a_request_that_runs_too_long_is_stopped(world, monkeypatch):
    monkeypatch.setattr(bridge, "PROMPT_TIMEOUT", 0)
    session = world.agent.session_id
    world.agent.stream = [event("session.status", session, status={"type": "busy"})]
    world.agent.working = {session: {"type": "busy"}}
    world.agent.on_abort = lambda: bridge.router.dispatch(event("session.idle", session))
    asyncio.run(asyncio.wait_for(bridge.run_turn(world.thread, world.entry, AUTHOR, "loop forever"), 2))
    assert world.agent.aborted == [session]
    assert "it ran past the 0 minute limit" in world.thread.text_of(world.thread.sent[0][0])
    assert world.thread.texts()[-1].startswith("I stopped this request because it ran for more than 0 minutes.")
    assert "turn" not in world.entry


def test_an_end_the_stream_never_reported_is_still_noticed(world):
    stream = recorded("tools")
    session = world.agent.session_id = root_session(stream)
    world.agent.stream = stream[:20]  # the stream drops while the agent is still working
    world.agent.messages = messages_from(stream, session)
    world.agent.working = {}  # the agent itself says it is no longer working
    asyncio.run(asyncio.wait_for(bridge.run_turn(world.thread, world.entry, AUTHOR, "do the thing"), 2))
    assert world.thread.texts()[-1] == "done."


def test_the_bridge_notes_a_running_request_and_keeps_the_note_when_shut_down(world):
    async def run():
        session = world.agent.session_id
        world.agent.stream = [event("session.status", session, status={"type": "busy"})]
        world.agent.working = {session: {"type": "busy"}}
        task = asyncio.create_task(bridge.run_turn(world.thread, world.entry, AUTHOR, "long job"))
        await asyncio.sleep(0.05)
        note = world.entry["turn"]
        assert note["statusMessageId"] == world.thread.sent[0][0] and note["label"] == "DeepSeek Flash"
        assert abs(note["startedAt"] - time.time()) < 5
        task.cancel()  # what a shutdown does to it
        with pytest.raises(asyncio.CancelledError):
            await task
        assert world.entry["turn"] == note and bridge.router.turns == {}

    asyncio.run(run())


def note_from_before(world, stream_name, seconds_ago=125):
    session = play(world, stream_name)
    world.agent.stream = []
    world.entry.update(sessionId=session, turn={"statusMessageId": 4242, "startedAt": time.time() - seconds_ago, "label": "DeepSeek Flash"})
    return session


def test_after_a_restart_a_request_that_finished_meanwhile_gets_its_reply(world):
    note_from_before(world, "tools")
    world.agent.working = {}
    asyncio.run(asyncio.wait_for(bridge.resume_turn(str(THREAD), world.entry), 2))
    assert world.thread.text_of(4242).startswith("-# ✅ Done in 2m 0")  # the same status message, and the time since it began
    assert world.thread.texts() == ["done."]
    assert world.agent.prompts == [] and "turn" not in world.entry


def test_after_a_restart_a_request_still_running_is_followed_to_its_end(world):
    async def run():
        session = note_from_before(world, "tools")
        world.agent.working = {session: {"type": "busy"}}
        task = asyncio.create_task(bridge.resume_turn(str(THREAD), world.entry))
        await asyncio.sleep(0.05)
        assert not task.done() and world.thread.text_of(4242).startswith("⏳ **Working**")
        bridge.router.dispatch(event("session.idle", session))
        await asyncio.wait_for(task, 2)
        assert world.thread.texts() == ["done."] and "turn" not in world.entry

    asyncio.run(run())


def test_after_a_restart_time_spent_waiting_on_people_still_does_not_count(world):
    async def run():
        session = note_from_before(world, "tools", seconds_ago=3000)
        world.agent.working = {session: {"type": "busy"}}
        # It had worked a minute, then waited on a question until the bridge stopped.
        world.entry["turn"].update(worked=60, paused=True, notedAt=time.time() - 2000)
        bridge.store.questions["que_1"] = {"thread": str(THREAD), "questions": [{"question": "Which?"}], "answers": [None], "messages": [77], "askedAt": time.time() - 2900}
        task = asyncio.create_task(bridge.resume_turn(str(THREAD), world.entry))
        await asyncio.sleep(0.05)
        turn = bridge.router.turn_for_thread(THREAD)
        assert turn.waiting and 59 < turn.running_for() < 62
        await bridge.questions.closed("que_1")
        assert not turn.waiting and world.entry["turn"]["paused"] is False
        bridge.router.dispatch(event("session.idle", session))
        await asyncio.wait_for(task, 2)
        assert world.thread.text_of(4242).startswith("-# ✅ Done in 1m 0")

    asyncio.run(run())


def test_after_a_restart_a_question_shown_while_checking_is_waited_on_once(world):
    async def run():
        session = note_from_before(world, "tools")

        async def statuses(directory):
            await bridge.questions.ask(session, {"id": "que_1", "sessionID": session, "questions": [{"question": "Which?"}]})
            return {session: {"type": "busy"}}

        world.agent.statuses = statuses
        task = asyncio.create_task(bridge.resume_turn(str(THREAD), world.entry))
        await asyncio.sleep(0.05)
        turn = bridge.router.turn_for_thread(THREAD)
        await bridge.questions.closed("que_1")
        assert not turn.waiting
        bridge.router.dispatch(event("session.idle", session))
        await asyncio.wait_for(task, 2)

    asyncio.run(run())


def test_a_note_without_a_session_is_just_dropped(world):
    world.entry["turn"] = {"statusMessageId": 4242, "startedAt": time.time(), "label": ""}
    asyncio.run(bridge.resume_turn(str(THREAD), world.entry))
    assert "turn" not in world.entry and world.thread.sent == [] and world.thread.edits == []


def test_stopping_a_thread_marks_the_request_and_dismisses_its_questions(world):
    async def run():
        world.entry["sessionId"] = "ses_1"
        turn = bridge.router.start("ses_1", THREAD)
        bridge.store.questions["que_1"] = {"thread": str(THREAD), "questions": [{"question": "Which?", "options": []}], "answers": [None], "messages": [77], "askedAt": time.time()}
        turn.pause()
        await bridge.stop_thread(THREAD, world.entry, bridge.queue.stop_point())
        assert turn.stopped and not turn.waiting
        assert world.agent.rejected == ["que_1"] and world.agent.aborted == ["ses_1"]
        assert world.thread.text_of(77).endswith("-# Stopped before it was answered.")
        bridge.router.finish(turn)

    asyncio.run(run())


def test_a_stop_while_a_request_gets_ready_keeps_its_prompt_from_going_out(world, monkeypatch):
    monkeypatch.setattr(bridge, "queue", bridge.ThreadQueue(4))

    async def run():
        ready = asyncio.Event()

        async def job():
            await ready.wait()  # downloading its attachments, say
            await bridge.run_turn(world.thread, world.entry, AUTHOR, "never mind")

        bridge.queue.submit(THREAD, job)
        await asyncio.sleep(0)
        await bridge.stop_thread(THREAD, world.entry, bridge.queue.stop_point())
        ready.set()
        while bridge.queue.load(THREAD):
            await asyncio.sleep(0.01)

    asyncio.run(asyncio.wait_for(run(), 2))
    assert world.agent.prompts == [] and world.thread.sent == [] and bridge.router.turns == {}


def test_a_stop_drops_waiting_jobs_but_not_ones_that_must_run():
    async def run():
        queue, ran, gate = bridge.ThreadQueue(4), [], asyncio.Event()

        async def blocker():
            await gate.wait()

        def job(name):
            async def run_it():
                ran.append(name)

            return run_it

        queue.submit(THREAD, blocker)
        queue.submit(THREAD, job("message"))
        queue.submit(THREAD, job("delete old session"), droppable=False)
        queue.drop_waiting(THREAD, queue.stop_point())
        gate.set()
        while queue.load(THREAD):
            await asyncio.sleep(0.01)
        assert ran == ["delete old session"]

    asyncio.run(asyncio.wait_for(run(), 2))


def test_a_stop_that_beats_the_prompt_to_the_agent_is_sent_again(world):
    session = world.agent.session_id
    world.agent.stream = [event("session.status", session, status={"type": "busy"})]
    world.agent.on_abort = lambda: bridge.router.dispatch(event("session.idle", session))
    prompt = world.agent.prompt_async

    async def stopped_on_the_way(session_id, directory, body):
        bridge.router.turn_for_thread(THREAD).stopped = True  # what stop_thread does meanwhile
        await prompt(session_id, directory, body)

    world.agent.prompt_async = stopped_on_the_way
    asyncio.run(asyncio.wait_for(bridge.run_turn(world.thread, world.entry, AUTHOR, "stop me"), 2))
    assert world.agent.aborted == [session]
    assert world.thread.text_of(world.thread.sent[0][0]).startswith("-# 🛑 Stopped after ")


def test_a_question_stops_the_requests_clock_until_it_is_answered(world):
    async def run():
        world.entry["sessionId"] = "ses_1"
        turn = bridge.router.start("ses_1", THREAD)
        await bridge.questions.ask("ses_1", {"id": "que_1", "sessionID": "ses_1", "questions": [{"question": "Which?", "options": [{"label": "A"}]}]})
        assert turn.waiting and bridge.questions.open_for(THREAD) == ["que_1"]
        await bridge.questions.closed("que_1")
        assert not turn.waiting
        bridge.router.finish(turn)

    asyncio.run(run())


def test_a_subagents_session_leads_to_its_parents_thread(world):
    async def run():
        world.entry["sessionId"] = "ses_parent"
        bridge.router.dispatch({"type": "session.created", "properties": {"sessionID": "ses_child", "info": {"id": "ses_child", "parentID": "ses_parent"}}})
        assert await bridge.thread_for_session("ses_child") == THREAD
        bridge.router._parents.clear()

        async def session(session_id, directory):  # the stream never mentioned this one: the agent is asked
            return {"id": session_id, "parentID": "ses_parent" if session_id == "ses_late_child" else None}

        world.agent.session = session
        assert await bridge.thread_for_session("ses_late_child") == THREAD
        assert await bridge.thread_for_session("ses_stranger") is None

    asyncio.run(run())


# ---------------------------------------------------------------- where the bot listens


class Message:
    """A message someone posts in `channel`, mentioning the bot unless told otherwise."""

    def __init__(self, channel, text, mention=True):
        self.author = AUTHOR
        self.type = discord.MessageType.default
        self.channel = channel
        self.guild = None
        self.content = f"<@{BOT_ID}> {text}" if mention else text
        self.mentions = [types.SimpleNamespace(id=BOT_ID)] if mention else []
        self.role_mentions = []
        self.attachments = []
        self.replies = []
        self.threads = []

    async def reply(self, text, *, allowed_mentions=None):
        self.replies.append(text)

    async def create_thread(self, *, name, auto_archive_duration):
        thread = Channel(700, name)
        self.threads.append(thread)
        return thread


class Jobs:
    def __init__(self):
        self.submitted = []  # (thread id, job)

    def submit(self, thread_id, job):
        self.submitted.append((thread_id, job))

    def stop_point(self):
        return 0


@pytest.fixture
def server(world, monkeypatch):
    """`world`, plus which channels are enabled (CHANNEL, and with it its threads)."""
    world.home = Channel(CHANNEL, "agent")
    world.enabled = True

    async def home_of(channel):
        return world.home if world.enabled and channel.id in (CHANNEL, THREAD, STRANGER) else None

    monkeypatch.setattr(bridge, "home_of", home_of)
    monkeypatch.setattr(bridge, "queue", Jobs())
    return world


def test_a_mention_in_an_enabled_channel_opens_a_thread(server):
    message = Message(Channel(CHANNEL), "fix the login bug")
    asyncio.run(bridge.on_message(message))
    assert [thread.name for thread in message.threads] == ["fix the login bug"]
    assert "700" in bridge.store.threads and [tid for tid, _ in bridge.queue.submitted] == [700]


def test_a_mention_anywhere_else_is_ignored(server):
    for message in (Message(Channel(ELSEWHERE), "hello"), Message(Channel(CHANNEL), "no mention", mention=False)):
        asyncio.run(bridge.on_message(message))
        assert message.threads == [] and message.replies == []
    assert bridge.queue.submitted == [] and list(bridge.store.threads) == [str(THREAD)]


def test_a_mention_in_someone_elses_thread_points_back_to_the_channel(server):
    message = Message(Channel(STRANGER), "help")
    asyncio.run(bridge.on_message(message))
    assert message.replies == [f"I can't start a thread inside a thread. Mention me in <#{CHANNEL}> instead."]
    assert message.threads == [] and bridge.queue.submitted == []


def test_a_thread_goes_quiet_once_its_channel_is_disabled(server):
    asyncio.run(bridge.on_message(Message(server.thread, "queued before", mention=False)))
    server.enabled = False
    asyncio.run(bridge.on_message(Message(server.thread, "still there?", mention=False)))
    # Both queued, so the thread's messages keep their order, and checked when they run.
    assert len(bridge.queue.submitted) == 2
    for _, job in bridge.queue.submitted:
        asyncio.run(job())
    assert server.agent.prompts == [] and server.thread.sent == []


# ---------------------------------------------------------------- /command


OLD_EXCHANGE = [
    {"info": {"id": "msg_old", "role": "user"}, "parts": [{"type": "text", "text": "earlier"}]},
    {"info": {"id": "msg_old_reply", "role": "assistant"}, "parts": [{"type": "text", "text": "old reply"}]},
]


def test_a_command_is_followed_like_a_prompt_and_answers_with_its_own_reply(world):
    session = world.agent.session_id
    world.entry.update(effort="max", agent="plan")
    world.agent.stream = [
        event("session.status", session, status={"type": "busy"}),
        event("message.updated", session, info={"id": "msg_u", "role": "user"}),
    ]
    world.agent.messages = OLD_EXCHANGE  # not where the reply comes from
    world.agent.command_answer = {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Looks good."}]}
    asyncio.run(asyncio.wait_for(bridge.run_command(world.thread, world.entry, "review", "HEAD~1"), 2))

    ((sent_to, body),) = world.agent.commands_sent
    assert sent_to == session == world.entry["sessionId"]
    assert body == {"command": "review", "arguments": "HEAD~1", "model": FLASH, "variant": "max", "agent": "plan"}
    status_id, first_status, _ = world.thread.sent[0]
    assert "· /review · DeepSeek Flash · effort max · plan agent" in first_status
    assert world.thread.text_of(status_id).startswith("-# ✅ Done in ")
    assert world.thread.texts()[1:] == ["Looks good."]
    assert "turn" not in world.entry and bridge.router.turns == {}


def test_a_command_in_a_thread_whose_session_was_lost_starts_a_fresh_one(world):
    world.entry["sessionId"] = "ses_forgotten"
    world.agent.missing = {"ses_forgotten"}
    world.agent.command_answer = {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Reviewed."}]}
    asyncio.run(asyncio.wait_for(bridge.run_command(world.thread, world.entry, "review", ""), 2))
    assert world.entry["sessionId"] == "ses_new" and [sid for sid, _ in world.agent.commands_sent] == ["ses_new"]
    assert "started a fresh session" in world.thread.texts()[0] and world.thread.texts()[-1] == "Reviewed."


def test_a_command_the_agent_refuses_reports_the_error_and_not_an_older_reply(world):
    world.agent.command_fails = OpenCodeError("POST /session/ses_new/command -> 500: boom", 500)
    world.agent.messages = OLD_EXCHANGE
    asyncio.run(asyncio.wait_for(bridge.run_command(world.thread, world.entry, "review", ""), 2))
    assert world.thread.text_of(world.thread.sent[0][0]).startswith("-# ⚠️ Ended with an error after ")
    assert world.thread.texts()[1:] == ["The agent stopped with an error: POST /session/ses_new/command -> 500: boom"]


def test_a_command_whose_call_times_out_after_the_work_began_still_reports_the_reply(world):
    session = world.agent.session_id
    world.agent.stream = [
        event("session.status", session, status={"type": "busy"}),
        event("message.updated", session, info={"id": "msg_u", "role": "user"}),
    ]
    world.agent.working = {session: {"type": "busy"}}
    world.agent.messages = [
        {"info": {"id": "msg_u", "role": "user"}, "parts": [{"type": "text", "text": "/review"}]},
        {"info": {"id": "msg_a", "role": "assistant"}, "parts": [{"type": "text", "text": "Reviewed after all."}]},
    ]

    async def command(session_id, directory, body, timeout):
        for item in world.agent.stream:
            bridge.router.dispatch(item)
        # A question kept the work going past the call's own deadline.
        raise TimeoutError(f"POST /session/{session_id}/command timed out after 2700s")

    world.agent.command = command

    async def run():
        task = asyncio.create_task(bridge.run_command(world.thread, world.entry, "review", ""))
        await asyncio.sleep(0.05)
        world.agent.working = {}
        bridge.router.dispatch(event("session.idle", session))
        await asyncio.wait_for(task, 2)

    asyncio.run(run())
    assert world.thread.text_of(world.thread.sent[0][0]).startswith("-# ✅ Done in ")
    assert world.thread.texts()[1:] == ["Reviewed after all."]


# ---------------------------------------------------------------- attachments


class Attachment:
    def __init__(self, filename, data, content_type=None, size=None, fails=None):
        self.filename = filename
        self.content_type = content_type
        self.size = len(data) if size is None else size
        self.url = f"https://cdn.example/{filename}"
        self._data = data
        self._fails = fails
        self.reads = 0

    async def read(self):
        self.reads += 1
        if self._fails:
            raise self._fails
        return self._data


def test_text_attachments_are_sent_as_their_content_and_the_rest_as_links():
    files = [
        Attachment("notes.txt", b"hello", "text/plain; charset=utf-8"),
        Attachment("calc.py", "π = 3.14\n".encode()),  # Discord leaves out types it does not know
        Attachment("data.json", b'{"a": 1}', "application/json"),
        Attachment("shot.png", b"\x89PNG", "image/png"),
        Attachment("big.log", b"x", "text/plain", size=bridge.TEXT_ATTACHMENT_LIMIT + 1),
        Attachment("binary.txt", b"\xff\xfe\x01", "text/plain"),
        Attachment("nul.txt", b"a\x00b", "text/plain"),
        Attachment("flaky.txt", b"", "text/plain", fails=aiohttp.ClientConnectionError("connection reset")),
    ]
    parts, links = asyncio.run(bridge.attachment_parts(types.SimpleNamespace(attachments=files)))
    assert [part["filename"] for part in parts] == ["notes.txt", "calc.py", "data.json"]
    assert parts[0] == {"type": "file", "mime": "text/plain", "filename": "notes.txt", "url": "data:text/plain;base64,aGVsbG8="}
    assert all(part["mime"] == "text/plain" for part in parts)  # the agent refuses application/json
    names = ("shot.png", "big.log", "binary.txt", "nul.txt", "flaky.txt")
    assert links == [f"{name}: https://cdn.example/{name}" for name in names]
    assert files[3].reads == files[4].reads == 0  # not downloaded at all

    assert bridge.with_links("look", []) == "look"
    assert bridge.with_links("look", links[:1]) == "look\n\n(Attached files, fetch them if needed:\nshot.png: https://cdn.example/shot.png)"
    assert bridge.with_links("", links[:1]).startswith("(Attached files")


def test_text_files_known_by_name_are_read_too():
    files = [Attachment("Dockerfile", b"FROM python:3.13\n"), Attachment(".gitignore", b"*.pyc\n"), Attachment("NOTES", b"x")]
    parts, links = asyncio.run(bridge.attachment_parts(types.SimpleNamespace(attachments=files)))
    assert [part["filename"] for part in parts] == ["Dockerfile", ".gitignore"]
    assert links == ["NOTES: https://cdn.example/NOTES"]  # no type, no suffix and no known name


def test_a_messages_text_files_share_one_size_budget():
    size = 90_000
    files = [Attachment(f"part{n}.log", b"x" * size, "text/plain") for n in range(3)] + [Attachment("small.txt", b"ok", "text/plain")]
    parts, links = asyncio.run(bridge.attachment_parts(types.SimpleNamespace(attachments=files)))
    # 2 x 90 KB fit in 200 KB, the third does not, the small one still does.
    assert [part["filename"] for part in parts] == ["part0.log", "part1.log", "small.txt"]
    assert links == ["part2.log: https://cdn.example/part2.log"] and files[2].reads == 0


def test_files_go_out_with_the_prompt(world):
    session = world.agent.session_id
    world.agent.stream = [event("session.status", session, status={"type": "busy"}), event("session.idle", session)]
    part = {"type": "file", "mime": "text/plain", "filename": "a.txt", "url": "data:text/plain;base64,eA=="}
    asyncio.run(bridge.run_turn(world.thread, world.entry, AUTHOR, "", [part]))
    body = world.agent.prompts[0][1]
    assert body["parts"] == [
        {
            "type": "text",
            "text": "Discord user Erich says:\n\n(No message, only the attached files.)\n\n"
            "(Attached to this message, with their content included here: a.txt. They are not in the checkout.)",
        },
        part,
    ]


# ---------------------------------------------------------------- settings and replies


def test_a_blank_number_setting_means_its_default(monkeypatch):
    monkeypatch.setenv("PROMPT_TIMEOUT_SECONDS", " ")
    assert bridge.number("PROMPT_TIMEOUT_SECONDS", 1800) == 1800
    monkeypatch.setenv("MAX_CONCURRENT", "4")
    assert bridge.number("MAX_CONCURRENT", 16, int) == 4
    monkeypatch.setenv("MAX_CONCURRENT", "lots")
    with pytest.raises(SystemExit):
        bridge.number("MAX_CONCURRENT", 16, int)


def test_a_long_reply_is_split_with_its_code_blocks_intact():
    limit = bridge.DISCORD_LIMIT
    text = "Here it is:\n```json\n" + "x" * (2 * limit + 100) + "\n```\nThat's all."
    chunks = bridge.split_for_discord(text)
    assert all(len(chunk) <= limit + 20 for chunk in chunks)
    assert all(chunk.count("```") % 2 == 0 for chunk in chunks)  # every code block is closed
    assert not any(chunk.replace("```json", "").replace("```", "").strip() == "" for chunk in chunks)
    assert "".join(chunks).count("x") == 2 * limit + 100
    assert chunks[0].startswith("Here it is:\n```json\nxxx") and chunks[-1].endswith("```\nThat's all.")
    assert all(chunk.startswith("```json\n") for chunk in chunks[1:])  # reopened with its language

    # A code block that would open on a message's last line moves to the next one whole.
    text = "a" * (limit - 100) + "\n```py\n" + "b" * 500 + "\n```"
    assert bridge.split_for_discord(text) == ["a" * (limit - 100), "```py\n" + "b" * 500 + "\n```"]
    assert bridge.split_for_discord("short\n\n```\ncode\n```") == ["short\n\n```\ncode\n```"]
