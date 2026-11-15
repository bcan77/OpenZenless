# OpenZenless GUI Guide

## Topbar Controls
- **Basic | Advanced | 3D Viewer**: Switch between the organized asset browser, the OpenIV-style raw file/hex/CLI inspector, and the embedded 3D GLB viewer.
- **Version Pill**: Displays live Unity `psoversioncode`, `res_revision`, `.blk` count, and `CLI+Wine` status. Click it for the full build & environment check dialog.
- **Open Root...**: Point OpenZenless at any ZZZ Android/PC build folder.
- **Batch Export... (`Ctrl+E`)**: Open the pipeline export wizard for Characters, Animations Only, Curated Maps, Raw Stages, Fonts, Custom Asset Types, Wwise Audio, or Full Export.
- **Docs / Help**: View documentation inside the app.

## Basic Mode
1. Choose a category on the left (**Characters**, **Curated Maps**, **Texture2D Sections**, **Audio Archives**, **Fonts & Localization**).
2. Filter items using the live search bar.
3. Review full container/export status on the right, then click **Open in Advanced Mode** or run a targeted export.

## Advanced Mode
1. Browse `Blocks/`, `Audio/`, `il2cpp/Metadata/`, `Video/`, extracted APKs, or Stage/Character bookmarks on the left.
2. Select any file or container in the center table (`Ctrl+F` to filter, `F5` to refresh) and click **Open / View** (or double-click):
   - **Folders**: Navigate inside.
   - **`.pck` (`AKPK`)**: Unpack and browse embedded `.bnk`/`.wem` entries natively.
   - **`.blk`**: Dump Unity asset JSONs via `AnimeStudio.CLI.exe`.
   - **Images (`.png`/`.jpg`/`.webp`/`.bmp`)**: Auto-render in **Visual Preview**.
   - **Text/JSON/Config**: Display in **Properties / Dump**.
   - **Other Binaries**: Inspect in the 4 KB paged **Hex Viewer** with arbitrary `0x...` offset jump.

## 3D Viewer Mode
- Automatically starts a local loopback HTTP server for `FinalOutput/` and embeds `Viewer/index.html` inside the app.
- Use **Rebuild Catalog & Reload** after new exports to refresh `Catalogs/` and `Viewer/viewer_data.json`.
