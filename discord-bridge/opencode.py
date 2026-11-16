"""Client for the OpenCode server's HTTP API, used by the bridge."""

import asyncio
import json
import logging

import aiohttp

log = logging.getLogger("bridge")


class OpenCodeError(Exception):
    """The OpenCode server answered with an HTTP error status."""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class PromptTimedOut(Exception):
    """A prompt ran past its deadline. `aborted` says whether the agent was stopped."""

    def __init__(self, message, aborted):
        super().__init__(message)
        self.aborted = aborted


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

    async def request(self, method, path, *, directory=None, body=None, timeout=60):
        """Calls the API. `timeout` is a deadline in seconds for the whole call."""
        params = {"directory": directory} if directory else None
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

    async def create_session(self, directory, title):
        session = await self.request("POST", "/session", directory=directory, body={"title": title})
        return session["id"]

    async def abort(self, session_id, directory):
        await self.request("POST", f"/session/{session_id}/abort", directory=directory, timeout=10)

    async def prompt(self, session_id, directory, body, timeout):
        """Sends one prompt and waits for the agent's reply.

        If the call fails without an HTTP status (deadline passed, connection
        dropped), the agent may still be working with nobody waiting for the
        answer, so it is told to stop.
        """
        try:
            return await self.request(
                "POST", f"/session/{session_id}/message", directory=directory, body=body, timeout=timeout
            )
        except (TimeoutError, aiohttp.ClientError) as err:
            try:
                await self.abort(session_id, directory)
                aborted = True
            except Exception as abort_err:
                aborted = False
                log.error("could not stop session %s: %s", session_id, abort_err)
            if isinstance(err, TimeoutError):
                raise PromptTimedOut(str(err), aborted) from err
            raise
