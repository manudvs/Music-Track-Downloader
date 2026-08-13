"""Fetch Spotify playlist/track metadata (no audio download from Spotify)."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass
class SpotifyTrack:
    title: str
    artist: str
    duration_ms: int
    spotify_id: str
    uri: str

    @property
    def duration_sec(self) -> float:
        return self.duration_ms / 1000.0 if self.duration_ms else 0.0

    @property
    def search_query(self) -> str:
        """Primary artist + title (quotes reserved for core song name)."""
        primary = self.artist.split(",")[0].strip() if self.artist else ""
        primary = re.sub(r"\([^)]*\)", "", primary).strip()
        title = self.title.strip()
        if primary and primary.lower() not in title.lower():
            return f"{primary} {title}"
        return title

    def search_queries(self) -> list[str]:
        """Several query shapes — YouTube titles often use (Radio Mix) not - Radio Mix."""
        primary = self.artist.split(",")[0].strip() if self.artist else ""
        primary = re.sub(r"\([^)]*\)", "", primary).strip()
        title = self.title.strip()
        # "Damn Night - Tatan Bilbao Remix" → core + remixer form
        paren = re.sub(r"\s*-\s*", " (", title, count=1) + ")" if " - " in title else title
        core = re.split(r"\s*-\s*", title, maxsplit=1)[0].strip()
        queries: list[str] = []
        if primary:
            queries.extend(
                [
                    f"{primary} {title}",
                    f"{primary} {paren}",
                    f'{primary} "{core}"',
                    f"{primary} {core}",
                ]
            )
        queries.extend([title, paren, core])
        # unique preserve order
        seen: set[str] = set()
        out: list[str] = []
        for q in queries:
            q = re.sub(r"\s+", " ", q).strip()
            if q and q.lower() not in seen:
                seen.add(q.lower())
                out.append(q)
        return out[:6]


def spotify_target_stem(artist: str, title: str) -> str:
    name = f"{artist.strip()} - {title.strip()}" if artist.strip() else title.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f#]', " ", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name[:120] or "track"


@dataclass
class SpotifyPlaylist:
    name: str
    spotify_id: str
    tracks: list[SpotifyTrack]


_SPOTIFY_ID_RE = re.compile(r"^[0-9A-Za-z]{22}$")


def parse_spotify_url(url: str) -> tuple[str, str] | None:
    """Return (kind, id) where kind is 'playlist' | 'track' | 'album', or None."""
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().removeprefix("www.")
    if host not in {"open.spotify.com", "spotify.com", "play.spotify.com"}:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    # /playlist/ID, /intl-fr/playlist/ID, /track/ID, …
    kind = None
    sid = None
    for i, part in enumerate(parts):
        if part in {"playlist", "track", "album"} and i + 1 < len(parts):
            kind = part
            sid = parts[i + 1].split("?")[0]
            break
    if not kind or not sid or not _SPOTIFY_ID_RE.match(sid):
        return None
    return kind, sid


def is_spotify_url(url: str) -> bool:
    return parse_spotify_url(url) is not None


def _http_get(url: str, timeout: float = 25.0) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "ignore")


def _parse_embed_entity(html: str) -> dict[str, Any] | None:
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    entity = (
        data.get("props", {})
        .get("pageProps", {})
        .get("state", {})
        .get("data", {})
        .get("entity")
    )
    return entity if isinstance(entity, dict) else None


def _track_from_embed_item(item: dict[str, Any]) -> SpotifyTrack | None:
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    artist = str(item.get("subtitle") or "").replace("\xa0", " ").strip()
    # Embed sometimes joins artists with special spaces / commas
    artist = re.sub(r"\s*,\s*", ", ", artist)
    uri = str(item.get("uri") or "")
    sid = ""
    if uri.startswith("spotify:track:"):
        sid = uri.rsplit(":", 1)[-1]
    duration = item.get("duration")
    try:
        duration_ms = int(duration or 0)
    except (TypeError, ValueError):
        duration_ms = 0
    return SpotifyTrack(
        title=title,
        artist=artist,
        duration_ms=duration_ms,
        spotify_id=sid,
        uri=uri or (f"spotify:track:{sid}" if sid else ""),
    )


def fetch_spotify_embed(kind: str, spotify_id: str) -> SpotifyPlaylist | None:
    """Read public metadata from Spotify's embed page (no audio, no API key)."""
    url = f"https://open.spotify.com/embed/{kind}/{spotify_id}"
    try:
        html = _http_get(url)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return None
    entity = _parse_embed_entity(html)
    if not entity:
        return None

    name = str(entity.get("name") or entity.get("title") or "Spotify").strip()
    tracks: list[SpotifyTrack] = []

    if kind == "track":
        # Single track entity looks like a trackList item + name fields
        item = {
            "title": entity.get("title") or entity.get("name"),
            "subtitle": entity.get("subtitle")
            or ", ".join(
                str(a.get("name") or "")
                for a in (entity.get("artists") or [])
                if isinstance(a, dict)
            ),
            "uri": entity.get("uri") or f"spotify:track:{spotify_id}",
            "duration": entity.get("duration"),
        }
        parsed = _track_from_embed_item(item)
        if parsed:
            tracks.append(parsed)
        return SpotifyPlaylist(name=name, spotify_id=spotify_id, tracks=tracks)

    for raw in entity.get("trackList") or []:
        if not isinstance(raw, dict):
            continue
        parsed = _track_from_embed_item(raw)
        if parsed:
            tracks.append(parsed)

    return SpotifyPlaylist(name=name, spotify_id=spotify_id, tracks=tracks)


def fetch_spotify_catalog(url: str) -> SpotifyPlaylist | None:
    """Resolve a Spotify open URL to playlist/track metadata."""
    parsed = parse_spotify_url(url)
    if not parsed:
        return None
    kind, sid = parsed
    if kind == "album":
        # Album embed uses the same trackList shape
        return fetch_spotify_embed("album", sid)
    return fetch_spotify_embed(kind, sid)


def spotify_tracks_as_entries(playlist: SpotifyPlaylist) -> list[dict[str, Any]]:
    """Build yt-dlp-like flat entries for the download engine."""
    entries: list[dict[str, Any]] = []
    for track in playlist.tracks:
        stem = spotify_target_stem(track.artist, track.title)
        entries.append(
            {
                "id": track.spotify_id or track.search_query,
                "title": stem,
                "track": track.title,
                "artist": track.artist,
                "duration": track.duration_sec or None,
                "webpage_url": f"https://open.spotify.com/track/{track.spotify_id}"
                if track.spotify_id
                else "",
                "url": "",
                "_spotify_query": track.search_query,
                "_spotify_queries": track.search_queries(),
                "_expected_duration": track.duration_sec or None,
                "_target_stem": stem,
            }
        )
    return entries
