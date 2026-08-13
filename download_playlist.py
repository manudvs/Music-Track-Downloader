#!/usr/bin/env python3
"""Download SoundCloud or YouTube tracks and playlists."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import random
import urllib.request
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

import yt_dlp

from audio_quality import (
    analyze_audio,
    candidate_acceptable,
    duration_match_score,
    meets_min_quality,
    newest_audio_file,
    quality_score,
    rank_search_candidates,
    resolve_ffprobe_bin,
    sanitize_filename_stem,
)
from spotify_meta import (
    fetch_spotify_catalog,
    is_spotify_url,
    parse_spotify_url,
    spotify_tracks_as_entries,
)

ResultKind = Literal["soundcloud", "youtube", "spotify", "skipped", "failed", "cancelled"]
Platform = Literal["soundcloud", "youtube", "spotify"]
MediaKind = Literal["track", "playlist"]
print_lock = threading.Lock()
cleanup_lock = threading.Lock()
_worker_cookie_lock = threading.Lock()
_worker_cookie_counter = 0
_soundcloud_gate = threading.Semaphore(2)
_soundcloud_pace_lock = threading.Lock()
_soundcloud_last_start = 0.0
SOUNDCLOUD_PACE_SEC = 0.8
SOUNDCLOUD_MAX_RETRIES = 5
_log_callback: Any | None = None
_cancel_event = threading.Event()


def extend_system_path() -> None:
    """Ensure common tool locations are visible (GUI bundles often have a minimal PATH)."""
    extra: list[str] = []
    if sys.platform == "win32":
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(key)
            if base:
                extra.append(str(Path(base) / "ffmpeg" / "bin"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            extra.append(str(Path(local) / "Microsoft" / "WinGet" / "Links"))
        extra.append(r"C:\ffmpeg\bin")
    else:
        extra.extend(
            [
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/opt/homebrew/sbin",
                "/usr/local/sbin",
            ]
        )
    current = os.environ.get("PATH", "")
    prefix = os.pathsep.join(p for p in extra if p and Path(p).is_dir())
    if prefix and prefix not in current:
        os.environ["PATH"] = prefix + os.pathsep + current


def platform_tool(base: str) -> str:
    return f"{base}.exe" if sys.platform == "win32" else base


def set_log_callback(callback: Any | None) -> None:
    """Optional hook for GUI / other frontends to receive log lines."""
    global _log_callback
    _log_callback = callback


def request_cancel() -> None:
    """Ask an in-progress download_playlist() run to stop."""
    _cancel_event.set()


def clear_cancel() -> None:
    _cancel_event.clear()


def is_cancelled() -> bool:
    return _cancel_event.is_set()


class CancelledError(Exception):
    """Raised when the user stops a download."""


def log(message: str) -> None:
    with print_lock:
        print(message, flush=True)
        if _log_callback is not None:
            try:
                _log_callback(message)
            except Exception:  # noqa: BLE001 - UI must not crash downloads
                pass


def sanitize_folder_name(name: str) -> str:
    """Make a playlist name safe as a folder name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.rstrip(". ")
    return cleaned[:80] or "playlist"


_ffmpeg_location: str | None = None  # directory containing ffmpeg + ffprobe
_ffmpeg_bin: str | None = None
_ffprobe_is_real: bool | None = None


