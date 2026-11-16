"""discord-bridge: connects one Discord channel to the OpenCode server.

- @mention the bot in DISCORD_CHANNEL_ID -> it opens a thread with its own
  OpenCode session and answers there. Every session works in the shared clone
  (BASE_DIR); the agent can make branches or worktrees itself if asked.
- Any later message in that thread continues the same session. Only threads
  under DISCORD_CHANNEL_ID count, so threads left in a previously configured
  channel are ignored.
- Threads run in parallel, up to MAX_CONCURRENT at once.
- "!stop" in a thread aborts the running request and drops that thread's
  queued ones. A request that runs longer than PROMPT_TIMEOUT_SECONDS (30 min)
  is aborted too, freeing its slot.
- Slash commands (commands.py) choose a thread's model, thinking effort and
  agent, and control its session. The bridge keeps those settings (state.py)
  and sends them with every prompt.

This container holds the Discord bot token. It never runs user commands; it
only forwards text to the agent's HTTP API and posts the replies back.
"""

import asyncio
import contextlib
import itertools
import logging
import os
import re
import signal
import sys
import time
from collections import Counter

import discord
from discord import app_commands

import commands
from catalog import Catalog
from opencode import OpenCode, OpenCodeError, PromptTimedOut
from state import Store, set_setting, settings_of

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s", stream=sys.stdout)
logging.getLogger("discord").setLevel(logging.WARNING)
log = logging.getLogger("bridge")


def env(name, default=None):
    value = os.environ.get(name, default)
    if not value or value.startswith("replace-me"):
        log.error("%s is not set", name)
        sys.exit(1)
    return value


TOKEN = env("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(env("DISCORD_CHANNEL_ID"))
OPENCODE_URL = env("OPENCODE_URL", "http://opencode-agent:4096")
OPENCODE_PASSWORD = env("OPENCODE_SERVER_PASSWORD")
STATE_FILE = os.environ.get("STATE_FILE", "/data/threads.json")
BASE_DIR = os.environ.get("BASE_DIR", "/work/repo")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "16"))
PROMPT_TIMEOUT = float(os.environ.get("PROMPT_TIMEOUT_SECONDS", "1800"))
# Longest wait for /compact, which is one model call over the whole conversation.
COMPACT_TIMEOUT = 300
DISCORD_LIMIT = 1900

NO_MENTIONS = discord.AllowedMentions.none()
agent = OpenCode(OPENCODE_URL, OPENCODE_PASSWORD)
store = Store(STATE_FILE, BASE_DIR)  # thread -> session map and settings
catalog = Catalog(agent, BASE_DIR)  # models, effort levels and agents the agent offers


# ---------------------------------------------------------------- queueing


