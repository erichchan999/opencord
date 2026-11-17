"""discord-bridge: connects Discord to the OpenCode server.

- @mention the bot in an enabled channel -> it opens
  a thread with its own OpenCode session and answers there. Every session
  works in the shared clone (BASE_DIR); the agent can make branches or
  worktrees itself if asked.
- Any later message in that thread continues the same session, for as long
  as the thread's parent channel stays enabled.
- Threads run in parallel, up to MAX_CONCURRENT at once.
- "!stop" in a thread aborts the running request and drops that thread's
  queued ones. A request is also stopped when it works longer than
  PROMPT_TIMEOUT_SECONDS, shows no activity for PROMPT_SILENCE_SECONDS, or
  leaves a question unanswered for QUESTION_TIMEOUT_SECONDS.
- Slash commands (commands.py) choose a thread's model, thinking effort and
  agent, and control its session: undo, fork, diff, shell commands, the
  agent's own commands and more. The bridge keeps the settings (state.py) and
  sends them with every prompt.
- Text files attached to a message are sent to the agent as their content.
  Other attachments are passed as links.
- A prompt is handed to the agent and the work is then followed on the agent's
  event stream (events.py). While it runs, one status message in the thread
  shows what the agent is doing (status.py), and the agent's questions appear
  with buttons (questions.py).
- A request that was running when the bridge stopped is picked back up when it
  starts again.

This container holds the Discord bot token. It runs nothing itself: prompts,
/shell and the other session commands go to the agent's HTTP API, and the
bridge posts what comes back.
"""

import asyncio
import base64
import contextlib
import functools
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

import access
import commands
import events
from catalog import Catalog, model_key
from events import Router, parts_text, turn_reply, wait_for_turn
from opencode import OpenCode, OpenCodeError
from questions import Questions
from state import Store, set_setting, settings_of
from status import MESSAGE_LIMIT, StatusMessage, format_duration, render_finished, render_working

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s", stream=sys.stdout)
logging.getLogger("discord").setLevel(logging.WARNING)
log = logging.getLogger("bridge")


def env(name, default=None):
    value = os.environ.get(name, default)
    if not value or value.startswith("replace-me"):
        log.error("%s is not set", name)
        sys.exit(1)
    return value


def number(name, default, kind=float):
    """A number from the environment. Left blank, it is the default."""
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return kind(value)
    except ValueError:
        log.error("%s must be a number, not %r", name, value)
        sys.exit(1)


TOKEN = env("DISCORD_BOT_TOKEN")
OPENCODE_URL = env("OPENCODE_URL", "http://opencode-agent:4096")
OPENCODE_PASSWORD = env("OPENCODE_SERVER_PASSWORD")
STATE_FILE = os.environ.get("STATE_FILE", "/data/threads.json")
BASE_DIR = os.environ.get("BASE_DIR", "/work/repo")
CHANNEL_IDS = set(map(str.strip, os.environ.get("DISCORD_CHANNEL_IDS", "").split(","))) - {""}
MAX_CONCURRENT = number("MAX_CONCURRENT", 16, int)
# Longest a request may work, not counting time spent waiting for a person to
# answer a question.
PROMPT_TIMEOUT = number("PROMPT_TIMEOUT_SECONDS", 1800)
# A request is stopped when the agent shows no activity at all for this long.
SILENCE_TIMEOUT = number("PROMPT_SILENCE_SECONDS", 900)
# A request is stopped when a question from the agent goes unanswered this long.
QUESTION_TIMEOUT = number("QUESTION_TIMEOUT_SECONDS", 3600)
# Longest wait for /compact, which is one model call over the whole conversation.
COMPACT_TIMEOUT = 300
# Longest a /shell command may run.
SHELL_TIMEOUT = number("SHELL_TIMEOUT_SECONDS", 600)
# Text files attached to a message are sent to the agent as their content, up
# to this size each and the second size for all of a message's files together,
# which keeps them well inside the model's context. Files past either limit,
# and files that are not text, are passed as a link instead.
TEXT_ATTACHMENT_LIMIT = 100_000
TEXT_ATTACHMENTS_TOTAL = 200_000
# How many of a session's latest messages are read to find the reply to a request.
REPLY_MESSAGES = 50
DISCORD_LIMIT = MESSAGE_LIMIT

