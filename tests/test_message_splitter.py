import json
import random
import re
import time
import unittest
from pathlib import Path

from src.message_splitter import (
    MAX_MESSAGE_LENGTH,
    MAX_MESSAGES_PER_REPLY,
    split_reply,
)


CASES_PATH = Path(__file__).parent / "fixtures" / "message_splitter_cases.json"
FUZZ_SEED = 20260922
FUZZ_ROUNDS = 1500
SLOW_INPUT_SECONDS = 1.0


class GoldenCaseTests(unittest.TestCase):
    def test_matches_every_golden_case(self):
        cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))

        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertEqual(case["expected"], split_reply(case["input"]))


class SentenceBoundaryTests(unittest.TestCase):
    def test_keeps_number_abbreviation_with_its_number(self):
        reply = "the model no. 5 is my favorite. it's fast."

        self.assertEqual(
            ["the model no. 5 is my favorite", "it's fast"],
            split_reply(reply),
        )

    def test_splits_after_no_used_as_a_word(self):
        self.assertEqual(
            ["i said no", "then he left"],
            split_reply("i said no. then he left"),
        )

    def test_keeps_abbreviations_and_initialisms_mid_sentence(self):
        reply = "dr. stone is on at 5 p.m. vs. last week. e.g. tonight"

        self.assertEqual(
            ["dr. stone is on at 5 p.m. vs. last week", "e.g. tonight"],
            split_reply(reply),
        )

    def test_splits_after_question_and_exclamation_runs(self):
        self.assertEqual(
            ["wait what?!", "no way!!!", "ok"],
            split_reply("wait what?! no way!!! ok"),
        )

    def test_keeps_ellipsis_inside_and_at_the_end_of_a_message(self):
        self.assertEqual(["wait... what", "hmm..."], split_reply("wait... what. hmm..."))

    def test_keeps_detached_dots_together(self):
        self.assertEqual(["wait . . . what"], split_reply("wait . . . what"))

    def test_keeps_numbered_headings_and_quoted_list_items_together(self):
        self.assertEqual(
            ["### 1. install python", "> 2. make a venv", "- 3. done"],
            split_reply("### 1. install python\n> 2. make a venv\n- 3. done"),
        )

    def test_handles_superscript_numbers_before_a_period(self):
        self.assertEqual(["it's about 10²", "so 100"], split_reply("it's about 10². so 100"))

    def test_keeps_list_markers_with_their_items(self):
        self.assertEqual(
            ["1. apples", "2. bananas"],
            split_reply("1. apples\n2. bananas"),
        )

    def test_keeps_inline_enumeration_together(self):
        reply = "top 3: 1. frieren 2. dandadan 3. one piece. fight me"

        self.assertEqual(
            ["top 3: 1. frieren 2. dandadan 3. one piece", "fight me"],
            split_reply(reply),
        )

    def test_splits_after_pronouns_i_and_u(self):
        self.assertEqual(
            ["love u", "same, so am i", "anyway"],
            split_reply("love u. same, so am i. anyway"),
        )

    def test_keeps_single_letter_initials_together(self):
        self.assertEqual(
            ["j. k. rowling and ulysses s. grant"],
            split_reply("j. k. rowling and ulysses s. grant"),
        )

    def test_treats_non_breaking_space_as_whitespace(self):
        self.assertEqual(
            ["model no.\u00a05 is out", "nice"],
            split_reply("model no.\u00a05 is out.\u00a0nice"),
        )

    def test_does_not_split_inside_protected_spans(self):
        reply = (
            "run `pip install. then go` now. ||he dies. oops|| lol. "
            "*sighs. fine.* ok. he said 'nope. never.' so yeah"
        )

        self.assertEqual(
            [
                "run `pip install. then go` now",
                "||he dies. oops|| lol",
                "*sighs. fine.* ok",
                "he said 'nope. never.' so yeah",
            ],
            split_reply(reply),
        )

    def test_apostrophes_do_not_protect_text(self):
        self.assertEqual(
            ["don't", "it's fine", "y'all know"],
            split_reply("don't. it's fine. y'all know"),
        )


