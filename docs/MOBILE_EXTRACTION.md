# Extracting Your Mobile (Android) Copy of ZZZ to PC

This guide covers copying your own installed Android copy of **Zenless Zone Zero** (`com.HoYoverse.Nap`) onto your PC for browsing and exporting with **OpenZenless**.

## Option 1: Using ADB (Recommended)

1. Enable **Developer Options** and **USB Debugging** on your Android device, then connect via USB.
2. Verify the device is detected:
   ```bash
   adb devices
   ```
3. Pull the game asset folder (`Blocks/`, `Audio/`, `Video/`, `il2cpp/`, and revision files):
   ```bash
   mkdir -p ~/ZZZ_Android_Build/com.HoYoverse.Nap
   adb pull /sdcard/Android/data/com.HoYoverse.Nap/files ~/ZZZ_Android_Build/com.HoYoverse.Nap/files
   ```
   *(If Android 13+ Scoped Storage blocks `adb pull` on `/sdcard/Android/data/` over USB, use **Option 2** below.)*
4. *(Optional)* Pull the installed APKs (for `libil2cpp.so`, `libunity.so`, and `split_UnityDataAssetPack`):
   ```bash
   mkdir -p ~/ZZZ_Android_Build/APKs
   for p in $(adb shell pm path com.HoYoverse.Nap | sed 's/package://g' | tr -d '\r'); do
     adb pull "$p" ~/ZZZ_Android_Build/APKs/
   done
   ```

## Option 2: Shizuku + File Manager (Android 13–15 Scoped Storage)

1. Install **Shizuku** and a Shizuku-compatible file manager (such as **MT Manager**, **FV File Explorer**, or **MixPlorer**).
2. Grant the file manager access via Shizuku, then copy `/sdcard/Android/data/com.HoYoverse.Nap/files/` to `/sdcard/Download/com.HoYoverse.Nap/files/`.
3. Transfer `/sdcard/Download/com.HoYoverse.Nap/` to your PC over USB (MTP) or `adb pull`.

## Expected Folder Layout

```text
ZZZ_Android_Build/
├── com.HoYoverse.Nap/
│   └── files/
│       ├── Blocks/           # .blk asset containers
│       ├── Audio/Android/    # Wwise .pck archives
│       ├── il2cpp/Metadata/  # global-metadata.dat
│       ├── psoversioncode
│       ├── base_revision
│       ├── res_revision
│       └── data_revision
└── APKs/                     # Optional extracted split APKs
```

## Opening in OpenZenless

```bash
./run.sh gui --root ~/ZZZ_Android_Build
# or click "Open Root..." in the topbar and select ~/ZZZ_Android_Build
```
