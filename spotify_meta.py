"""Fetch Spotify playlist/track metadata (no audio download from Spotify)."""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlparse

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
# Web-player persisted query (pathfinder). Official /v1 API is quota-capped
# for anonymous embed tokens and silently stops at the embed's 100 tracks.
_PATHFINDER_HASHES = [
    "91d4c2bc3e0cd1bc672281c4f1f59f43ff55ba726ca04a45810d99bd091f3f0e",
    "7982b11e21535cd2594badc40030b745671b61a1fa66766e569d45e6364f3422",
]


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
        """Artist-first queries — never lead with a bare generic title."""
        primary = self.artist.split(",")[0].strip() if self.artist else ""
        primary = re.sub(r"\([^)]*\)", "", primary).strip()
        title = self.title.strip()
        core = re.split(r"\s*-\s*", title, maxsplit=1)[0].strip()
        stripped = re.sub(
            r"\s*-\s*(mixed|radio[\s-]*mix|radio[\s-]*edit|extended(\s*mix)?)\s*$",
            "",
            title,
            flags=re.I,
        ).strip()
        paren = re.sub(r"\s*-\s*", " (", title, count=1) + ")" if " - " in title else title
        queries: list[str] = []
        if primary:
            queries.extend(
                [
                    f'{primary} "{core}"',
                    f"{primary} - {title}",
                    f"{primary} {title} official audio",
                    f"{primary} {paren}",
                    f"{primary} {stripped}" if stripped != title else "",
                    f"{primary} {core}",
                ]
            )
            if re.search(r"[A-Za-z]\d|\d[A-Za-z]", core):
                spaced = re.sub(r"([A-Za-z])(\d)", r"\1 \2", core)
                spaced = re.sub(r"(\d)([A-Za-z])", r"\1 \2", spaced)
                queries.append(f"{primary} {spaced}")
        core_words = [w for w in re.findall(r"[a-z0-9']+", core.lower()) if len(w) > 2]
        # Bare title only when it is distinctive (not "Toxic" / "Signals")
        if len(core_words) >= 3:
            queries.append(title)
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
    total_reported: int | None = None


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


