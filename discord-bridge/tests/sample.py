"""Sample agent responses shared by the tests, shaped like OpenCode 1.19.5's."""

import json
import pathlib

from catalog import Snapshot, parse_agents, parse_commands, parse_models

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def recorded(name):
    """The events of tests/fixtures/events-<name>.jsonl, recorded from a real server."""
    lines = (FIXTURES / f"events-{name}.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def root_session(events):
    """The session a person would be talking to: the first one created without a parent."""
    for event in events:
        info = (event.get("properties") or {}).get("info") or {}
        if event.get("type") == "session.created" and not info.get("parentID"):
            return info["id"]
    raise AssertionError("no session.created in the recording")


def messages_from(events, session_id):
    """What GET /session/<id>/message would return once these events have happened."""
    infos, parts = {}, {}
    for event in events:
        props = event.get("properties") or {}
        if props.get("sessionID") != session_id:
            continue
        if event.get("type") == "message.updated":
            infos[props["info"]["id"]] = props["info"]
        elif event.get("type") == "message.part.updated":
            part = props["part"]
            parts.setdefault(part["messageID"], {})[part["id"]] = part
    return [{"info": info, "parts": list(parts.get(message_id, {}).values())} for message_id, info in infos.items()]

FLASH = "deepseek-proxy/deepseek-flash"
PRO = "deepseek-proxy/deepseek-v4-pro"

# GET /provider. Only "deepseek-proxy" is connected; Pro has no effort levels
# here so the tests can cover a model without any.
PROVIDERS = {
    "all": [
        {
            "id": "deepseek-proxy",
            "models": {
                "deepseek-flash": {
                    "name": "DeepSeek Flash",
                    "limit": {"context": 128000, "output": 8192},
                    "variants": {
                        "max": {"reasoningEffort": "max"},
                        "off": {"thinking": {"type": "disabled"}},
                        "medium": {"disabled": True},
                        "low": {"reasoningEffort": "low"},
                        "high": {"reasoningEffort": "high"},
                    },
                },
                "deepseek-v4-pro": {"name": "DeepSeek V4 Pro", "limit": {"context": 128000}, "variants": {}},
            },
        },
        {"id": "opencode", "models": {"free-model": {"name": "Free", "variants": {}}}},
    ],
    "connected": ["deepseek-proxy"],
}

# GET /agent
AGENTS = [
    {"name": "build", "mode": "primary", "description": "The default agent."},
    {"name": "plan", "mode": "primary", "description": "Plan mode."},
    {"name": "title", "mode": "primary", "hidden": True},
    {"name": "explore", "mode": "subagent"},
]


# GET /command
COMMANDS = [
    {"name": "init", "description": "guided AGENTS.md setup", "source": "command", "hints": ["$ARGUMENTS"]},
    {"name": "review", "description": "review changes [commit|branch|pr], defaults to uncommitted", "source": "command", "hints": ["$ARGUMENTS"]},
    {"name": "customize-opencode", "description": "Change OpenCode's own settings.", "source": "skill", "hints": []},
    {"description": "a command without a name is skipped"},
]


def snapshot():
    return Snapshot(
        models=parse_models(PROVIDERS), agents=parse_agents(AGENTS), commands=parse_commands(COMMANDS), default_model=FLASH
    )
