import asyncio
import base64
import re
import json
from types import SimpleNamespace

import aiohttp
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from src.config import (
    GOOGLE_API_KEY,
    GOOGLE_RESPONSE_MODEL,
    GOOGLE_STRONG_RESPONSE_MODEL,
    OPENROUTER_API_KEY,
    RESPONSE_MODEL,
    RESPONSE_PROVIDER,
)
from src.message_splitter import split_reply
from src.tools import execute_tool_call
from src.prompts import build_prompt, message_to_turn


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

with open("src/tools.json", "r", encoding="utf-8") as f:
    tools = json.load(f)

MAX_TOOL_ROUNDS = 8
GOOGLE_UNAVAILABLE_RETRY_SECONDS = 5
GOOGLE_UNAVAILABLE_RETRY_ATTEMPTS = 2
google_client = None
_LOCK_IN_RE = re.compile(
    r"^(?P<mentions>(?:<@!?\d+>\s*|@\S+\s+)*)lock\s+in\b(?:\s*[,:;!.-]*)\s*",
    re.IGNORECASE,
)


def _lock_in_match(text):
    if not text:
        return None
    return _LOCK_IN_RE.match(text.lstrip())


def is_lock_in_request(message):
    return _lock_in_match(getattr(message, "content", None) or "") is not None


def strip_lock_in_prefix(text):
    if not text:
        return text
    stripped = text.lstrip()
    leading = text[: len(text) - len(stripped)]
    match = _LOCK_IN_RE.match(stripped)
    if not match:
        return text
    mentions = match.group("mentions") or ""
    rest = stripped[match.end():]
    return f"{leading}{mentions}{rest}".rstrip()


def _strip_lock_in_from_history(history, message):
    author_name = getattr(getattr(message, "author", None), "name", None)
    author_prefix = f"{author_name}: " if author_name else None
    for step in reversed(history):
        if step.get("type") != "user_input":
            continue
        for block in step.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text") or ""
            lines = text.split("\n")
            for index in range(len(lines) - 1, -1, -1):
                line = lines[index]
                if author_prefix and line.startswith(author_prefix):
                    body = line[len(author_prefix):]
                    stripped = strip_lock_in_prefix(body)
                    if stripped != body:
                        lines[index] = author_prefix + stripped
                        block["text"] = "\n".join(lines)
                        return
                stripped = strip_lock_in_prefix(line)
                if stripped != line:
                    lines[index] = stripped
                    block["text"] = "\n".join(lines)
                    return
        return


def _active_response_backend(lock_in=False):
    if lock_in:
        return "google", GOOGLE_STRONG_RESPONSE_MODEL
    if RESPONSE_PROVIDER == "openrouter":
        return "openrouter", RESPONSE_MODEL
    return "google", GOOGLE_RESPONSE_MODEL


def _google_client():
    global google_client
    if google_client is None:
        google_client = genai.Client(api_key=GOOGLE_API_KEY)
    return google_client


def _encode_thought_signature(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return value


def _decode_thought_signature(value):
    if not value:
        return None
    if isinstance(value, bytes):
        return value
    return base64.b64decode(value)


def _function_result_text(result):
    if isinstance(result, list):
        return "".join(
            block.get("text", "")
            for block in result
            if block.get("type") == "text"
        )
    return result


def _function_result_payload(result):
    text = _function_result_text(result)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    if parsed is not None:
        return {"result": parsed}
    return {"result": str(text)}


def _thought_text(step):
    summary = step.get("summary")
    if isinstance(summary, list):
        return "".join(
            block.get("text", "")
            for block in summary
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return summary or ""


def _enum_value(value):
    if value is None:
        return None
    return getattr(value, "value", value)


def _as_enum(enum_cls, value, default=None):
    if value is None:
        return default
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError:
        return default


def _sanitize_step(step):
    """Drop empty text blocks from a step dump.

    The model emits thought steps whose summary is an empty text block. Replaying
    one is rejected with "Missing text in content of type text", so strip them
    while keeping the thought signature that carries tool context.
    """
    sanitized = dict(step)
    for field in ("content", "summary", "result"):
        blocks = sanitized.get(field)
        if not isinstance(blocks, list):
            continue

        kept = [
            block
            for block in blocks
            if not (
                isinstance(block, dict)
                and block.get("type") == "text"
                and not block.get("text")
            )
        ]
        if kept:
            sanitized[field] = kept
        else:
            sanitized.pop(field, None)

    return sanitized


def _thought_summaries(message):
    summaries = [
        detail.get("summary")
        for detail in message.get("reasoning_details") or []
        if detail.get("type") == "reasoning.summary" and detail.get("summary")
    ]
    reasoning = message.get("reasoning")
    return summaries or ([reasoning] if reasoning else [])


def _print_thought_summary(message):
    thoughts = _thought_summaries(message)
    if thoughts:
        print("Thought summary:")
        for thought in thoughts:
            print(thought)
        print()


def _openrouter_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {"type": "object"}),
            },
        }
        for tool in tools
    ]


