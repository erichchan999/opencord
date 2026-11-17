"""Event handling, checked against streams recorded from OpenCode 1.19.5."""

import asyncio

import events
from events import Router, Turn, is_request, request_text, tool_detail, turn_reply, wait_for_turn
from opencode import EventParser
from sample import messages_from, recorded, root_session

THREAD = 600


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def replay(name, stop_before=None):
    """Feeds a recording through a Router. Returns (router, turn, the events fed)."""
    stream = recorded(name)
    router = Router()
    turn = router.start(root_session(stream), THREAD)
    fed = []
    for event in stream:
        if stop_before is not None and stop_before(event):
            break
        router.dispatch(event)
        fed.append(event)
    return router, turn, fed


# ------------------------------------------------------------ recordings


def test_a_request_with_tools_and_todos():
    _, turn, _ = replay("tools")
    assert turn.done.is_set() and turn.active and turn.error is None
    assert [tool["tool"] for tool in turn.tools.values()] == ["todowrite", "bash", "todowrite"]
    assert all(tool["status"] == "completed" for tool in turn.tools.values())
    assert [tool["detail"] for tool in turn.tools.values()][1] == "echo hello-from-spike"
    assert [(todo["content"], todo["status"]) for todo in turn.todos] == [("run echo", "completed"), ("report", "completed")]
    assert turn.text == "done."
    assert turn.tokens == 8132
    assert turn.user_message_id.startswith("msg_")
    assert turn.current_tool() is None


def test_the_users_own_text_is_not_taken_for_the_agents():
    # The prompt arrives on the stream as a text part too. It must not show up as what the agent is writing.
    _, turn, _ = replay("tools", stop_before=lambda e: e["type"] == "session.status")
    assert turn.text == "" and not turn.active


def test_a_running_tool_is_the_current_activity():
    def bash_finished(event):
        part = event["properties"].get("part") or {}
        return part.get("tool") == "bash" and (part.get("state") or {}).get("status") == "completed"

    _, turn, _ = replay("busy", stop_before=bash_finished)
    assert turn.current_tool() == {"tool": "bash", "status": "running", "detail": "sleep 8"}
    assert not turn.done.is_set()


def test_an_aborted_request_ends_with_its_error():
    _, turn, _ = replay("abort")
    assert turn.done.is_set()
    assert turn.error == ("MessageAbortedError", "Aborted")


def test_a_subagent_counts_as_activity_but_does_not_end_the_request():
    stream = recorded("subagent")
    root = root_session(stream)
    child_idle = next(i for i, e in enumerate(stream) if e["type"] == "session.idle" and e["properties"]["sessionID"] != root)
    router = Router()
    turn = router.start(root, THREAD)
    for event in stream[: child_idle + 1]:
        router.dispatch(event)
    assert not turn.done.is_set()  # only the subagent went idle
    assert {tool["tool"] for tool in turn.tools.values()} == {"task", "bash"}
    assert turn.current_tool()["tool"] == "task" and turn.current_tool()["detail"] == "Run echo command"
    for event in stream[child_idle + 1 :]:
        router.dispatch(event)
    assert turn.done.is_set()
    router.finish(turn)
    assert router.turns == {} and router._parents == {}


def test_question_events_reach_the_handlers():
    async def run():
        asked, closed = [], []
        stream = recorded("question")
        router = Router()

        async def on_question(session_id, request):
            asked.append((session_id, request["id"], len(request["questions"])))

        async def on_closed(request_id):
            closed.append(request_id)

        router.on_question, router.on_question_closed = on_question, on_closed
        root = root_session(stream)
        router.start(root, THREAD)
        for event in stream:
            router.dispatch(event)
        while not router._jobs.empty():
            await router._jobs.get_nowait()
        assert asked == [(root, asked[0][1], 1)] and asked[0][1].startswith("que_")
        assert closed == [asked[0][1]]

    asyncio.run(run())


def test_events_for_other_sessions_are_ignored():
    stream = recorded("tools")
    router = Router()
    turn = router.start("ses_someone_else", THREAD)
    for event in stream:
        router.dispatch(event)
    assert not turn.done.is_set() and turn.tools == {} and not turn.active


