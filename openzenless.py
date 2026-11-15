#!/usr/bin/env python3
"""
ZZZ BlkExplorer — OpenIV-style Proprietary File Viewer & CLI Tool.

Supports:
  - Game build/version checking (base_revision, res_revision, data_revision,
    audio_revision, Unity psoversioncode, AnimeStudio.CLI + Wine health).
  - Native Wwise AKPK (.pck) inspection and .bnk/.wem extraction.
  - Proprietary ZZZ .blk container inspection via AnimeStudio.CLI.exe.
  - Paged Hex + ASCII viewer for any binary file (.blk, .pck, .dat, .so, .apk).
  - Full PySide6 OpenIV-style desktop GUI + scriptable CLI subcommands.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

GAME_ROOT = Path(os.environ.get("ZZZ_GAME_ROOT", "/home/bcan77/ZenlessZoneZero_v3.4_Android_Build"))
FINAL_ROOT = Path(os.environ.get("ZZZ_FINAL_OUTPUT", "/mnt/extra/zzz34_output/FinalOutput"))
EXTRACTOR_ROOT = FINAL_ROOT / "Extractor"
CLI_DIR = EXTRACTOR_ROOT / "tools" / "AnimeStudioUltimate" / "AnimeStudio.CLI" / "bin" / "Release" / "net9.0"
CLI_EXE = CLI_DIR / "AnimeStudio.CLI.exe"
CACHE_DIR = Path(os.environ.get("ZZZ_BLK_EXPLORER_CACHE", "/tmp/zzz_blk_explorer"))
APP_DIR = Path(__file__).resolve().parent
ASSETS_DIR = APP_DIR / "assets"
LOGO_HEADER = ASSETS_DIR / "OpenZenless_Header.png"
LOGO_ICON = ASSETS_DIR / "OpenZenless_WhiteText.png"
SECTIONS_INDEX_FILE = APP_DIR / "texture_sections_index.json"
ANIMEWWISE_DIR = EXTRACTOR_ROOT / "tools" / "AnimeWwise"
NAP_MAP_FILE = ANIMEWWISE_DIR / "maps" / "nap.map"
VGMSTREAM_CLI = EXTRACTOR_ROOT / "tools" / "vgmstream" / "build" / "cli" / "vgmstream-cli"
DOC_FILE = APP_DIR / "README.md"
_WWISE_MAPPER_CACHE = None


def resolve_files_root(game_root: Path) -> Path:
    for cand in (
        game_root / "com.HoYoverse.Nap" / "files",
        game_root / "ZenlessZoneZero_Data" / "StreamingAssets",
        game_root / "ZenlessZoneZero Game" / "ZenlessZoneZero_Data" / "StreamingAssets",
        game_root / "StreamingAssets",
    ):
        if cand.exists():
            return cand
    return game_root


def detect_build_platform(game_root: Path, files_root: Path) -> str:
    s = f"{game_root} {files_root}"
    if (files_root / "Audio" / "Windows").exists() or "ZenlessZoneZero_Data" in s or (game_root / "ZenlessZoneZero_Data").exists():
        return "PC (Windows)"
    if (files_root / "Audio" / "Android").exists() or "com.HoYoverse.Nap" in s:
        return "Android"
    return "Unknown"


def human_size(n: int) -> str:
    if n < 0:
        return "-"
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024 or unit == "GB":
            return f"{int(val)} B" if unit == "B" else f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} GB"


def _read_small_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_version_field(files_root: Path, name: str) -> str:
    for base in (files_root, files_root.parent / "Persistent"):
        val = _read_small_text(base / name)
        if val:
            return val
    return "unknown"


def collect_game_version(game_root: Path) -> dict:
    files_root = resolve_files_root(game_root)
    info = {
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
        "audio_lang": _read_version_field(files_root, "audio_lang"),
        "blk_count": 0,
        "pck_count": 0,
        "cli_exists": CLI_EXE.exists(),
        "wine_exists": shutil.which("wine") is not None,
    }
    blocks_dir = files_root / "Blocks"
    if blocks_dir.exists():
        info["blk_count"] = sum(1 for _ in blocks_dir.rglob("*.blk"))
    audio_dir = files_root / "Audio"
    if audio_dir.exists():
        info["pck_count"] = sum(1 for _ in audio_dir.rglob("*.pck"))
    return info


def detect_magic(path: Path) -> tuple[str, bytes]:
    try:
        with path.open("rb") as fh:
            head = fh.read(64)
    except OSError:
        return "Unreadable", b""
    if head.startswith(b"blk\x00") or path.suffix.lower() == ".blk":
        if head[:4] == b"blk\x00":
            return "ZZZ BLK (blk\\0)", head
        return "ZZZ BLK (obf/custom)", head
    if head.startswith(b"UnityFS"):
        return "UnityFS Bundle", head
    if head.startswith(b"AKPK"):
        return "Wwise AKPK Package", head
    if head.startswith(b"BKHD"):
        return "Wwise SoundBank (BKHD)", head
    if head.startswith(b"RIFF"):
        return "RIFF / Wwise WEM", head
    if head.startswith(b"\x7fELF"):
        return "ELF Shared Library", head
    if head.startswith(b"PK\x03\x04"):
        return "ZIP / APK Archive", head
    if head.startswith(b"\xaf\x1b\xb1\xfa"):
        return "Unity global-metadata", head
    if path.name == "global-metadata.dat":
        return "IL2CPP Metadata (obf)", head
    return path.suffix.lstrip(".").upper() or "Binary", head


def format_hex_dump(data: bytes, base_offset: int = 0) -> str:
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off : off + 16]
        hex_left = " ".join(f"{b:02X}" for b in chunk[:8])
        hex_right = " ".join(f"{b:02X}" for b in chunk[8:])
        ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base_offset + off:08X}  {hex_left:<23}  {hex_right:<23}  |{ascii_str}|")
    return "\n".join(lines)


def parse_akpk(path: Path) -> dict:
    """Parse Wwise AKPK header and embedded Bank/WEM/External entry tables."""
    entries = []
    with path.open("rb") as fh:
        hdr = fh.read(28)
        if len(hdr) < 28 or hdr[:4] != b"AKPK":
            return {"error": "Not a valid AKPK header", "entries": []}
        header_size, version, lang_map_size, bnk_tbl_size, wem_tbl_size = struct.unpack("<IIIII", hdr[4:24])
        ext_tbl_size = struct.unpack("<I", hdr[24:28])[0]

        fh.seek(28 + lang_map_size)
        for kind, tbl_size, entry_fmt in (
            ("BNK", bnk_tbl_size, "<IIIII"),
            ("WEM", wem_tbl_size, "<IIIII"),
            ("EXT", ext_tbl_size, "<QIIII"),
        ):
            if tbl_size < 4:
                fh.read(tbl_size)
                continue
            raw = fh.read(tbl_size)
            count = struct.unpack("<I", raw[:4])[0]
            pos = 4
            step = struct.calcsize(entry_fmt)
            for _ in range(count):
                if pos + step > len(raw):
                    break
                fid, align, size, offset_blk, lang_id = struct.unpack_from(entry_fmt, raw, pos)
                pos += step
                entries.append({
                    "type": f"Wwise {kind}",
                    "name": f"{fid}.{kind.lower()}" if kind != "EXT" else f"{fid:016x}.wem",
                    "id": str(fid),
                    "offset": offset_blk * max(1, align),
                    "size": size,
                    "lang_id": lang_id,
                })
    return {
        "header_size": header_size,
        "version": version,
        "lang_map_size": lang_map_size,
        "bnk_table_size": bnk_tbl_size,
        "wem_table_size": wem_tbl_size,
        "ext_table_size": ext_tbl_size,
        "entries": entries,
    }


def scan_blk_json(blk_path: Path, out_dir: Path | None = None, types: list[str] | None = None) -> tuple[Path, list[dict], str]:
    if not CLI_EXE.exists():
        raise FileNotFoundError(f"AnimeStudio.CLI.exe not found at {CLI_EXE}")
    if shutil.which("wine") is None:
        raise FileNotFoundError("wine executable not found in PATH")
    target_dir = out_dir or (CACHE_DIR / "blk_json" / blk_path.stem)
    target_dir.mkdir(parents=True, exist_ok=True)
    existing = list(target_dir.rglob("*.json"))
    log_text = "(cached)"
    if not existing:
        env = os.environ.copy()
        env["WINEDEBUG"] = "-all"
        use_types = types or [
            "Texture2D", "Material", "Shader", "Mesh",
            "GameObject", "TextAsset", "MonoBehaviour", "Sprite", "AudioClip",
        ]
        cmd = [
            "wine", str(CLI_EXE),
            "Z:" + str(blk_path).replace("/", "\\"),
            "Z:" + str(target_dir).replace("/", "\\"),
            "--game", "ZZZ",
            "--types", *use_types,
            "--group_assets", "ByType",
            "--export_type", "JSON",
        ]
        proc = subprocess.run(cmd, cwd=CLI_DIR, env=env, capture_output=True, text=True, timeout=300)
        log_text = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        existing = list(target_dir.rglob("*.json"))
        if proc.returncode != 0 and not existing:
            raise RuntimeError(f"CLI exited {proc.returncode}:\n{log_text[-1200:]}")

    rows = []
    for p in sorted(existing):
        rel = p.relative_to(target_dir)
        asset_type = rel.parts[0] if len(rel.parts) > 1 else "Asset"
        rows.append({
            "type": asset_type,
            "name": p.stem,
            "path": str(p),
            "size": p.stat().st_size,
            "blk": str(blk_path),
        })
    return target_dir, rows, log_text


def load_character_index() -> dict[str, list[str]]:
    for cand in (
        EXTRACTOR_ROOT / "data" / "asset_map" / "character_index_lean.json",
        EXTRACTOR_ROOT / "data" / "asset_map" / "character_index_rich.json",
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
    inv = EXTRACTOR_ROOT / "data" / "all_maps_inventory.json"
    if not inv.exists():
        return {}
    try:
        data = json.loads(inv.read_text())
        return {k: v.get("blocks", []) for k, v in (data.get("stages") or {}).items()}
    except Exception:
        return {}


def load_curated_map_index() -> dict[str, list[str]]:
    stages = load_stage_index()
    curated: dict[str, list[str]] = {}
    maps_dir = FINAL_ROOT / "Maps_Combined"
    if maps_dir.exists():
        for glb in sorted(maps_dir.glob("*_full.glb")):
            name = glb.name[:-9]
            curated[name] = stages.get(name, [])
    if not curated:
        vd = FINAL_ROOT / "Viewer" / "viewer_data.json"
        if vd.exists():
            try:
                data = json.loads(vd.read_text())
                for m in data.get("maps", []):
                    mid = m.get("id") or m.get("name")
                    if mid:
                        curated[mid] = stages.get(mid, [])
            except Exception:
                pass
    return curated


def load_wwise_mapper():
    global _WWISE_MAPPER_CACHE
    if _WWISE_MAPPER_CACHE is not None:
        return _WWISE_MAPPER_CACHE
    if not NAP_MAP_FILE.is_file():
        return None
    if str(ANIMEWWISE_DIR) not in sys.path:
        sys.path.insert(0, str(ANIMEWWISE_DIR))
    from mapper import Mapper
    _WWISE_MAPPER_CACHE = Mapper(str(NAP_MAP_FILE))
    return _WWISE_MAPPER_CACHE


def load_character_names() -> dict[str, str]:
    p = EXTRACTOR_ROOT / "data" / "character_names.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except Exception:
        return {}


def load_texture_sections() -> dict[str, dict]:
    if not SECTIONS_INDEX_FILE.exists():
        return {}
    try:
        return json.loads(SECTIONS_INDEX_FILE.read_text())
    except Exception:
        return {}


def _emit(log_cb, msg: str):
    if log_cb is not None:
        log_cb(msg)
    else:
        print(msg, flush=True)


def run_streaming_cmd(cmd: list[str], cwd: Path | None = None, env: dict | None = None, log_cb=None) -> int:
    _emit(log_cb, f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        _emit(log_cb, line.rstrip("\n"))
    return proc.wait()


def export_blk_asset_types(
    source_path: Path,
    out_dir: Path,
    types: list[str],
    export_mode: str = "Convert",
    name_filter: str = "",
    log_cb=None,
) -> int:
    if not CLI_EXE.exists():
        raise FileNotFoundError(f"AnimeStudio.CLI.exe not found at {CLI_EXE}")
    if shutil.which("wine") is None:
        raise FileNotFoundError("wine executable not found in PATH")
    if not source_path.exists():
        raise FileNotFoundError(f"Source path not found: {source_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["WINEDEBUG"] = "-all"
    cmd = [
        "wine", str(CLI_EXE),
        "Z:" + str(source_path).replace("/", "\\"),
        "Z:" + str(out_dir).replace("/", "\\"),
        "--game", "ZZZ",
        "--types", *types,
        "--group_assets", "ByType",
        "--export_type", export_mode,
    ]
    if name_filter.strip():
        cmd.extend(["--filter_with_name", name_filter.strip()])
    return run_streaming_cmd(cmd, cwd=CLI_DIR, env=env, log_cb=log_cb)


def export_character_full(
    char_name: str,
    out_dir: Path | None = None,
    convert_glb: bool = False,
    log_cb=None,
) -> int:
    script = EXTRACTOR_ROOT / "lib" / "extract_character_v2.py"
    if not script.exists():
        raise FileNotFoundError(f"Missing {script}")
    rc = run_streaming_cmd([sys.executable, str(script), char_name], log_cb=log_cb)
    if rc != 0:
        return rc
    if convert_glb:
        blender = shutil.which("blender")
        if not blender:
            _emit(log_cb, "WARNING: blender not found in PATH; skipping GLB conversion.")
        else:
            bl_script = EXTRACTOR_ROOT / "lib" / "blender_export_character.py"
            rc = run_streaming_cmd([blender, "-b", "--python", str(bl_script), "--", char_name], log_cb=log_cb)
            if rc != 0:
                return rc
            morph_script = EXTRACTOR_ROOT / "lib" / "inject_character_facial_morphs.py"
            if morph_script.exists():
                rc = run_streaming_cmd([sys.executable, str(morph_script), "--char", char_name, "--force"], log_cb=log_cb)
                if rc != 0:
                    return rc
    src_char_dir = FINAL_ROOT / "Characters" / char_name
    if out_dir is not None and src_char_dir.exists():
        dest = out_dir / char_name
        if dest.resolve() != src_char_dir.resolve():
            _emit(log_cb, f"Copying {src_char_dir} -> {dest}")
            shutil.copytree(src_char_dir, dest, dirs_exist_ok=True)
    return 0


def export_character_animations_only(
    char_name: str,
    game_root: Path,
    out_dir: Path,
    log_cb=None,
) -> int:
    chars = load_character_index()
    blocks = chars.get(char_name)
    if not blocks:
        raise KeyError(f"Character '{char_name}' not found in character index.")
    files_root = resolve_files_root(game_root)
    blocks_dir = files_root / "Blocks"
    resolved = [blocks_dir / f"{b}.blk" for b in blocks if (blocks_dir / f"{b}.blk").is_file()]
    if not resolved:
        resolved = [files_root / b for b in blocks if (files_root / b).is_file()]
    if not resolved:
        raise FileNotFoundError(f"No .blk files found for '{char_name}' under {files_root}")

    dest_dir = out_dir / char_name / "Animations"
    dest_dir.mkdir(parents=True, exist_ok=True)
    _emit(log_cb, f"Exporting animations only for {char_name} from {len(resolved)} .blk file(s) -> {dest_dir}")
    last_rc = 0
    for blk in resolved:
        rc = export_blk_asset_types(
            blk,
            dest_dir,
            types=["AnimationClip", "Animator", "Avatar"],
            export_mode="Convert",
            log_cb=log_cb,
        )
        if rc != 0:
            last_rc = rc
    return last_rc


def export_scene_map(
    stage_name: str | None,
    resume: bool = True,
    workers: int = 1,
    out_dir: Path | None = None,
    log_cb=None,
) -> int:
    script = EXTRACTOR_ROOT / "lib" / "export_maps_pipeline.py"
    if not script.exists():
        raise FileNotFoundError(f"Missing {script}")
    cmd = [sys.executable, str(script), "--workers", str(max(1, workers))]
    if resume:
        cmd.append("--resume")
    if stage_name and stage_name.lower() != "all":
        cmd.append(stage_name)
    rc = run_streaming_cmd(cmd, log_cb=log_cb)
    if rc != 0:
        return rc
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        maps_dir = FINAL_ROOT / "Maps_Combined"
        if stage_name and stage_name.lower() != "all":
            cand = maps_dir / f"{stage_name}_full.glb"
            if cand.is_file():
                _emit(log_cb, f"Copying {cand.name} -> {out_dir}")
                shutil.copy2(cand, out_dir / cand.name)
        elif maps_dir.exists():
            for glb in sorted(maps_dir.glob("*_full.glb")):
                shutil.copy2(glb, out_dir / glb.name)
            _emit(log_cb, f"Copied all *_full.glb maps to {out_dir}")
    return 0


def export_audio_pck(
    source_path: Path,
    out_dir: Path,
    limit: int = 0,
    use_wwise_map: bool = True,
    decode_wav: bool = False,
    log_cb=None,
) -> int:
    if not source_path.exists():
        raise FileNotFoundError(f"Audio source not found: {source_path}")
    pcks = [source_path] if source_path.is_file() else sorted(source_path.rglob("*.pck"))
    if not pcks:
        raise FileNotFoundError(f"No .pck files found in {source_path}")
    mapper = None
    if use_wwise_map:
        _emit(log_cb, f"Loading AnimeWwise map: {NAP_MAP_FILE.name}...")
        mapper = load_wwise_mapper()
        if mapper is None:
            _emit(log_cb, "WARNING: nap.map not found; falling back to raw AKPK filenames.")
    if decode_wav and not VGMSTREAM_CLI.is_file():
        _emit(log_cb, f"WARNING: vgmstream-cli not found at {VGMSTREAM_CLI}; keeping .wem files.")
        decode_wav = False

    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    mapped_count = 0
    for pck in pcks:
        ak = parse_akpk(pck)
        if ak.get("error"):
            _emit(log_cb, f"SKIP {pck.name}: {ak['error']}")
            continue
        entries = ak["entries"][:limit] if limit > 0 else ak["entries"]
        lang_hint = "English" if "/En/" in str(pck) else (pck.parent.name if pck.parent.name in ("En", "Jp", "Cn", "Kr") else "SFX")
        with pck.open("rb") as src:
            for e in entries:
                src.seek(e["offset"])
                raw = src.read(e["size"])
                stem = Path(e["name"]).stem.lower()
                res = mapper.get_key(stem, addLang=True) if (mapper and e["name"].endswith(".wem")) else None
                if res:
                    rel_path, raw_lang = res[0], (res[1] or lang_hint)
                    lang = raw_lang.split("(")[0].strip() if "(" in raw_lang else raw_lang
                    dest = out_dir / lang / (rel_path.replace("\\", "/").lstrip("/") + ".wem")
                    mapped_count += 1
                elif mapper is not None:
                    dest = out_dir / lang_hint / "_Unresolved" / pck.stem / e["name"]
                else:
                    sub_out = out_dir / pck.stem if len(pcks) > 1 else out_dir
                    dest = sub_out / e["name"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                if decode_wav and dest.suffix.lower() == ".wem":
                    wav_dest = dest.with_suffix(".wav")
                    proc = subprocess.run(
                        [str(VGMSTREAM_CLI), "-o", str(wav_dest), str(dest)],
                        capture_output=True, text=True,
                    )
                    if proc.returncode == 0 and wav_dest.is_file():
                        dest.unlink(missing_ok=True)
        total += len(entries)
        _emit(log_cb, f"Unpacked {len(entries)} entries from {pck.name} (mapped so far: {mapped_count}/{total})")
    _emit(log_cb, f"Total audio entries extracted: {total} (AnimeWwise mapped: {mapped_count})")
    return 0


def export_full_game(
    game_root: Path,
    out_dir: Path,
    include_chars: bool = True,
    include_scenes: bool = True,
    include_fonts: bool = True,
    include_audio: bool = True,
    convert_glb: bool = False,
    use_wwise_map: bool = True,
    decode_wav: bool = False,
    dry_run: bool = False,
    log_cb=None,
) -> int:
    files_root = resolve_files_root(game_root)
    chars = sorted(load_character_index().keys())
    maps = sorted(load_curated_map_index().keys())
    _emit(log_cb, "=== OpenZenless Full Export Plan ===")
    _emit(log_cb, f"Game Root   : {files_root}")
    _emit(log_cb, f"Output Root : {out_dir}")
    _emit(log_cb, f"Characters  : {len(chars) if include_chars else 'SKIPPED'} (GLB={convert_glb})")
    _emit(log_cb, f"Curated Maps: {len(maps) if include_scenes else 'SKIPPED'}")
    _emit(log_cb, f"Fonts       : {'YES' if include_fonts else 'SKIPPED'}")
    _emit(log_cb, f"Audio (.pck): {'YES' if include_audio else 'SKIPPED'} (AnimeWwise={use_wwise_map}, WAV={decode_wav})")
    if dry_run:
        _emit(log_cb, "[DRY RUN] No files were modified.")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    if include_chars:
        for idx, ch in enumerate(chars, 1):
            _emit(log_cb, f"[{idx}/{len(chars)}] Exporting character {ch}...")
            export_character_full(ch, out_dir=out_dir / "Characters", convert_glb=convert_glb, log_cb=log_cb)
    if include_scenes:
        _emit(log_cb, "Exporting curated maps...")
        export_scene_map("all", resume=True, workers=1, out_dir=out_dir / "Maps_Combined", log_cb=log_cb)
    if include_fonts:
        _emit(log_cb, "Exporting all Fonts from Blocks/...")
        export_blk_asset_types(files_root / "Blocks", out_dir / "Fonts", ["Font"], "Convert", log_cb=log_cb)
    if include_audio:
        audio_dir = files_root / "Audio"
        if audio_dir.exists():
            _emit(log_cb, "Unpacking & organizing Wwise .pck archives...")
            export_audio_pck(
                audio_dir, out_dir / "Audio",
                use_wwise_map=use_wwise_map, decode_wav=decode_wav, log_cb=log_cb,
            )
    for lib_name in ("build_character_catalog.py", "build_viewer_data.py"):
        lib_script = EXTRACTOR_ROOT / "lib" / lib_name
        if lib_script.exists():
            run_streaming_cmd([sys.executable, str(lib_script)], log_cb=log_cb)
    _emit(log_cb, "=== Full Export Complete ===")
    return 0


def launch_gui(root_path: Path):
    from PySide6.QtCore import Qt, QThread, Signal, QSize
    from PySide6.QtGui import QColor, QFont, QIcon, QKeySequence, QPixmap, QShortcut
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
        QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
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
                if not CLI_EXE.exists() or shutil.which("wine") is None:
                    self.failed.emit("AnimeStudio.CLI.exe or wine is not available.")
                    return
                out_dir = CACHE_DIR / "blk_convert" / self.blk_path.stem / self.asset_type
                out_dir.mkdir(parents=True, exist_ok=True)
                for ext in (".png", ".tga", ".fbx", ".wav", ".ogg", ".txt", ".bytes"):
                    cand = list(out_dir.rglob(f"{self.asset_name}{ext}"))
                    if cand:
                        self.finished_ok.emit(str(cand[0]), "cached")
                        return
                env = os.environ.copy()
                env["WINEDEBUG"] = "-all"
                cmd = [
                    "wine", str(CLI_EXE),
                    "Z:" + str(self.blk_path).replace("/", "\\"),
                    "Z:" + str(out_dir).replace("/", "\\"),
                    "--game", "ZZZ",
                    "--types", self.asset_type,
                    "--filter_with_name", self.asset_name,
                    "--group_assets", "ByType",
                    "--export_type", "Convert",
                ]
                proc = subprocess.run(cmd, cwd=CLI_DIR, env=env, capture_output=True, text=True, timeout=180)
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
            self.resize(880, 650)
            lay = QVBoxLayout(self)

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
            self.btn_browse_src = QPushButton("Browse File/Dir...")
            self.btn_browse_src.clicked.connect(self._browse_source)
            src_row.addWidget(self.source_edit, 1)
            src_row.addWidget(self.btn_browse_src)
            form.addRow("Source (.blk/.pck/Dir):", src_row)

            out_row = QHBoxLayout()
            self.out_edit = QLineEdit(str(FINAL_ROOT / "Exports"))
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
            opts_lay = QVBoxLayout(opts)
            row1 = QHBoxLayout()
            self.chk_glb = QCheckBox("Convert Character to GLB + Facial Morphs (Blender)")
            self.chk_resume = QCheckBox("Resume Map Checkpoints")
            self.chk_resume.setChecked(True)
            self.chk_dry = QCheckBox("Dry Run (Preview Full Plan)")
            self.spin_workers = QSpinBox()
            self.spin_workers.setRange(1, 8)
            self.spin_workers.setValue(1)
            row1.addWidget(self.chk_glb)
            row1.addWidget(self.chk_resume)
            row1.addWidget(self.chk_dry)
            row1.addWidget(QLabel("Map Workers:"))
            row1.addWidget(self.spin_workers)
            row1.addStretch()

            row2 = QHBoxLayout()
            self.chk_wwise_map = QCheckBox("Auto-name & Organize Voices via AnimeWwise (nap.map)")
            self.chk_wwise_map.setChecked(True)
            self.chk_wav = QCheckBox("Decode .wem -> .wav (vgmstream-cli)")
            row2.addWidget(self.chk_wwise_map)
            row2.addWidget(self.chk_wav)
            row2.addStretch()

            opts_lay.addLayout(row1)
            opts_lay.addLayout(row2)
            lay.addWidget(opts)

            self.log_box = QPlainTextEdit()
            self.log_box.setReadOnly(True)
            self.log_box.setFont(QFont("JetBrains Mono, Consolas, monospace", 10))
            lay.addWidget(self.log_box, 1)

            btns = QHBoxLayout()
            self.btn_run = QPushButton("Run Export")
            self.btn_run.clicked.connect(self._start_export)
            self.btn_close = QPushButton("Close")
            self.btn_close.clicked.connect(self.accept)
            btns.addStretch()
            btns.addWidget(self.btn_run)
            btns.addWidget(self.btn_close)
            lay.addLayout(btns)

            self._on_scope_changed()

        def _browse_source(self):
            d = QFileDialog.getExistingDirectory(self, "Select Source Directory", str(self.parent_win.files_root))
            if d:
                self.source_edit.setText(d)

        def _browse_out(self):
            d = QFileDialog.getExistingDirectory(self, "Select Output Directory", self.out_edit.text() or str(FINAL_ROOT))
            if d:
                self.out_edit.setText(d)

        def _on_scope_changed(self):
            scope = self.scope_combo.currentData()
            self.target_combo.clear()
            chars = sorted(self.parent_win.char_blocks.keys())
            curated_maps = sorted(self.parent_win.curated_maps.keys())
            stages = sorted(self.parent_win.stage_blocks.keys())
            default_src = (
                str(self.parent_win.current_container)
                if self.parent_win.current_container and self.parent_win.current_container.exists()
                else str(self.parent_win.files_root / ("Audio" if scope == "audio" else "Blocks"))
            )
            self.source_edit.setText(default_src)
            if scope in ("character", "animations"):
                self.target_combo.addItems(chars)
                self.target_combo.setEnabled(True)
                self.source_edit.setEnabled(False)
                self.btn_browse_src.setEnabled(False)
            elif scope == "map":
                self.target_combo.addItem("all")
                self.target_combo.addItems(curated_maps)
                self.target_combo.setEnabled(True)
                self.source_edit.setEnabled(False)
                self.btn_browse_src.setEnabled(False)
            elif scope == "stage":
                self.target_combo.addItems(stages)
                self.target_combo.setEnabled(True)
                self.source_edit.setEnabled(False)
                self.btn_browse_src.setEnabled(False)
            else:
                self.target_combo.addItem("(Uses Source Path)")
                self.target_combo.setEnabled(False)
                self.source_edit.setEnabled(scope != "full")
                self.btn_browse_src.setEnabled(scope != "full")

            self.types_edit.setEnabled(scope == "assets")
            self.mode_combo.setEnabled(scope == "assets")
            self.filter_edit.setEnabled(scope == "assets")
            self.chk_glb.setEnabled(scope in ("character", "full"))
            self.chk_resume.setEnabled(scope in ("map", "stage", "full"))
            self.spin_workers.setEnabled(scope in ("map", "stage"))
            self.chk_dry.setEnabled(scope == "full")
            self.chk_wwise_map.setEnabled(scope in ("audio", "full"))
            self.chk_wav.setEnabled(scope in ("audio", "full"))

        def _start_export(self):
            scope = self.scope_combo.currentData()
            target = self.target_combo.currentText().strip()
            src = Path(self.source_edit.text().strip())
            out = Path(self.out_edit.text().strip())
            game_root = self.parent_win.game_root
            convert_glb = self.chk_glb.isChecked()
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
                    return export_character_full(target, out_dir=out, convert_glb=convert_glb, log_cb=cb)
                if scope == "animations":
                    return export_character_animations_only(target, game_root, out, log_cb=cb)
                if scope in ("map", "stage"):
                    return export_scene_map(target, resume=resume, workers=workers, out_dir=out, log_cb=cb)
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
                    include_chars=True, include_scenes=True, include_fonts=True, include_audio=True,
                    convert_glb=convert_glb, use_wwise_map=use_wwise, decode_wav=decode_wav,
                    dry_run=dry_run, log_cb=cb,
                )

            self.worker = BatchExportWorker(task)
            self.worker.log_line.connect(self.log_box.appendPlainText)
            self.worker.finished_rc.connect(self._on_done)
            self.worker.failed.connect(self._on_err)
            self.worker.start()

        def _on_done(self, rc: int):
            self.btn_run.setEnabled(True)
            self.log_box.appendPlainText(f"\n[Finished with exit code {rc}]")

        def _on_err(self, msg: str):
            self.btn_run.setEnabled(True)
            self.log_box.appendPlainText(f"\n[ERROR] {msg}")
            QMessageBox.warning(self, "Export Error", msg)

    class BlkExplorerWindow(QMainWindow):
        def __init__(self, initial_root: Path):
            super().__init__()
            self.setWindowTitle("OpenZenless — ZZZ Proprietary File Explorer")
            if LOGO_ICON.is_file():
                self.setWindowIcon(QIcon(str(LOGO_ICON)))
            self.resize(1500, 920)
            self.game_root = initial_root
            self.files_root = resolve_files_root(initial_root)
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
            inv = EXTRACTOR_ROOT / "data" / "all_maps_inventory.json"
            if not inv.exists():
                return {}
            try:
                data = json.loads(inv.read_text())
                return {k: v.get("blocks", []) for k, v in (data.get("stages") or {}).items()}
            except Exception:
                return {}

        def _load_char_blocks(self) -> dict[str, list[str]]:
            idx = EXTRACTOR_ROOT / "data" / "asset_map" / "character_index_lean.json"
            if not idx.exists():
                return {}
            try:
                data = json.loads(idx.read_text())
                chars = data.get("characters", data)
                return {k: v.get("blocks", []) for k, v in chars.items() if isinstance(v, dict)}
            except Exception:
                return {}

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
            if LOGO_HEADER.is_file():
                pix = QPixmap(str(LOGO_HEADER))
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

            self.search_box = QLineEdit()
            self.search_box.setMaximumWidth(210)
            self.search_box.setPlaceholderText("Filter... (Ctrl+F)")
            self.search_box.textChanged.connect(self._apply_filter)
            nav_row.addWidget(self.search_box)

            self.type_filter = QComboBox()
            self.type_filter.addItem("All Types")
            self.type_filter.currentTextChanged.connect(self._apply_filter)
            nav_row.addWidget(self.type_filter)

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
            self.table.setColumnWidth(0, 320)
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
            splitter.setSizes([280, 650, 530])

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
                self._ensure_viewer_loaded()

        def _build_viewer_page(self) -> QWidget:
            self._viewer_server = None
            self._viewer_port = None
            self._web_view = None
            w = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(8, 8, 8, 8)
            lay.setSpacing(6)
            bar = QHBoxLayout()
            self.viewer_status_lbl = QLabel("Exported Asset 3D Viewer & Catalog (Characters, Maps, Animations)")
            self.viewer_status_lbl.setStyleSheet("color: #94a3b8; font-weight: 600;")
            btn_rebuild = QPushButton("Rebuild Catalog & Reload")
            btn_rebuild.clicked.connect(self._rebuild_and_reload_viewer)
            btn_ext = QPushButton("Open in Browser")
            btn_ext.clicked.connect(self._open_viewer_external)
            bar.addWidget(self.viewer_status_lbl)
            bar.addStretch()
            bar.addWidget(btn_rebuild)
            bar.addWidget(btn_ext)
            lay.addLayout(bar)
            self.viewer_container = QVBoxLayout()
            lay.addLayout(self.viewer_container, 1)
            return w

        def _start_viewer_server(self) -> int:
            if self._viewer_port is not None:
                return self._viewer_port
            import functools
            import http.server
            import socketserver
            import threading

            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FINAL_ROOT))

            class _QuietHandler(http.server.SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=str(FINAL_ROOT), **kwargs)

                def log_message(self, format, *args):
                    pass

            class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
                daemon_threads = True

            srv = _Server(("127.0.0.1", 0), _QuietHandler)
            self._viewer_server = srv
            self._viewer_port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            return self._viewer_port

        def _ensure_viewer_loaded(self):
            from PySide6.QtCore import QUrl
            port = self._start_viewer_server()
            url = f"http://127.0.0.1:{port}/Viewer/index.html"
            self.viewer_status_lbl.setText(f"Embedded 3D Viewer • {url}")
            if self._web_view is None:
                from PySide6.QtWebEngineWidgets import QWebEngineView
                self._web_view = QWebEngineView()
                self.viewer_container.addWidget(self._web_view, 1)
                self._web_view.setUrl(QUrl(url))

        def _rebuild_and_reload_viewer(self):
            for lib_name in ("build_character_catalog.py", "build_viewer_data.py"):
                s = EXTRACTOR_ROOT / "lib" / lib_name
                if s.exists():
                    subprocess.run([sys.executable, str(s)], capture_output=True)
            if self._web_view is not None:
                self._web_view.reload()
            self.status.showMessage("Rebuilt catalog & reloaded 3D Viewer")

        def _open_viewer_external(self):
            import webbrowser
            port = self._start_viewer_server()
            webbrowser.open(f"http://127.0.0.1:{port}/Viewer/index.html")

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
            btn_row.addWidget(self.btn_basic_inspect)
            btn_row.addWidget(self.btn_basic_export1)
            btn_row.addWidget(self.btn_basic_export2)
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
                    out_dir = FINAL_ROOT / "Characters" / cid
                    has_glb = (out_dir / "Model").exists() and any((out_dir / "Model").glob("*.glb"))
                    status = f"{len(self.char_blocks[cid])} .blk" + (" • GLB Ready" if has_glb else "")
                    items.append({"cat": cat, "name": loc, "status": status, "id": cid, "blocks": self.char_blocks[cid]})
            elif cat == "maps":
                for st in sorted(self.curated_maps.keys()):
                    glb = FINAL_ROOT / "Maps_Combined" / f"{st}_full.glb"
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
                cdir = FINAL_ROOT / "Characters" / r["id"]
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
            out = QFileDialog.getExistingDirectory(self, "Select Export Destination", str(FINAL_ROOT / "Exports"))
            if not out:
                return
            out_dir = Path(out)
            if cat == "characters":
                self._run_basic_export(
                    f"Exporting {r['name']}",
                    lambda cb: export_character_full(r["id"], out_dir=out_dir, convert_glb=True, log_cb=cb),
                )
            elif cat == "maps":
                self._run_basic_export(
                    f"Exporting Map {r['id']}",
                    lambda cb: export_scene_map(r["id"], resume=True, workers=1, out_dir=out_dir, log_cb=cb),
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
                        use_wwise_map=True, decode_wav=False, log_cb=cb,
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
            out = QFileDialog.getExistingDirectory(self, "Select Animations Destination", str(FINAL_ROOT / "Exports"))
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
            v = collect_game_version(self.game_root)
            cli_tag = "CLI+Wine OK" if (v["cli_exists"] and v["wine_exists"]) else "CLI/Wine Missing"
            txt = f"{v['platform']} | Unity {v['unity_version']} | res {v['res_revision']} | {v['blk_count']} .blk | {cli_tag}"
            self.header_version_pill.setText(txt)

        def show_version_dialog(self):
            v = collect_game_version(self.game_root)
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
                f"AnimeStudio CLI:  {CLI_EXE} ({'OK' if v['cli_exists'] else 'MISSING'})",
                f"Wine Runtime:     {'OK' if v['wine_exists'] else 'MISSING'}",
            ]
            QMessageBox.information(self, "ZZZ Build & Environment Check", "\n".join(lines))

        def show_docs_dialog(self):
            dlg = QDialog(self)
            dlg.setWindowTitle("ZZZ BlkExplorer — CLI & GUI Documentation")
            dlg.resize(820, 620)
            lay = QVBoxLayout(dlg)
            view = QTextBrowser()
            if DOC_FILE.exists():
                view.setMarkdown(DOC_FILE.read_text(encoding="utf-8"))
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
                plat = detect_build_platform(self.game_root, self.files_root)
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

        def choose_root_folder(self):
            d = QFileDialog.getExistingDirectory(self, "Select ZZZ Game Root", str(self.game_root))
            if d:
                self.game_root = Path(d)
                self.files_root = resolve_files_root(self.game_root)
                self._populate_tree()
                self._refresh_version_badge()
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
                and CLI_EXE.exists()
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
            out_dir = QFileDialog.getExistingDirectory(self, "Select Export Destination", str(FINAL_ROOT))
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
    win = BlkExplorerWindow(root_path)
    win.show()
    sys.exit(app.exec())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openzenless",
        description="OpenZenless — OpenIV-style ZZZ proprietary file viewer & batch exporter.",
    )
    sub = p.add_subparsers(dest="cmd")

    p_gui = sub.add_parser("gui", help="Launch the PySide6 desktop GUI (default)")
    p_gui.add_argument("--root", type=Path, default=GAME_ROOT, help="Path to ZZZ Android/PC build root")

    p_ver = sub.add_parser("version", help="Check ZZZ build revisions, container counts, and CLI/Wine health")
    p_ver.add_argument("--root", type=Path, default=GAME_ROOT, help="Path to ZZZ Android/PC build root")
    p_ver.add_argument("--json", action="store_true", help="Output JSON")

    p_ins = sub.add_parser("inspect", help="Inspect binary magic, AKPK tables, and hex dump of a file")
    p_ins.add_argument("file", type=Path, help="Path to .blk, .pck, .dat, .so, etc.")
    p_ins.add_argument("--offset", default="0x0", help="Hex or decimal byte offset (default: 0x0)")
    p_ins.add_argument("--length", type=int, default=256, help="Number of bytes to hex-dump (default: 256)")

    p_pck = sub.add_parser("unpack-pck", help="Extract .bnk/.wem entries from a Wwise AKPK .pck file")
    p_pck.add_argument("pck", type=Path, help="Path to .pck file")
    p_pck.add_argument("out_dir", type=Path, help="Destination directory")
    p_pck.add_argument("--limit", type=int, default=0, help="Max entries to extract (0 = all)")

    p_blk = sub.add_parser("scan-blk", help="Dump Unity asset JSONs from a .blk file via AnimeStudio.CLI")
    p_blk.add_argument("blk", type=Path, help="Path to .blk file")
    p_blk.add_argument("--out-dir", type=Path, default=None, help="Output directory (defaults to cache)")
    p_blk.add_argument("--types", nargs="+", default=None, help="Unity asset types to dump")

    p_exp = sub.add_parser(
        "export",
        help="Batch/pipeline export: character, animations, map, stage, fonts, assets, audio, or full",
    )
    p_exp.add_argument(
        "scope",
        choices=["character", "animations", "map", "stage", "scene", "fonts", "assets", "audio", "full"],
        help="What to export",
    )
    p_exp.add_argument("--name", default="", help="Character, map, or stage name (or 'all' for maps)")
    p_exp.add_argument("--root", type=Path, default=GAME_ROOT, help="Path to ZZZ build root")
    p_exp.add_argument("--source", type=Path, default=None, help="Source .blk, .pck, or folder (for fonts/assets/audio)")
    p_exp.add_argument("--out-dir", type=Path, default=FINAL_ROOT / "Exports", help="Destination directory")
    p_exp.add_argument("--types", nargs="+", default=["Texture2D", "Sprite"], help="Unity asset types (for 'assets' scope)")
    p_exp.add_argument("--mode", choices=["Convert", "JSON", "Raw"], default="Convert", help="AnimeStudio CLI export mode")
    p_exp.add_argument("--filter", default="", help="Asset name substring filter (for 'assets' scope)")
    p_exp.add_argument("--glb", action="store_true", help="Convert character FBX to GLB + facial morphs via Blender")
    p_exp.add_argument("--no-resume", action="store_true", help="Disable resume for map/stage pipeline")
    p_exp.add_argument("--workers", type=int, default=1, help="Concurrent map workers")
    p_exp.add_argument("--limit", type=int, default=0, help="Max audio entries per .pck (0 = all)")
    p_exp.add_argument("--no-wwise-map", action="store_true", help="Disable AnimeWwise nap.map auto-naming/sorting")
    p_exp.add_argument("--wav", action="store_true", help="Decode extracted .wem audio to .wav via vgmstream-cli")
    p_exp.add_argument("--skip-chars", action="store_true", help="Skip characters in 'full' export")
    p_exp.add_argument("--skip-scenes", action="store_true", help="Skip scenes in 'full' export")
    p_exp.add_argument("--skip-fonts", action="store_true", help="Skip fonts in 'full' export")
    p_exp.add_argument("--skip-audio", action="store_true", help="Skip audio in 'full' export")
    p_exp.add_argument("--dry-run", action="store_true", help="Preview 'full' export plan without running")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.cmd or "gui"

    if cmd == "gui":
        launch_gui(getattr(args, "root", GAME_ROOT))
        return 0

    if cmd == "version":
        v = collect_game_version(args.root)
        if args.json:
            print(json.dumps(v, indent=2))
        else:
            for k, val in v.items():
                print(f"{k:18s}: {val}")
        return 0

    if cmd == "inspect":
        if not args.file.is_file():
            print(f"File not found: {args.file}", file=sys.stderr)
            return 1
        magic, _ = detect_magic(args.file)
        print(f"File   : {args.file}")
        print(f"Size   : {args.file.stat().st_size:,} bytes ({human_size(args.file.stat().st_size)})")
        print(f"Format : {magic}")
        if args.file.suffix.lower() == ".pck":
            ak = parse_akpk(args.file)
            print(f"AKPK   : version={ak.get('version')} entries={len(ak.get('entries', []))}")
        off = int(args.offset, 16) if str(args.offset).lower().startswith("0x") else int(args.offset)
        with args.file.open("rb") as fh:
            fh.seek(max(0, off))
            print(format_hex_dump(fh.read(args.length), max(0, off)))
        return 0

    if cmd == "unpack-pck":
        ak = parse_akpk(args.pck)
        if ak.get("error"):
            print(ak["error"], file=sys.stderr)
            return 1
        args.out_dir.mkdir(parents=True, exist_ok=True)
        entries = ak["entries"][: args.limit] if args.limit > 0 else ak["entries"]
        with args.pck.open("rb") as src:
            for e in entries:
                src.seek(e["offset"])
                (args.out_dir / e["name"]).write_bytes(src.read(e["size"]))
        print(f"Extracted {len(entries)} entries to {args.out_dir}")
        return 0

    if cmd == "scan-blk":
        out_dir, rows, _log = scan_blk_json(args.blk, out_dir=args.out_dir, types=args.types)
        print(f"Scanned {args.blk.name}: {len(rows)} JSON assets in {out_dir}")
        for r in rows[:25]:
            print(f"  {r['type']:16s} {r['name']}")
        return 0

    if cmd == "export":
        files_root = resolve_files_root(args.root)
        scope = args.scope
        if scope == "character":
            if not args.name:
                print("--name is required for 'character' export", file=sys.stderr)
                return 2
            return export_character_full(args.name, out_dir=args.out_dir, convert_glb=args.glb)
        if scope == "animations":
            if not args.name:
                print("--name is required for 'animations' export", file=sys.stderr)
                return 2
            return export_character_animations_only(args.name, args.root, args.out_dir)
        if scope in ("map", "stage", "scene"):
            return export_scene_map(
                args.name or "all",
                resume=not args.no_resume,
                workers=args.workers,
                out_dir=args.out_dir,
            )
        if scope == "fonts":
            src = args.source or (files_root / "Blocks")
            return export_blk_asset_types(src, args.out_dir / "Fonts", ["Font"], "Convert")
        if scope == "assets":
            src = args.source or (files_root / "Blocks")
            return export_blk_asset_types(src, args.out_dir / "Assets", args.types, args.mode, args.filter)
        if scope == "audio":
            src = args.source or (files_root / "Audio")
            return export_audio_pck(
                src, args.out_dir / "Audio",
                limit=args.limit,
                use_wwise_map=not args.no_wwise_map,
                decode_wav=args.wav,
            )
        if scope == "full":
            return export_full_game(
                args.root,
                args.out_dir,
                include_chars=not args.skip_chars,
                include_scenes=not args.skip_scenes,
                include_fonts=not args.skip_fonts,
                include_audio=not args.skip_audio,
                convert_glb=args.glb,
                use_wwise_map=not args.no_wwise_map,
                decode_wav=args.wav,
                dry_run=args.dry_run,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
