import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src.short_term_memory import MemoryManager, StoredImage, StoredMessage


class FakeAttachment:
    def __init__(
        self,
        id=200,
        filename="image.png",
        content_type="image/png",
        data=b"image-bytes",
        size=None,
        fail=False,
    ):
        self.id = id
        self.filename = filename
        self.content_type = content_type
        self._data = data
        self.size = len(data) if size is None else size
        self.fail = fail

    async def read(self):
        if self.fail:
            raise RuntimeError("download failed")
        return self._data


class ShortTermMemoryTests(unittest.IsolatedAsyncioTestCase):
    def make_discord_message(self, id=100, channel_id=10, attachments=None):
        author = SimpleNamespace(id=1, name="bushima")
        channel = SimpleNamespace(id=channel_id)
        mention = SimpleNamespace(id=2, name="friend")
        return SimpleNamespace(
            id=id,
            attachments=tuple(attachments or ()),
            author=author,
            channel=channel,
            content="hello <@2> — hi",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            mentions=(mention,),
        )

    async def test_add_message_converts_discord_message_to_stored_message(self):
        memory = MemoryManager(max_messages=10)
        discord_message = self.make_discord_message()

        await memory.add_message(discord_message)

        messages = memory.get_messages(discord_message.channel.id)
        self.assertEqual(1, len(messages))
        self.assertIsInstance(messages[0], StoredMessage)
        self.assertEqual(discord_message.id, messages[0].id)
        self.assertEqual("hello <@2>  hi", messages[0].content)
        self.assertEqual(list(discord_message.attachments), messages[0].attachments)
        self.assertEqual(list(discord_message.mentions), messages[0].mentions)
        self.assertEqual([], messages[0].images)
        self.assertEqual([], messages[0].steps_dump)

    async def test_add_message_accepts_existing_stored_message(self):
        memory = MemoryManager(max_messages=10)
        stored_message = StoredMessage.from_discord(self.make_discord_message())

        await memory.add_message(stored_message)

        self.assertIs(stored_message, memory.get_messages(stored_message.channel.id)[0])

    def test_stored_message_defaults_images_to_empty_list(self):
        stored_message = StoredMessage.from_discord(self.make_discord_message())

        self.assertEqual([], stored_message.images)

    async def test_add_message_caches_image_attachments_to_disk(self):
        with TemporaryDirectory() as cache_dir:
            memory = MemoryManager(max_messages=10, image_cache_dir=cache_dir)
            image = FakeAttachment(data=b"png-data")
            discord_message = self.make_discord_message(attachments=[image])

            await memory.add_message(discord_message)

            stored_message = memory.get_messages(discord_message.channel.id)[0]
            self.assertEqual(1, len(stored_message.images))
            stored_image = stored_message.images[0]
            self.assertIsInstance(stored_image, StoredImage)
            self.assertEqual(image.id, stored_image.attachment_id)
            self.assertEqual(image.filename, stored_image.filename)
            self.assertEqual(image.content_type, stored_image.content_type)
            self.assertEqual(image.size, stored_image.size)
            self.assertEqual(b"png-data", Path(stored_image.cache_path).read_bytes())

    async def test_add_message_ignores_non_image_attachments(self):
        with TemporaryDirectory() as cache_dir:
            memory = MemoryManager(max_messages=10, image_cache_dir=cache_dir)
            attachment = FakeAttachment(
                id=201,
                filename="notes.txt",
                content_type="text/plain",
                data=b"text",
            )
            discord_message = self.make_discord_message(attachments=[attachment])

            await memory.add_message(discord_message)

            stored_message = memory.get_messages(discord_message.channel.id)[0]
            self.assertEqual([], stored_message.images)
            self.assertEqual([], list(Path(cache_dir).iterdir()))

    async def test_eviction_removes_cached_image_files(self):
        with TemporaryDirectory() as cache_dir:
            memory = MemoryManager(max_messages=1, image_cache_dir=cache_dir)
            first = self.make_discord_message(
                id=100,
                attachments=[FakeAttachment(id=200, filename="first.png")],
            )
            second = self.make_discord_message(
                id=101,
                attachments=[FakeAttachment(id=201, filename="second.png")],
            )

            await memory.add_message(first)
            first_path = Path(
                memory.get_messages(first.channel.id)[0].images[0].cache_path
            )
            self.assertTrue(first_path.exists())

            await memory.add_message(second)

            self.assertFalse(first_path.exists())
            messages = memory.get_messages(first.channel.id)
            self.assertEqual([101], [message.id for message in messages])
            self.assertTrue(Path(messages[0].images[0].cache_path).exists())

    async def test_pop_removes_cached_image_files(self):
        with TemporaryDirectory() as cache_dir:
            memory = MemoryManager(max_messages=10, image_cache_dir=cache_dir)
            discord_message = self.make_discord_message(
                attachments=[FakeAttachment(filename="pop.png")]
            )

            await memory.add_message(discord_message)
            cache_path = Path(
                memory.get_messages(discord_message.channel.id)[0]
                .images[0]
                .cache_path
            )

            popped = memory.pop(discord_message.channel.id)

            self.assertEqual(discord_message.id, popped.id)
            self.assertFalse(cache_path.exists())

    async def test_clear_removes_cached_image_files(self):
        with TemporaryDirectory() as cache_dir:
            memory = MemoryManager(max_messages=10, image_cache_dir=cache_dir)
            discord_message = self.make_discord_message(
                attachments=[FakeAttachment(filename="clear.png")]
            )

            await memory.add_message(discord_message)
            cache_path = Path(
                memory.get_messages(discord_message.channel.id)[0]
                .images[0]
                .cache_path
            )

            memory.clear()

            self.assertFalse(cache_path.exists())
            self.assertEqual([], memory.get_messages(discord_message.channel.id))

    async def test_attachment_read_failure_stores_message_without_image(self):
        with TemporaryDirectory() as cache_dir:
            memory = MemoryManager(max_messages=10, image_cache_dir=cache_dir)
            discord_message = self.make_discord_message(
                attachments=[FakeAttachment(fail=True)]
            )

            with patch("builtins.print"):
                await memory.add_message(discord_message)

            stored_message = memory.get_messages(discord_message.channel.id)[0]
            self.assertEqual(discord_message.id, stored_message.id)
            self.assertEqual([], stored_message.images)
            self.assertEqual([], list(Path(cache_dir).iterdir()))

    async def test_dump_steps_accepts_dicts_and_model_dump_objects(self):
        memory = MemoryManager(max_messages=10)
        discord_message = self.make_discord_message()
        await memory.add_message(discord_message)

        step_obj = SimpleNamespace()
        step_obj.model_dump = lambda: {"type": "function_call", "name": "reminder"}

        memory.dump_steps(
            discord_message,
            [
                {"type": "user_input", "content": [{"type": "text", "text": "hi"}]},
                step_obj,
                {
                    "type": "function_result",
                    "name": "reminder",
                    "call_id": "call_1",
                    "result": [{"type": "text", "text": "ok"}],
                },
            ],
        )

        self.assertEqual(
            [
                {"type": "user_input", "content": [{"type": "text", "text": "hi"}]},
                {"type": "function_call", "name": "reminder"},
                {
                    "type": "function_result",
                    "name": "reminder",
                    "call_id": "call_1",
                    "result": [{"type": "text", "text": "ok"}],
                },
            ],
            memory.get_messages(discord_message.channel.id)[0].steps_dump,
        )


if __name__ == "__main__":
    unittest.main()
