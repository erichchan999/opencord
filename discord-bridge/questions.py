"""Questions from the agent, answered in Discord.

OpenCode's question tool pauses a request until someone answers. Each question
becomes one message in the thread, with a button per option (a menu when there
are many, or when several can be picked) and a button for typing an answer.
When every question of a request has an answer, the answers go to the agent.

Open questions are kept in the bridge's state file, so the buttons still work
after the bridge restarts. A button click arrives as an interaction whose
custom id names the request, the question and the option.
"""

import contextlib
import logging
import re
import time

import discord

from opencode import OpenCodeError

log = logging.getLogger("bridge")

NO_MENTIONS = discord.AllowedMentions.none()
GONE = "This question is no longer waiting for an answer."
# Up to this many options get a button each; more go in a menu.
BUTTON_OPTIONS = 4
ANSWER_LIMIT = 1000

# q:<request id>:<question number>:<o|s|c>[:<option number>]
#   o = an option's button, s = the menu, c = the "Type an answer" button
CUSTOM_ID = re.compile(r"^q:(?P<request>[A-Za-z0-9_]+):(?P<index>\d+):(?P<kind>[osc])(?::(?P<option>\d+))?$")


def custom_id(request_id, index, kind, option=None):
    return f"q:{request_id}:{index}:{kind}" + ("" if option is None else f":{option}")


def parse_custom_id(value):
    """(request id, question number, kind, option number or None), or None if it is not one of ours."""
    match = CUSTOM_ID.match(value or "")
    if not match:
        return None
    option = match["option"]
    return match["request"], int(match["index"]), match["kind"], None if option is None else int(option)


def shown(text, limit=300):
    """Text from the agent or a person, safe to put inside a formatted message."""
    return discord.utils.escape_markdown(" ".join(str(text).split())[:limit])


def question_text(question, *, answer=None, by=None, note=None):
    """The message for one question: open, answered (`answer`, `by`) or closed with a `note`."""
    lines = [f"❓ **{shown(question.get('question') or question.get('header') or 'The agent has a question')}**"]
    if answer is None and note is None:
        for option in [o for o in question.get("options") or [] if o.get("description")][:10]:
            lines.append(f"- **{shown(option.get('label', ''), 80)}**: {shown(option['description'], 150)}")
        if question.get("multiple"):
            lines.append("-# You can pick more than one.")
    if answer is not None:
        lines.append(f"**Answer:** {shown(', '.join(answer), 1200)}" + (f" (from {shown(by, 80)})" if by else ""))
    if note:
        lines.append(f"-# {note}")
    return "\n".join(lines)[:1900]


def build_view(request_id, index, question):
    """The buttons or menu for one question. Must be called while the event loop runs."""
    view = discord.ui.View(timeout=None)
    options = (question.get("options") or [])[:25]
    multiple = bool(question.get("multiple"))
    if options and (multiple or len(options) > BUTTON_OPTIONS):
        view.add_item(
            discord.ui.Select(
                custom_id=custom_id(request_id, index, "s"),
                placeholder="Choose one or more" if multiple else "Choose one",
                min_values=1,
                max_values=len(options) if multiple else 1,
                options=[
                    discord.SelectOption(
                        label=(option.get("label") or f"Option {number + 1}")[:100],
                        value=str(number),
                        description=(option.get("description") or "")[:100] or None,
                    )
                    for number, option in enumerate(options)
                ],
            )
        )
    else:
        for number, option in enumerate(options):
            view.add_item(
                discord.ui.Button(
                    label=(option.get("label") or f"Option {number + 1}")[:80],
                    custom_id=custom_id(request_id, index, "o", number),
                    style=discord.ButtonStyle.primary,
                )
            )
    if question.get("custom", True) or not options:
        view.add_item(
            discord.ui.Button(
                label="Type an answer", custom_id=custom_id(request_id, index, "c"), style=discord.ButtonStyle.secondary
            )
        )
    # Stopped so discord.py does not keep it and try to handle the clicks
    # itself. Clicks are handled from the custom id, which also works for
    # messages sent before a restart.
    view.stop()
    return view


class AnswerModal(discord.ui.Modal):
    """The pop-up for typing an answer."""

    def __init__(self, questions, request_id, index, question):
        super().__init__(title=(question.get("header") or "Your answer")[:45], timeout=None)
        self._questions = questions
        self._request_id = request_id
        self._index = index
        self.answer = discord.ui.TextInput(
            label="Your answer", style=discord.TextStyle.paragraph, max_length=ANSWER_LIMIT, required=True
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction):
        await self._questions.answer_typed(interaction, self._request_id, self._index, self.answer.value)


