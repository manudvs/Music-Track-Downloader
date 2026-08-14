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
        "official",
        "audio",
        "video",
        "lyric",
        "lyrics",
        "clip",
        "officiel",
        "hd",
        "hq",
        "4k",
        "topic",
    }
)

# Allowed in titles without counting as a "different song"
_TITLE_FILLER = _STOP_WORDS | frozenset(
    {
        "original",
        "mix",
        "version",
        "visualizer",
        "visual",
        "hq",
        "free",
        "download",
        "out",
        "now",
        "prod",
        "produced",
    }
)

_ALWAYS_BAD_RE = re.compile(
    r"\b("
    r"karaoke|slowed|sped\s*up|8d|nightcore|tiktok|"
    r"reaction|mashup|remix\s*contest|bootleg|"
    r"weekend\s*mix|dj\s*set|full\s*set|megamix|podcast"
    r")\b",
    re.IGNORECASE,
)

MIN_IDENTITY_KEEP = 0.55


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
    raw = re.findall(r"[a-z0-9']+", text.lower())
    words: list[str] = []
    for token in raw:
        chunks = [part for part in re.split(r"\d+", token) if part]
        if len(chunks) > 1:
            words.extend(chunks)
        else:
            words.append(token)
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


def canonical_expected_title(title: str | None) -> str:
    """Drop Spotify DJ-playlist ' - Mixed' (not a real mix version)."""
    if not title:
        return ""
    return re.sub(r"\s*-\s*mixed\s*$", "", title.strip(), flags=re.I).strip()


def strip_label_brackets(title: str) -> str:
    """Remove [AZZUR] / [Moan] label tags — not part of the song name."""
    return re.sub(r"\s*\[[^]]+\]", " ", title).strip()


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


def candidate_uploader(entry: dict[str, Any] | None) -> str:
    if not entry:
        return ""
    parts: list[str] = []
    for key in ("channel", "uploader", "artist", "creator"):
        val = entry.get(key)
        if val:
            parts.append(str(val))
    return " ".join(parts)