# ------------------------------------------------------------ a turn's own rules


def status(kind, **more):
    return {"sessionID": "ses_1", "status": {"type": kind, **more}}


def test_an_idle_left_over_from_the_request_before_does_not_end_this_one():
    turn = Turn("ses_1", THREAD)
    turn.apply("session.status", status("idle"))
    turn.apply("session.idle", {"sessionID": "ses_1"})
    assert not turn.done.is_set()
    turn.apply("session.status", status("busy"))
    turn.apply("session.idle", {"sessionID": "ses_1"})
    assert turn.done.is_set()


def test_an_error_before_the_work_starts_wakes_the_waiter_and_is_dropped_if_work_follows():
    turn = Turn("ses_1", THREAD)
    turn.apply("session.error", {"error": {"name": "UnknownError", "data": {"message": "Agent not found"}}})
    assert turn.error == ("UnknownError", "Agent not found") and turn.wake.is_set()
    turn.apply("session.error", {"error": {"name": "UnknownError", "data": {"message": "a stack trace"}}})
    assert turn.error[1] == "Agent not found"  # the first one is the readable one
    turn.apply("session.status", status("busy"))
    assert turn.error is None  # it belonged to an earlier request


def test_a_provider_retry_is_shown_until_work_resumes():
    turn = Turn("ses_1", THREAD)
    turn.apply("session.status", status("retry", attempt=2, message="overloaded", next=5))
    assert turn.retry == {"attempt": 2, "message": "overloaded"} and turn.active
    turn.apply("session.status", status("busy"))
    assert turn.retry is None


def test_the_clock_stops_while_a_person_is_being_waited_on():
    clock = Clock()
    turn = Turn("ses_1", THREAD, clock=clock)
    clock.now += 10
    turn.pause()
    turn.pause()  # a second question
    clock.now += 500
    assert turn.waiting and turn.running_for() == 10
    turn.resume()
    assert turn.waiting
    turn.resume()
    clock.now += 5
    assert not turn.waiting and turn.running_for() == 15 and turn.quiet_for() == 5


def test_a_resumed_turn_counts_the_time_before_the_restart():
    clock = Clock()
    turn = Turn("ses_1", THREAD, active=True, elapsed=120, clock=clock)
    assert turn.running_for() == 120 and turn.quiet_for() == 0


def test_tool_detail_picks_the_telling_input():
    assert tool_detail("bash", {"input": {"command": "npm test\n  --watch=false"}}) == "npm test --watch=false"
    assert tool_detail("read", {"input": {"filePath": "/work/sandbox/a.py"}}) == "/work/sandbox/a.py"
    assert tool_detail("task", {"input": {"description": "Run echo", "prompt": "long"}}) == "Run echo"
    assert tool_detail("bash", {"input": {}}) == ""
    assert tool_detail("mystery", {"input": {"x": 1}, "title": "Did a thing"}) == "Did a thing"


# ------------------------------------------------------------ waiting for the end


def wait(turn, working=True, age=0.0, **limits):
    async def is_working():
        calls.append(1)
        if isinstance(working, Exception):
            raise working
        return working

    calls = []
    options = {"time_limit": 1e9, "silence_limit": 1e9, "question_limit": 1e9, **limits}
    return asyncio.wait_for(wait_for_turn(turn, is_working, question_age=lambda: age, **options), 2), calls


def test_wait_returns_when_the_stream_says_done(monkeypatch):
    monkeypatch.setattr(events, "POLL_SECONDS", 0.01)

    async def run():
        turn = Turn("ses_1", THREAD, active=True)
        waiting, _ = wait(turn)
        asyncio.get_running_loop().call_later(0.05, turn.done.set)
        assert await waiting == "done"

    asyncio.run(run())


def test_wait_notices_an_end_the_stream_missed(monkeypatch):
    monkeypatch.setattr(events, "POLL_SECONDS", 0.01)

    async def run():
        waiting, calls = wait(Turn("ses_1", THREAD, active=True), working=False)
        assert await waiting == "done" and calls

    asyncio.run(run())


