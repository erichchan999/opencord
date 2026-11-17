import types

import discord

import access


def channel(granted):
    return types.SimpleNamespace(
        id=22, guild=types.SimpleNamespace(id=11, me=object()), permissions_for=lambda member: granted
    )


def test_channel_requires_both_configuration_and_discord_visibility():
    visible = channel(discord.Permissions(view_channel=True))
    hidden = channel(discord.Permissions.none())
    assert not access.enabled(visible, set())
    assert not access.enabled(visible, {"99"})
    assert access.enabled(visible, {"22"})
    assert not access.enabled(hidden, {"22"})


def test_missing_names_what_the_bot_lacks():
    granted = discord.Permissions(view_channel=True, send_messages=True, read_message_history=True)
    assert access.missing(channel(granted)) == [
        "create_public_threads", "send_messages_in_threads", "add_reactions", "attach_files",
    ]
