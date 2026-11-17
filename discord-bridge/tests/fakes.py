"""Stand-ins for Discord objects, shared by the tests."""

import types

import discord


class Missing(discord.NotFound):
    """What Discord raises for a message that has been deleted."""

    def __init__(self):
        Exception.__init__(self, "Unknown Message")


class Channel:
    """A thread: records what is sent to it and how its messages are edited."""

    def __init__(self, channel_id=600, name="a thread"):
        self.id = channel_id
        self.name = name
        self.sent = []  # (message id, text, view)
        self.files = []  # (message id, file name, bytes)
        self.edits = []  # (message id, text, view) with view "unchanged" when not given
        self.deleted = set()
        self.fail_sends = False
        self.fail_edit = None  # an exception for edit() to raise
        self._next_id = 9000

    async def send(self, content=None, *, view=None, file=None, files=None, allowed_mentions=None, suppress_embeds=False):
        if self.fail_sends:
            raise discord.HTTPException(types.SimpleNamespace(status=500, reason="boom"), "boom")
        self._next_id += 1
        self.sent.append((self._next_id, content, view))
        for one in ([file] if file is not None else []) + list(files or []):
            self.files.append((self._next_id, one.filename, one.fp.getvalue()))
        return types.SimpleNamespace(id=self._next_id)

    async def edit(self, *, name):
        """Renames the thread."""
        if self.fail_edit:
            raise self.fail_edit
        self.name = name

    def get_partial_message(self, message_id):
        channel = self

        class Partial:
            async def edit(self, *, content=None, view="unchanged", allowed_mentions=None):
                if message_id in channel.deleted:
                    raise Missing()
                channel.edits.append((message_id, content, view))

        return Partial()

    async def typing(self):
        pass

    def text_of(self, message_id):
        """The message's text after every edit so far."""
        texts = [text for mid, text, _ in self.sent if mid == message_id] + [text for mid, text, _ in self.edits if mid == message_id]
        return texts[-1]

    def texts(self):
        return [text for _, text, _ in self.sent]


class Response:
    def __init__(self):
        self.sent = []  # (text, ephemeral)
        self.edited = []  # (text, view)
        self.modals = []
        self.deferred = None

    async def send_message(self, text, *, ephemeral=False, allowed_mentions=None):
        self.sent.append((text, ephemeral))

    async def edit_message(self, *, content=None, view="unchanged", allowed_mentions=None):
        self.edited.append((content, view))

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def defer(self, *, ephemeral=False):
        self.deferred = ephemeral

    def is_done(self):
        return bool(self.sent or self.edited or self.modals) or self.deferred is not None


class Followup:
    def __init__(self):
        self.sent = []  # (text, ephemeral)
        self.views = []  # the view sent with each message, or None
        self.files = []  # (file name, bytes)
        self.fail = False

    async def send(self, text, *, ephemeral=False, allowed_mentions=None, view=None, file=None):
        if self.fail:  # the interaction expired
            raise discord.HTTPException(types.SimpleNamespace(status=401, reason="expired"), "expired")
        self.sent.append((text, ephemeral))
        self.views.append(view)
        if file is not None:
            self.files.append((file.filename, file.fp.getvalue()))


class Interaction:
    """A slash command, button click or menu choice made in `channel_id`."""

    def __init__(self, channel_id, *, custom_id=None, values=None, message_text=None, user="tester", channel=None):
        self.channel_id = channel_id
        self.channel = channel or types.SimpleNamespace(id=channel_id)
        self.user = types.SimpleNamespace(name=user, display_name=user.title(), id=42)
        self.response = Response()
        self.followup = Followup()
        self.client = None
        self.command = None
        self.type = discord.InteractionType.component if custom_id else discord.InteractionType.application_command
        self.data = {"custom_id": custom_id, "values": values or []} if custom_id else {}
        self.message = types.SimpleNamespace(content=message_text)