def test_wait_gives_a_fresh_prompt_time_to_start(monkeypatch):
    monkeypatch.setattr(events, "POLL_SECONDS", 0.01)

    async def run():
        clock = Clock()
        turn = Turn("ses_1", THREAD, clock=clock)  # not active yet: the agent has not picked the prompt up
        turn.wake.set()  # the stream reconnected just now
        waiting, calls = wait(turn, working=False)
        task = asyncio.ensure_future(waiting)
        await asyncio.sleep(0.05)
        assert not task.done()  # "not working" is no proof that it ended
        clock.now += events.START_GRACE_SECONDS
        assert await task == "done"

    asyncio.run(run())


def test_wait_ends_at_once_on_an_error_before_the_work_started(monkeypatch):
    monkeypatch.setattr(events, "POLL_SECONDS", 30)

    async def run():
        turn = Turn("ses_1", THREAD)
        waiting, _ = wait(turn, working=False)
        task = asyncio.ensure_future(waiting)
        await asyncio.sleep(0.01)
        turn.apply("session.error", {"error": {"name": "UnknownError", "data": {"message": "Model not found"}}})
        assert await task == "done"

    asyncio.run(run())


def test_wait_stops_a_request_that_runs_too_long_or_goes_silent(monkeypatch):
    monkeypatch.setattr(events, "POLL_SECONDS", 0.01)

    async def run():
        clock = Clock()
        turn = Turn("ses_1", THREAD, active=True, clock=clock)
        clock.now += 100
        turn.last_event = clock.now
        waiting, _ = wait(turn, time_limit=60)
        assert await waiting == "timeout"

        quiet = Turn("ses_1", THREAD, active=True, clock=clock)
        clock.now += 100
        waiting, _ = wait(quiet, silence_limit=60)
        assert await waiting == "silent"

    asyncio.run(run())


def test_wait_does_not_time_out_while_a_question_is_open(monkeypatch):
    monkeypatch.setattr(events, "POLL_SECONDS", 0.01)

    async def run():
        clock = Clock()
        turn = Turn("ses_1", THREAD, active=True, clock=clock)
        turn.pause()
        clock.now += 5000
        waiting, _ = wait(turn, time_limit=60, silence_limit=60, question_limit=9000, age=5000)
        task = asyncio.ensure_future(waiting)
        await asyncio.sleep(0.05)
        assert not task.done()
        turn.done.set()
        assert await task == "done"

        late = Turn("ses_1", THREAD, active=True, clock=clock)
        late.pause()
        waiting, _ = wait(late, question_limit=3600, age=3601)
        assert await waiting == "unanswered"

    asyncio.run(run())


def test_wait_keeps_going_when_the_agent_cannot_be_asked(monkeypatch):
    monkeypatch.setattr(events, "POLL_SECONDS", 0.01)

    async def run():
        turn = Turn("ses_1", THREAD, active=True)
        waiting, calls = wait(turn, working=OSError("agent is restarting"))
        task = asyncio.ensure_future(waiting)
        await asyncio.sleep(0.05)
        assert not task.done() and calls
        turn.done.set()
        assert await task == "done"

    asyncio.run(run())


# ------------------------------------------------------------ the reply


def test_reply_is_the_last_step_of_this_request():
    stream = recorded("tools")
    session = root_session(stream)
    _, turn, _ = replay("tools")
    messages = messages_from(stream, session)
    assert len(messages) == 5  # the prompt and four steps
    assert turn_reply(messages, turn.user_message_id) == ("done.", None)
    assert turn_reply(messages) == ("done.", None)  # without the prompt's id, after a restart


def test_reply_carries_the_error_the_last_step_ended_with():
    aborted = {"name": "MessageAbortedError", "data": {"message": "Aborted"}}
    messages = [
        {"info": {"id": "u1", "role": "user"}, "parts": []},
        {"info": {"id": "a1", "role": "assistant", "parentID": "u1"}, "parts": [{"type": "text", "text": "Starting"}]},
        {"info": {"id": "a2", "role": "assistant", "parentID": "u1", "error": aborted}, "parts": [{"type": "text", "text": "Half an ans"}]},
    ]
    assert turn_reply(messages, "u1") == ("Half an ans", aborted)


