"""Configuration, path detection, and build/version inspection for OpenZenless."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path.home() / ".local" / "share" / "openzenless" / "tools"
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
    return Path.home()


def has_saved_game_root() -> bool:
    if os.environ.get("ZZZ_GAME_ROOT") and Path(os.environ["ZZZ_GAME_ROOT"]).exists():
        return True
    cfg = load_user_config()
    return bool(cfg.get("game_root") and Path(cfg["game_root"]).exists())


def is_valid_game_root(game_root: Path) -> bool:
    if not game_root.exists():
        return False
    files_root = resolve_files_root(game_root)
    if not files_root.is_dir():
        return False
    if (files_root / "Blocks").is_dir() or (files_root / "Audio").is_dir():
        return True
    return next(files_root.rglob("*.blk"), None) is not None or next(files_root.rglob("*.pck"), None) is not None


def detect_default_final_root() -> Path:
    if os.environ.get("ZZZ_FINAL_OUTPUT"):
        return Path(os.environ["ZZZ_FINAL_OUTPUT"])
    cfg = load_user_config()
    if cfg.get("final_root") and Path(cfg["final_root"]).exists():
        return Path(cfg["final_root"])
    for cand in (
        APP_DIR / "FinalOutput",
        Path.cwd() / "FinalOutput",
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
CACHE_DIR = Path(
    os.environ.get("ZZZ_BLK_EXPLORER_CACHE")
    or (Path.home() / ".local" / "share" / "openzenless" / "cache")
)
_LEGACY_TMP_CACHE = Path(tempfile.gettempdir()) / "zzz_blk_explorer"
if not (CACHE_DIR / "asset_map").exists() and (_LEGACY_TMP_CACHE / "asset_map").exists():
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copytree(_LEGACY_TMP_CACHE / "asset_map", CACHE_DIR / "asset_map", dirs_exist_ok=True)
    except Exception:
        pass
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
        TOOLS_DIR / "AnimeStudio" / "AnimeStudio.CLI" / "bin" / "Release" / "net9.0-windows" / "AnimeStudio.CLI.exe",
        TOOLS_DIR / "AnimeStudio" / "AnimeStudio.CLI" / "bin" / "Release" / "net9.0" / "AnimeStudio.CLI.exe",
        EXTRACTOR_ROOT / "tools" / "AnimeStudioUltimate" / "AnimeStudio.CLI" / "bin" / "Release" / "net9.0" / "AnimeStudio.CLI.exe",
        EXTRACTOR_ROOT / "tools" / "AnimeStudio" / "AnimeStudio.CLI" / "bin" / "Release" / "net9.0-windows" / "AnimeStudio.CLI.exe",
        APP_DIR / "tools" / "AnimeStudioUltimate" / "AnimeStudio.CLI" / "bin" / "Release" / "net9.0" / "AnimeStudio.CLI.exe",
        APP_DIR / "tools" / "AnimeStudio.CLI.exe",
    ])
    which_cli = shutil.which("AnimeStudio.CLI.exe")
    if which_cli:
        cli_cands.append(Path(which_cli))
    CLI_EXE = next((p for p in cli_cands if p.is_file()), cli_cands[0])
    CLI_DIR = CLI_EXE.parent

    ww_cands = [
        TOOLS_DIR / "AnimeWwise",
        EXTRACTOR_ROOT / "tools" / "AnimeWwise",
        APP_DIR / "tools" / "AnimeWwise",
    ]
    ANIMEWWISE_DIR = next((p for p in ww_cands if (p / "maps" / "nap.map").is_file()), ww_cands[0])
    NAP_MAP_FILE = ANIMEWWISE_DIR / "maps" / "nap.map"

    vgm_cands = [
        TOOLS_DIR / "vgmstream" / "build" / "cli" / "vgmstream-cli",
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


def find_blk_dirs(game_root: Path) -> list[Path]:
    """Return ordered list of Blocks directories (main files/Blocks plus APK split Blocks if present)."""
    files_root = resolve_files_root(game_root)
    dirs: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path):
        try:
            rp = p.resolve()
        except Exception:
            rp = p
        if p.is_dir() and rp not in seen:
            seen.add(rp)
            dirs.append(p)

    _add(files_root / "Blocks")
    for base in (game_root, files_root.parent, files_root.parent.parent):
        _add(base / "APKs" / "extracted" / "split_UnityDataAssetPack" / "assets" / "Blocks")
        _add(base / "split_UnityDataAssetPack" / "assets" / "Blocks")
    return dirs


def load_res_version_order(files_root: Path) -> dict[str, int]:
    """Parse res_version_persist / data_version_persist into {blk_rel_or_name: priority_index} (0 = newest)."""
    order: dict[str, int] = {}
    idx = 0
    for name in ("res_version_persist", "data_version_persist"):
        for cand in (files_root / name, files_root.parent / name):
            if not cand.is_file():
                continue
            try:
                data = json.loads(cand.read_text(encoding="utf-8", errors="ignore"))
                entries = data.get("files") if isinstance(data, dict) else None
                if not isinstance(entries, list):
                    continue
                for item in entries:
                    if not isinstance(item, dict):
                        continue
                    remote = str(item.get("remoteName") or "").replace("\\", "/")
                    if not remote.lower().endswith(".blk"):
                        continue
                    rel = remote.split("Blocks/", 1)[-1] if "Blocks/" in remote else remote.lstrip("/")
                    base = Path(rel).name
                    if rel not in order:
                        order[rel] = idx
                    if base not in order:
                        order[base] = idx
                    idx += 1
            except Exception:
                pass
    return order


def collect_ordered_blks(game_root: Path) -> tuple[Path, list[Path]]:
    """Collect all unique .blk files across main & APK Blocks dirs, ordered by res_version_persist priority."""
    files_root = resolve_files_root(game_root)
    blk_dirs = find_blk_dirs(game_root)
    order_map = load_res_version_order(files_root)

    if len(blk_dirs) > 1:
        try:
            common_base = Path(os.path.commonpath([str(d.resolve()) for d in blk_dirs]))
        except Exception:
            common_base = game_root.resolve()
    elif blk_dirs:
        common_base = blk_dirs[0].parent.resolve()
    else:
        common_base = files_root.resolve()

    by_rel: dict[str, tuple[int, float, Path]] = {}
    for bdir in blk_dirs:
        for p in bdir.rglob("*.blk"):
            if not p.is_file():
                continue
            try:
                rel_in_blocks = p.relative_to(bdir).as_posix()
            except Exception:
                rel_in_blocks = p.name
            try:
                rel_common = p.resolve().relative_to(common_base).as_posix()
            except Exception:
                rel_common = rel_in_blocks
            prio = order_map.get(rel_in_blocks, order_map.get(p.name, 999999))
            try:
                mt = p.stat().st_mtime
            except OSError:
                mt = 0.0
            prev = by_rel.get(rel_common)
            if prev is None or (prio, -mt) < (prev[0], prev[1]):
                by_rel[rel_common] = (prio, -mt, p)

    ordered = [
        item[2]
        for _, item in sorted(by_rel.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0]))
    ]
    return common_base, ordered


def resolve_blk_path(game_root: Path | None, blk_ref: str) -> Path | None:
    """Resolve a stored relative/basename .blk reference against game_root and its Blocks dirs."""
    if not blk_ref:
        return None
    p = Path(blk_ref)
    if p.is_file():
        return p
    root = game_root if game_root is not None else detect_default_game_root()
    norm = blk_ref.replace("\\", "/").lstrip("/")
    files_root = resolve_files_root(root)
    cands = [
        root / norm,
        files_root / norm,
        files_root.parent / norm,
    ]
    for bdir in find_blk_dirs(root):
        cands.append(bdir / norm)
        cands.append(bdir / Path(norm).name)
        if "Blocks/" in norm:
            cands.append(bdir / norm.split("Blocks/", 1)[1])
    for cand in cands:
        if cand.is_file():
            return cand
    return None


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