NO_MENTIONS = discord.AllowedMentions.none()
agent = OpenCode(OPENCODE_URL, OPENCODE_PASSWORD)
store = Store(STATE_FILE, BASE_DIR)  # thread -> session map, settings, open questions
catalog = Catalog(agent, BASE_DIR)  # models, effort levels and agents the agent offers
router = Router()  # the requests in progress, fed from the agent's event stream
shutting_down = False  # set when the bridge has been told to stop


# ---------------------------------------------------------------- queueing


class ThreadQueue:
    """Runs one request at a time per thread, and at most `limit` at once overall."""

    def __init__(self, limit):
        self._slots = asyncio.Semaphore(limit)
        self._locks = {}
        self._pending = Counter()
        self._dropped_before = {}  # thread id -> jobs submitted before this number are skipped
        self._running = {}  # thread id -> number of the job that has started
        self._seq = itertools.count()
        self._tasks = set()  # asyncio only keeps weak references to tasks

    def submit(self, thread_id, job, droppable=True):
        """Queues `job` for the thread. A job that is not droppable runs even after a stop."""
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        self._pending[thread_id] += 1
        task = asyncio.create_task(self._run(thread_id, lock, job, next(self._seq), droppable))
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

    def stopped(self, thread_id):
        """True when the thread's started job was submitted before a stop point given to drop_waiting.

        A job that has started is not dropped, so one that is still getting
        ready (waiting for a slot, reading attachments) checks this before it
        sends anything.
        """
        running = self._running.get(thread_id)
        return running is not None and running < self._dropped_before.get(thread_id, -1)

    async def _run(self, thread_id, lock, job, seq, droppable):
        try:
            async with lock:
                if droppable and seq < self._dropped_before.get(thread_id, -1):
                    return
                self._running[thread_id] = seq
                try:
                    async with self._slots:
                        await job()
                finally:
                    del self._running[thread_id]
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
    """Splits a reply into messages under Discord's limit, keeping code blocks intact.

    A code block cut in two is closed at the end of one message and opened
    again, with its language, at the start of the next. A line longer than a
    message is cut into pieces.
    """
    piece_size = DISCORD_LIMIT - 50  # leaves room for the fence lines around a piece
    chunks, lines, size = [], [], 0
    opener = None  # the line that opened the code block we are in, or None

    def body():
        # A code block that opens on the chunk's last line has nothing in it
        # yet, so it moves to the next message whole.
        return lines[:-1] if opener is not None and lines[-1:] == [opener] else lines

    def flush():
        nonlocal lines, size
        kept = body()
        if any(line.strip() for line in kept):
            chunks.append("\n".join(kept + (["```"] if kept is lines and opener is not None else [])))
        lines = [opener] if opener is not None else []
        size = sum(len(line) + 1 for line in lines)

    for line in text.split("\n"):
        for start in range(0, max(len(line), 1), piece_size):
            piece = line[start : start + piece_size]
            if size + len(piece) + 5 > DISCORD_LIMIT:  # 5: a newline and a closing fence
                flush()
            lines.append(piece)
            size += len(piece) + 1
        if line.lstrip().startswith("```"):
            opener = None if opener is not None else (line if len(line) <= piece_size else "```")
    if any(line.strip() for line in body()):
        chunks.append("\n".join(body()))
    return chunks


async def send(channel, content, file=None):
    """Posts a message, with `file` (a discord.File) attached if given."""
    if file is None:
        await channel.send(content, allowed_mentions=NO_MENTIONS)
    else:
        await channel.send(content, file=file, allowed_mentions=NO_MENTIONS)


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
    """The message's text without the mention of the bot. Attachments are not included."""
    role = bot_role(message)
    text = re.sub(rf"<@!?{bot_id}>", "", message.content)
    if role is not None:
        text = text.replace(f"<@&{role.id}>", "")
    return text.strip()


# Attachments of these kinds are read as text. Discord guesses the type from
# the file name and leaves it out for names it does not know, so common
# source file endings count too.
TEXT_TYPES = {
    "application/json", "application/xml", "application/javascript", "application/x-javascript",
    "application/yaml", "application/x-yaml", "application/toml", "application/x-sh", "application/sql",
    "application/x-python", "application/typescript",
}
TEXT_SUFFIXES = {
    ".txt", ".md", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".env", ".xml", ".html", ".css", ".scss", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".java", ".kt", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".sh",
    ".bash", ".ps1", ".sql", ".diff", ".patch", ".dockerfile", ".gradle", ".lua", ".r", ".vue", ".svelte",
}
# Text files known by their whole name, which have no suffix to go by.
TEXT_NAMES = {
    "dockerfile", "makefile", "justfile", "procfile", "gemfile", "readme", "license", "changelog",
    ".env", ".gitignore", ".gitattributes", ".dockerignore", ".editorconfig", ".npmrc", ".prettierrc", ".eslintrc",
}


