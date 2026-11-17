"""Which channels the bot works in: only ones whose permissions name it."""

import types

import discord

import access


class Role:
    def __init__(self, default=False):
        self._default = default

    def is_default(self):
        return self._default


EVERYONE, BOT_ROLE, OTHER_ROLE = Role(default=True), Role(), Role()
ALL_NEEDED = discord.Permissions(**{name: True for name in access.NEEDED})


class Channel:
    def __init__(self, *overwrites, granted=ALL_NEEDED, name="agent"):
        self.name = name
        self.me = types.SimpleNamespace(roles=[EVERYONE, BOT_ROLE])
        self.guild = types.SimpleNamespace(me=self.me)
        self._overwrites = list(overwrites)  # (target, discord.PermissionOverwrite)
        self._granted = granted

    def overwrites_for(self, target):
        for who, overwrite in self._overwrites:
            if (self.me if who == "me" else who) is target:
                return overwrite
        return discord.PermissionOverwrite()

    def permissions_for(self, member):
        assert member is self.me
        return self._granted


VIEW = discord.PermissionOverwrite(view_channel=True)


def test_seeing_a_channel_through_everyone_is_not_enough():
    assert not access.added_here(Channel())
    assert not access.added_here(Channel((EVERYONE, VIEW)))
    assert not access.added_here(Channel((OTHER_ROLE, VIEW)))  # a role the bot does not have
    assert not access.added_here(Channel(("me", discord.PermissionOverwrite(send_messages=True))))


def test_a_channel_that_names_the_bot_or_its_role_counts():
    assert access.added_here(Channel(("me", VIEW)))
    assert access.added_here(Channel((BOT_ROLE, VIEW)))


def test_a_deny_that_wins_still_keeps_the_bot_out():
    assert not access.added_here(Channel((BOT_ROLE, VIEW), granted=discord.Permissions.none()))


def test_a_channel_outside_a_server_never_counts():
    assert not access.added_here(types.SimpleNamespace(guild=None))


def test_missing_names_what_the_bot_lacks():
    granted = discord.Permissions(view_channel=True, send_messages=True, read_message_history=True)
    assert access.missing(Channel(("me", VIEW), granted=granted)) == [
        "create_public_threads", "send_messages_in_threads", "add_reactions", "attach_files",
    ]
    assert access.missing(Channel(("me", VIEW))) == []


def test_the_server_is_split_into_added_and_merely_seen_channels():
    added, seen = Channel((BOT_ROLE, VIEW), name="agent"), Channel(name="general")
    blocked = Channel((BOT_ROLE, discord.PermissionOverwrite(view_channel=False)), granted=discord.Permissions.none())
    guild = types.SimpleNamespace(text_channels=[seen, added, blocked], me=added.me)
    for channel in guild.text_channels:
        channel.me = channel.guild.me = guild.me
    assert access.channels_added(guild) == [added]
    assert access.seen_but_not_added(guild) == [seen]