def _google_function_declarations():
    return [
        types.FunctionDeclaration(
            name=tool["name"],
            description=tool.get("description", ""),
            parameters_json_schema=tool.get(
                "parameters",
                {"type": "object"},
            ),
        )
        for tool in tools
    ]


def _google_tools():
    return [
        types.Tool(
            code_execution=types.ToolCodeExecution(),
            function_declarations=_google_function_declarations(),
        )
    ]


def _content_to_openrouter(content):
    if isinstance(content, str):
        return content

    converted = []
    for block in content or []:
        if block.get("type") == "text":
            converted.append({"type": "text", "text": block.get("text", "")})
        elif block.get("type") == "image":
            converted.append({
                "type": "image_url",
                "image_url": {
                    "url": (
                        f"data:{block.get('mime_type', 'image/jpeg')};base64,"
                        f"{block.get('data', '')}"
                    ),
                },
            })
    return converted


def _history_to_openrouter(history):
    messages = []
    pending_tool_calls = []

    def flush_tool_calls():
        if pending_tool_calls:
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": list(pending_tool_calls),
            })
            pending_tool_calls.clear()

    for step in history:
        step_type = step.get("type")
        if step_type == "function_call":
            arguments = step.get("arguments") or {}
            pending_tool_calls.append({
                "id": step.get("id"),
                "type": "function",
                "function": {
                    "name": step.get("name"),
                    "arguments": json.dumps(arguments),
                },
            })
            continue

        flush_tool_calls()
        if step_type == "user_input":
            messages.append({
                "role": "user",
                "content": _content_to_openrouter(step.get("content")),
            })
        elif step_type == "assistant_message":
            content = step.get("content", "")
            if isinstance(content, list):
                content = "".join(
                    block.get("text", "")
                    for block in content
                    if block.get("type") == "text"
                )
            messages.append({"role": "assistant", "content": content})
        elif step_type == "function_result":
            tool_message = {
                "role": "tool",
                "tool_call_id": step.get("call_id"),
                "content": str(_function_result_text(step.get("result", ""))),
            }
            if step.get("name"):
                tool_message["name"] = step["name"]
            messages.append(tool_message)
        elif step.get("role"):
            messages.append(step)

    flush_tool_calls()
    return messages


def _content_to_google_parts(content):
    if isinstance(content, str):
        return [types.Part(text=content)]

    parts = []
    for block in content or []:
        if block.get("type") == "text":
            parts.append(types.Part(text=block.get("text", "")))
        elif block.get("type") == "image":
            parts.append(types.Part.from_bytes(
                data=base64.b64decode(block.get("data", "")),
                mime_type=block.get("mime_type", "image/jpeg"),
            ))
    return parts or [types.Part(text="")]


def _signature_kwargs(step, key="thought_signature"):
    signature = _decode_thought_signature(step.get(key))
    if signature:
        return {"thought_signature": signature}
    return {}


