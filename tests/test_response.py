import base64
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from google.genai import errors as genai_errors
from google.genai import types

from src.response import (
    _google_generate_config,
    _google_response_to_completion,
    _google_tools,
    _history_to_google,
    _history_to_openrouter,
    _openrouter_tools,
    _request_completion,
    _request_google,
    _sanitize_step,
    _strip_lock_in_from_history,
    get_response,
    is_lock_in_request,
    strip_lock_in_prefix,
)
from src.short_term_memory import MemoryManager


class SanitizeStepTests(unittest.TestCase):
    def test_drops_empty_summary_block_but_keeps_signature(self):
        step = {
            "signature": "abc==",
            "summary": [{"text": "", "type": "text"}],
            "type": "thought",
        }

        self.assertEqual(
            {"signature": "abc==", "type": "thought"},
            _sanitize_step(step),
        )

    def test_keeps_populated_blocks(self):
        step = {
            "content": [
                {"text": "", "type": "text"},
                {"text": "ok", "type": "text"},
                {"type": "image", "data": "..."},
            ],
            "type": "model_output",
        }

        self.assertEqual(
            [{"text": "ok", "type": "text"}, {"type": "image", "data": "..."}],
            _sanitize_step(step)["content"],
        )

    def test_leaves_unrelated_steps_untouched(self):
        step = {
            "arguments": {"query": "nabemono"},
            "id": "call_1",
            "name": "web_search",
            "type": "function_call",
        }

        self.assertEqual(step, _sanitize_step(step))


