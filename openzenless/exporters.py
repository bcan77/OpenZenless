"""Batch and pipeline exporters for characters, animations, maps, fonts, assets, and audio."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as cfg
from .containers import parse_akpk
from .indexer import (
    _emit,
    load_character_index,
    load_curated_map_index,
    load_wwise_mapper,
)


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
    if not source_path.exists():
        raise FileNotFoundError(f"Source path not found: {source_path}")
    if export_mode == "Raw" and not cfg.CLI_EXE.exists():
        out_dir.mkdir(parents=True, exist_ok=True)
        blks = [source_path] if source_path.is_file() else sorted(source_path.rglob("*.blk"))
        if not blks:
            raise FileNotFoundError(f"No .blk files found under {source_path}")
        for b in blks:
            shutil.copy2(b, out_dir / b.name)
        _emit(log_cb, f"Copied {len(blks)} raw .blk container(s) -> {out_dir}")
        return 0
    if not cfg.CLI_EXE.exists():
        raise FileNotFoundError(f"AnimeStudio.CLI.exe not found at {cfg.CLI_EXE}")
    if shutil.which("wine") is None:
        raise FileNotFoundError("wine executable not found in PATH")
    out_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["WINEDEBUG"] = "-all"
    cmd = [
        "wine", str(cfg.CLI_EXE),
        "Z:" + str(source_path).replace("/", "\\"),
        "Z:" + str(out_dir).replace("/", "\\"),
        "--game", "ZZZ",
        "--types", *types,
        "--group_assets", "ByType",
        "--export_type", export_mode,
    ]
    if name_filter.strip():
        cmd.extend(["--filter_with_name", name_filter.strip()])
    return run_streaming_cmd(cmd, cwd=cfg.CLI_DIR, env=env, log_cb=log_cb)


def export_character_full(
    char_name: str,
    out_dir: Path | None = None,
    convert_glb: bool = False,
    log_cb=None,
    game_root: Path | None = None,
) -> int:
    script = cfg.EXTRACTOR_ROOT / "lib" / "extract_character_v2.py"
    if not script.exists():
        chars = load_character_index()
        blocks = chars.get(char_name)
        if blocks and cfg.CLI_EXE.is_file() and shutil.which("wine") is not None:
            files_root = cfg.resolve_files_root(game_root or cfg.GAME_ROOT)
            blocks_dir = files_root / "Blocks"
            resolved = [blocks_dir / f"{os.path.basename(b)}" for b in blocks if (blocks_dir / f"{os.path.basename(b)}").is_file()]
            if not resolved:
                resolved = [blocks_dir / f"{b}.blk" for b in blocks if (blocks_dir / f"{b}.blk").is_file()]
            if resolved:
                dest = (out_dir or (cfg.FINAL_ROOT / "Exports")) / char_name
                _emit(log_cb, f"extract_character_v2.py not found; exporting {len(resolved)} .blk(s) via AnimeStudio.CLI -> {dest}")
                last_rc = 0
                for blk in resolved:
                    rc = export_blk_asset_types(
                        blk, dest,
                        types=["Mesh", "Texture2D", "AnimationClip", "Animator", "Avatar"],
                        export_mode="Convert",
                        log_cb=log_cb,
                    )
                    if rc != 0:
                        last_rc = rc
                return last_rc
        raise FileNotFoundError(
            f"Missing {script} (and AnimeStudio.CLI.exe fallback unavailable). "
            "Point OpenZenless to your FinalOutput/Extractor folder."
        )
    rc = run_streaming_cmd([sys.executable, str(script), char_name], log_cb=log_cb)
    if rc != 0:
        return rc
    if convert_glb:
        blender = shutil.which("blender")
        if not blender:
            _emit(log_cb, "WARNING: blender not found in PATH; skipping GLB conversion.")
        else:
            bl_script = cfg.EXTRACTOR_ROOT / "lib" / "blender_export_character.py"
            rc = run_streaming_cmd([blender, "-b", "--python", str(bl_script), "--", char_name], log_cb=log_cb)
            if rc != 0:
                return rc
            morph_script = cfg.EXTRACTOR_ROOT / "lib" / "inject_character_facial_morphs.py"
            if morph_script.exists():
                rc = run_streaming_cmd([sys.executable, str(morph_script), "--char", char_name, "--force"], log_cb=log_cb)
                if rc != 0:
                    return rc
    src_char_dir = cfg.FINAL_ROOT / "Characters" / char_name
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
    files_root = cfg.resolve_files_root(game_root)
    blocks_dir = files_root / "Blocks"
    resolved = [blocks_dir / f"{os.path.basename(b)}" for b in blocks if (blocks_dir / f"{os.path.basename(b)}").is_file()]
    if not resolved:
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
    script = cfg.EXTRACTOR_ROOT / "lib" / "export_maps_pipeline.py"
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
        maps_dir = cfg.FINAL_ROOT / "Maps_Combined"
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
        _emit(log_cb, f"Loading AnimeWwise map: {cfg.NAP_MAP_FILE.name}...")
        mapper = load_wwise_mapper()
        if mapper is None:
            _emit(log_cb, "WARNING: nap.map not found; falling back to raw AKPK filenames.")
    if decode_wav and not cfg.VGMSTREAM_CLI.is_file():
        _emit(log_cb, f"WARNING: vgmstream-cli not found at {cfg.VGMSTREAM_CLI}; keeping .wem files.")
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
                        [str(cfg.VGMSTREAM_CLI), "-o", str(wav_dest), str(dest)],
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
    files_root = cfg.resolve_files_root(game_root)
    chars = sorted(load_character_index().keys())
    maps = sorted(load_curated_map_index().keys())

    char_script = cfg.EXTRACTOR_ROOT / "lib" / "extract_character_v2.py"
    map_script = cfg.EXTRACTOR_ROOT / "lib" / "export_maps_pipeline.py"
    blocks_dir = files_root / "Blocks"
    audio_dir = files_root / "Audio"
    if not audio_dir.exists() and files_root.exists() and any(files_root.rglob("*.pck")):
        audio_dir = files_root
    has_pcks = audio_dir.exists() and any(audio_dir.rglob("*.pck"))
    has_cli = cfg.CLI_EXE.is_file() and shutil.which("wine") is not None

    chars_ready = include_chars and bool(chars) and (char_script.is_file() or (has_cli and blocks_dir.exists()))
    scenes_ready = include_scenes and map_script.is_file()
    fonts_ready = include_fonts and blocks_dir.exists() and has_cli
    audio_ready = include_audio and has_pcks

    def _status_str(enabled: bool, ready: bool, detail: str, missing_reason: str) -> str:
        if not enabled:
            return "SKIPPED"
        return f"{detail} (READY)" if ready else f"{detail} (UNAVAILABLE: {missing_reason})"

    char_reason = (
        f"missing {char_script.name}" if chars else "character_index_lean.json not found in Extractor/data/asset_map"
    )
    font_reason = "missing Blocks/" if not blocks_dir.exists() else "missing AnimeStudio.CLI.exe or wine"

    _emit(log_cb, "=== OpenZenless Full Export Plan ===")
    _emit(log_cb, f"Game Root   : {files_root} ({'OK' if files_root.exists() else 'MISSING'})")
    _emit(log_cb, f"Output Root : {out_dir}")
    _emit(log_cb, f"Characters  : {_status_str(include_chars, chars_ready, f'{len(chars)} (GLB={convert_glb})', char_reason)}")
    _emit(log_cb, f"Curated Maps: {_status_str(include_scenes, scenes_ready, f'{len(maps)}', f'missing {map_script.name}')}")
    _emit(log_cb, f"Fonts       : {_status_str(include_fonts, fonts_ready, 'Blocks/*.blk', font_reason)}")
    _emit(log_cb, f"Audio (.pck): {_status_str(include_audio, audio_ready, f'AnimeWwise={use_wwise_map}, WAV={decode_wav}', 'no .pck files found')}")
    if dry_run:
        _emit(log_cb, "[DRY RUN] No files were modified.")
        return 0

    if not files_root.exists():
        _emit(log_cb, f"[ERROR] Game root does not exist: {files_root}")
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    ran_any = False
    had_errors = False

    if include_chars:
        if not chars_ready:
            _emit(log_cb, f"[SKIP] Characters: {char_reason}")
        else:
            ran_any = True
            for idx, ch in enumerate(chars, 1):
                _emit(log_cb, f"[{idx}/{len(chars)}] Exporting character {ch}...")
                try:
                    rc = export_character_full(
                        ch, out_dir=out_dir / "Characters", convert_glb=convert_glb, log_cb=log_cb, game_root=game_root
                    )
                    if rc != 0:
                        had_errors = True
                        _emit(log_cb, f"[WARN] Character {ch} exited with code {rc}")
                except Exception as exc:
                    had_errors = True
                    _emit(log_cb, f"[ERROR] Character {ch} failed: {exc}")

    if include_scenes:
        if not scenes_ready:
            _emit(log_cb, f"[SKIP] Curated Maps: missing {map_script}")
        else:
            ran_any = True
            _emit(log_cb, "Exporting curated maps...")
            try:
                rc = export_scene_map("all", resume=True, workers=1, out_dir=out_dir / "Maps_Combined", log_cb=log_cb)
                if rc != 0:
                    had_errors = True
                    _emit(log_cb, f"[WARN] Curated Maps exited with code {rc}")
            except Exception as exc:
                had_errors = True
                _emit(log_cb, f"[ERROR] Curated Maps failed: {exc}")

    if include_fonts:
        if not fonts_ready:
            _emit(log_cb, f"[SKIP] Fonts: {font_reason}")
        else:
            ran_any = True
            _emit(log_cb, "Exporting all Fonts from Blocks/...")
            try:
                rc = export_blk_asset_types(blocks_dir, out_dir / "Fonts", ["Font"], "Convert", log_cb=log_cb)
                if rc != 0:
                    had_errors = True
                    _emit(log_cb, f"[WARN] Fonts export exited with code {rc}")
            except Exception as exc:
                had_errors = True
                _emit(log_cb, f"[ERROR] Fonts export failed: {exc}")

    if include_audio:
        if not audio_ready:
            _emit(log_cb, f"[SKIP] Audio: no .pck files found under {files_root}")
        else:
            ran_any = True
            _emit(log_cb, "Unpacking & organizing Wwise .pck archives...")
            try:
                rc = export_audio_pck(
                    audio_dir, out_dir / "Audio",
                    use_wwise_map=use_wwise_map, decode_wav=decode_wav, log_cb=log_cb,
                )
                if rc != 0:
                    had_errors = True
            except Exception as exc:
                had_errors = True
                _emit(log_cb, f"[ERROR] Audio export failed: {exc}")

    for lib_name in ("build_character_catalog.py", "build_viewer_data.py"):
        lib_script = cfg.EXTRACTOR_ROOT / "lib" / lib_name
        if lib_script.exists():
            run_streaming_cmd([sys.executable, str(lib_script)], log_cb=log_cb)

    if not ran_any:
        _emit(log_cb, "=== Full Export Finished (no stages were runnable with current Game/Extractor roots) ===")
        return 1
    _emit(log_cb, f"=== Full Export Complete{' (with warnings/errors)' if had_errors else ''} ===")
    return 1 if had_errors else 0
