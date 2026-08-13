"""App identity and version (keep in sync with VERSION file)."""

from __future__ import annotations

APP_NAME = "Music Track Downloader"
APP_NAME_SHORT = "MusicTrackDownloader"
APP_VERSION = "1.1.8"

GITHUB_OWNER = "emmanueldevins"
GITHUB_REPO = "Music-Track-Downloader"
GITHUB_REPO_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
RELEASES_URL = f"{GITHUB_REPO_URL}/releases/latest"
# Raw VERSION on main — updated when you bump the version and push.
REMOTE_VERSION_URL = (
    f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/VERSION"
)

DOWNLOAD_MAC_URL = (
    f"{GITHUB_REPO_URL}/releases/latest/download/{APP_NAME_SHORT}-mac-arm64.zip"
)
DOWNLOAD_WIN_URL = (
    f"{GITHUB_REPO_URL}/releases/latest/download/{APP_NAME_SHORT}-win64.zip"
)
