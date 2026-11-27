"""Configuration, path detection, and build/version inspection for OpenZenless."""
from __future__ import annotations

import ctypes
import gc
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def is_windows() -> bool:
    return os.name == "nt"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def _resolve_config_dir() -> Path:
    legacy = Path.home() / ".config" / "openzenless"
    if is_windows():
        if legacy.exists():
            return legacy
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "OpenZenless"
        return Path.home() / "AppData" / "Roaming" / "OpenZenless"
    if is_macos():
        if legacy.exists():
            return legacy
        return Path.home() / "Library" / "Application Support" / "OpenZenless"
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg_cfg) / "openzenless") if xdg_cfg else legacy


def _resolve_data_dir() -> Path:
    legacy = Path.home() / ".local" / "share" / "openzenless"
    if is_windows():
        if legacy.exists():
            return legacy
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "OpenZenless"
        return Path.home() / "AppData" / "Local" / "OpenZenless"
    if is_macos():
        if legacy.exists():
            return legacy
        return Path.home() / "Library" / "Application Support" / "OpenZenless"
    xdg_data = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg_data) / "openzenless") if xdg_data else legacy


APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = _resolve_config_dir()
DATA_DIR = _resolve_data_dir()
TOOLS_DIR = DATA_DIR / "tools"
CONFIG_FILE = CONFIG_DIR / "config.json"


_STDIO_LOG_FH = None