def has_real_ffprobe() -> bool:
    """True when a real ffprobe binary exists (not our ffmpeg symlink fallback)."""
    global _ffprobe_is_real
    if _ffprobe_is_real is not None:
        return _ffprobe_is_real

    extend_system_path()
    probe = shutil.which("ffprobe")
    if not probe:
        _ffprobe_is_real = False
        return False

    wrap = Path(tempfile.gettempdir()) / "mtd-ffmpeg-bin" / platform_tool("ffprobe")
    try:
        if Path(probe).resolve() == wrap.resolve():
            _ffprobe_is_real = False
            return False
    except OSError:
        pass

    try:
        result = subprocess.run(
            [probe, "-version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        combined = f"{result.stdout}\n{result.stderr}"
        _ffprobe_is_real = "ffprobe version" in combined
    except (OSError, subprocess.TimeoutExpired):
        _ffprobe_is_real = False
    return _ffprobe_is_real


def resolve_ffmpeg_dir() -> str | None:
    """Return a directory that contains both `ffmpeg` and `ffprobe` (yt-dlp needs both)."""
    global _ffmpeg_location, _ffmpeg_bin, _ffprobe_is_real
    if _ffmpeg_location is not None:
        return _ffmpeg_location or None

    extend_system_path()

    # 1) Bundled next to the app executable (static ffmpeg + ffprobe)
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        ff_name = platform_tool("ffmpeg")
        fp_name = platform_tool("ffprobe")
        bundled_ff = exe_dir / ff_name
        bundled_fp = exe_dir / fp_name
        if bundled_ff.is_file() and bundled_fp.is_file():
            usable = sys.platform == "win32" or os.access(bundled_ff, os.X_OK)
            if usable:
                directory = str(exe_dir)
                _ffmpeg_location = directory
                _ffmpeg_bin = str(bundled_ff)
                _ffprobe_is_real = True
                os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
                return directory

    # 2) System install (Homebrew) — dev machines
    which_ff = shutil.which("ffmpeg")
    which_fp = shutil.which("ffprobe")
    if which_ff and which_fp:
        directory = str(Path(which_ff).resolve().parent)
        _ffmpeg_location = directory
        _ffmpeg_bin = which_ff
        _ffprobe_is_real = True
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
        return directory

    # 3) imageio-ffmpeg fallback — ffmpeg only; ffprobe may be faked
    sources: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        exe_dir = Path(sys.executable).resolve().parent
        sources.extend(
            [
                meipass / platform_tool("ffmpeg"),
                exe_dir / platform_tool("ffmpeg"),
                meipass / "ffmpeg",
                exe_dir / "ffmpeg",
                exe_dir.parent / "Resources" / "ffmpeg",
            ]
        )
        sources.extend(sorted(meipass.glob("ffmpeg*")))
        sources.extend(sorted((meipass / "imageio_ffmpeg" / "binaries").glob("ffmpeg*")))
        sources.extend(
            sorted((exe_dir.parent / "Resources" / "imageio_ffmpeg" / "binaries").glob("ffmpeg*"))
        )

    try:
        import imageio_ffmpeg

        sources.append(Path(imageio_ffmpeg.get_ffmpeg_exe()))
    except Exception:  # noqa: BLE001
        pass

    src = next(
        (
            p
            for p in sources
            if p.is_file() and (sys.platform == "win32" or os.access(p, os.X_OK))
        ),
        None,
    )
    if src is None:
        _ffmpeg_location = ""
        return None

    wrap = Path(tempfile.gettempdir()) / "mtd-ffmpeg-bin"
    wrap.mkdir(parents=True, exist_ok=True)
    ff_name = platform_tool("ffmpeg")
    fp_name = platform_tool("ffprobe")
    dest = wrap / ff_name
    try:
        if dest.is_symlink() or dest.exists():
            dest.unlink()
        if sys.platform == "win32":
            shutil.copy2(src, dest)
        else:
            dest.symlink_to(src)
    except OSError:
        shutil.copy2(src, dest)
        if sys.platform != "win32":
            dest.chmod(0o755)

    if not has_real_ffprobe():
        dest = wrap / fp_name
        try:
            if dest.is_symlink() or dest.exists():
                dest.unlink()
            if sys.platform == "win32":
                shutil.copy2(src, dest)
            else:
                dest.symlink_to(src)
        except OSError:
            shutil.copy2(src, dest)
            if sys.platform != "win32":
                dest.chmod(0o755)

    _ffmpeg_location = str(wrap)
    _ffmpeg_bin = str(wrap / ff_name)
    os.environ["PATH"] = str(wrap) + os.pathsep + os.environ.get("PATH", "")
    return _ffmpeg_location


def resolve_ffmpeg() -> str | None:
    """Locate ffmpeg binary path (file)."""
    global _ffmpeg_bin
    directory = resolve_ffmpeg_dir()
    if not directory:
        return None
    if _ffmpeg_bin:
        return _ffmpeg_bin
    candidate = Path(directory) / platform_tool("ffmpeg")
    return str(candidate) if candidate.is_file() else None


def apply_ffmpeg(ydl_opts: dict) -> None:
    directory = resolve_ffmpeg_dir()
    if directory:
        ydl_opts["ffmpeg_location"] = directory


def resolve_deno() -> str | None:
    """Locate Deno (required by modern yt-dlp for YouTube JS challenges)."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            [
                meipass / platform_tool("deno"),
                exe_dir / platform_tool("deno"),
                meipass / "deno",
                exe_dir / "deno",
                exe_dir.parent / "Resources" / "deno",
                exe_dir.parent / "Frameworks" / "deno",
            ]
        )
    found = shutil.which("deno")
    if found:
        candidates.append(Path(found))
    if sys.platform != "win32":
        candidates.extend(
            [
                Path("/opt/homebrew/bin/deno"),
                Path("/usr/local/bin/deno"),
            ]
        )
    for path in candidates:
        if path.is_file() and (sys.platform == "win32" or os.access(path, os.X_OK)):
            return str(path)
    return None


def apply_js_runtime(ydl_opts: dict) -> None:
    """Enable Deno/Node so YouTube formats resolve (EJS challenges)."""
    deno = resolve_deno()
    if deno:
        ydl_opts["js_runtimes"] = {"deno": {"path": deno}}
        return
    node = shutil.which("node")
    if node:
        ydl_opts["js_runtimes"] = {"node": {"path": node}}
        return
    # Keep default deno key so yt-dlp still looks on PATH.
    ydl_opts["js_runtimes"] = {"deno": {}}


def convert_youtube_leftovers(output_dir: Path) -> tuple[int, int]:
    """Convert leftover YouTube .mp4 / .webm into .m4a. Returns (ok, failed)."""
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        log("ffmpeg introuvable — impossible de convertir les .mp4/.webm")
        return 0, 0

    ok = 0
    failed = 0
    sources = sorted(
        list(output_dir.glob("*.mp4")) + list(output_dir.glob("*.webm")),
        key=lambda p: p.name,
    )
    for source in sources:
        if is_cancelled():
            break
        m4a = source.with_suffix(".m4a")
        if m4a.exists() and m4a.stat().st_size > 0 and m4a != source:
            source.unlink(missing_ok=True)
            ok += 1
            continue

        cover = None
        for ext in (".jpg", ".jpeg", ".webp", ".png"):
            candidate = source.with_suffix(ext)
            if candidate.exists():
                cover = candidate
                break

        cmd = [ffmpeg, "-y", "-i", str(source)]
        if cover is not None:
            cmd += [
                "-i",
                str(cover),
                "-map",
                "0:a:0",
                "-map",
                "1:0",
                "-c:a",
                "copy",
                "-c:v",
                "mjpeg",
                "-disposition:v",
                "attached_pic",
            ]
        else:
            cmd += ["-vn", "-c:a", "copy"]
        cmd.append(str(m4a))

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0 or not m4a.exists():
                cmd = [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(source),
                    "-vn",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "256k",
                    str(m4a),
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode == 0 and m4a.exists() and m4a.stat().st_size > 0:
                source.unlink(missing_ok=True)
                if cover is not None:
                    cover.unlink(missing_ok=True)
                for side in (
                    source.with_suffix(".webp"),
                    source.with_suffix(".jpg"),
                    source.with_suffix(".jpeg"),
                ):
                    side.unlink(missing_ok=True)
                ok += 1
            else:
                failed += 1
                log(f"Conversion échouée: {source.name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log(f"Conversion échouée: {source.name} ({exc})")

    cleanup_sidecar_images(output_dir)
    return ok, failed


def convert_file_to_m4a(source: Path, dest: Path | None = None) -> Path | None:
    """Convert a single audio/video file to .m4a."""
    if not source.is_file():
        return None
    if source.suffix.lower() == ".m4a" and source.stat().st_size > 1024:
        return source
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        return None
    dest = dest or source.with_suffix(".m4a")
    cmd = [ffmpeg, "-y", "-i", str(source), "-vn", "-c:a", "aac", "-b:a", "256k", str(dest)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0 and dest.is_file() and dest.stat().st_size > 1024:
        if dest != source:
            source.unlink(missing_ok=True)
        return dest
    return None


def purge_orphan_media(output_dir: Path, *, keep_stems: set[str] | None = None) -> None:
    """Remove stray .webm/.mp4 and sidecars not matching kept Spotify stems."""
    keep = keep_stems or set()
    for ext in (".webm", ".mp4", ".webp", ".jpg", ".jpeg", ".png"):
        for path in output_dir.glob(f"*{ext}"):
            if not path.is_file():
                continue
            stem = path.stem
            if keep and any(stem == k or stem.startswith(k) for k in keep):
                continue
            path.unlink(missing_ok=True)


def classify_url(url: str) -> tuple[Platform, MediaKind] | None:
    """Detect SoundCloud/YouTube/Spotify and track vs playlist."""
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path or ""
    qs = parse_qs(parsed.query)

    if host.endswith("soundcloud.com"):
        if "/sets/" in path:
            return "soundcloud", "playlist"
        return "soundcloud", "track"

    if host in {"youtu.be", "youtube.com", "m.youtube.com", "music.youtube.com"}:
        if host == "youtu.be":
            return "youtube", "track"
        if "/playlist" in path or (qs.get("list") and not qs.get("v")):
            return "youtube", "playlist"
        return "youtube", "track"

    spotify = parse_spotify_url(url)
    if spotify:
        kind, _sid = spotify
        if kind in {"playlist", "album"}:
            return "spotify", "playlist"
        return "spotify", "track"

    return None


def is_supported_media_url(url: str) -> bool:
    return classify_url(url) is not None or is_spotify_url(url)


def normalize_url(url: str) -> str:
    """Strip sharing junk. Keep single-item intent (ignore playlist context params)."""
    url = url.strip()
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    classified = classify_url(url)
    if classified is None:
        return url

    platform, kind = classified
    if platform == "soundcloud":
        # Drop ?in= / ?si= / utm — path alone identifies track or set.
        return urlunparse(
            (parsed.scheme or "https", parsed.netloc, parsed.path, "", "", "")
        )

    if platform == "spotify":
        spotify = parse_spotify_url(url)
        if spotify:
            kind_sp, sid = spotify
            return f"https://open.spotify.com/{kind_sp}/{sid}"
        return urlunparse(
            (parsed.scheme or "https", parsed.netloc, parsed.path, "", "", "")
        )

    # YouTube
    qs = parse_qs(parsed.query)
    keep: dict[str, str] = {}
    if host.endswith("youtu.be"):
        video_id = parsed.path.strip("/")
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
    if kind == "playlist" and qs.get("list"):
        keep["list"] = qs["list"][0]
        path = "/playlist"
    elif qs.get("v"):
        keep["v"] = qs["v"][0]
        path = "/watch"
    elif qs.get("list"):
        keep["list"] = qs["list"][0]
        path = "/playlist"
    else:
        return urlunparse(
            (parsed.scheme or "https", parsed.netloc, parsed.path, "", "", "")
        )
    return urlunparse(
        (parsed.scheme or "https", "www.youtube.com", path, "", urlencode(keep), "")
    )


def entries_from_info(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Playlist entries, or a one-item list for a single track/video."""
    raw = info.get("entries")
    if raw is not None:
        return [entry for entry in raw if entry]
    return [info]


def entry_media_url(entry: dict[str, Any], platform: Platform) -> str | None:
    track_url = entry.get("url") or entry.get("webpage_url") or entry.get("original_url")
    if isinstance(track_url, str) and track_url.startswith("http"):
        return track_url
    track_id = entry.get("id")
    if platform == "youtube" and track_id:
        return f"https://www.youtube.com/watch?v={track_id}"
    if isinstance(track_url, str) and track_url:
        return track_url
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download SoundCloud, YouTube, or Spotify (search) tracks and playlists.",
    )
    parser.add_argument(
        "url",
        help="SoundCloud, YouTube, or Spotify URL (track or playlist)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("downloads"),
        help="Output directory (default: ./downloads)",
    )
    parser.add_argument(
        "-f",
        "--format",
        default="best",
        choices=("best", "mp3", "m4a", "opus", "wav"),
        help=(
            "Output format (default: best). "
            "'best' keeps the highest-quality stream without re-encoding (recommended for DJ use)."
        ),
    )
    parser.add_argument(
        "--items",
        metavar="RANGE",
        help="Download only specific items, e.g. '1-10' or '3,7,12' (yt-dlp playlist syntax)",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=4,
        metavar="N",
        help="Download N tracks in parallel (default: 4, max: 12)",
    )
    parser.add_argument(
        "--browser",
        metavar="BROWSER",
        help=(
            "Use cookies from your logged-in browser session "
            "(e.g. chrome, safari, firefox). Easiest way to use your SoundCloud account."
        ),
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        metavar="FILE",
        help="Netscape cookies file exported from your browser.",
    )
    parser.add_argument(
        "--oauth-token",
        metavar="TOKEN",
        help="SoundCloud OAuth token (alternative to --browser / --cookies).",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip tracks that are already downloaded (useful when retrying missing ones).",
    )
    parser.add_argument(
        "--youtube-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="If SoundCloud fails, search YouTube and download the best match (default: on).",
    )
    parser.add_argument(
        "--quality-max",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Verify bitrate/duration after download and try alternate SoundCloud/YouTube "
            "matches when quality is low (default: on)."
        ),
    )
    return parser


