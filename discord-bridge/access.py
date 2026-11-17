"""OpenCord's channel list controls input. Discord controls message delivery."""

NEEDED = (
    "view_channel",
    "send_messages",
    "read_message_history",
    "create_public_threads",
    "send_messages_in_threads",
    "add_reactions",
    "attach_files",
)


def visible(channel):
    me = channel.guild.me
    return me is not None and channel.permissions_for(me).view_channel


def enabled(channel, channel_ids):
    """The channel is configured and Discord lets the bot see it."""
    return str(channel.id) in channel_ids and visible(channel)


def missing(channel):
    """The NEEDED permissions the bot lacks in this text channel."""
    me = channel.guild.me
    if me is None:
        return list(NEEDED)
    granted = channel.permissions_for(me)
    return [name for name in NEEDED if not getattr(granted, name)]
