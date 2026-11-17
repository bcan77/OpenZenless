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

    _replace_in_file("AnimeStudio.CLI/App.config", [
        (
            '<add key="exportMaterials" value="False" />',
            '<add key="exportMaterials" value="True" />',
        ),
        (
            '<add key="texs" value=\'{}\' />',
            '<add key="texs" value=\'{"_MainTex":0,"_BaseMap":0,"_BaseColorMap":0,"_Diffuse":0,"_Albedo":0,"_MainTex0":0,"_BumpMap":3,"_NormalMap":3,"_MetallicGlossMap":2,"_SpecGlossMap":2}\' />',
        ),
    ])

    _replace_in_file("AnimeStudio.CLI/Program.cs", [
        (
            "                var files = o.Input.Attributes.HasFlag(FileAttributes.Directory) ? Directory.GetFiles(o.Input.FullName, \"*.*\", SearchOption.AllDirectories).OrderBy(x => x.Length).ToArray() : new string[] { o.Input.FullName };",
            "                string[] files;\n"
            "                if (o.Input.Extension.Equals(\".blklist\", StringComparison.OrdinalIgnoreCase))\n"
            "                {\n"
            "                    files = File.ReadAllLines(o.Input.FullName).Where(l => !string.IsNullOrWhiteSpace(l)).Select(l => l.Trim()).ToArray();\n"
            "                }\n"
            "                else\n"
            "                {\n"
            "                    files = o.Input.Attributes.HasFlag(FileAttributes.Directory) ? Directory.GetFiles(o.Input.FullName, \"*.*\", SearchOption.AllDirectories).OrderBy(x => x.Length).ToArray() : new string[] { o.Input.FullName };\n"
            "                }",
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
            "                    }",
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
            "                }\n"
            "                if (Properties.Settings.Default.scrapeMonos)",
        ),
    ])

    _replace_in_file("AnimeStudio.CLI/Studio.cs", [
        (
            "        public static bool SkipContainer = false;",
            "        public static bool SkipContainer = false;\n        public static string PrimarySourceName = null;\n        public static HashSet<string> PrimarySourceNames = null;",
        ),
        (
            "            var matches = exportableAssets.Where(x =>\n            {",
            "            var skipProxies = Environment.GetEnvironmentVariable(\"AS_SKIP_PROXIES\") == \"1\";\n"
            "            var matches = exportableAssets.Where(x =>\n"
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
            "                if (skipProxies && !string.IsNullOrEmpty(x.Text))\n"
            "                {\n"
            "                    var low = x.Text.ToLowerInvariant();\n"
            "                    if (low.EndsWith(\"_proxy\") || low.Contains(\"_proxy (\") || low.Contains(\"_proxy_\")\n"
            "                        || low.EndsWith(\"_col\") || low.Contains(\"_col_\")\n"
            "                        || low.EndsWith(\"_lod1\") || low.EndsWith(\"_lod2\") || low.EndsWith(\"_lod3\")\n"
            "                        || low.Contains(\"_lod1 (\") || low.Contains(\"_lod2 (\") || low.Contains(\"_lod3 (\")\n"
            "                        || low.StartsWith(\"sm_merged_bp_\") || low.StartsWith(\"napmesh,\"))\n"
            "                        return false;\n"
            "                }",
        ),
    ])

    _replace_in_file("AnimeStudio.Utility/ModelConverter.cs", [
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
            "                            break;\n",
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
            "                bool diffuseOnly = global::System.Environment.GetEnvironmentVariable(\"AS_DIFFUSE_ONLY\") == \"1\";\n"
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
            "                    else if (!hasStdDiffuse && (texEnv.Key == \"_RoomTex\" || texEnv.Key == \"_BlindsTex\" || (texEnv.Key == \"_MaskMap\" && m_Texture2D.m_Name.EndsWith(\"_D\", global::System.StringComparison.OrdinalIgnoreCase))))\n"
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
    ])

    _replace_in_file("AnimeStudio/AssetsHelper.cs", [
        (
            "        public static string[] ProcessFiles(string[] files_list)\n"
            "        {\n"
            "            HashSet<string> files = new HashSet<string>(files_list, StringComparer.OrdinalIgnoreCase);",
            "        public static string[] ProcessFiles(string[] files_list)\n"
            "        {\n"
            "            Offsets.Clear();\n"
            "            HashSet<string> files = new HashSet<string>(files_list, StringComparer.OrdinalIgnoreCase);",
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

    return changed


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

    studio_dir = cfg.TOOLS_DIR / "AnimeStudio"
    if studio_dir.is_dir() and _patch_animestudio_cli(studio_dir, log_cb=log_cb):
        dotnet = shutil.which("dotnet")
        csproj = studio_dir / "AnimeStudio.CLI" / "AnimeStudio.CLI.csproj"
        if dotnet and csproj.is_file():
            _emit(log_cb, "Rebuilding AnimeStudio.CLI after applying OpenZenless patches...")
            _stream_cmd([dotnet, "build", str(csproj), "-c", "Release", "-f", "net9.0-windows"], log_cb=log_cb)
            cfg._resolve_tool_paths()

    return {
        "cli_exists": cfg.CLI_EXE.is_file(),
        "cli_path": str(cfg.CLI_EXE),
        "nap_map_exists": cfg.NAP_MAP_FILE.is_file(),
        "nap_map_path": str(cfg.NAP_MAP_FILE),
        "wine_exists": shutil.which("wine") is not None,
    }
