from __future__ import annotations

import discord


def guild_from_message(message: discord.Message) -> discord.Guild:
    if message.guild is None:
        raise ValueError("Music tools can only be used in a server.")
    return message.guild


def voice_channel_from_user(
    user: discord.abc.User,
) -> discord.VoiceChannel | discord.StageChannel:
    if not isinstance(user, discord.Member):
        raise ValueError("Music can only be played for a server member.")

    voice_channel = user.voice.channel if user.voice else None
    if not isinstance(
        voice_channel,
        (discord.VoiceChannel, discord.StageChannel),
    ):
        raise ValueError("You need to be in a voice channel first.")
    return voice_channel
