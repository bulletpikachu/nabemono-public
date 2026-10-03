import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.persona.store import PersonaFact
from src.tools import available_functions, execute_tool_call, persona_search


class PersonaToolTests(unittest.IsolatedAsyncioTestCase):
    def test_registers_persona_search(self):
        self.assertIn("persona_search", available_functions)

    async def test_returns_authoritative_match(self):
        fact = PersonaFact(
            id=1,
            key="favorite_color",
            value="Green.",
            aliases=("favorite colour",),
            created_at="2026-01-01",
            updated_at="2026-01-01",
            match="alias",
        )
        with patch(
            "src.persona.service.persona_store.search",
            Mock(return_value=[fact]),
        ):
            payload = json.loads(await persona_search(None, "favorite colour"))

        self.assertTrue(payload["found"])
        self.assertEqual("favorite_color", payload["matches"][0]["key"])
        self.assertEqual("Green.", payload["matches"][0]["value"])
        self.assertNotIn("aliases", payload["matches"][0])
        self.assertEqual("These facts are authoritative.", payload["note"])

    async def test_returns_explicit_no_match(self):
        with patch(
            "src.persona.service.persona_store.search",
            Mock(return_value=[]),
        ):
            payload = json.loads(await persona_search(None, "birthday"))

        self.assertFalse(payload["found"])
        self.assertEqual([], payload["matches"])
        self.assertIn("Do not invent", payload["note"])

    async def test_execute_tool_call_dispatches_persona_search(self):
        tool_call = SimpleNamespace(
            name="persona_search",
            arguments={"query": "favorite color"},
        )
        with patch(
            "src.tools.persona_search",
            return_value='{"found": false}',
        ) as handler:
            available_functions["persona_search"] = handler
            try:
                result = await execute_tool_call(None, tool_call)
            finally:
                available_functions["persona_search"] = persona_search

        handler.assert_awaited_once_with(None, query="favorite color")
        self.assertEqual('{"found": false}', result)


if __name__ == "__main__":
    unittest.main()
