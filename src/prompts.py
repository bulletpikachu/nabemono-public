import base64
import re
from pathlib import Path
from src.short_term_memory import MemoryManager
from src.affection import affection_check, get_users, get_affection
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PROMPTS_PATH = Path("prompts")
AFFECTIONS_PATH = PROMPTS_PATH / "affections"


def load_prompt_file(path):
    return Path(path).read_text(encoding="utf-8")


def load_system_prompt():
    return load_prompt_file(PROMPTS_PATH / "system_prompt.txt")

def load_affection_classifier():
    return load_prompt_file(PROMPTS_PATH / "affection.txt")

def load_affection(affection):
    return load_prompt_file(AFFECTIONS_PATH / f"{affection}.txt")


def clean_content(message):
    content = message.content

    for user in message.mentions:
        content = content.replace(f"<@{user.id}>", f"@{user.name}")
        content = content.replace(f"<@!{user.id}>", f"@{user.name}")

    return content


async def build_prompt(
    message,
    affection="neutral",
    memory: MemoryManager = None,
    long_term_memories=None,
):
    # CONTEXT
    system_prompt = load_system_prompt()
    context = memory.get_messages(message.channel.id, 10, False) if memory else []
    reply_context = await replied_message_context(message)
    chat_history = []
    for context_message in context:
        turns = message_to_turn(context_message)
        if (
            reply_context
            and context_message.id == message.id
            and turns
            and turns[0].get("type") == "user_input"
        ):
            content = turns[0].get("content") or []
            text_block = next(
                (block for block in content if block.get("type") == "text"),
                None,
            )
            if text_block:
                text_block["text"] = f"{reply_context}\n{text_block['text']}"
        chat_history.extend(turns)


    # LONG TERM MEMORY
    long_term_memory = "\n".join(
        [f"- {memory.content}" for memory in (long_term_memories or [])]
    )
    if not long_term_memory:
        long_term_memory = "- none"

    # TIME
    time_pacific = datetime.now(ZoneInfo("America/Los_Angeles")).isoformat()

    # BUILD PROMPT
    prompt = (
        system_prompt
        .replace("{long_term_memory}", long_term_memory)
        .replace(
            "{affection}",
            load_affection(affection).replace("{user}", message.author.name),
        )
        .replace("{time}", time_pacific)
    )

    # RETURN
    print(f"[PROMPT]: {prompt}")
    print(f"[CHAT HISTORY]: {chat_history}")
    return prompt, chat_history


async def replied_message_context(message):
    reference = getattr(message, "reference", None)
    if not reference:
        return None

    replied_message = getattr(reference, "resolved", None)
    if replied_message is None and getattr(reference, "message_id", None):
        try:
            replied_message = await message.channel.fetch_message(reference.message_id)
        except Exception as error:
            print(f"[REPLY CONTEXT ERROR]: {error}")
            return None

    replied_content = getattr(replied_message, "content", "").strip()
    if not replied_content:
        return None

    return f'[REPLIED TO MESSAGE: "{replied_content}"]'


def message_to_turn(message):
    if message.steps_dump and not getattr(message, "images", []):
        return message.steps_dump

    content = [{
        "type": "text",
        "text": f"{message.author.name}: {clean_content(message)}",
    }]
    for image in getattr(message, "images", []):
        image_content = image_to_content(image)
        if image_content:
            content.append(image_content)

    return [{
        "type": "user_input",
        "content": content,
    }]


def image_to_content(image):
    try:
        image_bytes = Path(image.cache_path).read_bytes()
    except Exception as error:
        print(f"[IMAGE PROMPT ERROR]: skipped {image.cache_path}: {error}")
        return None

    return {
        "type": "image",
        "data": base64.b64encode(image_bytes).decode("utf-8"),
        "mime_type": image.content_type,
    }
