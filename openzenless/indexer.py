"""Asset indexing, character/stage/section loaders, and startup asset mapping."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config as cfg

_WWISE_MAPPER_CACHE = None


def _emit(log_cb, msg: str):
    if log_cb is not None:
        log_cb(msg)
    else:
        print(msg, flush=True)


def load_character_index() -> dict[str, list[str]]:
    for cand in (
        cfg.EXTRACTOR_ROOT / "data" / "asset_map" / "character_index_lean.json",
        cfg.EXTRACTOR_ROOT / "data" / "asset_map" / "character_index_rich.json",
        cfg.CACHE_DIR / "asset_map" / "character_index_auto.json",
        cfg.CACHE_DIR / "asset_map" / "character_index_fallback.json",
    ):
        if cand.exists():
            try:
                data = json.loads(cand.read_text())
                chars = data.get("characters", data)
                return {k: v.get("blocks", []) for k, v in chars.items() if isinstance(v, dict)}
            except Exception:
                pass
    return {}


def load_stage_index() -> dict[str, list[str]]:
    stages: dict[str, list[str]] = {}
    for inv in (
        cfg.EXTRACTOR_ROOT / "data" / "all_maps_inventory.json",
        cfg.CACHE_DIR / "asset_map" / "stage_index_auto.json",
        cfg.CACHE_DIR / "asset_map" / "stage_index_fallback.json",
    ):
        if inv.exists():
            try:
                data = json.loads(inv.read_text())
                stages = {k: list(v.get("blocks", [])) for k, v in (data.get("stages") or {}).items()}
                if stages:
                    break
            except Exception:
                pass
    if stages:
        for sec, info in load_texture_sections().items():
            if sec.startswith("NapStreaming/"):
                sname = sec.split("/", 1)[1].split("/")[0]
                if sname and "." not in sname and sname not in _NON_MAP_SECTIONS:
                    blks = stages.setdefault(sname, [])
                    for b in info.get("blocks", []):
                        if b not in blks:
                            blks.append(b)
    return stages


_NON_MAP_SECTIONS = frozenset({
    "AudioData", "Doors", "ReflectionData", "SpecialEnvVolume",
    "Map3D", "Others", "Stage_Sound", "Env_Test",
})


def load_curated_map_index() -> dict[str, list[str]]:
    stages = load_stage_index()
    curated: dict[str, list[str]] = {}
    maps_dir = cfg.FINAL_ROOT / "Maps_Combined"
    if maps_dir.exists():
        for glb in sorted(maps_dir.glob("*_full.glb")):
            name = glb.name[:-9]
            curated[name] = stages.get(name, [])
    if not curated:
        vd = cfg.FINAL_ROOT / "Viewer" / "viewer_data.json"
        if vd.exists():
            try:
                data = json.loads(vd.read_text())
                for m in data.get("maps", []):
                    mid = m.get("id") or m.get("name")
                    if mid:
                        curated[mid] = stages.get(mid, [])
            except Exception:
                pass
    if not curated and stages:
        return {
            k: v
            for k, v in stages.items()
            if k not in _NON_MAP_SECTIONS
        }
    return curated


def load_wwise_mapper():
    global _WWISE_MAPPER_CACHE
    if _WWISE_MAPPER_CACHE is not None:
        return _WWISE_MAPPER_CACHE
    if not cfg.NAP_MAP_FILE.is_file():
        return None
    if str(cfg.ANIMEWWISE_DIR) not in sys.path:
        sys.path.insert(0, str(cfg.ANIMEWWISE_DIR))
    from mapper import Mapper
    _WWISE_MAPPER_CACHE = Mapper(str(cfg.NAP_MAP_FILE))
    return _WWISE_MAPPER_CACHE


def load_character_names() -> dict[str, str]:
    p = cfg.EXTRACTOR_ROOT / "data" / "character_names.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except Exception:
        return {}


def load_texture_sections() -> dict[str, dict]:
    if not cfg.SECTIONS_INDEX_FILE.exists():
        return {}
    try:
        return json.loads(cfg.SECTIONS_INDEX_FILE.read_text())
    except Exception:
        return {}


def has_asset_map(game_root: Path) -> bool:
    files_root = cfg.resolve_files_root(game_root)
    has_ext_chars = any(
        (cfg.EXTRACTOR_ROOT / "data" / "asset_map" / name).is_file()
        for name in ("character_index_lean.json", "character_index_rich.json")
    )
    has_ext_maps = (cfg.EXTRACTOR_ROOT / "data" / "all_maps_inventory.json").is_file()
    if has_ext_chars and has_ext_maps:
        return True
    manifest = cfg.CACHE_DIR / "asset_map" / "manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text())
            if data.get("files_root") == str(files_root):
                if cfg.CLI_EXE.is_file() and (files_root / "Blocks").is_dir():
                    if data.get("auto_characters", 0) > 0 or data.get("user_skipped"):
                        return True
                    return False
                if data.get("blk_count", 0) > 0 or data.get("pck_count", 0) > 0:
                    return True
                if not (files_root / "Blocks").exists() and not (files_root / "Audio").exists():
                    return True
        except Exception:
            pass
    return False


_AVATAR_RE = re.compile(
    r"(Avatar_(?:Male|Female)_Size\d+_[A-Za-z0-9]+(?:_(?!Ani|Body|Face|Hair|Eye|Model|UI|Weapon|Cloth|Skin|Skill|Common|Effect|NPC|Player|Story|Timeline|Mat|Tex|Mesh|LOD)[A-Za-z0-9]+)?)"
)
_STAGE_RE = re.compile(r"^(?:Stage|Scene|Map)_[A-Za-z0-9]+$")
_PROG_RE = re.compile(r"\[(\d+)/(\d+)\]\s+Processed")
_ASSET_TYPES_INTEREST = frozenset(
    ("Mesh", "Texture2D", "GameObject", "Animator", "Avatar", "AnimationClip", "SkinnedMeshRenderer")
)


def _parse_cli_asset_map(
    map_json: Path,
    map_dir: Path,
    log_cb=None,
    progress_cb=None,
) -> tuple[int, int, int]:
    total_bytes = max(1, map_json.stat().st_size)
    total_mb = max(1, total_bytes // (1024 * 1024))
    chars_auto: dict[str, set[str]] = {}
    short_blocks: dict[str, set[str]] = {}
    stages_auto: dict[str, set[str]] = {}
    font_blocks: set[str] = set()

    entry_count = 0
    bytes_read = 0
    next_report = 32 * 1024 * 1024
    cur_name = ""
    cur_container = ""
    cur_source = ""
    cur_type = ""

    def _flush_entry():
        nonlocal entry_count
        if not cur_source and not cur_name:
            return
        entry_count += 1
        blk_name = os.path.basename(cur_source.replace("\\", "/"))
        if not blk_name:
            return
        if cur_type == "Font":
            font_blocks.add(blk_name)
        if cur_type in _ASSET_TYPES_INTEREST:
            hay = f"{cur_name} {cur_container}"
            for m in _AVATAR_RE.findall(hay):
                chars_auto.setdefault(m, set()).add(blk_name)
            if "_" in cur_name and not cur_name.startswith(("Avatar_", "Stage_", "Scene_", "Map_", "MainCity_", "Env_")):
                short_tok = cur_name.split("_", 1)[0]
                if len(short_tok) >= 3:
                    short_blocks.setdefault(short_tok, set()).add(blk_name)
            if cur_type in ("GameObject", "Mesh") and _STAGE_RE.match(cur_name):
                stages_auto.setdefault(cur_name, set()).add(blk_name)

    def _extract_str_val(line: str, key: str) -> str | None:
        idx = line.find(key)
        if idx < 0:
            return None
        rest = line[idx + len(key):].lstrip()
        if not rest.startswith('"'):
            return None
        end = rest.rfind('"')
        if end <= 0:
            return None
        return rest[1:end]

    with map_json.open("r", encoding="utf-8", errors="ignore", buffering=1024 * 1024) as f:
        for line in f:
            bytes_read += len(line.encode("utf-8", errors="ignore"))
            if progress_cb is not None and bytes_read >= next_report:
                pct = 85 + min(7, int(7 * bytes_read / total_bytes))
                progress_cb(pct, f"Parsing AssetMap ({bytes_read // (1024 * 1024)}/{total_mb} MB)...")
                next_report += 32 * 1024 * 1024

            s = line.strip()
            if s.startswith('"Name":'):
                cur_name = _extract_str_val(s, '"Name":') or ""
            elif s.startswith('"Container":'):
                cur_container = _extract_str_val(s, '"Container":') or ""
            elif s.startswith('"Source":'):
                cur_source = _extract_str_val(s, '"Source":') or ""
            elif s.startswith('"Type":'):
                cur_type = _extract_str_val(s, '"Type":') or ""
            elif s in ("}", "},"):
                _flush_entry()
                cur_name = cur_container = cur_source = cur_type = ""

    for ckey, blks in chars_auto.items():
        parts = ckey.split("_")
        if ckey.startswith("Avatar_") and len(parts) == 4:
            blks.update(short_blocks.get(parts[3], ()))

    if chars_auto:
        (map_dir / "character_index_auto.json").write_text(
            json.dumps({"characters": {k: {"blocks": sorted(v)} for k, v in sorted(chars_auto.items())}}, indent=2)
        )
    if stages_auto:
        (map_dir / "stage_index_auto.json").write_text(
            json.dumps({"stages": {k: {"blocks": sorted(v)} for k, v in sorted(stages_auto.items())}}, indent=2)
        )
    if font_blocks:
        (map_dir / "fonts_index_auto.json").write_text(
            json.dumps({"blocks": sorted(font_blocks)}, indent=2)
        )
    _emit(
        log_cb,
        f"Parsed AnimeStudio AssetMap ({entry_count} entries): "
        f"{len(chars_auto)} characters, {len(stages_auto)} stages, {len(font_blocks)} font blocks",
    )
    return len(chars_auto), len(stages_auto), len(font_blocks)


def build_asset_map(
    game_root: Path,
    log_cb=None,
    progress_cb=None,
    use_cli_assetmap: bool = True,
    cancel_event=None,
) -> dict:
    files_root = cfg.resolve_files_root(game_root)
    map_dir = cfg.CACHE_DIR / "asset_map"
    map_dir.mkdir(parents=True, exist_ok=True)

    def _prog(pct: int, msg: str):
        if progress_cb is not None:
            progress_cb(pct, msg)
        _emit(log_cb, msg)

    _prog(5, f"Mapping assets for {files_root}...")

    lib_dir = cfg.EXTRACTOR_ROOT / "lib"
    ran_scripts = []
    for script_name in ("build_asset_map.py", "map_assets.py", "build_character_catalog.py", "build_viewer_data.py"):
        s = lib_dir / script_name
        if s.is_file():
            _prog(15, f"Running {script_name}...")
            _emit(log_cb, f"$ {sys.executable} {s}")
            proc = subprocess.Popen(
                [sys.executable, str(s)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                _emit(log_cb, line.rstrip("\n"))
            ran_scripts.append({"script": script_name, "rc": proc.wait()})

    _prog(25, "Scanning .blk containers...")
    blk_files = sorted(files_root.rglob("*.blk")) if files_root.exists() else []
    blk_names = {p.name for p in blk_files}
    blk_stems = {p.stem for p in blk_files}

    _prog(30, "Scanning .pck audio archives...")
    pck_files = sorted(files_root.rglob("*.pck")) if files_root.exists() else []

    auto_chars = auto_stages = auto_fonts = 0
    blocks_dir = files_root / "Blocks"
    cli_map_file = map_dir / "zzz_assets_map.json"
    if (
        use_cli_assetmap
        and blocks_dir.is_dir()
        and blk_files
        and cfg.CLI_EXE.is_file()
        and shutil.which("wine") is not None
    ):
        if not cli_map_file.is_file():
            _prog(35, f"Building full AnimeStudio AssetMap across {len(blk_files)} .blk files...")
            env = os.environ.copy()
            env["WINEDEBUG"] = "-all"
            cmd = [
                "wine", str(cfg.CLI_EXE),
                "Z:" + str(blocks_dir).replace("/", "\\"),
                "Z:" + str(map_dir).replace("/", "\\"),
                "--game", "ZZZ",
                "--map_op", "AssetMap",
                "--map_type", "JSON",
                "--map_name", "zzz_assets_map",
            ]
            proc = subprocess.Popen(
                cmd, cwd=str(cfg.CLI_DIR), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                if cancel_event is not None and cancel_event.is_set():
                    proc.terminate()
                    _emit(log_cb, "Full AssetMap scan skipped by user; using quick index.")
                    break
                sline = line.rstrip("\n")
                m = _PROG_RE.search(sline)
                if m:
                    cur, tot = int(m.group(1)), max(1, int(m.group(2)))
                    pct = 35 + int(50 * cur / tot)
                    _prog(pct, f"AnimeStudio AssetMap [{cur}/{tot}]")
                elif sline.strip():
                    _emit(log_cb, sline)
            proc.wait()
        if cli_map_file.is_file():
            _prog(85, "Parsing AnimeStudio AssetMap JSON...")
            auto_chars, auto_stages, auto_fonts = _parse_cli_asset_map(
                cli_map_file, map_dir, log_cb=log_cb, progress_cb=progress_cb
            )

    _prog(92, "Indexing sections & cross-referencing containers...")
    sections = load_texture_sections()
    chars_fallback: dict[str, dict] = {}
    stages_fallback: dict[str, dict] = {}

    for sec, info in sections.items():
        blks = [b for b in info.get("blocks", []) if (b in blk_names or Path(b).stem in blk_stems)]
        if not blks and blk_names:
            continue
        use_blks = blks or info.get("blocks", [])
        if sec.startswith("Char/"):
            found_avatars = False
            for sample in info.get("samples", []):
                for part in sample.replace("\\", "/").split("/"):
                    if part.startswith("Avatar_") and "." not in part:
                        entry = chars_fallback.setdefault(part, {"blocks": []})
                        for b in use_blks:
                            if b not in entry["blocks"]:
                                entry["blocks"].append(b)
                        found_avatars = True
            if not found_avatars:
                cname = sec.split("/", 1)[1].split("/")[0]
                if cname and cname != "Avatar":
                    entry = chars_fallback.setdefault(cname, {"blocks": []})
                    for b in use_blks:
                        if b not in entry["blocks"]:
                            entry["blocks"].append(b)
        elif sec.startswith(("Scenes/", "Stage/", "StageLogic/", "NapStreaming/")):
            sname = sec.split("/", 1)[1].split("/")[0]
            if sname and "." not in sname and sname not in _NON_MAP_SECTIONS:
                entry = stages_fallback.setdefault(sname, {"blocks": []})
                for b in use_blks:
                    if b not in entry["blocks"]:
                        entry["blocks"].append(b)

    chars_out_dir = cfg.FINAL_ROOT / "Characters"
    if chars_out_dir.is_dir():
        for ch_dir in sorted(chars_out_dir.iterdir()):
            if ch_dir.is_dir():
                chars_fallback.setdefault(ch_dir.name, {"blocks": []})

    if not (cfg.EXTRACTOR_ROOT / "data" / "asset_map" / "character_index_lean.json").is_file():
        (map_dir / "character_index_fallback.json").write_text(
            json.dumps({"characters": chars_fallback}, indent=2)
        )
    if not (cfg.EXTRACTOR_ROOT / "data" / "all_maps_inventory.json").is_file():
        (map_dir / "stage_index_fallback.json").write_text(
            json.dumps({"stages": stages_fallback}, indent=2)
        )

    manifest = {
        "files_root": str(files_root),
        "blk_count": len(blk_files),
        "pck_count": len(pck_files),
        "auto_characters": auto_chars,
        "auto_stages": auto_stages,
        "auto_fonts": auto_fonts,
        "fallback_characters": len(chars_fallback),
        "fallback_stages": len(stages_fallback),
        "user_skipped": bool(cancel_event is not None and cancel_event.is_set()),
        "ran_scripts": ran_scripts,
        "built_at": int(time.time()),
    }
    (map_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    _prog(
        100,
        f"Asset map ready: {len(blk_files)} .blk, {len(pck_files)} .pck, "
        f"{auto_chars or len(chars_fallback)} characters, {auto_stages or len(stages_fallback)} stages",
    )
    return manifest
