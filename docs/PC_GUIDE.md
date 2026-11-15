# Using OpenZenless with the PC Copy of ZZZ

**OpenZenless** automatically detects both **Android** and **PC (Windows)** installations of Zenless Zone Zero.

## Supported PC Root Paths

When clicking **Open Root...** or passing `--root` / `ZZZ_GAME_ROOT`, you can point OpenZenless to **any** of these folders:

1. **HoYoPlay Install Folder**: `<InstallDir>/ZenlessZoneZero Game/`
2. **Data Folder**: `<InstallDir>/ZenlessZoneZero Game/ZenlessZoneZero_Data/`
3. **StreamingAssets Folder**: `<InstallDir>/ZenlessZoneZero Game/ZenlessZoneZero_Data/StreamingAssets/`

OpenZenless automatically resolves `Blocks/`, `Audio/Windows/`, `ZenlessZoneZero_Data/`, and revision metadata (`psoversioncode`, `res_revision`, `data_revision`, `audio_revision`, etc.).

## Typical PC Folder Layout

```text
ZenlessZoneZero Game/
├── ZenlessZoneZero.exe
└── ZenlessZoneZero_Data/
    ├── Managed/
    ├── il2cpp_data/
    │   └── Metadata/global-metadata.dat
    ├── Persistent/
    └── StreamingAssets/
        ├── Blocks/           # .blk asset containers
        ├── Audio/Windows/    # Wwise .pck archives (En, Jp, Cn, Kr, SFX)
        ├── Video/
        ├── psoversioncode
        ├── res_revision
        └── data_revision
```

## Quick Commands for a PC Copy

```bash
# Check detected PC build version, revisions, and container counts
./run.sh version --root "/path/to/ZenlessZoneZero Game"

# Launch the GUI pointed at your PC install
./run.sh gui --root "/path/to/ZenlessZoneZero Game"

# Unpack & auto-name PC Wwise .pck audio via AnimeWwise nap.map
./run.sh export audio --source "/path/to/ZenlessZoneZero Game/ZenlessZoneZero_Data/StreamingAssets/Audio/Windows/Full/En" --wav --out-dir ./Exports
```

> **Tip:** In **Advanced Mode**, OpenZenless adds both **`Game Files (PC (Windows))`** (`StreamingAssets/`) and **`ZenlessZoneZero_Data (PC)`** to the left archive tree so you can browse `il2cpp_data/`, `Persistent/`, `Blocks/`, and `Audio/Windows/` side by side.
