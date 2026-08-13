#!/usr/bin/env python3
"""Desktop UI for Music Track Downloader."""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QPointF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QLinearGradient,
    QPainter,
    QRadialGradient,
    QTextCursor,
)
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from download_playlist import (
    download_playlist,
    fetch_playlist_display_name,
    is_supported_media_url,
    request_cancel,
    resolve_ffmpeg,
    sanitize_folder_name,
    set_log_callback,
)
from version import (
    APP_NAME,
    APP_VERSION,
    DOWNLOAD_MAC_URL,
    DOWNLOAD_WIN_URL,
    GITHUB_OWNER,
    GITHUB_REPO,
    RELEASES_URL,
    REMOTE_VERSION_URL,
)

ROOT = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    DEFAULT_OUTPUT = Path.home() / "Downloads" / "MusicTrackDownloader"
else:
    DEFAULT_OUTPUT = ROOT / "downloads"


def optimal_jobs() -> int:
    """Parallel downloads: a bit above CPU count, capped for stability."""
    cpus = os.cpu_count() or 4
    return max(2, min(cpus * 2, 12))


def parse_version(text: str) -> tuple[int, ...]:
    cleaned = text.strip().lstrip("vV")
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def version_is_newer(remote: str, local: str) -> bool:
    return parse_version(remote) > parse_version(local)


def _ssl_context():
    """CA bundle that works inside a frozen PyInstaller .app (macOS often lacks system certs)."""
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def _parse_version_body(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    first = text.splitlines()[0].strip().lstrip("vV")
    if first and parse_version(first) != (0,):
        return first
    return None


def fetch_remote_version(timeout: float = 8.0) -> str | None:
    """Read VERSION from GitHub (urllib + certifi, then curl fallback)."""
    urls = (
        REMOTE_VERSION_URL,
        f"https://cdn.jsdelivr.net/gh/{GITHUB_OWNER}/{GITHUB_REPO}@main/VERSION",
    )

    # 1) urllib with certifi (fixes most frozen-app SSL failures on Mac)
    for url in urls:
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": f"{APP_NAME}/{APP_VERSION}",
                    "Accept": "text/plain",
                    "Cache-Control": "no-cache",
                },
            )
            with urllib.request.urlopen(
                request, timeout=timeout, context=_ssl_context()
            ) as response:
                parsed = _parse_version_body(
                    response.read().decode("utf-8", "ignore")
                )
                if parsed:
                    return parsed
        except (OSError, urllib.error.URLError, ValueError):
            continue

    # 2) system curl (macOS always has it; uses Keychain trust store)
    for url in urls:
        try:
            result = subprocess.run(
                ["curl", "-fsSL", "--max-time", str(int(timeout)), url],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout + 2,
            )
            if result.returncode == 0:
                parsed = _parse_version_body(result.stdout)
                if parsed:
                    return parsed
        except (OSError, subprocess.TimeoutExpired):
            continue

    return None


