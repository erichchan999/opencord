"""Runs the slash command handlers against stand-ins for Discord and the agent."""

import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands

import commands
from catalog import split_model
from fakes import Channel, Interaction
from sample import FLASH, PRO, snapshot
from state import Store

CHANNEL = 500  # an enabled channel
THREAD = 600  # a thread the bridge opened in it
STRANGER = 650  # a thread in it the bridge did not open
ELSEWHERE = 999  # a channel that is not enabled


class Queue:
    def __init__(self):
        self.busy = 0
        self.jobs = []

    def load(self, thread_id):
        return self.busy if thread_id == THREAD else 0

    def stop_point(self):
        return 7

    def submit(self, thread_id, job):
        self.jobs.append((thread_id, job))

    def run_all(self):
        jobs, self.jobs = self.jobs, []
        for _, job in jobs:
            asyncio.run(job())


class Agent:
    def __init__(self):
        self.summarized = []
        self.history = []  # the session's messages
        self.info = {"id": "ses_1", "title": "Discord: Fix the login bug"}  # GET /session/{id}
        self.todo_list = []
        self.changes = {}  # request message id -> file changes
        self.calls = []
        self.shell_answer = None
        self.shell_fails = None

    async def recent_messages(self, session_id, directory, limit):
        return self.history

    async def messages(self, session_id, directory):
        return self.history

    async def session(self, session_id, directory):
        return self.info

    async def summarize(self, session_id, directory, model, timeout):
        self.summarized.append((session_id, model))

    async def todos(self, session_id, directory):
        return self.todo_list

    async def diff(self, session_id, directory, message_id):
        return self.changes.get(message_id, [])

    async def revert(self, session_id, directory, message_id):
        self.calls.append(("revert", session_id, message_id))
        self.info["revert"] = {"messageID": message_id}

    async def unrevert(self, session_id, directory):
        self.calls.append(("unrevert", session_id))
        self.info.pop("revert", None)

    async def fork(self, session_id, directory, before=None):
        self.calls.append(("fork", session_id, before))
        return {"id": "ses_fork"}

    async def rename(self, session_id, directory, title):
        self.calls.append(("rename", session_id, title))

    async def delete_session(self, session_id, directory):
        self.calls.append(("delete", session_id))

    async def shell(self, session_id, directory, agent, command, timeout, model=None):
        self.calls.append(("shell", session_id, agent, command, model))
        if self.shell_fails:
            raise self.shell_fails
        return self.shell_answer

    async def abort(self, session_id, directory):
        self.calls.append(("abort", session_id))


class Catalog:
    async def get(self):
        return snapshot()


class Harness:
    def __init__(self, tmp_path):
        self.store = Store(str(tmp_path / "threads.json"), "/work/repo")
        self.store.load()
        self.entry = self.store.register_thread(THREAD, 42)
        self.thread = Channel(THREAD, "Fix the login bug")
        self.queue = Queue()
        self.agent = Agent()
        self.stopped = []
        self.posted = []  # texts posted to any channel
        self.posted_to = []  # (channel id, text)
        self.files = []  # (file name, bytes) posted with them
        self.commands_run = []
        self.opened = []  # (channel id, name, starter text) of threads opened
        self.tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
        self.home = Channel(CHANNEL, "agent")
        self.enabled = True

        async def home_of(channel):
            return self.home if self.enabled and channel.id in (CHANNEL, THREAD, STRANGER, 700) else None

        async def stop_thread(thread_id, entry, stop_point):
            self.stopped.append((thread_id, stop_point))

        async def send(channel, text, file=None):
            self.posted.append(text)
            self.posted_to.append((channel.id, text))
            if file is not None:
                self.files.append((file.filename, file.fp.getvalue()))

        async def report_failure(channel, error):
            self.posted.append(f"failed: {error}")

        async def keep_typing(channel):
            await asyncio.sleep(3600)

        async def ensure_session(channel, entry):
            entry["sessionId"] = entry.get("sessionId") or "ses_new"
            return entry["sessionId"]

        async def run_command(channel, entry, name, arguments):
            self.commands_run.append((channel.id, entry["sessionId"], name, arguments))

        self.open_fails = None

        async def open_thread(channel, name, starter):
            if self.open_fails:
                raise self.open_fails
            self.opened.append((channel.id, name, starter))
            return Channel(700, name)

        self.on_component = commands.setup(
            self.tree,
            commands.Context(
                store=self.store, agent=self.agent, catalog=Catalog(), queue=self.queue,
                compact_timeout=5, shell_timeout=600, home_of=home_of, stop_thread=stop_thread,
                send=send, report_failure=report_failure, keep_typing=keep_typing, ensure_session=ensure_session,
                run_command=run_command, open_thread=open_thread,
            ),
        )

    def interaction(self, channel_id=THREAD, **options):
        return Interaction(channel_id, channel=self.thread if channel_id == THREAD else None, **options)

    def run(self, command, channel_id=THREAD, /, **arguments):
        """Runs a command as if used in that channel. Returns the interaction to inspect."""
        interaction = self.interaction(channel_id)
        asyncio.run(self.tree.get_command(command).callback(interaction, **arguments))
        return interaction

    def click(self, custom_id, channel_id=THREAD):
        interaction = self.interaction(channel_id, custom_id=custom_id)
        assert asyncio.run(self.on_component(interaction)) is True
        return interaction

    def suggest(self, command, parameter, typed, channel_id=THREAD):
        handler = self.tree.get_command(command)._params[parameter].autocomplete
        return [(c.name, c.value) for c in asyncio.run(handler(Interaction(channel_id), typed))]

    def saved(self):
        again = Store(self.store.path, "/work/repo")
        again.load()
        return again


