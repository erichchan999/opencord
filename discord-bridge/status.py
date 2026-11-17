"""The live status message: one Discord message per request, edited in place
while the agent works and reduced to a single line when it is done.

The render functions only build text. StatusMessage does the sending.
"""

import asyncio
import logging

import discord

log = logging.getLogger("bridge")

NO_MENTIONS = discord.AllowedMentions.none()
MESSAGE_LIMIT = 1900
DETAIL_LIMIT = 150  # of one tool call or one todo
TODO_LINES = 8
TODO_MARKS = {"completed": "✅", "in_progress": "▶️", "cancelled": "✖️"}
# Shortest gap between two edits of one message. Discord allows about five
# edits per five seconds in a channel.
EDIT_INTERVAL = 2.0


def format_tokens(count):
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}".removesuffix(".0") + "k"


def format_duration(seconds):
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def plain(text, limit=DETAIL_LIMIT):
    """One line of text that cannot break the message's formatting."""
    text = " ".join(str(text).split()).replace("`", "'")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def plural(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def todo_lines(todos, limit=TODO_LINES):
    """One line per todo. Past `limit` lines (None: no limit), finished ones are folded into a count."""
    lines = [f"{TODO_MARKS.get(todo.get('status'), '⬜')} {plain(todo.get('content', ''))}" for todo in todos]
    if limit is None or len(lines) <= limit:
        return lines
    # Too many to show: fold the finished ones into a count and list what is left.
    finished = sum(1 for todo in todos if todo.get("status") in ("completed", "cancelled"))
    left = [line for line, todo in zip(lines, todos) if todo.get("status") not in ("completed", "cancelled")]
    shown = left[: limit - 2]
    folded = [f"✅ {finished} done"] if finished else []
    more = [f"… and {len(left) - len(shown)} more"] if len(left) > len(shown) else []
    return folded + shown + more


def activity_line(turn):
    """What the agent is doing at this moment."""
    if turn.waiting:
        return "❓ Waiting for an answer to the question below"
    if turn.retry:
        return f"⚠️ The model provider is not answering. Retry {turn.retry.get('attempt') or ''}: {plain(turn.retry.get('message', ''))}"
    tool = turn.current_tool()
    if tool:
        return f"🔧 `{plain(tool['tool'], 40)}` {plain(tool['detail'])}".rstrip()
    if turn.text.strip():
        # The end of what it is writing, so each update shows something new.
        return "💬 …" + plain(turn.text[-400:], 400)[-DETAIL_LIMIT:].lstrip()
    return "💭 Thinking"


def render_working(turn, label, started_at):
    """The message while the agent works. `label` names the model and settings in use."""
    head = f"⏳ **Working** · started <t:{int(started_at)}:R>" + (f" · {label}" if label else "")
    lines = [head, activity_line(turn), *todo_lines(turn.todos)]
    if turn.tools:
        lines.append(f"-# {plural(len(turn.tools), 'tool call')} so far")
    return "\n".join(lines)[:MESSAGE_LIMIT]


def render_finished(turn, outcome, minutes=None):
    """The one line the message ends as.

    outcome is "done", "stopped" or "error", or wait_for_turn's reason for
    stopping the turn ("timeout", "silent", "unanswered"). `minutes` is the
    limit that was passed, for those three.
    """
    took = format_duration(turn.running_for())
    facts = []
    if turn.tools:
        facts.append(plural(len(turn.tools), "tool call"))
    if turn.tokens:
        facts.append(f"{format_tokens(turn.tokens)} tokens in context")
    tail = "".join(f" · {fact}" for fact in facts)
    if outcome == "done":
        return f"-# ✅ Done in {took}{tail}"
    if outcome == "stopped":
        return f"-# 🛑 Stopped after {took}{tail}"
    if outcome == "error":
        return f"-# ⚠️ Ended with an error after {took}{tail}"
    reason = {
        "timeout": f"it ran past the {minutes} minute limit",
        "silent": f"the agent showed no activity for {minutes} minutes",
        "unanswered": f"nobody answered the question within {minutes} minutes",
    }[outcome]
    return f"-# ⏱️ Stopped after {took}: {reason}{tail}"


class StatusMessage:
    """Sends the status text to a thread, then edits that same message."""

    def __init__(self, channel, message_id=None, interval=EDIT_INTERVAL):
        self._channel = channel
        self.message_id = message_id
        self._interval = interval
        self._shown = None

    async def show(self, text):
        if text == self._shown:
            return
        try:
            if self.message_id is not None:
                try:
                    # Embeds stay suppressed: that flag was set when the message was sent.
                    await self._channel.get_partial_message(self.message_id).edit(
                        content=text, allowed_mentions=NO_MENTIONS
                    )
                    self._shown = text
                    return
                except discord.NotFound:
                    self.message_id = None  # someone deleted it: post a new one
            message = await self._channel.send(text, allowed_mentions=NO_MENTIONS, suppress_embeds=True)
            self.message_id = message.id
            self._shown = text
        except discord.HTTPException as err:
            log.warning("could not update the status message: %s", err)

    async def follow(self, turn, render):
        """Keeps the message current until cancelled, with at most one edit per interval."""
        while True:
            await turn.changed.wait()
            turn.changed.clear()
            await self.show(render())
            await asyncio.sleep(self._interval)