def looks_like_text(attachment):
    kind = (attachment.content_type or "").split(";")[0].strip().lower()
    name = (attachment.filename or "").lower()
    suffix = os.path.splitext(name)[1]  # empty for ".env" or "Dockerfile"
    return kind.startswith("text/") or kind in TEXT_TYPES or suffix in TEXT_SUFFIXES or name in TEXT_NAMES


async def attachment_part(attachment):
    """The file part for a text attachment, or None to pass it as a link."""
    try:
        data = await attachment.read()
        if "\x00" in data.decode("utf-8"):
            return None  # not text after all
    except Exception as err:  # a failed download, or not UTF-8: the link still works
        log.info("attachment %s is passed as a link: %s", attachment.filename, err)
        return None
    return {
        # Sent as plain text whatever the file is: the agent turns text/plain
        # into file content, but refuses some other text types.
        "type": "file",
        "mime": "text/plain",
        "filename": attachment.filename,
        "url": "data:text/plain;base64," + base64.b64encode(data).decode("ascii"),
    }


async def attachment_parts(message):
    """(file parts, links) for a message's attachments.

    Text files within the size limits become file parts, which the agent
    reads as their content. Every other attachment is listed as a link.
    """
    # Chosen from the sizes Discord reports, in order, before anything is downloaded.
    chosen, budget = [], TEXT_ATTACHMENTS_TOTAL
    for attachment in message.attachments:
        if looks_like_text(attachment) and attachment.size <= min(TEXT_ATTACHMENT_LIMIT, budget):
            budget -= attachment.size
            chosen.append(attachment)
    found = dict(zip(map(id, chosen), await asyncio.gather(*(attachment_part(a) for a in chosen))))
    parts, links = [], []
    for attachment in message.attachments:
        part = found.get(id(attachment))
        if part is None:
            links.append(f"{attachment.filename}: {attachment.url}")
        else:
            parts.append(part)
    return parts, links


def with_links(text, links):
    if not links:
        return text
    return (text + "\n\n" if text else "") + "(Attached files, fetch them if needed:\n" + "\n".join(links) + ")"


def thread_name(text, author):
    first_line = " ".join(text.split("\n")[0].split())
    return (first_line or f"Request from {author}")[:90]


def session_title(thread):
    # A thread that wasn't in discord.py's cache arrives as a PartialMessageable,
    # which has no name.
    return commands.session_title(getattr(thread, "name", None) or thread.id)


async def keep_typing(channel):
    while True:
        with contextlib.suppress(Exception):
            await channel.typing()  # one typing indicator, shown for about 10 seconds
        await asyncio.sleep(8)


# ---------------------------------------------------------------- handlers


def register_thread(thread, author):
    # Registered right away, before the request waits for a slot, so messages
    # posted in the thread meanwhile are queued behind it instead of ignored.
    # run_turn creates the session when the first request runs.
    entry = store.register_thread(thread.id, author.id)
    log.info("thread %s opened by %s", thread.id, author.name)
    return entry


async def home_of(channel):
    """The text channel the bot works under for `channel`: the channel itself, or
    a thread's parent. None when OpenCord cannot accept input there."""
    if not isinstance(channel, (discord.Thread, discord.abc.GuildChannel)):
        # Missing from discord.py's cache (a PartialMessageable), so ask Discord.
        # Unknown channels are disabled.
        found = client.get_channel(channel.id)
        if found is None:
            with contextlib.suppress(discord.HTTPException):
                found = await client.fetch_channel(channel.id)
        channel = found
    home = channel.parent if isinstance(channel, discord.Thread) else channel
    return home if isinstance(home, discord.TextChannel) and access.enabled(home, CHANNEL_IDS) else None


def channel_for(thread_id):
    """Something to send to for a thread id, whether or not discord.py has the thread cached."""
    return client.get_channel(int(thread_id)) or client.get_partial_messageable(int(thread_id))


