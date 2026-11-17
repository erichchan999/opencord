"""The agent's questions: shown with buttons, answered by people, sent back."""

import asyncio
import time
import types

import discord

from fakes import Channel, Interaction
from opencode import OpenCodeError
from questions import GONE, AnswerModal, Questions, build_view, custom_id, parse_custom_id, question_text
from state import Store

THREAD = 600
SESSION = "ses_thread"
LETTER = {
    "question": "Which letter?",
    "header": "Letter",
    "options": [{"label": "A", "description": "Choose letter A"}, {"label": "B", "description": "Choose letter B"}],
}
TOPPINGS = {
    "question": "Which toppings?",
    "header": "Toppings",
    "options": [{"label": "Cheese"}, {"label": "Ham"}, {"label": "Olives"}],
    "multiple": True,
}


def request(request_id="que_1", *questions):
    return {"id": request_id, "sessionID": SESSION, "questions": list(questions or [LETTER])}


class Agent:
    def __init__(self):
        self.replies = []
        self.rejected = []
        self.pending = []
        self.working = {SESSION: {"type": "busy"}}
        self.reply_error = None

    async def reply_question(self, request_id, directory, answers):
        if self.reply_error:
            raise self.reply_error
        self.replies.append((request_id, answers))

    async def reject_question(self, request_id, directory):
        self.rejected.append(request_id)

    async def pending_questions(self, directory):
        return self.pending

    async def statuses(self, directory):
        return self.working


class Harness:
    def __init__(self, tmp_path):
        self.store = Store(str(tmp_path / "threads.json"), "/work/sandbox")
        self.store.load()
        self.store.register_thread(THREAD, 42)["sessionId"] = SESSION
        self.agent = Agent()
        self.channel = Channel(THREAD)
        self.opened, self.closed = [], []
        self.enabled = True

        async def thread_for_session(session_id):
            return THREAD if session_id == SESSION else None

        async def home_of(channel):
            return self.channel if self.enabled else None

        self.questions = Questions(
            self.store, self.agent, "/work/sandbox",
            channel_for=lambda thread_id: self.channel,
            home_of=home_of,
            thread_for_session=thread_for_session,
            on_open=self.opened.append,
            on_close=self.closed.append,
        )

    def click(self, request_id, index, kind, option=None, values=None, text="the question"):
        """Uses a button or menu. Returns the interaction to inspect."""
        interaction = Interaction(THREAD, custom_id=custom_id(request_id, index, kind, option), values=values, message_text=text)
        assert asyncio.run(self.questions.on_component(interaction)) is True
        return interaction

    def saved(self):
        again = Store(self.store.path, "/work/sandbox")
        again.load()
        return again


def test_custom_ids_round_trip_and_reject_strangers():
    assert parse_custom_id(custom_id("que_1e3c68e73001bJJNhS452evhQl", 2, "o", 3)) == ("que_1e3c68e73001bJJNhS452evhQl", 2, "o", 3)
    assert parse_custom_id(custom_id("que_1", 0, "s")) == ("que_1", 0, "s", None)
    assert parse_custom_id(custom_id("que_1", 0, "c")) == ("que_1", 0, "c", None)
    for other in ("", None, "someone-elses-button", "q:que_1:x:o:1", "q:que_1:0:z"):
        assert parse_custom_id(other) is None
    assert len(custom_id("que_" + "x" * 40, 99, "o", 24)) <= 100  # Discord's limit


def test_question_text_in_its_three_states():
    assert question_text(LETTER) == "❓ **Which letter?**\n- **A**: Choose letter A\n- **B**: Choose letter B"
    assert question_text(TOPPINGS) == "❓ **Which toppings?**\n-# You can pick more than one."
    assert question_text(LETTER, answer=["B"], by="Erich") == "❓ **Which letter?**\n**Answer:** B (from Erich)"
    assert question_text(LETTER, note=GONE) == f"❓ **Which letter?**\n-# {GONE}"
    assert "\\*\\*bold\\*\\*" in question_text({"question": "Is **bold** ok?", "options": []})