def only_reply(interaction):
    assert len(interaction.response.sent) == 1
    return interaction.response.sent[0]


def request(message_id, text, who="Tester", created=1763380800000):
    """A prompt the bridge forwarded, as the agent stores it."""
    return {
        "info": {"id": message_id, "role": "user", "time": {"created": created}},
        "parts": [{"type": "text", "text": f"Discord user {who} says:\n\n{text}"}],
    }


def reply(message_id, text="", tools=()):
    parts = [{"type": "tool", "tool": tool, "state": {"status": "completed", "input": given}} for tool, given in tools]
    if text:
        parts.append({"type": "text", "text": text})
    return {"info": {"id": message_id, "role": "assistant"}, "parts": parts}


ALL_COMMANDS = {
    "model", "effort", "agent", "status", "help", "stop", "clear", "compact", "todo", "diff", "export",
    "undo", "redo", "fork", "rename", "shell", "command",
}


def test_commands_fit_discords_limits(tmp_path):
    registered = Harness(tmp_path).tree.get_commands()
    assert {command.name for command in registered} == ALL_COMMANDS
    for command in registered:
        assert 1 <= len(command.description) <= 100, command.name
        for parameter in command.parameters:
            assert 1 <= len(parameter.description) <= 100, (command.name, parameter.name)


def test_a_queued_shell_is_skipped_when_its_channel_is_disabled(tmp_path):
    h = Harness(tmp_path)
    interaction = h.run("shell", command="ls")
    assert len(h.queue.jobs) == 1
    h.enabled = False
    h.queue.run_all()
    assert h.agent.calls == []
    assert interaction.followup.sent == [("Skipped: OpenCord was disabled in this channel before this could run.", False)]


def test_stop_still_works_once_the_channel_is_disabled(tmp_path):
    h = Harness(tmp_path)
    h.queue.busy = 1
    h.enabled = False
    h.run("stop")
    assert h.stopped == [(THREAD, 7)]


def test_model_in_a_thread_changes_that_thread(tmp_path):
    h = Harness(tmp_path)
    text, private = only_reply(h.run("model", name=PRO))
    assert "DeepSeek V4 Pro" in text and not private
    assert h.entry["model"] == PRO
    assert h.saved().threads[str(THREAD)]["model"] == PRO
    assert h.store.defaults == {}


def test_model_in_the_main_channel_sets_the_default_for_new_threads(tmp_path):
    h = Harness(tmp_path)
    text, private = only_reply(h.run("model", CHANNEL, name="deepseek-v4-pro"))
    assert "New threads" in text and not private
    assert h.store.defaults == {"model": PRO}
    assert "model" not in h.entry
    assert h.store.register_thread(601, 42)["model"] == PRO


def test_model_default_clears_the_setting(tmp_path):
    h = Harness(tmp_path)
    h.entry["model"] = PRO
    text, _ = only_reply(h.run("model", name="Default"))
    assert "model" not in h.entry and "DeepSeek Flash" in text


def test_unknown_model_is_refused_privately_and_changes_nothing(tmp_path):
    h = Harness(tmp_path)
    text, private = only_reply(h.run("model", name="gpt-9 @everyone"))
    assert private and FLASH in text and PRO in text
    assert "model" not in h.entry


THREAD_COMMANDS = [
    ("stop", {}), ("clear", {}), ("compact", {}), ("todo", {}), ("diff", {}), ("export", {}), ("undo", {}),
    ("redo", {}), ("fork", {}), ("rename", {"name": "x"}), ("shell", {"command": "ls"}),
    ("command", {"name": "review"}),
]


