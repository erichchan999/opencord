"""Files the agent sends to its thread with the discord_attach tool.

The tool (opencode-agent/tool/discord_attach.ts) only checks a file and marks
it. When a request ends, the bridge gathers the files marked during it and
posts them after the reply, fetched through the agent's API: this container
has no access to the agent's files.

The paths come from the turn's events, and also from the request's messages,
since a request picked up after a restart has no events from before it.
"""

import io
import logging
import posixpath

import discord

from commands import FILE_LIMIT, NO_MENTIONS
from events import attached_path, subagent_session
from opencode import FileTooLarge
from status import MESSAGE_LIMIT, plain

log = logging.getLogger("bridge")

# Discord takes at most this many attachments on one message.
PER_MESSAGE = 10
# How deep subagents started by subagents are searched for files.
SUBAGENT_DEPTH = 3


async def marked_in(steps, agent, directory, depth=SUBAGENT_DEPTH):
    """Paths marked in a request's messages, a subagent's at the point it ran, in call order."""
    paths = []
    for message in steps:
        for part in message.get("parts") or []:
            path = attached_path(part)
            if path:
                paths.append(path)
            child = subagent_session(part)
            if child and depth:
                try:
                    paths += await marked_in(await agent.messages(child, directory), agent, directory, depth - 1)
                except Exception as err:
                    log.warning("could not read subagent session %s for its files: %s", child, err)
    return paths


def last_of_each(paths):
    """The paths without repeats, each where it was marked last."""
    order = {}
    for path in paths:
        order.pop(path, None)
        order[path] = None
    return list(order)


def megabytes(size):
    return f"{size / 1_000_000:.1f} MB"


async def post(channel, agent, directory, paths, count=PER_MESSAGE, size=FILE_LIMIT):
    """Fetches the files and posts them to the thread, at most `count` files
    and `size` bytes to a message.

    A message's files are posted as soon as the next file would not fit, so
    no more than one message's worth and one more file are held in memory. A file that can't be
    fetched or is too large gets a note naming it instead. Nothing here
    raises: the reply has been posted already.
    """
    notes, group = [], []

    async def send_group():
        try:
            await channel.send(
                files=[discord.File(io.BytesIO(data), filename=name) for name, data in group],
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException as err:
            log.warning("could not post %d attached files: %s", len(group), err)
            names = ", ".join(f"`{plain(name, 100)}`" for name, _ in group)
            notes.append(f"{names}: Discord refused them ({plain(str(err), 100)})")
        group.clear()

    for path in last_of_each(paths):
        shown = f"`{plain(path, 100)}`"
        try:
            data = await agent.read_file(directory, path, FILE_LIMIT)
        except FileTooLarge as err:
            notes.append(f"{shown} is {megabytes(err.size)}, over the {megabytes(FILE_LIMIT)} Discord takes")
            continue
        except Exception as err:
            log.warning("could not fetch %s to attach it: %s", path, err)
            notes.append(f"{shown} could not be fetched from the agent")
            continue
        if group and (len(group) == count or sum(len(d) for _, d in group) + len(data) > size):
            await send_group()
        group.append((posixpath.basename(path), data))
    if group:
        await send_group()
    if notes:
        try:
            text = "Not attached: " + "; ".join(notes) + "."
            await channel.send(text[:MESSAGE_LIMIT], allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as err:
            log.warning("could not say which files were not attached: %s", err)