def apply_auth(ydl_opts: dict, cookies: Path | None, oauth_token: str | None) -> None:
    if cookies:
        ydl_opts["cookiefile"] = str(cookies)
    if oauth_token:
        ydl_opts["username"] = "oauth"
        ydl_opts["password"] = oauth_token


def validate_cookie_file(path: Path) -> bool:
    """Reject empty or corrupted Netscape cookie exports (parallel writes can zero the file)."""
    try:
        if not path.is_file() or path.stat().st_size < 10:
            return False
        sample = path.read_bytes()[:8192]
        if b"\x00" in sample:
            return False
        text = sample.decode("utf-8", errors="ignore")
        return "# Netscape HTTP Cookie File" in text or ".com" in text
    except OSError:
        return False


def worker_ydl_opts(base_opts: dict) -> dict:
    """Give each parallel worker its own cookie copy so yt-dlp cannot corrupt a shared file."""
    opts = dict(base_opts)
    cookie_file = opts.get("cookiefile")
    if not cookie_file:
        return opts

    master = Path(cookie_file)
    if not validate_cookie_file(master):
        opts.pop("cookiefile", None)
        return opts

    global _worker_cookie_counter
    with _worker_cookie_lock:
        _worker_cookie_counter += 1
        worker_id = _worker_cookie_counter
    worker_path = master.parent / "workers" / f"cookies-{worker_id}.txt"
    worker_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # copy2 preserves mode — never copy a read-only master to a read-only worker.
        shutil.copyfile(master, worker_path)
        worker_path.chmod(0o600)
    except OSError as exc:
        opts.pop("cookiefile", None)
        log(f"    ↳ Cookies worker indisponibles ({exc})")
        return opts
    opts["cookiefile"] = str(worker_path)
    return opts


def snapshot_audio_names(output_dir: Path) -> set[str]:
    audio_exts = {".m4a", ".mp3", ".opus", ".wav", ".flac", ".aac", ".mp4", ".webm"}
    return {
        path.name
        for path in output_dir.iterdir()
        if path.is_file() and path.suffix.lower() in audio_exts
    }


def is_rate_limited(exc: BaseException) -> bool:
    if isinstance(exc, HTTPError) and exc.code in (429, 403):
        return True
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, HTTPError) and cause.code in (429, 403):
        return True
    text = str(exc).lower()
    return "429" in text or "too many requests" in text or "rate limit" in text


def pace_soundcloud() -> None:
    """Stagger SoundCloud download starts to avoid API bursts."""
    global _soundcloud_last_start
    with _soundcloud_pace_lock:
        now = time.monotonic()
        wait = SOUNDCLOUD_PACE_SEC - (now - _soundcloud_last_start)
        if wait > 0:
            time.sleep(wait)
        _soundcloud_last_start = time.monotonic()


def soundcloud_retry_wait(attempt: int) -> float:
    return min(90.0, (2**attempt) * 3 + random.uniform(0, 2))


def safari_cookies_readable() -> bool:
    """macOS often blocks Safari cookie DB without Full Disk Access."""
    candidates = [
        Path.home()
        / "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
        Path.home() / "Library/Cookies/Cookies.binarycookies",
    ]
    for path in candidates:
        try:
            with path.open("rb") as handle:
                handle.read(1)
            return True
        except OSError:
            continue
    return False


def export_browser_cookies(browser: str, dest: Path) -> Path:
    """Extract browser cookies once so parallel workers don't fight Chrome's DB."""
    if browser == "safari" and not safari_cookies_readable():
        raise PermissionError(
            "Safari cookies bloqués par macOS (Accès disque complet requis)"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.with_name(dest.name + ".part")
    if staging.exists():
        staging.unlink(missing_ok=True)
    # Keep yt-dlp cookie errors out of the GUI activity log.
    sink = io.StringIO()
    with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(sink):
        with yt_dlp.YoutubeDL(
            {
                "quiet": True,
                "no_warnings": True,
                "cookiesfrombrowser": (browser,),
                "skip_download": True,
            }
        ) as ydl:
            _ = list(ydl.cookiejar)
            ydl.cookiejar.save(
                filename=str(staging), ignore_discard=True, ignore_expires=True
            )
    if not validate_cookie_file(staging):
        staging.unlink(missing_ok=True)
        raise ValueError("Export cookies invalide (fichier vide ou corrompu)")
    if dest.exists():
        dest.unlink(missing_ok=True)
    staging.replace(dest)
    dest.chmod(0o600)
    return dest


def explain_cookie_failure(browser: str, exc: BaseException | None = None) -> None:
    """Tell the user why browser cookies failed and how to continue."""
    if browser == "safari":
        log(
            "Safari inaccessible (macOS bloque les cookies). "
            "Choisis Chrome ou « Aucun » — suite sans compte."
        )
        return
    detail = f" ({exc})" if exc else ""
    log(f"Cookies {browser} indisponibles{detail}. Suite sans compte SoundCloud.")


def art_postprocessors() -> list[dict]:
    """Embed cover art with mutagen. Must run last — FFmpegMetadata would wipe it."""
    return [{"key": "EmbedThumbnail"}]


def apply_format(ydl_opts: dict, audio_format: str, *, embed_art: bool = True) -> None:
    apply_ffmpeg(ydl_opts)
    ydl_opts["format"] = "ba*[abr>=256]/ba*[abr>=160]/bestaudio/best"
    # Keep fragment parallelism modest — SoundCloud rate-limits aggressive bursts.
    ydl_opts["concurrent_fragment_downloads"] = 4
    ydl_opts["sleep_interval_requests"] = 0.5
    if embed_art:
        ydl_opts["writethumbnail"] = True
        ydl_opts["convertthumbnails"] = "jpg"

    postprocessors: list[dict] = []
    if audio_format != "best":
        postprocessors.append(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "0",
            }
        )
    if embed_art:
        postprocessors.extend(art_postprocessors())

    if postprocessors:
        ydl_opts["postprocessors"] = postprocessors


def archive_path(output_dir: Path) -> Path:
    return output_dir / ".download-archive.txt"


def existing_audio_for_title(output_dir: Path, title: str) -> bool:
    """Match by exact filename stem so *, ?, [] in titles don't break glob."""
    audio_exts = {".m4a", ".mp3", ".opus", ".wav", ".flac", ".aac", ".mp4"}
    for path in output_dir.iterdir():
        if path.is_file() and path.suffix.lower() in audio_exts and path.stem == title:
            return True
    return False


def is_track_downloaded(
    output_dir: Path,
    index: int,
    entry: dict[str, Any],
    title_map: dict[str, str],
) -> bool:
    target_stem = entry.get("_target_stem")
    if target_stem:
        final = output_dir / f"{target_stem}.m4a"
        if final.is_file() and final.stat().st_size > 1024:
            return True

    track_id = str(entry.get("id") or "")
    archive = archive_path(output_dir)

    if track_id and archive.exists():
        for line in archive.read_text().splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == track_id:
                return True

    title = entry.get("title") or title_map.get(track_id) or ""
    if title and str(title).upper() != "NA" and existing_audio_for_title(output_dir, str(title)):
        return True

    # Legacy numbered filenames from earlier downloads.
    prefix = f"{index:02d} - "
    for path in output_dir.iterdir():
        if path.is_file() and path.name.startswith(prefix):
            return True
    return False


def slug_to_words(slug: str) -> str:
    slug = re.sub(r"[^\w\s-]", " ", slug)
    slug = re.sub(r"[-_]+", " ", slug)
    return re.sub(r"\s+", " ", slug).strip()