class HybridToolCallingTests(unittest.IsolatedAsyncioTestCase):
    def make_message(self, id=100, channel_id=10):
        return SimpleNamespace(
            id=id,
            attachments=[],
            author=SimpleNamespace(id=1, name="bushima"),
            channel=SimpleNamespace(id=channel_id),
            content="remind me in 5 seconds",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            mentions=[],
        )

    def make_completion(
        self,
        content="done.",
        tool_calls=None,
        thought=None,
    ):
        reasoning_details = (
            [{"type": "reasoning.summary", "summary": thought}]
            if thought
            else []
        )
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls or [],
                    "reasoning_details": reasoning_details,
                },
            }],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
            "model": "test-model",
        }

    async def test_executes_custom_tool_and_dumps_full_turn(self):
        message = self.make_message()
        memory = MemoryManager(max_messages=10)
        await memory.add_message(message)

        function_call = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "reminder",
                "arguments": json.dumps({
                    "seconds": 5,
                    "reminder_message": "hi",
                }),
            },
        }
        first = self.make_completion(
            content=None,
            tool_calls=[function_call],
            thought="I should create a reminder.",
        )
        second = self.make_completion(
            content="ok i'll remind you.",
            thought="The reminder was created.",
        )

        with (
            patch("src.response.build_prompt", new=AsyncMock(return_value=(
                "system",
                [{
                    "type": "user_input",
                    "content": [{"type": "text", "text": "bushima: remind me in 5 seconds"}],
                }],
            ))),
            patch(
                "src.response._request_completion",
                new=AsyncMock(side_effect=[first, second]),
            ) as request_completion,
            patch(
                "src.response.execute_tool_call",
                new=AsyncMock(return_value="Reminder task created!"),
            ) as execute_tool_call,
        ):
            output, analytics = await get_response(message, memory=memory)

        self.assertEqual(["ok i'll remind you"], output)
        self.assertEqual(1, analytics["tool_steps"])
        self.assertEqual(
            [
                "I should create a reminder.",
                "The reminder was created.",
            ],
            analytics["thought_signatures"],
        )
        self.assertEqual("reminder", analytics["tool_results"][0]["name"])
        self.assertEqual(
            "Reminder task created!",
            analytics["tool_results"][0]["result"],
        )
        self.assertEqual(2, request_completion.await_count)
        execute_tool_call.assert_awaited_once()

        stored = memory.get_messages(message.channel.id)[0]
        step_types = [step["type"] for step in stored.steps_dump]
        self.assertEqual(
            ["user_input", "function_call", "function_result", "assistant_message"],
            step_types,
        )
        self.assertEqual("call_1", stored.steps_dump[2]["call_id"])
        self.assertEqual(
            "Reminder task created!",
            stored.steps_dump[2]["result"][0]["text"],
        )

        openrouter_tools = _openrouter_tools()
        tool_names = [tool["function"]["name"] for tool in openrouter_tools]
        self.assertIn("reminder", tool_names)
        self.assertIn("interim_message", tool_names)
        self.assertIn("web_search", tool_names)
        self.assertIn("visit_page", tool_names)
        self.assertIn("wikipedia_search", tool_names)
        self.assertIn("wikipedia_page", tool_names)
        self.assertIn("read_channel_history", tool_names)
        self.assertIn("send_file", tool_names)

    async def test_interim_message_is_collected_in_analytics(self):
        sent = SimpleNamespace(
            id=555,
            channel=SimpleNamespace(id=10),
            created_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        )
        message = self.make_message()
        message.channel.send = AsyncMock(return_value=sent)
        memory = MemoryManager(max_messages=10)
        await memory.add_message(message)

        first = self.make_completion(
            content=None,
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "interim_message",
                    "arguments": json.dumps({
                        "message": "let me search that up..",
                    }),
                },
            }],
            thought="I should tell them I'm looking it up.",
        )
        second = self.make_completion(
            content="spain won.",
            thought="I found the answer.",
        )

        with (
            patch("src.response.build_prompt", new=AsyncMock(return_value=(
                "system",
                [{
                    "type": "user_input",
                    "content": [{"type": "text", "text": "bushima: Who won the world cup?"}],
                }],
            ))),
            patch(
                "src.response._request_completion",
                new=AsyncMock(side_effect=[first, second]),
            ),
        ):
            output, analytics = await get_response(message, memory=memory)

        self.assertEqual(["spain won"], output)
        self.assertEqual(1, analytics["tool_steps"])
        self.assertEqual("interim_message", analytics["tool_results"][0]["name"])
        self.assertEqual(1, len(analytics["interim_messages"]))
        self.assertEqual(555, analytics["interim_messages"][0]["message_id"])
        self.assertEqual(
            "2026-01-01T00:00:01+00:00",
            analytics["interim_messages"][0]["created_at"],
        )
        message.channel.send.assert_awaited_once_with("let me search that up..")

    async def test_tool_error_is_json_and_continues_the_turn(self):
        message = self.make_message()
        memory = MemoryManager(max_messages=10)
        await memory.add_message(message)

        first = self.make_completion(
            content=None,
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "visit_page",
                    "arguments": json.dumps({
                        "url": "https://usc.edu/academic-calendar/",
                    }),
                },
            }],
        )
        second = self.make_completion(
            content="that page blocked me, let me try another source.",
        )

        with (
            patch("src.response.build_prompt", new=AsyncMock(return_value=(
                "system",
                [{
                    "type": "user_input",
                    "content": [{"type": "text", "text": "bushima: what's USC's next academic break?"}],
                }],
            ))),
            patch(
                "src.response._request_completion",
                new=AsyncMock(side_effect=[first, second]),
            ) as request_completion,
            patch(
                "src.response.execute_tool_call",
                new=AsyncMock(side_effect=RuntimeError(
                    "403, message='Forbidden', url='https://usc.edu/academic-calendar/'"
                )),
            ),
        ):
            output, analytics = await get_response(message, memory=memory)

        self.assertEqual(
            ["that page blocked me, let me try another source"],
            output,
        )
        result = json.loads(analytics["tool_results"][0]["result"])
        self.assertEqual("visit_page", result["tool"])
        self.assertIn("403", result["error"])
        self.assertIn("try a different", result["note"])

        followup_history = request_completion.call_args_list[1].args[1]
        tool_result = next(
            step for step in followup_history if step.get("type") == "function_result"
        )
        self.assertEqual("visit_page", tool_result["name"])
        self.assertEqual("call_1", tool_result["call_id"])
        self.assertEqual(result, json.loads(tool_result["result"][0]["text"]))

        openrouter_messages = _history_to_openrouter(followup_history)
        tool_message = next(
            item for item in openrouter_messages if item.get("role") == "tool"
        )
        self.assertEqual("call_1", tool_message["tool_call_id"])
        self.assertEqual("visit_page", tool_message["name"])
        self.assertEqual(analytics["tool_results"][0]["result"], tool_message["content"])


class GoogleProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_google_tools_include_custom_functions(self):
        google_tools = _google_tools()
        tool_names = [
            declaration.name
            for tool in google_tools
            for declaration in (tool.function_declarations or [])
        ]
        self.assertIn("reminder", tool_names)
        self.assertIn("interim_message", tool_names)
        self.assertIn("web_search", tool_names)
        self.assertIn("visit_page", tool_names)
        self.assertIn("read_channel_history", tool_names)
        self.assertIn("send_file", tool_names)
        self.assertTrue(any(tool.code_execution is not None for tool in google_tools))

    def test_google_config_enables_hybrid_code_execution(self):
        config = _google_generate_config("system")
        self.assertTrue(config.tool_config.include_server_side_tool_invocations)
        self.assertEqual(
            types.FunctionCallingConfigMode.VALIDATED,
            config.tool_config.function_calling_config.mode,
        )
        self.assertTrue(
            any(tool.code_execution is not None for tool in config.tools)
        )

    def test_history_to_google_preserves_thought_signatures_and_json_results(self):
        signature = base64.b64encode(b"sig-bytes").decode("ascii")
        history = [
            {
                "type": "user_input",
                "content": [{"type": "text", "text": "bushima: next usc break?"}],
            },
            {
                "type": "thought",
                "summary": [{"type": "text", "text": "look it up"}],
                "signature": signature,
            },
            {
                "type": "function_call",
                "id": "call_1",
                "name": "visit_page",
                "arguments": {"url": "https://usc.edu/academic-calendar/"},
                "thought_signature": signature,
            },
            {
                "type": "function_result",
                "name": "visit_page",
                "call_id": "call_1",
                "result": [{
                    "type": "text",
                    "text": json.dumps({
                        "url": "https://usc.edu/academic-calendar/",
                        "error": "HTTP 403 Forbidden",
                    }),
                }],
            },
            {
                "type": "assistant_message",
                "content": [{"type": "text", "text": "that page blocked me"}],
            },
        ]

        contents = _history_to_google(history)

        self.assertEqual("user", contents[0].role)
        self.assertEqual("bushima: next usc break?", contents[0].parts[0].text)
        self.assertEqual("model", contents[1].role)
        self.assertTrue(contents[1].parts[0].thought)
        self.assertEqual(b"sig-bytes", contents[1].parts[0].thought_signature)
        self.assertEqual("visit_page", contents[1].parts[1].function_call.name)
        self.assertEqual(b"sig-bytes", contents[1].parts[1].thought_signature)
        self.assertEqual("user", contents[2].role)
        self.assertEqual("visit_page", contents[2].parts[0].function_response.name)
        self.assertEqual("call_1", contents[2].parts[0].function_response.id)
        self.assertEqual(
            "HTTP 403 Forbidden",
            contents[2].parts[0].function_response.response["error"],
        )
        self.assertEqual("model", contents[3].role)
        self.assertEqual("that page blocked me", contents[3].parts[0].text)

    def test_google_response_to_completion_extracts_tools_and_thoughts(self):
        signature = b"sig-bytes"
        response = SimpleNamespace(
            model_version="gemini-2.5-flash",
            usage_metadata=SimpleNamespace(
                prompt_token_count=11,
                candidates_token_count=4,
                thoughts_token_count=3,
            ),
            candidates=[SimpleNamespace(
                content=SimpleNamespace(parts=[
                    types.Part(text="I should visit the calendar.", thought=True),
                    types.Part(
                        function_call=types.FunctionCall(
                            id="call_1",
                            name="visit_page",
                            args={"url": "https://usc.edu/academic-calendar/"},
                        ),
                        thought_signature=signature,
                    ),
                ]),
            )],
        )

        completion = _google_response_to_completion(response)
        message = completion["choices"][0]["message"]

        self.assertEqual("gemini-2.5-flash", completion["model"])
        self.assertEqual(11, completion["usage"]["prompt_tokens"])
        self.assertEqual(3, completion["usage"]["completion_tokens_details"]["reasoning_tokens"])
        self.assertEqual(
            "I should visit the calendar.",
            message["reasoning_details"][0]["summary"],
        )
        self.assertEqual("visit_page", message["tool_calls"][0]["function"]["name"])
        self.assertEqual(
            base64.b64encode(signature).decode("ascii"),
            message["tool_calls"][0]["thought_signature"],
        )

    def test_google_response_to_completion_extracts_code_execution(self):
        signature = b"sig-bytes"
        response = SimpleNamespace(
            model_version="gemini-3.5-flash",
            usage_metadata=SimpleNamespace(
                prompt_token_count=11,
                candidates_token_count=4,
                thoughts_token_count=3,
            ),
            candidates=[SimpleNamespace(
                content=SimpleNamespace(parts=[
                    types.Part(
                        executable_code=types.ExecutableCode(
                            id="code_1",
                            language=types.Language.PYTHON,
                            code="print(6 * 7)",
                        ),
                        thought_signature=signature,
                    ),
                    types.Part(
                        code_execution_result=types.CodeExecutionResult(
                            id="code_1",
                            outcome=types.Outcome.OUTCOME_OK,
                            output="42\n",
                        ),
                    ),
                    types.Part(text="it's 42."),
                ]),
            )],
        )

        completion = _google_response_to_completion(response)
        message = completion["choices"][0]["message"]

        self.assertEqual("print(6 * 7)", message["executable_code_parts"][0]["code"])
        self.assertEqual("PYTHON", message["executable_code_parts"][0]["language"])
        self.assertEqual("42\n", message["code_execution_result_parts"][0]["output"])
        self.assertEqual("OUTCOME_OK", message["code_execution_result_parts"][0]["outcome"])
        self.assertEqual("it's 42.", message["content"])

    def test_history_to_google_replays_code_execution_in_the_same_model_turn(self):
        signature = base64.b64encode(b"sig-bytes").decode("ascii")
        history = [
            {
                "type": "user_input",
                "content": [{"type": "text", "text": "bushima: 6 times 7?"}],
            },
            {
                "type": "executable_code",
                "id": "code_1",
                "language": "PYTHON",
                "code": "print(6 * 7)",
                "thought_signature": signature,
            },
            {
                "type": "code_execution_result",
                "id": "code_1",
                "outcome": "OUTCOME_OK",
                "output": "42\n",
            },
            {
                "type": "assistant_message",
                "content": [{"type": "text", "text": "it's 42."}],
            },
        ]

        contents = _history_to_google(history)

        self.assertEqual(2, len(contents))
        self.assertEqual("user", contents[0].role)
        self.assertEqual("model", contents[1].role)
        self.assertEqual("print(6 * 7)", contents[1].parts[0].executable_code.code)
        self.assertEqual(types.Language.PYTHON, contents[1].parts[0].executable_code.language)
        self.assertEqual(b"sig-bytes", contents[1].parts[0].thought_signature)
        self.assertEqual("42\n", contents[1].parts[1].code_execution_result.output)
        self.assertEqual("it's 42.", contents[1].parts[2].text)

    async def test_request_completion_uses_google_by_default(self):
        with (
            patch("src.response.RESPONSE_PROVIDER", "google"),
            patch(
                "src.response._request_google",
                new=AsyncMock(return_value={"choices": []}),
            ) as request_google,
            patch(
                "src.response._request_openrouter",
                new=AsyncMock(),
            ) as request_openrouter,
        ):
            await _request_completion("system", [])

        request_google.assert_awaited_once()
        request_openrouter.assert_not_called()

    async def test_request_completion_can_switch_to_openrouter(self):
        with (
            patch("src.response.RESPONSE_PROVIDER", "openrouter"),
            patch(
                "src.response._request_google",
                new=AsyncMock(),
            ) as request_google,
            patch(
                "src.response._request_openrouter",
                new=AsyncMock(return_value={"choices": []}),
            ) as request_openrouter,
        ):
            await _request_completion("system", [])

        request_openrouter.assert_awaited_once()
        request_google.assert_not_called()

    async def test_request_completion_forwards_google_model(self):
        with (
            patch("src.response.RESPONSE_PROVIDER", "google"),
            patch(
                "src.response._request_google",
                new=AsyncMock(return_value={"choices": []}),
            ) as request_google,
        ):
            await _request_completion(
                "system",
                [],
                model="gemini-3.5-flash",
                provider="google",
            )

        request_google.assert_awaited_once_with(
            "system",
            [],
            model="gemini-3.5-flash",
        )

    def make_unavailable_error(self):
        return genai_errors.ServerError(
            503,
            {
                "error": {
                    "code": 503,
                    "message": "This model is currently experiencing high demand.",
                    "status": "UNAVAILABLE",
                },
            },
        )

    def make_google_text_response(self, text="ok"):
        return SimpleNamespace(
            model_version="gemini-3.5-flash",
            usage_metadata=SimpleNamespace(
                prompt_token_count=1,
                candidates_token_count=1,
                thoughts_token_count=0,
            ),
            candidates=[SimpleNamespace(
                content=SimpleNamespace(parts=[types.Part(text=text)]),
            )],
        )

    async def test_request_google_retries_identical_503_request(self):
        generate_content = AsyncMock(side_effect=[
            self.make_unavailable_error(),
            self.make_google_text_response("ok"),
        ])
        client = MagicMock()
        client.aio.models.generate_content = generate_content

        with (
            patch("src.response._google_client", return_value=client),
            patch("src.response.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            completion = await _request_google(
                "system",
                [{
                    "type": "user_input",
                    "content": [{"type": "text", "text": "hello"}],
                }],
                model="gemini-3.5-flash",
            )

        self.assertEqual("ok", completion["choices"][0]["message"]["content"])
        sleep.assert_awaited_once_with(5)
        self.assertEqual(2, generate_content.await_count)
        first_kwargs = generate_content.await_args_list[0].kwargs
        second_kwargs = generate_content.await_args_list[1].kwargs
        self.assertEqual("gemini-3.5-flash", first_kwargs["model"])
        self.assertIs(first_kwargs["contents"], second_kwargs["contents"])
        self.assertIs(first_kwargs["config"], second_kwargs["config"])
        self.assertEqual(first_kwargs["model"], second_kwargs["model"])

    async def test_request_google_does_not_retry_non_503(self):
        error = genai_errors.ClientError(
            400,
            {"error": {"code": 400, "status": "INVALID_ARGUMENT"}},
        )
        generate_content = AsyncMock(side_effect=error)
        client = MagicMock()
        client.aio.models.generate_content = generate_content

        with (
            patch("src.response._google_client", return_value=client),
            patch("src.response.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(genai_errors.ClientError):
                await _request_google("system", [], model="gemini-3.5-flash")

        sleep.assert_not_called()
        self.assertEqual(1, generate_content.await_count)

    async def test_request_google_raises_after_exhausted_503_retries(self):
        generate_content = AsyncMock(side_effect=self.make_unavailable_error())
        client = MagicMock()
        client.aio.models.generate_content = generate_content

        with (
            patch("src.response._google_client", return_value=client),
            patch("src.response.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(genai_errors.ServerError):
                await _request_google("system", [], model="gemini-3.5-flash")

        self.assertEqual(3, generate_content.await_count)
        self.assertEqual(2, sleep.await_count)
        sleep.assert_awaited_with(5)

    async def test_get_response_dumps_thought_parts_for_google_replay(self):
        message = SimpleNamespace(
            id=100,
            attachments=[],
            author=SimpleNamespace(id=1, name="bushima"),
            channel=SimpleNamespace(id=10),
            content="next break?",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            mentions=[],
        )
        memory = MemoryManager(max_messages=10)
        await memory.add_message(message)

        first = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": json.dumps({"query": "usc break"}),
                        },
                        "thought_signature": "abc",
                    }],
                    "reasoning_details": [{
                        "type": "reasoning.summary",
                        "summary": "look it up",
                    }],
                    "thought_parts": [{
                        "text": "look it up",
                        "thought_signature": "abc",
                    }],
                },
            }],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
            "model": "gemini-2.5-flash",
        }
        second = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "labor day.",
                    "tool_calls": [],
                    "reasoning_details": [],
                },
            }],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
            "model": "gemini-2.5-flash",
        }

        with (
            patch("src.response.build_prompt", new=AsyncMock(return_value=(
                "system",
                [{
                    "type": "user_input",
                    "content": [{"type": "text", "text": "bushima: next break?"}],
                }],
            ))),
            patch(
                "src.response._request_completion",
                new=AsyncMock(side_effect=[first, second]),
            ),
            patch(
                "src.response.execute_tool_call",
                new=AsyncMock(return_value='{"results": []}'),
            ),
        ):
            output, analytics = await get_response(message, memory=memory)

        self.assertEqual(["labor day"], output)
        stored = memory.get_messages(message.channel.id)[0]
        step_types = [step["type"] for step in stored.steps_dump]
        self.assertEqual(
            [
                "user_input",
                "thought",
                "function_call",
                "function_result",
                "assistant_message",
            ],
            step_types,
        )
        self.assertEqual("abc", stored.steps_dump[1]["signature"])
        self.assertEqual("abc", stored.steps_dump[2]["thought_signature"])
        self.assertEqual("look it up", analytics["thought_signatures"][0])

    async def test_get_response_dumps_google_code_execution(self):
        message = SimpleNamespace(
            id=100,
            attachments=[],
            author=SimpleNamespace(id=1, name="bushima"),
            channel=SimpleNamespace(id=10),
            content="what is 6 times 7?",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            mentions=[],
        )
        memory = MemoryManager(max_messages=10)
        await memory.add_message(message)

        completion = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "it's 42.",
                    "tool_calls": [],
                    "reasoning_details": [],
                    "executable_code_parts": [{
                        "id": "code_1",
                        "language": "PYTHON",
                        "code": "print(6 * 7)",
                        "thought_signature": base64.b64encode(b"sig-bytes").decode("ascii"),
                    }],
                    "code_execution_result_parts": [{
                        "id": "code_1",
                        "outcome": "OUTCOME_OK",
                        "output": "42\n",
                    }],
                },
            }],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
            "model": "gemini-3.5-flash",
        }

        with (
            patch("src.response.build_prompt", new=AsyncMock(return_value=(
                "system",
                [{
                    "type": "user_input",
                    "content": [{"type": "text", "text": "bushima: what is 6 times 7?"}],
                }],
            ))),
            patch(
                "src.response._request_completion",
                new=AsyncMock(return_value=completion),
            ),
        ):
            output, analytics = await get_response(message, memory=memory)

        self.assertEqual(["it's 42"], output)
        self.assertEqual(1, analytics["tool_steps"])
        self.assertEqual("code_execution", analytics["tool_results"][0]["name"])
        self.assertEqual("print(6 * 7)", analytics["tool_results"][0]["arguments"]["code"])
        self.assertEqual("42\n", analytics["tool_results"][0]["result"])

        stored = memory.get_messages(message.channel.id)[0]
        step_types = [step["type"] for step in stored.steps_dump]
        self.assertEqual(
            [
                "user_input",
                "executable_code",
                "code_execution_result",
                "assistant_message",
            ],
            step_types,
        )
        google_contents = _history_to_google(stored.steps_dump)
        self.assertEqual("print(6 * 7)", google_contents[1].parts[0].executable_code.code)
        self.assertEqual("42\n", google_contents[1].parts[1].code_execution_result.output)
        self.assertEqual("it's 42.", google_contents[1].parts[2].text)