async def thread_for_session(session_id):
    """The thread a session belongs to. A subagent's session counts as its parent's."""
    for _ in range(4):
        session_id = router.root(session_id)
        for thread_id, entry in store.threads.items():
            if entry.get("sessionId") == session_id:
                return int(thread_id)
        # Not known from the stream (the subagent started before the bridge did): ask the agent.
        try:
            session_id = ((await agent.session(session_id, BASE_DIR)) or {}).get("parentID")
        except Exception:
            return None
        if not session_id:
            return None
    return None


def note_clock(entry, turn):
    """Adds how long the turn has worked to its note, so a restarted bridge
    leaves out the time spent waiting on people. True if there was a note."""
    note = entry.get("turn")
    if note is not None:
        note.update(worked=turn.running_for(), paused=turn.waiting, notedAt=time.time())
    return note is not None


def worked_before(note):
    """Seconds a request from before a restart had worked, from its note."""
    now = time.time()
    if not isinstance(note.get("worked"), (int, float)):
        return max(0.0, now - (note.get("startedAt") or now))
    # While it was waiting on a person its clock was stopped, downtime included.
    since = 0.0 if note.get("paused") else now - (note.get("notedAt") or now)
    return max(0.0, note["worked"] + since)


def question_opened(thread_id):
    turn = router.turn_for_thread(thread_id)
    if turn is not None:
        turn.pause()  # the request's clock stops while a person is being waited on
        if note_clock(store.threads.get(str(thread_id)) or {}, turn):
            store.save()


def question_closed(thread_id):
    turn = router.turn_for_thread(thread_id)
    if turn is not None:
        turn.resume()
        if note_clock(store.threads.get(str(thread_id)) or {}, turn):
            store.save()


questions = Questions(
    store,
    agent,
    BASE_DIR,
    channel_for=channel_for,
    home_of=home_of,
    thread_for_session=thread_for_session,
    on_open=question_opened,
    on_close=question_closed,
)
router.on_question = questions.ask
router.on_question_closed = questions.closed


async def stop_thread(thread_id, entry, stop_point):
    """Drops the thread's jobs submitted before stop_point and stops the one running."""
    queue.drop_waiting(thread_id, stop_point)
    turn = router.turn_for_thread(thread_id)
    if turn is not None:
        turn.stopped = True
    await questions.close_thread(thread_id, "Stopped before it was answered.")
    if entry.get("sessionId"):
        with contextlib.suppress(Exception):
            await agent.abort(entry["sessionId"], entry["directory"])


async def prompt_settings(thread, entry):
    """(fields, label) for this thread's next prompt.

    `fields` are the model, variant and agent to send. `label` says the same
    in words, for the status message. A setting the agent no longer offers
    (opencode.json changed since it was chosen) is removed from the thread,
    and the thread is told.
    """
    try:
        snapshot = await catalog.get()
    except Exception as err:
        log.error("thread %s: could not read the model list, sending without settings: %s", thread.id, err)
        return {}, ""
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
    label = []
    if "model" in options:
        label.append(snapshot.model_name(model_key(options["model"])))
    if "variant" in options:
        label.append(f"effort {options['variant']}")
    if "agent" in options:
        label.append(f"{options['agent']} agent")
    return options, " · ".join(label)


async def ensure_session(thread, entry):
    """The thread's session id. A thread that has none yet gets a new session."""
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
    return entry["sessionId"]


async def live_session(thread, entry):
    """Like ensure_session, and also replaces a session the agent no longer has.

    For /command and /shell, whose calls cannot start over by themselves.
    run_turn recovers from a lost session on its own, without this extra call.
    """
    session_id = await ensure_session(thread, entry)
    try:
        await agent.session(session_id, entry["directory"])
    except OpenCodeError as err:
        if err.status != 404:
            raise
        await replace_session(thread, entry)
    return entry["sessionId"]


async def replace_session(thread, entry):
    """Starts a new session for a thread whose session the agent no longer has
    (its data volume was reset, say), and tells the thread."""
    entry["sessionId"] = await agent.create_session(entry["directory"], session_title(thread))
    store.save()
    await send(thread, "My earlier context for this thread was lost, so I started a fresh session.")
    return entry["sessionId"]


