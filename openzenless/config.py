"""Configuration, path detection, and build/version inspection for OpenZenless."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = Path.home() / ".config" / "openzenless" / "config.json"


def load_user_config() -> dict:
    try:
        if CONFIG_FILE.is_file():
            return json.loads(CONFIG_FILE.read_text())
    except Exception:
        pass
    return {}


def save_user_config(cfg: dict):
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        cur = load_user_config()
        cur.update(cfg)
        CONFIG_FILE.write_text(json.dumps(cur, indent=2))
    except Exception:
        pass


def detect_default_game_root() -> Path:
    if os.environ.get("ZZZ_GAME_ROOT"):
        return Path(os.environ["ZZZ_GAME_ROOT"])
    cfg = load_user_config()
    if cfg.get("game_root") and Path(cfg["game_root"]).exists():
        return Path(cfg["game_root"])
    for cand in (
        Path("/home/bcan77/ZenlessZoneZero_v3.4_Android_Build"),
        Path.home() / "ZenlessZoneZero_v3.4_Android_Build",
        Path.home() / "ZenlessZoneZero Game",
    ):
        if cand.exists():
            return cand
    return Path.home()


def detect_default_final_root() -> Path:
    if os.environ.get("ZZZ_FINAL_OUTPUT"):
        return Path(os.environ["ZZZ_FINAL_OUTPUT"])
    cfg = load_user_config()
    if cfg.get("final_root") and Path(cfg["final_root"]).exists():
        return Path(cfg["final_root"])
    for cand in (
        APP_DIR / "FinalOutput",
        Path.cwd() / "FinalOutput",
        Path("/mnt/extra/zzz34_output/FinalOutput"),
        Path.home() / "OpenZenless_Output",
    ):
        if cand.exists():
            return cand
    return Path.home() / "OpenZenless_Output"


GAME_ROOT = detect_default_game_root()
FINAL_ROOT = detect_default_final_root()
EXTRACTOR_ROOT = FINAL_ROOT / "Extractor"
CLI_DIR = EXTRACTOR_ROOT / "tools" / "AnimeStudioUltimate" / "AnimeStudio.CLI" / "bin" / "Release" / "net9.0"
CLI_EXE = CLI_DIR / "AnimeStudio.CLI.exe"
CACHE_DIR = Path(os.environ.get("ZZZ_BLK_EXPLORER_CACHE", "/tmp/zzz_blk_explorer"))
ASSETS_DIR = APP_DIR / "assets"
LOGO_HEADER = ASSETS_DIR / "OpenZenless_Header.png"
LOGO_ICON = ASSETS_DIR / "OpenZenless_WhiteText.png"
SECTIONS_INDEX_FILE = APP_DIR / "texture_sections_index.json"
ANIMEWWISE_DIR = EXTRACTOR_ROOT / "tools" / "AnimeWwise"
NAP_MAP_FILE = ANIMEWWISE_DIR / "maps" / "nap.map"
VGMSTREAM_CLI = EXTRACTOR_ROOT / "tools" / "vgmstream" / "build" / "cli" / "vgmstream-cli"
DOC_FILE = APP_DIR / "README.md"


def _resolve_tool_paths():
    global CLI_DIR, CLI_EXE, ANIMEWWISE_DIR, NAP_MAP_FILE, VGMSTREAM_CLI
    cli_cands = []
    if os.environ.get("ANIMESTUDIO_CLI"):
        cli_cands.append(Path(os.environ["ANIMESTUDIO_CLI"]))
    cli_cands.extend([
        EXTRACTOR_ROOT / "tools" / "AnimeStudioUltimate" / "AnimeStudio.CLI" / "bin" / "Release" / "net9.0" / "AnimeStudio.CLI.exe",
        APP_DIR / "tools" / "AnimeStudioUltimate" / "AnimeStudio.CLI" / "bin" / "Release" / "net9.0" / "AnimeStudio.CLI.exe",
        APP_DIR / "tools" / "AnimeStudio.CLI.exe",
    ])
    which_cli = shutil.which("AnimeStudio.CLI.exe")
    if which_cli:
        cli_cands.append(Path(which_cli))
    CLI_EXE = next((p for p in cli_cands if p.is_file()), cli_cands[0])
    CLI_DIR = CLI_EXE.parent

    ww_cands = [
        EXTRACTOR_ROOT / "tools" / "AnimeWwise",
        APP_DIR / "tools" / "AnimeWwise",
    ]
    ANIMEWWISE_DIR = next((p for p in ww_cands if (p / "maps" / "nap.map").is_file()), ww_cands[0])
    NAP_MAP_FILE = ANIMEWWISE_DIR / "maps" / "nap.map"

    vgm_cands = [
        EXTRACTOR_ROOT / "tools" / "vgmstream" / "build" / "cli" / "vgmstream-cli",
        APP_DIR / "tools" / "vgmstream" / "build" / "cli" / "vgmstream-cli",
    ]
    which_vgm = shutil.which("vgmstream-cli")
    if which_vgm:
        vgm_cands.append(Path(which_vgm))
    VGMSTREAM_CLI = next((p for p in vgm_cands if p.is_file()), vgm_cands[0])


_resolve_tool_paths()


def set_final_root(new_root: Path):
    global FINAL_ROOT, EXTRACTOR_ROOT
    if new_root.name == "Extractor" and (new_root / "tools").exists():
        EXTRACTOR_ROOT = new_root
        FINAL_ROOT = new_root.parent
    else:
        FINAL_ROOT = new_root
        if (new_root / "Extractor").exists():
            EXTRACTOR_ROOT = new_root / "Extractor"
    _resolve_tool_paths()
    save_user_config({"final_root": str(FINAL_ROOT)})


def resolve_files_root(game_root: Path) -> Path:
    for cand in (
        game_root / "com.HoYoverse.Nap" / "files",
        game_root / "files",
        game_root / "ZenlessZoneZero_Data" / "StreamingAssets",
        game_root / "ZenlessZoneZero Game" / "ZenlessZoneZero_Data" / "StreamingAssets",
        game_root / "StreamingAssets",
    ):
        if cand.exists():
            return cand
    if (game_root / "Blocks").exists() or (game_root / "Audio").exists():
        return game_root
    if game_root.name in ("Blocks", "Audio") and (
        (game_root.parent / "Blocks").exists() or (game_root.parent / "Audio").exists()
    ):
        return game_root.parent
    return game_root


def detect_build_platform(game_root: Path, files_root: Path) -> str:
    s = f"{game_root} {files_root}"
    if (files_root / "Audio" / "Windows").exists() or "ZenlessZoneZero_Data" in s or (game_root / "ZenlessZoneZero_Data").exists():
        return "PC (Windows)"
    if (
        (files_root / "Audio" / "Android").exists()
        or "com.HoYoverse.Nap" in s
        or (files_root / "il2cpp").exists()
        or (files_root / "hib").exists()
    ):
        return "Android"
    return "Unknown"


def _read_small_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""


def _read_version_field(files_root: Path, name: str) -> str:
    for cand in (files_root / name, files_root.parent / name):
        if cand.is_file():
            val = _read_small_text(cand)
            if val:
                return val
    return "unknown"


def collect_game_version(game_root: Path) -> dict:
    files_root = resolve_files_root(game_root)
    blk_dir = files_root / "Blocks"
    audio_dir = files_root / "Audio"
    blk_count = len(list(blk_dir.rglob("*.blk"))) if blk_dir.exists() else 0
    pck_count = len(list(audio_dir.rglob("*.pck"))) if audio_dir.exists() else 0

    audio_lang = "unknown"
    for lang_cand in (files_root / "Audio" / "Android", files_root / "Audio" / "Windows"):
        if lang_cand.is_dir():
            audio_lang = ", ".join(sorted(p.name for p in lang_cand.iterdir() if p.is_dir())) or "present"
            break

    return {
        "platform": detect_build_platform(game_root, files_root),
        "game_root": str(game_root),
        "files_root": str(files_root),
        "exists": files_root.exists(),
        "unity_version": _read_version_field(files_root, "psoversioncode"),
        "base_revision": _read_version_field(files_root, "base_revision"),
        "res_revision": _read_version_field(files_root, "res_revision"),
        "data_revision": _read_version_field(files_root, "data_revision"),
        "audio_revision": _read_version_field(files_root, "audio_revision"),
        "silence_revision": _read_version_field(files_root, "silence_revision"),
        "op_version": _read_version_field(files_root, "op_version"),
        "audio_lang": audio_lang,
        "blk_count": blk_count,
        "pck_count": pck_count,
        "cli_exists": CLI_EXE.is_file(),
        "wine_exists": shutil.which("wine") is not None,
    }
