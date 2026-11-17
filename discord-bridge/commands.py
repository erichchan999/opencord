"""Slash commands: the Discord side of OpenCode's session settings and controls.

The commands are registered to every server the bot is in. Session commands
work only in enabled channels and the threads the bot opened there.
Inside one of the bridge's threads they act on that thread. /model, /effort
and /agent used in an enabled channel (outside a thread) set what new threads
start with, in every enabled channel.

A change is answered publicly, so everyone in the thread sees it. Lookups and
refusals are shown only to the person who ran the command.

Commands that work on the session (/compact, /fork, /shell, /command, /redo
and the undo button) are queued like messages, so they never run in the middle
of a request.
"""

import asyncio
import contextlib
import io
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

import discord
from discord import app_commands

from catalog import DEFAULT_WORD, model_key
from events import is_request, parts_text, request_text
from state import SETTINGS, set_setting, settings_of
from status import MESSAGE_LIMIT, format_duration, format_tokens, plain, plural

log = logging.getLogger("bridge")

NO_MENTIONS = discord.AllowedMentions.none()
# How many of a session's latest messages /status reads to find the last reply.
STATUS_MESSAGES = 20
# Largest file posted to Discord, which takes up to 10 MB from a bot.
FILE_LIMIT = 8_000_000
# Custom ids of the buttons these commands show start with this.
BUTTON_PREFIX = "c:"
# Longest wait for Discord to rename a thread. It allows two renames every ten minutes.
RENAME_WAIT = 10
# The bridge puts this in front of every message it forwards (see bridge.run_turn).
SPEAKER = re.compile(r"^Discord user (.+?) says:\n\n", re.S)


@dataclass
class Context:
    """What the commands need from the rest of the bridge."""

    store: Any  # state.Store
    agent: Any  # opencode.OpenCode
    catalog: Any  # catalog.Catalog
    queue: Any  # bridge.ThreadQueue
    compact_timeout: float
    shell_timeout: float
    home_of: Callable  # async (channel) -> its enabled text channel (itself or a thread's parent), or None
    stop_thread: Callable  # async (thread_id, entry, stop_point)
    send: Callable  # async (channel, text, file=None)
    report_failure: Callable  # async (channel, error)
    keep_typing: Callable  # async (channel), runs until cancelled
    ensure_session: Callable  # async (channel, entry) -> session id, creating or replacing one if needed
    run_command: Callable  # async (channel, entry, name, arguments), follows it like a prompt
    open_thread: Callable  # async (channel, name, starter text) -> a new thread in that channel


def session_title(name):
    """The title a thread's session gets on the agent."""
    return f"Discord: {name}"


def context_usage(messages, snapshot):
    """(tokens in context, model's context limit or None) from a session's latest messages.

    The last reply's token total is what the model was sent plus what it
    wrote, which is the size of the conversation so far. None if there is no
    reply yet.
    """
    for message in reversed(messages or []):
        info = message.get("info") or {}
        total = (info.get("tokens") or {}).get("total")
        if info.get("role") == "assistant" and total:
            model = snapshot.models.get(f"{info.get('providerID')}/{info.get('modelID')}", {})
            return total, model.get("context")
    return None


def describe_settings(snapshot, settings):
    """Lines for /status: what these settings mean right now."""
    options, _ = snapshot.resolve(settings)
    if "model" in options:
        key = model_key(options["model"])
        own = settings.get("model") == key
        model = f"{snapshot.model_name(key)} (`{key}`{'' if own else ', the default'})"
    else:
        model = "the agent's default"
    return [
        f"**Model:** {model}",
        f"**Effort:** {options.get('variant') or 'the model default'}",
        f"**Agent:** {options.get('agent') or snapshot.default_agent + ' (the default)'}",
    ]


def matching_choices(pairs, typed):
    """Up to 25 (label, value) pairs that contain what has been typed so far."""
    typed = typed.strip().lower()
    return [
        app_commands.Choice(name=label[:100], value=value[:100])
        for label, value in pairs
        if typed in label.lower() or typed in value.lower()
    ][:25]


def snippet(text, limit=100):
    """Text from the conversation as one short line that cannot break a message's formatting."""
    return discord.utils.escape_markdown(plain(text, limit))


def quoted(text):
    """Something a person typed, made safe to show back to them."""
    return snippet(text, 80)


def name_of(user):
    return discord.utils.escape_markdown(getattr(user, "display_name", None) or user.name)


