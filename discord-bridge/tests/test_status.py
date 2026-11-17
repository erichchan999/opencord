import asyncio

from events import Turn
from fakes import Channel
from status import StatusMessage, format_duration, plain, render_finished, render_working, todo_lines

THREAD = 600


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def tool(turn, part_id, name, state_status, **given):
    turn.apply("message.part.updated", {"part": {"id": part_id, "type": "tool", "tool": name, "state": {"status": state_status, "input": given}}})


def todo(content, state_status="pending"):
    return {"content": content, "status": state_status, "priority": "high"}


def test_format_duration():
    assert [format_duration(s) for s in (0, 59.4, 60, 160, 3600, 3725)] == ["0s", "59s", "1m 00s", "2m 40s", "1h 00m", "1h 02m"]


def test_plain_keeps_text_on_one_harmless_line():
    assert plain("a `b`\n  c") == "a 'b' c"
    assert plain("x" * 200) == "x" * 149 + "…"


def test_a_new_request_shows_as_thinking():
    text = render_working(Turn("ses_1", THREAD), "DeepSeek Flash · effort high", 1794822771.9)
    assert text == "⏳ **Working** · started <t:1794822771:R> · DeepSeek Flash · effort high\n💭 Thinking"
    assert render_working(Turn("ses_1", THREAD), "", 5).splitlines()[0] == "⏳ **Working** · started <t:5:R>"


def test_the_running_tool_and_the_todo_list_are_shown():
    turn = Turn("ses_1", THREAD)
    tool(turn, "p1", "read", "completed", filePath="src/convert.ts")
    tool(turn, "p2", "bash", "running", command="npm test -- convert")
    turn.apply("todo.updated", {"todos": [todo("Read the test", "completed"), todo("Fix `rounding`", "in_progress"), todo("Run the suite")]})
    assert render_working(turn, "", 5).splitlines()[1:] == [
        "🔧 `bash` npm test -- convert",
        "✅ Read the test",
        "▶️ Fix 'rounding'",
        "⬜ Run the suite",
        "-# 2 tool calls so far",
    ]


def test_what_is_shown_first_when_several_things_are_true():
    turn = Turn("ses_1", THREAD)
    turn.text = "Let me look at the failing test first.\n\nThen I will fix it."
    assert render_working(turn, "", 5).splitlines()[1] == "💬 …Let me look at the failing test first. Then I will fix it."
    tool(turn, "p1", "bash", "pending")
    assert render_working(turn, "", 5).splitlines()[1] == "🔧 `bash`"
    turn.retry = {"attempt": 2, "message": "Provider is overloaded"}
    assert render_working(turn, "", 5).splitlines()[1] == "⚠️ The model provider is not answering. Retry 2: Provider is overloaded"
    turn.pause()
    assert render_working(turn, "", 5).splitlines()[1] == "❓ Waiting for an answer to the question below"


def test_long_writing_shows_its_latest_part():
    turn = Turn("ses_1", THREAD)
    turn.text = "start " + "word " * 200 + "the very end"
    line = render_working(turn, "", 5).splitlines()[1]
    assert line.startswith("💬 …") and line.endswith("the very end") and len(line) < 170


def test_a_long_todo_list_is_folded():
    todos = [todo(f"done {n}", "completed") for n in range(6)] + [todo("now", "in_progress")] + [todo(f"later {n}") for n in range(9)]
    lines = todo_lines(todos)
    assert lines == ["✅ 6 done", "▶️ now", *[f"⬜ later {n}" for n in range(5)], "… and 4 more"]
    assert len(todo_lines(todos[:8])) == 8


def test_the_message_never_outgrows_discord():
    turn = Turn("ses_1", THREAD)
    turn.todos = [todo("x" * 400) for _ in range(8)]
    for n in range(30):
        tool(turn, f"p{n}", "bash", "completed", command="y" * 400)
    assert len(render_working(turn, "label", 5)) <= 1900


def test_the_final_line_for_each_way_a_request_ends():
    clock = Clock()
    turn = Turn("ses_1", THREAD, clock=clock)
    clock.now += 160
    assert render_finished(turn, "done") == "-# ✅ Done in 2m 40s"
    tool(turn, "p1", "bash", "completed", command="ls")
    turn.tokens = 38200
    assert render_finished(turn, "done") == "-# ✅ Done in 2m 40s · 1 tool call · 38.2k tokens in context"
    assert render_finished(turn, "stopped") == "-# 🛑 Stopped after 2m 40s · 1 tool call · 38.2k tokens in context"
    assert render_finished(turn, "error").startswith("-# ⚠️ Ended with an error after 2m 40s")
    assert render_finished(turn, "timeout", 30).startswith("-# ⏱️ Stopped after 2m 40s: it ran past the 30 minute limit")
    assert "no activity for 15 minutes" in render_finished(turn, "silent", 15)
    assert "nobody answered the question within 60 minutes" in render_finished(turn, "unanswered", 60)


def test_status_message_is_sent_once_then_edited():
    async def run():
        channel = Channel()
        status = StatusMessage(channel)
        await status.show("one")
        await status.show("one")  # nothing new
        await status.show("two")
        assert channel.sent == [(9001, "one", None)] and status.message_id == 9001
        assert channel.edits == [(9001, "two", "unchanged")]

    asyncio.run(run())


def test_status_message_continues_one_from_before_a_restart():
    async def run():
        channel = Channel()
        await StatusMessage(channel, message_id=4242).show("back")
        assert channel.sent == [] and channel.edits == [(4242, "back", "unchanged")]

    asyncio.run(run())


def test_a_deleted_status_message_is_posted_again():
    async def run():
        channel = Channel()
        status = StatusMessage(channel)
        await status.show("one")
        channel.deleted.add(9001)
        await status.show("two")  # the edit fails: the message is gone, so it is posted again at once
        assert channel.texts() == ["one", "two"] and status.message_id == 9002
        await status.show("two")
        assert channel.texts() == ["one", "two"]

    asyncio.run(run())


def test_follow_edits_no_faster_than_its_interval():
    async def run():
        channel = Channel()
        turn = Turn("ses_1", THREAD)
        status = StatusMessage(channel, interval=0.05)
        shown = iter(range(100))
        task = asyncio.create_task(status.follow(turn, lambda: f"update {next(shown)}"))
        for _ in range(20):  # twenty changes in a tenth of a second
            turn.changed.set()
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.1)
        task.cancel()
        assert 2 <= len(channel.sent) + len(channel.edits) <= 5

    asyncio.run(run())
