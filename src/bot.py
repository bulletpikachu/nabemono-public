import discord
from discord.ext import commands
from discord import app_commands
from src.config import (
    CHANNEL_ID,
    MEMORY_DB_PATH,
    MEMORY_DEDUP_MIN_SCORE,
    MEMORY_EMBEDDING_MODEL,
    MEMORY_EXTRACT_CONTEXT_MESSAGES,
    MEMORY_MIN_SCORE,
    MEMORY_TOP_K,
    MUSIC_UPLOAD_DIR,
    MUSIC_WEB_BASE_URL,
    MUSIC_WEB_HOST,
    MUSIC_WEB_PORT,
    OWNER_ID,
    PERSONA_DASHBOARD_PASSWORD,
    TOKEN,
)
from src.short_term_memory import MemoryManager
from src.prompts import clean_content
from src.quotes import get_clear_quote
from src.classifier import affection_classifier
from src.response import get_response
from src.affection import affection_check, get_affection, set_affection, update_affection, affection_ranking
from src.long_term_memory import LongTermMemory
from src.memory_extractor import extract_memory
from src.analytics import ResponseAnalytics, ResponseAnalyticsStore
from src.music.commands import register_music_commands
from src.music.player import music_manager
from src.music.web import MusicWebServer
from src.persona.service import persona_store
from src.reminders import reminder_scheduler
from src.canvas.commands import register_canvas_commands
from src.canvas.scheduler import canvas_scheduler
import asyncio
import json
from datetime import datetime, timezone

intents = discord.Intents.default()
intents.message_content = True


class NabemonoBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.music_web = MusicWebServer(
            music_manager,
            MUSIC_WEB_HOST,
            MUSIC_WEB_PORT,
            MUSIC_UPLOAD_DIR,
            MUSIC_WEB_BASE_URL,
            persona_store,
            PERSONA_DASHBOARD_PASSWORD,
        )

    async def setup_hook(self) -> None:
        await super().setup_hook()
        await self.music_web.start()

    async def close(self) -> None:
        await canvas_scheduler.close()
        await music_manager.close_all()
        await self.music_web.close()
        await super().close()


client = NabemonoBot()
register_music_commands(client)
register_canvas_commands(client)

response_memory = MemoryManager(max_messages=10)
analytics_store = ResponseAnalyticsStore()
long_term_memory = LongTermMemory(
    db_path=MEMORY_DB_PATH,
    embedding_model=MEMORY_EMBEDDING_MODEL,
    top_k=MEMORY_TOP_K,
    min_score=MEMORY_MIN_SCORE,
    dedup_min_score=MEMORY_DEDUP_MIN_SCORE,
)


def is_owner(user):
    return user.id == OWNER_ID