class Questions:
    """Shows the agent's questions in threads and sends the answers back.

    store.questions maps a request id to
      {"thread", "questions", "answers", "messages", "askedAt"}
    where answers holds, per question, the chosen labels or None while open,
    and messages holds the Discord message of each question.
    """

    def __init__(self, store, agent, directory, *, channel_for, home_of, thread_for_session, on_open, on_close):
        self._store = store
        self._agent = agent
        self._directory = directory
        self._channel_for = channel_for  # (thread id) -> something to send to
        self._home_of = home_of  # async (channel) -> its enabled parent, or None
        self._thread_for_session = thread_for_session  # async (session id) -> thread id or None
        self._on_open = on_open  # (thread id), when a request starts waiting on people
        self._on_close = on_close  # (thread id), when it stops

    def open_for(self, thread_id):
        return [rid for rid, entry in self._store.questions.items() if entry.get("thread") == str(thread_id)]

    def oldest_age(self, thread_id):
        """Seconds the thread's longest-waiting question has been open, or 0."""
        asked = [self._store.questions[rid].get("askedAt") or 0 for rid in self.open_for(thread_id)]
        return time.time() - min(asked) if asked else 0

    # ------------------------------------------------------------ from the agent

    async def ask(self, session_id, request):
        """The agent asked something ("question.asked")."""
        thread_id = await self._thread_for_session(session_id)
        if thread_id is not None:
            await self._show(thread_id, request)

    async def _show(self, thread_id, request):
        request_id = request.get("id")
        questions = request.get("questions") or []
        if not request_id or request_id in self._store.questions:
            return
        if not questions:
            with contextlib.suppress(Exception):
                await self._agent.reply_question(request_id, self._directory, [])
            return
        entry = {
            "thread": str(thread_id),
            "questions": questions,
            "answers": [None] * len(questions),
            "messages": [],
            "askedAt": time.time(),
        }
        # Stored before the first send, so a second notice of the same request is ignored.
        self._store.questions[request_id] = entry
        self._on_open(thread_id)
        channel = self._channel_for(thread_id)
        log.info("thread %s: the agent asks %d question(s)", thread_id, len(questions))
        try:
            for index, question in enumerate(questions):
                message = await channel.send(
                    question_text(question), view=build_view(request_id, index, question), allowed_mentions=NO_MENTIONS
                )
                entry["messages"].append(message.id)
        except discord.HTTPException as err:
            log.error("thread %s: could not show a question: %s", thread_id, err)
            await self._retire(request_id, GONE, reject=True)
            return
        self._store.save()

    async def closed(self, request_id):
        """The agent stopped waiting ("question.replied" or "question.rejected")."""
        await self._retire(request_id, GONE)

    async def close_thread(self, thread_id, note=GONE):
        """Dismisses every open question of a thread, on the agent as well."""
        for request_id in self.open_for(thread_id):
            await self._retire(request_id, note, reject=True)

    async def _retire(self, request_id, note, reject=False):
        entry = self._store.questions.pop(request_id, None)
        if entry is None:
            return
        self._store.save()
        thread_id = int(entry["thread"])
        if reject:
            # An aborted session keeps its question pending unless it is rejected.
            with contextlib.suppress(Exception):
                await self._agent.reject_question(request_id, self._directory)
        channel = self._channel_for(thread_id)
        for index, message_id in enumerate(entry["messages"]):
            if entry["answers"][index] is None:
                with contextlib.suppress(discord.HTTPException):
                    await channel.get_partial_message(message_id).edit(
                        content=question_text(entry["questions"][index], note=note), view=None, allowed_mentions=NO_MENTIONS
                    )
        self._on_close(thread_id)

    async def reconcile(self):
        """Brings the threads in line with what the agent is waiting on, after the stream was away."""
        pending = await self._agent.pending_questions(self._directory)
        working = await self._agent.statuses(self._directory)
        pending_ids = {request.get("id") for request in pending}
        for request_id in [rid for rid in self._store.questions if rid not in pending_ids]:
            await self._retire(request_id, GONE)
        for request in pending:
            if request.get("id") in self._store.questions:
                continue
            thread_id = await self._thread_for_session(request.get("sessionID"))
            if thread_id is None:
                continue
            session_id = (self._store.threads.get(str(thread_id)) or {}).get("sessionId")
            if session_id in working:
                await self._show(thread_id, request)
            else:
                # Left behind by a request that has ended. Nothing will use the answer.
                with contextlib.suppress(Exception):
                    await self._agent.reject_question(request["id"], self._directory)

    # ------------------------------------------------------------ from people

    async def on_component(self, interaction):
        """A button or menu was used. Returns False if it is not one of the bridge's."""
        data = interaction.data or {}
        parsed = parse_custom_id(data.get("custom_id"))
        if parsed is None:
            return False
        request_id, index, kind, option = parsed
        entry = await self._open_entry(interaction, request_id, index)
        if entry is None:
            return True
        question = entry["questions"][index]
        options = question.get("options") or []
        if kind == "c":
            await interaction.response.send_modal(AnswerModal(self, request_id, index, question))
            return True
        numbers = [option] if kind == "o" else [int(value) for value in data.get("values") or [] if str(value).isdigit()]
        labels = [options[number].get("label") or f"Option {number + 1}" for number in numbers if number is not None and number < len(options)]
        if not labels:
            await interaction.response.defer()
            return True
        await self._record(interaction, request_id, index, labels)
        return True

    async def answer_typed(self, interaction, request_id, index, text):
        """The pop-up for typing an answer was submitted."""
        entry = await self._open_entry(interaction, request_id, index)
        if entry is not None and text.strip():
            await self._record(interaction, request_id, index, [text.strip()[:ANSWER_LIMIT]])

    async def answer_from_message(self, message, text):
        """Uses a plain message as the answer when exactly one question is open in its thread.

        Returns False when it is not clear which question the message would
        answer. The caller then treats it as an ordinary message.
        """
        open_ids = self.open_for(message.channel.id)
        if len(open_ids) != 1:
            return False
        request_id = open_ids[0]
        entry = self._store.questions[request_id]
        unanswered = [index for index, answer in enumerate(entry["answers"]) if answer is None]
        if len(unanswered) != 1:
            return False
        index = unanswered[0]
        question = entry["questions"][index]
        typed = text.strip()
        label = next((o["label"] for o in question.get("options") or [] if (o.get("label") or "").lower() == typed.lower()), None)
        if not typed or (label is None and question.get("custom") is False):
            return False
        answer = [label or typed[:ANSWER_LIMIT]]
        entry["answers"][index] = answer
        self._store.save()
        if index < len(entry["messages"]):
            with contextlib.suppress(discord.HTTPException):
                await message.channel.get_partial_message(entry["messages"][index]).edit(
                    content=question_text(question, answer=answer, by=message.author.display_name),
                    view=None,
                    allowed_mentions=NO_MENTIONS,
                )
        await self._send_if_complete(request_id)
        return True

    async def _open_entry(self, interaction, request_id, index):
        """The request's entry if this question can still be answered. Otherwise tells the person and returns None."""
        channel = interaction.channel or self._channel_for(interaction.channel_id)
        if await self._home_of(channel) is None:
            await interaction.response.send_message("OpenCord is disabled in this channel.", ephemeral=True)
            return None
        entry = self._store.questions.get(request_id)
        if entry is not None and entry["thread"] != str(interaction.channel_id):
            await interaction.response.send_message(GONE, ephemeral=True)
            return None
        if entry is None or index >= len(entry["questions"]):
            content = getattr(interaction.message, "content", None)
            if content and GONE not in content:
                # Take the buttons off the message they clicked.
                await self._edit(interaction, f"{content}\n-# {GONE}")
            else:
                await interaction.response.send_message(GONE, ephemeral=True)
            return None
        if entry["answers"][index] is not None:
            await interaction.response.send_message("That one already has an answer.", ephemeral=True)
            return None
        return entry

    async def _edit(self, interaction, text):
        """Replaces the question message's text and removes its buttons."""
        if interaction.response.is_done():
            await interaction.edit_original_response(content=text, view=None, allowed_mentions=NO_MENTIONS)
        else:
            await interaction.response.edit_message(content=text, view=None, allowed_mentions=NO_MENTIONS)

    async def _record(self, interaction, request_id, index, labels):
        entry = self._store.questions[request_id]
        entry["answers"][index] = labels
        self._store.save()
        log.info("thread %s: %s answered a question", entry["thread"], interaction.user.name)
        await self._edit(
            interaction, question_text(entry["questions"][index], answer=labels, by=interaction.user.display_name)
        )
        await self._send_if_complete(request_id)

    async def _send_if_complete(self, request_id):
        """Passes the answers to the agent once every question of the request has one."""
        entry = self._store.questions.get(request_id)
        if entry is None or any(answer is None for answer in entry["answers"]):
            return
        del self._store.questions[request_id]
        self._store.save()
        thread_id = int(entry["thread"])
        try:
            await self._agent.reply_question(request_id, self._directory, entry["answers"])
        except Exception as err:
            gone = isinstance(err, OpenCodeError) and err.status == 404
            log.warning("thread %s: answer not delivered: %s", thread_id, err)
            with contextlib.suppress(discord.HTTPException):
                await self._channel_for(thread_id).send(
                    "The agent had already stopped waiting for that answer, so it was not used."
                    if gone
                    else f"I could not pass the answer on to the agent: {str(err)[:300]}",
                    allowed_mentions=NO_MENTIONS,
                )
        finally:
            self._on_close(thread_id)