async def run_turn(thread, entry, author, text, files=()):
    """Sends one message to the agent and follows the work until it ends.

    `files` are file parts from attachment_parts, sent along with the text.
    """
    log.info("thread %s: prompt from %s (%d chars, %d files)", thread.id, author.name, len(text), len(files))
    await ensure_session(thread, entry)
    options, label = await prompt_settings(thread, entry)
    log.info("thread %s: using %s", thread.id, label or "the agent's defaults")
    says = text or "(No message, only the attached files.)"
    if files:
        # Without this the agent looks for the files in the checkout, fails to
        # find them, and distrusts the content it was given.
        names = ", ".join(part["filename"] for part in files)
        says += f"\n\n(Attached to this message, with their content included here: {names}. They are not in the checkout.)"
    body = {
        "parts": [{"type": "text", "text": f"Discord user {author.display_name} says:\n\n{says}"}, *files],
        # The agent may ask questions: they are answered from Discord. Sent
        # every time, because sessions used before the bridge could do that
        # have the tool switched off, and this switches it back on.
        "tools": {"question": True},
        **options,
    }
    if queue.stopped(thread.id):  # stopped while it got ready, before there was a turn to mark
        log.info("thread %s: stopped before the prompt went out", thread.id)
        return
    # Registered before the prompt goes out, so none of its events are missed.
    turn = router.start(entry["sessionId"], thread.id)
    try:
        try:
            await agent.prompt_async(entry["sessionId"], entry["directory"], body)
        except OpenCodeError as err:
            if err.status != 404:
                raise
            router.finish(turn)
            await replace_session(thread, entry)
            turn = router.start(entry["sessionId"], thread.id)
            await agent.prompt_async(entry["sessionId"], entry["directory"], body)
        if turn.stopped:
            # Stopped while the prompt was on its way, so the abort may have reached the agent first.
            with contextlib.suppress(Exception):
                await agent.abort(turn.session_id, entry["directory"])
        await follow_turn(thread, entry, turn, label, time.time())
    finally:
        router.finish(turn)


def command_ended(turn, call):
    """The request behind a /command returned. Its work is over, or it failed to start."""
    if call.cancelled():
        return
    err = call.exception()
    if err is None:
        turn.done.set()
        return
    # The agent may still be working (the connection dropped, or the call
    # outlasted its timeout while a question waited). The waiter asks the
    # agent. Only a call that failed before the work began is the request's
    # error: once it has begun, the agent's own messages say how it went.
    if turn.error is None and not turn.active:
        turn.error = (type(err).__name__, str(err))
    turn.wake.set()


async def run_command(thread, entry, name, arguments):
    """Runs one of the agent's slash commands and follows the work like a prompt's."""
    log.info("thread %s: /%s (%d chars of arguments)", thread.id, name, len(arguments))
    session_id = await live_session(thread, entry)
    options, label = await prompt_settings(thread, entry)
    body = {"command": name, "arguments": arguments}
    if "model" in options:
        body["model"] = model_key(options["model"])
    if "variant" in options:
        body["variant"] = options["variant"]
    if "agent" in options:
        body["agent"] = options["agent"]
    if queue.stopped(thread.id):
        log.info("thread %s: stopped before /%s went out", thread.id, name)
        return
    turn = router.start(session_id, thread.id)
    # The command call only returns when the work is done. Its result is the reply.
    call = asyncio.create_task(agent.command(session_id, entry["directory"], body, PROMPT_TIMEOUT + SILENCE_TIMEOUT))
    call.add_done_callback(functools.partial(command_ended, turn))
    try:
        await follow_turn(thread, entry, turn, " · ".join(filter(None, [f"/{name}", label])), time.time(), call=call)
    finally:
        call.cancel()
        router.finish(turn)


async def resume_turn(thread_id, entry):
    """Picks a request back up after a restart. It may still be running, or have ended meanwhile."""
    await client.wait_until_ready()
    info = entry.get("turn") or {}
    session_id = entry.get("sessionId")
    if not session_id:
        entry.pop("turn", None)
        store.save()
        return
    started_at = info.get("startedAt") or time.time()
    # Registered before asking the agent, so the end of the work is not missed in between.
    turn = router.start(session_id, int(thread_id), active=True, elapsed=worked_before(info))
    # Counted before anything waits: a question shown from then on pauses the turn itself.
    for _ in questions.open_for(thread_id):
        turn.pause()
    try:
        working = session_id in await agent.statuses(entry["directory"])
        log.info(
            "thread %s: picking up a request from before the restart (%s)",
            thread_id, "still running" if working else "finished meanwhile",
        )
        if not working:
            turn.done.set()
        await follow_turn(
            channel_for(thread_id), entry, turn, info.get("label") or "", started_at, info.get("statusMessageId")
        )
    finally:
        router.finish(turn)


