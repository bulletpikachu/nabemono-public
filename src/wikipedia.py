import asyncio
import re
from pathlib import Path
from urllib.parse import unquote, urljoin

import aiohttp

from src.config import (
    WIKIPEDIA_DIR,
    WIKIPEDIA_INDEX_URL,
    WIKIPEDIA_MAX_CHARS,
    WIKIPEDIA_MAX_RESULTS,
    WIKIPEDIA_ZIM_PREFIX,
)


ZIM_HREF_RE = re.compile(r"""href=["']([^"']+\.zim)["']""", re.IGNORECASE)
ZIM_DATE_RE = re.compile(r"_(\d{4}-\d{2})\.zim$")
PROGRESS_INTERVAL_BYTES = 256 * 1024 * 1024
# dumps.wikimedia.org 403s generic clients after Kiwix redirects there.
DOWNLOAD_HEADERS = {
    "User-Agent": "nabemono/1.0 (offline wikipedia zim fetch)",
    "Accept": "*/*",
}

_archive = None
_archive_path = None
_state = {
    "ready": False,
    "path": None,
    "downloading": False,
    "downloaded_bytes": 0,
    "error": None,
}
_lock = asyncio.Lock()


def parse_zim_filenames(html):
    names = []
    seen = set()
    for match in ZIM_HREF_RE.finditer(html or ""):
        name = unquote(match.group(1)).rsplit("/", 1)[-1]
        if not name.endswith(".zim") or name.endswith(".zim.sha256"):
            continue
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def pick_latest_zim(filenames, prefix=WIKIPEDIA_ZIM_PREFIX):
    candidates = [
        name
        for name in filenames
        if name.startswith(prefix) and name.endswith(".zim")
    ]
    if not candidates:
        return None

    def sort_key(name):
        match = ZIM_DATE_RE.search(name)
        return match.group(1) if match else ""

    return max(candidates, key=sort_key)


def find_local_zim(directory=WIKIPEDIA_DIR, prefix=WIKIPEDIA_ZIM_PREFIX):
    root = Path(directory)
    if not root.exists():
        return None
    matches = [
        path
        for path in root.glob(f"{prefix}*.zim")
        if path.is_file() and path.stat().st_size > 0
    ]
    if not matches:
        return None
    return max(matches, key=lambda path: path.name)


def get_status():
    return dict(_state)


def _set_state(**updates):
    _state.update(updates)


def _resolve_entry(archive, title):
    title = (title or "").strip()
    if not title:
        return None

    candidates = [title]
    underscored = title.replace(" ", "_")
    spaced = title.replace("_", " ")
    if underscored not in candidates:
        candidates.append(underscored)
    if spaced not in candidates:
        candidates.append(spaced)

    for candidate in candidates:
        if archive.has_entry_by_path(candidate):
            return archive.get_entry_by_path(candidate)
        if getattr(archive, "has_entry_by_title", None) and archive.has_entry_by_title(
            candidate
        ):
            return archive.get_entry_by_title(candidate)
    return None


def _follow_redirects(entry):
    seen = set()
    while getattr(entry, "is_redirect", False):
        identity = getattr(entry, "path", id(entry))
        if identity in seen:
            break
        seen.add(identity)
        entry = entry.get_redirect_entry()
    return entry


def _entry_payload(entry):
    entry = _follow_redirects(entry)
    return {
        "title": getattr(entry, "title", "") or "",
        "path": getattr(entry, "path", "") or "",
    }


def _load_search_backends():
    from libzim.search import Query, Searcher
    from libzim.suggestion import SuggestionSearcher

    return SuggestionSearcher, Searcher, Query


def _search_archive(
    archive,
    query,
    max_results=WIKIPEDIA_MAX_RESULTS,
    backends=None,
):
    SuggestionSearcher, Searcher, Query = backends or _load_search_backends()

    results = []
    seen = set()

    def add_path(path):
        if not path or path in seen:
            return
        seen.add(path)
        try:
            entry = archive.get_entry_by_path(path)
        except Exception:
            results.append({"title": path, "path": path})
            return
        results.append(_entry_payload(entry))

    try:
        suggestion = SuggestionSearcher(archive).suggest(query)
        for path in suggestion.getResults(0, max_results):
            add_path(path)
            if len(results) >= max_results:
                return results
    except Exception as error:
        print(f"[WIKIPEDIA]: suggestion search failed: {error}")

    try:
        search = Searcher(archive).search(Query().set_query(query))
        remaining = max_results - len(results)
        if remaining > 0:
            for path in search.getResults(0, remaining + len(seen)):
                add_path(path)
                if len(results) >= max_results:
                    break
    except Exception as error:
        print(f"[WIKIPEDIA]: full-text search failed: {error}")

    return results


