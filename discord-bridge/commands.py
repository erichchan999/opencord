"""Slash commands: the Discord side of OpenCode's session settings and controls.

The commands are registered to the one server the watched channel is in.
Inside one of the bridge's threads they act on that thread. /model, /effort
and /agent used in the main channel set what new threads start with.

A change is answered publicly, so everyone in the thread sees it. Lookups and
refusals are shown only to the person who ran the command.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Callable

import discord
from discord import app_commands

from catalog import DEFAULT_WORD
from state import set_setting, settings_of

log = logging.getLogger("bridge")

NO_MENTIONS = discord.AllowedMentions.none()
# How many of a session's latest messages /status reads to find the last reply.
STATUS_MESSAGES = 20


@dataclass
class Context:
    """What the commands need from the rest of the bridge."""

    store: Any  # state.Store
    agent: Any  # opencode.OpenCode
    catalog: Any  # catalog.Catalog
    queue: Any  # bridge.ThreadQueue
    channel_id: int
    compact_timeout: float
    thread_parent_id: Callable  # async (channel) -> parent channel id, or None
    stop_thread: Callable  # async (thread_id, entry, stop_point)
    send: Callable  # async (channel, text)
    report_failure: Callable  # async (channel, error)
    keep_typing: Callable  # async (channel), runs until cancelled


def format_tokens(count):
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}".removesuffix(".0") + "k"


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
        key = f"{options['model']['providerID']}/{options['model']['modelID']}"
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


def quoted(text):
    """Something a person typed, made safe to show back to them."""
    return discord.utils.escape_markdown(text.strip()[:80])


def setup(tree, ctx):
    """Adds the commands to `tree`."""
    wrong_place = f"I only work in <#{ctx.channel_id}> and the threads I open there."
    thread_only = "Use this inside one of my threads."

    def channel_of(interaction):
        return interaction.channel or interaction.client.get_partial_messageable(interaction.channel_id)

    async def scope_of(interaction):
        """("thread", its entry) in one of the bridge's threads, ("channel", the defaults) in
        the watched channel, (None, None) anywhere else."""
        if interaction.channel_id == ctx.channel_id:
            return "channel", ctx.store.defaults
        entry = ctx.store.threads.get(str(interaction.channel_id))
        if entry is not None and await ctx.thread_parent_id(channel_of(interaction)) == ctx.channel_id:
            return "thread", entry
        return None, None

    async def say(interaction, text, *, private=False):
        await interaction.response.send_message(text, ephemeral=private, allowed_mentions=NO_MENTIONS)

    async def refuse(interaction, text):
        await say(interaction, text, private=True)

    async def thread_entry(interaction):
        """The entry of the thread the command was used in. Refuses and returns None elsewhere."""
        where, entry = await scope_of(interaction)
        if where == "thread":
            return entry
        await refuse(interaction, thread_only if where == "channel" else wrong_place)
        return None

    def note(interaction, what):
        log.info("channel %s: /%s by %s", interaction.channel_id, what, interaction.user.name)

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
        await refuse(
            interaction,
            f"**Start:** mention me in <#{ctx.channel_id}> with a question or a task. I open a thread, "
            "and every later message in it continues the same session.\n\n"
            "**In a thread**\n"
            "`/model` choose the model\n"
            "`/effort` choose how hard the model thinks\n"
            "`/agent` switch between agents such as `build` and `plan`\n"
            "`/status` show the thread's settings and how full its context is\n"
            "`/stop` stop the current request and drop waiting ones (`!stop` does the same)\n"
            "`/new` start a fresh session in the same thread\n"
            "`/compact` replace the conversation so far with a summary to free up context\n\n"
            "**In the main channel** `/model`, `/effort` and `/agent` set what new threads start with, "
            "and `/status` shows those defaults.",
        )

    # ------------------------------------------------------------ controls

    @tree.command(name="stop", description="Stop the current request in this thread and drop the ones waiting.")
    @app_commands.guild_only()
    async def stop(interaction: discord.Interaction):
        # Taken first, so a message sent after this command is not dropped
        # while the checks below wait on Discord.
        stop_point = ctx.queue.stop_point()
        entry = await thread_entry(interaction)
        if entry is None:
            return
        if not ctx.queue.load(interaction.channel_id):
            return await refuse(interaction, "Nothing is running in this thread.")
        await interaction.response.defer()
        note(interaction, "stop")
        await ctx.stop_thread(interaction.channel_id, entry, stop_point)
        await interaction.followup.send("Stopped the current request.", allowed_mentions=NO_MENTIONS)

    @tree.command(name="new", description="Start a fresh session in this thread. Settings stay, context is dropped.")
    @app_commands.guild_only()
    async def new(interaction: discord.Interaction):
        stop_point = ctx.queue.stop_point()
        entry = await thread_entry(interaction)
        if entry is None:
            return
        await interaction.response.defer()
        note(interaction, "new")
        await ctx.stop_thread(interaction.channel_id, entry, stop_point)
        # The next message creates the new session. The old one stays on the agent.
        entry["sessionId"] = None
        ctx.store.save()
        await interaction.followup.send(
            "Started a fresh session for this thread. Its settings are kept, "
            "and earlier messages are no longer part of the context.",
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
        snapshot = await ctx.catalog.get()
        model = snapshot.resolve(settings_of(entry))[0].get("model")
        if model is None:
            return await refuse(interaction, "I can't tell which model to summarise with. Pick one with `/model` first.")
        channel = channel_of(interaction)

        async def job():
            session_id = entry.get("sessionId")  # read here: /new may have run while this waited
            if not session_id:
                return await ctx.send(channel, "There is nothing to compact: this thread has a fresh session.")
            typing = asyncio.create_task(ctx.keep_typing(channel))
            try:
                await ctx.agent.summarize(session_id, entry["directory"], model, ctx.compact_timeout)
                await ctx.send(channel, "Compacted. Earlier messages are now a summary.")
            except Exception as err:
                await ctx.report_failure(channel, err)
            finally:
                typing.cancel()

        waiting = ctx.queue.load(interaction.channel_id)
        note(interaction, "compact")
        # Queued like a message, so it never runs in the middle of a request.
        ctx.queue.submit(interaction.channel_id, job)
        await say(interaction, "Compacting this thread's conversation" + (" after the current request." if waiting else "."))

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
