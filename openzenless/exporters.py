"""Batch and pipeline exporters for characters, animations, maps, fonts, assets, and audio."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Sequence

from . import cdn_downloader
from . import config as cfg
from .containers import extract_bnk_wems, parse_akpk, parse_bnk
from .indexer import (
    _emit,
    _retarget_cab_map_base,
    load_character_index,
    load_curated_map_index,
    load_font_blocks,
    load_oz_character_index,
    load_oz_entities_index,
    load_oz_map_index,
    load_oz_media_index,
    load_shader_blocks,
    load_stage_index,
    load_stage_texture_blocks,
    load_stage_texture_blocks_for_dir,
    load_wwise_mapper,
    resolve_material_textures,
    sync_ondemand_blks_index,
)


def run_streaming_cmd(cmd: list[str], cwd: Path | None = None, env: dict | None = None, log_cb=None) -> int:
    _emit(log_cb, f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
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


def export_blk_asset_types(
    source_path: Path,
    out_dir: Path,
    types: list[str],
    export_mode: str = "Convert",
    name_filter: str = "",
    log_cb=None,
    map_op: str = "",
    map_name: str = "",
    diffuse_only: bool = False,
    skip_proxies: bool = False,
    stage_tokens: str = "",
    container_hash_file: Path | None = None,
    cab_filter: str = "",
    extra_cabs: str = "",
    group_assets: str = "ByType",
    skip_animations: bool = False,
) -> int:
    if not source_path.exists():
        raise FileNotFoundError(f"Source path not found: {source_path}")
    if export_mode == "Raw" and not cfg.CLI_EXE.exists():
        out_dir.mkdir(parents=True, exist_ok=True)
        blks = [source_path] if source_path.is_file() else sorted(source_path.rglob("*.blk"))
        if not blks:
            raise FileNotFoundError(f"No .blk files found under {source_path}")
        for b in blks:
            shutil.copy2(b, out_dir / b.name)
        _emit(log_cb, f"Copied {len(blks)} raw .blk container(s) -> {out_dir}")
        return 0
    if not cfg.CLI_EXE.exists() or not cfg.can_run_cli():
        try:
            from . import tools

            tools.ensure_extractor_tools(log_cb=log_cb)
        except Exception as exc:
            _emit(log_cb, f"[WARN] Automatic tool setup attempt finished with note: {exc}")
    if not cfg.CLI_EXE.exists():
        raise FileNotFoundError(f"AnimeStudio.CLI.exe not found at {cfg.CLI_EXE}")
    if not cfg.can_run_cli():
        raise FileNotFoundError("wine executable not found (install Wine via Tools & Dependencies or `openzenless setup-tools --wine`)")
    out_dir.mkdir(parents=True, exist_ok=True)
    extra_env: dict[str, str] = {}
    if diffuse_only:
        extra_env["AS_DIFFUSE_ONLY"] = "1"
    if skip_proxies:
        extra_env["AS_SKIP_PROXIES"] = "1"
    if stage_tokens:
        extra_env["AS_STAGE_TOKENS"] = stage_tokens
    if container_hash_file is not None:
        extra_env["AS_CONTAINER_HASH_FILE"] = cfg.to_cli_path(container_hash_file)
    if cab_filter:
        extra_env["AS_CAB_FILTER"] = cab_filter
    if extra_cabs:
        extra_env["AS_EXTRA_CABS"] = extra_cabs
    if skip_animations:
        extra_env["AS_SKIP_ANIMATIONS"] = "1"
    env = cfg.build_cli_env(extra_env)
    cmd = cfg.build_cli_cmd([
        cfg.to_cli_path(source_path),
        cfg.to_cli_path(out_dir),
        "--game", "ZZZ",
        "--types", *types,
        "--group_assets", group_assets or "ByType",
        "--export_type", export_mode,
    ])
    if map_op and map_name:
        cmd.extend(["--map_op", map_op, "--map_name", map_name])
    if name_filter.strip():
        cmd.extend(["--names", name_filter.strip()])
    return run_streaming_cmd(cmd, cwd=cfg.CLI_DIR, env=env, log_cb=log_cb)


_CHAR_PART_TOKENS = (
    "Ani", "Body", "Face", "Hair", "Eye", "Eyes", "Model", "ModelAvatar",
    "UI", "Weapon", "Cloth", "Skin", "Skill", "Common", "Effect", "NPC",
    "Player", "Story", "Timeline", "Mat", "Tex", "Mesh", "LOD", "Pose",
    "Show", "Walk", "Run", "Attack", "Idle", "Evade", "Switch", "QTE",
    "Ult", "Hit", "Die", "Gal", "Lobby", "Quest", "Cutscene", "Cam",
    "Camera", "Bone", "Rig", "Head", "Tail", "Wing", "Wings", "Hat",
    "Cape", "Coat", "Dress", "Shoe", "Shoes", "Glove", "Gloves",
    "Accessory", "Acc", "Prop", "Sticker", "Icon", "Portrait", "Card",
    "Bust", "Half", "Full", "Avatar", "Default", "Bip001", "Root",
)


def _char_base_short(char_name: str) -> str:
    parts = char_name.split("_")
    if char_name.startswith("Avatar_") and len(parts) >= 4:
        return "_".join(parts[3:])
    return char_name


def _char_short_name(char_name: str, all_chars=None) -> str:
    short = _char_base_short(char_name)
    neg_parts = []
    if all_chars:
        prefix = short + "_"
        for other in all_chars:
            other_short = _char_base_short(other)
            if other_short.startswith(prefix):
                sub = other_short[len(prefix):]
                if sub:
                    neg_parts.append(f"_{re.escape(sub)}(?:$|_)")
    neg = f"(?!{'|'.join(sorted(set(neg_parts)))})" if neg_parts else ""
    if "_" not in short:
        parts_pat = "|".join(_CHAR_PART_TOKENS)
        return f"(?:^|_){re.escape(short)}{neg}(?=$|_(?:{parts_pat})(?:$|_))"
    return f"(?:^|_){re.escape(short)}{neg}(?:$|_)"


def _zzz_cab_map_exists() -> bool:
    return (cfg.CLI_DIR / "Maps" / "ZZZ.bin").is_file()


def export_blks_batch(
    blk_paths: list[Path],
    out_dir: Path,
    types: list[str],
    export_mode: str = "Convert",
    name_filter: str = "",
    log_cb=None,
    use_cab_map: bool | None = None,
    diffuse_only: bool = False,
    skip_proxies: bool = False,
    stage_tokens: str = "",
    container_hash_file: Path | None = None,
    cab_filter: str = "",
    extra_cabs: str = "",
    group_assets: str = "ByType",
    skip_animations: bool = False,
) -> int:
    if not blk_paths:
        return 0
    cab_active = _zzz_cab_map_exists() if use_cab_map is None else (use_cab_map and _zzz_cab_map_exists())
    map_op = "CABMap" if cab_active else ""
    map_name = "ZZZ" if cab_active else ""
    if len(blk_paths) == 1:
        return export_blk_asset_types(
            blk_paths[0], out_dir, types=types, export_mode=export_mode,
            name_filter=name_filter, log_cb=log_cb,
            map_op=map_op, map_name=map_name, diffuse_only=diffuse_only, skip_proxies=skip_proxies,
            stage_tokens=stage_tokens, container_hash_file=container_hash_file,
            cab_filter=cab_filter, extra_cabs=extra_cabs, group_assets=group_assets,
            skip_animations=skip_animations,
        )
    if cab_active:
        with tempfile.NamedTemporaryFile("w", suffix=".blklist", delete=False, encoding="utf-8") as tf:
            for blk in blk_paths:
                tf.write(cfg.to_cli_path(blk) + "\n")
            list_path = Path(tf.name)
        try:
            return export_blk_asset_types(
                list_path, out_dir, types=types, export_mode=export_mode,
                name_filter=name_filter, log_cb=log_cb,
                map_op=map_op, map_name=map_name, diffuse_only=diffuse_only, skip_proxies=skip_proxies,
                stage_tokens=stage_tokens, container_hash_file=container_hash_file,
                cab_filter=cab_filter, extra_cabs=extra_cabs, group_assets=group_assets,
                skip_animations=skip_animations,
            )
        finally:
            list_path.unlink(missing_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix="oz_stage_"))
    try:
        for blk in blk_paths:
            link = stage_dir / blk.name
            if not link.exists():
                try:
                    os.symlink(blk, link)
                except OSError:
                    try:
                        os.link(blk, link)
                    except OSError:
                        shutil.copy2(blk, link)
        return export_blk_asset_types(
            stage_dir, out_dir, types=types, export_mode=export_mode,
            name_filter=name_filter, log_cb=log_cb, diffuse_only=diffuse_only, skip_proxies=skip_proxies,
            stage_tokens=stage_tokens, container_hash_file=container_hash_file,
            cab_filter=cab_filter, extra_cabs=extra_cabs, group_assets=group_assets,
            skip_animations=skip_animations,
        )
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


def _merge_glbs(chunk_paths: list[Path], out_path: Path, log_cb=None) -> int:
    """Merge multiple binary glTF 2.0 (.glb) chunks while deduplicating images, samplers, and textures."""
    if not chunk_paths:
        return 1
    if len(chunk_paths) == 1:
        shutil.move(str(chunk_paths[0]), str(out_path))
        return 0

    def _remap_tex_refs(obj, tex_map: dict[int, int]):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.endswith("Texture") and isinstance(v, dict) and "index" in v:
                    old_i = v["index"]
                    if old_i in tex_map:
                        v["index"] = tex_map[old_i]
                elif isinstance(v, (dict, list)):
                    _remap_tex_refs(v, tex_map)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    _remap_tex_refs(item, tex_map)

    merged: dict = {
        "asset": {"version": "2.0", "generator": "OpenZenless GLB Merger"},
        "scene": 0,
        "scenes": [{"nodes": []}],
    }
    ext_used: list[str] = []
    ext_req: list[str] = []
    nodes: list[dict] = []
    meshes: list[dict] = []
    materials: list[dict] = []
    textures: list[dict] = []
    images: list[dict] = []
    samplers: list[dict] = []
    accessors: list[dict] = []
    buffer_views: list[dict] = []
    skins: list[dict] = []
    animations: list[dict] = []
    cameras: list[dict] = []

    img_sha_to_idx: dict[str, int] = {}
    sampler_to_idx: dict[str, int] = {}
    tex_to_idx: dict[tuple, int] = {}
    seen_chunk_geo: set[tuple] = set()
    bin_parts: list[bytes] = []
    bin_len = 0

    def _mat4_from_nd(nd: dict) -> list[list[float]]:
        if "matrix" in nd:
            m = nd["matrix"]
            return [
                [float(m[0]), float(m[4]), float(m[8]), float(m[12])],
                [float(m[1]), float(m[5]), float(m[9]), float(m[13])],
                [float(m[2]), float(m[6]), float(m[10]), float(m[14])],
                [float(m[3]), float(m[7]), float(m[11]), float(m[15])],
            ]
        t = nd.get("translation") or (0.0, 0.0, 0.0)
        q = nd.get("rotation") or (0.0, 0.0, 0.0, 1.0)
        s = nd.get("scale") or (1.0, 1.0, 1.0)
        x, y, z, w = float(q[0]), float(q[1]), float(q[2]), float(q[3])
        sx, sy, sz = float(s[0]), float(s[1]), float(s[2])
        xx, yy, zz = x * x, y * y, z * z
        xy, xz, yz = x * y, x * z, y * z
        xw, yw, zw = x * w, y * w, z * w
        return [
            [(1.0 - 2.0 * (yy + zz)) * sx, (2.0 * (xy - zw)) * sy, (2.0 * (xz + yw)) * sz, float(t[0])],
            [(2.0 * (xy + zw)) * sx, (1.0 - 2.0 * (xx + zz)) * sy, (2.0 * (yz - xw)) * sz, float(t[1])],
            [(2.0 * (xz - yw)) * sx, (2.0 * (yz + xw)) * sy, (1.0 - 2.0 * (xx + yy)) * sz, float(t[2])],
            [0.0, 0.0, 0.0, 1.0],
        ]

    def _mat4_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
        return [
            [
                a[r][0] * b[0][c] + a[r][1] * b[1][c] + a[r][2] * b[2][c] + a[r][3] * b[3][c]
                for c in range(4)
            ]
            for r in range(4)
        ]

    def _append_bin(raw_bytes: bytes) -> tuple[int, int]:
        nonlocal bin_len
        off = bin_len
        ln = len(raw_bytes)
        pad = (-ln) & 3
        bin_parts.append(raw_bytes)
        if pad:
            bin_parts.append(b"\x00" * pad)
        bin_len += ln + pad
        return off, ln

    for cidx, cp in enumerate(chunk_paths):
        raw = cp.read_bytes()
        magic, _ver, total_len = struct.unpack_from("<III", raw, 0)
        if magic != 0x46546C67:
            raise ValueError(f"Invalid GLB magic in {cp}")
        pos = 12
        jdata: dict = {}
        bdata = b""
        while pos + 8 <= min(total_len, len(raw)):
            clen, ctype = struct.unpack_from("<II", raw, pos)
            pos += 8
            payload = raw[pos : pos + clen]
            pos += clen
            if ctype == 0x4E4F534A:
                jdata = json.loads(payload.decode("utf-8"))
            elif ctype == 0x004E4942:
                bdata = payload

        if cidx == 0 and "asset" in jdata:
            merged["asset"] = jdata["asset"]
        for e in jdata.get("extensionsUsed", ()):
            if e not in ext_used:
                ext_used.append(e)
        for e in jdata.get("extensionsRequired", ()):
            if e not in ext_req:
                ext_req.append(e)

        c_bvs = jdata.get("bufferViews", [])
        samp_map: dict[int, int] = {}
        for i, s in enumerate(jdata.get("samplers", [])):
            sk = json.dumps(s, sort_keys=True)
            if sk not in sampler_to_idx:
                sampler_to_idx[sk] = len(samplers)
                samplers.append(s)
            samp_map[i] = sampler_to_idx[sk]

        img_bv_indices: set[int] = set()
        img_map: dict[int, int] = {}
        for i, im in enumerate(jdata.get("images", [])):
            im_copy = dict(im)
            if "bufferView" in im_copy:
                obv = im_copy["bufferView"]
                img_bv_indices.add(obv)
                bv = c_bvs[obv]
                boff = bv.get("byteOffset", 0)
                blen = bv.get("byteLength", 0)
                img_bytes = bdata[boff : boff + blen]
                sha = hashlib.sha256(img_bytes).hexdigest()
                if sha in img_sha_to_idx:
                    img_map[i] = img_sha_to_idx[sha]
                    continue
                noff, nlen = _append_bin(img_bytes)
                nbv_idx = len(buffer_views)
                buffer_views.append({"buffer": 0, "byteOffset": noff, "byteLength": nlen})
                im_copy["bufferView"] = nbv_idx
                n_idx = len(images)
                images.append(im_copy)
                img_sha_to_idx[sha] = n_idx
                img_map[i] = n_idx
            else:
                n_idx = len(images)
                images.append(im_copy)
                img_map[i] = n_idx

        tex_map: dict[int, int] = {}
        for i, tx in enumerate(jdata.get("textures", [])):
            tx_copy = dict(tx)
            if "source" in tx_copy and tx_copy["source"] in img_map:
                tx_copy["source"] = img_map[tx_copy["source"]]
            if "sampler" in tx_copy and tx_copy["sampler"] in samp_map:
                tx_copy["sampler"] = samp_map[tx_copy["sampler"]]
            tkey = (tx_copy.get("source"), tx_copy.get("sampler"), json.dumps(tx_copy.get("extensions"), sort_keys=True))
            if tkey not in tex_to_idx:
                tex_to_idx[tkey] = len(textures)
                textures.append(tx_copy)
            tex_map[i] = tex_to_idx[tkey]

        bv_map: dict[int, int] = {}
        for i, bv in enumerate(c_bvs):
            if i in img_bv_indices:
                continue
            boff = bv.get("byteOffset", 0)
            blen = bv.get("byteLength", 0)
            noff, nlen = _append_bin(bdata[boff : boff + blen])
            nbv = dict(bv)
            nbv["buffer"] = 0
            nbv["byteOffset"] = noff
            nbv["byteLength"] = nlen
            bv_map[i] = len(buffer_views)
            buffer_views.append(nbv)

        acc_base = len(accessors)
        for acc in jdata.get("accessors", []):
            ac = dict(acc)
            if "bufferView" in ac:
                ac["bufferView"] = bv_map[ac["bufferView"]]
            if "sparse" in ac and isinstance(ac["sparse"], dict):
                sp = dict(ac["sparse"])
                if "indices" in sp and isinstance(sp["indices"], dict) and "bufferView" in sp["indices"]:
                    sp["indices"] = dict(sp["indices"], bufferView=bv_map[sp["indices"]["bufferView"]])
                if "values" in sp and isinstance(sp["values"], dict) and "bufferView" in sp["values"]:
                    sp["values"] = dict(sp["values"], bufferView=bv_map[sp["values"]["bufferView"]])
                ac["sparse"] = sp
            accessors.append(ac)

        mat_base = len(materials)
        for mat in jdata.get("materials", []):
            mc = json.loads(json.dumps(mat))
            _remap_tex_refs(mc, tex_map)
            materials.append(mc)

        mesh_base = len(meshes)
        for mesh in jdata.get("meshes", []):
            ms = dict(mesh)
            new_prims = []
            for prim in mesh.get("primitives", []):
                pr = dict(prim)
                if "attributes" in pr:
                    pr["attributes"] = {k: v + acc_base for k, v in pr["attributes"].items()}
                if "indices" in pr:
                    pr["indices"] += acc_base
                if "material" in pr:
                    pr["material"] += mat_base
                if "targets" in pr:
                    pr["targets"] = [{k: v + acc_base for k, v in tg.items()} for tg in pr["targets"]]
                new_prims.append(pr)
            ms["primitives"] = new_prims
            meshes.append(ms)

        cam_base = len(cameras)
        cameras.extend(jdata.get("cameras", []))

        c_scenes = jdata.get("scenes", [])
        c_sidx = jdata.get("scene", 0)
        c_nodes = jdata.get("nodes", [])
        c_meshes = jdata.get("meshes", [])
        c_accs = jdata.get("accessors", [])
        skipped_mesh_nodes: set[int] = set()
        if not jdata.get("animations") and c_scenes and c_sidx < len(c_scenes):
            mesh_bounds: list[tuple[str, int, tuple[float, float, float] | None, tuple[float, float, float] | None]] = []
            for cm in c_meshes:
                bm = re.sub(r"(?:\.\d+|\s*\(\d+\))$", "", (cm.get("name") or "").lower()).strip()
                mins_l: list[list[float]] = []
                maxs_l: list[list[float]] = []
                tris = 0
                for pr in cm.get("primitives", []):
                    pa_idx = (pr.get("attributes") or {}).get("POSITION")
                    if pa_idx is not None and pa_idx < len(c_accs):
                        pa = c_accs[pa_idx]
                        if "min" in pa and "max" in pa:
                            mins_l.append(pa["min"])
                            maxs_l.append(pa["max"])
                    if "indices" in pr and pr["indices"] < len(c_accs):
                        tris += int(c_accs[pr["indices"]].get("count", 0)) // 3
                if mins_l:
                    lmin = (min(v[0] for v in mins_l), min(v[1] for v in mins_l), min(v[2] for v in mins_l))
                    lmax = (max(v[0] for v in maxs_l), max(v[1] for v in maxs_l), max(v[2] for v in maxs_l))
                else:
                    lmin, lmax = None, None
                mesh_bounds.append((bm, tris, lmin, lmax))

            ident4 = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
            stack = [(rn, ident4) for rn in reversed(c_scenes[c_sidx].get("nodes", []))]
            while stack:
                ni, pm = stack.pop()
                if ni < 0 or ni >= len(c_nodes):
                    continue
                cnd = c_nodes[ni]
                wm = _mat4_mul(pm, _mat4_from_nd(cnd))
                mi = cnd.get("mesh")
                if mi is not None and mi < len(mesh_bounds):
                    bm, tris, lmin, lmax = mesh_bounds[mi]
                    if tris > 0 and lmin is not None and lmax is not None:
                        xs = (lmin[0], lmax[0])
                        ys = (lmin[1], lmax[1])
                        zs = (lmin[2], lmax[2])
                        wxs: list[float] = []
                        wys: list[float] = []
                        wzs: list[float] = []
                        for cx in xs:
                            for cy in ys:
                                for cz in zs:
                                    wxs.append(wm[0][0] * cx + wm[0][1] * cy + wm[0][2] * cz + wm[0][3])
                                    wys.append(wm[1][0] * cx + wm[1][1] * cy + wm[1][2] * cz + wm[1][3])
                                    wzs.append(wm[2][0] * cx + wm[2][1] * cy + wm[2][2] * cz + wm[2][3])
                        gkey = (
                            bm, tris,
                            round(min(wxs), 4), round(min(wys), 4), round(min(wzs), 4),
                            round(max(wxs), 4), round(max(wys), 4), round(max(wzs), 4),
                        )
                        if gkey in seen_chunk_geo:
                            skipped_mesh_nodes.add(ni)
                        else:
                            seen_chunk_geo.add(gkey)
                for ch in reversed(cnd.get("children", [])):
                    stack.append((ch, wm))

        node_base = len(nodes)
        skin_base = len(skins)
        for ni, nd in enumerate(c_nodes):
            nc = dict(nd)
            if "children" in nc:
                nc["children"] = [c + node_base for c in nc["children"]]
            if ni in skipped_mesh_nodes:
                nc.pop("mesh", None)
            elif "mesh" in nc:
                nc["mesh"] += mesh_base
            if "skin" in nc:
                nc["skin"] += skin_base
            if "camera" in nc:
                nc["camera"] += cam_base
            nodes.append(nc)

        for sk in jdata.get("skins", []):
            sc = dict(sk)
            if "inverseBindMatrices" in sc:
                sc["inverseBindMatrices"] += acc_base
            if "skeleton" in sc:
                sc["skeleton"] += node_base
            if "joints" in sc:
                sc["joints"] = [j + node_base for j in sc["joints"]]
            skins.append(sc)

        for an in jdata.get("animations", []):
            ac = dict(an)
            if "samplers" in ac:
                ac["samplers"] = [
                    dict(sm, input=sm["input"] + acc_base, output=sm["output"] + acc_base)
                    for sm in ac["samplers"]
                ]
            if "channels" in ac:
                new_ch = []
                for ch in ac["channels"]:
                    cc = dict(ch)
                    if "target" in cc and isinstance(cc["target"], dict):
                        tg = dict(cc["target"])
                        if "node" in tg:
                            tg["node"] += node_base
                        cc["target"] = tg
                    new_ch.append(cc)
                ac["channels"] = new_ch
            animations.append(ac)

        if c_scenes and c_sidx < len(c_scenes):
            merged["scenes"][0]["nodes"].extend(n + node_base for n in c_scenes[c_sidx].get("nodes", []))

    if ext_used:
        merged["extensionsUsed"] = ext_used
    if ext_req:
        merged["extensionsRequired"] = ext_req
    for key, arr in (
        ("nodes", nodes), ("meshes", meshes), ("materials", materials),
        ("textures", textures), ("images", images), ("samplers", samplers),
        ("accessors", accessors), ("bufferViews", buffer_views),
        ("skins", skins), ("animations", animations), ("cameras", cameras),
    ):
        if arr:
            merged[key] = arr
    if bin_len > 0:
        merged["buffers"] = [{"byteLength": bin_len}]

    json_bytes = json.dumps(merged, separators=(",", ":")).encode("utf-8")
    jpad = (-len(json_bytes)) & 3
    if jpad:
        json_bytes += b" " * jpad
    bin_bytes = b"".join(bin_parts)
    has_bin = len(bin_bytes) > 0
    total_glb = 12 + 8 + len(json_bytes) + (8 + len(bin_bytes) if has_bin else 0)
    with out_path.open("wb") as fout:
        fout.write(struct.pack("<III", 0x46546C67, 2, total_glb))
        fout.write(struct.pack("<II", len(json_bytes), 0x4E4F534A))
        fout.write(json_bytes)
        if has_bin:
            fout.write(struct.pack("<II", len(bin_bytes), 0x004E4942))
            fout.write(bin_bytes)
    _emit(log_cb, f"Merged {len(chunk_paths)} GLB chunk(s) -> {out_path.name} ({len(images)} unique images, {len(nodes)} nodes)")
    cfg.release_os_memory()
    return 0


_BLENDER_CONVERT_SCRIPT = r'''
import bpy, os, json as _json, re as _re, zlib as _zlib
from pathlib import Path as _Path
from mathutils import Matrix as _Mat, Vector as _Vec, Quaternion as _Quat

bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
sc.render.threads_mode = 'AUTO'
try:
    _cp = bpy.context.preferences.addons['cycles'].preferences
    for _bt in ('OPTIX', 'CUDA', 'HIP', 'ONEAPI'):
        try:
            _cp.compute_device_type = _bt
            _cp.get_devices()
            if any(d.type != 'CPU' for d in _cp.devices):
                for _d in _cp.devices:
                    _d.use = (_d.type != 'CPU')
                break
        except Exception:
            pass
except Exception:
    pass

hold = bpy.data.collections.new('Hold')
hold.use_fake_user = True
_canonical_img_names = {}

def _img_key(img):
    fp = getattr(img, 'filepath', '') or ''
    if fp:
        try:
            ap = bpy.path.abspath(fp)
            if os.path.isfile(ap):
                return 'file:' + os.path.basename(ap).lower() + ':' + str(os.path.getsize(ap))
        except Exception:
            pass
    nm = os.path.splitext(os.path.basename(fp or img.name))[0].lower()
    nm = _re.sub(r'(?:\.[0-9]+|\s*\([0-9]+\))$', '', nm)
    return 'stem:' + nm

def _mat_nodes_ok(m):
    if m is None or not m.node_tree:
        return False
    return bpy.app.version >= (5, 0, 0) or bool(getattr(m, 'use_nodes', False))

def _dedup_imgs():
    remap = {}
    for img in list(bpy.data.images):
        if img.name in ('Render Result', 'Viewer Node'):
            continue
        k = _img_key(img)
        cname = _canonical_img_names.get(k)
        canon = bpy.data.images.get(cname) if cname else None
        if canon is None:
            _canonical_img_names[k] = img.name
        elif canon != img:
            remap[img] = canon
    if remap:
        for mat in bpy.data.materials:
            if not _mat_nodes_ok(mat):
                continue
            for nd in mat.node_tree.nodes:
                if nd.type == 'TEX_IMAGE' and nd.image in remap:
                    nd.image = remap[nd.image]
        for dup in remap:
            try:
                bpy.data.images.remove(dup, do_unlink=True)
            except Exception:
                pass

def _stash():
    for o in list(sc.collection.objects):
        sc.collection.objects.unlink(o)
        hold.objects.link(o)
    for c in list(sc.collection.children):
        sc.collection.children.unlink(c)
        hold.children.link(c)

def _fbx_import_call(fp, global_scale=None, auto_orient=False):
    kw = {'filepath': fp}
    if global_scale is not None:
        kw['global_scale'] = float(global_scale)
    try:
        rna = bpy.ops.import_scene.fbx.get_rna_type().properties
        if 'ignore_leaf_bones' in rna:
            kw['ignore_leaf_bones'] = False
        if auto_orient:
            if 'automatic_bone_orientation' in rna:
                kw['automatic_bone_orientation'] = True
            elif 'use_automatic_bone_orientation' in rna:
                kw['use_automatic_bone_orientation'] = True
    except Exception:
        pass
    bpy.ops.import_scene.fbx(**kw)

fbxs = _CFG['fbxs']
objs = _CFG['objs']
_char_dir = _CFG['char_dir']
_char_name = _CFG['char_name']
_out_glb = _CFG['out_glb']
_save_blend = _CFG['save_blend']
_anim_dir = _CFG['anim_dir']
_is_char_dir = bool(_CFG['is_char'])
_mat_to_tex = _CFG['mat_to_tex']
_allow_fuzzy_base = bool(_CFG['allow_fuzzy'])
_max_anims = int(_CFG.get('max_anims', 32))

_obj_import_rank = {}
def _import_fbx(fp, rank=0):
    before = set(bpy.data.objects)
    _fbx_import_call(fp, auto_orient=False)
    new = [o for o in bpy.data.objects if o not in before]
    if _is_char_dir:
        arms = [o for o in new if o.type == 'ARMATURE']
        if arms:
            ascale = min((max(abs(float(v)) for v in o.matrix_world.to_scale()) for o in arms), default=1.0)
            if 0.0 < ascale < 0.1:
                for o in new:
                    bpy.data.objects.remove(o, do_unlink=True)
                _fbx_import_call(fp, global_scale=1.0 / ascale, auto_orient=False)
                new = [o for o in bpy.data.objects if o not in before]
                print(f'Normalized FBX scale {ascale:.4f} -> 1.0: {fp}', flush=True)
    for o in new:
        _obj_import_rank[o.as_pointer()] = rank

for idx, fp in enumerate(fbxs, 1):
    try:
        _import_fbx(fp, idx)
        _stash()
    except Exception as _e:
        print(f'SKIP FBX {fp}: {_e}', flush=True)
    if idx % 50 == 0:
        _dedup_imgs()
    if idx % 250 == 0:
        print(f'Imported FBX {idx}/{len(fbxs)} (unique images={len(bpy.data.images)})', flush=True)

_imported_objs = False
_need_objs = objs and (
    not any(o.type == 'MESH' for o in bpy.data.objects)
    or (_is_char_dir and not any(k in o.name.lower() for o in bpy.data.objects if o.type == 'MESH' for k in ('body', 'face', 'hair')))
)
if _need_objs:
    _imported_objs = True
    if _is_char_dir:
        _sep = [p for p in objs if os.path.basename(p).lower().startswith('separatemesh_')]
        if _sep:
            _sep_low = ' '.join(os.path.basename(p).lower() for p in _sep)
            objs = [p for p in objs if p in _sep or os.path.splitext(os.path.basename(p))[0].lower() not in _sep_low]
    for idx, fp in enumerate(objs, 1):
        if _is_char_dir or os.path.basename(fp).lower().startswith('separatemesh_'):
            bpy.ops.wm.obj_import(filepath=fp, forward_axis='Y', up_axis='Z')
        else:
            bpy.ops.wm.obj_import(filepath=fp)
        _stash()
        if idx % 250 == 0:
            print(f'Imported OBJ {idx}/{len(objs)}', flush=True)

sc.collection.children.link(hold)
_dedup_imgs()

if any(o.type == 'ARMATURE' for o in bpy.data.objects) and (_anim_dir or _save_blend):
    _is_char_dir = True

def _mat_has_img(m):
    if not _mat_nodes_ok(m):
        return False
    return any(n.type == 'TEX_IMAGE' and getattr(n, 'image', None) is not None for n in m.node_tree.nodes)

def _is_greybox_mat(m):
    if m is None:
        return False
    ml = m.name.lower()
    if 'shadowproxy' in ml:
        return True
    if (ml == 'lit' or ml.startswith('lit.') or ml == 'unlit' or ml.startswith('unlit.') or 'mihoyo_unlit_unlit' in ml) and not _mat_has_img(m):
        return True
    return False

def _is_proxy_obj(obj):
    cur = obj
    while cur is not None:
        nl = cur.name.lower()
        if _re.search(r'(?:_col(?:$|_|lider)|_proxy(?:$|_|\s|\.|[0-9])|_hairshadow(?:$|_|\s|\.|[0-9])|^volumetricfog|^scenepropcollider_|^blockout_|^stage_.*_ld$|^sm_merged_bp_|^napmesh,)', nl):
            return True
        cur = cur.parent
    if obj.type == 'MESH' and obj.data and obj.data.materials:
        non_none = [m for m in obj.data.materials if m is not None]
        if non_none and all(_is_greybox_mat(m) for m in non_none):
            return True
    return False

_mesh_objs = [o for o in bpy.data.objects if o.type == 'MESH' and o.data]
_non_proxy = [o for o in _mesh_objs if not _is_proxy_obj(o)]
if _non_proxy:
    for o in _mesh_objs:
        if o not in _non_proxy:
            bpy.data.objects.remove(o, do_unlink=True)

if not _is_char_dir and _non_proxy:
    _seen_geo = set()
    _dup_removed = 0
    bpy.context.view_layer.update()
    for o in sorted(_non_proxy, key=lambda _o: _obj_import_rank.get(_o.as_pointer(), 999999)):
        if not o.data or len(o.data.vertices) == 0:
            continue
        _bm = _re.sub(r'(?:\.[0-9]+|\s*\([0-9]+\))$', '', o.data.name.lower()).strip()
        _mw = o.matrix_world
        _pts = [_mw @ _Vec(c) for c in o.bound_box]
        _gkey = (
            _bm, len(o.data.vertices), len(o.data.polygons),
            round(min(p.x for p in _pts), 4), round(min(p.y for p in _pts), 4), round(min(p.z for p in _pts), 4),
            round(max(p.x for p in _pts), 4), round(max(p.y for p in _pts), 4), round(max(p.z for p in _pts), 4),
        )
        if _gkey in _seen_geo:
            bpy.data.objects.remove(o, do_unlink=True)
            _dup_removed += 1
        else:
            _seen_geo.add(_gkey)
    if _dup_removed:
        print(f'Deduplicated {_dup_removed} exact co-planar duplicate mesh(es)', flush=True)

# Rig & Armature Unification for pre-rigged animation use
_master_arm = None
if _is_char_dir:
    _helper_re = _re.compile(
        r'(?:hitcollider|wallcollider|camerabasepoint|hudpoint|faceshadowpoint|hairshadow|fx_root|_lod[0-9]+|foretwist old|normal_03_origin|set_tail_end|branch_04_center|^ctr_[a-z]_|^root$)'
    )
    for _eo in list(bpy.data.objects):
        if _eo.type == 'EMPTY' and not _eo.children and _helper_re.search(_eo.name.lower()):
            bpy.data.objects.remove(_eo, do_unlink=True)

    _arms = [o for o in bpy.data.objects if o.type == 'ARMATURE' and o.data]
    if _arms:
        def _arm_score(_a):
            _bips = sum(1 for _b in _a.data.bones if 'bip' in _b.name.lower() and not _re.search(r'\.[0-9]+$', _b.name))
            return (_bips, len(_a.data.bones), -len(_a.name))
        _master_arm = max(_arms, key=_arm_score)

        # Strip .001 / .002 suffixes on master armature where base name is free
        for _b in list(_master_arm.data.bones):
            _bn_clean = _re.sub(r'\.[0-9]+$', '', _b.name)
            if _bn_clean != _b.name and _bn_clean not in _master_arm.data.bones:
                _b.name = _bn_clean

        # Merge missing bones from secondary armatures into master armature
        _sec_arms = [a for a in _arms if a != _master_arm]
        if _sec_arms:
            _bpy_active = bpy.context.view_layer.objects
            _bpy_active.active = _master_arm
            bpy.ops.object.mode_set(mode='EDIT')
            _ebones = _master_arm.data.edit_bones
            _inv_mw = _master_arm.matrix_world.inverted()
            for _sa in _sec_arms:
                _to_master = _inv_mw @ _sa.matrix_world
                _pending = []
                for _sb in _sa.data.bones:
                    _clean = _re.sub(r'\.[0-9]+$', '', _sb.name)
                    if _clean not in _ebones:
                        _eb = _ebones.new(_clean)
                        _eb.head = _to_master @ _sb.head_local
                        _eb.tail = _to_master @ _sb.tail_local
                        if (_eb.tail - _eb.head).length < 1e-5:
                            _eb.tail = _eb.head + _Vec((0.0, 0.01, 0.0))
                        _eb.matrix = _to_master @ _sb.matrix_local
                        _p_clean = _re.sub(r'\.[0-9]+$', '', _sb.parent.name) if _sb.parent else None
                        _pending.append((_eb.name, _p_clean))
                for _bname, _pname in _pending:
                    if _pname and _pname in _ebones:
                        _ebones[_bname].parent = _ebones[_pname]
            bpy.ops.object.mode_set(mode='OBJECT')

        # Rebind every mesh to _master_arm and clean .001 vertex groups
        _master_bone_names = set(_master_arm.data.bones.keys())
        _char_meshes = [o for o in bpy.data.objects if o.type == 'MESH' and o.data]
        for _mo in _char_meshes:
            _mw = _mo.matrix_world.copy()
            if _mo.parent != _master_arm:
                _mo.parent = _master_arm
                _mo.matrix_world = _mw
            # Clean duplicate .001 vertex group suffixes and merge weights
            _vgs = list(_mo.vertex_groups)
            for _vg in _vgs:
                _base = _re.sub(r'\.[0-9]+$', '', _vg.name)
                if _base != _vg.name and _base in _master_bone_names:
                    _dst_vg = _mo.vertex_groups.get(_base)
                    if _dst_vg is None:
                        _vg.name = _base
                    elif _dst_vg != _vg:
                        for _v in _mo.data.vertices:
                            for _g in _v.groups:
                                if _g.group == _vg.index:
                                    _dst_vg.add([_v.index], _g.weight, 'ADD')
                        _mo.vertex_groups.remove(_vg)
            # Deduplicate / retarget ARMATURE modifiers to _master_arm
            _kept_mod = None
            for _mod in list(_mo.modifiers):
                if _mod.type == 'ARMATURE':
                    if _kept_mod is None:
                        _mod.object = _master_arm
                        _kept_mod = _mod
                    else:
                        _mo.modifiers.remove(_mod)
            if _kept_mod is None and len(_mo.vertex_groups) > 0:
                _mod = _mo.modifiers.new(name='Armature', type='ARMATURE')
                _mod.object = _master_arm

        # Auto-bind unrigged fallback meshes (e.g. SeparateMesh OBJ or unskinned weapon)
        _donor_meshes = [o for o in _char_meshes if len(o.vertex_groups) >= 8]
        _donor_mesh = max(_donor_meshes, key=lambda o: len(o.data.vertices), default=None)
        for _mo in _char_meshes:
            if len(_mo.vertex_groups) > 0 or len(_mo.data.vertices) == 0:
                continue
            _nl = _mo.name.lower()
            _bound = False
            if any(k in _nl for k in ('weapon', 'sword', 'blade', 'gun', 'hammer', 'staff', 'bow')):
                _wbone = next((_bn for _bn in ('Weapon_R', 'Weapon_L', 'Bip001 Prop1', 'Bip001 R Hand', 'Bip001 L Hand') if _bn in _master_bone_names), None)
                if _wbone:
                    _vg = _mo.vertex_groups.new(name=_wbone)
                    _vg.add(list(range(len(_mo.data.vertices))), 1.0, 'REPLACE')
                    _bound = True
            if not _bound and _donor_mesh is not None and _donor_mesh != _mo:
                try:
                    for _o in bpy.data.objects:
                        _o.select_set(False)
                    _donor_mesh.select_set(True)
                    _mo.select_set(True)
                    bpy.context.view_layer.objects.active = _mo
                    bpy.ops.object.data_transfer(
                        use_reverse_transfer=False,
                        data_type='VGROUP_WEIGHTS',
                        layers_select_src='ALL',
                        layers_select_dst='NAME',
                        vert_mapping='POLYINTERP_NEAREST',
                    )
                    _bound = len(_mo.vertex_groups) > 0
                except Exception:
                    pass
            if not _bound:
                _fbone = next((_bn for _bn in ('Bip001 Head', 'Bip001 Spine2', 'Bip001 Pelvis', 'Bip001') if _bn in _master_bone_names), None)
                if not _fbone and _master_arm.data.bones:
                    _fbone = _master_arm.data.bones[0].name
                if _fbone:
                    _vg = _mo.vertex_groups.new(name=_fbone)
                    _vg.add(list(range(len(_mo.data.vertices))), 1.0, 'REPLACE')
            if len(_mo.vertex_groups) > 0 and not any(_m.type == 'ARMATURE' for _m in _mo.modifiers):
                _mod = _mo.modifiers.new(name='Armature', type='ARMATURE')
                _mod.object = _master_arm

        for _sa in _sec_arms:
            try:
                bpy.data.objects.remove(_sa, do_unlink=True)
            except Exception:
                pass

        try:
            _master_arm.name = _char_name or 'Armature'
            if _master_arm.data:
                _master_arm.data.name = f'{_char_name}_Rig' if _char_name else 'Rig'
            _master_arm.show_in_front = True
            _master_arm.data.display_type = 'OCTAHEDRAL'
        except Exception:
            pass
        print(f'Unified rig on {_master_arm.name}: {len(_master_arm.data.bones)} bones, {len(_char_meshes)} bound meshes', flush=True)

def _has_base_tex(mat):
    if not _mat_nodes_ok(mat):
        return False
    for nd in mat.node_tree.nodes:
        if nd.type == 'TEX_IMAGE' and nd.image is not None:
            for lk in nd.outputs[0].links:
                if lk.to_socket.name == 'Base Color':
                    return True
    return False

def _img_has_alpha(img):
    if img is None or img.depth not in (32, 64, 128) or not img.has_data:
        return False
    try:
        px = img.pixels
        n = len(px)
        step = max(4, (n // 4096) * 4)
        for i in range(3, n, step):
            if px[i] < 0.98:
                return True
    except Exception:
        pass
    return False

# Scan PNGs and Material JSONs across both _char_dir and its parent for diffuse & normal maps
_png_roots = [_char_dir]
if os.path.isdir(os.path.join(os.path.dirname(_char_dir), 'Texture2D')):
    _png_roots.append(os.path.dirname(_char_dir))
_all_pngs = []
_all_mat_jsons = []
for _pr in _png_roots:
    for _p in sorted(_Path(_pr).rglob('*.png')):
        _all_pngs.append(str(_p))
    for _mp in sorted(_Path(_pr).rglob('*.json')):
        if _mp.parent.name in ('Material', 'Materials') or _mp.stem.startswith('MAT_'):
            _all_mat_jsons.append(_mp)

_png_by_stem = {}
for _p in _all_pngs:
    _st = os.path.splitext(os.path.basename(_p))[0].lower()
    _prev = _png_by_stem.get(_st)
    if _prev is None or os.path.getsize(_p) > os.path.getsize(_prev):
        _png_by_stem[_st] = _p

_mat_diff_json = dict(_mat_to_tex)
_mat_norm_json = {}
_DIFF_SLOTS = ('_MainTex', '_BaseMap', '_BaseColorMap', '_Diffuse', '_Albedo', '_MainTex0', '_BaseColor', '_ColorMap')
_NORM_SLOTS = ('_LightTex', '_BumpMap', '_NormalMap', '_NormalTex', '_Normal')
for _mp in _all_mat_jsons:
    try:
        _md = _json.loads(_mp.read_text(encoding='utf-8', errors='ignore'))
    except Exception:
        continue
    _tenv = (_md.get('m_SavedProperties') or {}).get('m_TexEnvs') or {}
    if not isinstance(_tenv, dict):
        continue
    _mkeys = [_mp.stem.lower()]
    if _md.get('m_Name'):
        _mkeys.append(str(_md['m_Name']).lower())
    for _sl in _DIFF_SLOTS:
        _tv = _tenv.get(_sl)
        if isinstance(_tv, dict):
            _mt = _tv.get('m_Texture') or {}
            _tn = (_mt.get('Name') or '').strip().lower()
            if _tn and not _mt.get('IsNull', False):
                for _mk in _mkeys:
                    _mat_diff_json.setdefault(_mk, _tn)
                break
    for _sl in _NORM_SLOTS:
        _tv = _tenv.get(_sl)
        if isinstance(_tv, dict):
            _mt = _tv.get('m_Texture') or {}
            _tn = (_mt.get('Name') or '').strip().lower()
            if _tn and not _mt.get('IsNull', False):
                if _sl == '_LightTex' and (
                    any(k in _tn for k in ('lightmap', 'ramp', 'sdf', 'shadow'))
                    or not _tn.endswith(('_n', '_norm', '_normal', '_bump'))
                ):
                    continue
                for _mk in _mkeys:
                    _mat_norm_json.setdefault(_mk, _tn)
                break

_allow_fuzzy = _allow_fuzzy_base or _imported_objs
if _allow_fuzzy and _png_by_stem:
    diffuse_pngs = [
        (_st, _p) for _st, _p in _png_by_stem.items()
        if not _st.endswith(('_n', '_m', '_a', '_e', '_h', '_b', '_ao', '_occ', '_rough', '_metal', '_spec', '_ramp', '_lightmap', '_lm', '_norm', '_normal', '_bump', '_mask', '_sdf', '_data', '_rm'))
    ]
    _img_cache = {}
    _mat_cache = {}
    def _pick_tex(name):
        raw_nl = _re.sub(r'\.[0-9]+$', '', name.lower())
        nl = _re.sub(r'\s*\[[^\]]+\]$', '', raw_nl)
        nl = _re.sub(r'\s*\([0-9]+\)$', '', nl).strip()
        nl = _re.sub(r'^separatemesh_.*?_model_', '', nl)
        nl = _re.sub(r'^separatemesh_avatar_[^_]+_[^_]+_[^_]+_', '', nl)
        nl = _re.sub(r'_(?:lod[0-9]+|model)$', '', nl)
        m_body = _re.search(r'_body_([0-9]+)(?:_[0-9]+)?$', nl)
        m_num = _re.search(r'_([0-9]+)$', nl)
        num_suf = m_body.group(1) if m_body else (m_num.group(1) if m_num else None)
        nl_base = _re.sub(r'_[0-9]+$', '', nl)
        mat_cands = [raw_nl, nl, 'mat_' + nl, nl_base, 'mat_' + nl_base]
        if m_body:
            m_bprefix = _re.search(r'^(.+_body_[0-9]+)_[0-9]+$', nl)
            if m_bprefix:
                bp = m_bprefix.group(1)
                mat_cands.extend([bp, 'mat_' + bp])
        for _ck in mat_cands:
            _exact = _mat_diff_json.get(_ck)
            if _exact and _exact in _png_by_stem:
                return _png_by_stem[_exact]
        toks = [t for t in nl_base.split('_') if t]
        best_p, best_s = None, 0
        for st, fp in diffuse_pngs:
            if any(k in st for k in ('decal', 'floodlight', '_eff')) and not any(k in nl for k in ('decal', 'floodlight', 'eff', 'light')):
                continue
            sc_val = 0
            map_bonus = (40 if (num_suf and f'_map{num_suf}' in st) else 0)
            if st == nl + '_d' or st == nl_base + '_d' or st == nl:
                sc_val = 1000
            elif len(nl) >= 6 and st.startswith(nl + '_'):
                sc_val = 800 + (20 if st.endswith('_d') else 0) + map_bonus
            elif len(nl_base) >= 6 and st.startswith(nl_base + '_'):
                sc_val = 500 + (20 if st.endswith('_d') else 0) + map_bonus
            elif len(toks) >= 3:
                for k in range(len(toks), 2, -1):
                    pref = '_'.join(toks[:k])
                    if st.startswith(pref + '_'):
                        sc_val = 100 + k * 10 + (10 if st.endswith('_d') else 0) + map_bonus
                        break
            if sc_val > best_s:
                best_s, best_p = sc_val, fp
        return best_p

    def _make_mat(fp, obj_name=''):
        nl = (os.path.basename(fp) + ' ' + obj_name).lower()
        is_sky = bool(_re.search(r'(?:^|[_\s.])(skybox|skydome|sky_?dome|sky_?sphere|sky_?plane|sky|cloud|dome|backdrop)(?:$|[_\s.0-9])', nl))
        is_cutout = any(t in nl for t in ('hair', 'lash', 'leaf', 'tree', 'plant', 'bush', 'grass', 'foliage', 'shadow'))
        key = (fp, is_sky, is_cutout)
        if key in _mat_cache:
            return _mat_cache[key]
        img = _img_cache.get(fp)
        if img is None:
            img = bpy.data.images.load(fp, check_existing=True)
            _img_cache[fp] = img
        mat = bpy.data.materials.new(name=os.path.splitext(os.path.basename(fp))[0])
        mat.diffuse_color = (1.0, 1.0, 1.0, 1.0)
        if bpy.app.version < (5, 0, 0) and hasattr(mat, 'use_nodes'):
            mat.use_nodes = True
        nt = mat.node_tree
        bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None) or nt.nodes.new('ShaderNodeBsdfPrincipled')
        tex = nt.nodes.new('ShaderNodeTexImage')
        tex.image = img
        nt.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
        is_trans_hint = is_sky or is_cutout or any(t in nl for t in ('decal', 'glass', 'eff', 'fx', 'floodlight'))
        if is_trans_hint and _img_has_alpha(img):
            nt.links.new(tex.outputs['Alpha'], bsdf.inputs['Alpha'])
            if hasattr(mat, 'blend_method'):
                mat.blend_method = 'CLIP' if (is_cutout or 'decal' in nl) else 'BLEND'
            if hasattr(mat, 'alpha_threshold'):
                mat.alpha_threshold = 0.45
        if is_sky or is_cutout:
            mat.use_backface_culling = False
        _mat_cache[key] = mat
        return mat

    if diffuse_pngs:
        for o in bpy.data.objects:
            if o.type != 'MESH' or not o.data:
                continue
            if o.data.materials:
                for _si in range(len(o.data.materials)):
                    _om = o.data.materials[_si]
                    if _has_base_tex(_om):
                        continue
                    _mn = _om.name if _om else ''
                    _pn = o.parent.name if o.parent else ''
                    _fp = (_pick_tex(_mn) if _mn else None) or _pick_tex(o.name) or _pick_tex(o.data.name) or (_pick_tex(_pn) if _pn else None)
                    if _fp:
                        o.data.materials[_si] = _make_mat(_fp, o.name)
            else:
                _pn = o.parent.name if o.parent else ''
                _fp = _pick_tex(o.name) or _pick_tex(o.data.name) or (_pick_tex(_pn) if _pn else None)
                if _fp:
                    o.data.materials.append(_make_mat(_fp, o.name))

_dedup_imgs()

# Material polish + Normal Map (_LightTex / *_N.png) wiring + duplicate Alpha removal
_norm_img_cache = {}
def _find_normal_png(mat_name, base_img):
    ml_clean = _re.sub(r'\.[0-9]+$', '', mat_name.lower())
    for _ck in (ml_clean, 'mat_' + ml_clean):
        _nhit = _mat_norm_json.get(_ck)
        if _nhit and _nhit in _png_by_stem:
            return _png_by_stem[_nhit]
    if base_img is not None:
        bfp = getattr(base_img, 'filepath', '') or base_img.name
        bst = os.path.splitext(os.path.basename(bfp))[0].lower()
        bst = _re.sub(r'\.[0-9]+$', '', bst)
        for _cand_st in (
            (_re.sub(r'_(?:d|diff|diffuse|albedo)$', '_n', bst) if bst.endswith(('_d', '_diff', '_diffuse', '_albedo')) else bst + '_n'),
            bst + '_normal',
        ):
            if _cand_st in _png_by_stem:
                return _png_by_stem[_cand_st]
    return None

for mat in bpy.data.materials:
    if not _mat_nodes_ok(mat):
        continue
    nt = mat.node_tree
    bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    ml = mat.name.lower()
    is_alpha_mat = any(t in ml for t in ('hair', 'lash', 'eye', 'leaf', 'tree', 'plant', 'bush', 'grass', 'foliage', 'sky', 'decal', 'glass', 'fx', 'eff'))

    # Clean Alpha links on opaque materials so ZZZ packed alpha maps do not turn models translucent
    if bsdf and 'Alpha' in bsdf.inputs:
        for _lk in list(bsdf.inputs['Alpha'].links):
            _src_img = getattr(_lk.from_node, 'image', None)
            if not _is_char_dir:
                nt.links.remove(_lk)
            elif not (is_alpha_mat and _img_has_alpha(_src_img)):
                nt.links.remove(_lk)
        if hasattr(mat, 'blend_method') and not bsdf.inputs['Alpha'].is_linked:
            mat.blend_method = 'OPAQUE'

    # Find linked Base Color image
    base_img = None
    if bsdf and 'Base Color' in bsdf.inputs and bsdf.inputs['Base Color'].is_linked:
        _bn = bsdf.inputs['Base Color'].links[0].from_node
        if _bn.type == 'TEX_IMAGE':
            base_img = _bn.image

    # Wire tangent Normal map on rigged characters/entities (or keep existing normal links)
    if _is_char_dir and bsdf and 'Normal' in bsdf.inputs:
        _nfp = _find_normal_png(mat.name, base_img)
        if _nfp:
            _nimg = _norm_img_cache.get(_nfp)
            if _nimg is None:
                _nimg = bpy.data.images.load(_nfp, check_existing=True)
                try:
                    _nimg.colorspace_settings.name = 'Non-Color'
                except Exception:
                    pass
                _norm_img_cache[_nfp] = _nimg
            _nm_node = next((n for n in nt.nodes if n.type == 'NORMAL_MAP'), None)
            if _nm_node is None:
                _nm_node = nt.nodes.new('ShaderNodeNormalMap')
            if 'Strength' in _nm_node.inputs:
                _nm_node.inputs['Strength'].default_value = 0.75
            _ntex = next((n for n in nt.nodes if n.type == 'TEX_IMAGE' and n.image == _nimg), None)
            if _ntex is None:
                _ntex = nt.nodes.new('ShaderNodeTexImage')
                _ntex.image = _nimg
            nt.links.new(_ntex.outputs['Color'], _nm_node.inputs['Color'])
            nt.links.new(_nm_node.outputs['Normal'], bsdf.inputs['Normal'])
        else:
            for _dnm in [n for n in nt.nodes if n.type == 'NORMAL_MAP']:
                if 'Color' in _dnm.inputs and not _dnm.inputs['Color'].is_linked:
                    nt.nodes.remove(_dnm)

    # Remove unlinked orphan TEX_IMAGE nodes (keep connected Base Color / Alpha / Normal nodes)
    for nd in list(nt.nodes):
        if nd.type == 'TEX_IMAGE' and not any(out.is_linked for out in nd.outputs):
            nt.nodes.remove(nd)

    if bsdf:
        _rough = (0.42 if any(t in ml for t in ('weapon', 'blade', 'metal', 'armor')) else 0.65) if _is_char_dir else 0.92
        _spec = (0.50 if any(t in ml for t in ('weapon', 'blade', 'metal', 'armor')) else 0.35) if _is_char_dir else 0.05
        if 'Roughness' in bsdf.inputs and not bsdf.inputs['Roughness'].is_linked:
            bsdf.inputs['Roughness'].default_value = _rough
        if 'Metallic' in bsdf.inputs and not bsdf.inputs['Metallic'].is_linked:
            bsdf.inputs['Metallic'].default_value = 0.0
        for _sp_k in ('Specular IOR Level', 'Specular'):
            if _sp_k in bsdf.inputs and not bsdf.inputs[_sp_k].is_linked:
                bsdf.inputs[_sp_k].default_value = _spec
    if bsdf and 'Emission Color' in bsdf.inputs and not bsdf.inputs['Emission Color'].is_linked:
        if 'Emission Strength' in bsdf.inputs:
            bsdf.inputs['Emission Strength'].default_value = 0.0
        bsdf.inputs['Emission Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    if bsdf and bsdf.inputs['Base Color'].is_linked:
        dc = mat.diffuse_color
        if any(abs(float(dc[k]) - 1.0) > 0.02 for k in range(3)):
            src_sock = bsdf.inputs['Base Color'].links[0].from_socket
            if src_sock.node.type != 'MIX':
                mx = nt.nodes.new('ShaderNodeMix')
                mx.data_type = 'RGBA'
                mx.blend_type = 'MULTIPLY'
                mx.inputs[0].default_value = 1.0
                mx.inputs[7].default_value = (float(dc[0]), float(dc[1]), float(dc[2]), 1.0)
                nt.links.new(src_sock, mx.inputs[6])
                nt.links.new(mx.outputs[2], bsdf.inputs['Base Color'])
    if any(t in ml for t in ('hair', 'lash', 'leaf', 'tree', 'plant', 'bush', 'grass', 'foliage', 'sky', 'decal')):
        mat.use_backface_culling = False

# Bake exported Unity .anim AnimationClips into Blender Actions & NLA Tracks
_baked_actions = []
if _is_char_dir and _master_arm is not None and _anim_dir and os.path.isdir(_anim_dir):
    _anim_files = sorted(_Path(_anim_dir).rglob('*.anim'))
    if _anim_files:
        def _anim_prio(_p):
            _nl = _p.stem.lower()
            _rank = 5
            if 'idle' in _nl or 'maincity' in _nl:
                _rank = 0
            elif any(k in _nl for k in ('stand', 'walk', 'run', 'sprint')):
                _rank = 1
            elif any(k in _nl for k in ('appear', 'win', 'select', 'switch')):
                _rank = 2
            elif any(k in _nl for k in ('attack', 'skill', 'special', 'ultimate')):
                _rank = 3
            try:
                _big = 0 if _p.stat().st_size <= 5 * 1024 * 1024 else 1
            except OSError:
                _big = 0
            return (_rank, _big, len(_nl), _nl)
        _anim_files.sort(key=_anim_prio)
        _anim_files = _anim_files[:max(1, _max_anims)]

        try:
            from bpy_extras import anim_utils as _anim_utils
        except Exception:
            _anim_utils = None
        import math as _math

        def _parse_anim_section(lines, start_idx, is_quat):
            curves = {}
            leaf_entries = []
            leaf_counts = {}
            i = start_idx + 1
            n = len(lines)
            base_indent = len(lines[start_idx]) - len(lines[start_idx].lstrip())
            cur_t, cur_v, ct = [], [], None
            while i < n:
                ln = lines[i]
                s = ln.strip()
                if not s:
                    i += 1
                    continue
                if (len(ln) - len(ln.lstrip())) <= base_indent and not s.startswith('-'):
                    break
                if s.startswith('- curve:'):
                    cur_t, cur_v, ct = [], [], None
                elif s.startswith('time:'):
                    try:
                        ct = float(s.split(':', 1)[1].strip())
                    except ValueError:
                        ct = None
                elif s.startswith('value:') and ct is not None:
                    _lb, _rb = s.find('{'), s.rfind('}')
                    if _lb >= 0 and _rb > _lb:
                        d = {}
                        for _pt in s[_lb + 1:_rb].split(','):
                            if ':' in _pt:
                                _k, _v = _pt.split(':', 1)
                                try:
                                    d[_k.strip()] = float(_v.strip())
                                except ValueError:
                                    pass
                        if d:
                            cur_t.append(ct)
                            if is_quat:
                                cur_v.append((d.get('x', 0.0), d.get('y', 0.0), d.get('z', 0.0), d.get('w', 1.0)))
                            else:
                                cur_v.append((d.get('x', 0.0), d.get('y', 0.0), d.get('z', 0.0)))
                    ct = None
                elif s.startswith('path:'):
                    rp = s.split(':', 1)[1].strip().strip("'\"")
                    if cur_t:
                        curves[rp] = (cur_t, cur_v)
                        lf = rp.rsplit('/', 1)[-1]
                        if lf:
                            leaf_counts[lf] = leaf_counts.get(lf, 0) + 1
                            leaf_entries.append((lf, cur_t, cur_v))
                    cur_t, cur_v, ct = [], [], None
                i += 1
            for lf, ta, va in leaf_entries:
                if leaf_counts.get(lf, 0) == 1 and lf not in curves:
                    curves[lf] = (ta, va)
            return curves, i

        def _euler_deg_to_quat(vals):
            out = []
            for ex, ey, ez in vals:
                rx = _math.radians(ex) * 0.5
                ry = _math.radians(ey) * 0.5
                rz = _math.radians(ez) * 0.5
                cx, sx = _math.cos(rx), _math.sin(rx)
                cy, sy = _math.cos(ry), _math.sin(ry)
                cz, sz = _math.cos(rz), _math.sin(rz)
                out.append((
                    cy * sx * cz + sy * cx * sz,
                    sy * cx * cz - cy * sx * sz,
                    cy * cx * sz - sy * sx * cz,
                    cy * cx * cz + sy * sx * sz,
                ))
            return out

        def _load_unity_anim_fast(apath):
            lines = apath.read_text(encoding='utf-8', errors='ignore').splitlines()
            cname = apath.stem
            rot_c, eul_c, pos_c = {}, {}, {}
            i, n = 0, len(lines)
            while i < n:
                s = lines[i].strip()
                if s.startswith('m_Name:') and cname == apath.stem:
                    v = s.split(':', 1)[1].strip().strip("'\"")
                    if v:
                        cname = v
                elif s.startswith('m_RotationCurves:'):
                    c, i = _parse_anim_section(lines, i, True)
                    rot_c.update(c)
                    continue
                elif s.startswith('m_EulerCurves:'):
                    c, i = _parse_anim_section(lines, i, False)
                    eul_c.update(c)
                    continue
                elif s.startswith('m_PositionCurves:'):
                    c, i = _parse_anim_section(lines, i, False)
                    pos_c.update(c)
                    continue
                i += 1
            for k, (ta, ev) in eul_c.items():
                if k not in rot_c and ev:
                    rot_c[k] = (ta, _euler_deg_to_quat(ev))
            return cname, rot_c, pos_c

        # Build bone lookup table (names, paths, CRC32 hashes, and raw local rest transforms)
        _arm_scale = sum(abs(float(v)) for v in _master_arm.matrix_world.to_scale()) / 3.0
        _pos_scale = (1.0 / _arm_scale) if (0.005 < _arm_scale < 0.2) else 1.0
        _prefixes = ['', f'{_char_name}_Model/', f'{_char_name}/', 'Root/', 'Model/']
        _bone_records = []
        for _b in _master_arm.data.bones:
            pts = []
            cur = _b
            while cur is not None:
                pts.append(cur.name)
                cur = cur.parent
            pts.reverse()
            cands = [_b.name]
            for k in range(len(pts)):
                sub = '/'.join(pts[k:])
                cands.append(sub)
                for _pf in _prefixes:
                    full_sub = _pf + sub
                    cands.append(f'path_{_zlib.crc32(full_sub.encode("utf-8"))}')
            loc_m = (_b.parent.matrix_local.inverted() @ _b.matrix_local) if _b.parent else _b.matrix_local
            rest_q = loc_m.to_quaternion()
            inv_rest_q = rest_q.inverted()
            rest_t = loc_m.translation.copy()
            _bone_records.append((_b.name, list(dict.fromkeys(cands)), rest_q, inv_rest_q, rest_t))

        if _master_arm.animation_data is None:
            _master_arm.animation_data_create()
        try:
            bpy.context.scene.render.fps = 60
            bpy.context.scene.render.fps_base = 1.0
        except Exception:
            pass

        for _ap in _anim_files:
            try:
                _cname, _rot_c, _pos_c = _load_unity_anim_fast(_ap)
                if not _rot_c and not _pos_c:
                    continue
                _short_name = _cname
                if _char_name and _short_name.startswith(_char_name + '_Ani_'):
                    _short_name = _short_name[len(_char_name) + 5:]
                elif _char_name and _short_name.startswith(_char_name + '_'):
                    _short_name = _short_name[len(_char_name) + 1:]
                _act = bpy.data.actions.new(name=_short_name or _cname)
                _act.use_fake_user = True
                _slot = None
                if hasattr(_act, 'slots') and _anim_utils is not None:
                    _slot = _act.slots.new('OBJECT', _master_arm.name)
                    _master_arm.animation_data.action = _act
                    _master_arm.animation_data.action_slot = _slot
                    _fcurves = _anim_utils.action_ensure_channelbag_for_slot(_act, _slot).fcurves
                else:
                    _fcurves = _act.fcurves

                _bone_tracks = []
                for _bname, _cands, _rq, _inv_rq, _rt in _bone_records:
                    _rc_hit = next((_rot_c[_c] for _c in _cands if _c in _rot_c), None)
                    _pc_hit = next((_pos_c[_c] for _c in _cands if _c in _pos_c), None)
                    if _rc_hit is None and _pc_hit is None:
                        continue
                    _dqs = None
                    if _rc_hit is not None:
                        _ts, _qvs = _rc_hit
                        if len(_ts) > 120:
                            _step = max(1, len(_ts) // 90)
                            _idx = list(range(0, len(_ts) - 1, _step)) + [len(_ts) - 1]
                            _ts = [_ts[i] for i in _idx]
                            _qvs = [_qvs[i] for i in _idx]
                        _prev_q = None
                        _rows = []
                        for _t, (_qx, _qy, _qz, _qw) in zip(_ts, _qvs):
                            _uq = _Quat((_qw, _qx, -_qy, -_qz))
                            _dq = _inv_rq @ _uq
                            if _prev_q is not None and _prev_q.dot(_dq) < 0.0:
                                _dq = -_dq
                            _prev_q = _dq
                            _rows.append((1.0 + _t * 60.0, _dq))
                        _dqs = _rows
                        _bname_q = _bname.replace('\\', '\\\\').replace('"', '\\"')
                        _dp = f'pose.bones["{_bname_q}"].rotation_quaternion'
                        for _ci, _attr_i in enumerate((0, 1, 2, 3)):
                            _fc = _fcurves.new(data_path=_dp, index=_attr_i)
                            _fc.keyframe_points.add(len(_rows))
                            _co = []
                            for _fr, _dq in _rows:
                                _co.append(_fr)
                                _co.append(float(_dq[_attr_i]))
                            _fc.keyframe_points.foreach_set('co', _co)
                            _fc.update()

                    if _pc_hit is not None:
                        _pts, _pvs = _pc_hit
                        _is_root_bone = (_bname.lower() in ('bip001', 'root', 'bone_root', 'bip001 pelvis'))
                        if not _is_root_bone:
                            _max_pd = max(
                                (_Vec((-_px * _pos_scale, _py * _pos_scale, _pz * _pos_scale)) - _rt).length
                                for _px, _py, _pz in _pvs[::max(1, len(_pvs) // 8)]
                            ) if _pvs else 0.0
                            _include_pos = _max_pd > 1e-3
                        else:
                            _include_pos = True
                        if _include_pos and _pvs:
                            if len(_pts) > 120:
                                _step = max(1, len(_pts) // 90)
                                _idx = list(range(0, len(_pts) - 1, _step)) + [len(_pts) - 1]
                                _pts = [_pts[i] for i in _idx]
                                _pvs = [_pvs[i] for i in _idx]
                            _prows = []
                            for _t, (_px, _py, _pz) in zip(_pts, _pvs):
                                _up = _Vec((-_px * _pos_scale, _py * _pos_scale, _pz * _pos_scale))
                                _dl = _inv_rq @ (_up - _rt)
                                _prows.append((1.0 + _t * 60.0, _dl))
                            _dp = f'pose.bones["{_bname_q}"].location'
                            for _ci in (0, 1, 2):
                                _fc = _fcurves.new(data_path=_dp, index=_ci)
                                _fc.keyframe_points.add(len(_prows))
                                _co = []
                                for _fr, _dl in _prows:
                                    _co.append(_fr)
                                    _co.append(float(_dl[_ci]))
                                _fc.keyframe_points.foreach_set('co', _co)
                                _fc.update()
                    _bone_tracks.append((_bname, _dqs))

                if len(_fcurves) > 0:
                    _tr = _master_arm.animation_data.nla_tracks.new()
                    _tr.name = _act.name
                    _st = _tr.strips.new(_act.name, 1, _act)
                    if hasattr(_st, 'action_slot') and _slot is not None:
                        _st.action_slot = _slot
                    _tr.mute = True
                    _baked_actions.append((_act, _slot))
                else:
                    bpy.data.actions.remove(_act, do_unlink=True)
            except Exception as _ae:
                print(f'WARN anim bake {_ap.name}: {_ae}', flush=True)

        _master_arm.animation_data.action = None
        for _pb in _master_arm.pose.bones:
            _pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            _pb.location = (0.0, 0.0, 0.0)
            _pb.scale = (1.0, 1.0, 1.0)
        print(f'Baked {len(_baked_actions)} animation Action(s) onto {_master_arm.name}', flush=True)

try:
    bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
except Exception:
    pass

# Export GLB (preserving raw Unity bone axes for OpenZenless 3D Viewer & embedded Actions/Morphs)
_gltf_kw = {'filepath': _out_glb, 'export_format': 'GLB'}
if _is_char_dir:
    try:
        _grna = bpy.ops.export_scene.gltf.get_rna_type().properties
        if 'export_skins' in _grna:
            _gltf_kw['export_skins'] = True
        if 'export_morph' in _grna:
            _gltf_kw['export_morph'] = True
        if 'export_morph_normal' in _grna:
            _gltf_kw['export_morph_normal'] = True
        if _baked_actions:
            if 'export_animations' in _grna:
                _gltf_kw['export_animations'] = True
            if 'export_force_sampling' in _grna:
                _gltf_kw['export_force_sampling'] = False
            if 'export_animation_mode' in _grna:
                _gltf_kw['export_animation_mode'] = 'ACTIONS'
            elif 'export_nla_strips' in _grna:
                _gltf_kw['export_nla_strips'] = True
    except Exception:
        pass
bpy.ops.export_scene.gltf(**_gltf_kw)

# Convert master armature bone orientations to clean Blender limb-aligned orientations + conjugate Actions & save .blend
if _save_blend:
    if _master_arm is not None and fbxs:
        try:
            _raw_mats = {b.name: b.matrix_local.copy() for b in _master_arm.data.bones}
            _raw_parents = {b.name: (b.parent.name if b.parent else None) for b in _master_arm.data.bones}
            _before_tmp = set(bpy.data.objects)
            _raw_s = sum(abs(float(v)) for v in _master_arm.matrix_world.to_scale()) / 3.0
            _imp_s = None
            try:
                _fbx_import_call(fbxs[0], auto_orient=True)
                _tmp_new = [o for o in bpy.data.objects if o not in _before_tmp]
                _tmp_arms = [o for o in _tmp_new if o.type == 'ARMATURE']
                if _tmp_arms:
                    _ts = min((max(abs(float(v)) for v in o.matrix_world.to_scale()) for o in _tmp_arms), default=1.0)
                    if 0.0 < _ts < 0.1 and _raw_s > 0.2:
                        for o in _tmp_new:
                            bpy.data.objects.remove(o, do_unlink=True)
                        _fbx_import_call(fbxs[0], global_scale=1.0 / _ts, auto_orient=True)
                        _tmp_new = [o for o in bpy.data.objects if o not in _before_tmp]
                        _tmp_arms = [o for o in _tmp_new if o.type == 'ARMATURE']
                _auto_arm = max(_tmp_arms, key=lambda a: len(a.data.bones), default=None) if _tmp_arms else None
                if _auto_arm is not None:
                    _auto_info = {
                        _re.sub(r'\.[0-9]+$', '', b.name): (b.head_local.copy(), b.tail_local.copy(), b.matrix_local.copy())
                        for b in _auto_arm.data.bones
                    }
                    bpy.context.view_layer.objects.active = _master_arm
                    bpy.ops.object.mode_set(mode='EDIT')
                    for _eb in _master_arm.data.edit_bones:
                        _ai = _auto_info.get(_eb.name)
                        if _ai is not None:
                            _eb.head = _ai[0]
                            _eb.tail = _ai[1]
                            if (_eb.tail - _eb.head).length < 1e-5:
                                _eb.tail = _eb.head + _Vec((0.0, 0.02, 0.0))
                            try:
                                _, _roll = _ai[2].to_3x3().to_quaternion(), 0.0
                                _eb.align_roll((_ai[2].to_3x3() @ _Vec((0.0, 0.0, 1.0))))
                            except Exception:
                                pass
                    bpy.ops.object.mode_set(mode='OBJECT')

                    # Conjugate all baked rotation_quaternion & location FCurves from raw axes -> auto axes
                    _D_q = {}
                    _D_loc = {}
                    for _b in _master_arm.data.bones:
                        _rm = _raw_mats.get(_b.name)
                        if _rm is None:
                            continue
                        _D_q[_b.name] = (_b.matrix_local.to_3x3().inverted() @ _rm.to_3x3()).to_quaternion()
                        _pn = _raw_parents.get(_b.name)
                        _rp = _raw_mats.get(_pn) if _pn else None
                        if _rp is not None:
                            _D_loc[_b.name] = (_b.matrix_local.to_3x3().inverted() @ _rp.to_3x3()) @ _rm.to_3x3().inverted()
                        else:
                            _D_loc[_b.name] = _b.matrix_local.to_3x3().inverted()

                    if _baked_actions:
                        from bpy_extras import anim_utils as _anim_utils
                    for _act, _slot in _baked_actions:
                        _fcs = _anim_utils.action_ensure_channelbag_for_slot(_act, _slot).fcurves if (hasattr(_act, 'slots') and _slot is not None) else _act.fcurves
                        _by_bone_q = {}
                        _by_bone_p = {}
                        for _fc in _fcs:
                            _dp = _fc.data_path
                            if _dp.startswith('pose.bones[') and _dp.endswith('].rotation_quaternion'):
                                _bn = _json.loads(_dp[len('pose.bones['):-len('].rotation_quaternion')])
                                _by_bone_q.setdefault(_bn, {})[_fc.array_index] = _fc
                            elif _dp.startswith('pose.bones[') and _dp.endswith('].location'):
                                _bn = _json.loads(_dp[len('pose.bones['):-len('].location')])
                                _by_bone_p.setdefault(_bn, {})[_fc.array_index] = _fc
                        for _bn, _imap in _by_bone_q.items():
                            _dq_conv = _D_q.get(_bn)
                            if _dq_conv is None or len(_imap) != 4:
                                continue
                            _dq_inv = _dq_conv.inverted()
                            _npts = len(_imap[0].keyframe_points)
                            _bufs = []
                            for _ci in range(4):
                                _arr = [0.0] * (_npts * 2)
                                _imap[_ci].keyframe_points.foreach_get('co', _arr)
                                _bufs.append(_arr)
                            for _ki in range(_npts):
                                _vi = _ki * 2 + 1
                                _q = _Quat((_bufs[0][_vi], _bufs[1][_vi], _bufs[2][_vi], _bufs[3][_vi]))
                                _nq = _dq_conv @ _q @ _dq_inv
                                for _ci in range(4):
                                    _bufs[_ci][_vi] = float(_nq[_ci])
                            for _ci in range(4):
                                _imap[_ci].keyframe_points.foreach_set('co', _bufs[_ci])
                                _imap[_ci].update()
                        for _bn, _imap in _by_bone_p.items():
                            _rm = _raw_mats.get(_bn)
                            _b_final = _master_arm.data.bones.get(_bn)
                            if _rm is None or _b_final is None or len(_imap) != 3:
                                continue
                            _M_p = _b_final.matrix_local.to_3x3().inverted() @ _rm.to_3x3()
                            _npts = len(_imap[0].keyframe_points)
                            _bufs = []
                            for _ci in range(3):
                                _arr = [0.0] * (_npts * 2)
                                _imap[_ci].keyframe_points.foreach_get('co', _arr)
                                _bufs.append(_arr)
                            for _ki in range(_npts):
                                _vi = _ki * 2 + 1
                                _v = _M_p @ _Vec((_bufs[0][_vi], _bufs[1][_vi], _bufs[2][_vi]))
                                for _ci in range(3):
                                    _bufs[_ci][_vi] = float(_v[_ci])
                            for _ci in range(3):
                                _imap[_ci].keyframe_points.foreach_set('co', _bufs[_ci])
                                _imap[_ci].update()
            finally:
                for o in [obj for obj in bpy.data.objects if obj not in _before_tmp and obj != _master_arm]:
                    try:
                        bpy.data.objects.remove(o, do_unlink=True)
                    except Exception:
                        pass
                try:
                    bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
                except Exception:
                    pass
        except Exception as _be:
            print(f'WARN auto-orient .blend step: {_be}', flush=True)

    try:
        bpy.ops.file.pack_all()
    except Exception as _pe:
        print(f'WARN pack_all: {_pe}', flush=True)
    bpy.ops.wm.save_as_mainfile(filepath=_save_blend, compress=True)
    print(f'Saved pre-rigged Blender project -> {_save_blend}', flush=True)
'''


def _blender_convert_chunk(
    blender: str,
    char_dir: Path,
    char_name: str,
    fbxs: list[Path],
    objs: list[Path],
    out_glb: Path,
    mat_to_tex: dict[str, str],
    allow_fuzzy_fallback: bool,
    log_cb=None,
    anim_dir: Path | None = None,
    save_blend: Path | None = None,
    is_char: bool | None = None,
) -> int:
    try:
        max_anims = int(os.environ.get("OPENZENLESS_MAX_BAKED_ANIMS", "16"))
    except ValueError:
        max_anims = 16
    cfg_dict = {
        "fbxs": [str(p) for p in fbxs],
        "objs": [str(p) for p in objs],
        "char_dir": str(char_dir),
        "char_name": char_name,
        "out_glb": str(out_glb),
        "save_blend": str(save_blend) if save_blend else "",
        "anim_dir": str(anim_dir) if (anim_dir and anim_dir.is_dir()) else "",
        "is_char": bool(is_char if is_char is not None else char_name.startswith(("Avatar_", "Monster_", "Boss_", "NPC_", "Bangboo_", "Pet_", "Weapon_"))),
        "mat_to_tex": mat_to_tex,
        "allow_fuzzy": bool(allow_fuzzy_fallback),
        "max_anims": max_anims,
    }
    py_expr = f"import json as _init_json\n_CFG = _init_json.loads({json.dumps(cfg_dict)!r})\n" + _BLENDER_CONVERT_SCRIPT
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
        tf.write(py_expr)
        script_path = Path(tf.name)
    env = {**os.environ, **cfg.configure_gpu_env(apply=False)}
    try:
        return run_streaming_cmd([blender, "--background", "--python-exit-code", "1", "--python", str(script_path)], env=env, log_cb=log_cb)
    finally:
        script_path.unlink(missing_ok=True)


def _convert_dir_to_glb(
    char_dir: Path,
    char_name: str,
    log_cb=None,
    out_glb: Path | None = None,
    anim_dir: Path | None = None,
) -> int:
    blender = cfg.find_blender()
    if not blender:
        try:
            from . import tools

            blender = tools.ensure_blender(log_cb=log_cb)
        except Exception as exc:
            _emit(log_cb, f"[WARN] Blender auto-install skipped: {exc}")
            blender = None
    if not blender:
        _emit(log_cb, "WARNING: blender not found in PATH; skipping GLB conversion.")
        return 0
    fbxs = sorted(char_dir.rglob("*.fbx"))
    objs = sorted(char_dir.rglob("*.obj"))
    def _filter_models(paths: list[Path]) -> list[Path]:
        non_col = [p for p in paths if not any(t in p.stem.lower() for t in ("_col_", "_proxy"))]
        return non_col if non_col else paths
    def _dedup_cab_models(paths: list[Path]) -> list[Path]:
        seen: set[tuple[str, str, str]] = set()
        out: list[Path] = []
        for p in paths:
            cab_dir = next((part for part in p.parts if part.startswith("CAB-") or part.startswith("BuildPlayer-")), None)
            if cab_dir is not None:
                key = (cab_dir.lower(), p.parent.name.lower(), p.name.lower())
                if key in seen:
                    continue
                seen.add(key)
            out.append(p)
        return out
    fbxs = _dedup_cab_models(_filter_models(fbxs))
    objs = _dedup_cab_models(_filter_models(objs))
    def _fbx_priority(p: Path) -> tuple[int, str]:
        parts_low = [pt.lower() for pt in p.parts]
        if "streaming" in parts_low:
            return (0, str(p))
        if "basescene" in parts_low:
            return (1, str(p))
        if any(pt.startswith("extrascene") for pt in parts_low):
            return (2, str(p))
        return (1, str(p))
    fbxs.sort(key=_fbx_priority)
    eff_anim_dir = (
        anim_dir
        if (anim_dir and anim_dir.is_dir() and any(anim_dir.rglob("*.anim")))
        else (
            char_dir / "AnimationClip"
            if ((char_dir / "AnimationClip").is_dir() and any((char_dir / "AnimationClip").rglob("*.anim")))
            else None
        )
    )
    is_char = char_name.startswith(("Avatar_", "Monster_", "Boss_", "NPC_", "Bangboo_", "Pet_", "Weapon_")) or (eff_anim_dir is not None)
    if is_char:
        fbxs = [p for p in fbxs if "Animations" not in p.parts and "AnimationClip" not in p.parts]
        objs = [p for p in objs if "Animations" not in p.parts and "AnimationClip" not in p.parts]
        non_anim_fbxs = [p for p in fbxs if "Animator" not in p.parts]
        if non_anim_fbxs:
            fbxs = non_anim_fbxs
    if not fbxs and not objs:
        _emit(log_cb, f"No FBX/OBJ models found in {char_dir}; skipping GLB conversion.")
        return 0
    glb_path = out_glb or (char_dir / f"{char_name}.glb")
    blend_path = glb_path.with_suffix(".blend") if is_char else None
    cab_active = _zzz_cab_map_exists() and bool(fbxs)
    mat_to_tex, _, _ = ({}, [], []) if (cab_active and not is_char and not objs) else resolve_material_textures(char_dir)
    chunk_size = cfg.adaptive_blender_chunk_size()
    if is_char or len(fbxs) <= chunk_size:
        _emit(log_cb, f"Converting {len(fbxs)} FBX / {len(objs)} OBJ model(s) to GLB{' + .blend' if blend_path else ''} via Blender -> {glb_path.name}")
        return _blender_convert_chunk(
            blender, char_dir, char_name, fbxs, objs, glb_path, mat_to_tex,
            allow_fuzzy_fallback=(not cab_active or is_char), log_cb=log_cb,
            anim_dir=eff_anim_dir, save_blend=blend_path, is_char=is_char,
        )
    chunks = [fbxs[i : i + chunk_size] for i in range(0, len(fbxs), chunk_size)]
    max_w = min(len(chunks), cfg.adaptive_worker_count("blender", max_workers=4))
    _emit(log_cb, f"Converting {len(fbxs)} FBX model(s) in {len(chunks)} bounded Blender batch(es) (workers={max_w}) -> {glb_path.name}")
    tmp_dir = Path(tempfile.mkdtemp(prefix="oz_glb_chunks_"))
    log_lock = threading.Lock()

    def _safe_log(msg: str):
        if log_cb is not None:
            with log_lock:
                log_cb(msg)

    def _run_chunk(idx_cf: tuple[int, list[Path]]) -> tuple[int, int, Path]:
        idx, c_fbxs = idx_cf
        c_glb = tmp_dir / f"chunk_{idx:03d}.glb"
        _emit(_safe_log, f"[Batch {idx + 1}/{len(chunks)}] Converting {len(c_fbxs)} FBXs...")
        rc = _blender_convert_chunk(
            blender, char_dir, char_name, c_fbxs, [], c_glb, mat_to_tex,
            allow_fuzzy_fallback=(not cab_active), log_cb=_safe_log,
        )
        return idx, rc, c_glb

    try:
        results: dict[int, tuple[int, Path]] = {}
        if max_w > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as pool:
                for idx, rc, c_glb in pool.map(_run_chunk, enumerate(chunks)):
                    results[idx] = (rc, c_glb)
        else:
            for item in enumerate(chunks):
                idx, rc, c_glb = _run_chunk(item)
                results[idx] = (rc, c_glb)
        chunk_glbs: list[Path] = []
        for idx in range(len(chunks)):
            rc, c_glb = results[idx]
            if rc != 0 or not c_glb.is_file():
                return rc or 1
            chunk_glbs.append(c_glb)
        return _merge_glbs(chunk_glbs, glb_path, log_cb=log_cb)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _organize_character_stage(stage_dir: Path, dest: Path) -> None:
    """Arrange staged BySource character exports into canonical ByType folders."""
    tex_exts = {".png", ".tga", ".dds", ".jpg", ".jpeg", ".webp"}
    for p in sorted(stage_dir.rglob("*")):
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        targets: list[Path] = []
        if suf in (".fbx", ".obj"):
            targets.append(dest / "Animator" / p.name)
        elif suf == ".json" and (p.parent.name == "Materials" or p.stem.startswith("MAT_")):
            targets.append(dest / "Material" / p.name)
            targets.append(dest / "Animator" / "Materials" / p.name)
        elif suf in tex_exts:
            targets.append(dest / "Texture2D" / p.name)
            targets.append(dest / "Animator" / p.name)
        elif suf in (".shader", ".txt"):
            targets.append(dest / "Shader" / p.name)
        elif suf == ".anim":
            targets.append(dest / "AnimationClip" / p.name)
        for dst in targets:
            if suf in tex_exts and dst.is_file():
                try:
                    if dst.stat().st_size > p.stat().st_size:
                        continue
                except OSError:
                    pass
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)


def collect_entry_required_blks(
    entry: dict,
    include_anims: bool = True,
    include_deps: bool = True,
) -> list[str]:
    """Collect unique referenced `.blk` names for a character/entity index record."""
    refs: list[str] = []
    seen: set[str] = set()

    def _add(val: str):
        if val:
            b = Path(str(val).replace("\\", "/")).name
            if b and b not in seen:
                seen.add(b)
                refs.append(b)

    _add(entry.get("prefab_blk", ""))
    for b in entry.get("mesh_blks") or ():
        _add(b)
    for b in entry.get("blocks") or ():
        _add(b)
    if include_deps:
        for b in entry.get("dep_blks") or ():
            _add(b)
    if include_anims:
        for b in entry.get("anim_blks") or ():
            _add(b)
    return refs


def collect_map_required_blks(stage_name: str, entry: dict) -> list[str]:
    """Collect unique referenced `.blk` names (base scene + streaming + dependencies) for an OZ map entry."""
    refs: list[str] = []
    seen: set[str] = set()

    def _add(val: str):
        if val:
            b = Path(str(val).replace("\\", "/")).name
            if b and b not in seen:
                seen.add(b)
                refs.append(b)

    st = entry.get("streaming") or {}
    if isinstance(st, dict):
        _add(st.get("manifest_blk", ""))
        for b in st.get("blks") or ():
            _add(b)
    bs = entry.get("base_scene") or {}
    if isinstance(bs, dict):
        _add(bs.get("blk", ""))
    for es in entry.get("extra_scenes") or ():
        if isinstance(es, dict):
            _add(es.get("blk", ""))
    for b in entry.get("dep_blks") or ():
        _add(b)
    if not refs and stage_name:
        for b in load_stage_index().get(stage_name, ()):
            _add(b)
    return refs


def ensure_required_blks(
    game_root: Path | None,
    blk_refs: Sequence[str],
    path_hashes: Sequence[int | str] = (),
    bundle_hashes: Sequence[int | str] = (),
    include_deps: bool = True,
    log_cb=None,
    label: str = "",
    sync_index: bool = True,
) -> list[Path]:
    """Resolve required `.blk` paths locally and auto-pull missing containers via HoYoverse CDN if enabled."""
    root = game_root or cfg.GAME_ROOT
    clean_refs: list[str] = []
    seen_ref: set[str] = set()
    for b in blk_refs:
        if not b:
            continue
        s = str(b).replace("\\", "/").strip()
        if s and s not in seen_ref:
            seen_ref.add(s)
            clean_refs.append(s)

    local_paths: list[Path] = []
    seen_local: set[Path] = set()
    missing_refs: list[str] = []
    for bref in clean_refs:
        rp = cfg.resolve_blk_path(root, bref)
        if rp is not None and rp.is_file():
            try:
                rrp = rp.resolve()
            except Exception:
                rrp = rp
            if rrp not in seen_local:
                seen_local.add(rrp)
                local_paths.append(rp)
        else:
            missing_refs.append(bref)

    pulled: list[Path] = []
    need_pull = bool(missing_refs) or (not local_paths and (bool(path_hashes) or bool(bundle_hashes)))
    if need_pull and cfg.is_auto_cdn_pull_enabled():
        tag = label or "export"
        _emit(
            log_cb,
            f"[CDN] {tag}: {len(missing_refs)}/{len(clean_refs)} .blk ref(s) missing locally — checking HoYoverse CDN ({cfg.get_cdn_region()})...",
        )
        try:
            pulled = cdn_downloader.resolve_and_download_targeted_blks(
                root,
                blk_names=missing_refs or clean_refs,
                path_hashes=path_hashes,
                bundle_hashes=bundle_hashes,
                include_deps=include_deps,
                log_cb=log_cb,
            )
        except Exception as exc:
            _emit(log_cb, f"[CDN] WARNING: targeted .blk pull failed ({exc}); continuing with local files.")

    all_paths: list[Path] = []
    seen_all: set[Path] = set()
    for p in (*local_paths, *pulled):
        if not p.is_file():
            continue
        try:
            rp = p.resolve()
        except Exception:
            rp = p
        if rp not in seen_all:
            seen_all.add(rp)
            all_paths.append(p)

    if sync_index and all_paths:
        if pulled or cfg.is_cdn_workspace_root(root) or not _zzz_cab_map_exists():
            sync_ondemand_blks_index(root, pulled or all_paths, log_cb=log_cb)
        else:
            common_base, _, _ = cfg.collect_ordered_blks_with_prio(root, use_cache=True)
            _retarget_cab_map_base(cfg.CLI_DIR / "Maps" / "ZZZ.bin", cfg.to_cli_path(common_base))
    return all_paths


def _export_oz_entry(
    matched_name: str,
    entry: dict,
    all_entries: dict[str, dict],
    out_dir: Path | None = None,
    convert_glb: bool = False,
    log_cb=None,
    game_root: Path | None = None,
    export_shaders: bool = False,
    fallback_subdir: str = "",
    entity_category: str = "",
) -> int:
    root = game_root or cfg.GAME_ROOT
    req_blks = collect_entry_required_blks(entry, include_anims=True, include_deps=True)
    p_hashes = [h for h in [entry.get("prefab_key"), *(entry.get("anim_keys") or ())] if h]
    b_hashes = list(entry.get("bundle_hashes") or ())
    if req_blks or p_hashes or b_hashes:
        ensure_required_blks(
            root,
            req_blks,
            path_hashes=p_hashes,
            bundle_hashes=b_hashes,
            include_deps=True,
            log_cb=log_cb,
            label=matched_name,
            sync_index=True,
        )
        # Reload entry/all_entries in case on-demand CDN pull updated CAB/block pointers cleanly
        if entity_category:
            refreshed = load_oz_entities_index().get(entity_category) or {}
            if refreshed:
                all_entries = refreshed
                entry = refreshed.get(matched_name, entry)
        else:
            refreshed = load_oz_character_index()
            if refreshed:
                all_entries = refreshed
                entry = refreshed.get(matched_name, entry)
    base_out = out_dir or (cfg.FINAL_ROOT / "Exports")
    if fallback_subdir and base_out.name.lower() != fallback_subdir.lower():
        dest = base_out / fallback_subdir / matched_name
    else:
        dest = base_out / matched_name
    dest.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix="_oz_char_stage_", dir=dest))

    def _model_outputs() -> list[Path]:
        return [
            p for p in stage_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in (".fbx", ".obj")
        ]

    def _find_variant() -> tuple[str, dict] | None:
        base_mesh = set(entry.get("mesh_blks") or [])
        name_low = matched_name.lower()
        best = None
        for key, cand in all_entries.items():
            if key == matched_name or not key.lower().startswith(name_low):
                continue
            cand_mesh = set(cand.get("mesh_blks") or [])
            if not cand_mesh or (len(base_mesh) > 1 and not (base_mesh & cand_mesh)):
                continue
            score = (len(cand_mesh), len(cand.get("extra_cabs") or []))
            if best is None or score > best[0]:
                best = (score, key, cand)
        return (best[1], best[2]) if best is not None else None

    def _export_prefab(cand_entry: dict) -> int:
        prefab_path = cfg.resolve_blk_path(root, cand_entry.get("prefab_blk", ""))
        if prefab_path is None:
            return 1
        extra_cabs_str = ",".join(cand_entry.get("extra_cabs") or [])
        prefab_cab = cand_entry.get("prefab_cab", "")
        prefab_key = cand_entry.get("prefab_key", "")
        _emit(
            log_cb,
            f"Exporting {matched_name} ({prefab_path.name}, cab={prefab_cab}, "
            f"+{len(cand_entry.get('extra_cabs') or [])} SeparateMesh CABs) -> {dest}",
        )
        hash_file: Path | None = None
        try:
            if prefab_key:
                with tempfile.NamedTemporaryFile("w", suffix=".hashes", delete=False, encoding="utf-8") as hf:
                    hf.write(str(prefab_key) + "\n")
                    hash_file = Path(hf.name)
            types_list = ["GameObject"] + (["Shader"] if export_shaders else [])
            return export_blk_asset_types(
                prefab_path, stage_dir,
                types=types_list,
                export_mode="Convert",
                log_cb=log_cb,
                map_op="CABMap",
                map_name="ZZZ",
                diffuse_only=False,
                skip_proxies=False,
                container_hash_file=hash_file,
                cab_filter=prefab_cab,
                extra_cabs=extra_cabs_str,
                group_assets="BySource",
                skip_animations=True,
            )
        finally:
            if hash_file is not None:
                hash_file.unlink(missing_ok=True)

    def _export_asset_map_blks(cand_entry: dict) -> int:
        blk_refs = [
            cand_entry.get("prefab_blk", ""),
            *(cand_entry.get("mesh_blks") or []),
            *(cand_entry.get("blocks") or []),
        ]
        resolved: list[Path] = []
        seen_p: set[Path] = set()
        for bref in blk_refs:
            rp = cfg.resolve_blk_path(root, bref)
            if rp is not None:
                try:
                    rrp = rp.resolve()
                except Exception:
                    rrp = rp
                if rrp not in seen_p:
                    seen_p.add(rrp)
                    resolved.append(rp)
        if not resolved:
            _emit(log_cb, f"ERROR: No .blk files resolved for {matched_name} under {root}")
            return 1
        _emit(log_cb, f"Exporting {matched_name} from {len(resolved)} .blk(s) (asset_map) -> {dest}")
        types_list = ["Animator", "Avatar", "Mesh", "Texture2D", "Material"]
        if export_shaders:
            types_list.append("Shader")
        pats = [rf"(?:^|_){re.escape(matched_name)}(?:$|_)", re.escape(matched_name)]
        rc = 1
        for pat in pats:
            rc = export_blks_batch(
                resolved, stage_dir,
                types=types_list,
                export_mode="Convert",
                name_filter=pat,
                log_cb=log_cb,
                diffuse_only=False,
                group_assets="BySource",
                skip_animations=True,
            )
            if _model_outputs():
                return rc
        return rc

    chosen_entry = entry
    last_rc = 0
    try:
        use_cabmap = bool(entry.get("prefab_cab")) and _zzz_cab_map_exists()
        model_outputs: list[Path] = []
        chosen_name = matched_name

        if use_cabmap:
            candidates: list[tuple[str, dict]] = [(matched_name, entry)]
            variant = _find_variant()
            if variant is not None:
                candidates.append(variant)
            for idx, (cand_name, cand_entry) in enumerate(candidates):
                last_rc = _export_prefab(cand_entry)
                model_outputs = _model_outputs()
                if model_outputs:
                    chosen_name, chosen_entry = cand_name, cand_entry
                    break
                if idx + 1 < len(candidates):
                    _emit(
                        log_cb,
                        f"WARNING: no mesh FBX/OBJ produced for {cand_name}; "
                        f"retrying with {candidates[idx + 1][0]}",
                    )
        else:
            last_rc = _export_asset_map_blks(entry)
            model_outputs = _model_outputs()

        if not model_outputs:
            _emit(
                log_cb,
                f"ERROR: no mesh FBX/OBJ produced for {matched_name}; "
                "the selected prefab contains no exportable mesh.",
            )
            return last_rc or 2
        if chosen_name != matched_name:
            _emit(log_cb, f"Using fallback prefab {chosen_name} for {matched_name}.")

        # Export AnimationClips first so Blender conversion can bake them into Actions & NLA tracks
        anim_entry = entry if (entry.get("anim_blks") or []) else chosen_entry
        anim_blks = [
            p for b in (anim_entry.get("anim_blks") or [])
            if (p := cfg.resolve_blk_path(root, b)) is not None
        ]
        if anim_blks:
            anim_keys = anim_entry.get("anim_keys") or []
            anim_hash_file: Path | None = None
            try:
                if anim_keys:
                    with tempfile.NamedTemporaryFile("w", suffix=".hashes", delete=False, encoding="utf-8") as ahf:
                        ahf.write("\n".join(str(k) for k in anim_keys) + "\n")
                        anim_hash_file = Path(ahf.name)
                rc_anim = export_blks_batch(
                    anim_blks, dest,
                    types=["AnimationClip"],
                    export_mode="Convert",
                    name_filter="" if anim_keys else re.escape(matched_name),
                    log_cb=log_cb,
                    use_cab_map=_zzz_cab_map_exists(),
                    container_hash_file=anim_hash_file,
                    group_assets="ByType",
                )
                if rc_anim != 0:
                    _emit(log_cb, f"WARNING: AnimationClip export returned rc={rc_anim} (some clips could not be decoded).")
            finally:
                if anim_hash_file is not None:
                    anim_hash_file.unlink(missing_ok=True)

        if convert_glb and last_rc == 0:
            rc_glb = _convert_dir_to_glb(
                stage_dir,
                matched_name,
                log_cb=log_cb,
                out_glb=dest / f"{matched_name}.glb",
                anim_dir=dest / "AnimationClip",
            )
            if rc_glb != 0:
                last_rc = rc_glb
        _organize_character_stage(stage_dir, dest)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
    return last_rc


def export_character_full(
    char_name: str,
    out_dir: Path | None = None,
    convert_glb: bool = False,
    log_cb=None,
    game_root: Path | None = None,
    export_shaders: bool = False,
) -> int:
    script = cfg.EXTRACTOR_ROOT / "lib" / "extract_character_v2.py"
    if not script.exists():
        oz_chars = load_oz_character_index()
        matched_name = None
        if oz_chars:
            matched_name = char_name if char_name in oz_chars else next(
                (k for k in oz_chars if k.lower() == char_name.lower()), None
            )
        if matched_name and cfg.can_run_cli():
            return _export_oz_entry(
                matched_name,
                oz_chars[matched_name],
                oz_chars,
                out_dir=out_dir,
                convert_glb=convert_glb,
                log_cb=log_cb,
                game_root=game_root,
                export_shaders=export_shaders,
                fallback_subdir="",
            )

        chars = load_character_index()
        blocks = chars.get(char_name)
        if blocks and cfg.can_run_cli():
            root_eff = game_root or cfg.GAME_ROOT
            resolved = ensure_required_blks(
                root_eff,
                blocks,
                include_deps=False,
                log_cb=log_cb,
                label=char_name,
                sync_index=True,
            )
            if resolved:
                dest = (out_dir or (cfg.FINAL_ROOT / "Exports")) / char_name
                char_short = _char_short_name(char_name, chars.keys())
                _emit(log_cb, f"Exporting {char_name} from {len(resolved)} .blk(s) via AnimeStudio.CLI (filter={char_short}) -> {dest}")
                types_list = ["Animator", "Avatar", "Mesh", "Texture2D", "Material"]
                if export_shaders:
                    types_list.append("Shader")
                last_rc = export_blks_batch(
                    resolved, dest,
                    types=types_list,
                    export_mode="Convert",
                    name_filter=char_short,
                    log_cb=log_cb,
                    diffuse_only=False,
                )
                rc_anim = export_blks_batch(
                    resolved, dest,
                    types=["AnimationClip"],
                    export_mode="Convert",
                    name_filter=char_short,
                    log_cb=log_cb,
                )
                if rc_anim != 0:
                    _emit(log_cb, f"WARNING: AnimationClip export returned rc={rc_anim} (some clips could not be decoded).")
                if convert_glb and last_rc == 0:
                    rc_glb = _convert_dir_to_glb(dest, char_name, log_cb=log_cb, anim_dir=dest / "AnimationClip")
                    if rc_glb != 0:
                        last_rc = rc_glb
                return last_rc
        raise FileNotFoundError(
            f"Missing {script} (and AnimeStudio.CLI.exe fallback unavailable). "
            "Point OpenZenless to your FinalOutput/Extractor folder."
        )
    rc = run_streaming_cmd([sys.executable, str(script), char_name], log_cb=log_cb)
    if rc != 0:
        return rc
    if convert_glb:
        blender = cfg.find_blender()
        if not blender:
            _emit(log_cb, "WARNING: blender not found in PATH; skipping GLB conversion.")
        else:
            bl_script = cfg.EXTRACTOR_ROOT / "lib" / "blender_export_character.py"
            rc = run_streaming_cmd([blender, "-b", "--python", str(bl_script), "--", char_name], log_cb=log_cb)
            if rc != 0:
                return rc
            morph_script = cfg.EXTRACTOR_ROOT / "lib" / "inject_character_facial_morphs.py"
            if morph_script.exists():
                rc = run_streaming_cmd([sys.executable, str(morph_script), "--char", char_name, "--force"], log_cb=log_cb)
                if rc != 0:
                    return rc
    src_char_dir = cfg.FINAL_ROOT / "Characters" / char_name
    if out_dir is not None and src_char_dir.exists():
        dest = out_dir / char_name
        if dest.resolve() != src_char_dir.resolve():
            _emit(log_cb, f"Copying {src_char_dir} -> {dest}")
            shutil.copytree(src_char_dir, dest, dirs_exist_ok=True)
    return 0


_ENTITY_SUBDIR_MAP = {
    "monsters": "Monsters",
    "npcs": "NPCs",
    "weapons": "Weapons",
    "props": "Props",
}


def export_entity_full(
    entity_id: str,
    category: str = "",
    out_dir: Path | None = None,
    convert_glb: bool = False,
    log_cb=None,
    game_root: Path | None = None,
    export_shaders: bool = False,
) -> int:
    """Export any OZ indexed 3D entity (Monster, NPC, Weapon, Prop, or Character/Bangboo)."""
    cat_low = (category or "").strip().lower()
    if cat_low in ("characters", "character", "bangboo"):
        return export_character_full(
            entity_id, out_dir=out_dir, convert_glb=convert_glb,
            log_cb=log_cb, game_root=game_root, export_shaders=export_shaders,
        )
    ents = load_oz_entities_index()
    search_cats = [cat_low] if cat_low in ents else ["monsters", "npcs", "weapons", "props"]
    for ck in search_cats:
        cmap = ents.get(ck) or {}
        m = entity_id if entity_id in cmap else next((k for k in cmap if k.lower() == entity_id.lower()), None)
        if m is not None:
            return _export_oz_entry(
                m,
                cmap[m],
                cmap,
                out_dir=out_dir,
                convert_glb=convert_glb,
                log_cb=log_cb,
                game_root=game_root,
                export_shaders=export_shaders,
                fallback_subdir=_ENTITY_SUBDIR_MAP.get(ck, ""),
                entity_category=ck,
            )
    return export_character_full(
        entity_id, out_dir=out_dir, convert_glb=convert_glb,
        log_cb=log_cb, game_root=game_root, export_shaders=export_shaders,
    )


def export_character_family(
    member_ids: list[str],
    out_dir: Path,
    convert_glb: bool = True,
    log_cb=None,
    game_root: Path | None = None,
    export_shaders: bool = False,
) -> int:
    """Export every member (base + skins + alt forms + companions) in a character family."""
    clean_ids = [m for m in dict.fromkeys(member_ids) if m]
    if not clean_ids:
        return 1
    first_rc = 0
    for idx, cid in enumerate(clean_ids, 1):
        _emit(log_cb, f"[{idx}/{len(clean_ids)}] Exporting family member {cid}...")
        try:
            rc = export_character_full(
                cid, out_dir=out_dir, convert_glb=convert_glb,
                log_cb=log_cb, game_root=game_root, export_shaders=export_shaders,
            )
        except Exception as exc:
            _emit(log_cb, f"[ERROR] Family member {cid} failed: {exc}")
            rc = 1
        if rc != 0 and first_rc == 0:
            first_rc = rc
    return first_rc


def _export_oz_anims_entry(
    matched_name: str,
    entry: dict,
    game_root: Path,
    dest_dir: Path,
    log_cb=None,
    entity_category: str = "",
) -> int | None:
    anim_keys = entry.get("anim_keys") or []
    anim_blks = ensure_required_blks(
        game_root,
        entry.get("anim_blks") or [],
        path_hashes=anim_keys,
        bundle_hashes=entry.get("bundle_hashes") or [],
        include_deps=False,
        log_cb=log_cb,
        label=f"{matched_name} animations",
        sync_index=True,
    )
    if entity_category:
        ref = (load_oz_entities_index().get(entity_category) or {}).get(matched_name)
    else:
        ref = load_oz_character_index().get(matched_name)
    if isinstance(ref, dict):
        entry = ref
        anim_keys = entry.get("anim_keys") or anim_keys
        resolved_ref = [
            p for b in (entry.get("anim_blks") or [])
            if (p := cfg.resolve_blk_path(game_root, b)) is not None
        ]
        if resolved_ref:
            anim_blks = resolved_ref
    if not anim_blks:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    anim_hash_file: Path | None = None
    try:
        if anim_keys:
            with tempfile.NamedTemporaryFile("w", suffix=".hashes", delete=False, encoding="utf-8") as ahf:
                ahf.write("\n".join(str(k) for k in anim_keys) + "\n")
                anim_hash_file = Path(ahf.name)
        _emit(
            log_cb,
            f"Exporting animations for {matched_name} from {len(anim_blks)} .blk(s) "
            f"({len(anim_keys) or '?'} keys) -> {dest_dir}",
        )
        return export_blks_batch(
            anim_blks,
            dest_dir,
            types=["AnimationClip"],
            export_mode="Convert",
            name_filter="" if anim_keys else re.escape(matched_name),
            log_cb=log_cb,
            use_cab_map=_zzz_cab_map_exists(),
            container_hash_file=anim_hash_file,
            group_assets="ByType",
        )
    finally:
        if anim_hash_file is not None:
            anim_hash_file.unlink(missing_ok=True)


def export_entity_animations_only(
    entity_id: str,
    game_root: Path,
    out_dir: Path,
    category: str = "",
    log_cb=None,
) -> int:
    cat_low = (category or "").strip().lower()
    if cat_low in ("characters", "character", "bangboo"):
        return export_character_animations_only(entity_id, game_root, out_dir, log_cb=log_cb)
    ents = load_oz_entities_index()
    search_cats = [cat_low] if cat_low in ents else ["monsters", "npcs", "weapons", "props"]
    for ck in search_cats:
        cmap = ents.get(ck) or {}
        m = entity_id if entity_id in cmap else next((k for k in cmap if k.lower() == entity_id.lower()), None)
        if m is not None:
            sub = _ENTITY_SUBDIR_MAP.get(ck, "")
            dest_dir = (out_dir / sub / m) if (sub and out_dir.name.lower() != sub.lower()) else (out_dir / m)
            rc = _export_oz_anims_entry(m, cmap[m], game_root, dest_dir, log_cb=log_cb, entity_category=ck)
            if rc is not None:
                return rc
            raise ValueError(f"No animation blocks indexed for {m}")
    return export_character_animations_only(entity_id, game_root, out_dir, log_cb=log_cb)


def export_character_animations_only(
    char_name: str,
    game_root: Path,
    out_dir: Path,
    log_cb=None,
) -> int:
    oz_chars = load_oz_character_index()
    matched_name = None
    if oz_chars:
        matched_name = char_name if char_name in oz_chars else next(
            (k for k in oz_chars if k.lower() == char_name.lower()), None
        )
    if matched_name:
        rc = _export_oz_anims_entry(matched_name, oz_chars[matched_name], game_root, out_dir / matched_name, log_cb=log_cb)
        if rc is not None:
            return rc

    chars = load_character_index()
    blocks = chars.get(char_name)
    if not blocks:
        raise KeyError(f"Character '{char_name}' not found in character index.")
    resolved = ensure_required_blks(
        game_root,
        blocks,
        include_deps=False,
        log_cb=log_cb,
        label=f"{char_name} animations",
        sync_index=True,
    )
    if not resolved:
        raise FileNotFoundError(f"No .blk files found for '{char_name}' under {cfg.resolve_files_root(game_root)}")

    dest_dir = out_dir / char_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    char_short = _char_short_name(char_name, chars.keys())
    _emit(log_cb, f"Exporting animations only for {char_name} (filter={char_short}) from {len(resolved)} .blk file(s) -> {dest_dir}")
    return export_blks_batch(
        resolved,
        dest_dir,
        types=["AnimationClip", "Animator", "Avatar"],
        export_mode="Convert",
        name_filter=char_short,
        log_cb=log_cb,
    )


def export_scene_map(
    stage_name: str | None,
    resume: bool = True,
    workers: int = 1,
    out_dir: Path | None = None,
    log_cb=None,
    game_root: Path | None = None,
    export_shaders: bool = False,
) -> int:
    script = cfg.EXTRACTOR_ROOT / "lib" / "export_maps_pipeline.py"
    if not script.exists():
        oz_maps = load_oz_map_index()
        if oz_maps and cfg.can_run_cli():
            oz_targets: list[tuple[str, dict]] = []
            if not stage_name or stage_name.lower() == "all":
                oz_targets = list(oz_maps.items())
            elif stage_name in oz_maps:
                oz_targets = [(stage_name, oz_maps[stage_name])]
            else:
                ci_match = next((k for k in oz_maps if k.lower() == stage_name.lower()), None)
                if ci_match is not None:
                    oz_targets = [(ci_match, oz_maps[ci_match])]
            if oz_targets:
                root = game_root or cfg.GAME_ROOT
                base_out = out_dir or (cfg.FINAL_ROOT / "Exports")
                map_types = ["GameObject"] + (["Shader"] if export_shaders else [])
                log_lock = threading.Lock()

                def _safe_log(msg: str):
                    if log_cb is not None:
                        with log_lock:
                            log_cb(msg)

                def _export_one_oz_map(sname: str, entry: dict) -> int:
                    req_blks = collect_map_required_blks(sname, entry)
                    st_pre = entry.get("streaming") or {}
                    p_hashes = (st_pre.get("hashes") or ()) if isinstance(st_pre, dict) else ()
                    b_hashes = entry.get("bundle_hashes") or ()
                    if req_blks or p_hashes or b_hashes:
                        ensure_required_blks(
                            root,
                            req_blks,
                            path_hashes=p_hashes,
                            bundle_hashes=b_hashes,
                            include_deps=True,
                            log_cb=_safe_log,
                            label=f"map {sname}",
                            sync_index=True,
                        )
                        ref_m = load_oz_map_index().get(sname)
                        if isinstance(ref_m, dict):
                            entry = ref_m
                    if not _zzz_cab_map_exists():
                        _emit(_safe_log, f"WARNING: CABMap ZZZ.bin not available for {sname}; skipping.")
                        return 1
                    dest = base_out / sname
                    dest.mkdir(parents=True, exist_ok=True)
                    rc = 0
                    st = entry.get("streaming")
                    bs = entry.get("base_scene")
                    extra_scenes = [
                        es for es in (entry.get("extra_scenes") or ())
                        if isinstance(es, dict) and es.get("blk")
                    ]
                    has_st = isinstance(st, dict) and bool(st.get("blks")) and bool(st.get("hashes"))
                    has_bs = isinstance(bs, dict) and bool(bs.get("blk"))

                    def _do_streaming() -> int:
                        stream_blks = [
                            p for b in st["blks"]
                            if (p := cfg.resolve_blk_path(root, b)) is not None
                        ]
                        if not stream_blks:
                            return 0
                        _emit(
                            _safe_log,
                            f"Exporting {sname} Streaming ({len(st['hashes'])} sectors across {len(stream_blks)} .blk(s)) -> {dest / 'Streaming'}",
                        )
                        hash_file: Path | None = None
                        try:
                            with tempfile.NamedTemporaryFile("w", suffix=".hashes", delete=False, encoding="utf-8") as hf:
                                hf.write("\n".join(str(h) for h in st["hashes"]) + "\n")
                                hash_file = Path(hf.name)
                            return export_blks_batch(
                                stream_blks,
                                dest / "Streaming",
                                types=map_types,
                                export_mode="Convert",
                                log_cb=_safe_log,
                                use_cab_map=True,
                                diffuse_only=(not export_shaders),
                                skip_proxies=True,
                                container_hash_file=hash_file,
                                group_assets="BySource",
                                skip_animations=True,
                            )
                        finally:
                            if hash_file is not None:
                                hash_file.unlink(missing_ok=True)

                    def _do_basescene() -> int:
                        bs_path = cfg.resolve_blk_path(root, bs["blk"])
                        if bs_path is None:
                            return 0
                        _emit(
                            _safe_log,
                            f"Exporting {sname} BaseScene ({bs_path.name}, cab={bs.get('cab', '')}) -> {dest / 'BaseScene'}",
                        )
                        return export_blk_asset_types(
                            bs_path,
                            dest / "BaseScene",
                            types=map_types,
                            export_mode="Convert",
                            log_cb=_safe_log,
                            map_op="CABMap",
                            map_name="ZZZ",
                            diffuse_only=(not export_shaders),
                            skip_proxies=True,
                            cab_filter=bs.get("cab", ""),
                            group_assets="BySource",
                            skip_animations=True,
                        )

                    def _do_extrascene(idx: int, es: dict) -> int:
                        es_path = cfg.resolve_blk_path(root, es["blk"])
                        if es_path is None:
                            return 0
                        sub_dir = dest / f"ExtraScene_{idx}"
                        _emit(
                            _safe_log,
                            f"Exporting {sname} ExtraScene_{idx} ({es_path.name}, cab={es.get('cab', '')}) -> {sub_dir}",
                        )
                        return export_blk_asset_types(
                            es_path,
                            sub_dir,
                            types=map_types,
                            export_mode="Convert",
                            log_cb=_safe_log,
                            map_op="CABMap",
                            map_name="ZZZ",
                            diffuse_only=(not export_shaders),
                            skip_proxies=True,
                            cab_filter=es.get("cab", ""),
                            group_assets="BySource",
                            skip_animations=True,
                        )

                    sub_count = int(has_st) + int(has_bs) + len(extra_scenes)
                    if workers > 1 and len(oz_targets) == 1 and sub_count >= 2:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, sub_count)) as subpool:
                            subfuts = []
                            if has_st:
                                subfuts.append(subpool.submit(_do_streaming))
                            if has_bs:
                                subfuts.append(subpool.submit(_do_basescene))
                            for idx, es in enumerate(extra_scenes):
                                subfuts.append(subpool.submit(_do_extrascene, idx, es))
                            for sfut in subfuts:
                                rc_sub = sfut.result()
                                if rc_sub != 0 and rc == 0:
                                    rc = rc_sub
                    else:
                        if has_st:
                            rc_st = _do_streaming()
                            if rc_st != 0:
                                rc = rc_st
                        if has_bs:
                            rc_bs = _do_basescene()
                            if rc_bs != 0:
                                rc = rc_bs
                        for idx, es in enumerate(extra_scenes):
                            rc_es = _do_extrascene(idx, es)
                            if rc_es != 0 and rc == 0:
                                rc = rc_es

                    if rc == 0 or list(dest.rglob("*.fbx")):
                        rc_glb = _convert_dir_to_glb(dest, sname, log_cb=_safe_log)
                        if rc_glb != 0:
                            rc = rc_glb
                    return rc

                last_rc = 0
                if workers > 1 and len(oz_targets) > 1:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(oz_targets))) as pool:
                        futs = [pool.submit(_export_one_oz_map, sname, entry) for sname, entry in oz_targets]
                        for fut in concurrent.futures.as_completed(futs):
                            rc_one = fut.result()
                            if rc_one != 0:
                                last_rc = rc_one
                else:
                    for sname, entry in oz_targets:
                        rc_one = _export_one_oz_map(sname, entry)
                        if rc_one != 0:
                            last_rc = rc_one
                return last_rc

        stages = load_curated_map_index()
        if cfg.can_run_cli() and stages:
            files_root = cfg.resolve_files_root(game_root or cfg.GAME_ROOT)
            blocks_dir = files_root / "Blocks"
            targets = (
                [(stage_name, stages[stage_name])]
                if stage_name and stage_name.lower() != "all" and stage_name in stages
                else list(stages.items()) if (not stage_name or stage_name.lower() == "all") else []
            )
            if targets:
                base_out = out_dir or (cfg.FINAL_ROOT / "Exports")
                last_rc = 0
                for sname, blocks in targets:
                    resolved = ensure_required_blks(
                        game_root or cfg.GAME_ROOT,
                        blocks,
                        include_deps=True,
                        log_cb=log_cb,
                        label=sname,
                        sync_index=True,
                    )
                    if not resolved:
                        continue
                    dest = base_out / sname
                    cab_active = _zzz_cab_map_exists()
                    _emit(
                        log_cb,
                        f"Exporting map {sname} ({len(resolved)} .blk(s)) via AnimeStudio.CLI "
                        f"({'CABMap' if cab_active else 'legacy'} mode) -> {dest}",
                    )
                    if cab_active:
                        map_types = ["GameObject"]
                        if export_shaders:
                            map_types.append("Shader")
                        clean_stage = re.sub(r"^(?:Stage|Env|Scene|Map)_", "", sname)
                        st_toks = [t.lower() for t in clean_stage.split("_") if len(t) >= 3 and not t.isdigit()]
                        stage_tokens_str = ",".join(st_toks)
                        rc = export_blks_batch(
                            resolved, dest,
                            types=map_types,
                            export_mode="Convert",
                            name_filter="",
                            log_cb=log_cb,
                            use_cab_map=True,
                            diffuse_only=(not export_shaders),
                            skip_proxies=True,
                            stage_tokens=stage_tokens_str,
                        )
                    else:
                        map_types = ["Mesh", "Texture2D", "GameObject", "Animator", "Material"]
                        if export_shaders:
                            map_types.append("Shader")
                        rc = export_blks_batch(
                            resolved, dest,
                            types=map_types,
                            export_mode="Convert",
                            name_filter="",
                            log_cb=log_cb,
                            use_cab_map=False,
                        )
                        _mat_map, mat_blks, mat_texs = resolve_material_textures(dest)
                        tex_blk_names, tex_pat = load_stage_texture_blocks_for_dir(sname, dest)
                        all_tex_blks = list(dict.fromkeys(mat_blks + tex_blk_names))[:96]
                        if all_tex_blks:
                            seen_names = {p.name for p in resolved}
                            extra_tex = [
                                blocks_dir / os.path.basename(b)
                                for b in all_tex_blks
                                if os.path.basename(b) not in seen_names and (blocks_dir / os.path.basename(b)).is_file()
                            ]
                            if extra_tex:
                                extra_pat = tex_pat
                                if mat_texs:
                                    mat_re = "|".join(re.escape(t) for t in mat_texs[:300])
                                    extra_pat = f"(?:{tex_pat})|(?:^(?:{mat_re})$)" if tex_pat else f"^(?:{mat_re})$"
                                _emit(log_cb, f"Exporting shared map Texture2Ds ({len(extra_tex)} .blk(s), {len(_mat_map)} Material PathIDs resolved) -> {dest}")
                                export_blks_batch(
                                    extra_tex, dest,
                                    types=["Texture2D"],
                                    export_mode="Convert",
                                    name_filter=extra_pat,
                                    log_cb=log_cb,
                                    use_cab_map=False,
                                )
                    if rc == 0 or (dest.exists() and (any(dest.rglob("*.fbx")) or any(dest.rglob("*.obj")))):
                        rc_glb = _convert_dir_to_glb(dest, sname, log_cb=log_cb)
                        if rc_glb != 0:
                            rc = rc_glb
                    if rc != 0:
                        last_rc = rc
                return last_rc
        raise FileNotFoundError(
            f"Missing {script} (and AnimeStudio.CLI.exe stage fallback unavailable)."
        )
    cmd = [sys.executable, str(script), "--workers", str(max(1, workers))]
    if resume:
        cmd.append("--resume")
    if stage_name and stage_name.lower() != "all":
        cmd.append(stage_name)
    rc = run_streaming_cmd(cmd, log_cb=log_cb)
    if rc != 0:
        return rc
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        maps_dir = cfg.FINAL_ROOT / "Maps_Combined"
        if stage_name and stage_name.lower() != "all":
            cand = maps_dir / f"{stage_name}_full.glb"
            if cand.is_file():
                _emit(log_cb, f"Copying {cand.name} -> {out_dir}")
                shutil.copy2(cand, out_dir / cand.name)
        elif maps_dir.exists():
            for glb in sorted(maps_dir.glob("*_full.glb")):
                shutil.copy2(glb, out_dir / glb.name)
            _emit(log_cb, f"Copied all *_full.glb maps to {out_dir}")
    return 0


def _resolve_unique_blks(game_root: Path, blk_refs: list[str]) -> list[Path]:
    resolved: list[Path] = []
    seen: set[Path] = set()
    for b in blk_refs:
        rp = cfg.resolve_blk_path(game_root, str(b))
        if rp is None:
            continue
        try:
            rrp = rp.resolve()
        except Exception:
            rrp = rp
        if rrp not in seen:
            seen.add(rrp)
            resolved.append(rp)
    return resolved


def export_fonts(
    game_root: Path,
    out_dir: Path,
    source_path: Path | None = None,
    log_cb=None,
) -> int:
    """Export Font (TTF/OTF) assets from cached font blocks (or fallback to Blocks/)."""
    if source_path is not None and source_path.is_file():
        return export_blk_asset_types(source_path, out_dir, ["Font"], "Convert", log_cb=log_cb)
    font_refs = load_font_blocks()
    resolved = (
        ensure_required_blks(game_root, font_refs, include_deps=False, log_cb=log_cb, label="fonts", sync_index=False)
        if font_refs
        else []
    )
    if resolved:
        _emit(log_cb, f"Exporting Fonts from {len(resolved)} indexed .blk(s) -> {out_dir}")
        return export_blks_batch(resolved, out_dir, ["Font"], "Convert", log_cb=log_cb)
    src = source_path if (source_path is not None and source_path.exists()) else (cfg.resolve_files_root(game_root) / "Blocks")
    return export_blk_asset_types(src, out_dir, ["Font"], "Convert", log_cb=log_cb)


def export_texture_section(
    sec_name: str,
    blks: list[str],
    game_root: Path,
    out_dir: Path,
    log_cb=None,
) -> int:
    """Export Texture2D & Sprite assets for a texture_sections_index section (split-APK safe)."""
    resolved = ensure_required_blks(game_root, blks, include_deps=False, log_cb=log_cb, label=sec_name, sync_index=False)
    if not resolved:
        raise FileNotFoundError(f"No .blk files found for section '{sec_name}' under {game_root}")
    dest = out_dir / sec_name.replace("/", "_")
    _emit(log_cb, f"Exporting section {sec_name} ({len(resolved)} .blk(s)) -> {dest}")
    return export_blks_batch(resolved, dest, ["Texture2D", "Sprite"], "Convert", log_cb=log_cb)


def export_spine_rig(
    rig_name: str,
    game_root: Path,
    out_dir: Path,
    log_cb=None,
) -> int:
    """Export paired Spine 2D .skel / .atlas TextAssets and textures."""
    spine_map = load_oz_media_index().get("spine") or {}
    key = rig_name if rig_name in spine_map else next((k for k in spine_map if k.lower() == rig_name.lower()), None)
    if key is None:
        raise KeyError(f"Spine rig '{rig_name}' not found in oz_media_index.json")
    info = spine_map[key]
    resolved = ensure_required_blks(
        game_root, list(info.get("blks") or []), include_deps=False, log_cb=log_cb, label=f"Spine {key}", sync_index=False
    )
    if not resolved:
        raise FileNotFoundError(f"No .blk files resolved for Spine rig '{key}' under {game_root}")
    stem = info.get("name") or key
    dest = out_dir / "Spine" / stem
    _emit(log_cb, f"Exporting Spine 2D rig {stem} from {len(resolved)} .blk(s) -> {dest}")
    return export_blks_batch(
        resolved,
        dest,
        types=["TextAsset", "Texture2D"],
        export_mode="Convert",
        name_filter=rf"(?i)^{re.escape(stem)}(?:\.|_|$)",
        log_cb=log_cb,
    )


def export_ui_sprite_group(
    group_key: str,
    game_root: Path,
    out_dir: Path,
    log_cb=None,
) -> int:
    """Export a curated UI Sprite bucket (portraits, mindscapes, W-Engines, icons, etc.)."""
    ui_map = load_oz_media_index().get("ui_sprites") or {}
    key = group_key if group_key in ui_map else next((k for k in ui_map if k.lower() == group_key.lower()), None)
    if key is None:
        raise KeyError(f"UI Sprite pack '{group_key}' not found in oz_media_index.json")
    info = ui_map[key]
    resolved = ensure_required_blks(
        game_root, list(info.get("blks") or []), include_deps=False, log_cb=log_cb, label=f"Sprites {key}", sync_index=False
    )
    if not resolved:
        raise FileNotFoundError(f"No .blk files resolved for UI Sprite pack '{key}' under {game_root}")
    dest = out_dir / "UI_Sprites" / key
    _emit(log_cb, f"Exporting UI Sprite pack '{info.get('label', key)}' ({len(resolved)} .blk(s)) -> {dest}")
    return export_blks_batch(
        resolved,
        dest,
        types=["Sprite", "Texture2D"],
        export_mode="Convert",
        name_filter=str(info.get("name_pattern") or ""),
        log_cb=log_cb,
    )


def export_data_pack(
    pack_key: str,
    game_root: Path,
    out_dir: Path,
    log_cb=None,
) -> int:
    """Export an indexed TextAsset/Shader data pack (subtitles, .cloth, .human, dialogue graphs, shaders)."""
    data_map = load_oz_media_index().get("data_packs") or {}
    key = pack_key if pack_key in data_map else next((k for k in data_map if k.lower() == pack_key.lower()), None)
    if key is None:
        raise KeyError(f"Data pack '{pack_key}' not found in oz_media_index.json")
    info = data_map[key]
    blks = list(info.get("blks") or [])
    if not blks and key == "shaders":
        blks = load_shader_blocks()
    resolved = ensure_required_blks(
        game_root, blks, include_deps=False, log_cb=log_cb, label=f"Data {key}", sync_index=False
    )
    if not resolved:
        raise FileNotFoundError(f"No .blk files resolved for data pack '{key}' under {game_root}")
    types = list(info.get("types") or ["TextAsset"])
    dest = out_dir / "Data" / key
    _emit(log_cb, f"Exporting Data Pack '{info.get('label', key)}' ({len(resolved)} .blk(s)) -> {dest}")
    return export_blks_batch(
        resolved,
        dest,
        types=types,
        export_mode="Convert",
        name_filter=str(info.get("name_pattern") or ""),
        log_cb=log_cb,
    )


def _infer_audio_lang_hint(pck: Path) -> str:
    norm = str(pck).replace("\\", "/")
    if "/En/" in norm:
        return "English"
    if pck.parent.name in ("En", "Jp", "Cn", "Kr"):
        return pck.parent.name
    return "SFX"


def _resolve_wem_dest(
    out_dir: Path,
    lang_hint: str,
    pck_stem: str,
    entry_name: str,
    mapper,
    multi_pck: bool,
    bank_stem: str = "",
) -> tuple[Path, bool]:
    stem = Path(entry_name).stem.lower()
    is_wem = entry_name.lower().endswith(".wem")
    res = mapper.get_key(stem, addLang=True) if (mapper and is_wem) else None
    if res:
        rel_path, raw_lang = res[0], (res[1] or lang_hint)
        lang = raw_lang.split("(")[0].strip() if "(" in raw_lang else raw_lang
        return out_dir / lang / (rel_path.replace("\\", "/").lstrip("/") + ".wem"), True
    if bank_stem:
        return out_dir / lang_hint / "SoundBanks" / pck_stem / bank_stem / entry_name, False
    if mapper is not None:
        return out_dir / lang_hint / "_Unresolved" / pck_stem / entry_name, False
    sub_out = out_dir / pck_stem if multi_pck else out_dir
    return sub_out / entry_name, False


def _decode_wav_jobs(wav_jobs: list[tuple[Path, Path]]) -> None:
    if not wav_jobs:
        return
    def _decode_one(job: tuple[Path, Path]):
        w_src, w_dst = job
        proc = subprocess.run(
            [str(cfg.VGMSTREAM_CLI), "-o", str(w_dst), str(w_src)],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            **cfg.subprocess_no_window_kwargs(),
        )
        if proc.returncode == 0 and w_dst.is_file():
            w_src.unlink(missing_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.adaptive_worker_count("audio", max_workers=12)) as pool:
        list(pool.map(_decode_one, wav_jobs))


def _ensure_audio_tools(need_map: bool, decode_wav: bool, log_cb=None) -> bool:
    """Auto-install AnimeWwise nap.map / vgmstream-cli when needed and return effective decode_wav flag."""
    if need_map and not cfg.NAP_MAP_FILE.is_file():
        try:
            from . import tools

            tools.ensure_extractor_tools(log_cb=log_cb, install_wine=False, install_vgmstream=False)
        except Exception as exc:
            _emit(log_cb, f"[WARN] AnimeWwise auto-fetch note: {exc}")
    if decode_wav and not cfg.VGMSTREAM_CLI.is_file():
        try:
            from . import tools

            tools.ensure_vgmstream(log_cb=log_cb)
        except Exception as exc:
            _emit(log_cb, f"[WARN] vgmstream auto-install note: {exc}")
    if decode_wav and not cfg.VGMSTREAM_CLI.is_file():
        _emit(log_cb, f"WARNING: vgmstream-cli not found at {cfg.VGMSTREAM_CLI}; keeping .wem files.")
        return False
    return decode_wav


def export_audio_pck(
    source_path: Path,
    out_dir: Path,
    limit: int = 0,
    use_wwise_map: bool = True,
    decode_wav: bool = False,
    extract_bnk: bool = True,
    log_cb=None,
) -> int:
    if not source_path.exists():
        raise FileNotFoundError(f"Audio source not found: {source_path}")
    is_single_bnk = source_path.is_file() and source_path.suffix.lower() == ".bnk"
    pcks = [] if is_single_bnk else ([source_path] if source_path.is_file() else sorted(source_path.rglob("*.pck")))
    if not is_single_bnk and not pcks:
        raise FileNotFoundError(f"No .pck files found in {source_path}")
    decode_wav = _ensure_audio_tools(use_wwise_map, decode_wav, log_cb=log_cb)
    mapper = None
    if use_wwise_map:
        _emit(log_cb, f"Loading AnimeWwise map: {cfg.NAP_MAP_FILE.name}...")
        mapper = load_wwise_mapper()
        if mapper is None:
            _emit(log_cb, "WARNING: nap.map not found; falling back to raw AKPK filenames.")

    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    mapped_count = 0
    bnk_wem_count = 0

    if is_single_bnk:
        raw = source_path.read_bytes()
        lang_hint = _infer_audio_lang_hint(source_path)
        wems = extract_bnk_wems(raw)
        wav_jobs: list[tuple[Path, Path]] = []
        for wid, wbytes in wems:
            dest, is_map = _resolve_wem_dest(
                out_dir, lang_hint, source_path.stem, f"{wid}.wem", mapper, False, bank_stem=source_path.stem
            )
            if is_map:
                mapped_count += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(wbytes)
            if decode_wav:
                wav_jobs.append((dest, dest.with_suffix(".wav")))
        _decode_wav_jobs(wav_jobs)
        _emit(log_cb, f"Extracted {len(wems)} embedded WEM(s) from {source_path.name} (mapped: {mapped_count})")
        return 0

    for pck in pcks:
        ak = parse_akpk(pck)
        if ak.get("error"):
            _emit(log_cb, f"SKIP {pck.name}: {ak['error']}")
            continue
        entries = ak["entries"][:limit] if limit > 0 else ak["entries"]
        lang_hint = _infer_audio_lang_hint(pck)
        wav_jobs: list[tuple[Path, Path]] = []
        pck_bnk_wems = 0
        with pck.open("rb") as src:
            for e in entries:
                src.seek(e["offset"])
                raw = src.read(e["size"])
                is_bnk = e["name"].lower().endswith(".bnk") or raw[:4] == b"BKHD"
                if extract_bnk and is_bnk:
                    wems = extract_bnk_wems(raw)
                    if wems:
                        bank_stem = Path(e["name"]).stem
                        for wid, wbytes in wems:
                            dest, is_map = _resolve_wem_dest(
                                out_dir, lang_hint, pck.stem, f"{wid}.wem", mapper, len(pcks) > 1, bank_stem=bank_stem
                            )
                            if is_map:
                                mapped_count += 1
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(wbytes)
                            if decode_wav:
                                wav_jobs.append((dest, dest.with_suffix(".wav")))
                        pck_bnk_wems += len(wems)
                        bnk_wem_count += len(wems)
                        continue
                dest, is_map = _resolve_wem_dest(
                    out_dir, lang_hint, pck.stem, e["name"], mapper, len(pcks) > 1
                )
                if is_map:
                    mapped_count += 1
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                if decode_wav and dest.suffix.lower() == ".wem":
                    wav_jobs.append((dest, dest.with_suffix(".wav")))
        _decode_wav_jobs(wav_jobs)
        total += len(entries)
        _emit(
            log_cb,
            f"Unpacked {len(entries)} entries (+{pck_bnk_wems} .bnk WEMs) from {pck.name} "
            f"(mapped so far: {mapped_count})",
        )
    _emit(
        log_cb,
        f"Total audio entries extracted: {total} (+{bnk_wem_count} embedded .bnk WEMs, AnimeWwise mapped: {mapped_count})",
    )
    return 0


def scan_audio_groups(audio_root: Path, use_wwise_map: bool = True) -> list[dict]:
    """Fast AKPK header scan grouping mapped WEMs and SoundBanks by category/voice pack."""
    if not audio_root.exists():
        return []
    pcks = [audio_root] if audio_root.is_file() else sorted(audio_root.rglob("*.pck"))
    if not pcks:
        return []
    mapper = load_wwise_mapper() if use_wwise_map else None
    groups: dict[str, dict] = {}
    for pck in pcks:
        ak = parse_akpk(pck)
        if ak.get("error"):
            continue
        lang_hint = _infer_audio_lang_hint(pck)
        pck_str = str(pck)
        for e in ak.get("entries", ()):
            ename = e["name"]
            is_bnk = ename.lower().endswith(".bnk") or e.get("type") == "Wwise BNK"
            stem = Path(ename).stem.lower()
            res = mapper.get_key(stem, addLang=True) if (mapper and ename.lower().endswith(".wem")) else None
            if res:
                rel_path, raw_lang = res[0], (res[1] or lang_hint)
                lang = raw_lang.split("(")[0].strip() if "(" in raw_lang else raw_lang
                rel_unix = rel_path.replace("\\", "/").lstrip("/")
                parts = [p for p in rel_unix.split("/") if p]
                g_sub = "/".join(parts[:2]) if len(parts) >= 2 else (parts[0] if parts else "Misc")
                gkey = f"{lang}/{g_sub}"
                label = f"{lang} — {g_sub}"
                rel_dest = f"{lang}/{rel_unix}.wem"
            elif is_bnk:
                gkey = f"_bnk/{pck.stem}"
                label = f"SoundBanks (.bnk) — {pck.stem}"
                rel_dest = f"{lang_hint}/SoundBanks/{pck.stem}/{ename}"
            else:
                gkey = f"_unresolved/{lang_hint}/{pck.stem}"
                label = f"Unresolved SFX — {pck.stem} ({lang_hint})"
                rel_dest = f"{lang_hint}/_Unresolved/{pck.stem}/{ename}"
            grp = groups.setdefault(gkey, {
                "group_key": gkey,
                "label": label,
                "pck_paths": set(),
                "entries": [],
                "count": 0,
                "bnk_count": 0,
                "total_size": 0,
            })
            grp["pck_paths"].add(pck_str)
            grp["entries"].append((pck_str, int(e["offset"]), int(e["size"]), rel_dest, is_bnk))
            grp["count"] += 1
            if is_bnk:
                grp["bnk_count"] += 1
            grp["total_size"] += int(e["size"])
    out = []
    for gkey, grp in sorted(groups.items(), key=lambda kv: (kv[0].startswith("_"), kv[1]["label"].lower())):
        out.append({
            "group_key": grp["group_key"],
            "label": grp["label"],
            "pck_paths": sorted(grp["pck_paths"]),
            "entries": grp["entries"],
            "count": grp["count"],
            "bnk_count": grp["bnk_count"],
            "total_size": grp["total_size"],
        })
    return out


def export_audio_entries(
    entries: list[tuple],
    out_dir: Path,
    decode_wav: bool = False,
    extract_bnk: bool = True,
    log_cb=None,
) -> int:
    """Extract a selected list of ``(pck_path, offset, size, rel_dest, is_bnk)`` audio entries."""
    if not entries:
        return 0
    decode_wav = _ensure_audio_tools(extract_bnk, decode_wav, log_cb=log_cb)
    out_dir.mkdir(parents=True, exist_ok=True)
    mapper = load_wwise_mapper() if extract_bnk else None
    by_pck: dict[str, list[tuple[int, int, str, bool]]] = {}
    for pck_s, off, sz, rel_d, is_bnk in entries:
        by_pck.setdefault(pck_s, []).append((off, sz, rel_d, bool(is_bnk)))
    wav_jobs: list[tuple[Path, Path]] = []
    written = 0
    for pck_s, jobs in by_pck.items():
        pck = Path(pck_s)
        if not pck.is_file():
            continue
        lang_hint = _infer_audio_lang_hint(pck)
        with pck.open("rb") as src:
            for off, sz, rel_d, is_bnk in jobs:
                src.seek(off)
                raw = src.read(sz)
                if extract_bnk and (is_bnk or raw[:4] == b"BKHD"):
                    wems = extract_bnk_wems(raw)
                    if wems:
                        b_stem = Path(rel_d).stem
                        for wid, wbytes in wems:
                            dest, _ = _resolve_wem_dest(
                                out_dir, lang_hint, pck.stem, f"{wid}.wem", mapper, False, bank_stem=b_stem
                            )
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(wbytes)
                            written += 1
                            if decode_wav:
                                wav_jobs.append((dest, dest.with_suffix(".wav")))
                        continue
                dest = out_dir / rel_d.lstrip("/")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                written += 1
                if decode_wav and dest.suffix.lower() == ".wem":
                    wav_jobs.append((dest, dest.with_suffix(".wav")))
    _decode_wav_jobs(wav_jobs)
    _emit(log_cb, f"Extracted {written} audio file(s) -> {out_dir}")
    return 0


def export_full_game(
    game_root: Path,
    out_dir: Path,
    include_chars: bool = True,
    include_scenes: bool = True,
    include_fonts: bool = True,
    include_audio: bool = True,
    convert_glb: bool = False,
    use_wwise_map: bool = True,
    decode_wav: bool = False,
    dry_run: bool = False,
    log_cb=None,
    export_shaders: bool = False,
) -> int:
    files_root = cfg.resolve_files_root(game_root)
    chars = sorted(load_character_index().keys())
    maps = sorted(load_curated_map_index().keys())

    char_script = cfg.EXTRACTOR_ROOT / "lib" / "extract_character_v2.py"
    map_script = cfg.EXTRACTOR_ROOT / "lib" / "export_maps_pipeline.py"
    blocks_dir = files_root / "Blocks"
    audio_dir = files_root / "Audio"
    if not audio_dir.exists() and files_root.exists() and any(files_root.rglob("*.pck")):
        audio_dir = files_root
    has_pcks = audio_dir.exists() and any(audio_dir.rglob("*.pck"))
    has_cli = cfg.can_run_cli()

    chars_ready = include_chars and bool(chars) and (char_script.is_file() or (has_cli and blocks_dir.exists()))
    scenes_ready = include_scenes and (map_script.is_file() or (has_cli and blocks_dir.exists() and bool(maps)))
    fonts_ready = include_fonts and blocks_dir.exists() and has_cli
    audio_ready = include_audio and has_pcks

    def _status_str(enabled: bool, ready: bool, detail: str, missing_reason: str) -> str:
        if not enabled:
            return "SKIPPED"
        return f"{detail} (READY)" if ready else f"{detail} (UNAVAILABLE: {missing_reason})"

    char_reason = (
        f"missing {char_script.name}" if chars else "character_index_lean.json not found in Extractor/data/asset_map"
    )
    font_reason = "missing Blocks/" if not blocks_dir.exists() else "missing AnimeStudio.CLI.exe or runtime"

    _emit(log_cb, "=== OpenZenless Full Export Plan ===")
    _emit(log_cb, f"Game Root   : {files_root} ({'OK' if files_root.exists() else 'MISSING'})")
    _emit(log_cb, f"Output Root : {out_dir}")
    _emit(log_cb, f"Characters  : {_status_str(include_chars, chars_ready, f'{len(chars)} (GLB={convert_glb})', char_reason)}")
    _emit(log_cb, f"Curated Maps: {_status_str(include_scenes, scenes_ready, f'{len(maps)}', f'missing {map_script.name}')}")
    _emit(log_cb, f"Fonts       : {_status_str(include_fonts, fonts_ready, 'Blocks/*.blk', font_reason)}")
    _emit(log_cb, f"Audio (.pck): {_status_str(include_audio, audio_ready, f'AnimeWwise={use_wwise_map}, WAV={decode_wav}', 'no .pck files found')}")
    if dry_run:
        _emit(log_cb, "[DRY RUN] No files were modified.")
        return 0

    if not files_root.exists():
        _emit(log_cb, f"[ERROR] Game root does not exist: {files_root}")
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    ran_any = False
    had_errors = False

    if include_chars:
        if not chars_ready:
            _emit(log_cb, f"[SKIP] Characters: {char_reason}")
        else:
            ran_any = True
            for idx, ch in enumerate(chars, 1):
                _emit(log_cb, f"[{idx}/{len(chars)}] Exporting character {ch}...")
                try:
                    rc = export_character_full(
                        ch, out_dir=out_dir / "Characters", convert_glb=convert_glb, log_cb=log_cb,
                        game_root=game_root, export_shaders=export_shaders,
                    )
                    if rc != 0:
                        had_errors = True
                        _emit(log_cb, f"[WARN] Character {ch} exited with code {rc}")
                except Exception as exc:
                    had_errors = True
                    _emit(log_cb, f"[ERROR] Character {ch} failed: {exc}")

    if include_scenes:
        if not scenes_ready:
            _emit(log_cb, f"[SKIP] Curated Maps: missing {map_script}")
        else:
            ran_any = True
            _emit(log_cb, "Exporting curated maps...")
            try:
                rc = export_scene_map(
                    "all", resume=True, workers=1, out_dir=out_dir / "Maps_Combined",
                    log_cb=log_cb, game_root=game_root, export_shaders=export_shaders,
                )
                if rc != 0:
                    had_errors = True
                    _emit(log_cb, f"[WARN] Curated Maps exited with code {rc}")
            except Exception as exc:
                had_errors = True
                _emit(log_cb, f"[ERROR] Curated Maps failed: {exc}")

    if include_fonts:
        if not fonts_ready:
            _emit(log_cb, f"[SKIP] Fonts: {font_reason}")
        else:
            ran_any = True
            _emit(log_cb, "Exporting all Fonts...")
            try:
                rc = export_fonts(game_root, out_dir / "Fonts", log_cb=log_cb)
                if rc != 0:
                    had_errors = True
                    _emit(log_cb, f"[WARN] Fonts export exited with code {rc}")
            except Exception as exc:
                had_errors = True
                _emit(log_cb, f"[ERROR] Fonts export failed: {exc}")

    if include_audio:
        if not audio_ready:
            _emit(log_cb, f"[SKIP] Audio: no .pck files found under {files_root}")
        else:
            ran_any = True
            _emit(log_cb, "Unpacking & organizing Wwise .pck archives...")
            try:
                rc = export_audio_pck(
                    audio_dir, out_dir / "Audio",
                    use_wwise_map=use_wwise_map, decode_wav=decode_wav, log_cb=log_cb,
                )
                if rc != 0:
                    had_errors = True
            except Exception as exc:
                had_errors = True
                _emit(log_cb, f"[ERROR] Audio export failed: {exc}")

    for lib_name in ("build_character_catalog.py", "build_viewer_data.py"):
        lib_script = cfg.EXTRACTOR_ROOT / "lib" / lib_name
        if lib_script.exists():
            run_streaming_cmd([sys.executable, str(lib_script)], log_cb=log_cb)

    if not ran_any:
        _emit(log_cb, "=== Full Export Finished (no stages were runnable with current Game/Extractor roots) ===")
        return 1
    _emit(log_cb, f"=== Full Export Complete{' (with warnings/errors)' if had_errors else ''} ===")
    return 1 if had_errors else 0