class CleanupTests(unittest.TestCase):
    def test_returns_nothing_for_blank_reply(self):
        self.assertEqual([], split_reply(" \n\t\r\n "))

    def test_drops_one_trailing_period_but_keeps_initialisms(self):
        self.assertEqual(
            ["i live in the u.s.", "i like apples, oranges, etc"],
            split_reply("i live in the u.s.\ni like apples, oranges, etc."),
        )

    def test_drops_lines_that_are_only_a_period(self):
        self.assertEqual(["ok", "...", "bye"], split_reply("ok\n.\n...\n . \nbye"))


class CodeBlockTests(unittest.TestCase):
    def test_sends_fenced_code_block_intact(self):
        code = "```python\nx = 1. \n\n# done. ok\nprint(x)\n```"

        self.assertEqual(
            ["here's the fix", code, "should work now"],
            split_reply(f"here's the fix.\n{code}\nshould work now."),
        )

    def test_closes_an_unclosed_fence(self):
        self.assertEqual(
            ["look:", "```\nwhile true. do\n```"],
            split_reply("look:\n```\nwhile true. do"),
        )

    def test_splits_long_code_block_between_lines_and_reopens_fence(self):
        lines = [f"print('line {index}. ok')" for index in range(200)]
        reply = "```py\n" + "\n".join(lines) + "\n```"

        messages = split_reply(reply)

        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message), MAX_MESSAGE_LENGTH)
            self.assertTrue(message.startswith("```py\n"))
            self.assertTrue(message.endswith("\n```"))
        inner = [message[len("```py\n") : -len("\n```")] for message in messages]
        self.assertEqual(lines, "\n".join(inner).split("\n"))

    def test_hard_cuts_a_code_line_longer_than_the_limit(self):
        line = "x" * 5000
        reply = f"```json\n{line}\n```"

        messages = split_reply(reply)

        for message in messages:
            self.assertLessEqual(len(message), MAX_MESSAGE_LENGTH)
            self.assertTrue(message.startswith("```json\n"))
            self.assertTrue(message.endswith("\n```"))
        inner = [message[len("```json\n") : -len("\n```")] for message in messages]
        self.assertEqual(line, "".join(inner))

    def test_rewraps_a_long_single_line_fence(self):
        body = "npm install. " * 200
        messages = split_reply(f"```{body}```")

        for message in messages:
            self.assertLessEqual(len(message), MAX_MESSAGE_LENGTH)
            self.assertTrue(message.startswith("```\n"))
            self.assertTrue(message.endswith("\n```"))


class LengthTests(unittest.TestCase):
    def test_cuts_a_long_sentence_at_whitespace(self):
        words = [f"word{index}" for index in range(600)]

        messages = split_reply(" ".join(words))

        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message), MAX_MESSAGE_LENGTH)
        self.assertEqual(words, " ".join(messages).split(" "))

    def test_hard_cut_does_not_split_a_custom_emoji(self):
        emoji = "<:blob:123456789012345678>"
        reply = "a" * (MAX_MESSAGE_LENGTH - 10) + emoji + "b" * 50

        messages = split_reply(reply)

        self.assertEqual(reply, "".join(messages))
        self.assertTrue(any(emoji in message for message in messages))


