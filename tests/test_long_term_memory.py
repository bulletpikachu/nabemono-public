import asyncio
import tempfile
import unittest
from pathlib import Path

import numpy as np

from src.long_term_memory import LongTermMemory


class FakeEmbedder:
    def embed(self, texts):
        for text in texts:
            lower = text.lower()
            if "wide" in lower:
                yield np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
            elif "similar" in lower:
                yield np.array([0.99, 0.14, 0.0], dtype=np.float32)
            elif "short" in lower or "terse" in lower:
                yield np.array([1.0, 0.0, 0.0], dtype=np.float32)
            elif "music" in lower:
                yield np.array([0.0, 1.0, 0.0], dtype=np.float32)
            else:
                yield np.array([0.0, 0.0, 1.0], dtype=np.float32)


class LongTermMemoryTests(unittest.TestCase):
    def make_memory(self, db_path):
        return LongTermMemory(
            db_path=db_path,
            embedding_model="fake-model",
            top_k=5,
            min_score=0.35,
            embedder=FakeEmbedder(),
        )

    def test_embedding_serialization_round_trip(self):
        vector = np.array([0.25, 0.5, 0.75], dtype=np.float32)
        blob = LongTermMemory._serialize_embedding(vector)
        restored = LongTermMemory._deserialize_embedding(blob)
        np.testing.assert_array_equal(vector, restored)

    def test_remember_and_search_ranks_relevant_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self.make_memory(Path(temp_dir) / "memory.sqlite3")
            short_id = memory.remember(
                user_id=1,
                username="bushima",
                channel_id=10,
                content="bushima prefers short answers",
                source_message_id=100,
                importance=2,
            )
            memory.remember(
                user_id=1,
                username="bushima",
                channel_id=10,
                content="bushima likes music recommendations",
                source_message_id=101,
                importance=1,
            )

            results = memory.search("please be terse", user_id=1, channel_id=10)

        self.assertEqual(short_id, results[0].id)
        self.assertEqual("bushima prefers short answers", results[0].content)
        self.assertGreaterEqual(results[0].score, 0.99)

    def test_forget_deletes_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self.make_memory(Path(temp_dir) / "memory.sqlite3")
            memory_id = memory.remember(
                user_id=1,
                username="bushima",
                channel_id=10,
                content="bushima prefers short answers",
            )

            self.assertTrue(memory.forget(memory_id))
            self.assertEqual([], memory.search("short answers", min_score=0))

    def test_search_filters_by_min_score(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self.make_memory(Path(temp_dir) / "memory.sqlite3")
            memory.remember(1, "bushima", 10, "bushima prefers short answers")
            memory.remember(1, "bushima", 10, "bushima likes music recommendations")

            instance_default = memory.search("terse please")
            explicit_low = memory.search("terse please", min_score=-1)
            explicit_high = memory.search("terse please", min_score=0.5)

        # "music" scores 0.0 against the query: below the instance min_score
        # of 0.35, but included when an explicit min_score overrides it.
        self.assertEqual(1, len(instance_default))
        self.assertEqual(2, len(explicit_low))
        self.assertEqual(1, len(explicit_high))
        self.assertEqual("bushima prefers short answers", explicit_high[0].content)

    def test_search_respects_top_k(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self.make_memory(Path(temp_dir) / "memory.sqlite3")
            memory.remember(1, "bushima", 10, "bushima prefers short answers")
            memory.remember(2, "friend", 10, "friend wants similar replies")
            memory.remember(3, "guest", 10, "guest likes music recommendations")

            all_results = memory.search("terse please", min_score=-1)
            capped = memory.search("terse please", min_score=-1, top_k=1)

            memory.top_k = 2
            instance_capped = memory.search("terse please", min_score=-1)

        self.assertEqual(3, len(all_results))
        self.assertEqual(1, len(capped))
        self.assertEqual("bushima prefers short answers", capped[0].content)
        self.assertEqual(2, len(instance_capped))

    def test_dedup_refreshes_username(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self.make_memory(Path(temp_dir) / "memory.sqlite3")
            memory.remember(1, "bushima", 10, "bushima prefers short answers")
            memory.remember(1, "nabefan", 10, "bushima wants similar terse replies")

            old_name = memory.list_for_user("bushima")
            new_name = memory.list_for_user("nabefan")

        self.assertEqual([], old_name)
        self.assertEqual(1, len(new_name))

    def test_search_rejects_non_string_query(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self.make_memory(Path(temp_dir) / "memory.sqlite3")
            memory.remember(1, "bushima", 10, "bushima prefers short answers")

            self.assertEqual([], memory.search({"a set, not a string"}))
            self.assertEqual([], memory.search(None))
            self.assertEqual([], memory.search("   "))

    def test_remember_deduplicates_near_identical_memories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self.make_memory(Path(temp_dir) / "memory.sqlite3")
            first_id = memory.remember(
                1, "bushima", 10, "bushima prefers short answers", importance=2
            )
            second_id = memory.remember(
                1, "bushima", 10, "bushima wants similar terse replies", importance=1
            )

            stored = memory.list_for_user("bushima")

        self.assertEqual(first_id, second_id)
        self.assertEqual(1, len(stored))
        self.assertEqual("bushima wants similar terse replies", stored[0].content)
        self.assertEqual(2, stored[0].importance)

    def test_remember_does_not_deduplicate_across_users(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self.make_memory(Path(temp_dir) / "memory.sqlite3")
            first_id = memory.remember(1, "bushima", 10, "bushima prefers short answers")
            second_id = memory.remember(2, "friend", 10, "friend prefers short answers")

        self.assertNotEqual(first_id, second_id)

    def test_search_skips_embeddings_with_mismatched_dimensions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self.make_memory(Path(temp_dir) / "memory.sqlite3")
            memory.remember(1, "bushima", 10, "a wide legacy memory")
            kept_id = memory.remember(1, "bushima", 10, "bushima prefers short answers")

            results = memory.search("terse please", min_score=0)

        self.assertEqual([kept_id], [result.id for result in results])

    def test_async_wrappers_round_trip(self):
        async def scenario(memory):
            memory_id = await memory.remember_async(
                1, "bushima", 10, "bushima prefers short answers"
            )
            results = await memory.search_async("terse please")
            return memory_id, results

        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self.make_memory(Path(temp_dir) / "memory.sqlite3")
            memory_id, results = asyncio.run(scenario(memory))

        self.assertEqual(memory_id, results[0].id)

    def test_clear_user_deletes_only_matching_username(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self.make_memory(Path(temp_dir) / "memory.sqlite3")
            memory.remember(1, "bushima", 10, "bushima prefers short answers")
            memory.remember(2, "friend", 10, "friend prefers short answers")

            deleted = memory.clear_user("bushima")
            friend_results = memory.search("short answers", user_id=2, min_score=0)

        self.assertEqual(1, deleted)
        self.assertEqual(1, len(friend_results))
        self.assertEqual("friend", friend_results[0].username)


if __name__ == "__main__":
    unittest.main()