def compact_alpha(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def artist_in_text(artists: str | None, *blobs: str) -> bool:
    if not artists or not artists.strip():
        return True
    hay = normalize_title_key(" ".join(b for b in blobs if b))
    compact_hay = compact_alpha(hay)
    tokens = all_artist_tokens(artists)
    if tokens and any(tok in hay for tok in tokens):
        return True
    primary = artists.split(",")[0].strip()
    compact_artist = compact_alpha(re.sub(r"\([^)]*\)", "", primary))
    if len(compact_artist) >= 5 and compact_artist in compact_hay:
        return True
    return False


def version_flags(text: str) -> set[str]:
    key = normalize_title_key(text)
    flags: set[str] = set()
    if re.search(r"\bradio\s*(edit|mix|version)?\b", key):
        flags.add("radio")
    if re.search(r"\bextended\b", key):
        flags.add("extended")
    if re.search(r"\bremix\b", key):
        flags.add("remix")
    if re.search(r"\bmixed\b", key) and "remix" not in flags:
        flags.add("mixed")
    return flags


def extra_title_tokens(
    title: str,
    expected_title: str | None,
    expected_artists: str | None,
) -> list[str]:
    """Content words in the candidate that are not the expected song/artist."""
    extra: list[str] = []
    expected = canonical_expected_title(expected_title)
    title = strip_label_brackets(title)
    allowed = set(_TITLE_FILLER)
    allowed.update(significant_words(expected))
    allowed.update(all_artist_tokens(expected_artists or ""))
    allowed.update(version_flags(expected))
    compact_artists = compact_alpha(expected_artists or "")
    if "radio" in allowed:
        allowed.update({"radio", "edit"})
    if "extended" in allowed:
        allowed.add("extended")
    allowed.add("mixed")
    for word in significant_words(title):
        if word in allowed:
            continue
        if word in {"original", "mix", "edit", "radio", "extended", "remix", "mixed"}:
            continue
        if compact_artists and word in compact_artists:
            continue
        extra.append(word)
    return extra


def title_hit_ratio(title: str, expected_title: str | None) -> float:
    expected = canonical_expected_title(expected_title)
    if not expected:
        return 1.0
    key_title = normalize_title_key(title)
    key_expected = normalize_title_key(expected)
    if key_expected and key_expected in key_title:
        return 1.0
    exp_words = significant_words(expected)
    if not exp_words:
        return 1.0
    hits = sum(1 for w in exp_words if w in key_title)
    return hits / len(exp_words)


def identity_score(
    title: str,
    *,
    expected_title: str | None = None,
    expected_artists: str | None = None,
    uploader: str | None = None,
) -> float:
    """0–1: is this the same recording, ignoring bitrate."""
    if not title or not title.strip():
        return 0.0
    title_hits = title_hit_ratio(title, expected_title)
    artist_ok = artist_in_text(expected_artists, title, uploader or "")
    extra = extra_title_tokens(title, expected_title, expected_artists)
    exp_flags = version_flags(canonical_expected_title(expected_title))
    got_flags = version_flags(title)
    score = 0.0
    score += 0.45 if artist_ok else 0.0
    score += 0.40 * title_hits
    if exp_flags:
        if exp_flags <= got_flags or (exp_flags & got_flags):
            score += 0.15
        else:
            score -= 0.25
    elif "radio" in got_flags or "extended" in got_flags:
        score -= 0.08
    if extra:
        exp_n = len(significant_words(canonical_expected_title(expected_title)))
        penalty = 0.18 * len(extra) if exp_n <= 2 else 0.08 * len(extra)
        score -= min(0.40, penalty)
    up_key = normalize_title_key(uploader or "")
    if artist_ok and ("topic" in up_key or "vevo" in up_key):
        score += 0.05
    return max(0.0, min(1.0, score))


def candidate_acceptable(
    title: str,
    *,
    expected_title: str | None = None,
    expected_artists: str | None = None,
    duration: float | None = None,
    expected_duration: float | None = None,
    strict: bool = False,
    uploader: str | None = None,
    require_identity: bool | None = None,
    relax_duration: bool = False,
) -> bool:
    """Reject obvious wrong matches before downloading."""
    if not title or not title.strip():
        return False
    lower = title.lower()
    expected = canonical_expected_title(expected_title)
    key_title = normalize_title_key(title)
    key_expected = normalize_title_key(expected)
    identity = strict if require_identity is None else require_identity
    artist_ok = artist_in_text(expected_artists, title, uploader or "")
    title_hits = title_hit_ratio(title, expected_title)
    exp_words = significant_words(expected) if expected else []
    extra = extra_title_tokens(title, expected_title, expected_artists)
    identity_strong = bool(
        artist_ok and title_hits >= 0.8 and (len(exp_words) > 2 or not extra)
    )

    if _ALWAYS_BAD_RE.search(title):
        return False
    if identity:
        if _BAD_TITLE_RE.search(title) and not identity_strong:
            return False
        if "#" in title:
            return False
        if " x " in lower and " x " not in (expected_title or "").lower():
            return False

    if identity and expected_artists and not artist_ok:
        return False

    if identity and expected_title:
        if title_hits < 0.55:
            if "extended" in key_expected and "original mix" in key_title:
                pass
            elif "radio" in key_expected and "radio" in key_title:
                core = significant_words(
                    re.sub(r"\bradio\s*(mix|edit)\b", "", expected_title or "", flags=re.I)
                )
                if not (core and all(w in key_title for w in core)):
                    return False
            else:
                return False
        # Short titles: extra words = different song ("Welcome to the Good Hood")
        if len(exp_words) <= 2 and extra:
            return False
        if len(extra) >= 3:
            return False
        exp_flags = version_flags(expected)
        got_flags = version_flags(title)
        if "radio" in exp_flags and "radio" not in got_flags and not relax_duration:
            return False
        if "extended" in exp_flags and "extended" not in got_flags and "original" not in key_title:
            if not relax_duration:
                return False
        if "radio" not in exp_flags and "radio" in got_flags and not identity_strong:
            return False
        if "extended" not in exp_flags and "extended" in got_flags and not identity_strong:
            return False
        if "remix" in got_flags and "remix" not in exp_flags and not identity_strong:
            return False

    if expected_duration and duration and duration > 0:
        diff = abs(duration - expected_duration) / expected_duration
        abs_diff = abs(duration - expected_duration)
        shorter, longer = min(duration, expected_duration), max(duration, expected_duration)
        if identity_strong:
            # Same artist+title: radio vs extended of the same track
            if 90 <= shorter and longer <= 720 and shorter / longer >= 0.38:
                pass
            elif diff > 0.35 and abs_diff > 45:
                return False
            elif longer > 720 and abs_diff > 90:
                return False
        elif relax_duration:
            if diff > 0.35 and abs_diff > 45:
                return False
            if duration > expected_duration + 90 and duration > 360:
                return False
        else:
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
    abs_diff = abs(actual - expected)
    diff = abs_diff / expected
    if abs_diff <= 5 or diff <= 0.03:
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
    require_identity: bool | None = None,
    relax_duration: bool = False,
) -> list[dict[str, Any]]:
    """Sort flat search results: identity first, then duration."""
    scored: list[tuple[float, float, dict[str, Any]]] = []
    identity = strict if require_identity is None else require_identity
    for cand in candidates:
        if not cand:
            continue
        title = str(cand.get("title") or cand.get("fulltitle") or "")
        uploader = candidate_uploader(cand)
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
            uploader=uploader,
            require_identity=identity,
            relax_duration=relax_duration,
        ):
            continue
        ident = identity_score(
            title,
            expected_title=expected_title,
            expected_artists=expected_artists,
            uploader=uploader,
        )
        if identity and ident < MIN_IDENTITY_KEEP:
            continue
        dur_s = duration_match_score(dur_f, expected_duration)
        scored.append((ident, dur_s + title_penalty(title), cand))
    scored.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    return [c for _, _, c in scored]


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
