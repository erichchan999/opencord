"""Client for the OpenCode server's HTTP API, used by the bridge."""

import asyncio
import json
import logging

import aiohttp

log = logging.getLogger("bridge")

# The server sends a heartbeat on the event stream every 10 seconds. A read
# that waits longer than this means the connection is dead.
EVENT_READ_TIMEOUT = 35


class OpenCodeError(Exception):
    """The OpenCode server answered with an HTTP error status."""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class EventParser:
    """Turns the bytes of a text/event-stream body into the JSON payload of each event."""

    def __init__(self):
        self._buffer = b""
        self._data = []

    def feed(self, chunk):
        """Takes the next bytes read and returns the events they complete."""
        events = []
        *lines, self._buffer = (self._buffer + chunk).split(b"\n")
        for line in lines:
            line = line.rstrip(b"\r")
            if line.startswith(b"data:"):
                self._data.append(line[5:].removeprefix(b" "))
            elif not line and self._data:  # a blank line ends the event
                payload = b"\n".join(self._data)
                self._data = []
                try:
                    events.append(json.loads(payload))
                except ValueError:
                    log.warning("skipped an event that is not JSON (%d bytes)", len(payload))
            # Other fields (event:, id:, comments) are not used by this stream.
        return events


class OpenCode:
    def __init__(self, url, password):
        self._url = url.rstrip("/")
        self._auth = aiohttp.BasicAuth("opencode", password)
        self._http = None

    async def __aenter__(self):
        self._http = aiohttp.ClientSession(auth=self._auth)
        return self

    async def __aexit__(self, *exc):
        await self._http.close()

    async def request(self, method, path, *, directory=None, params=None, body=None, timeout=60):
        """Calls the API. `timeout` is a deadline in seconds for the whole call."""
        params = dict(params or {})
        if directory:
            params["directory"] = directory
        try:
            async with self._http.request(
                method,
                self._url + path,
                params=params,
                json=body,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as res:
                text = await res.text()
        except TimeoutError:
            raise TimeoutError(f"{method} {path} timed out after {round(timeout)}s") from None
        if res.status >= 400:
            raise OpenCodeError(f"{method} {path} -> {res.status}: {text[:400]}", res.status)
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    async def wait_until_ready(self):
        attempt = 0
        while True:
            attempt += 1
            try:
                await self.request("GET", "/global/health", timeout=5)
                return
            except (OSError, aiohttp.ClientError, OpenCodeError) as err:
                if attempt % 10 == 1:
                    log.info("waiting for opencode-agent (%s)", err)
                await asyncio.sleep(3)

    # ------------------------------------------------------------ sessions

    async def create_session(self, directory, title):
        session = await self.request("POST", "/session", directory=directory, body={"title": title})
        return session["id"]

    async def session(self, session_id, directory):
        return await self.request("GET", f"/session/{session_id}", directory=directory, timeout=10)

    async def statuses(self, directory):
        """{session id: status} for the sessions that are working. Idle ones are left out."""
        return await self.request("GET", "/session/status", directory=directory, timeout=10) or {}

    async def prompt_async(self, session_id, directory, body):
        """Hands a prompt to the agent and returns at once. The work shows up on the event stream."""
        await self.request("POST", f"/session/{session_id}/prompt_async", directory=directory, body=body, timeout=30)

    async def abort(self, session_id, directory):
        await self.request("POST", f"/session/{session_id}/abort", directory=directory, timeout=10)

    async def recent_messages(self, session_id, directory, limit):
        """The session's last `limit` messages, oldest first."""
        return await self.request(
            "GET", f"/session/{session_id}/message", directory=directory, params={"limit": limit}, timeout=10
        )

    async def summarize(self, session_id, directory, model, timeout):
        """Replaces the conversation so far with a summary. `model` is {"providerID", "modelID"}."""
        await self.request(
            "POST", f"/session/{session_id}/summarize", directory=directory, body=model, timeout=timeout
        )

    async def messages(self, session_id, directory):
        """Every message of the session, oldest first."""
        return await self.request("GET", f"/session/{session_id}/message", directory=directory, timeout=30) or []

    async def todos(self, session_id, directory):
        return await self.request("GET", f"/session/{session_id}/todo", directory=directory, timeout=10) or []

    async def delete_session(self, session_id, directory):
        await self.request("DELETE", f"/session/{session_id}", directory=directory, timeout=30)

    async def rename(self, session_id, directory, title):
        await self.request("PATCH", f"/session/{session_id}", directory=directory, body={"title": title}, timeout=10)

    async def diff(self, session_id, directory, message_id):
        """The files one request changed, each {"file", "status", "additions", "deletions", "patch"}.

        `message_id` is the request's user message. Without one the server returns nothing.
        """
        return await self.request(
            "GET", f"/session/{session_id}/diff", directory=directory, params={"messageID": message_id}, timeout=30
        ) or []

    async def revert(self, session_id, directory, message_id):
        """Takes back a request and everything after it: out of the context, and its file changes undone.

        The server accepts any message id without complaint, so pass one that exists.
        """
        return await self.request(
            "POST", f"/session/{session_id}/revert", directory=directory, body={"messageID": message_id}, timeout=60
        )

    async def unrevert(self, session_id, directory):
        """Brings back everything revert took away. Only possible until the next prompt."""
        return await self.request("POST", f"/session/{session_id}/unrevert", directory=directory, timeout=60)

    async def fork(self, session_id, directory, before=None):
        """Copies the session with its conversation. Returns the new session.

        With `before` (a message id), only the messages before that one are copied.
        """
        body = {"messageID": before} if before else {}
        return await self.request("POST", f"/session/{session_id}/fork", directory=directory, body=body, timeout=30)

    async def shell(self, session_id, directory, agent, command, timeout, model=None):
        """Runs a shell command in the session and waits for it. Returns the message holding its output.

        `model` is {"providerID", "modelID"}, recorded on that message.
        """
        body = {"agent": agent, "command": command}
        if model:
            body["model"] = model
        return await self.request(
            "POST", f"/session/{session_id}/shell", directory=directory, body=body, timeout=timeout
        )

    async def command(self, session_id, directory, body, timeout):
        """Runs one of the agent's slash commands and waits for it to finish.

        `body` holds "command" and "arguments", and optionally "model"
        ("<provider>/<model>"), "variant" and "agent". The work shows up on
        the event stream like a prompt's.
        """
        return await self.request(
            "POST", f"/session/{session_id}/command", directory=directory, body=body, timeout=timeout
        )

    # ------------------------------------------------------------ what the agent offers

    async def providers(self, directory):
        """Providers with their models. `connected` names the ones a prompt can use."""
        return await self.request("GET", "/provider", directory=directory, timeout=10)

    async def config(self, directory):
        return await self.request("GET", "/config", directory=directory, timeout=10)

    async def agents(self, directory):
        return await self.request("GET", "/agent", directory=directory, timeout=10)

    async def commands(self, directory):
        """The agent's own slash commands and skills, each {"name", "description", "source"}."""
        return await self.request("GET", "/command", directory=directory, timeout=10) or []

    # ------------------------------------------------------------ questions from the agent

    async def pending_questions(self, directory):
        """Question requests the agent is waiting on, each {"id", "sessionID", "questions"}."""
        return await self.request("GET", "/question", directory=directory, timeout=10) or []

    async def reply_question(self, request_id, directory, answers):
        """`answers` holds one list of chosen labels (or typed text) per question, in order."""
        await self.request(
            "POST", f"/question/{request_id}/reply", directory=directory, body={"answers": answers}, timeout=10
        )

    async def reject_question(self, request_id, directory):
        """Dismisses a question. The agent stops the request it was asked in."""
        await self.request("POST", f"/question/{request_id}/reject", directory=directory, timeout=10)

    # ------------------------------------------------------------ event stream

    async def events(self, directory):
        """Yields events from the agent until the connection ends or goes quiet.

        The first event is "server.connected". Events that happen while
        nobody is connected are not replayed.
        """
        parser = EventParser()
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=EVENT_READ_TIMEOUT)
        async with self._http.get(self._url + "/event", params={"directory": directory}, timeout=timeout) as res:
            if res.status >= 400:
                raise OpenCodeError(f"GET /event -> {res.status}", res.status)
            # Read in raw chunks: one event can be far longer than the line
            # length aiohttp's line reader accepts.
            async for chunk in res.content.iter_any():
                for event in parser.feed(chunk):
                    yield event