def test_reply_ignores_earlier_requests_and_text_that_is_not_the_agents():
    def message(role, message_id, parent=None, text=None, **part):
        parts = [{"type": "text", "text": text, **part}] if text else []
        return {"info": {"id": message_id, "role": role, "parentID": parent}, "parts": parts}

    messages = [
        message("user", "u1", text="first"),
        message("assistant", "a1", "u1", text="first answer"),
        message("user", "u2", text="second"),
        message("assistant", "a2", "u2", text="looking"),
        message("assistant", "a3", "u2", text="hidden", synthetic=True),
    ]
    assert turn_reply(messages, "u1") == ("first answer", None)
    assert turn_reply(messages, "u2") == ("", None)  # the last step wrote nothing a person should see
    assert turn_reply(messages[:4]) == ("looking", None)
    assert turn_reply([]) == ("", None)
    assert turn_reply(messages[:1], "u1") == ("", None)


def test_the_previous_requests_message_sent_again_is_not_this_turns():
    # After a request ends, OpenCode sends its user message again with a
    # summary. That can arrive after the next request's turn has started.
    turn = Turn("ses_1", 600)
    turn.apply("message.updated", {"info": {"id": "msg_old", "role": "user", "summary": {"diffs": []}}})
    assert turn.user_message_id is None
    turn.apply("message.updated", {"info": {"id": "msg_new", "role": "user"}})
    turn.apply("message.updated", {"info": {"id": "msg_new", "role": "user", "summary": {"diffs": []}}})
    turn.apply("message.updated", {"info": {"id": "msg_note", "role": "user"}})  # a note later in the turn
    assert turn.user_message_id == "msg_new"


def test_the_agents_notes_to_itself_are_not_new_requests():
    # What a command run as a subtask leaves: the command, the task tool call,
    # a note the agent adds after the subtask, and the answer.
    messages = [
        {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "subtask", "command": "review", "description": "review changes"}]},
        {"info": {"id": "a1", "role": "assistant"}, "parts": [{"type": "tool", "tool": "task", "state": {"status": "completed"}}]},
        {"info": {"id": "u2", "role": "user"}, "parts": [{"type": "text", "text": "Summarize the task tool output above.", "synthetic": True}]},
        {"info": {"id": "a2", "role": "assistant"}, "parts": [{"type": "text", "text": "No problems found."}]},
    ]
    assert [is_request(m) for m in messages] == [True, False, False, False]
    assert turn_reply(messages, "u1") == ("No problems found.", None)
    assert turn_reply(messages) == ("No problems found.", None)
    assert request_text(messages[0]) == "/review"
    attached = {"info": {"role": "user"}, "parts": [{"type": "file", "filename": "a.txt"}, {"type": "text", "text": "read it", "synthetic": True}]}
    assert is_request(attached)
    prompt = {"info": {"role": "user"}, "parts": [{"type": "text", "text": "  two\n lines "}]}
    assert request_text(prompt) == "two lines"


# ------------------------------------------------------------ reading the stream


def test_parser_handles_events_split_across_reads():
    parser = EventParser()
    assert parser.feed(b'data: {"type":"server.conn') == []
    assert parser.feed(b'ected"}\n') == []
    assert parser.feed(b'\ndata: {"type":"a"}\r\n\r\n: comment\n\ndata: {"type":"b"}\n\ndata: {"ty') == [
        {"type": "server.connected"},
        {"type": "a"},
        {"type": "b"},
    ]
    assert parser.feed(b'pe":"c"}\n\n') == [{"type": "c"}]


def test_parser_skips_what_is_not_json_and_takes_very_long_events():
    parser = EventParser()
    big = b'{"type":"big","text":"' + b"x" * 500_000 + b'"}'
    events_out = parser.feed(b"data: not json\n\ndata: " + big + b"\n\n")
    assert [event["type"] for event in events_out] == ["big"] and len(events_out[0]["text"]) == 500_000


def test_parser_reads_a_whole_recording():
    stream = recorded("subagent")
    parser = EventParser()
    import json

    body = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in stream)
    out = []
    for start in range(0, len(body), 1000):
        out += parser.feed(body[start : start + 1000])
    assert out == stream
