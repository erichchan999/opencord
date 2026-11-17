import asyncio

from catalog import Catalog, model_key, parse_agents, parse_commands, parse_models, split_model
from sample import AGENTS, COMMANDS, FLASH, PRO, PROVIDERS, snapshot


def test_only_connected_providers_are_offered():
    assert list(parse_models(PROVIDERS)) == [FLASH, PRO]


def test_effort_levels_are_ordered_and_skip_disabled_ones():
    models = parse_models(PROVIDERS)
    assert models[FLASH]["efforts"] == ["off", "low", "high", "max"]
    assert models[PRO]["efforts"] == []
    assert models[FLASH]["context"] == 128000 and models[FLASH]["name"] == "DeepSeek Flash"


def test_only_agents_a_person_can_pick_are_offered():
    assert parse_agents(AGENTS) == {"build": "The default agent.", "plan": "Plan mode."}


def test_split_model_keeps_slashes_in_the_model_id():
    assert split_model("router/vendor/model") == {"providerID": "router", "modelID": "vendor/model"}


def test_find_model_accepts_key_id_or_name_in_any_case():
    snap = snapshot()
    assert snap.find_model(FLASH) == FLASH
    assert snap.find_model(" DEEPSEEK-V4-PRO ") == PRO
    assert snap.find_model("deepseek flash") == FLASH
    assert snap.find_model("gpt") is None
    assert snap.find_model("") is None


def test_no_settings_still_names_the_default_model():
    assert snapshot().resolve({}) == ({"model": split_model(FLASH)}, [])


def test_settings_become_prompt_fields():
    options, dropped = snapshot().resolve({"model": FLASH, "effort": "max", "agent": "plan"})
    assert options == {"model": split_model(FLASH), "variant": "max", "agent": "plan"}
    assert dropped == []


def test_effort_is_checked_against_the_default_model_when_the_thread_has_none():
    assert snapshot().resolve({"effort": "low"}) == ({"model": split_model(FLASH), "variant": "low"}, [])


def test_settings_the_agent_no_longer_offers_are_dropped():
    options, dropped = snapshot().resolve({"model": "deepseek-proxy/gone", "effort": "max", "agent": "gone"})
    assert options == {"model": split_model(FLASH), "variant": "max"}
    assert dropped == ["model", "agent"]
    assert snapshot().resolve({"model": PRO, "effort": "max"}) == ({"model": split_model(PRO)}, ["effort"])


def test_efforts_for_follows_the_model_the_settings_end_up_on():
    snap = snapshot()
    assert snap.efforts_for({}) == ["off", "low", "high", "max"]
    assert snap.efforts_for({"model": PRO}) == []
    assert snap.efforts_for({"model": "deepseek-proxy/gone"}) == ["off", "low", "high", "max"]


class FakeAgent:
    def __init__(self):
        self.calls = 0
        self.fail = False

    async def providers(self, directory):
        self.calls += 1
        if self.fail:
            raise OSError("agent is down")
        return PROVIDERS

    async def config(self, directory):
        return {"model": FLASH}

    async def agents(self, directory):
        return AGENTS

    async def commands(self, directory):
        return COMMANDS


def test_parse_commands_keeps_named_ones_with_their_descriptions():
    assert parse_commands(COMMANDS) == {
        "init": "guided AGENTS.md setup",
        "review": "review changes [commit|branch|pr], defaults to uncommitted",
        "customize-opencode": "Change OpenCode's own settings.",
    }
    assert parse_commands(None) == {}


def test_catalog_reads_once_then_serves_from_memory():
    async def run():
        agent = FakeAgent()
        catalog = Catalog(agent, "/work/sandbox", ttl=60)
        first = await catalog.get()
        second = await catalog.get()
        assert first is second and agent.calls == 1
        assert first.default_model == FLASH and first.default_agent == "build"
        assert list(first.commands) == ["init", "review", "customize-opencode"]

    asyncio.run(run())


def test_callers_waiting_on_the_first_read_share_it_and_a_failed_one_is_tried_again():
    async def run():
        agent = FakeAgent()
        catalog = Catalog(agent, "/work/sandbox", ttl=60)
        agent.fail = True
        results = await asyncio.gather(catalog.get(), catalog.get(), catalog.get(), return_exceptions=True)
        assert all(isinstance(result, OSError) for result in results) and agent.calls == 1
        agent.fail = False
        first, second = await asyncio.gather(catalog.get(), catalog.get())
        assert first is second and agent.calls == 2

    asyncio.run(run())


def test_stale_catalog_answers_at_once_and_refreshes_behind():
    async def run():
        agent = FakeAgent()
        catalog = Catalog(agent, "/work/sandbox", ttl=0)
        first = await catalog.get()
        stale = await catalog.get()  # returns the old snapshot, starts a refresh
        assert stale is first
        await asyncio.sleep(0.01)
        assert agent.calls == 2 and await catalog.get() is not first

    asyncio.run(run())


def test_failed_refresh_keeps_the_last_good_snapshot():
    async def run():
        agent = FakeAgent()
        catalog = Catalog(agent, "/work/sandbox", ttl=0)
        first = await catalog.get()
        agent.fail = True
        await catalog.get()
        await asyncio.sleep(0.01)
        assert await catalog.get() is first

    asyncio.run(run())


def test_a_failing_command_list_leaves_the_rest_of_the_catalog_working():
    async def run():
        agent = FakeAgent()

        async def commands(directory):
            raise OSError("GET /command -> 500")

        agent.commands = commands
        snap = await Catalog(agent, "/work/sandbox").get()
        assert snap.commands == {} and list(snap.models) == [FLASH, PRO] and snap.default_model == FLASH

    asyncio.run(run())


def test_model_key_is_the_inverse_of_split_model():
    assert model_key(split_model(FLASH)) == FLASH


def test_default_model_missing_from_the_list_is_not_used():
    async def run():
        agent = FakeAgent()

        async def config(directory):
            return {"model": "deepseek-proxy/renamed"}

        agent.config = config
        snap = await Catalog(agent, "/work/sandbox").get()
        assert snap.default_model is None
        assert snap.resolve({"effort": "max"}) == ({}, ["effort"])

    asyncio.run(run())
