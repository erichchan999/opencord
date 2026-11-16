"""Sample agent responses shared by the tests, shaped like OpenCode 1.19.5's."""

from catalog import Snapshot, parse_agents, parse_models

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


def snapshot():
    return Snapshot(models=parse_models(PROVIDERS), agents=parse_agents(AGENTS), default_model=FLASH)
