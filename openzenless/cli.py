"""Command-line interface for OpenZenless."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as cfg
cfg.configure_gpu_env()
from .adb_import import (
    detect_remote_files_dir,
    detect_zzz_package,
    ensure_adb_available,
    find_adb,
    list_adb_devices,
    pull_from_phone,
)
from .cdn_downloader import (
    DOWNLOAD_PROFILES,
    VOICE_LANGS,
    download_pc_build,
    fetch_zzz_cdn_info,
)
from .containers import detect_magic, format_hex_dump, human_size, parse_akpk, scan_blk_json
from .tools import (
    ensure_adb,
    ensure_blender,
    ensure_dotnet,
    ensure_extractor_tools,
    ensure_vgmstream,
    ensure_wine,
    get_environment_status,
)
from .exporters import (
    export_audio_pck,
    export_blk_asset_types,
    export_character_animations_only,
    export_character_full,
    export_data_pack,
    export_entity_full,
    export_fonts,
    export_full_game,
    export_scene_map,
    export_spine_rig,
    export_ui_sprite_group,
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

    p_phone = sub.add_parser("pull-phone", help="Pull ZZZ game files & split APKs from an ADB-connected Android phone")
    p_phone.add_argument("--dest", type=Path, default=Path.home() / "OpenZenless_ZZZ_Android", help="Destination folder")
    p_phone.add_argument("--serial", default="", help="ADB device serial (optional if one device connected)")
    p_phone.add_argument("--pkg", default="", help="Android package name (default: auto-detect com.HoYoverse.Nap / com.miHoYo.Nap)")
    p_phone.add_argument("--remote-dir", default="", help="On-device files/ directory (e.g. /sdcard/Download/com.HoYoverse.Nap/files)")
    p_phone.add_argument("--no-files", action="store_true", help="Skip pulling files/ game assets")
    p_phone.add_argument("--no-apks", action="store_true", help="Skip pulling split APKs")
    p_phone.add_argument("--no-extract-apk-blocks", action="store_true", help="Skip extracting base .blk files from split_UnityDataAssetPack.apk")
    p_phone.add_argument("--no-auto-install-adb", action="store_true", help="Do not auto-install Google Platform-Tools (adb) if missing")

    p_cdn = sub.add_parser("download-pc", help="Download official ZZZ PC build directly from HoYoverse CDN")
    p_cdn.add_argument("--dest", type=Path, default=Path.home() / "OpenZenless_ZZZ_PC", help="Destination root folder")
    p_cdn.add_argument("--region", choices=["global", "cn"], default="global", help="HoYoverse server region (default: global)")
    p_cdn.add_argument("--profile", choices=list(DOWNLOAD_PROFILES.keys()), default="assets", help="Download scope (default: assets)")
    p_cdn.add_argument(
        "--langs", nargs="*", default=[], choices=[c for c, _ in VOICE_LANGS],
        help="Optional voice language packs (en-us, ja-jp, zh-cn, ko-kr)",
    )
    p_cdn.add_argument("--workers", type=int, default=4, help="Concurrent download workers (1-8, default: 4)")
    p_cdn.add_argument("--info", action="store_true", help="Query live version & profile sizes without downloading")
    p_cdn.add_argument("--ignore-space-check", action="store_true", help="Skip free disk space verification")

    p_tools = sub.add_parser("setup-tools", help="Check or auto-install portable tools (AnimeStudio.CLI, Wine, .NET 9, ADB, vgmstream, Blender)")
    p_tools.add_argument("--check", action="store_true", help="Print detected OS, Python deps & tool health without installing")
    p_tools.add_argument("--json", action="store_true", help="Output environment status JSON (with --check)")
    p_tools.add_argument("--all", action="store_true", help="Auto-install core tools plus ADB and Blender")
    p_tools.add_argument("--adb", action="store_true", help="Auto-install Google Android Platform-Tools (adb)")
    p_tools.add_argument("--vgmstream", action="store_true", help="Auto-install portable vgmstream-cli")
    p_tools.add_argument("--wine", action="store_true", help="Auto-install portable WoW64 Wine (Linux x86_64)")
    p_tools.add_argument("--dotnet", action="store_true", help="Auto-install portable .NET 9 SDK")
    p_tools.add_argument("--blender", action="store_true", help="Auto-install portable Blender 4.2 LTS")

    p_exp = sub.add_parser(
        "export",
        help="Batch/pipeline export: character, animations, monster, npc, weapon, prop, map, stage, spine, sprites, data, fonts, assets, audio, or full",
    )
    p_exp.add_argument(
        "scope",
        choices=[
            "character", "animations", "monster", "npc", "weapon", "prop",
            "map", "stage", "scene", "spine", "sprites", "data",
            "fonts", "assets", "audio", "full",
        ],
        help="What to export",
    )
    p_exp.add_argument("--name", default="", help="Character, entity, map, stage, spine rig, sprite pack, or data pack name")
    p_exp.add_argument("--root", type=Path, default=cfg.GAME_ROOT, help="Path to ZZZ build root")
    p_exp.add_argument("--source", type=Path, default=None, help="Source .blk, .pck, or folder (for fonts/assets/audio)")
    p_exp.add_argument("--out-dir", type=Path, default=cfg.FINAL_ROOT / "Exports", help="Destination directory")
    p_exp.add_argument("--types", nargs="+", default=["Texture2D", "Sprite"], help="Unity asset types (for 'assets' scope)")
    p_exp.add_argument("--mode", choices=["Convert", "JSON", "Raw"], default="Convert", help="AnimeStudio CLI export mode")
    p_exp.add_argument("--filter", default="", help="Asset name substring filter (for 'assets' scope)")
    p_exp.add_argument("--glb", action="store_true", help="Convert character/entity FBX to GLB + facial morphs via Blender")
    p_exp.add_argument("--no-resume", action="store_true", help="Disable resume for map/stage pipeline")
    p_exp.add_argument("--workers", type=int, default=1, help="Concurrent map workers")
    p_exp.add_argument("--limit", type=int, default=0, help="Max audio entries per .pck (0 = all)")
    p_exp.add_argument("--no-wwise-map", action="store_true", help="Disable AnimeWwise nap.map auto-naming/sorting")
    p_exp.add_argument("--wav", action="store_true", help="Decode extracted .wem audio to .wav via vgmstream-cli")
    p_exp.add_argument("--no-bnk", action="store_true", help="Do not extract embedded .wem entries from Wwise .bnk SoundBanks")
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

    if cmd == "pull-phone":
        adb_exe = find_adb()
        if not adb_exe:
            if args.no_auto_install_adb:
                print("Error: `adb` executable not found in PATH. Install Android Platform Tools or drop --no-auto-install-adb.", file=sys.stderr)
                return 1
            print("`adb` not found — auto-installing portable Google Platform-Tools...", flush=True)
            adb_exe = ensure_adb_available(log_cb=print)
        if not adb_exe:
            print("Error: Could not install `adb`. Tip: Download the official PC build directly with: openzenless download-pc", file=sys.stderr)
            return 1
        devices = [d for d in list_adb_devices(adb=adb_exe) if d["state"] == "device"]
        serial = args.serial.strip()
        if not serial:
            if not devices:
                print("Error: No authorized ADB device found. Check USB Debugging and `adb devices`.", file=sys.stderr)
                print("Tip: No Android device? Download the official PC build directly with: openzenless download-pc", file=sys.stderr)
                return 1
            if len(devices) > 1:
                print("Multiple ADB devices connected; pass --serial:", file=sys.stderr)
                for d in devices:
                    print(f"  {d['serial']} ({d['model']})", file=sys.stderr)
                return 2
            serial = devices[0]["serial"]
        pkg = args.pkg.strip()
        if not pkg:
            pkg, _apks = detect_zzz_package(serial, adb=adb_exe)
        if not pkg:
            print(f"Error: ZZZ package (com.HoYoverse.Nap / com.miHoYo.Nap) not found on {serial}.", file=sys.stderr)
            return 1
        remote_dir = args.remote_dir.strip()
        if not args.no_files and not remote_dir:
            remote_dir, msg = detect_remote_files_dir(serial, pkg, adb=adb_exe)
            if not remote_dir:
                print(f"Error: {msg}", file=sys.stderr)
                return 1
        try:
            out = pull_from_phone(
                dest_root=args.dest,
                serial=serial,
                pkg=pkg,
                remote_files_dir=remote_dir or None,
                pull_files=not args.no_files,
                pull_apks=not args.no_apks,
                extract_apk_blocks=not args.no_extract_apk_blocks,
                adb=adb_exe,
                log_cb=print,
            )
            print(f"Saved ZZZ files to: {out}")
            return 0
        except Exception as ex:
            print(f"Error: {ex}", file=sys.stderr)
            return 1

    if cmd == "download-pc":
        try:
            if args.info:
                info = fetch_zzz_cdn_info(region=args.region, parse_game_manifest=True)
                print(f"Region  : {info['region_label']} ({info['region']})")
                print(f"Version : {info['version']} (build_id={info['build_id']})")
                print("Profiles:")
                for pk, st in info["profiles"].items():
                    gb = st["size"] / (1024 ** 3)
                    print(f"  {pk:8s}: {st['file_count']:6d} files, {gb:6.2f} GB — {DOWNLOAD_PROFILES[pk]}")
                print("Voice Packs:")
                for code, label in VOICE_LANGS:
                    v = info["voices"].get(code)
                    if v:
                        gb = v["size"] / (1024 ** 3)
                        print(f"  {code:8s}: {v['file_count']:5d} files, {gb:5.2f} GB ({label})")
                return 0
            out = download_pc_build(
                dest_root=args.dest,
                region=args.region,
                profile=args.profile,
                voice_langs=args.langs,
                workers=args.workers,
                log_cb=print,
                ignore_space_check=args.ignore_space_check,
            )
            print(f"Saved official ZZZ PC build to: {out}")
            return 0
        except Exception as ex:
            print(f"Error: {ex}", file=sys.stderr)
            return 1

    if cmd == "setup-tools":
        if args.check:
            st = get_environment_status()
            if args.json:
                print(json.dumps(st, indent=2))
                return 0
            print(f"OS        : {st['os_detail']} ({st['arch']}, {st['display']}, pkg={st['pkg_manager']})")
            print(f"Python    : {st['python']} ({', '.join(k for k, ok in st['python_modules'].items() if ok)})")
            print(f"Tools Dir : {st['tools_dir']}")
            missing_py = [k for k, ok in st["python_modules"].items() if not ok]
            if missing_py:
                print(f"Missing Py: {', '.join(missing_py)}")
            print("External Tools:")
            for row in st["tools"]:
                loc = row["path"] or row["hint"]
                print(f"  {row['name']:26s}: {row['status']:14s} | {loc}")
            return 0
        specific = any((args.adb, args.vgmstream, args.wine, args.dotnet, args.blender))
        if specific and not args.all:
            if args.adb:
                ensure_adb(log_cb=print)
            if args.vgmstream:
                ensure_vgmstream(log_cb=print)
            if args.wine:
                ensure_wine(log_cb=print)
            if args.dotnet:
                ensure_dotnet(log_cb=print)
            if args.blender:
                ensure_blender(log_cb=print)
            return 0
        res = ensure_extractor_tools(
            log_cb=print,
            install_adb=args.all,
            install_blender=args.all,
            install_wine=True,
            install_vgmstream=True,
        )
        print(json.dumps(res, indent=2))
        return 0 if res.get("cli_exists") else 1

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
        if scope in ("monster", "npc", "weapon", "prop"):
            if not args.name:
                print(f"--name is required for '{scope}' export", file=sys.stderr)
                return 2
            cat_map = {"monster": "monsters", "npc": "npcs", "weapon": "weapons", "prop": "props"}
            return export_entity_full(args.name, category=cat_map[scope], out_dir=args.out_dir, convert_glb=args.glb, game_root=args.root)
        if scope in ("spine", "sprites", "data"):
            if not args.name:
                print(f"--name is required for '{scope}' export", file=sys.stderr)
                return 2
            if scope == "spine":
                return export_spine_rig(args.name, args.root, args.out_dir)
            if scope == "sprites":
                return export_ui_sprite_group(args.name, args.root, args.out_dir)
            return export_data_pack(args.name, args.root, args.out_dir)
        if scope in ("map", "stage", "scene"):
            return export_scene_map(
                args.name or "all",
                resume=not args.no_resume,
                workers=args.workers,
                out_dir=args.out_dir,
                game_root=args.root,
            )
        if scope == "fonts":
            return export_fonts(args.root, args.out_dir / "Fonts", source_path=args.source)
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
                extract_bnk=not args.no_bnk,
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