def ensure_stdio_handles() -> None:
    """Redirect None sys.stdout / sys.stderr (e.g. under Windows pythonw.exe) to a log file."""
    global _STDIO_LOG_FH
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_env = os.environ.get("OPENZENLESS_LAUNCH_LOG", "").strip()
        log_path = Path(log_env) if log_env else (DATA_DIR / "openzenless.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if _STDIO_LOG_FH is None or _STDIO_LOG_FH.closed:
            _STDIO_LOG_FH = log_path.open("a", encoding="utf-8", errors="replace", buffering=1)
        if sys.stdout is None:
            sys.stdout = _STDIO_LOG_FH
        if sys.stderr is None:
            sys.stderr = _STDIO_LOG_FH
    except Exception:
        pass


ensure_stdio_handles()


def load_user_config() -> dict:
    try:
        if CONFIG_FILE.is_file():
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        pass
    return {}


def save_user_config(cfg: dict):
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        cur = load_user_config()
        cur.update(cfg)
        CONFIG_FILE.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    except Exception:
        pass


def record_export_dir(path: Path) -> None:
    """Persist up to 8 recent export destination directories in user config."""
    try:
        resolved = Path(path).expanduser().resolve()
    except Exception:
        resolved = Path(path).expanduser()
    target = str(resolved)
    cur = load_user_config()
    prev = cur.get("recent_export_dirs")
    items = [str(p) for p in prev if isinstance(p, str) and p.strip()] if isinstance(prev, list) else []
    deduped = [target]
    norm_seen = {os.path.normcase(target)}
    for item in items:
        nc = os.path.normcase(item)
        if nc not in norm_seen:
            norm_seen.add(nc)
            deduped.append(item)
        if len(deduped) >= 8:
            break
    save_user_config({"recent_export_dirs": deduped})


def get_export_search_roots() -> list[Path]:
    """Return deduplicated roots to search for exported models (FINAL_ROOT subdirs + recent export dirs)."""
    base = [
        FINAL_ROOT / "Exports",
        FINAL_ROOT / "Characters",
        FINAL_ROOT / "Maps_Combined",
        FINAL_ROOT,
    ]
    recents = load_user_config().get("recent_export_dirs")
    if isinstance(recents, list):
        for item in recents:
            if isinstance(item, str) and item.strip():
                base.append(Path(item))
    out: list[Path] = []
    seen: set[str] = set()
    for p in base:
        try:
            key = os.path.normcase(str(p.resolve()))
        except Exception:
            key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def is_auto_cdn_pull_enabled() -> bool:
    """Return True when missing .blk containers should be auto-pulled from HoYoverse CDN."""
    env = os.environ.get("OPENZENLESS_AUTO_CDN_PULL", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    val = load_user_config().get("auto_pull_cdn_blks")
    return True if val is None else bool(val)


def get_cdn_region() -> str:
    """Return configured HoYoverse CDN server region ('global' or 'cn')."""
    env_reg = os.environ.get("OPENZENLESS_CDN_REGION", "").strip().lower()
    if env_reg in ("global", "cn"):
        return env_reg
    reg = str(load_user_config().get("cdn_region") or "global").strip().lower()
    return reg if reg in ("global", "cn") else "global"


def set_cdn_preferences(auto_pull: bool | None = None, region: str | None = None) -> None:
    """Persist On-Demand CDN auto-pull toggle and server region."""
    upd: dict = {}
    if auto_pull is not None:
        upd["auto_pull_cdn_blks"] = bool(auto_pull)
    if region is not None and str(region).strip().lower() in ("global", "cn"):
        upd["cdn_region"] = str(region).strip().lower()
    if upd:
        save_user_config(upd)


def is_cdn_workspace_root(game_root: Path) -> bool:
    """Return True if game_root is an initialized OpenZenless On-Demand / CDN workspace."""
    if not game_root.exists():
        return False
    for cand in (
        game_root / ".openzenless_cdn_state.json",
        game_root / "ZenlessZoneZero_Data" / "StreamingAssets" / ".openzenless_cdn_state.json",
    ):
        if cand.is_file():
            return True
    return False


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
    if is_cdn_workspace_root(game_root):
        return True
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
    or (DATA_DIR / "cache")
)
_LEGACY_TMP_CACHE = Path(tempfile.gettempdir()) / "zzz_blk_explorer"
if not (CACHE_DIR / "asset_map").exists() and (_LEGACY_TMP_CACHE / "asset_map").exists():
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copytree(_LEGACY_TMP_CACHE / "asset_map", CACHE_DIR / "asset_map", dirs_exist_ok=True)
    except Exception:
        pass
ASSETS_DIR = APP_DIR / "assets"
TOON_PRESETS_DIR = ASSETS_DIR / "toon_presets"
LOGO_HEADER = ASSETS_DIR / "OpenZenless_Header.png"
LOGO_ICON = ASSETS_DIR / "OpenZenless_WhiteText.png"
SECTIONS_INDEX_FILE = APP_DIR / "texture_sections_index.json"
ANIMEWWISE_DIR = EXTRACTOR_ROOT / "tools" / "AnimeWwise"
NAP_MAP_FILE = ANIMEWWISE_DIR / "maps" / "nap.map"
VGMSTREAM_CLI = TOOLS_DIR / "vgmstream" / ("vgmstream-cli.exe" if is_windows() else "vgmstream-cli")
DOC_FILE = APP_DIR / "README.md"


def _first_existing_exe(
    env_var: str | None,
    direct_cands: list[Path],
    which_names: tuple[str, ...] = (),
) -> str | None:
    if env_var:
        ev = os.environ.get(env_var, "").strip()
        if ev and Path(ev).is_file():
            return str(Path(ev))
    for cand in direct_cands:
        if cand.is_file():
            return str(cand)
    for wname in which_names:
        found = shutil.which(wname)
        if found:
            return found
    return None


def find_wine() -> str | None:
    """Return path to Wine executable on POSIX, or None on Windows/if missing."""
    if is_windows():
        return None
    cands = [
        TOOLS_DIR / "wine" / "bin" / "wine",
        TOOLS_DIR / "wine" / "bin" / "wine64",
        Path("/opt/wine-staging/bin/wine"),
        Path("/opt/homebrew/bin/wine64"),
        Path("/usr/local/bin/wine64"),
    ]
    return _first_existing_exe("OPENZENLESS_WINE", cands, ("wine", "wine64"))


def find_adb() -> str | None:
    """Return path to `adb` / `adb.exe` across portable tools, PATH, and Android SDK dirs."""
    exe = "adb.exe" if is_windows() else "adb"
    cands = [TOOLS_DIR / "platform-tools" / exe]
    for sdk_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        val = os.environ.get(sdk_var, "").strip()
        if val:
            cands.append(Path(val) / "platform-tools" / exe)
    cands.append(Path.home() / "Android" / "Sdk" / "platform-tools" / exe)
    if is_windows():
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            cands.append(Path(local_app) / "Android" / "Sdk" / "platform-tools" / exe)
    elif is_macos():
        cands.append(Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / exe)
    return _first_existing_exe("OPENZENLESS_ADB", cands, (exe, "adb"))


def find_dotnet() -> str | None:
    """Return path to `dotnet` / `dotnet.exe`."""
    exe = "dotnet.exe" if is_windows() else "dotnet"
    cands = [
        TOOLS_DIR / "dotnet" / exe,
        Path.home() / ".dotnet" / exe,
        Path("/usr/share/dotnet") / exe,
    ]
    if is_windows():
        for pf_var in ("ProgramFiles", "ProgramFiles(x86)"):
            pf = os.environ.get(pf_var, "").strip()
            if pf:
                cands.append(Path(pf) / "dotnet" / exe)
    elif is_macos():
        cands.extend([Path("/usr/local/share/dotnet") / exe, Path("/opt/homebrew/opt/dotnet/bin") / exe])
    return _first_existing_exe("OPENZENLESS_DOTNET", cands, (exe, "dotnet"))


def find_blender() -> str | None:
    """Return path to `blender` across env override, portable dir, PATH, and OS install dirs."""
    exe = "blender.exe" if is_windows() else "blender"
    cands = [TOOLS_DIR / "blender" / exe]
    if (TOOLS_DIR / "blender").is_dir():
        cands.extend(sorted((TOOLS_DIR / "blender").glob(f"*/{exe}")))
    if is_windows():
        for pf_var in ("ProgramFiles",):
            pf = os.environ.get(pf_var, r"C:\Program Files")
            bf_root = Path(pf) / "Blender Foundation"
            if bf_root.is_dir():
                cands.extend(sorted(bf_root.glob(f"Blender*/{exe}"), reverse=True))
    elif is_macos():
        cands.append(Path("/Applications/Blender.app/Contents/MacOS/Blender"))
    else:
        cands.extend([Path("/snap/bin/blender"), Path.home() / ".local" / "bin" / "blender"])
    return _first_existing_exe("BLENDER_BIN", cands, (exe, "blender"))


def find_vgmstream() -> Path | None:
    """Return path to `vgmstream-cli` / `vgmstream-cli.exe` if installed."""
    exe = "vgmstream-cli.exe" if is_windows() else "vgmstream-cli"
    cands = [
        TOOLS_DIR / "vgmstream" / exe,
        TOOLS_DIR / "vgmstream" / "build" / "cli" / exe,
        EXTRACTOR_ROOT / "tools" / "vgmstream" / exe,
        EXTRACTOR_ROOT / "tools" / "vgmstream" / "build" / "cli" / exe,
        APP_DIR / "tools" / "vgmstream" / exe,
    ]
    found = _first_existing_exe(None, cands, (exe, "vgmstream-cli"))
    return Path(found) if found else None


def _resolve_tool_paths():
    global CLI_DIR, CLI_EXE, ANIMEWWISE_DIR, NAP_MAP_FILE, VGMSTREAM_CLI
    cli_cands = []
    if os.environ.get("ANIMESTUDIO_CLI"):
        cli_cands.append(Path(os.environ["ANIMESTUDIO_CLI"]))
    cli_cands.extend([
        TOOLS_DIR / "AnimeStudio" / "AnimeStudio.CLI" / "bin" / "Release" / "net9.0-windows" / "publish" / "AnimeStudio.CLI.exe",
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

    vgm_exe = "vgmstream-cli.exe" if is_windows() else "vgmstream-cli"
    VGMSTREAM_CLI = find_vgmstream() or (TOOLS_DIR / "vgmstream" / vgm_exe)


_resolve_tool_paths()


def can_run_cli() -> bool:
    """Return True if AnimeStudio.CLI.exe exists and can run natively (Windows) or via Wine."""
    return CLI_EXE.is_file() and (is_windows() or find_wine() is not None)


def to_cli_path(p: Path | str) -> str:
    """Convert a host filesystem path to a native Windows path or Wine Z: path."""
    resolved = str(Path(p).resolve())
    if is_windows():
        return resolved
    return ("Z:" + resolved).replace("/", "\\")


def build_cli_cmd(args: list[str]) -> list[str]:
    """Build an AnimeStudio.CLI invocation command for Windows native or POSIX Wine."""
    if is_windows():
        return [str(CLI_EXE), *args]
    wine = find_wine()
    if not wine:
        raise FileNotFoundError(
            "wine executable not found (install Wine via Tools & Dependencies or `openzenless setup-tools --wine`)."
        )
    return [wine, str(CLI_EXE), *args]


def build_cli_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build subprocess environment with WINEDEBUG suppression and portable tool PATH entries."""
    env = os.environ.copy()
    if not is_windows():
        env.setdefault("WINEDEBUG", "-all")
    prepend: list[str] = []
    for d in (
        TOOLS_DIR / "wine" / "bin",
        TOOLS_DIR / "dotnet",
        TOOLS_DIR / "platform-tools",
        TOOLS_DIR / "vgmstream",
        TOOLS_DIR / "blender",
    ):
        if d.is_dir():
            prepend.append(str(d))
    if prepend:
        cur = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(prepend + ([cur] if cur else []))
    if extra:
        env.update(extra)
    return env


def subprocess_no_window_kwargs() -> dict:
    """Return subprocess keyword args to suppress console popups on Windows."""
    if is_windows() and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


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
        game_root / "com.miHoYo.Nap" / "files",
        game_root / "files",
        game_root / "ZenlessZoneZero_Data" / "StreamingAssets",
        game_root / "ZenlessZoneZero Game" / "ZenlessZoneZero_Data" / "StreamingAssets",
        game_root / "ZenlessZoneZero" / "ZenlessZoneZero_Data" / "StreamingAssets",
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
    _add(game_root / "ZenlessZoneZero_Data" / "StreamingAssets" / "Blocks")
    for base in (game_root, files_root.parent, files_root.parent.parent):
        _add(base / "APKs" / "extracted" / "split_UnityDataAssetPack" / "assets" / "Blocks")
        _add(base / "split_UnityDataAssetPack" / "assets" / "Blocks")
    return dirs


_RES_ORDER_CACHE: dict[str, tuple[tuple[tuple[str, int, int], ...], dict[str, int]]] = {}
_BLK_COLLECT_CACHE: dict[str, tuple[float, Path, list[Path], dict[str, tuple[int, float]]]] = {}


def invalidate_blk_cache() -> None:
    """Clear cached res_version_persist and collected .blk directory listings."""
    _RES_ORDER_CACHE.clear()
    _BLK_COLLECT_CACHE.clear()


def load_res_version_order(files_root: Path) -> dict[str, int]:
    """Parse res_version_persist / data_version_persist into {blk_rel_or_name: priority_index} (0 = newest)."""
    cands: list[Path] = []
    sig_parts: list[tuple[str, int, int]] = []
    for name in ("res_version_persist", "data_version_persist"):
        for cand in (files_root / name, files_root.parent / name):
            if cand.is_file():
                try:
                    st = cand.stat()
                    sig_parts.append((str(cand), st.st_mtime_ns, st.st_size))
                    cands.append(cand)
                except OSError:
                    pass
    sig = tuple(sig_parts)
    cache_k = str(files_root)
    cached = _RES_ORDER_CACHE.get(cache_k)
    if cached is not None and cached[0] == sig:
        return cached[1]

    order: dict[str, int] = {}
    idx = 0
    for cand in cands:
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
    _RES_ORDER_CACHE[cache_k] = (sig, order)
    return order


def collect_ordered_blks_with_prio(
    game_root: Path,
    use_cache: bool = True,
) -> tuple[Path, list[Path], dict[str, tuple[int, float]]]:
    """Collect ordered unique .blk paths plus O(1) `{blk_rel_or_name: (prio, -mtime)}` map."""
    try:
        cache_k = str(game_root.resolve())
    except Exception:
        cache_k = str(game_root)
    now = time.monotonic()
    if use_cache:
        cached = _BLK_COLLECT_CACHE.get(cache_k)
        if cached is not None and (now - cached[0]) < 15.0:
            return cached[1], list(cached[2]), dict(cached[3])

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

    ordered: list[Path] = []
    prio_lookup: dict[str, tuple[int, float]] = {}
    for rel_common, (prio, neg_mt, p) in sorted(by_rel.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0])):
        ordered.append(p)
        pv = (prio, neg_mt)
        prio_lookup.setdefault(rel_common, pv)
        prio_lookup.setdefault(p.name, pv)
        if "/Blocks/" in rel_common:
            prio_lookup.setdefault("Blocks/" + rel_common.split("/Blocks/", 1)[1], pv)
        elif rel_common.startswith("Blocks/"):
            prio_lookup.setdefault(rel_common, pv)
    _BLK_COLLECT_CACHE[cache_k] = (now, common_base, ordered, prio_lookup)
    return common_base, ordered, prio_lookup


def collect_ordered_blks(game_root: Path) -> tuple[Path, list[Path]]:
    """Collect all unique .blk files across main & APK Blocks dirs, ordered by res_version_persist priority."""
    common_base, ordered, _ = collect_ordered_blks_with_prio(game_root)
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
        or "com.miHoYo.Nap" in s
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

    wine_path = find_wine()
    wine_ok = True if is_windows() else (wine_path is not None)
    runtime_label = (
        "Native (Windows)"
        if is_windows()
        else ("Wine OK" if wine_path else "Wine MISSING")
    )

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
        "cli_ready": can_run_cli(),
        "wine_exists": wine_ok,
        "runtime_label": runtime_label,
        "adb_exists": find_adb() is not None,
        "vgmstream_exists": VGMSTREAM_CLI.is_file(),
        "blender_exists": find_blender() is not None,
        "cdn_workspace": is_cdn_workspace_root(game_root),
        "auto_cdn_pull": is_auto_cdn_pull_enabled(),
        "cdn_region": get_cdn_region(),
    }


_MEM_CACHE: tuple[float, float, float] | None = None
_GPU_INFO_CACHE: dict[str, str] | None = None


def detect_memory_gb() -> tuple[float, float]:
    """Return `(total_ram_gib, available_ram_gib)` across Linux, Windows, and macOS."""
    global _MEM_CACHE
    now = time.monotonic()
    if _MEM_CACHE is not None and (now - _MEM_CACHE[0]) < 3.0:
        return _MEM_CACHE[1], _MEM_CACHE[2]
    total_b = 0
    avail_b = 0
    try:
        if is_linux() and Path("/proc/meminfo").is_file():
            for ln in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
                if ln.startswith("MemTotal:"):
                    total_b = int(ln.split()[1]) * 1024
                elif ln.startswith("MemAvailable:"):
                    avail_b = int(ln.split()[1]) * 1024
        elif is_windows():
            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_uint32),
                    ("dwMemoryLoad", ctypes.c_uint32),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("sullAvailExtendedVirtual", ctypes.c_uint64),
                ]
            st = _MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                total_b = int(st.ullTotalPhys)
                avail_b = int(st.ullAvailPhys)
        elif hasattr(os, "sysconf"):
            pages = os.sysconf("SC_PHYS_PAGES")
            psize = os.sysconf("SC_PAGE_SIZE")
            total_b = int(pages) * int(psize)
            try:
                avail_pages = os.sysconf("SC_AVPHYS_PAGES")
                avail_b = int(avail_pages) * int(psize)
            except Exception:
                avail_b = int(total_b * 0.65)
    except Exception:
        pass
    if total_b <= 0:
        total_b = 8 * (1024 ** 3)
    if avail_b <= 0:
        avail_b = int(total_b * 0.6)
    tot_gb = round(total_b / (1024 ** 3), 2)
    av_gb = round(avail_b / (1024 ** 3), 2)
    _MEM_CACHE = (now, tot_gb, av_gb)
    return tot_gb, av_gb


def detect_gpu_info() -> dict[str, str]:
    """Detect GPU vendor/driver profile without spawning subprocesses."""
    global _GPU_INFO_CACHE
    if _GPU_INFO_CACHE is not None:
        return dict(_GPU_INFO_CACHE)
    vendor = "Unknown"
    driver = ""
    has_discrete = "0"
    if is_linux():
        if os.path.exists("/proc/driver/nvidia/version"):
            vendor = "NVIDIA"
            driver = "nvidia"
            has_discrete = "1"
        else:
            cards: list[tuple[str, str]] = []
            try:
                for dev_dir in sorted(Path("/sys/class/drm").glob("card[0-9]*/device")):
                    vf = dev_dir / "vendor"
                    bf = dev_dir / "boot_vga"
                    if vf.is_file():
                        vend = vf.read_text(encoding="utf-8", errors="ignore").strip().lower()
                        boot = bf.read_text(encoding="utf-8", errors="ignore").strip() if bf.is_file() else ""
                        cards.append((vend, boot))
            except Exception:
                pass
            vend_names = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel", "0x1af4": "VirtIO"}
            if cards:
                v0 = cards[0][0]
                vendor = vend_names.get(v0, v0)
                for v, b in cards:
                    if b == "0" and v in ("0x10de", "0x1002", "0x8086"):
                        has_discrete = "1"
                        vendor = vend_names.get(v, v)
                        break
                    if len(cards) >= 2 and cards[0][0] == "0x8086" and v in ("0x1002", "0x10de"):
                        has_discrete = "1"
                        vendor = vend_names.get(v, v)
                        break
                driver = "mesa" if vendor in ("AMD", "Intel", "VirtIO") else ""
    elif is_windows():
        vendor = "DirectX/WGL"
        driver = "windows"
    elif is_macos():
        vendor = "Apple Metal/GL"
        driver = "cocoa"
    _GPU_INFO_CACHE = {"vendor": vendor, "driver": driver, "has_discrete": has_discrete}
    return dict(_GPU_INFO_CACHE)


def release_os_memory() -> None:
    """Reclaim freed Python and C-heap memory across Linux and Windows."""
    gc.collect()
    if is_linux():
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
    elif is_windows():
        try:
            ctypes.windll.kernel32.SetProcessWorkingSetSize(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.c_size_t(-1),
                ctypes.c_size_t(-1),
            )
        except Exception:
            pass
        try:
            ctypes.CDLL("ucrtbase.dll")._heapmin()
        except Exception:
            pass


def should_use_safe_opengl() -> bool:
    """Return True when MSAA / desktop OpenGL should fall back to software/0-sample mode (RDP, VMs, safe flag)."""
    safe_env = os.environ.get("OPENZENLESS_SAFE_GL", "").strip().lower()
    if safe_env in ("1", "true", "yes", "on"):
        return True
    gpu_pref = os.environ.get("OPENZENLESS_GPU", "").strip().lower()
    if gpu_pref in ("cpu", "0", "off", "software"):
        return True
    if os.environ.get("QT_OPENGL", "").strip().lower() == "software":
        return True
    if is_windows():
        if os.environ.get("SESSIONNAME", "").strip().upper().startswith("RDP-"):
            return True
        try:
            # SM_REMOTESESSION = 0x1000
            if ctypes.windll.user32.GetSystemMetrics(0x1000) != 0:
                return True
        except Exception:
            pass
    return False


def configure_gpu_env(apply: bool = True) -> dict[str, str]:
    """Return (and optionally apply) discrete GPU PRIME offload, Mesa threading & Wayland compatibility env vars."""
    ensure_stdio_handles()
    env_vars: dict[str, str] = {}
    gpu_pref = os.environ.get("OPENZENLESS_GPU", "").strip().lower()
    if is_windows():
        if should_use_safe_opengl():
            env_vars.setdefault("QT_OPENGL", "software")
    elif is_linux():
        if os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
            if os.environ.get("OPENZENLESS_WAYLAND", "").strip().lower() not in ("1", "true", "yes"):
                env_vars["QT_QPA_PLATFORM"] = "xcb"

        qpa = (env_vars.get("QT_QPA_PLATFORM") or os.environ.get("QT_QPA_PLATFORM", "")).strip().lower()
        if not os.environ.get("PYOPENGL_PLATFORM"):
            if qpa.startswith("xcb") or (os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")):
                env_vars["PYOPENGL_PLATFORM"] = "glx"
            elif qpa.startswith("wayland"):
                env_vars["PYOPENGL_PLATFORM"] = "egl"

        gpu = detect_gpu_info()
        if gpu_pref in ("cpu", "0", "off", "software"):
            env_vars.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
            env_vars.setdefault("QT_OPENGL", "software")
        elif gpu_pref not in ("igpu",):
            if gpu.get("driver") == "nvidia":
                env_vars.update({
                    "__NV_PRIME_RENDER_OFFLOAD": "1",
                    "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
                    "__VK_LAYER_NV_optimus": "NVIDIA_only",
                })
            elif gpu.get("has_discrete") == "1":
                env_vars["DRI_PRIME"] = "1"
            if gpu.get("driver") == "mesa" and not os.environ.get("mesa_glthread"):
                env_vars["mesa_glthread"] = "true"
    if apply:
        for k, v in env_vars.items():
            os.environ.setdefault(k, v)
    return env_vars


def scan_existing_game_installs() -> list[Path]:
    """Probe common OS locations for existing ZZZ PC or Android builds."""
    home = Path.home()
    cands: list[Path] = [
        home / "OpenZenless_ZZZ_PC",
        home / "OpenZenless_ZZZ_Android",
        home / "ZZZ_Android",
        home / "ZZZ_PC",
        home / "ZenlessZoneZero Game",
        home / "Downloads" / "ZenlessZoneZero Game",
    ]
    if is_windows():
        for drv in ("C:", "D:", "E:", "F:", "G:"):
            base = Path(f"{drv}/")
            if not base.exists():
                continue
            cands.extend([
                base / "HoYoPlay" / "games" / "ZenlessZoneZero Game",
                base / "Program Files" / "HoYoPlay" / "games" / "ZenlessZoneZero Game",
                base / "Program Files" / "ZenlessZoneZero Game",
                base / "Games" / "ZenlessZoneZero Game",
                base / "ZenlessZoneZero Game",
            ])
    elif is_macos():
        cands.extend([
            home / "Games" / "ZenlessZoneZero Game",
            home / "Applications" / "ZenlessZoneZero Game",
        ])
    else:
        cands.extend([
            home / "Games" / "zenless-zone-zero",
            home / "Games" / "ZenlessZoneZero Game",
            home / "Games" / "sleepy-launcher",
            home / ".local" / "share" / "sleepy-launcher" / "game",
            home / "Games" / "Heroic" / "ZenlessZoneZero Game",
            home / ".wine" / "drive_c" / "Program Files" / "HoYoPlay" / "games" / "ZenlessZoneZero Game",
        ])

    found: list[Path] = []
    seen: set[Path] = set()
    for p in cands:
        try:
            rp = p.resolve()
        except Exception:
            rp = p
        if rp in seen:
            continue
        if is_valid_game_root(p):
            seen.add(rp)
            found.append(p)
    return found


def default_worker_count(max_workers: int = 4) -> int:
    """Return default parallel worker count tuned for the host CPU core count."""
    return adaptive_worker_count("blender", max_workers=max_workers)


def adaptive_worker_count(task: str = "general", max_workers: int = 6) -> int:
    """Return RAM- and CPU-aware worker count for indexing, Blender, audio, or I/O tasks."""
    env_ov = os.environ.get("OPENZENLESS_WORKERS", "").strip()
    if env_ov.isdigit() and int(env_ov) >= 1:
        return min(max_workers, max(1, int(env_ov)))

    cpus = max(1, os.cpu_count() or 4)
    total_gb, avail_gb = detect_memory_gb()
    t = (task or "general").strip().lower()

    if t == "index":
        cpu_cap = max(1, cpus - 2) if cpus >= 4 else max(1, cpus - 1)
        mem_cap = max(1, int(avail_gb // 1.3))
        if total_gb <= 8.5:
            mem_cap = min(mem_cap, 2)
        return max(1, min(max_workers, cpu_cap, mem_cap))

    if t == "blender":
        cpu_cap = max(1, cpus // 3) if cpus >= 4 else 1
        mem_cap = max(1, int(avail_gb // 1.8))
        if total_gb <= 8.5:
            mem_cap = 1
        return max(1, min(max_workers, cpu_cap, mem_cap))

    if t in ("io", "audio"):
        return max(1, min(max_workers, max(2, cpus)))

    return max(1, min(max_workers, max(1, cpus // 2)))


def adaptive_blender_chunk_size() -> int:
    """Return per-batch FBX chunk limit scaled to available host memory."""
    total_gb, avail_gb = detect_memory_gb()
    if total_gb <= 8.5 or avail_gb <= 3.5:
        return 180
    if total_gb <= 14.0 or avail_gb <= 6.0:
        return 260
    return 350


def adaptive_viewer_max_tex_dim(image_count: int) -> int:
    """Choose safe max GL texture edge size based on scene complexity and host RAM."""
    env_dim = os.environ.get("OPENZENLESS_MAX_TEX_DIM", "").strip()
    if env_dim.isdigit() and int(env_dim) >= 256:
        return int(env_dim)
    total_gb, avail_gb = detect_memory_gb()
    low_ram = (total_gb <= 8.5) or (avail_gb <= 2.5)
    if low_ram:
        if image_count <= 60:
            return 2048
        if image_count <= 350:
            return 1024
        return 512
    if image_count <= 80:
        return 4096
    if image_count <= 500:
        return 2048
    return 1024


def get_hardware_profile() -> dict:
    """Return concise hardware profile + adaptive concurrency settings."""
    total_gb, avail_gb = detect_memory_gb()
    gpu = detect_gpu_info()
    cpus = max(1, os.cpu_count() or 4)
    return {
        "cpu_threads": cpus,
        "ram_total_gb": total_gb,
        "ram_avail_gb": avail_gb,
        "gpu_vendor": gpu.get("vendor", "Unknown"),
        "gpu_discrete": gpu.get("has_discrete") == "1",
        "index_workers": adaptive_worker_count("index", 6),
        "blender_workers": adaptive_worker_count("blender", 4),
        "blender_chunk_size": adaptive_blender_chunk_size(),
    }