def test_commands_do_nothing_outside_the_channels_and_threads_the_bot_works_in(tmp_path):
    h = Harness(tmp_path)
    everywhere = [("model", {"name": PRO}), ("effort", {"level": "max"}), ("agent", {"name": "plan"}), ("status", {})]
    for place in (ELSEWHERE, STRANGER):
        for command, arguments in everywhere + THREAD_COMMANDS:
            text, private = only_reply(h.run(command, place, **arguments))
            assert private and "enabled channels" in text, (place, command)
    assert h.store.defaults == {} and h.stopped == [] and h.queue.jobs == [] and h.agent.calls == []


def test_help_points_to_the_channel_it_is_used_under(tmp_path):
    h = Harness(tmp_path)
    assert f"mention me in <#{CHANNEL}>" in only_reply(h.run("help", THREAD))[0]
    assert "mention me in an enabled channel" in only_reply(h.run("help", ELSEWHERE))[0]


def test_thread_only_commands_say_so_in_the_main_channel(tmp_path):
    h = Harness(tmp_path)
    for command, arguments in THREAD_COMMANDS:
        text, private = only_reply(h.run(command, CHANNEL, **arguments))
        assert private and "inside one of my threads" in text, command
    assert h.queue.jobs == [] and h.agent.calls == []


def test_effort_levels_come_from_the_threads_model(tmp_path):
    h = Harness(tmp_path)
    text, private = only_reply(h.run("effort", level="MAX"))
    assert h.entry["effort"] == "max" and "**max**" in text and not private

    text, private = only_reply(h.run("effort", level="medium"))
    assert private and "`off`, `low`, `high`, `max`" in text and h.entry["effort"] == "max"

    only_reply(h.run("effort", level="default"))
    assert "effort" not in h.entry

    h.entry["model"] = PRO
    text, private = only_reply(h.run("effort", level="max"))
    assert private and "no effort levels" in text and "effort" not in h.entry


def test_switching_to_a_model_without_the_chosen_effort_resets_it(tmp_path):
    h = Harness(tmp_path)
    h.entry["effort"] = "max"
    text, _ = only_reply(h.run("model", name=PRO))
    assert h.entry["model"] == PRO and "effort" not in h.entry
    assert "no **max** effort level" in text


def test_agent_must_be_one_the_agent_offers(tmp_path):
    h = Harness(tmp_path)
    text, private = only_reply(h.run("agent", name="plan"))
    assert h.entry["agent"] == "plan" and "Plan mode." in text and not private

    text, private = only_reply(h.run("agent", name="title"))
    assert private and "`build`, `plan`" in text and h.entry["agent"] == "plan"

    only_reply(h.run("agent", name="default"))
    assert "agent" not in h.entry


def test_a_change_during_a_request_says_when_it_applies(tmp_path):
    h = Harness(tmp_path)
    assert "next message" not in only_reply(h.run("model", name=PRO))[0]
    h.queue.busy = 1
    assert "next message" in only_reply(h.run("model", name=FLASH))[0]
    assert "next message" not in only_reply(h.run("model", CHANNEL, name=PRO))[0]


def test_suggestions_follow_what_is_typed(tmp_path):
    h = Harness(tmp_path)
    assert [value for _, value in h.suggest("model", "name", "")] == [FLASH, PRO, "default"]
    assert [value for _, value in h.suggest("model", "name", "pro")] == [FLASH, PRO]  # "pro" is in the provider id
    assert [value for _, value in h.suggest("model", "name", "v4")] == [PRO]
    assert [value for _, value in h.suggest("effort", "level", "")] == ["off", "low", "high", "max", "default"]
    assert [value for _, value in h.suggest("effort", "level", "", ELSEWHERE)] == ["off", "low", "high", "max", "default"]
    assert [value for _, value in h.suggest("agent", "name", "pl")] == ["plan"]
    assert [value for _, value in h.suggest("command", "name", "")] == ["init", "review", "customize-opencode"]
    assert h.suggest("command", "name", "uncommitted") == [
        ("review: review changes [commit|branch|pr], defaults to uncommitted", "review")
    ]


def test_stop_needs_something_running(tmp_path):
    h = Harness(tmp_path)
    text, private = only_reply(h.run("stop"))
    assert private and "Nothing is running" in text and h.stopped == []

    h.queue.busy = 2
    interaction = h.run("stop")
    assert h.stopped == [(THREAD, 7)]
    assert interaction.response.deferred is False
    assert interaction.followup.sent == [("Stopped the current request.", False)]


