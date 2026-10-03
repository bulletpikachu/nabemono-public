from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.music.context import voice_channel_from_user
from src.music.models import EnqueueResult, YoutubeSelection
from src.music.player import MusicManager, music_manager
from src.music.service import enqueue_youtube, session_web_url
from src.music.youtube import MAX_PLAYLIST_TRACKS, is_youtube_url

logger = logging.getLogger(__name__)


def play_confirmation(
    result: EnqueueResult,
    selection: YoutubeSelection,
) -> str:
    if result.count == 1:
        message = (
            "Playing..."
            if result.started
            else f"Added to the queue at position {result.position}."
        )
    else:
        title = selection.playlist_title
        source = (
            f" from **{discord.utils.escape_markdown(title)[:200]}**"
            if title
            else " from the playlist"
        )
        message = (
            f"Playing {result.count} tracks{source}."
            if result.started
            else f"Added {result.count} tracks to the queue{source}."
        )

    if selection.truncated:
        message += f" Only the first {MAX_PLAYLIST_TRACKS} tracks were added."
    return message


async def _require_guild(
    interaction: discord.Interaction,
) -> discord.Guild | None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Music commands can only be used in a server.",
            ephemeral=True,
        )
        return None
    return interaction.guild


async def _require_user_voice_channel(
    interaction: discord.Interaction,
) -> discord.VoiceChannel | discord.StageChannel | None:
    try:
        return voice_channel_from_user(interaction.user)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return None


