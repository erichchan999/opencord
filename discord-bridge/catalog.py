"""What the agent offers right now: models with their effort levels, and agents.

Read from the OpenCode server rather than configured here, so the slash
commands always list what opencode.json actually provides.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("bridge")

# Display order for effort levels. Names not listed here sort after these.
EFFORT_ORDER = ("off", "none", "minimal", "low", "medium", "high", "xhigh", "max")
# Typed instead of a name to clear a setting.
DEFAULT_WORD = "default"


def effort_rank(name):
    return (EFFORT_ORDER.index(name), "") if name in EFFORT_ORDER else (len(EFFORT_ORDER), name)


def parse_models(providers):
    """GET /provider -> {"<provider>/<model>": {"name", "efforts", "context"}}.

    Only connected providers count: those are the ones a prompt can use.
    """
    connected = set(providers.get("connected") or [])
    models = {}
    for provider in providers.get("all") or []:
        if provider.get("id") not in connected:
            continue
        for model_id, model in (provider.get("models") or {}).items():
            variants = model.get("variants") or {}
            efforts = [name for name, variant in variants.items() if not (variant or {}).get("disabled")]
            models[f"{provider['id']}/{model_id}"] = {
                "name": model.get("name") or model_id,
                "efforts": sorted(efforts, key=effort_rank),
                "context": (model.get("limit") or {}).get("context"),
            }
    return models


def parse_agents(agents):
    """GET /agent -> {name: description} for the agents a person can pick."""
    return {
        agent["name"]: agent.get("description") or ""
        for agent in agents or []
        if agent.get("name") and agent.get("mode") in ("primary", "all") and not agent.get("hidden")
    }


def parse_commands(commands):
    """GET /command -> {name: description}."""
    return {command["name"]: command.get("description") or "" for command in commands or [] if command.get("name")}


def split_model(key):
    provider_id, _, model_id = key.partition("/")
    return {"providerID": provider_id, "modelID": model_id}


@dataclass
class Snapshot:
    models: dict = field(default_factory=dict)
    agents: dict = field(default_factory=dict)
    commands: dict = field(default_factory=dict)  # the agent's own slash commands and skills
    default_model: str | None = None
    default_agent: str = "build"

    def model_name(self, key):
        return self.models.get(key, {}).get("name") or key or "the agent's own choice"

    def find_model(self, text):
        """Matches what someone typed to a model key: the key itself, a model id, or a display name."""
        wanted = text.strip().lower()
        if not wanted:
            return None
        exact = [key for key in self.models if key.lower() == wanted]
        loose = [
            key
            for key, model in self.models.items()
            if key.partition("/")[2].lower() == wanted or model["name"].lower() == wanted
        ]
        matches = exact or loose
        return matches[0] if len(matches) == 1 else None

    def efforts_for(self, settings):
        """The effort levels of the model these settings end up on."""
        key = settings.get("model")
        if key not in self.models:
            key = self.default_model
        return self.models.get(key, {}).get("efforts", [])

    def resolve(self, settings):
        """Checks a thread's settings against what the agent offers now.

        Returns (options, dropped): the fields to add to a prompt, and the
        names of settings that no longer exist and were left out.
        """
        options, dropped = {}, []
        key = settings.get("model")
        if key and key not in self.models:
            dropped.append("model")
            key = None
        # Sent even when the thread has no model of its own, so an effort
        # level always applies to the model it was checked against.
        key = key or self.default_model
        if key in self.models:
            options["model"] = split_model(key)
        effort = settings.get("effort")
        if effort:
            if effort in self.models.get(key, {}).get("efforts", []):
                options["variant"] = effort
            else:
                dropped.append("effort")
        agent = settings.get("agent")
        if agent:
            if agent in self.agents:
                options["agent"] = agent
            else:
                dropped.append("agent")
        return options, dropped


class Catalog:
    """Fetches a Snapshot from the agent and keeps it for a short while.

    A stale snapshot is returned at once and refreshed in the background, so a
    slash command never waits on the agent once the first fetch has worked.
    """

    def __init__(self, agent, directory, ttl=60):
        self._agent = agent
        self._directory = directory
        self._ttl = ttl
        self._snapshot = None
        self._fetched_at = 0.0
        self._refreshing = None

    async def _fetch(self):
        providers, config, agents, commands = await asyncio.gather(
            self._agent.providers(self._directory),
            self._agent.config(self._directory),
            self._agent.agents(self._directory),
            self._agent.commands(self._directory),
        )
        models = parse_models(providers or {})
        default_model = (config or {}).get("model")
        if default_model not in models:
            default_model = None
        self._snapshot = Snapshot(
            models=models,
            agents=parse_agents(agents),
            commands=parse_commands(commands),
            default_model=default_model,
            # OpenCode uses "build" unless its config names another agent.
            default_agent=(config or {}).get("default_agent") or "build",
        )
        self._fetched_at = time.monotonic()
        return self._snapshot

    async def _refresh(self):
        try:
            await self._fetch()
        except Exception as err:
            log.error("could not refresh the model list: %s", err)
        finally:
            self._refreshing = None

    async def get(self):
        if self._snapshot is None:
            return await self._fetch()
        if time.monotonic() - self._fetched_at > self._ttl and self._refreshing is None:
            self._refreshing = asyncio.create_task(self._refresh())
        return self._snapshot
