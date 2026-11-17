"""Follows the agent's event stream.

A prompt is handed to the agent with prompt_async, which returns at once. What
the agent then does (tool calls, todo list, text, questions, the end of the
work) arrives as events. A Turn collects the events of one request, and the
Router hands each event to the Turn it belongs to.

Events that happen while the stream is disconnected are lost, so nothing here
relies on seeing every one: wait_for_turn also asks the agent directly whether
a session is still working.
"""

import asyncio
import logging
import time

log = logging.getLogger("bridge")

# How often a waiting request checks on the agent when the stream is quiet.
POLL_SECONDS = 20
# A session that has shown no activity this long after its prompt went out, and
# is not working, is taken to have ended without the stream saying so.
START_GRACE_SECONDS = 10


def tool_detail(tool, state):
    """A short description of what a tool call is doing, from its input."""
    given = state.get("input") or {}
    keys = {
        "bash": "command",
        "read": "filePath",
        "edit": "filePath",
        "write": "filePath",
        "glob": "pattern",
        "grep": "pattern",
        "webfetch": "url",
        "websearch": "query",
        "task": "description",
        "skill": "name",
    }
    value = given.get(keys.get(tool, ""))
    if not isinstance(value, str) or not value.strip():
        value = state.get("title")
    return " ".join(value.split()) if isinstance(value, str) else ""


class Turn:
    """One request being worked on: what the event stream has said about it so far."""

    def __init__(self, session_id, thread_id, *, active=False, elapsed=0.0, clock=time.monotonic):
        self.session_id = session_id
        self.thread_id = thread_id
        self._clock = clock
        # `elapsed` is how long the request had already run, for one picked up after a restart.
        self.started = clock() - elapsed
        self.last_event = clock()
        # Set once the agent has started on this prompt. Until then an "idle"
        # event is left over from the previous request and means nothing.
        self.active = active
        self.done = asyncio.Event()  # the agent finished working
        self.changed = asyncio.Event()  # something the status message shows is different
        self.wake = asyncio.Event()  # the waiter should check on the agent now
        self.stopped = False  # a person stopped it
        self.error = None  # (name, message) from the stream
        self.user_message_id = None  # the message this prompt became
        self.retry = None  # {"attempt", "message"} while the provider is being retried
        self.todos = []
        self.text = ""  # what the agent is writing right now
        self.tokens = None  # size of the conversation after the latest step
        self.tools = {}  # part id -> {"tool", "status", "detail"}, in the order first seen
        self._roles = {}  # message id -> role
        self._parts = {}  # part id -> type
        self._paused = 0  # open questions: the clock stops while a person is being waited on
        self._paused_at = 0.0
        self._paused_total = 0.0

    # ------------------------------------------------------------ time

    def pause(self):
        """Called when a question is put to a person."""
        if not self._paused:
            self._paused_at = self._clock()
        self._paused += 1
        self.changed.set()

    def resume(self):
        """Called when a question has been answered or dropped."""
        if not self._paused:
            return
        self._paused -= 1
        if not self._paused:
            self._paused_total += self._clock() - self._paused_at
            self.last_event = self._clock()
        self.changed.set()

    @property
    def waiting(self):
        """True while a person is being waited on."""
        return self._paused > 0

    def running_for(self):
        """Seconds the agent has had to work, leaving out time spent waiting on a person."""
        now = self._clock()
        waited = self._paused_total + (now - self._paused_at if self._paused else 0)
        return now - self.started - waited

    def quiet_for(self):
        return self._clock() - self.last_event

    # ------------------------------------------------------------ what to show

    def current_tool(self):
        """The tool call in progress, preferring the one started last."""
        running = [tool for tool in self.tools.values() if tool["status"] == "running"]
        pending = [tool for tool in self.tools.values() if tool["status"] == "pending"]
        return (running or pending or [None])[-1]

    # ------------------------------------------------------------ events

    def apply(self, kind, props, child=False):
        """Takes one event for this turn's session, or for a subagent session under it."""
        self.last_event = self._clock()
        if kind == "message.part.updated":
            self._part(props.get("part") or {}, child)
        elif kind == "message.part.delta":
            mine = self._parts.get(props.get("partID")) == "text" and self._roles.get(props.get("messageID")) == "assistant"
            if mine and props.get("field") == "text" and not child:
                self.text += props.get("delta") or ""
                self.changed.set()
        elif kind == "message.updated":
            self._message(props.get("info") or {}, child)
        elif child:
            return  # a subagent's status, todos and errors are its parent's business, not the thread's
        elif kind == "session.status":
            self._status(props.get("status") or {})
        elif kind == "session.idle":
            self._ended()
        elif kind == "session.error":
            error = props.get("error") or {}
            if self.error is None:  # later ones repeat the first with a stack trace
                self.error = (error.get("name") or "error", (error.get("data") or {}).get("message") or "")
            self.wake.set()
        elif kind == "todo.updated":
            self.todos = props.get("todos") or []
            self.changed.set()

    def _status(self, status):
        kind = status.get("type")
        if kind == "idle":
            return self._ended()
        if not self.active:
            self.active = True
            self.error = None  # an error seen before the work began belongs to the request before
        retry = {"attempt": status.get("attempt"), "message": status.get("message") or ""} if kind == "retry" else None
        if retry != self.retry:
            self.retry = retry
            self.changed.set()

    def _ended(self):
        if self.active:
            self.done.set()

    def _message(self, info, child):
        self._roles[info.get("id")] = info.get("role")
        if child:
            return
        if info.get("role") == "user" and self.user_message_id is None:
            self.user_message_id = info.get("id")
        elif info.get("role") == "assistant":
            self.active = True
            self._tokens(info.get("tokens"))

    def _tokens(self, tokens):
        total = (tokens or {}).get("total")
        if total:
            self.tokens = total

    def _part(self, part, child):
        kind = part.get("type")
        self._parts[part.get("id")] = kind
        if kind == "tool":
            state = part.get("state") or {}
            tool = self.tools.setdefault(part.get("id"), {"tool": part.get("tool") or "tool", "detail": ""})
            tool["status"] = state.get("status")
            tool["detail"] = tool_detail(tool["tool"], state) or tool["detail"]
            self.changed.set()
        elif child:
            return
        elif kind == "text" and self._roles.get(part.get("messageID")) == "assistant":
            self.text = part.get("text") or ""
            self.changed.set()
        elif kind == "step-finish":
            self._tokens(part.get("tokens"))