def _history_to_google(history):
    contents = []
    pending_model_parts = []
    pending_function_responses = []

    def flush_model_parts():
        if pending_model_parts:
            contents.append(types.Content(role="model", parts=list(pending_model_parts)))
            pending_model_parts.clear()

    def flush_function_responses():
        if pending_function_responses:
            contents.append(types.Content(
                role="user",
                parts=list(pending_function_responses),
            ))
            pending_function_responses.clear()

    def begin_model_turn():
        flush_function_responses()

    for step in history:
        step_type = step.get("type")
        if step_type == "thought":
            begin_model_turn()
            part_kwargs = {"thought": True}
            text = _thought_text(step)
            if text:
                part_kwargs["text"] = text
            part_kwargs.update(_signature_kwargs(step, "signature"))
            if "text" in part_kwargs or "thought_signature" in part_kwargs:
                pending_model_parts.append(types.Part(**part_kwargs))
            continue

        if step_type == "function_call":
            begin_model_turn()
            function_call_kwargs = {
                "name": step.get("name"),
                "args": step.get("arguments") or {},
            }
            if step.get("id"):
                function_call_kwargs["id"] = step.get("id")
            part_kwargs = {
                "function_call": types.FunctionCall(**function_call_kwargs)
            }
            part_kwargs.update(_signature_kwargs(step))
            pending_model_parts.append(types.Part(**part_kwargs))
            continue

        if step_type == "executable_code":
            begin_model_turn()
            executable_code_kwargs = {
                "code": step.get("code") or "",
                "language": _as_enum(
                    types.Language,
                    step.get("language"),
                    types.Language.PYTHON,
                ),
            }
            if step.get("id"):
                executable_code_kwargs["id"] = step["id"]
            part_kwargs = {
                "executable_code": types.ExecutableCode(**executable_code_kwargs),
            }
            part_kwargs.update(_signature_kwargs(step))
            pending_model_parts.append(types.Part(**part_kwargs))
            continue

        if step_type == "code_execution_result":
            begin_model_turn()
            result_kwargs = {"output": step.get("output") or ""}
            outcome = _as_enum(types.Outcome, step.get("outcome"))
            if outcome is not None:
                result_kwargs["outcome"] = outcome
            if step.get("id"):
                result_kwargs["id"] = step["id"]
            part_kwargs = {
                "code_execution_result": types.CodeExecutionResult(**result_kwargs),
            }
            part_kwargs.update(_signature_kwargs(step))
            pending_model_parts.append(types.Part(**part_kwargs))
            continue

        if step_type == "server_tool_call":
            begin_model_turn()
            tool_call_kwargs = {
                "args": step.get("args") or {},
            }
            if step.get("id"):
                tool_call_kwargs["id"] = step["id"]
            if step.get("tool_type"):
                tool_call_kwargs["tool_type"] = step["tool_type"]
            part_kwargs = {"tool_call": types.ToolCall(**tool_call_kwargs)}
            part_kwargs.update(_signature_kwargs(step))
            pending_model_parts.append(types.Part(**part_kwargs))
            continue

        if step_type == "server_tool_response":
            begin_model_turn()
            tool_response_kwargs = {
                "response": step.get("response") or {},
            }
            if step.get("id"):
                tool_response_kwargs["id"] = step["id"]
            if step.get("tool_type"):
                tool_response_kwargs["tool_type"] = step["tool_type"]
            part_kwargs = {
                "tool_response": types.ToolResponse(**tool_response_kwargs),
            }
            part_kwargs.update(_signature_kwargs(step))
            pending_model_parts.append(types.Part(**part_kwargs))
            continue

        if step_type == "assistant_message":
            begin_model_turn()
            content = step.get("content", "")
            if isinstance(content, list):
                content = "".join(
                    block.get("text", "")
                    for block in content
                    if block.get("type") == "text"
                )
            pending_model_parts.append(types.Part(text=content or ""))
            continue

        flush_model_parts()
        if step_type == "function_result":
            function_response_kwargs = {
                "name": step.get("name"),
                "response": _function_result_payload(step.get("result", "")),
            }
            if step.get("call_id"):
                function_response_kwargs["id"] = step.get("call_id")
            pending_function_responses.append(types.Part(
                function_response=types.FunctionResponse(**function_response_kwargs)
            ))
            continue

        flush_function_responses()
        if step_type == "user_input":
            contents.append(types.Content(
                role="user",
                parts=_content_to_google_parts(step.get("content")),
            ))

    flush_model_parts()
    flush_function_responses()
    return contents


