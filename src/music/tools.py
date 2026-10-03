from __future__ import annotations

import asyncio
import json

import discord

from src.music.context import guild_from_message, voice_channel_from_user
from src.music.models import Track
from src.music.player import MusicManager, music_manager
from src.music.service import enqueue_youtube, session_web_url
from src.music.telemetry import music_telemetry
from src.music.youtube import search_youtube_tracks


def _track_search_result(track: Track) -> dict[str, str | float | None]:
    return {
        "title": track.title,
        "url": track.url,
        "duration_seconds": track.duration,
    }


async def youtube_search(
    original_message: discord.Message,
    query: str,
    max_results: int = 5,
) -> str:
    tracks = await asyncio.to_thread(search_youtube_tracks, query, max_results)
    music_telemetry.record(
        "searched",
        guild_id=getattr(getattr(original_message, "guild", None), "id", None),
        user=getattr(original_message, "author", None),
        query=query,
    )
    return json.dumps(
        {
            "query": query,
            "results": [_track_search_result(track) for track in tracks],
        },
        ensure_ascii=False,
    )


async def music_queue(
    original_message: discord.Message,
    manager: MusicManager = music_manager,
) -> str:
    guild = guild_from_message(original_message)
    session = manager.get(guild.id)
    snapshot = (
        session.snapshot()
        if session is not None
        else {
            "status": "empty",
            "current": None,
            "queue": [],
            "remaining_seconds": 0,
            "remaining_complete": True,
        }
    )
    return json.dumps(
        {"guild_id": guild.id, **snapshot},
        ensure_ascii=False,
    )


async def music_play(
    original_message: discord.Message,
    url: str,
    manager: MusicManager = music_manager,
) -> str:
    guild = guild_from_message(original_message)
    voice_channel = voice_channel_from_user(original_message.author)
    existing = manager.get(guild.id)
    existing_token = getattr(existing, "token", None)
    selection, result = await enqueue_youtube(
        manager,
        guild,
        voice_channel,
        original_message.channel,
        url,
        requester=original_message.author,
    )
    session = manager.get(guild.id)
    controller_url = (
        session_web_url(session)
        if existing_token is None and session is not None
        else None
    )
    if controller_url is not None:
        await original_message.channel.send(
            f"Control this session [here]({controller_url})."
        )

    return json.dumps(
        {
            "status": "playing" if result.started else "queued",
            "tracks_added": result.count,
            "queue_position": result.position,
            "playlist_title": selection.playlist_title,
            "playlist_truncated": selection.truncated,
            "queue": session.snapshot() if session is not None else None,
        },
        ensure_ascii=False,
    )
