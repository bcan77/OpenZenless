"""Command-line interface for OpenZenless."""
from __future__ import annotations

import argparse
import json
import sys
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openzenless",
        description="OpenZenless — OpenIV-style ZZZ proprietary file viewer & batch exporter.",
    )
    sub = p.add_subparsers(dest="cmd")

    p_gui = sub.add_parser("gui", help="Launch the PySide6 desktop GUI (default)")
    p_gui.add_argument("--root", type=Path, default=cfg.GAME_ROOT, help="Path to ZZZ Android/PC build root")

    p_ver = sub.add_parser("version", help="Check ZZZ build revisions, container counts, and CLI/Wine health")
    p_ver.add_argument("--root", type=Path, default=cfg.GAME_ROOT, help="Path to ZZZ Android/PC build root")
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
    p_exp.add_argument("--root", type=Path, default=cfg.GAME_ROOT, help="Path to ZZZ build root")
    p_exp.add_argument("--source", type=Path, default=None, help="Source .blk, .pck, or folder (for fonts/assets/audio)")
    p_exp.add_argument("--out-dir", type=Path, default=cfg.FINAL_ROOT / "Exports", help="Destination directory")
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
        from .gui import launch_gui
        launch_gui(getattr(args, "root", cfg.GAME_ROOT))
        return 0

    if cmd == "version":
        v = cfg.collect_game_version(args.root)
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
        files_root = cfg.resolve_files_root(args.root)
        scope = args.scope
        if scope == "character":
            if not args.name:
                print("--name is required for 'character' export", file=sys.stderr)
                return 2
            return export_character_full(args.name, out_dir=args.out_dir, convert_glb=args.glb, game_root=args.root)
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