def _google_response_to_completion(response, model=None):
    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    completion_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    thought_tokens = int(getattr(usage, "thoughts_token_count", 0) or 0)
    model = (
        getattr(response, "model_version", None)
        or model
        or GOOGLE_RESPONSE_MODEL
    )

    candidates = getattr(response, "candidates", None) or []
    if not candidates or not getattr(candidates[0], "content", None):
        error = getattr(response, "prompt_feedback", None)
        return {
            "choices": [],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "completion_tokens_details": {
                    "reasoning_tokens": thought_tokens,
                },
            },
            "model": model,
            "error": str(error) if error else "no candidates",
        }

    text_parts = []
    thought_parts = []
    tool_calls = []
    thoughts = []
    executable_code_parts = []
    code_execution_result_parts = []
    server_tool_parts = []
    parts = candidates[0].content.parts or []
    for index, part in enumerate(parts):
        signature = _encode_thought_signature(getattr(part, "thought_signature", None))
        function_call = getattr(part, "function_call", None)
        executable_code = getattr(part, "executable_code", None)
        code_execution_result = getattr(part, "code_execution_result", None)
        server_tool_call = getattr(part, "tool_call", None)
        server_tool_response = getattr(part, "tool_response", None)
        if getattr(part, "thought", False) and not function_call:
            if part.text:
                thoughts.append(part.text)
            if part.text or signature:
                thought_parts.append({
                    "text": part.text or "",
                    "thought_signature": signature,
                })
            continue
        if function_call:
            args = function_call.args or {}
            if hasattr(args, "items"):
                args = dict(args)
            tool_calls.append({
                "id": function_call.id or f"call_{index}",
                "type": "function",
                "function": {
                    "name": function_call.name,
                    "arguments": json.dumps(args),
                },
                "thought_signature": signature,
            })
            continue
        if executable_code:
            executable_code_parts.append({
                "id": getattr(executable_code, "id", None),
                "code": executable_code.code or "",
                "language": _enum_value(getattr(executable_code, "language", None)),
                "thought_signature": signature,
            })
            continue
        if code_execution_result:
            code_execution_result_parts.append({
                "id": getattr(code_execution_result, "id", None),
                "outcome": _enum_value(getattr(code_execution_result, "outcome", None)),
                "output": code_execution_result.output or "",
                "thought_signature": signature,
            })
            continue
        if server_tool_call:
            args = server_tool_call.args or {}
            if hasattr(args, "items"):
                args = dict(args)
            server_tool_parts.append({
                "type": "server_tool_call",
                "id": getattr(server_tool_call, "id", None),
                "tool_type": _enum_value(getattr(server_tool_call, "tool_type", None)),
                "args": args,
                "thought_signature": signature,
            })
            continue
        if server_tool_response:
            response_payload = server_tool_response.response or {}
            if hasattr(response_payload, "items"):
                response_payload = dict(response_payload)
            server_tool_parts.append({
                "type": "server_tool_response",
                "id": getattr(server_tool_response, "id", None),
                "tool_type": _enum_value(getattr(server_tool_response, "tool_type", None)),
                "response": response_payload,
                "thought_signature": signature,
            })
            continue
        if isinstance(part.text, str):
            text_parts.append(part.text)

    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "".join(text_parts) or None,
                "tool_calls": tool_calls,
                "reasoning_details": [
                    {"type": "reasoning.summary", "summary": thought}
                    for thought in thoughts
                ],
                "thought_parts": thought_parts,
                "executable_code_parts": executable_code_parts,
                "code_execution_result_parts": code_execution_result_parts,
                "server_tool_parts": server_tool_parts,
            },
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "completion_tokens_details": {
                "reasoning_tokens": thought_tokens,
            },
        },
        "model": model,
    }


async def _request_openrouter(system_instruction, history):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": RESPONSE_MODEL,
        "messages": [
            {"role": "system", "content": system_instruction},
            *_history_to_openrouter(history),
        ],
        "tools": _openrouter_tools(),
        "reasoning": {
            "enabled": True,
            "exclude": False,
        },
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            OPENROUTER_URL,
            headers=headers,
            json=payload,
        ) as response:
            payload_response = await response.json(content_type=None)
            if response.status >= 400:
                print(f"[OPENROUTER ERROR]: {response.status} {payload_response}")
            response.raise_for_status()
            return payload_response


