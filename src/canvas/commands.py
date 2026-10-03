from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.canvas.client import CanvasFetchError, load_timezone
from src.canvas.scheduler import CanvasScheduler, canvas_scheduler
from src.canvas.service import CanvasService, canvas_service, format_summary
from src.canvas.store import CanvasError
from src.config import (
    CANVAS_DEFAULT_LOOKAHEAD_DAYS,
    CANVAS_DEFAULT_TIMEZONE,
    CANVAS_MAX_LOOKAHEAD_DAYS,
)


logger = logging.getLogger(__name__)


class CanvasConnectModal(discord.ui.Modal, title="Connect Canvas Calendar"):
    feed_url = discord.ui.TextInput(
        label="Canvas Calendar Feed URL",
        placeholder="https://canvas.example.edu/feeds/calendars/...",
        required=True,
        max_length=2000,
    )

    def __init__(
        self,
        *,
        service: CanvasService,
        scheduler: CanvasScheduler,
        timezone_name: str,
        lookahead_days: int,
    ) -> None:
        super().__init__()
        self.service = service
        self.scheduler = scheduler
        self.timezone_name = timezone_name
        self.lookahead_days = lookahead_days

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            connection = await self.service.connect(
                interaction.user.id,
                self.feed_url.value,
                timezone_name=self.timezone_name,
                lookahead_days=self.lookahead_days,
            )
            self.scheduler.wake()
            await interaction.followup.send(
                "Canvas connected. I found "
                f"{len(self.service.store.list_events(interaction.user.id))} upcoming "
                f"calendar items.",
                ephemeral=True,
            )
        except (CanvasError, CanvasFetchError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception("Canvas connection failed")
            await interaction.followup.send(
                "Canvas could not be connected.",
                ephemeral=True,
            )


def register_canvas_commands(
    bot: commands.Bot,
    service: CanvasService = canvas_service,
    scheduler: CanvasScheduler = canvas_scheduler,
) -> app_commands.Group:
    canvas_group = app_commands.Group(
        name="canvas",
        description="Connect Canvas and view upcoming assignments",
    )

    @canvas_group.command(
        name="connect",
        description="Connect your Canvas calendar feed",
    )
    async def connect(interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            CanvasConnectModal(
                service=service,
                scheduler=scheduler,
                timezone_name=CANVAS_DEFAULT_TIMEZONE,
                lookahead_days=CANVAS_DEFAULT_LOOKAHEAD_DAYS,
            )
        )

    @canvas_group.command(
        name="configure",
        description="Change your Canvas timezone and summary window",
    )
    @app_commands.describe(
        timezone="IANA timezone, such as America/Los_Angeles",
        days=f"Days included in daily summaries (1-{CANVAS_MAX_LOOKAHEAD_DAYS})",
    )
    async def configure(
        interaction: discord.Interaction,
        timezone: str,
        days: app_commands.Range[int, 1, CANVAS_MAX_LOOKAHEAD_DAYS],
    ) -> None:
        try:
            connection = service.configure(
                interaction.user.id,
                timezone_name=timezone,
                lookahead_days=int(days),
            )
            scheduler.wake()
            await interaction.response.send_message(
                f"Canvas summaries now cover {connection.lookahead_days} day(s), "
                f"with the daily DM at {service.summary_time} in {connection.timezone}.",
                ephemeral=True,
            )
        except (CanvasError, CanvasFetchError) as error:
            await interaction.response.send_message(str(error), ephemeral=True)

    @canvas_group.command(
        name="upcoming",
        description="Show your upcoming Canvas assignments",
    )
    @app_commands.describe(days="Override the configured look-ahead window")
    async def upcoming(
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, CANVAS_MAX_LOOKAHEAD_DAYS] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await service.upcoming(
                interaction.user.id,
                days=int(days) if days is not None else None,
            )
            await interaction.followup.send(
                format_summary(result),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (CanvasError, CanvasFetchError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception:
            logger.exception("Canvas upcoming lookup failed")
            await interaction.followup.send(
                "Canvas assignments could not be loaded. Please try again later.",
                ephemeral=True,
            )

    @canvas_group.command(
        name="status",
        description="Show your Canvas connection and summary settings",
    )
    async def status(interaction: discord.Interaction) -> None:
        try:
            connection = service.store.get_connection(interaction.user.id)
            if connection is None:
                raise CanvasError("connect a Canvas calendar first")
            zone = load_timezone(connection.timezone)
            next_send = connection.next_summary_at.astimezone(zone).strftime(
                "%Y-%m-%d %I:%M %p %Z"
            )
            refresh = (
                connection.last_refresh_at.astimezone(zone).strftime(
                    "%Y-%m-%d %I:%M %p %Z"
                )
                if connection.last_refresh_at
                else "never"
            )
            await interaction.response.send_message(
                f"Connected: yes\n"
                f"Timezone: {connection.timezone}\n"
                f"Look-ahead: {connection.lookahead_days} day(s)\n"
                f"Next DM: {next_send}\n"
                f"Last refresh: {refresh}",
                ephemeral=True,
            )
        except (CanvasError, CanvasFetchError) as error:
            await interaction.response.send_message(str(error), ephemeral=True)

    @canvas_group.command(
        name="disconnect",
        description="Remove your Canvas feed and cached assignments",
    )
    async def disconnect(interaction: discord.Interaction) -> None:
        if not service.disconnect(interaction.user.id):
            await interaction.response.send_message(
                "No Canvas calendar is connected.",
                ephemeral=True,
            )
            return
        scheduler.wake()
        await interaction.response.send_message(
            "Canvas disconnected and its cached assignments were deleted.",
            ephemeral=True,
        )

    bot.tree.add_command(canvas_group)
    return canvas_group

