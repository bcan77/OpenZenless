"""Batch and pipeline exporters for characters, animations, maps, fonts, assets, and audio."""
from __future__ import annotations

import os
import re
import shutil
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
    cmd = [
        "wine", str(cfg.CLI_EXE),
        "Z:" + str(source_path).replace("/", "\\"),
        "Z:" + str(out_dir).replace("/", "\\"),
        "--game", "ZZZ",
        "--types", *types,
        "--group_assets", "ByType",
        "--export_type", export_mode,
    ]
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


def export_blks_batch(
    blk_paths: list[Path],
    out_dir: Path,
    types: list[str],
    export_mode: str = "Convert",
    name_filter: str = "",
    log_cb=None,
) -> int:
    if not blk_paths:
        return 0
    if len(blk_paths) == 1:
        return export_blk_asset_types(
            blk_paths[0], out_dir, types=types, export_mode=export_mode,
            name_filter=name_filter, log_cb=log_cb,
        )
    stage_dir = Path(tempfile.mkdtemp(prefix="oz_stage_"))
    try:
        for blk in blk_paths:
            link = stage_dir / blk.name
            if not link.exists():
                os.symlink(blk, link)
        return export_blk_asset_types(
            stage_dir, out_dir, types=types, export_mode=export_mode,
            name_filter=name_filter, log_cb=log_cb,
        )
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


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
    mat_to_tex, _, _ = resolve_material_textures(char_dir)
    fbx_list = [str(p) for p in fbxs]
    obj_list = [str(p) for p in objs]
    py_expr = (
        "import bpy\n"
        "bpy.ops.wm.read_factory_settings(use_empty=True)\n"
        "sc = bpy.context.scene\n"
        "hold = bpy.data.collections.new('Hold')\n"
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
        "    if idx % 250 == 0:\n"
        "        print(f'Imported FBX {idx}/{len(fbxs)}', flush=True)\n"
        "import os, re as _re\n"
        f"_is_char_dir = {char_name.startswith('Avatar_')!r}\n"
        "if not any(o.type == 'MESH' for o in bpy.data.objects):\n"
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
        f"pngs = [str(p) for p in sorted(__import__('pathlib').Path({str(char_dir)!r}).rglob('*.png'))]\n"
        f"_mat_to_tex = {mat_to_tex!r}\n"
        "_png_by_stem = {os.path.splitext(os.path.basename(_p))[0].lower(): _p for _p in pngs}\n"
        "diffuse_pngs = []\n"
        "for _p in pngs:\n"
        "    _st = os.path.splitext(os.path.basename(_p))[0].lower()\n"
        "    if _st.endswith(('_n', '_m', '_a', '_e', '_h', '_b', '_ao', '_occ', '_rough', '_metal', '_spec', '_ramp', '_lightmap', '_lm', '_norm', '_normal', '_bump', '_mask', '_sdf', '_data', '_rm')):\n"
        "        continue\n"
        "    diffuse_pngs.append((_st, _p))\n"
        "_img_cache = {}\n"
        "_mat_cache = {}\n"
        "def _pick_tex(name):\n"
        "    nl = _re.sub(r'\\.[0-9]+$', '', name.lower())\n"
        "    _exact = _mat_to_tex.get(nl)\n"
        "    if _exact and _exact in _png_by_stem:\n"
        "        return _png_by_stem[_exact]\n"
        "    nl = _re.sub(r'\\s*\\[[^\\]]+\\]$', '', nl)\n"
        "    nl = _re.sub(r'\\s*\\([0-9]+\\)$', '', nl).strip()\n"
        "    nl = _re.sub(r'^separatemesh_.*?_model_', '', nl)\n"
        "    nl = _re.sub(r'^separatemesh_avatar_[^_]+_[^_]+_[^_]+_', '', nl)\n"
        "    nl = _re.sub(r'_(?:lod[0-9]+|model)$', '', nl)\n"
        "    m_num = _re.search(r'_([0-9]+)$', nl)\n"
        "    num_suf = m_num.group(1) if m_num else None\n"
        "    nl_base = _re.sub(r'_[0-9]+$', '', nl)\n"
        "    toks = [t for t in nl_base.split('_') if t]\n"
        "    cc_words = [w.lower() for w in _re.findall(r'[A-Z][a-z]{3,}|[a-z]{4,}', _re.sub(r'\\.[0-9]+$', '', name)) if w.lower() not in ('prop', 'wall', 'decal', 'model', 'mesh', 'separatemesh', 'avatar', 'floor', 'ground', 'ceiling', 'building', 'object')]\n"
        "    is_avatar = len(toks) >= 2 and toks[1] in ('body', 'face', 'hair', 'weapon', 'eye', 'cloth', 'eyeglass', 'glasses', 'tail', 'coat')\n"
        "    best_p, best_s = None, 0\n"
        "    for st, fp in diffuse_pngs:\n"
        "        if any(k in st for k in ('decal', 'floodlight', '_eff')) and not any(k in nl for k in ('decal', 'floodlight', 'eff', 'light')):\n"
        "            continue\n"
        "        sc_val = 0\n"
        "        map_bonus = (40 if (num_suf and f'_map{num_suf}' in st) else 0)\n"
        "        if st == nl + '_d' or st == nl_base + '_d' or st == nl:\n"
        "            sc_val = 1000\n"
        "        elif len(nl) >= 6 and st.startswith(nl + '_'):\n"
        "            sc_val = 800 + (20 if st.endswith('_d') else 0) + map_bonus\n"
        "        elif len(nl_base) >= 6 and st.startswith(nl_base + '_'):\n"
        "            sc_val = 500 + (20 if st.endswith('_d') else 0) + map_bonus\n"
        "        elif is_avatar and len(toks) >= 2 and st.startswith('_'.join(toks[:2]) + '_'):\n"
        "            sc_val = 250 + (20 if st.endswith('_d') else 0) + map_bonus\n"
        "        elif is_avatar and f'_{toks[1]}' in st:\n"
        "            sc_val = 160 + (20 if st.endswith('_d') else 0) + map_bonus\n"
        "        elif len(toks) >= 3:\n"
        "            for k in range(len(toks), 2, -1):\n"
        "                pref = '_'.join(toks[:k])\n"
        "                if st.startswith(pref + '_'):\n"
        "                    sc_val = 100 + k * 10 + (10 if st.endswith('_d') else 0) + map_bonus\n"
        "                    break\n"
        "        elif is_avatar and toks[1] in ('hair', 'eyeglass', 'glasses', 'tail', 'coat') and ('_body' in st or '_face' in st) and st.endswith('_d'):\n"
        "            sc_val = 60\n"
        "        if sc_val == 0 and len(toks) >= 3:\n"
        "            noun = toks[-1].rstrip('s')\n"
        "            if len(noun) >= 5 and noun in st:\n"
        "                sc_val = 90 + (10 if st.endswith('_d') else 0)\n"
        "        if sc_val == 0 and toks and cc_words:\n"
        "            for _cw in cc_words:\n"
        "                if _cw == toks[0]:\n"
        "                    continue\n"
        "                if len(_cw) >= 4 and st.startswith(toks[0] + '_') and _cw in st:\n"
        "                    sc_val = 75 + (10 if st.endswith('_d') else 0)\n"
        "                    break\n"
        "                if len(_cw) >= 6 and _cw in st:\n"
        "                    sc_val = 65 + (10 if st.endswith('_d') else 0)\n"
        "                    break\n"
        "        if sc_val > best_s:\n"
        "            best_s, best_p = sc_val, fp\n"
        "    return best_p\n"
        "def _has_base_tex(mat):\n"
        "    if not mat or not mat.use_nodes or not mat.node_tree:\n"
        "        return False\n"
        "    for nd in mat.node_tree.nodes:\n"
        "        if nd.type == 'TEX_IMAGE' and nd.image is not None:\n"
        "            for lk in nd.outputs[0].links:\n"
        "                if lk.to_socket.name == 'Base Color':\n"
        "                    return True\n"
        "    return False\n"
        "def _img_has_alpha(img):\n"
        "    if img.depth not in (32, 64, 128) or not img.has_data:\n"
        "        return False\n"
        "    try:\n"
        "        px = img.pixels\n"
        "        n = len(px)\n"
        "        step = max(4, (n // 4096) * 4)\n"
        "        for i in range(3, n, step):\n"
        "            if px[i] < 0.98:\n"
        "                return True\n"
        "    except Exception:\n"
        "        pass\n"
        "    return False\n"
        "def _make_mat(fp, obj_name=''):\n"
        "    nl = (os.path.basename(fp) + ' ' + obj_name).lower()\n"
        "    is_sky = bool(_re.search(r'(?:^|[_\\s.])(skybox|skydome|sky_?dome|sky_?sphere|sky_?plane|sky|cloud|dome|backdrop)(?:$|[_\\s.0-9])', nl))\n"
        "    is_cutout = any(t in nl for t in ('hair', 'lash', 'leaf', 'tree', 'plant', 'bush', 'grass', 'foliage', 'shadow'))\n"
        "    key = (fp, is_sky, is_cutout)\n"
        "    if key in _mat_cache:\n"
        "        return _mat_cache[key]\n"
        "    img = _img_cache.get(fp)\n"
        "    if img is None:\n"
        "        img = bpy.data.images.load(fp, check_existing=True)\n"
        "        _img_cache[fp] = img\n"
        "    mat = bpy.data.materials.new(name=os.path.splitext(os.path.basename(fp))[0])\n"
        "    mat.use_nodes = True\n"
        "    nt = mat.node_tree\n"
        "    bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)\n"
        "    if bsdf is None:\n"
        "        bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')\n"
        "    tex = nt.nodes.new('ShaderNodeTexImage')\n"
        "    tex.image = img\n"
        "    nt.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])\n"
        "    is_trans_hint = is_sky or is_cutout or any(t in nl for t in ('decal', 'glass', 'eff', 'fx', 'floodlight'))\n"
        "    has_a = _img_has_alpha(img) if is_trans_hint else False\n"
        "    if has_a:\n"
        "        nt.links.new(tex.outputs['Alpha'], bsdf.inputs['Alpha'])\n"
        "        if hasattr(mat, 'blend_method'):\n"
        "            mat.blend_method = 'CLIP' if (is_cutout or 'decal' in nl) else 'BLEND'\n"
        "        if hasattr(mat, 'alpha_threshold'):\n"
        "            mat.alpha_threshold = 0.45\n"
        "    if is_sky or is_cutout:\n"
        "        mat.use_backface_culling = False\n"
        "    _mat_cache[key] = mat\n"
        "    return mat\n"
        "if diffuse_pngs:\n"
        "    _mesh_objs = [o for o in bpy.data.objects if o.type == 'MESH' and o.data]\n"
        "    _non_proxy = [o for o in _mesh_objs if not _re.search(r'(?:_col_|_col$|_proxy(?:\\.[0-9]+)?$|_hairshadow(?:\\.[0-9]+)?$|_lod[1-3](?:\\.[0-9]+)?$)', o.name.lower())]\n"
        "    if _non_proxy:\n"
        "        for o in _mesh_objs:\n"
        "            if o not in _non_proxy:\n"
        "                bpy.data.objects.remove(o, do_unlink=True)\n"
        "    for o in bpy.data.objects:\n"
        "        if o.type != 'MESH' or not o.data:\n"
        "            continue\n"
        "        if o.data.materials:\n"
        "            for _si in range(len(o.data.materials)):\n"
        "                _om = o.data.materials[_si]\n"
        "                if _has_base_tex(_om):\n"
        "                    continue\n"
        "                _mn = _om.name if _om else ''\n"
        "                _pn = o.parent.name if o.parent else ''\n"
        "                _fp = (_pick_tex(_mn) if _mn else None) or _pick_tex(o.name) or _pick_tex(o.data.name) or (_pick_tex(_pn) if _pn else None)\n"
        "                if _fp:\n"
        "                    o.data.materials[_si] = _make_mat(_fp, o.name)\n"
        "        else:\n"
        "            _pn = o.parent.name if o.parent else ''\n"
        "            _fp = _pick_tex(o.name) or _pick_tex(o.data.name) or (_pick_tex(_pn) if _pn else None)\n"
        "            if _fp:\n"
        "                o.data.materials.append(_make_mat(_fp, o.name))\n"
        f"bpy.ops.export_scene.gltf(filepath={str(glb_path)!r}, export_format='GLB')\n"
    )
    _emit(log_cb, f"Converting {len(fbxs)} FBX / {len(objs)} OBJ model(s) to GLB via Blender -> {glb_path.name}")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
        tf.write(py_expr)
        script_path = Path(tf.name)
    try:
        return run_streaming_cmd([blender, "--background", "--python", str(script_path)], log_cb=log_cb)
    finally:
        script_path.unlink(missing_ok=True)


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
                types_list = ["Mesh", "Texture2D", "Animator", "Avatar", "Material"]
                if export_shaders:
                    types_list.append("Shader")
                last_rc = export_blks_batch(
                    resolved, dest,
                    types=types_list,
                    export_mode="Convert",
                    name_filter=char_short,
                    log_cb=log_cb,
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
                    _emit(log_cb, f"Exporting map {sname} ({len(resolved)} .blk(s)) via AnimeStudio.CLI -> {dest}")
                    map_types = ["Mesh", "Texture2D", "GameObject", "Animator", "Material"]
                    if export_shaders:
                        map_types.append("Shader")
                    rc = export_blks_batch(
                        resolved, dest,
                        types=map_types,
                        export_mode="Convert",
                        name_filter="",
                        log_cb=log_cb,
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