def http_json(url: str) -> Any | None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8", "ignore"))
    except (OSError, json.JSONDecodeError):
        return None


def soundcloud_client_id() -> str | None:
    cache = Path.home() / ".cache" / "yt-dlp" / "soundcloud" / "client_id.json"
    if cache.is_file():
        try:
            data = json.loads(cache.read_text())
            client_id = data.get("data")
            if isinstance(client_id, str) and client_id:
                return client_id
        except json.JSONDecodeError:
            pass
    return None


def fetch_playlist_titles(playlist_url: str) -> dict[str, str]:
    """Fetch track titles via SoundCloud API (works even when streams return 404)."""
    titles: dict[str, str] = {}
    meta = fetch_playlist_track_meta(playlist_url)
    for track_id, info in meta.items():
        if info.get("title"):
            titles[track_id] = info["title"]
    return titles


def fetch_playlist_track_meta(playlist_url: str) -> dict[str, dict[str, str]]:
    """Return track_id -> {title, artist, permalink_url} for playlist tracks."""
    client_id = soundcloud_client_id()
    if not client_id:
        return {}

    resolved = http_json(
        f"https://api-v2.soundcloud.com/resolve?url={quote(playlist_url, safe='')}"
        f"&client_id={client_id}"
    )
    if not isinstance(resolved, dict):
        return {}

    track_ids = [str(track["id"]) for track in (resolved.get("tracks") or []) if track.get("id")]
    if not track_ids:
        return {}

    meta: dict[str, dict[str, str]] = {}
    batch_size = 50
    for start in range(0, len(track_ids), batch_size):
        batch = track_ids[start : start + batch_size]
        tracks = http_json(
            "https://api-v2.soundcloud.com/tracks?"
            f"ids={','.join(batch)}&client_id={client_id}"
        )
        if not isinstance(tracks, list):
            continue
        for track in tracks:
            track_id = str(track.get("id") or "")
            if not track_id:
                continue
            user = track.get("user") or {}
            artist = ""
            if isinstance(user, dict):
                artist = str(user.get("username") or user.get("permalink") or "")
            meta[track_id] = {
                "title": str(track.get("title") or ""),
                "artist": artist,
                "permalink_url": str(track.get("permalink_url") or ""),
            }
    return meta


def youtube_search_query(
    entry: dict[str, Any],
    title_map: dict[str, str],
    track_meta: dict[str, dict[str, str]] | None = None,
) -> str | None:
    page_url = entry.get("webpage_url") or entry.get("url") or ""
    parsed = urlparse(page_url)
    artist = ""
    if parsed.netloc == "soundcloud.com":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2:
            artist = parts[0].replace("-", " ")

    track_id = str(entry.get("id") or "")
    info = (track_meta or {}).get(track_id) or {}
    if info.get("artist"):
        artist = info["artist"]

    title = entry.get("title")
    if (not title or str(title).upper() == "NA") and info.get("title"):
        title = info["title"]
    if (not title or str(title).upper() == "NA") and track_id in title_map:
        title = title_map[track_id]

    if title and str(title).upper() != "NA":
        if artist and artist.lower() not in str(title).lower():
            return f"{artist} {title}"
        return str(title)

    if parsed.netloc == "soundcloud.com":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2:
            slug = parts[1]
            return f"{parts[0]} {slug_to_words(slug)}"

    if info.get("permalink_url"):
        parsed_permalink = urlparse(info["permalink_url"])
        parts = [part for part in parsed_permalink.path.split("/") if part]
        if len(parts) >= 2:
            return f"{parts[0]} {slug_to_words(parts[1])}"

    return None


def cleanup_sidecar_images(output_dir: Path) -> None:
    """Remove leftover thumbnail files when a matching audio file exists."""
    with cleanup_lock:
        audio_exts = {".m4a", ".mp3", ".opus", ".wav", ".flac", ".aac", ".mp4"}
        audio_stems = {
            path.stem for path in output_dir.iterdir() if path.suffix.lower() in audio_exts
        }
        for ext in (".webp", ".jpg", ".jpeg", ".png"):
            for image in output_dir.glob(f"*{ext}"):
                if image.stem in audio_stems:
                    image.unlink()


def fetch_playlist_display_name(url: str) -> str | None:
    """Return a title for folder naming (track, set, YouTube, or Spotify)."""
    url = normalize_url(url)
    classified = classify_url(url)
    if classified is None:
        return None
    platform, _kind = classified

    if platform == "spotify":
        catalog = fetch_spotify_catalog(url)
        return catalog.name if catalog else None

    if platform == "soundcloud":
        client_id = soundcloud_client_id()
        if client_id:
            resolved = http_json(
                f"https://api-v2.soundcloud.com/resolve?url={quote(url, safe='')}"
                f"&client_id={client_id}"
            )
            if isinstance(resolved, dict) and resolved.get("title"):
                return str(resolved["title"])

    try:
        with yt_dlp.YoutubeDL(
            {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
        ) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(info, dict):
        return None
    title = info.get("title") or info.get("playlist_title") or info.get("fulltitle")
    return str(title) if title else None


def download_url(
    url: str,
    output_template: str,
    base_opts: dict,
    *,
    youtube: bool = False,
    soundcloud: bool = False,
    audio_format: str = "best",
) -> bool:
    if is_cancelled():
        raise CancelledError()

    output_dir = Path(output_template).parent
    before_files = snapshot_audio_names(output_dir)

    opts = {
        **worker_ydl_opts(base_opts),
        "outtmpl": output_template,
        "noplaylist": True,
        "ignoreerrors": False,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    apply_ffmpeg(opts)
    if youtube:
        apply_js_runtime(opts)

    def _progress_hook(status: dict) -> None:
        if is_cancelled():
            raise CancelledError("Download cancelled by user")

    opts["progress_hooks"] = [_progress_hook]

    if youtube:
        # Keep cookiefile for YouTube anti-bot; drop SoundCloud oauth only.
        opts.pop("username", None)
        opts.pop("password", None)
        opts["format"] = "bestaudio/best"
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["tv", "web_creator", "android", "web"],
            }
        }
        codec = audio_format if audio_format != "best" else "m4a"
        postprocessors: list[dict] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec,
                "preferredquality": "0",
            }
        ]
        if has_real_ffprobe():
            opts["writethumbnail"] = True
            opts["convertthumbnails"] = "jpg"
            postprocessors.extend(art_postprocessors())
        opts["postprocessors"] = postprocessors

    attempts = SOUNDCLOUD_MAX_RETRIES if soundcloud else 1

    def _attempt_download() -> bool:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        return True

    for attempt in range(attempts):
        if is_cancelled():
            raise CancelledError()
        try:
            if soundcloud:
                with _soundcloud_gate:
                    pace_soundcloud()
                    return _attempt_download()
            return _attempt_download()
        except CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize yt-dlp / urllib failures
            if is_cancelled() or "cancelled" in str(exc).lower():
                raise CancelledError() from exc
            if soundcloud and is_rate_limited(exc) and attempt < attempts - 1:
                wait = soundcloud_retry_wait(attempt)
                log(f"    ↳ SoundCloud saturé (429), pause {wait:.0f}s…")
                time.sleep(wait)
                continue
            after_files = snapshot_audio_names(output_dir)
            if after_files - before_files:
                return True
            if isinstance(exc, json.JSONDecodeError):
                log(f"    ↳ Réponse invalide (JSON): {exc}")
                return False
            msg = str(exc).splitlines()[-1] if str(exc) else "erreur inconnue"
            log(f"    ↳ {msg}")
            return False
    return False


def parse_item_range(items: str | None, total: int) -> set[int] | None:
    if not items:
        return None

    selected: set[int] = set()
    for part in items.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            selected.update(range(start, end + 1))
        else:
            selected.add(int(part))

    return {index for index in selected if 1 <= index <= total}


def list_search_candidates(
    prefix: str,
    query: str,
    n: int,
    base_opts: dict,
) -> list[dict[str, Any]]:
    """Flat-search SoundCloud (scsearch) or YouTube (ytsearch)."""
    if not query or n < 1:
        return []
    opts = {
        **worker_ydl_opts(base_opts),
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": True,
    }
    if prefix.startswith("yt"):
        apply_js_runtime(opts)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"{prefix}{n}:{query}", download=False)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(info, dict):
        return []
    entries = info.get("entries") or []
    return [e for e in entries if isinstance(e, dict) and e]


