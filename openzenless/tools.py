"""Automatic tool bootstrapper for AnimeStudio.CLI and AnimeWwise."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import config as cfg


def _emit(log_cb, msg: str):
    if log_cb is not None:
        log_cb(msg)
    else:
        print(msg, flush=True)


def _stream_cmd(cmd: list[str], log_cb=None) -> int:
    _emit(log_cb, f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        _emit(log_cb, line.rstrip("\n"))
    return proc.wait()


def ensure_extractor_tools(log_cb=None, progress_cb=None) -> dict:
    """Install/build AnimeStudio.CLI and AnimeWwise into TOOLS_DIR if missing."""
    cfg.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    cfg._resolve_tool_paths()

    def _prog(pct: int, msg: str):
        if progress_cb is not None:
            progress_cb(pct, msg)
        _emit(log_cb, msg)

    if not cfg.CLI_EXE.is_file():
        git = shutil.which("git")
        dotnet = shutil.which("dotnet")
        if not git or not dotnet:
            _emit(log_cb, f"[WARN] Cannot auto-build AnimeStudio.CLI (git={bool(git)}, dotnet={bool(dotnet)}).")
        else:
            as_dir = cfg.TOOLS_DIR / "AnimeStudio"
            if not as_dir.exists():
                _prog(10, "Cloning Escartem/AnimeStudio...")
                _stream_cmd(
                    [git, "clone", "--depth", "1", "https://github.com/Escartem/AnimeStudio.git", str(as_dir)],
                    log_cb=log_cb,
                )
            if as_dir.exists():
                # Patch net10.0 target frameworks if local SDK is .NET 9
                for csproj in as_dir.rglob("*.csproj"):
                    txt = csproj.read_text(encoding="utf-8", errors="ignore")
                    if "net10.0" in txt:
                        csproj.write_text(
                            txt.replace(";net10.0-windows", "").replace(";net10.0", ""),
                            encoding="utf-8",
                        )
                _prog(35, "Building AnimeStudio.CLI (net9.0-windows)...")
                _stream_cmd(
                    [
                        dotnet, "build",
                        str(as_dir / "AnimeStudio.CLI" / "AnimeStudio.CLI.csproj"),
                        "-c", "Release", "-f", "net9.0-windows",
                    ],
                    log_cb=log_cb,
                )
                mingw = shutil.which("x86_64-w64-mingw32-g++")
                zzzv2_src = as_dir / "AnimeStudio.ACL" / "AnimeStudio.ACL.ZZZV2"
                if mingw and (zzzv2_src / "ACLLibrary.cpp").is_file():
                    _prog(55, "Building AnimeStudio.ACL.ZZZV2.dll (mingw-w64)...")
                    dll_out = as_dir / "AnimeStudio.CLI" / "bin" / "Release" / "net9.0-windows" / "AnimeStudio.ACL.ZZZV2.dll"
                    rc_dll = _stream_cmd(
                        [
                            mingw, "-std=c++20", "-O2", "-shared", f"-I{zzzv2_src}",
                            "-DNDEBUG", "-D_AS_DLL", "-DACLZZZV2_EXPORTS", "-D_WINDOWS", "-D_USRDLL", "-w",
                            str(zzzv2_src / "dllmain.cpp"), str(zzzv2_src / "ACLLibrary.cpp"),
                            "-o", str(dll_out), "-static", "-static-libgcc", "-static-libstdc++",
                        ],
                        log_cb=log_cb,
                    )
                    if rc_dll == 0 and dll_out.is_file():
                        for extra_dir in (
                            as_dir / "AnimeStudio.Libraries",
                            as_dir / "AnimeStudio.Utility" / "bin" / "Release" / "net9.0",
                        ):
                            if extra_dir.is_dir():
                                shutil.copy2(dll_out, extra_dir / "AnimeStudio.ACL.ZZZV2.dll")
                cfg._resolve_tool_paths()

    if not cfg.NAP_MAP_FILE.is_file():
        git = shutil.which("git")
        if git:
            ww_dir = cfg.TOOLS_DIR / "AnimeWwise"
            if not ww_dir.exists():
                _prog(70, "Cloning Escartem/AnimeWwise (nap.map)...")
                _stream_cmd(
                    [git, "clone", "--depth", "1", "https://github.com/Escartem/AnimeWwise.git", str(ww_dir)],
                    log_cb=log_cb,
                )
            cfg._resolve_tool_paths()

    return {
        "cli_exists": cfg.CLI_EXE.is_file(),
        "cli_path": str(cfg.CLI_EXE),
        "nap_map_exists": cfg.NAP_MAP_FILE.is_file(),
        "nap_map_path": str(cfg.NAP_MAP_FILE),
        "wine_exists": shutil.which("wine") is not None,
    }