class Router:
    """Hands each event to the Turn it belongs to."""

    def __init__(self):
        self.turns = {}  # session id -> Turn
        self._parents = {}  # subagent session id -> the session that started it
        self.on_question = None  # async (session id, request), for "question.asked"
        self.on_question_closed = None  # async (request id), when one is answered or dismissed
        self._jobs = asyncio.Queue()

    def start(self, session_id, thread_id, **options):
        turn = Turn(session_id, thread_id, **options)
        self.turns[session_id] = turn
        return turn

    def finish(self, turn):
        if self.turns.get(turn.session_id) is turn:
            del self.turns[turn.session_id]
        for child in [child for child in self._parents if self.root(child) == turn.session_id]:
            del self._parents[child]

    def root(self, session_id):
        """The session a person is talking to, for a session that may be a subagent's."""
        for _ in range(10):  # subagents can nest, but not this deep
            parent = self._parents.get(session_id)
            if parent is None:
                break
            session_id = parent
        return session_id

    def turn_for_thread(self, thread_id):
        return next((turn for turn in self.turns.values() if turn.thread_id == thread_id), None)

    def wake_all(self):
        """Makes every waiting request check on the agent, after the stream was away."""
        for turn in self.turns.values():
            turn.wake.set()

    def dispatch(self, event):
        kind = event.get("type")
        props = event.get("properties") or {}
        if kind == "session.created":
            info = props.get("info") or {}
            if info.get("parentID"):
                self._parents[info.get("id")] = info["parentID"]
        session_id = props.get("sessionID")
        if not session_id:
            return
        root = self.root(session_id)
        turn = self.turns.get(root)
        if turn is not None:
            turn.apply(kind, props, child=root != session_id)
        # Questions involve Discord, which is slow. They are handled one at a
        # time on the side so the stream keeps being read.
        if kind == "question.asked" and self.on_question:
            self._jobs.put_nowait(self.on_question(session_id, props))
        elif kind in ("question.replied", "question.rejected") and self.on_question_closed:
            self._jobs.put_nowait(self.on_question_closed(props.get("requestID")))

    async def run_jobs(self):
        while True:
            job = await self._jobs.get()
            try:
                await job
            except Exception:
                log.exception("handling a question event")


