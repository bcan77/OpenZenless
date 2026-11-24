"""Official HoYoverse PC CDN (HoYoPlay + Sophon) manifest parser & resumable downloader."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

from . import config as cfg


REGIONS: dict[str, dict[str, str]] = {
    "global": {
        "label": "Global (HoYoverse)",
        "biz": "nap_global",
        "game_id": "U5hbdsT9W7",
        "launcher_id": "VYTpXlbWo8",
        "hyp_api": "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api",
        "sophon_api": "https://sg-downloader-api.hoyoverse.com/downloader/sophon_chunk/api",
    },
    "cn": {
        "label": "China Mainland (miHoYo)",
        "biz": "nap_cn",
        "game_id": "x6znKlJ0xK",
        "launcher_id": "jGHBHlcOq1",
        "hyp_api": "https://hyp-api.mihoyo.com/hyp/hyp-connect/api",
        "sophon_api": "https://api-takumi.mihoyo.com/downloader/sophon_chunk/api",
    },
}

VOICE_LANGS: list[tuple[str, str]] = [
    ("en-us", "English"),
    ("ja-jp", "Japanese"),
    ("zh-cn", "Chinese (Simplified)"),
    ("ko-kr", "Korean"),
]

DOWNLOAD_PROFILES: dict[str, str] = {
    "assets": "Assets + Base Audio (Blocks/, Audio/, Revisions — Recommended)",
    "blocks": "Blocks + Metadata Only (Blocks/ & Revisions — Smallest)",
    "full": "Full PC Build (Executable, Data, Video & Assets)",
}

_STATE_FILENAME = ".openzenless_cdn_state.json"


@dataclass(frozen=True)
class SophonChunk:
    name: str
    decompressed_md5: str
    offset: int
    compressed_size: int
    decompressed_size: int
    compressed_md5: str = ""


@dataclass(frozen=True)
class SophonAsset:
    name: str
    size: int
    md5: str
    asset_type: int
    chunks: tuple[SophonChunk, ...]


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


def _emit_log(log_cb: Callable[[str], None] | None, msg: str) -> None:
    if log_cb is not None:
        log_cb(f"[{time.strftime('%H:%M:%S')}] {msg}")


def _http_get_bytes(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "OpenZenless/0.3"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_get_json(url: str, timeout: int = 25) -> dict:
    raw = _http_get_bytes(url, timeout=timeout)
    return json.loads(raw.decode("utf-8", errors="replace"))


def _zstd_decompress(data: bytes) -> bytes:
    """Decompress zstd bytes via Python 3.14 stdlib, `zstandard` package, or `zstd` CLI."""
    try:
        import compression.zstd as _cz  # type: ignore
        return _cz.decompress(data)
    except ImportError:
        pass
    try:
        import zstandard as _zs  # type: ignore
        return _zs.ZstdDecompressor().decompress(data)
    except ImportError:
        pass
    zstd_bin = shutil.which("zstd")
    if zstd_bin:
        proc = subprocess.run(
            [zstd_bin, "-dc"],
            input=data,
            capture_output=True,
            timeout=60,
            **cfg.subprocess_no_window_kwargs(),
        )
        if proc.returncode == 0:
            return proc.stdout
        raise RuntimeError(f"zstd CLI failed (rc={proc.returncode}): {proc.stderr.decode('utf-8', 'replace')}")
    raise RuntimeError(
        "No zstd decompressor available. Upgrade Python to 3.14+, install `zstandard` (`pip install zstandard`), "
        "or install the `zstd` CLI tool."
    )


def _read_varint(buf: bytes | memoryview, pos: int) -> tuple[int, int]:
    shift = 0
    val = 0
    while True:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7


def _iter_proto_fields(buf: bytes | memoryview):
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = _read_varint(buf, pos)
        fn = tag >> 3
        wt = tag & 7
        if wt == 0:
            val, pos = _read_varint(buf, pos)
            yield fn, wt, val
        elif wt == 2:
            ln, pos = _read_varint(buf, pos)
            end = pos + ln
            yield fn, wt, buf[pos:end]
            pos = end
        elif wt == 1:
            yield fn, wt, buf[pos : pos + 8]
            pos += 8
        elif wt == 5:
            yield fn, wt, buf[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wt} at offset {pos}")


def _decode_sophon_chunk(raw: memoryview) -> SophonChunk:
    name = ""
    dec_md5 = ""
    offset = 0
    comp_size = 0
    dec_size = 0
    comp_md5 = ""
    for fn, wt, val in _iter_proto_fields(raw):
        if fn == 1 and wt == 2:
            name = bytes(val).decode("utf-8", errors="replace")
        elif fn == 2 and wt == 2:
            dec_md5 = bytes(val).decode("utf-8", errors="replace")
        elif fn == 3 and wt == 0:
            offset = int(val)
        elif fn == 4 and wt == 0:
            comp_size = int(val)
        elif fn == 5 and wt == 0:
            dec_size = int(val)
        elif fn == 7 and wt == 2:
            comp_md5 = bytes(val).decode("utf-8", errors="replace")
    return SophonChunk(
        name=name,
        decompressed_md5=dec_md5,
        offset=offset,
        compressed_size=comp_size,
        decompressed_size=dec_size,
        compressed_md5=comp_md5,
    )


def _decode_sophon_manifest(raw: bytes) -> list[SophonAsset]:
    mv = memoryview(raw)
    assets: list[SophonAsset] = []
    for fn, wt, val in _iter_proto_fields(mv):
        if fn != 1 or wt != 2:
            continue
        name = ""
        size = 0
        md5 = ""
        asset_type = 0
        chunks: list[SophonChunk] = []
        for afn, awt, aval in _iter_proto_fields(val):
            if afn == 1 and awt == 2:
                name = bytes(aval).decode("utf-8", errors="replace")
            elif afn == 2 and awt == 2:
                chunks.append(_decode_sophon_chunk(aval))
            elif afn == 3 and awt == 0:
                asset_type = int(aval)
            elif afn == 4 and awt == 0:
                size = int(aval)
            elif afn == 5 and awt == 2:
                md5 = bytes(aval).decode("utf-8", errors="replace")
        if not name or asset_type == 64:
            continue
        assets.append(SophonAsset(
            name=name,
            size=size,
            md5=md5,
            asset_type=asset_type,
            chunks=tuple(chunks),
        ))
    return assets


def _matches_profile(asset_name: str, profile: str) -> bool:
    if profile == "full":
        return True
    norm = asset_name.replace("\\", "/").lstrip("/")
    sa_prefix = "ZenlessZoneZero_Data/StreamingAssets/"
    if norm.startswith(f"{sa_prefix}Blocks/"):
        return True
    if profile == "assets" and norm.startswith(f"{sa_prefix}Audio/"):
        return True
    if norm.startswith(sa_prefix):
        rel_sa = norm[len(sa_prefix):]
        # Top-level revision / version / config metadata files inside StreamingAssets/
        if "/" not in rel_sa:
            return True
    return False


def _sanitize_rel_path(dest_root: Path, asset_name: str) -> Path | None:
    norm = asset_name.replace("\\", "/").lstrip("/")
    posix = PurePosixPath(norm)
    parts = [p for p in posix.parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    root_res = dest_root.resolve()
    target = (root_res.joinpath(*parts)).resolve()
    if root_res not in target.parents:
        return None
    return target


def _load_manifest_assets(manifest_meta: dict) -> list[SophonAsset]:
    """Download (with local cache) and decode a Sophon manifest entry into `SophonAsset` rows."""
    man = manifest_meta.get("manifest") or {}
    man_dl = manifest_meta.get("manifest_download") or {}
    man_id = str(man.get("id") or "").strip()
    prefix = str(man_dl.get("url_prefix") or "").rstrip("/")
    suffix = str(man_dl.get("url_suffix") or "")
    compressed = int(man_dl.get("compression", 1)) == 1
    if not man_id or not prefix:
        raise ValueError("Invalid Sophon manifest metadata entry (missing id or url_prefix).")

    cache_dir = cfg.CACHE_DIR / "cdn_manifests"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{man_id}.pb"
    if cache_file.is_file():
        raw = cache_file.read_bytes()
    else:
        url = f"{prefix}/{man_id}{suffix}"
        raw_dl = _http_get_bytes(url, timeout=60)
        if compressed or raw_dl[:4] == b"\x28\xb5\x2f\xfd":
            raw = _zstd_decompress(raw_dl)
        else:
            raw = raw_dl
        tmp_c = cache_file.with_suffix(".pb.tmp")
        tmp_c.write_bytes(raw)
        tmp_c.replace(cache_file)
    return _decode_sophon_manifest(raw)


def fetch_legacy_packages(region: str = "global") -> dict:
    """Query HoYoPlay `getGamePackages` (split ZIP archives / audio packs metadata)."""
    reg = REGIONS.get(region)
    if not reg:
        raise ValueError(f"Unknown region {region!r}; expected one of {tuple(REGIONS)}")
    url = f"{reg['hyp_api']}/getGamePackages?launcher_id={reg['launcher_id']}&game_ids[]={reg['game_id']}"
    data = _http_get_json(url, timeout=20)
    if data.get("retcode") != 0:
        raise RuntimeError(f"getGamePackages error: {data}")
    pkgs = (data.get("data") or {}).get("game_packages") or []
    return pkgs[0] if pkgs else {}


def fetch_zzz_cdn_info(region: str = "global", parse_game_manifest: bool = True) -> dict:
    """Query live HoYoPlay `getGameBranches` + Sophon `getBuild` for Zenless Zone Zero PC."""
    reg = REGIONS.get(region)
    if not reg:
        raise ValueError(f"Unknown region {region!r}; expected one of {tuple(REGIONS)}")

    br_url = f"{reg['hyp_api']}/getGameBranches?launcher_id={reg['launcher_id']}&game_ids[]={reg['game_id']}"
    br_res = _http_get_json(br_url, timeout=20)
    if br_res.get("retcode") != 0:
        raise RuntimeError(f"HoYoPlay getGameBranches failed: {br_res.get('message', br_res)}")

    branches = (br_res.get("data") or {}).get("game_branches") or []
    if not branches or not branches[0].get("main"):
        raise RuntimeError(f"No main ZZZ branch found for region {region!r}.")
    main_br = branches[0]["main"]

    gb_url = (
        f"{reg['sophon_api']}/getBuild"
        f"?branch={main_br['branch']}&package_id={main_br['package_id']}&password={main_br['password']}"
    )
    gb_res = _http_get_json(gb_url, timeout=25)
    if gb_res.get("retcode") != 0:
        raise RuntimeError(f"Sophon getBuild failed: {gb_res.get('message', gb_res)}")

    gb_data = gb_res.get("data") or {}
    manifests = gb_data.get("manifests") or []
    by_field = {str(m.get("matching_field") or ""): m for m in manifests}

    voices: dict[str, dict] = {}
    for code, label in VOICE_LANGS:
        vm = by_field.get(code)
        if vm:
            st = vm.get("stats") or {}
            voices[code] = {
                "code": code,
                "label": label,
                "size": int(st.get("uncompressed_size") or 0),
                "file_count": int(st.get("file_count") or 0),
                "manifest": vm,
            }

    game_man = by_field.get("game")
    if not game_man:
        raise RuntimeError("Sophon getBuild did not include the base `game` manifest.")
    game_stats = game_man.get("stats") or {}
    profiles_summary: dict[str, dict[str, int]] = {
        "full": {
            "size": int(game_stats.get("uncompressed_size") or 0),
            "file_count": int(game_stats.get("file_count") or 0),
        }
    }
    if parse_game_manifest:
        game_assets = _load_manifest_assets(game_man)
        for prof in ("assets", "blocks"):
            matched = [a for a in game_assets if _matches_profile(a.name, prof)]
            profiles_summary[prof] = {
                "size": sum(a.size for a in matched),
                "file_count": len(matched),
            }
        profiles_summary["full"] = {
            "size": sum(a.size for a in game_assets),
            "file_count": len(game_assets),
        }
    else:
        profiles_summary["assets"] = dict(profiles_summary["full"])
        profiles_summary["blocks"] = dict(profiles_summary["full"])

    return {
        "region": region,
        "region_label": reg["label"],
        "version": str(gb_data.get("tag") or main_br.get("tag") or ""),
        "build_id": str(gb_data.get("build_id") or ""),
        "package_id": str(main_br.get("package_id") or ""),
        "profiles": profiles_summary,
        "voices": voices,
        "manifests_by_field": by_field,
    }


def _file_md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            buf = fh.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _download_chunk_verified(
    url: str,
    chunk: SophonChunk,
    compressed: bool,
    cancel_event: threading.Event | None,
) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(4):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("CDN download cancelled by user.")
        try:
            comp_bytes = _http_get_bytes(url, timeout=45)
            if chunk.compressed_md5:
                got_c = hashlib.md5(comp_bytes).hexdigest()
                if got_c.lower() != chunk.compressed_md5.lower():
                    raise ValueError(f"Compressed MD5 mismatch for chunk {chunk.name}")
            dec_bytes = _zstd_decompress(comp_bytes) if compressed else comp_bytes
            if chunk.decompressed_md5:
                got_d = hashlib.md5(dec_bytes).hexdigest()
                if got_d.lower() != chunk.decompressed_md5.lower():
                    raise ValueError(f"Decompressed MD5 mismatch for chunk {chunk.name}")
            if chunk.decompressed_size > 0 and len(dec_bytes) != chunk.decompressed_size:
                raise ValueError(
                    f"Decompressed chunk size mismatch for {chunk.name}: {len(dec_bytes)} != {chunk.decompressed_size}"
                )
            return dec_bytes
        except Exception as exc:
            last_exc = exc
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("CDN download cancelled by user.")
            if attempt < 3:
                time.sleep(min(4.0, 0.5 * (2 ** attempt)))
    raise RuntimeError(f"Failed downloading chunk {chunk.name}: {last_exc}")


def download_pc_build(
    dest_root: Path,
    region: str = "global",
    profile: str = "assets",
    voice_langs: Sequence[str] = (),
    workers: int = 4,
    log_cb: Callable[[str], None] | None = None,
    progress_cb: Callable[[int, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    filter_fn: Callable[[str], bool] | None = None,
    ignore_space_check: bool = False,
) -> Path:
    """Download official Zenless Zone Zero PC files via HoYoverse Sophon CDN into `dest_root`."""
    if profile not in DOWNLOAD_PROFILES:
        raise ValueError(f"Unknown profile {profile!r}; expected one of {tuple(DOWNLOAD_PROFILES)}")

    dest_root = Path(dest_root).expanduser()
    _emit_log(log_cb, f"Querying HoYoverse CDN ({region}) for ZZZ build metadata...")
    if progress_cb:
        progress_cb(-1, f"Querying HoYoverse CDN ({region})...")

    info = fetch_zzz_cdn_info(region=region, parse_game_manifest=False)
    by_field = info["manifests_by_field"]
    version = info["version"]
    _emit_log(log_cb, f"Official ZZZ PC build: v{version} (build_id={info['build_id']}, profile={profile})")

    selected_jobs: list[tuple[dict, SophonAsset]] = []
    game_man = by_field["game"]
    _emit_log(log_cb, "Downloading & decoding main game manifest...")
    game_assets = _load_manifest_assets(game_man)
    for asset in game_assets:
        if not _matches_profile(asset.name, profile):
            continue
        if filter_fn is not None and not filter_fn(asset.name):
            continue
        selected_jobs.append((game_man, asset))

    for lang in voice_langs:
        v_man = by_field.get(lang)
        if not v_man:
            _emit_log(log_cb, f"Warning: voice pack '{lang}' not found in CDN manifests; skipping.")
            continue
        _emit_log(log_cb, f"Downloading & decoding voice manifest ({lang})...")
        for asset in _load_manifest_assets(v_man):
            if filter_fn is not None and not filter_fn(asset.name):
                continue
            selected_jobs.append((v_man, asset))

    if not selected_jobs:
        raise RuntimeError("No CDN assets matched the selected profile/filter.")

    state_path = dest_root / _STATE_FILENAME
    state_files: dict[str, dict] = {}
    if state_path.is_file():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("files"), dict):
                state_files = loaded["files"]
        except Exception:
            state_files = {}

    pending: list[tuple[dict, SophonAsset, Path, str]] = []
    total_files = len(selected_jobs)
    total_bytes = sum(a.size for _, a in selected_jobs)
    already_bytes = 0
    done_files = 0

    for man_meta, asset in selected_jobs:
        dst = _sanitize_rel_path(dest_root, asset.name)
        if dst is None:
            continue
        rel = asset.name.replace("\\", "/").lstrip("/")
        rec = state_files.get(rel)
        if dst.is_file() and dst.stat().st_size == asset.size:
            if rec and rec.get("size") == asset.size and (not asset.md5 or rec.get("md5", "").lower() == asset.md5.lower()):
                already_bytes += asset.size
                done_files += 1
                continue
            if not asset.md5 or _file_md5(dst).lower() == asset.md5.lower():
                state_files[rel] = {"size": asset.size, "md5": asset.md5}
                already_bytes += asset.size
                done_files += 1
                continue
        pending.append((man_meta, asset, dst, rel))

    remaining_bytes = max(0, total_bytes - already_bytes)
    _emit_log(
        log_cb,
        f"Plan: {total_files} files ({_fmt_bytes(total_bytes)}) — "
        f"{done_files} already verified ({_fmt_bytes(already_bytes)}), "
        f"{len(pending)} remaining ({_fmt_bytes(remaining_bytes)}).",
    )

    dest_root.mkdir(parents=True, exist_ok=True)
    probe = dest_root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free_bytes = shutil.disk_usage(probe).free
    if not ignore_space_check and free_bytes < int(remaining_bytes * 1.03):
        raise OSError(
            f"Insufficient disk space at {dest_root}: need at least {_fmt_bytes(int(remaining_bytes * 1.03))}, "
            f"but only {_fmt_bytes(free_bytes)} free."
        )

    if not pending:
        state_tmp = state_path.with_name(state_path.name + ".tmp")
        state_tmp.write_text(json.dumps({"version": version, "files": state_files}, indent=2), encoding="utf-8")
        state_tmp.replace(state_path)
        if progress_cb:
            progress_cb(100, f"v{version} • All {total_files} files verified ({_fmt_bytes(total_bytes)})")
        return dest_root

    lock = threading.Lock()
    start_t = time.monotonic()
    last_emit_t = 0.0
    last_save_n = 0
    done_bytes = already_bytes
    session_bytes = 0

    def _save_state():
        tmp = state_path.with_name(state_path.name + ".tmp")
        tmp.write_text(json.dumps({"version": version, "files": state_files}), encoding="utf-8")
        tmp.replace(state_path)

    def _report_progress(cur_name: str = "", force: bool = False):
        nonlocal last_emit_t
        now = time.monotonic()
        if not force and now - last_emit_t < 0.25:
            return
        last_emit_t = now
        elapsed = max(0.2, now - start_t)
        rate = session_bytes / elapsed
        rem = max(0, total_bytes - done_bytes)
        eta = _fmt_duration(rem / rate) if rate > 1024 else "--:--"
        pct = int(done_bytes * 100 / total_bytes) if total_bytes > 0 else int(done_files * 100 / max(1, total_files))
        pct = max(0, min(100, pct))
        short_name = PurePosixPath(cur_name).name if cur_name else ""
        status = (
            f"v{version} • {pct}% • {_fmt_bytes(done_bytes)} / {_fmt_bytes(total_bytes)} "
            f"({_fmt_bytes(int(rate))}/s, ETA {eta}) • {done_files}/{total_files} files"
        )
        if short_name:
            status += f" • {short_name}"
        if progress_cb:
            progress_cb(pct, status)

    def _download_one(job: tuple[dict, SophonAsset, Path, str]):
        nonlocal done_bytes, session_bytes, done_files, last_save_n
        man_meta, asset, dst, rel = job
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("CDN download cancelled by user.")

        dst.parent.mkdir(parents=True, exist_ok=True)
        chunk_dl = man_meta.get("chunk_download") or {}
        url_prefix = str(chunk_dl.get("url_prefix") or "").rstrip("/")
        url_suffix = str(chunk_dl.get("url_suffix") or "")
        compressed = int(chunk_dl.get("compression", 1)) == 1

        part = dst.with_name(dst.name + ".part")
        if asset.size == 0 and not asset.chunks:
            dst.write_bytes(b"")
        else:
            mode = "r+b" if part.is_file() else "w+b"
            with part.open(mode) as fh:
                fh.seek(0, os.SEEK_END)
                existing_part_size = fh.tell()
                for chunk in asset.chunks:
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("CDN download cancelled by user.")
                    c_end = chunk.offset + chunk.decompressed_size
                    reused = False
                    if chunk.decompressed_size > 0 and existing_part_size >= c_end and chunk.decompressed_md5:
                        fh.seek(chunk.offset)
                        prev_bytes = fh.read(chunk.decompressed_size)
                        if (
                            len(prev_bytes) == chunk.decompressed_size
                            and hashlib.md5(prev_bytes).hexdigest().lower() == chunk.decompressed_md5.lower()
                        ):
                            reused = True
                    if not reused:
                        curl = f"{url_prefix}/{chunk.name}{url_suffix}"
                        c_data = _download_chunk_verified(curl, chunk, compressed=compressed, cancel_event=cancel_event)
                        fh.seek(chunk.offset)
                        fh.write(c_data)
                        inc = len(c_data)
                    else:
                        inc = chunk.decompressed_size
                    with lock:
                        done_bytes += inc
                        if not reused:
                            session_bytes += inc
                    _report_progress(rel)
                fh.flush()
                if os.fstat(fh.fileno()).st_size != asset.size:
                    fh.truncate(asset.size)

            if asset.md5:
                got_file_md5 = _file_md5(part)
                if got_file_md5.lower() != asset.md5.lower():
                    part.unlink(missing_ok=True)
                    raise ValueError(f"Final MD5 mismatch for {rel}: {got_file_md5} != {asset.md5}")
            part.replace(dst)

        with lock:
            state_files[rel] = {"size": asset.size, "md5": asset.md5}
            done_files += 1
            if done_files - last_save_n >= 25:
                last_save_n = done_files
                _save_state()
        _report_progress(rel)

    _report_progress(force=True)
    max_w = max(1, min(8, int(workers)))
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as pool:
            futs = [pool.submit(_download_one, j) for j in pending]
            for fut in concurrent.futures.as_completed(futs):
                fut.result()
    finally:
        with lock:
            if state_files:
                _save_state()

    elapsed_all = _fmt_duration(time.monotonic() - start_t)
    _report_progress(force=True)
    _emit_log(log_cb, f"HoYoverse PC CDN download complete in {elapsed_all}: {dest_root}")
    if progress_cb:
        progress_cb(100, f"v{version} • Complete ({total_files} files, {_fmt_bytes(total_bytes)} in {elapsed_all})")
    return dest_root
