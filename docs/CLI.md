# OpenZenless CLI Reference

Run commands via `./run.sh <subcommand>` or `python3 openzenless.py <subcommand>`.

## `gui` — Launch Desktop Application
```bash
./run.sh gui [--root /path/to/ZZZ_Build]
```

## `version` — Check Game Build & Tool Health
```bash
./run.sh version [--root /path/to/ZZZ_Build] [--json]
```
Reads `psoversioncode`, `base_revision`, `res_revision`, `data_revision`, `audio_revision`, `silence_revision`, `op_version`, `.blk`/`.pck` counts, and checks `AnimeStudio.CLI.exe` + `wine`.

## `inspect` — Binary Magic, AKPK Summary & Hex Dump
```bash
./run.sh inspect <file> [--offset 0x0] [--length 256]
```

## `unpack-pck` — Extract Raw Entries from a Wwise `.pck`
```bash
./run.sh unpack-pck <file.pck> <out_dir> [--limit N]
```

## `scan-blk` — Dump Unity Asset JSONs from a `.blk`
```bash
./run.sh scan-blk <file.blk> [--out-dir /path/to/out] [--types Texture2D Material Shader]
```

## `export` — Scoped & Full Pipeline Exports
```bash
# Whole character (FBX + textures, optionally + Blender GLB & facial morphs)
./run.sh export character --name Avatar_Female_Size02_Anby [--glb] [--out-dir ./Exports]

# Character animations only (AnimationClip / Animator / Avatar)
./run.sh export animations --name Avatar_Female_Size02_Anby --out-dir ./Exports

# Curated map or 'all' curated maps
./run.sh export map --name FortuneHIA [--workers 1] [--out-dir ./Exports]
./run.sh export map --name all --out-dir ./Exports

# Raw stage export
./run.sh export stage --name Stage_ConstructionSite_2_3 --out-dir ./Exports

# Fonts (TTF/OTF)
./run.sh export fonts [--source /path/to/Blocks_or_file.blk] --out-dir ./Exports

# Custom Unity asset types
./run.sh export assets --types Texture2D Shader --mode Convert [--filter UI] --out-dir ./Exports

# Wwise .pck audio with AnimeWwise nap.map auto-naming + optional WAV decode
./run.sh export audio [--source /path/to/Audio_or_file.pck] [--wav] [--no-wwise-map] [--limit 0] --out-dir ./Exports

# Full export (Characters + Curated Maps + Fonts + Voices + Catalogs)
./run.sh export full [--glb] [--wav] [--skip-chars] [--skip-scenes] [--skip-fonts] [--skip-audio] [--dry-run] --out-dir ./Exports
```
