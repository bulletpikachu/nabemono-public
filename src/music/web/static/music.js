const base = window.location.pathname.replace(/\/$/, "");
const $ = selector => document.querySelector(selector);
const notice = $("#notice");
let state = null;
let renderedAt = performance.now();
let draggedPosition = null;
let ended = false;
let connected = false;
let pending = 0;
let radioStations = [];

function navigate() {
  const tabs = ["add", "radio", "queue", "history"];
  const tab = tabs.includes(location.hash.slice(1)) ? location.hash.slice(1) : "add";
  for (const name of tabs) {
    $(`#panel-${name}`).hidden = name !== tab;
    const link = $(`nav a[href="#${name}"]`);
    if (name === tab) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  $("#page-title").textContent = {add: "Add music", radio: "Radio", queue: "Queue", history: "Session history"}[tab];
}
window.addEventListener("hashchange", navigate);
navigate();

function formatTime(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  const total = Math.max(0, Math.floor(Number(value)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
}
function elapsedNow() {
  if (!state?.current) return 0;
  const elapsed = Number(state.current.elapsed_seconds || 0);
  if (ended || !connected || state.current.paused || state.current.elapsed_seconds == null) return elapsed;
  return elapsed + (performance.now() - renderedAt) / 1000;
}
function renderProgress() {
  const current = state?.current;
  const duration = Number(current?.duration_seconds || 0);
  const elapsed = duration ? Math.min(elapsedNow(), duration) : elapsedNow();
  $("#progress").max = duration || 1;
  $("#progress").value = duration ? elapsed : 0;
  $("#timing").textContent = current ? `${formatTime(elapsed)} / ${formatTime(current.duration_seconds)}` : "0:00";
}
function syncControls() {
  document.querySelectorAll("main button, main input").forEach(element => {
    element.disabled = ended || !connected || pending > 0 || element.dataset.unavailable === "true";
  });
  $("#pause").disabled ||= !state?.current || state.current.paused || Boolean(state?.radio);
  $("#resume").disabled ||= !state?.current || !state.current.paused || Boolean(state?.radio);
  $("#skip").disabled ||= !state?.current || Boolean(state?.radio);
  $("#pause").hidden = Boolean(state?.current?.paused);
  $("#resume").hidden = !state?.current?.paused;
}
function renderRadio() {
  $("#radio-stations").replaceChildren();
  for (const station of radioStations) {
    const item = document.createElement("article");
    item.className = "station-item";
    const info = document.createElement("div");
    const name = document.createElement("h3");
    name.textContent = station.name;
    const description = document.createElement("p");
    description.className = "muted";
    description.textContent = station.description;
    info.append(name, description);
    const active = state?.radio?.id === station.id;
    const button = action(
      `Play ${station.name}`,
      active ? "Live" : "Listen →",
      () => safePost("/api/radio/play", {station_id: station.id}),
      active,
    );
    item.append(info, button);
    $("#radio-stations").append(item);
  }
  $("#empty-radio").hidden = radioStations.length > 0;
  $("#radio-stop").hidden = !state?.radio;
  const interrupted = state?.interrupted;
  $("#radio-resume").textContent = interrupted
    ? `Stopping radio will resume ${interrupted.title || "the interrupted track"} at ${formatTime(interrupted.elapsed_seconds)}.`
    : state?.radio ? "Stopping radio will return to the music queue." : "";
}
function details(track) {
  const node = document.createElement("div");
  const title = document.createElement("div");
  title.className = "track-title";
  title.textContent = track.title || "Untitled track";
  const meta = document.createElement("p");
  meta.className = "track-meta muted";
  meta.textContent = [track.artist, track.source === "local" ? "Uploaded audio" : "YouTube", formatTime(track.duration_seconds), track.requested_by && `by ${track.requested_by}`].filter(Boolean).join(" · ");
  node.append(title, meta);
  return node;
}
function action(label, text, action, unavailable = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = text;
  button.setAttribute("aria-label", label);
  button.title = label;
  button.dataset.unavailable = String(unavailable);
  button.addEventListener("click", action);
  return button;
}
function renderQueue() {
  const active = document.activeElement;
  const focusedPosition = active?.closest("#queue li")?.dataset.position;
  const focusedAction = active?.getAttribute("aria-label")?.split(":")[0];
  $("#queue").replaceChildren();
  for (const track of state.queue) {
    const item = document.createElement("li");
    item.className = "track-item";
    item.draggable = true;
    item.dataset.position = String(track.position);
    const handle = document.createElement("span");
    handle.className = "drag-handle";
    handle.textContent = String(track.position).padStart(2, "0");
    handle.title = "Drag to reorder";
    const actions = document.createElement("div");
    actions.className = "track-actions";
    const move = destination => safePost("/api/queue/move", {source: track.position, destination});
    actions.append(
      action(`Move up: ${track.title}`, "↑", () => move(track.position - 1), track.position === 1),
      action(`Move down: ${track.title}`, "↓", () => move(track.position + 1), track.position === state.queue.length),
      action(`Remove: ${track.title}`, "×", () => safePost("/api/queue/remove", {position: track.position}))
    );
    item.append(handle, details(track), actions);
    item.addEventListener("dragstart", event => {
      if (ended || !connected || pending) { event.preventDefault(); return; }
      draggedPosition = track.position;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", String(track.position));
      item.classList.add("dragging");
    });
    item.addEventListener("dragend", () => { draggedPosition = null; item.classList.remove("dragging"); document.querySelectorAll(".drop-target").forEach(el => el.classList.remove("drop-target")); });
    item.addEventListener("dragover", event => { event.preventDefault(); item.classList.add("drop-target"); });
    item.addEventListener("dragleave", () => item.classList.remove("drop-target"));
    item.addEventListener("drop", event => {
      event.preventDefault();
      item.classList.remove("drop-target");
      if (draggedPosition && draggedPosition !== track.position) safePost("/api/queue/move", {source: draggedPosition, destination: track.position});
      draggedPosition = null;
    });
    $("#queue").append(item);
  }
  $("#empty-queue").hidden = state.queue.length > 0;
  if (focusedPosition && focusedAction) {
    const row = [...$("#queue").children].find(item => item.dataset.position === focusedPosition);
    const button = row && [...row.querySelectorAll("button")].find(item => item.getAttribute("aria-label").startsWith(`${focusedAction}:`));
    button?.focus();
  }
}
function renderHistory() {
  const history = state.history || [];
  $("#history").replaceChildren();
  for (const track of history) {
    const item = document.createElement("li");
    item.className = "track-item";
    const marker = document.createElement("span");
    marker.className = "muted";
    marker.textContent = track.outcome === "skipped" ? "↗" : track.outcome === "failed" ? "!" : "✓";
    marker.setAttribute("aria-hidden", "true");
    const info = details(track);
    const outcome = document.createElement("p");
    outcome.className = "track-meta muted";
    const time = new Date(track.ended_at * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
    outcome.textContent = `${{played: "Played", skipped: "Skipped", failed: "Playback failed"}[track.outcome] || "Played"} · ${time}`;
    info.append(outcome);
    item.append(marker, info);
    $("#history").append(item);
  }
  $("#empty-history").hidden = history.length > 0;
}
const artwork = $("#now-artwork");
artwork.addEventListener("error", () => { artwork.hidden = true; $("#artwork-fallback").hidden = false; });
function render(nextState) {
  const changed = !state || state.revision !== nextState.revision;
  state = nextState;
  renderedAt = performance.now();
  const current = state.current;
  const radio = state.radio;
  $("#now-title").textContent = radio?.name || current?.title || "Nothing playing yet.";
  $("#now-artist").textContent = radio?.description || current?.artist || "";
  $("#playback-status").textContent = radio ? "Live radio" : current?.paused ? "Paused" : current && current.elapsed_seconds == null ? "Loading track" : "Now playing";
  $("#progress-row").hidden = Boolean(radio);
  const url = current?.artwork_url || "";
  artwork.alt = current?.title ? `Artwork for ${current.title}` : "";
  if (artwork.dataset.url !== url) {
    artwork.dataset.url = url;
    artwork.hidden = !url;
    $("#artwork-fallback").hidden = Boolean(url);
    if (url) artwork.src = url;
    else artwork.removeAttribute("src");
  }
  $("#remaining").textContent = `${formatTime(state.remaining_seconds)}${state.remaining_complete ? "" : "+"} remaining`;
  if (changed) { renderQueue(); renderHistory(); renderRadio(); }
  syncControls();
  renderProgress();
}
async function post(path, body) {
  const options = {method: "POST"};
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) { options.headers = {"Content-Type": "application/json"}; options.body = JSON.stringify(body); }
  const response = await fetch(`${base}${path}`, options);
  if (!response.ok) throw new Error((await response.text()) || `Request failed (${response.status})`);
  return response.json();
}
async function safePost(path, body) {
  if (ended || !connected || pending) return null;
  pending++;
  syncControls();
  notice.textContent = path === "/api/upload" ? "Uploading and checking audio…" : "Working…";
  try {
    const result = await post(path, body);
    const response = await fetch(`${base}/api/queue`);
    if (response.ok) render(await response.json());
    notice.textContent = path === "/api/queue/move" ? "Queue reordered." : result.status ? result.status.replaceAll("_", " ") : "Done.";
    return result;
  } catch (error) { notice.textContent = error.message; return null; }
  finally { pending--; syncControls(); }
}
for (const name of ["pause", "resume", "skip"]) $(`#${name}`).addEventListener("click", () => safePost(`/api/${name}`));
$("#radio-stop").addEventListener("click", () => safePost("/api/radio/stop"));
$("#youtube-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const result = await safePost("/api/play", {url: $("#youtube-url").value});
  if (result) { notice.textContent = `Added ${result.added} track${result.added === 1 ? "" : "s"}.`; form.reset(); }
});
$("#upload-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  if ($("#audio-file").files[0]?.size > 50 * 1024 * 1024) { notice.textContent = "Choose an audio file of 50 MB or less."; return; }
  const result = await safePost("/api/upload", new FormData(form));
  if (result) { notice.textContent = `Added ${result.title}.`; form.reset(); }
});
const events = new EventSource(`${base}/api/events`);
events.addEventListener("queue", event => {
  connected = true;
  render(JSON.parse(event.data));
});
events.addEventListener("ended", () => {
  ended = true;
  connected = false;
  notice.textContent = "This session has ended. Join a new session from Discord to play more music.";
  events.close();
  syncControls();
});
events.onerror = () => { connected = false; syncControls(); };
fetch(`${base}/api/radio`)
  .then(response => {
    if (!response.ok) throw new Error("Could not load radio stations.");
    return response.json();
  })
  .then(payload => {
    radioStations = payload.stations || [];
    renderRadio();
    syncControls();
  })
  .catch(error => { notice.textContent = error.message; });
syncControls();
setInterval(renderProgress, 500);
