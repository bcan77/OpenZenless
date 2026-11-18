"""Batch and pipeline exporters for characters, animations, maps, fonts, assets, and audio."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from . import config as cfg
from .containers import parse_akpk
from .indexer import (
    _emit,
    load_character_index,
    load_curated_map_index,
    load_stage_texture_blocks,
    load_stage_texture_blocks_for_dir,
    load_wwise_mapper,
    resolve_material_textures,
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
    if not cfg.CLI_EXE.exists():
        raise FileNotFoundError(f"AnimeStudio.CLI.exe not found at {cfg.CLI_EXE}")
    if shutil.which("wine") is None:
        raise FileNotFoundError("wine executable not found in PATH")
    out_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["WINEDEBUG"] = "-all"
    if diffuse_only:
        env["AS_DIFFUSE_ONLY"] = "1"
    if skip_proxies:
        env["AS_SKIP_PROXIES"] = "1"
    cmd = [
        "wine", str(cfg.CLI_EXE),
        "Z:" + str(source_path).replace("/", "\\"),
        "Z:" + str(out_dir).replace("/", "\\"),
        "--game", "ZZZ",
        "--types", *types,
        "--group_assets", "ByType",
        "--export_type", export_mode,
    ]
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
        )
    if cab_active:
        with tempfile.NamedTemporaryFile("w", suffix=".blklist", delete=False, encoding="utf-8") as tf:
            for blk in blk_paths:
                tf.write("Z:" + str(blk).replace("/", "\\") + "\n")
            list_path = Path(tf.name)
        try:
            return export_blk_asset_types(
                list_path, out_dir, types=types, export_mode=export_mode,
                name_filter=name_filter, log_cb=log_cb,
                map_op=map_op, map_name=map_name, diffuse_only=diffuse_only, skip_proxies=skip_proxies,
            )
        finally:
            list_path.unlink(missing_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix="oz_stage_"))
    try:
        for blk in blk_paths:
            link = stage_dir / blk.name
            if not link.exists():
                os.symlink(blk, link)
        return export_blk_asset_types(
            stage_dir, out_dir, types=types, export_mode=export_mode,
            name_filter=name_filter, log_cb=log_cb, diffuse_only=diffuse_only, skip_proxies=skip_proxies,
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
    bin_parts: list[bytes] = []
    bin_len = 0

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

        node_base = len(nodes)
        skin_base = len(skins)
        for nd in jdata.get("nodes", []):
            nc = dict(nd)
            if "children" in nc:
                nc["children"] = [c + node_base for c in nc["children"]]
            if "mesh" in nc:
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

        c_scenes = jdata.get("scenes", [])
        c_sidx = jdata.get("scene", 0)
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
    return 0


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
) -> int:
    fbx_list = [str(p) for p in fbxs]
    obj_list = [str(p) for p in objs]
    py_expr = (
        "import bpy, os, re as _re\n"
        "bpy.ops.wm.read_factory_settings(use_empty=True)\n"
        "sc = bpy.context.scene\n"
        "hold = bpy.data.collections.new('Hold')\n"
        "hold.use_fake_user = True\n"
        "_canonical_img_names = {}\n"
        "def _img_key(img):\n"
        "    fp = getattr(img, 'filepath', '') or ''\n"
        "    if fp:\n"
        "        try:\n"
        "            ap = bpy.path.abspath(fp)\n"
        "            if os.path.isfile(ap):\n"
        "                return 'file:' + os.path.basename(ap).lower() + ':' + str(os.path.getsize(ap))\n"
        "        except Exception:\n"
        "            pass\n"
        "    nm = os.path.splitext(os.path.basename(fp or img.name))[0].lower()\n"
        "    nm = _re.sub(r'(?:\\.[0-9]+|\\s*\\([0-9]+\\))$', '', nm)\n"
        "    return 'stem:' + nm\n"
        "def _dedup_imgs():\n"
        "    remap = {}\n"
        "    for img in list(bpy.data.images):\n"
        "        if img.name in ('Render Result', 'Viewer Node'):\n"
        "            continue\n"
        "        k = _img_key(img)\n"
        "        cname = _canonical_img_names.get(k)\n"
        "        canon = bpy.data.images.get(cname) if cname else None\n"
        "        if canon is None:\n"
        "            _canonical_img_names[k] = img.name\n"
        "        elif canon != img:\n"
        "            remap[img] = canon\n"
        "    if remap:\n"
        "        for mat in bpy.data.materials:\n"
        "            if not mat.use_nodes or not mat.node_tree:\n"
        "                continue\n"
        "            for nd in mat.node_tree.nodes:\n"
        "                if nd.type == 'TEX_IMAGE' and nd.image in remap:\n"
        "                    nd.image = remap[nd.image]\n"
        "        for dup in remap:\n"
        "            bpy.data.images.remove(dup, do_unlink=True)\n"
        "def _stash():\n"
        "    for o in list(sc.collection.objects):\n"
        "        sc.collection.objects.unlink(o)\n"
        "        hold.objects.link(o)\n"
        "    for c in list(sc.collection.children):\n"
        "        sc.collection.children.unlink(c)\n"
        "        hold.children.link(c)\n"
        f"fbxs = {fbx_list!r}\n"
        f"objs = {obj_list!r}\n"
        "for idx, fp in enumerate(fbxs, 1):\n"
        "    try:\n"
        "        bpy.ops.import_scene.fbx(filepath=fp)\n"
        "        _stash()\n"
        "    except Exception as _e:\n"
        "        print(f'SKIP FBX {fp}: {_e}', flush=True)\n"
        "    if idx % 50 == 0:\n"
        "        _dedup_imgs()\n"
        "    if idx % 250 == 0:\n"
        "        print(f'Imported FBX {idx}/{len(fbxs)} (unique images={len(bpy.data.images)})', flush=True)\n"
        f"_is_char_dir = {char_name.startswith('Avatar_')!r}\n"
        "_imported_objs = False\n"
        "_need_objs = objs and (not any(o.type == 'MESH' for o in bpy.data.objects) or (_is_char_dir and not any(k in o.name.lower() for o in bpy.data.objects if o.type == 'MESH' for k in ('body', 'face', 'hair'))))\n"
        "if _need_objs:\n"
        "    _imported_objs = True\n"
        "    if _is_char_dir:\n"
        "        _sep = [p for p in objs if os.path.basename(p).lower().startswith('separatemesh_')]\n"
        "        if _sep:\n"
        "            _sep_low = ' '.join(os.path.basename(p).lower() for p in _sep)\n"
        "            objs = [p for p in objs if p in _sep or os.path.splitext(os.path.basename(p))[0].lower() not in _sep_low]\n"
        "    for idx, fp in enumerate(objs, 1):\n"
        "        if _is_char_dir or os.path.basename(fp).lower().startswith('separatemesh_'):\n"
        "            bpy.ops.wm.obj_import(filepath=fp, forward_axis='NEGATIVE_Y', up_axis='Z')\n"
        "        else:\n"
        "            bpy.ops.wm.obj_import(filepath=fp)\n"
        "        _stash()\n"
        "        if idx % 250 == 0:\n"
        "            print(f'Imported OBJ {idx}/{len(objs)}', flush=True)\n"
        "sc.collection.children.link(hold)\n"
        "_dedup_imgs()\n"
        "def _mat_has_img(m):\n"
        "    if m is None or not m.use_nodes or not m.node_tree:\n"
        "        return False\n"
        "    return any(n.type == 'TEX_IMAGE' and getattr(n, 'image', None) is not None for n in m.node_tree.nodes)\n"
        "def _is_greybox_mat(m):\n"
        "    if m is None:\n"
        "        return False\n"
        "    ml = m.name.lower()\n"
        "    if 'shadowproxy' in ml:\n"
        "        return True\n"
        "    if (ml == 'lit' or ml.startswith('lit.')) and not _mat_has_img(m):\n"
        "        return True\n"
        "    return False\n"
        "def _is_proxy_obj(obj):\n"
        "    cur = obj\n"
        "    while cur is not None:\n"
        "        nl = cur.name.lower()\n"
        "        if _re.search(r'(?:_col(?:$|_|lider)|_proxy(?:$|_|\\s|\\.|[0-9])|_hairshadow(?:$|_|\\s|\\.|[0-9])|_lod[1-3](?:$|_|\\s|\\.|[0-9])|^scenepropcollider_|^blockout_|^stage_.*_ld$|^sm_merged_bp_|^napmesh,)', nl):\n"
        "            return True\n"
        "        cur = cur.parent\n"
        "    if obj.type == 'MESH' and obj.data and obj.data.materials:\n"
        "        non_none = [m for m in obj.data.materials if m is not None]\n"
        "        if non_none and all(_is_greybox_mat(m) for m in non_none):\n"
        "            return True\n"
        "    return False\n"
        "_mesh_objs = [o for o in bpy.data.objects if o.type == 'MESH' and o.data]\n"
        "_non_proxy = [o for o in _mesh_objs if not _is_proxy_obj(o)]\n"
        "if _non_proxy:\n"
        "    for o in _mesh_objs:\n"
        "        if o not in _non_proxy:\n"
        "            bpy.data.objects.remove(o, do_unlink=True)\n"
        "def _has_base_tex(mat):\n"
        "    if not mat or not mat.use_nodes or not mat.node_tree:\n"
        "        return False\n"
        "    for nd in mat.node_tree.nodes:\n"
        "        if nd.type == 'TEX_IMAGE' and nd.image is not None:\n"
        "            for lk in nd.outputs[0].links:\n"
        "                if lk.to_socket.name == 'Base Color':\n"
        "                    return True\n"
        "    return False\n"
        f"_allow_fuzzy = {allow_fuzzy_fallback!r} or _imported_objs\n"
        "if _allow_fuzzy:\n"
        f"    pngs = [str(p) for p in sorted(__import__('pathlib').Path({str(char_dir)!r}).rglob('*.png'))]\n"
        f"    _mat_to_tex = {mat_to_tex!r}\n"
        "    _png_by_stem = {os.path.splitext(os.path.basename(_p))[0].lower(): _p for _p in pngs}\n"
        "    diffuse_pngs = []\n"
        "    for _p in pngs:\n"
        "        _st = os.path.splitext(os.path.basename(_p))[0].lower()\n"
        "        if _st.endswith(('_n', '_m', '_a', '_e', '_h', '_b', '_ao', '_occ', '_rough', '_metal', '_spec', '_ramp', '_lightmap', '_lm', '_norm', '_normal', '_bump', '_mask', '_sdf', '_data', '_rm')):\n"
        "            continue\n"
        "        diffuse_pngs.append((_st, _p))\n"
        "    _img_cache = {}\n"
        "    _mat_cache = {}\n"
        "    def _pick_tex(name):\n"
        "        nl = _re.sub(r'\\.[0-9]+$', '', name.lower())\n"
        "        _exact = _mat_to_tex.get(nl)\n"
        "        if _exact and _exact in _png_by_stem:\n"
        "            return _png_by_stem[_exact]\n"
        "        nl = _re.sub(r'\\s*\\[[^\\]]+\\]$', '', nl)\n"
        "        nl = _re.sub(r'\\s*\\([0-9]+\\)$', '', nl).strip()\n"
        "        nl = _re.sub(r'^separatemesh_.*?_model_', '', nl)\n"
        "        nl = _re.sub(r'^separatemesh_avatar_[^_]+_[^_]+_[^_]+_', '', nl)\n"
        "        nl = _re.sub(r'_(?:lod[0-9]+|model)$', '', nl)\n"
        "        m_num = _re.search(r'_([0-9]+)$', nl)\n"
        "        num_suf = m_num.group(1) if m_num else None\n"
        "        nl_base = _re.sub(r'_[0-9]+$', '', nl)\n"
        "        toks = [t for t in nl_base.split('_') if t]\n"
        "        best_p, best_s = None, 0\n"
        "        for st, fp in diffuse_pngs:\n"
        "            if any(k in st for k in ('decal', 'floodlight', '_eff')) and not any(k in nl for k in ('decal', 'floodlight', 'eff', 'light')):\n"
        "                continue\n"
        "            sc_val = 0\n"
        "            map_bonus = (40 if (num_suf and f'_map{num_suf}' in st) else 0)\n"
        "            if st == nl + '_d' or st == nl_base + '_d' or st == nl:\n"
        "                sc_val = 1000\n"
        "            elif len(nl) >= 6 and st.startswith(nl + '_'):\n"
        "                sc_val = 800 + (20 if st.endswith('_d') else 0) + map_bonus\n"
        "            elif len(nl_base) >= 6 and st.startswith(nl_base + '_'):\n"
        "                sc_val = 500 + (20 if st.endswith('_d') else 0) + map_bonus\n"
        "            elif len(toks) >= 3:\n"
        "                for k in range(len(toks), 2, -1):\n"
        "                    pref = '_'.join(toks[:k])\n"
        "                    if st.startswith(pref + '_'):\n"
        "                        sc_val = 100 + k * 10 + (10 if st.endswith('_d') else 0) + map_bonus\n"
        "                        break\n"
        "            if sc_val > best_s:\n"
        "                best_s, best_p = sc_val, fp\n"
        "        return best_p\n"
        "    def _img_has_alpha(img):\n"
        "        if img.depth not in (32, 64, 128) or not img.has_data:\n"
        "            return False\n"
        "        try:\n"
        "            px = img.pixels\n"
        "            n = len(px)\n"
        "            step = max(4, (n // 4096) * 4)\n"
        "            for i in range(3, n, step):\n"
        "                if px[i] < 0.98:\n"
        "                    return True\n"
        "        except Exception:\n"
        "            pass\n"
        "        return False\n"
        "    def _make_mat(fp, obj_name=''):\n"
        "        nl = (os.path.basename(fp) + ' ' + obj_name).lower()\n"
        "        is_sky = bool(_re.search(r'(?:^|[_\\s.])(skybox|skydome|sky_?dome|sky_?sphere|sky_?plane|sky|cloud|dome|backdrop)(?:$|[_\\s.0-9])', nl))\n"
        "        is_cutout = any(t in nl for t in ('hair', 'lash', 'leaf', 'tree', 'plant', 'bush', 'grass', 'foliage', 'shadow'))\n"
        "        key = (fp, is_sky, is_cutout)\n"
        "        if key in _mat_cache:\n"
        "            return _mat_cache[key]\n"
        "        img = _img_cache.get(fp)\n"
        "        if img is None:\n"
        "            img = bpy.data.images.load(fp, check_existing=True)\n"
        "            _img_cache[fp] = img\n"
        "        mat = bpy.data.materials.new(name=os.path.splitext(os.path.basename(fp))[0])\n"
        "        mat.use_nodes = True\n"
        "        nt = mat.node_tree\n"
        "        bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None) or nt.nodes.new('ShaderNodeBsdfPrincipled')\n"
        "        tex = nt.nodes.new('ShaderNodeTexImage')\n"
        "        tex.image = img\n"
        "        nt.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])\n"
        "        is_trans_hint = is_sky or is_cutout or any(t in nl for t in ('decal', 'glass', 'eff', 'fx', 'floodlight'))\n"
        "        if is_trans_hint and _img_has_alpha(img):\n"
        "            nt.links.new(tex.outputs['Alpha'], bsdf.inputs['Alpha'])\n"
        "            if hasattr(mat, 'blend_method'):\n"
        "                mat.blend_method = 'CLIP' if (is_cutout or 'decal' in nl) else 'BLEND'\n"
        "            if hasattr(mat, 'alpha_threshold'):\n"
        "                mat.alpha_threshold = 0.45\n"
        "        if is_sky or is_cutout:\n"
        "            mat.use_backface_culling = False\n"
        "        _mat_cache[key] = mat\n"
        "        return mat\n"
        "    if diffuse_pngs:\n"
        "        for o in bpy.data.objects:\n"
        "            if o.type != 'MESH' or not o.data:\n"
        "                continue\n"
        "            if o.data.materials:\n"
        "                for _si in range(len(o.data.materials)):\n"
        "                    _om = o.data.materials[_si]\n"
        "                    if _has_base_tex(_om):\n"
        "                        continue\n"
        "                    _mn = _om.name if _om else ''\n"
        "                    _pn = o.parent.name if o.parent else ''\n"
        "                    _fp = (_pick_tex(_mn) if _mn else None) or _pick_tex(o.name) or _pick_tex(o.data.name) or (_pick_tex(_pn) if _pn else None)\n"
        "                    if _fp:\n"
        "                        o.data.materials[_si] = _make_mat(_fp, o.name)\n"
        "            else:\n"
        "                _pn = o.parent.name if o.parent else ''\n"
        "                _fp = _pick_tex(o.name) or _pick_tex(o.data.name) or (_pick_tex(_pn) if _pn else None)\n"
        "                if _fp:\n"
        "                    o.data.materials.append(_make_mat(_fp, o.name))\n"
        "_dedup_imgs()\n"
        "for mat in bpy.data.materials:\n"
        "    if not mat.use_nodes or not mat.node_tree:\n"
        "        continue\n"
        "    nt = mat.node_tree\n"
        "    for nd in list(nt.nodes):\n"
        "        if nd.type == 'TEX_IMAGE' and not any(lk.to_socket.name == 'Base Color' for lk in nd.outputs[0].links):\n"
        "            nt.nodes.remove(nd)\n"
        "    bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)\n"
        "    if bsdf and bsdf.inputs['Base Color'].is_linked:\n"
        "        dc = mat.diffuse_color\n"
        "        if any(abs(float(dc[k]) - 1.0) > 0.02 for k in range(3)):\n"
        "            src_sock = bsdf.inputs['Base Color'].links[0].from_socket\n"
        "            if src_sock.node.type != 'MIX':\n"
        "                mx = nt.nodes.new('ShaderNodeMix')\n"
        "                mx.data_type = 'RGBA'\n"
        "                mx.blend_type = 'MULTIPLY'\n"
        "                mx.inputs[0].default_value = 1.0\n"
        "                mx.inputs[7].default_value = (float(dc[0]), float(dc[1]), float(dc[2]), 1.0)\n"
        "                nt.links.new(src_sock, mx.inputs[6])\n"
        "                nt.links.new(mx.outputs[2], bsdf.inputs['Base Color'])\n"
        "    if any(t in mat.name.lower() for t in ('hair', 'lash', 'leaf', 'tree', 'plant', 'bush', 'grass', 'foliage', 'sky', 'decal')):\n"
        "        mat.use_backface_culling = False\n"
        "try:\n"
        "    bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)\n"
        "except Exception:\n"
        "    pass\n"
        f"bpy.ops.export_scene.gltf(filepath={str(out_glb)!r}, export_format='GLB')\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
        tf.write(py_expr)
        script_path = Path(tf.name)
    try:
        return run_streaming_cmd([blender, "--background", "--python", str(script_path)], log_cb=log_cb)
    finally:
        script_path.unlink(missing_ok=True)


def _convert_dir_to_glb(char_dir: Path, char_name: str, log_cb=None) -> int:
    blender = shutil.which("blender")
    if not blender:
        _emit(log_cb, "WARNING: blender not found in PATH; skipping GLB conversion.")
        return 0
    fbxs = sorted(char_dir.rglob("*.fbx"))
    objs = sorted(char_dir.rglob("*.obj"))
    def _filter_models(paths: list[Path]) -> list[Path]:
        non_col = [p for p in paths if not any(t in p.stem.lower() for t in ("_col_", "_proxy"))]
        base = non_col if non_col else paths
        non_lod = [p for p in base if not any(t in p.stem.lower() for t in ("_lod1", "_lod2", "_lod3"))]
        return non_lod if non_lod else base
    fbxs = _filter_models(fbxs)
    objs = _filter_models(objs)
    if not fbxs and not objs:
        _emit(log_cb, f"No FBX/OBJ models found in {char_dir}; skipping GLB conversion.")
        return 0
    glb_path = char_dir / f"{char_name}.glb"
    cab_active = _zzz_cab_map_exists() and bool(fbxs)
    is_char = char_name.startswith("Avatar_")
    mat_to_tex, _, _ = ({}, [], []) if (cab_active and not is_char and not objs) else resolve_material_textures(char_dir)
    chunk_size = 350
    if len(fbxs) <= chunk_size:
        _emit(log_cb, f"Converting {len(fbxs)} FBX / {len(objs)} OBJ model(s) to GLB via Blender -> {glb_path.name}")
        return _blender_convert_chunk(
            blender, char_dir, char_name, fbxs, objs, glb_path, mat_to_tex,
            allow_fuzzy_fallback=(not cab_active or is_char), log_cb=log_cb,
        )
    chunks = [fbxs[i : i + chunk_size] for i in range(0, len(fbxs), chunk_size)]
    _emit(log_cb, f"Converting {len(fbxs)} FBX model(s) in {len(chunks)} bounded Blender batch(es) -> {glb_path.name}")
    tmp_dir = Path(tempfile.mkdtemp(prefix="oz_glb_chunks_"))
    try:
        chunk_glbs: list[Path] = []
        for idx, c_fbxs in enumerate(chunks):
            c_glb = tmp_dir / f"chunk_{idx:03d}.glb"
            _emit(log_cb, f"[Batch {idx + 1}/{len(chunks)}] Converting {len(c_fbxs)} FBXs...")
            rc = _blender_convert_chunk(
                blender, char_dir, char_name, c_fbxs, [], c_glb, mat_to_tex,
                allow_fuzzy_fallback=(not cab_active), log_cb=log_cb,
            )
            if rc != 0 or not c_glb.is_file():
                return rc or 1
            chunk_glbs.append(c_glb)
        return _merge_glbs(chunk_glbs, glb_path, log_cb=log_cb)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
        chars = load_character_index()
        blocks = chars.get(char_name)
        if blocks and cfg.CLI_EXE.is_file() and shutil.which("wine") is not None:
            files_root = cfg.resolve_files_root(game_root or cfg.GAME_ROOT)
            blocks_dir = files_root / "Blocks"
            resolved = [blocks_dir / f"{os.path.basename(b)}" for b in blocks if (blocks_dir / f"{os.path.basename(b)}").is_file()]
            if not resolved:
                resolved = [blocks_dir / f"{b}.blk" for b in blocks if (blocks_dir / f"{b}.blk").is_file()]
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
                    diffuse_only=(not export_shaders),
                )
                if convert_glb and last_rc == 0:
                    rc_glb = _convert_dir_to_glb(dest, char_name, log_cb=log_cb)
                    if rc_glb != 0:
                        last_rc = rc_glb
                rc_anim = export_blks_batch(
                    resolved, dest,
                    types=["AnimationClip"],
                    export_mode="Convert",
                    name_filter=char_short,
                    log_cb=log_cb,
                )
                if rc_anim != 0:
                    _emit(log_cb, f"WARNING: AnimationClip export returned rc={rc_anim} (some clips could not be decoded).")
                return last_rc
        raise FileNotFoundError(
            f"Missing {script} (and AnimeStudio.CLI.exe fallback unavailable). "
            "Point OpenZenless to your FinalOutput/Extractor folder."
        )
    rc = run_streaming_cmd([sys.executable, str(script), char_name], log_cb=log_cb)
    if rc != 0:
        return rc
    if convert_glb:
        blender = shutil.which("blender")
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


def export_character_animations_only(
    char_name: str,
    game_root: Path,
    out_dir: Path,
    log_cb=None,
) -> int:
    chars = load_character_index()
    blocks = chars.get(char_name)
    if not blocks:
        raise KeyError(f"Character '{char_name}' not found in character index.")
    files_root = cfg.resolve_files_root(game_root)
    blocks_dir = files_root / "Blocks"
    resolved = [blocks_dir / f"{os.path.basename(b)}" for b in blocks if (blocks_dir / f"{os.path.basename(b)}").is_file()]
    if not resolved:
        resolved = [blocks_dir / f"{b}.blk" for b in blocks if (blocks_dir / f"{b}.blk").is_file()]
    if not resolved:
        resolved = [files_root / b for b in blocks if (files_root / b).is_file()]
    if not resolved:
        raise FileNotFoundError(f"No .blk files found for '{char_name}' under {files_root}")

    dest_dir = out_dir / char_name / "Animations"
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
        stages = load_curated_map_index()
        if cfg.CLI_EXE.is_file() and shutil.which("wine") is not None and stages:
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
                    resolved = [blocks_dir / os.path.basename(b) for b in blocks if (blocks_dir / os.path.basename(b)).is_file()]
                    if not resolved:
                        resolved = [blocks_dir / f"{b}.blk" for b in blocks if (blocks_dir / f"{b}.blk").is_file()]
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
                        rc = export_blks_batch(
                            resolved, dest,
                            types=map_types,
                            export_mode="Convert",
                            name_filter="",
                            log_cb=log_cb,
                            use_cab_map=True,
                            diffuse_only=(not export_shaders),
                            skip_proxies=True,
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


def export_audio_pck(
    source_path: Path,
    out_dir: Path,
    limit: int = 0,
    use_wwise_map: bool = True,
    decode_wav: bool = False,
    log_cb=None,
) -> int:
    if not source_path.exists():
        raise FileNotFoundError(f"Audio source not found: {source_path}")
    pcks = [source_path] if source_path.is_file() else sorted(source_path.rglob("*.pck"))
    if not pcks:
        raise FileNotFoundError(f"No .pck files found in {source_path}")
    mapper = None
    if use_wwise_map:
        _emit(log_cb, f"Loading AnimeWwise map: {cfg.NAP_MAP_FILE.name}...")
        mapper = load_wwise_mapper()
        if mapper is None:
            _emit(log_cb, "WARNING: nap.map not found; falling back to raw AKPK filenames.")
    if decode_wav and not cfg.VGMSTREAM_CLI.is_file():
        _emit(log_cb, f"WARNING: vgmstream-cli not found at {cfg.VGMSTREAM_CLI}; keeping .wem files.")
        decode_wav = False

    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    mapped_count = 0
    for pck in pcks:
        ak = parse_akpk(pck)
        if ak.get("error"):
            _emit(log_cb, f"SKIP {pck.name}: {ak['error']}")
            continue
        entries = ak["entries"][:limit] if limit > 0 else ak["entries"]
        lang_hint = "English" if "/En/" in str(pck) else (pck.parent.name if pck.parent.name in ("En", "Jp", "Cn", "Kr") else "SFX")
        with pck.open("rb") as src:
            for e in entries:
                src.seek(e["offset"])
                raw = src.read(e["size"])
                stem = Path(e["name"]).stem.lower()
                res = mapper.get_key(stem, addLang=True) if (mapper and e["name"].endswith(".wem")) else None
                if res:
                    rel_path, raw_lang = res[0], (res[1] or lang_hint)
                    lang = raw_lang.split("(")[0].strip() if "(" in raw_lang else raw_lang
                    dest = out_dir / lang / (rel_path.replace("\\", "/").lstrip("/") + ".wem")
                    mapped_count += 1
                elif mapper is not None:
                    dest = out_dir / lang_hint / "_Unresolved" / pck.stem / e["name"]
                else:
                    sub_out = out_dir / pck.stem if len(pcks) > 1 else out_dir
                    dest = sub_out / e["name"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                if decode_wav and dest.suffix.lower() == ".wem":
                    wav_dest = dest.with_suffix(".wav")
                    proc = subprocess.run(
                        [str(cfg.VGMSTREAM_CLI), "-o", str(wav_dest), str(dest)],
                        capture_output=True, text=True,
                    )
                    if proc.returncode == 0 and wav_dest.is_file():
                        dest.unlink(missing_ok=True)
        total += len(entries)
        _emit(log_cb, f"Unpacked {len(entries)} entries from {pck.name} (mapped so far: {mapped_count}/{total})")
    _emit(log_cb, f"Total audio entries extracted: {total} (AnimeWwise mapped: {mapped_count})")
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
    has_cli = cfg.CLI_EXE.is_file() and shutil.which("wine") is not None

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
    font_reason = "missing Blocks/" if not blocks_dir.exists() else "missing AnimeStudio.CLI.exe or wine"

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
            _emit(log_cb, "Exporting all Fonts from Blocks/...")
            try:
                rc = export_blk_asset_types(blocks_dir, out_dir / "Fonts", ["Font"], "Convert", log_cb=log_cb)
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