def _is_unavailable_error(error):
    if isinstance(error, genai_errors.APIError):
        return int(getattr(error, "code", 0) or 0) == 503
    return False


def _google_generate_config(system_instruction):
    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=_google_tools(),
        tool_config=types.ToolConfig(
            include_server_side_tool_invocations=True,
            function_calling_config=types.FunctionCallingConfig(
                mode=types.FunctionCallingConfigMode.VALIDATED,
            ),
        ),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            disable=True,
        ),
        thinking_config=types.ThinkingConfig(
            include_thoughts=True,
            thinking_level=types.ThinkingLevel.HIGH,
        ),
    )


async def _request_google(system_instruction, history, model=None):
    model_name = model or GOOGLE_RESPONSE_MODEL
    request = {
        "model": model_name,
        "contents": _history_to_google(history),
        "config": _google_generate_config(system_instruction),
    }
    attempts = GOOGLE_UNAVAILABLE_RETRY_ATTEMPTS + 1
    for attempt in range(1, attempts + 1):
        try:
            response = await _google_client().aio.models.generate_content(**request)
            return _google_response_to_completion(response, model=model_name)
        except Exception as error:
            print(f"[GOOGLE ERROR]: {error}")
            if not _is_unavailable_error(error) or attempt >= attempts:
                raise
            print(
                f"[GOOGLE RETRY]: 503, retrying identical request in "
                f"{GOOGLE_UNAVAILABLE_RETRY_SECONDS}s "
                f"(attempt {attempt}/{attempts})"
            )
            await asyncio.sleep(GOOGLE_UNAVAILABLE_RETRY_SECONDS)


async def _request_completion(system_instruction, history, model=None, provider=None):
    provider_name = provider or RESPONSE_PROVIDER
    if provider_name == "openrouter":
        return await _request_openrouter(system_instruction, history)
    return await _request_google(system_instruction, history, model=model)


