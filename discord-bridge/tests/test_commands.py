"""Runs the slash command handlers against stand-ins for Discord and the agent."""

import asyncio
import types

import discord
from discord import app_commands

import commands
from catalog import split_model
from sample import FLASH, PRO, snapshot
from state import Store

CHANNEL = 500  # the watched channel
THREAD = 600  # a thread the bridge opened in it
ELSEWHERE = 999


class Response:
    def __init__(self):
        self.sent = []
        self.deferred = None

    async def send_message(self, text, *, ephemeral=False, allowed_mentions=None):
        self.sent.append((text, ephemeral))

    async def defer(self, *, ephemeral=False):
        self.deferred = ephemeral

    def is_done(self):
        return bool(self.sent) or self.deferred is not None


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, text, *, ephemeral=False, allowed_mentions=None):
        self.sent.append((text, ephemeral))


class Interaction:
    def __init__(self, channel_id):
        self.channel_id = channel_id
        self.channel = types.SimpleNamespace(id=channel_id)
        self.user = types.SimpleNamespace(name="tester")
        self.response = Response()
        self.followup = Followup()
        self.client = None
        self.command = None


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


class Agent:
    def __init__(self):
        self.summarized = []
        self.messages = []

    async def recent_messages(self, session_id, directory, limit):
        return self.messages

    async def summarize(self, session_id, directory, model, timeout):
        self.summarized.append((session_id, model))


class Catalog:
    async def get(self):
        return snapshot()


class Harness:
    def __init__(self, tmp_path):
        self.store = Store(str(tmp_path / "threads.json"), "/work/repo")
        self.store.load()
        self.entry = self.store.register_thread(THREAD, 42)
        self.queue = Queue()
        self.agent = Agent()
        self.stopped = []
        self.posted = []
        self.tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))

        async def thread_parent_id(channel):
            return CHANNEL if channel.id == THREAD else None

        async def stop_thread(thread_id, entry, stop_point):
            self.stopped.append((thread_id, stop_point))

        async def send(channel, text):
            self.posted.append(text)

        async def report_failure(channel, error):
            self.posted.append(f"failed: {error}")

        async def keep_typing(channel):
            await asyncio.sleep(3600)

        commands.setup(
            self.tree,
            commands.Context(
                store=self.store, agent=self.agent, catalog=Catalog(), queue=self.queue, channel_id=CHANNEL,
                compact_timeout=5, thread_parent_id=thread_parent_id, stop_thread=stop_thread, send=send,
                report_failure=report_failure, keep_typing=keep_typing,
            ),
        )

    def run(self, command, channel_id=THREAD, **arguments):
        """Runs a command as if used in that channel. Returns the interaction to inspect."""
        interaction = Interaction(channel_id)
        asyncio.run(self.tree.get_command(command).callback(interaction, **arguments))
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


def test_commands_fit_discords_limits(tmp_path):
    registered = {c.name: c for c in Harness(tmp_path).tree.get_commands()}
    assert set(registered) == {"model", "effort", "agent", "status", "help", "stop", "new", "compact"}
    for command in registered.values():
        assert 1 <= len(command.description) <= 100, command.name
        for parameter in command.parameters:
            assert 1 <= len(parameter.description) <= 100, (command.name, parameter.name)


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


def test_commands_do_nothing_outside_the_channel_and_its_threads(tmp_path):
    h = Harness(tmp_path)
    for command, arguments in [("model", {"name": PRO}), ("effort", {"level": "max"}), ("agent", {"name": "plan"}), ("status", {}), ("stop", {}), ("new", {}), ("compact", {})]:
        text, private = only_reply(h.run(command, ELSEWHERE, **arguments))
        assert private and f"<#{CHANNEL}>" in text, command
    assert h.store.defaults == {} and h.stopped == [] and h.queue.jobs == []


def test_thread_only_commands_say_so_in_the_main_channel(tmp_path):
    h = Harness(tmp_path)
    for command in ("stop", "new", "compact"):
        text, private = only_reply(h.run(command, CHANNEL))
        assert private and "inside one of my threads" in text, command


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


def test_stop_needs_something_running(tmp_path):
    h = Harness(tmp_path)
    text, private = only_reply(h.run("stop"))
    assert private and "Nothing is running" in text and h.stopped == []

    h.queue.busy = 2
    interaction = h.run("stop")
    assert h.stopped == [(THREAD, 7)]
    assert interaction.response.deferred is False
    assert interaction.followup.sent == [("Stopped the current request.", False)]


def test_new_drops_the_session_but_keeps_the_settings(tmp_path):
    h = Harness(tmp_path)
    h.entry.update(sessionId="ses_old", model=PRO)
    interaction = h.run("new")
    assert h.stopped == [(THREAD, 7)]
    assert h.entry["sessionId"] is None and h.entry["model"] == PRO
    assert h.saved().threads[str(THREAD)]["sessionId"] is None
    assert "fresh session" in interaction.followup.sent[0][0]


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
    h.agent.messages = [
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


def test_help_is_private_and_names_every_command(tmp_path):
    h = Harness(tmp_path)
    text, private = only_reply(h.run("help", ELSEWHERE))
    assert private
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