class LockInModelTests(unittest.TestCase):
    def test_detects_lock_in_after_bot_mention(self):
        message = SimpleNamespace(content="<@99> lock in, compare apples")
        self.assertTrue(is_lock_in_request(message))

    def test_detects_lock_in_case_insensitive_with_cleaned_mention(self):
        message = SimpleNamespace(content="@nabemono LOCK IN compare apples")
        self.assertTrue(is_lock_in_request(message))

    def test_ignores_lock_in_when_it_is_not_the_start(self):
        message = SimpleNamespace(content="<@99> please lock in later")
        self.assertFalse(is_lock_in_request(message))

    def test_ignores_locking_in(self):
        message = SimpleNamespace(content="<@99> locking in")
        self.assertFalse(is_lock_in_request(message))

    def test_strips_lock_in_but_keeps_the_bot_mention(self):
        self.assertEqual(
            "@nabemono compare apples",
            strip_lock_in_prefix("@nabemono lock in, compare apples"),
        )
        self.assertEqual(
            "<@99> compare apples",
            strip_lock_in_prefix("<@99> LOCK IN: compare apples"),
        )

    def test_strips_lock_in_from_reply_context_history(self):
        history = [{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": '[REPLIED TO MESSAGE: "old"]\nbushima: @nabemono lock in, compare apples',
            }],
        }]
        _strip_lock_in_from_history(
            history,
            SimpleNamespace(author=SimpleNamespace(name="bushima")),
        )
        self.assertEqual(
            '[REPLIED TO MESSAGE: "old"]\nbushima: @nabemono compare apples',
            history[0]["content"][0]["text"],
        )


