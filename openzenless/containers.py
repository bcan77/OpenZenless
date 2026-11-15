"""Binary container inspection, AKPK (.pck) parsing, and .blk JSON scanning."""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
from pathlib import Path

from . import config as cfg


def human_size(n: int) -> str:
    if n < 0:
        return "-"
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024 or unit == "GB":
            return f"{int(val)} B" if unit == "B" else f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} GB"


def detect_magic(path: Path) -> tuple[str, bytes]:
    try:
        with path.open("rb") as fh:
            head = fh.read(64)
    except OSError:
        return "Unreadable", b""
    if head.startswith(b"blk\x00") or path.suffix.lower() == ".blk":
        if head[:4] == b"blk\x00":
            return "ZZZ BLK (blk\\0)", head
        return "ZZZ BLK (obf/custom)", head
    if head.startswith(b"UnityFS"):
        return "UnityFS Bundle", head
    if head.startswith(b"AKPK"):
        return "Wwise AKPK Package", head
    if head.startswith(b"BKHD"):
        return "Wwise SoundBank (BKHD)", head
    if head.startswith(b"RIFF"):
        return "RIFF / Wwise WEM", head
    if head.startswith(b"\x7fELF"):
        return "ELF Shared Library", head
    if head.startswith(b"PK\x03\x04"):
        return "ZIP / APK Archive", head
    if head.startswith(b"\xaf\x1b\xb1\xfa"):
        return "Unity global-metadata", head
    if path.name == "global-metadata.dat":
        return "IL2CPP Metadata (obf)", head
    return path.suffix.lstrip(".").upper() or "Binary", head


def format_hex_dump(data: bytes, base_offset: int = 0) -> str:
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off : off + 16]
        hex_left = " ".join(f"{b:02X}" for b in chunk[:8])
        hex_right = " ".join(f"{b:02X}" for b in chunk[8:])
        ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base_offset + off:08X}  {hex_left:<23}  {hex_right:<23}  |{ascii_str}|")
    return "\n".join(lines)


def parse_akpk(path: Path) -> dict:
    """Parse Wwise AKPK header and embedded Bank/WEM/External entry tables."""
    entries = []
    with path.open("rb") as fh:
        hdr = fh.read(28)
        if len(hdr) < 28 or hdr[:4] != b"AKPK":
            return {"error": "Not a valid AKPK header", "entries": []}
        header_size, version, lang_map_size, bnk_tbl_size, wem_tbl_size = struct.unpack("<IIIII", hdr[4:24])
        ext_tbl_size = struct.unpack("<I", hdr[24:28])[0]

        fh.seek(28 + lang_map_size)
        for kind, tbl_size, entry_fmt in (
            ("BNK", bnk_tbl_size, "<IIIII"),
            ("WEM", wem_tbl_size, "<IIIII"),
            ("EXT", ext_tbl_size, "<QIIII"),
        ):
            if tbl_size < 4:
                fh.read(tbl_size)
                continue
            raw = fh.read(tbl_size)
            count = struct.unpack("<I", raw[:4])[0]
            pos = 4
            step = struct.calcsize(entry_fmt)
            for _ in range(count):
                if pos + step > len(raw):
                    break
                fid, align, size, offset_blk, lang_id = struct.unpack_from(entry_fmt, raw, pos)
                pos += step
                entries.append({
                    "type": f"Wwise {kind}",
                    "name": f"{fid}.{kind.lower()}" if kind != "EXT" else f"{fid:016x}.wem",
                    "id": str(fid),
                    "offset": offset_blk * max(1, align),
                    "size": size,
                    "lang_id": lang_id,
                })
    return {
        "header_size": header_size,
        "version": version,
        "lang_map_size": lang_map_size,
        "bnk_table_size": bnk_tbl_size,
        "wem_table_size": wem_tbl_size,
        "ext_table_size": ext_tbl_size,
        "entries": entries,
    }


def scan_blk_json(blk_path: Path, out_dir: Path | None = None, types: list[str] | None = None) -> tuple[Path, list[dict], str]:
    if not cfg.CLI_EXE.exists():
        raise FileNotFoundError(f"AnimeStudio.CLI.exe not found at {cfg.CLI_EXE}")
    if shutil.which("wine") is None:
        raise FileNotFoundError("wine executable not found in PATH")
    target_dir = out_dir or (cfg.CACHE_DIR / "blk_json" / blk_path.stem)
    target_dir.mkdir(parents=True, exist_ok=True)
    existing = list(target_dir.rglob("*.json"))
    log_text = "(cached)"
    if not existing:
        env = os.environ.copy()
        env["WINEDEBUG"] = "-all"
        use_types = types or [
            "Texture2D", "Material", "Shader", "Mesh",
            "GameObject", "TextAsset", "MonoBehaviour", "Sprite", "AudioClip",
        ]
        cmd = [
            "wine", str(cfg.CLI_EXE),
            "Z:" + str(blk_path).replace("/", "\\"),
            "Z:" + str(target_dir).replace("/", "\\"),
            "--game", "ZZZ",
            "--types", *use_types,
            "--group_assets", "ByType",
            "--export_type", "JSON",
        ]
        proc = subprocess.run(cmd, cwd=cfg.CLI_DIR, env=env, capture_output=True, text=True, timeout=300)
        log_text = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        existing = list(target_dir.rglob("*.json"))
        if proc.returncode != 0 and not existing:
            raise RuntimeError(f"CLI exited {proc.returncode}:\n{log_text[-1200:]}")

    rows = []
    for p in sorted(existing):
        rel = p.relative_to(target_dir)
        asset_type = rel.parts[0] if len(rel.parts) > 1 else "Asset"
        rows.append({
            "type": asset_type,
            "name": p.stem,
            "path": str(p),
            "size": p.stat().st_size,
            "blk": str(blk_path),
        })
    return target_dir, rows, log_text