async def follow_turn(thread, entry, turn, label, started_at, status_message_id=None, call=None):
    """Shows a turn's progress in the thread until it ends, then posts the outcome.

    `call` is the task of a request that answers when the work is done (a
    /command). Its answer is the reply.
    """
    status = StatusMessage(thread, status_message_id)

    def render():
        return render_working(turn, label, started_at)

    async def is_working():
        return turn.session_id in await agent.statuses(entry["directory"])

    typing = asyncio.create_task(keep_typing(thread))
    try:
        await status.show(render())
        # Kept while the turn runs. A bridge that restarts reads it to pick the request back up.
        entry["turn"] = {"statusMessageId": status.message_id, "startedAt": started_at, "label": label}
        note_clock(entry, turn)
        store.save()
        updater = asyncio.create_task(status.follow(turn, render))
        try:
            outcome = await wait_for_turn(
                turn,
                is_working,
                time_limit=PROMPT_TIMEOUT,
                silence_limit=SILENCE_TIMEOUT,
                question_limit=QUESTION_TIMEOUT,
                question_age=lambda: questions.oldest_age(thread.id),
            )
        finally:
            updater.cancel()
        await finish_turn(thread, entry, turn, status, outcome, call)
    except Exception:
        # A failure here is reported by the caller. While shutting down the
        # note stays, failure or not, so the request is resumed on restart.
        if not shutting_down:
            entry.pop("turn", None)
            store.save()
        raise
    finally:
        typing.cancel()


async def call_answer(call):
    """The message a finished request answered with, or None if there is none to use."""
    if call is None:
        return None
    if not call.done():
        # The stream can say the work is over a moment before the answer arrives.
        await asyncio.wait({call}, timeout=15)
    if not call.done() or call.cancelled() or call.exception() is not None:
        return None
    answer = call.result()
    return answer if isinstance(answer, dict) else None


async def finish_turn(thread, entry, turn, status, outcome, call=None):
    """Posts what a turn produced. `outcome` is what wait_for_turn returned."""
    limits = {"timeout": PROMPT_TIMEOUT, "silent": SILENCE_TIMEOUT, "unanswered": QUESTION_TIMEOUT}
    minutes = round(limits.get(outcome, 0) / 60)
    halted = True
    if outcome == "done":
        await questions.close_thread(thread.id)  # none should be left, but the buttons must not linger
    else:
        # Still working, or waiting on a person: stop it.
        await questions.close_thread(thread.id, "Nobody answered in time." if outcome == "unanswered" else "Stopped before it was answered.")
        try:
            await agent.abort(turn.session_id, entry["directory"])
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(turn.done.wait(), 10)  # let the agent write the stop down
        except Exception as err:
            halted = False
            log.error("thread %s: could not stop session %s: %s", thread.id, turn.session_id, err)

    reply, error, unread = "", None, None
    answer = await call_answer(call)
    try:
        if answer is not None:
            reply, error = parts_text(answer.get("parts")), (answer.get("info") or {}).get("error")
        elif call is not None and call.done() and turn.user_message_id is None:
            pass  # the request failed before it reached the session: turn.error says why
        else:
            messages = await agent.recent_messages(turn.session_id, entry["directory"], REPLY_MESSAGES)
            reply, error = turn_reply(messages, turn.user_message_id)
    except Exception as err:
        unread = err
        log.error("thread %s: could not read the agent's reply: %s", thread.id, err)

    name = (error or {}).get("name") or (turn.error or (None, None))[0]
    detail = ((error or {}).get("data") or {}).get("message") or (turn.error or (None, None))[1] or name
    if outcome != "done":
        result = outcome
    elif turn.stopped or name == "MessageAbortedError":
        result = "stopped"  # by a person, and "!stop" or the command has already answered them
    elif name:
        result = "error"
    else:
        result = "done"
    log.info(
        "thread %s: %s after %s, %d tool calls, %d chars",
        thread.id, f"error ({detail})" if result == "error" else result,
        format_duration(turn.running_for()), len(turn.tools), len(reply),
    )
    await status.show(render_finished(turn, result, minutes))

    if reply:
        await send_long(thread, reply)
    carry_on = "Anything it changed so far is still in the checkout. Send another message to carry on."
    if result == "timeout" and halted:
        await send(thread, f"I stopped this request because it ran for more than {minutes} minutes. {carry_on}")
    elif result == "silent" and halted:
        await send(thread, f"I stopped this request because the agent showed no activity for {minutes} minutes. {carry_on}")
    elif result in ("timeout", "silent"):
        await send(thread, "This request ran past its limit. I tried to stop it but the agent didn't respond, so it may still be running.")
    elif result == "unanswered":
        await send(thread, f"Nobody answered the agent's question within {minutes} minutes, so I stopped this request. {carry_on}")
    elif result == "error":
        await send(thread, f"The agent stopped with an error: {str(detail)[:500]}")
    elif result == "done" and unread:
        await send(thread, f"The agent finished, but I could not read its reply: {str(unread)[:300]}")
    elif result == "done" and not reply:
        await send(thread, "Done. (The agent finished without a text reply.)")
    # Cleared last: if the bridge stops before this, the restart posts the outcome again rather than losing it.
    entry.pop("turn", None)
    store.save()