def _analytics_timestamp(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return datetime.now(timezone.utc)


def _store_response_analytics(
    message_id,
    response_time,
    request_message,
    analytics_info,
    is_interim=False,
):
    request_time = request_message.created_at or datetime.now(timezone.utc)
    latency_ms = max(
        0,
        int((response_time - request_time).total_seconds() * 1000),
    )
    record = ResponseAnalytics(
        message_id=message_id,
        request_message_id=request_message.id,
        channel_id=request_message.channel.id,
        model=analytics_info["model"],
        timestamp=response_time,
        latency_ms=latency_ms,
        input_tokens=analytics_info["input_tokens"],
        output_tokens=analytics_info["output_tokens"],
        thought_tokens=analytics_info["thought_tokens"],
        tool_tokens=analytics_info["tool_tokens"],
        total_tokens=analytics_info["total_tokens"],
        tool_steps=analytics_info["tool_steps"],
        thought_signatures=analytics_info["thought_signatures"],
        tool_results=analytics_info["tool_results"],
        is_interim=is_interim,
    )
    analytics_store.append(record)


@client.event
async def on_ready():
    try:
        synced = await client.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(e)
    
    game_assets = {
        #"large_image": "https://bulletmaji.me/images/electiontally.png",
        "large_text": "im nabemono"
    }
    game = discord.Game("test", platform="test", assets=game_assets)
    await client.change_presence(status=discord.Status.online, activity=game)

    print(f"Logged in as {client.user}")
    reminder_scheduler.start(client)
    canvas_scheduler.start(client)
    asyncio.create_task(_prepare_wikipedia())


@client.event
async def on_voice_state_update(member, before, after):
    if client.user is None or member.id != client.user.id:
        return
    if before.channel is not None and after.channel is None:
        session = music_manager.get(member.guild.id)
        if session is not None:
            await session.close()


@client.tree.command(name="clear", description="clears nabemono's short term memory.")
async def clear(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        await interaction.response.send_message("i don't want to forget!!")
        return
    response_memory.clear()
    await interaction.response.send_message(get_clear_quote())


@client.tree.command(name="secret", description="secret!")
async def secret(interaction: discord.Interaction, code: str):
    if not is_owner(interaction.user):
        await interaction.response.send_message("you found the secret command!", ephemeral=True)
        return
    await interaction.channel.send(code)
    await interaction.response.send_message("sent", ephemeral=True)


@client.tree.command(name="affection", description="view affection leaderboard")
async def affection(interaction: discord.Interaction):
    ranking = affection_ranking()
    lines = []

    for i, (username, value) in enumerate(ranking):
        lines.append(f"{i+1}. {username:<20} {value:>0.0f}")

    text = "```\n" + "          \n".join(lines) + "\n```"

    embed = discord.Embed(
        title="♡♡♡ HEART METER ♡♡♡",
        description=text,
        color=discord.Color.pink(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="\"hmpf\"", icon_url="https://bulletmaji.me/images/electiontally.png")
    embed.set_author(name="nabemono's lovey dovey")

    await interaction.response.send_message(embed=embed)
    return

@client.tree.command(name="setaffection", description="set someone's affection")
async def setaffection(interaction: discord.Interaction, username: str, value: float):
    if not is_owner(interaction.user):
        await interaction.response.send_message("uhh.. who are you?")
        return
    set_affection(username, value)
    await interaction.response.send_message(f"set {username}'s affection to {value}", ephemeral=True)


@client.tree.command(name="analytics", description="view recent response analytics")
async def analytics(interaction: discord.Interaction, limit: int = 5):
    if not is_owner(interaction.user):
        await interaction.response.send_message("uhh.. who are you?")
        return

    records = analytics_store.get_recent(limit=max(1, min(limit, 10)))
    if not records:
        await interaction.response.send_message("nothing recorded yet")
        return

    lines = []
    for record in records:
        lines.append(
            f"#{record.message_id} req={record.request_message_id} model={record.model} "
            f"tok={record.total_tokens} latency={record.latency_ms}ms"
            f"{' interim' if record.is_interim else ''}"
        )

    await interaction.response.send_message("\n".join(lines))


@client.tree.command(name="memory_search", description="search nabemono's long term memory")
async def memory_search(interaction: discord.Interaction, query: str):
    if not is_owner(interaction.user):
        await interaction.response.send_message("uhh.. who are you?", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    memories = await long_term_memory.search_async(query, min_score=0)
    if not memories:
        await interaction.followup.send("no memories found", ephemeral=True)
        return

    lines = [
        f"{memory.id}. [{memory.score:.2f}] {memory.username}: {memory.content}"
        for memory in memories
    ]
    await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)


@client.tree.command(name="memory_forget", description="delete one long term memory")
async def memory_forget(interaction: discord.Interaction, memory_id: int):
    if not is_owner(interaction.user):
        await interaction.response.send_message("uhh.. who are you?", ephemeral=True)
        return

    deleted = long_term_memory.forget(memory_id)
    if deleted:
        await interaction.response.send_message(f"forgot memory {memory_id}", ephemeral=True)
    else:
        await interaction.response.send_message(f"couldn't find memory {memory_id}", ephemeral=True)


@client.tree.command(name="memory_clear_user", description="clear long term memories for a user")
async def memory_clear_user(interaction: discord.Interaction, username: str):
    if not is_owner(interaction.user):
        await interaction.response.send_message("uhh.. who are you?", ephemeral=True)
        return

    deleted = long_term_memory.clear_user(username)
    await interaction.response.send_message(
        f"forgot {deleted} memories for {username}",
        ephemeral=True,
    )


async def _send_detail(channel, heading, details):
    prefix = f"{heading}\n"
    chunk_size = 2000 - len(prefix)
    for start in range(0, len(details), chunk_size):
        await channel.send(prefix + details[start : start + chunk_size])


@client.event
async def on_message(message):
    # -------- ADD TO SHORT-TERM MEMORY --------
    if message.author == client.user: # nabemono's messages are stored in memory during response generation
        return
    await response_memory.add_message(message)

    # -------- VERBAL COMMANDS --------
    info_command = " ".join(message.content.lower().split())
    if message.reference and info_command in {"info", "info -thought", "info -tool"}:
        if message.reference.resolved:
            replied_message = message.reference.resolved
        else:
            replied_message = await message.channel.fetch_message(
                message.reference.message_id
            )
        if replied_message.author != client.user:
            return

        record = analytics_store.find_by_message_id(message.reference.message_id)
        if not record:
            await message.channel.send("no metrics found for this message")
            return
        if info_command == "info -thought":
            details = "\n\n---\n\n".join(record.thought_signatures)
            await _send_detail(
                message.channel,
                f"thoughts for message #{record.message_id}",
                details or "no thought signatures recorded",
            )
            return
        if info_command == "info -tool":
            details = json.dumps(
                record.tool_results,
                ensure_ascii=False,
                indent=2,
            )
            await _send_detail(
                message.channel,
                f"tools for message #{record.message_id}",
                details if record.tool_results else "no tool calls recorded",
            )
            return
        content = (
            f"```Message #{record.message_id}\n"
            f"{'Interim: yes\n' if record.is_interim else ''}"
            f"Requested by #{record.request_message_id}\n"
            f"Completed by {record.model}\n\n"
            f"Input: {record.input_tokens}\n"
            f"Output: {record.output_tokens}\n"
            f"Thought: {record.thought_tokens}\n"
            f"Tool: {record.tool_tokens}\n"
            f"Total: {record.total_tokens}\n\n"
            f"{record.tool_steps} function calls\n"
            f"Latency: {record.latency_ms}ms\n```"
        )
        async with message.channel.typing():
            await message.channel.send(content)
        return


    # -------- MESSAGE CHECKS --------
    # Deterministic checks for messages. Usually ignored, but can trigger specific responses.
    
    if message.author.bot: # ignore any bot messages
        return

    if message.content.lower().startswith("yo betanine"):
        if message.channel.id != CHANNEL_ID:
            return
        sticker = discord.utils.get(message.guild.stickers, id=1485928821038383314)
        await message.channel.send(stickers=[sticker])
        return
    
    if message.guild is not None and client.user not in message.mentions:
        return
    
    if message.content.lower().startswith("!"):
        return

    # -------- MEMORY RETRIEVAL --------
    try:
        memory_query = clean_content(message)
        long_term_memories = await long_term_memory.search_async(memory_query)
        print(
            "[MEMORY RETRIEVED]: "
            + ", ".join([f"{memory.id}:{memory.score:.2f}" for memory in long_term_memories])
        )
    except Exception as e:
        print(f"[MEMORY SEARCH ERROR]: {e}")
        long_term_memories = []

    # -------- SEND RESPONSE --------
    affection_state = affection_check(get_affection(message.author.name))
    async with message.channel.typing():
        reply_messages, analytics_info = await get_response(
            message,
            affection_state,
            response_memory,
            long_term_memories=long_term_memories,
        )
        for interim in analytics_info.get("interim_messages") or []:
            _store_response_analytics(
                int(interim["message_id"]),
                _analytics_timestamp(interim.get("created_at")),
                message,
                analytics_info,
                is_interim=True,
            )
        # split_reply keeps every message within Discord's length limit.
        response_messages = []
        for reply_message in reply_messages:
            sent_message = await message.channel.send(reply_message)
            response_messages.append(sent_message)

        if response_messages:
            for response_message in response_messages:
                _store_response_analytics(
                    response_message.id,
                    response_message.created_at or datetime.now(timezone.utc),
                    message,
                    analytics_info,
                )
        elif not analytics_info.get("interim_messages"):
            _store_response_analytics(
                message.id,
                datetime.now(timezone.utc),
                message,
                analytics_info,
            )

    affection = await affection_classifier(message)
    update_affection(message.author.name, affection)

    # -------- MEMORY EXTRACTION --------
    try:
        # Exclude the triggering message by id: newer messages may have
        # arrived in the channel while the response was being generated.
        context_messages = [
            stored_message
            for stored_message in response_memory.get_messages(message.channel.id)
            if stored_message.id != message.id
        ][-MEMORY_EXTRACT_CONTEXT_MESSAGES:]
        extracted_memories = await extract_memory(message, context_messages=context_messages)
        for extracted_memory in extracted_memories:
            memory_id = await long_term_memory.remember_async(
                user_id=message.author.id,
                username=message.author.name,
                channel_id=message.channel.id,
                content=extracted_memory["memory"],
                source_message_id=message.id,
                importance=extracted_memory["importance"],
            )
            print(f"[MEMORY STORED]: {memory_id} {extracted_memory['memory']}")
    except Exception as e:
        print(f"[MEMORY ERROR]: {e}")


async def _prepare_wikipedia():
    try:
        from src.wikipedia import ensure_wikipedia

        path = await ensure_wikipedia()
        print(f"[WIKIPEDIA]: ready ({path})")
    except Exception as error:
        print(f"[WIKIPEDIA ERROR]: {error}")


def run_bot():
    client.run(TOKEN)
