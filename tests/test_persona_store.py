import tempfile
import unittest
from pathlib import Path

from src.persona.store import PersonaConflictError, PersonaStore


class PersonaStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "persona.sqlite3"
        self.store = PersonaStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_creates_normalized_fact_with_aliases(self):
        fact = self.store.create(
            key="Favorite Color",
            value="Green.",
            aliases=["favorite colour", " preferred color "],
        )

        self.assertEqual("favorite_color", fact.key)
        self.assertEqual("Green.", fact.value)
        self.assertEqual(
            ("favorite colour", "preferred color"),
            fact.aliases,
        )

    def test_searches_by_key_alias_and_natural_question(self):
        self.store.create(
            key="favorite_color",
            value="Green.",
            aliases=["preferred color"],
        )

        self.assertEqual(
            "key",
            self.store.search("favorite color")[0].match,
        )
        self.assertEqual(
            "alias",
            self.store.search("preferred color")[0].match,
        )
        self.assertEqual(
            "favorite_color",
            self.store.search("what is your favorite color?")[0].key,
        )

    def test_returns_no_match_instead_of_guessing(self):
        self.store.create(key="favorite_color", value="Green.")

        self.assertEqual([], self.store.search("birthday"))
        self.assertEqual([], self.store.search(""))

    def test_rejects_key_and_alias_conflicts(self):
        self.store.create(
            key="favorite_color",
            value="Green.",
            aliases=["preferred color"],
        )

        with self.assertRaises(PersonaConflictError):
            self.store.create(
                key="preferred_color",
                value="Blue.",
                aliases=["favorite color"],
            )

    def test_updates_and_deletes_aliases_persistently(self):
        fact = self.store.create(
            key="hometown",
            value="Irvine.",
            aliases=["where are you from"],
        )
        updated = self.store.update(
            fact.id,
            key="home_town",
            value="Los Angeles.",
            aliases=["place you grew up"],
        )

        self.assertIsNotNone(updated)
        self.assertEqual([], self.store.search("where are you from"))
        self.assertEqual(
            "Los Angeles.",
            PersonaStore(self.db_path).search("place you grew up")[0].value,
        )
        self.assertTrue(self.store.delete(fact.id))
        self.assertEqual([], self.store.list_facts())
        self.assertFalse(self.store.delete(fact.id))


if __name__ == "__main__":
    unittest.main()