def test_buttons_for_a_few_options_and_a_menu_for_many_or_multiple():
    async def run():
        few = build_view("que_1", 0, LETTER).children
        assert [(type(item).__name__, item.label, item.custom_id) for item in few] == [
            ("Button", "A", "q:que_1:0:o:0"),
            ("Button", "B", "q:que_1:0:o:1"),
            ("Button", "Type an answer", "q:que_1:0:c"),
        ]
        menu, typed = build_view("que_1", 1, TOPPINGS).children
        assert isinstance(menu, discord.ui.Select) and menu.custom_id == "q:que_1:1:s"
        assert (menu.min_values, menu.max_values) == (1, 3)
        assert [(option.label, option.value) for option in menu.options] == [("Cheese", "0"), ("Ham", "1"), ("Olives", "2")]
        assert typed.label == "Type an answer"

        many = {"question": "Pick", "options": [{"label": f"Option {n}"} for n in range(7)], "custom": False}
        (only,) = build_view("que_1", 0, many).children
        assert isinstance(only, discord.ui.Select) and only.max_values == 1 and len(only.options) == 7

        open_ended = build_view("que_1", 0, {"question": "Why?", "options": [], "custom": False}).children
        assert [item.label for item in open_ended] == ["Type an answer"]  # otherwise there would be no way to answer
        assert build_view("que_1", 0, LETTER).is_finished()  # so discord.py does not handle the clicks itself

    asyncio.run(run())


def test_a_question_is_posted_and_remembered(tmp_path):
    h = Harness(tmp_path)
    asyncio.run(h.questions.ask(SESSION, request("que_1", LETTER, TOPPINGS)))
    assert [text.splitlines()[0] for text in h.channel.texts()] == ["❓ **Which letter?**", "❓ **Which toppings?**"]
    assert all(view is not None for _, _, view in h.channel.sent)
    assert h.questions.open_for(THREAD) == ["que_1"] and h.opened == [THREAD]
    saved = h.saved().questions["que_1"]
    assert saved["messages"] == [9001, 9002] and saved["answers"] == [None, None] and saved["thread"] == str(THREAD)

    asyncio.run(h.questions.ask(SESSION, request("que_1", LETTER, TOPPINGS)))  # told about it twice
    assert len(h.channel.sent) == 2 and h.opened == [THREAD]


def test_questions_from_sessions_that_are_not_ours_are_left_alone(tmp_path):
    h = Harness(tmp_path)
    asyncio.run(h.questions.ask("ses_other", request()))
    assert h.channel.sent == [] and h.store.questions == {}


def test_clicking_an_option_answers_the_agent(tmp_path):
    h = Harness(tmp_path)
    asyncio.run(h.questions.ask(SESSION, request()))
    interaction = h.click("que_1", 0, "o", 1)
    assert interaction.response.edited == [("❓ **Which letter?**\n**Answer:** B (from Tester)", None)]
    assert h.agent.replies == [("que_1", [["B"]])]
    assert h.store.questions == {} and h.saved().questions == {} and h.closed == [THREAD]


def test_revocation_blocks_buttons_and_open_modals_without_closing_the_question(tmp_path):
    h = Harness(tmp_path)
    asyncio.run(h.questions.ask(SESSION, request("que_1", LETTER)))
    h.enabled = False
    button = h.click("que_1", 0, "o", option=0)
    modal = Interaction(THREAD)
    asyncio.run(h.questions.answer_typed(modal, "que_1", 0, "C"))
    for interaction in (button, modal):
        assert interaction.response.sent == [("OpenCord is disabled in this channel.", True)]
    assert h.agent.replies == [] and h.questions.open_for(THREAD) == ["que_1"]
    h.enabled = True
    h.click("que_1", 0, "o", option=0)
    assert h.agent.replies == [("que_1", [["A"]])]


def test_every_question_of_a_request_must_be_answered_before_it_is_sent(tmp_path):
    h = Harness(tmp_path)
    asyncio.run(h.questions.ask(SESSION, request("que_1", LETTER, TOPPINGS)))
    h.click("que_1", 0, "o", 0)
    assert h.agent.replies == [] and h.saved().questions["que_1"]["answers"] == [["A"], None]
    interaction = h.click("que_1", 1, "s", values=["0", "2"])
    assert interaction.response.edited[0][0].endswith("**Answer:** Cheese, Olives (from Tester)")
    assert h.agent.replies == [("que_1", [["A"], ["Cheese", "Olives"]])]
    assert h.opened == [THREAD] and h.closed == [THREAD]