def candidate_url(entry: dict[str, Any], *, youtube: bool) -> str | None:
    url = entry.get("webpage_url") or entry.get("url") or entry.get("id")
    if not url:
        return None
    url = str(url)
    if youtube and not url.startswith("http"):
        return f"https://www.youtube.com/watch?v={url}"
    if not youtube and url.startswith("http") is False and "/" not in url:
        # SoundCloud flat id alone is not enough
        return None
    return url


def track_scratch_dir() -> Path:
    """Local temp folder — never on USB (ffprobe/ffmpeg fail there often)."""
    base = Path(tempfile.gettempdir()) / "mtd-tracks"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="mtd-track-", dir=str(base)))


def evaluate_new_file(
    output_dir: Path,
    before: set[str],
    *,
    expected_duration: float | None,
) -> tuple[Path | None, Any]:
    path = newest_audio_file(output_dir, before)
    if path is None:
        # yt-dlp sometimes writes dl.m4a / dl.webm after snapshot
        for candidate in sorted(
            output_dir.iterdir(),
            key=lambda p: p.stat().st_mtime if p.is_file() else 0,
            reverse=True,
        ):
            if (
                candidate.is_file()
                and candidate.suffix.lower()
                in {".m4a", ".mp3", ".opus", ".wav", ".flac", ".aac", ".mp4", ".webm"}
                and candidate.name not in before
                and not candidate.name.startswith("_best")
                and candidate.stat().st_size > 1024
            ):
                path = candidate
                break
    if path is None:
        return None, None
    if path.suffix.lower() in {".webm", ".mp4"}:
        converted = convert_file_to_m4a(path)
        if converted:
            path = converted
    probe = resolve_ffprobe_bin(resolve_ffmpeg_dir())
    quality = analyze_audio(path, ffprobe=probe)
    if quality is None:
        time.sleep(0.4)
        quality = analyze_audio(path, ffprobe=probe)
    return path, quality


def _finalize_track_file(
    best_path: Path,
    output_dir: Path,
    target_stem: str | None,
) -> Path:
    """Copy best temp download to final Spotify-style filename."""
    if not best_path.is_file():
        raise FileNotFoundError(best_path)
    if not target_stem:
        return best_path
    final = output_dir / f"{target_stem}.m4a"
    if final.exists():
        final.unlink(missing_ok=True)
    if best_path.suffix.lower() != ".m4a":
        converted = convert_file_to_m4a(best_path, final)
        if converted:
            return converted
    shutil.copy2(best_path, final)
    return final


def _persist_best_copy(src: Path, hold: Path) -> Path | None:
    """Copy a candidate into a stable hold file (survives temp cleanup between attempts)."""
    if not src.is_file():
        return None
    if src.suffix.lower() != ".m4a":
        converted = convert_file_to_m4a(src, hold)
        if converted:
            return hold
    shutil.copy2(src, hold)
    return hold


def download_search_with_quality(
    *,
    index: int,
    total: int,
    query: str,
    expected_duration: float | None,
    output_dir: Path,
    base_opts: dict,
    audio_format: str,
    quality_max: bool,
    prefer_soundcloud: bool = True,
    expected_title: str | None = None,
    expected_artists: str | None = None,
    target_stem: str | None = None,
    strict_match: bool = False,
    extra_queries: list[str] | None = None,
) -> ResultKind:
    """Search SC/YT in an isolated temp dir; keep one .m4a with clean name."""
    n = 5 if quality_max else 3
    max_attempts = 5 if quality_max else 3
    strict = strict_match or bool(target_stem)

    queries: list[str] = []
    for q in [query, *(extra_queries or [])]:
        q = (q or "").strip()
        if q and q not in queries:
            queries.append(q)
    if not queries:
        log(f"[{index:02d}/{total}] Pas de requête de recherche.")
        return "failed"

    candidates: list[tuple[str, dict[str, Any]]] = []
    seen_ids: set[str] = set()
    for q in queries[:4]:
        if prefer_soundcloud:
            for entry in list_search_candidates("scsearch", q, n, base_opts):
                eid = str(entry.get("id") or entry.get("url") or entry.get("title") or "")
                key = f"sc:{eid}"
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                candidates.append(("soundcloud", entry))
        for entry in list_search_candidates("ytsearch", q, n, base_opts):
            eid = str(entry.get("id") or entry.get("url") or entry.get("title") or "")
            key = f"yt:{eid}"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            candidates.append(("youtube", entry))

    accept_strict = strict
    sc = rank_search_candidates(
        [e for s, e in candidates if s == "soundcloud"],
        query=queries[0],
        expected_duration=expected_duration,
        expected_title=expected_title,
        expected_artists=expected_artists,
        strict=accept_strict,
    )
    yt = rank_search_candidates(
        [e for s, e in candidates if s == "youtube"],
        query=queries[0],
        expected_duration=expected_duration,
        expected_title=expected_title,
        expected_artists=expected_artists,
        strict=accept_strict,
    )
    if not sc and not yt and strict:
        log(f"[{index:02d}/{total}] Filtre strict vide — élargissement…")
        accept_strict = False
        sc = rank_search_candidates(
            [e for s, e in candidates if s == "soundcloud"],
            query=queries[0],
            expected_duration=expected_duration,
            expected_title=expected_title,
            expected_artists=expected_artists,
            strict=False,
        )
        yt = rank_search_candidates(
            [e for s, e in candidates if s == "youtube"],
            query=queries[0],
            expected_duration=expected_duration,
            expected_title=expected_title,
            expected_artists=expected_artists,
            strict=False,
        )
    ordered: list[tuple[str, dict[str, Any]]] = []
    if prefer_soundcloud:
        for i in range(max(len(sc), len(yt))):
            if i < len(sc):
                ordered.append(("soundcloud", sc[i]))
            if i < len(yt):
                ordered.append(("youtube", yt[i]))
    else:
        for i in range(max(len(sc), len(yt))):
            if i < len(yt):
                ordered.append(("youtube", yt[i]))
            if i < len(sc):
                ordered.append(("soundcloud", sc[i]))

    if not ordered:
        log(f"[{index:02d}/{total}] Aucun résultat acceptable pour: {queries[0]}")
        return "failed"

    best_path: Path | None = None
    best_score = -1.0
    best_kind: ResultKind = "failed"
    best_summary = ""
    tried = 0
    hold_file = "_best.m4a"

    temp_dir = track_scratch_dir()
    hold_path = temp_dir / hold_file
    try:
        for source, entry in ordered:
            if tried >= max_attempts or is_cancelled():
                break
            url = candidate_url(entry, youtube=(source == "youtube"))
            if not url:
                continue
            title = str(entry.get("title") or queries[0])
            cand_dur = entry.get("duration")
            try:
                cand_dur_f = float(cand_dur) if cand_dur is not None else None
            except (TypeError, ValueError):
                cand_dur_f = None
            if not candidate_acceptable(
                title,
                expected_title=expected_title,
                expected_artists=expected_artists,
                duration=cand_dur_f,
                expected_duration=expected_duration,
                strict=accept_strict,
            ):
                continue

            tried += 1
            log(f"[{index:02d}/{total}] Essai {source}: {title}")

            for old in temp_dir.iterdir():
                if old.is_file() and old.name != hold_file:
                    old.unlink(missing_ok=True)

            before = snapshot_audio_names(temp_dir)
            output_template = str(temp_dir / "dl.%(ext)s")
            try:
                ok = download_url(
                    url,
                    output_template,
                    base_opts,
                    youtube=(source == "youtube"),
                    soundcloud=(source == "soundcloud"),
                    audio_format=audio_format,
                )
            except CancelledError:
                return "cancelled"
            if not ok:
                continue

            cleanup_sidecar_images(temp_dir)
            path, quality = evaluate_new_file(
                temp_dir, before, expected_duration=expected_duration
            )
            if path is None:
                continue

            if quality and expected_duration and quality.duration_sec:
                post_ok = candidate_acceptable(
                    title,
                    expected_title=expected_title,
                    expected_artists=expected_artists,
                    duration=quality.duration_sec,
                    expected_duration=expected_duration,
                    strict=accept_strict,
                )
                if not post_ok:
                    if path != hold_path and path.exists():
                        path.unlink(missing_ok=True)
                    log(f"[{index:02d}/{total}]   ↳ rejeté après analyse ({quality.summary()})")
                    continue

            if quality:
                score = quality_score(quality, expected_duration=expected_duration)
                summary = quality.summary()
            else:
                # ffprobe often fails on USB / slow disks — keep the file, score from metadata
                size_mb = path.stat().st_size / (1024 * 1024)
                score = 0.55 + 0.35 * duration_match_score(
                    cand_dur_f, expected_duration
                )
                if size_mb < 0.4:
                    score -= 0.2
                summary = f"fichier {size_mb:.1f} Mo (ffprobe indisponible)"
            log(f"[{index:02d}/{total}]   ↳ {summary} (score {score:.2f})")

            if score > best_score:
                saved = _persist_best_copy(path, hold_path)
                if saved:
                    best_path = saved
                    best_score = score
                    best_kind = "soundcloud" if source == "soundcloud" else "youtube"
                    best_summary = summary
            elif path != hold_path and path.exists():
                path.unlink(missing_ok=True)

            if quality and meets_min_quality(
                quality, expected_duration=expected_duration, strict=accept_strict
            ):
                break

        if best_path is None or not best_path.is_file():
            log(f"[{index:02d}/{total}] Échec recherche: {queries[0]}")
            return "failed"

        # Never discard a downloaded file just because ffprobe could not read it
        # (common on USB sticks). Only reject if we measured quality and it is terrible.
        if accept_strict and best_score < 0.35:
            log(f"[{index:02d}/{total}] Aucune version fiable (score {best_score:.2f})")
            return "failed"

        try:
            final_path = _finalize_track_file(best_path, output_dir, target_stem)
        except OSError as exc:
            log(f"[{index:02d}/{total}] Erreur enregistrement: {exc}")
            return "failed"
        warn = " ⚠ qualité moyenne" if best_score < 0.65 else ""
        label = final_path.name if target_stem else best_summary
        log(f"[{index:02d}/{total}] OK ({best_kind}) — {label}{warn}")
        return best_kind
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def verify_or_upgrade_download(
    *,
    index: int,
    total: int,
    path: Path | None,
    quality: Any,
    query: str | None,
    expected_duration: float | None,
    output_dir: Path,
    base_opts: dict,
    audio_format: str,
    quality_max: bool,
    current_kind: ResultKind,
) -> ResultKind:
    """If quality is low, try search upgrades when quality_max is on."""
    if not quality_max or not query:
        if path and quality:
            log(f"[{index:02d}/{total}] OK ({current_kind}) — {quality.summary()}")
        elif path:
            log(f"[{index:02d}/{total}] OK ({current_kind})")
        return current_kind

    if quality and meets_min_quality(
        quality, expected_duration=expected_duration, strict=True
    ):
        log(f"[{index:02d}/{total}] OK ({current_kind}) — {quality.summary()}")
        return current_kind

    if quality:
        log(
            f"[{index:02d}/{total}] Qualité faible ({quality.summary()}) — "
            "recherche d’une meilleure version…"
        )
    else:
        log(f"[{index:02d}/{total}] Qualité inconnue — recherche d’une meilleure version…")

    baseline_score = (
        quality_score(quality, expected_duration=expected_duration) if quality else 0.0
    )
    # Move baseline aside so search can produce a clean "best" file
    baseline_backup: Path | None = None
    if path and path.exists():
        baseline_backup = path.with_name(f".baseline-{path.name}")
        try:
            path.replace(baseline_backup)
        except OSError:
            baseline_backup = None

    upgraded = download_search_with_quality(
        index=index,
        total=total,
        query=query,
        expected_duration=expected_duration,
        output_dir=output_dir,
        base_opts=base_opts,
        audio_format=audio_format,
        quality_max=True,
        prefer_soundcloud=True,
    )

    if upgraded in {"soundcloud", "youtube"}:
        if baseline_backup and baseline_backup.exists():
            probe = resolve_ffprobe_bin(resolve_ffmpeg_dir())
            new_files = [
                p
                for p in output_dir.iterdir()
                if p.is_file()
                and p.suffix.lower()
                in {".m4a", ".mp3", ".opus", ".wav", ".flac", ".aac", ".mp4"}
                and not p.name.startswith(".baseline-")
            ]
            best_new = None
            best_new_score = -1.0
            for cand in new_files:
                q = analyze_audio(cand, ffprobe=probe)
                sc = quality_score(q, expected_duration=expected_duration) if q else 0.0
                if sc > best_new_score:
                    best_new_score = sc
                    best_new = cand
            if best_new is not None and best_new_score >= baseline_score:
                baseline_backup.unlink(missing_ok=True)
                return upgraded
            # Revert to baseline
            for cand in new_files:
                cand.unlink(missing_ok=True)
            try:
                baseline_backup.replace(path)  # type: ignore[arg-type]
            except OSError:
                pass
            log(
                f"[{index:02d}/{total}] Meilleure version introuvable — "
                f"on garde l’originale ({baseline_score:.2f})"
            )
            return current_kind
        return upgraded

    if baseline_backup and baseline_backup.exists() and path:
        try:
            baseline_backup.replace(path)
        except OSError:
            pass
        log(f"[{index:02d}/{total}] On garde la version initiale ({baseline_score:.2f})")
        return current_kind
    return "failed"