class MessageCapTests(unittest.TestCase):
    def test_sends_each_sentence_when_within_the_cap(self):
        reply = " ".join(f"sentence{index}." for index in range(MAX_MESSAGES_PER_REPLY))

        self.assertEqual(MAX_MESSAGES_PER_REPLY, len(split_reply(reply)))

    def test_sends_whole_lines_when_sentences_exceed_the_cap(self):
        first = " ".join(f"one{index}." for index in range(8))
        second = " ".join(f"two{index}." for index in range(8))

        self.assertEqual(
            [first[:-1], second[:-1]],
            split_reply(f"{first}\n{second}"),
        )

    def test_packs_lines_when_lines_exceed_the_cap(self):
        items = [f"{index}. item {index}" for index in range(1, 16)]

        self.assertEqual(["\n".join(items)], split_reply("\n".join(items)))

    def test_packs_code_blocks_with_the_lines_around_them(self):
        code = "```\nx = 1\n```"
        items = [f"- item {index}" for index in range(12)]
        reply = "\n".join(items[:6] + [code] + items[6:])

        self.assertEqual([reply], split_reply(reply))

    def test_packs_many_tiny_code_blocks_into_few_messages(self):
        reply = "```a```" * 1000

        messages = split_reply(reply)

        self.assertLessEqual(len(messages), 5)
        self.assertEqual(reply.replace("```", ""), "".join(messages).replace("```", "").replace("\n", ""))


class InvariantTests(unittest.TestCase):
    ATOMS = [
        "hey", "ok", "no.", "no. 5", "dr.", "e.g.", "u.s.", "etc.", "i.", "u.",
        "j. k.", "1.", "2.", "3.14", "vol. 2", "wait...", "hmm…", ". . .",
        "?!", "!!!", ".", "(", ")", "[", "]", '"', "'", "’", "`", "``", "||",
        "**", "*", "__", "~~", "<:blob:123>", "<@456>", "https://a.b/c.d",
        "\n", "\r\n", "\n\n", "\u00a0", "\t", "x" * 300,
    ]

    def _random_reply(self, rng):
        count = rng.choice([3, 10, 40, 400])
        atoms = [rng.choice(self.ATOMS) for _ in range(count)]
        separators = [rng.choice([" ", " ", "", "  "]) for _ in range(count)]
        return "".join(
            atom + separator for atom, separator in zip(atoms, separators, strict=True)
        )

    def test_random_replies_produce_valid_messages(self):
        rng = random.Random(FUZZ_SEED)

        for round_number in range(FUZZ_ROUNDS):
            reply = self._random_reply(rng)
            messages = split_reply(reply)
            with self.subTest(round=round_number, reply=reply[:200]):
                self.assertIsInstance(messages, list)
                for message in messages:
                    self.assertIsInstance(message, str)
                    self.assertTrue(message.strip())
                    self.assertEqual(message.strip(), message)
                    self.assertLessEqual(len(message), MAX_MESSAGE_LENGTH)
                if "`" not in reply:
                    self.assertEqual(
                        re.sub(r"[\s.]", "", reply),
                        re.sub(r"[\s.]", "", "".join(messages)),
                    )


class PerformanceTests(unittest.TestCase):
    PATHOLOGICAL_INPUTS = {
        "periods": "." * 50_000,
        "periods_then_space": "." * 50_000 + " x",
        "brackets": "(" * 25_000 + "]" * 25_000,
        "nested_brackets": "(a. " * 12_000 + ")" * 12_000,
        "short_sentences": "a. " * 20_000,
        "abbreviations": "no. " * 20_000,
        "backticks": "`" * 20_000,
        "backtick_widths": " ".join("`" * width for width in range(1, 300)),
        "quotes": "'a " * 20_000,
        "emphasis": "*a " * 20_000,
        "no_whitespace": "a" * 100_000,
        "enumeration": " ".join(f"{index % 999}. x" for index in range(15_000)),
    }

    def test_pathological_inputs_finish_quickly(self):
        for name, reply in self.PATHOLOGICAL_INPUTS.items():
            with self.subTest(input=name):
                started = time.perf_counter()
                messages = split_reply(reply)
                elapsed = time.perf_counter() - started

                self.assertLess(elapsed, SLOW_INPUT_SECONDS)
                for message in messages:
                    self.assertLessEqual(len(message), MAX_MESSAGE_LENGTH)


if __name__ == "__main__":
    unittest.main()
