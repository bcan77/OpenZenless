"""PySide6 OpenIV-style desktop GUI for OpenZenless."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import config as cfg
from .adb_import import (
    adb_connect_wireless,
    detect_remote_files_dir,
    detect_zzz_package,
    ensure_adb_available,
    find_adb,
    list_adb_devices,
    pull_from_phone,
)
from . import catalog
from . import cdn_downloader
from .cdn_downloader import (
    DOWNLOAD_PROFILES,
    REGIONS,
    VOICE_LANGS,
)
from .containers import detect_magic, extract_bnk_wems, format_hex_dump, human_size, parse_akpk, parse_bnk, scan_blk_json
from .exporters import (
    collect_entry_required_blks,
    collect_map_required_blks,
    export_audio_entries,
    export_audio_pck,
    export_blk_asset_types,
    export_character_animations_only,
    export_character_family,
    export_character_full,
    export_data_pack,
    export_entity_animations_only,
    export_entity_full,
    export_fonts,
    export_full_game,
    export_scene_map,
    export_spine_rig,
    export_texture_section,
    export_ui_sprite_group,
    scan_audio_groups,
)
from .indexer import (
    build_asset_map,
    ensure_prebuilt_index_loaded,
    has_asset_map,
    invalidate_index_cache,
    load_character_index,
    load_character_names,
    load_oz_character_index,
    load_oz_entities_index,
    load_oz_map_index,
    load_oz_media_index,
    load_curated_map_index,
    load_stage_index,
    load_texture_sections,
)
from .tools import (
    ensure_adb,
    ensure_blender,
    ensure_extractor_tools,
    ensure_vgmstream,
    get_environment_status,
)
try:
    from .viewer3d import NativeViewerWidget
    _VIEWER_IMPORT_ERR = ""
except Exception as _viewer_exc:
    NativeViewerWidget = None  # type: ignore
    _VIEWER_IMPORT_ERR = str(_viewer_exc)


def launch_gui(root_path: Path):
    from PySide6.QtCore import Qt, QThread, Signal, QSize
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QKeySequence, QPixmap, QShortcut
    from PySide6.QtWidgets import (
        QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
        QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
        QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
        QProgressBar, QPushButton, QSizePolicy, QSpinBox, QSplitter, QStackedWidget,
        QStatusBar, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
        QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
    )

    def _fit_to_screen(widget, pref_w: int, pref_h: int):
        scr = QGuiApplication.primaryScreen()
        if scr is not None:
            avail = scr.availableGeometry()
            w = min(pref_w, max(480, int(avail.width() * 0.94)))
            h = min(pref_h, max(400, int(avail.height() * 0.92)))
        else:
            w, h = pref_w, pref_h
        widget.resize(w, h)

    _detached_threads: list[QThread] = []

    def _retire_thread(th: QThread | None, wait_ms: int = 5000):
        if th is None or not th.isRunning():
            return
        th.wait(wait_ms)
        if th.isRunning():
            try:
                th.setParent(None)
            except Exception:
                pass
            _detached_threads.append(th)
            th.finished.connect(lambda t=th: _detached_threads.remove(t) if t in _detached_threads else None)

    DARK_QSS = """
    QMainWindow, QWidget, QDialog {
        background-color: #0f1117; color: #e5e9f0;
        font-family: "Inter", "Segoe UI", "Noto Sans", sans-serif; font-size: 12px;
    }
    QLabel { background: transparent; color: #e5e9f0; }
    #headerBanner {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #141824, stop:1 #1b2130);
        border-bottom: 1px solid #283044;
    }
    QPushButton#versionPill {
        background-color: #1e2536; border: 1px solid #2e3850; border-radius: 13px;
        padding: 4px 12px; color: #93c5fd; font-size: 11px; font-weight: 600;
    }
    QPushButton#versionPill:hover { border-color: #f59e0b; color: #fde68a; }
    QPushButton#modeBtn {
        background-color: #171c28; border: 1px solid #2b3344; border-radius: 6px;
        padding: 5px 14px; color: #94a3b8; font-weight: 600;
    }
    QPushButton#modeBtn:checked {
        background-color: #26354d; border-color: #38bdf8; color: #ffffff;
    }
    QToolBar {
        background-color: #151923; border-bottom: 1px solid #252b3b; spacing: 8px; padding: 6px 10px;
    }
    QLineEdit, QComboBox, QSpinBox {
        background-color: #0b0d12; border: 1px solid #2b3344; border-radius: 6px;
        padding: 5px 10px; color: #e5e9f0; min-height: 20px;
    }
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #f59e0b; }
    QComboBox QAbstractItemView {
        background-color: #141822; border: 1px solid #2b3344; color: #e5e9f0;
        selection-background-color: #26354d; selection-color: #ffffff;
    }
    QSpinBox::up-button, QSpinBox::down-button {
        background-color: #1e2433; border-left: 1px solid #2b3344; width: 18px;
    }
    QSpinBox::up-button:hover, QSpinBox::down-button:hover { background-color: #283044; }
    QCheckBox { spacing: 8px; color: #e5e9f0; }
    QCheckBox:disabled { color: #5b6478; }
    QCheckBox::indicator {
        width: 15px; height: 15px; border-radius: 4px;
        border: 1px solid #475569; background-color: #0b0d12;
    }
    QCheckBox::indicator:hover { border-color: #f59e0b; }
    QCheckBox::indicator:checked {
        background-color: #f59e0b; border-color: #fbbf24;
    }
    QCheckBox::indicator:disabled {
        border-color: #222736; background-color: #141822;
    }
    QPushButton {
        background-color: #1e2433; border: 1px solid #323b50; border-radius: 6px;
        padding: 6px 12px; color: #e5e9f0; font-weight: 500;
    }
    QPushButton:hover { background-color: #283044; border-color: #f59e0b; }
    QPushButton:pressed { background-color: #161b26; }
    QPushButton:disabled { color: #5b6478; background-color: #141822; border-color: #222736; }
    QPushButton#primaryBtn {
        background-color: #f59e0b; border: 1px solid #fbbf24; color: #0f1117; font-weight: 700;
    }
    QPushButton#primaryBtn:hover { background-color: #fbbf24; }
    QGroupBox {
        border: 1px solid #262d3d; border-radius: 6px; margin-top: 8px; padding-top: 10px;
        font-weight: 600; color: #9ca3af;
    }
    QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
    QFrame#sourceCard {
        background-color: #131722; border: 1px solid #262e42; border-radius: 8px;
    }
    QFrame#sourceCard:hover { border-color: #f59e0b; }
    QTreeWidget, QListWidget, QTableWidget, QPlainTextEdit, QTextBrowser {
        background-color: #0d1016; border: 1px solid #222836; border-radius: 6px;
        alternate-background-color: #12161f; gridline-color: #1c2230;
        selection-background-color: #26354d; selection-color: #ffffff;
    }
    QHeaderView::section {
        background-color: #171c28; color: #94a3b8; border: none;
        border-right: 1px solid #222836; border-bottom: 1px solid #222836;
        padding: 6px 10px; font-weight: 600;
    }
    QTabWidget::pane { border: 1px solid #222836; border-radius: 6px; background-color: #0d1016; }
    QTabBar::tab {
        background-color: #151923; color: #94a3b8; padding: 7px 14px;
        border: 1px solid #222836; border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px;
        margin-right: 2px;
    }
    QTabBar::tab:selected { background-color: #0d1016; color: #f59e0b; font-weight: 600; }
    QStatusBar { background-color: #141822; border-top: 1px solid #252b3b; color: #94a3b8; }
    QScrollBar:vertical { background: #0d1016; width: 10px; border-radius: 5px; }
    QScrollBar::handle:vertical { background: #2b3344; border-radius: 5px; min-height: 24px; }
    QScrollBar::handle:vertical:hover { background: #475569; }
    """

    class BlkInspectWorker(QThread):
        finished_ok = Signal(str, list, str)
        failed = Signal(str)

        def __init__(self, blk_path: Path):
            super().__init__()
            self.blk_path = blk_path

        def run(self):
            try:
                _dir, rows, log_text = scan_blk_json(self.blk_path)
                self.finished_ok.emit(str(self.blk_path), rows, log_text)
            except Exception as exc:
                self.failed.emit(str(exc))

    class AssetConvertWorker(QThread):
        finished_ok = Signal(str, str)
        failed = Signal(str)

        def __init__(self, blk_path: Path, asset_type: str, asset_name: str):
            super().__init__()
            self.blk_path = blk_path
            self.asset_type = asset_type
            self.asset_name = asset_name

        def run(self):
            try:
                if not cfg.can_run_cli():
                    self.failed.emit("AnimeStudio.CLI.exe or Wine runtime is not available.")
                    return
                out_dir = cfg.CACHE_DIR / "blk_convert" / self.blk_path.stem / self.asset_type
                out_dir.mkdir(parents=True, exist_ok=True)
                for ext in (".png", ".tga", ".fbx", ".wav", ".ogg", ".txt", ".bytes"):
                    cand = list(out_dir.rglob(f"{self.asset_name}{ext}"))
                    if cand:
                        self.finished_ok.emit(str(cand[0]), "cached")
                        return
                env = cfg.build_cli_env()
                cmd = cfg.build_cli_cmd([
                    cfg.to_cli_path(self.blk_path),
                    cfg.to_cli_path(out_dir),
                    "--game", "ZZZ",
                    "--types", self.asset_type,
                    "--names", self.asset_name,
                    "--group_assets", "ByType",
                    "--export_type", "Convert",
                ])
                proc = subprocess.run(
                    cmd, cwd=cfg.CLI_DIR, env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=180,
                    **cfg.subprocess_no_window_kwargs(),
                )
                files = [p for p in out_dir.rglob("*") if p.is_file() and self.asset_name.lower() in p.stem.lower()]
                if not files:
                    files = [p for p in out_dir.rglob("*") if p.is_file()]
                if not files:
                    self.failed.emit(f"No converted output (rc={proc.returncode}):\n{proc.stdout[-800:]}")
                    return
                self.finished_ok.emit(str(files[0]), proc.stdout[-800:])
            except Exception as exc:
                self.failed.emit(str(exc))

    class BatchExportWorker(QThread):
        log_line = Signal(str)
        finished_rc = Signal(int)
        failed = Signal(str)

        def __init__(self, fn):
            super().__init__()
            self.fn = fn

        def run(self):
            try:
                rc = self.fn(lambda line: self.log_line.emit(line))
                self.finished_rc.emit(int(rc or 0))
            except Exception as exc:
                self.failed.emit(str(exc))

    class AssetMapWorker(QThread):
        progress = Signal(int, str)
        log_line = Signal(str)
        finished_ok = Signal(dict)
        failed = Signal(str)

        def __init__(self, game_root: Path):
            super().__init__()
            self.game_root = game_root
            self.cancel_event = threading.Event()

        def run(self):
            try:
                if not cfg.CLI_EXE.is_file() or not cfg.NAP_MAP_FILE.is_file():
                    ensure_extractor_tools(
                        log_cb=lambda line: self.log_line.emit(line),
                        progress_cb=lambda pct, msg: self.progress.emit(min(pct, 25), msg),
                    )
                manifest = build_asset_map(
                    self.game_root,
                    log_cb=lambda line: self.log_line.emit(line),
                    progress_cb=lambda pct, msg: self.progress.emit(pct, msg),
                    cancel_event=self.cancel_event,
                )
                self.finished_ok.emit(manifest)
            except Exception as exc:
                self.failed.emit(str(exc))

    class AssetMapDialog(QDialog):
        def __init__(self, game_root: Path, parent=None):
            super().__init__(parent)
            self.game_root = game_root
            self.setWindowTitle("OpenZenless — Building Asset Map")
            _fit_to_screen(self, 680, 420)
            lay = QVBoxLayout(self)
            lay.setSpacing(10)

            title = QLabel("Indexing Game Assets Before Opening Viewer")
            title.setStyleSheet("font-size: 15px; font-weight: 700; color: #f59e0b;")
            lay.addWidget(title)

            self.status_lbl = QLabel(f"Scanning {cfg.resolve_files_root(game_root)}...")
            self.status_lbl.setStyleSheet("color: #94a3b8;")
            self.status_lbl.setWordWrap(True)
            lay.addWidget(self.status_lbl)

            self.pbar = QProgressBar()
            self.pbar.setRange(0, 100)
            self.pbar.setValue(0)
            lay.addWidget(self.pbar)

            self.log_box = QPlainTextEdit()
            self.log_box.setReadOnly(True)
            self.log_box.setFont(QFont("JetBrains Mono, Consolas, monospace", 10))
            lay.addWidget(self.log_box, 1)

            btns = QHBoxLayout()
            self.btn_skip = QPushButton("Use Quick Index")
            self.btn_skip.setToolTip("Cancel full AnimeStudio scan and open immediately with quick section index")
            self.btn_skip.clicked.connect(self._use_quick_index)
            btns.addStretch()
            btns.addWidget(self.btn_skip)
            lay.addLayout(btns)

            self.worker = AssetMapWorker(game_root)
            self.worker.progress.connect(self._on_progress)
            self.worker.log_line.connect(self.log_box.appendPlainText)
            self.worker.finished_ok.connect(self._on_done)
            self.worker.failed.connect(self._on_failed)
            self.worker.start()

        def _use_quick_index(self):
            self.btn_skip.setEnabled(False)
            self.btn_skip.setText("Finishing Quick Index...")
            self.worker.cancel_event.set()

        def _on_progress(self, pct: int, msg: str):
            self.pbar.setValue(pct)
            self.status_lbl.setText(msg)

        def _on_done(self, _manifest: dict):
            self.pbar.setValue(100)
            self.status_lbl.setText("Asset Map complete! Opening OpenZenless...")
            QApplication.processEvents()
            self.accept()

        def _on_failed(self, msg: str):
            self.status_lbl.setText(f"Mapping warning: {msg}")
            self.log_box.appendPlainText(f"[ERROR] {msg}")
            self.btn_skip.setEnabled(True)
            self.btn_skip.setText("Continue Anyway")

        def reject(self):
            if self.worker.isRunning():
                self.worker.cancel_event.set()
                _retire_thread(self.worker, 5000)
            super().reject()

        def closeEvent(self, event):
            if self.worker.isRunning():
                self.worker.cancel_event.set()
                _retire_thread(self.worker, 5000)
            super().closeEvent(event)

    class ExportDialog(QDialog):
        SCOPES = [
            ("character", "Whole Character / Bangboo (FBX + Textures + Optional GLB)"),
            ("animations", "Character Animations Only (AnimationClip / Animator)"),
            ("monster", "Monster / Boss (FBX + Textures + Optional GLB)"),
            ("npc", "NPC (FBX + Textures + Optional GLB)"),
            ("weapon", "W-Engine / Proxy Accessory / Prop (FBX + Optional GLB)"),
            ("map", "Curated Map (GLB Pipeline)"),
            ("stage", "Raw Stage (.blk Export / GLB)"),
            ("sprites", "UI Sprite Pack (Portraits, Mindscapes, Icons)"),
            ("spine", "Spine 2D Rig (.skel + .atlas + Texture2D)"),
            ("data", "Game Data Pack (Subtitles, .cloth, .human, Shaders)"),
            ("fonts", "Fonts (TTF / OTF from .blk)"),
            ("assets", "Custom Asset Types (Textures, Shaders, TextAsset, Mesh, ...)"),
            ("audio", "Wwise Audio (.pck / .bnk -> Auto-Named Voices / WAV)"),
            ("full", "Full Export (Characters + Maps + Fonts + Voices)"),
        ]

        def __init__(self, parent: "BlkExplorerWindow"):
            super().__init__(parent)
            self.parent_win = parent
            self.worker: BatchExportWorker | None = None
            self.setWindowTitle("OpenZenless — Batch & Pipeline Export")
            _fit_to_screen(self, 900, 680)
            lay = QVBoxLayout(self)

            tool_bar = QHBoxLayout()
            self.tool_status_lbl = QLabel()
            self.tool_status_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
            self.btn_setup_tools = QPushButton("Tools & Auto-Install...")
            self.btn_setup_tools.setToolTip("Inspect and auto-install AnimeStudio.CLI, Wine, .NET 9, ADB, vgmstream & Blender")
            self.btn_setup_tools.clicked.connect(self._setup_tools)
            self.btn_locate_ext = QPushButton("Locate Output/Extractor...")
            self.btn_locate_ext.clicked.connect(self._locate_extractor)
            tool_bar.addWidget(self.tool_status_lbl, 1)
            tool_bar.addWidget(self.btn_setup_tools)
            tool_bar.addWidget(self.btn_locate_ext)
            lay.addLayout(tool_bar)

            form = QFormLayout()
            self.scope_combo = QComboBox()
            for key, label in self.SCOPES:
                self.scope_combo.addItem(label, key)
            self.scope_combo.currentIndexChanged.connect(self._on_scope_changed)
            form.addRow("Export Scope:", self.scope_combo)

            self.target_combo = QComboBox()
            self.target_combo.setEditable(True)
            form.addRow("Target (Char / Stage):", self.target_combo)

            src_row = QHBoxLayout()
            self.source_edit = QLineEdit()
            self.btn_browse_file = QPushButton("File...")
            self.btn_browse_file.setToolTip("Select a single .blk or .pck file")
            self.btn_browse_file.clicked.connect(self._browse_source_file)
            self.btn_browse_src = QPushButton("Dir...")
            self.btn_browse_src.setToolTip("Select a source folder")
            self.btn_browse_src.clicked.connect(self._browse_source)
            src_row.addWidget(self.source_edit, 1)
            src_row.addWidget(self.btn_browse_file)
            src_row.addWidget(self.btn_browse_src)
            form.addRow("Source (.blk/.pck/Dir):", src_row)

            out_row = QHBoxLayout()
            self.out_edit = QLineEdit(str(cfg.FINAL_ROOT / "Exports"))
            self.btn_browse_out = QPushButton("Browse...")
            self.btn_browse_out.clicked.connect(self._browse_out)
            out_row.addWidget(self.out_edit, 1)
            out_row.addWidget(self.btn_browse_out)
            form.addRow("Destination Folder:", out_row)

            self.types_edit = QLineEdit("Texture2D Sprite Shader TextAsset Font")
            form.addRow("Asset Types (space-sep):", self.types_edit)

            self.mode_combo = QComboBox()
            self.mode_combo.addItems(["Convert", "JSON", "Raw"])
            form.addRow("CLI Export Mode:", self.mode_combo)

            self.filter_edit = QLineEdit()
            self.filter_edit.setPlaceholderText("Optional asset name substring filter...")
            form.addRow("Name Filter:", self.filter_edit)
            lay.addLayout(form)

            opts = QGroupBox("Options")
            opts_lay = QGridLayout(opts)
            opts_lay.setHorizontalSpacing(14)
            opts_lay.setVerticalSpacing(8)
            self.chk_glb = QCheckBox("Convert Character to GLB + Facial Morphs (Blender)")
            self.chk_shaders = QCheckBox("Export Shaders & Materials")
            self.chk_extract_bnk = QCheckBox("Extract embedded .bnk SoundBanks")
            self.chk_extract_bnk.setChecked(True)
            self.chk_resume = QCheckBox("Resume Map Checkpoints")
            self.chk_resume.setChecked(True)
            self.chk_dry = QCheckBox("Dry Run (Preview Full Plan)")
            self.spin_workers = QSpinBox()
            self.spin_workers.setRange(1, 8)
            self.spin_workers.setValue(cfg.default_worker_count(4))
            workers_row = QHBoxLayout()
            workers_row.setContentsMargins(0, 0, 0, 0)
            workers_row.addWidget(QLabel("Map Workers:"))
            workers_row.addWidget(self.spin_workers)
            workers_row.addStretch()

            self.chk_wwise_map = QCheckBox("Auto-name && Organize Voices via AnimeWwise (nap.map)")
            self.chk_wwise_map.setChecked(True)
            self.chk_wav = QCheckBox("Decode .wem -> .wav (vgmstream-cli)")
            self.chk_wav.setChecked(cfg.VGMSTREAM_CLI.is_file())

            self.chk_inc_chars = QCheckBox("Include Characters")
            self.chk_inc_chars.setChecked(True)
            self.chk_inc_scenes = QCheckBox("Include Curated Maps")
            self.chk_inc_scenes.setChecked(True)
            self.chk_inc_fonts = QCheckBox("Include Fonts")
            self.chk_inc_fonts.setChecked(True)
            self.chk_inc_audio = QCheckBox("Include Audio (.pck)")
            self.chk_inc_audio.setChecked(True)

            opts_lay.addWidget(self.chk_glb, 0, 0)
            opts_lay.addWidget(self.chk_resume, 0, 1)
            opts_lay.addLayout(workers_row, 0, 2)
            opts_lay.addWidget(self.chk_wwise_map, 1, 0)
            opts_lay.addWidget(self.chk_wav, 1, 1)
            opts_lay.addWidget(self.chk_dry, 1, 2)
            opts_lay.addWidget(self.chk_shaders, 2, 0)
            opts_lay.addWidget(self.chk_extract_bnk, 2, 1)

            full_row = QHBoxLayout()
            full_row.setContentsMargins(0, 4, 0, 0)
            full_row.setSpacing(14)
            full_row.addWidget(QLabel("Full Export:"))
            full_row.addWidget(self.chk_inc_chars)
            full_row.addWidget(self.chk_inc_scenes)
            full_row.addWidget(self.chk_inc_fonts)
            full_row.addWidget(self.chk_inc_audio)
            full_row.addStretch()
            opts_lay.addLayout(full_row, 3, 0, 1, 3)
            lay.addWidget(opts)

            self.log_box = QPlainTextEdit()
            self.log_box.setReadOnly(True)
            self.log_box.setFont(QFont("JetBrains Mono, Consolas, monospace", 10))
            lay.addWidget(self.log_box, 1)

            btns = QHBoxLayout()
            self.btn_run = QPushButton("Run Export")
            self.btn_run.setObjectName("primaryBtn")
            self.btn_run.clicked.connect(self._start_export)
            self.btn_close = QPushButton("Close")
            self.btn_close.clicked.connect(self._safe_close)
            btns.addStretch()
            btns.addWidget(self.btn_run)
            btns.addWidget(self.btn_close)
            lay.addLayout(btns)

            self._refresh_tool_status()
            self._on_scope_changed()

        def _refresh_tool_status(self):
            cli_ok = cfg.CLI_EXE.is_file()
            rt_lbl = "Native" if cfg.is_windows() else ("OK" if cfg.find_wine() else "MISSING")
            vgm_ok = cfg.VGMSTREAM_CLI.is_file()
            adb_ok = find_adb() is not None
            bl_ok = cfg.find_blender() is not None
            self.tool_status_lbl.setText(
                f"Tools — CLI: {'OK' if cli_ok else 'MISSING'} | "
                f"Runtime: {rt_lbl} | "
                f"vgmstream: {'OK' if vgm_ok else 'MISSING'} | "
                f"ADB: {'OK' if adb_ok else 'Missing'} | "
                f"Blender: {'OK' if bl_ok else 'No'}"
            )

        def _setup_tools(self):
            if self.worker is not None and self.worker.isRunning():
                return
            dlg = ToolsManagerDialog(self.parent_win.game_root, self)
            dlg.exec()
            self._refresh_tool_status()
            self.parent_win._refresh_version_badge()

        def _locate_extractor(self):
            d = QFileDialog.getExistingDirectory(
                self, "Select FinalOutput or Extractor Folder", str(cfg.FINAL_ROOT)
            )
            if d:
                self.parent_win.apply_output_root(Path(d))
                self.out_edit.setText(str(cfg.FINAL_ROOT / "Exports"))
                self._refresh_tool_status()
                self._on_scope_changed()

        def _browse_source_file(self):
            f, _ = QFileDialog.getOpenFileName(
                self, "Select Source Container (.blk / .pck)",
                str(self.parent_win.files_root),
                "ZZZ Containers (*.blk *.pck);;All Files (*)",
            )
            if f:
                self.source_edit.setText(f)

        def _browse_source(self):
            d = QFileDialog.getExistingDirectory(self, "Select Source Directory", str(self.parent_win.files_root))
            if d:
                self.source_edit.setText(d)

        def _browse_out(self):
            d = QFileDialog.getExistingDirectory(self, "Select Output Directory", self.out_edit.text() or str(cfg.FINAL_ROOT))
            if d:
                self.out_edit.setText(d)

        def _on_scope_changed(self):
            scope = self.scope_combo.currentData()
            self.target_combo.clear()
            chars = sorted(self.parent_win.char_blocks.keys())
            curated_maps = sorted(self.parent_win.curated_maps.keys())
            stages = sorted(self.parent_win.stage_blocks.keys())
            ents = self.parent_win.entities_index
            media = self.parent_win.media_index
            cur = self.parent_win.current_container
            if scope == "full":
                default_src = str(self.parent_win.game_root)
            elif scope == "audio":
                default_src = (
                    str(cur)
                    if (cur and cur.is_file() and cur.suffix.lower() in (".pck", ".bnk"))
                    else str(self.parent_win.files_root / "Audio")
                )
            else:
                default_src = (
                    str(cur)
                    if (cur and cur.is_file() and cur.suffix.lower() == ".blk")
                    else str(self.parent_win.files_root / "Blocks")
                )
            self.source_edit.setText(default_src)
            if self.target_combo.lineEdit():
                self.target_combo.lineEdit().setPlaceholderText("")
            uses_target = scope in (
                "character", "animations", "monster", "npc", "weapon",
                "map", "stage", "sprites", "spine", "data",
            )
            if scope in ("character", "animations"):
                self.target_combo.addItems(chars)
                if not chars and self.target_combo.lineEdit():
                    self.target_combo.lineEdit().setPlaceholderText("No characters mapped yet — check Game/Extractor root")
            elif scope == "monster":
                self.target_combo.addItems(sorted((ents.get("monsters") or {}).keys()))
            elif scope == "npc":
                self.target_combo.addItems(sorted((ents.get("npcs") or {}).keys()))
            elif scope == "weapon":
                self.target_combo.addItems(sorted({**(ents.get("weapons") or {}), **(ents.get("props") or {})}.keys()))
            elif scope == "map":
                self.target_combo.addItem("all")
                self.target_combo.addItems(curated_maps)
            elif scope == "stage":
                self.target_combo.addItems(stages)
                if not stages and self.target_combo.lineEdit():
                    self.target_combo.lineEdit().setPlaceholderText("No stages mapped yet — check Game/Extractor root")
            elif scope == "sprites":
                self.target_combo.addItems(list((media.get("ui_sprites") or {}).keys()))
            elif scope == "spine":
                self.target_combo.addItems(sorted((media.get("spine") or {}).keys()))
            elif scope == "data":
                self.target_combo.addItems(list((media.get("data_packs") or {}).keys()))
            else:
                self.target_combo.addItem("(Uses Source Path)")

            self.target_combo.setEnabled(uses_target)
            allow_src = scope in ("fonts", "assets", "audio")
            self.source_edit.setEnabled(allow_src)
            self.btn_browse_file.setEnabled(allow_src)
            self.btn_browse_src.setEnabled(allow_src)

            self.types_edit.setEnabled(scope == "assets")
            self.mode_combo.setEnabled(scope == "assets")
            self.filter_edit.setEnabled(scope == "assets")
            self.chk_glb.setEnabled(scope in ("character", "monster", "npc", "weapon", "full"))
            self.chk_shaders.setEnabled(scope in ("character", "monster", "npc", "weapon", "map", "stage", "full"))
            self.chk_extract_bnk.setEnabled(scope in ("audio", "full"))
            self.chk_resume.setEnabled(scope in ("map", "stage", "full"))
            self.spin_workers.setEnabled(scope in ("map", "stage"))
            self.chk_dry.setEnabled(scope == "full")
            self.chk_wwise_map.setEnabled(scope in ("audio", "full"))
            self.chk_wav.setEnabled(scope in ("audio", "full"))
            for chk in (self.chk_inc_chars, self.chk_inc_scenes, self.chk_inc_fonts, self.chk_inc_audio):
                chk.setEnabled(scope == "full")

        def _start_export(self):
            if self.worker is not None and self.worker.isRunning():
                return
            scope = self.scope_combo.currentData()
            target = self.target_combo.currentText().strip()
            src_text = self.source_edit.text().strip()
            src = Path(src_text) if src_text else Path(".")
            out_text = self.out_edit.text().strip()
            if not out_text:
                QMessageBox.warning(self, "Export Error", "Please specify a Destination Folder.")
                return
            if scope in ("character", "animations", "monster", "npc", "weapon", "map", "stage", "sprites", "spine", "data") and not target:
                QMessageBox.warning(self, "Export Error", f"Please select or enter a Target for '{scope}' export.")
                return
            if scope in ("assets", "audio") and (not src_text or not src.exists()):
                QMessageBox.warning(self, "Export Error", f"Source path does not exist:\n{src_text}")
                return
            inc_chars = self.chk_inc_chars.isChecked()
            inc_scenes = self.chk_inc_scenes.isChecked()
            inc_fonts = self.chk_inc_fonts.isChecked()
            inc_audio = self.chk_inc_audio.isChecked()
            if scope == "full" and not any((inc_chars, inc_scenes, inc_fonts, inc_audio)):
                QMessageBox.warning(self, "Export Error", "Select at least one Full Export scope.")
                return

            out = Path(out_text)
            game_root = self.parent_win.game_root
            convert_glb = self.chk_glb.isChecked()
            export_shaders = self.chk_shaders.isChecked()
            extract_bnk = self.chk_extract_bnk.isChecked()
            resume = self.chk_resume.isChecked()
            dry_run = self.chk_dry.isChecked()
            workers = self.spin_workers.value()
            use_wwise = self.chk_wwise_map.isChecked()
            decode_wav = self.chk_wav.isChecked()
            types = [t for t in self.types_edit.text().split() if t]
            mode = self.mode_combo.currentText()
            flt = self.filter_edit.text().strip()

            self.log_box.clear()
            self.btn_run.setEnabled(False)

            def task(cb):
                if scope == "character":
                    return export_character_full(target, out_dir=out, convert_glb=convert_glb, log_cb=cb, game_root=game_root, export_shaders=export_shaders)
                if scope == "animations":
                    return export_character_animations_only(target, game_root, out, log_cb=cb)
                if scope in ("monster", "npc", "weapon"):
                    cat_map = {"monster": "monsters", "npc": "npcs", "weapon": "weapons" if target.startswith(("Weapon_", "Belle_", "Wise_")) else "props"}
                    return export_entity_full(target, category=cat_map[scope], out_dir=out, convert_glb=convert_glb, log_cb=cb, game_root=game_root, export_shaders=export_shaders)
                if scope in ("map", "stage"):
                    return export_scene_map(target, resume=resume, workers=workers, out_dir=out, log_cb=cb, game_root=game_root, export_shaders=export_shaders)
                if scope == "sprites":
                    return export_ui_sprite_group(target, game_root, out, log_cb=cb)
                if scope == "spine":
                    return export_spine_rig(target, game_root, out, log_cb=cb)
                if scope == "data":
                    return export_data_pack(target, game_root, out, log_cb=cb)
                if scope == "fonts":
                    return export_fonts(game_root, out / "Fonts", source_path=src if src.is_file() else None, log_cb=cb)
                if scope == "assets":
                    return export_blk_asset_types(src, out / "Assets", types or ["Texture2D"], mode, flt, log_cb=cb)
                if scope == "audio":
                    return export_audio_pck(
                        src, out / "Audio",
                        use_wwise_map=use_wwise, decode_wav=decode_wav, extract_bnk=extract_bnk, log_cb=cb,
                    )
                return export_full_game(
                    game_root, out,
                    include_chars=inc_chars, include_scenes=inc_scenes,
                    include_fonts=inc_fonts, include_audio=inc_audio,
                    convert_glb=convert_glb, use_wwise_map=use_wwise, decode_wav=decode_wav,
                    dry_run=dry_run, log_cb=cb, export_shaders=export_shaders,
                )

            self.worker = BatchExportWorker(task)
            self.worker.log_line.connect(self.log_box.appendPlainText)
            self.worker.finished_rc.connect(self._on_done)
            self.worker.failed.connect(self._on_err)
            self.worker.start()

        def _on_done(self, rc: int):
            self.btn_run.setEnabled(True)
            self.log_box.appendPlainText(f"\n[Finished with exit code {rc}]")
            if rc == 0:
                self.parent_win.curated_maps = load_curated_map_index()
                self.parent_win._populate_basic_categories()

        def _on_err(self, msg: str):
            self.btn_run.setEnabled(True)
            self.log_box.appendPlainText(f"\n[ERROR] {msg}")
            QMessageBox.warning(self, "Export Error", msg)

        def _safe_close(self):
            if self.worker is not None and self.worker.isRunning():
                QMessageBox.information(self, "Export Running", "Please wait for the current export to finish.")
                return
            self.accept()

        def closeEvent(self, event):
            if self.worker is not None and self.worker.isRunning():
                QMessageBox.information(self, "Export Running", "Please wait for the current export to finish.")
                event.ignore()
                return
            super().closeEvent(event)

    class PhoneImportWorker(QThread):
        log_line = Signal(str)
        progress_changed = Signal(int, str)
        finished_ok = Signal(str)
        failed = Signal(str)

        def __init__(
            self,
            dest_root: Path,
            serial: str,
            pkg: str,
            remote_files_dir: str,
            pull_files: bool,
            pull_apks: bool,
        ):
            super().__init__()
            self.dest_root = dest_root
            self.serial = serial
            self.pkg = pkg
            self.remote_files_dir = remote_files_dir
            self.pull_files = pull_files
            self.pull_apks = pull_apks
            self._cancel_event = threading.Event()

        def cancel(self):
            self._cancel_event.set()

        def run(self):
            try:
                out = pull_from_phone(
                    dest_root=self.dest_root,
                    serial=self.serial,
                    pkg=self.pkg,
                    remote_files_dir=self.remote_files_dir,
                    pull_files=self.pull_files,
                    pull_apks=self.pull_apks,
                    extract_apk_blocks=self.pull_apks,
                    log_cb=self.log_line.emit,
                    cancel_event=self._cancel_event,
                    progress_cb=self.progress_changed.emit,
                )
                self.finished_ok.emit(str(out))
            except Exception as ex:
                self.failed.emit(str(ex))

    class ToolsInstallWorker(QThread):
        log_line = Signal(str)
        progress_changed = Signal(int, str)
        finished_ok = Signal()
        failed = Signal(str)

        def __init__(self, fn):
            super().__init__()
            self.fn = fn
            self.cancel_event = threading.Event()

        def cancel(self):
            self.cancel_event.set()

        def run(self):
            try:
                self.fn(self.log_line.emit, self.progress_changed.emit, self.cancel_event)
                self.finished_ok.emit()
            except Exception as ex:
                self.failed.emit(str(ex))

    class ToolsManagerDialog(QDialog):
        def __init__(self, game_root: Path, parent=None):
            super().__init__(parent)
            self.game_root = game_root
            self._worker: ToolsInstallWorker | None = None
            self.setWindowTitle("OpenZenless — Environment & Tool Dependencies")
            _fit_to_screen(self, 860, 620)

            lay = QVBoxLayout(self)
            lay.setContentsMargins(16, 14, 16, 14)
            lay.setSpacing(10)

            title = QLabel("Cross-Device Compatibility, Build Info & Auto-Installers")
            title.setStyleSheet("font-size: 15px; font-weight: 700; color: #f59e0b;")
            lay.addWidget(title)

            self.env_summary = QLabel("")
            self.env_summary.setWordWrap(True)
            self.env_summary.setStyleSheet("color: #cbd5e1;")
            lay.addWidget(self.env_summary)

            self.build_summary = QLabel("")
            self.build_summary.setWordWrap(True)
            self.build_summary.setStyleSheet("color: #94a3b8; font-size: 11px;")
            lay.addWidget(self.build_summary)

            self.tools_table = QTableWidget(0, 3)
            self.tools_table.setHorizontalHeaderLabels(["Component", "Status", "Location / Notes"])
            self.tools_table.setAlternatingRowColors(True)
            self.tools_table.setSelectionMode(QAbstractItemView.NoSelection)
            self.tools_table.setEditTriggers(QTableWidget.NoEditTriggers)
            self.tools_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
            self.tools_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
            self.tools_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
            self.tools_table.verticalHeader().setVisible(False)
            self.tools_table.setMinimumHeight(210)
            lay.addWidget(self.tools_table)

            act_row = QHBoxLayout()
            self.btn_install_core = QPushButton("Auto-Install Core Tools")
            self.btn_install_core.setObjectName("primaryBtn")
            self.btn_install_core.setToolTip("Install AnimeStudio.CLI, Wine (Linux x64), .NET 9, AnimeWwise, vgmstream & ADB into user tools folder")
            self.btn_install_core.clicked.connect(lambda: self._run_task(
                "Installing Core Tools...",
                lambda lcb, pcb, ce: ensure_extractor_tools(
                    log_cb=lcb, progress_cb=pcb, install_adb=True,
                    install_wine=True, install_vgmstream=True, cancel_event=ce,
                ),
            ))
            self.btn_install_adb = QPushButton("Install ADB")
            self.btn_install_adb.clicked.connect(lambda: self._run_task(
                "Installing Google Android Platform-Tools (ADB)...",
                lambda lcb, pcb, ce: ensure_adb(log_cb=lcb, progress_cb=pcb, cancel_event=ce),
            ))
            self.btn_install_vgm = QPushButton("Install vgmstream")
            self.btn_install_vgm.clicked.connect(lambda: self._run_task(
                "Installing vgmstream-cli...",
                lambda lcb, pcb, ce: ensure_vgmstream(log_cb=lcb, progress_cb=pcb, cancel_event=ce),
            ))
            self.btn_install_blender = QPushButton("Install Blender 4.2")
            self.btn_install_blender.clicked.connect(lambda: self._run_task(
                "Installing portable Blender 4.2 LTS...",
                lambda lcb, pcb, ce: ensure_blender(log_cb=lcb, progress_cb=pcb, cancel_event=ce),
            ))
            self.btn_refresh = QPushButton("Refresh")
            self.btn_refresh.clicked.connect(self._refresh)
            for b in (
                self.btn_install_core, self.btn_install_adb,
                self.btn_install_vgm, self.btn_install_blender, self.btn_refresh,
            ):
                act_row.addWidget(b)
            act_row.addStretch()
            lay.addLayout(act_row)

            self.prog_lbl = QLabel("")
            self.prog_lbl.setStyleSheet("color: #93c5fd; font-weight: 600;")
            self.prog_lbl.setMinimumWidth(0)
            self.prog_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.prog_lbl.setVisible(False)
            lay.addWidget(self.prog_lbl)

            self.pbar = QProgressBar()
            self.pbar.setVisible(False)
            lay.addWidget(self.pbar)

            self.log_box = QPlainTextEdit()
            self.log_box.setReadOnly(True)
            self.log_box.setFont(QFont("JetBrains Mono, Consolas, monospace", 9))
            self.log_box.setPlaceholderText("Installer logs appear here...")
            lay.addWidget(self.log_box, 1)

            foot = QHBoxLayout()
            foot.addStretch()
            self.btn_cancel = QPushButton("Cancel")
            self.btn_cancel.setEnabled(False)
            self.btn_cancel.clicked.connect(self._cancel)
            self.btn_close = QPushButton("Close")
            self.btn_close.clicked.connect(self._safe_close)
            foot.addWidget(self.btn_cancel)
            foot.addWidget(self.btn_close)
            lay.addLayout(foot)

            self._refresh()

        def _refresh(self):
            st = get_environment_status()
            py_mods = ", ".join(f"{k}:{'OK' if ok else 'NO'}" for k, ok in st["python_modules"].items())
            hw = st.get("hardware") or {}
            hw_line = (
                f"<br><b>Hardware:</b> {hw.get('cpu_threads', '?')} threads &nbsp;|&nbsp; "
                f"{hw.get('ram_avail_gb', '?')}/{hw.get('ram_total_gb', '?')} GiB RAM free &nbsp;|&nbsp; "
                f"GPU: {hw.get('gpu_vendor', 'Unknown')} &nbsp;|&nbsp; "
                f"Adaptive Workers: index={hw.get('index_workers', '?')}, blender={hw.get('blender_workers', '?')} (chunk={hw.get('blender_chunk_size', '?')})"
                if hw else ""
            )
            self.env_summary.setText(
                f"<b>Host:</b> {st['os_detail']} ({st['arch']}, {st['display']}) &nbsp;|&nbsp; "
                f"<b>Python {st['python']}:</b> {py_mods}{hw_line}<br>"
                f"<b>Portable Tools Dir:</b> {st['tools_dir']}"
            )
            gv = cfg.collect_game_version(self.game_root)
            if gv["exists"]:
                self.build_summary.setText(
                    f"ZZZ Build: {gv['platform']} | Unity {gv['unity_version']} | res {gv['res_revision']} | "
                    f"{gv['blk_count']:,} .blk | {gv['pck_count']:,} .pck | Audio: {gv['audio_lang']}"
                )
            else:
                self.build_summary.setText(f"ZZZ Build: No build loaded ({gv['game_root']})")

            rows = st["tools"]
            self.tools_table.setRowCount(len(rows))
            for i, r in enumerate(rows):
                it0 = QTableWidgetItem(r["name"])
                it0.setToolTip(r["required_for"])
                it1 = QTableWidgetItem(r["status"])
                it1.setForeground(QColor("#4ade80" if r["ok"] else "#fbbf24"))
                it2 = QTableWidgetItem(r["path"] or r["hint"])
                self.tools_table.setItem(i, 0, it0)
                self.tools_table.setItem(i, 1, it1)
                self.tools_table.setItem(i, 2, it2)

        def _set_busy(self, busy: bool):
            for b in (
                self.btn_install_core, self.btn_install_adb,
                self.btn_install_vgm, self.btn_install_blender, self.btn_refresh, self.btn_close,
            ):
                b.setEnabled(not busy)
            self.btn_cancel.setEnabled(busy)
            self.pbar.setVisible(busy)
            self.prog_lbl.setVisible(busy)

        def _run_task(self, title: str, fn):
            if self._worker is not None and self._worker.isRunning():
                return
            self.log_box.clear()
            self.log_box.appendPlainText(title)
            self.prog_lbl.setText(title)
            self.pbar.setRange(0, 0)
            self._set_busy(True)
            self._worker = ToolsInstallWorker(fn)
            self._worker.log_line.connect(self.log_box.appendPlainText)
            self._worker.progress_changed.connect(self._on_prog)
            self._worker.finished_ok.connect(self._on_done)
            self._worker.failed.connect(self._on_fail)
            self._worker.start()

        def _on_prog(self, pct: int, msg: str):
            self.prog_lbl.setText(msg)
            if pct < 0:
                self.pbar.setRange(0, 0)
            else:
                self.pbar.setRange(0, 100)
                self.pbar.setValue(max(0, min(100, pct)))

        def _on_done(self):
            self._set_busy(False)
            self.log_box.appendPlainText("Done!")
            self._refresh()

        def _on_fail(self, msg: str):
            self._set_busy(False)
            self.log_box.appendPlainText(f"[ERROR] {msg}")
            self._refresh()
            QMessageBox.warning(self, "Tool Installation Warning", msg)

        def _cancel(self):
            if self._worker and self._worker.isRunning():
                self._worker.cancel()
                self.btn_cancel.setEnabled(False)
                self.prog_lbl.setText("Cancelling...")

        def _safe_close(self):
            if self._worker and self._worker.isRunning():
                return
            self.accept()

        def reject(self):
            if self._worker and self._worker.isRunning():
                self._worker.cancel()
                self._worker.wait(3000)
            super().reject()

        def closeEvent(self, event):
            if self._worker and self._worker.isRunning():
                self._worker.cancel()
                self._worker.wait(3000)
            super().closeEvent(event)

    class CdnInfoWorker(QThread):
        ready = Signal(dict)
        failed = Signal(str)

        def __init__(self, region: str):
            super().__init__()
            self.region = region

        def run(self):
            try:
                self.ready.emit(cdn_downloader.fetch_zzz_cdn_info(region=self.region, parse_game_manifest=True))
            except Exception as ex:
                self.failed.emit(str(ex))

    class CdnDownloadWorker(QThread):
        log_line = Signal(str)
        progress_changed = Signal(int, str)
        finished_ok = Signal(str)
        failed = Signal(str)

        def __init__(self, dest_root: Path, region: str, profile: str, voice_langs: list[str], workers: int, ignore_space: bool):
            super().__init__()
            self.dest_root = dest_root
            self.region = region
            self.profile = profile
            self.voice_langs = voice_langs
            self.workers = workers
            self.ignore_space = ignore_space
            self._cancel_event = threading.Event()

        def cancel(self):
            self._cancel_event.set()

        def run(self):
            try:
                out = cdn_downloader.download_pc_build(
                    dest_root=self.dest_root,
                    region=self.region,
                    profile=self.profile,
                    voice_langs=self.voice_langs,
                    workers=self.workers,
                    log_cb=self.log_line.emit,
                    progress_cb=self.progress_changed.emit,
                    cancel_event=self._cancel_event,
                    ignore_space_check=self.ignore_space,
                )
                self.finished_ok.emit(str(out))
            except Exception as ex:
                self.failed.emit(str(ex))

    class CdnDownloadDialog(QDialog):
        def __init__(self, parent=None, default_dest: Path | None = None):
            super().__init__(parent)
            self.setWindowTitle("Download ZZZ PC Build from Official HoYoverse CDN")
            _fit_to_screen(self, 740, 560)
            self.dest_root: Path = default_dest or (Path.home() / "OpenZenless_ZZZ_PC")
            self._info: dict | None = None
            self._info_worker: CdnInfoWorker | None = None
            self._dl_worker: CdnDownloadWorker | None = None

            lay = QVBoxLayout(self)
            lay.setContentsMargins(18, 16, 18, 16)
            lay.setSpacing(10)

            title = QLabel("Download Official Zenless Zone Zero PC Files (HoYoverse CDN)")
            title.setStyleSheet("font-size: 15px; font-weight: 700; color: #f59e0b;")
            lay.addWidget(title)

            desc = QLabel(
                "Downloads verified PC game assets directly from HoYoverse's official Sophon CDN. "
                "Supports resume across restarts and verifies MD5 checksums on every chunk."
            )
            desc.setWordWrap(True)
            desc.setStyleSheet("color: #94a3b8;")
            lay.addWidget(desc)

            form = QFormLayout()
            form.setSpacing(8)

            reg_row = QHBoxLayout()
            self.region_combo = QComboBox()
            for rkey, rmeta in REGIONS.items():
                self.region_combo.addItem(rmeta["label"], rkey)
            self.region_combo.currentIndexChanged.connect(self._probe_cdn)
            self.btn_refresh_info = QPushButton("Check Latest Build")
            self.btn_refresh_info.clicked.connect(self._probe_cdn)
            reg_row.addWidget(self.region_combo, 1)
            reg_row.addWidget(self.btn_refresh_info)
            form.addRow("Server Region:", reg_row)

            self.build_lbl = QLabel("Querying HoYoverse CDN...")
            self.build_lbl.setWordWrap(True)
            form.addRow("Live Build:", self.build_lbl)

            self.profile_combo = QComboBox()
            for pkey, plabel in DOWNLOAD_PROFILES.items():
                self.profile_combo.addItem(plabel, pkey)
            self.profile_combo.currentIndexChanged.connect(self._update_size_estimate)
            form.addRow("Download Scope:", self.profile_combo)

            voice_row = QHBoxLayout()
            self.voice_checks: dict[str, QCheckBox] = {}
            for code, label in VOICE_LANGS:
                chk = QCheckBox(label)
                chk.stateChanged.connect(self._update_size_estimate)
                self.voice_checks[code] = chk
                voice_row.addWidget(chk)
            voice_row.addStretch()
            form.addRow("Extra Voice Packs:", voice_row)

            w_row = QHBoxLayout()
            self.workers_spin = QSpinBox()
            self.workers_spin.setRange(1, 8)
            self.workers_spin.setValue(4)
            self.chk_skip_space = QCheckBox("Skip free disk space check")
            w_row.addWidget(self.workers_spin)
            w_row.addSpacing(12)
            w_row.addWidget(self.chk_skip_space)
            w_row.addStretch()
            form.addRow("Parallel Workers:", w_row)

            dest_row = QHBoxLayout()
            self.dest_edit = QLineEdit(str(self.dest_root))
            self.dest_edit.textChanged.connect(self._update_size_estimate)
            btn_dest = QPushButton("Browse...")
            btn_dest.clicked.connect(self._browse_dest)
            dest_row.addWidget(self.dest_edit, 1)
            dest_row.addWidget(btn_dest)
            form.addRow("Save To Folder:", dest_row)

            lay.addLayout(form)

            self.space_lbl = QLabel("")
            self.space_lbl.setWordWrap(True)
            lay.addWidget(self.space_lbl)

            self.status_lbl = QLabel("")
            self.status_lbl.setStyleSheet("color: #93c5fd; font-weight: 600;")
            self.status_lbl.setMinimumWidth(0)
            self.status_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.status_lbl.setVisible(False)
            lay.addWidget(self.status_lbl)

            self.pbar = QProgressBar()
            self.pbar.setVisible(False)
            lay.addWidget(self.pbar)

            self.log_box = QPlainTextEdit()
            self.log_box.setReadOnly(True)
            self.log_box.setMaximumBlockCount(3000)
            self.log_box.setFont(QFont("JetBrains Mono, Consolas, monospace", 9))
            self.log_box.setPlaceholderText("CDN manifest and download logs appear here...")
            lay.addWidget(self.log_box, 1)

            btn_row = QHBoxLayout()
            btn_row.addStretch()
            self.btn_cancel = QPushButton("Pause / Cancel")
            self.btn_cancel.setEnabled(False)
            self.btn_cancel.clicked.connect(self._cancel_dl)
            self.btn_close = QPushButton("Close")
            self.btn_close.clicked.connect(self.reject)
            self.btn_start = QPushButton("Start Download")
            self.btn_start.setObjectName("primaryBtn")
            self.btn_start.clicked.connect(self._start_dl)
            btn_row.addWidget(self.btn_cancel)
            btn_row.addWidget(self.btn_close)
            btn_row.addWidget(self.btn_start)
            lay.addLayout(btn_row)

            self._probe_cdn()

        def _browse_dest(self):
            start = self.dest_edit.text().strip() or str(Path.home())
            d = QFileDialog.getExistingDirectory(self, "Select Destination Folder", start)
            if d:
                self.dest_edit.setText(d)

        def _probe_cdn(self):
            if self._dl_worker and self._dl_worker.isRunning():
                return
            if self._info_worker and self._info_worker.isRunning():
                self._info_worker.wait(1500)
            reg = self.region_combo.currentData() or "global"
            self.build_lbl.setText(f"Querying HoYoverse CDN ({reg})...")
            self.build_lbl.setStyleSheet("color: #93c5fd;")
            self._info_worker = CdnInfoWorker(reg)
            self._info_worker.ready.connect(self._on_info_ready)
            self._info_worker.failed.connect(self._on_info_failed)
            self._info_worker.start()

        def _on_info_ready(self, info: dict):
            self._info = info
            self.build_lbl.setText(f"ZZZ PC v{info['version']} (build {info['build_id']})")
            self.build_lbl.setStyleSheet("color: #4ade80; font-weight: 600;")
            for idx in range(self.profile_combo.count()):
                pk = self.profile_combo.itemData(idx)
                st = info["profiles"].get(pk)
                if st:
                    gb = st["size"] / (1024 ** 3)
                    self.profile_combo.setItemText(idx, f"{DOWNLOAD_PROFILES[pk]} — {st['file_count']:,} files ({gb:.2f} GB)")
            for code, label in VOICE_LANGS:
                v = info["voices"].get(code)
                if v:
                    self.voice_checks[code].setText(f"{label} ({v['size'] / (1024 ** 3):.2f} GB)")
            self._update_size_estimate()

        def _on_info_failed(self, msg: str):
            self.build_lbl.setText(f"Could not query CDN: {msg}")
            self.build_lbl.setStyleSheet("color: #f87171;")

        def _update_size_estimate(self):
            dest = Path(self.dest_edit.text().strip() or str(Path.home() / "OpenZenless_ZZZ_PC")).expanduser()
            probe = dest
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            try:
                free_gb = shutil.disk_usage(probe).free / (1024 ** 3)
            except OSError:
                free_gb = 0.0
            if not self._info:
                self.space_lbl.setText(f"Free disk space at destination: {free_gb:.1f} GB")
                self.space_lbl.setStyleSheet("color: #94a3b8;")
                return
            pk = self.profile_combo.currentData() or "assets"
            st = self._info["profiles"].get(pk) or {}
            need_bytes = int(st.get("size") or 0)
            n_files = int(st.get("file_count") or 0)
            for code, chk in self.voice_checks.items():
                if chk.isChecked() and code in self._info["voices"]:
                    need_bytes += int(self._info["voices"][code]["size"])
                    n_files += int(self._info["voices"][code]["file_count"])
            need_gb = need_bytes / (1024 ** 3)
            self.space_lbl.setText(
                f"Estimated download: {need_gb:.2f} GB ({n_files:,} files)  •  Free disk space: {free_gb:.1f} GB"
            )
            if free_gb > 0 and free_gb < need_gb * 1.03:
                self.space_lbl.setStyleSheet("color: #f87171;")
            else:
                self.space_lbl.setStyleSheet("color: #4ade80;")

        def _start_dl(self):
            if self._dl_worker and self._dl_worker.isRunning():
                return
            dest = Path(self.dest_edit.text().strip() or str(Path.home() / "OpenZenless_ZZZ_PC")).expanduser()
            reg = self.region_combo.currentData() or "global"
            prof = self.profile_combo.currentData() or "assets"
            langs = [c for c, chk in self.voice_checks.items() if chk.isChecked()]
            workers = self.workers_spin.value()

            self.log_box.clear()
            self.status_lbl.setText("Starting HoYoverse CDN download...")
            self.status_lbl.setStyleSheet("color: #93c5fd; font-weight: 600;")
            self.status_lbl.setVisible(True)
            self.pbar.setRange(0, 0)
            self.pbar.setVisible(True)
            self.btn_start.setEnabled(False)
            self.btn_refresh_info.setEnabled(False)
            self.btn_close.setEnabled(False)
            self.btn_cancel.setEnabled(True)

            self._dl_worker = CdnDownloadWorker(
                dest_root=dest,
                region=reg,
                profile=prof,
                voice_langs=langs,
                workers=workers,
                ignore_space=self.chk_skip_space.isChecked(),
            )
            self._dl_worker.log_line.connect(self.log_box.appendPlainText)
            self._dl_worker.progress_changed.connect(self._on_prog)
            self._dl_worker.finished_ok.connect(self._on_dl_ok)
            self._dl_worker.failed.connect(self._on_dl_fail)
            self._dl_worker.start()

        def _on_prog(self, pct: int, msg: str):
            self.status_lbl.setText(msg)
            self.status_lbl.setVisible(bool(msg))
            if pct < 0:
                self.pbar.setRange(0, 0)
            else:
                self.pbar.setRange(0, 100)
                self.pbar.setValue(max(0, min(100, pct)))

        def _cancel_dl(self):
            if self._dl_worker:
                self.status_lbl.setText("Pausing / cancelling download (progress saved)...")
                self.status_lbl.setStyleSheet("color: #fbbf24; font-weight: 600;")
                self._dl_worker.cancel()
                self.btn_cancel.setEnabled(False)

        def _reset_btns(self):
            self.pbar.setVisible(False)
            self.btn_start.setEnabled(True)
            self.btn_refresh_info.setEnabled(True)
            self.btn_close.setEnabled(True)
            self.btn_cancel.setEnabled(False)

        def _on_dl_ok(self, out_path: str):
            self._reset_btns()
            self.status_lbl.setStyleSheet("color: #4ade80; font-weight: 600;")
            self.dest_root = Path(out_path)
            self.accept()

        def _on_dl_fail(self, msg: str):
            self._reset_btns()
            self.status_lbl.setText(f"Stopped: {msg}")
            self.status_lbl.setStyleSheet("color: #f87171; font-weight: 600;")
            self.log_box.appendPlainText(f"ERROR: {msg}")
            QMessageBox.warning(self, "HoYoverse CDN Download", msg)

        def reject(self):
            if self._dl_worker and self._dl_worker.isRunning():
                self._dl_worker.cancel()
                _retire_thread(self._dl_worker, 5000)
            if self._info_worker and self._info_worker.isRunning():
                _retire_thread(self._info_worker, 2000)
            super().reject()

    class PhoneImportDialog(QDialog):
        def __init__(self, parent=None, default_dest: Path | None = None):
            super().__init__(parent)
            self.setWindowTitle("Load Zenless Zone Zero from Phone (ADB)")
            _fit_to_screen(self, 700, 520)
            self.dest_root: Path = default_dest or (Path.home() / "OpenZenless_ZZZ_Android")
            self._worker: PhoneImportWorker | None = None
            self._adb_worker: ToolsInstallWorker | None = None
            self._detected_pkg: str = "com.HoYoverse.Nap"
            self._devices: list[dict] = []

            lay = QVBoxLayout(self)
            lay.setContentsMargins(18, 16, 18, 16)
            lay.setSpacing(10)

            title = QLabel("Import ZZZ From Connected Android Phone (ADB)")
            title.setStyleSheet("font-size: 15px; font-weight: 700; color: #f59e0b;")
            lay.addWidget(title)

            dev_row = QHBoxLayout()
            dev_row.addWidget(QLabel("ADB Device:"))
            self.dev_combo = QComboBox()
            self.dev_combo.currentIndexChanged.connect(self._on_device_changed)
            dev_row.addWidget(self.dev_combo, 1)
            self.btn_install_adb = QPushButton("Auto-Install ADB")
            self.btn_install_adb.setObjectName("primaryBtn")
            self.btn_install_adb.setToolTip("Download & install portable Google Android Platform-Tools (`adb`) automatically")
            self.btn_install_adb.clicked.connect(self._auto_install_adb)
            dev_row.addWidget(self.btn_install_adb)
            self.btn_refresh = QPushButton("Refresh Devices")
            self.btn_refresh.clicked.connect(self._refresh_devices)
            dev_row.addWidget(self.btn_refresh)
            lay.addLayout(dev_row)

            wifi_row = QHBoxLayout()
            wifi_row.addWidget(QLabel("Wireless ADB:"))
            self.wifi_edit = QLineEdit()
            self.wifi_edit.setPlaceholderText("Optional phone IP:Port (e.g. 192.168.1.50:5555)")
            self.btn_wifi_connect = QPushButton("Connect")
            self.btn_wifi_connect.clicked.connect(self._connect_wireless)
            wifi_row.addWidget(self.wifi_edit, 1)
            wifi_row.addWidget(self.btn_wifi_connect)
            lay.addLayout(wifi_row)

            self.dev_status = QLabel("")
            self.dev_status.setWordWrap(True)
            lay.addWidget(self.dev_status)

            form = QFormLayout()
            form.setSpacing(8)
            self.remote_edit = QLineEdit()
            self.remote_edit.setPlaceholderText("/sdcard/Android/data/com.HoYoverse.Nap/files")
            form.addRow("Phone files/ Path:", self.remote_edit)

            dest_row = QHBoxLayout()
            self.dest_edit = QLineEdit(str(self.dest_root))
            btn_dest_browse = QPushButton("Browse...")
            btn_dest_browse.clicked.connect(self._browse_dest)
            dest_row.addWidget(self.dest_edit, 1)
            dest_row.addWidget(btn_dest_browse)
            form.addRow("Save To Folder:", dest_row)
            lay.addLayout(form)

            self.chk_files = QCheckBox("Pull game assets (Blocks/, Audio/, il2cpp/, revisions)")
            self.chk_files.setChecked(True)
            self.chk_apks = QCheckBox("Pull split APKs & extract base .blk packs (split_UnityDataAssetPack.apk)")
            self.chk_apks.setChecked(True)
            lay.addWidget(self.chk_files)
            lay.addWidget(self.chk_apks)

            self.transfer_status = QLabel("")
            self.transfer_status.setStyleSheet("color: #93c5fd; font-weight: 600;")
            self.transfer_status.setMinimumWidth(0)
            self.transfer_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.transfer_status.setVisible(False)
            lay.addWidget(self.transfer_status)

            self.progress = QProgressBar()
            self.progress.setVisible(False)
            lay.addWidget(self.progress)

            self.log_box = QPlainTextEdit()
            self.log_box.setReadOnly(True)
            self.log_box.setMaximumBlockCount(2000)
            self.log_box.setFont(QFont("JetBrains Mono, Consolas, monospace", 9))
            self.log_box.setPlaceholderText("ADB transfer progress will appear here...")
            lay.addWidget(self.log_box, 1)

            btn_row = QHBoxLayout()
            self.btn_fallback_cdn = QPushButton("No Phone? Download PC Build (CDN)...")
            self.btn_fallback_cdn.setToolTip("Download official ZZZ PC build directly from HoYoverse CDN instead")
            self.btn_fallback_cdn.clicked.connect(self._fallback_to_cdn)
            btn_row.addWidget(self.btn_fallback_cdn)
            btn_row.addStretch()
            self.btn_cancel = QPushButton("Cancel")
            self.btn_cancel.setEnabled(False)
            self.btn_cancel.clicked.connect(self._cancel_pull)
            self.btn_close = QPushButton("Close")
            self.btn_close.clicked.connect(self.reject)
            self.btn_start = QPushButton("Start Pull")
            self.btn_start.setObjectName("primaryBtn")
            self.btn_start.clicked.connect(self._start_pull)
            btn_row.addWidget(self.btn_cancel)
            btn_row.addWidget(self.btn_close)
            btn_row.addWidget(self.btn_start)
            lay.addLayout(btn_row)

            self._refresh_devices()

        def _auto_install_adb(self):
            if self._adb_worker and self._adb_worker.isRunning():
                return
            self.log_box.clear()
            self.transfer_status.setText("Installing Google Android Platform-Tools (ADB)...")
            self.transfer_status.setStyleSheet("color: #93c5fd; font-weight: 600;")
            self.transfer_status.setVisible(True)
            self.progress.setRange(0, 0)
            self.progress.setVisible(True)
            self.btn_install_adb.setEnabled(False)
            self.btn_refresh.setEnabled(False)

            self._adb_worker = ToolsInstallWorker(
                lambda lcb, pcb, ce: ensure_adb(log_cb=lcb, progress_cb=pcb, cancel_event=ce)
            )
            self._adb_worker.log_line.connect(self.log_box.appendPlainText)
            self._adb_worker.progress_changed.connect(self._on_progress)
            self._adb_worker.finished_ok.connect(self._on_adb_installed)
            self._adb_worker.failed.connect(self._on_adb_failed)
            self._adb_worker.start()

        def _on_adb_installed(self):
            self.progress.setVisible(False)
            self.btn_install_adb.setEnabled(True)
            self.btn_refresh.setEnabled(True)
            self.transfer_status.setStyleSheet("color: #4ade80; font-weight: 600;")
            self._refresh_devices()

        def _on_adb_failed(self, msg: str):
            self.progress.setVisible(False)
            self.btn_install_adb.setEnabled(True)
            self.btn_refresh.setEnabled(True)
            self.transfer_status.setText(f"ADB install failed: {msg}")
            self.transfer_status.setStyleSheet("color: #f87171; font-weight: 600;")
            QMessageBox.warning(self, "ADB Install Failed", msg)

        def _connect_wireless(self):
            if not find_adb():
                QMessageBox.information(self, "ADB Required", "Click 'Auto-Install ADB' first before connecting wirelessly.")
                return
            target = self.wifi_edit.text().strip()
            ok, msg = adb_connect_wireless(target)
            self.log_box.appendPlainText(msg)
            if not ok:
                QMessageBox.warning(self, "Wireless ADB", msg)
            self._refresh_devices()

        def _fallback_to_cdn(self):
            dlg = CdnDownloadDialog(self)
            if dlg.exec() == QDialog.Accepted:
                self.dest_root = dlg.dest_root
                self.accept()

        def _browse_dest(self):
            start = self.dest_edit.text().strip() or str(Path.home())
            d = QFileDialog.getExistingDirectory(self, "Select Destination Folder", start)
            if d:
                self.dest_edit.setText(d)

        def _refresh_devices(self):
            self.dev_combo.blockSignals(True)
            self.dev_combo.clear()
            self._devices = []
            adb_exe = find_adb()
            self.btn_install_adb.setVisible(not bool(adb_exe))
            self.btn_wifi_connect.setEnabled(bool(adb_exe))
            if not adb_exe:
                self.dev_status.setText(
                    "ADB (`adb`) was not found. Click 'Auto-Install ADB' above to download portable Google Platform-Tools, "
                    "or use 'No Phone? Download PC Build (CDN)...' below."
                )
                self.dev_status.setStyleSheet("color: #f87171;")
                self.btn_start.setEnabled(False)
                self.dev_combo.blockSignals(False)
                return
            try:
                self._devices = list_adb_devices(adb=adb_exe)
            except Exception as ex:
                self.dev_status.setText(f"Failed to query ADB: {ex}")
                self.dev_status.setStyleSheet("color: #f87171;")
                self.btn_start.setEnabled(False)
                self.dev_combo.blockSignals(False)
                return

            for dev in self._devices:
                self.dev_combo.addItem(dev["label"], dev)
            self.dev_combo.blockSignals(False)

            if not self._devices:
                self.dev_status.setText(
                    "No ADB devices detected. Enable USB Debugging on your phone (or connect Wireless ADB), "
                    "or download the PC build directly via HoYoverse CDN below."
                )
                self.dev_status.setStyleSheet("color: #fbbf24;")
                self.btn_start.setEnabled(False)
                return
            self._on_device_changed(0)

        def _on_device_changed(self, idx: int):
            if idx < 0 or idx >= len(self._devices):
                self.btn_start.setEnabled(False)
                return
            dev = self._devices[idx]
            serial = dev["serial"]
            state = dev["state"]
            if state == "no_permissions":
                self.dev_status.setText(
                    f"Device {serial} has insufficient USB permissions (`no permissions`). "
                    "Add udev rules (e.g. install `android-udev`) or add your user to `plugdev`, replug USB, or connect via Wireless ADB."
                )
                self.dev_status.setStyleSheet("color: #f87171;")
                self.btn_start.setEnabled(False)
                return
            if state == "unauthorized":
                self.dev_status.setText(
                    f"Device {serial} is unauthorized. Unlock your phone and accept the 'Allow USB debugging?' prompt, then click Refresh Devices."
                )
                self.dev_status.setStyleSheet("color: #fbbf24;")
                self.btn_start.setEnabled(False)
                return
            if state != "device":
                self.dev_status.setText(f"Device {serial} is `{state}` and not ready for ADB transfer.")
                self.dev_status.setStyleSheet("color: #fbbf24;")
                self.btn_start.setEnabled(False)
                return

            pkg, apk_paths = detect_zzz_package(serial)
            if not pkg:
                self.dev_status.setText(
                    f"Connected to {serial}, but Zenless Zone Zero (`com.HoYoverse.Nap` / `com.miHoYo.Nap`) was not found on this device."
                )
                self.dev_status.setStyleSheet("color: #fbbf24;")
                self.btn_start.setEnabled(False)
                return

            self._detected_pkg = pkg
            remote_dir, msg = detect_remote_files_dir(serial, pkg)
            if remote_dir:
                self.remote_edit.setText(remote_dir)
                self.dev_status.setText(f"{pkg} ({len(apk_paths)} APKs). {msg}")
                self.dev_status.setStyleSheet("color: #4ade80;")
            else:
                if not self.remote_edit.text().strip():
                    self.remote_edit.setText(f"/sdcard/Download/{pkg}/files")
                self.dev_status.setText(f"Detected {pkg} ({len(apk_paths)} APKs). {msg}")
                self.dev_status.setStyleSheet("color: #fbbf24;")
            self.btn_start.setEnabled(True)

        def _start_pull(self):
            idx = self.dev_combo.currentIndex()
            if idx < 0 or idx >= len(self._devices):
                return
            if not self.chk_files.isChecked() and not self.chk_apks.isChecked():
                QMessageBox.information(self, "Nothing Selected", "Select at least one option to pull.")
                return
            dest = Path(self.dest_edit.text().strip() or str(Path.home() / "OpenZenless_ZZZ_Android")).expanduser()
            serial = self._devices[idx]["serial"]
            remote_dir = self.remote_edit.text().strip()

            self.log_box.clear()
            self.transfer_status.setText("Starting ADB import...")
            self.transfer_status.setStyleSheet("color: #93c5fd; font-weight: 600;")
            self.transfer_status.setVisible(True)
            self.progress.setRange(0, 0)
            self.progress.setVisible(True)
            self.btn_start.setEnabled(False)
            self.btn_refresh.setEnabled(False)
            self.btn_close.setEnabled(False)
            self.btn_fallback_cdn.setEnabled(False)
            self.btn_cancel.setEnabled(True)
            self.log_box.appendPlainText(
                f"Device: {serial} | Package: {self._detected_pkg} | Destination: {dest}"
            )

            self._worker = PhoneImportWorker(
                dest_root=dest,
                serial=serial,
                pkg=self._detected_pkg,
                remote_files_dir=remote_dir,
                pull_files=self.chk_files.isChecked(),
                pull_apks=self.chk_apks.isChecked(),
            )
            self._worker.log_line.connect(self.log_box.appendPlainText)
            self._worker.progress_changed.connect(self._on_progress)
            self._worker.finished_ok.connect(self._on_pull_ok)
            self._worker.failed.connect(self._on_pull_fail)
            self._worker.start()

        def _on_progress(self, pct: int, status_text: str):
            self.transfer_status.setText(status_text)
            self.transfer_status.setVisible(bool(status_text))
            if pct < 0:
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, 100)
                self.progress.setValue(max(0, min(100, pct)))

        def _cancel_pull(self):
            if self._worker:
                self.transfer_status.setText("Cancelling ADB transfer...")
                self.transfer_status.setStyleSheet("color: #fbbf24; font-weight: 600;")
                self.log_box.appendPlainText("Cancellation requested; stopping active ADB process...")
                self._worker.cancel()
                self.btn_cancel.setEnabled(False)

        def _reset_controls(self):
            self.progress.setVisible(False)
            self.btn_start.setEnabled(True)
            self.btn_refresh.setEnabled(True)
            self.btn_close.setEnabled(True)
            self.btn_fallback_cdn.setEnabled(True)
            self.btn_cancel.setEnabled(False)

        def _on_pull_ok(self, out_path: str):
            self._reset_controls()
            self.transfer_status.setStyleSheet("color: #4ade80; font-weight: 600;")
            self.dest_root = Path(out_path)
            self.accept()

        def _on_pull_fail(self, msg: str):
            self._reset_controls()
            self.transfer_status.setText(f"Failed: {msg}")
            self.transfer_status.setStyleSheet("color: #f87171; font-weight: 600;")
            self.log_box.appendPlainText(f"ERROR: {msg}")
            QMessageBox.warning(self, "ADB Import Failed", msg)

        def reject(self):
            for th in (self._worker, self._adb_worker):
                if th is not None and th.isRunning():
                    th.cancel()
                    th.wait(3000)
            super().reject()

    class GameRootDialog(QDialog):
        """Unified Game Data Source Manager for On-Demand CDN, Local Install, Bulk CDN, and Phone ADB."""

        def __init__(self, initial_root: Path, parent=None):
            super().__init__(parent)
            self.setWindowTitle("OpenZenless — Game Data Source Manager")
            _fit_to_screen(self, 800, 540)
            self.selected_root = initial_root

            lay = QVBoxLayout(self)
            lay.setContentsMargins(18, 16, 18, 16)
            lay.setSpacing(10)

            title = QLabel("Game Data Source Manager")
            title.setStyleSheet("font-size: 16px; font-weight: 700; color: #f59e0b;")
            lay.addWidget(title)

            desc = QLabel(
                "Pick any of the 4 supported ways to load ZZZ assets: zero-install On-Demand CDN streaming, "
                "an existing local PC/Android build, bulk HoYoverse CDN download, or Android ADB pull."
            )
            desc.setWordWrap(True)
            desc.setStyleSheet("color: #94a3b8;")
            lay.addWidget(desc)

            ws_box = QGroupBox("Active Workspace & On-Demand CDN Settings")
            ws_lay = QVBoxLayout(ws_box)
            ws_lay.setSpacing(8)

            detected = cfg.scan_existing_game_installs()
            if detected:
                det_row = QHBoxLayout()
                det_row.addWidget(QLabel("Detected Builds:"))
                self.detected_combo = QComboBox()
                self.detected_combo.addItem(f"Found {len(detected)} workspace(s) on disk — select to fill...", "")
                for dp in detected:
                    fr = cfg.resolve_files_root(dp)
                    plat = "On-Demand CDN" if cfg.is_cdn_workspace_root(dp) else cfg.detect_build_platform(dp, fr)
                    self.detected_combo.addItem(f"[{plat}] {dp}", str(dp))
                self.detected_combo.currentIndexChanged.connect(self._pick_detected)
                det_row.addWidget(self.detected_combo, 1)
                ws_lay.addLayout(det_row)
            else:
                self.detected_combo = None

            if cfg.is_valid_game_root(initial_root):
                start_val = str(initial_root)
            elif detected:
                start_val = str(detected[0])
            else:
                start_val = str(Path.home() / "OpenZenless_ZZZ_CDN")

            path_row = QHBoxLayout()
            path_row.addWidget(QLabel("Workspace Folder:"))
            self.path_edit = QLineEdit(start_val)
            self.path_edit.setPlaceholderText("Select ZZZ folder or On-Demand CDN workspace...")
            self.path_edit.textChanged.connect(lambda _t: self._validate_path())
            btn_browse = QPushButton("Browse...")
            btn_browse.clicked.connect(self._browse)
            path_row.addWidget(self.path_edit, 1)
            path_row.addWidget(btn_browse)
            ws_lay.addLayout(path_row)

            cdn_opts = QHBoxLayout()
            self.chk_auto_cdn = QCheckBox("Auto-pull missing .blk files from HoYoverse CDN on export (On-Demand Mode)")
            self.chk_auto_cdn.setChecked(cfg.is_auto_cdn_pull_enabled())
            self.chk_auto_cdn.stateChanged.connect(lambda _s: self._validate_path())
            cdn_opts.addWidget(self.chk_auto_cdn, 1)
            cdn_opts.addWidget(QLabel("Server:"))
            self.cdn_region_combo = QComboBox()
            for rkey, rmeta in REGIONS.items():
                self.cdn_region_combo.addItem(rmeta["label"], rkey)
            cur_reg_idx = self.cdn_region_combo.findData(cfg.get_cdn_region())
            if cur_reg_idx >= 0:
                self.cdn_region_combo.setCurrentIndex(cur_reg_idx)
            cdn_opts.addWidget(self.cdn_region_combo)
            ws_lay.addLayout(cdn_opts)

            self.status_lbl = QLabel("")
            self.status_lbl.setWordWrap(True)
            ws_lay.addWidget(self.status_lbl)
            lay.addWidget(ws_box)

            src_box = QGroupBox("Acquire / Switch Game Files")
            grid = QGridLayout(src_box)
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(10)

            def _make_card(card_title: str, body: str, btn_label: str, slot, primary: bool = False) -> QFrame:
                card = QFrame()
                card.setObjectName("sourceCard")
                c_lay = QVBoxLayout(card)
                c_lay.setContentsMargins(12, 10, 12, 10)
                c_lay.setSpacing(6)
                t_lbl = QLabel(card_title)
                t_lbl.setStyleSheet("font-size: 13px; font-weight: 700; color: #f59e0b;" if primary else "font-size: 13px; font-weight: 700; color: #e2e8f0;")
                b_lbl = QLabel(body)
                b_lbl.setWordWrap(True)
                b_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
                c_lay.addWidget(t_lbl)
                c_lay.addWidget(b_lbl, 1)
                btn = QPushButton(btn_label)
                if primary:
                    btn.setObjectName("primaryBtn")
                btn.clicked.connect(slot)
                c_lay.addWidget(btn)
                return card

            self.card_ondemand = _make_card(
                "On-Demand CDN (Zero-Install)",
                "Browse the bundled 196 characters & 320 maps immediately (0 GB upfront) and automatically pull only the required .blk containers when exporting.",
                "Start in On-Demand CDN Mode",
                self._start_ondemand_mode,
                primary=True,
            )
            self.card_local = _make_card(
                "Local Installation (PC / Android)",
                "Point to an existing ZenlessZoneZero Game folder or previously pulled Android files/ directory on your drive.",
                "Browse Local Folder...",
                self._browse,
            )
            self.card_bulk_cdn = _make_card(
                "Bulk HoYoverse CDN Download",
                "Download all Blocks (54 GB), Assets + Audio, or the full verified ZZZ PC build with chunk MD5 resume.",
                "Open Bulk CDN Downloader...",
                self._download_from_cdn,
            )
            self.card_adb = _make_card(
                "Android Phone (USB / Wi-Fi ADB)",
                "Pull Blocks/, Audio/, and split APKs directly from a connected Android phone (auto-installs ADB if needed).",
                "Open Phone ADB Importer...",
                self._load_from_phone,
            )
            grid.addWidget(self.card_ondemand, 0, 0)
            grid.addWidget(self.card_local, 0, 1)
            grid.addWidget(self.card_bulk_cdn, 1, 0)
            grid.addWidget(self.card_adb, 1, 1)
            lay.addWidget(src_box, 1)

            btn_row = QHBoxLayout()
            btn_tools = QPushButton("Tools & Dependencies...")
            btn_tools.setToolTip("Inspect and auto-install AnimeStudio.CLI, Wine, .NET 9, ADB, vgmstream & Blender")
            btn_tools.clicked.connect(self._open_tools)
            btn_row.addWidget(btn_tools)
            btn_row.addStretch()
            self.btn_skip = QPushButton("Cancel / Skip")
            self.btn_skip.clicked.connect(self.reject)
            self.btn_continue = QPushButton("Use Selected Workspace")
            self.btn_continue.setObjectName("primaryBtn")
            self.btn_continue.clicked.connect(self._accept_root)
            btn_row.addWidget(self.btn_skip)
            btn_row.addWidget(self.btn_continue)
            lay.addLayout(btn_row)

            self._validate_path()

        def _pick_detected(self, idx: int):
            if self.detected_combo and idx > 0:
                p = self.detected_combo.itemData(idx)
                if p:
                    self.path_edit.setText(p)

        def _browse(self):
            start = self.path_edit.text().strip() or str(Path.home())
            d = QFileDialog.getExistingDirectory(self, "Select ZZZ Game Installation", start)
            if d:
                self.path_edit.setText(d)

        def _start_ondemand_mode(self):
            cur = self.path_edit.text().strip()
            cur_p = Path(cur).expanduser() if cur else (Path.home() / "OpenZenless_ZZZ_CDN")
            if cur_p.exists() and cfg.is_valid_game_root(cur_p) and not cfg.is_cdn_workspace_root(cur_p):
                dest = Path.home() / "OpenZenless_ZZZ_CDN"
            else:
                dest = cur_p
            reg = str(self.cdn_region_combo.currentData() or "global")
            cdn_downloader.init_cdn_workspace(dest, region=reg)
            ensure_prebuilt_index_loaded()
            self.chk_auto_cdn.setChecked(True)
            self.path_edit.setText(str(dest))
            self._accept_root()

        def _load_from_phone(self):
            cur = self.path_edit.text().strip()
            default_dest = Path(cur) if (cur and not cur.endswith("_CDN")) else (Path.home() / "OpenZenless_ZZZ_Android")
            dlg = PhoneImportDialog(self, default_dest=default_dest)
            if dlg.exec() == QDialog.Accepted:
                self.path_edit.setText(str(dlg.dest_root))
                self._validate_path()

        def _download_from_cdn(self):
            cur = self.path_edit.text().strip()
            default_dest = Path(cur) if (cur and not cur.endswith("_CDN")) else (Path.home() / "OpenZenless_ZZZ_PC")
            dlg = CdnDownloadDialog(self, default_dest=default_dest)
            if dlg.exec() == QDialog.Accepted:
                self.path_edit.setText(str(dlg.dest_root))
                self._validate_path()

        def _open_tools(self):
            cur = Path(self.path_edit.text().strip() or str(self.selected_root))
            ToolsManagerDialog(cur, self).exec()

        def _validate_path(self):
            text = self.path_edit.text().strip()
            p = Path(text).expanduser() if text else None
            auto_cdn = self.chk_auto_cdn.isChecked()
            if p and cfg.is_cdn_workspace_root(p):
                fr = cfg.resolve_files_root(p)
                n_blk = len(list((fr / "Blocks").glob("*.blk"))) if (fr / "Blocks").is_dir() else 0
                self.status_lbl.setText(
                    f"Ready: On-Demand CDN Workspace ({n_blk:,} cached .blk) at {p}"
                )
                self.status_lbl.setStyleSheet("color: #4ade80;")
                self.btn_continue.setEnabled(True)
            elif p and cfg.is_valid_game_root(p):
                fr = cfg.resolve_files_root(p)
                plat = cfg.detect_build_platform(p, fr)
                extra = " (missing .blks will auto-pull via CDN)" if auto_cdn else ""
                self.status_lbl.setText(f"Detected valid ZZZ files ({plat}) at: {fr}{extra}")
                self.status_lbl.setStyleSheet("color: #4ade80;")
                self.btn_continue.setEnabled(True)
            elif p and auto_cdn:
                self.status_lbl.setText(
                    f"Ready for On-Demand CDN Mode — will initialize workspace at {p} with the bundled catalog."
                )
                self.status_lbl.setStyleSheet("color: #93c5fd;")
                self.btn_continue.setEnabled(True)
            else:
                self.status_lbl.setText(
                    "No ZZZ Blocks/ or .blk/.pck files detected yet. Choose one of the 4 sources below or enable On-Demand Mode."
                )
                self.status_lbl.setStyleSheet("color: #fbbf24;")
                self.btn_continue.setEnabled(False)

        def _accept_root(self):
            raw = self.path_edit.text().strip() or str(Path.home() / "OpenZenless_ZZZ_CDN")
            p = Path(raw).expanduser()
            auto_cdn = self.chk_auto_cdn.isChecked()
            reg = str(self.cdn_region_combo.currentData() or "global")
            cfg.set_cdn_preferences(auto_pull=auto_cdn, region=reg)
            if auto_cdn and not cfg.is_valid_game_root(p):
                cdn_downloader.init_cdn_workspace(p, region=reg)
            self.selected_root = p
            cfg.GAME_ROOT = p
            cfg.save_user_config({"game_root": str(p)})
            self.accept()

    class BlkExplorerWindow(QMainWindow):
        def __init__(self, initial_root: Path):
            super().__init__()
            self.setWindowTitle("OpenZenless — ZZZ Proprietary File Explorer")
            if cfg.LOGO_ICON.is_file():
                self.setWindowIcon(QIcon(str(cfg.LOGO_ICON)))
            _fit_to_screen(self, 1500, 920)
            self.game_root = initial_root
            self.files_root = cfg.resolve_files_root(initial_root)
            self.current_dir: Path | None = None
            self.current_container: Path | None = None
            self.current_bnk_bytes: bytes | None = None
            self.current_bnk_parent: Path | None = None
            self.current_bnk_parent_offset: int = 0
            self.current_mode = "dir"
            self.all_rows: list[dict] = []
            self.hex_file: Path | None = None
            self.hex_offset = 0
            self.workers: list[QThread] = []

            self.stage_blocks = self._load_stage_blocks()
            self.curated_maps = load_curated_map_index()
            self.char_blocks = self._load_char_blocks()
            self.char_names = load_character_names()
            self.entities_index = load_oz_entities_index()
            self.media_index = load_oz_media_index()
            self.texture_sections = load_texture_sections()
            self.audio_groups_cache: list[dict] | None = None
            self.basic_all_items: list[dict] = []

            self._build_ui()
            self._populate_tree()
            self._populate_basic_categories()
            self._refresh_version_badge()
            if self.files_root.exists():
                self.open_directory(self.files_root)

        def _load_stage_blocks(self) -> dict[str, list[str]]:
            return load_stage_index()

        def _load_char_blocks(self) -> dict[str, list[str]]:
            return load_character_index()

        def _build_ui(self):
            root_widget = QWidget()
            root_layout = QVBoxLayout(root_widget)
            root_layout.setContentsMargins(0, 0, 0, 0)
            root_layout.setSpacing(0)
            self.setCentralWidget(root_widget)

            header = QWidget()
            header.setObjectName("headerBanner")
            h_lay = QHBoxLayout(header)
            h_lay.setContentsMargins(14, 10, 14, 10)
            h_lay.setSpacing(12)

            logo_lbl = QLabel()
            if cfg.LOGO_HEADER.is_file():
                pix = QPixmap(str(cfg.LOGO_HEADER))
                if not pix.isNull():
                    logo_lbl.setPixmap(pix.scaledToHeight(32, Qt.SmoothTransformation))
            else:
                logo_lbl.setText("OpenZenless")
                logo_lbl.setStyleSheet("font-size: 18px; font-weight: 700; color: #f59e0b;")
            h_lay.addWidget(logo_lbl)

            self.btn_mode_basic = QPushButton("Basic")
            self.btn_mode_basic.setObjectName("modeBtn")
            self.btn_mode_basic.setCheckable(True)
            self.btn_mode_basic.setChecked(True)
            self.btn_mode_basic.clicked.connect(lambda: self._switch_mode(0))
            h_lay.addWidget(self.btn_mode_basic)

            self.btn_mode_adv = QPushButton("Advanced")
            self.btn_mode_adv.setObjectName("modeBtn")
            self.btn_mode_adv.setCheckable(True)
            self.btn_mode_adv.clicked.connect(lambda: self._switch_mode(1))
            h_lay.addWidget(self.btn_mode_adv)

            self.btn_mode_viewer = QPushButton("3D Viewer")
            self.btn_mode_viewer.setObjectName("modeBtn")
            self.btn_mode_viewer.setCheckable(True)
            self.btn_mode_viewer.setToolTip("Interactive 3D GLB + Catalog Viewer for exported Characters & Maps")
            self.btn_mode_viewer.clicked.connect(lambda: self._switch_mode(2))
            h_lay.addWidget(self.btn_mode_viewer)

            h_lay.addStretch()

            self.header_version_pill = QPushButton("Checking build...")
            self.header_version_pill.setObjectName("versionPill")
            self.header_version_pill.setToolTip("Click to view full ZZZ build & CLI/Wine details")
            self.header_version_pill.clicked.connect(self.show_version_dialog)
            h_lay.addWidget(self.header_version_pill)

            self.btn_open_folder = QPushButton("Game Source...")
            self.btn_open_folder.setToolTip("Configure On-Demand CDN, Local Game Folder, Bulk CDN Download, or Phone ADB Import")
            self.btn_open_folder.clicked.connect(self.choose_root_folder)
            h_lay.addWidget(self.btn_open_folder)

            self.btn_rebuild_map = QPushButton("Rebuild Asset Map...")
            self.btn_rebuild_map.setToolTip("Scan game files with AnimeStudio.CLI to rebuild character & stage indexes")
            self.btn_rebuild_map.clicked.connect(self.rebuild_asset_map)
            h_lay.addWidget(self.btn_rebuild_map)

            self.btn_batch_export = QPushButton("Batch Export...")
            self.btn_batch_export.setObjectName("primaryBtn")
            self.btn_batch_export.setToolTip("Export characters, animations, scenes, fonts, audio, or full game (Ctrl+E)")
            self.btn_batch_export.clicked.connect(self.open_batch_export_dialog)
            h_lay.addWidget(self.btn_batch_export)

            self.btn_docs = QPushButton("Docs / Help")
            self.btn_docs.clicked.connect(self.show_docs_dialog)
            h_lay.addWidget(self.btn_docs)

            root_layout.addWidget(header)

            self.mode_stack = QStackedWidget()
            root_layout.addWidget(self.mode_stack, 1)

            self.basic_page = self._build_basic_page()
            self.mode_stack.addWidget(self.basic_page)

            splitter = QSplitter(Qt.Horizontal)
            splitter.setContentsMargins(8, 8, 8, 8)
            self.mode_stack.addWidget(splitter)

            self.tree = QTreeWidget()
            self.tree.setHeaderLabels(["Game Archive / Virtual Bookmarks"])
            self.tree.itemExpanded.connect(self._on_tree_expanded)
            self.tree.itemClicked.connect(self._on_tree_clicked)
            splitter.addWidget(self.tree)

            center_box = QWidget()
            center_lay = QVBoxLayout(center_box)
            center_lay.setContentsMargins(0, 0, 0, 0)
            center_lay.setSpacing(6)

            nav_row = QHBoxLayout()
            nav_row.setSpacing(6)
            self.btn_up = QPushButton("Up")
            self.btn_up.setToolTip("Go to parent folder (Backspace)")
            self.btn_up.clicked.connect(self.go_up)
            nav_row.addWidget(self.btn_up)

            self.path_bar = QLineEdit()
            self.path_bar.setPlaceholderText("Path or container...")
            self.path_bar.returnPressed.connect(self._on_path_entered)
            nav_row.addWidget(self.path_bar, 1)

            self.btn_open_view = QPushButton("Open / View")
            self.btn_open_view.setToolTip("Open folder/container or view selected file (Enter)")
            self.btn_open_view.clicked.connect(self.open_selected_item)
            self.btn_open_view.setEnabled(False)
            nav_row.addWidget(self.btn_open_view)

            self.btn_export = QPushButton("Export Selected")
            self.btn_export.clicked.connect(self.export_selected_item)
            self.btn_export.setEnabled(False)
            nav_row.addWidget(self.btn_export)
            center_lay.addLayout(nav_row)

            filter_row = QHBoxLayout()
            filter_row.setSpacing(6)
            self.search_box = QLineEdit()
            self.search_box.setPlaceholderText("Filter... (Ctrl+F)")
            self.search_box.textChanged.connect(self._apply_filter)
            filter_row.addWidget(self.search_box, 1)

            self.type_filter = QComboBox()
            self.type_filter.setMinimumWidth(140)
            self.type_filter.addItem("All Types")
            self.type_filter.currentTextChanged.connect(self._apply_filter)
            filter_row.addWidget(self.type_filter)
            center_lay.addLayout(filter_row)

            QShortcut(QKeySequence("Ctrl+F"), self, activated=self.search_box.setFocus)
            QShortcut(QKeySequence("Ctrl+E"), self, activated=self.open_batch_export_dialog)
            QShortcut(QKeySequence("F5"), self, activated=self._refresh_current_view)

            self.table = QTableWidget(0, 4)
            self.table.setHorizontalHeaderLabels(["Name", "Type / Format", "Size", "Details / Path"])
            self.table.setAlternatingRowColors(True)
            self.table.setSelectionBehavior(QTableWidget.SelectRows)
            self.table.setEditTriggers(QTableWidget.NoEditTriggers)
            self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
            self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
            self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
            self.table.setColumnWidth(0, 260)
            self.table.itemSelectionChanged.connect(self._on_table_selection)
            self.table.itemDoubleClicked.connect(self._on_table_double_click)
            center_lay.addWidget(self.table)
            splitter.addWidget(center_box)

            self.tabs = QTabWidget()
            self.props_view = QPlainTextEdit()
            self.props_view.setReadOnly(True)
            self.props_view.setFont(QFont("JetBrains Mono, Consolas, monospace", 10))
            self.tabs.addTab(self.props_view, "Properties / Dump")

            prev_w = QWidget()
            prev_lay = QVBoxLayout(prev_w)
            self.preview_info = QLabel("Select a Texture2D/Sprite inside a .blk to decode & preview.")
            self.preview_info.setWordWrap(True)
            self.btn_preview_tex = QPushButton("Decode & Preview Texture (CLI)")
            self.btn_preview_tex.clicked.connect(self.preview_selected_texture)
            self.btn_preview_tex.setVisible(False)
            self.btn_preview_tex.setEnabled(False)
            self.preview_img = QLabel()
            self.preview_img.setAlignment(Qt.AlignCenter)
            self.preview_img.setFrameShape(QFrame.StyledPanel)
            prev_lay.addWidget(self.preview_info)
            prev_lay.addWidget(self.btn_preview_tex)
            prev_lay.addWidget(self.preview_img, 1)
            self.tabs.addTab(prev_w, "Visual Preview")

            hex_w = QWidget()
            hex_lay = QVBoxLayout(hex_w)
            hex_bar = QHBoxLayout()
            self.hex_offset_edit = QLineEdit("0x0")
            self.hex_offset_edit.setMaximumWidth(140)
            self.hex_offset_edit.returnPressed.connect(self._jump_hex_offset)
            btn_go_hex = QPushButton("Jump Offset")
            btn_go_hex.clicked.connect(self._jump_hex_offset)
            btn_next_hex = QPushButton("Next 4 KB")
            btn_next_hex.clicked.connect(lambda: self._load_hex_chunk(self.hex_offset + 4096))
            btn_prev_hex = QPushButton("Prev 4 KB")
            btn_prev_hex.clicked.connect(lambda: self._load_hex_chunk(max(0, self.hex_offset - 4096)))
            hex_bar.addWidget(QLabel("Offset:"))
            hex_bar.addWidget(self.hex_offset_edit)
            hex_bar.addWidget(btn_go_hex)
            hex_bar.addWidget(btn_prev_hex)
            hex_bar.addWidget(btn_next_hex)
            hex_bar.addStretch()
            self.hex_view = QPlainTextEdit()
            self.hex_view.setReadOnly(True)
            self.hex_view.setFont(QFont("JetBrains Mono, Consolas, monospace", 10))
            hex_lay.addLayout(hex_bar)
            hex_lay.addWidget(self.hex_view, 1)
            self.tabs.addTab(hex_w, "Hex Viewer")

            splitter.addWidget(self.tabs)
            splitter.setSizes([250, 760, 490])

            self.viewer_page = self._build_viewer_page()
            self.mode_stack.addWidget(self.viewer_page)

            self.status = QStatusBar()
            self.progress = QProgressBar()
            self.progress.setMaximumWidth(180)
            self.progress.setVisible(False)
            self.status.addPermanentWidget(self.progress)
            self.setStatusBar(self.status)

        def _switch_mode(self, idx: int):
            prev_idx = self.mode_stack.currentIndex()
            if prev_idx == 2 and idx != 2 and hasattr(self, "viewer_widget"):
                self.viewer_widget.unload_scene("left 3D Viewer")
            self.mode_stack.setCurrentIndex(idx)
            self.btn_mode_basic.setChecked(idx == 0)
            self.btn_mode_adv.setChecked(idx == 1)
            self.btn_mode_viewer.setChecked(idx == 2)
            if idx == 2:
                self.viewer_widget.refresh_catalog()

        def _release_viewer_before_heavy_task(self, reason: str = "starting task"):
            if hasattr(self, "viewer_widget") and self.viewer_widget.has_loaded_scene():
                self.viewer_widget.unload_scene(reason)

        def _build_viewer_page(self) -> QWidget:
            if NativeViewerWidget is not None:
                self.viewer_widget = NativeViewerWidget(choose_output_cb=self._choose_output_root)
                return self.viewer_widget

            class _FallbackViewerWidget(QWidget):
                def __init__(self, parent=None):
                    super().__init__(parent)
                    self._worker: BatchExportWorker | None = None
                    lay = QVBoxLayout(self)
                    lay.setContentsMargins(28, 28, 28, 28)
                    lay.setSpacing(12)
                    title = QLabel("3D Viewer Dependencies Required")
                    title.setStyleSheet("font-size: 16px; font-weight: 700; color: #f59e0b;")
                    lay.addWidget(title)
                    msg = QLabel(
                        "The native OpenGL 3D Viewer requires `numpy`, `PyOpenGL`, and `pygltflib` "
                        "(plus system OpenGL drivers).\n\n"
                        f"Import error: {_VIEWER_IMPORT_ERR}"
                    )
                    msg.setWordWrap(True)
                    msg.setStyleSheet("color: #cbd5e1;")
                    lay.addWidget(msg)
                    self.btn_install = QPushButton("Install 3D Viewer Dependencies (numpy, PyOpenGL, pygltflib)")
                    self.btn_install.setObjectName("primaryBtn")
                    self.btn_install.clicked.connect(self._install_deps)
                    lay.addWidget(self.btn_install, alignment=Qt.AlignLeft)
                    self.log_box = QPlainTextEdit()
                    self.log_box.setReadOnly(True)
                    self.log_box.setVisible(False)
                    lay.addWidget(self.log_box, 1)
                    lay.addStretch()

                def _install_deps(self):
                    if self._worker and self._worker.isRunning():
                        return
                    self.btn_install.setEnabled(False)
                    self.log_box.setVisible(True)
                    self.log_box.clear()

                    def _task(cb):
                        cmd = [sys.executable, "-m", "pip", "install", "numpy", "PyOpenGL", "pygltflib"]
                        cb("$ " + " ".join(cmd))
                        proc = subprocess.Popen(
                            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            **cfg.subprocess_no_window_kwargs(),
                        )
                        assert proc.stdout is not None
                        for ln in proc.stdout:
                            cb(ln.rstrip("\n"))
                        return proc.wait()

                    self._worker = BatchExportWorker(_task)
                    self._worker.log_line.connect(self.log_box.appendPlainText)
                    self._worker.finished_rc.connect(lambda rc: (
                        self.btn_install.setEnabled(True),
                        QMessageBox.information(
                            self, "3D Viewer Dependencies",
                            "Installed! Restart OpenZenless to enable the Native 3D Viewer."
                            if rc == 0 else f"pip exited with code {rc}; see log.",
                        ),
                    ))
                    self._worker.failed.connect(lambda err: (
                        self.btn_install.setEnabled(True),
                        self.log_box.appendPlainText(f"[ERROR] {err}"),
                    ))
                    self._worker.start()

                def unload_scene(self, _reason: str = ""):
                    pass

                def has_loaded_scene(self) -> bool:
                    return False

                def refresh_catalog(self):
                    pass

                def shutdown(self):
                    if self._worker and self._worker.isRunning():
                        self._worker.wait(2000)

            self.viewer_widget = _FallbackViewerWidget(self)
            return self.viewer_widget

        def _choose_output_root(self):
            d = QFileDialog.getExistingDirectory(self, "Select Export Output Root (containing Exports / Characters / Maps_Combined)", str(cfg.FINAL_ROOT))
            if d:
                self.apply_output_root(Path(d))

        def apply_output_root(self, new_root: Path):
            cfg.set_final_root(new_root)
            self._reload_indexes()
            self.viewer_widget.refresh_catalog()
            self.status.showMessage(f"Output Root set to {cfg.FINAL_ROOT}")

        def _build_basic_page(self) -> QWidget:
            page = QSplitter(Qt.Horizontal)
            page.setContentsMargins(8, 8, 8, 8)

            self.basic_cats = QListWidget()
            self.basic_cats.currentRowChanged.connect(self._on_basic_category_changed)
            page.addWidget(self.basic_cats)

            mid = QWidget()
            mid_lay = QVBoxLayout(mid)
            mid_lay.setContentsMargins(0, 0, 0, 0)
            mid_lay.setSpacing(6)
            self.basic_search = QLineEdit()
            self.basic_search.setPlaceholderText("Search category items (name, ID, skin, section)...")
            self.basic_search.textChanged.connect(self._filter_basic_items)
            mid_lay.addWidget(self.basic_search)

            self.basic_kind_combo = QComboBox()
            self.basic_kind_combo.setToolTip("Filter by detected type / category")
            self.basic_kind_combo.currentIndexChanged.connect(self._on_basic_kind_changed)
            mid_lay.addWidget(self.basic_kind_combo)

            self.basic_tree = QTreeWidget()
            self.basic_tree.setHeaderLabels(["Name / Group", "Count / Status", "Identifier"])
            self.basic_tree.setAlternatingRowColors(True)
            self.basic_tree.setRootIsDecorated(True)
            self.basic_tree.setUniformRowHeights(True)
            self.basic_tree.setSelectionMode(QAbstractItemView.SingleSelection)
            self.basic_tree.setSortingEnabled(False)
            hdr = self.basic_tree.header()
            hdr.setSectionResizeMode(0, QHeaderView.Interactive)
            hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
            hdr.setSectionResizeMode(2, QHeaderView.Stretch)
            self.basic_tree.setColumnWidth(0, 320)
            self.basic_tree.itemSelectionChanged.connect(self._on_basic_item_selected)
            self.basic_tree.itemDoubleClicked.connect(lambda _it, _col: self._basic_inspect_in_advanced())
            mid_lay.addWidget(self.basic_tree)
            page.addWidget(mid)

            detail = QWidget()
            d_lay = QVBoxLayout(detail)
            d_lay.setContentsMargins(8, 0, 0, 0)
            d_lay.setSpacing(8)
            self.basic_title = QLabel("Select an item to inspect or export")
            self.basic_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #f59e0b;")
            self.basic_title.setWordWrap(True)
            d_lay.addWidget(self.basic_title)

            self.basic_info = QPlainTextEdit()
            self.basic_info.setReadOnly(True)
            self.basic_info.setFont(QFont("JetBrains Mono, Consolas, monospace", 10))
            d_lay.addWidget(self.basic_info, 1)

            btn_row = QHBoxLayout()
            self.btn_basic_inspect = QPushButton("Open in Advanced Mode")
            self.btn_basic_inspect.clicked.connect(self._basic_inspect_in_advanced)
            self.btn_basic_inspect.setEnabled(False)
            self.btn_basic_export1 = QPushButton("Export...")
            self.btn_basic_export1.setObjectName("primaryBtn")
            self.btn_basic_export1.clicked.connect(self._basic_export_primary)
            self.btn_basic_export1.setEnabled(False)
            self.btn_basic_export2 = QPushButton("Animations Only")
            self.btn_basic_export2.clicked.connect(self._basic_export_secondary)
            self.btn_basic_export2.setVisible(False)
            self.btn_basic_export_group = QPushButton("Export All in Group")
            self.btn_basic_export_group.clicked.connect(self._basic_export_group)
            self.btn_basic_export_group.setVisible(False)
            self.chk_basic_shaders = QCheckBox("Export Shaders & Materials")
            self.chk_basic_bnk = QCheckBox("Extract .bnk SFX")
            self.chk_basic_bnk.setChecked(True)
            self.chk_basic_bnk.setVisible(False)
            btn_row.addWidget(self.btn_basic_inspect)
            btn_row.addWidget(self.btn_basic_export1)
            btn_row.addWidget(self.btn_basic_export2)
            btn_row.addWidget(self.btn_basic_export_group)
            btn_row.addWidget(self.chk_basic_shaders)
            btn_row.addWidget(self.chk_basic_bnk)
            d_lay.addLayout(btn_row)

            page.addWidget(detail)
            page.setSizes([230, 680, 560])
            return page

        def _populate_basic_categories(self):
            self.basic_cats.clear()
            oz_chars = load_oz_character_index()
            char_fams = catalog.build_character_tree(oz_chars, include_bangboo=False) if oz_chars else []
            n_chars = len(char_fams) if char_fams else len(self.char_blocks)
            n_bangboo = len(catalog.classify_bangboo(oz_chars)) if oz_chars else 0
            n_monsters = len((self.entities_index.get("monsters") or {}))
            n_npcs = len((self.entities_index.get("npcs") or {}))
            n_weapons = len((self.entities_index.get("weapons") or {})) + len((self.entities_index.get("props") or {}))
            n_spine = len((self.media_index.get("spine") or {}))
            n_sprites = len((self.media_index.get("ui_sprites") or {}))
            n_clean_tex = sum(1 for k in self.texture_sections if catalog.is_texture_section(k))
            pcks = list((self.files_root / "Audio").rglob("*.pck")) if (self.files_root / "Audio").exists() else []
            n_data = 1 + len((self.media_index.get("data_packs") or {}))
            entries = [
                ("characters", f"Characters ({n_chars})"),
                ("bangboo", f"Bangboo ({n_bangboo})"),
                ("monsters", f"Monsters & Bosses ({n_monsters})"),
                ("npcs", f"NPCs ({n_npcs})"),
                ("weapons", f"W-Engines & Props ({n_weapons})"),
                ("maps", f"Curated Maps ({len(self.curated_maps)})"),
                ("media2d", f"UI, Sprites & Spine 2D ({n_sprites + n_spine + n_clean_tex})"),
                ("audio", f"Audio ({len(pcks)} .pck)"),
                ("fonts", f"Fonts & Game Data ({n_data})"),
            ]
            for key, label in entries:
                it = QListWidgetItem(label)
                it.setData(Qt.UserRole, key)
                self.basic_cats.addItem(it)
            self.basic_cats.setCurrentRow(0)

        def _on_basic_kind_changed(self):
            it = self.basic_cats.currentItem()
            if it and it.data(Qt.UserRole) == "audio":
                kf = self.basic_kind_combo.currentData()
                if kf == "mapped" and self.audio_groups_cache is None:
                    self.audio_groups_cache = scan_audio_groups(self.files_root / "Audio", use_wwise_map=True)
                    existing_ids = {r["id"] for r in self.basic_all_items}
                    for grp in self.audio_groups_cache:
                        gid = grp["group_key"]
                        if gid in existing_ids:
                            continue
                        self.basic_all_items.append({
                            "cat": "audio",
                            "subkind": "mapped_group",
                            "kind_key": "mapped",
                            "name": grp["label"],
                            "status": f"{grp['count']} entries • {human_size(grp['total_size'])}",
                            "id": gid,
                            "entries": grp["entries"],
                            "pck_paths": grp["pck_paths"],
                            "count": grp["count"],
                            "bnk_count": grp["bnk_count"],
                        })
            self._filter_basic_items()

        def _on_basic_category_changed(self, row: int):
            it = self.basic_cats.item(row)
            if not it:
                return
            cat = it.data(Qt.UserRole)
            items: list[dict] = []
            glb_roots = [cfg.FINAL_ROOT]
            self.basic_kind_combo.blockSignals(True)
            self.basic_kind_combo.clear()

            if cat == "characters":
                for label, key in (
                    ("Playable Agents & Proxies (default)", "playable"),
                    ("Playable Agents", "agent"),
                    ("Proxies (Belle & Wise)", "proxy"),
                    ("Has Skins / Alt Forms", "multi"),
                    ("Companions & Weapons", "companions"),
                    ("Limited / Model only", "limited_model"),
                    ("Needs attention / Empty stubs", "issues"),
                    ("All Characters", "all"),
                ):
                    self.basic_kind_combo.addItem(label, key)
                self.basic_kind_combo.setVisible(True)
                oz_chars = load_oz_character_index()
                if oz_chars:
                    families = catalog.build_character_tree(oz_chars, include_bangboo=False)
                    for grp in families:
                        pinfo = grp["primary_info"]
                        pid = grp["primary_id"]
                        p_name = self.char_names.get(pid) or grp["display"]
                        has_glb = catalog.find_exported_glb(pid, glb_roots) is not None
                        child_rows: list[dict] = []
                        child_search_tokens: list[str] = []
                        for cinfo in grp["children"]:
                            cid = cinfo["id"]
                            cname = self.char_names.get(cid) or cinfo["short_label"]
                            c_glb = catalog.find_exported_glb(cid, glb_roots) is not None
                            c_st = catalog.char_status_text(cinfo) + (" • GLB Ready" if c_glb else "")
                            c_blks = [b for b in [oz_chars[cid].get("prefab_blk", ""), *(oz_chars[cid].get("mesh_blks") or [])] if b]
                            child_search_tokens.extend([cname.lower(), cinfo["display"].lower(), cid.lower()])
                            child_rows.append({
                                "cat": "characters",
                                "name": cname,
                                "status": c_st,
                                "id": cid,
                                "blocks": c_blks,
                                "kind_key": cinfo["kind"],
                                "issues": cinfo["issues"],
                                "info": cinfo,
                                "search_extra": f"{p_name.lower()} {cinfo['display'].lower()}",
                            })
                        stat_parts = [catalog.CHAR_KIND_LABELS.get(pinfo["kind"], pinfo["kind"])]
                        if grp["skin_count"]:
                            stat_parts.append(f"{grp['skin_count']} skin{'s' if grp['skin_count'] > 1 else ''}")
                        if grp["alt_count"]:
                            stat_parts.append(f"{grp['alt_count']} alt")
                        if grp["companion_count"]:
                            stat_parts.append(f"{grp['companion_count']} comp")
                        if pinfo["anim_count"]:
                            stat_parts.append(f"{pinfo['anim_count']} anims")
                        if has_glb:
                            stat_parts.append("GLB Ready")
                        p_blks = [b for b in [oz_chars[pid].get("prefab_blk", ""), *(oz_chars[pid].get("mesh_blks") or [])] if b]
                        items.append({
                            "cat": "characters",
                            "name": p_name,
                            "status": " • ".join(stat_parts),
                            "id": pid,
                            "blocks": p_blks,
                            "kind_key": pinfo["kind"],
                            "role": grp["role"],
                            "primary_info": pinfo,
                            "info": pinfo,
                            "issues": grp["issues"],
                            "children": child_rows,
                            "family_member_ids": grp["member_ids"],
                            "search_extra": " ".join(child_search_tokens),
                        })
                else:
                    for cid in sorted(self.char_blocks):
                        has_glb = catalog.find_exported_glb(cid, glb_roots) is not None
                        items.append({
                            "cat": "characters",
                            "name": self.char_names.get(cid) or catalog.display_name(cid),
                            "status": f"{len(self.char_blocks[cid])} .blk" + (" • GLB Ready" if has_glb else ""),
                            "id": cid,
                            "blocks": self.char_blocks[cid],
                            "kind_key": "character",
                            "role": "agent",
                            "primary_info": {"kind": "character"},
                            "issues": [],
                            "children": [],
                            "family_member_ids": [cid],
                        })

            elif cat == "bangboo":
                for label, key in (
                    ("Playable / Full Bangboo (default)", "valid"),
                    ("All Bangboo", "all"),
                    ("Needs attention", "issues"),
                ):
                    self.basic_kind_combo.addItem(label, key)
                self.basic_kind_combo.setVisible(True)
                oz_chars = load_oz_character_index()
                bmap = catalog.classify_bangboo(oz_chars)
                for bid, info in bmap.items():
                    e = oz_chars.get(bid) or {}
                    blks = [b for b in [e.get("prefab_blk", ""), *(e.get("mesh_blks") or [])] if b]
                    has_glb = catalog.find_exported_glb(bid, glb_roots) is not None
                    st_parts = ["Bangboo", f"{len(blks)} .blk"]
                    if info["anim_count"]:
                        st_parts.append(f"{info['anim_count']} anims")
                    if has_glb:
                        st_parts.append("GLB Ready")
                    items.append({
                        "cat": "bangboo",
                        "name": info["display"],
                        "status": " • ".join(st_parts),
                        "id": bid,
                        "blocks": blks,
                        "kind_key": info["kind"],
                        "issues": info["issues"],
                        "info": info,
                    })

            elif cat == "monsters":
                for label, key in (
                    ("All Monsters & Bosses", "all"),
                    ("Bosses", "boss"),
                    ("Regular Enemies", "enemy"),
                    ("Has Animations", "animated"),
                ):
                    self.basic_kind_combo.addItem(label, key)
                self.basic_kind_combo.setVisible(True)
                raw_m = self.entities_index.get("monsters") or {}
                cmap = catalog.classify_monsters(raw_m)
                for mid, info in cmap.items():
                    e = raw_m.get(mid) or {}
                    blks = e.get("blocks") or e.get("mesh_blks") or []
                    has_glb = catalog.find_exported_glb(mid, glb_roots) is not None
                    st_parts = ["Boss" if info["kind"] == "boss" else "Monster", f"{len(blks)} .blk"]
                    if info["anim_count"]:
                        st_parts.append(f"{info['anim_count']} anims")
                    if has_glb:
                        st_parts.append("GLB Ready")
                    items.append({
                        "cat": "monsters",
                        "name": info["display"],
                        "status": " • ".join(st_parts),
                        "id": mid,
                        "blocks": blks,
                        "kind_key": info["kind"],
                        "anim_count": info["anim_count"],
                        "info": info,
                    })

            elif cat == "npcs":
                for label, key in (
                    ("Named NPCs (default)", "named"),
                    ("All NPCs", "all"),
                    ("Numeric / Crowd NPCs", "numeric"),
                    ("Has Animations", "animated"),
                ):
                    self.basic_kind_combo.addItem(label, key)
                self.basic_kind_combo.setVisible(True)
                raw_n = self.entities_index.get("npcs") or {}
                cmap = catalog.classify_npcs(raw_n)
                for nid, info in cmap.items():
                    e = raw_n.get(nid) or {}
                    blks = e.get("blocks") or e.get("mesh_blks") or []
                    has_glb = catalog.find_exported_glb(nid, glb_roots) is not None
                    st_parts = ["Named NPC" if info["kind"] == "named" else "Numeric NPC", f"{len(blks)} .blk"]
                    if info["anim_count"]:
                        st_parts.append(f"{info['anim_count']} anims")
                    if has_glb:
                        st_parts.append("GLB Ready")
                    items.append({
                        "cat": "npcs",
                        "name": info["display"],
                        "status": " • ".join(st_parts),
                        "id": nid,
                        "blocks": blks,
                        "kind_key": info["kind"],
                        "anim_count": info["anim_count"],
                        "info": info,
                    })

            elif cat == "weapons":
                for label, key in (
                    ("W-Engines & Proxy Accessories (default)", "gear"),
                    ("All W-Engines & Props", "all"),
                    ("W-Engines", "wengine"),
                    ("Proxy Accessories (Belle / Wise)", "accessory"),
                    ("Items", "item"),
                    ("Devices & Interactables", "device"),
                    ("Destructibles", "destruction"),
                    ("Game Props", "prop"),
                ):
                    self.basic_kind_combo.addItem(label, key)
                self.basic_kind_combo.setVisible(True)
                raw_w = self.entities_index.get("weapons") or {}
                raw_p = self.entities_index.get("props") or {}
                wmap = catalog.classify_weapons_props(raw_w, raw_p)
                grouped: dict[str, list[dict]] = {}
                for eid, info in wmap.items():
                    src_d = raw_w if info["category_src"] == "weapons" else raw_p
                    e = src_d.get(eid) or {}
                    blks = e.get("blocks") or e.get("mesh_blks") or []
                    has_glb = catalog.find_exported_glb(eid, glb_roots) is not None
                    st = f"{info['group_label']} • {len(blks)} .blk" + (" • GLB Ready" if has_glb else "")
                    grouped.setdefault(info["group_label"], []).append({
                        "cat": "weapons",
                        "name": info["display"],
                        "status": st,
                        "id": eid,
                        "blocks": blks,
                        "kind_key": info["kind"],
                        "category_src": info["category_src"],
                        "info": info,
                        "search_extra": info["group_label"].lower(),
                    })
                for glabel, children in grouped.items():
                    kinds = sorted({c["kind_key"] for c in children})
                    items.append({
                        "cat": "weapons",
                        "is_folder": True,
                        "name": glabel,
                        "status": f"{len(children)} item(s)",
                        "id": f"group:{glabel}",
                        "blocks": [],
                        "kind_key": kinds[0] if len(kinds) == 1 else "all",
                        "sub_kinds": set(kinds),
                        "children": children,
                        "search_extra": " ".join(c["name"].lower() + " " + c["id"].lower() for c in children),
                    })

            elif cat == "maps":
                for label, key in (
                    ("Curated maps (default)", "curated"),
                    ("Open areas, hubs & bosses", "top"),
                    ("All stages & maps", "all"),
                    ("Open areas (streaming)", "open"),
                    ("Hubs / city", "hub"),
                    ("Combat stages", "combat"),
                    ("Boss arenas", "boss"),
                    ("Zero Abyss / Complex rooms", "zero"),
                    ("NPC worksites", "npc"),
                    ("VR / training", "vr"),
                    ("Minigames", "minigame"),
                    ("Internal / test", "internal"),
                ):
                    self.basic_kind_combo.addItem(label, key)
                self.basic_kind_combo.setVisible(True)
                all_stages = self.stage_blocks or {}
                oz_maps = load_oz_map_index()
                minfo = catalog.classify_maps(oz_maps) if oz_maps else {}
                names = sorted(set(all_stages) | set(self.curated_maps), key=lambda n: (n.lower().replace("_streaming", ""), "_streaming" in n.lower()))
                for st in names:
                    info = minfo.get(st)
                    blks = self.curated_maps.get(st) or all_stages.get(st, [])
                    has_glb = catalog.find_exported_glb(st, glb_roots) is not None
                    if info:
                        status = catalog.map_status_text(info) + f" • {len(blks)} .blk"
                        disp = catalog.map_display_name(st, info)
                        cat_key = info["category"]
                    else:
                        status = f"{len(blks)} .blk"
                        disp, cat_key = st, "hub"
                    if has_glb:
                        status += " • GLB Ready"
                    items.append({
                        "cat": cat, "name": st if disp == st else f"{disp}", "status": status, "id": st,
                        "blocks": blks, "kind_key": cat_key, "issues": (info or {}).get("issues", []),
                        "curated": st in self.curated_maps, "info": info,
                    })

            elif cat == "media2d":
                for label, key in (
                    ("Curated 2D (Sprites, Spine & Clean Sections)", "curated2d"),
                    ("UI Sprite Packs", "sprite_pack"),
                    ("Spine 2D Rigs (.skel + .atlas)", "spine"),
                    ("Texture2D Sections (clean)", "tex_clean"),
                    ("All Raw Sections (incl. VoiceBlocks / Data)", "tex_all"),
                ):
                    self.basic_kind_combo.addItem(label, key)
                self.basic_kind_combo.setVisible(True)
                for skey, sinfo in (self.media_index.get("ui_sprites") or {}).items():
                    blks = sinfo.get("blks") or []
                    items.append({
                        "cat": "media2d",
                        "subkind": "sprite_pack",
                        "kind_key": "sprite_pack",
                        "name": f"[UI Pack] {sinfo.get('label', skey)}",
                        "status": f"{sinfo.get('count', 0):,} sprites • {len(blks)} .blk",
                        "id": skey,
                        "blocks": blks,
                        "samples": sinfo.get("samples") or [],
                    })
                for rkey, rinfo in sorted((self.media_index.get("spine") or {}).items(), key=lambda kv: kv[0].lower()):
                    blks = rinfo.get("blks") or []
                    n_sk = len(rinfo.get("skel_names") or [])
                    n_at = len(rinfo.get("atlas_names") or [])
                    items.append({
                        "cat": "media2d",
                        "subkind": "spine",
                        "kind_key": "spine",
                        "name": f"[Spine 2D] {rinfo.get('name', rkey)}",
                        "status": f"{n_sk} skel / {n_at} atlas • {len(blks)} .blk",
                        "id": rkey,
                        "blocks": blks,
                        "samples": [*(rinfo.get("skel_names") or []), *(rinfo.get("atlas_names") or [])],
                    })
                for sec, info in sorted(self.texture_sections.items()):
                    blks = info.get("blocks", [])
                    is_clean = catalog.is_texture_section(sec)
                    items.append({
                        "cat": "media2d",
                        "subkind": "texture_section",
                        "kind_key": "tex_clean" if is_clean else "tex_noise",
                        "name": sec,
                        "status": f"{len(blks)} .blk",
                        "id": sec,
                        "blocks": blks,
                        "samples": info.get("samples", []),
                    })

            elif cat == "audio":
                for label, key in (
                    ("Audio Archives (.pck)", "pck"),
                    ("Mapped Voices, Music & SoundBanks (nap.map)", "mapped"),
                ):
                    self.basic_kind_combo.addItem(label, key)
                self.basic_kind_combo.setVisible(True)
                audio_root = self.files_root / "Audio"
                if audio_root.exists():
                    for pck in sorted(audio_root.rglob("*.pck")):
                        rel = str(pck.relative_to(audio_root))
                        items.append({
                            "cat": cat,
                            "subkind": "pck",
                            "kind_key": "pck",
                            "name": pck.name,
                            "status": human_size(pck.stat().st_size),
                            "id": rel,
                            "path": str(pck),
                        })
                if self.audio_groups_cache:
                    for grp in self.audio_groups_cache:
                        items.append({
                            "cat": "audio",
                            "subkind": "mapped_group",
                            "kind_key": "mapped",
                            "name": grp["label"],
                            "status": f"{grp['count']} entries • {human_size(grp['total_size'])}",
                            "id": grp["group_key"],
                            "entries": grp["entries"],
                            "pck_paths": grp["pck_paths"],
                            "count": grp["count"],
                            "bnk_count": grp["bnk_count"],
                        })

            elif cat == "fonts":
                font_blks = []
                f_idx = cfg.CACHE_DIR / "asset_map" / "fonts_index_auto.json"
                if f_idx.is_file():
                    try:
                        font_blks = json.loads(f_idx.read_text()).get("blocks") or []
                    except Exception:
                        font_blks = []
                items.append({
                    "cat": "fonts",
                    "subkind": "fonts_all",
                    "name": "All Game Fonts (TTF / OTF)",
                    "status": f"{len(font_blks)} indexed .blk" if font_blks else "AnimeStudio CLI",
                    "id": "fonts_all",
                    "blocks": font_blks,
                })
                for dk, dp in (self.media_index.get("data_packs") or {}).items():
                    blks = dp.get("blks") or []
                    items.append({
                        "cat": "fonts",
                        "subkind": "data_pack",
                        "pack_key": dk,
                        "name": dp.get("label", dk),
                        "status": f"{dp.get('count', 0):,} assets • {len(blks)} .blk",
                        "id": dk,
                        "blocks": blks,
                        "samples": dp.get("samples") or [],
                    })

            if cat in ("fonts",):
                self.basic_kind_combo.setVisible(False)
            self.basic_kind_combo.setCurrentIndex(0)
            self.basic_kind_combo.blockSignals(False)
            self.basic_all_items = items
            self._filter_basic_items()

        def _filter_basic_items(self):
            q = self.basic_search.text().strip().lower()
            kf = self.basic_kind_combo.currentData() if self.basic_kind_combo.isVisible() else "all"

            def _row_matches_q(row_d: dict, include_extra: bool = True) -> bool:
                if not q:
                    return True
                if (
                    q in row_d["name"].lower()
                    or q in row_d["id"].lower()
                    or q in row_d["status"].lower()
                ):
                    return True
                return include_extra and (q in row_d.get("search_extra", ""))

            def _kind_ok(r: dict) -> bool:
                if kf in (None, "all"):
                    return True
                if kf == "curated":
                    return bool(r.get("curated", True))
                if kf == "top":
                    return bool(r.get("curated", True)) and r.get("kind_key") in catalog.MAP_TOP_CATEGORIES
                if kf == "playable":
                    return (r.get("primary_info") or {}).get("kind", r.get("kind_key")) != "stub"
                if kf == "agent":
                    return r.get("role") == "agent" and r.get("kind_key") in ("character", "limited", "model")
                if kf == "proxy":
                    return r.get("role") == "proxy"
                if kf == "multi":
                    return bool(r.get("children"))
                if kf == "companions":
                    return any((c.get("info") or {}).get("kind") in ("companion", "attachment") for c in r.get("children", ()))
                if kf == "limited_model":
                    return r.get("kind_key") in ("limited", "model") or any(
                        (c.get("info") or {}).get("kind") in ("limited", "model") for c in r.get("children", ())
                    )
                if kf == "issues":
                    return bool(r.get("issues"))
                if kf == "valid":
                    return r.get("kind_key") == "bangboo"
                if kf == "animated":
                    return int(r.get("anim_count") or 0) > 0
                if kf == "gear":
                    return bool(r.get("sub_kinds", {r.get("kind_key")}) & {"wengine", "accessory"})
                if kf == "curated2d":
                    return r.get("kind_key") in ("sprite_pack", "spine", "tex_clean")
                if kf == "tex_all":
                    return r.get("kind_key") in ("tex_clean", "tex_noise")
                if "sub_kinds" in r:
                    return kf in r["sub_kinds"]
                return r.get("kind_key") == kf

            def _visible_children(parent_r: dict) -> tuple[list[dict], bool]:
                ch_list = parent_r.get("children") or []
                if not ch_list:
                    return [], False
                if kf == "companions":
                    ch_list = [c for c in ch_list if (c.get("info") or {}).get("kind") in ("companion", "attachment")]
                elif kf == "issues":
                    ch_list = [c for c in ch_list if c.get("issues")]
                elif kf == "limited_model":
                    ch_list = [c for c in ch_list if (c.get("info") or {}).get("kind") in ("limited", "model")]
                if not q:
                    return ch_list, bool(parent_r.get("is_folder"))
                matched = [c for c in ch_list if _row_matches_q(c, include_extra=True)]
                if matched:
                    return matched, True
                if _row_matches_q(parent_r, include_extra=False):
                    return ch_list, bool(parent_r.get("is_folder"))
                return ch_list, bool(parent_r.get("is_folder"))

            self.basic_tree.clear()
            first_item: QTreeWidgetItem | None = None
            for r in self.basic_all_items:
                if not _kind_ok(r):
                    continue
                if q and not _row_matches_q(r, include_extra=True) and not any(
                    _row_matches_q(c, include_extra=True) for c in r.get("children", ())
                ):
                    continue
                top = QTreeWidgetItem([r["name"], r["status"], r["id"]])
                top.setData(0, Qt.UserRole, r)
                vis_children, expand_node = _visible_children(r)
                for c in vis_children:
                    ch_it = QTreeWidgetItem([c["name"], c["status"], c["id"]])
                    ch_it.setData(0, Qt.UserRole, c)
                    top.addChild(ch_it)
                self.basic_tree.addTopLevelItem(top)
                if expand_node and vis_children:
                    top.setExpanded(True)
                if first_item is None:
                    first_item = top
            if first_item is not None:
                self.basic_tree.setCurrentItem(first_item)
            self._on_basic_item_selected()

        def _selected_basic_item(self) -> dict | None:
            it = self.basic_tree.currentItem()
            return it.data(0, Qt.UserRole) if it else None

        def _on_basic_item_selected(self):
            r = self._selected_basic_item()
            if not r:
                self.btn_basic_inspect.setEnabled(False)
                self.btn_basic_export1.setEnabled(False)
                self.btn_basic_export2.setVisible(False)
                self.btn_basic_export_group.setVisible(False)
                self.chk_basic_shaders.setVisible(False)
                self.chk_basic_bnk.setVisible(False)
                return
            cat = r["cat"]
            is_folder = bool(r.get("is_folder"))
            self.basic_title.setText(r["name"])
            can_inspect = (not is_folder) and (
                bool(r.get("blocks")) or bool(r.get("path")) or bool(r.get("pck_paths")) or cat == "fonts"
            )
            self.btn_basic_inspect.setEnabled(can_inspect)
            self.btn_basic_export1.setEnabled(not is_folder)
            self.btn_basic_export2.setVisible((not is_folder) and cat in ("characters", "bangboo", "monsters", "npcs"))
            mem_ids = r.get("family_member_ids") or []
            self.btn_basic_export_group.setVisible(cat == "characters" and len(mem_ids) > 1)
            self.chk_basic_shaders.setVisible(cat in ("characters", "bangboo", "monsters", "npcs", "weapons", "maps"))
            self.chk_basic_bnk.setVisible(cat == "audio")

            lines = [f"Category : {cat}", f"ID / Key : {r['id']}", f"Status   : {r['status']}"]
            info = r.get("info")
            if info:
                if cat == "characters":
                    lines.append(f"Type     : {catalog.CHAR_KIND_LABELS.get(info['kind'], info['kind'])}")
                    if info.get("base"):
                        lines.append(f"Base     : {info['base']}")
                    if info.get("family_display") and len(mem_ids) > 1:
                        lines.append(f"Family   : {info['family_display']} ({len(mem_ids)} member(s))")
                    lines.append(f"Meshes   : {info['mesh_count']} .blk")
                    anim_src = f" (inherited from {info['anim_inherited_from']})" if info.get("anim_inherited_from") else ""
                    lines.append(f"Animations: {info['anim_count']} clips{anim_src}")
                elif cat in ("bangboo", "monsters", "npcs", "weapons"):
                    lines.append(f"Type     : {info.get('kind', cat)}")
                    lines.append(f"Meshes   : {info.get('mesh_count', 0)} .blk")
                    lines.append(f"Animations: {info.get('anim_count', 0)} clips")
                elif cat == "maps":
                    lines.append(f"Type     : {catalog.MAP_CATEGORY_LABELS.get(info['category'], info['category'])}")
                    if info.get("layer_of"):
                        lines.append(f"Layer of : {info['layer_of']}")
                    if info.get("variant_of"):
                        lines.append(f"Duplicate of: {info['variant_of']}")
                    if info["sectors"]:
                        lines.append(f"Sectors  : {info['sectors']} (coverage {(info['coverage'] or 0) * 100:.0f}%)")
                    if info.get("extra_scenes"):
                        oz_m = load_oz_map_index().get(r["id"]) or {}
                        ex_list = [
                            str(e.get("cab") or e.get("blk") or "")
                            for e in (oz_m.get("extra_scenes") or ())
                            if isinstance(e, dict)
                        ]
                        lines.append(f"Extra scenes: {len(ex_list)} ({', '.join(ex_list)})")
                for iss in r.get("issues", []):
                    lines.append(f"! Issue  : {iss}")

            if cat in ("characters", "bangboo", "monsters", "npcs", "weapons"):
                btn_lbls = {
                    "characters": "Export Whole Character",
                    "bangboo": "Export Bangboo (GLB)",
                    "monsters": "Export Monster / Boss",
                    "npcs": "Export NPC",
                    "weapons": "Export 3D Model",
                }
                self.btn_basic_export1.setText(btn_lbls[cat])
                self.btn_basic_export2.setText("Export Animations Only")
                if len(mem_ids) > 1:
                    self.btn_basic_export_group.setText(f"Export Family ({len(mem_ids)})")
                if r.get("blocks"):
                    lines.append(f"Blocks   : {', '.join(r['blocks'][:15])}")
                if is_folder:
                    lines.append(f"Subgroup contains {len(r.get('children') or [])} item(s). Expand to select and export one.")
                exp_dir = {
                    "characters": cfg.FINAL_ROOT / "Characters" / r["id"],
                    "bangboo": cfg.FINAL_ROOT / "Exports" / r["id"],
                    "monsters": cfg.FINAL_ROOT / "Exports" / "Monsters" / r["id"],
                    "npcs": cfg.FINAL_ROOT / "Exports" / "NPCs" / r["id"],
                    "weapons": cfg.FINAL_ROOT / "Exports" / ("Weapons" if r.get("category_src") == "weapons" else "Props") / r["id"],
                }.get(cat)
                if exp_dir and exp_dir.exists():
                    files = sorted(str(p.relative_to(exp_dir)) for p in exp_dir.rglob("*") if p.is_file())
                    lines.append(f"\nExported Files ({len(files)}):")
                    lines.extend(f"  - {f}" for f in files[:30])
            elif cat == "maps":
                self.btn_basic_export1.setText("Export Curated Map (GLB)")
                lines.append(f"Blocks ({len(r['blocks'])}): {', '.join(r['blocks'][:20])}")
            elif cat == "media2d":
                sk = r.get("subkind")
                self.btn_basic_export1.setText(
                    "Export UI Sprite Pack" if sk == "sprite_pack"
                    else "Export Spine 2D Rig" if sk == "spine"
                    else "Export Section Texture2Ds"
                )
                lines.append(f"Blocks ({len(r['blocks'])}): {', '.join(r['blocks'][:20])}")
                if r.get("samples"):
                    lines.append("\nSample Assets:")
                    lines.extend(f"  - {s}" for s in r["samples"])
            elif cat == "audio":
                if r.get("subkind") == "mapped_group":
                    self.btn_basic_export1.setText("Extract Mapped Group")
                    lines.append(f"Source .pck(s): {', '.join(Path(p).name for p in r.get('pck_paths', [])[:10])}")
                    lines.append(f"Entries  : {r.get('count', 0)} (SoundBanks: {r.get('bnk_count', 0)})")
                else:
                    self.btn_basic_export1.setText("Unpack .pck Audio")
                    lines.append(f"File Path: {r.get('path', '')}")
            elif cat == "fonts":
                if r.get("subkind") == "data_pack":
                    self.btn_basic_export1.setText("Export Data Pack")
                    lines.append(f"Blocks ({len(r['blocks'])}): {', '.join(r['blocks'][:20])}")
                    if r.get("samples"):
                        lines.append("\nSample Assets:")
                        lines.extend(f"  - {s}" for s in r["samples"])
                else:
                    self.btn_basic_export1.setText("Export All Fonts (TTF/OTF)")
                    if r.get("blocks"):
                        lines.append(f"Font Blocks ({len(r['blocks'])}): {', '.join(r['blocks'][:20])}")
            req_chk_blks: list[str] = []
            if cat in ("characters", "bangboo") and not is_folder:
                oz_rec = load_oz_character_index().get(r["id"])
                if isinstance(oz_rec, dict):
                    req_chk_blks = collect_entry_required_blks(oz_rec, include_anims=True, include_deps=True)
            elif cat in ("monsters", "npcs", "weapons") and not is_folder:
                src_k = r.get("category_src") if cat == "weapons" else cat
                oz_rec = (self.entities_index.get(src_k or cat) or {}).get(r["id"])
                if isinstance(oz_rec, dict):
                    req_chk_blks = collect_entry_required_blks(oz_rec, include_anims=True, include_deps=True)
            elif cat == "maps" and not is_folder:
                oz_rec = load_oz_map_index().get(r["id"])
                if isinstance(oz_rec, dict):
                    req_chk_blks = collect_map_required_blks(r["id"], oz_rec)
            if not req_chk_blks and not is_folder and r.get("blocks"):
                req_chk_blks = list(r.get("blocks") or [])
            if req_chk_blks:
                local_n = sum(1 for b in req_chk_blks if cfg.resolve_blk_path(self.game_root, str(b)) is not None)
                miss_n = len(req_chk_blks) - local_n
                if miss_n == 0:
                    lines.append(f"Sources  : {local_n}/{len(req_chk_blks)} .blk on disk (ready offline)")
                elif cfg.is_auto_cdn_pull_enabled():
                    lines.append(f"Sources  : {local_n}/{len(req_chk_blks)} .blk on disk ({miss_n} will auto-pull via CDN on export)")
                else:
                    lines.append(f"Sources  : {local_n}/{len(req_chk_blks)} .blk on disk ({miss_n} missing locally)")

            self.basic_info.setPlainText("\n".join(lines))

        def _basic_inspect_in_advanced(self):
            r = self._selected_basic_item()
            if not r or r.get("is_folder"):
                return
            cat = r["cat"]
            self._switch_mode(1)
            if cat in ("characters", "bangboo", "monsters", "npcs", "weapons", "maps", "media2d"):
                self._open_block_group(f"{cat.title()}: {r['name']}", r.get("blocks", []))
            elif cat == "audio":
                if r.get("path"):
                    self.open_akpk(Path(r["path"]))
                elif r.get("pck_paths"):
                    self.open_akpk(Path(r["pck_paths"][0]))
            elif cat == "fonts":
                if r.get("blocks"):
                    self._open_block_group(f"Fonts / Data: {r['name']}", r.get("blocks", []))
                else:
                    self.open_directory(self.files_root / "Blocks")

        def _run_basic_export(self, title: str, fn):
            self._release_viewer_before_heavy_task("starting export")
            dlg = QDialog(self)
            dlg.setWindowTitle(title)
            dlg.resize(760, 440)
            lay = QVBoxLayout(dlg)
            log = QPlainTextEdit()
            log.setReadOnly(True)
            log.setFont(QFont("JetBrains Mono, Consolas, monospace", 10))
            lay.addWidget(log, 1)
            btn_close = QPushButton("Close")
            btn_close.setEnabled(False)
            btn_close.clicked.connect(dlg.accept)
            lay.addWidget(btn_close)
            w = BatchExportWorker(fn)
            w.log_line.connect(log.appendPlainText)
            w.finished_rc.connect(lambda rc: (btn_close.setEnabled(True), log.appendPlainText(f"\n[Done rc={rc}]")))
            w.failed.connect(lambda err: (btn_close.setEnabled(True), log.appendPlainText(f"\n[ERROR] {err}")))
            self.workers.append(w)
            w.start()
            dlg.exec()
            self._refresh_version_badge()
            self._on_basic_item_selected()

        def _basic_export_primary(self):
            r = self._selected_basic_item()
            if not r or r.get("is_folder"):
                return
            cat = r["cat"]
            out = QFileDialog.getExistingDirectory(self, "Select Export Destination", str(cfg.FINAL_ROOT / "Exports"))
            if not out:
                return
            out_dir = Path(out)
            export_shaders = self.chk_basic_shaders.isChecked()
            extract_bnk = self.chk_basic_bnk.isChecked()
            if cat in ("characters", "bangboo"):
                self._run_basic_export(
                    f"Exporting {r['name']}",
                    lambda cb: export_character_full(r["id"], out_dir=out_dir, convert_glb=True, log_cb=cb, game_root=self.game_root, export_shaders=export_shaders),
                )
            elif cat in ("monsters", "npcs", "weapons"):
                ent_cat = r.get("category_src") if cat == "weapons" else cat
                self._run_basic_export(
                    f"Exporting {r['name']}",
                    lambda cb: export_entity_full(r["id"], category=ent_cat, out_dir=out_dir, convert_glb=True, log_cb=cb, game_root=self.game_root, export_shaders=export_shaders),
                )
            elif cat == "maps":
                self._run_basic_export(
                    f"Exporting Map {r['id']}",
                    lambda cb: export_scene_map(
                        r["id"], resume=True, workers=cfg.default_worker_count(4),
                        out_dir=out_dir, log_cb=cb, game_root=self.game_root, export_shaders=export_shaders,
                    ),
                )
            elif cat == "media2d":
                sk = r.get("subkind")
                if sk == "sprite_pack":
                    self._run_basic_export(
                        f"Exporting UI Pack: {r['name']}",
                        lambda cb: export_ui_sprite_group(r["id"], self.game_root, out_dir, log_cb=cb),
                    )
                elif sk == "spine":
                    self._run_basic_export(
                        f"Exporting Spine 2D: {r['name']}",
                        lambda cb: export_spine_rig(r["id"], self.game_root, out_dir, log_cb=cb),
                    )
                else:
                    self._run_basic_export(
                        f"Exporting Texture2D: {r['name']}",
                        lambda cb: export_texture_section(r["id"], r.get("blocks", []), self.game_root, out_dir, log_cb=cb),
                    )
            elif cat == "audio":
                if r.get("subkind") == "mapped_group":
                    self._run_basic_export(
                        f"Extracting Audio: {r['name']}",
                        lambda cb: export_audio_entries(
                            r.get("entries", []), out_dir / "Audio",
                            decode_wav=cfg.VGMSTREAM_CLI.is_file(), extract_bnk=extract_bnk, log_cb=cb,
                        ),
                    )
                else:
                    self._run_basic_export(
                        f"Unpacking {r['name']} (AnimeWwise Auto-Named)",
                        lambda cb: export_audio_pck(
                            Path(r["path"]), out_dir / "Audio",
                            use_wwise_map=True, decode_wav=cfg.VGMSTREAM_CLI.is_file(),
                            extract_bnk=extract_bnk, log_cb=cb,
                        ),
                    )
            elif cat == "fonts":
                if r.get("subkind") == "data_pack":
                    self._run_basic_export(
                        f"Exporting {r['name']}",
                        lambda cb: export_data_pack(r["pack_key"], self.game_root, out_dir, log_cb=cb),
                    )
                else:
                    self._run_basic_export(
                        "Exporting Fonts",
                        lambda cb: export_fonts(self.game_root, out_dir / "Fonts", log_cb=cb),
                    )

        def _basic_export_secondary(self):
            r = self._selected_basic_item()
            if not r or r.get("is_folder") or r["cat"] not in ("characters", "bangboo", "monsters", "npcs"):
                return
            out = QFileDialog.getExistingDirectory(self, "Select Animations Destination", str(cfg.FINAL_ROOT / "Exports"))
            if not out:
                return
            cat = r["cat"]
            if cat in ("characters", "bangboo"):
                self._run_basic_export(
                    f"Exporting Animations: {r['name']}",
                    lambda cb: export_character_animations_only(r["id"], self.game_root, Path(out), log_cb=cb),
                )
            else:
                self._run_basic_export(
                    f"Exporting Animations: {r['name']}",
                    lambda cb: export_entity_animations_only(r["id"], self.game_root, Path(out), category=cat, log_cb=cb),
                )

        def _basic_export_group(self):
            r = self._selected_basic_item()
            if not r or r["cat"] != "characters":
                return
            mem_ids = r.get("family_member_ids") or []
            if len(mem_ids) <= 1:
                return
            out = QFileDialog.getExistingDirectory(self, "Select Family Export Destination", str(cfg.FINAL_ROOT / "Exports"))
            if not out:
                return
            export_shaders = self.chk_basic_shaders.isChecked()
            self._run_basic_export(
                f"Exporting Family: {r['name']} ({len(mem_ids)} members)",
                lambda cb: export_character_family(
                    mem_ids, Path(out), convert_glb=True, log_cb=cb,
                    game_root=self.game_root, export_shaders=export_shaders,
                ),
            )

        def _refresh_current_view(self):
            self._refresh_version_badge()
            if self.current_mode == "dir" and self.current_dir and self.current_dir.exists():
                self.open_directory(self.current_dir)

        def _refresh_version_badge(self):
            v = cfg.collect_game_version(self.game_root)
            if v["cli_ready"]:
                cli_tag = "CLI Ready" if cfg.is_windows() else "CLI+Wine OK"
            elif not v["cli_exists"]:
                cli_tag = "CLI Missing (Click to Setup)"
            else:
                cli_tag = "Wine Missing (Click to Setup)"
            if v["blk_count"] == 0 and (v.get("cdn_workspace") or v.get("auto_cdn_pull")):
                reg = str(v.get("cdn_region") or "global").upper()
                pull_tag = "Auto-Pull ON" if v.get("auto_cdn_pull") else "Auto-Pull OFF"
                txt = f"On-Demand CDN ({reg}) | 0 local .blk ({pull_tag}) | {cli_tag}"
            else:
                cdn_tag = " (+CDN)" if v.get("auto_cdn_pull") else ""
                txt = f"{v['platform']} | Unity {v['unity_version']} | res {v['res_revision']} | {v['blk_count']} .blk{cdn_tag} | {cli_tag}"
            self.header_version_pill.setText(txt)

        def show_version_dialog(self):
            ToolsManagerDialog(self.game_root, self).exec()
            self._refresh_version_badge()

        def show_docs_dialog(self):
            dlg = QDialog(self)
            dlg.setWindowTitle("OpenZenless — CLI & GUI Documentation")
            _fit_to_screen(dlg, 820, 620)
            lay = QVBoxLayout(dlg)
            view = QTextBrowser()
            if cfg.DOC_FILE.exists():
                view.setMarkdown(cfg.DOC_FILE.read_text(encoding="utf-8"))
            else:
                view.setPlainText("README.md not found.")
            btn_close = QPushButton("Close")
            btn_close.clicked.connect(dlg.accept)
            lay.addWidget(view, 1)
            lay.addWidget(btn_close, alignment=Qt.AlignRight)
            dlg.exec()

        def _populate_tree(self):
            self.tree.clear()
            if self.files_root.exists():
                plat = cfg.detect_build_platform(self.game_root, self.files_root)
                lbl = f"Game Files ({plat})" if plat != "Unknown" else "Game Files (Root)"
                root_item = QTreeWidgetItem(self.tree, [lbl])
                root_item.setData(0, Qt.UserRole, ("dir", str(self.files_root)))
                root_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                root_item.setExpanded(True)
                self._populate_dir_node(root_item, self.files_root)

            for pc_data in (
                self.game_root / "ZenlessZoneZero_Data",
                self.game_root / "ZenlessZoneZero Game" / "ZenlessZoneZero_Data",
                self.files_root.parent if self.files_root.name == "StreamingAssets" else Path("/nonexistent"),
            ):
                if pc_data.exists() and pc_data.name == "ZenlessZoneZero_Data":
                    pc_item = QTreeWidgetItem(self.tree, ["ZenlessZoneZero_Data (PC)"])
                    pc_item.setData(0, Qt.UserRole, ("dir", str(pc_data)))
                    pc_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                    break

            apk_dir = self.game_root / "APKs" / "extracted"
            if apk_dir.exists():
                apk_item = QTreeWidgetItem(self.tree, ["APKs (extracted)"])
                apk_item.setData(0, Qt.UserRole, ("dir", str(apk_dir)))
                apk_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)

            if self.stage_blocks:
                st_root = QTreeWidgetItem(self.tree, [f"Stages ({len(self.stage_blocks)})"])
                for st in sorted(self.stage_blocks.keys()):
                    ch = QTreeWidgetItem(st_root, [f"{st} ({len(self.stage_blocks[st])} blks)"])
                    ch.setData(0, Qt.UserRole, ("stage", st))

            if self.char_blocks:
                ch_root = QTreeWidgetItem(self.tree, [f"Characters ({len(self.char_blocks)})"])
                for cname in sorted(self.char_blocks.keys()):
                    ch = QTreeWidgetItem(ch_root, [f"{cname} ({len(self.char_blocks[cname])} blks)"])
                    ch.setData(0, Qt.UserRole, ("char", cname))

        def _populate_dir_node(self, item: QTreeWidgetItem, path: Path):
            if item.childCount() > 0:
                return
            try:
                dirs = sorted([p for p in path.iterdir() if p.is_dir()], key=lambda p: p.name.lower())
            except OSError:
                return
            for d in dirs[:400]:
                child = QTreeWidgetItem(item, [d.name])
                child.setData(0, Qt.UserRole, ("dir", str(d)))
                child.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)

        def _on_tree_expanded(self, item: QTreeWidgetItem):
            meta = item.data(0, Qt.UserRole)
            if meta and meta[0] == "dir":
                self._populate_dir_node(item, Path(meta[1]))

        def _on_tree_clicked(self, item: QTreeWidgetItem, _col: int):
            meta = item.data(0, Qt.UserRole)
            if not meta:
                return
            kind, val = meta
            if kind == "dir":
                self.open_directory(Path(val))
            elif kind == "stage":
                self._open_block_group(f"Stage: {val}", self.stage_blocks.get(val, []))
            elif kind == "char":
                self._open_block_group(f"Character: {val}", self.char_blocks.get(val, []))

        def _reload_indexes(self):
            invalidate_index_cache()
            self.stage_blocks = self._load_stage_blocks()
            self.curated_maps = load_curated_map_index()
            self.char_blocks = self._load_char_blocks()
            self.char_names = load_character_names()
            self.entities_index = load_oz_entities_index()
            self.media_index = load_oz_media_index()
            self.audio_groups_cache = None
            self._populate_tree()
            self._populate_basic_categories()
            self._refresh_version_badge()

        def rebuild_asset_map(self):
            self._release_viewer_before_heavy_task("rebuilding asset map")
            force_full = bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)
            map_dir = cfg.CACHE_DIR / "asset_map"
            to_unlink = [
                "manifest.json",
                "zzz_assets_map.json",
                "oz_maps_index.json",
                "oz_characters_index.json",
                "oz_entities_index.json",
                "oz_media_index.json",
                "character_index_auto.json",
                "stage_index_auto.json",
                "fonts_index_auto.json",
                "shaders_index_auto.json",
            ]
            if force_full:
                to_unlink.extend(["oz_index_records.jsonl", "oz_index_blk_state.json"])
            for fname in to_unlink:
                p = map_dir / fname
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
            invalidate_index_cache()
            if self.files_root.exists():
                AssetMapDialog(self.game_root, self).exec()
            self._reload_indexes()

        def choose_root_folder(self):
            dlg = GameRootDialog(self.game_root, self)
            if dlg.exec() == QDialog.Accepted:
                self.game_root = dlg.selected_root
                self.files_root = cfg.resolve_files_root(self.game_root)
                has_blks = any(self.files_root.rglob("*.blk")) if self.files_root.exists() else False
                if not has_asset_map(self.game_root) and has_blks:
                    AssetMapDialog(self.game_root, self).exec()
                self._reload_indexes()
                if self.files_root.exists():
                    self.open_directory(self.files_root)

        def go_up(self):
            if self.current_mode == "bnk" and self.current_bnk_parent and self.current_bnk_parent.is_file():
                self.open_akpk(self.current_bnk_parent)
            elif self.current_mode in ("blk", "akpk", "bnk") and self.current_container:
                self.open_directory(self.current_container.parent)
            elif self.current_dir and self.current_dir.parent != self.current_dir:
                self.open_directory(self.current_dir.parent)

        def _on_path_entered(self):
            p = Path(self.path_bar.text().strip())
            if p.is_dir():
                self.open_directory(p)
            elif p.is_file():
                if p.suffix.lower() == ".blk":
                    self.open_blk(p)
                elif p.suffix.lower() == ".pck":
                    self.open_akpk(p)
                elif p.suffix.lower() == ".bnk":
                    self.open_bnk(p, str(p))

        def open_directory(self, path: Path):
            self.current_mode = "dir"
            self.current_dir = path
            self.current_container = None
            self.path_bar.setText(str(path))
            rows = []
            try:
                entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError as exc:
                QMessageBox.warning(self, "Read Error", str(exc))
                return
            for p in entries[:5000]:
                if p.is_dir():
                    rows.append({"name": p.name, "type": "Folder", "size": -1, "details": str(p), "path": str(p), "is_dir": True})
                else:
                    magic, _ = detect_magic(p)
                    st = p.stat()
                    rows.append({"name": p.name, "type": magic, "size": st.st_size, "details": str(p), "path": str(p), "is_dir": False})
            self._set_rows(rows)
            self.status.showMessage(f"Folder: {path} ({len(rows)} items)")

        def _open_block_group(self, label: str, blk_names: list[str]):
            self.current_mode = "group"
            self.current_container = None
            self.path_bar.setText(label)
            blocks_dir = self.files_root / "Blocks"
            rows = []
            seen: set[str] = set()
            for bn in blk_names:
                p = cfg.resolve_blk_path(self.game_root, str(bn))
                disp_name = p.name if p is not None else os.path.basename(str(bn))
                key_n = str(p) if p is not None else disp_name
                if key_n in seen:
                    continue
                seen.add(key_n)
                if p is not None and p.exists():
                    magic, _ = detect_magic(p)
                    rows.append({"name": p.name, "type": magic, "size": p.stat().st_size, "details": str(p), "path": str(p), "is_dir": False})
                else:
                    cand = blocks_dir / os.path.basename(str(bn))
                    rows.append({"name": os.path.basename(str(bn)), "type": "Missing .blk", "size": -1, "details": str(cand), "path": str(cand), "is_dir": False})
            self._set_rows(rows)
            self.status.showMessage(f"{label} — {len(rows)} .blk files")

        def _set_rows(self, rows: list[dict]):
            self.all_rows = rows
            types = sorted({r["type"] for r in rows})
            self.type_filter.blockSignals(True)
            self.type_filter.clear()
            self.type_filter.addItem("All Types")
            self.type_filter.addItems(types)
            self.type_filter.blockSignals(False)
            self._apply_filter()

        def _apply_filter(self):
            q = self.search_box.text().strip().lower()
            tf = self.type_filter.currentText()
            filtered = [
                r for r in self.all_rows
                if (tf == "All Types" or r["type"] == tf)
                and (not q or q in r["name"].lower() or q in r["type"].lower() or q in r.get("details", "").lower())
            ]
            self.table.setSortingEnabled(False)
            self.table.setRowCount(len(filtered))
            for i, r in enumerate(filtered):
                it_name = QTableWidgetItem(r["name"])
                it_name.setData(Qt.UserRole, r)
                if r.get("is_dir"):
                    it_name.setForeground(QColor("#fbbf24"))
                elif r["type"].startswith("ZZZ BLK"):
                    it_name.setForeground(QColor("#60a5fa"))
                elif r["type"].startswith("Wwise"):
                    it_name.setForeground(QColor("#34d399"))
                it_type = QTableWidgetItem(r["type"])
                it_size = QTableWidgetItem(human_size(r["size"]))
                it_size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                it_det = QTableWidgetItem(r.get("details", ""))
                self.table.setItem(i, 0, it_name)
                self.table.setItem(i, 1, it_type)
                self.table.setItem(i, 2, it_size)
                self.table.setItem(i, 3, it_det)
            self.table.setSortingEnabled(True)

        def _selected_row(self) -> dict | None:
            sel = self.table.selectedItems()
            if not sel:
                return None
            it = self.table.item(sel[0].row(), 0)
            return it.data(Qt.UserRole) if it else None

        def _on_table_selection(self):
            r = self._selected_row()
            if not r:
                self.btn_open_view.setEnabled(False)
                self.btn_export.setEnabled(False)
                self.btn_preview_tex.setVisible(False)
                self.btn_preview_tex.setEnabled(False)
                return
            p = Path(r["path"])
            self.btn_open_view.setEnabled(True)
            self.btn_export.setEnabled(self.current_mode in ("blk", "akpk", "bnk") or p.is_file())
            is_blk_tex = (
                self.current_mode == "blk"
                and r["type"] in ("Texture2D", "Sprite")
                and cfg.can_run_cli()
            )
            self.btn_preview_tex.setVisible(is_blk_tex)
            self.btn_preview_tex.setEnabled(is_blk_tex)

            if self.current_mode in ("dir", "group") and p.is_file():
                ext = p.suffix.lower()
                if ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
                    pix = QPixmap(str(p))
                    if not pix.isNull():
                        self.preview_info.setText(f"{p.name} — {pix.width()} x {pix.height()} px")
                        self.preview_img.setPixmap(pix.scaled(480, 480, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                        self.tabs.setCurrentIndex(1)
                if ext in (".txt", ".json", ".md", ".xml", ".ini", ".cfg", ".log", ".py", ".sh", ".csv", ".yaml", ".yml"):
                    try:
                        self.props_view.setPlainText(p.read_text(errors="replace")[:200_000])
                    except Exception as exc:
                        self.props_view.setPlainText(f"Failed to read file: {exc}")
                else:
                    magic, head = detect_magic(p)
                    info = [
                        f"File: {p.name}",
                        f"Path: {p}",
                        f"Size: {p.stat().st_size:,} bytes ({human_size(p.stat().st_size)})",
                        f"Detected Format: {magic}",
                        f"Magic Hex (16B): {' '.join(f'{b:02X}' for b in head[:16])}",
                    ]
                    if ext == ".pck" and head[:4] == b"AKPK":
                        ak = parse_akpk(p)
                        info.append(f"AKPK Version: {ak.get('version')}, Header Size: {ak.get('header_size')}")
                        info.append(f"Embedded Audio Entries: {len(ak.get('entries', []))}")
                    self.props_view.setPlainText("\n".join(info))
                self.hex_file = p
                self._load_hex_chunk(0)
            elif self.current_mode == "blk" and p.is_file():
                try:
                    self.props_view.setPlainText(p.read_text(errors="replace")[:200_000])
                except Exception as exc:
                    self.props_view.setPlainText(f"Failed to read JSON: {exc}")
                self.hex_file = p
                self._load_hex_chunk(0)
            elif self.current_mode == "akpk":
                info = [
                    f"Entry: {r['name']}",
                    f"Type: {r['type']}",
                    f"ID: {r.get('id')}",
                    f"Offset in .pck: 0x{r['offset']:X} ({r['offset']:,})",
                    f"Size: {r['size']:,} bytes ({human_size(r['size'])})",
                    f"Language ID: {r.get('lang_id')}",
                ]
                if r["type"] == "Wwise BNK":
                    info.append("Hint: Double-click or click Open/View to inspect embedded DIDX/DATA WEM streams.")
                self.props_view.setPlainText("\n".join(info))
                if self.current_container and self.current_container.exists():
                    self.hex_file = self.current_container
                    self._load_hex_chunk(r["offset"])
            elif self.current_mode == "bnk":
                info = [
                    f"Embedded WEM: {r['name']}",
                    f"WEM ID: {r.get('id')}",
                    f"Offset in .bnk: 0x{r['offset']:X} ({r['offset']:,})",
                    f"Size: {r['size']:,} bytes ({human_size(r['size'])})",
                ]
                self.props_view.setPlainText("\n".join(info))
                if self.current_container and self.current_container.exists():
                    self.hex_file = self.current_container
                    self._load_hex_chunk(self.current_bnk_parent_offset + r["offset"])

        def open_selected_item(self):
            r = self._selected_row()
            if not r:
                return
            p = Path(r["path"])
            if r.get("is_dir"):
                self.open_directory(p)
            elif self.current_mode == "akpk" and r["type"] == "Wwise BNK" and self.current_container and self.current_container.is_file():
                with self.current_container.open("rb") as fh:
                    fh.seek(r["offset"])
                    raw_bnk = fh.read(r["size"])
                self.open_bnk(
                    raw_bnk,
                    f"{self.current_container} :: {r['name']}",
                    parent_pck=self.current_container,
                    parent_offset=r["offset"],
                )
            elif p.is_file() and p.suffix.lower() == ".blk" and self.current_mode in ("dir", "group"):
                self.open_blk(p)
            elif p.is_file() and p.suffix.lower() == ".pck" and self.current_mode == "dir":
                self.open_akpk(p)
            elif p.is_file() and p.suffix.lower() == ".bnk" and self.current_mode == "dir":
                self.open_bnk(p, str(p))
            elif self.current_mode == "blk" and r["type"] in ("Texture2D", "Sprite"):
                self.preview_selected_texture()
            elif p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
                self.tabs.setCurrentIndex(1)
            elif p.is_file() and p.suffix.lower() in (".txt", ".json", ".md", ".xml", ".ini", ".cfg", ".log", ".py", ".sh", ".csv", ".yaml", ".yml"):
                self.tabs.setCurrentIndex(0)
            else:
                self.tabs.setCurrentIndex(2)

        def _on_table_double_click(self, _item: QTableWidgetItem):
            self.open_selected_item()

        def _load_hex_chunk(self, offset: int):
            if not self.hex_file or not self.hex_file.is_file():
                return
            self.hex_offset = max(0, offset)
            self.hex_offset_edit.setText(f"0x{self.hex_offset:X}")
            try:
                with self.hex_file.open("rb") as fh:
                    fh.seek(self.hex_offset)
                    chunk = fh.read(4096)
                self.hex_view.setPlainText(format_hex_dump(chunk, self.hex_offset))
            except OSError as exc:
                self.hex_view.setPlainText(f"Hex read error: {exc}")

        def _jump_hex_offset(self):
            s = self.hex_offset_edit.text().strip()
            try:
                off = int(s, 16) if s.lower().startswith("0x") else int(s)
                self._load_hex_chunk(off)
            except ValueError:
                QMessageBox.warning(self, "Invalid Offset", f"Could not parse offset: {s}")

        def open_akpk(self, pck_path: Path):
            parsed = parse_akpk(pck_path)
            if parsed.get("error"):
                QMessageBox.warning(self, "AKPK Parse Error", parsed["error"])
                return
            self.current_mode = "akpk"
            self.current_container = pck_path
            self.current_bnk_bytes = None
            self.current_bnk_parent = None
            self.current_bnk_parent_offset = 0
            self.path_bar.setText(str(pck_path))
            rows = [{
                "name": e["name"],
                "type": e["type"],
                "size": e["size"],
                "details": f"offset=0x{e['offset']:X} lang={e['lang_id']}",
                "path": str(pck_path),
                "offset": e["offset"],
                "id": e["id"],
                "lang_id": e["lang_id"],
            } for e in parsed["entries"]]
            self._set_rows(rows)
            self.status.showMessage(f"AKPK: {pck_path.name} — {len(rows)} embedded audio entries")

        def open_bnk(self, bnk_source: Path | bytes, label: str, parent_pck: Path | None = None, parent_offset: int = 0):
            raw = bnk_source.read_bytes() if isinstance(bnk_source, Path) else bnk_source
            parsed = parse_bnk(raw)
            if parsed.get("error"):
                QMessageBox.warning(self, "BNK Parse Error", parsed["error"])
                return
            self.current_mode = "bnk"
            self.current_bnk_bytes = raw
            self.current_bnk_parent = parent_pck
            self.current_bnk_parent_offset = parent_offset
            self.current_container = parent_pck or (bnk_source if isinstance(bnk_source, Path) else None)
            self.path_bar.setText(label)
            rows = [{
                "name": e["name"],
                "type": e["type"],
                "size": e["size"],
                "details": f"bnk_id={parsed.get('bank_id')} chunks={','.join(parsed.get('chunks', []))} offset=0x{e['offset']:X}",
                "path": str(self.current_container or label),
                "offset": e["offset"],
                "id": e["id"],
            } for e in parsed["entries"]]
            self._set_rows(rows)
            self.status.showMessage(f"BNK SoundBank ({len(rows)} embedded WEM streams)")

        def load_selected_blk(self):
            r = self._selected_row()
            if r and Path(r["path"]).suffix.lower() == ".blk":
                self.open_blk(Path(r["path"]))

        def open_blk(self, blk_path: Path):
            self.progress.setRange(0, 0)
            self.progress.setVisible(True)
            self.status.showMessage(f"Loading {blk_path.name} via AnimeStudio.CLI...")
            w = BlkInspectWorker(blk_path)
            w.finished_ok.connect(self._on_blk_loaded)
            w.failed.connect(self._on_worker_error)
            self.workers.append(w)
            w.start()

        def _on_blk_loaded(self, blk_str: str, rows: list, _log: str):
            self.progress.setVisible(False)
            blk_path = Path(blk_str)
            self.current_mode = "blk"
            self.current_container = blk_path
            self.path_bar.setText(blk_str)
            for r in rows:
                r["details"] = f"{blk_path.name} :: {r['type']}"
            self._set_rows(rows)
            self.status.showMessage(f"Loaded {blk_path.name}: {len(rows)} assets")

        def preview_selected_texture(self):
            r = self._selected_row()
            if not r or not self.current_container:
                return
            self.progress.setRange(0, 0)
            self.progress.setVisible(True)
            self.status.showMessage(f"Decoding {r['name']}...")
            w = AssetConvertWorker(self.current_container, r["type"], r["name"])
            w.finished_ok.connect(self._on_tex_ready)
            w.failed.connect(self._on_worker_error)
            self.workers.append(w)
            w.start()

        def _on_tex_ready(self, img_path: str, _log: str):
            self.progress.setVisible(False)
            pix = QPixmap(img_path)
            if pix.isNull():
                self.preview_info.setText(f"Extracted {img_path}, but QPixmap could not decode it.")
                return
            self.preview_info.setText(f"{Path(img_path).name} — {pix.width()} x {pix.height()} px")
            self.preview_img.setPixmap(pix.scaled(460, 460, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.tabs.setCurrentIndex(1)
            self.status.showMessage(f"Previewed {Path(img_path).name}")

        def export_selected_item(self):
            r = self._selected_row()
            if not r:
                return
            out_dir = QFileDialog.getExistingDirectory(self, "Select Export Destination", str(cfg.FINAL_ROOT))
            if not out_dir:
                return
            dest_root = Path(out_dir)
            if self.current_mode == "akpk" and self.current_container:
                dest = dest_root / r["name"]
                with self.current_container.open("rb") as src:
                    src.seek(r["offset"])
                    dest.write_bytes(src.read(r["size"]))
                self.status.showMessage(f"Exported {dest}")
            elif self.current_mode == "bnk" and self.current_bnk_bytes is not None:
                dest = dest_root / r["name"]
                dest.write_bytes(self.current_bnk_bytes[r["offset"] : r["offset"] + r["size"]])
                self.status.showMessage(f"Extracted BNK WEM -> {dest}")
            elif self.current_mode == "blk" and self.current_container:
                shutil.copy2(r["path"], dest_root / Path(r["path"]).name)
                self.status.showMessage(f"Copied JSON to {dest_root}")
            else:
                p = Path(r["path"])
                if p.is_file():
                    shutil.copy2(p, dest_root / p.name)
                    self.status.showMessage(f"Copied {p.name} to {dest_root}")

        def open_batch_export_dialog(self):
            self._release_viewer_before_heavy_task("starting export")
            dlg = ExportDialog(self)
            dlg.exec()

        def closeEvent(self, event):
            if hasattr(self, "viewer_widget"):
                self.viewer_widget.shutdown()
            super().closeEvent(event)

        def _on_worker_error(self, msg: str):
            self.progress.setVisible(False)
            self.status.showMessage("Error")
            QMessageBox.warning(self, "BlkExplorer Error", msg)

    cfg.ensure_stdio_handles()
    cfg.configure_gpu_env()
    safe_gl = cfg.should_use_safe_opengl()
    from PySide6.QtGui import QSurfaceFormat
    if safe_gl:
        QApplication.setAttribute(Qt.AA_UseSoftwareOpenGL, True)
    fmt = QSurfaceFormat()
    fmt.setRenderableType(QSurfaceFormat.OpenGL)
    fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
    fmt.setDepthBufferSize(24)
    fmt.setSamples(0 if safe_gl else 4)
    fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(fmt)
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)
    should_index = True
    if not cfg.has_saved_game_root() or not cfg.is_valid_game_root(root_path):
        dlg = GameRootDialog(root_path)
        if dlg.exec() == QDialog.Accepted:
            root_path = dlg.selected_root
        else:
            should_index = False
    files_root = cfg.resolve_files_root(root_path)
    if should_index and not has_asset_map(root_path) and files_root.exists():
        has_containers = any(files_root.rglob("*.blk")) or any(files_root.rglob("*.pck"))
        if has_containers or (cfg.EXTRACTOR_ROOT / "lib").is_dir():
            AssetMapDialog(root_path).exec()
    win = BlkExplorerWindow(root_path)
    win.show()
    win.raise_()
    win.activateWindow()
    rc = app.exec()
    for th in list(_detached_threads):
        if th.isRunning():
            th.wait(5000)
    sys.exit(rc)