def process_track(
    *,
    index: int,
    total: int,
    entry: dict[str, Any],
    output_dir: Path,
    base_opts: dict,
    audio_format: str,
    youtube_fallback: bool,
    title_map: dict[str, str],
    track_meta: dict[str, dict[str, str]],
    platform: Platform,
    quality_max: bool = True,
) -> ResultKind:
    if is_cancelled():
        return "cancelled"

    expected_duration: float | None = None
    raw_dur = entry.get("_expected_duration") or entry.get("duration")
    try:
        if raw_dur is not None:
            expected_duration = float(raw_dur)
    except (TypeError, ValueError):
        expected_duration = None

    # Spotify (and generic search entries): never download from Spotify itself
    if platform == "spotify":
        query = str(entry.get("_spotify_query") or entry.get("title") or "").strip()
        if not query:
            log(f"[{index:02d}/{total}] Pas de titre Spotify, ignoré.")
            return "failed"
        expected_title = str(entry.get("track") or "")
        expected_artists = str(entry.get("artist") or "")
        target_stem = str(entry.get("_target_stem") or "")
        if not target_stem and expected_title:
            target_stem = sanitize_filename_stem(expected_artists, expected_title)
        extra = entry.get("_spotify_queries")
        extra_queries = [str(q) for q in extra] if isinstance(extra, list) else None
        log(f"[{index:02d}/{total}] Spotify → {target_stem or query}")
        return download_search_with_quality(
            index=index,
            total=total,
            query=query,
            expected_duration=expected_duration,
            output_dir=output_dir,
            base_opts=base_opts,
            audio_format=audio_format,
            quality_max=quality_max,
            prefer_soundcloud=True,
            expected_title=expected_title or None,
            expected_artists=expected_artists or None,
            target_stem=target_stem or None,
            strict_match=True,
            extra_queries=extra_queries,
        )

    track_id = str(entry.get("id") or "")
    track_url = entry_media_url(entry, platform)
    if platform == "soundcloud":
        permalink = (track_meta.get(track_id) or {}).get("permalink_url")
        if permalink and (not track_url or "api-v2.soundcloud.com" in str(track_url)):
            track_url = permalink

    if not track_url:
        log(f"[{index:02d}/{total}] Pas d’URL, ignoré.")
        return "failed"

    output_template = str(output_dir / "%(title)s.%(ext)s")
    query = youtube_search_query(entry, title_map, track_meta)

    # Native YouTube link (video or playlist item)
    if platform == "youtube":
        log(f"[{index:02d}/{total}] YouTube: {track_url}")
        before = snapshot_audio_names(output_dir)
        try:
            if download_url(
                track_url,
                output_template,
                base_opts,
                youtube=True,
                audio_format=audio_format,
            ):
                cleanup_sidecar_images(output_dir)
                path, quality = evaluate_new_file(
                    output_dir, before, expected_duration=expected_duration
                )
                if quality_max and query and (
                    quality is None
                    or not meets_min_quality(
                        quality, expected_duration=expected_duration, strict=True
                    )
                ):
                    return verify_or_upgrade_download(
                        index=index,
                        total=total,
                        path=path,
                        quality=quality,
                        query=query,
                        expected_duration=expected_duration,
                        output_dir=output_dir,
                        base_opts=base_opts,
                        audio_format=audio_format,
                        quality_max=quality_max,
                        current_kind="youtube",
                    )
                if quality:
                    log(f"[{index:02d}/{total}] OK (YouTube) — {quality.summary()}")
                else:
                    log(f"[{index:02d}/{total}] OK (YouTube)")
                return "youtube"
        except CancelledError:
            log(f"[{index:02d}/{total}] Arrêté.")
            return "cancelled"
        log(f"[{index:02d}/{total}] Échec YouTube.")
        return "failed"

    # SoundCloud (with optional YouTube / quality search fallback)
    log(f"[{index:02d}/{total}] SoundCloud: {track_url}")
    before = snapshot_audio_names(output_dir)
    sc_ok = False
    path = None
    quality = None
    try:
        if download_url(track_url, output_template, base_opts, soundcloud=True):
            cleanup_sidecar_images(output_dir)
            path, quality = evaluate_new_file(
                output_dir, before, expected_duration=expected_duration
            )
            sc_ok = True
    except CancelledError:
        log(f"[{index:02d}/{total}] Arrêté.")
        return "cancelled"

    if is_cancelled():
        return "cancelled"

    if sc_ok:
        if quality_max and query and (
            quality is None
            or not meets_min_quality(
                quality, expected_duration=expected_duration, strict=False
            )
        ):
            return verify_or_upgrade_download(
                index=index,
                total=total,
                path=path,
                quality=quality,
                query=query,
                expected_duration=expected_duration,
                output_dir=output_dir,
                base_opts=base_opts,
                audio_format=audio_format,
                quality_max=quality_max,
                current_kind="soundcloud",
            )
        if quality:
            log(f"[{index:02d}/{total}] OK (SoundCloud) — {quality.summary()}")
        else:
            log(f"[{index:02d}/{total}] OK (SoundCloud)")
        return "soundcloud"

    if not youtube_fallback:
        log(f"[{index:02d}/{total}] Échec SoundCloud.")
        return "failed"

    if not query:
        log(f"[{index:02d}/{total}] Échec SoundCloud, pas de requête YouTube.")
        return "failed"

    log(f"[{index:02d}/{total}] Secours recherche: {query}")
    return download_search_with_quality(
        index=index,
        total=total,
        query=query,
        expected_duration=expected_duration,
        output_dir=output_dir,
        base_opts=base_opts,
        audio_format=audio_format,
        quality_max=quality_max,
        prefer_soundcloud=False,
    )


