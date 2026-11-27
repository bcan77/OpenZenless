"""Automatic tool bootstrapper and cross-platform portable installers for OpenZenless."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from . import config as cfg


def _emit(log_cb, msg: str):
    if log_cb is not None:
        log_cb(msg)
    else:
        print(msg, flush=True)


def _stream_cmd(
    cmd: list[str],
    log_cb=None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> int:
    _emit(log_cb, f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env or cfg.build_cli_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **cfg.subprocess_no_window_kwargs(),
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        _emit(log_cb, line.rstrip("\n"))
    return proc.wait()


def _download_url(
    url: str,
    dest: Path,
    log_cb=None,
    progress_cb=None,
    cancel_event: threading.Event | None = None,
    label: str = "",
    timeout: int = 30,
) -> Path:
    """Download `url` to `dest` via a `.part` temp file with throttled progress and cancel support."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    tag = label or dest.name
    _emit(log_cb, f"Downloading {tag}: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "OpenZenless/0.3"})
    start_t = time.monotonic()
    last_cb_t = 0.0
    last_log_pct = -10
    done = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, part.open("wb") as out:
            cl = resp.headers.get("Content-Length")
            total = int(cl) if cl and cl.isdigit() else 0
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError(f"Cancelled download: {tag}")
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                pct = int(done * 100 / total) if total > 0 else -1
                if now - last_cb_t >= 0.25 or (total > 0 and done >= total):
                    last_cb_t = now
                    mb_done = done / (1024 * 1024)
                    rate = mb_done / max(0.2, now - start_t)
                    if total > 0:
                        mb_tot = total / (1024 * 1024)
                        msg = f"{tag}: {mb_done:.1f} / {mb_tot:.1f} MB ({rate:.1f} MB/s)"
                    else:
                        msg = f"{tag}: {mb_done:.1f} MB ({rate:.1f} MB/s)"
                    if progress_cb is not None:
                        progress_cb(pct, msg)
                    if log_cb is not None and pct >= 0 and pct - last_log_pct >= 20:
                        last_log_pct = pct
                        _emit(log_cb, f"  {msg} ({pct}%)")
        part.replace(dest)
        _emit(log_cb, f"Downloaded {tag} ({done / (1024 * 1024):.1f} MB)")
        return dest
    except Exception:
        part.unlink(missing_ok=True)
        raise


def _safe_dest_path(dest_dir: Path, raw_name: str, strip_top_dir: bool) -> Path | None:
    norm = raw_name.replace("\\", "/").lstrip("/")
    if not norm:
        return None
    posix = PurePosixPath(norm)
    parts = [p for p in posix.parts if p not in ("", ".")]
    if strip_top_dir:
        parts = parts[1:]
    if not parts or any(p == ".." for p in parts):
        return None
    dest_resolved = dest_dir.resolve()
    target = (dest_resolved.joinpath(*parts)).resolve()
    if dest_resolved not in target.parents and target != dest_resolved:
        return None
    return target


def _safe_extract_zip(zip_path: Path, dest_dir: Path, strip_top_dir: bool = False) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                target = _safe_dest_path(dest_dir, info.filename, strip_top_dir)
                if target is not None:
                    target.mkdir(parents=True, exist_ok=True)
                continue
            target = _safe_dest_path(dest_dir, info.filename, strip_top_dir)
            if target is None:
                continue
            # Skip symlink entries in untrusted zips unless normal files
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            perm = (info.external_attr >> 16) & 0o777
            if os.name == "posix" and (perm & 0o111):
                try:
                    os.chmod(target, perm | 0o755)
                except OSError:
                    pass


def _safe_extract_tar(tar_path: Path, dest_dir: Path, strip_top_dir: bool = False) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    symlinks: list[tuple[Path, str]] = []
    with tarfile.open(tar_path, "r:*") as tf:
        for member in tf.getmembers():
            target = _safe_dest_path(dest_dir, member.name, strip_top_dir)
            if target is None:
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.issym():
                symlinks.append((target, member.linkname))
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                if src is not None:
                    with src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                if os.name == "posix" and (member.mode & 0o111):
                    try:
                        os.chmod(target, (member.mode & 0o777) | 0o755)
                    except OSError:
                        pass
        for link_target, linkname in symlinks:
            link_path = Path(linkname)
            if link_path.is_absolute():
                continue
            resolved_link = (link_target.parent / link_path).resolve()
            if dest_resolved not in resolved_link.parents and resolved_link != dest_resolved:
                continue
            try:
                link_target.parent.mkdir(parents=True, exist_ok=True)
                link_target.unlink(missing_ok=True)
                os.symlink(linkname, link_target)
            except OSError:
                pass


def _chmod_exec(path: Path) -> None:
    if os.name == "posix" and path.is_file():
        try:
            os.chmod(path, path.stat().st_mode | 0o755)
        except OSError:
            pass


_OZ_DUMP_CS = (
    "        private static readonly Regex OzPrefabDirRegex = new Regex(\n"
    "            @\"Assets/NapResources/NapStreaming/([A-Za-z0-9_.\\-]+)/Prefab/?\",\n"
    "            RegexOptions.Compiled);\n\n"
    "        private static readonly Regex OzSectorRegex = new Regex(\n"
    "            @\"^(?!MC_)[A-Z][A-Z0-9]{0,5}_[0-9]+(?:_HLOD)?$\",\n"
    "            RegexOptions.Compiled);\n\n"
    "        private static readonly Regex OzMeshPathRegex = new Regex(\n"
    "            @\"Assets/[A-Za-z0-9_./\\- ()]+\\.mesh\",\n"
    "            RegexOptions.Compiled | RegexOptions.IgnoreCase);\n\n"
    "        private static string _lastOzFontBlk = \"\";\n"
    "        private static string _lastOzShaderBlk = \"\";\n\n"
    "        public static void DumpOzIndexForLoadedFiles(string blkFile, StreamWriter jsonlOut, string baseDir = \"\")\n"
    "        {\n"
    "            string blkRel;\n"
    "            if (!string.IsNullOrEmpty(baseDir))\n"
    "            {\n"
    "                string rel = Path.GetRelativePath(Path.GetFullPath(baseDir), Path.GetFullPath(blkFile)).Replace('\\\\', '/');\n"
    "                blkRel = !rel.StartsWith(\"..\") ? rel : blkFile.Replace('\\\\', '/');\n"
    "            }\n"
    "            else\n"
    "            {\n"
    "                string normBlk = blkFile.Replace('\\\\', '/');\n"
    "                int blocksIdx = normBlk.IndexOf(\"/Blocks/\", StringComparison.OrdinalIgnoreCase);\n"
    "                blkRel = blocksIdx >= 0 ? normBlk.Substring(blocksIdx + \"/Blocks/\".Length) : Path.GetFileName(blkFile);\n"
    "            }\n\n"
    "            var targetScripts = new Dictionary<(string cab, long pathId), string>();\n"
    "            bool hasTargetScripts = false;\n"
    "            foreach (var sf in assetsManager.assetsFileList)\n"
    "            {\n"
    "                if (sf == null) continue;\n"
    "                int meshCount = 0;\n"
    "                if (!string.IsNullOrEmpty(sf.fileName) && sf.fileName.StartsWith(\"BuildPlayer-\", StringComparison.OrdinalIgnoreCase) && !sf.fileName.EndsWith(\".sharedAssets\", StringComparison.OrdinalIgnoreCase))\n"
    "                    jsonlOut.WriteLine(JsonConvert.SerializeObject(new { k = \"scene\", blk = blkRel, cab = sf.fileName }, global::Newtonsoft.Json.Formatting.None));\n"
    "                if (sf.m_Objects != null)\n"
    "                {\n"
    "                    foreach (var oi in sf.m_Objects)\n"
    "                    {\n"
    "                        if (oi.classID == (int)ClassIDType.Mesh) meshCount++;\n"
    "                        else if (oi.classID == (int)ClassIDType.Font && !string.Equals(_lastOzFontBlk, blkRel, StringComparison.Ordinal))\n"
    "                        {\n"
    "                            _lastOzFontBlk = blkRel;\n"
    "                            jsonlOut.WriteLine(JsonConvert.SerializeObject(new { k = \"font\", blk = blkRel }, global::Newtonsoft.Json.Formatting.None));\n"
    "                        }\n"
    "                        else if (oi.classID == (int)ClassIDType.Shader && !string.Equals(_lastOzShaderBlk, blkRel, StringComparison.Ordinal))\n"
    "                        {\n"
    "                            _lastOzShaderBlk = blkRel;\n"
    "                            jsonlOut.WriteLine(JsonConvert.SerializeObject(new { k = \"shader\", blk = blkRel }, global::Newtonsoft.Json.Formatting.None));\n"
    "                        }\n"
    "                        else if (oi.classID == (int)ClassIDType.MonoScript)\n"
    "                        {\n"
    "                            try\n"
    "                            {\n"
    "                                var ms = new MonoScript(new ObjectReader(sf.reader, sf, oi, Game));\n"
    "                                string cls = ms.m_ClassName ?? ms.Name ?? \"\";\n"
    "                                if (string.Equals(cls, \"NapStreamingData\", StringComparison.OrdinalIgnoreCase) || string.Equals(cls, \"NapLodController\", StringComparison.OrdinalIgnoreCase))\n"
    "                                {\n"
    "                                    targetScripts[(sf.fileName ?? \"\", oi.m_PathID)] = cls;\n"
    "                                    hasTargetScripts = true;\n"
    "                                }\n"
    "                            }\n"
    "                            catch { }\n"
    "                        }\n"
    "                    }\n"
    "                }\n"
    "                var cabRow = new { k = \"cab\", blk = blkRel, cab = sf.fileName ?? \"\", off = sf.offset, meshes = meshCount, deps = sf.m_Externals != null ? sf.m_Externals.Select(x => x.fileName).ToList() : new List<string>() };\n"
    "                jsonlOut.WriteLine(JsonConvert.SerializeObject(cabRow, global::Newtonsoft.Json.Formatting.None));\n"
    "            }\n\n"
    "            var infoByCab = new Dictionary<string, Dictionary<long, ObjectInfo>>(StringComparer.OrdinalIgnoreCase);\n"
    "            var mainContainerByCabId = new Dictionary<(string cab, long pathId), string>();\n"
    "            var preloadContainerByCabId = new Dictionary<(string cab, long pathId), string>();\n"
    "            foreach (var sf in assetsManager.assetsFileList)\n"
    "            {\n"
    "                if (sf?.m_Objects == null) continue;\n"
    "                var sfCab = sf.fileName ?? \"\";\n"
    "                var infoById = new Dictionary<long, ObjectInfo>(sf.m_Objects.Count);\n"
    "                foreach (var oi in sf.m_Objects) infoById[oi.m_PathID] = oi;\n"
    "                infoByCab[sfCab] = infoById;\n"
    "                foreach (var oi in sf.m_Objects)\n"
    "                {\n"
    "                    if (oi.classID != (int)ClassIDType.AssetBundle) continue;\n"
    "                    try\n"
    "                    {\n"
    "                        var ab = new AssetBundle(new ObjectReader(sf.reader, sf, oi, Game));\n"
    "                        foreach (var kv in ab.m_Container)\n"
    "                        {\n"
    "                            string key = kv.Key ?? \"\";\n"
    "                            var info = kv.Value;\n"
    "                            string mainType = \"\"; string mainName = \"\";\n"
    "                            if (info.asset != null && info.asset.m_FileID == 0 && infoById.TryGetValue(info.asset.m_PathID, out var mainOi))\n"
    "                            {\n"
    "                                var mainCls = (ClassIDType)mainOi.classID;\n"
    "                                mainType = mainCls.ToString();\n"
    "                                mainContainerByCabId[(sfCab, mainOi.m_PathID)] = key;\n"
    "                                try\n"
    "                                {\n"
    "                                    if (mainCls == ClassIDType.GameObject) mainName = new GameObject(new ObjectReader(sf.reader, sf, mainOi, Game)).m_Name ?? \"\";\n"
    "                                    else if (mainCls == ClassIDType.Animator)\n"
    "                                    {\n"
    "                                        var anim = new Animator(new ObjectReader(sf.reader, sf, mainOi, Game));\n"
    "                                        if (anim.m_GameObject != null && anim.m_GameObject.m_FileID == 0 && infoById.TryGetValue(anim.m_GameObject.m_PathID, out var goOi))\n"
    "                                            mainName = new GameObject(new ObjectReader(sf.reader, sf, goOi, Game)).m_Name ?? \"\";\n"
    "                                    }\n"
    "                                    else if (mainCls == ClassIDType.Mesh || mainCls == ClassIDType.AnimationClip || mainCls == ClassIDType.Font || mainCls == ClassIDType.TextAsset || mainCls == ClassIDType.Sprite || mainCls == ClassIDType.Texture2D || mainCls == ClassIDType.Shader)\n"
    "                                    {\n"
    "                                        var nr = new ObjectReader(sf.reader, sf, mainOi, Game); nr.Reset();\n"
    "                                        if (nr.platform == BuildTarget.NoTarget) { _ = new PPtr<EditorExtension>(nr); _ = new PPtr<Object>(nr); }\n"
    "                                        mainName = nr.ReadAlignedString() ?? \"\";\n"
    "                                    }\n"
    "                                    else if (mainCls == ClassIDType.MonoBehaviour) mainName = new MonoBehaviour(new ObjectReader(sf.reader, sf, mainOi, Game)).m_Name ?? \"\";\n"
    "                                }\n"
    "                                catch { }\n"
    "                            }\n"
    "                            if (hasTargetScripts && ab.m_PreloadTable != null)\n"
    "                            {\n"
    "                                int safeStart = Math.Max(0, info.preloadIndex);\n"
    "                                int safeEnd = Math.Min(info.preloadIndex + info.preloadSize, ab.m_PreloadTable.Count);\n"
    "                                for (int k = safeStart; k < safeEnd; k++)\n"
    "                                    try { var pptr = ab.m_PreloadTable[k]; if (pptr != null && pptr.m_FileID == 0) preloadContainerByCabId.TryAdd((sfCab, pptr.m_PathID), key); } catch { }\n"
    "                            }\n"
    "                            jsonlOut.WriteLine(JsonConvert.SerializeObject(new { k = \"ab\", blk = blkRel, cab = sfCab, key, type = mainType, name = mainName }, global::Newtonsoft.Json.Formatting.None));\n"
    "                        }\n"
    "                    }\n"
    "                    catch (Exception ex) { Logger.Warning($\"[OzIndex] AssetBundle error in {blkRel}/{sfCab}: {ex.Message}\"); }\n"
    "                }\n"
    "            }\n"
    "            foreach (var sf in assetsManager.assetsFileList)\n"
    "            {\n"
    "                if (sf?.m_Objects == null) continue;\n"
    "                var sfCab = sf.fileName ?? \"\";\n"
    "                infoByCab.TryGetValue(sfCab, out var infoById);\n"
    "                foreach (var oi in sf.m_Objects)\n"
    "                {\n"
    "                    if (oi.classID != (int)ClassIDType.MonoBehaviour) continue;\n"
    "                    try\n"
    "                    {\n"
    "                        var mb = new MonoBehaviour(new ObjectReader(sf.reader, sf, oi, Game));\n"
    "                        string scriptName = \"\";\n"
    "                        if (mb.m_Script != null && mb.m_Script.m_PathID != 0)\n"
    "                        {\n"
    "                            string scriptCab = mb.m_Script.m_FileID == 0 ? sfCab : (mb.m_Script.m_FileID > 0 && sf.m_Externals != null && mb.m_Script.m_FileID <= sf.m_Externals.Count ? (sf.m_Externals[mb.m_Script.m_FileID - 1].fileName ?? \"\") : \"\");\n"
    "                            if (!string.IsNullOrEmpty(scriptCab)) targetScripts.TryGetValue((scriptCab, mb.m_Script.m_PathID), out scriptName);\n"
    "                        }\n"
    "                        string objName = mb.m_Name ?? \"\";\n"
    "                        if (string.Equals(scriptName, \"NapStreamingData\", StringComparison.OrdinalIgnoreCase) || objName.IndexOf(\"NapStreamingData\", StringComparison.OrdinalIgnoreCase) >= 0)\n"
    "                        {\n"
    "                            var raw = mb.GetRawData();\n"
    "                            if (raw != null && raw.Length > 0)\n"
    "                            {\n"
    "                                string text = global::System.Text.Encoding.Latin1.GetString(raw);\n"
    "                                var dirMatch = OzPrefabDirRegex.Match(text);\n"
    "                                string mapName = dirMatch.Success ? dirMatch.Groups[1].Value : objName;\n"
    "                                string prefabDir = dirMatch.Success ? dirMatch.Value.TrimEnd('/') : \"\";\n"
    "                                var seen = new HashSet<string>(StringComparer.Ordinal); var sectors = new List<string>();\n"
    "                                for (int pos = 0; pos + 8 <= raw.Length; pos += 4)\n"
    "                                {\n"
    "                                    int slen = global::System.Buffers.Binary.BinaryPrimitives.ReadInt32LittleEndian(raw.AsSpan(pos, 4));\n"
    "                                    if (slen < 3 || slen > 64 || pos + 4 + slen > raw.Length) continue;\n"
    "                                    int pad = (4 - (slen & 3)) & 3;\n"
    "                                    if (pos + 4 + slen + pad > raw.Length) continue;\n"
    "                                    bool ok = true;\n"
    "                                    for (int p = 0; p < pad; p++) { if (raw[pos + 4 + slen + p] != 0) { ok = false; break; } }\n"
    "                                    if (!ok) continue;\n"
    "                                    for (int c = 0; c < slen; c++) { byte b = raw[pos + 4 + c]; if (!((b >= (byte)'A' && b <= (byte)'Z') || (b >= (byte)'0' && b <= (byte)'9') || b == (byte)'_')) { ok = false; break; } }\n"
    "                                    if (!ok) continue;\n"
    "                                    string tok = global::System.Text.Encoding.ASCII.GetString(raw, pos + 4, slen);\n"
    "                                    if (OzSectorRegex.IsMatch(tok) && seen.Add(tok)) sectors.Add(tok);\n"
    "                                }\n"
    "                                string container = mainContainerByCabId.TryGetValue((sfCab, oi.m_PathID), out var mc) ? mc : (preloadContainerByCabId.TryGetValue((sfCab, oi.m_PathID), out var pc) ? pc : \"\");\n"
    "                                jsonlOut.WriteLine(JsonConvert.SerializeObject(new { k = \"stream\", blk = blkRel, cab = sfCab, key = container, size = oi.byteSize, map = mapName, prefab_dir = prefabDir, sectors }, global::Newtonsoft.Json.Formatting.None));\n"
    "                            }\n"
    "                        }\n"
    "                        else if (string.Equals(scriptName, \"NapLodController\", StringComparison.OrdinalIgnoreCase) || (string.IsNullOrEmpty(scriptName) && oi.byteSize >= 32 && oi.byteSize <= 16384))\n"
    "                        {\n"
    "                            var raw = mb.GetRawData();\n"
    "                            if (raw != null && raw.Length > 0)\n"
    "                            {\n"
    "                                string text = global::System.Text.Encoding.Latin1.GetString(raw);\n"
    "                                if (string.IsNullOrEmpty(scriptName) && (text.IndexOf(\"Assets/\", StringComparison.OrdinalIgnoreCase) < 0 || text.IndexOf(\".mesh\", StringComparison.OrdinalIgnoreCase) < 0))\n"
    "                                    continue;\n"
    "                                var matches = OzMeshPathRegex.Matches(text);\n"
    "                                if (matches.Count > 0)\n"
    "                                {\n"
    "                                    string goName = \"\";\n"
    "                                    try { if (mb.m_GameObject != null && mb.m_GameObject.m_FileID == 0 && infoById != null && infoById.TryGetValue(mb.m_GameObject.m_PathID, out var goOi)) goName = new GameObject(new ObjectReader(sf.reader, sf, goOi, Game)).m_Name ?? \"\"; } catch { }\n"
    "                                    var seenPaths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);\n"
    "                                    foreach (Match m in matches)\n"
    "                                    {\n"
    "                                        if (seenPaths.Add(m.Value))\n"
    "                                            jsonlOut.WriteLine(JsonConvert.SerializeObject(new { k = \"lod\", blk = blkRel, cab = sfCab, go = goName, mesh_path = m.Value }, global::Newtonsoft.Json.Formatting.None));\n"
    "                                    }\n"
    "                                }\n"
    "                            }\n"
    "                        }\n"
    "                    }\n"
    "                    catch (Exception ex) { Logger.Warning($\"[OzIndex] Object error in {blkRel}/{sfCab}#{oi.m_PathID}: {ex.Message}\"); }\n"
    "                }\n"
    "            }\n"
    "        }"
)


def _patch_animestudio_cli(studio_dir: Path, log_cb=None) -> bool:
    """Apply OpenZenless material, diffuse-priority, .blklist, and CABMap fixes to AnimeStudio."""
    changed = False

    def _replace_in_file(rel: str, replacements: list[tuple[str, str]]):
        nonlocal changed
        p = studio_dir / rel
        if not p.is_file():
            return
        txt = p.read_text(encoding="utf-8", errors="replace")
        orig = txt
        for old, new in replacements:
            if new not in txt and old in txt:
                txt = txt.replace(old, new, 1)
        if txt != orig:
            p.write_text(txt, encoding="utf-8")
            _emit(log_cb, f"Patched {rel}")
            changed = True

    def _write_file_if_diff(rel: str, content: str):
        nonlocal changed
        p = studio_dir / rel
        if p.is_file() and p.read_text(encoding="utf-8", errors="replace") == content:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        _emit(log_cb, f"Wrote {rel}")
        changed = True

    _write_file_if_diff(
        "AnimeStudio/Classes/LODGroup.cs",
        "using System.Collections.Generic;\n\n"
        "namespace AnimeStudio\n"
        "{\n"
        "    public sealed class LODGroup : Component\n"
        "    {\n"
        "        public Vector3 m_LocalReferencePoint;\n"
        "        public float m_Size;\n"
        "        public int m_FadeMode;\n"
        "        public bool m_AnimateCrossFading;\n"
        "        public bool m_LastLODIsBillboard;\n"
        "        public List<List<PPtr<Renderer>>> m_LODRenderers = new List<List<PPtr<Renderer>>>();\n\n"
        "        public LODGroup(ObjectReader reader) : base(reader)\n"
        "        {\n"
        "            m_LocalReferencePoint = reader.ReadVector3();\n"
        "            m_Size = reader.ReadSingle();\n"
        "            m_FadeMode = reader.ReadInt32();\n"
        "            m_AnimateCrossFading = reader.ReadBoolean();\n"
        "            m_LastLODIsBillboard = reader.ReadBoolean();\n"
        "            reader.AlignStream();\n\n"
        "            int lodCount = reader.ReadInt32();\n"
        "            bool isZZZ = reader.Game?.Type.IsZZZGroup() == true;\n"
        "            if (lodCount > 0 && lodCount <= 16)\n"
        "            {\n"
        "                for (int i = 0; i < lodCount; i++)\n"
        "                {\n"
        "                    reader.ReadSingle();\n"
        "                    reader.ReadSingle();\n"
        "                    if (isZZZ)\n"
        "                        reader.ReadInt32();\n"
        "                    int rendererCount = reader.ReadInt32();\n"
        "                    var renderers = new List<PPtr<Renderer>>();\n"
        "                    if (rendererCount > 0 && rendererCount <= 2048)\n"
        "                    {\n"
        "                        for (int j = 0; j < rendererCount; j++)\n"
        "                        {\n"
        "                            renderers.Add(new PPtr<Renderer>(reader));\n"
        "                        }\n"
        "                    }\n"
        "                    m_LODRenderers.Add(renderers);\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}\n",
    )

    _write_file_if_diff(
        "AnimeStudio/Classes/NapLODMeshFilter.cs",
        "using System;\n"
        "using System.Collections.Generic;\n\n"
        "namespace AnimeStudio\n"
        "{\n"
        "    public sealed class NapLODMeshFilter : MeshFilter\n"
        "    {\n"
        "        public Vector3 m_LocalReferencePoint;\n"
        "        public List<(float Distance, PPtr<Mesh> Mesh)> m_LODs = new List<(float Distance, PPtr<Mesh> Mesh)>();\n\n"
        "        public NapLODMeshFilter(ObjectReader reader) : base(reader, true)\n"
        "        {\n"
        "            m_LocalReferencePoint = reader.ReadVector3();\n"
        "            int lodCount = reader.ReadInt32();\n"
        "            if (lodCount > 0 && lodCount <= 16)\n"
        "            {\n"
        "                for (int i = 0; i < lodCount; i++)\n"
        "                {\n"
        "                    float dist = reader.ReadSingle();\n"
        "                    var mesh = new PPtr<Mesh>(reader);\n"
        "                    m_LODs.Add((dist, mesh));\n"
        "                }\n"
        "            }\n"
        "            PPtr<Mesh> chosen = null;\n"
        "            foreach (var entry in m_LODs)\n"
        "            {\n"
        "                if (entry.Mesh != null && !entry.Mesh.IsNull)\n"
        "                {\n"
        "                    chosen = entry.Mesh;\n"
        "                    break;\n"
        "                }\n"
        "            }\n"
        "            m_Mesh = chosen ?? (m_LODs.Count > 0 ? m_LODs[0].Mesh : null);\n"
        "        }\n"
        "    }\n"
        "}\n",
    )

    _replace_in_file("AnimeStudio.CLI/App.config", [
        (
            '<add key="exportMaterials" value="False" />',
            '<add key="exportMaterials" value="True" />',
        ),
        (
            '<add key="texs" value=\'{}\' />',
            '<add key="texs" value=\'{"_MainTex":0,"_BaseMap":0,"_BaseColorMap":0,"_Diffuse":0,"_Albedo":0,"_MainTex0":0,"_BumpMap":3,"_NormalMap":3,"_MetallicGlossMap":2,"_SpecGlossMap":2}\' />',
        ),
        (
            '<add key="types" value=\'{"Animation":{"Item1":true,"Item2":false},"AnimationClip":{"Item1":true,"Item2":true},"Animator":{"Item1":true,"Item2":true},"AnimatorController":{"Item1":true,"Item2":false},"AnimatorOverrideController":{"Item1":true,"Item2":false},"AssetBundle":{"Item1":true,"Item2":false},"AudioClip":{"Item1":true,"Item2":true},"Avatar":{"Item1":true,"Item2":false},"Font":{"Item1":true,"Item2":true},"GameObject":{"Item1":true,"Item2":false},"IndexObject":{"Item1":true,"Item2":false},"Material":{"Item1":true,"Item2":true},"Mesh":{"Item1":true,"Item2":true},"MeshFilter":{"Item1":true,"Item2":false},"MeshRenderer":{"Item1":true,"Item2":false},"MiHoYoBinData":{"Item1":true,"Item2":true},"MonoBehaviour":{"Item1":true,"Item2":true},"MonoScript":{"Item1":true,"Item2":false},"MovieTexture":{"Item1":true,"Item2":true},"PlayerSettings":{"Item1":true,"Item2":false},"RectTransform":{"Item1":true,"Item2":false},"Shader":{"Item1":true,"Item2":true},"SkinnedMeshRenderer":{"Item1":true,"Item2":false},"Sprite":{"Item1":true,"Item2":true},"SpriteAtlas":{"Item1":true,"Item2":false},"TextAsset":{"Item1":true,"Item2":true},"Texture2D":{"Item1":true,"Item2":true},"Transform":{"Item1":true,"Item2":false},"VideoClip":{"Item1":true,"Item2":true},"ResourceManager":{"Item1":true,"Item2":false}}\' />',
            '<add key="types" value=\'{"Animation":{"Item1":true,"Item2":false},"AnimationClip":{"Item1":true,"Item2":true},"Animator":{"Item1":true,"Item2":true},"AnimatorController":{"Item1":true,"Item2":false},"AnimatorOverrideController":{"Item1":true,"Item2":false},"AssetBundle":{"Item1":true,"Item2":false},"AudioClip":{"Item1":true,"Item2":true},"Avatar":{"Item1":true,"Item2":false},"Font":{"Item1":true,"Item2":true},"GameObject":{"Item1":true,"Item2":false},"IndexObject":{"Item1":true,"Item2":false},"LODGroup":{"Item1":true,"Item2":false},"Material":{"Item1":true,"Item2":true},"Mesh":{"Item1":true,"Item2":true},"MeshFilter":{"Item1":true,"Item2":false},"MeshRenderer":{"Item1":true,"Item2":false},"MiHoYoBinData":{"Item1":true,"Item2":true},"MonoBehaviour":{"Item1":true,"Item2":true},"MonoScript":{"Item1":true,"Item2":false},"MovieTexture":{"Item1":true,"Item2":true},"PlayerSettings":{"Item1":true,"Item2":false},"RectTransform":{"Item1":true,"Item2":false},"Shader":{"Item1":true,"Item2":true},"SkinnedMeshRenderer":{"Item1":true,"Item2":false},"Sprite":{"Item1":true,"Item2":true},"SpriteAtlas":{"Item1":true,"Item2":false},"TextAsset":{"Item1":true,"Item2":true},"Texture2D":{"Item1":true,"Item2":true},"Transform":{"Item1":true,"Item2":false},"VideoClip":{"Item1":true,"Item2":true},"ResourceManager":{"Item1":true,"Item2":false},"NapAssetBundleIndexAsset":{"Item1":true,"Item2":true},"NapLODMeshFilter":{"Item1":true,"Item2":false}}\' />',
        ),
    ])

    _replace_in_file("AnimeStudio.CLI/Program.cs", [
        (
            "                if (o.GroupAssetsType == AssetGroupOption.ByContainer)",
            "                if (o.GroupAssetsType == AssetGroupOption.ByContainer\n"
            "                    || !string.IsNullOrEmpty(Environment.GetEnvironmentVariable(\"AS_CONTAINER_HASH_FILE\")))",
        ),
        (
            "                var files = o.Input.Attributes.HasFlag(FileAttributes.Directory) ? Directory.GetFiles(o.Input.FullName, \"*.*\", SearchOption.AllDirectories).OrderBy(x => x.Length).ToArray() : new string[] { o.Input.FullName };",
            "                string[] files;\n"
            "                if (o.Input.Extension.Equals(\".blklist\", StringComparison.OrdinalIgnoreCase))\n"
            "                {\n"
            "                    files = File.ReadAllLines(o.Input.FullName).Where(l => !string.IsNullOrWhiteSpace(l)).Select(l => Path.GetFullPath(l.Trim())).ToArray();\n"
            "                }\n"
            "                else\n"
            "                {\n"
            "                    files = o.Input.Attributes.HasFlag(FileAttributes.Directory) ? Directory.GetFiles(o.Input.FullName, \"*.*\", SearchOption.AllDirectories).OrderBy(x => x.Length).ToArray() : new string[] { o.Input.FullName };\n"
            "                }",
        ),
        (
            "                        AssetsHelper.BuildCABMap(files, o.MapName, o.Input.FullName, game);",
            "                        var cabBase = Environment.GetEnvironmentVariable(\"AS_CABMAP_BASE_DIR\");\n"
            "                        if (string.IsNullOrEmpty(cabBase))\n"
            "                            cabBase = o.Input.Attributes.HasFlag(FileAttributes.Directory) ? o.Input.FullName : Path.GetDirectoryName(files[0]);\n"
            "                        AssetsHelper.BuildCABMap(files, o.MapName, Path.GetFullPath(cabBase), game);",
        ),
        (
            "                if (o.MapOp.Equals(MapOpType.None) || o.MapOp.HasFlag(MapOpType.Load))",
            "                if (o.MapOp.Equals(MapOpType.None) || o.MapOp.Equals(MapOpType.CABMap) || o.MapOp.Equals(MapOpType.AssetMap | MapOpType.Load))",
        ),
        (
            "                    var path = Path.GetDirectoryName(Path.GetFullPath(files[0]));\n"
            "                    ImportHelper.MergeSplitAssets(path);\n"
            "                    var toReadFile = ImportHelper.ProcessingSplitFiles(files.ToList());\n"
            "\n"
            "                    var fileList = new List<string>(toReadFile);",
            "                    var fileList = new List<string>();\n"
            "                    if (o.Input.Extension.Equals(\".blklist\", StringComparison.OrdinalIgnoreCase))\n"
            "                    {\n"
            "                        fileList.AddRange(files);\n"
            "                    }\n"
            "                    else\n"
            "                    {\n"
            "                        var path = Path.GetDirectoryName(Path.GetFullPath(files[0]));\n"
            "                        ImportHelper.MergeSplitAssets(path);\n"
            "                        var toReadFile = ImportHelper.ProcessingSplitFiles(files.ToList());\n"
            "                        fileList.AddRange(toReadFile);\n"
            "                    }\n\n"
            "                    var extractDir = Environment.GetEnvironmentVariable(\"AS_EXTRACT_DIR\");\n"
            "                    if (!string.IsNullOrEmpty(extractDir))\n"
            "                    {\n"
            "                        Directory.CreateDirectory(extractDir);\n"
            "                        int extDone = 0;\n"
            "                        foreach (var file in fileList)\n"
            "                        {\n"
            "                            try\n"
            "                            {\n"
            "                                string norm = file.Replace('\\\\', '/');\n"
            "                                string subDir = \"\";\n"
            "                                int bIdx = norm.IndexOf(\"/Blocks/\", StringComparison.OrdinalIgnoreCase);\n"
            "                                if (bIdx >= 0)\n"
            "                                {\n"
            "                                    string rel = norm.Substring(bIdx + \"/Blocks/\".Length);\n"
            "                                    subDir = Path.GetDirectoryName(rel) ?? \"\";\n"
            "                                }\n"
            "                                string targetDir = string.IsNullOrEmpty(subDir) ? extractDir : Path.Combine(extractDir, subDir);\n"
            "                                Directory.CreateDirectory(targetDir);\n"
            "                                ExtractFile(file, targetDir);\n"
            "                            }\n"
            "                            catch (Exception ex) { Logger.Error($\"[Extract] Failed {file}: {ex}\"); }\n"
            "                            if (++extDone % 50 == 0 || extDone == fileList.Count)\n"
            "                                Console.WriteLine($\"[Extract] {extDone}/{fileList.Count}\");\n"
            "                        }\n"
            "                        return;\n"
            "                    }\n\n"
            "                    var ozIndexOut = Environment.GetEnvironmentVariable(\"AS_DUMP_OZ_INDEX\");\n"
            "                    if (!string.IsNullOrEmpty(ozIndexOut))\n"
            "                    {\n"
            "                        var ozBaseDir = Environment.GetEnvironmentVariable(\"AS_OZ_INDEX_BASE_DIR\") ?? \"\";\n"
            "                        assetsManager.ResolveDependencies = false;\n"
            "                        assetsManager.SkipProcess = true;\n"
            "                        assetsManager.Silent = true;\n"
            "                        var ozDir = Path.GetDirectoryName(Path.GetFullPath(ozIndexOut));\n"
            "                        if (!string.IsNullOrEmpty(ozDir)) Directory.CreateDirectory(ozDir);\n"
            "                        using var jsonlOut = new StreamWriter(ozIndexOut, false, new System.Text.UTF8Encoding(false), 65536);\n"
            "                        int ozDone = 0;\n"
            "                        foreach (var file in fileList)\n"
            "                        {\n"
            "                            bool processedViaCallback = false;\n"
            "                            assetsManager.AfterBundleLoaded = () =>\n"
            "                            {\n"
            "                                if (assetsManager.assetsFileList.Count == 0 && assetsManager.ResourceFileCount == 0) return;\n"
            "                                processedViaCallback = true;\n"
            "                                if (assetsManager.assetsFileList.Count > 0)\n"
            "                                    DumpOzIndexForLoadedFiles(file, jsonlOut, ozBaseDir);\n"
            "                                assetsManager.ClearLoadedAssets();\n"
            "                                StringCache.Clear();\n"
            "                            };\n"
            "                            try\n"
            "                            {\n"
            "                                assetsManager.LoadFiles(new[] { file }, mergeSplitAssets: false);\n"
            "                                if (!processedViaCallback && assetsManager.assetsFileList.Count > 0)\n"
            "                                    DumpOzIndexForLoadedFiles(file, jsonlOut, ozBaseDir);\n"
            "                            }\n"
            "                            catch (Exception ex) { Logger.Error($\"[OzIndex] Failed {file}: {ex.Message}\"); }\n"
            "                            finally\n"
            "                            {\n"
            "                                assetsManager.AfterBundleLoaded = null;\n"
            "                                assetsManager.Clear();\n"
            "                                StringCache.Clear();\n"
            "                            }\n"
            "                            if (++ozDone % 32 == 0)\n"
            "                                GC.Collect(GC.MaxGeneration, GCCollectionMode.Optimized, blocking: false, compacting: false);\n"
            "                            if (ozDone % 50 == 0 || ozDone == fileList.Count)\n"
            "                            {\n"
            "                                jsonlOut.Flush();\n"
            "                                Console.WriteLine($\"[OzIndex] {ozDone}/{fileList.Count}\");\n"
            "                            }\n"
            "                        }\n"
            "                        jsonlOut.Flush();\n"
            "                        return;\n"
            "                    }\n\n"
            "                    var hashFile = Environment.GetEnvironmentVariable(\"AS_CONTAINER_HASH_FILE\");\n"
            "                    ContainerHashAllowList = (!string.IsNullOrEmpty(hashFile) && File.Exists(hashFile))\n"
            "                        ? new HashSet<string>(File.ReadAllLines(hashFile).Select(l => l.Trim()).Where(l => !string.IsNullOrEmpty(l) && !l.StartsWith(\"#\")), StringComparer.Ordinal)\n"
            "                        : null;\n"
            "                    var cabFilterEnv = Environment.GetEnvironmentVariable(\"AS_CAB_FILTER\");\n"
            "                    CabAllowList = !string.IsNullOrWhiteSpace(cabFilterEnv)\n"
            "                        ? new HashSet<string>(cabFilterEnv.Split(new[] { ',', ';' }, StringSplitOptions.RemoveEmptyEntries).Select(s => s.Trim()).Where(s => !string.IsNullOrEmpty(s)), StringComparer.OrdinalIgnoreCase)\n"
            "                        : null;",
        ),
        (
            "                        if (ClassIDType.GameObject.CanExport())\n"
            "                        {\n"
            "                            TypeFlags.SetType(ClassIDType.Animator, true, false);\n"
            "                        }",
            "                        if (ClassIDType.GameObject.CanExport())\n"
            "                        {\n"
            "                            TypeFlags.SetType(ClassIDType.Animator, true, false);\n"
            "                            TypeFlags.SetType(ClassIDType.LODGroup, true, false);\n"
            "                            TypeFlags.SetType(ClassIDType.NapLODMeshFilter, true, false);\n"
            "                            TypeFlags.SetType(ClassIDType.MonoBehaviour, true, ClassIDType.MonoBehaviour.CanExport());\n"
            "                            TypeFlags.SetType(ClassIDType.MonoScript, true, ClassIDType.MonoScript.CanExport());\n"
            "                        }",
        ),
        (
            "                    foreach (var file in fileList)\n"
            "                    {\n"
            "                        assetsManager.LoadFiles(file);\n"
            "                        if (assetsManager.assetsFileList.Count > 0)\n"
            "                        {\n"
            "                            BuildAssetData(classTypeFilter, o.NameFilter, o.ContainerFilter, ref i);\n"
            "                            ExportAssets(o.Output.FullName, exportableAssets, o.GroupAssetsType, o.AssetExportType);\n"
            "                        }\n"
            "                        exportableAssets.Clear();\n"
            "                        assetsManager.Clear();\n"
            "                    }\n"
            "                }\n"
            "                if (Properties.Settings.Default.scrapeMonos)",
            "                    IsSectorStage = false;\n"
            "                    ModelConverter.ResetStageDedup();\n"
            "                    foreach (var file in fileList)\n"
            "                    {\n"
            "                        PrimarySourceName = assetsManager.ResolveDependencies ? Path.GetFileName(file) : null;\n"
            "                        assetsManager.LoadFiles(new[] { file }, mergeSplitAssets: false);\n"
            "                        if (assetsManager.assetsFileList.Count > 0)\n"
            "                        {\n"
            "                            BuildAssetData(classTypeFilter, o.NameFilter, o.ContainerFilter, ref i);\n"
            "                            ExportAssets(o.Output.FullName, exportableAssets, o.GroupAssetsType, o.AssetExportType);\n"
            "                        }\n"
            "                        exportableAssets.Clear();\n"
            "                        assetsManager.Clear();\n"
            "                    }\n"
            "                    PrimarySourceName = null;\n"
            "                    ContainerHashAllowList = null;\n"
            "                    CabAllowList = null;\n"
            "                    IsSectorStage = false;\n"
            "                    ModelConverter.ResetStageDedup();\n"
            "                }\n"
            "                if (Properties.Settings.Default.scrapeMonos)",
        ),
    ])

    _replace_in_file("AnimeStudio.CLI/Exporter.cs", [
        (
            "        private static bool TryExportFolder(string dir, AssetItem item, out string fullPath)\n"
            "        {\n"
            "            var fileName = FixFileName(item.Text);\n"
            "            fullPath = Path.Combine(dir, fileName);\n"
            "            if (!Directory.Exists(fullPath))\n"
            "            {\n"
            "                return true;\n"
            "            }\n"
            "            if (Properties.Settings.Default.allowDuplicates)",
            "        private static bool TryExportFolder(string dir, AssetItem item, out string fullPath, bool forceAllowDuplicates = false)\n"
            "        {\n"
            "            var fileName = FixFileName(item.Text);\n"
            "            fullPath = Path.Combine(dir, fileName);\n"
            "            if (!Directory.Exists(fullPath))\n"
            "            {\n"
            "                return true;\n"
            "            }\n"
            "            if (forceAllowDuplicates || Properties.Settings.Default.allowDuplicates)",
        ),
        (
            "            var m_Animator = (Animator)item.Asset;\n"
            "            var options = new ModelConverter.Options()\n"
            "            {\n"
            "                imageFormat = Properties.Settings.Default.convertType,\n"
            "                game = Studio.Game,\n"
            "                collectAnimations = Properties.Settings.Default.collectAnimations,",
            "            var m_Animator = (Animator)item.Asset;\n"
            "            var options = new ModelConverter.Options()\n"
            "            {\n"
            "                imageFormat = Properties.Settings.Default.convertType,\n"
            "                game = Studio.Game,\n"
            "                collectAnimations = (Environment.GetEnvironmentVariable(\"AS_SKIP_ANIMATIONS\") != \"1\") && Properties.Settings.Default.collectAnimations,",
        ),
        (
            "        public static bool ExportGameObject(AssetItem item, string exportPath, List <AssetItem> animationList = null)\n"
            "        {\n"
            "            if (!TryExportFolder(exportPath, item, out var exportFullPath))\n"
            "                return false;\n\n"
            "            var m_GameObject = (GameObject)item.Asset;\n"
            "            return ExportGameObject(m_GameObject, exportFullPath + Path.DirectorySeparatorChar, animationList);\n"
            "        }\n\n"
            "        public static bool ExportGameObject(GameObject gameObject, string exportPath, List<AssetItem> animationList = null)\n"
            "        {\n"
            "            var options = new ModelConverter.Options()\n"
            "            {\n"
            "                imageFormat = Properties.Settings.Default.convertType,\n"
            "                game = Studio.Game,\n"
            "                collectAnimations = Properties.Settings.Default.collectAnimations,\n"
            "                exportMaterials = Properties.Settings.Default.exportMaterials,\n"
            "                materials = new HashSet<Material>(),\n"
            "                uvs = JsonConvert.DeserializeObject<Dictionary<string, (bool, int)>>(Properties.Settings.Default.uvs),\n"
            "                texs = JsonConvert.DeserializeObject<Dictionary<string, int>>(Properties.Settings.Default.texs),\n"
            "            };\n"
            "            var convert = animationList != null\n"
            "                ? new ModelConverter(gameObject, options, animationList.Select(x => (AnimationClip)x.Asset).ToArray())\n"
            "                : new ModelConverter(gameObject, options);\n"
            "            \n"
            "            if (convert.MeshList.Count == 0)\n"
            "            {\n"
            "                Logger.Info($\"GameObject {gameObject.m_Name} has no mesh, skipping...\");\n"
            "                return false;\n"
            "            }\n"
            "            if (options.exportMaterials)\n"
            "            {\n"
            "                var materialExportPath = Path.Combine(exportPath, \"Materials\");\n"
            "                Directory.CreateDirectory(materialExportPath);\n"
            "                foreach (var material in options.materials)\n"
            "                {\n"
            "                    var matItem = new AssetItem(material);\n"
            "                    ExportJSONFile(matItem, materialExportPath);\n"
            "                }\n"
            "            }\n"
            "            exportPath = exportPath + FixFileName(gameObject.m_Name) + \".fbx\";\n"
            "            ExportFbx(convert, exportPath);\n"
            "            return true;\n"
            "        }",
            "        private static ModelConverter ConvertGameObject(GameObject gameObject, List<AssetItem> animationList, out ModelConverter.Options options)\n"
            "        {\n"
            "            options = new ModelConverter.Options()\n"
            "            {\n"
            "                imageFormat = Properties.Settings.Default.convertType,\n"
            "                game = Studio.Game,\n"
            "                collectAnimations = (Environment.GetEnvironmentVariable(\"AS_SKIP_ANIMATIONS\") != \"1\") && Properties.Settings.Default.collectAnimations,\n"
            "                exportMaterials = Properties.Settings.Default.exportMaterials,\n"
            "                materials = new HashSet<Material>(),\n"
            "                uvs = JsonConvert.DeserializeObject<Dictionary<string, (bool, int)>>(Properties.Settings.Default.uvs),\n"
            "                texs = JsonConvert.DeserializeObject<Dictionary<string, int>>(Properties.Settings.Default.texs),\n"
            "            };\n"
            "            return animationList != null\n"
            "                ? new ModelConverter(gameObject, options, animationList.Select(x => (AnimationClip)x.Asset).ToArray())\n"
            "                : new ModelConverter(gameObject, options);\n"
            "        }\n\n"
            "        private static bool SaveConvertedGameObject(ModelConverter convert, ModelConverter.Options options, GameObject gameObject, string exportPath)\n"
            "        {\n"
            "            Directory.CreateDirectory(exportPath);\n"
            "            if (options.exportMaterials)\n"
            "            {\n"
            "                var materialExportPath = Path.Combine(exportPath, \"Materials\");\n"
            "                Directory.CreateDirectory(materialExportPath);\n"
            "                foreach (var material in options.materials)\n"
            "                {\n"
            "                    var matItem = new AssetItem(material);\n"
            "                    ExportJSONFile(matItem, materialExportPath);\n"
            "                }\n"
            "            }\n"
            "            var fbxPath = Path.Combine(exportPath, FixFileName(gameObject.m_Name) + \".fbx\");\n"
            "            ExportFbx(convert, fbxPath);\n"
            "            return true;\n"
            "        }\n\n"
            "        public static bool ExportGameObject(AssetItem item, string exportPath, List<AssetItem> animationList = null)\n"
            "        {\n"
            "            var m_GameObject = (GameObject)item.Asset;\n"
            "            var convert = ConvertGameObject(m_GameObject, animationList, out var options);\n"
            "            if (convert.MeshList.Count == 0)\n"
            "            {\n"
            "                Logger.Info($\"GameObject {m_GameObject.m_Name} has no mesh, skipping...\");\n"
            "                return false;\n"
            "            }\n"
            "            bool allowDups = Environment.GetEnvironmentVariable(\"AS_SKIP_PROXIES\") == \"1\";\n"
            "            if (!TryExportFolder(exportPath, item, out var exportFullPath, forceAllowDuplicates: allowDups))\n"
            "                return false;\n"
            "            return SaveConvertedGameObject(convert, options, m_GameObject, exportFullPath);\n"
            "        }\n\n"
            "        public static bool ExportGameObject(GameObject gameObject, string exportPath, List<AssetItem> animationList = null)\n"
            "        {\n"
            "            var convert = ConvertGameObject(gameObject, animationList, out var options);\n"
            "            if (convert.MeshList.Count == 0)\n"
            "            {\n"
            "                Logger.Info($\"GameObject {gameObject.m_Name} has no mesh, skipping...\");\n"
            "                return false;\n"
            "            }\n"
            "            return SaveConvertedGameObject(convert, options, gameObject, exportPath);\n"
            "        }",
        ),
        (
            "                case ClassIDType.Material:\n"
            "                    return ExportJSONFile(item, exportPath);",
            "                case ClassIDType.Material:\n"
            "                    return ExportJSONFile(item, exportPath);\n"
            "                case ClassIDType.NapAssetBundleIndexAsset:\n"
            "                    return ExportJSONFile(item, exportPath);",
        ),
        (
            "                exportAllNodes = Properties.Settings.Default.exportAllNodes,\n"
            "                exportSkins = Properties.Settings.Default.exportSkins,\n"
            "                exportAnimations = Properties.Settings.Default.exportAnimations,\n"
            "                exportBlendShape = Properties.Settings.Default.exportBlendShape,",
            "                exportAllNodes = Properties.Settings.Default.exportAllNodes,\n"
            "                exportSkins = Properties.Settings.Default.exportSkins,\n"
            "                exportAnimations = (Environment.GetEnvironmentVariable(\"AS_SKIP_ANIMATIONS\") != \"1\") && Properties.Settings.Default.exportAnimations,\n"
            "                exportBlendShape = Properties.Settings.Default.exportBlendShape,",
        ),
    ])

    _replace_in_file("AnimeStudio/Classes/MeshFilter.cs", [
        (
            "    public sealed class MeshFilter : Component\n"
            "    {\n"
            "        public PPtr<Mesh> m_Mesh;\n\n"
            "        public MeshFilter(ObjectReader reader) : base(reader)",
            "    public class MeshFilter : Component\n"
            "    {\n"
            "        public PPtr<Mesh> m_Mesh;\n\n"
            "        protected MeshFilter(ObjectReader reader, bool skipRead) : base(reader) { }\n\n"
            "        public MeshFilter(ObjectReader reader) : base(reader)",
        ),
    ])

    _replace_in_file("AnimeStudio/Classes/Renderer.cs", [
        (
            "        public StaticBatchInfo m_StaticBatchInfo;\n"
            "        public uint[] m_SubsetIndices;\n"
            "        private bool isNewHeader = false;",
            "        public StaticBatchInfo m_StaticBatchInfo;\n"
            "        public uint[] m_SubsetIndices;\n"
            "        public bool m_Enabled = true;\n"
            "        public byte m_CastShadows = 1;\n"
            "        private bool isNewHeader = false;",
        ),
        (
            "                    var m_Enabled = reader.ReadBoolean();\n"
            "                    var m_CastShadows = reader.ReadByte();",
            "                    m_Enabled = reader.ReadBoolean();\n"
            "                    m_CastShadows = reader.ReadByte();",
        ),
        (
            "                    var m_Enabled = reader.ReadBoolean();\n"
            "                    reader.AlignStream();\n"
            "                    var m_CastShadows = reader.ReadByte();",
            "                    m_Enabled = reader.ReadBoolean();\n"
            "                    reader.AlignStream();\n"
            "                    m_CastShadows = reader.ReadByte();",
        ),
    ])

    _replace_in_file("AnimeStudio/Classes/GameObject.cs", [
        (
            "using AnimeStudio;\n"
            "using System;\n"
            "using System.Collections.Generic;",
            "using AnimeStudio;\n"
            "using System;\n"
            "using System.Buffers.Binary;\n"
            "using System.Collections.Generic;",
        ),
        (
            "        public List<PPtr<Component>> m_Components;\n"
            "        public string m_Name;\n\n"
            "        public Transform m_Transform;\n"
            "        public MeshRenderer m_MeshRenderer;\n"
            "        public MeshFilter m_MeshFilter;",
            "        public List<PPtr<Component>> m_Components;\n"
            "        public string m_Name;\n"
            "        public int m_Layer;\n"
            "        public bool m_IsActive = true;\n"
            "        public bool m_SuppressExport = false;\n\n"
            "        public Transform m_Transform;\n"
            "        public MeshRenderer m_MeshRenderer;\n"
            "        public MeshFilter m_MeshFilter;\n"
            "        public LODGroup m_LODGroup;",
        ),
        (
            "            var m_Layer = reader.ReadInt32();\n"
            "            m_Name = reader.ReadAlignedString();",
            "            m_Layer = reader.ReadInt32();\n"
            "            m_Name = reader.ReadAlignedString();\n"
            "            if (reader.Position + 3 <= reader.byteStart + reader.byteSize)\n"
            "            {\n"
            "                var m_Tag = reader.ReadUInt16();\n"
            "                m_IsActive = reader.ReadBoolean();\n"
            "            }",
        ),
        (
            "                if (m_GameObject.m_SkinnedMeshRenderer != null)\n"
            "                {\n"
            "                    var mesh = GetMesh(m_GameObject.m_SkinnedMeshRenderer);\n"
            "                    meshes.Add(mesh != null);\n"
            "                }\n\n"
            "                foreach (var pptr in m_Transform.m_Children)\n"
            "                {\n"
            "                    if (pptr.TryGet(out var child))\n"
            "                        meshes.Add(HasMesh(child, meshes));\n"
            "                }\n\n"
            "                return meshes.Any(x => x == true);\n"
            "            }\n"
            "            catch(Exception e)\n"
            "            {\n"
            "                Logger.Warning($\"Unable to verify if {m_Transform?.Name} has meshes, skipping...\");\n"
            "                return false;\n"
            "            }\n"
            "        }",
            "                if (m_GameObject.m_SkinnedMeshRenderer != null)\n"
            "                {\n"
            "                    var mesh = GetMesh(m_GameObject.m_SkinnedMeshRenderer);\n"
            "                    meshes.Add(mesh != null);\n"
            "                }\n\n"
            "                if (m_GameObject.m_Components != null)\n"
            "                {\n"
            "                    foreach (var compPptr in m_GameObject.m_Components)\n"
            "                    {\n"
            "                        if (compPptr.TryGet<MonoBehaviour>(out var mb) && IsNapStreamingBrg(mb))\n"
            "                        {\n"
            "                            meshes.Add(true);\n"
            "                            break;\n"
            "                        }\n"
            "                    }\n"
            "                }\n\n"
            "                foreach (var pptr in m_Transform.m_Children)\n"
            "                {\n"
            "                    if (pptr.TryGet(out var child))\n"
            "                        meshes.Add(HasMesh(child, meshes));\n"
            "                }\n\n"
            "                return meshes.Any(x => x == true);\n"
            "            }\n"
            "            catch(Exception e)\n"
            "            {\n"
            "                Logger.Warning($\"Unable to verify if {m_Transform?.Name} has meshes, skipping...\");\n"
            "                return false;\n"
            "            }\n"
            "        }\n\n"
            "        private static bool IsNapStreamingBrg(MonoBehaviour mb)\n"
            "        {\n"
            "            if (mb == null)\n"
            "                return false;\n"
            "            var name = mb.Name;\n"
            "            if (!string.IsNullOrEmpty(name))\n"
            "                return string.Equals(name, \"NapStreamingBRGHelper\", StringComparison.OrdinalIgnoreCase);\n"
            "            if (mb.byteSize < 68 || mb.byteSize > 32 * 1024 * 1024)\n"
            "                return false;\n"
            "            var data = mb.GetRawData();\n"
            "            if (data == null || data.Length < 68)\n"
            "                return false;\n"
            "            int nameLen = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(28, 4));\n"
            "            if (nameLen < 0 || nameLen > 256)\n"
            "                return false;\n"
            "            int hdr = 32 + ((nameLen + 3) & ~3);\n"
            "            if (hdr + 36 > data.Length)\n"
            "                return false;\n"
            "            int instCount = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(hdr, 4));\n"
            "            if (instCount <= 0 || instCount > 200000)\n"
            "                return false;\n"
            "            int fltCount = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(hdr + 28, 4));\n"
            "            return fltCount == 16 + instCount * 28;\n"
            "        }",
        ),
    ])

    _replace_in_file("AnimeStudio.CLI/Studio.cs", [
        (
            "        public static bool SkipContainer = false;\n"
            "        public static AssetsManager assetsManager = new AssetsManager() { ResolveDependencies = false };",
            "        public static bool SkipContainer = false;\n"
            "        public static bool IsSectorStage = false;\n"
            "        public static string PrimarySourceName = null;\n"
            "        public static HashSet<string> PrimarySourceNames = null;\n"
            "        public static HashSet<string> ContainerHashAllowList = null;\n"
            "        public static HashSet<string> CabAllowList = null;\n"
            "        public static AssetsManager assetsManager = new AssetsManager() { ResolveDependencies = false };\n\n"
            + _OZ_DUMP_CS,
        ),
        (
            "            var matches = exportableAssets.Where(x =>\n            {\n                var isMatchRegex = nameFilters.IsNullOrEmpty() || nameFilters.Any(y => y.IsMatch(x.Text));\n                var isFilteredType = typeFilters.IsNullOrEmpty() || typeFilters.Contains(x.Type);\n                var isContainerMatch = containerFilters.IsNullOrEmpty() || containerFilters.Any(y => y.IsMatch(x.Container));\n                return isMatchRegex && isFilteredType && isContainerMatch;\n            }).ToArray();\n            exportableAssets.Clear();\n            exportableAssets.AddRange(matches);",
            "            var skipProxies = Environment.GetEnvironmentVariable(\"AS_SKIP_PROXIES\") == \"1\";\n"
            "            var candidateList = exportableAssets.Where(x =>\n"
            "            {\n"
            "                var srcName = Path.GetFileName(x.SourceFile?.originalPath ?? x.SourceFile?.fullName ?? \"\");\n"
            "                if (PrimarySourceNames != null && PrimarySourceNames.Count > 0)\n"
            "                {\n"
            "                    if (!PrimarySourceNames.Contains(srcName))\n"
            "                        return false;\n"
            "                }\n"
            "                else if (!string.IsNullOrEmpty(PrimarySourceName) && !string.Equals(srcName, PrimarySourceName, StringComparison.OrdinalIgnoreCase))\n"
            "                {\n"
            "                    return false;\n"
            "                }\n"
            "                if (skipProxies)\n"
            "                {\n"
            "                    if (!string.IsNullOrEmpty(x.Text) && IsProxyObjectName(x.Text))\n"
            "                        return false;\n"
            "                    if (x.Asset is GameObject go && go.m_Transform != null)\n"
            "                    {\n"
            "                        var cur = go.m_Transform;\n"
            "                        int steps = 0;\n"
            "                        while (cur != null && steps++ < 64)\n"
            "                        {\n"
            "                            if (cur.m_GameObject.TryGet(out var cgo) && !string.IsNullOrEmpty(cgo.m_Name) && IsProxyObjectName(cgo.m_Name))\n"
            "                                return false;\n"
            "                            if (!cur.m_Father.TryGet(out var f))\n"
            "                                break;\n"
            "                            cur = f;\n"
            "                        }\n"
            "                    }\n"
            "                }\n"
            "                var isMatchRegex = nameFilters.IsNullOrEmpty() || nameFilters.Any(y => y.IsMatch(x.Text));\n"
            "                var isFilteredType = typeFilters.IsNullOrEmpty() || typeFilters.Contains(x.Type);\n"
            "                var isContainerMatch = containerFilters.IsNullOrEmpty() || containerFilters.Any(y => y.IsMatch(x.Container));\n"
            "                return isMatchRegex && isFilteredType && isContainerMatch;\n"
            "            }).ToList();\n"
            "\n"
            "            if (skipProxies || (ContainerHashAllowList != null && ContainerHashAllowList.Count > 0) || (CabAllowList != null && CabAllowList.Count > 0))\n"
            "            {\n"
            "                var hasHashAllow = ContainerHashAllowList != null && ContainerHashAllowList.Count > 0;\n"
            "                var hasCabAllow = CabAllowList != null && CabAllowList.Count > 0;\n"
            "                if (hasHashAllow || hasCabAllow)\n"
            "                {\n"
            "                    candidateList.RemoveAll(x =>\n"
            "                    {\n"
            "                        bool hashOk = hasHashAllow && !string.IsNullOrEmpty(x.Container) && ContainerHashAllowList.Contains(x.Container);\n"
            "                        bool cabOk = hasCabAllow && x.SourceFile != null && CabAllowList.Contains(x.SourceFile.fileName ?? \"\");\n"
            "                        return !hashOk && !cabOk;\n"
            "                    });\n"
            "                }\n\n"
            "                if (skipProxies && !hasHashAllow && !hasCabAllow)\n"
            "                {\n"
            "                    if (!IsSectorStage && candidateList.Any(x => x.Asset is GameObject go && IsSectorRootName(go.m_Name)))\n"
            "                        IsSectorStage = true;\n\n"
            "                    if (IsSectorStage)\n"
            "                    {\n"
            "                        candidateList.RemoveAll(x =>\n"
            "                        {\n"
            "                            if (x.Asset is not GameObject go || go.m_Transform == null)\n"
            "                                return false;\n"
            "                            var rootT = go.m_Transform;\n"
            "                            int steps = 0;\n"
            "                            while (steps++ < 64 && rootT.m_Father.TryGet(out var f))\n"
            "                                rootT = f;\n"
            "                            if (!rootT.m_Father.IsNull)\n"
            "                                return false;\n"
            "                            return !rootT.m_GameObject.TryGet(out var rootGO) || !IsSectorRootName(rootGO.m_Name);\n"
            "                        });\n"
            "                    }\n"
            "                }\n\n"
            "                var exportableGOs = new HashSet<GameObject>(candidateList.Where(x => x.Asset is GameObject).Select(x => (GameObject)x.Asset));\n"
            "                candidateList.RemoveAll(x =>\n"
            "                {\n"
            "                    if (x.Asset is not GameObject go || go.m_Transform == null)\n"
            "                        return false;\n"
            "                    var childT = go.m_Transform;\n"
            "                    int steps = 0;\n"
            "                    while (steps++ < 64 && childT.m_Father.TryGet(out var fatherT))\n"
            "                    {\n"
            "                        if (fatherT.m_Children == null || !fatherT.m_Children.Any(c => c.TryGet(out var cT) && cT == childT))\n"
            "                            break;\n"
            "                        if (fatherT.m_GameObject.TryGet(out var fatherGO) && exportableGOs.Contains(fatherGO))\n"
            "                            return true;\n"
            "                        childT = fatherT;\n"
            "                    }\n"
            "                    return false;\n"
            "                });\n"
            "            }\n\n"
            "            exportableAssets.Clear();\n"
            "            exportableAssets.AddRange(candidateList);\n"
            "        }\n\n"
            "        public static bool IsProxyObjectName(string text)\n"
            "        {\n"
            "            if (string.IsNullOrEmpty(text))\n"
            "                return false;\n"
            "            var low = text.ToLowerInvariant();\n"
            "            return low.EndsWith(\"_proxy\") || low.Contains(\"_proxy (\") || low.Contains(\"_proxy_\")\n"
            "                || low.EndsWith(\"_col\") || low.Contains(\"_col_\")\n"
            "                || low.EndsWith(\"_collider\") || low.Contains(\"_collider (\") || low.Contains(\"_collider_\")\n"
            "                || low.StartsWith(\"scenepropcollider_\")\n"
            "                || low.StartsWith(\"blockout_\")\n"
            "                || (low.StartsWith(\"stage_\") && (low.EndsWith(\"_ld\") || low.Contains(\"_ld (\")))\n"
            "                || low.StartsWith(\"volumetricfog\")\n"
            "                || low.StartsWith(\"sm_merged_bp_\") || low.StartsWith(\"napmesh,\");\n"
            "        }\n\n"
            "        public static bool IsSectorRootName(string name)\n"
            "        {\n"
            "            if (string.IsNullOrEmpty(name) || name.StartsWith(\"MC_\", StringComparison.OrdinalIgnoreCase))\n"
            "                return false;\n"
            "            int us = name.IndexOf('_');\n"
            "            if (us < 1 || us > 6 || us + 1 >= name.Length || !char.IsDigit(name[us + 1]))\n"
            "                return false;\n"
            "            for (int i = 0; i < us; i++)\n"
            "            {\n"
            "                char c = name[i];\n"
            "                if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (i > 0 && char.IsDigit(c))))\n"
            "                    return false;\n"
            "            }\n"
            "            return true;",
        ),
        (
            "        public static bool IsSectorRootName(string name)\n"
            "        {\n"
            "            if (string.IsNullOrEmpty(name))\n"
            "                return false;\n"
            "            string[] prefixes = { \"B_\", \"G_\", \"S_\", \"E_\", \"D_\", \"M_\", \"OS_\", \"BRG_\", \"SSP_\", \"SP_\" };\n"
            "            foreach (var p in prefixes)\n"
            "            {\n"
            "                if (name.StartsWith(p, StringComparison.OrdinalIgnoreCase) && name.Length > p.Length && char.IsDigit(name[p.Length]))\n"
            "                    return true;\n"
            "            }\n"
            "            return false;\n"
            "        }",
            "        public static bool IsSectorRootName(string name)\n"
            "        {\n"
            "            if (string.IsNullOrEmpty(name) || name.StartsWith(\"MC_\", StringComparison.OrdinalIgnoreCase))\n"
            "                return false;\n"
            "            int us = name.IndexOf('_');\n"
            "            if (us < 1 || us > 6 || us + 1 >= name.Length || !char.IsDigit(name[us + 1]))\n"
            "                return false;\n"
            "            for (int i = 0; i < us; i++)\n"
            "            {\n"
            "                char c = name[i];\n"
            "                if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (i > 0 && char.IsDigit(c))))\n"
            "                    return false;\n"
            "            }\n"
            "            return true;\n"
            "        }",
        ),
        (
            "                        else\n"
            "                        {\n"
            "                            container = null;\n"
            "                        }\n\n"
            "                        var preloadIndex = m_Container.Value.preloadIndex;\n"
            "                        var preloadSize = m_Container.Value.preloadSize;\n"
            "                        var preloadEnd = preloadIndex + preloadSize;\n"
            "                        for (int k = preloadIndex; k < preloadEnd; k++)\n"
            "                        {\n"
            "                            containers.Add((m_AssetBundle.m_PreloadTable[k], container));\n"
            "                        }",
            "                        else\n"
            "                        {\n"
            "                            container = m_Container.Key;\n"
            "                        }\n\n"
            "                        var preloadIndex = m_Container.Value.preloadIndex;\n"
            "                        var preloadSize = m_Container.Value.preloadSize;\n"
            "                        var preloadEnd = preloadIndex + preloadSize;\n"
            "                        int safeStart = global::System.Math.Max(0, preloadIndex);\n"
            "                        int safeEnd = global::System.Math.Min(preloadEnd, m_AssetBundle.m_PreloadTable.Count);\n"
            "                        for (int k = safeStart; k < safeEnd; k++)\n"
            "                        {\n"
            "                            containers.Add((m_AssetBundle.m_PreloadTable[k], container));\n"
            "                        }\n"
            "                        if (m_Container.Value.asset != null && !m_Container.Value.asset.IsNull)\n"
            "                        {\n"
            "                            containers.Add((m_Container.Value.asset, container));\n"
            "                        }",
        ),
        (
            "                case Animator _ when ClassIDType.Animator.CanExport():\n"
            "                    exportable = true;\n"
            "                    break;",
            "                case Animator _ when ClassIDType.Animator.CanExport():\n"
            "                    exportable = true;\n"
            "                    break;\n"
            "                case NapAssetBundleIndexAsset _ when ClassIDType.NapAssetBundleIndexAsset.CanExport():\n"
            "                    exportable = true;\n"
            "                    break;",
        ),
    ])

    _brg_helpers = (
        "\n\n        private static Vector3 RotateVectorByQuat(Quaternion q, Vector3 v)\n"
        "        {\n"
        "            if (q.X == 0f && q.Y == 0f && q.Z == 0f && q.W == 0f)\n"
        "                return v;\n"
        "            float tx = 2f * (q.Y * v.Z - q.Z * v.Y);\n"
        "            float ty = 2f * (q.Z * v.X - q.X * v.Z);\n"
        "            float tz = 2f * (q.X * v.Y - q.Y * v.X);\n"
        "            return new Vector3(\n"
        "                v.X + q.W * tx + (q.Y * tz - q.Z * ty),\n"
        "                v.Y + q.W * ty + (q.Z * tx - q.X * tz),\n"
        "                v.Z + q.W * tz + (q.X * ty - q.Y * tx));\n"
        "        }\n\n"
        "        private static Vector3 ComputeWorldPosition(Transform t)\n"
        "        {\n"
        "            var pos = t.m_LocalPosition;\n"
        "            var cur = t;\n"
        "            int steps = 0;\n"
        "            while (steps++ < 64 && cur.m_Father.TryGet(out var parent))\n"
        "            {\n"
        "                pos = new Vector3(pos.X * parent.m_LocalScale.X, pos.Y * parent.m_LocalScale.Y, pos.Z * parent.m_LocalScale.Z);\n"
        "                pos = RotateVectorByQuat(parent.m_LocalRotation, pos) + parent.m_LocalPosition;\n"
        "                cur = parent;\n"
        "            }\n"
        "            return pos;\n"
        "        }\n\n"
        "        private static bool TryValidateBrgBatches(byte[] data, int batchStart, int instCount, int tailBytes)\n"
        "        {\n"
        "            if (batchStart < 0 || batchStart + 4 > data.Length)\n"
        "                return false;\n"
        "            int batchCount = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(batchStart, 4));\n"
        "            if (batchCount <= 0 || batchCount > 100000)\n"
        "                return false;\n"
        "            long pos = (long)batchStart + 4L;\n"
        "            for (int b = 0; b < batchCount; b++)\n"
        "            {\n"
        "                if (pos + 40L > data.Length)\n"
        "                    return false;\n"
        "                int idxCount = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan((int)pos + 36, 4));\n"
        "                if (idxCount < 0 || idxCount > instCount)\n"
        "                    return false;\n"
        "                long nextPos = pos + 40L + (long)idxCount * 4L + tailBytes;\n"
        "                if (nextPos > data.Length)\n"
        "                    return false;\n"
        "                pos = nextPos;\n"
        "            }\n"
        "            return pos == data.Length;\n"
        "        }\n\n"
        "        private static readonly string[] KnownForeignStagePrefixes =\n"
        "        {\n"
        "            \"event_\", \"datacenter_\", \"abandonedmine_\", \"whitfield_\", \"suburbs_\",\n"
        "            \"maincity_\", \"port_\", \"outerring_\", \"volcanicmine_\", \"wilderness_\",\n"
        "            \"belltowersquare_\", \"hiddenreef_\", \"forbiddenarea_\", \"constructionsite_\",\n"
        "            \"underground_\", \"tops_\"\n"
        "        };\n\n"
        "        private void FlushBrgChunk(\n"
        "            Transform m_Transform,\n"
        "            List<ImportedVertex> chunkVerts,\n"
        "            Dictionary<string, ImportedSubmesh> submeshesByMat,\n"
        "            ref int chunkIdx)\n"
        "        {\n"
        "            if (chunkVerts.Count == 0 || submeshesByMat.Count == 0)\n"
        "            {\n"
        "                chunkVerts.Clear();\n"
        "                submeshesByMat.Clear();\n"
        "                return;\n"
        "            }\n\n"
        "            var goName = m_Transform.m_GameObject.TryGet(out var gObj) && !string.IsNullOrEmpty(gObj.m_Name) ? gObj.m_Name : \"BRG\";\n"
        "            string frameName;\n"
        "            do\n"
        "            {\n"
        "                frameName = $\"{goName}_BRG_{chunkIdx++}\";\n"
        "            } while (RootFrame.FindChild(frameName, false) != null);\n\n"
        "            var chunkFrame = CreateFrame(frameName, Vector3.Zero, new Quaternion(0, 0, 0, 1), Vector3.One);\n"
        "            RootFrame.AddChild(chunkFrame);\n\n"
        "            var iMesh = new ImportedMesh\n"
        "            {\n"
        "                Path = chunkFrame.Path,\n"
        "                VertexList = new List<ImportedVertex>(chunkVerts),\n"
        "                SubmeshList = submeshesByMat.Values.Where(sm => sm.FaceList.Count > 0).ToList(),\n"
        "                hasNormal = true,\n"
        "                hasUV = new bool[8],\n"
        "                uvType = new int[8],\n"
        "                hasTangent = false,\n"
        "                hasColor = false\n"
        "            };\n"
        "            iMesh.hasUV[0] = true;\n"
        "            if (options.uvs != null && options.uvs.TryGetValue(\"UV0\", out var uvCfg))\n"
        "                iMesh.uvType[0] = uvCfg.Item2;\n\n"
        "            if (iMesh.SubmeshList.Count > 0)\n"
        "                MeshList.Add(iMesh);\n\n"
        "            chunkVerts.Clear();\n"
        "            submeshesByMat.Clear();\n"
        "        }\n\n"
        "        private void ConvertNapStreamingBRG(Transform m_Transform, MonoBehaviour mb)\n"
        "        {\n"
        "            bool skipProxies = global::System.Environment.GetEnvironmentVariable(\"AS_SKIP_PROXIES\") == \"1\";\n"
        "            bool manifestExact = !string.IsNullOrEmpty(global::System.Environment.GetEnvironmentVariable(\"AS_CONTAINER_HASH_FILE\"));\n"
        "            var data = mb.GetRawData();\n"
        "            if (data == null || data.Length < 68)\n"
        "                return;\n\n"
        "            int nameLen = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(28, 4));\n"
        "            if (nameLen < 0 || nameLen > 256)\n"
        "                return;\n"
        "            int hdr = 32 + ((nameLen + 3) & ~3);\n"
        "            if (hdr + 36 > data.Length)\n"
        "                return;\n\n"
        "            int instCount = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(hdr, 4));\n"
        "            if (instCount <= 0 || instCount > 200000)\n"
        "                return;\n"
        "            int fltCount = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(hdr + 28, 4));\n"
        "            if (fltCount != 16 + instCount * 28)\n"
        "                return;\n\n"
        "            long batchStartLong = (long)hdr + 32L + (long)fltCount * 4L;\n"
        "            if (batchStartLong < 0 || batchStartLong + 4L > data.Length)\n"
        "                return;\n"
        "            int batchStart = (int)batchStartLong;\n"
        "            int o2wBase = hdr + 32 + 64;\n\n"
        "            int tailBytes;\n"
        "            if (TryValidateBrgBatches(data, batchStart, instCount, 16))\n"
        "            {\n"
        "                tailBytes = 16;\n"
        "                HasSeenTail16Brg = true;\n"
        "            }\n"
        "            else if (TryValidateBrgBatches(data, batchStart, instCount, 12))\n"
        "            {\n"
        "                if (skipProxies && !manifestExact && HasSeenTail16Brg)\n"
        "                    return;\n"
        "                tailBytes = 12;\n"
        "            }\n"
        "            else\n"
        "            {\n"
        "                return;\n"
        "            }\n\n"
        "            int batchCount = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(batchStart, 4));\n"
        "            int pos = batchStart + 4;\n"
        "            var validBatches = new List<(Mesh mesh, Material mat, int subIdx, int[] indices)>(batchCount);\n\n"
        "            for (int b = 0; b < batchCount; b++)\n"
        "            {\n"
        "                int meshFid = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(pos, 4));\n"
        "                long meshPid = BinaryPrimitives.ReadInt64LittleEndian(data.AsSpan(pos + 4, 8));\n"
        "                int matFid = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(pos + 12, 4));\n"
        "                long matPid = BinaryPrimitives.ReadInt64LittleEndian(data.AsSpan(pos + 16, 8));\n"
        "                int subIdx = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(pos + 28, 4));\n"
        "                int idxCount = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(pos + 36, 4));\n\n"
        "                var indices = new int[idxCount];\n"
        "                int idxOff = pos + 40;\n"
        "                for (int i = 0; i < idxCount; i++)\n"
        "                {\n"
        "                    indices[i] = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(idxOff + i * 4, 4));\n"
        "                }\n"
        "                pos += 40 + idxCount * 4 + tailBytes;\n\n"
        "                if (idxCount == 0)\n"
        "                    continue;\n"
        "                new PPtr<Material>(matFid, matPid, mb.assetsFile).TryGet(out var mat);\n"
        "                if (skipProxies && IsProxyOrGreyboxMaterial(mat))\n"
        "                    continue;\n\n"
        "                if (!new PPtr<Mesh>(meshFid, meshPid, mb.assetsFile).TryGet(out var mesh) || mesh == null)\n"
        "                    continue;\n"
        "                if (mesh.m_VertexCount <= 0 || mesh.m_Vertices == null || mesh.m_Vertices.Length == 0\n"
        "                    || mesh.m_Indices == null || mesh.m_Indices.Count == 0\n"
        "                    || subIdx < 0 || subIdx >= mesh.m_SubMeshes.Count)\n"
        "                    continue;\n"
        "                if (skipProxies && !string.IsNullOrEmpty(mesh.m_Name) && IsProxyObjectName(mesh.m_Name))\n"
        "                    continue;\n\n"
        "                validBatches.Add((mesh, mat, subIdx, indices));\n"
        "            }\n\n"
        "            if (validBatches.Count == 0)\n"
        "                return;\n\n"
        "            var stageTokensEnv = global::System.Environment.GetEnvironmentVariable(\"AS_STAGE_TOKENS\") ?? \"\";\n"
        "            var stageTokens = stageTokensEnv\n"
        "                .Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)\n"
        "                .Select(t => t.ToLowerInvariant())\n"
        "                .ToArray();\n\n"
        "            if (stageTokens.Length > 0)\n"
        "            {\n"
        "                long stageDraws = 0;\n"
        "                long foreignDraws = 0;\n"
        "                long totalDraws = 0;\n"
        "                foreach (var vb in validBatches)\n"
        "                {\n"
        "                    string mLow = (vb.mesh.m_Name ?? \"\").ToLowerInvariant();\n"
        "                    string matLow = (vb.mat?.m_Name ?? \"\").ToLowerInvariant();\n"
        "                    bool isStage = stageTokens.Any(tok => mLow.Contains(tok) || matLow.Contains(tok));\n"
        "                    totalDraws += vb.indices.Length;\n"
        "                    if (isStage)\n"
        "                    {\n"
        "                        stageDraws += vb.indices.Length;\n"
        "                    }\n"
        "                    else\n"
        "                    {\n"
        "                        foreach (var fp in KnownForeignStagePrefixes)\n"
        "                        {\n"
        "                            if (mLow.StartsWith(fp, StringComparison.Ordinal))\n"
        "                            {\n"
        "                                string stem = fp.TrimEnd('_');\n"
        "                                if (!stageTokens.Contains(stem))\n"
        "                                {\n"
        "                                    foreignDraws += vb.indices.Length;\n"
        "                                }\n"
        "                                break;\n"
        "                            }\n"
        "                        }\n"
        "                    }\n"
        "                }\n\n"
        "                if (stageDraws > 0)\n"
        "                {\n"
        "                    HasSeenStageBrgToken = true;\n"
        "                }\n"
        "                else if (skipProxies && !manifestExact && totalDraws > 0 && (HasSeenStageBrgToken || foreignDraws * 5 >= totalDraws * 4))\n"
        "                {\n"
        "                    return;\n"
        "                }\n"
        "            }\n\n"
        "            var chunkVerts = new List<ImportedVertex>(4096);\n"
        "            var submeshesByMat = new Dictionary<string, ImportedSubmesh>(StringComparer.Ordinal);\n"
        "            int chunkIdx = 0;\n\n"
        "            foreach (var vb in validBatches)\n"
        "            {\n"
        "                var bMesh = vb.mesh;\n"
        "                var bMat = vb.mat;\n"
        "                int bSubIdx = vb.subIdx;\n"
        "                var bIndices = vb.indices;\n\n"
        "                int firstIndex = 0;\n"
        "                for (int s = 0; s < bSubIdx; s++)\n"
        "                    firstIndex += (int)bMesh.m_SubMeshes[s].indexCount;\n"
        "                int indexCount = ((int)bMesh.m_SubMeshes[bSubIdx].indexCount / 3) * 3;\n"
        "                if (indexCount <= 0 || firstIndex < 0 || (long)firstIndex + indexCount > bMesh.m_Indices.Count)\n"
        "                    continue;\n\n"
        "                var subMesh = bMesh.m_SubMeshes[bSubIdx];\n"
        "                uint bvOffset = 0u;\n"
        "                if (subMesh.baseVertex > 0 && subMesh.vertexCount > 0)\n"
        "                {\n"
        "                    uint minRaw = uint.MaxValue, maxRaw = 0;\n"
        "                    for (int t = 0; t < indexCount; t++)\n"
        "                    {\n"
        "                        uint r = bMesh.m_Indices[firstIndex + t];\n"
        "                        if (r < minRaw) minRaw = r;\n"
        "                        if (r > maxRaw) maxRaw = r;\n"
        "                    }\n"
        "                    if (minRaw < subMesh.firstVertex && maxRaw < subMesh.vertexCount && (long)maxRaw + subMesh.baseVertex < bMesh.m_VertexCount)\n"
        "                        bvOffset = subMesh.baseVertex;\n"
        "                }\n\n"
        "                int vStride = (bMesh.m_Vertices.Length == bMesh.m_VertexCount * 4) ? 4 : 3;\n"
        "                bool hasNorm = bMesh.m_Normals != null && bMesh.m_Normals.Length >= bMesh.m_VertexCount * 3;\n"
        "                int nStride = hasNorm ? ((bMesh.m_Normals.Length == bMesh.m_VertexCount * 4) ? 4 : 3) : 0;\n"
        "                var uv0 = bMesh.GetUV(0);\n"
        "                bool hasUv0 = uv0 != null && uv0.Length >= bMesh.m_VertexCount * 2;\n"
        "                int uStride = hasUv0 ? (int)(uv0.Length / bMesh.m_VertexCount) : 0;\n\n"
        "                var vertRemap = new Dictionary<uint, int>();\n"
        "                var localVerts = new List<(float vx, float vy, float vz, float nx, float ny, float nz, float u, float v)>();\n"
        "                var localTris = new int[indexCount];\n"
        "                bool badMeshData = false;\n\n"
        "                for (int t = 0; t < indexCount; t++)\n"
        "                {\n"
        "                    uint rawV = bMesh.m_Indices[firstIndex + t] + bvOffset;\n"
        "                    if (rawV >= (uint)bMesh.m_VertexCount)\n"
        "                    {\n"
        "                        badMeshData = true;\n"
        "                        break;\n"
        "                    }\n"
        "                    if (!vertRemap.TryGetValue(rawV, out int lvIdx))\n"
        "                    {\n"
        "                        lvIdx = localVerts.Count;\n"
        "                        vertRemap[rawV] = lvIdx;\n"
        "                        int vi = (int)rawV * vStride;\n"
        "                        float vx = bMesh.m_Vertices[vi], vy = bMesh.m_Vertices[vi + 1], vz = bMesh.m_Vertices[vi + 2];\n"
        "                        float nx = 0f, ny = 1f, nz = 0f;\n"
        "                        if (hasNorm)\n"
        "                        {\n"
        "                            int ni = (int)rawV * nStride;\n"
        "                            nx = bMesh.m_Normals[ni];\n"
        "                            ny = bMesh.m_Normals[ni + 1];\n"
        "                            nz = bMesh.m_Normals[ni + 2];\n"
        "                        }\n"
        "                        float u = 0f, v = 0f;\n"
        "                        if (hasUv0)\n"
        "                        {\n"
        "                            int ui = (int)rawV * uStride;\n"
        "                            u = uv0[ui];\n"
        "                            v = uv0[ui + 1];\n"
        "                        }\n"
        "                        localVerts.Add((vx, vy, vz, nx, ny, nz, u, v));\n"
        "                    }\n"
        "                    localTris[t] = lvIdx;\n"
        "                }\n"
        "                if (badMeshData || localVerts.Count == 0)\n"
        "                    continue;\n\n"
        "                ImportedMaterial iMat = null;\n"
        "                foreach (int k in bIndices)\n"
        "                {\n"
        "                    if (k < 0 || k >= instCount)\n"
        "                        continue;\n"
        "                    int off = o2wBase + k * 48;\n"
        "                    if (off + 48 > batchStart)\n"
        "                        continue;\n\n"
        "                    float f0 = BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(off, 4));\n"
        "                    float f1 = BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(off + 4, 4));\n"
        "                    float f2 = BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(off + 8, 4));\n"
        "                    float f3 = BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(off + 12, 4));\n"
        "                    float f4 = BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(off + 16, 4));\n"
        "                    float f5 = BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(off + 20, 4));\n"
        "                    float f6 = BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(off + 24, 4));\n"
        "                    float f7 = BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(off + 28, 4));\n"
        "                    float f8 = BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(off + 32, 4));\n"
        "                    float f9 = BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(off + 36, 4));\n"
        "                    float f10 = BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(off + 40, 4));\n"
        "                    float f11 = BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(off + 44, 4));\n\n"
        "                    float det = f0 * (f4 * f8 - f5 * f7) - f3 * (f1 * f8 - f2 * f7) + f6 * (f1 * f5 - f2 * f4);\n"
        "                    if (!float.IsFinite(f9) || !float.IsFinite(f10) || !float.IsFinite(f11) || !float.IsFinite(det) || Math.Abs(det) < 1e-9f)\n"
        "                        continue;\n\n"
        "                    int rotHash = ((int)Math.Round(f0 * 100f) << 16) ^ ((int)Math.Round(f6 * 100f) & 0xFFFF);\n"
        "                    var brgKey = (bMesh.m_PathID, bSubIdx, (int)Math.Round(f9 * 10f), (int)Math.Round(f10 * 10f), (int)Math.Round(f11 * 10f), rotHash);\n"
        "                    if (skipProxies && !manifestExact && !ExportedBrgDraws.Add(brgKey))\n"
        "                        continue;\n\n"
        "                    if (chunkVerts.Count + localVerts.Count > 60000 && chunkVerts.Count > 0)\n"
        "                        FlushBrgChunk(m_Transform, chunkVerts, submeshesByMat, ref chunkIdx);\n\n"
        "                    iMat ??= ConvertMaterial(bMat);\n"
        "                    string matKey = iMat.Name ?? \"__null__\";\n"
        "                    if (!submeshesByMat.TryGetValue(matKey, out var submeshOut))\n"
        "                    {\n"
        "                        submeshOut = new ImportedSubmesh\n"
        "                        {\n"
        "                            Material = iMat.Name,\n"
        "                            BaseVertex = 0,\n"
        "                            FaceList = new List<ImportedFace>()\n"
        "                        };\n"
        "                        submeshesByMat[matKey] = submeshOut;\n"
        "                    }\n\n"
        "                    float sign = det < 0f ? -1f : 1f;\n"
        "                    float c00 = (f4 * f8 - f5 * f7) * sign;\n"
        "                    float c10 = (f5 * f6 - f3 * f8) * sign;\n"
        "                    float c20 = (f3 * f7 - f4 * f6) * sign;\n"
        "                    float c01 = (f7 * f2 - f8 * f1) * sign;\n"
        "                    float c11 = (f8 * f0 - f6 * f2) * sign;\n"
        "                    float c21 = (f6 * f1 - f7 * f0) * sign;\n"
        "                    float c02 = (f1 * f5 - f2 * f4) * sign;\n"
        "                    float c12 = (f2 * f3 - f0 * f5) * sign;\n"
        "                    float c22 = (f0 * f4 - f1 * f3) * sign;\n\n"
        "                    int baseV = chunkVerts.Count;\n"
        "                    for (int vIdx = 0; vIdx < localVerts.Count; vIdx++)\n"
        "                    {\n"
        "                        var lv = localVerts[vIdx];\n"
        "                        float wx = f0 * lv.vx + f3 * lv.vy + f6 * lv.vz + f9;\n"
        "                        float wy = f1 * lv.vx + f4 * lv.vy + f7 * lv.vz + f10;\n"
        "                        float wz = f2 * lv.vx + f5 * lv.vy + f8 * lv.vz + f11;\n\n"
        "                        float nwx = c00 * lv.nx + c01 * lv.ny + c02 * lv.nz;\n"
        "                        float nwy = c10 * lv.nx + c11 * lv.ny + c12 * lv.nz;\n"
        "                        float nwz = c20 * lv.nx + c21 * lv.ny + c22 * lv.nz;\n"
        "                        float nLenSq = nwx * nwx + nwy * nwy + nwz * nwz;\n"
        "                        if (nLenSq > 1e-12f && float.IsFinite(nLenSq))\n"
        "                        {\n"
        "                            float invLen = 1f / (float)Math.Sqrt(nLenSq);\n"
        "                            nwx *= invLen; nwy *= invLen; nwz *= invLen;\n"
        "                        }\n"
        "                        else\n"
        "                        {\n"
        "                            nwx = 0f; nwy = 1f; nwz = 0f;\n"
        "                        }\n\n"
        "                        var iv = new ImportedVertex\n"
        "                        {\n"
        "                            Vertex = new Vector3(-wx, wy, wz),\n"
        "                            Normal = new Vector3(-nwx, nwy, nwz),\n"
        "                            UV = new float[8][]\n"
        "                        };\n"
        "                        iv.UV[0] = new[] { lv.u, lv.v };\n"
        "                        chunkVerts.Add(iv);\n"
        "                    }\n\n"
        "                    bool flipWinding = det >= 0f;\n"
        "                    for (int t = 0; t < localTris.Length; t += 3)\n"
        "                    {\n"
        "                        int ia = baseV + localTris[t];\n"
        "                        int ib = baseV + localTris[t + 1];\n"
        "                        int ic = baseV + localTris[t + 2];\n"
        "                        submeshOut.FaceList.Add(new ImportedFace\n"
        "                        {\n"
        "                            VertexIndices = flipWinding ? new[] { ic, ib, ia } : new[] { ia, ib, ic }\n"
        "                        });\n"
        "                    }\n"
        "                }\n"
        "            }\n\n"
        "            FlushBrgChunk(m_Transform, chunkVerts, submeshesByMat, ref chunkIdx);\n"
        "        }"
    )

    _replace_in_file("AnimeStudio.Utility/ModelConverter.cs", [
        (
            "using Newtonsoft.Json;\n"
            "using System;\n"
            "using System.Collections.Generic;\n"
            "using System.Linq;\n"
            "using System.Text;",
            "using Newtonsoft.Json;\n"
            "using System;\n"
            "using System.Buffers.Binary;\n"
            "using System.Collections.Generic;\n"
            "using System.Linq;\n"
            "using System.Text;",
        ),
        (
            "        private Dictionary<Texture2D, string> textureNameDictionary = new Dictionary<Texture2D, string>();\n"
            "        private Dictionary<Transform, ImportedFrame> transformDictionary = new Dictionary<Transform, ImportedFrame>();\n"
            "        Dictionary<uint, string> morphChannelNames = new Dictionary<uint, string>();",
            "        private Dictionary<Texture2D, string> textureNameDictionary = new Dictionary<Texture2D, string>();\n"
            "        private Dictionary<Transform, ImportedFrame> transformDictionary = new Dictionary<Transform, ImportedFrame>();\n"
            "        private HashSet<Renderer> suppressedLodRenderers = new HashSet<Renderer>();\n"
            "        Dictionary<uint, string> morphChannelNames = new Dictionary<uint, string>();\n\n"
            "        public static readonly HashSet<(long, int, int, int, int, int)> ExportedBrgDraws = new HashSet<(long, int, int, int, int, int)>();\n"
            "        public static readonly HashSet<(long, int, int, int, int, int)> ExportedMeshRenderers = new HashSet<(long, int, int, int, int, int)>();\n"
            "        public static bool HasSeenStageBrgToken = false;\n"
            "        public static bool HasSeenTail16Brg = false;\n\n"
            "        /// <summary>Resets cross-blk deduplication and stage heuristics between batch stage exports.</summary>\n"
            "        public static void ResetStageDedup()\n"
            "        {\n"
            "            ExportedBrgDraws.Clear();\n"
            "            ExportedMeshRenderers.Clear();\n"
            "            HasSeenStageBrgToken = false;\n"
            "            HasSeenTail16Brg = false;\n"
            "        }",
        ),
        (
            "            foreach (var m_GameObject in m_GameObjects)\n"
            "            {\n"
            "                var m_Transform = m_GameObject.m_Transform;\n"
            "                ConvertMeshRenderer(m_Transform);\n"
            "            }",
            "            foreach (var m_GameObject in m_GameObjects)\n"
            "            {\n"
            "                CollectSuppressedLodRenderers(m_GameObject.m_Transform);\n"
            "            }\n"
            "            foreach (var m_GameObject in m_GameObjects)\n"
            "            {\n"
            "                var m_Transform = m_GameObject.m_Transform;\n"
            "                ConvertMeshRenderer(m_Transform);\n"
            "            }",
        ),
        (
            "                CreateBonePathHash(m_Transform);\n"
            "            }\n\n"
            "            ConvertMeshRenderer(m_Transform);\n"
            "        }",
            "                CreateBonePathHash(m_Transform);\n"
            "            }\n\n"
            "            CollectSuppressedLodRenderers(m_Transform);\n"
            "            ConvertMeshRenderer(m_Transform);\n"
            "        }",
        ),
        (
            "                //default values\n"
            "                iMat.Diffuse = new Color(0.8f, 0.8f, 0.8f, 1);\n"
            "                iMat.Ambient = new Color(0.2f, 0.2f, 0.2f, 1);\n"
            "                iMat.Emissive = new Color(0, 0, 0, 1);\n"
            "                iMat.Specular = new Color(0.2f, 0.2f, 0.2f, 1);\n"
            "                iMat.Reflection = new Color(0, 0, 0, 1);\n"
            "                iMat.Shininess = 20f;\n"
            "                iMat.Transparency = 0f;\n"
            "                foreach (var col in mat.m_SavedProperties.m_Colors)\n"
            "                {\n"
            "                    switch (col.Key)\n"
            "                    {\n"
            "                        case \"_Color\":\n"
            "                            iMat.Diffuse = col.Value;\n"
            "                            break;\n",
            "                //default values\n"
            "                iMat.Diffuse = new Color(1.0f, 1.0f, 1.0f, 1);\n"
            "                iMat.Ambient = new Color(0.2f, 0.2f, 0.2f, 1);\n"
            "                iMat.Emissive = new Color(0, 0, 0, 1);\n"
            "                iMat.Specular = new Color(0.2f, 0.2f, 0.2f, 1);\n"
            "                iMat.Reflection = new Color(0, 0, 0, 1);\n"
            "                iMat.Shininess = 20f;\n"
            "                iMat.Transparency = 0f;\n"
            "                bool diffuseOnly = global::System.Environment.GetEnvironmentVariable(\"AS_DIFFUSE_ONLY\") == \"1\";\n"
            "                bool hasEmissionTex = false;\n"
            "                foreach (var kv in mat.m_SavedProperties.m_TexEnvs)\n"
            "                {\n"
            "                    if ((kv.Key == \"_EmissionMap\" || kv.Key == \"_EmissiveTex\" || kv.Key == \"_EmissionTex\" || kv.Key == \"_SecondaryEmissionTex\")\n"
            "                        && kv.Value.m_Texture.TryGet<Texture2D>(out _))\n"
            "                    {\n"
            "                        hasEmissionTex = true;\n"
            "                        break;\n"
            "                    }\n"
            "                }\n"
            "                bool hasBaseColorProp = false;\n"
            "                bool hasShallowColor = false;\n"
            "                bool hasDeepColor = false;\n"
            "                Color shallowColor = default;\n"
            "                Color deepColor = default;\n"
            "                foreach (var col in mat.m_SavedProperties.m_Colors)\n"
            "                {\n"
            "                    switch (col.Key)\n"
            "                    {\n"
            "                        case \"_BaseColor\":\n"
            "                            iMat.Diffuse = col.Value;\n"
            "                            hasBaseColorProp = true;\n"
            "                            break;\n"
            "                        case \"_Color\":\n"
            "                            if (!hasBaseColorProp)\n"
            "                                iMat.Diffuse = col.Value;\n"
            "                            break;\n"
            "                        case \"_ShallowColor\":\n"
            "                            shallowColor = col.Value;\n"
            "                            hasShallowColor = true;\n"
            "                            break;\n"
            "                        case \"_DeepColor\":\n"
            "                            deepColor = col.Value;\n"
            "                            hasDeepColor = true;\n"
            "                            break;\n",
        ),
        (
            "                bool hasBaseColorProp = false;\n"
            "                foreach (var col in mat.m_SavedProperties.m_Colors)\n"
            "                {\n"
            "                    switch (col.Key)\n"
            "                    {\n"
            "                        case \"_BaseColor\":\n"
            "                            iMat.Diffuse = col.Value;\n"
            "                            hasBaseColorProp = true;\n"
            "                            break;\n"
            "                        case \"_Color\":\n"
            "                            if (!hasBaseColorProp)\n"
            "                                iMat.Diffuse = col.Value;\n"
            "                            break;",
            "                bool hasBaseColorProp = false;\n"
            "                bool hasShallowColor = false;\n"
            "                bool hasDeepColor = false;\n"
            "                Color shallowColor = default;\n"
            "                Color deepColor = default;\n"
            "                foreach (var col in mat.m_SavedProperties.m_Colors)\n"
            "                {\n"
            "                    switch (col.Key)\n"
            "                    {\n"
            "                        case \"_BaseColor\":\n"
            "                            iMat.Diffuse = col.Value;\n"
            "                            hasBaseColorProp = true;\n"
            "                            break;\n"
            "                        case \"_Color\":\n"
            "                            if (!hasBaseColorProp)\n"
            "                                iMat.Diffuse = col.Value;\n"
            "                            break;\n"
            "                        case \"_ShallowColor\":\n"
            "                            shallowColor = col.Value;\n"
            "                            hasShallowColor = true;\n"
            "                            break;\n"
            "                        case \"_DeepColor\":\n"
            "                            deepColor = col.Value;\n"
            "                            hasDeepColor = true;\n"
            "                            break;",
        ),
        (
            "                        case \"_EmissionColor\":\n"
            "                            iMat.Emissive = col.Value;\n"
            "                            break;",
            "                        case \"_EmissionColor\":\n"
            "                            if (!diffuseOnly && hasEmissionTex)\n"
            "                                iMat.Emissive = col.Value;\n"
            "                            break;",
        ),
        (
            "                //textures\n"
            "                iMat.Textures = new List<ImportedMaterialTexture>();\n"
            "                foreach (var texEnv in mat.m_SavedProperties.m_TexEnvs)\n"
            "                {\n"
            "                    if (!texEnv.Value.m_Texture.TryGet<Texture2D>(out var m_Texture2D)) //TODO other Texture\n"
            "                    {\n"
            "                        continue;\n"
            "                    }\n"
            "\n"
            "                    var texture = new ImportedMaterialTexture();\n"
            "                    iMat.Textures.Add(texture);\n"
            "\n"
            "                    int dest = -1;\n"
            "                    if (options.texs.TryGetValue(texEnv.Key, out var target))\n"
            "                        dest = target;\n"
            "                    else if (texEnv.Key == \"_MainTex\")\n"
            "                        dest = 0;\n"
            "                    else if (texEnv.Key == \"_BumpMap\")\n"
            "                        dest = 3;\n"
            "                    else if (texEnv.Key.Contains(\"Specular\"))\n"
            "                        dest = 2;\n"
            "                    else if (texEnv.Key.Contains(\"Normal\"))\n"
            "                        dest = 1;\n"
            "\n"
            "                    texture.Dest = dest;",
            "                //textures\n"
            "                iMat.Textures = new List<ImportedMaterialTexture>();\n"
            "                bool hasColorIdMap = false;\n"
            "                foreach (var kv in mat.m_SavedProperties.m_TexEnvs)\n"
            "                {\n"
            "                    if (kv.Key == \"_ColorIDMap\" && kv.Value.m_Texture.TryGet<Texture2D>(out _))\n"
            "                    {\n"
            "                        hasColorIdMap = true;\n"
            "                        break;\n"
            "                    }\n"
            "                }\n"
            "                bool hasBaseMap = false;\n"
            "                bool hasStdDiffuse = false;\n"
            "                foreach (var kv in mat.m_SavedProperties.m_TexEnvs)\n"
            "                {\n"
            "                    if (!kv.Value.m_Texture.TryGet<Texture2D>(out var candTex))\n"
            "                        continue;\n"
            "                    bool badNonDiff = hasColorIdMap\n"
            "                        && (candTex.m_Name.EndsWith(\"_N\", global::System.StringComparison.OrdinalIgnoreCase)\n"
            "                            || candTex.m_Name.EndsWith(\"_M\", global::System.StringComparison.OrdinalIgnoreCase));\n"
            "                    if (kv.Key == \"_BaseMap\" || kv.Key == \"_BaseColorMap\")\n"
            "                    {\n"
            "                        if (!badNonDiff)\n"
            "                        {\n"
            "                            hasBaseMap = true;\n"
            "                            hasStdDiffuse = true;\n"
            "                        }\n"
            "                    }\n"
            "                    else if (kv.Key == \"_MainTex\" || kv.Key == \"_MainTex0\" || kv.Key == \"_Diffuse\" || kv.Key == \"_Albedo\")\n"
            "                    {\n"
            "                        if (!badNonDiff)\n"
            "                            hasStdDiffuse = true;\n"
            "                    }\n"
            "                }\n"
            "                if (!hasStdDiffuse && !hasColorIdMap)\n"
            "                {\n"
            "                    if (hasShallowColor && hasDeepColor)\n"
            "                    {\n"
            "                        iMat.Diffuse = new Color(\n"
            "                            shallowColor.R * 0.65f + deepColor.R * 0.35f,\n"
            "                            shallowColor.G * 0.65f + deepColor.G * 0.35f,\n"
            "                            shallowColor.B * 0.65f + deepColor.B * 0.35f,\n"
            "                            0.78f);\n"
            "                        iMat.Transparency = 0.22f;\n"
            "                    }\n"
            "                    else if (hasShallowColor)\n"
            "                    {\n"
            "                        iMat.Diffuse = new Color(shallowColor.R, shallowColor.G, shallowColor.B, 0.78f);\n"
            "                        iMat.Transparency = 0.22f;\n"
            "                    }\n"
            "                    else if (!string.IsNullOrEmpty(mat.m_Name) && mat.m_Name.IndexOf(\"_Water_\", global::System.StringComparison.OrdinalIgnoreCase) >= 0)\n"
            "                    {\n"
            "                        iMat.Diffuse = new Color(0.14f, 0.42f, 0.56f, 0.78f);\n"
            "                        iMat.Transparency = 0.22f;\n"
            "                    }\n"
            "                }\n"
            "                foreach (var texEnv in mat.m_SavedProperties.m_TexEnvs)\n"
            "                {\n"
            "                    if (!texEnv.Value.m_Texture.TryGet<Texture2D>(out var m_Texture2D)) //TODO other Texture\n"
            "                    {\n"
            "                        continue;\n"
            "                    }\n"
            "\n"
            "                    int dest = -1;\n"
            "                    bool isBadNonDiff = hasColorIdMap\n"
            "                        && (texEnv.Key == \"_BaseMap\" || texEnv.Key == \"_BaseColorMap\" || texEnv.Key == \"_MainTex\")\n"
            "                        && (m_Texture2D.m_Name.EndsWith(\"_N\", global::System.StringComparison.OrdinalIgnoreCase)\n"
            "                            || m_Texture2D.m_Name.EndsWith(\"_M\", global::System.StringComparison.OrdinalIgnoreCase));\n"
            "                    if (isBadNonDiff || (hasBaseMap && texEnv.Key == \"_MainTex\"))\n"
            "                        dest = -1;\n"
            "                    else if (options.texs.TryGetValue(texEnv.Key, out var target))\n"
            "                        dest = target;\n"
            "                    else if (texEnv.Key == \"_MainTex\")\n"
            "                        dest = 0;\n"
            "                    else if (!hasStdDiffuse && (texEnv.Key == \"_RoomTex\" || texEnv.Key == \"_BlindsTex\" || texEnv.Key == \"_ColorIDMap\" || (m_Texture2D.m_Name.EndsWith(\"_D\", global::System.StringComparison.OrdinalIgnoreCase) && !texEnv.Key.Contains(\"Detail\") && !texEnv.Key.Contains(\"Emission\") && !texEnv.Key.Contains(\"Normal\") && !texEnv.Key.Contains(\"Bump\"))))\n"
            "                        dest = 0;\n"
            "                    else if (texEnv.Key == \"_BumpMap\")\n"
            "                        dest = 3;\n"
            "                    else if (texEnv.Key.Contains(\"Specular\"))\n"
            "                        dest = 2;\n"
            "                    else if (texEnv.Key.Contains(\"Normal\"))\n"
            "                        dest = 1;\n"
            "\n"
            "                    if (diffuseOnly && dest != 0)\n"
            "                        continue;\n"
            "\n"
            "                    var texture = new ImportedMaterialTexture();\n"
            "                    iMat.Textures.Add(texture);\n"
            "                    texture.Dest = dest;",
        ),
        (
            "                //textures\n"
            "                iMat.Textures = new List<ImportedMaterialTexture>();\n"
            "                bool hasBaseMap = false;\n"
            "                bool hasStdDiffuse = false;\n"
            "                foreach (var kv in mat.m_SavedProperties.m_TexEnvs)\n"
            "                {\n"
            "                    if (!kv.Value.m_Texture.TryGet<Texture2D>(out _))\n"
            "                        continue;\n"
            "                    if (kv.Key == \"_BaseMap\" || kv.Key == \"_BaseColorMap\")\n"
            "                    {\n"
            "                        hasBaseMap = true;\n"
            "                        hasStdDiffuse = true;\n"
            "                    }\n"
            "                    else if (kv.Key == \"_MainTex\" || kv.Key == \"_MainTex0\" || kv.Key == \"_Diffuse\" || kv.Key == \"_Albedo\")\n"
            "                    {\n"
            "                        hasStdDiffuse = true;\n"
            "                    }\n"
            "                }\n"
            "                foreach (var texEnv in mat.m_SavedProperties.m_TexEnvs)\n"
            "                {\n"
            "                    if (!texEnv.Value.m_Texture.TryGet<Texture2D>(out var m_Texture2D)) //TODO other Texture\n"
            "                    {\n"
            "                        continue;\n"
            "                    }\n"
            "\n"
            "                    int dest = -1;\n"
            "                    if (hasBaseMap && texEnv.Key == \"_MainTex\")\n"
            "                        dest = -1;\n"
            "                    else if (options.texs.TryGetValue(texEnv.Key, out var target))\n"
            "                        dest = target;\n"
            "                    else if (texEnv.Key == \"_MainTex\")\n"
            "                        dest = 0;\n"
            "                    else if (!hasStdDiffuse && (texEnv.Key == \"_RoomTex\" || texEnv.Key == \"_BlindsTex\" || texEnv.Key == \"_ColorIDMap\" || (m_Texture2D.m_Name.EndsWith(\"_D\", global::System.StringComparison.OrdinalIgnoreCase) && !texEnv.Key.Contains(\"Detail\") && !texEnv.Key.Contains(\"Emission\") && !texEnv.Key.Contains(\"Normal\") && !texEnv.Key.Contains(\"Bump\"))))\n"
            "                        dest = 0;",
            "                //textures\n"
            "                iMat.Textures = new List<ImportedMaterialTexture>();\n"
            "                bool hasColorIdMap = false;\n"
            "                foreach (var kv in mat.m_SavedProperties.m_TexEnvs)\n"
            "                {\n"
            "                    if (kv.Key == \"_ColorIDMap\" && kv.Value.m_Texture.TryGet<Texture2D>(out _))\n"
            "                    {\n"
            "                        hasColorIdMap = true;\n"
            "                        break;\n"
            "                    }\n"
            "                }\n"
            "                bool hasBaseMap = false;\n"
            "                bool hasStdDiffuse = false;\n"
            "                foreach (var kv in mat.m_SavedProperties.m_TexEnvs)\n"
            "                {\n"
            "                    if (!kv.Value.m_Texture.TryGet<Texture2D>(out var candTex))\n"
            "                        continue;\n"
            "                    bool badNonDiff = hasColorIdMap\n"
            "                        && (candTex.m_Name.EndsWith(\"_N\", global::System.StringComparison.OrdinalIgnoreCase)\n"
            "                            || candTex.m_Name.EndsWith(\"_M\", global::System.StringComparison.OrdinalIgnoreCase));\n"
            "                    if (kv.Key == \"_BaseMap\" || kv.Key == \"_BaseColorMap\")\n"
            "                    {\n"
            "                        if (!badNonDiff)\n"
            "                        {\n"
            "                            hasBaseMap = true;\n"
            "                            hasStdDiffuse = true;\n"
            "                        }\n"
            "                    }\n"
            "                    else if (kv.Key == \"_MainTex\" || kv.Key == \"_MainTex0\" || kv.Key == \"_Diffuse\" || kv.Key == \"_Albedo\")\n"
            "                    {\n"
            "                        if (!badNonDiff)\n"
            "                            hasStdDiffuse = true;\n"
            "                    }\n"
            "                }\n"
            "                if (!hasStdDiffuse && !hasColorIdMap)\n"
            "                {\n"
            "                    if (hasShallowColor && hasDeepColor)\n"
            "                    {\n"
            "                        iMat.Diffuse = new Color(\n"
            "                            shallowColor.R * 0.65f + deepColor.R * 0.35f,\n"
            "                            shallowColor.G * 0.65f + deepColor.G * 0.35f,\n"
            "                            shallowColor.B * 0.65f + deepColor.B * 0.35f,\n"
            "                            0.78f);\n"
            "                        iMat.Transparency = 0.22f;\n"
            "                    }\n"
            "                    else if (hasShallowColor)\n"
            "                    {\n"
            "                        iMat.Diffuse = new Color(shallowColor.R, shallowColor.G, shallowColor.B, 0.78f);\n"
            "                        iMat.Transparency = 0.22f;\n"
            "                    }\n"
            "                    else if (!string.IsNullOrEmpty(mat.m_Name) && mat.m_Name.IndexOf(\"_Water_\", global::System.StringComparison.OrdinalIgnoreCase) >= 0)\n"
            "                    {\n"
            "                        iMat.Diffuse = new Color(0.14f, 0.42f, 0.56f, 0.78f);\n"
            "                        iMat.Transparency = 0.22f;\n"
            "                    }\n"
            "                }\n"
            "                foreach (var texEnv in mat.m_SavedProperties.m_TexEnvs)\n"
            "                {\n"
            "                    if (!texEnv.Value.m_Texture.TryGet<Texture2D>(out var m_Texture2D)) //TODO other Texture\n"
            "                    {\n"
            "                        continue;\n"
            "                    }\n"
            "\n"
            "                    int dest = -1;\n"
            "                    bool isBadNonDiff = hasColorIdMap\n"
            "                        && (texEnv.Key == \"_BaseMap\" || texEnv.Key == \"_BaseColorMap\" || texEnv.Key == \"_MainTex\")\n"
            "                        && (m_Texture2D.m_Name.EndsWith(\"_N\", global::System.StringComparison.OrdinalIgnoreCase)\n"
            "                            || m_Texture2D.m_Name.EndsWith(\"_M\", global::System.StringComparison.OrdinalIgnoreCase));\n"
            "                    if (isBadNonDiff || (hasBaseMap && texEnv.Key == \"_MainTex\"))\n"
            "                        dest = -1;\n"
            "                    else if (options.texs.TryGetValue(texEnv.Key, out var target))\n"
            "                        dest = target;\n"
            "                    else if (texEnv.Key == \"_MainTex\")\n"
            "                        dest = 0;\n"
            "                    else if (!hasStdDiffuse && (texEnv.Key == \"_RoomTex\" || texEnv.Key == \"_BlindsTex\" || texEnv.Key == \"_ColorIDMap\" || (m_Texture2D.m_Name.EndsWith(\"_D\", global::System.StringComparison.OrdinalIgnoreCase) && !texEnv.Key.Contains(\"Detail\") && !texEnv.Key.Contains(\"Emission\") && !texEnv.Key.Contains(\"Normal\") && !texEnv.Key.Contains(\"Bump\"))))\n"
            "                        dest = 0;",
        ),
        (
            "        private void ConvertMeshRenderer(Transform m_Transform)\n"
            "        {\n"
            "            m_Transform.m_GameObject.TryGet(out var m_GameObject);\n",
            "        private void CollectSuppressedLodRenderers(Transform rootTransform)\n"
            "        {\n"
            "            if (rootTransform == null)\n"
            "                return;\n"
            "            var keptLod0 = new HashSet<Renderer>();\n"
            "            var lowerLods = new HashSet<Renderer>();\n"
            "            var stack = new Stack<(Transform t, int depth)>();\n"
            "            stack.Push((rootTransform, 0));\n"
            "            while (stack.Count > 0)\n"
            "            {\n"
            "                var (cur, depth) = stack.Pop();\n"
            "                if (cur == null || depth > 128)\n"
            "                    continue;\n"
            "                if (cur.m_GameObject.TryGet(out var go) && go.m_LODGroup != null && go.m_LODGroup.m_LODRenderers != null)\n"
            "                {\n"
            "                    var lods = go.m_LODGroup.m_LODRenderers;\n"
            "                    int bestLod = -1;\n"
            "                    for (int li = 0; li < lods.Count; li++)\n"
            "                    {\n"
            "                        bool anyValid = false;\n"
            "                        foreach (var rp in lods[li])\n"
            "                        {\n"
            "                            if (rp != null && rp.TryGet(out var r) && r != null)\n"
            "                            {\n"
            "                                keptLod0.Add(r);\n"
            "                                anyValid = true;\n"
            "                            }\n"
            "                        }\n"
            "                        if (anyValid)\n"
            "                        {\n"
            "                            bestLod = li;\n"
            "                            break;\n"
            "                        }\n"
            "                    }\n"
            "                    if (bestLod >= 0)\n"
            "                    {\n"
            "                        for (int li = bestLod + 1; li < lods.Count; li++)\n"
            "                        {\n"
            "                            foreach (var rp in lods[li])\n"
            "                            {\n"
            "                                if (rp != null && rp.TryGet(out var r) && r != null)\n"
            "                                {\n"
            "                                    lowerLods.Add(r);\n"
            "                                }\n"
            "                            }\n"
            "                        }\n"
            "                    }\n"
            "                }\n"
            "                if (cur.m_Children != null)\n"
            "                {\n"
            "                    foreach (var cp in cur.m_Children)\n"
            "                    {\n"
            "                        if (cp.TryGet(out var child))\n"
            "                            stack.Push((child, depth + 1));\n"
            "                    }\n"
            "                }\n"
            "            }\n"
            "            lowerLods.ExceptWith(keptLod0);\n"
            "            suppressedLodRenderers.UnionWith(lowerLods);\n"
            "        }\n\n"
            "        private void ConvertMeshRenderer(Transform m_Transform)\n"
            "        {\n"
            "            m_Transform.m_GameObject.TryGet(out var m_GameObject);\n"
            "            if (m_GameObject != null && m_GameObject.m_SuppressExport)\n"
            "                return;\n"
            "\n"
            "            bool skipProxies = global::System.Environment.GetEnvironmentVariable(\"AS_SKIP_PROXIES\") == \"1\";\n"
            "            if (skipProxies && m_GameObject != null && !string.IsNullOrEmpty(m_GameObject.m_Name) && IsProxyObjectName(m_GameObject.m_Name))\n"
            "                return;\n",
        ),
        (
            "        private void ConvertMeshRenderer(Renderer meshR)\n"
            "        {\n"
            "            var mesh = GetMesh(meshR);\n"
            "            if (mesh == null)\n"
            "                return;\n"
            "            var iMesh = new ImportedMesh();",
            "        private static bool IsProxyObjectName(string text)\n"
            "        {\n"
            "            if (string.IsNullOrEmpty(text))\n"
            "                return false;\n"
            "            var low = text.ToLowerInvariant();\n"
            "            return low.EndsWith(\"_proxy\") || low.Contains(\"_proxy (\") || low.Contains(\"_proxy_\")\n"
            "                || low.EndsWith(\"_col\") || low.Contains(\"_col_\")\n"
            "                || low.EndsWith(\"_collider\") || low.Contains(\"_collider (\") || low.Contains(\"_collider_\")\n"
            "                || low.StartsWith(\"scenepropcollider_\")\n"
            "                || low.StartsWith(\"blockout_\")\n"
            "                || (low.StartsWith(\"stage_\") && (low.EndsWith(\"_ld\") || low.Contains(\"_ld (\")))\n"
            "                || low.StartsWith(\"volumetricfog\")\n"
            "                || low.StartsWith(\"sm_merged_bp_\") || low.StartsWith(\"napmesh,\");\n"
            "        }\n\n"
            "        private static bool IsProxyOrGreyboxMaterial(Material mat)\n"
            "        {\n"
            "            if (mat == null)\n"
            "                return true;\n"
            "            if (!string.IsNullOrEmpty(mat.m_Name))\n"
            "            {\n"
            "                if (mat.m_Name.IndexOf(\"ShadowProxy\", global::System.StringComparison.OrdinalIgnoreCase) >= 0)\n"
            "                    return true;\n"
            "                if (mat.m_Name.StartsWith(\"Eff_\", global::System.StringComparison.OrdinalIgnoreCase))\n"
            "                    return true;\n"
            "            }\n"
            "            bool isPlaceholder = string.Equals(mat.m_Name, \"Lit\", global::System.StringComparison.OrdinalIgnoreCase)\n"
            "                || string.Equals(mat.m_Name, \"Unlit\", global::System.StringComparison.OrdinalIgnoreCase)\n"
            "                || string.Equals(mat.m_Name, \"miHoYo_Unlit_Unlit\", global::System.StringComparison.OrdinalIgnoreCase);\n"
            "            if (isPlaceholder\n"
            "                && mat.m_SavedProperties?.m_TexEnvs != null\n"
            "                && !mat.m_SavedProperties.m_TexEnvs.Any(kv => !kv.Value.m_Texture.IsNull))\n"
            "                return true;\n"
            "            return false;\n"
            "        }\n\n"
            "        private void ConvertMeshRenderer(Renderer meshR)\n"
            "        {\n"
            "            var mesh = GetMesh(meshR);\n"
            "            if (mesh == null)\n"
            "                return;\n"
            "            bool skipProxies = global::System.Environment.GetEnvironmentVariable(\"AS_SKIP_PROXIES\") == \"1\";\n"
            "            if (skipProxies && suppressedLodRenderers.Contains(meshR))\n"
            "                return;\n"
            "            bool manifestExact = !string.IsNullOrEmpty(global::System.Environment.GetEnvironmentVariable(\"AS_CONTAINER_HASH_FILE\"));\n"
            "            if (skipProxies && meshR.m_Materials.Count > 0 && meshR.m_Materials.All(m => !m.TryGet(out var mat) || IsProxyOrGreyboxMaterial(mat)))\n"
            "                return;\n"
            "            var iMesh = new ImportedMesh();",
        ),
        (
            "        private void ConvertTexture2D(Texture2D m_Texture2D, string name)\n"
            "        {\n"
            "            var iTex = ImportedHelpers.FindTexture(name, TextureList);\n"
            "            if (iTex != null)\n"
            "            {\n"
            "                return;\n"
            "            }\n\n"
            "            var stream = m_Texture2D.ConvertToStream(options.imageFormat, true);\n"
            "            if (stream != null)\n"
            "            {\n"
            "                using (stream)\n"
            "                {\n"
            "                    iTex = new ImportedTexture(stream, name);\n"
            "                    TextureList.Add(iTex);\n"
            "                }\n"
            "            }\n"
            "        }",
            "        private static readonly global::System.Collections.Concurrent.ConcurrentDictionary<(string, long, ImageFormat), byte[]> _encodedTextureCache\n"
            "            = new global::System.Collections.Concurrent.ConcurrentDictionary<(string, long, ImageFormat), byte[]>();\n\n"
            "        private void ConvertTexture2D(Texture2D m_Texture2D, string name)\n"
            "        {\n"
            "            var iTex = ImportedHelpers.FindTexture(name, TextureList);\n"
            "            if (iTex != null)\n"
            "            {\n"
            "                return;\n"
            "            }\n\n"
            "            var cacheKey = (m_Texture2D.assetsFile?.fileName ?? \"\", m_Texture2D.m_PathID, options.imageFormat);\n"
            "            if (_encodedTextureCache.TryGetValue(cacheKey, out var cachedBytes))\n"
            "            {\n"
            "                if (cachedBytes != null && cachedBytes.Length > 0)\n"
            "                {\n"
            "                    using (var ms = new global::System.IO.MemoryStream(cachedBytes, writable: false))\n"
            "                    {\n"
            "                        iTex = new ImportedTexture(ms, name);\n"
            "                        TextureList.Add(iTex);\n"
            "                    }\n"
            "                }\n"
            "                return;\n"
            "            }\n\n"
            "            var stream = m_Texture2D.ConvertToStream(options.imageFormat, true);\n"
            "            if (stream != null)\n"
            "            {\n"
            "                using (stream)\n"
            "                {\n"
            "                    var bytes = stream.ToArray();\n"
            "                    if (_encodedTextureCache.Count > 2048)\n"
            "                    {\n"
            "                        _encodedTextureCache.Clear();\n"
            "                    }\n"
            "                    _encodedTextureCache[cacheKey] = bytes;\n"
            "                    iTex = new ImportedTexture(stream, name);\n"
            "                    TextureList.Add(iTex);\n"
            "                }\n"
            "            }\n"
            "        }",
        ),
        (
            "                    if (meshR.m_Materials[i - firstSubMesh].TryGet(out var m_Material))\n"
            "                    {\n"
            "                        mat = m_Material;\n"
            "                    }\n"
            "                }\n"
            "                ImportedMaterial iMat = ConvertMaterial(mat);",
            "                    if (meshR.m_Materials[i - firstSubMesh].TryGet(out var m_Material))\n"
            "                    {\n"
            "                        mat = m_Material;\n"
            "                    }\n"
            "                }\n"
            "                if (skipProxies && IsProxyOrGreyboxMaterial(mat))\n"
            "                {\n"
            "                    firstFace += numFaces;\n"
            "                    continue;\n"
            "                }\n"
            "                ImportedMaterial iMat = ConvertMaterial(mat);",
        ),
        (
            "            if (m_GameObject.m_SkinnedMeshRenderer != null)\n"
            "            {\n"
            "                ConvertMeshRenderer(m_GameObject.m_SkinnedMeshRenderer);\n"
            "            }\n\n"
            "            if (m_GameObject.m_Animation != null)",
            "            if (m_GameObject.m_SkinnedMeshRenderer != null)\n"
            "            {\n"
            "                ConvertMeshRenderer(m_GameObject.m_SkinnedMeshRenderer);\n"
            "            }\n\n"
            "            if (m_GameObject.m_Components != null)\n"
            "            {\n"
            "                foreach (var compPptr in m_GameObject.m_Components)\n"
            "                {\n"
            "                    if (compPptr.TryGet<MonoBehaviour>(out var mb))\n"
            "                    {\n"
            "                        var mbName = mb.Name;\n"
            "                        if (string.Equals(mbName, \"NapStreamingBRGHelper\", StringComparison.OrdinalIgnoreCase)\n"
            "                            || (string.IsNullOrEmpty(mbName) && mb.byteSize >= 68 && mb.byteSize <= 32 * 1024 * 1024))\n"
            "                        {\n"
            "                            ConvertNapStreamingBRG(m_Transform, mb);\n"
            "                        }\n"
            "                    }\n"
            "                }\n"
            "            }\n\n"
            "            if (m_GameObject.m_Animation != null)",
        ),
        (
            "            else if (meshR.m_SubsetIndices?.Length > 0)\n"
            "            {\n"
            "                firstSubMesh = (int)meshR.m_SubsetIndices.Min(x => x);\n"
            "                foreach (var index in meshR.m_SubsetIndices)\n"
            "                {\n"
            "                    subHashSet.Add((int)index);\n"
            "                }\n"
            "                combine = true;\n"
            "            }",
            "            else if (meshR.m_SubsetIndices?.Length > 0)\n"
            "            {\n"
            "                firstSubMesh = (int)meshR.m_SubsetIndices.Min(x => x);\n"
            "                foreach (var index in meshR.m_SubsetIndices)\n"
            "                {\n"
            "                    subHashSet.Add((int)index);\n"
            "                }\n"
            "                combine = true;\n"
            "            }\n\n"
            "            bool trackMrKey = false;\n"
            "            (long, int, int, int, int, int) mrKey = default;\n"
            "            if (skipProxies && !manifestExact && m_GameObject2?.m_Transform != null)\n"
            "            {\n"
            "                var wpos = ComputeWorldPosition(m_GameObject2.m_Transform);\n"
            "                var q = m_GameObject2.m_Transform.m_LocalRotation;\n"
            "                int rHash = ((int)Math.Round(q.X * 100f) & 0xFF)\n"
            "                    | (((int)Math.Round(q.Y * 100f) & 0xFF) << 8)\n"
            "                    | (((int)Math.Round(q.Z * 100f) & 0xFF) << 16);\n"
            "                mrKey = (mesh.m_PathID, firstSubMesh,\n"
            "                    (int)Math.Round(wpos.X * 10f),\n"
            "                    (int)Math.Round(wpos.Y * 10f),\n"
            "                    (int)Math.Round(wpos.Z * 10f),\n"
            "                    rHash);\n"
            "                if (ExportedMeshRenderers.Contains(mrKey))\n"
            "                    return;\n"
            "                trackMrKey = true;\n"
            "            }",
        ),
        (
            "            MeshList.Add(iMesh);\n"
            "        }\n\n"
            "        private static Mesh GetMesh(Renderer meshR)",
            "            if (iMesh.SubmeshList.Count > 0)\n"
            "            {\n"
            "                if (trackMrKey)\n"
            "                    ExportedMeshRenderers.Add(mrKey);\n"
            "                MeshList.Add(iMesh);\n"
            "            }\n"
            "        }" + _brg_helpers + "\n\n"
            "        private static Mesh GetMesh(Renderer meshR)",
        ),
        (
            "            if (iMesh.SubmeshList.Count > 0)\n"
            "                MeshList.Add(iMesh);\n"
            "        }\n\n"
            "        private static Mesh GetMesh(Renderer meshR)",
            "            if (iMesh.SubmeshList.Count > 0)\n"
            "            {\n"
            "                if (trackMrKey)\n"
            "                    ExportedMeshRenderers.Add(mrKey);\n"
            "                MeshList.Add(iMesh);\n"
            "            }\n"
            "        }" + _brg_helpers + "\n\n"
            "        private static Mesh GetMesh(Renderer meshR)",
        ),
    ])

    _replace_in_file("AnimeStudio/AssetsHelper.cs", [
        (
            "        private static Dictionary<string, HashSet<long>> Offsets = new Dictionary<string, HashSet<long>>();",
            "        private static Dictionary<string, HashSet<long>> Offsets = new Dictionary<string, HashSet<long>>(StringComparer.OrdinalIgnoreCase);",
        ),
        (
            "        public static void ClearOffsets()\n"
            "        {\n"
            "            Offsets.Clear();\n"
            "            Logger.Verbose(\"Cleared cached offsets\");\n"
            "        }",
            "        public static void ClearOffsets()\n"
            "        {\n"
            "            Offsets.Clear();\n"
            "            Logger.Verbose(\"Cleared cached offsets\");\n"
            "        }\n\n"
            "        public static string[] GetOffsetPaths() => Offsets.Keys.ToArray();",
        ),
        (
            "        public static bool TryGet(string path, out long[] offsets)\n"
            "        {\n"
            "            if (Offsets.TryGetValue(path, out var list) && list.Count > 0)\n"
            "            {\n"
            "                Logger.Verbose($\"Found {list.Count} offsets for path {path}\");",
            "        public static bool TryGet(string path, out long[] offsets)\n"
            "        {\n"
            "            var full = string.IsNullOrEmpty(path) ? path : Path.GetFullPath(path);\n"
            "            if (!string.IsNullOrEmpty(full) && Offsets.TryGetValue(full, out var list) && list.Count > 0)\n"
            "            {\n"
            "                Logger.Verbose($\"Found {list.Count} offsets for path {full}\");",
        ),
        (
            "                    var fullPath = Path.Combine(BaseFolder, entry.Path);",
            "                    var fullPath = Path.GetFullPath(Path.Combine(BaseFolder, entry.Path));",
        ),
        (
            "                    var fullPath = Path.GetFullPath(Path.Combine(BaseFolder, entry.Path));\n"
            "                    Logger.Verbose($\"Found {cab} in {fullPath}\");",
            "                    var fullPath = Path.GetFullPath(Path.Combine(BaseFolder, entry.Path));\n"
            "                    if (!File.Exists(fullPath))\n"
            "                        continue;\n"
            "                    Logger.Verbose($\"Found {cab} in {fullPath}\");",
        ),
        (
            "            var relativePath = Path.GetRelativePath(BaseFolder, path);",
            "            var relativePath = Path.GetRelativePath(BaseFolder, Path.GetFullPath(path));",
        ),
        (
            "        public static string[] ProcessFiles(string[] files_list)\n"
            "        {\n"
            "            HashSet<string> files = new HashSet<string>(files_list, StringComparer.OrdinalIgnoreCase);",
            "        public static string[] ProcessFiles(string[] files_list)\n"
            "        {\n"
            "            Offsets.Clear();\n"
            "            HashSet<string> files = new HashSet<string>(files_list.Select(Path.GetFullPath), StringComparer.OrdinalIgnoreCase);",
        ),
        (
            "            var path = Path.GetDirectoryName(Path.GetFullPath(files[0]));\n"
            "            // Merge splits once for the whole batch — not once per file inside AssetsManager.LoadFiles.\n"
            "            ImportHelper.MergeSplitAssets(path);\n"
            "            var toReadFile = ImportHelper.ProcessingSplitFiles(files.ToList());\n\n"
            "            var filesList = new List<string>(toReadFile);",
            "            List<string> filesList;\n"
            "            if (files.Length > 1 && files.All(f => f.EndsWith(\".blk\", StringComparison.OrdinalIgnoreCase)))\n"
            "            {\n"
            "                filesList = new List<string>(files);\n"
            "            }\n"
            "            else\n"
            "            {\n"
            "                var path = Path.GetDirectoryName(Path.GetFullPath(files[0]));\n"
            "                ImportHelper.MergeSplitAssets(path);\n"
            "                var toReadFile = ImportHelper.ProcessingSplitFiles(files.ToList());\n"
            "                filesList = new List<string>(toReadFile);\n"
            "            }",
        ),
    ])

    _replace_in_file("AnimeStudio.Utility/ACL/ACL.cs", [
        (
            "        public static void DecompressTracksV2(byte[] transform_data, byte[] scalar_data, byte[] db, byte[] db_bulk_data, out float[] values, out float[] times)\n"
            "        {\n"
            "            var decompressedClip = new DecompressedClip();",
            "        public static void DecompressTracksV2(byte[] transform_data, byte[] scalar_data, byte[] db, byte[] db_bulk_data, out float[] values, out float[] times)\n"
            "        {\n"
            "            if (transform_data == null || transform_data.Length == 0 || db == null || db.Length == 0 || db_bulk_data == null || db_bulk_data.Length == 0)\n"
            "            {\n"
            "                throw new InvalidDataException(\"ZZZ ACL V2: missing transform or database buffer (stripped clip database not loaded).\");\n"
            "            }\n"
            "            var decompressedClip = new DecompressedClip();",
        ),
    ])

    _replace_in_file("AnimeStudio/AssetsManager.cs", [
        (
            "        internal HashSet<string> assetsFileListHash = new HashSet<string>(StringComparer.OrdinalIgnoreCase);",
            "        internal HashSet<string> assetsFileListHash = new HashSet<string>(StringComparer.OrdinalIgnoreCase);\n"
            "        internal HashSet<SerializedFile> fallbackAssetsFiles = new HashSet<SerializedFile>();",
        ),
        (
            "                .ThenBy(file => new FileInfo(file).Length)",
            "                .ThenBy(file => File.Exists(file) ? new FileInfo(file).Length : 0L)",
        ),
        (
            "        private void LoadFile(string fullName)\n"
            "        {\n"
            "            if (Game?.Type == GameType.AFKJourney",
            "        private void LoadFile(string fullName)\n"
            "        {\n"
            "            if (!File.Exists(fullName))\n"
            "                return;\n"
            "            if (Game?.Type == GameType.AFKJourney",
        ),
        (
            "        public bool EnsureAnimatorAvatarLoaded(Animator animator)\n"
            "        {\n"
            "            if (Game?.Type.IsZZZ() != true || animator?.m_Avatar == null)",
            "        public bool EnsureAnimatorAvatarLoaded(Animator animator)\n"
            "        {\n"
            "            if (Environment.GetEnvironmentVariable(\"AS_SKIP_PROXIES\") == \"1\"\n"
            "                && string.IsNullOrEmpty(Environment.GetEnvironmentVariable(\"AS_EXTRA_CABS\")))\n"
            "                return false;\n"
            "            if (Game?.Type.IsZZZ() != true || animator?.m_Avatar == null)",
        ),
        (
            "            Progress.Reset();\n"
            "            //use a for loop because list size can change\n"
            "            for (var i = 0; i < importFiles.Count; i++)\n"
            "            {\n"
            "                LoadFile(importFiles[i]);\n"
            "                Progress.Report(i + 1, importFiles.Count);\n"
            "                if (tokenSource.IsCancellationRequested)\n"
            "                {\n"
            "                    Logger.Info(\"Loading files has been aborted !!\");\n"
            "                    break;\n"
            "                }\n"
            "            }",
            "            var alreadyFull = ResolveDependencies\n"
            "                ? new HashSet<string>(files.Where(f => !AssetsHelper.TryGet(f, out _)), StringComparer.OrdinalIgnoreCase)\n"
            "                : null;\n\n"
            "            Progress.Reset();\n"
            "            //use a for loop because list size can change\n"
            "            for (var i = 0; i < importFiles.Count; i++)\n"
            "            {\n"
            "                LoadFile(importFiles[i]);\n"
            "                Progress.Report(i + 1, importFiles.Count);\n"
            "                if (tokenSource.IsCancellationRequested)\n"
            "                {\n"
            "                    Logger.Info(\"Loading files has been aborted !!\");\n"
            "                    break;\n"
            "                }\n"
            "            }\n\n"
            "            // Runtime fallback for CABMap name collisions across .blk files:\n"
            "            // FindCAB() only inspects CABMap entries owned by the primary file's path, so any\n"
            "            // collided CAB loaded from the primary file won't have its m_Externals pre-queued.\n"
            "            // Resolving unloaded externals directly from loaded SerializedFiles closes the gap.\n"
            "            if (ResolveDependencies && !tokenSource.IsCancellationRequested)\n"
            "            {\n"
            "                var directFileCount = assetsFileList.Count;\n"
            "                var checkedCabs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);\n"
            "                var extraCabsEnv = Environment.GetEnvironmentVariable(\"AS_EXTRA_CABS\");\n"
            "                var extraCabsPending = !string.IsNullOrWhiteSpace(extraCabsEnv)\n"
            "                    ? new Queue<string>(extraCabsEnv.Split(new[] { ',', ';' }, StringSplitOptions.RemoveEmptyEntries).Select(s => s.Trim()).Where(s => !string.IsNullOrEmpty(s)))\n"
            "                    : null;\n"
            "                int scannedFiles = 0;\n"
            "                while ((scannedFiles < assetsFileList.Count || (extraCabsPending != null && extraCabsPending.Count > 0)) && !tokenSource.IsCancellationRequested)\n"
            "                {\n"
            "                    var missingCabs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);\n"
            "                    for (; scannedFiles < assetsFileList.Count; scannedFiles++)\n"
            "                    {\n"
            "                        foreach (var ext in assetsFileList[scannedFiles].m_Externals)\n"
            "                        {\n"
            "                            var cab = ext.fileName;\n"
            "                            if (!string.IsNullOrEmpty(cab) && !assetsFileListHash.Contains(cab) && checkedCabs.Add(cab))\n"
            "                            {\n"
            "                                missingCabs.Add(cab);\n"
            "                            }\n"
            "                        }\n"
            "                    }\n"
            "                    if (extraCabsPending != null)\n"
            "                    {\n"
            "                        while (extraCabsPending.Count > 0)\n"
            "                        {\n"
            "                            var cab = extraCabsPending.Dequeue();\n"
            "                            if (!assetsFileListHash.Contains(cab) && checkedCabs.Add(cab))\n"
            "                                missingCabs.Add(cab);\n"
            "                        }\n"
            "                    }\n"
            "                    if (missingCabs.Count == 0)\n"
            "                        break;\n\n"
            "                    AssetsHelper.ClearOffsets();\n"
            "                    AssetsHelper.AddCABOffsetsFast(alreadyFull, missingCabs);\n"
            "                    var extraFiles = AssetsHelper.GetOffsetPaths();\n"
            "                    if (extraFiles.Length == 0)\n"
            "                        break;\n"
            "                    foreach (var extra in extraFiles)\n"
            "                    {\n"
            "                        if (tokenSource.IsCancellationRequested)\n"
            "                            break;\n"
            "                        LoadFile(extra);\n"
            "                    }\n"
            "                }\n"
            "                for (int k = directFileCount; k < assetsFileList.Count; k++)\n"
            "                    fallbackAssetsFiles.Add(assetsFileList[k]);\n"
            "            }",
        ),
        (
            "            assetsFileList.Clear();\n\n"
            "            foreach (var resourceFileReader in resourceFileReaders)",
            "            assetsFileList.Clear();\n"
            "            fallbackAssetsFiles.Clear();\n\n"
            "            foreach (var resourceFileReader in resourceFileReaders)",
        ),
        (
            "                    if (isFallbackFile\n"
            "                        && objectReader.type != ClassIDType.Material\n"
            "                        && objectReader.type != ClassIDType.Texture2D\n"
            "                        && objectReader.type != ClassIDType.Shader\n"
            "                        && objectReader.type != ClassIDType.AssetBundle\n"
            "                        && objectReader.type != ClassIDType.ResourceManager\n"
            "                        && objectReader.type != ClassIDType.NapAssetBundleIndexAsset)\n",
            "                    if (isFallbackFile\n"
            "                        && objectReader.type != ClassIDType.Avatar\n"
            "                        && objectReader.type != ClassIDType.Material\n"
            "                        && objectReader.type != ClassIDType.Texture2D\n"
            "                        && objectReader.type != ClassIDType.Shader\n"
            "                        && objectReader.type != ClassIDType.Mesh\n"
            "                        && objectReader.type != ClassIDType.MonoScript\n"
            "                        && objectReader.type != ClassIDType.AssetBundle\n"
            "                        && objectReader.type != ClassIDType.ResourceManager\n"
            "                        && objectReader.type != ClassIDType.NapAssetBundleIndexAsset)\n",
        ),
        (
            "            foreach (var assetsFile in assetsFileList)\n"
            "            {\n"
            "                foreach (var objectInfo in assetsFile.m_Objects)\n"
            "                {\n"
            "                    if (tokenSource.IsCancellationRequested)\n"
            "                    {\n"
            "                        Logger.Info(\"Reading assets has been cancelled !!\");\n"
            "                        return;\n"
            "                    }\n"
            "                    var objectReader = new ObjectReader(assetsFile.reader, assetsFile, objectInfo, Game);\n"
            "                    try",
            "            foreach (var assetsFile in assetsFileList)\n"
            "            {\n"
            "                bool isFallbackFile = fallbackAssetsFiles.Contains(assetsFile);\n"
            "                foreach (var objectInfo in assetsFile.m_Objects)\n"
            "                {\n"
            "                    if (tokenSource.IsCancellationRequested)\n"
            "                    {\n"
            "                        Logger.Info(\"Reading assets has been cancelled !!\");\n"
            "                        return;\n"
            "                    }\n"
            "                    var objectReader = new ObjectReader(assetsFile.reader, assetsFile, objectInfo, Game);\n"
            "                    if (isFallbackFile\n"
            "                        && objectReader.type != ClassIDType.Avatar\n"
            "                        && objectReader.type != ClassIDType.Material\n"
            "                        && objectReader.type != ClassIDType.Texture2D\n"
            "                        && objectReader.type != ClassIDType.Shader\n"
            "                        && objectReader.type != ClassIDType.Mesh\n"
            "                        && objectReader.type != ClassIDType.MonoScript\n"
            "                        && objectReader.type != ClassIDType.AssetBundle\n"
            "                        && objectReader.type != ClassIDType.ResourceManager\n"
            "                        && objectReader.type != ClassIDType.NapAssetBundleIndexAsset)\n"
            "                    {\n"
            "                        Progress.Report(++i, progressCount);\n"
            "                        continue;\n"
            "                    }\n"
            "                    try",
        ),
        (
            "                            ClassIDType.IndexObject when ClassIDType.IndexObject.CanParse() => new IndexObject(objectReader),\n"
            "                            ClassIDType.Material when ClassIDType.Material.CanParse() => new Material(objectReader),\n"
            "                            ClassIDType.Mesh when ClassIDType.Mesh.CanParse() => new Mesh(objectReader),\n"
            "                            ClassIDType.MeshFilter when ClassIDType.MeshFilter.CanParse() => new MeshFilter(objectReader),",
            "                            ClassIDType.IndexObject when ClassIDType.IndexObject.CanParse() => new IndexObject(objectReader),\n"
            "                            ClassIDType.LODGroup when ClassIDType.LODGroup.CanParse() || ClassIDType.GameObject.CanParse() => new LODGroup(objectReader),\n"
            "                            ClassIDType.Material when ClassIDType.Material.CanParse() => new Material(objectReader),\n"
            "                            ClassIDType.Mesh when ClassIDType.Mesh.CanParse() => new Mesh(objectReader),\n"
            "                            ClassIDType.MeshFilter when ClassIDType.MeshFilter.CanParse() => new MeshFilter(objectReader),\n"
            "                            ClassIDType.NapLODMeshFilter when ClassIDType.MeshFilter.CanParse() || ClassIDType.NapLODMeshFilter.CanParse() => new NapLODMeshFilter(objectReader),",
        ),
        (
            "                                    case MeshFilter m_MeshFilter:\n"
            "                                        Logger.Verbose($\"Fetched MeshFilter component with {m_MeshFilter.m_PathID} in file {m_MeshFilter.assetsFile.fileName}, assigning to GameObject components...\");\n"
            "                                        m_GameObject.m_MeshFilter = m_MeshFilter;\n"
            "                                            break;",
            "                                    case MeshFilter m_MeshFilter:\n"
            "                                        Logger.Verbose($\"Fetched MeshFilter component with {m_MeshFilter.m_PathID} in file {m_MeshFilter.assetsFile.fileName}, assigning to GameObject components...\");\n"
            "                                        if (m_GameObject.m_MeshFilter == null || m_MeshFilter.GetType() == typeof(MeshFilter))\n"
            "                                            m_GameObject.m_MeshFilter = m_MeshFilter;\n"
            "                                        break;\n"
            "                                    case LODGroup m_LODGroup:\n"
            "                                        Logger.Verbose($\"Fetched LODGroup component with {m_LODGroup.m_PathID} in file {m_LODGroup.assetsFile.fileName}, assigning to GameObject components...\");\n"
            "                                        m_GameObject.m_LODGroup = m_LODGroup;\n"
            "                                        break;",
        ),
        (
            "                    if (avatar.m_Transform != null)\n"
            "                    {\n"
            "                        foreach (var childPtr in avatar.m_Transform.m_Children)\n"
            "                        {\n"
            "                            if (childPtr.TryGet(out var child) && child.m_GameObject.TryGet(out var childGO))\n"
            "                            {\n"
            "                                var childName = childGO.Name;\n"
            "                                foreach (var i in childGO.m_Components)\n"
            "                                {\n"
            "                                    if (i.TryGet<MonoBehaviour>(out var comp))\n"
            "                                    {\n"
            "                                        if(comp.Name == \"NapLodController\")\n"
            "                                        {\n"
            "                                            // Safely decode raw bytes to string\n"
            "                                            var raw = comp.GetRawData();\n"
            "                                            string Path = raw != null ? System.Text.Encoding.UTF8.GetString(raw) : string.Empty;\n"
            "                                            if (string.IsNullOrEmpty(Path))\n"
            "                                                continue;\n\n"
            "                                            int assetIndex = Path.IndexOf(\"Assets\", StringComparison.Ordinal);\n\n"
            "                                            string trimmed;\n"
            "                                            if (assetIndex != -1)\n"
            "                                            {\n"
            "                                                // Return the substring starting from the found index\n"
            "                                                trimmed = Path.Substring(assetIndex);\n"
            "                                            }\n"
            "                                            else if (Path.Length > 40)\n"
            "                                            {\n"
            "                                                // only take substring if long enough\n"
            "                                                trimmed = Path.Substring(40);\n"
            "                                            }\n"
            "                                            else\n"
            "                                            {\n"
            "                                                // too short to be useful\n"
            "                                                Logger.Verbose($\"NapLodController path too short ({Path.Length}), skipping\");\n"
            "                                                continue;\n"
            "                                            }\n\n"
            "                                            trimmed = trimmed?.Trim();\n"
            "                                            if (string.IsNullOrEmpty(trimmed))\n"
            "                                                continue;\n\n"
            "                                            // ensure \".mesh\" exists before taking substring\n"
            "                                            int meshIndex = trimmed.IndexOf(\".mesh\", StringComparison.OrdinalIgnoreCase);\n"
            "                                            if (meshIndex <= 0)\n"
            "                                            {\n"
            "                                                Logger.Verbose($\".mesh not found in '{trimmed}', skipping\");\n"
            "                                                continue;\n"
            "                                            }\n\n"
            "                                            trimmed = trimmed.Substring(0, meshIndex);\n\n"
            "                                            // safely get last token after '/', if present\n"
            "                                            int lastSlash = trimmed.LastIndexOf('/');\n"
            "                                            if (lastSlash >= 0 && lastSlash < trimmed.Length - 1)\n"
            "                                                trimmed = trimmed.Substring(lastSlash + 1);\n\n"
            "                                            trimmed = trimmed.Trim();\n"
            "                                            if (string.IsNullOrEmpty(trimmed))\n"
            "                                                continue;\n\n\n"
            "                                            if (separateMeshes.TryGetValue(trimmed, out var meshPPtr))\n"
            "                                            {\n"
            "                                                Logger.Verbose($\"Trying to attach {trimmed} to {childName}\");\n"
            "                                                if (childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull)\n"
            "                                                {\n"
            "                                                    Logger.Info($\"Attached {trimmed} to {childName}\");\n"
            "                                                    childGO.m_SkinnedMeshRenderer.m_Mesh = meshPPtr;\n"
            "                                                }\n"
            "                                                else if (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)\n"
            "                                                {\n"
            "                                                    Logger.Info($\"Attached {trimmed} to {childName}\");\n"
            "                                                    childGO.m_MeshFilter.m_Mesh = meshPPtr;\n"
            "                                                }\n"
            "                                            }\n"
            "                                            else if (separateMeshes.TryGetValue(childName, out meshPPtr))\n"
            "                                            {\n"
            "                                                Logger.Verbose($\"Trying to attach {childName} to {childName}\");\n"
            "                                                if (childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull)\n"
            "                                                {\n"
            "                                                    Logger.Info($\"Attached {childName} to {childName}\");\n"
            "                                                    childGO.m_SkinnedMeshRenderer.m_Mesh = meshPPtr;\n"
            "                                                }\n"
            "                                                else if (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)\n"
            "                                                {\n"
            "                                                    Logger.Info($\"Attached {childName} to {childName}\");\n"
            "                                                    childGO.m_MeshFilter.m_Mesh = meshPPtr;\n"
            "                                                }\n"
            "                                            }\n\n"
            "                                        }\n"
            "                                    }\n"
            "                                }\n"
            "                            }\n"
            "                        }\n"
            "                    }",
            "                    if (avatar.m_Transform != null)\n"
            "                    {\n"
            "                        bool attachedAnyLod0 = false;\n"
            "                        var allChildren = new List<GameObject>();\n"
            "                        var visitedTransforms = new HashSet<Transform>();\n"
            "                        var stack = new Stack<(PPtr<Transform> ptr, int depth)>();\n"
            "                        for (int ci = avatar.m_Transform.m_Children.Count - 1; ci >= 0; ci--)\n"
            "                        {\n"
            "                            stack.Push((avatar.m_Transform.m_Children[ci], 1));\n"
            "                        }\n\n"
            "                        while (stack.Count > 0)\n"
            "                        {\n"
            "                            var (childPtr, depth) = stack.Pop();\n"
            "                            if (depth > 48 || !childPtr.TryGet(out var child) || !visitedTransforms.Add(child))\n"
            "                                continue;\n\n"
            "                            if (child.m_Children != null)\n"
            "                            {\n"
            "                                for (int ci = child.m_Children.Count - 1; ci >= 0; ci--)\n"
            "                                {\n"
            "                                    stack.Push((child.m_Children[ci], depth + 1));\n"
            "                                }\n"
            "                            }\n\n"
            "                            if (!child.m_GameObject.TryGet(out var childGO))\n"
            "                                continue;\n\n"
            "                            allChildren.Add(childGO);\n"
            "                            var childName = childGO.Name;\n"
            "                            bool childAttached = false;\n\n"
            "                            foreach (var i in childGO.m_Components)\n"
            "                            {\n"
            "                                if (i.TryGet<MonoBehaviour>(out var comp) && comp.Name == \"NapLodController\")\n"
            "                                {\n"
            "                                    var raw = comp.GetRawData();\n"
            "                                    string rawPath = raw != null ? System.Text.Encoding.UTF8.GetString(raw) : string.Empty;\n"
            "                                    if (string.IsNullOrEmpty(rawPath))\n"
            "                                        continue;\n\n"
            "                                    int assetIndex = rawPath.IndexOf(\"Assets\", StringComparison.Ordinal);\n"
            "                                    string trimmed;\n"
            "                                    if (assetIndex != -1)\n"
            "                                    {\n"
            "                                        trimmed = rawPath.Substring(assetIndex);\n"
            "                                    }\n"
            "                                    else if (rawPath.Length > 40)\n"
            "                                    {\n"
            "                                        trimmed = rawPath.Substring(40);\n"
            "                                    }\n"
            "                                    else\n"
            "                                    {\n"
            "                                        Logger.Verbose($\"NapLodController path too short ({rawPath.Length}), skipping\");\n"
            "                                        continue;\n"
            "                                    }\n\n"
            "                                    trimmed = trimmed?.Trim();\n"
            "                                    if (string.IsNullOrEmpty(trimmed))\n"
            "                                        continue;\n\n"
            "                                    int meshIndex = trimmed.IndexOf(\".mesh\", StringComparison.OrdinalIgnoreCase);\n"
            "                                    if (meshIndex <= 0)\n"
            "                                    {\n"
            "                                        Logger.Verbose($\".mesh not found in '{trimmed}', skipping\");\n"
            "                                        continue;\n"
            "                                    }\n\n"
            "                                    trimmed = trimmed.Substring(0, meshIndex);\n"
            "                                    int lastSlash = trimmed.LastIndexOf('/');\n"
            "                                    if (lastSlash >= 0 && lastSlash < trimmed.Length - 1)\n"
            "                                        trimmed = trimmed.Substring(lastSlash + 1);\n\n"
            "                                    trimmed = trimmed.Trim();\n"
            "                                    if (string.IsNullOrEmpty(trimmed))\n"
            "                                        continue;\n\n"
            "                                    if (separateMeshes.TryGetValue(trimmed, out var meshPPtr))\n"
            "                                    {\n"
            "                                        Logger.Verbose($\"Trying to attach {trimmed} to {childName}\");\n"
            "                                        if (childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull)\n"
            "                                        {\n"
            "                                            Logger.Info($\"Attached {trimmed} to {childName}\");\n"
            "                                            childGO.m_SkinnedMeshRenderer.m_Mesh = meshPPtr;\n"
            "                                            childAttached = true;\n"
            "                                            attachedAnyLod0 = true;\n"
            "                                        }\n"
            "                                        else if (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)\n"
            "                                        {\n"
            "                                            Logger.Info($\"Attached {trimmed} to {childName}\");\n"
            "                                            childGO.m_MeshFilter.m_Mesh = meshPPtr;\n"
            "                                            childAttached = true;\n"
            "                                            attachedAnyLod0 = true;\n"
            "                                        }\n"
            "                                    }\n"
            "                                    else if (separateMeshes.TryGetValue(childName, out meshPPtr))\n"
            "                                    {\n"
            "                                        Logger.Verbose($\"Trying to attach {childName} to {childName}\");\n"
            "                                        if (childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull)\n"
            "                                        {\n"
            "                                            Logger.Info($\"Attached {childName} to {childName}\");\n"
            "                                            childGO.m_SkinnedMeshRenderer.m_Mesh = meshPPtr;\n"
            "                                            childAttached = true;\n"
            "                                            attachedAnyLod0 = true;\n"
            "                                        }\n"
            "                                        else if (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)\n"
            "                                        {\n"
            "                                            Logger.Info($\"Attached {childName} to {childName}\");\n"
            "                                            childGO.m_MeshFilter.m_Mesh = meshPPtr;\n"
            "                                            childAttached = true;\n"
            "                                            attachedAnyLod0 = true;\n"
            "                                        }\n"
            "                                    }\n"
            "                                }\n"
            "                            }\n\n"
            "                            if (!childAttached && ((childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull) || (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)))\n"
            "                            {\n"
            "                                if (separateMeshes.TryGetValue($\"SeparateMesh_{rootName}_{childName}\", out var mp)\n"
            "                                    || separateMeshes.TryGetValue($\"SeparateMesh_{rootName}_Model_{childName}\", out mp)\n"
            "                                    || separateMeshes.TryGetValue(childName, out mp))\n"
            "                                {\n"
            "                                    Logger.Info($\"Attached SeparateMesh fallback to {childName} under {rootName}\");\n"
            "                                    if (childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull)\n"
            "                                        childGO.m_SkinnedMeshRenderer.m_Mesh = mp;\n"
            "                                    else if (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)\n"
            "                                        childGO.m_MeshFilter.m_Mesh = mp;\n"
            "                                    attachedAnyLod0 = true;\n"
            "                                }\n"
            "                            }\n"
            "                        }\n\n"
            "                        if (attachedAnyLod0)\n"
            "                        {\n"
            "                            foreach (var go in allChildren)\n"
            "                            {\n"
            "                                var goName = go.Name;\n"
            "                                if (!string.IsNullOrEmpty(goName)\n"
            "                                    && (goName.EndsWith(\"_LOD3\", StringComparison.OrdinalIgnoreCase)\n"
            "                                        || goName.IndexOf(\"HairShadow\", StringComparison.OrdinalIgnoreCase) >= 0))\n"
            "                                {\n"
            "                                    go.m_SuppressExport = true;\n"
            "                                }\n"
            "                            }\n"
            "                        }\n"
            "                    }",
        ),
        (
            "                            if (!childAttached && childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull)\n"
            "                            {\n"
            "                                if (separateMeshes.TryGetValue($\"SeparateMesh_{rootName}_{childName}\", out var mp)\n"
            "                                    || separateMeshes.TryGetValue($\"SeparateMesh_{rootName}_Model_{childName}\", out mp))\n"
            "                                {\n"
            "                                    Logger.Info($\"Attached SeparateMesh fallback to {childName} under {rootName}\");\n"
            "                                    childGO.m_SkinnedMeshRenderer.m_Mesh = mp;\n"
            "                                    attachedAnyLod0 = true;\n"
            "                                }\n"
            "                            }",
            "                            if (!childAttached && ((childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull) || (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)))\n"
            "                            {\n"
            "                                if (separateMeshes.TryGetValue($\"SeparateMesh_{rootName}_{childName}\", out var mp)\n"
            "                                    || separateMeshes.TryGetValue($\"SeparateMesh_{rootName}_Model_{childName}\", out mp)\n"
            "                                    || separateMeshes.TryGetValue(childName, out mp))\n"
            "                                {\n"
            "                                    Logger.Info($\"Attached SeparateMesh fallback to {childName} under {rootName}\");\n"
            "                                    if (childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull)\n"
            "                                        childGO.m_SkinnedMeshRenderer.m_Mesh = mp;\n"
            "                                    else if (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)\n"
            "                                        childGO.m_MeshFilter.m_Mesh = mp;\n"
            "                                    attachedAnyLod0 = true;\n"
            "                                }\n"
            "                            }",
        ),
        (
            "                Logger.Info($\"Found {avatars.Count} Avatars\");\n"
            "                foreach (var avatar in avatars)\n"
            "                {\n"
            "                    var rootName = avatar.Name;\n"
            "                    Logger.Verbose($\"Attempting to process SeparateMesh for {rootName}\");\n\n\n"
            "                    if (avatar.m_Transform != null)\n"
            "                    {\n"
            "                        bool attachedAnyLod0 = false;\n"
            "                        var allChildren = new List<GameObject>();\n"
            "                        var visitedTransforms = new HashSet<Transform>();\n"
            "                        var stack = new Stack<(PPtr<Transform> ptr, int depth)>();\n"
            "                        for (int ci = avatar.m_Transform.m_Children.Count - 1; ci >= 0; ci--)\n"
            "                        {\n"
            "                            stack.Push((avatar.m_Transform.m_Children[ci], 1));\n"
            "                        }\n\n"
            "                        while (stack.Count > 0)\n"
            "                        {\n"
            "                            var (childPtr, depth) = stack.Pop();\n"
            "                            if (depth > 48 || !childPtr.TryGet(out var child) || !visitedTransforms.Add(child))\n"
            "                                continue;\n\n"
            "                            if (child.m_Children != null)\n"
            "                            {\n"
            "                                for (int ci = child.m_Children.Count - 1; ci >= 0; ci--)\n"
            "                                {\n"
            "                                    stack.Push((child.m_Children[ci], depth + 1));\n"
            "                                }\n"
            "                            }\n\n"
            "                            if (!child.m_GameObject.TryGet(out var childGO))\n"
            "                                continue;\n\n"
            "                            allChildren.Add(childGO);\n"
            "                            var childName = childGO.Name;\n"
            "                            bool childAttached = false;\n\n"
            "                            foreach (var i in childGO.m_Components)\n"
            "                            {\n"
            "                                if (i.TryGet<MonoBehaviour>(out var comp) && comp.Name == \"NapLodController\")\n"
            "                                {\n"
            "                                    var raw = comp.GetRawData();\n"
            "                                    string rawPath = raw != null ? System.Text.Encoding.UTF8.GetString(raw) : string.Empty;\n"
            "                                    if (string.IsNullOrEmpty(rawPath))\n"
            "                                        continue;\n\n"
            "                                    int assetIndex = rawPath.IndexOf(\"Assets\", StringComparison.Ordinal);\n"
            "                                    string trimmed;\n"
            "                                    if (assetIndex != -1)\n"
            "                                    {\n"
            "                                        trimmed = rawPath.Substring(assetIndex);\n"
            "                                    }\n"
            "                                    else if (rawPath.Length > 40)\n"
            "                                    {\n"
            "                                        trimmed = rawPath.Substring(40);\n"
            "                                    }\n"
            "                                    else\n"
            "                                    {\n"
            "                                        Logger.Verbose($\"NapLodController path too short ({rawPath.Length}), skipping\");\n"
            "                                        continue;\n"
            "                                    }\n\n"
            "                                    trimmed = trimmed?.Trim();\n"
            "                                    if (string.IsNullOrEmpty(trimmed))\n"
            "                                        continue;\n\n"
            "                                    int meshIndex = trimmed.IndexOf(\".mesh\", StringComparison.OrdinalIgnoreCase);\n"
            "                                    if (meshIndex <= 0)\n"
            "                                    {\n"
            "                                        Logger.Verbose($\".mesh not found in '{trimmed}', skipping\");\n"
            "                                        continue;\n"
            "                                    }\n\n"
            "                                    trimmed = trimmed.Substring(0, meshIndex);\n"
            "                                    int lastSlash = trimmed.LastIndexOf('/');\n"
            "                                    if (lastSlash >= 0 && lastSlash < trimmed.Length - 1)\n"
            "                                        trimmed = trimmed.Substring(lastSlash + 1);\n\n"
            "                                    trimmed = trimmed.Trim();\n"
            "                                    if (string.IsNullOrEmpty(trimmed))\n"
            "                                        continue;\n\n"
            "                                    if (separateMeshes.TryGetValue(trimmed, out var meshPPtr))\n"
            "                                    {\n"
            "                                        Logger.Verbose($\"Trying to attach {trimmed} to {childName}\");\n"
            "                                        if (childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull)\n"
            "                                        {\n"
            "                                            Logger.Info($\"Attached {trimmed} to {childName}\");\n"
            "                                            childGO.m_SkinnedMeshRenderer.m_Mesh = meshPPtr;\n"
            "                                            childAttached = true;\n"
            "                                            attachedAnyLod0 = true;\n"
            "                                        }\n"
            "                                        else if (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)\n"
            "                                        {\n"
            "                                            Logger.Info($\"Attached {trimmed} to {childName}\");\n"
            "                                            childGO.m_MeshFilter.m_Mesh = meshPPtr;\n"
            "                                            childAttached = true;\n"
            "                                            attachedAnyLod0 = true;\n"
            "                                        }\n"
            "                                    }\n"
            "                                    else if (separateMeshes.TryGetValue(childName, out meshPPtr))\n"
            "                                    {\n"
            "                                        Logger.Verbose($\"Trying to attach {childName} to {childName}\");\n"
            "                                        if (childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull)\n"
            "                                        {\n"
            "                                            Logger.Info($\"Attached {childName} to {childName}\");\n"
            "                                            childGO.m_SkinnedMeshRenderer.m_Mesh = meshPPtr;\n"
            "                                            childAttached = true;\n"
            "                                            attachedAnyLod0 = true;\n"
            "                                        }\n"
            "                                        else if (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)\n"
            "                                        {\n"
            "                                            Logger.Info($\"Attached {childName} to {childName}\");\n"
            "                                            childGO.m_MeshFilter.m_Mesh = meshPPtr;\n"
            "                                            childAttached = true;\n"
            "                                            attachedAnyLod0 = true;\n"
            "                                        }\n"
            "                                    }\n"
            "                                }\n"
            "                            }\n\n"
            "                            if (!childAttached && ((childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull) || (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)))\n"
            "                            {\n"
            "                                if (separateMeshes.TryGetValue($\"SeparateMesh_{rootName}_{childName}\", out var mp)\n"
            "                                    || separateMeshes.TryGetValue($\"SeparateMesh_{rootName}_Model_{childName}\", out mp)\n"
            "                                    || separateMeshes.TryGetValue(childName, out mp))\n"
            "                                {\n"
            "                                    Logger.Info($\"Attached SeparateMesh fallback to {childName} under {rootName}\");\n"
            "                                    if (childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull)\n"
            "                                        childGO.m_SkinnedMeshRenderer.m_Mesh = mp;\n"
            "                                    else if (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)\n"
            "                                        childGO.m_MeshFilter.m_Mesh = mp;\n"
            "                                    attachedAnyLod0 = true;\n"
            "                                }\n"
            "                            }\n"
            "                        }\n\n"
            "                        if (attachedAnyLod0)\n"
            "                        {\n"
            "                            foreach (var go in allChildren)\n"
            "                            {\n"
            "                                var goName = go.Name;\n"
            "                                if (!string.IsNullOrEmpty(goName)\n"
            "                                    && (goName.EndsWith(\"_LOD3\", StringComparison.OrdinalIgnoreCase)\n"
            "                                        || goName.IndexOf(\"HairShadow\", StringComparison.OrdinalIgnoreCase) >= 0))\n"
            "                                {\n"
            "                                    go.m_SuppressExport = true;\n"
            "                                }\n"
            "                            }\n"
            "                        }\n"
            "                    }\n"
            "                }",
            "                Logger.Info($\"Found {avatars.Count} Avatars\");\n"
            "                var pendingFallbacks = new List<(string rootName, GameObject childGO, string childName, List<GameObject> allChildren)>();\n"
            "                bool TryAttachSeparateMeshFallback(string rName, GameObject cGo, string cName)\n"
            "                {\n"
            "                    if ((cGo.m_SkinnedMeshRenderer != null && cGo.m_SkinnedMeshRenderer.m_Mesh.IsNull)\n"
            "                        || (cGo.m_MeshFilter != null && cGo.m_MeshFilter.m_Mesh.IsNull))\n"
            "                    {\n"
            "                        if (separateMeshes.TryGetValue($\"SeparateMesh_{rName}_{cName}\", out var mp)\n"
            "                            || separateMeshes.TryGetValue($\"SeparateMesh_{rName}_Model_{cName}\", out mp)\n"
            "                            || separateMeshes.TryGetValue(cName, out mp))\n"
            "                        {\n"
            "                            Logger.Info($\"Attached SeparateMesh fallback to {cName} under {rName}\");\n"
            "                            if (cGo.m_SkinnedMeshRenderer != null && cGo.m_SkinnedMeshRenderer.m_Mesh.IsNull)\n"
            "                                cGo.m_SkinnedMeshRenderer.m_Mesh = mp;\n"
            "                            else if (cGo.m_MeshFilter != null && cGo.m_MeshFilter.m_Mesh.IsNull)\n"
            "                                cGo.m_MeshFilter.m_Mesh = mp;\n"
            "                            return true;\n"
            "                        }\n"
            "                    }\n"
            "                    return false;\n"
            "                }\n"
            "                void SuppressLowerLodChildren(List<GameObject> children)\n"
            "                {\n"
            "                    foreach (var go in children)\n"
            "                    {\n"
            "                        var goName = go.Name;\n"
            "                        if (!string.IsNullOrEmpty(goName)\n"
            "                            && (goName.EndsWith(\"_LOD3\", StringComparison.OrdinalIgnoreCase)\n"
            "                                || goName.IndexOf(\"HairShadow\", StringComparison.OrdinalIgnoreCase) >= 0))\n"
            "                        {\n"
            "                            go.m_SuppressExport = true;\n"
            "                        }\n"
            "                    }\n"
            "                }\n"
            "                foreach (var avatar in avatars)\n"
            "                {\n"
            "                    var rootName = avatar.Name;\n"
            "                    Logger.Verbose($\"Attempting to process SeparateMesh for {rootName}\");\n\n\n"
            "                    if (avatar.m_Transform != null)\n"
            "                    {\n"
            "                        bool attachedAnyLod0 = false;\n"
            "                        var allChildren = new List<GameObject>();\n"
            "                        var visitedTransforms = new HashSet<Transform>();\n"
            "                        var stack = new Stack<(PPtr<Transform> ptr, int depth)>();\n"
            "                        for (int ci = avatar.m_Transform.m_Children.Count - 1; ci >= 0; ci--)\n"
            "                        {\n"
            "                            stack.Push((avatar.m_Transform.m_Children[ci], 1));\n"
            "                        }\n\n"
            "                        while (stack.Count > 0)\n"
            "                        {\n"
            "                            var (childPtr, depth) = stack.Pop();\n"
            "                            if (depth > 48 || !childPtr.TryGet(out var child) || !visitedTransforms.Add(child))\n"
            "                                continue;\n\n"
            "                            if (child.m_Children != null)\n"
            "                            {\n"
            "                                for (int ci = child.m_Children.Count - 1; ci >= 0; ci--)\n"
            "                                {\n"
            "                                    stack.Push((child.m_Children[ci], depth + 1));\n"
            "                                }\n"
            "                            }\n\n"
            "                            if (!child.m_GameObject.TryGet(out var childGO))\n"
            "                                continue;\n\n"
            "                            allChildren.Add(childGO);\n"
            "                            var childName = childGO.Name;\n"
            "                            bool childAttached = false;\n\n"
            "                            foreach (var i in childGO.m_Components)\n"
            "                            {\n"
            "                                if (i.TryGet<MonoBehaviour>(out var comp) && comp.Name == \"NapLodController\")\n"
            "                                {\n"
            "                                    var raw = comp.GetRawData();\n"
            "                                    string rawPath = raw != null ? System.Text.Encoding.UTF8.GetString(raw) : string.Empty;\n"
            "                                    if (string.IsNullOrEmpty(rawPath))\n"
            "                                        continue;\n\n"
            "                                    int assetIndex = rawPath.IndexOf(\"Assets\", StringComparison.Ordinal);\n"
            "                                    string trimmed;\n"
            "                                    if (assetIndex != -1)\n"
            "                                    {\n"
            "                                        trimmed = rawPath.Substring(assetIndex);\n"
            "                                    }\n"
            "                                    else if (rawPath.Length > 40)\n"
            "                                    {\n"
            "                                        trimmed = rawPath.Substring(40);\n"
            "                                    }\n"
            "                                    else\n"
            "                                    {\n"
            "                                        Logger.Verbose($\"NapLodController path too short ({rawPath.Length}), skipping\");\n"
            "                                        continue;\n"
            "                                    }\n\n"
            "                                    trimmed = trimmed?.Trim();\n"
            "                                    if (string.IsNullOrEmpty(trimmed))\n"
            "                                        continue;\n\n"
            "                                    int meshIndex = trimmed.IndexOf(\".mesh\", StringComparison.OrdinalIgnoreCase);\n"
            "                                    if (meshIndex <= 0)\n"
            "                                    {\n"
            "                                        Logger.Verbose($\".mesh not found in '{trimmed}', skipping\");\n"
            "                                        continue;\n"
            "                                    }\n\n"
            "                                    trimmed = trimmed.Substring(0, meshIndex);\n"
            "                                    string lodFolder = string.Empty;\n"
            "                                    int lastSlash = trimmed.LastIndexOf('/');\n"
            "                                    if (lastSlash >= 0 && lastSlash < trimmed.Length - 1)\n"
            "                                    {\n"
            "                                        int prevSlash = lastSlash > 0 ? trimmed.LastIndexOf('/', lastSlash - 1) : -1;\n"
            "                                        lodFolder = (prevSlash >= 0 ? trimmed.Substring(prevSlash + 1, lastSlash - prevSlash - 1) : trimmed.Substring(0, lastSlash)).Trim();\n"
            "                                        trimmed = trimmed.Substring(lastSlash + 1);\n"
            "                                    }\n\n"
            "                                    trimmed = trimmed.Trim();\n"
            "                                    if (string.IsNullOrEmpty(trimmed))\n"
            "                                        continue;\n\n"
            "                                    if (separateMeshes.TryGetValue(trimmed, out var meshPPtr))\n"
            "                                    {\n"
            "                                        Logger.Verbose($\"Trying to attach {trimmed} to {childName}\");\n"
            "                                        bool attachedNow = false;\n"
            "                                        if (childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull)\n"
            "                                        {\n"
            "                                            Logger.Info($\"Attached {trimmed} to {childName}\");\n"
            "                                            childGO.m_SkinnedMeshRenderer.m_Mesh = meshPPtr;\n"
            "                                            attachedNow = true;\n"
            "                                        }\n"
            "                                        else if (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)\n"
            "                                        {\n"
            "                                            Logger.Info($\"Attached {trimmed} to {childName}\");\n"
            "                                            childGO.m_MeshFilter.m_Mesh = meshPPtr;\n"
            "                                            attachedNow = true;\n"
            "                                        }\n"
            "                                        if (attachedNow)\n"
            "                                        {\n"
            "                                            if (!childAttached)\n"
            "                                            {\n"
            "                                                if (!string.IsNullOrEmpty(lodFolder))\n"
            "                                                    separateMeshes.TryAdd($\"SeparateMesh_{lodFolder}_{childName}\", meshPPtr);\n"
            "                                                if (!string.IsNullOrEmpty(rootName))\n"
            "                                                    separateMeshes.TryAdd($\"SeparateMesh_{rootName}_{childName}\", meshPPtr);\n"
            "                                                if (!string.IsNullOrEmpty(childName))\n"
            "                                                    separateMeshes.TryAdd(childName, meshPPtr);\n"
            "                                            }\n"
            "                                            childAttached = true;\n"
            "                                            attachedAnyLod0 = true;\n"
            "                                        }\n"
            "                                    }\n"
            "                                    else if (separateMeshes.TryGetValue(childName, out meshPPtr))\n"
            "                                    {\n"
            "                                        Logger.Verbose($\"Trying to attach {childName} to {childName}\");\n"
            "                                        if (childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull)\n"
            "                                        {\n"
            "                                            Logger.Info($\"Attached {childName} to {childName}\");\n"
            "                                            childGO.m_SkinnedMeshRenderer.m_Mesh = meshPPtr;\n"
            "                                            childAttached = true;\n"
            "                                            attachedAnyLod0 = true;\n"
            "                                        }\n"
            "                                        else if (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)\n"
            "                                        {\n"
            "                                            Logger.Info($\"Attached {childName} to {childName}\");\n"
            "                                            childGO.m_MeshFilter.m_Mesh = meshPPtr;\n"
            "                                            childAttached = true;\n"
            "                                            attachedAnyLod0 = true;\n"
            "                                        }\n"
            "                                    }\n"
            "                                }\n"
            "                            }\n\n"
            "                            if (!childAttached && ((childGO.m_SkinnedMeshRenderer != null && childGO.m_SkinnedMeshRenderer.m_Mesh.IsNull) || (childGO.m_MeshFilter != null && childGO.m_MeshFilter.m_Mesh.IsNull)))\n"
            "                            {\n"
            "                                if (TryAttachSeparateMeshFallback(rootName, childGO, childName))\n"
            "                                    attachedAnyLod0 = true;\n"
            "                                else\n"
            "                                    pendingFallbacks.Add((rootName, childGO, childName, allChildren));\n"
            "                            }\n"
            "                        }\n\n"
            "                        if (attachedAnyLod0)\n"
            "                            SuppressLowerLodChildren(allChildren);\n"
            "                    }\n"
            "                }\n"
            "                foreach (var (pRootName, pChildGO, pChildName, pAllChildren) in pendingFallbacks)\n"
            "                {\n"
            "                    if (TryAttachSeparateMeshFallback(pRootName, pChildGO, pChildName))\n"
            "                        SuppressLowerLodChildren(pAllChildren);\n"
            "                }",
        ),
    ])

    _replace_in_file("AnimeStudio/SerializedFile.cs", [
        (
            "        public void AddObject(Object obj)\n"
            "        {\n"
            "            Logger.Verbose($\"Caching object with {obj.m_PathID} in file {fileName}...\");\n"
            "            Objects.Add(obj);\n"
            "            ObjectsDic.Add(obj.m_PathID, obj);\n"
            "        }",
            "        public void AddObject(Object obj)\n"
            "        {\n"
            "            Logger.Verbose($\"Caching object with {obj.m_PathID} in file {fileName}...\");\n"
            "            if (ObjectsDic.TryAdd(obj.m_PathID, obj))\n"
            "                Objects.Add(obj);\n"
            "        }",
        ),
    ])

    _replace_in_file("AnimeStudio/Classes/Avatar.cs", [
        (
            "                m_TOS.Add(reader.ReadUInt32(), reader.ReadAlignedString());",
            "                var tosKey = reader.ReadUInt32();\n"
            "                var tosVal = reader.ReadAlignedString();\n"
            "                m_TOS[tosKey] = tosVal;",
        ),
    ])

    _replace_in_file("AnimeStudio/Classes/NapAssetBundleIndexAsset.cs", [
        (
            "        public NapAssetBundleIndexAsset(ObjectReader reader) : base(reader)\n"
            "        {\n"
            "            var m_AssetArraySize = reader.ReadInt32();",
            "        public NapAssetBundleIndexAsset(ObjectReader reader) : base(reader)\n"
            "        {\n"
            "            m_AssetArray = new List<IndexAssetRef>();\n"
            "            m_BundleArray = new List<IndexBundleRef>();\n"
            "            m_BlockArray = new List<IndexBlockRef>();\n"
            "            m_ChildrenIndexArray = new List<uint>();\n"
            "            try\n"
            "            {\n"
            "                var m_AssetArraySize = reader.ReadInt32();",
        ),
        (
            "            for (int i = 0; i < m_ChildrenIndexArraySize; i++)\n"
            "                m_ChildrenIndexArray.Add(reader.ReadUInt32());\n"
            "        }",
            "                for (int i = 0; i < m_ChildrenIndexArraySize; i++)\n"
            "                    m_ChildrenIndexArray.Add(reader.ReadUInt32());\n"
            "            }\n"
            "            catch { }\n"
            "        }",
        ),
        (
            "                childrenStartIndex = reader.ReadUInt32();\n"
            "                childrenEndIndex = reader.ReadUInt32();\n"
            "                fileSize = reader.ReadUInt32();",
            "                childrenStartIndex = reader.ReadUInt32();\n"
            "                childrenEndIndex = reader.ReadUInt32();\n"
            "                reader.ReadUInt32();\n"
            "                reader.ReadUInt32();\n"
            "                fileSize = reader.ReadUInt32();",
        ),
    ])

    studio_cs = studio_dir / "AnimeStudio.CLI" / "Studio.cs"
    if studio_cs.is_file():
        stxt = studio_cs.read_text(encoding="utf-8", errors="replace")
        m_oz = re.search(
            r"        private static readonly Regex OzPrefabDirRegex = new Regex\(.*?\n        \}\n(?=        public static AssemblyLoader assemblyLoader)",
            stxt,
            flags=re.DOTALL,
        )
        if m_oz and m_oz.group(0) != _OZ_DUMP_CS + "\n":
            studio_cs.write_text(
                stxt[: m_oz.start()] + _OZ_DUMP_CS + "\n" + stxt[m_oz.end() :],
                encoding="utf-8",
            )
            _emit(log_cb, "Upgraded DumpOzIndexForLoadedFiles in AnimeStudio.CLI/Studio.cs")
            changed = True

    return changed


def ensure_adb(log_cb=None, progress_cb=None, cancel_event: threading.Event | None = None) -> str | None:
    """Install portable Google Android Platform-Tools (`adb`) into TOOLS_DIR if missing."""
    existing = cfg.find_adb()
    if existing:
        return existing

    if cfg.is_windows():
        url = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
        exe_name = "adb.exe"
    elif cfg.is_macos():
        url = "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"
        exe_name = "adb"
    else:
        url = "https://dl.google.com/android/repository/platform-tools-latest-linux.zip"
        exe_name = "adb"

    cfg.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_zip = cfg.TOOLS_DIR / "_platform_tools_tmp.zip"
    try:
        _download_url(
            url, tmp_zip, log_cb=log_cb, progress_cb=progress_cb,
            cancel_event=cancel_event, label="Android Platform-Tools (ADB)",
        )
        _emit(log_cb, f"Extracting ADB to {cfg.TOOLS_DIR / 'platform-tools'}...")
        _safe_extract_zip(tmp_zip, cfg.TOOLS_DIR, strip_top_dir=False)
        adb_bin = cfg.TOOLS_DIR / "platform-tools" / exe_name
        _chmod_exec(adb_bin)
        _chmod_exec(cfg.TOOLS_DIR / "platform-tools" / "fastboot")
        if adb_bin.is_file():
            try:
                res = subprocess.run(
                    [str(adb_bin), "version"], capture_output=True, text=True,
                    timeout=10, **cfg.subprocess_no_window_kwargs(),
                )
                first_ln = (res.stdout or "").strip().splitlines()[0] if res.stdout else ""
                _emit(log_cb, f"ADB ready: {adb_bin} ({first_ln})")
            except Exception as exc:
                _emit(log_cb, f"ADB installed at {adb_bin} (version check note: {exc})")
            return str(adb_bin)
        _emit(log_cb, "[WARN] ADB archive extracted, but adb executable was not found.")
        return cfg.find_adb()
    finally:
        tmp_zip.unlink(missing_ok=True)


def ensure_vgmstream(log_cb=None, progress_cb=None, cancel_event: threading.Event | None = None) -> Path | None:
    """Install portable vgmstream-cli release binary into TOOLS_DIR/vgmstream if missing."""
    existing = cfg.find_vgmstream()
    if existing and existing.is_file():
        return existing

    if cfg.is_windows():
        url = "https://github.com/vgmstream/vgmstream/releases/latest/download/vgmstream-win64.zip"
        exe_name = "vgmstream-cli.exe"
    elif cfg.is_macos():
        url = "https://github.com/vgmstream/vgmstream/releases/latest/download/vgmstream-mac.zip"
        exe_name = "vgmstream-cli"
    else:
        url = "https://github.com/vgmstream/vgmstream/releases/latest/download/vgmstream-linux.zip"
        exe_name = "vgmstream-cli"

    dest_dir = cfg.TOOLS_DIR / "vgmstream"
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp_zip = cfg.TOOLS_DIR / "_vgmstream_tmp.zip"
    try:
        _download_url(
            url, tmp_zip, log_cb=log_cb, progress_cb=progress_cb,
            cancel_event=cancel_event, label="vgmstream-cli",
        )
        _emit(log_cb, f"Extracting vgmstream-cli to {dest_dir}...")
        _safe_extract_zip(tmp_zip, dest_dir, strip_top_dir=False)
        exe_path = dest_dir / exe_name
        _chmod_exec(exe_path)
        cfg._resolve_tool_paths()
        if exe_path.is_file():
            _emit(log_cb, f"vgmstream-cli ready: {exe_path}")
            return exe_path
        found = cfg.find_vgmstream()
        if found:
            _chmod_exec(found)
        return found
    finally:
        tmp_zip.unlink(missing_ok=True)


def _has_dotnet9_sdk(dotnet_bin: str) -> bool:
    try:
        res = subprocess.run(
            [dotnet_bin, "--list-sdks"], capture_output=True, text=True,
            timeout=15, **cfg.subprocess_no_window_kwargs(),
        )
        return any(line.strip().startswith("9.") for line in (res.stdout or "").splitlines())
    except Exception:
        return False


def ensure_runtime_python_packages(log_cb=None) -> bool:
    """Verify required runtime Python dependencies and auto-install any missing packages via pip."""
    reqs = [
        ("PySide6", "PySide6>=6.6.0"),
        ("PIL", "Pillow>=10.0.0"),
        ("numpy", "numpy>=1.24.0"),
        ("OpenGL", "PyOpenGL>=3.1.6"),
        ("pygltflib", "pygltflib>=1.16.0"),
    ]
    if sys.version_info < (3, 14):
        reqs.append(("zstandard", "zstandard>=0.22.0"))
    missing: list[str] = []
    for imp_name, pkg_spec in reqs:
        try:
            __import__(imp_name)
        except Exception:
            missing.append(pkg_spec)
    if not missing:
        return True
    _emit(log_cb, f"Installing missing Python package(s): {', '.join(missing)}")
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", *missing]
    rc = _stream_cmd(cmd, log_cb=log_cb)
    if rc != 0:
        _emit(log_cb, f"Retrying pip install with --no-cache-dir (previous rc={rc})...")
        rc = _stream_cmd([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--quiet", *missing], log_cb=log_cb)
    return rc == 0


def ensure_dotnet(log_cb=None, progress_cb=None, cancel_event: threading.Event | None = None) -> str | None:
    """Install portable .NET 9.0 SDK into TOOLS_DIR/dotnet if no .NET 9 SDK is present."""
    existing = cfg.find_dotnet()
    if existing and _has_dotnet9_sdk(existing):
        return existing

    dotnet_dir = cfg.TOOLS_DIR / "dotnet"
    dotnet_dir.mkdir(parents=True, exist_ok=True)
    if cfg.is_windows():
        script_url = "https://dot.net/v1/dotnet-install.ps1"
        script_path = cfg.TOOLS_DIR / "_dotnet-install.ps1"
        exe_name = "dotnet.exe"
    else:
        script_url = "https://dot.net/v1/dotnet-install.sh"
        script_path = cfg.TOOLS_DIR / "_dotnet-install.sh"
        exe_name = "dotnet"

    try:
        _download_url(
            script_url, script_path, log_cb=log_cb, progress_cb=progress_cb,
            cancel_event=cancel_event, label=".NET 9 Installer",
        )
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Cancelled .NET 9 installation.")
        _emit(log_cb, f"Installing portable .NET 9.0 SDK to {dotnet_dir}...")
        cmd_env = cfg.build_cli_env()
        if cfg.is_windows():
            ps = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
            cmd_env = {**cmd_env, "OZ_DOTNET_SCRIPT": str(script_path), "OZ_DOTNET_DIR": str(dotnet_dir)}
            cmd = [
                ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
                "& $env:OZ_DOTNET_SCRIPT -Channel 9.0 -InstallDir $env:OZ_DOTNET_DIR",
            ]
        else:
            _chmod_exec(script_path)
            cmd = ["bash", str(script_path), "--channel", "9.0", "--install-dir", str(dotnet_dir)]
        rc = _stream_cmd(cmd, log_cb=log_cb, env=cmd_env)
        if rc != 0:
            _emit(log_cb, f"[WARN] .NET 9 installer exited with code {rc}.")
        exe_path = dotnet_dir / exe_name
        _chmod_exec(exe_path)
        if exe_path.is_file():
            _emit(log_cb, f".NET 9 SDK ready: {exe_path}")
            return str(exe_path)
        return cfg.find_dotnet()
    finally:
        script_path.unlink(missing_ok=True)


def ensure_wine(log_cb=None, progress_cb=None, cancel_event: threading.Event | None = None) -> str | None:
    """Install portable WoW64 Wine on Linux x86_64 into TOOLS_DIR/wine if Wine is missing."""
    if cfg.is_windows():
        return None
    existing = cfg.find_wine()
    if existing:
        return existing

    arch = platform.machine().lower()
    if not (cfg.is_linux() and arch in ("x86_64", "amd64")):
        hint = "brew install --cask wine-stable" if cfg.is_macos() else "Install Wine via your distribution package manager."
        _emit(log_cb, f"[INFO] Portable auto-Wine is only available on Linux x86_64 ({cfg.is_linux()}/{arch}). {hint}")
        return None

    _emit(log_cb, "Querying latest Kron4ek portable WoW64 Wine release...")
    req = urllib.request.Request(
        "https://api.github.com/repos/Kron4ek/Wine-Builds/releases/latest",
        headers={"User-Agent": "OpenZenless/0.3"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        rel = json.loads(resp.read().decode("utf-8", errors="replace"))
    assets = rel.get("assets") or []
    preferred = [a for a in assets if re.match(r"^wine-[^-]+-amd64-wow64\.tar\.xz$", a.get("name", ""))]
    if not preferred:
        preferred = [a for a in assets if a.get("name", "").endswith("amd64-wow64.tar.xz")]
    if not preferred:
        _emit(log_cb, "[WARN] Could not find an amd64-wow64 Wine build in Kron4ek releases.")
        return None

    asset = preferred[0]
    wine_dir = cfg.TOOLS_DIR / "wine"
    tmp_tar = cfg.TOOLS_DIR / f"_{asset['name']}"
    try:
        _download_url(
            asset["browser_download_url"], tmp_tar, log_cb=log_cb,
            progress_cb=progress_cb, cancel_event=cancel_event, label=f"Portable {asset['name']}",
        )
        if wine_dir.exists():
            shutil.rmtree(wine_dir, ignore_errors=True)
        _emit(log_cb, f"Extracting Wine into {wine_dir} (this may take a moment)...")
        _safe_extract_tar(tmp_tar, wine_dir, strip_top_dir=True)
        for bname in ("wine", "wine64", "wineserver", "wineboot", "winecfg"):
            _chmod_exec(wine_dir / "bin" / bname)
        wine_bin = wine_dir / "bin" / "wine"
        if not wine_bin.is_file():
            wine_bin = wine_dir / "bin" / "wine64"
        if wine_bin.is_file():
            try:
                res = subprocess.run(
                    [str(wine_bin), "--version"], env=cfg.build_cli_env(),
                    capture_output=True, text=True, timeout=15,
                    **cfg.subprocess_no_window_kwargs(),
                )
                _emit(log_cb, f"Portable Wine ready: {wine_bin} ({(res.stdout or '').strip()})")
            except Exception as exc:
                _emit(log_cb, f"Portable Wine extracted to {wine_bin} ({exc})")
            return str(wine_bin)
        return cfg.find_wine()
    finally:
        tmp_tar.unlink(missing_ok=True)


def ensure_blender(log_cb=None, progress_cb=None, cancel_event: threading.Event | None = None) -> str | None:
    """Install portable Blender 4.2 LTS into TOOLS_DIR/blender on Linux x64 / Windows x64."""
    existing = cfg.find_blender()
    if existing:
        return existing

    arch = platform.machine().lower()
    if arch not in ("x86_64", "amd64"):
        _emit(log_cb, f"[INFO] Portable Blender auto-install targets x86_64/amd64 (got {arch}). Install Blender from blender.org.")
        return None
    if cfg.is_macos():
        _emit(log_cb, "[INFO] On macOS, install Blender from https://www.blender.org/download/ or `brew install --cask blender`.")
        return None

    idx_url = "https://download.blender.org/release/Blender4.2/"
    req = urllib.request.Request(idx_url, headers={"User-Agent": "OpenZenless/0.3"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    if cfg.is_windows():
        matches = sorted(set(re.findall(r"blender-4\.2\.\d+-windows-x64\.zip", html)))
        exe_name = "blender.exe"
    else:
        matches = sorted(set(re.findall(r"blender-4\.2\.\d+-linux-x64\.tar\.xz", html)))
        exe_name = "blender"

    if not matches:
        _emit(log_cb, "[WARN] Could not locate Blender 4.2 LTS archive on download.blender.org.")
        return None

    fname = matches[-1]
    bl_dir = cfg.TOOLS_DIR / "blender"
    tmp_arch = cfg.TOOLS_DIR / f"_{fname}"
    try:
        _download_url(
            idx_url + fname, tmp_arch, log_cb=log_cb,
            progress_cb=progress_cb, cancel_event=cancel_event, label=fname,
        )
        if bl_dir.exists():
            shutil.rmtree(bl_dir, ignore_errors=True)
        _emit(log_cb, f"Extracting {fname} into {bl_dir}...")
        if fname.endswith(".zip"):
            _safe_extract_zip(tmp_arch, bl_dir, strip_top_dir=True)
        else:
            _safe_extract_tar(tmp_arch, bl_dir, strip_top_dir=True)
        exe_path = bl_dir / exe_name
        _chmod_exec(exe_path)
        if exe_path.is_file():
            _emit(log_cb, f"Blender ready: {exe_path}")
            return str(exe_path)
        return cfg.find_blender()
    finally:
        tmp_arch.unlink(missing_ok=True)


def _fetch_github_repo(
    repo: str,
    dest: Path,
    branch: str = "master",
    log_cb=None,
    progress_cb=None,
    cancel_event: threading.Event | None = None,
) -> bool:
    """Clone via `git` or fall back to GitHub archive ZIP download when `git` is unavailable."""
    if dest.exists() and any(dest.iterdir()):
        return True
    git = shutil.which("git")
    url_git = f"https://github.com/{repo}.git"
    if git:
        rc = _stream_cmd([git, "clone", "--depth", "1", url_git, str(dest)], log_cb=log_cb)
        if rc == 0 and dest.exists():
            return True
    zip_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
    tmp_zip = cfg.TOOLS_DIR / f"_{dest.name}_src.zip"
    try:
        _download_url(
            zip_url, tmp_zip, log_cb=log_cb, progress_cb=progress_cb,
            cancel_event=cancel_event, label=f"{repo} ({branch})",
        )
        _emit(log_cb, f"Extracting {repo} source to {dest}...")
        _safe_extract_zip(tmp_zip, dest, strip_top_dir=True)
        return dest.is_dir() and any(dest.iterdir())
    finally:
        tmp_zip.unlink(missing_ok=True)


def _prep_animestudio_projects(as_dir: Path) -> None:
    """Strip net10.0 TFMs and <SelfContained>false</SelfContained> so .NET 9 self-contained publish succeeds."""
    for csproj in as_dir.rglob("*.csproj"):
        txt = csproj.read_text(encoding="utf-8", errors="ignore")
        new_txt = txt.replace(";net10.0-windows", "").replace(";net10.0", "")
        new_txt = re.sub(r"\s*<SelfContained>false</SelfContained>\s*", "\n", new_txt)
        if new_txt != txt:
            csproj.write_text(new_txt, encoding="utf-8")


def _build_animestudio_cli(as_dir: Path, dotnet: str, log_cb=None, progress_cb=None) -> bool:
    """Patch, publish/build AnimeStudio.CLI for win-x64, and compile AnimeStudio.ACL.ZZZV2.dll if mingw exists."""
    _patch_animestudio_cli(as_dir, log_cb=log_cb)
    _prep_animestudio_projects(as_dir)

    csproj = as_dir / "AnimeStudio.CLI" / "AnimeStudio.CLI.csproj"
    if not csproj.is_file():
        return False
    env = cfg.build_cli_env({"DOTNET_ROOT": str(Path(dotnet).parent)})
    bin_base = as_dir / "AnimeStudio.CLI" / "bin" / "Release" / "net9.0-windows"
    pub_dir = bin_base / "publish"

    if progress_cb:
        progress_cb(45, "Publishing AnimeStudio.CLI (self-contained win-x64)...")
    _emit(log_cb, "Publishing AnimeStudio.CLI (net9.0-windows self-contained win-x64)...")
    rc = _stream_cmd(
        [
            dotnet, "publish", str(csproj),
            "-c", "Release", "-f", "net9.0-windows", "-r", "win-x64",
            "--self-contained", "true", "-p:EnableWindowsTargeting=true",
            "-o", str(pub_dir),
        ],
        log_cb=log_cb,
        env=env,
    )
    if rc != 0 or not (pub_dir / "AnimeStudio.CLI.exe").is_file():
        _emit(log_cb, "Self-contained publish incomplete; falling back to `dotnet build`...")
        rc = _stream_cmd(
            [dotnet, "build", str(csproj), "-c", "Release", "-f", "net9.0-windows", "-p:EnableWindowsTargeting=true"],
            log_cb=log_cb,
            env=env,
        )

    mingw = shutil.which("x86_64-w64-mingw32-g++") or (shutil.which("g++") if cfg.is_windows() else None)
    zzzv2_src = as_dir / "AnimeStudio.ACL" / "AnimeStudio.ACL.ZZZV2"
    acl_dst_dirs = (
        pub_dir,
        bin_base,
        as_dir / "AnimeStudio.Libraries",
        as_dir / "AnimeStudio.Utility" / "bin" / "Release" / "net9.0",
    )
    copied_acl = False
    if mingw and (zzzv2_src / "ACLLibrary.cpp").is_file():
        if progress_cb:
            progress_cb(65, "Building AnimeStudio.ACL.ZZZV2.dll (mingw-w64)...")
        dll_tmp = cfg.TOOLS_DIR / "_AnimeStudio.ACL.ZZZV2.dll"
        rc_dll = _stream_cmd(
            [
                mingw, "-std=c++20", "-O2", "-shared", f"-I{zzzv2_src}",
                "-DNDEBUG", "-D_AS_DLL", "-DACLZZZV2_EXPORTS", "-D_WINDOWS", "-D_USRDLL", "-w",
                str(zzzv2_src / "dllmain.cpp"), str(zzzv2_src / "ACLLibrary.cpp"),
                "-o", str(dll_tmp), "-static", "-static-libgcc", "-static-libstdc++",
            ],
            log_cb=log_cb,
            env=env,
        )
        if rc_dll == 0 and dll_tmp.is_file():
            for dst_dir in acl_dst_dirs:
                if dst_dir.is_dir():
                    shutil.copy2(dll_tmp, dst_dir / "AnimeStudio.ACL.ZZZV2.dll")
            dll_tmp.unlink(missing_ok=True)
            copied_acl = True
    bundled_acl = cfg.ASSETS_DIR / "win64" / "AnimeStudio.ACL.ZZZV2.dll"
    if not copied_acl and bundled_acl.is_file():
        _emit(log_cb, "Installing prebuilt AnimeStudio.ACL.ZZZV2.dll...")
        for dst_dir in acl_dst_dirs:
            if dst_dir.is_dir():
                shutil.copy2(bundled_acl, dst_dir / "AnimeStudio.ACL.ZZZV2.dll")

    # Preserve previously generated CAB Maps (e.g. ZZZ.bin) when upgrading from non-publish to publish layout
    if (
        pub_dir.is_dir()
        and (bin_base / "Maps" / "ZZZ.bin").is_file()
        and not (pub_dir / "Maps" / "ZZZ.bin").is_file()
    ):
        _emit(log_cb, "Preserving existing Maps cache in publish directory...")
        shutil.copytree(bin_base / "Maps", pub_dir / "Maps", dirs_exist_ok=True)

    cfg._resolve_tool_paths()
    return cfg.CLI_EXE.is_file()


def ensure_extractor_tools(
    log_cb=None,
    progress_cb=None,
    install_adb: bool = False,
    install_blender: bool = False,
    install_wine: bool = True,
    install_vgmstream: bool = True,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Install/build portable AnimeStudio.CLI, AnimeWwise, Wine, vgmstream, ADB, and Blender into TOOLS_DIR."""
    cfg.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    cfg._resolve_tool_paths()

    def _prog(pct: int, msg: str):
        if progress_cb is not None:
            progress_cb(pct, msg)
        _emit(log_cb, msg)

    if install_wine and not cfg.is_windows() and not cfg.find_wine():
        _prog(5, "Installing portable Wine runtime...")
        try:
            ensure_wine(log_cb=log_cb, progress_cb=progress_cb, cancel_event=cancel_event)
        except Exception as exc:
            _emit(log_cb, f"[WARN] Wine auto-install failed: {exc}")

    if install_vgmstream and not cfg.find_vgmstream():
        _prog(15, "Installing portable vgmstream-cli...")
        try:
            ensure_vgmstream(log_cb=log_cb, progress_cb=progress_cb, cancel_event=cancel_event)
        except Exception as exc:
            _emit(log_cb, f"[WARN] vgmstream auto-install failed: {exc}")

    if install_adb and not cfg.find_adb():
        _prog(22, "Installing portable Android Platform-Tools (ADB)...")
        try:
            ensure_adb(log_cb=log_cb, progress_cb=progress_cb, cancel_event=cancel_event)
        except Exception as exc:
            _emit(log_cb, f"[WARN] ADB auto-install failed: {exc}")

    if install_blender and not cfg.find_blender():
        _prog(28, "Installing portable Blender 4.2 LTS...")
        try:
            ensure_blender(log_cb=log_cb, progress_cb=progress_cb, cancel_event=cancel_event)
        except Exception as exc:
            _emit(log_cb, f"[WARN] Blender auto-install failed: {exc}")

    as_dir = cfg.TOOLS_DIR / "AnimeStudio"
    if not cfg.CLI_EXE.is_file():
        _prog(32, "Fetching Escartem/AnimeStudio source...")
        if _fetch_github_repo("Escartem/AnimeStudio", as_dir, branch="master", log_cb=log_cb, progress_cb=progress_cb, cancel_event=cancel_event):
            dotnet = ensure_dotnet(log_cb=log_cb, progress_cb=progress_cb, cancel_event=cancel_event)
            if dotnet:
                _build_animestudio_cli(as_dir, dotnet, log_cb=log_cb, progress_cb=progress_cb)
            else:
                _emit(log_cb, "[WARN] .NET 9 SDK could not be installed; AnimeStudio.CLI build skipped.")
    elif as_dir.is_dir() and _patch_animestudio_cli(as_dir, log_cb=log_cb):
        dotnet = ensure_dotnet(log_cb=log_cb, progress_cb=progress_cb, cancel_event=cancel_event)
        if dotnet:
            _emit(log_cb, "Rebuilding AnimeStudio.CLI after applying OpenZenless patches...")
            _build_animestudio_cli(as_dir, dotnet, log_cb=log_cb, progress_cb=progress_cb)

    bundled_acl = cfg.ASSETS_DIR / "win64" / "AnimeStudio.ACL.ZZZV2.dll"
    if cfg.CLI_EXE.is_file() and bundled_acl.is_file() and not (cfg.CLI_DIR / "AnimeStudio.ACL.ZZZV2.dll").is_file():
        try:
            shutil.copy2(bundled_acl, cfg.CLI_DIR / "AnimeStudio.ACL.ZZZV2.dll")
            _emit(log_cb, f"Copied prebuilt AnimeStudio.ACL.ZZZV2.dll -> {cfg.CLI_DIR}")
        except Exception as exc:
            _emit(log_cb, f"[WARN] Could not copy AnimeStudio.ACL.ZZZV2.dll: {exc}")

    if not cfg.NAP_MAP_FILE.is_file():
        ww_dir = cfg.TOOLS_DIR / "AnimeWwise"
        _prog(80, "Fetching Escartem/AnimeWwise (nap.map)...")
        try:
            _fetch_github_repo("Escartem/AnimeWwise", ww_dir, branch="master", log_cb=log_cb, progress_cb=progress_cb, cancel_event=cancel_event)
        except Exception as exc:
            _emit(log_cb, f"[WARN] AnimeWwise fetch failed: {exc}")
        cfg._resolve_tool_paths()

    _prog(100, "Tool check finished.")
    return {
        "cli_exists": cfg.CLI_EXE.is_file(),
        "cli_path": str(cfg.CLI_EXE),
        "nap_map_exists": cfg.NAP_MAP_FILE.is_file(),
        "nap_map_path": str(cfg.NAP_MAP_FILE),
        "wine_exists": True if cfg.is_windows() else (cfg.find_wine() is not None),
        "runtime_ok": True if cfg.is_windows() else (cfg.find_wine() is not None),
        "adb_exists": cfg.find_adb() is not None,
        "adb_path": cfg.find_adb() or "",
        "vgmstream_exists": cfg.VGMSTREAM_CLI.is_file(),
        "vgmstream_path": str(cfg.VGMSTREAM_CLI) if cfg.VGMSTREAM_CLI.is_file() else "",
        "blender_exists": cfg.find_blender() is not None,
        "blender_path": cfg.find_blender() or "",
        "dotnet_exists": cfg.find_dotnet() is not None,
    }


def _detect_linux_distro() -> str:
    os_rel = Path("/etc/os-release")
    if os_rel.is_file():
        for line in os_rel.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    return "Linux"


def _detect_pkg_manager() -> str:
    if cfg.is_windows():
        return "winget" if shutil.which("winget") else "portable"
    if cfg.is_macos():
        return "brew" if shutil.which("brew") else "portable"
    for pm in ("pacman", "apt-get", "dnf", "zypper", "apk"):
        if shutil.which(pm):
            return pm
    return "portable"


def get_environment_status() -> dict:
    """Collect OS, Python module, and external tool diagnostics for CLI and GUI."""
    py_mods = {}
    for mod_name, imp_name in (
        ("PySide6", "PySide6"),
        ("Pillow", "PIL"),
        ("numpy", "numpy"),
        ("PyOpenGL", "OpenGL"),
        ("pygltflib", "pygltflib"),
    ):
        try:
            __import__(imp_name)
            py_mods[mod_name] = True
        except Exception:
            py_mods[mod_name] = False
    has_zstd = False
    for zm in ("compression.zstd", "zstandard"):
        try:
            __import__(zm)
            has_zstd = True
            break
        except Exception:
            pass
    py_mods["zstd"] = has_zstd or (shutil.which("zstd") is not None)

    wine_path = cfg.find_wine()
    adb_path = cfg.find_adb()
    dotnet_path = cfg.find_dotnet()
    blender_path = cfg.find_blender()
    vgm_path = cfg.find_vgmstream()
    arch = platform.machine().lower()
    if cfg.is_windows():
        display = "Windows Desktop"
    elif cfg.is_macos():
        display = "macOS Cocoa"
    elif os.environ.get("WAYLAND_DISPLAY"):
        qpa = os.environ.get("QT_QPA_PLATFORM", "auto")
        display = f"Wayland (QT_QPA_PLATFORM={qpa})"
    elif os.environ.get("DISPLAY"):
        display = f"X11 ({os.environ.get('DISPLAY')})"
    else:
        display = "Headless"

    pm = _detect_pkg_manager()
    wine_hint = {
        "pacman": "Auto-installs Wine WoW64 (x64) or: sudo pacman -S wine",
        "apt-get": "Auto-installs Wine WoW64 (x64) or: sudo apt install wine",
        "dnf": "Auto-installs Wine WoW64 (x64) or: sudo dnf install wine",
        "brew": "brew install --cask wine-stable",
    }.get(pm, "Auto-installs Wine WoW64 on Linux x64")

    tools_list = [
        {
            "key": "cli",
            "name": "AnimeStudio.CLI.exe",
            "required_for": "Asset indexing & mesh/texture/anim export",
            "ok": cfg.CLI_EXE.is_file(),
            "status": "OK" if cfg.CLI_EXE.is_file() else "Missing",
            "path": str(cfg.CLI_EXE) if cfg.CLI_EXE.is_file() else "",
            "auto_installable": True,
            "hint": "Click 'Auto-Install Core Tools' or `openzenless setup-tools`",
        },
        {
            "key": "runtime",
            "name": "Windows Native Runtime" if cfg.is_windows() else "Wine Runtime",
            "required_for": "Running AnimeStudio.CLI.exe on Linux/macOS",
            "ok": True if cfg.is_windows() else (wine_path is not None),
            "status": "Native" if cfg.is_windows() else ("OK" if wine_path else "Missing"),
            "path": "Native Windows" if cfg.is_windows() else (wine_path or ""),
            "auto_installable": (cfg.is_linux() and arch in ("x86_64", "amd64")),
            "hint": "Built-in on Windows" if cfg.is_windows() else wine_hint,
        },
        {
            "key": "dotnet",
            "name": ".NET 9 SDK",
            "required_for": "Building & patching AnimeStudio.CLI from source",
            "ok": bool(dotnet_path and _has_dotnet9_sdk(dotnet_path)),
            "status": "OK" if (dotnet_path and _has_dotnet9_sdk(dotnet_path)) else ("Partial" if dotnet_path else "Missing"),
            "path": dotnet_path or "",
            "auto_installable": True,
            "hint": "Auto-installs portable .NET 9 SDK when needed",
        },
        {
            "key": "nap_map",
            "name": "AnimeWwise (nap.map)",
            "required_for": "Voice bank auto-naming & character sorting",
            "ok": cfg.NAP_MAP_FILE.is_file(),
            "status": "OK" if cfg.NAP_MAP_FILE.is_file() else "Missing",
            "path": str(cfg.NAP_MAP_FILE) if cfg.NAP_MAP_FILE.is_file() else "",
            "auto_installable": True,
            "hint": "Auto-downloaded from Escartem/AnimeWwise",
        },
        {
            "key": "vgmstream",
            "name": "vgmstream-cli",
            "required_for": "Decoding Wwise .wem audio to .wav",
            "ok": bool(vgm_path and vgm_path.is_file()),
            "status": "OK" if (vgm_path and vgm_path.is_file()) else "Missing",
            "path": str(vgm_path) if (vgm_path and vgm_path.is_file()) else "",
            "auto_installable": True,
            "hint": "Auto-installs official binary from vgmstream releases",
        },
        {
            "key": "adb",
            "name": "Android Platform-Tools (ADB)",
            "required_for": "Importing ZZZ files directly from Android",
            "ok": adb_path is not None,
            "status": "OK" if adb_path else "Missing",
            "path": adb_path or "",
            "auto_installable": True,
            "hint": "Auto-installs official Google Platform-Tools zip",
        },
        {
            "key": "blender",
            "name": "Blender (Optional)",
            "required_for": "FBX -> GLB conversion & facial morph injection",
            "ok": blender_path is not None,
            "status": "OK" if blender_path else "Not Installed",
            "path": blender_path or "",
            "auto_installable": (arch in ("x86_64", "amd64") and not cfg.is_macos()),
            "hint": "Portable Blender 4.2 LTS (~300 MB) optional for GLB exports",
        },
    ]

    return {
        "os": platform.system(),
        "os_detail": _detect_linux_distro() if cfg.is_linux() else platform.platform(),
        "arch": platform.machine(),
        "display": display,
        "pkg_manager": pm,
        "python": platform.python_version(),
        "tools_dir": str(cfg.TOOLS_DIR),
        "python_modules": py_mods,
        "tools": tools_list,
        "hardware": cfg.get_hardware_profile(),
    }