async def get_response(message, affection="neutral", memory=None, long_term_memories=None):
    
    # ------- BUILD PROMPT -------
    prompt = await build_prompt(
        message,
        affection,
        memory,
        long_term_memories=long_term_memories,
    )
    history = list(prompt[1])
    lock_in = is_lock_in_request(message)
    if lock_in:
        _strip_lock_in_from_history(history, message)
    provider_name, model_name = _active_response_backend(lock_in)
    print(f"[PROVIDER]: {provider_name} ({model_name})")

    # ------- CALL LLM -------
    all_new_steps = []
    tool_steps = 0
    input_tokens = 0
    output_tokens = 0
    thought_tokens = 0
    tool_tokens = 0
    thought_signatures = []
    tool_results = []
    interim_messages = []
    completion = None
    output_text = ""

    for _ in range(MAX_TOOL_ROUNDS):
        completion = await _request_completion(
            prompt[0],
            history,
            model=model_name,
            provider=provider_name,
        )
        usage = completion.get("usage") or {}
        round_input_tokens = int(usage.get("prompt_tokens") or 0)
        round_output_tokens = int(usage.get("completion_tokens") or 0)
        input_tokens += round_input_tokens
        output_tokens += round_output_tokens
        thought_tokens += int(
            (usage.get("completion_tokens_details") or {}).get(
                "reasoning_tokens", 0
            ) or 0
        )
        print(
            f"[TOKENS]: INPUT: {round_input_tokens} "
            f"OUTPUT: {round_output_tokens}"
        )

        if completion.get("error"):
            print(f"[COMPLETION ERROR]: {completion['error']}")
        choices = completion.get("choices") or []
        if not choices:
            print(f"[COMPLETION]: no choices in completion: {completion}")
        response_message = choices[0].get("message", {}) if choices else {}
        _print_thought_summary(response_message)
        thought_signatures.extend(_thought_summaries(response_message))
        output_text = response_message.get("content") or ""
        function_calls = response_message.get("tool_calls") or []

        for thought_part in response_message.get("thought_parts") or []:
            thought_step = _sanitize_step({
                "type": "thought",
                "summary": [{"type": "text", "text": thought_part.get("text") or ""}],
                "signature": thought_part.get("thought_signature"),
            })
            history.append(thought_step)
            all_new_steps.append(thought_step)

        for server_part in response_message.get("server_tool_parts") or []:
            server_step = dict(server_part)
            history.append(server_step)
            all_new_steps.append(server_step)

        for code_part in response_message.get("executable_code_parts") or []:
            print("[TOOL] code_execution")
            code_step = {
                "type": "executable_code",
                "code": code_part.get("code") or "",
                "language": code_part.get("language") or "PYTHON",
            }
            if code_part.get("id"):
                code_step["id"] = code_part["id"]
            if code_part.get("thought_signature"):
                code_step["thought_signature"] = code_part["thought_signature"]
            history.append(code_step)
            all_new_steps.append(code_step)
            tool_steps += 1

        for result_part in response_message.get("code_execution_result_parts") or []:
            result_step = {
                "type": "code_execution_result",
                "outcome": result_part.get("outcome"),
                "output": result_part.get("output") or "",
            }
            if result_part.get("id"):
                result_step["id"] = result_part["id"]
            if result_part.get("thought_signature"):
                result_step["thought_signature"] = result_part["thought_signature"]
            history.append(result_step)
            all_new_steps.append(result_step)
            last_code = (response_message.get("executable_code_parts") or [{}])[-1]
            tool_results.append({
                "name": "code_execution",
                "arguments": {
                    "language": last_code.get("language"),
                    "code": last_code.get("code"),
                },
                "result": result_part.get("output") or "",
            })

        if not function_calls:
            assistant_step = {
                "type": "assistant_message",
                "content": [{"type": "text", "text": output_text}],
            }
            history.append(assistant_step)
            all_new_steps.append(assistant_step)
            break

        parsed_calls = []
        for raw_tool_call in function_calls:
            function = raw_tool_call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            tool_call = SimpleNamespace(
                id=raw_tool_call.get("id"),
                name=function.get("name"),
                arguments=arguments,
                thought_signature=raw_tool_call.get("thought_signature"),
            )
            parsed_calls.append(tool_call)
            function_step = {
                "type": "function_call",
                "id": tool_call.id,
                "name": tool_call.name,
                "arguments": tool_call.arguments,
            }
            if tool_call.thought_signature:
                function_step["thought_signature"] = tool_call.thought_signature
            history.append(function_step)
            all_new_steps.append(function_step)
            tool_steps += 1

        for tool_call in parsed_calls:
            try:
                function_response = await execute_tool_call(message, tool_call)
            except Exception as error:
                print(f"[TOOL ERROR]: {tool_call.name}: {error}")
                function_response = json.dumps(
                    {
                        "error": str(error),
                        "tool": tool_call.name,
                        "note": "the tool failed. try a different approach or another URL.",
                    },
                    ensure_ascii=False,
                )

            function_result = {
                "type": "function_result",
                "name": tool_call.name,
                "call_id": tool_call.id,
                "result": [{"type": "text", "text": str(function_response)}],
            }
            history.append(function_result)
            all_new_steps.append(function_result)
            tool_results.append({
                "name": tool_call.name,
                "arguments": tool_call.arguments,
                "result": str(function_response),
            })
            if tool_call.name == "interim_message":
                try:
                    payload = json.loads(function_response)
                except (json.JSONDecodeError, TypeError):
                    payload = None
                if isinstance(payload, dict) and payload.get("message_id") is not None:
                    interim_messages.append(payload)

    # ------- PARSING -------
    output = split_reply(output_text)

    # ------- ADD TO MEMORY -------
    # Store a complete turn replacement: user_input + model/tool steps so
    # message_to_turn can replay the full Interaction history next time.
    if memory:
        user_turns = []
        for stored_message in memory.get_messages(message.channel.id):
            if stored_message.id == message.id:
                user_turns = message_to_turn(stored_message)
                break
        memory.dump_steps(message, user_turns + all_new_steps)

    analytics = {
        "model": (
            completion.get("model", model_name)
            if completion
            else model_name
        ),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thought_tokens": thought_tokens,
        "tool_tokens": tool_tokens,
        "total_tokens": input_tokens + output_tokens,
        "tool_steps": tool_steps,
        "thought_signatures": thought_signatures,
        "tool_results": tool_results,
        "interim_messages": interim_messages,
    }

    return output, analytics