def test_clear_drops_the_session_keeps_the_settings_and_deletes_the_old_one_after_the_queue(tmp_path):
    h = Harness(tmp_path)
    h.entry.update(sessionId="ses_old", model=PRO)
    interaction = h.run("clear")
    assert h.stopped == [(THREAD, 7)]
    assert h.entry["sessionId"] is None and h.entry["model"] == PRO
    assert h.saved().threads[str(THREAD)]["sessionId"] is None
    assert "Cleared this thread's context" in interaction.followup.sent[0][0]
    # Deleted from the thread's queue, after whatever request was stopped has finished.
    assert h.agent.calls == [] and [thread_id for thread_id, _ in h.queue.jobs] == [THREAD]
    h.enabled = False  # a channel disabled meanwhile doesn't keep the old session around
    h.queue.run_all()
    assert h.agent.calls == [("delete", "ses_old")]


def test_clear_without_a_session_deletes_nothing(tmp_path):
    h = Harness(tmp_path)
    interaction = h.run("clear")
    assert h.queue.jobs == [] and h.entry["sessionId"] is None
    assert "Cleared this thread's context" in interaction.followup.sent[0][0]


def test_clear_still_clears_when_the_old_session_cannot_be_deleted(tmp_path):
    h = Harness(tmp_path)
    h.entry["sessionId"] = "ses_old"

    async def refuse_delete(session_id, directory):
        raise OSError("agent unreachable")

    h.agent.delete_session = refuse_delete
    h.run("clear")
    h.queue.run_all()  # logs a warning, raises nothing
    assert h.entry["sessionId"] is None


def test_compact_waits_its_turn_and_uses_the_threads_model(tmp_path):
    h = Harness(tmp_path)
    text, private = only_reply(h.run("compact"))
    assert private and "nothing to compact" in text and h.queue.jobs == []

    h.entry.update(sessionId="ses_1", model=PRO)
    h.queue.busy = 1
    text, private = only_reply(h.run("compact"))
    assert not private and "after the current request" in text
    (thread_id, job), = h.queue.jobs
    assert thread_id == THREAD and h.agent.summarized == []
    asyncio.run(job())
    assert h.agent.summarized == [("ses_1", split_model(PRO))]
    assert h.posted == ["Compacted. Earlier messages are now a summary."]


def test_compact_uses_the_model_chosen_while_it_waited(tmp_path):
    h = Harness(tmp_path)
    h.entry["sessionId"] = "ses_1"
    h.queue.busy = 1
    h.run("compact")
    h.entry["model"] = PRO  # someone runs /model before the compaction gets its turn
    h.queue.run_all()
    assert h.agent.summarized == [("ses_1", split_model(PRO))]


def test_compact_queued_before_new_finds_nothing_to_do(tmp_path):
    h = Harness(tmp_path)
    h.entry["sessionId"] = "ses_1"
    h.run("compact")
    h.entry["sessionId"] = None
    asyncio.run(h.queue.jobs[0][1]())
    assert h.agent.summarized == [] and "nothing to compact" in h.posted[0]


def test_status_in_a_thread(tmp_path):
    h = Harness(tmp_path)
    interaction = h.run("status")
    text, private = interaction.followup.sent[0]
    assert interaction.response.deferred is True and private
    assert "DeepSeek Flash (`deepseek-proxy/deepseek-flash`, the default)" in text
    assert "**Effort:** the model default" in text and "**Agent:** build (the default)" in text
    assert "**State:** idle" in text and "none yet" in text

    h.entry.update(sessionId="ses_1", model=PRO, effort="max", agent="plan")
    h.queue.busy = 3
    h.agent.history = [
        {"info": {"role": "user"}},
        {"info": {"role": "assistant", "providerID": "deepseek-proxy", "modelID": "deepseek-v4-pro", "tokens": {"total": 32000}}},
    ]
    text, _ = h.run("status").followup.sent[0]
    assert "DeepSeek V4 Pro (`deepseek-proxy/deepseek-v4-pro`)" in text
    # Pro has no effort levels in the sample, so the stored "max" does not apply.
    assert "**Effort:** the model default" in text and "**Agent:** plan" in text
    assert "**State:** working, 2 more waiting" in text
    assert "**Context:** 32k tokens of 128k (25%)" in text and "`ses_1`" in text


def test_status_in_the_main_channel_shows_the_defaults(tmp_path):
    h = Harness(tmp_path)
    h.store.defaults["effort"] = "low"
    h.queue.busy = 1
    text, private = h.run("status", CHANNEL).followup.sent[0]
    assert private and text.startswith("New threads start with:")
    assert "**Effort:** low" in text and "1 known, 1 working" in text


def test_help_is_private_fits_one_message_and_names_every_command(tmp_path):
    h = Harness(tmp_path)
    text, private = only_reply(h.run("help", ELSEWHERE))
    assert private and len(text) <= 2000
    for command in h.tree.get_commands():
        assert command.name == "help" or f"`/{command.name}`" in text


