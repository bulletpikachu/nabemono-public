from __future__ import annotations

import asyncio

import discord

from src.config import MUSIC_WEB_BASE_URL
from src.music.models import EnqueueResult, YoutubeSelection
from src.music.player import MusicManager
from src.music.youtube import discover_youtube_tracks, is_youtube_url


def session_web_url(session: object) -> str | None:
    token = getattr(session, "token", None)
    if not MUSIC_WEB_BASE_URL or not token:
        return None
    return f"{MUSIC_WEB_BASE_URL}/s/{token}"


async def enqueue_youtube(
    manager: MusicManager,
    guild: discord.Guild,
    voice_channel: discord.VoiceChannel | discord.StageChannel,
    text_channel: discord.abc.Messageable,
    url: str,
    requester: object | None = None,
) -> tuple[YoutubeSelection, EnqueueResult]:
    url = url.strip()
    if not is_youtube_url(url):
        raise ValueError("Please provide a valid HTTPS YouTube URL.")

    selection = await asyncio.to_thread(discover_youtube_tracks, url)
    session = manager.get_or_create(guild)
    try:
        await session.connect(voice_channel)
        enqueue_tracks = getattr(session, "enqueue_tracks", None)
        if enqueue_tracks is not None:
            result = await enqueue_tracks(
                selection.tracks,
                text_channel,
                requester=requester,
            )
        else:
            result = session.enqueue(
                selection.tracks,
                text_channel,
                requester=requester,
            )
    except Exception:
        if session.current is None and not session.queue:
            await session.close()
        raise

    return selection, result