def file_of(data, filename):
    """A discord.File holding `data`, cut to what Discord accepts."""
    if len(data) > FILE_LIMIT:
        data = data[:FILE_LIMIT] + b"\n\n[cut here: the rest is too large for Discord]\n"
    return discord.File(io.BytesIO(data), filename=filename)


def unfenced(text):
    """Text that cannot end a code block: no two backticks are left side by side."""
    return re.sub(r"`(?=`)", "`​", text)


# ---------------------------------------------------------------- reading a session


def live_messages(messages, session):
    """(messages still in the conversation, how many were left out).

    After /undo, the messages taken back are still listed until the next
    prompt drops them for good. They no longer count.
    """
    messages = messages or []
    revert_id = ((session or {}).get("revert") or {}).get("messageID")
    ids = [(m.get("info") or {}).get("id") for m in messages]
    if revert_id in ids:
        cut = ids.index(revert_id)
        return messages[:cut], len(messages) - cut
    return messages, 0


def asked(message):
    """(who, text) of a request: the Discord user and what they wrote.

    `who` is None for a slash command, or a prompt sent some other way.
    """
    if any(part.get("type") == "subtask" for part in message.get("parts") or []):
        return None, request_text(message)
    text = parts_text(message.get("parts"))
    match = SPEAKER.match(text)
    if match:
        return match.group(1), text[match.end() :]
    return None, text


def shell_result(message):
    """(output, error) of a /shell run, from the message the agent stored for it."""
    for part in (message or {}).get("parts") or []:
        if part.get("type") == "tool":
            state = part.get("state") or {}
            output = state.get("output")
            if not isinstance(output, str):
                output = (state.get("metadata") or {}).get("output") or ""
            return output, state.get("error") if state.get("status") == "error" else None
    return "", None


def shell_message(output, limit=MESSAGE_LIMIT - 100):
    """(text, full output to attach or None) for posting a command's output."""
    output = output.rstrip()
    if not output:
        return "-# The command printed nothing.", None
    # Made safe before it is measured and cut, since that makes it longer.
    safe = unfenced(output)
    if len(safe) + 8 <= limit:
        return f"```\n{safe}\n```", None
    # The end of the output usually says how it went.
    tail = safe[-(limit - 100) :]
    tail = tail[tail.find("\n") + 1 :] if "\n" in tail else tail
    return f"```\n…\n{tail}\n```\n-# The end of the output. All of it is attached.", output


# Said by /undo: OpenCode only snapshots files in a git repo, and the sandbox is not one.
FILES_STAY = "Files in the sandbox stay as they are."


def undo_question(target):
    who, text = asked(target)
    return "\n".join(
        [
            f"Undo the last request, “{snippet(text)}”{' from ' + discord.utils.escape_markdown(who) if who else ''}?",
            f"It comes out of the conversation, so the agent forgets it. {FILES_STAY}",
            "`/redo` brings it back, until the next message is sent in this thread.",
        ]
    )