def test_format_tokens():
    assert [commands.format_tokens(n) for n in (0, 999, 1000, 7719, 128000)] == ["0", "999", "1k", "7.7k", "128k"]


def test_context_usage_reads_the_last_reply():
    snap = snapshot()
    assert commands.context_usage([], snap) is None
    assert commands.context_usage([{"info": {"role": "user"}}], snap) is None
    messages = [
        {"info": {"role": "assistant", "providerID": "deepseek-proxy", "modelID": "deepseek-flash", "tokens": {"total": 100}}},
        {"info": {"role": "assistant", "providerID": "other", "modelID": "unknown", "tokens": {"total": 500}}},
        {"info": {"role": "user"}},
    ]
    assert commands.context_usage(messages, snap) == (500, None)
    assert commands.context_usage(messages[:1], snap) == (100, 128000)


# ---------------------------------------------------------------- reading a session


def test_live_messages_leave_out_what_undo_took_back():
    history = [request("msg_1", "a"), reply("msg_2", "A"), request("msg_3", "b"), reply("msg_4", "B")]
    assert commands.live_messages(history, {"id": "ses_1"}) == (history, 0)
    assert commands.live_messages(history, {"revert": {"messageID": "msg_3"}}) == (history[:2], 2)
    assert commands.live_messages(history, {"revert": {"messageID": "msg_gone"}}) == (history, 0)
    assert commands.live_messages(None, None) == ([], 0)


def test_asked_separates_the_person_from_their_words():
    assert commands.asked(request("msg_1", "fix it\nplease", who="Erich")) == ("Erich", "fix it\nplease")
    command = {"info": {"role": "user"}, "parts": [{"type": "subtask", "command": "review", "description": "review"}]}
    assert commands.asked(command) == (None, "/review")
    plain = {"info": {"role": "user"}, "parts": [{"type": "text", "text": "From the TUI"}]}
    assert commands.asked(plain) == (None, "From the TUI")


def test_build_diff_keeps_each_requests_patches_under_a_heading():
    patch_a = "Index: a.txt\n===\n--- a.txt\n+++ a.txt\n@@ -1 +1,2 @@\n one\n+two\n"
    patch_b = "Index: b.txt\n===\n--- b.txt\n+++ b.txt\n@@ -0,0 +1 @@\n+new\n"
    text, totals = commands.build_diff(
        [
            (1, "add two", [{"file": "a.txt", "patch": patch_a, "additions": 1, "deletions": 0}]),
            (2, "just asking", []),
            (3, "more", [{"file": "a.txt", "patch": patch_a, "additions": 2, "deletions": 1}, {"file": "b.txt", "patch": patch_b, "additions": 1, "deletions": 0}]),
        ]
    )
    assert text.startswith("# Request 1: add two\nIndex: a.txt")
    assert "# Request 2" not in text and "# Request 3: more\n" in text
    assert totals == {"a.txt": [3, 1], "b.txt": [1, 0]}
    assert commands.diff_summary(totals) == "**2 files changed** in this session (+4 −1):\n`a.txt` +3 −1\n`b.txt` +1 −0"


def test_transcript_shows_requests_replies_and_tools():
    history = [
        request("msg_1", "list the files", who="Erich"),
        reply("msg_2", tools=[("bash", {"command": "ls -la"})]),
        {"info": {"id": "msg_3", "role": "user"}, "parts": [{"type": "text", "text": "Summarize the task", "synthetic": True}]},
        reply("msg_4", "Two files.\n\n- a\n- b"),
        {"info": {"id": "msg_5", "role": "assistant", "error": {"name": "APIError", "data": {"message": "rate limited"}}}, "parts": []},
    ]
    text = commands.transcript(history, "Files", "ses_1", undone=2, now=datetime(2026, 11, 17, 9, 30, tzinfo=timezone.utc))
    assert text.startswith("# Files\n\nOpenCode session `ses_1`, exported 2026-11-17 09:30 UTC.")
    assert "## Erich · 2025-11-17 12:00 UTC\n\nlist the files" in text
    assert text.count("### Agent") == 1 and "### Agent\n\n- `bash` ls -la\n\nTwo files." in text
    assert "Summarize the task" not in text
    assert "Two files.\n\n- a\n- b\n\n> ⚠️ APIError: rate limited" in text
    assert text.endswith("_2 later messages taken back with /undo are left out._\n")
    assert "\n\n\n" not in text