def test_a_second_click_on_an_answered_question_is_turned_away(tmp_path):
    h = Harness(tmp_path)
    asyncio.run(h.questions.ask(SESSION, request("que_1", LETTER, TOPPINGS)))
    h.click("que_1", 0, "o", 0)
    interaction = h.click("que_1", 0, "o", 1)
    assert interaction.response.sent == [("That one already has an answer.", True)]
    assert h.store.questions["que_1"]["answers"][0] == ["A"]


def test_a_button_on_a_question_that_is_gone_loses_its_buttons(tmp_path):
    h = Harness(tmp_path)
    interaction = h.click("que_old", 0, "o", 0, text="❓ **An old question**")
    assert interaction.response.edited == [(f"❓ **An old question**\n-# {GONE}", None)]
    again = h.click("que_old", 0, "o", 0, text=f"❓ **An old question**\n-# {GONE}")
    assert again.response.sent == [(GONE, True)]
    assert h.agent.replies == []


def test_buttons_that_are_not_the_bridges_are_ignored(tmp_path):
    h = Harness(tmp_path)
    interaction = Interaction(THREAD, custom_id="some-other-bot")
    assert asyncio.run(h.questions.on_component(interaction)) is False
    assert not interaction.response.is_done()


def test_typing_an_answer_goes_through_a_pop_up(tmp_path):
    async def run():
        h = Harness(tmp_path)
        await h.questions.ask(SESSION, request())
        click = Interaction(THREAD, custom_id=custom_id("que_1", 0, "c"))
        await h.questions.on_component(click)
        (modal,) = click.response.modals
        assert isinstance(modal, AnswerModal) and modal.title == "Letter" and h.agent.replies == []

        submit = Interaction(THREAD, custom_id="unused", user="erich")
        await h.questions.answer_typed(submit, "que_1", 0, "  neither, use C  ")
        assert submit.response.edited == [("❓ **Which letter?**\n**Answer:** neither, use C (from Erich)", None)]
        assert h.agent.replies == [("que_1", [["neither, use C"]])]

        late = Interaction(THREAD, custom_id="unused", message_text=None)
        await h.questions.answer_typed(late, "que_1", 0, "too late")
        assert late.response.sent == [(GONE, True)] and len(h.agent.replies) == 1

    asyncio.run(run())


def message(text, author="erich"):
    return types.SimpleNamespace(channel=None, author=types.SimpleNamespace(display_name=author.title(), name=author), content=text)


def test_a_plain_message_answers_the_one_open_question(tmp_path):
    h = Harness(tmp_path)
    asyncio.run(h.questions.ask(SESSION, request()))
    sent = message("b")
    sent.channel = h.channel
    assert asyncio.run(h.questions.answer_from_message(sent, "b")) is True
    assert h.agent.replies == [("que_1", [["B"]])]  # matched to the option, whatever the case
    assert h.channel.text_of(9001) == "❓ **Which letter?**\n**Answer:** B (from Erich)"
    assert h.channel.edits[-1][2] is None  # buttons removed

    asyncio.run(h.questions.ask(SESSION, request("que_2")))
    sent = message("something else entirely")
    sent.channel = h.channel
    assert asyncio.run(h.questions.answer_from_message(sent, "something else entirely")) is True
    assert h.agent.replies[-1] == ("que_2", [["something else entirely"]])


def test_a_plain_message_is_not_an_answer_when_it_is_unclear_which_question_it_is_for(tmp_path):
    h = Harness(tmp_path)
    sent = message("hello")
    sent.channel = h.channel
    assert asyncio.run(h.questions.answer_from_message(sent, "hello")) is False  # nothing is open
    asyncio.run(h.questions.ask(SESSION, request("que_1", LETTER, TOPPINGS)))
    assert asyncio.run(h.questions.answer_from_message(sent, "A")) is False  # two questions
    h.click("que_1", 0, "o", 0)
    assert asyncio.run(h.questions.answer_from_message(sent, "Ham")) is True  # one left
    assert h.agent.replies == [("que_1", [["A"], ["Ham"]])]

    closed = {**LETTER, "custom": False}
    asyncio.run(h.questions.ask(SESSION, request("que_2", closed)))
    assert asyncio.run(h.questions.answer_from_message(sent, "neither")) is False  # only the options are allowed
    assert asyncio.run(h.questions.answer_from_message(sent, "a")) is True


