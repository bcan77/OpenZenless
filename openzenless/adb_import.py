"""ADB device detection and Android ZZZ build importer for OpenZenless."""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

ZZZ_PACKAGES = ("com.HoYoverse.Nap", "com.miHoYo.Nap")


def find_adb() -> str | None:
    """Return path to the `adb` executable if installed on PATH."""
    return shutil.which("adb")


def _run_adb(args: list[str], serial: str | None = None, adb: str | None = None, timeout: int = 15) -> subprocess.CompletedProcess:
    exe = adb or find_adb()
    if not exe:
        raise FileNotFoundError("adb executable not found in PATH")
    cmd = [exe]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def parse_adb_devices_output(output: str) -> list[dict]:
    """Parse `adb devices -l` text into a list of device dicts."""
    devices: list[dict] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("List of devices") or line.startswith("* "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial = parts[0]
        state = parts[1]
        meta: dict[str, str] = {}
        for tok in parts[2:]:
            if ":" in tok:
                k, v = tok.split(":", 1)
                meta[k] = v
        model = meta.get("model", "")
        device_code = meta.get("device", "")
        product = meta.get("product", "")
        pretty_name = model or device_code or serial
        label = f"{pretty_name} [{serial}] ({state})"
        devices.append({
            "serial": serial,
            "state": state,
            "model": model,
            "device": device_code,
            "product": product,
            "label": label,
        })
    return devices


def list_adb_devices(adb: str | None = None) -> list[dict]:
    """Return connected ADB devices and their authorization states."""
    res = _run_adb(["devices", "-l"], adb=adb, timeout=10)
    return parse_adb_devices_output(res.stdout or "")


def _get_pkg_apk_paths(serial: str, pkg: str, adb: str | None = None) -> list[str]:
    res = _run_adb(["shell", "pm", "path", pkg], serial=serial, adb=adb, timeout=15)
    paths: list[str] = []
    for line in (res.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("package:"):
            paths.append(line[len("package:"):].strip())
    return paths


def detect_zzz_package(serial: str, adb: str | None = None) -> tuple[str | None, list[str]]:
    """Return `(package_name, apk_paths)` for the installed ZZZ package on `serial`."""
    for pkg in ZZZ_PACKAGES:
        try:
            paths = _get_pkg_apk_paths(serial, pkg, adb=adb)
        except Exception:
            paths = []
        if paths:
            return pkg, paths
    return None, []


def _ls_dir(serial: str, remote_dir: str, adb: str | None = None) -> tuple[bool, list[str], str]:
    res = _run_adb(["shell", "ls", "-1", remote_dir], serial=serial, adb=adb, timeout=10)
    combined = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
    if res.returncode != 0:
        return False, [], combined
    entries = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    return True, entries, combined


def _has_zzz_files(entries: list[str]) -> bool:
    s = {e.rstrip("/") for e in entries}
    if "Blocks" in s or "Audio" in s or "res_version_persist" in s:
        return True
    return any(e.lower().endswith((".blk", ".pck")) for e in s)


def detect_remote_files_dir(serial: str, pkg: str, adb: str | None = None) -> tuple[str | None, str]:
    """Probe accessible Android paths for ZZZ `files/` data (`Blocks/`, `Audio/`, `.blk`)."""
    candidates = [
        f"/sdcard/Android/data/{pkg}/files",
        f"/sdcard/Download/{pkg}/files",
        f"/sdcard/Download/ZZZ/files",
        f"/sdcard/Download/ZZZ_Android_Build/{pkg}/files",
    ]
    scoped_blocked = False
    for cand in candidates:
        ok, entries, msg = _ls_dir(serial, cand, adb=adb)
        if ok and _has_zzz_files(entries):
            return cand, f"Found ZZZ asset directory at {cand}"
        if not ok and ("Permission denied" in msg or "Operation not permitted" in msg):
            if "/Android/data/" in cand:
                scoped_blocked = True

    for parent in (f"/sdcard/Download/{pkg}", "/sdcard/Download/ZZZ"):
        ok, entries, _ = _ls_dir(serial, parent, adb=adb)
        if ok and "files" in {e.rstrip("/") for e in entries}:
            sub = f"{parent}/files"
            ok2, entries2, _ = _ls_dir(serial, sub, adb=adb)
            if ok2 and _has_zzz_files(entries2):
                return sub, f"Found ZZZ asset directory at {sub}"

    if scoped_blocked:
        return None, (
            f"Android Scoped Storage blocked direct ADB access to /sdcard/Android/data/{pkg}/files. "
            f"Use Shizuku + a file manager (MT Manager / FV / MixPlorer) to copy `files/` into "
            f"`/sdcard/Download/{pkg}/files/`, then click Refresh or enter that path manually."
        )
    return None, (
        f"Could not auto-locate `{pkg}/files/` on the device. "
        f"Enter the on-device `files/` path manually (e.g. `/sdcard/Download/{pkg}/files`) "
        f"or uncheck asset pulling to pull split APKs only."
    )


def _stream_adb_cmd(
    cmd: list[str],
    log_cb: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> int:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    try:
        buf = []
        while True:
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise RuntimeError("ADB pull cancelled by user.")
            ch = proc.stdout.read(1)
            if ch == "" and proc.poll() is not None:
                break
            if ch in ("\n", "\r"):
                line = "".join(buf).strip()
                buf.clear()
                if line and log_cb:
                    log_cb(line)
            elif ch:
                buf.append(ch)
        tail = "".join(buf).strip()
        if tail and log_cb:
            log_cb(tail)
        return proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()


def _extract_unity_asset_pack_blocks(apk_path: Path, extract_root: Path, log_cb: Callable[[str], None] | None = None) -> int:
    count = 0
    extract_root_resolved = extract_root.resolve()
    with zipfile.ZipFile(apk_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            posix = PurePosixPath(info.filename)
            if ".." in posix.parts or posix.is_absolute():
                continue
            norm = info.filename.lstrip("/")
            if not norm.startswith("assets/Blocks/"):
                continue
            dest = (extract_root / norm).resolve()
            if extract_root_resolved not in dest.parents and dest != extract_root_resolved:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            if norm.lower().endswith(".blk"):
                count += 1
    if log_cb:
        log_cb(f"Extracted {count} base .blk files from {apk_path.name} into {extract_root}")
    return count


def pull_from_phone(
    dest_root: Path,
    serial: str,
    pkg: str,
    remote_files_dir: str | None = None,
    pull_files: bool = True,
    pull_apks: bool = True,
    extract_apk_blocks: bool = True,
    log_cb: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    adb: str | None = None,
) -> Path:
    """Pull ZZZ assets and/or split APKs from an ADB-connected device into `dest_root`."""
    exe = adb or find_adb()
    if not exe:
        raise FileNotFoundError("adb executable not found in PATH.")

    def log(msg: str):
        if log_cb:
            log_cb(msg)

    dest_root = Path(dest_root).expanduser()
    dest_root.mkdir(parents=True, exist_ok=True)

    if pull_files:
        remote = (remote_files_dir or "").strip().rstrip("/")
        if not remote:
            raise ValueError("Remote files directory is empty. Provide a device path or disable 'Pull game assets'.")
        pkg_dir = dest_root / pkg
        files_dir = pkg_dir / "files"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # Match `adb pull` folder semantics without creating nested `files/files/`.
        if remote.endswith("/files") and files_dir.exists():
            pull_target = pkg_dir
        elif not files_dir.exists():
            pull_target = files_dir
        else:
            pull_target = files_dir

        log(f"Pulling ZZZ assets: {remote} -> {files_dir}")
        rc = _stream_adb_cmd([exe, "-s", serial, "pull", remote, str(pull_target)], log_cb=log_cb, cancel_event=cancel_event)
        if rc != 0:
            raise RuntimeError(
                f"adb pull exited with code {rc} for `{remote}`. "
                f"If Android Scoped Storage blocked `/sdcard/Android/data/`, stage via Shizuku into `/sdcard/Download/{pkg}/files/`."
            )

    if pull_apks:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("ADB pull cancelled by user.")
        apk_paths = _get_pkg_apk_paths(serial, pkg, adb=exe)
        if not apk_paths:
            detected_pkg, apk_paths = detect_zzz_package(serial, adb=exe)
            if detected_pkg:
                pkg = detected_pkg
        if not apk_paths:
            log(f"Warning: `pm path {pkg}` returned no APKs; skipping APK pull.")
        else:
            apks_dir = dest_root / "APKs"
            apks_dir.mkdir(parents=True, exist_ok=True)
            for remote_apk in apk_paths:
                local_apk = apks_dir / Path(remote_apk).name
                log(f"Pulling APK: {Path(remote_apk).name} -> {local_apk}")
                rc = _stream_adb_cmd([exe, "-s", serial, "pull", remote_apk, str(local_apk)], log_cb=log_cb, cancel_event=cancel_event)
                if rc != 0:
                    raise RuntimeError(f"adb pull failed for APK `{remote_apk}` (exit {rc}).")

            if extract_apk_blocks:
                asset_pack = apks_dir / "split_UnityDataAssetPack.apk"
                if not asset_pack.is_file():
                    for cand in apks_dir.glob("*UnityDataAssetPack*.apk"):
                        asset_pack = cand
                        break
                if asset_pack.is_file():
                    extract_dir = apks_dir / "extracted" / asset_pack.stem
                    _extract_unity_asset_pack_blocks(asset_pack, extract_dir, log_cb=log_cb)
                else:
                    log("Note: split_UnityDataAssetPack.apk not present among pulled APKs.")

    log(f"Phone import complete: {dest_root}")
    return dest_root
