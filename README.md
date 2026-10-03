# nabemono

## Run with Docker

Install Docker with the Compose plugin, then create the environment file:

```sh
cp .env.example .env
```

Fill in the tokens, API keys, channel ID, and model names in `.env`, then start
the bot:

```sh
docker compose up -d --build
docker compose logs -f nabemono
```

To deploy a new version:

```sh
git pull
docker compose up -d --build
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

## Web dashboard and music controller on Synology

The bot starts a password-protected persona dashboard and music controller on
container port 8080. The dashboard at `/` edits the authoritative facts returned
by `persona_search`; changes take effect without restarting the bot. Each time
the bot joins a voice channel it posts an unguessable `/s/{token}` controller
link, which expires when the bot leaves. The public HTTPS endpoint can stay on a
VPS running Caddy while the bot remains on a Synology NAS.

1. Install Tailscale on the Synology NAS and the VPS, then authenticate both to
   the same tailnet.
2. On the NAS, get its stable Tailscale IPv4 address from the Tailscale package
   or with `tailscale ip -4`.
3. In the bot's `.env`, set:

   ```dotenv
   MUSIC_WEB_PUBLISH_HOST=100.x.y.z
   MUSIC_WEB_PORT=8080
   MUSIC_WEB_BASE_URL=https://nabemono.bulletmaji.me
   PERSONA_DASHBOARD_PASSWORD=use-a-long-random-password
   ```

   `MUSIC_WEB_PUBLISH_HOST` binds Docker's published port only to the NAS's
   Tailscale interface. If the Synology firewall is enabled, allow TCP 8080
   from the tailnet, not from the public internet.
4. Point `nabemono.bulletmaji.me` at the VPS and add this to its
   Caddyfile, replacing the upstream address with the NAS's Tailscale address:

   ```caddy
   nabemono.bulletmaji.me {
       reverse_proxy 100.x.y.z:8080
   }
   ```

5. Reload Caddy and rebuild the bot with `docker compose up -d --build`.

Caddy terminates HTTPS and preserves the original `Host` header by default.
The connection from the VPS to the NAS is encrypted by Tailscale, and no router
port needs to be forwarded to the NAS. The controller supports its live event
stream through Caddy without additional configuration.

The dashboard fails closed with HTTP 503 when
`PERSONA_DASHBOARD_PASSWORD` is empty. Its login cookie is only marked secure
when `MUSIC_WEB_BASE_URL` uses HTTPS, so production deployments must keep that
value set to the public HTTPS origin.

### Radio stations

The web controller's Radio tab only lists stations configured in
`src/music/radio.py`. Stream URLs remain server-side and cannot be submitted
through the web interface or API. Add future stations to `RADIO_STATIONS`, then
rebuild the container.