def download_playlist(
    url: str,
    output_dir: Path,
    audio_format: str = "best",
    items: str | None = None,
    browser: str | None = None,
    cookies: Path | None = None,
    oauth_token: str | None = None,
    skip_existing: bool = False,
    youtube_fallback: bool = True,
    jobs: int = 4,
    quality_max: bool = True,
) -> int:
    clear_cancel()
    global _worker_cookie_counter
    _worker_cookie_counter = 0
    extend_system_path()
    url = normalize_url(url)
    classified = classify_url(url)
    if classified is None:
        log("URL non supportée. Utilise un lien SoundCloud, YouTube ou Spotify.")
        return 1
    platform, kind = classified

    output_dir.mkdir(parents=True, exist_ok=True)
    jobs = max(1, min(jobs, 12))
    # YouTube / Spotify searches rate-limit aggressive parallel downloads.
    if platform == "youtube":
        jobs = min(jobs, 4)
    elif platform == "spotify":
        jobs = 1  # one track at a time — isolated temp dirs + clean filenames
    elif platform == "soundcloud":
        # SoundCloud rate-limits bursts — keep parallelism low.
        jobs = min(jobs, 2 if (browser or cookies or oauth_token) else 3)

    cookie_file = cookies
    temp_cookie_path: Path | None = None
    browser_for_auth: str | None = browser
    # Browser cookies help SoundCloud quality AND YouTube anti-bot.
    if browser_for_auth and not cookies:
        if browser_for_auth == "safari" and not safari_cookies_readable():
            explain_cookie_failure("safari")
            browser_for_auth = None
        else:
            temp_cookie_path = Path(tempfile.mkdtemp(prefix="mtd-cookies-")) / "cookies.txt"
            log(f"Export des cookies {browser_for_auth}…")
            try:
                cookie_file = export_browser_cookies(browser_for_auth, temp_cookie_path)
                browser_for_auth = None  # parallel workers use the exported file only
            except Exception as exc:  # noqa: BLE001 - continue without login
                explain_cookie_failure(browser_for_auth, exc)
                cookie_file = None
                browser_for_auth = None
                if temp_cookie_path.exists():
                    try:
                        temp_cookie_path.unlink(missing_ok=True)
                        temp_cookie_path.parent.rmdir()
                    except OSError:
                        pass
                    temp_cookie_path = None

    if platform in {"youtube", "spotify"} and not cookie_file and not browser_for_auth:
        log(
            "YouTube exige souvent une connexion navigateur. "
            "Choisis Chrome (connecté à YouTube) pour éviter le blocage « not a bot »."
        )
    if platform in {"youtube", "spotify"} and not resolve_deno() and not shutil.which("node"):
        log(
            "Attention: Deno manquant (nécessaire pour YouTube). "
            "Installe avec: brew install deno"
        )

    if is_cancelled():
        log("Arrêté avant le début.")
        return 2

    entries: list[dict[str, Any]] = []
    track_meta: dict[str, dict[str, str]] = {}
    title_map: dict[str, str] = {}
    spotify_playlist_name: str | None = None

    if platform == "spotify":
        log("Spotify: lecture des métadonnées (pas de téléchargement depuis Spotify)…")
        catalog = fetch_spotify_catalog(url)
        if not catalog or not catalog.tracks:
            log(
                "Impossible de lire la playlist Spotify. "
                "Vérifie que le lien est public, puis réessaie."
            )
            return 1
        spotify_playlist_name = catalog.name
        entries = spotify_tracks_as_entries(catalog)
        log(f"Playlist Spotify « {catalog.name} » — {len(entries)} piste(s).")
        if len(entries) >= 100:
            log(
                "Note: l’embed Spotify peut tronquer les très longues playlists "
                "(~100 premiers titres)."
            )
    else:
        extract_opts: dict = {
            "quiet": True,
            "extract_flat": "in_playlist",
            "ignoreerrors": True,
        }
        apply_ffmpeg(extract_opts)
        if platform == "youtube":
            apply_js_runtime(extract_opts)
        # For a single YouTube watch URL, never expand an attached playlist.
        if platform == "youtube" and kind == "track":
            extract_opts["noplaylist"] = True

        if cookie_file:
            apply_auth(extract_opts, cookie_file, oauth_token if platform == "soundcloud" else None)
        elif browser_for_auth:
            extract_opts["cookiesfrombrowser"] = (browser_for_auth,)
            apply_auth(
                extract_opts,
                None,
                oauth_token if platform == "soundcloud" else None,
            )
        else:
            apply_auth(extract_opts, None, oauth_token if platform == "soundcloud" else None)

        try:
            with yt_dlp.YoutubeDL(extract_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001
            err_text = str(exc).lower()
            # Safari / TCC sometimes fails only at extract time.
            if browser_for_auth and (
                "cookie" in err_text or "not permitted" in err_text
            ):
                explain_cookie_failure(browser_for_auth, exc)
                browser_for_auth = None
                extract_opts.pop("cookiesfrombrowser", None)
                extract_opts.pop("cookiefile", None)
                apply_auth(extract_opts, None, oauth_token)
                with yt_dlp.YoutubeDL(extract_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            elif cookie_file and (
                "expecting value" in err_text
                or "403" in err_text
                or "cookie" in err_text
                or "netscape" in err_text
            ):
                log("Cookies navigateur invalides — lecture de la playlist sans cookies.")
                cookie_file = None
                browser_for_auth = None
                extract_opts.pop("cookiefile", None)
                extract_opts.pop("cookiesfrombrowser", None)
                apply_auth(extract_opts, None, oauth_token if platform == "soundcloud" else None)
                with yt_dlp.YoutubeDL(extract_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            else:
                log(f"Erreur : {exc}")
                return 1

        if not info:
            log("Impossible de lire le lien.")
            return 1

        entries = entries_from_info(info)
        if not entries:
            log("Aucun média trouvé.")
            return 1

        # Prefer stable permalink for single SoundCloud tracks (flat extract can be a temp stream).
        if platform == "soundcloud" and kind == "track":
            entries[0]["url"] = url
            entries[0]["webpage_url"] = url

        if platform == "soundcloud" and kind == "playlist":
            track_meta = fetch_playlist_track_meta(url)
            title_map = {
                track_id: meta["title"]
                for track_id, meta in track_meta.items()
                if meta.get("title")
            }
            if track_meta:
                log(f"Métadonnées SoundCloud: {len(track_meta)} pistes.")
            elif youtube_fallback:
                log(
                    "Attention: métadonnées SoundCloud indisponibles; "
                    "le secours YouTube peut manquer des titres."
                )
        elif platform == "soundcloud" and kind == "track":
            # Enrich single-track entry when flat extract is sparse.
            track_id = str(entries[0].get("id") or "")
            if track_id and not entries[0].get("title"):
                resolved_title = fetch_playlist_display_name(url)
                if resolved_title:
                    entries[0]["title"] = resolved_title
                    title_map[track_id] = resolved_title

    selected = parse_item_range(items, len(entries))
    use_fallback = youtube_fallback and platform in {"soundcloud", "spotify"}

    if (use_fallback or quality_max or platform == "spotify") and not has_real_ffprobe():
        log(
            "Note: ffprobe absent — contrôle qualité limité "
            "(installe: brew install ffmpeg)."
        )

    base_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "download_archive": None,
        "nooverwrites": skip_existing,
        "continuedl": skip_existing,
    }
    sc_oauth = oauth_token if platform == "soundcloud" else None
    if cookie_file:
        apply_auth(base_opts, cookie_file, sc_oauth)
    elif browser_for_auth:
        base_opts["cookiesfrombrowser"] = (browser_for_auth,)
        apply_auth(base_opts, None, sc_oauth)
    else:
        apply_auth(base_opts, None, sc_oauth)

    if platform in {"soundcloud", "spotify"}:
        apply_format(base_opts, audio_format)

    kind_fr = "playlist" if kind == "playlist" else "piste"
    if platform == "soundcloud":
        source_fr = "SoundCloud"
    elif platform == "youtube":
        source_fr = "YouTube"
    else:
        source_fr = "Spotify→SC/YT"

    if platform == "soundcloud":
        if cookie_file or oauth_token:
            auth_note = " (connecté — jusqu’à 256 kbps AAC avec Go+)"
        elif browser:
            auth_note = (
                " (cookies navigateur indisponibles — ~160 kbps public; secours YouTube actif)"
            )
        else:
            auth_note = " (sans compte — ~160 kbps public; secours YouTube actif)"
        fallback_note = " + secours YouTube" if use_fallback else ""
        quality_line = f"Qualité: {audio_format}{auth_note}{fallback_note}"
    elif platform == "spotify":
        quality_line = (
            "Qualité: recherche SoundCloud/YouTube"
            + (" + contrôle bitrate/durée" if quality_max else "")
            + " (jamais depuis Spotify)"
        )
    else:
        if cookie_file or browser_for_auth:
            quality_line = "Qualité: audio YouTube (cookies navigateur)"
        else:
            quality_line = "Qualité: audio YouTube (sans cookies — risque de blocage bot)"
        if quality_max:
            quality_line += " + contrôle qualité"

    log(f"Téléchargement ({source_fr} · {kind_fr}) → {output_dir.resolve()}")
    if spotify_playlist_name:
        log(f"Titre Spotify: {spotify_playlist_name}")
    log(quality_line)
    log(f"Téléchargements parallèles: {jobs}")
    log(f"URL: {url}\n")

    work_items: list[tuple[int, dict[str, Any]]] = []
    skipped = 0
    for index, entry in enumerate(entries, start=1):
        if selected is not None and index not in selected:
            continue
        if skip_existing and is_track_downloaded(output_dir, index, entry, title_map):
            log(f"[{index:02d}/{len(entries)}] Déjà téléchargé, ignoré.")
            skipped += 1
            continue
        work_items.append((index, entry))

    if platform == "soundcloud" and len(work_items) > 20:
        log("SoundCloud: ralentissement auto si trop de requêtes (429).")
    if platform == "spotify":
        log("Spotify: chaque piste est cherchée sur SoundCloud puis YouTube.")

    soundcloud_ok = 0
    youtube_ok = 0
    failed = 0
    cancelled = 0
    failed_tracks: list[str] = []
    index_to_label = {
        index: str(
            entry.get("_target_stem")
            or entry.get("title")
            or entry.get("_spotify_query")
            or f"piste {index}"
        )
        for index, entry in work_items
    }

    try:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(
                    process_track,
                    index=index,
                    total=len(entries),
                    entry=entry,
                    output_dir=output_dir,
                    base_opts=base_opts,
                    audio_format=audio_format,
                    youtube_fallback=use_fallback,
                    title_map=title_map,
                    track_meta=track_meta,
                    platform=platform,
                    quality_max=quality_max,
                ): index
                for index, entry in work_items
            }
            for future in as_completed(futures):
                track_index = futures[future]
                if is_cancelled():
                    for pending in futures:
                        pending.cancel()
                try:
                    result = future.result()
                except (CancelledError, FutureCancelledError):
                    cancelled += 1
                    continue
                except Exception as exc:  # noqa: BLE001 - one bad track must not crash the batch
                    failed += 1
                    label = index_to_label.get(track_index, f"piste {track_index}")
                    failed_tracks.append(f"[{track_index:02d}] {label}")
                    log(f"Erreur piste: {exc}")
                    continue
                if result == "soundcloud":
                    soundcloud_ok += 1
                elif result == "youtube":
                    youtube_ok += 1
                elif result == "cancelled":
                    cancelled += 1
                elif result == "failed":
                    failed += 1
                    label = index_to_label.get(track_index, f"piste {track_index}")
                    failed_tracks.append(f"[{track_index:02d}] {label}")
    finally:
        if temp_cookie_path and temp_cookie_path.exists():
            workers_dir = temp_cookie_path.parent / "workers"
            if workers_dir.is_dir():
                shutil.rmtree(workers_dir, ignore_errors=True)
            try:
                temp_cookie_path.chmod(0o600)
                temp_cookie_path.unlink()
                temp_cookie_path.parent.rmdir()
            except OSError:
                pass

    if cancelled or is_cancelled():
        leftovers = [
            p
            for p in output_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".mp4", ".webm"}
        ]
        if leftovers:
            log(f"\nConversion des .mp4/.webm restants ({len(leftovers)})…")
            convert_youtube_leftovers(output_dir)
        if failed_tracks:
            log("\nPistes non téléchargées :")
            for line in failed_tracks:
                log(f"  ✗ {line}")
        log(
            f"\nArrêté. SoundCloud: {soundcloud_ok}, YouTube: {youtube_ok}, "
            f"échecs: {failed}, ignorés: {skipped}, annulés: {cancelled}."
        )
        return 2

    leftovers = [
        p
        for p in output_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".mp4", ".webm"}
    ]
    if leftovers:
        log(f"\nConversion de {len(leftovers)} fichier(s) .mp4/.webm → .m4a…")
        conv_ok, conv_fail = convert_youtube_leftovers(output_dir)
        if conv_ok:
            youtube_ok += conv_ok
            failed = max(0, failed - conv_ok)
        failed += conv_fail

    if platform == "spotify":
        keep_stems = {
            str(entry.get("_target_stem"))
            for _, entry in work_items
            if entry.get("_target_stem")
        }
        purge_orphan_media(output_dir, keep_stems=keep_stems)
        m4a_count = len(list(output_dir.glob("*.m4a")))
        log(f"Dossier final: {m4a_count} fichier(s) .m4a (playlist: {len(entries)} pistes).")

    if failed_tracks:
        log("\nPistes non téléchargées :")
        for line in sorted(failed_tracks):
            log(f"  ✗ {line}")
        log("Astuce: décoche « Ignorer déjà téléchargés » et relance pour retenter seulement les manquants.")

    log(
        f"\nTerminé. SoundCloud: {soundcloud_ok}, YouTube: {youtube_ok}, "
        f"échecs: {failed}, ignorés: {skipped}."
    )
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return download_playlist(
        url=args.url,
        output_dir=args.output,
        audio_format=args.format,
        items=args.items,
        browser=args.browser,
        cookies=args.cookies,
        oauth_token=args.oauth_token,
        skip_existing=args.skip_existing,
        youtube_fallback=args.youtube_fallback,
        jobs=args.jobs,
        quality_max=args.quality_max,
    )


if __name__ == "__main__":
    sys.exit(main())