def confirm_view(message_id):
    """The buttons under the /undo question. Must be built while the event loop runs."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(label="Undo it", style=discord.ButtonStyle.danger, custom_id=f"{BUTTON_PREFIX}undo:{message_id}")
    )
    view.add_item(discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id=f"{BUTTON_PREFIX}cancel"))
    # Stopped so discord.py does not keep it: clicks arrive in on_component.
    view.stop()
    return view


async def rename_thread(thread, client, name):
    """Renames a thread, fetching it first if discord.py's cache gave a stand-in that can't be edited.

    Raises TimeoutError after RENAME_WAIT seconds, or Discord's error.
    """
    if not hasattr(thread, "edit"):
        thread = await client.fetch_channel(thread.id)
    await asyncio.wait_for(thread.edit(name=name), RENAME_WAIT)


def fork_name(name):
    base = re.sub(r" \(fork(?: \d+)?\)$", "", name or "thread")
    return f"{base[:90]} (fork)"


def setup(tree, ctx):
    """Adds the commands to `tree`."""
    wrong_place = "I only work in enabled channels and the threads I open there."
    thread_only = "Use this inside one of my threads."
    busy = "I'm working on a request in this thread. Wait for it to finish, or `/stop` it, then try again."
    nothing_to_redo = "There is nothing to redo. `/redo` works after `/undo`, until the next message is sent."

    def channel_of(interaction):
        return interaction.channel or interaction.client.get_partial_messageable(interaction.channel_id)

    async def scope_of(interaction):
        """("thread", its entry) in one of the bridge's threads, ("channel", the defaults) in
        an enabled channel, (None, None) anywhere else.

        Discord delivers commands from every channel, even ones the bot cannot
        see, so this is the gate for them."""
        home = await ctx.home_of(channel_of(interaction))
        if home is None:
            return None, None
        if home.id == interaction.channel_id:
            return "channel", ctx.store.defaults
        entry = ctx.store.threads.get(str(interaction.channel_id))
        return ("thread", entry) if entry is not None else (None, None)

    async def say(interaction, text, *, private=False):
        await interaction.response.send_message(text, ephemeral=private, allowed_mentions=NO_MENTIONS)

    async def refuse(interaction, text):
        await say(interaction, text, private=True)

    def enqueue(interaction, job):
        """Queues `job` for the thread. Skipped, and the person told, if the channel was disabled meanwhile."""

        async def run_if_enabled():
            channel = channel_of(interaction)
            if await ctx.home_of(channel) is not None:
                return await job()
            # Also answers a deferred command, which would otherwise stay "thinking".
            await later(interaction, channel)("Skipped: OpenCord was disabled in this channel before this could run.")

        ctx.queue.submit(interaction.channel_id, run_if_enabled)

    async def thread_entry(interaction):
        """The entry of the thread the command was used in. Refuses and returns None elsewhere."""
        where, entry = await scope_of(interaction)
        if where == "thread":
            return entry
        await refuse(interaction, thread_only if where == "channel" else wrong_place)
        return None

    def note(interaction, what):
        log.info("channel %s: /%s by %s", interaction.channel_id, what, interaction.user.name)

    def later(interaction, channel):
        """Answers a deferred command from a queued job. Posts in the channel if the answer is too late."""

        async def answer(text):
            try:
                await interaction.followup.send(text, allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                await ctx.send(channel, text)

        return answer

    def queued(interaction):
        """Said when a queued command will wait for the request in progress."""
        return " after the current request" if ctx.queue.load(interaction.channel_id) else ""

    async def session_and_messages(entry):
        """The session and all its messages, read together."""
        session_id, directory = entry["sessionId"], entry["directory"]
        return await asyncio.gather(ctx.agent.session(session_id, directory), ctx.agent.messages(session_id, directory))

    def when(interaction, where):
        """Added to a change made while the thread is busy."""
        busy = where == "thread" and ctx.queue.load(interaction.channel_id)
        return " It applies from the next message." if busy else ""

    async def change(interaction, scope, name, value, thread_text, channel_text, extra=""):
        """Stores a setting for the thread or the channel defaults and announces it."""
        where, target = scope
        set_setting(target, name, value)
        ctx.store.save()
        note(interaction, f"{name} {value or DEFAULT_WORD}")
        await say(interaction, (thread_text if where == "thread" else channel_text) + extra + when(interaction, where))

    # ------------------------------------------------------------ settings

    async def model_choices(interaction: discord.Interaction, typed: str):
        snapshot = await ctx.catalog.get()
        pairs = [(f"{model['name']} ({key})", key) for key, model in snapshot.models.items()]
        pairs.append((f"Default ({snapshot.model_name(snapshot.default_model)})", DEFAULT_WORD))
        return matching_choices(pairs, typed)

    @tree.command(name="model", description="Choose the model for this thread. In the main channel: for new threads.")
    @app_commands.describe(name="The model to use")
    @app_commands.autocomplete(name=model_choices)
    @app_commands.guild_only()
    async def model(interaction: discord.Interaction, name: str):
        where, target = await scope_of(interaction)
        if where is None:
            return await refuse(interaction, wrong_place)
        snapshot = await ctx.catalog.get()
        if name.strip().lower() == DEFAULT_WORD:
            key = None
        else:
            key = snapshot.find_model(name)
            if key is None:
                offered = ", ".join(f"`{k}`" for k in snapshot.models) or "none"
                return await refuse(interaction, f"I don't have a model called {quoted(name)}. Available: {offered}.")
        shown = f"**{snapshot.model_name(key or snapshot.default_model)}**"
        extra = ""
        effort = target.get("effort")
        if effort and effort not in snapshot.efforts_for({"model": key}):
            set_setting(target, "effort", None)
            extra = f" It has no **{effort}** effort level, so effort is back on the model default."
        if key:
            await change(
                interaction, (where, target), "model", key,
                f"This thread now uses {shown}.",
                f"New threads will start on {shown}. Existing threads keep their own setting.",
                extra,
            )
        else:
            await change(
                interaction, (where, target), "model", None,
                f"This thread is back on the default model, {shown}.",
                f"New threads will start on the agent's default model, {shown}.",
                extra,
            )

    async def effort_choices(interaction: discord.Interaction, typed: str):
        _, target = await scope_of(interaction)
        snapshot = await ctx.catalog.get()
        pairs = [(level, level) for level in snapshot.efforts_for(target or {})]
        pairs.append(("default (leave it to the model)", DEFAULT_WORD))
        return matching_choices(pairs, typed)

    @tree.command(name="effort", description="Choose how hard the model thinks in this thread, or in new threads.")
    @app_commands.describe(level="The thinking effort")
    @app_commands.autocomplete(level=effort_choices)
    @app_commands.guild_only()
    async def effort(interaction: discord.Interaction, level: str):
        where, target = await scope_of(interaction)
        if where is None:
            return await refuse(interaction, wrong_place)
        snapshot = await ctx.catalog.get()
        level = level.strip().lower()
        if level == DEFAULT_WORD:
            return await change(
                interaction, (where, target), "effort", None,
                "This thread is back on the model's default thinking effort.",
                "New threads will use the model's default thinking effort.",
            )
        offered = snapshot.efforts_for(target)
        if level not in offered:
            listing = ", ".join(f"`{name}`" for name in offered)
            return await refuse(
                interaction,
                f"The current model offers these effort levels: {listing}, or `{DEFAULT_WORD}`."
                if offered
                else "The current model has no effort levels to choose from.",
            )
        await change(
            interaction, (where, target), "effort", level,
            f"Thinking effort for this thread is now **{level}**.",
            f"New threads will start with thinking effort **{level}**. Existing threads keep their own setting.",
        )

    async def agent_choices(interaction: discord.Interaction, typed: str):
        snapshot = await ctx.catalog.get()
        pairs = [(f"{name}: {text}" if text else name, name) for name, text in snapshot.agents.items()]
        pairs.append((f"default ({snapshot.default_agent})", DEFAULT_WORD))
        return matching_choices(pairs, typed)

    @tree.command(name="agent", description="Choose the OpenCode agent for this thread, such as build or plan.")
    @app_commands.describe(name="The agent to use")
    @app_commands.autocomplete(name=agent_choices)
    @app_commands.guild_only()
    async def agent(interaction: discord.Interaction, name: str):
        where, target = await scope_of(interaction)
        if where is None:
            return await refuse(interaction, wrong_place)
        snapshot = await ctx.catalog.get()
        name = name.strip().lower()
        if name == DEFAULT_WORD:
            return await change(
                interaction, (where, target), "agent", None,
                f"This thread is back on the default agent, **{snapshot.default_agent}**.",
                f"New threads will start with the default agent, **{snapshot.default_agent}**.",
            )
        if name not in snapshot.agents:
            listing = ", ".join(f"`{known}`" for known in snapshot.agents) or "none"
            return await refuse(interaction, f"I don't have an agent called {quoted(name)}. Available: {listing}.")
        about = f" {snapshot.agents[name]}" if snapshot.agents[name] else ""
        await change(
            interaction, (where, target), "agent", name,
            f"This thread now uses the **{name}** agent.{about}",
            f"New threads will start with the **{name}** agent. Existing threads keep their own setting.",
        )

    # ------------------------------------------------------------ lookups

    @tree.command(name="status", description="Show this thread's model, effort, agent and how full its context is.")
    @app_commands.guild_only()
    async def status(interaction: discord.Interaction):
        where, target = await scope_of(interaction)
        if where is None:
            return await refuse(interaction, wrong_place)
        # Reads from the agent, which can take longer than Discord waits for a first answer.
        await interaction.response.defer(ephemeral=True)
        snapshot = await ctx.catalog.get()
        lines = describe_settings(snapshot, settings_of(target))
        if where == "channel":
            working = sum(1 for thread_id in ctx.store.threads if ctx.queue.load(int(thread_id)))
            lines.insert(0, "New threads start with:")
            lines.append(f"**Threads:** {len(ctx.store.threads)} known, {working} working right now")
        else:
            load = ctx.queue.load(interaction.channel_id)
            waiting = f", {load - 1} more waiting" if load > 1 else ""
            lines.append(f"**State:** {'working' + waiting if load else 'idle'}")
            session_id = target.get("sessionId")
            if session_id:
                usage = None
                with contextlib.suppress(Exception):
                    messages = await ctx.agent.recent_messages(session_id, target["directory"], STATUS_MESSAGES)
                    usage = context_usage(messages, snapshot)
                if usage:
                    used, limit = usage
                    of = f" of {format_tokens(limit)} ({round(100 * used / limit)}%)" if limit else ""
                    lines.append(f"**Context:** {format_tokens(used)} tokens{of}")
                lines.append(f"**Session:** `{session_id}`")
            else:
                lines.append("**Session:** none yet. One starts with the next message.")
        await interaction.followup.send("\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS)

    @tree.command(name="help", description="List what I can do and the commands I understand.")
    @app_commands.guild_only()
    async def help_(interaction: discord.Interaction):
        home = await ctx.home_of(channel_of(interaction))
        where = f"<#{home.id}>" if home is not None else "an enabled channel"
        await refuse(
            interaction,
            f"**Start:** mention me in {where} with a question or a task. I open a thread, "
            "and every later message in it continues the same session. Mention me with nothing else to open "
            "an empty thread you can set up before you ask. Text files you attach are sent "
            "to the agent as their content.\n\n"
            "**Settings**\n"
            "`/model` choose the model\n"
            "`/effort` choose how hard the model thinks\n"
            "`/agent` switch between agents such as `build` and `plan`\n"
            "**Looking**\n"
            "`/status` the thread's settings and how full its context is\n"
            "**The session**\n"
            "`/stop` stop the current request and drop waiting ones (`!stop` does the same)\n"
            "`/undo` take the last request out of the conversation (files stay as they are), `/redo` bring it back\n"
            "`/clear` clear the context and start over in the same thread\n"
            "`/compact` replace the conversation so far with a summary to free up context\n"
            "`/fork` open a new thread that carries on from here\n"
            "`/rename` rename the thread and its session\n"
            "**Running**\n"
            "`/shell` run a shell command in the sandbox. The agent sees the output too.\n"
            "`/command` run one of the agent's own commands, such as `review`\n\n"
            "**In the main channel** `/model`, `/effort` and `/agent` set what new threads start with, "
            "and `/status` shows those defaults. The rest work inside a thread.",
        )

    # ------------------------------------------------------------ controls

    @tree.command(name="stop", description="Stop the current request in this thread and drop the ones waiting.")
    @app_commands.guild_only()
    async def stop(interaction: discord.Interaction):
        # Taken first, so a message sent after this command is not dropped
        # while the checks below wait on Discord.
        stop_point = ctx.queue.stop_point()
        # Any of the bridge's threads, even after its channel was disabled, so
        # work already running can still be stopped.
        entry = ctx.store.threads.get(str(interaction.channel_id)) or await thread_entry(interaction)
        if entry is None:
            return
        if not ctx.queue.load(interaction.channel_id):
            return await refuse(interaction, "Nothing is running in this thread.")
        await interaction.response.defer()
        note(interaction, "stop")
        await ctx.stop_thread(interaction.channel_id, entry, stop_point)
        await interaction.followup.send("Stopped the current request.", allowed_mentions=NO_MENTIONS)

    @tree.command(name="clear", description="Clear this thread's context and start over. Settings stay.")
    @app_commands.guild_only()
    async def clear(interaction: discord.Interaction):
        stop_point = ctx.queue.stop_point()
        entry = await thread_entry(interaction)
        if entry is None:
            return
        await interaction.response.defer()
        note(interaction, "clear")
        await ctx.stop_thread(interaction.channel_id, entry, stop_point)
        old, directory = entry.get("sessionId"), entry["directory"]
        # Cleared before anything waits, so the next message creates a new session.
        entry["sessionId"] = None
        ctx.store.save()
        if old:

            async def delete_old():
                try:
                    await ctx.agent.delete_session(old, directory)
                    log.info("thread %s: deleted session %s", interaction.channel_id, old)
                except Exception as err:
                    log.warning("thread %s: could not delete session %s: %s", interaction.channel_id, old, err)

            # Queued, so a stopped request finishes writing up its outcome
            # before its session goes. Not skipped for a disabled channel, and
            # not dropped by a later stop.
            ctx.queue.submit(interaction.channel_id, delete_old, droppable=False)
        await interaction.followup.send(
            "Cleared this thread's context. Its settings are kept, and the next message starts from scratch.",
            allowed_mentions=NO_MENTIONS,
        )

    @tree.command(name="compact", description="Replace the conversation so far with a summary to free up context.")
    @app_commands.guild_only()
    async def compact(interaction: discord.Interaction):
        entry = await thread_entry(interaction)
        if entry is None:
            return
        if not entry.get("sessionId"):
            return await refuse(interaction, "There is nothing to compact yet.")
        no_model = "I can't tell which model to summarise with. Pick one with `/model` first."

        async def model_now():
            return (await ctx.catalog.get()).resolve(settings_of(entry))[0].get("model")

        if await model_now() is None:
            return await refuse(interaction, no_model)
        channel = channel_of(interaction)

        async def job():
            session_id = entry.get("sessionId")  # read here: /clear may have run while this waited
            if not session_id:
                return await ctx.send(channel, "There is nothing to compact: this thread has a fresh session.")
            # Also read here: /model may have changed while this waited.
            model = await model_now()
            if model is None:
                return await ctx.send(channel, no_model)
            typing = asyncio.create_task(ctx.keep_typing(channel))
            try:
                await ctx.agent.summarize(session_id, entry["directory"], model, ctx.compact_timeout)
                await ctx.send(channel, "Compacted. Earlier messages are now a summary.")
            except Exception as err:
                await ctx.report_failure(channel, err)
            finally:
                typing.cancel()

        note(interaction, "compact")
        waiting = queued(interaction)
        # Queued like a message, so it never runs in the middle of a request.
        enqueue(interaction, job)
        await say(interaction, f"Compacting this thread's conversation{waiting}.")

    # ------------------------------------------------------------ undo and redo

    async def undo_target(entry):
        """The request /undo would take back, or None."""
        session, messages = await session_and_messages(entry)
        requests = [m for m in live_messages(messages, session)[0] if is_request(m)]
        return requests[-1] if requests else None

    @tree.command(name="undo", description="Take the last request out of this thread's conversation. Files stay as they are.")
    @app_commands.guild_only()
    async def undo(interaction: discord.Interaction):
        entry = await thread_entry(interaction)
        if entry is None:
            return
        if not entry.get("sessionId"):
            return await refuse(interaction, "There is nothing to undo yet.")
        if ctx.queue.load(interaction.channel_id):
            return await refuse(interaction, busy)
        await interaction.response.defer(ephemeral=True)
        target = await undo_target(entry)
        if target is None:
            return await interaction.followup.send("There is nothing left to undo in this session.", ephemeral=True)
        # Asked first: the agent forgets the request for everyone in the thread.
        await interaction.followup.send(
            undo_question(target),
            view=confirm_view(target["info"]["id"]),
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    async def undo_clicked(interaction, message_id):
        where, entry = await scope_of(interaction)
        if where != "thread" or not entry.get("sessionId"):
            return await interaction.response.edit_message(content="This thread has nothing to undo any more.", view=None)
        if ctx.queue.load(interaction.channel_id):
            return await interaction.response.edit_message(content=busy, view=None)
        await interaction.response.edit_message(content="Undoing…", view=None)
        note(interaction, "undo")
        channel, who = channel_of(interaction), name_of(interaction.user)

        async def job():
            try:
                target = await undo_target(entry) if entry.get("sessionId") else None
                if target is None or target["info"]["id"] != message_id:
                    return await ctx.send(
                        channel, "Nothing was undone: this thread changed after the undo was asked for. Use `/undo` again."
                    )
                await ctx.agent.revert(entry["sessionId"], entry["directory"], message_id)
                await ctx.send(
                    channel,
                    f"↩️ **{who}** undid the last request, “{snippet(asked(target)[1])}”. {FILES_STAY} "
                    "`/redo` brings it back until the next message is sent.",
                )
            except Exception as err:
                await ctx.report_failure(channel, err)

        enqueue(interaction, job)

    @tree.command(name="redo", description="Bring back what /undo took out of the conversation.")
    @app_commands.guild_only()
    async def redo(interaction: discord.Interaction):
        entry = await thread_entry(interaction)
        if entry is None:
            return
        if not entry.get("sessionId"):
            return await refuse(interaction, nothing_to_redo)
        if ctx.queue.load(interaction.channel_id):
            return await refuse(interaction, busy)
        await interaction.response.defer()
        note(interaction, "redo")
        channel, who = channel_of(interaction), name_of(interaction.user)
        answer = later(interaction, channel)

        async def job():
            try:
                session, messages = await session_and_messages(entry)
                if not (session or {}).get("revert"):
                    return await answer(nothing_to_redo)
                live, _ = live_messages(messages, session)
                back = sum(1 for m in messages[len(live) :] if is_request(m))
                await ctx.agent.unrevert(entry["sessionId"], entry["directory"])
                await answer(f"↪️ **{who}** brought back {plural(back, 'request')} taken back with `/undo`.")
            except Exception as err:
                log.error("thread %s: /redo failed: %s", interaction.channel_id, err)
                await answer(f"Something went wrong on my side: {str(err)[:300]}")

        # Queued so it never runs in the middle of a request.
        enqueue(interaction, job)

    # ------------------------------------------------------------ fork and rename

    @tree.command(name="fork", description="Open a new thread that carries on from here with a copy of the conversation.")
    @app_commands.guild_only()
    async def fork(interaction: discord.Interaction):
        entry = await thread_entry(interaction)
        if entry is None:
            return
        if not entry.get("sessionId"):
            return await refuse(interaction, "There is nothing to fork yet. Send a message first.")
        channel, who, user_id = channel_of(interaction), name_of(interaction.user), interaction.user.id
        source_id = interaction.channel_id
        name = fork_name(getattr(channel, "name", None))
        home = await ctx.home_of(channel)  # the copy opens next to this thread

        async def job():
            session_id = entry.get("sessionId")
            if not session_id:
                return await ctx.send(channel, "There is nothing to fork: this thread has a fresh session.")
            try:
                session = await ctx.agent.session(session_id, entry["directory"])
                # Requests taken back with /undo stay behind.
                before = ((session or {}).get("revert") or {}).get("messageID")
                forked = await ctx.agent.fork(session_id, entry["directory"], before)
                try:
                    thread = await ctx.open_thread(home, name, f"🍴 **{who}** forked <#{source_id}> into a new thread.")
                except Exception:
                    # Without its thread nobody can reach the copy.
                    with contextlib.suppress(Exception):
                        await ctx.agent.delete_session(forked["id"], entry["directory"])
                    raise
                copy = ctx.store.register_thread(thread.id, user_id)
                for setting in SETTINGS:
                    set_setting(copy, setting, entry.get(setting))
                copy["sessionId"] = forked["id"]
                if entry.get("placeholderName"):
                    copy["placeholderName"] = True  # still unnamed: its first request names it
                ctx.store.save()
                log.info("thread %s: forked into thread %s, session %s", source_id, thread.id, forked["id"])
                with contextlib.suppress(Exception):
                    await ctx.agent.rename(forked["id"], entry["directory"], session_title(name))
                await ctx.send(
                    thread,
                    f"This thread carries on from <#{source_id}> with a copy of its conversation and settings. "
                    "From here the two go their own ways, but both work in the same sandbox.",
                )
                await ctx.send(channel, f"🍴 Forked into <#{thread.id}>.")
            except Exception as err:
                await ctx.report_failure(channel, err)

        note(interaction, "fork")
        waiting = queued(interaction)
        # Queued so the copy never catches a request half done.
        enqueue(interaction, job)
        await say(interaction, f"Forking this thread{waiting}.")

    @tree.command(name="rename", description="Rename this thread and its OpenCode session.")
    @app_commands.describe(name="The new name")
    @app_commands.guild_only()
    async def rename(interaction: discord.Interaction, name: str):
        entry = await thread_entry(interaction)
        if entry is None:
            return
        name = " ".join(name.split())[:100]
        if not name:
            return await refuse(interaction, "The new name can't be empty.")
        await interaction.response.defer()
        note(interaction, "rename")
        try:
            await rename_thread(channel_of(interaction), interaction.client, name)
        except TimeoutError:
            return await interaction.followup.send(
                "Discord did not rename the thread within 10 seconds, so I gave up. It allows only two renames "
                "of a thread every ten minutes, which is the usual reason. Try again in a few minutes.",
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException as err:
            return await interaction.followup.send(f"Discord did not let me rename this thread: {err}", allowed_mentions=NO_MENTIONS)
        if entry.pop("placeholderName", None):
            ctx.store.save()  # a name someone chose is kept when the first request comes
        if entry.get("sessionId"):
            try:
                await ctx.agent.rename(entry["sessionId"], entry["directory"], session_title(name))
            except Exception as err:
                log.warning("thread %s: could not rename session: %s", interaction.channel_id, err)
        await interaction.followup.send(f"Renamed this thread to **{quoted(name)}**.", allowed_mentions=NO_MENTIONS)

    # ------------------------------------------------------------ running things

    @tree.command(name="shell", description="Run a shell command in the sandbox. The agent sees the output too.")
    @app_commands.describe(command="The command, as you would type it in a terminal")
    @app_commands.guild_only()
    async def shell(interaction: discord.Interaction, command: str):
        entry = await thread_entry(interaction)
        if entry is None:
            return
        command = command.strip()
        if not command:
            return await refuse(interaction, "Tell me which command to run.")
        channel = channel_of(interaction)

        async def job():
            typing = asyncio.create_task(ctx.keep_typing(channel))
            session_id = None
            try:
                session_id = await ctx.ensure_session(channel, entry)
                snapshot = await ctx.catalog.get()
                options, _ = snapshot.resolve(settings_of(entry))
                message = await ctx.agent.shell(
                    session_id,
                    entry["directory"],
                    options.get("agent") or snapshot.default_agent,
                    command,
                    ctx.shell_timeout,
                    options.get("model"),
                )
                output, error = shell_result(message)
                if error:
                    return await ctx.send(channel, f"The command could not run: {plain(error, 500)}")
                text, full = shell_message(output)
                await ctx.send(channel, text, file_of(full.encode(), "output.txt") if full else None)
            except TimeoutError:
                if session_id:
                    with contextlib.suppress(Exception):
                        await ctx.agent.abort(session_id, entry["directory"])
                took = format_duration(ctx.shell_timeout)
                await ctx.send(channel, f"The command was still running after {took}, so I stopped it.")
            except Exception as err:
                await ctx.report_failure(channel, err)
            finally:
                typing.cancel()

        note(interaction, "shell")
        waiting = queued(interaction)
        enqueue(interaction, job)
        await say(interaction, f"🖥️ **{name_of(interaction.user)}** runs `{plain(command, 300)}`{waiting}.")

    async def command_choices(interaction: discord.Interaction, typed: str):
        snapshot = await ctx.catalog.get()
        pairs = [(f"{name}: {text}" if text else name, name) for name, text in snapshot.commands.items()]
        return matching_choices(pairs, typed)

    @tree.command(name="command", description="Run one of the agent's own commands or skills, such as review.")
    @app_commands.describe(name="The command to run", arguments="What to pass to it, if anything")
    @app_commands.autocomplete(name=command_choices)
    @app_commands.guild_only()
    async def command_(interaction: discord.Interaction, name: str, arguments: str = ""):
        entry = await thread_entry(interaction)
        if entry is None:
            return
        snapshot = await ctx.catalog.get()
        name = name.strip().removeprefix("/")
        if name not in snapshot.commands:
            listing = ", ".join(f"`{known}`" for known in snapshot.commands) or "none"
            return await refuse(interaction, f"The agent has no command called {quoted(name)}. Available: {listing}.")
        arguments = arguments.strip()
        channel = channel_of(interaction)

        async def job():
            try:
                await ctx.run_command(channel, entry, name, arguments)
            except Exception as err:
                await ctx.report_failure(channel, err)

        note(interaction, f"command {name}")
        waiting = queued(interaction)
        enqueue(interaction, job)
        shown = f"/{name} {plain(arguments, 300)}".rstrip()
        await say(interaction, f"▶️ **{name_of(interaction.user)}** runs `{shown}`{waiting}.")

    # ------------------------------------------------------------ buttons

    async def on_component(interaction):
        """A button under one of these commands' messages was clicked. Returns False if it is not one."""
        custom_id = (interaction.data or {}).get("custom_id") or ""
        if not custom_id.startswith(BUTTON_PREFIX):
            return False
        action, _, value = custom_id.removeprefix(BUTTON_PREFIX).partition(":")
        if action == "undo" and value:
            await undo_clicked(interaction, value)
        elif action == "cancel":
            await interaction.response.edit_message(content="Cancelled. Nothing was undone.", view=None)
        else:
            await interaction.response.edit_message(content="This button no longer works.", view=None)
        return True

    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        name = interaction.command.name if interaction.command else "?"
        log.error("command /%s failed: %s", name, error, exc_info=error)
        text = "Something went wrong on my side."
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)

    return on_component