async def open_thread(channel, name, starter):
    """Opens a thread in `channel` under a new message `starter`. Returns the thread."""
    message = await channel.send(starter, allowed_mentions=NO_MENTIONS)
    return await message.create_thread(name=name[:100], auto_archive_duration=1440)


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
command_buttons = commands.setup(
    tree,
    commands.Context(
        store=store,
        agent=agent,
        catalog=catalog,
        queue=queue,
        compact_timeout=COMPACT_TIMEOUT,
        shell_timeout=SHELL_TIMEOUT,
        home_of=home_of,
        stop_thread=stop_thread,
        send=send,
        report_failure=report_failure,
        keep_typing=keep_typing,
        ensure_session=live_session,
        run_command=run_command,
        open_thread=open_thread,
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
    # Only threads in a channel that is still enabled count, except for "!stop",
    # so work already running can still be stopped after a channel is disabled.
    # That check can wait on Discord, so nothing is queued after it: follow-ups
    # make it inside their queued job, and the thread's messages keep their order.
    entry = store.threads.get(str(channel.id))
    if entry is not None:
        text = clean_content(message, bot_id)
        if not text and not message.attachments:
            return
        if text == "!stop":
            await stop_thread(channel.id, entry, queue.stop_point())
            with contextlib.suppress(discord.HTTPException):
                await message.add_reaction("🛑")
            return

        if questions.open_for(channel.id):
            # The agent is waiting on a person, so a message here is most
            # likely the answer. Queued as a request it would sit behind the
            # very request that is waiting for it.
            if await home_of(channel) is None:
                return
            if text and not message.attachments and await questions.answer_from_message(message, text):
                with contextlib.suppress(discord.HTTPException):
                    await message.add_reaction("✅")
                return
            await send(
                channel,
                "The agent is waiting for answers to the questions above. I will get to your message after that.",
            )

        async def follow_up():
            try:
                files, links = await attachment_parts(message)
                if await home_of(channel) is not None:
                    await run_turn(channel, entry, message.author, with_links(text, links), files)
            except Exception as err:
                await report_failure(channel, err)

        queue.submit(channel.id, follow_up)
        return

    # New requests: an @mention in an enabled channel.
    if not mentions_bot(message, bot_id):
        return
    home = await home_of(channel)
    if home is None:
        return
    if home.id != channel.id:  # a thread someone else opened
        with contextlib.suppress(discord.HTTPException):
            await message.reply(
                f"I can't start a thread inside a thread. Mention me in <#{home.id}> instead.",
                allowed_mentions=NO_MENTIONS,
            )
        return
    text = clean_content(message, bot_id)
    if not text and not message.attachments:
        await message.reply(
            "Mention me with a question or a task and I will start a thread for it.", allowed_mentions=NO_MENTIONS
        )
        return

    name = thread_name(text or message.attachments[0].filename, message.author.name)
    try:
        thread = await message.create_thread(name=name, auto_archive_duration=1440)
    except discord.HTTPException as err:
        await report_failure(channel, f'could not start a thread (does the bot have "Create Public Threads"?): {err}')
        return

    entry = register_thread(thread, message.author)

    async def new_request():
        try:
            files, links = await attachment_parts(message)
            if await home_of(home) is not None:
                await run_turn(thread, entry, message.author, with_links(text, links), files)
        except Exception as err:
            await report_failure(thread, err)

    queue.submit(thread.id, new_request)


@client.event
async def on_interaction(interaction):
    # Slash commands are handled by the command tree. This is for the buttons
    # and menus under the agent's questions, and the ones /undo shows.
    if interaction.type is not discord.InteractionType.component:
        return
    try:
        if not await questions.on_component(interaction):
            await command_buttons(interaction)
    except Exception:
        log.exception("handling a button")
        with contextlib.suppress(discord.HTTPException):
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong on my side.", ephemeral=True)


_background = set()  # asyncio only keeps weak references to tasks


def in_background(coroutine):
    task = asyncio.create_task(coroutine)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def check_questions():
    try:
        await questions.reconcile()
    except Exception as err:
        log.error("could not check for questions the agent is waiting on: %s", err)


def stream_connected():
    """The event stream is up. What happened while it was away was not sent, so look for it."""
    router.wake_all()
    if client.is_ready():
        in_background(check_questions())


async def resume_job(thread_id, entry):
    try:
        await resume_turn(thread_id, entry)
    except Exception as err:
        await report_failure(channel_for(thread_id), err)


_ready_once = False


@client.event
async def on_ready():
    global _ready_once
    if _ready_once:  # on_ready fires again after every reconnect
        return
    _ready_once = True
    log.info("logged in as %s", client.user)
    # Questions asked while the bridge was down have no buttons yet.
    in_background(check_questions())
    if not client.guilds:
        log.warning("not in any server yet. Invite the bot with the link in the README.")
    for guild in client.guilds:
        await prepare_server(guild)
    found = {str(channel.id) for guild in client.guilds for channel in guild.text_channels}
    if unknown := sorted(CHANNEL_IDS - found):
        log.warning(
            "DISCORD_CHANNEL_IDS lists %s, which is not a text channel in any of my servers. Check the ID.",
            ", ".join(unknown),
        )


@client.event
async def on_guild_join(guild):
    await prepare_server(guild)


async def prepare_server(guild):
    """Logs the enabled channels in `guild` and registers the slash commands there."""
    enabled = [channel for channel in guild.text_channels if access.enabled(channel, CHANNEL_IDS)]
    if not enabled:
        log.warning("%s: no enabled channels. Check DISCORD_CHANNEL_IDS and the bot's View Channel permission.", guild.name)
    for channel in enabled:
        log_working_in(channel)
    ignored = [channel for channel in guild.text_channels if access.visible(channel) and channel not in enabled]
    if ignored:
        log.warning(
            "%s: I can also read these channels, which I ignore: %s. Deny me View Channel there to keep their messages away (README).",
            guild.name,
            ", ".join(f"#{channel.name}" for channel in ignored),
        )
    # Registered to each server, where they show up at once. Commands
    # registered globally can take an hour to appear.
    try:
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        log.info("%s: slash commands ready: %s", guild.name, " ".join(f"/{command.name}" for command in synced))
    except discord.Forbidden as err:
        log.error(
            "%s: could not register slash commands: %s. Re-invite the bot with the invite link in the README.",
            guild.name,
            err,
        )
    except discord.HTTPException as err:
        log.error("%s: could not register slash commands: %s", guild.name, err)


def log_working_in(channel):
    missing = access.missing(channel)
    shown = f"; MISSING permissions: {', '.join(missing)}" if missing else ""
    log.info("%s: working in #%s%s", channel.guild.name, channel.name, shown)


@client.event
async def on_guild_channel_update(before, after):
    if not isinstance(after, discord.TextChannel):
        return
    was, now = access.enabled(before, CHANNEL_IDS), access.enabled(after, CHANNEL_IDS)
    if now and not was:
        log_working_in(after)
    elif was and not now:
        log.info("%s: no longer working in #%s", after.guild.name, after.name)


async def main():
    global shutting_down
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
        stream = asyncio.create_task(events.follow(agent, BASE_DIR, router, stream_connected))
        jobs = asyncio.create_task(router.run_jobs())
        # Requests that were running when the bridge last stopped go to the
        # front of their thread's queue, ahead of any new message.
        for thread_id, entry in store.threads.items():
            if entry.get("turn"):
                queue.submit(int(thread_id), functools.partial(resume_job, thread_id, entry))
        bot = asyncio.create_task(client.start(TOKEN))
        stopping = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({bot, stopping}, return_when=asyncio.FIRST_COMPLETED)
        shutting_down = True
        stream.cancel()
        jobs.cancel()
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
