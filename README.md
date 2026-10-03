# nabemono

## Run with Docker

Create the environment file:

```sh
cp .env.example .env
```

Fill in `.env`, then start
the bot:

```sh
docker compose up -d --build
docker compose logs -f nabemono
```

Stop the bot with `docker compose down`. The `nabemono-data` volume retains
long-term memory, affection state, analytics, music telemetry, encrypted Canvas
connections and cached assignments, persona facts, cached Discord images,
uploaded music while it is queued, and the local Wikipedia ZIM. Uploaded music
is deleted after playback, removal, or the end of its voice session.
The first start downloads
`wikipedia_en_all_mini` (about 12 GB) from Kiwix if it is not already present.
Set `WIKIPEDIA_ZIM_PREFIX` to choose a different dump, such as
`wikipedia_en_all_nopic`.
The `model-cache` volume retains downloaded embedding models.

To also delete all persisted bot data and model downloads, run:

```sh
docker compose down -v
```

## Canvas assignment summaries

Canvas Calendar Feed URLs are private bearer links. Before using the integration,
set `CANVAS_ENCRYPTION_KEY` in `.env`:

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep that key stable across restarts. Changing or losing it makes saved feed URLs
unreadable; users will need to reconnect.

In Canvas, open Calendar, choose **Calendar Feed**, and copy the generated feed
URL. In Discord:

1. Run `/canvas connect` and paste the feed URL into the private modal. New
   connections use the configured default timezone and one-day summary window.
2. Use `/canvas upcoming` for an on-demand refresh and private summary.
3. Use `/canvas configure` to change timezone or look-ahead days.
4. Use `/canvas status` or `/canvas disconnect` to inspect or remove the
   connection.

Nabemono refreshes the feed immediately before each daily DM at 7:00 AM in the
user's configured timezone. On-demand requests refresh unless the feed was
fetched within the last five minutes. If Canvas is temporarily unavailable, the
daily DM uses the last successful cache and marks it as stale. Feed URLs are
encrypted in SQLite, omitted from responses and logs, and deleted with cached
events on disconnect.

## Web dashboard and music controller

The bot starts a password-protected persona dashboard and music controller on
container port 8080. The dashboard at `/` edits the authoritative facts returned
by `persona_search`; changes take effect without restarting the bot. Each time
the bot joins a voice channel it posts an unguessable `/s/{token}` controller
link, which expires when the bot leaves.


### Radio stations

The web controller's Radio tab only lists stations configured in
`src/music/radio.py`. Stream URLs remain server-side and cannot be submitted
through the web interface or API. Add future stations to `RADIO_STATIONS`, then
rebuild the container.
