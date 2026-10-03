import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.maps import (
    UCI_LATITUDE,
    UCI_LONGITUDE,
    format_maps_response,
    maps_location,
    search_maps,
)
from src.tools import execute_tool_call


def text_block(text, annotations=None):
    return SimpleNamespace(type="text", text=text, annotations=annotations or [])


def place(name, url):
    return SimpleNamespace(type="place_citation", name=name, url=url)


class MapsLocationTests(unittest.TestCase):
    def test_defaults_to_uci(self):
        self.assertEqual(
            (UCI_LATITUDE, UCI_LONGITUDE, "UC Irvine"),
            maps_location(),
        )

    def test_uses_an_explicit_place(self):
        self.assertEqual(
            (37.78, -122.4, None),
            maps_location(37.78, -122.4),
        )

    def test_rejects_a_partial_or_invalid_place(self):
        with self.assertRaises(ValueError):
            maps_location(33.6, None)
        with self.assertRaises(ValueError):
            maps_location(120, -117.8)


class FormatMapsResponseTests(unittest.TestCase):
    def test_keeps_answer_and_unique_place_citations(self):
        interaction = SimpleNamespace(
            output_text="ignored when steps have text",
            steps=[
                SimpleNamespace(
                    type="model_output",
                    content=[
                        text_block(
                            "Anteatery is on campus.",
                            [
                                place("Anteatery", "https://maps.google.com/?cid=1"),
                                place("Anteatery", "https://maps.google.com/?cid=1"),
                            ],
                        )
                    ],
                )
            ],
        )

        payload = format_maps_response(
            interaction,
            query="food near here",
            latitude=UCI_LATITUDE,
            longitude=UCI_LONGITUDE,
            location_name="UC Irvine",
        )

        self.assertEqual("Anteatery is on campus.", payload["answer"])
        self.assertEqual("UC Irvine", payload["location"]["name"])
        self.assertEqual(
            [{"name": "Anteatery", "url": "https://maps.google.com/?cid=1"}],
            payload["sources"],
        )
        self.assertIn("Google Maps", payload["note"])

    def test_falls_back_to_output_text(self):
        payload = format_maps_response(
            SimpleNamespace(output_text="No places nearby.", steps=[]),
            query="food",
            latitude=1,
            longitude=2,
            location_name=None,
        )

        self.assertEqual("No places nearby.", payload["answer"])
        self.assertNotIn("name", payload["location"])
        self.assertNotIn("note", payload)


class MapsSearchToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_tool_call_defaults_to_uci(self):
        interaction = SimpleNamespace(
            output_text="",
            steps=[
                SimpleNamespace(
                    type="model_output",
                    content=[
                        text_block(
                            "UTC has several places to eat.",
                            [place("UTC", "https://maps.google.com/?cid=2")],
                        )
                    ],
                )
            ],
        )
        client = SimpleNamespace(
            aio=SimpleNamespace(
                interactions=SimpleNamespace(create=AsyncMock(return_value=interaction))
            )
        )
        tool_call = SimpleNamespace(
            name="maps_search",
            arguments={"query": " places to eat "},
        )

        with (
            patch("src.maps._maps_client", return_value=client),
            patch("src.maps.GOOGLE_RESPONSE_MODEL", "gemini-test"),
            patch("builtins.print"),
        ):
            result = json.loads(await execute_tool_call(SimpleNamespace(), tool_call))

        client.aio.interactions.create.assert_awaited_once_with(
            model="gemini-test",
            input="places to eat",
            tools=[
                {
                    "type": "google_maps",
                    "latitude": UCI_LATITUDE,
                    "longitude": UCI_LONGITUDE,
                }
            ],
        )
        self.assertEqual("UTC has several places to eat.", result["answer"])
        self.assertEqual("UC Irvine", result["location"]["name"])

    async def test_search_maps_passes_requested_coordinates(self):
        client = SimpleNamespace(
            aio=SimpleNamespace(
                interactions=SimpleNamespace(
                    create=AsyncMock(return_value=SimpleNamespace(output_text="ok", steps=[]))
                )
            )
        )

        payload = await search_maps(
            "coffee",
            latitude=34.05,
            longitude=-118.25,
            client=client,
            model="gemini-test",
        )

        client.aio.interactions.create.assert_awaited_once_with(
            model="gemini-test",
            input="coffee",
            tools=[
                {
                    "type": "google_maps",
                    "latitude": 34.05,
                    "longitude": -118.25,
                }
            ],
        )
        self.assertNotIn("name", payload["location"])


if __name__ == "__main__":
    unittest.main()