class ThreadQueue:
    """Runs one request at a time per thread, and at most `limit` at once overall."""

    def __init__(self, limit):
        self._slots = asyncio.Semaphore(limit)
        self._locks = {}
        self._pending = Counter()
        self._dropped_before = {}  # thread id -> jobs submitted before this number are skipped
        self._seq = itertools.count()
        self._tasks = set()  # asyncio only keeps weak references to tasks

    def submit(self, thread_id, job):
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        self._pending[thread_id] += 1
        task = asyncio.create_task(self._run(thread_id, lock, job, next(self._seq)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def load(self, thread_id):
        """How many of this thread's jobs are running or waiting."""
        return self._pending[thread_id]

    def stop_point(self):
        """A marker for drop_waiting: jobs submitted before it can be dropped."""
        return next(self._seq)

    def drop_waiting(self, thread_id, stop_point):
        """Skips this thread's jobs that were submitted before stop_point and haven't started."""
        if self._pending[thread_id]:
            self._dropped_before[thread_id] = max(stop_point, self._dropped_before.get(thread_id, -1))

    async def _run(self, thread_id, lock, job, seq):
        try:
            async with lock:
                if seq < self._dropped_before.get(thread_id, -1):
                    return
                async with self._slots:
                    await job()
        except Exception:
            log.exception("thread %s", thread_id)
        finally:
            self._pending[thread_id] -= 1
            if not self._pending[thread_id]:
                del self._pending[thread_id]
                self._locks.pop(thread_id, None)
                self._dropped_before.pop(thread_id, None)


queue = ThreadQueue(MAX_CONCURRENT)

# ---------------------------------------------------------------- Discord helpers


def split_for_discord(text):
    """Splits a reply into messages under Discord's limit, keeping code fences intact."""
    chunks = []
    current = ""
    in_fence = False

    def push():
        nonlocal current
        if not current.strip():
            return
        chunks.append(current + "\n```" if in_fence else current)
        current = "```\n" if in_fence else ""

    for line in text.split("\n"):
        # Checked before a long line is cut, so its tail can't hide the fence.
        fence = line.lstrip().startswith("```")
        while len(line) > DISCORD_LIMIT:
            push()
            current += line[:DISCORD_LIMIT]
            line = line[DISCORD_LIMIT:]
            if fence:
                in_fence = not in_fence
                fence = False
            push()
        if len(current) + len(line) + 1 > DISCORD_LIMIT:
            push()
        current += line if not current or current.endswith("\n") else "\n" + line
        if fence:
            in_fence = not in_fence
    if current.strip():
        chunks.append(current)
    return chunks


async def send(channel, content):
    await channel.send(content, allowed_mentions=NO_MENTIONS)


async def send_long(channel, text):
    for chunk in split_for_discord(text):
        await send(channel, chunk)


def bot_role(message):
    """The bot's own managed role, which Discord's autocomplete often offers instead of the bot."""
    return message.guild.self_role if message.guild else None


def mentions_bot(message, bot_id):
    role = bot_role(message)
    return any(user.id == bot_id for user in message.mentions) or (role is not None and role in message.role_mentions)


def clean_content(message, bot_id):
    role = bot_role(message)
    text = re.sub(rf"<@!?{bot_id}>", "", message.content)
    if role is not None:
        text = text.replace(f"<@&{role.id}>", "")
    text = text.strip()
    files = [f"{a.filename}: {a.url}" for a in message.attachments]
    if files:
        text += "\n\n(Attached files, fetch them if needed:\n" + "\n".join(files) + ")"
    return text


def thread_name(text, author):
    first_line = " ".join(text.split("\n")[0].split())
    return (first_line or f"Request from {author}")[:90]


def session_title(thread):
    # A thread that wasn't in discord.py's cache arrives as a PartialMessageable,
    # which has no name.
    return f"Discord: {getattr(thread, 'name', None) or thread.id}"


def parts_text(parts):
    texts = [
        p["text"]
        for p in parts or []
        if p.get("type") == "text" and not p.get("synthetic") and not p.get("ignored") and p.get("text")
    ]
    return "\n\n".join(texts).strip()


async def keep_typing(channel):
    while True:
        with contextlib.suppress(Exception):
            await channel.typing()  # one typing indicator, shown for about 10 seconds
        await asyncio.sleep(8)


# ---------------------------------------------------------------- handlers


def register_thread(thread, author):
    # Registered right away, before the request waits for a slot, so messages
    # posted in the thread meanwhile are queued behind it instead of ignored.
    # run_prompt creates the session when the first request runs.
    entry = store.register_thread(thread.id, author.id)
    log.info("thread %s opened by %s", thread.id, author.name)
    return entry


async def thread_parent_id(thread):
    # A thread missing from discord.py's cache arrives as a PartialMessageable
    # with no parent, so ask Discord. None (unknown) counts as not ours.
    parent_id = getattr(thread, "parent_id", None)
    if parent_id is None:
        with contextlib.suppress(discord.HTTPException):
            parent_id = getattr(await client.fetch_channel(thread.id), "parent_id", None)
    return parent_id


async def stop_thread(thread_id, entry, stop_point):
    """Drops the thread's jobs submitted before stop_point and aborts the one running."""
    queue.drop_waiting(thread_id, stop_point)
    if entry.get("sessionId"):
        with contextlib.suppress(Exception):
            await agent.abort(entry["sessionId"], entry["directory"])


async def prompt_settings(thread, entry):
    """The model, variant and agent fields for this thread's next prompt.

    A setting the agent no longer offers (opencode.json changed since it was
    chosen) is removed from the thread, and the thread is told.
    """
    try:
        snapshot = await catalog.get()
    except Exception as err:
        log.error("thread %s: could not read the model list, sending without settings: %s", thread.id, err)
        return {}
    options, dropped = snapshot.resolve(settings_of(entry))
    if dropped:
        for name in dropped:
            set_setting(entry, name, None)
        store.save()
        await send(
            thread,
            f"The {' and '.join(dropped)} chosen for this thread {'is' if len(dropped) == 1 else 'are'} "
            "no longer offered by the agent, so I went back to the default.",
        )
    return options


async def run_prompt(thread, entry, message, text):
    started = time.monotonic()
    log.info("thread %s: prompt from %s (%d chars)", thread.id, message.author.name, len(text))
    typing = asyncio.create_task(keep_typing(thread))
    try:
        if not entry.get("sessionId"):
            entry["sessionId"] = await agent.create_session(entry["directory"], session_title(thread))
            log.info("thread %s: session %s", thread.id, entry["sessionId"])
            moved = entry.pop("movedToSharedClone", False)
            store.save()
            if moved:
                await send(
                    thread,
                    "This thread now works in the shared checkout, so I started a fresh session. "
                    "Earlier context from this thread is not carried over.",
                )

        options = await prompt_settings(thread, entry)
        model = options.get("model")
        log.info(
            "thread %s: model %s, effort %s, agent %s",
            thread.id,
            f"{model['providerID']}/{model['modelID']}" if model else "default",
            options.get("variant", "default"),
            options.get("agent", "default"),
        )
        body = {
            "parts": [{"type": "text", "text": f"Discord user {message.author.display_name} says:\n\n{text}"}],
            # The agent's question tool waits for an answer, and the bridge has
            # no way to give one yet, so a question would hold the request
            # until it times out. Off until the bridge can show questions.
            "tools": {"question": False},
            **options,
        }
        try:
            try:
                result = await agent.prompt(entry["sessionId"], entry["directory"], body, PROMPT_TIMEOUT)
            except OpenCodeError as err:
                if err.status != 404:
                    raise
                # Session disappeared (e.g. agent data volume was reset): start a new one.
                entry["sessionId"] = await agent.create_session(entry["directory"], session_title(thread))
                store.save()
                await send(thread, "My earlier context for this thread was lost, so I started a fresh session.")
                result = await agent.prompt(entry["sessionId"], entry["directory"], body, PROMPT_TIMEOUT)
        except PromptTimedOut as err:
            minutes = round(PROMPT_TIMEOUT / 60)
            log.info(
                "thread %s: timed out after %d min, %s session %s",
                thread.id, minutes, "stopped" if err.aborted else "could not stop", entry["sessionId"],
            )
            await send(
                thread,
                f"I stopped this request because it ran for more than {minutes} minutes. "
                "Anything it changed so far is still in the checkout. Send another message to carry on."
                if err.aborted
                else f"This request ran for more than {minutes} minutes. "
                "I tried to stop it but the agent didn't respond, so it may still be running.",
            )
            return

        result = result if isinstance(result, dict) else {}
        reply = parts_text(result.get("parts"))
        error = (result.get("info") or {}).get("error")
        detail = ((error.get("data") or {}).get("message") or error.get("name") or "unknown error") if error else None
        seconds = round(time.monotonic() - started)
        log.info(
            "thread %s: %s after %ds",
            thread.id, f"agent error ({detail})" if error else f"replied, {len(reply)} chars", seconds,
        )
        if reply:
            await send_long(thread, reply)
        if error and error.get("name") == "MessageAbortedError":
            pass  # someone stopped it, and "!stop" or the command has already answered them
        elif error:
            await send(thread, f"The agent stopped with an error: {str(detail)[:500]}")
        elif not reply:
            await send(thread, "Done. (The agent finished without a text reply.)")
    finally:
        typing.cancel()


async def report_failure(channel, err):
    log.error("%s", err, exc_info=err if isinstance(err, BaseException) else None)
    with contextlib.suppress(Exception):
        await send(channel, f"Something went wrong on my side: {str(err)[:300]}")


# The bot never joins voice, so skip discord.py's warnings about voice libraries.
discord.VoiceClient.warn_nacl = False
discord.VoiceClient.warn_dave = False

intents = discord.Intents.none()
intents.guilds = True
intents.guild_messages = True
intents.message_content = True
client = discord.Client(intents=intents, allowed_mentions=NO_MENTIONS)

# Slash commands arrive over the same gateway connection as messages.
tree = app_commands.CommandTree(client)
commands.setup(
    tree,
    commands.Context(
        store=store,
        agent=agent,
        catalog=catalog,
        queue=queue,
        channel_id=CHANNEL_ID,
        compact_timeout=COMPACT_TIMEOUT,
        thread_parent_id=thread_parent_id,
        stop_thread=stop_thread,
        send=send,
        report_failure=report_failure,
        keep_typing=keep_typing,
    ),
)


# Messages people write. System messages (thread renames, pins, joins) are
# ignored, since some carry text that would otherwise be sent as a prompt.
USER_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)


@client.event
async def on_message(message):
    if message.author.bot or client.user is None or message.type not in USER_MESSAGE_TYPES:
        return
    bot_id = client.user.id
    channel = message.channel

    # Follow-ups inside a thread this bridge owns. Looked up by ID rather than
    # channel type, so a thread missing from discord.py's cache still works.
    # Only threads under the watched channel count, so threads left over from a
    # previously configured channel stop forwarding messages.
    # The parent lookup can wait on Discord, so nothing is queued after it:
    # follow-ups check it inside their queued job, and "!stop" takes its stop
    # point first. Either way the thread's messages keep their order.
    entry = store.threads.get(str(channel.id))
    if entry is not None:
        text = clean_content(message, bot_id)
        if not text:
            return
        if text == "!stop":
            stop_point = queue.stop_point()
            if await thread_parent_id(channel) != CHANNEL_ID:
                return
            await stop_thread(channel.id, entry, stop_point)
            with contextlib.suppress(discord.HTTPException):
                await message.add_reaction("🛑")
            return

        async def follow_up():
            if await thread_parent_id(channel) != CHANNEL_ID:
                return
            try:
                await run_prompt(channel, entry, message, text)
            except Exception as err:
                await report_failure(channel, err)

        queue.submit(channel.id, follow_up)
        return

    # New requests: @mention in the one allowed channel.
    if channel.id != CHANNEL_ID or not mentions_bot(message, bot_id):
        return
    text = clean_content(message, bot_id)
    if not text:
        await message.reply(
            "Mention me with a question or a task and I will start a thread for it.", allowed_mentions=NO_MENTIONS
        )
        return

    try:
        thread = await message.create_thread(name=thread_name(text, message.author.name), auto_archive_duration=1440)
    except discord.HTTPException as err:
        await report_failure(channel, f'could not start a thread (does the bot have "Create Public Threads"?): {err}')
        return

    entry = register_thread(thread, message.author)

    async def new_request():
        try:
            await run_prompt(thread, entry, message, text)
        except Exception as err:
            await report_failure(thread, err)

    queue.submit(thread.id, new_request)


_ready_once = False


@client.event
async def on_ready():
    global _ready_once
    if _ready_once:  # on_ready fires again after every reconnect
        return
    _ready_once = True
    log.info("logged in as %s", client.user)
    try:
        channel = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
        perms = channel.permissions_for(channel.guild.me)
        needed = [
            "view_channel",
            "send_messages",
            "read_message_history",
            "create_public_threads",
            "send_messages_in_threads",
            "add_reactions",
            "attach_files",
        ]
        missing = [name for name in needed if not getattr(perms, name)]
        log.info("watching #%s%s", channel.name, f"; MISSING permissions: {', '.join(missing)}" if missing else "")
    except Exception as err:
        log.error("cannot access channel %s: %s", CHANNEL_ID, err)
        return
    # Registered to this one server, where they show up at once. Commands
    # registered globally can take an hour to appear.
    try:
        tree.copy_global_to(guild=channel.guild)
        synced = await tree.sync(guild=channel.guild)
        log.info("slash commands ready: %s", " ".join(f"/{command.name}" for command in synced))
    except discord.Forbidden as err:
        log.error(
            "could not register slash commands: %s. Re-invite the bot with the applications.commands scope "
            "(see the README).",
            err,
        )
    except discord.HTTPException as err:
        log.error("could not register slash commands: %s", err)


async def main():
    store.load()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with agent, client:
        await agent.wait_until_ready()
        log.info("opencode-agent is reachable")
        try:
            snapshot = await catalog.get()
            log.info(
                "agent offers models %s (default %s) and agents %s",
                ", ".join(snapshot.models) or "none",
                snapshot.default_model,
                ", ".join(snapshot.agents) or "none",
            )
        except Exception as err:  # retried on first use
            log.error("could not read the agent's model list yet: %s", err)
        bot = asyncio.create_task(client.start(TOKEN))
        stopping = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({bot, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if bot in done:
            bot.result()  # surfaces a login failure
        else:
            log.info("shutting down")
            bot.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bot


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        log.exception("fatal")
        sys.exit(1)
