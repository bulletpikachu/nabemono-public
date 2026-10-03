import json
import re

from google import genai
from google.genai import types

from src.config import GOOGLE_API_KEY, MEMORY_EXTRACT_MODEL, MEMORY_MIN_IMPORTANCE
from src.prompts import clean_content


google_client = None


EXTRACTION_SYSTEM_PROMPT = """
You extract durable long-term memories for a chatbot from Discord messages.

From the newest message only, extract facts worth remembering across conversations:
- stable user preferences
- stable user identity facts
- direct requests to remember something
- durable decisions or commitments

Do not extract:
- jokes, insults, commands, or random chatter
- temporary feelings or one-off events
- private secrets, credentials, or sensitive personal data
- anything uncertain or inferred too aggressively

Write each memory as a short standalone third-person fact that starts with the
user's name. Use importance 1 for ordinary useful facts, 2 for direct
"remember this" requests, and 3 only for highly important durable preferences.

Return {"memories": []} when there is nothing worth storing.
""".strip()


EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "importance": {"type": "integer"},
                },
                "required": ["content", "importance"],
            },
        },
    },
    "required": ["memories"],
}


def _strip_code_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


def _parse_extraction(content):
    """Parse model output into a list of {"memory", "importance"} dicts."""
    if not isinstance(content, str) or not content.strip():
        return []

    try:
        data = json.loads(_strip_code_fences(content))
    except json.JSONDecodeError:
        return []

    if isinstance(data, dict):
        items = data.get("memories", [])
    elif isinstance(data, list):
        items = data
    else:
        return []

    if not isinstance(items, list):
        return []

    memories = []
    for item in items:
        if not isinstance(item, dict):
            continue

        memory = str(item.get("content", "")).replace("—", "").strip()
        if not memory:
            continue

        try:
            importance = int(item.get("importance", 1))
        except (TypeError, ValueError):
            importance = 1
        importance = max(1, min(3, importance))

        if importance < MEMORY_MIN_IMPORTANCE:
            continue

        memories.append({"memory": memory, "importance": importance})
    return memories


def _build_extraction_input(message, context_messages=None):
    context_lines = [
        f"{context_message.author.name}: {clean_content(context_message)}"
        for context_message in (context_messages or [])
    ]
    context_block = "\n".join(context_lines) or "(no earlier messages)"
    return (
        f"Earlier messages, for context only:\n{context_block}\n\n"
        f"Newest message, extract memories from this one:\n"
        f"{message.author.name}: {clean_content(message)}"
    )


async def extract_memory(message, context_messages=None):
    global google_client
    if google_client is None:
        google_client = genai.Client(api_key=GOOGLE_API_KEY)

    response = await google_client.aio.models.generate_content(
        model=MEMORY_EXTRACT_MODEL,
        contents=_build_extraction_input(message, context_messages),
        config=types.GenerateContentConfig(
            system_instruction=EXTRACTION_SYSTEM_PROMPT,
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=EXTRACTION_SCHEMA,
        ),
    )

    content = (response.text or "").strip()
    print(f"[EXTRACTED MEMORY]: {content}")
    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        print(f"[EXTRACTION TOKENS]: {usage.total_token_count}")
    return _parse_extraction(content)