class LockInResponseTests(unittest.IsolatedAsyncioTestCase):
    def make_message(self, content):
        return SimpleNamespace(
            id=100,
            attachments=[],
            author=SimpleNamespace(id=1, name="bushima"),
            channel=SimpleNamespace(id=10),
            content=content,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            mentions=[SimpleNamespace(id=99, name="nabemono")],
        )

    async def test_get_response_uses_strong_google_model_for_lock_in(self):
        message = self.make_message("<@99> lock in, compare apples")
        memory = MemoryManager(max_messages=10)
        await memory.add_message(message)
        history = [{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": "bushima: @nabemono lock in, compare apples",
            }],
        }]
        completion = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "apple.",
                    "tool_calls": [],
                    "reasoning_details": [],
                },
            }],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
            "model": "gemini-3.5-flash",
        }

        with (
            patch("src.response.RESPONSE_PROVIDER", "openrouter"),
            patch("src.response.GOOGLE_STRONG_RESPONSE_MODEL", "gemini-3.5-flash"),
            patch(
                "src.response.build_prompt",
                new=AsyncMock(return_value=("system", history)),
            ),
            patch(
                "src.response._request_completion",
                new=AsyncMock(return_value=completion),
            ) as request_completion,
        ):
            output, analytics = await get_response(message, memory=memory)

        self.assertEqual(["apple"], output)
        self.assertEqual("gemini-3.5-flash", analytics["model"])
        self.assertEqual("system", request_completion.await_args.args[0])
        self.assertEqual(
            "bushima: @nabemono compare apples",
            request_completion.await_args.args[1][0]["content"][0]["text"],
        )
        self.assertEqual(
            "gemini-3.5-flash",
            request_completion.await_args.kwargs["model"],
        )
        self.assertEqual("google", request_completion.await_args.kwargs["provider"])

    async def test_get_response_keeps_default_model_without_lock_in(self):
        message = self.make_message("<@99> compare apples")
        memory = MemoryManager(max_messages=10)
        await memory.add_message(message)
        completion = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "apple.",
                    "tool_calls": [],
                    "reasoning_details": [],
                },
            }],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
            "model": "gemini-3.5-flash-lite",
        }

        with (
            patch("src.response.RESPONSE_PROVIDER", "google"),
            patch("src.response.GOOGLE_RESPONSE_MODEL", "gemini-3.5-flash-lite"),
            patch(
                "src.response.build_prompt",
                new=AsyncMock(return_value=("system", [{
                    "type": "user_input",
                    "content": [{
                        "type": "text",
                        "text": "bushima: @nabemono compare apples",
                    }],
                }])),
            ),
            patch(
                "src.response._request_completion",
                new=AsyncMock(return_value=completion),
            ) as request_completion,
        ):
            await get_response(message, memory=memory)

        self.assertEqual(
            "gemini-3.5-flash-lite",
            request_completion.await_args.kwargs["model"],
        )
        self.assertEqual("google", request_completion.await_args.kwargs["provider"])


if __name__ == "__main__":
    unittest.main()