def register_music_commands(
    bot: commands.Bot,
    manager: MusicManager = music_manager,
) -> app_commands.Group:
    music_group = app_commands.Group(
        name="music",
        description="Play and manage music in a voice channel",
    )
    queue_group = app_commands.Group(
        name="queue",
        description="Show or edit the music queue",
        parent=music_group,
    )

    @music_group.command(name="play", description="Play or queue audio from YouTube")
    @app_commands.describe(url="The YouTube video or playlist URL to play")
    async def play(interaction: discord.Interaction, url: str) -> None:
        if not is_youtube_url(url.strip()):
            await interaction.response.send_message(
                "Please provide a valid HTTPS YouTube URL.",
                ephemeral=True,
            )
            return

        voice_channel = await _require_user_voice_channel(interaction)
        if voice_channel is None:
            return
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "This command needs a server text channel.",
                ephemeral=True,
            )
            return

        existing = manager.get(interaction.guild.id)
        existing_token = existing.token if existing is not None else None
        await interaction.response.defer(thinking=True)
        try:
            selection, result = await enqueue_youtube(
                manager,
                interaction.guild,
                voice_channel,
                interaction.channel,
                url,
                requester=interaction.user,
            )
        except Exception:
            logger.exception("Could not queue YouTube URL %s", url)
            await interaction.followup.send(
                "Could not read or queue that YouTube URL.",
                ephemeral=True,
            )
            return

        message = play_confirmation(result, selection)
        session = manager.get(interaction.guild.id)
        web_url = session_web_url(session)
        if existing_token is None and web_url is not None:
            message += f"\nControl this session [here]({web_url})."
        await interaction.followup.send(message)

    @music_group.command(name="pause", description="Pause the current track")
    async def pause(interaction: discord.Interaction) -> None:
        guild = await _require_guild(interaction)
        if guild is None:
            return

        session = manager.get(guild.id)
        status = (
            await session.pause_async()
            if session is not None
            else "not_playing"
        )
        messages = {
            "paused": "Paused.",
            "already_paused": "Already paused.",
            "not_playing": "Nothing is playing.",
        }
        await interaction.response.send_message(messages[status])

    @music_group.command(name="resume", description="Resume the paused track")
    async def resume(interaction: discord.Interaction) -> None:
        guild = await _require_guild(interaction)
        if guild is None:
            return

        session = manager.get(guild.id)
        status = (
            await session.resume_async()
            if session is not None
            else "not_paused"
        )
        await interaction.response.send_message(
            "Resumed." if status == "resumed" else "Nothing is paused."
        )

    @music_group.command(name="skip", description="Skip the current song")
    async def skip(interaction: discord.Interaction) -> None:
        guild = await _require_guild(interaction)
        if guild is None:
            return

        session = manager.get(guild.id)
        skipped = await session.skip() if session is not None else False
        await interaction.response.send_message(
            "Skipped." if skipped else "Nothing is playing."
        )

    @music_group.command(
        name="join",
        description="Join the voice channel you're in",
    )
    async def join(interaction: discord.Interaction) -> None:
        voice_channel = await _require_user_voice_channel(interaction)
        if voice_channel is None:
            return

        await interaction.response.defer(thinking=True)
        session = manager.get_or_create(voice_channel.guild)
        try:
            status = await session.connect(voice_channel, move=True)
            session.text_channel = interaction.channel
        except Exception:
            if session.current is None and not session.queue:
                await session.close()
            logger.exception("Could not join voice channel %s", voice_channel.id)
            await interaction.followup.send(
                "Could not connect to that voice channel.",
                ephemeral=True,
            )
            return

        name = discord.utils.escape_markdown(voice_channel.name)[:250]
        messages = {
            "joined": f"Joined **{name}**.",
            "already_here": f"Already in **{name}**.",
            "moved": f"Moved to **{name}**.",
        }
        message = messages[status]
        web_url = session_web_url(session)
        if web_url is not None:
            message += f"\nControl this session [here]({web_url})."
        await interaction.followup.send(message)

    @music_group.command(name="leave", description="Leave and clear the queue")
    async def leave(interaction: discord.Interaction) -> None:
        guild = await _require_guild(interaction)
        if guild is None:
            return

        session = manager.get(guild.id)
        if session is not None:
            await session.close()

        await interaction.response.send_message(
            "Left the voice channel and cleared the queue."
        )

    @queue_group.command(name="show", description="Show the current song and queue")
    async def show_queue(interaction: discord.Interaction) -> None:
        guild = await _require_guild(interaction)
        if guild is None:
            return

        session = manager.get(guild.id)
        message = session.describe_queue() if session else "The queue is empty."
        await interaction.response.send_message(
            message,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @queue_group.command(name="remove", description="Remove a track from the queue")
    @app_commands.describe(number="Queue position to remove (from /music queue show)")
    async def queue_remove(
        interaction: discord.Interaction,
        number: app_commands.Range[int, 1, 10_000],
    ) -> None:
        guild = await _require_guild(interaction)
        if guild is None:
            return

        session = manager.get(guild.id)
        removed = (
            await session.remove_queued(number)
            if session is not None
            else None
        )
        if removed is None:
            await interaction.response.send_message(
                "That queue position does not exist.",
                ephemeral=True,
            )
            return

        label = discord.utils.escape_markdown(removed.title or removed.url)[:250]
        await interaction.response.send_message(
            f"Removed **{label}** from the queue."
        )

    @queue_group.command(name="swap", description="Swap two tracks in the queue")
    @app_commands.describe(
        first="First queue position",
        second="Second queue position",
    )
    async def queue_swap(
        interaction: discord.Interaction,
        first: app_commands.Range[int, 1, 10_000],
        second: app_commands.Range[int, 1, 10_000],
    ) -> None:
        guild = await _require_guild(interaction)
        if guild is None:
            return

        session = manager.get(guild.id)
        swapped = (
            await session.swap_queued_async(first, second)
            if session is not None
            else False
        )
        if not swapped:
            await interaction.response.send_message(
                "Those queue positions do not exist.",
                ephemeral=True,
            )
            return

        if first == second:
            await interaction.response.send_message(
                "Those are the same queue position."
            )
            return

        await interaction.response.send_message(
            f"Swapped queue positions {first} and {second}."
        )

    bot.tree.add_command(music_group)
    return music_group