def open_path(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    elif sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


STYLESHEET = """
QWidget#root {
    color: #f4f0ea;
    font-family: "Avenir Next", "SF Pro Display", "Helvetica Neue", sans-serif;
}

QLabel#brand {
    color: #ff5a1f;
    font-size: 28px;
    font-weight: 800;
    letter-spacing: -0.8px;
}

QLabel#versionLabel {
    color: rgba(244, 240, 234, 0.40);
    font-size: 12px;
    font-weight: 600;
}
QLabel#versionLabel:hover {
    color: rgba(255, 138, 76, 0.95);
}

QFrame#updateBanner {
    background: rgba(255, 90, 31, 0.18);
    border: 1px solid rgba(255, 90, 31, 0.45);
    border-radius: 14px;
}
QLabel#updateBannerText {
    color: #ffd0bc;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#updateBannerBtn {
    background: #ff5a1f;
    color: #1a100c;
    border: none;
    border-radius: 12px;
    padding: 8px 14px;
    font-size: 12px;
    font-weight: 700;
}
QPushButton#updateBannerBtn:hover { background: #ff7a45; }

QLabel#tagline {
    color: rgba(244, 240, 234, 0.62);
    font-size: 15px;
    font-weight: 500;
}


QLabel#hint {
    color: rgba(244, 240, 234, 0.38);
    font-size: 12px;
    font-weight: 500;
}

QLabel#footerLegal {
    color: rgba(244, 240, 234, 0.32);
    font-size: 11px;
    font-weight: 500;
}

QLabel#pathLabel {
    color: rgba(244, 240, 234, 0.55);
    font-size: 12px;
}

QLabel#statusReady { color: rgba(244, 240, 234, 0.45); font-size: 12px; font-weight: 600; }
QLabel#statusBusy { color: #ff8a4c; font-size: 12px; font-weight: 700; }
QLabel#statusOk { color: #6ee7a8; font-size: 12px; font-weight: 700; }
QLabel#statusWarn { color: #f5c26b; font-size: 12px; font-weight: 700; }

QLineEdit#urlInput, QLineEdit#nameInput {
    background: rgba(255, 255, 255, 0.06);
    color: #f4f0ea;
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 18px;
    padding: 18px 20px;
    font-size: 15px;
    font-weight: 500;
    selection-background-color: #ff5a1f;
}
QLineEdit#urlInput:focus, QLineEdit#nameInput:focus {
    border: 1px solid #ff5a1f;
    background: rgba(255, 90, 31, 0.08);
}
QLineEdit#nameInput {
    border-radius: 12px;
    padding: 12px 14px;
    font-size: 13px;
}

QFrame#optionsBar {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 14px;
}

QLabel#optLabel {
    color: rgba(244, 240, 234, 0.40);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.8px;
}

QComboBox, QSpinBox {
    background: rgba(255, 255, 255, 0.06);
    color: #f4f0ea;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 12px;
    min-height: 16px;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #1a1614;
    color: #f4f0ea;
    border: 1px solid rgba(255, 255, 255, 0.12);
    selection-background-color: #ff5a1f;
}

QCheckBox {
    color: rgba(244, 240, 234, 0.72);
    font-size: 12px;
    spacing: 6px;
}
QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border-radius: 4px;
    border: 1px solid rgba(255, 255, 255, 0.20);
    background: rgba(255, 255, 255, 0.04);
}
QCheckBox::indicator:checked {
    background: #ff5a1f;
    border-color: #ff5a1f;
}

QPushButton#primary {
    background: #ff5a1f;
    color: #1a0f0a;
    border: none;
    border-radius: 16px;
    padding: 16px 28px;
    font-size: 15px;
    font-weight: 800;
}
QPushButton#primary:hover { background: #ff7340; }
QPushButton#primary:pressed { background: #e64d12; }
QPushButton#primary:disabled {
    background: rgba(255, 255, 255, 0.08);
    color: rgba(244, 240, 234, 0.30);
}

QPushButton#link {
    background: transparent;
    color: #ff8a4c;
    border: none;
    padding: 4px 0;
    font-size: 12px;
    font-weight: 700;
    text-align: left;
}
QPushButton#link:hover { color: #ffb087; }

QPushButton#ghost {
    background: rgba(255, 255, 255, 0.05);
    color: #f4f0ea;
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 14px;
    padding: 14px 18px;
    font-size: 13px;
    font-weight: 650;
}
QPushButton#ghost:hover {
    background: rgba(255, 255, 255, 0.09);
}

QPushButton#stop {
    background: rgba(220, 70, 50, 0.18);
    color: #ff8a7a;
    border: 1px solid rgba(220, 70, 50, 0.35);
    border-radius: 14px;
    padding: 14px 18px;
    font-size: 13px;
    font-weight: 700;
}
QPushButton#stop:hover {
    background: rgba(220, 70, 50, 0.28);
    color: #ffb0a4;
}
QPushButton#stop:disabled {
    background: rgba(255, 255, 255, 0.04);
    color: rgba(244, 240, 234, 0.25);
    border-color: rgba(255, 255, 255, 0.06);
}

QFrame#logPanel {
    background: rgba(0, 0, 0, 0.28);
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 14px;
}

QTextEdit#logView {
    background: transparent;
    color: rgba(244, 240, 234, 0.55);
    border: none;
    padding: 4px 2px;
    font-family: "SF Mono", Menlo, Monaco, monospace;
    font-size: 11px;
}
"""


class Atmosphere(QWidget):
    """Warm charcoal canvas with a soft orange wash — not a flat black panel."""

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        base = QLinearGradient(0, 0, self.width(), self.height())
        base.setColorAt(0.0, QColor("#140e0c"))
        base.setColorAt(0.45, QColor("#1a1210"))
        base.setColorAt(1.0, QColor("#0d0b0a"))
        painter.fillRect(self.rect(), base)

        orb = QRadialGradient(QPointF(self.width() * 0.15, -40), self.width() * 0.55)
        orb.setColorAt(0.0, QColor(255, 90, 31, 55))
        orb.setColorAt(0.45, QColor(255, 90, 31, 18))
        orb.setColorAt(1.0, QColor(255, 90, 31, 0))
        painter.fillRect(self.rect(), orb)

        orb2 = QRadialGradient(QPointF(self.width() * 0.9, self.height() * 0.85), 280)
        orb2.setColorAt(0.0, QColor(120, 60, 30, 35))
        orb2.setColorAt(1.0, QColor(120, 60, 30, 0))
        painter.fillRect(self.rect(), orb2)


class DownloadWorker(QThread):
    line = Signal(str)
    finished_ok = Signal(int)

    def __init__(
        self,
        url: str,
        output: Path,
        browser: str,
        skip_existing: bool,
        quality_max: bool = True,
    ) -> None:
        super().__init__()
        self.url = url
        self.output = output
        self.browser = browser
        self.skip_existing = skip_existing
        self.quality_max = quality_max

    def run(self) -> None:
        set_log_callback(lambda message: self.line.emit(message))
        try:
            code = download_playlist(
                url=self.url,
                output_dir=self.output,
                browser=self.browser or None,
                jobs=optimal_jobs(),
                skip_existing=self.skip_existing,
                youtube_fallback=True,
                quality_max=self.quality_max,
            )
        except Exception as exc:  # noqa: BLE001
            self.line.emit(f"Erreur : {exc}")
            code = 1
        finally:
            set_log_callback(None)
        self.finished_ok.emit(code)


class UpdateCheckWorker(QThread):
    """Fetch remote VERSION without blocking the UI."""

    result = Signal(str)  # remote version string, or ""

    def run(self) -> None:
        remote = fetch_remote_version()
        self.result.emit(remote or "")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  ·  v{APP_VERSION}")
        self.resize(720, 640)
        self.setMinimumSize(580, 540)
        self.worker: DownloadWorker | None = None
        self._update_worker: UpdateCheckWorker | None = None
        self._remote_version: str | None = None
        self._folder = DEFAULT_OUTPUT
        self._last_output: Path | None = None
        self._title_url = ""
        self._url_title_timer = QTimer(self)
        self._url_title_timer.setSingleShot(True)
        self._url_title_timer.setInterval(450)
        self._url_title_timer.timeout.connect(self._refresh_title_from_url)

        root = Atmosphere()
        root.setObjectName("root")
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(36, 36, 36, 28)
        layout.setSpacing(0)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(12)
        brand = QLabel(APP_NAME)
        brand.setObjectName("brand")
        brand_row.addWidget(brand)
        brand_row.addStretch(1)
        self.version_label = QLabel(f"v{APP_VERSION}")
        self.version_label.setObjectName("versionLabel")
        self.version_label.setToolTip("Cliquer pour vérifier les mises à jour")
        self.version_label.setCursor(Qt.PointingHandCursor)
        self.version_label.mousePressEvent = (  # type: ignore[method-assign]
            lambda _event: self._start_update_check(force_dialog=True)
        )
        brand_row.addWidget(self.version_label, alignment=Qt.AlignBottom)
        layout.addLayout(brand_row)
        layout.addSpacing(6)

        tagline = QLabel(
            "SoundCloud, YouTube ou Spotify → fichiers audio, prêts pour la clé USB."
        )
        tagline.setObjectName("tagline")
        tagline.setWordWrap(True)
        layout.addWidget(tagline)
        layout.addSpacing(12)

        self.update_banner = QFrame()
        self.update_banner.setObjectName("updateBanner")
        banner_layout = QHBoxLayout(self.update_banner)
        banner_layout.setContentsMargins(14, 10, 14, 10)
        banner_layout.setSpacing(10)
        self.update_banner_text = QLabel()
        self.update_banner_text.setObjectName("updateBannerText")
        self.update_banner_text.setWordWrap(True)
        banner_layout.addWidget(self.update_banner_text, stretch=1)
        self.update_banner_btn = QPushButton("Télécharger")
        self.update_banner_btn.setObjectName("updateBannerBtn")
        self.update_banner_btn.setCursor(Qt.PointingHandCursor)
        self.update_banner_btn.clicked.connect(self._open_update_download)
        banner_layout.addWidget(self.update_banner_btn)
        self.update_banner.hide()
        layout.addWidget(self.update_banner)
        layout.addSpacing(16)

        self.url_input = QLineEdit()
        self.url_input.setObjectName("urlInput")
        self.url_input.setPlaceholderText(
            "Colle un lien SoundCloud, YouTube ou Spotify (piste ou playlist)…"
        )
        self.url_input.returnPressed.connect(self.start_download)
        self.url_input.editingFinished.connect(self._refresh_title_from_url)
        self.url_input.textChanged.connect(self._on_url_text_changed)
        layout.addWidget(self.url_input)
        layout.addSpacing(10)

        name_row = QHBoxLayout()
        name_row.setSpacing(10)
        self.name_input = QLineEdit()
        self.name_input.setObjectName("nameInput")
        self.name_input.setPlaceholderText("Nom de la playlist (pour le dossier)")
        self.name_input.textChanged.connect(self._refresh_path_label)
        name_row.addWidget(self.name_input, stretch=1)
        self.create_folder = QCheckBox("Créer un dossier")
        self.create_folder.setChecked(True)
        self.create_folder.setToolTip(
            "Si un nom est renseigné, les fichiers vont dans un sous-dossier "
            "portant ce nom."
        )
        self.create_folder.toggled.connect(self._refresh_path_label)
        name_row.addWidget(self.create_folder)
        layout.addLayout(name_row)
        layout.addSpacing(10)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.path_label = QLabel()
        self.path_label.setObjectName("pathLabel")
        self._refresh_path_label()
        change_btn = QPushButton("Changer le dossier")
        change_btn.setObjectName("link")
        change_btn.setCursor(Qt.PointingHandCursor)
        change_btn.clicked.connect(self.pick_folder)
        clear_btn = QPushButton("Effacer")
        clear_btn.setObjectName("link")
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setToolTip("Vider le lien, le titre et l’activité")
        clear_btn.clicked.connect(self.clear_form)
        path_row.addWidget(self.path_label, stretch=1)
        path_row.addWidget(clear_btn)
        path_row.addWidget(change_btn)
        layout.addLayout(path_row)
        layout.addSpacing(22)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.start_btn = QPushButton("Télécharger")
        self.start_btn.setObjectName("primary")
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.setDefault(True)
        self.start_btn.setMinimumHeight(52)
        self.start_btn.clicked.connect(self.start_download)
        self.stop_btn = QPushButton("Arrêter")
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        self.stop_btn.setMinimumHeight(52)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_download)
        self.open_btn = QPushButton("Ouvrir")
        self.open_btn.setObjectName("ghost")
        self.open_btn.setCursor(Qt.PointingHandCursor)
        self.open_btn.setMinimumHeight(52)
        self.open_btn.clicked.connect(self.open_folder)
        actions.addWidget(self.start_btn, stretch=3)
        actions.addWidget(self.stop_btn, stretch=1)
        actions.addWidget(self.open_btn, stretch=1)
        layout.addLayout(actions)
        layout.addSpacing(18)

        options = QFrame()
        options.setObjectName("optionsBar")
        opt_layout = QHBoxLayout(options)
        opt_layout.setContentsMargins(14, 12, 14, 12)
        opt_layout.setSpacing(16)

        browser_box = QVBoxLayout()
        browser_box.setSpacing(4)
        bl = QLabel("NAVIGATEUR")
        bl.setObjectName("optLabel")
        self.browser_combo = QComboBox()
        self.browser_combo.addItem("Chrome", "chrome")
        if sys.platform == "win32":
            self.browser_combo.addItem("Edge", "edge")
        elif sys.platform == "darwin":
            self.browser_combo.addItem("Safari (souvent bloqué)", "safari")
        self.browser_combo.addItem("Firefox", "firefox")
        self.browser_combo.addItem("Aucun", "")
        self.browser_combo.setCurrentIndex(0)  # Chrome — utile pour YouTube anti-bot
        if sys.platform == "win32":
            browser_tip = (
                "Chrome ou Edge recommandé (connecté à YouTube et/ou SoundCloud).\n"
                "YouTube bloque souvent sans cookies (« not a bot »)."
            )
        else:
            browser_tip = (
                "Chrome recommandé (connecté à YouTube et/ou SoundCloud).\n"
                "YouTube bloque souvent sans cookies (« not a bot »).\n"
                "Safari est souvent bloqué par macOS."
            )
        self.browser_combo.setToolTip(browser_tip)
        browser_box.addWidget(bl)
        browser_box.addWidget(self.browser_combo)
        opt_layout.addLayout(browser_box)

        toggles = QVBoxLayout()
        toggles.setSpacing(6)
        tl = QLabel("OPTIONS")
        tl.setObjectName("optLabel")
        toggles.addWidget(tl)
        self.skip_existing = QCheckBox("Ignorer déjà téléchargés")
        self.skip_existing.setChecked(True)
        toggles.addWidget(self.skip_existing)
        self.quality_max = QCheckBox("Qualité max")
        self.quality_max.setChecked(True)
        self.quality_max.setToolTip(
            "Vérifie le bitrate / la durée après téléchargement et cherche "
            "une meilleure version (SoundCloud / YouTube) si besoin. "
            "Plus lent, meilleur rendu USB."
        )
        toggles.addWidget(self.quality_max)
        opt_layout.addLayout(toggles, stretch=1)

        layout.addWidget(options)
        layout.addSpacing(8)

        account_hint = QLabel(
            "YouTube : choisis Chrome (connecté à youtube.com) pour éviter le blocage bot. "
            "SoundCloud : même navigateur si tu as un compte. "
            "Spotify : on lit seulement la liste, puis on cherche ailleurs (jamais depuis Spotify). "
            "Safari souvent bloqué sur Mac."
        )
        account_hint.setObjectName("hint")
        account_hint.setWordWrap(True)
        layout.addWidget(account_hint)
        layout.addSpacing(12)

        self.status = QLabel("Prêt")
        self.status.setObjectName("statusReady")
        layout.addWidget(self.status)
        layout.addSpacing(8)

        self.log_panel = QFrame()
        self.log_panel.setObjectName("logPanel")
        log_layout = QVBoxLayout(self.log_panel)
        log_layout.setContentsMargins(12, 10, 12, 10)
        log_layout.setSpacing(4)
        self.log_hint = QLabel("L’activité s’affiche ici pendant le téléchargement.")
        self.log_hint.setObjectName("hint")
        log_layout.addWidget(self.log_hint)
        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(140)
        self.log_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.log_view.hide()
        log_layout.addWidget(self.log_view)
        layout.addWidget(self.log_panel)
        layout.addStretch(1)

        footer = QLabel(
            "Usage personnel uniquement — pour ta propre musique. "
            "Pas d’usage commercial."
        )
        footer.setObjectName("footerLegal")
        footer.setWordWrap(True)
        footer.setAlignment(Qt.AlignCenter)
        footer.setToolTip(
            "Outil privé / éducatif : télécharge uniquement de la musique "
            "que tu as le droit d’utiliser (ta propre musique, ou contenu autorisé)."
        )
        layout.addSpacing(10)
        layout.addWidget(footer)

        # Check for updates shortly after launch (banner only; no error modal).
        QTimer.singleShot(1200, lambda: self._start_update_check(force_dialog=False))

    def _download_url_for_platform(self) -> str:
        return DOWNLOAD_WIN_URL if sys.platform == "win32" else DOWNLOAD_MAC_URL

    def _open_update_download(self) -> None:
        QDesktopServices.openUrl(QUrl(self._download_url_for_platform()))
        QDesktopServices.openUrl(QUrl(RELEASES_URL))

    def _start_update_check(self, force_dialog: bool = False) -> None:
        if self._update_worker is not None and self._update_worker.isRunning():
            return
        self._pending_force_dialog = force_dialog
        if force_dialog:
            self.set_status("Vérification des mises à jour…", "busy")
        self._update_worker = UpdateCheckWorker()
        self._update_worker.result.connect(self._on_update_check)
        self._update_worker.start()

    def _on_update_check(self, remote: str) -> None:
        self._update_worker = None
        force_dialog = getattr(self, "_pending_force_dialog", False)
        self._pending_force_dialog = False

        if not remote:
            if force_dialog:
                self.set_status("Impossible de vérifier les mises à jour (réseau).", "warn")
                QMessageBox.information(
                    self,
                    "Mise à jour",
                    "Impossible de contacter GitHub pour vérifier la version.\n"
                    "Vérifie ta connexion internet.",
                )
            return

        if not version_is_newer(remote, APP_VERSION):
            self.version_label.setText(f"v{APP_VERSION} · à jour")
            self.update_banner.hide()
            if force_dialog:
                self.set_status("Prêt", "ready")
                QMessageBox.information(
                    self,
                    "À jour",
                    f"Tu as déjà la dernière version (v{APP_VERSION}).",
                )
            return

        self._remote_version = remote
        self.version_label.setText(f"v{APP_VERSION} → v{remote}")
        self.update_banner_text.setText(
            f"Nouvelle version disponible : v{remote}  (tu as v{APP_VERSION})"
        )
        self.update_banner.show()
        self.set_status(f"Mise à jour disponible : v{remote}", "warn")
        if not force_dialog:
            return

        self.raise_()
        self.activateWindow()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Mise à jour disponible")
        box.setTextFormat(Qt.RichText)
        box.setText(
            f"Une nouvelle version est disponible : <b>v{remote}</b><br>"
            f"Tu as actuellement la <b>v{APP_VERSION}</b>."
        )
        box.setInformativeText(
            "Télécharge la dernière version, puis remplace l’ancienne app."
        )
        box.setWindowModality(Qt.ApplicationModal)
        download_btn = box.addButton("Télécharger", QMessageBox.AcceptRole)
        box.addButton("Plus tard", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is download_btn:
            self._open_update_download()

    def _resolve_output_dir(self) -> Path:
        name = self.name_input.text().strip()
        if self.create_folder.isChecked() and name:
            return self._folder / sanitize_folder_name(name)
        return self._folder

    def _refresh_path_label(self) -> None:
        path = str(self._resolve_output_dir())
        home = str(Path.home())
        if path.startswith(home):
            path = "~" + path[len(home) :]
        if len(path) > 58:
            path = "…" + path[-55:]
        self.path_label.setText(f"Enregistrement dans  {path}")

    def _on_url_text_changed(self, _text: str = "") -> None:
        self._url_title_timer.start()

    def _refresh_title_from_url(self) -> None:
        """Fetch title whenever the URL changes (paste a new link → new title)."""
        url = self.url_input.text().strip()
        if not url or not is_supported_media_url(url):
            return
        if url == self._title_url:
            return
        try:
            title = fetch_playlist_display_name(url)
        except Exception:  # noqa: BLE001
            return
        if title:
            self.name_input.setText(title)
            self._title_url = url
            self._refresh_path_label()

    def clear_form(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        self._url_title_timer.stop()
        self.url_input.clear()
        self.name_input.clear()
        self._title_url = ""
        self._last_output = None
        self.log_view.clear()
        self.log_view.hide()
        self.log_hint.show()
        self.log_view.setMaximumHeight(140)
        self.set_status("Prêt", "ready")
        self._refresh_path_label()
        self.url_input.setFocus()

    def set_status(self, text: str, kind: str = "ready") -> None:
        names = {
            "ready": "statusReady",
            "busy": "statusBusy",
            "ok": "statusOk",
            "warn": "statusWarn",
        }
        self.status.setObjectName(names.get(kind, "statusReady"))
        self.status.setText(text)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Choisir le dossier de téléchargement",
            str(self._folder),
        )
        if path:
            self._folder = Path(path)
            self._refresh_path_label()

    def open_folder(self) -> None:
        target = self._last_output or self._resolve_output_dir()
        open_path(target)

    def append_log(self, message: str) -> None:
        if self.log_view.isHidden():
            self.log_hint.hide()
            self.log_view.show()
            self.log_view.setMaximumHeight(200)
        self.log_view.moveCursor(QTextCursor.End)
        self.log_view.insertPlainText(message + "\n")
        self.log_view.moveCursor(QTextCursor.End)

    def start_download(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return

        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(
                self,
                "URL manquante",
                "Colle un lien SoundCloud, YouTube ou Spotify (piste ou playlist).",
            )
            return
        if not is_supported_media_url(url):
            QMessageBox.warning(
                self,
                "URL non supportée",
                "Utilise un lien SoundCloud, YouTube ou Spotify — piste seule ou playlist.",
            )
            return

        if self.create_folder.isChecked() and not self.name_input.text().strip():
            self._refresh_title_from_url()
            # Force fetch even if URL was already titled empty somehow.
            if not self.name_input.text().strip():
                self._title_url = ""
                self._refresh_title_from_url()

        browser = self.browser_combo.currentData() or ""
        output = self._resolve_output_dir()
        self._last_output = output

        self.log_view.clear()
        self.log_hint.hide()
        self.log_view.show()
        self.log_view.setMaximumHeight(200)
        self.set_status("Téléchargement…", "busy")
        self.start_btn.setEnabled(False)
        self.start_btn.setText("Téléchargement…")
        self.stop_btn.setEnabled(True)

        self.worker = DownloadWorker(
            url=url,
            output=output,
            browser=browser,
            skip_existing=self.skip_existing.isChecked(),
            quality_max=self.quality_max.isChecked(),
        )
        self.worker.line.connect(self.append_log)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.start()

    def stop_download(self) -> None:
        if self.worker is None or not self.worker.isRunning():
            return
        request_cancel()
        self.stop_btn.setEnabled(False)
        self.set_status("Arrêt en cours…", "busy")
        self.append_log("Arrêt demandé — fin des pistes en cours…")

    def on_finished(self, code: int) -> None:
        self.start_btn.setEnabled(True)
        self.start_btn.setText("Télécharger")
        self.stop_btn.setEnabled(False)
        if code == 0:
            self.set_status("Terminé — fichiers prêts pour la USB.", "ok")
        elif code == 2:
            self.set_status("Arrêté.", "warn")
        else:
            self.set_status("Terminé avec des erreurs — voir l’activité.", "warn")
        if code != 2:
            self.open_folder()
        self.worker = None


def main() -> int:
    resolve_ffmpeg()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
