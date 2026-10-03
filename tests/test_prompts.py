import base64
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.prompts import build_prompt, message_to_turn
from src.short_term_memory import StoredImage, StoredMessage


class PromptTests(unittest.TestCase):
    def make_message(self, images=None, steps_dump=None):
        author = SimpleNamespace(id=1, name="bushima")
        channel = SimpleNamespace(id=10)
        mention = SimpleNamespace(id=2, name="friend")
        return StoredMessage(
            id=100,
            attachments=[],
            author=author,
            channel=channel,
            content="caption this <@2>",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            mentions=[mention],
            images=images,
            steps_dump=steps_dump,
        )

    def test_message_to_turn_includes_cached_images(self):
        with TemporaryDirectory() as cache_dir:
            image_path = Path(cache_dir) / "image.jpg"
            image_path.write_bytes(b"jpeg-bytes")
            image = StoredImage(
                attachment_id=200,
                filename="image.jpg",
                content_type="image/jpeg",
                size=10,
                cache_path=image_path,
            )

            turns = message_to_turn(self.make_message(images=[image]))

            self.assertEqual(1, len(turns))
            self.assertEqual("user_input", turns[0]["type"])
            self.assertEqual(
                [
                    {
                        "type": "text",
                        "text": "bushima: caption this @friend",
                    },
                    {
                        "type": "image",
                        "data": base64.b64encode(b"jpeg-bytes").decode("utf-8"),
                        "mime_type": "image/jpeg",
                    },
                ],
                turns[0]["content"],
            )

    def test_message_to_turn_skips_missing_cached_images(self):
        image = StoredImage(
            attachment_id=200,
            filename="missing.jpg",
            content_type="image/jpeg",
            size=10,
            cache_path="/tmp/does-not-exist-nabemono-image.jpg",
        )

        with patch("builtins.print"):
            turns = message_to_turn(self.make_message(images=[image]))

        self.assertEqual(
            [{"type": "text", "text": "bushima: caption this @friend"}],
            turns[0]["content"],
        )

    def test_message_to_turn_prefers_steps_dump(self):
        steps_dump = [{"type": "assistant_message", "content": "cached"}]

        self.assertIs(
            steps_dump,
            message_to_turn(self.make_message(steps_dump=steps_dump)),
        )

    def test_message_to_turn_keeps_image_message_when_steps_dump_exists(self):
        with TemporaryDirectory() as cache_dir:
            image_path = Path(cache_dir) / "image.jpg"
            image_path.write_bytes(b"jpeg-bytes")
            image = StoredImage(
                attachment_id=200,
                filename="image.jpg",
                content_type="image/jpeg",
                size=10,
                cache_path=image_path,
            )
            message = self.make_message(
                images=[image],
                steps_dump=[{"type": "assistant_message", "content": "cached"}],
            )

            turns = message_to_turn(message)

            self.assertEqual("user_input", turns[0]["type"])
            self.assertEqual(
                [
                    {"type": "text", "text": "bushima: caption this @friend"},
                    {
                        "type": "image",
                        "data": base64.b64encode(b"jpeg-bytes").decode("utf-8"),
                        "mime_type": "image/jpeg",
                    },
                ],
                turns[0]["content"],
            )


class ReplyContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_prompt_prepends_replied_message_to_current_turn(self):
        author = SimpleNamespace(id=1, name="bushima")
        channel = SimpleNamespace(id=10)
        message = StoredMessage(
            id=100,
            attachments=[],
            author=author,
            channel=channel,
            content="<@9> what do you mean?",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            mentions=[SimpleNamespace(id=9, name="nabemono")],
        )
        message.reference = SimpleNamespace(
            message_id=99,
            resolved=SimpleNamespace(content="the original message"),
        )
        memory = SimpleNamespace(get_messages=lambda *args: [message])

        with (
            patch("src.prompts.load_system_prompt", return_value="{long_term_memory} {affection} {time}"),
            patch("src.prompts.load_affection", return_value="neutral"),
            patch("builtins.print"),
        ):
            _, turns = await build_prompt(message, memory=memory)

        self.assertEqual(
            (
                '[REPLIED TO MESSAGE: "the original message"]\n'
                "bushima: @nabemono what do you mean?"
            ),
            turns[0]["content"][0]["text"],
        )


if __name__ == "__main__":
    unittest.main()