def search_pages(query, max_results=WIKIPEDIA_MAX_RESULTS):
    if not _state["ready"] or _archive is None:
        return {
            "query": query,
            "results": [],
            "note": _unavailable_note(),
        }

    results = _search_archive(_archive, query, max_results=max_results)
    summary = {"query": query, "results": results}
    if not results:
        summary["note"] = "no wikipedia results"
    return summary


def get_page(title, max_chars=WIKIPEDIA_MAX_CHARS):
    from src.tools import extract_page_text, format_page_contents

    if not _state["ready"] or _archive is None:
        return {
            "title": title,
            "content": "",
            "note": _unavailable_note(),
        }

    entry = _resolve_entry(_archive, title)
    if entry is None:
        return {
            "title": title,
            "content": "",
            "note": "wikipedia page not found",
        }

    entry = _follow_redirects(entry)
    item = entry.get_item()
    html = bytes(item.content).decode("utf-8", errors="replace")
    page_title, content = extract_page_text(html)
    return format_page_contents(
        getattr(entry, "path", title),
        page_title or getattr(entry, "title", title),
        content,
        max_chars=max_chars,
    )


def _unavailable_note():
    if _state["downloading"]:
        downloaded = _state.get("downloaded_bytes") or 0
        megabytes = downloaded / (1024 * 1024)
        return f"local wikipedia is still downloading ({megabytes:.0f} MB so far)"
    if _state.get("error"):
        return f"local wikipedia is unavailable: {_state['error']}"
    return "local wikipedia is not ready"


def _open_archive(path):
    from libzim.reader import Archive

    global _archive, _archive_path
    _archive = Archive(str(path))
    _archive_path = Path(path)
    _set_state(ready=True, path=str(path), downloading=False, error=None)
    print(f"[WIKIPEDIA]: opened {path}")
    return _archive


async def _download_file(url, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    existing = partial.stat().st_size if partial.exists() else 0
    headers = dict(DOWNLOAD_HEADERS)
    if existing:
        headers["Range"] = f"bytes={existing}-"
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=300)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as response:
            if response.status == 200 and existing:
                existing = 0
                partial.unlink(missing_ok=True)
            elif response.status not in {200, 206}:
                response.raise_for_status()

            mode = "ab" if existing and response.status == 206 else "wb"
            downloaded = existing if mode == "ab" else 0
            _set_state(downloaded_bytes=downloaded)
            next_log_at = downloaded + PROGRESS_INTERVAL_BYTES
            print(f"[WIKIPEDIA]: downloading {url} to {destination.name}")

            with partial.open(mode) as handle:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    handle.write(chunk)
                    downloaded += len(chunk)
                    _set_state(downloaded_bytes=downloaded)
                    if downloaded >= next_log_at:
                        print(f"[WIKIPEDIA]: downloaded {downloaded / (1024 * 1024):.0f} MB")
                        next_log_at += PROGRESS_INTERVAL_BYTES

    partial.replace(destination)
    _set_state(downloaded_bytes=destination.stat().st_size)
    print(f"[WIKIPEDIA]: saved {destination}")
    return destination


async def _latest_remote_zim(prefix=WIKIPEDIA_ZIM_PREFIX):
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(WIKIPEDIA_INDEX_URL, headers=DOWNLOAD_HEADERS) as response:
            response.raise_for_status()
            html = await response.text()
    filename = pick_latest_zim(parse_zim_filenames(html), prefix=prefix)
    if not filename:
        raise RuntimeError(
            f"no zim matching {prefix!r} found at {WIKIPEDIA_INDEX_URL}"
        )
    return filename, urljoin(WIKIPEDIA_INDEX_URL, filename)


async def ensure_wikipedia(
    directory=WIKIPEDIA_DIR,
    prefix=WIKIPEDIA_ZIM_PREFIX,
):
    async with _lock:
        local = find_local_zim(directory, prefix)
        if local:
            if _archive is not None and _archive_path == local:
                return local
            _open_archive(local)
            return local

        _set_state(ready=False, downloading=True, error=None, downloaded_bytes=0)
        try:
            filename, url = await _latest_remote_zim(prefix=prefix)
            destination = Path(directory) / filename
            if not destination.exists() or destination.stat().st_size == 0:
                await _download_file(url, destination)
            _open_archive(destination)
            return destination
        except Exception as error:
            _set_state(ready=False, downloading=False, error=str(error))
            print(f"[WIKIPEDIA ERROR]: {error}")
            raise