def test_todo_lists_every_item_privately(tmp_path):
    h = Harness(tmp_path)
    text, private = only_reply(h.run("todo"))
    assert private and "no todo list" in text

    h.entry["sessionId"] = "ses_1"
    assert "no todo list in this session" in h.run("todo").followup.sent[0][0]

    h.agent.todo_list = [{"content": f"step {n}", "status": "completed" if n < 4 else "pending"} for n in range(12)]
    interaction = h.run("todo")
    text, private = interaction.followup.sent[0]
    assert interaction.response.deferred is True and private
    assert text.startswith("**Todo list** · 4 of 12 done\n") and "✅ step 0" in text and "⬜ step 11" in text


def test_diff_posts_the_sessions_changes_as_a_file(tmp_path):
    h = Harness(tmp_path)
    h.entry["sessionId"] = "ses_1"
    h.agent.history = [request("msg_1", "edit a"), reply("msg_2", "ok"), request("msg_3", "undone"), reply("msg_4", "ok")]
    patch = "--- a.txt\n+++ a.txt\n@@ -1 +1 @@\n-x\n+y\n"
    h.agent.changes = {"msg_1": [{"file": "a.txt", "patch": patch, "additions": 1, "deletions": 1}], "msg_3": [{"file": "z.txt", "patch": "zzz"}]}
    h.agent.info["revert"] = {"messageID": "msg_3"}  # the second request was undone
    interaction = h.run("diff")
    assert interaction.response.deferred is False
    assert interaction.followup.sent == [("**1 file changed** in this session (+1 −1):\n`a.txt` +1 −1", False)]
    ((name, data),) = interaction.followup.files
    assert name == "changes.diff" and data.decode() == f"# Request 1: edit a\n{patch.rstrip()}\n"

    h.agent.changes = {}
    assert h.run("diff").followup.sent == [("This session hasn't changed any files.", False)]


def test_export_posts_the_conversation_as_markdown(tmp_path):
    h = Harness(tmp_path)
    assert only_reply(h.run("export"))[1] is True  # nothing yet
    h.entry["sessionId"] = "ses_1"
    h.agent.history = [request("msg_1", "hello"), reply("msg_2", "Hi there.")]
    interaction = h.run("export")
    assert interaction.followup.sent == [("This thread's conversation so far: 1 request.", False)]
    ((name, data),) = interaction.followup.files
    text = data.decode()
    assert name == "conversation.md" and text.startswith("# Fix the login bug\n") and "Hi there." in text


def test_rename_renames_the_thread_and_its_session(tmp_path):
    h = Harness(tmp_path)
    h.entry["sessionId"] = "ses_1"
    interaction = h.run("rename", name="  Login   bug, take two ")
    assert h.thread.name == "Login bug, take two"
    assert h.agent.calls == [("rename", "ses_1", "Discord: Login bug, take two")]
    assert interaction.followup.sent == [("Renamed this thread to **Login bug, take two**.", False)]

    h.thread.fail_edit = discord.Forbidden(type("R", (), {"status": 403, "reason": "no"})(), "Missing Access")
    text, _ = h.run("rename", name="Nope").followup.sent[0]
    assert text.startswith("Discord did not let me rename this thread") and h.thread.name == "Login bug, take two"
    assert len(h.agent.calls) == 1

    assert "can't be empty" in only_reply(h.run("rename", name="   "))[0]


# ---------------------------------------------------------------- undo and redo


def test_undo_asks_first_and_names_the_files(tmp_path):
    h = Harness(tmp_path)
    assert "nothing to undo yet" in only_reply(h.run("undo"))[0]

    h.entry["sessionId"] = "ses_1"
    h.queue.busy = 1
    text, private = only_reply(h.run("undo"))
    assert private and "Wait for it to finish" in text
    h.queue.busy = 0

    assert h.run("undo").followup.sent == [("There is nothing left to undo in this session.", True)]

    h.agent.history = [request("msg_1", "first"), reply("msg_2"), request("msg_3", "add **logging**"), reply("msg_4", "done")]
    h.agent.changes = {"msg_3": [{"file": "app.py"}, {"file": "log.py"}]}
    interaction = h.run("undo")
    ((text, private),) = interaction.followup.sent
    assert private and text.startswith("Undo the last request, “add \\*\\*logging\\*\\*” from Tester?")
    assert "`app.py`, `log.py`" in text and "same checkout" in text and "`/redo`" in text
    view = interaction.followup.views[0]
    assert [item.custom_id for item in view.children] == ["c:undo:msg_3", "c:cancel"] and view.is_finished()
    assert h.agent.calls == [] and h.queue.jobs == []  # nothing happens before the click

    h.agent.changes = {}
    assert "changed no files" in h.run("undo").followup.sent[0][0]


