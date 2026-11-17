<div align="center">
  <img src="assets/OpenZenless_Header.png" alt="OpenZenless Logo" width="460" />
  <p><strong>Zenless Zone Zero asset viewer & extractor, for the people who just want to create!.</strong></p>
  

  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
    <img src="https://img.shields.io/badge/UI-PySide6%20%2B%20QtWebEngine-41CD52?style=for-the-badge&logo=qt&logoColor=white" alt="PySide6" />
    <img src="https://img.shields.io/badge/Game-Zenless%20Zone%20Zero%20v3.4-F59E0B?style=for-the-badge" alt="ZZZ v3.4" />
    <img src="https://img.shields.io/badge/Containers-.blk%20%7C%20.pck%20(AKPK)-38BDF8?style=for-the-badge" alt="Containers" />
  </p>
</div>

---

*NOTE : This program is HIGHLY EXPERIMENTAL, and may not work on your device. Windows support is unavailable until I can manage to verify that this build can export and view all textures/assets reliably.*

## How the program works;

- **3 Viewer Model (`Basic` | `Advanced` | `3D Viewer`)**
  - **Basic**: Browse organized **Characters**, **Curated Maps**, **Texture2D Sections** (UI, Comics, Story, Season 2, Effects, etc.), **Audio Archives**, and **Fonts** with options for neatly exporting them.
  - **Advanced Mode**: OpenIV-style raw archive browser for `.blk` containers, Wwise `.pck` (`AKPK`) packages, Unity asset JSON dumps, PNG texture previews, and a 4 KB paged Hex + ASCII viewer.
  - **3D Viewer**: Embedded `QWebEngineView` serving the interactive Three.js GLB + Catalog viewer for exported characters, animations, facial morphs, and maps.
- **Scoped & Full Pipeline Exports**
  - Whole Characters (FBX + Textures + Blender GLB + Facial Morphs)
  - Character Animations Only (`AnimationClip` / `Animator` / `Avatar`)
  - Curated Maps vs. Raw Stages (separated cleanly)
  - Fonts (`.ttf` / `.otf`) & Custom Unity Asset Types (`Texture2D`, `Sprite`, `Shader`, `TextAsset`, `Mesh`, etc.)
  - Wwise `.pck` Auto-Naming & Voice Organization via `AnimeWwise` (`nap.map`) + optional `.wem` → `.wav` decoding via `vgmstream-cli`
- **Live Build & Environment Verification**
  - Reads `psoversioncode`, `base_revision`, `res_revision`, `data_revision`, `audio_revision`, `silence_revision`, `op_version`, `.blk`/`.pck` counts, and `AnimeStudio.CLI` + `Wine` health.

---

## 📸 Screenshots

### Basic Mode (Organized Asset Browser)
![OpenZenless Basic Mode](docs/screenshots/basic_mode.png)

### Advanced Mode (OpenIV-Style Container & Hex Inspector)
![OpenZenless Advanced Mode](docs/screenshots/advanced_mode.png)

### Batch & Pipeline Export Wizard
![OpenZenless Batch Export](docs/screenshots/batch_export.png)

---

## 🏗️ Architecture

```mermaid
flowchart LR
    A[ZZZ Build Root<br/>.blk & .pck] --> B[OpenZenless<br/>PySide6 + CLI]
    B --> C[Basic Mode<br/>Chars / Maps / Texture2D / Audio / Fonts]
    B --> D[Advanced Mode<br/>AKPK / JSON / Preview / Hex]
    B --> E[3D Viewer<br/>QtWebEngine + Local HTTP]
    B --> F[Pipeline Exporters<br/>AnimeStudio.CLI + Blender + AnimeWwise + vgmstream]
    F --> G[FinalOutput<br/>Characters / Maps_Combined / Audio / Fonts / Catalogs]
    G --> E
```

---

## 🚀 Quick Start

```bash
# Install Python dependencies
pip install -r requirements.txt

# Launch the desktop GUI
./run.sh

# Or run CLI commands directly
./run.sh version
./run.sh export full --dry-run
```

### Environment Variables (Optional)
| Variable | Default | Description |
| :--- | :--- | :--- |
| `ZZZ_GAME_ROOT` | `~/.config/openzenless/config.json` (`$HOME` fallback) | Path to ZZZ installation root |
| `ZZZ_FINAL_OUTPUT` | `~/OpenZenless_Output` | Path to output directory (exports, `Extractor/`, `Viewer/`) |
| `ZZZ_BLK_EXPLORER_CACHE` | `$TMPDIR/zzz_blk_explorer` | Cache directory for decoded previews, asset maps & `.blk` JSON dumps |

---

## 📚 Documentation

- **[GUI Guide](docs/GUI.md)** — Walkthrough of Basic Mode, Advanced Mode, 3D Viewer, Batch Export, and shortcuts.
- **[CLI Reference](docs/CLI.md)** — Full command reference for `version`, `inspect`, `unpack-pck`, `scan-blk`, and `export`.
- **[Mobile (Android) Extraction Guide](docs/MOBILE_EXTRACTION.md)** — How to copy your mobile ZZZ build (`com.HoYoverse.Nap`) to PC via ADB or Shizuku.
- **[PC Copy Guide](docs/PC_GUIDE.md)** — How to browse and export directly from a PC (Windows) installation of ZZZ.
- **[Assets](assets/README.md)** — Brand logo variants.