async def follow(agent, directory, router, on_connected):
    """Reads the event stream forever, reconnecting whenever it drops."""
    delay = 1
    while True:
        try:
            async for event in agent.events(directory):
                if event.get("type") == "server.connected":
                    log.info("event stream connected")
                    delay = 1
                    on_connected()
                else:
                    router.dispatch(event)
            log.warning("event stream ended")
        except asyncio.CancelledError:
            raise
        except Exception as err:
            log.warning("event stream dropped: %s", err or type(err).__name__)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 15)


async def wait_for_turn(turn, is_working, *, time_limit, silence_limit, question_limit, question_age):
    """Waits for the agent to finish a turn.

    Returns "done", or the reason to stop it: "timeout" (worked longer than
    time_limit), "silent" (no sign of life for silence_limit) or "unanswered"
    (a question sat for question_limit).

    is_working() asks the agent whether the session is busy. question_age()
    is how long the turn's oldest open question has waited.
    """
    poll = POLL_SECONDS
    done = asyncio.create_task(turn.done.wait())
    try:
        while True:
            wake = asyncio.create_task(turn.wake.wait())
            await asyncio.wait({done, wake}, timeout=poll, return_when=asyncio.FIRST_COMPLETED)
            wake.cancel()
            if turn.done.is_set():
                return "done"
            woken = turn.wake.is_set()
            turn.wake.clear()
            if woken or turn.quiet_for() >= poll:
                # The stream may have missed the end of the work, or the work
                # may never have started (an error before the first step).
                try:
                    working = await is_working()
                except Exception as err:
                    log.warning("could not ask the agent about session %s: %s", turn.session_id, err)
                    working = True
                started = turn.active or turn.error or turn.quiet_for() >= START_GRACE_SECONDS
                if not working and started:
                    return "done"
            if turn.waiting:
                if question_age() >= question_limit:
                    return "unanswered"
            elif turn.running_for() >= time_limit:
                return "timeout"
            elif turn.quiet_for() >= silence_limit:
                return "silent"
    finally:
        done.cancel()


def parts_text(parts):
    texts = [
        p["text"]
        for p in parts or []
        if p.get("type") == "text" and not p.get("synthetic") and not p.get("ignored") and p.get("text")
    ]
    return "\n\n".join(texts).strip()


def is_request(message):
    """True for a message a person sent: a prompt or a slash command.

    The agent also writes "user" messages to itself in the middle of a request
    (after a subtask, around a compaction). Those are not requests.
    """
    if (message.get("info") or {}).get("role") != "user":
        return False
    return any(
        part.get("type") in ("subtask", "file")
        or (part.get("type") == "text" and not part.get("synthetic") and not part.get("ignored"))
        for part in message.get("parts") or []
    )


def request_text(message):
    """What a person asked for in a request, as one line."""
    for part in message.get("parts") or []:
        if part.get("type") == "subtask":
            return f"/{part.get('command') or part.get('description') or 'command'}"
    return " ".join(parts_text(message.get("parts")).split())


def turn_reply(messages, user_message_id=None):
    """(text, error) of one request in a session's recent messages, oldest first.

    A request produces several agent messages, one per step. The answer is
    the text of the last one. `error` is that message's error, if it has one.

    `user_message_id` is the message the request became. Without it (the
    bridge restarted mid-request) the latest request is used.
    """
    messages = messages or []
    ids = [(m.get("info") or {}).get("id") for m in messages]
    if user_message_id in ids:
        start = ids.index(user_message_id)
    elif user_message_id:
        start = -1  # the request began before the oldest message read, so all of these are its steps
    else:
        start = max((i for i, m in enumerate(messages) if is_request(m)), default=-1)
    steps = []
    for message in messages[start + 1 :]:
        if is_request(message):
            break  # the next request
        if (message.get("info") or {}).get("role") == "assistant":
            steps.append(message)
    if not steps:
        return "", None
    return parts_text(steps[-1].get("parts")), steps[-1]["info"].get("error")