def test_undo_happens_on_the_click(tmp_path):
    h = Harness(tmp_path)
    h.entry["sessionId"] = "ses_1"
    h.agent.history = [request("msg_1", "first"), reply("msg_2"), request("msg_3", "second"), reply("msg_4")]
    h.agent.changes = {"msg_3": [{"file": "app.py"}]}
    interaction = h.click("c:undo:msg_3")
    assert interaction.response.edited == [("Undoing…", None)]
    h.queue.run_all()
    assert h.agent.calls == [("revert", "ses_1", "msg_3")]
    assert h.posted == [
        "↩️ **Tester** undid the last request, “second”. Files put back: `app.py`. "
        "`/redo` brings it back until the next message is sent."
    ]

    # Undo again: the request before the one already taken back.
    h.click("c:undo:msg_1")
    h.queue.run_all()
    assert h.agent.calls[-1] == ("revert", "ses_1", "msg_1") and "“first”. It had changed no files." in h.posted[-1]


def test_an_undo_click_after_the_thread_moved_on_does_nothing(tmp_path):
    h = Harness(tmp_path)
    h.entry["sessionId"] = "ses_1"
    h.agent.history = [request("msg_1", "first"), reply("msg_2"), request("msg_5", "newer"), reply("msg_6")]
    h.click("c:undo:msg_1")
    h.queue.run_all()
    assert h.agent.calls == [] and "Nothing was undone" in h.posted[0]

    h.queue.busy = 1
    interaction = h.click("c:undo:msg_5")
    assert "Wait for it to finish" in interaction.response.edited[0][0] and h.queue.jobs == []


def test_undo_cancel_and_stale_buttons(tmp_path):
    h = Harness(tmp_path)
    assert h.click("c:cancel").response.edited == [("Cancelled. Nothing was undone.", None)]
    assert h.click("c:something-old").response.edited == [("This button no longer works.", None)]
    assert asyncio.run(h.on_component(Interaction(THREAD, custom_id="q:que_1:0:o:0"))) is False
    assert h.agent.calls == []


def test_redo_brings_back_what_undo_took(tmp_path):
    h = Harness(tmp_path)
    h.entry["sessionId"] = "ses_1"
    interaction = h.run("redo")
    h.queue.run_all()
    ((text, private),) = interaction.followup.sent
    assert text.startswith("There is nothing to redo.") and not private and h.agent.calls == []

    h.agent.history = [request("msg_1", "a"), reply("msg_2"), request("msg_3", "b"), reply("msg_4"), request("msg_5", "c")]
    h.agent.info["revert"] = {"messageID": "msg_3"}
    interaction = h.run("redo")
    assert interaction.response.deferred is False and h.agent.calls == []  # queued
    h.queue.run_all()
    assert h.agent.calls == [("unrevert", "ses_1")]
    assert interaction.followup.sent == [("↪️ **Tester** brought back 2 requests taken back with `/undo`, file changes included.", False)]


def test_a_late_redo_answer_goes_to_the_thread(tmp_path):
    h = Harness(tmp_path)
    h.entry["sessionId"] = "ses_1"
    h.agent.history = [request("msg_1", "a"), reply("msg_2"), request("msg_3", "b"), reply("msg_4")]
    h.agent.info["revert"] = {"messageID": "msg_3"}
    interaction = h.run("redo")
    interaction.followup.fail = True  # the interaction expired while the redo waited
    h.queue.run_all()
    assert h.posted == ["↪️ **Tester** brought back 1 request taken back with `/undo`, file changes included."]


# ---------------------------------------------------------------- fork


def test_fork_opens_a_thread_with_a_copy_of_the_session(tmp_path):
    h = Harness(tmp_path)
    assert "nothing to fork yet" in only_reply(h.run("fork"))[0]

    h.entry.update(sessionId="ses_1", model=PRO, agent="plan")
    h.store.defaults["effort"] = "max"  # a default the source thread does not have
    text, private = only_reply(h.run("fork"))
    assert text == "Forking this thread." and not private
    h.queue.run_all()
    assert h.agent.calls[0] == ("fork", "ses_1", None)
    assert h.opened == [(CHANNEL, "Fix the login bug (fork)", f"🍴 **Tester** forked <#{THREAD}> into a new thread.")]
    copy = h.saved().threads["700"]
    assert copy["sessionId"] == "ses_fork" and copy["model"] == PRO and copy["agent"] == "plan" and "effort" not in copy
    assert ("rename", "ses_fork", "Discord: Fix the login bug (fork)") in h.agent.calls
    assert h.posted_to[0][0] == 700 and f"carries on from <#{THREAD}>" in h.posted_to[0][1]
    assert h.posted_to[1] == (THREAD, "🍴 Forked into <#700>.")


