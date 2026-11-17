"""Where the bot works.

The bot works in a server text channel only when that channel's own
permissions name it: an allow for View Channel on the bot itself or on one of
its roles other than @everyone. Bots get @everyone's view of every public
channel, so seeing a channel is not enough. A channel created later stays off
until someone adds the bot to it. Threads go by their parent channel.

The README's setup also blocks the bot from viewing every other category, so
it does not even read them. This check is what holds when a spot is missed.
Adding the bot to a category instead adds it to every channel synced with
that category.
"""

# What the bot needs in a channel it works in. The startup log names any that are missing.
NEEDED = (
    "view_channel",
    "send_messages",
    "read_message_history",
    "create_public_threads",
    "send_messages_in_threads",
    "add_reactions",
    "attach_files",
)


def added_here(channel):
    """True when `channel` (a server text channel) names the bot in its permissions and lets it see the channel."""
    me = getattr(getattr(channel, "guild", None), "me", None)
    if me is None:
        return False
    named = [me, *(role for role in me.roles if not role.is_default())]
    if not any(channel.overwrites_for(target).view_channel for target in named):
        return False
    # A deny elsewhere (on the bot itself, say) still wins.
    return channel.permissions_for(me).view_channel


def missing(channel):
    """The NEEDED permissions the bot lacks in `channel`."""
    granted = channel.permissions_for(channel.guild.me)
    return [name for name in NEEDED if not getattr(granted, name)]


def channels_added(guild):
    """The server's text channels the bot works in."""
    return [channel for channel in guild.text_channels if added_here(channel)]


def seen_but_not_added(guild):
    """Text channels the bot can read but ignores. The README's setup blocks
    the bot from viewing them, so these point to a gap in that setup."""
    return [
        channel
        for channel in guild.text_channels
        if channel.permissions_for(guild.me).view_channel and not added_here(channel)
    ]
