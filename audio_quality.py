"""Analyze downloaded audio quality with ffprobe and score search candidates."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Soft / hard thresholds for DJ / USB use
MIN_BITRATE_OK = 160.0  # kbps — acceptable
MIN_BITRATE_GOOD = 192.0  # kbps — preferred
MIN_SAMPLE_RATE = 44100
DURATION_TOLERANCE = 0.08  # ±8% vs expected (strict Spotify matching)

_BAD_TITLE_RE = re.compile(
    r"\b("
    r"live|cover|karaoke|slowed|sped\s*up|8d|nightcore|tiktok|"
    r"official\s*video|lyric\s*video|audio\s*spectrum|reaction|"
    r"mashup|remix\s*contest|bootleg|visualizer|premiere|rework|"
    r"weekend\s*mix|dj\s*set|full\s*set|megamix|podcast"
    r")\b",
    re.IGNORECASE,
)

_REJECT_EXTRA_RE = re.compile(
    r"(#|premiere|visualizer|visual\b|rework|mashup|footage|"
    r"official\s*video|lyric\s*video|weekend\s*mix|"
    r"\b(?:dj|afro|house|techno)\s*mix\b.*\d{1,3}\b|\bmix\s*[-–]\s*\d{2,})",
    re.IGNORECASE,
)

_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "ft",
        "feat",
        "featuring",
        "vs",
        "and",
        "de",
        "original",
        "mix",
        "extended",
        "remix",
        "radio",
        "edit",
        "version",
        "official",
    }
)


@dataclass
class AudioQuality:
    path: Path
    bitrate_kbps: float | None = None
    sample_rate: int | None = None
    duration_sec: float | None = None
    codec: str | None = None
    channels: int | None = None

    def summary(self) -> str:
        bits = f"{self.bitrate_kbps:.0f} kbps" if self.bitrate_kbps else "? kbps"
        hz = f"{self.sample_rate // 1000} kHz" if self.sample_rate else "? kHz"
        dur = ""
        if self.duration_sec:
            m, s = divmod(int(self.duration_sec), 60)
            dur = f" · {m}:{s:02d}"
        codec = self.codec or "?"
        return f"{bits} · {hz} · {codec}{dur}"


def sanitize_filename_stem(artist: str, title: str) -> str:
    """Safe filename stem: 'Artist - Title'."""
    name = f"{artist.strip()} - {title.strip()}" if artist.strip() else title.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f#]', " ", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name[:120] or "track"


def significant_words(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return [w for w in words if len(w) > 2 and w not in _STOP_WORDS]


def primary_artist_tokens(artist: str) -> list[str]:
    primary = artist.split(",")[0].strip()
    primary = re.sub(r"\([^)]*\)", "", primary).strip()
    return [t for t in re.findall(r"[a-z0-9']+", primary.lower()) if len(t) >= 3]


def normalize_title_key(text: str) -> str:
    """Normalize titles so 'Foo - Radio Mix' ≈ 'Foo (Radio Mix)'."""
    t = text.lower().replace("\xa0", " ")
    t = t.replace("’", "'").replace("‘", "'")
    t = re.sub(r"[\[\(\{]", " ", t)
    t = re.sub(r"[\]\)\}]", " ", t)
    t = re.sub(r"[-–—_/|]+", " ", t)
    t = re.sub(r"[^\w\s']+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def all_artist_tokens(artists: str) -> list[str]:
    tokens: list[str] = []
    for part in re.split(r"[,&/]| feat\.? | ft\.? | featuring ", artists, flags=re.I):
        tokens.extend(primary_artist_tokens(part))
    # unique preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def candidate_acceptable(
    title: str,
    *,
    expected_title: str | None = None,
    expected_artists: str | None = None,
    duration: float | None = None,
    expected_duration: float | None = None,
    strict: bool = False,
) -> bool:
    """Reject obvious wrong matches before downloading."""
    if not title or not title.strip():
        return False
    lower = title.lower()
    norm_expected = (expected_title or "").lower().strip()
    key_title = normalize_title_key(title)
    key_expected = normalize_title_key(expected_title or "")

    if strict:
        if _BAD_TITLE_RE.search(title) or _REJECT_EXTRA_RE.search(title):
            return False
        if "#" in title:
            return False
        # Reject radio edit/mix only when Spotify title is NOT a radio version
        wants_radio = "radio" in key_expected
        if ("radio edit" in lower or "radio mix" in lower) and not wants_radio:
            dur_ok = (
                expected_duration
                and duration
                and abs(duration - expected_duration) / expected_duration
                <= DURATION_TOLERANCE
            )
            if not dur_ok:
                return False
        if " x " in lower and " x " not in (expected_title or "").lower():
            return False

    # Title match strength (hyphen / parentheses / punctuation tolerant)
    title_hits = 0.0
    exp_words = significant_words(expected_title or "") if expected_title else []
    if key_expected and key_expected in key_title:
        title_hits = 1.0
    elif exp_words:
        hits = sum(1 for w in exp_words if w in key_title)
        title_hits = hits / len(exp_words)

    if strict and expected_artists:
        tokens = all_artist_tokens(expected_artists)
        artist_ok = bool(tokens and any(tok in key_title for tok in tokens))
        # Strong title match can stand alone (uploads often omit artist)
        if tokens and not artist_ok and title_hits < 0.7:
            return False

    if strict and expected_title:
        if title_hits >= 0.55:
            pass
        elif "extended" in key_expected and "original mix" in key_title:
            pass
        elif "radio" in key_expected and "radio" in key_title:
            # core song name without "radio mix/edit"
            core = significant_words(
                re.sub(r"\bradio\s*(mix|edit)\b", "", expected_title or "", flags=re.I)
            )
            if core and all(w in key_title for w in core):
                pass
            else:
                return False
        else:
            return False

    if expected_duration and duration and duration > 0:
        diff = abs(duration - expected_duration) / expected_duration
        limit = DURATION_TOLERANCE if strict else 0.15
        if diff > limit:
            return False
        if duration > expected_duration * 1.15 and duration > 480:
            return False
        if duration > expected_duration + 90 and duration > 360:
            return False

    return True


def resolve_ffprobe_bin(ffmpeg_dir: str | None = None) -> str | None:
    names = ("ffprobe.exe", "ffprobe") if sys.platform == "win32" else ("ffprobe",)
    if ffmpeg_dir:
        for name in names:
            candidate = Path(ffmpeg_dir) / name
            if candidate.is_file():
                return str(candidate)
    return shutil.which("ffprobe")


def analyze_audio(path: Path, *, ffprobe: str | None = None) -> AudioQuality | None:
    """Return bitrate / sample rate / duration for an audio file."""
    if not path.is_file() or path.stat().st_size < 1024:
        return None
    probe = ffprobe or resolve_ffprobe_bin()
    if not probe:
        return None
    cmd = [
        probe,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}

    bitrate = None
    sample_rate = None
    channels = None
    codec = None
    duration = None

    if audio:
        codec = str(audio.get("codec_name") or "") or None
        try:
            if audio.get("bit_rate"):
                bitrate = float(audio["bit_rate"]) / 1000.0
        except (TypeError, ValueError):
            pass
        try:
            if audio.get("sample_rate"):
                sample_rate = int(audio["sample_rate"])
        except (TypeError, ValueError):
            pass
        try:
            if audio.get("channels"):
                channels = int(audio["channels"])
        except (TypeError, ValueError):
            pass
        try:
            if audio.get("duration"):
                duration = float(audio["duration"])
        except (TypeError, ValueError):
            pass

    if bitrate is None:
        try:
            if fmt.get("bit_rate"):
                bitrate = float(fmt["bit_rate"]) / 1000.0
        except (TypeError, ValueError):
            pass
    if duration is None:
        try:
            if fmt.get("duration"):
                duration = float(fmt["duration"])
        except (TypeError, ValueError):
            pass

    return AudioQuality(
        path=path,
        bitrate_kbps=bitrate,
        sample_rate=sample_rate,
        duration_sec=duration,
        codec=codec,
        channels=channels,
    )


def duration_match_score(actual: float | None, expected: float | None) -> float:
    if not expected or expected <= 0 or not actual or actual <= 0:
        return 0.55
    diff = abs(actual - expected) / expected
    if diff <= 0.05:
        return 1.0
    if diff <= DURATION_TOLERANCE:
        return 0.85
    if diff <= 0.15:
        return 0.45
    return 0.05


def title_penalty(title: str) -> float:
    if not title:
        return 0.0
    penalty = 0.0
    if _BAD_TITLE_RE.search(title):
        penalty -= 0.5
    if _REJECT_EXTRA_RE.search(title):
        penalty -= 0.35
    if "#" in title:
        penalty -= 0.4
    return penalty


def quality_score(
    q: AudioQuality,
    *,
    expected_duration: float | None = None,
) -> float:
    score = 0.0
    if q.bitrate_kbps:
        if q.bitrate_kbps >= 256:
            score += 0.45
        elif q.bitrate_kbps >= MIN_BITRATE_GOOD:
            score += 0.35
        elif q.bitrate_kbps >= MIN_BITRATE_OK:
            score += 0.20
        else:
            score += 0.05
    if q.sample_rate and q.sample_rate >= MIN_SAMPLE_RATE:
        score += 0.15
    score += 0.40 * duration_match_score(q.duration_sec, expected_duration)
    return score


def meets_min_quality(
    q: AudioQuality,
    *,
    expected_duration: float | None = None,
    strict: bool = False,
) -> bool:
    min_br = MIN_BITRATE_GOOD if strict else MIN_BITRATE_OK
    if q.bitrate_kbps is not None and q.bitrate_kbps < min_br:
        return False
    if q.sample_rate is not None and q.sample_rate < MIN_SAMPLE_RATE:
        return False
    if expected_duration and q.duration_sec:
        diff = abs(q.duration_sec - expected_duration) / expected_duration
        if diff > (DURATION_TOLERANCE if strict else 0.15):
            return False
    if q.bitrate_kbps is None and strict:
        return False
    return True


def rank_search_candidates(
    candidates: list[dict[str, Any]],
    *,
    query: str,
    expected_duration: float | None,
    expected_title: str | None = None,
    expected_artists: str | None = None,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Sort flat search results: filter bad matches, then score."""
    scored: list[tuple[float, dict[str, Any]]] = []
    q_lower = query.lower()
    for cand in candidates:
        if not cand:
            continue
        title = str(cand.get("title") or cand.get("fulltitle") or "")
        dur = cand.get("duration")
        try:
            dur_f = float(dur) if dur is not None else None
        except (TypeError, ValueError):
            dur_f = None
        if not candidate_acceptable(
            title,
            expected_title=expected_title,
            expected_artists=expected_artists,
            duration=dur_f,
            expected_duration=expected_duration,
            strict=strict,
        ):
            continue
        score = duration_match_score(dur_f, expected_duration)
        score += title_penalty(title)
        tokens = [t for t in re.split(r"\s+", q_lower) if len(t) > 2]
        if tokens:
            hits = sum(1 for t in tokens if t in title.lower())
            score += 0.15 * (hits / len(tokens))
        if expected_title and expected_title.lower() in title.lower():
            score += 0.25
        if expected_artists:
            for tok in primary_artist_tokens(expected_artists):
                if tok in title.lower():
                    score += 0.1
                    break
        scored.append((score, cand))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _, c in scored]


def newest_audio_file(output_dir: Path, before: set[str]) -> Path | None:
    """Return the newest audio file whose name was not in `before`."""
    audio_exts = {".m4a", ".mp3", ".opus", ".wav", ".flac", ".aac", ".mp4", ".webm"}
    after = [
        p
        for p in output_dir.iterdir()
        if p.is_file() and p.suffix.lower() in audio_exts and p.name not in before
    ]
    if not after:
        return None
    after.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return after[0]