def test_fork_after_an_undo_leaves_the_undone_part_behind(tmp_path):
    h = Harness(tmp_path)
    h.entry["sessionId"] = "ses_1"
    h.agent.info["revert"] = {"messageID": "msg_3"}
    h.queue.busy = 1
    assert only_reply(h.run("fork"))[0] == "Forking this thread after the current request."
    h.queue.run_all()
    assert h.agent.calls[0] == ("fork", "ses_1", "msg_3")


def test_a_fork_whose_thread_cannot_open_removes_the_copy(tmp_path):
    h = Harness(tmp_path)
    h.entry["sessionId"] = "ses_1"
    h.open_fails = discord.Forbidden(type("R", (), {"status": 403, "reason": "no"})(), "Missing Permissions")
    h.run("fork")
    h.queue.run_all()
    assert h.agent.calls == [("fork", "ses_1", None), ("delete", "ses_fork")]
    assert list(h.store.threads) == [str(THREAD)] and h.posted[0].startswith("failed: ")


def test_fork_name():
    assert commands.fork_name("Fix it") == "Fix it (fork)"
    assert commands.fork_name("Fix it (fork)") == "Fix it (fork)"
    assert commands.fork_name(None) == "thread (fork)"
    assert len(commands.fork_name("x" * 100)) <= 100


# ---------------------------------------------------------------- shell and command


def shell_answer(output, status="completed", error=None):
    state = {"status": status, "input": {"command": "x"}, "output": output}
    if error:
        state["error"] = error
    return {"info": {"role": "assistant"}, "parts": [{"type": "tool", "tool": "bash", "state": state}]}


def test_shell_runs_in_the_threads_session_and_posts_the_output(tmp_path):
    h = Harness(tmp_path)
    h.entry["agent"] = "plan"
    h.agent.shell_answer = shell_answer("a.txt\nb.txt\n")
    text, private = only_reply(h.run("shell", command="  ls  "))
    assert text == "🖥️ **Tester** runs `ls`." and not private
    h.queue.run_all()
    assert h.entry["sessionId"] == "ses_new"  # a thread without a session gets one
    assert h.agent.calls == [("shell", "ses_new", "plan", "ls", split_model(FLASH))]
    assert h.posted == ["```\na.txt\nb.txt\n```"]


def test_shell_output_that_is_too_long_is_attached(tmp_path):
    h = Harness(tmp_path)
    output = "\n".join(f"line {n}" for n in range(1000))
    h.agent.shell_answer = shell_answer(output)
    h.run("shell", command="seq")
    h.queue.run_all()
    (text,) = h.posted
    assert len(text) <= 1900 and text.startswith("```\n…\n") and "line 999\n```" in text and "line 0\n" not in text
    assert h.files == [("output.txt", output.encode())]


def test_shell_failures(tmp_path):
    h = Harness(tmp_path)
    h.agent.shell_answer = shell_answer("", status="error", error="spawn /bin/sh ENOENT")
    h.run("shell", command="ls")
    h.queue.run_all()
    h.agent.shell_answer = shell_answer("")
    h.run("shell", command="true")
    h.queue.run_all()
    assert h.posted == ["The command could not run: spawn /bin/sh ENOENT", "-# The command printed nothing."]

    h.agent.shell_fails = TimeoutError("POST /session/ses_new/shell timed out after 600s")
    h.run("shell", command="sleep 9999")
    h.queue.run_all()
    assert h.agent.calls[-1] == ("abort", "ses_new")
    assert h.posted[-1] == "The command was still running after 10m 00s, so I stopped it."


def test_shell_output_cannot_close_its_code_block():
    text, full = commands.shell_message("before ``` after")
    assert text == "```\nbefore `​`​` after\n```" and full is None
    for run in ("````", "``````", "`` ``"):
        inner = commands.shell_message(run)[0].removeprefix("```\n").removesuffix("\n```")
        assert "``" not in inner, run


def test_long_shell_output_made_of_fences_still_fits_one_message():
    output = "```\n" * 2000
    text, full = commands.shell_message(output)
    assert len(text) <= 1900 and full == output.rstrip()
    assert "``" not in text.removeprefix("```\n").split("\n```\n-#")[0]


def test_command_runs_one_of_the_agents_commands(tmp_path):
    h = Harness(tmp_path)
    text, private = only_reply(h.run("command", name="nope"))
    assert private and "`init`, `review`, `customize-opencode`" in text and h.queue.jobs == []

    h.entry["sessionId"] = "ses_1"
    h.queue.busy = 1
    text, private = only_reply(h.run("command", name="/review", arguments=" HEAD~1 "))
    assert text == "▶️ **Tester** runs `/review HEAD~1` after the current request." and not private
    h.queue.run_all()
    assert h.commands_run == [(THREAD, "ses_1", "review", "HEAD~1")]