def test_stopping_a_thread_dismisses_its_questions(tmp_path):
    h = Harness(tmp_path)
    asyncio.run(h.questions.ask(SESSION, request("que_1", LETTER, TOPPINGS)))
    h.click("que_1", 0, "o", 0)
    asyncio.run(h.questions.close_thread(THREAD, "Stopped before it was answered."))
    assert h.agent.rejected == ["que_1"] and h.agent.replies == []
    assert h.store.questions == {} and h.questions.open_for(THREAD) == []
    # Only the unanswered one is rewritten. The answered one keeps its answer.
    assert h.channel.edits == [(9002, "❓ **Which toppings?**\n-# Stopped before it was answered.", None)]
    assert h.closed == [THREAD]
    asyncio.run(h.questions.close_thread(THREAD))  # nothing left: no effect
    assert h.agent.rejected == ["que_1"]


def test_a_question_answered_elsewhere_is_closed_here(tmp_path):
    h = Harness(tmp_path)
    asyncio.run(h.questions.ask(SESSION, request()))
    asyncio.run(h.questions.closed("que_1"))
    assert h.channel.text_of(9001) == f"❓ **Which letter?**\n-# {GONE}"
    assert h.agent.rejected == [] and h.store.questions == {} and h.closed == [THREAD]
    asyncio.run(h.questions.closed("que_unknown"))  # the bridge's own answers come back as events too


def test_an_answer_the_agent_no_longer_wants_is_reported(tmp_path):
    h = Harness(tmp_path)
    asyncio.run(h.questions.ask(SESSION, request()))
    h.agent.reply_error = OpenCodeError("POST /question/que_1/reply -> 404", 404)
    h.click("que_1", 0, "o", 0)
    assert "already stopped waiting" in h.channel.texts()[-1]
    assert h.store.questions == {} and h.closed == [THREAD]


def test_a_question_that_cannot_be_posted_is_dismissed(tmp_path):
    h = Harness(tmp_path)
    h.channel.fail_sends = True
    asyncio.run(h.questions.ask(SESSION, request()))
    assert h.agent.rejected == ["que_1"] and h.store.questions == {}
    assert h.opened == [THREAD] and h.closed == [THREAD]


def test_after_the_stream_was_away_threads_match_what_the_agent_waits_on(tmp_path):
    h = Harness(tmp_path)
    asyncio.run(h.questions.ask(SESSION, request("que_answered_meanwhile")))
    h.agent.pending = [
        request("que_new"),  # asked while the bridge was not listening
        {"id": "que_foreign", "sessionID": "ses_other", "questions": [LETTER]},
    ]
    asyncio.run(h.questions.reconcile())
    assert h.questions.open_for(THREAD) == ["que_new"]
    assert h.channel.text_of(9001).endswith(GONE)
    assert h.channel.texts()[-1].startswith("❓ **Which letter?**") and len(h.channel.sent) == 2
    assert h.agent.rejected == []


def test_questions_shown_or_answered_while_checking_are_left_alone(tmp_path):
    h = Harness(tmp_path)
    asyncio.run(h.questions.ask(SESSION, request("que_answered_now")))
    h.agent.pending = [request("que_answered_now")]

    async def statuses(directory):
        # The stream keeps going while the bridge asks: one is asked, the other answered.
        await h.questions.ask(SESSION, request("que_asked_now"))
        await h.questions.on_component(Interaction(THREAD, custom_id=custom_id("que_answered_now", 0, "o", 0)))
        return h.agent.working

    h.agent.statuses = statuses
    sent = len(h.channel.sent)
    asyncio.run(h.questions.reconcile())
    assert h.questions.open_for(THREAD) == ["que_asked_now"]  # not taken for one the agent stopped waiting on
    assert len(h.channel.sent) == sent + 1  # the answered one is not shown again


def test_a_question_left_by_a_request_that_ended_is_dismissed_not_shown(tmp_path):
    h = Harness(tmp_path)
    h.agent.pending = [request("que_stale")]
    h.agent.working = {}
    asyncio.run(h.questions.reconcile())
    assert h.agent.rejected == ["que_stale"] and h.channel.sent == [] and h.store.questions == {}


def test_oldest_age(tmp_path):
    h = Harness(tmp_path)
    assert h.questions.oldest_age(THREAD) == 0
    asyncio.run(h.questions.ask(SESSION, request("que_1")))
    asyncio.run(h.questions.ask(SESSION, request("que_2")))
    h.store.questions["que_1"]["askedAt"] = time.time() - 500
    assert 499 < h.questions.oldest_age(THREAD) < 502
