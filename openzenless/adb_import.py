"""ADB device detection and Android ZZZ build importer for OpenZenless."""
from __future__ import annotations

import codecs
import errno
import os
import re
import select
import shlex
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

try:
    import pty
except ImportError:
    pty = None

from . import config as cfg

ZZZ_PACKAGES = ("com.HoYoverse.Nap", "com.miHoYo.Nap")

_ADB_PCT_RE = re.compile(r"^\[\s*(\d+)%\]\s*(.+)$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _fmt_bytes(num: int) -> str:
    val = float(max(0, num))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0 or unit == "TB":
            return f"{int(val)} {unit}" if unit == "B" else f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{num} B"


def _fmt_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hrs, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def _ts(msg: str) -> str:
    return f"[{time.strftime('%H:%M:%S')}] {msg}"


def find_adb() -> str | None:
    """Return path to the `adb` executable across portable tools, SDK dirs, and PATH."""
    return cfg.find_adb()


def ensure_adb_available(
    log_cb: Callable[[str], None] | None = None,
    progress_cb: Callable[[int, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> str | None:
    """Return existing `adb` or auto-install Google Android Platform-Tools into TOOLS_DIR."""
    from .tools import ensure_adb
    return ensure_adb(log_cb=log_cb, progress_cb=progress_cb, cancel_event=cancel_event)


def _run_adb(args: list[str], serial: str | None = None, adb: str | None = None, timeout: int = 15) -> subprocess.CompletedProcess:
    exe = adb or find_adb()
    if not exe:
        raise FileNotFoundError("adb executable not found (click Auto-Install ADB or run `openzenless setup-tools --adb`).")
    cmd = [exe]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        **cfg.subprocess_no_window_kwargs(),
    )


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
        if len(parts) >= 3 and parts[1] == "no" and parts[2].startswith("permissions"):
            state = "no_permissions"
            tok_start = 3
        else:
            state = parts[1]
            tok_start = 2
        meta: dict[str, str] = {}
        for tok in parts[tok_start:]:
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


def adb_connect_wireless(host_port: str, adb: str | None = None) -> tuple[bool, str]:
    """Connect to a device over Wireless Debugging (`ip:port`)."""
    target = host_port.strip()
    if not target:
        return False, "Enter a host:port (for example 192.168.1.50:5555)."
    if ":" not in target:
        target = f"{target}:5555"
    res = _run_adb(["connect", target], adb=adb, timeout=15)
    msg = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
    low = msg.lower()
    ok = res.returncode == 0 and ("connected to" in low or "already connected" in low)
    return ok, msg or f"adb connect exited with code {res.returncode}"


def adb_pair_wireless(host_port: str, pairing_code: str, adb: str | None = None) -> tuple[bool, str]:
    """Pair with an Android 11+ Wireless Debugging endpoint (`ip:port` + 6-digit code)."""
    target = host_port.strip()
    code = pairing_code.strip()
    if not target or not code:
        return False, "Both pairing host:port and 6-digit pairing code are required."
    res = _run_adb(["pair", target, code], adb=adb, timeout=20)
    msg = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
    ok = res.returncode == 0 and "successfully paired" in msg.lower()
    return ok, msg or f"adb pair exited with code {res.returncode}"


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
        "/sdcard/Download/ZZZ/files",
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


def _probe_remote_dir_bytes(serial: str, remote_dir: str, adb: str | None = None) -> int | None:
    """Best-effort remote directory byte estimate via `du -sk` (returns None on failure)."""
    try:
        q = shlex.quote(remote_dir)
        res = _run_adb(["shell", f"du -sk {q} 2>/dev/null"], serial=serial, adb=adb, timeout=20)
        for line in (res.stdout or "").splitlines():
            parts = line.strip().split()
            if parts and parts[0].isdigit():
                return int(parts[0]) * 1024
    except Exception:
        pass
    return None


def _probe_remote_file_bytes(serial: str, remote_file: str, adb: str | None = None) -> int | None:
    """Best-effort remote file byte size via `stat -c %s` or `ls -l` (returns None on failure)."""
    try:
        q = shlex.quote(remote_file)
        res = _run_adb(["shell", f"stat -c %s {q} 2>/dev/null || ls -l {q} 2>/dev/null"], serial=serial, adb=adb, timeout=10)
        for raw in (res.stdout or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.isdigit():
                return int(line)
            parts = line.split()
            if len(parts) >= 5 and parts[4].isdigit():
                return int(parts[4])
    except Exception:
        pass
    return None


def _measure_local_dir(path: Path) -> tuple[int, int]:
    """Return `(file_count, total_bytes)` under `path`, ignoring transient OSErrors."""
    files = 0
    total = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            files += 1
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return files, total


def _stream_adb_cmd(
    cmd: list[str],
    log_cb: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    progress_cb: Callable[[int, str], None] | None = None,
    phase_label: str = "",
    total_bytes: int | None = None,
    watch_path: Path | None = None,
    watch_is_dir: bool = False,
) -> int:
    start_t = time.monotonic()
    last_log_t = start_t
    last_mile_t = 0.0
    last_mile_pct = -1
    last_prog_t = 0.0
    last_prog_pct = -1
    last_watch_t = 0.0
    last_dir_scan_t = 0.0
    last_adb_pct = -1
    last_file = ""
    local_bytes = 0
    local_files = 0

    def _emit_log(msg: str):
        nonlocal last_log_t
        last_log_t = time.monotonic()
        if log_cb:
            log_cb(_ts(msg))

    def _sample_local(now: float):
        nonlocal local_bytes, local_files, last_dir_scan_t
        if watch_path is None:
            return
        try:
            if watch_is_dir:
                if watch_path.is_dir() and (now - last_dir_scan_t >= 3.0):
                    local_files, local_bytes = _measure_local_dir(watch_path)
                    last_dir_scan_t = now
            else:
                if watch_path.is_file():
                    local_files = 1
                    local_bytes = watch_path.stat().st_size
        except OSError:
            pass

    def _build_status(pct: int, now: float) -> str:
        parts = [phase_label] if phase_label else ["ADB transfer"]
        if pct >= 0:
            parts.append(f"{pct}%")
        if local_bytes > 0:
            rate = local_bytes / max(0.2, now - start_t)
            if total_bytes and total_bytes > 0:
                parts.append(f"{_fmt_bytes(local_bytes)} / {_fmt_bytes(total_bytes)} ({_fmt_bytes(int(rate))}/s)")
            else:
                parts.append(f"{_fmt_bytes(local_bytes)} ({_fmt_bytes(int(rate))}/s)")
        elif total_bytes and total_bytes > 0:
            parts.append(f"0 B / {_fmt_bytes(total_bytes)}")
        if watch_is_dir and local_files > 0:
            parts.append(f"{local_files} files")
        if last_file:
            parts.append(last_file)
        return " • ".join(parts)

    def _effective_pct() -> int:
        if last_adb_pct >= 0:
            return last_adb_pct
        if total_bytes and total_bytes > 0 and local_bytes > 0:
            return min(99, int(local_bytes * 100 / total_bytes))
        return -1

    def _handle_line(raw_line: str):
        nonlocal last_adb_pct, last_file, last_mile_t, last_mile_pct, last_prog_t, last_prog_pct
        clean = _ANSI_RE.sub("", raw_line).strip()
        if not clean:
            return
        now = time.monotonic()
        m = _ADB_PCT_RE.match(clean)
        if m:
            pct = max(0, min(100, int(m.group(1))))
            file_arg = m.group(2).strip()
            last_file = PurePosixPath(file_arg.replace("\\", "/")).name or file_arg
            last_adb_pct = pct
            _sample_local(now)
            if progress_cb and (now - last_prog_t >= 0.2 or pct >= 100 or pct != last_prog_pct):
                last_prog_t = now
                last_prog_pct = pct
                progress_cb(pct, _build_status(pct, now))
            if (
                last_mile_pct < 0
                or pct >= 100
                or (pct - last_mile_pct >= 5)
                or (now - last_mile_t >= 2.5 and pct != last_mile_pct)
            ):
                last_mile_t = now
                last_mile_pct = pct
                prefix = f"{phase_label}: " if phase_label else ""
                _emit_log(f"{prefix}{pct}% — {last_file}")
        else:
            _emit_log(clean)

    def _maybe_heartbeat(now: float):
        nonlocal last_watch_t
        if now - last_watch_t < 2.5:
            return
        last_watch_t = now
        _sample_local(now)
        pct = _effective_pct()
        if progress_cb:
            progress_cb(pct, _build_status(pct, now))
        if now - last_log_t >= 5.0:
            elapsed = _fmt_duration(now - start_t)
            details = [f"Still running ({elapsed} elapsed)"]
            if local_bytes > 0:
                rate = local_bytes / max(0.2, now - start_t)
                if total_bytes and total_bytes > 0:
                    details.append(f"{_fmt_bytes(local_bytes)} / {_fmt_bytes(total_bytes)} ({_fmt_bytes(int(rate))}/s)")
                else:
                    details.append(f"{_fmt_bytes(local_bytes)} ({_fmt_bytes(int(rate))}/s)")
            if watch_is_dir and local_files > 0:
                details.append(f"{local_files} files")
            if last_file:
                details.append(last_file)
            _emit_log(" • ".join(details))

    def _terminate_proc(proc: subprocess.Popen):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

    if pty is not None:
        master_fd, slave_fd = pty.openpty()
        proc: subprocess.Popen | None = None
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf = ""
        eof = False
        try:
            os.set_blocking(master_fd, False)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)
            slave_fd = -1

            while True:
                if cancel_event and cancel_event.is_set():
                    _emit_log("Cancelling ADB transfer...")
                    _terminate_proc(proc)
                    raise RuntimeError("ADB pull cancelled by user.")

                try:
                    rlist, _, _ = select.select([master_fd], [], [], 0.25)
                except InterruptedError:
                    continue

                if master_fd in rlist:
                    while True:
                        try:
                            chunk = os.read(master_fd, 4096)
                        except BlockingIOError:
                            break
                        except OSError as exc:
                            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                                break
                            if exc.errno == errno.EIO:
                                eof = True
                                break
                            raise
                        if not chunk:
                            eof = True
                            break
                        buf += dec.decode(chunk, final=False)
                        lines = re.split(r"[\r\n]+", buf)
                        buf = lines.pop()
                        for line in lines:
                            _handle_line(line)

                now = time.monotonic()
                _maybe_heartbeat(now)

                if eof:
                    break
                if proc.poll() is not None and not rlist:
                    break

            buf += dec.decode(b"", final=True)
            for line in re.split(r"[\r\n]+", buf):
                _handle_line(line)
            return proc.wait()
        finally:
            if slave_fd >= 0:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            try:
                os.close(master_fd)
            except OSError:
                pass
            if proc is not None and proc.poll() is None:
                _terminate_proc(proc)

    # Fallback pipe mode on platforms without `pty` (e.g. Windows).
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **cfg.subprocess_no_window_kwargs(),
    )
    assert proc.stdout is not None
    try:
        char_buf: list[str] = []
        while True:
            if cancel_event and cancel_event.is_set():
                _emit_log("Cancelling ADB transfer...")
                _terminate_proc(proc)
                raise RuntimeError("ADB pull cancelled by user.")
            ch = proc.stdout.read(1)
            if ch == "" and proc.poll() is not None:
                break
            if ch in ("\n", "\r"):
                _handle_line("".join(char_buf))
                char_buf.clear()
            elif ch:
                char_buf.append(ch)
            _maybe_heartbeat(time.monotonic())
        if char_buf:
            _handle_line("".join(char_buf))
        return proc.wait()
    finally:
        if proc.poll() is None:
            _terminate_proc(proc)


def _extract_unity_asset_pack_blocks(
    apk_path: Path,
    extract_root: Path,
    log_cb: Callable[[str], None] | None = None,
    progress_cb: Callable[[int, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> int:
    start_t = time.monotonic()
    count = 0
    extract_root_resolved = extract_root.resolve()
    with zipfile.ZipFile(apk_path, "r") as zf:
        entries: list[tuple[zipfile.ZipInfo, Path, bool]] = []
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
            entries.append((info, dest, norm.lower().endswith(".blk")))

        total_n = len(entries)
        total_b = sum(info.file_size for info, _, _ in entries)
        done_b = 0
        last_log_t = start_t
        last_log_pct = -1

        for idx, (info, dest, is_blk) in enumerate(entries, start=1):
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("ADB pull cancelled by user.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            if is_blk:
                count += 1
            done_b += info.file_size
            pct = int(done_b * 100 / total_b) if total_b > 0 else int(idx * 100 / max(1, total_n))
            pct = max(0, min(100, pct))
            msg = f"Extracting {apk_path.name}: {idx}/{total_n} ({_fmt_bytes(done_b)} / {_fmt_bytes(total_b)})"
            if progress_cb:
                progress_cb(pct, msg)
            now = time.monotonic()
            if log_cb and (last_log_pct < 0 or pct >= 100 or pct - last_log_pct >= 20 or now - last_log_t >= 2.0):
                last_log_t = now
                last_log_pct = pct
                log_cb(_ts(f"{msg} ({pct}%)"))

    elapsed = _fmt_duration(time.monotonic() - start_t)
    if log_cb:
        log_cb(_ts(f"Extracted {count} base .blk files from {apk_path.name} into {extract_root} ({elapsed})"))
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
    progress_cb: Callable[[int, str], None] | None = None,
) -> Path:
    """Pull ZZZ assets and/or split APKs from an ADB-connected device into `dest_root`."""
    exe = adb or find_adb()
    if not exe:
        raise FileNotFoundError("adb executable not found in PATH.")

    start_all = time.monotonic()

    def log(msg: str):
        if log_cb:
            log_cb(_ts(msg))

    def prog(pct: int, msg: str):
        if progress_cb:
            progress_cb(pct, msg)

    dest_root = Path(dest_root).expanduser()
    dest_root.mkdir(parents=True, exist_ok=True)

    total_steps = int(bool(pull_files)) + int(bool(pull_apks)) + int(bool(pull_apks and extract_apk_blocks))
    total_steps = max(1, total_steps)
    step_num = 0

    log(f"ADB import started • device={serial}, package={pkg}, dest={dest_root}")
    prog(-1, f"Starting ADB import from {serial}...")

    if pull_files:
        step_num += 1
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

        phase = f"Step {step_num}/{total_steps}: Assets"
        log(f"[{phase}] Probing remote asset size for {remote}...")
        prog(-1, f"{phase} • Probing remote size...")
        remote_bytes = _probe_remote_dir_bytes(serial, remote, adb=exe)
        if remote_bytes:
            log(f"[{phase}] Estimated remote size: {_fmt_bytes(remote_bytes)}")
        else:
            log(f"[{phase}] Remote size unavailable; tracking live bytes written to {files_dir.name}/")

        log(f"[{phase}] Pulling ZZZ assets: {remote} -> {files_dir}")
        rc = _stream_adb_cmd(
            [exe, "-s", serial, "pull", remote, str(pull_target)],
            log_cb=log_cb,
            cancel_event=cancel_event,
            progress_cb=progress_cb,
            phase_label=phase,
            total_bytes=remote_bytes,
            watch_path=files_dir,
            watch_is_dir=True,
        )
        if rc != 0:
            raise RuntimeError(
                f"adb pull exited with code {rc} for `{remote}`. "
                f"If Android Scoped Storage blocked `/sdcard/Android/data/`, stage via Shizuku into `/sdcard/Download/{pkg}/files/`."
            )

    if pull_apks:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("ADB pull cancelled by user.")
        step_num += 1
        phase = f"Step {step_num}/{total_steps}: APKs"
        log(f"[{phase}] Querying installed split APKs for {pkg}...")
        prog(-1, f"{phase} • Querying APKs...")
        apk_paths = _get_pkg_apk_paths(serial, pkg, adb=exe)
        if not apk_paths:
            detected_pkg, apk_paths = detect_zzz_package(serial, adb=exe)
            if detected_pkg:
                pkg = detected_pkg
        if not apk_paths:
            log(f"[{phase}] Warning: `pm path {pkg}` returned no APKs; skipping APK pull.")
        else:
            apks_dir = dest_root / "APKs"
            apks_dir.mkdir(parents=True, exist_ok=True)
            log(f"[{phase}] Found {len(apk_paths)} split APK(s).")
            for idx, remote_apk in enumerate(apk_paths, start=1):
                apk_name = Path(remote_apk).name
                local_apk = apks_dir / apk_name
                apk_bytes = _probe_remote_file_bytes(serial, remote_apk, adb=exe)
                size_tag = f" ({_fmt_bytes(apk_bytes)})" if apk_bytes else ""
                apk_phase = f"APK {idx}/{len(apk_paths)}: {apk_name}"
                log(f"[{phase}] Pulling {apk_name}{size_tag} -> {local_apk}")
                rc = _stream_adb_cmd(
                    [exe, "-s", serial, "pull", remote_apk, str(local_apk)],
                    log_cb=log_cb,
                    cancel_event=cancel_event,
                    progress_cb=progress_cb,
                    phase_label=apk_phase,
                    total_bytes=apk_bytes,
                    watch_path=local_apk,
                    watch_is_dir=False,
                )
                if rc != 0:
                    raise RuntimeError(f"adb pull failed for APK `{remote_apk}` (exit {rc}).")

            if extract_apk_blocks:
                step_num += 1
                ex_phase = f"Step {step_num}/{total_steps}: Extract APK"
                asset_pack = apks_dir / "split_UnityDataAssetPack.apk"
                if not asset_pack.is_file():
                    for cand in apks_dir.glob("*UnityDataAssetPack*.apk"):
                        asset_pack = cand
                        break
                if asset_pack.is_file():
                    extract_dir = apks_dir / "extracted" / asset_pack.stem
                    log(f"[{ex_phase}] Extracting base .blk blocks from {asset_pack.name}...")
                    _extract_unity_asset_pack_blocks(
                        asset_pack,
                        extract_dir,
                        log_cb=log_cb,
                        progress_cb=progress_cb,
                        cancel_event=cancel_event,
                    )
                else:
                    log(f"[{ex_phase}] Note: split_UnityDataAssetPack.apk not present among pulled APKs.")

    elapsed = _fmt_duration(time.monotonic() - start_all)
    prog(100, f"Phone import complete ({elapsed})")
    log(f"Phone import complete in {elapsed}: {dest_root}")
    return dest_root
