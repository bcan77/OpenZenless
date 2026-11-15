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
from .containers import detect_magic, format_hex_dump, human_size, parse_akpk, scan_blk_json
from .exporters import (
    export_audio_pck,
    export_blk_asset_types,
    export_character_animations_only,
    export_character_full,
    export_full_game,
    export_scene_map,
)
from .indexer import (
    build_asset_map,
    has_asset_map,
    load_character_index,
    load_character_names,
    load_curated_map_index,
    load_stage_index,
    load_texture_sections,
)
from .tools import ensure_extractor_tools
from .viewer3d import NativeViewerWidget


def launch_gui(root_path: Path):
    from PySide6.QtCore import Qt, QThread, Signal, QSize
    from PySide6.QtGui import QColor, QFont, QIcon, QKeySequence, QPixmap, QShortcut
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
        QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
        QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
        QProgressBar, QPushButton, QSpinBox, QSplitter, QStackedWidget,
        QStatusBar, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
        QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
    )

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
                if not cfg.CLI_EXE.exists() or shutil.which("wine") is None:
                    self.failed.emit("AnimeStudio.CLI.exe or wine is not available.")
                    return
                out_dir = cfg.CACHE_DIR / "blk_convert" / self.blk_path.stem / self.asset_type
                out_dir.mkdir(parents=True, exist_ok=True)
                for ext in (".png", ".tga", ".fbx", ".wav", ".ogg", ".txt", ".bytes"):
                    cand = list(out_dir.rglob(f"{self.asset_name}{ext}"))
                    if cand:
                        self.finished_ok.emit(str(cand[0]), "cached")
                        return
                env = os.environ.copy()
                env["WINEDEBUG"] = "-all"
                cmd = [
                    "wine", str(cfg.CLI_EXE),
                    "Z:" + str(self.blk_path).replace("/", "\\"),
                    "Z:" + str(out_dir).replace("/", "\\"),
                    "--game", "ZZZ",
                    "--types", self.asset_type,
                    "--names", self.asset_name,
                    "--group_assets", "ByType",
                    "--export_type", "Convert",
                ]
                proc = subprocess.run(cmd, cwd=cfg.CLI_DIR, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
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
            self.resize(680, 420)
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

        def closeEvent(self, event):
            if self.worker.isRunning():
                self.worker.cancel_event.set()
                self.worker.wait(1500)
            super().closeEvent(event)

    class ExportDialog(QDialog):
        SCOPES = [
            ("character", "Whole Character (FBX + Textures + Optional GLB)"),
            ("animations", "Character Animations Only (AnimationClip / Animator)"),
            ("map", "Curated Map (GLB Pipeline)"),
            ("stage", "Raw Stage (.blk Export / GLB)"),
            ("fonts", "Fonts (TTF / OTF from .blk)"),
            ("assets", "Custom Asset Types (Textures, Shaders, TextAsset, Mesh, ...)"),
            ("audio", "Wwise Audio (.pck -> Auto-Named Voices / WAV)"),
            ("full", "Full Export (Characters + Maps + Fonts + Voices)"),
        ]

        def __init__(self, parent: "BlkExplorerWindow"):
            super().__init__(parent)
            self.parent_win = parent
            self.worker: BatchExportWorker | None = None
            self.setWindowTitle("OpenZenless — Batch & Pipeline Export")
            self.resize(900, 680)
            lay = QVBoxLayout(self)

            tool_bar = QHBoxLayout()
            self.tool_status_lbl = QLabel()
            self.tool_status_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
            self.btn_setup_tools = QPushButton("Setup / Build Tools")
            self.btn_setup_tools.setToolTip("Automatically clone & build AnimeStudio.CLI and AnimeWwise into ~/.local/share/openzenless/tools")
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
            self.chk_resume = QCheckBox("Resume Map Checkpoints")
            self.chk_resume.setChecked(True)
            self.chk_dry = QCheckBox("Dry Run (Preview Full Plan)")
            self.spin_workers = QSpinBox()
            self.spin_workers.setRange(1, 8)
            self.spin_workers.setValue(1)
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

            full_row = QHBoxLayout()
            full_row.setContentsMargins(0, 4, 0, 0)
            full_row.setSpacing(14)
            full_row.addWidget(QLabel("Full Export:"))
            full_row.addWidget(self.chk_inc_chars)
            full_row.addWidget(self.chk_inc_scenes)
            full_row.addWidget(self.chk_inc_fonts)
            full_row.addWidget(self.chk_inc_audio)
            full_row.addStretch()
            opts_lay.addLayout(full_row, 2, 0, 1, 3)
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
            wine_ok = shutil.which("wine") is not None
            ext_ok = (cfg.EXTRACTOR_ROOT / "lib" / "extract_character_v2.py").is_file()
            vgm_ok = cfg.VGMSTREAM_CLI.is_file()
            self.tool_status_lbl.setText(
                f"Tools — CLI: {'OK' if cli_ok else 'MISSING'} | "
                f"Wine: {'OK' if wine_ok else 'MISSING'} | "
                f"Extractor Scripts: {'OK' if ext_ok else 'MISSING'} | "
                f"vgmstream: {'OK' if vgm_ok else 'MISSING'} | "
                f"Audio AKPK: Ready"
            )

        def _setup_tools(self):
            if self.worker is not None and self.worker.isRunning():
                return
            self.log_box.clear()
            self.btn_run.setEnabled(False)
            self.btn_setup_tools.setEnabled(False)

            def task(cb):
                res = ensure_extractor_tools(log_cb=cb)
                cb(f"Tools ready: {res}")
                return 0 if res.get("cli_exists") else 1

            self.worker = BatchExportWorker(task)
            self.worker.log_line.connect(self.log_box.appendPlainText)
            self.worker.finished_rc.connect(lambda rc: (
                self.btn_setup_tools.setEnabled(True),
                self._refresh_tool_status(),
                self.parent_win._refresh_version_badge(),
                self._on_done(rc),
            ))
            self.worker.failed.connect(lambda err: (
                self.btn_setup_tools.setEnabled(True),
                self._on_err(err),
            ))
            self.worker.start()

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
            cur = self.parent_win.current_container
            if scope == "full":
                default_src = str(self.parent_win.game_root)
            elif scope == "audio":
                default_src = (
                    str(cur)
                    if (cur and cur.is_file() and cur.suffix.lower() == ".pck")
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
            if scope in ("character", "animations"):
                self.target_combo.addItems(chars)
                if not chars and self.target_combo.lineEdit():
                    self.target_combo.lineEdit().setPlaceholderText("No characters mapped yet — check Game/Extractor root")
                self.target_combo.setEnabled(True)
                self.source_edit.setEnabled(False)
                self.btn_browse_file.setEnabled(False)
                self.btn_browse_src.setEnabled(False)
            elif scope == "map":
                self.target_combo.addItem("all")
                self.target_combo.addItems(curated_maps)
                self.target_combo.setEnabled(True)
                self.source_edit.setEnabled(False)
                self.btn_browse_file.setEnabled(False)
                self.btn_browse_src.setEnabled(False)
            elif scope == "stage":
                self.target_combo.addItems(stages)
                if not stages and self.target_combo.lineEdit():
                    self.target_combo.lineEdit().setPlaceholderText("No stages mapped yet — check Game/Extractor root")
                self.target_combo.setEnabled(True)
                self.source_edit.setEnabled(False)
                self.btn_browse_file.setEnabled(False)
                self.btn_browse_src.setEnabled(False)
            else:
                self.target_combo.addItem("(Uses Source Path)")
                self.target_combo.setEnabled(False)
                self.source_edit.setEnabled(scope != "full")
                self.btn_browse_file.setEnabled(scope != "full")
                self.btn_browse_src.setEnabled(scope != "full")

            self.types_edit.setEnabled(scope == "assets")
            self.mode_combo.setEnabled(scope == "assets")
            self.filter_edit.setEnabled(scope == "assets")
            self.chk_glb.setEnabled(scope in ("character", "full"))
            self.chk_shaders.setEnabled(scope in ("character", "map", "stage", "full"))
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
            if scope in ("character", "animations", "map", "stage") and not target:
                QMessageBox.warning(self, "Export Error", f"Please select or enter a Target for '{scope}' export.")
                return
            if scope in ("fonts", "assets", "audio") and (not src_text or not src.exists()):
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
                if scope in ("map", "stage"):
                    return export_scene_map(target, resume=resume, workers=workers, out_dir=out, log_cb=cb, game_root=game_root, export_shaders=export_shaders)
                if scope == "fonts":
                    return export_blk_asset_types(src, out / "Fonts", ["Font"], "Convert", log_cb=cb)
                if scope == "assets":
                    return export_blk_asset_types(src, out / "Assets", types or ["Texture2D"], mode, flt, log_cb=cb)
                if scope == "audio":
                    return export_audio_pck(
                        src, out / "Audio",
                        use_wwise_map=use_wwise, decode_wav=decode_wav, log_cb=cb,
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

    class GameRootDialog(QDialog):
        def __init__(self, initial_root: Path, parent=None):
            super().__init__(parent)
            self.setWindowTitle("OpenZenless — Select ZZZ Game Installation")
            self.resize(640, 230)
            self.selected_root = initial_root

            lay = QVBoxLayout(self)
            lay.setContentsMargins(18, 16, 18, 16)
            lay.setSpacing(10)

            title = QLabel("Locate Your Zenless Zone Zero Installation")
            title.setStyleSheet("font-size: 15px; font-weight: 700; color: #f59e0b;")
            lay.addWidget(title)

            desc = QLabel(
                "Choose your ZZZ game folder (for example: ZenlessZoneZero Game, "
                "com.HoYoverse.Nap, or its files/ folder) before indexing assets."
            )
            desc.setWordWrap(True)
            desc.setStyleSheet("color: #94a3b8;")
            lay.addWidget(desc)

            row = QHBoxLayout()
            self.path_edit = QLineEdit(str(initial_root) if cfg.is_valid_game_root(initial_root) else "")
            self.path_edit.setPlaceholderText("Select ZZZ game root folder...")
            self.path_edit.textChanged.connect(self._validate_path)
            btn_browse = QPushButton("Browse...")
            btn_browse.clicked.connect(self._browse)
            row.addWidget(self.path_edit, 1)
            row.addWidget(btn_browse)
            lay.addLayout(row)

            self.status_lbl = QLabel("")
            self.status_lbl.setWordWrap(True)
            lay.addWidget(self.status_lbl)

            btn_row = QHBoxLayout()
            btn_row.addStretch()
            self.btn_skip = QPushButton("Skip for now")
            self.btn_skip.clicked.connect(self.reject)
            self.btn_continue = QPushButton("Continue & Index")
            self.btn_continue.setObjectName("primaryBtn")
            self.btn_continue.clicked.connect(self._accept_root)
            btn_row.addWidget(self.btn_skip)
            btn_row.addWidget(self.btn_continue)
            lay.addLayout(btn_row)

            self._validate_path(self.path_edit.text())

        def _browse(self):
            start = self.path_edit.text().strip() or str(Path.home())
            d = QFileDialog.getExistingDirectory(self, "Select ZZZ Game Installation", start)
            if d:
                self.path_edit.setText(d)

        def _validate_path(self, text: str):
            p = Path(text.strip()) if text.strip() else None
            if p and cfg.is_valid_game_root(p):
                fr = cfg.resolve_files_root(p)
                plat = cfg.detect_build_platform(p, fr)
                self.status_lbl.setText(f"Detected valid ZZZ files ({plat}) at: {fr}")
                self.status_lbl.setStyleSheet("color: #4ade80;")
                self.btn_continue.setEnabled(True)
            else:
                self.status_lbl.setText("No ZZZ Blocks/ or .blk/.pck files detected in this folder yet.")
                self.status_lbl.setStyleSheet("color: #fbbf24;")
                self.btn_continue.setEnabled(False)

        def _accept_root(self):
            p = Path(self.path_edit.text().strip())
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
            self.resize(1500, 920)
            self.game_root = initial_root
            self.files_root = cfg.resolve_files_root(initial_root)
            self.current_dir: Path | None = None
            self.current_container: Path | None = None
            self.current_mode = "dir"
            self.all_rows: list[dict] = []
            self.hex_file: Path | None = None
            self.hex_offset = 0
            self.workers: list[QThread] = []

            self.stage_blocks = self._load_stage_blocks()
            self.curated_maps = load_curated_map_index()
            self.char_blocks = self._load_char_blocks()
            self.char_names = load_character_names()
            self.texture_sections = load_texture_sections()
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

            self.btn_open_folder = QPushButton("Open Root...")
            self.btn_open_folder.setToolTip("Choose ZZZ game root folder")
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
            self.mode_stack.setCurrentIndex(idx)
            self.btn_mode_basic.setChecked(idx == 0)
            self.btn_mode_adv.setChecked(idx == 1)
            self.btn_mode_viewer.setChecked(idx == 2)
            if idx == 2:
                self.viewer_widget.refresh_catalog()

        def _build_viewer_page(self) -> QWidget:
            self.viewer_widget = NativeViewerWidget(choose_output_cb=self._choose_output_root)
            return self.viewer_widget

        def _choose_output_root(self):
            d = QFileDialog.getExistingDirectory(self, "Select Export Output Root (containing Exports / Characters / Maps_Combined)", str(cfg.FINAL_ROOT))
            if d:
                self.apply_output_root(Path(d))

        def apply_output_root(self, new_root: Path):
            cfg.set_final_root(new_root)
            self.stage_blocks = self._load_stage_blocks()
            self.curated_maps = load_curated_map_index()
            self.char_blocks = self._load_char_blocks()
            self.char_names = load_character_names()
            self._populate_tree()
            self._populate_basic_categories()
            self._refresh_version_badge()
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
            self.basic_search.setPlaceholderText("Search category items (name, ID, section)...")
            self.basic_search.textChanged.connect(self._filter_basic_items)
            mid_lay.addWidget(self.basic_search)

            self.basic_table = QTableWidget(0, 3)
            self.basic_table.setHorizontalHeaderLabels(["Name / Section", "Count / Status", "Identifier"])
            self.basic_table.setAlternatingRowColors(True)
            self.basic_table.setSelectionBehavior(QTableWidget.SelectRows)
            self.basic_table.setEditTriggers(QTableWidget.NoEditTriggers)
            self.basic_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
            self.basic_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
            self.basic_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
            self.basic_table.setColumnWidth(0, 300)
            self.basic_table.itemSelectionChanged.connect(self._on_basic_item_selected)
            self.basic_table.itemDoubleClicked.connect(lambda _it: self._basic_inspect_in_advanced())
            mid_lay.addWidget(self.basic_table)
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
            self.chk_basic_shaders = QCheckBox("Export Shaders & Materials")
            btn_row.addWidget(self.btn_basic_inspect)
            btn_row.addWidget(self.btn_basic_export1)
            btn_row.addWidget(self.btn_basic_export2)
            btn_row.addWidget(self.chk_basic_shaders)
            d_lay.addLayout(btn_row)

            page.addWidget(detail)
            page.setSizes([230, 680, 560])
            return page

        def _populate_basic_categories(self):
            self.basic_cats.clear()
            pcks = list((self.files_root / "Audio").rglob("*.pck")) if (self.files_root / "Audio").exists() else []
            entries = [
                ("characters", f"Characters ({len(self.char_blocks)})"),
                ("maps", f"Curated Maps ({len(self.curated_maps)})"),
                ("textures", f"Texture2D Sections ({len(self.texture_sections)})"),
                ("audio", f"Audio Archives ({len(pcks)})"),
                ("fonts", "Fonts & Localization"),
            ]
            for key, label in entries:
                it = QListWidgetItem(label)
                it.setData(Qt.UserRole, key)
                self.basic_cats.addItem(it)
            self.basic_cats.setCurrentRow(0)

        def _on_basic_category_changed(self, row: int):
            it = self.basic_cats.item(row)
            if not it:
                return
            cat = it.data(Qt.UserRole)
            items: list[dict] = []
            if cat == "characters":
                for cid in sorted(self.char_blocks.keys()):
                    loc = self.char_names.get(cid, cid)
                    out_dir = cfg.FINAL_ROOT / "Characters" / cid
                    has_glb = (out_dir / "Model").exists() and any((out_dir / "Model").glob("*.glb"))
                    status = f"{len(self.char_blocks[cid])} .blk" + (" • GLB Ready" if has_glb else "")
                    items.append({"cat": cat, "name": loc, "status": status, "id": cid, "blocks": self.char_blocks[cid]})
            elif cat == "maps":
                for st in sorted(self.curated_maps.keys()):
                    glb = cfg.FINAL_ROOT / "Maps_Combined" / f"{st}_full.glb"
                    status = f"{len(self.curated_maps[st])} .blk" + (" • GLB Ready" if glb.exists() else "")
                    items.append({"cat": cat, "name": st, "status": status, "id": st, "blocks": self.curated_maps[st]})
            elif cat == "textures":
                for sec, info in sorted(self.texture_sections.items()):
                    blks = info.get("blocks", [])
                    items.append({"cat": cat, "name": sec, "status": f"{len(blks)} .blk", "id": sec, "blocks": blks, "samples": info.get("samples", [])})
            elif cat == "audio":
                audio_root = self.files_root / "Audio"
                if audio_root.exists():
                    for pck in sorted(audio_root.rglob("*.pck")):
                        rel = str(pck.relative_to(audio_root))
                        items.append({"cat": cat, "name": pck.name, "status": human_size(pck.stat().st_size), "id": rel, "path": str(pck)})
            elif cat == "fonts":
                items.append({
                    "cat": cat,
                    "name": "All Game Fonts (TTF / OTF)",
                    "status": "AnimeStudio CLI",
                    "id": str(self.files_root / "Blocks"),
                })
            self.basic_all_items = items
            self._filter_basic_items()

        def _filter_basic_items(self):
            q = self.basic_search.text().strip().lower()
            filtered = [
                r for r in self.basic_all_items
                if not q or q in r["name"].lower() or q in r["id"].lower() or q in r["status"].lower()
            ]
            self.basic_table.setSortingEnabled(False)
            self.basic_table.setRowCount(len(filtered))
            for i, r in enumerate(filtered):
                c0 = QTableWidgetItem(r["name"])
                c0.setData(Qt.UserRole, r)
                c1 = QTableWidgetItem(r["status"])
                c2 = QTableWidgetItem(r["id"])
                self.basic_table.setItem(i, 0, c0)
                self.basic_table.setItem(i, 1, c1)
                self.basic_table.setItem(i, 2, c2)
            self.basic_table.setSortingEnabled(True)
            if filtered:
                self.basic_table.selectRow(0)

        def _selected_basic_item(self) -> dict | None:
            sel = self.basic_table.selectedItems()
            if not sel:
                return None
            it = self.basic_table.item(sel[0].row(), 0)
            return it.data(Qt.UserRole) if it else None

        def _on_basic_item_selected(self):
            r = self._selected_basic_item()
            if not r:
                self.btn_basic_inspect.setEnabled(False)
                self.btn_basic_export1.setEnabled(False)
                self.btn_basic_export2.setVisible(False)
                return
            cat = r["cat"]
            self.basic_title.setText(r["name"])
            self.btn_basic_inspect.setEnabled(True)
            self.btn_basic_export1.setEnabled(True)
            self.btn_basic_export2.setVisible(cat == "characters")

            lines = [f"Category : {cat}", f"ID / Key : {r['id']}", f"Status   : {r['status']}"]
            if cat == "characters":
                self.btn_basic_export1.setText("Export Whole Character")
                self.btn_basic_export2.setText("Export Animations Only")
                cdir = cfg.FINAL_ROOT / "Characters" / r["id"]
                lines.append(f"Blocks   : {', '.join(r['blocks'][:15])}")
                if cdir.exists():
                    files = sorted(str(p.relative_to(cdir)) for p in cdir.rglob("*") if p.is_file())
                    lines.append(f"\nExported Files ({len(files)}):")
                    lines.extend(f"  - {f}" for f in files[:30])
            elif cat == "maps":
                self.btn_basic_export1.setText("Export Curated Map (GLB)")
                lines.append(f"Blocks ({len(r['blocks'])}): {', '.join(r['blocks'][:20])}")
            elif cat == "textures":
                self.btn_basic_export1.setText("Export Section Texture2Ds")
                lines.append(f"Blocks ({len(r['blocks'])}): {', '.join(r['blocks'][:20])}")
                if r.get("samples"):
                    lines.append("\nSample Asset Paths:")
                    lines.extend(f"  - {s}" for s in r["samples"])
            elif cat == "audio":
                self.btn_basic_export1.setText("Unpack .pck Audio")
                lines.append(f"File Path: {r['path']}")
            elif cat == "fonts":
                self.btn_basic_export1.setText("Export All Fonts (TTF/OTF)")
            self.basic_info.setPlainText("\n".join(lines))

        def _basic_inspect_in_advanced(self):
            r = self._selected_basic_item()
            if not r:
                return
            cat = r["cat"]
            self._switch_mode(1)
            if cat in ("characters", "maps", "textures"):
                self._open_block_group(f"{cat.title()}: {r['name']}", r.get("blocks", []))
            elif cat == "audio":
                self.open_akpk(Path(r["path"]))
            elif cat == "fonts":
                self.open_directory(self.files_root / "Blocks")

        def _run_basic_export(self, title: str, fn):
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

        def _basic_export_primary(self):
            r = self._selected_basic_item()
            if not r:
                return
            cat = r["cat"]
            out = QFileDialog.getExistingDirectory(self, "Select Export Destination", str(cfg.FINAL_ROOT / "Exports"))
            if not out:
                return
            out_dir = Path(out)
            export_shaders = self.chk_basic_shaders.isChecked()
            if cat == "characters":
                self._run_basic_export(
                    f"Exporting {r['name']}",
                    lambda cb: export_character_full(r["id"], out_dir=out_dir, convert_glb=True, log_cb=cb, game_root=self.game_root, export_shaders=export_shaders),
                )
            elif cat == "maps":
                self._run_basic_export(
                    f"Exporting Map {r['id']}",
                    lambda cb: export_scene_map(r["id"], resume=True, workers=1, out_dir=out_dir, log_cb=cb, game_root=self.game_root, export_shaders=export_shaders),
                )
            elif cat == "textures":
                blks = [self.files_root / "Blocks" / os.path.basename(b) for b in r.get("blocks", [])]
                blks = [b for b in blks if b.is_file()]
                def run_sec(cb):
                    rc = 0
                    for b in blks:
                        rc = export_blk_asset_types(b, out_dir / r["id"].replace("/", "_"), ["Texture2D", "Sprite"], "Convert", log_cb=cb)
                    return rc
                self._run_basic_export(f"Exporting Texture2D: {r['name']}", run_sec)
            elif cat == "audio":
                self._run_basic_export(
                    f"Unpacking {r['name']} (AnimeWwise Auto-Named)",
                    lambda cb: export_audio_pck(
                        Path(r["path"]), out_dir / "Audio",
                        use_wwise_map=True, decode_wav=cfg.VGMSTREAM_CLI.is_file(), log_cb=cb,
                    ),
                )
            elif cat == "fonts":
                self._run_basic_export(
                    "Exporting Fonts",
                    lambda cb: export_blk_asset_types(self.files_root / "Blocks", out_dir / "Fonts", ["Font"], "Convert", log_cb=cb),
                )

        def _basic_export_secondary(self):
            r = self._selected_basic_item()
            if not r or r["cat"] != "characters":
                return
            out = QFileDialog.getExistingDirectory(self, "Select Animations Destination", str(cfg.FINAL_ROOT / "Exports"))
            if not out:
                return
            self._run_basic_export(
                f"Exporting Animations: {r['name']}",
                lambda cb: export_character_animations_only(r["id"], self.game_root, Path(out), log_cb=cb),
            )

        def _refresh_current_view(self):
            self._refresh_version_badge()
            if self.current_mode == "dir" and self.current_dir and self.current_dir.exists():
                self.open_directory(self.current_dir)

        def _refresh_version_badge(self):
            v = cfg.collect_game_version(self.game_root)
            cli_tag = "CLI+Wine OK" if (v["cli_exists"] and v["wine_exists"]) else "CLI/Wine Missing"
            txt = f"{v['platform']} | Unity {v['unity_version']} | res {v['res_revision']} | {v['blk_count']} .blk | {cli_tag}"
            self.header_version_pill.setText(txt)

        def show_version_dialog(self):
            v = cfg.collect_game_version(self.game_root)
            lines = [
                f"Platform:         {v['platform']}",
                f"Game Root:        {v['game_root']}",
                f"Files Root:       {v['files_root']} ({'OK' if v['exists'] else 'MISSING'})",
                f"Unity Version:    {v['unity_version']}",
                f"Base Revision:    {v['base_revision']}",
                f"Res Revision:     {v['res_revision']}",
                f"Data Revision:    {v['data_revision']}",
                f"Audio Revision:   {v['audio_revision']}",
                f"Silence Revision: {v['silence_revision']}",
                f"OP Version:       {v['op_version']}",
                f"Audio Lang:       {v['audio_lang']}",
                f".blk Containers:  {v['blk_count']:,}",
                f".pck Archives:    {v['pck_count']:,}",
                f"AnimeStudio CLI:  {cfg.CLI_EXE} ({'OK' if v['cli_exists'] else 'MISSING'})",
                f"Wine Runtime:     {'OK' if v['wine_exists'] else 'MISSING'}",
            ]
            QMessageBox.information(self, "ZZZ Build & Environment Check", "\n".join(lines))

        def show_docs_dialog(self):
            dlg = QDialog(self)
            dlg.setWindowTitle("ZZZ BlkExplorer — CLI & GUI Documentation")
            dlg.resize(820, 620)
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
            self.stage_blocks = self._load_stage_blocks()
            self.curated_maps = load_curated_map_index()
            self.char_blocks = self._load_char_blocks()
            self.char_names = load_character_names()
            self._populate_tree()
            self._populate_basic_categories()
            self._refresh_version_badge()

        def rebuild_asset_map(self):
            map_dir = cfg.CACHE_DIR / "asset_map"
            for fname in (
                "manifest.json",
                "zzz_assets_map.json",
                "character_index_auto.json",
                "stage_index_auto.json",
                "fonts_index_auto.json",
            ):
                p = map_dir / fname
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
            if self.files_root.exists():
                AssetMapDialog(self.game_root, self).exec()
            self._reload_indexes()

        def choose_root_folder(self):
            d = QFileDialog.getExistingDirectory(self, "Select ZZZ Game Root", str(self.game_root))
            if d:
                self.game_root = Path(d)
                cfg.save_user_config({"game_root": str(self.game_root)})
                self.files_root = cfg.resolve_files_root(self.game_root)
                if not has_asset_map(self.game_root) and self.files_root.exists():
                    AssetMapDialog(self.game_root, self).exec()
                self._reload_indexes()
                self.open_directory(self.files_root)

        def go_up(self):
            if self.current_mode in ("blk", "akpk") and self.current_container:
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
            for bn in blk_names:
                p = blocks_dir / os.path.basename(bn)
                if p.exists():
                    magic, _ = detect_magic(p)
                    rows.append({"name": p.name, "type": magic, "size": p.stat().st_size, "details": str(p), "path": str(p), "is_dir": False})
                else:
                    rows.append({"name": os.path.basename(bn), "type": "Missing .blk", "size": -1, "details": str(p), "path": str(p), "is_dir": False})
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
            self.btn_export.setEnabled(self.current_mode in ("blk", "akpk") or p.is_file())
            is_blk_tex = (
                self.current_mode == "blk"
                and r["type"] in ("Texture2D", "Sprite")
                and cfg.CLI_EXE.exists()
                and shutil.which("wine") is not None
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
                self.props_view.setPlainText("\n".join(info))
                if self.current_container and self.current_container.exists():
                    self.hex_file = self.current_container
                    self._load_hex_chunk(r["offset"])

        def open_selected_item(self):
            r = self._selected_row()
            if not r:
                return
            p = Path(r["path"])
            if r.get("is_dir"):
                self.open_directory(p)
            elif p.is_file() and p.suffix.lower() == ".blk":
                self.open_blk(p)
            elif p.is_file() and p.suffix.lower() == ".pck":
                self.open_akpk(p)
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
            elif self.current_mode == "blk" and self.current_container:
                shutil.copy2(r["path"], dest_root / Path(r["path"]).name)
                self.status.showMessage(f"Copied JSON to {dest_root}")
            else:
                p = Path(r["path"])
                if p.is_file():
                    shutil.copy2(p, dest_root / p.name)
                    self.status.showMessage(f"Copied {p.name} to {dest_root}")

        def open_batch_export_dialog(self):
            dlg = ExportDialog(self)
            dlg.exec()

        def _on_worker_error(self, msg: str):
            self.progress.setVisible(False)
            self.status.showMessage("Error")
            QMessageBox.warning(self, "BlkExplorer Error", msg)

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
    sys.exit(app.exec())