def _ssl_context() -> ssl.SSLContext:
    """CA bundle that works inside a frozen PyInstaller .app."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def _http_get(url: str, timeout: float = 25.0) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _BROWSER_UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/javascript,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
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


def _http_json(url: str, token: str, timeout: float = 25.0) -> dict[str, Any] | None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _BROWSER_UA,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Origin": "https://open.spotify.com",
            "Referer": "https://open.spotify.com/",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=_ssl_context()
            ) as response:
                return json.loads(response.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:  # noqa: BLE001
                pass
            if exc.code in {429, 503} and attempt < 3:
                retry_after = 2.0 * (attempt + 1)
                try:
                    retry_after = max(retry_after, float(exc.headers.get("Retry-After") or 0))
                except (TypeError, ValueError):
                    pass
                # QUOTA_EXCEEDED won't recover quickly — don't burn retries.
                if b"QUOTA_EXCEEDED" in body:
                    return None
                time.sleep(min(retry_after, 20.0))
                continue
            if exc.code == 400 and b"PersistedQueryNotFound" in body:
                return {"__persisted_query_missing": True}
            return None
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return None
    return None


def _embed_access_token(html: str) -> str | None:
    match = re.search(r'"accessToken":"([^"]+)"', html)
    return match.group(1) if match else None


def _tracks_from_api_items(items: list[Any]) -> list[SpotifyTrack]:
    tracks: list[SpotifyTrack] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        track = raw.get("track") if "track" in raw else raw
        if not isinstance(track, dict) or track.get("type") == "episode":
            continue
        title = str(track.get("name") or "").strip()
        if not title:
            continue
        artists = ", ".join(
            str(a.get("name") or "")
            for a in (track.get("artists") or [])
            if isinstance(a, dict) and a.get("name")
        )
        sid = str(track.get("id") or "")
        uri = str(track.get("uri") or (f"spotify:track:{sid}" if sid else ""))
        try:
            duration_ms = int(track.get("duration_ms") or 0)
        except (TypeError, ValueError):
            duration_ms = 0
        tracks.append(
            SpotifyTrack(
                title=title,
                artist=artists,
                duration_ms=duration_ms,
                spotify_id=sid,
                uri=uri,
            )
        )
    return tracks


def _artist_names_from_pathfinder(data: dict[str, Any]) -> str:
    names: list[str] = []
    blobs: list[Any] = [data.get("artists")]
    album = data.get("albumOfTrack")
    if isinstance(album, dict):
        blobs.append(album.get("artists"))
    for blob in blobs:
        items = blob.get("items") if isinstance(blob, dict) else blob
        if not isinstance(items, list):
            continue
        for artist in items:
            if not isinstance(artist, dict):
                continue
            profile = artist.get("profile") if isinstance(artist.get("profile"), dict) else artist
            name = profile.get("name") if isinstance(profile, dict) else None
            if name and str(name) not in names:
                names.append(str(name))
        if names:
            break
    return ", ".join(names)


def _track_from_pathfinder_item(item: dict[str, Any]) -> SpotifyTrack | None:
    wrap = item.get("itemV2") if isinstance(item.get("itemV2"), dict) else item.get("item")
    data = wrap.get("data") if isinstance(wrap, dict) else None
    if not isinstance(data, dict):
        return None
    typename = str(data.get("__typename") or "")
    if typename and typename not in {"Track", "LocalTrack", "TrackResponseWrapper"}:
        return None
    title = str(data.get("name") or "").strip()
    if not title:
        return None
    uri = str(data.get("uri") or "")
    sid = ""
    if uri.startswith("spotify:track:"):
        sid = uri.rsplit(":", 1)[-1]
    duration_ms = 0
    duration = data.get("trackDuration")
    if isinstance(duration, dict):
        try:
            duration_ms = int(duration.get("totalMilliseconds") or 0)
        except (TypeError, ValueError):
            duration_ms = 0
    return SpotifyTrack(
        title=title,
        artist=_artist_names_from_pathfinder(data),
        duration_ms=duration_ms,
        spotify_id=sid,
        uri=uri or (f"spotify:track:{sid}" if sid else ""),
    )


def _discover_pathfinder_hashes(spotify_id: str) -> list[str]:
    """Read current fetchPlaylist hashes from the web-player JS bundle."""
    try:
        html = _http_get(f"https://open.spotify.com/playlist/{spotify_id}")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return []
    match = re.search(
        r"https://open\.spotifycdn\.com/cdn/build/web-player/web-player\.[a-f0-9]+\.js",
        html,
    )
    if not match:
        return []
    try:
        js = _http_get(match.group(0), timeout=40.0)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return []
    found: list[str] = []
    for hit in re.finditer(r"fetchPlaylist(?:Contents|Metadata)?", js):
        window = js[max(0, hit.start() - 280) : hit.end() + 280]
        for digest in re.findall(r"[a-f0-9]{64}", window):
            if digest not in found:
                found.append(digest)
    return found


def fetch_spotify_pathfinder_playlist_tracks(
    spotify_id: str, token: str
) -> tuple[list[SpotifyTrack], int | None]:
    """Paginate via the web-player GraphQL API (not quota-capped like /v1)."""
    hashes = list(_PATHFINDER_HASHES)
    page_size = 100
    chosen: str | None = None
    tracks: list[SpotifyTrack] = []
    total: int | None = None
    offset = 0
    discovered = False
    while offset < 2000:
        data: dict[str, Any] | None = None
        to_try = [chosen] if chosen else hashes
        for digest in to_try:
            if not digest:
                continue
            variables = json.dumps(
                {
                    "uri": f"spotify:playlist:{spotify_id}",
                    "offset": offset,
                    "limit": page_size,
                }
            )
            extensions = json.dumps(
                {"persistedQuery": {"version": 1, "sha256Hash": digest}}
            )
            url = (
                "https://api-partner.spotify.com/pathfinder/v1/query"
                f"?operationName=fetchPlaylist"
                f"&variables={quote(variables)}"
                f"&extensions={quote(extensions)}"
            )
            data = _http_json(url, token)
            if data and data.get("__persisted_query_missing"):
                data = None
                continue
            if data:
                chosen = digest
                break
        if not data and not chosen and not discovered:
            extra = _discover_pathfinder_hashes(spotify_id)
            discovered = True
            hashes = extra + [h for h in hashes if h not in extra]
            if extra:
                continue
            break
        if not data:
            break
        content = ((data.get("data") or {}).get("playlistV2") or {}).get("content")
        if not isinstance(content, dict):
            break
        if total is None:
            try:
                total = int(content.get("totalCount") or 0) or None
            except (TypeError, ValueError):
                total = None
        page: list[SpotifyTrack] = []
        for raw in content.get("items") or []:
            if not isinstance(raw, dict):
                continue
            parsed = _track_from_pathfinder_item(raw)
            if parsed:
                page.append(parsed)
        if not page:
            break
        tracks.extend(page)
        raw_count = len(content.get("items") or [])
        offset += raw_count or len(page)
        if total is not None and offset >= total:
            break
        if len(page) < page_size:
            break
    return tracks, total


def fetch_spotify_api_playlist_tracks(
    kind: str, spotify_id: str, token: str
) -> tuple[list[SpotifyTrack], int | None]:
    """Paginate official playlist/album tracks (embed page caps at 100)."""
    if kind == "playlist":
        base = f"https://api.spotify.com/v1/playlists/{spotify_id}/tracks"
        fields = "total,items(track(name,duration_ms,artists(name),id,uri,type))"
        page_size = 100
    elif kind == "album":
        base = f"https://api.spotify.com/v1/albums/{spotify_id}/tracks"
        fields = "total,items(name,duration_ms,artists(name),id,uri,type)"
        page_size = 50
    else:
        return [], None
    tracks: list[SpotifyTrack] = []
    total: int | None = None
    offset = 0
    while offset < 2000:
        url = (
            f"{base}?"
            + urlencode({"limit": page_size, "offset": offset, "fields": fields})
        )
        data = _http_json(url, token)
        if not data:
            break
        if total is None:
            try:
                total = int(data.get("total") or 0) or None
            except (TypeError, ValueError):
                total = None
        page = _tracks_from_api_items(data.get("items") or [])
        if not page:
            break
        tracks.extend(page)
        offset += page_size
        if total is not None and offset >= total:
            break
        if len(page) < page_size:
            break
    return tracks, total


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
    token = _embed_access_token(html)
    total_reported: int | None = None

    if kind == "track":
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
        return SpotifyPlaylist(
            name=name, spotify_id=spotify_id, tracks=tracks, total_reported=1
        )

    for raw in entity.get("trackList") or []:
        if not isinstance(raw, dict):
            continue
        parsed = _track_from_embed_item(raw)
        if parsed:
            tracks.append(parsed)

    if kind in {"playlist", "album"} and token:
        api_tracks: list[SpotifyTrack] = []
        if kind == "playlist":
            api_tracks, total_reported = fetch_spotify_pathfinder_playlist_tracks(
                spotify_id, token
            )
        if not api_tracks:
            api_tracks, total_reported = fetch_spotify_api_playlist_tracks(
                kind, spotify_id, token
            )
        if api_tracks and len(api_tracks) >= len(tracks):
            tracks = api_tracks

    return SpotifyPlaylist(
        name=name,
        spotify_id=spotify_id,
        tracks=tracks,
        total_reported=total_reported or len(tracks),
    )


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
