import json
import unittest

import discord
from discord.ext import commands

from src.canvas.commands import register_canvas_commands
from src.tools import available_functions


class CanvasCommandRegistrationTests(unittest.TestCase):
    def test_registers_canvas_command_group(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

        group = register_canvas_commands(bot)

        self.assertEqual(
            {"configure", "connect", "disconnect", "status", "upcoming"},
            {command.name for command in group.commands},
        )
        connect = group.get_command("connect")
        self.assertEqual([], connect.parameters)
        self.assertIs(bot.tree.get_command("canvas"), group)

    def test_registers_requester_scoped_llm_tool(self):
        self.assertIn("canvas_upcoming", available_functions)
        with open("src/tools.json", encoding="utf-8") as file:
            schemas = json.load(file)
        canvas_schema = next(
            schema for schema in schemas if schema["name"] == "canvas_upcoming"
        )
        self.assertEqual(30, canvas_schema["parameters"]["properties"]["days"]["maximum"])


if __name__ == "__main__":
    unittest.main()

