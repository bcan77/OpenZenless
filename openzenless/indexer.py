"""Asset indexing, character/stage/section loaders, and startup asset mapping."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config as cfg

_WWISE_MAPPER_CACHE = None


def _emit(log_cb, msg: str):
    if log_cb is not None:
        log_cb(msg)
    else:
        print(msg, flush=True)


def load_character_index() -> dict[str, list[str]]:
    for cand in (
        cfg.EXTRACTOR_ROOT / "data" / "asset_map" / "character_index_lean.json",
        cfg.EXTRACTOR_ROOT / "data" / "asset_map" / "character_index_rich.json",
        cfg.CACHE_DIR / "asset_map" / "character_index_auto.json",
        cfg.CACHE_DIR / "asset_map" / "character_index_fallback.json",
    ):
        if cand.exists():
            try:
                data = json.loads(cand.read_text())
                chars = data.get("characters", data)
                return {k: v.get("blocks", []) for k, v in chars.items() if isinstance(v, dict)}
            except Exception:
                pass
    return {}


def load_stage_index() -> dict[str, list[str]]:
    stages: dict[str, list[str]] = {}
    for inv in (
        cfg.EXTRACTOR_ROOT / "data" / "all_maps_inventory.json",
        cfg.CACHE_DIR / "asset_map" / "stage_index_fallback.json",
        cfg.CACHE_DIR / "asset_map" / "stage_index_auto.json",
    ):
        if inv.exists():
            try:
                data = json.loads(inv.read_text())
                for k, v in (data.get("stages") or {}).items():
                    dst = stages.setdefault(k, [])
                    for b in v.get("blocks", []):
                        if b not in dst:
                            dst.append(b)
            except Exception:
                pass
    if stages:
        for sec, info in load_texture_sections().items():
            if sec.startswith("NapStreaming/"):
                sname = sec.split("/", 1)[1].split("/")[0]
                if sname and "." not in sname and sname not in _NON_MAP_SECTIONS:
                    blks = stages.setdefault(sname, [])
                    for b in info.get("blocks", []):
                        if b not in blks:
                            blks.append(b)
        for sname, blks in list(stages.items()):
            parts = sname.split("_")
            for k in range(len(parts) - 1, 1, -1):
                parent = "_".join(parts[:k])
                if parent in stages:
                    for b in stages[parent]:
                        if b not in blks:
                            blks.append(b)
    mm_p = cfg.CACHE_DIR / "asset_map" / "stage_mat_mesh_blocks_auto.json"
    owners_p = cfg.CACHE_DIR / "asset_map" / "stage_blk_owners_auto.json"
    owners_data: dict[str, list] = {}
    if owners_p.is_file():
        try:
            owners_data = json.loads(owners_p.read_text())
        except Exception:
            owners_data = {}
    nap_blocks_by_stage: dict[str, set[str]] = {}
    for sec, info in load_texture_sections().items():
        if sec.startswith("NapStreaming/"):
            sname = sec.split("/", 1)[1].split("/")[0]
            if sname and "." not in sname:
                nap_blocks_by_stage.setdefault(sname, set()).update(info.get("blocks", ()))
    known_stage_roots = {
        t.lower()
        for s in stages
        for t in re.sub(r"^(?:Stage|Env|Scene|Map)_", "", s).split("_")
        if len(t) >= 3 and not t.isdigit()
    }
    if mm_p.is_file():
        try:
            mm_data = json.loads(mm_p.read_text())
            for sname, blks in stages.items():
                clean = re.sub(r"^(?:Stage|Env|Scene|Map)_", "", sname)
                toks = [t.lower() for t in clean.split("_") if len(t) >= 3 and not t.isdigit()]
                mm_toks = [toks[0]] if (toks and mm_data.get(toks[0])) else toks
                mm_cand: list[str] = []
                for t in mm_toks:
                    for b in mm_data.get(t, ()):
                        if b not in mm_cand:
                            mm_cand.append(b)
                nap_set = nap_blocks_by_stage.get(sname, set())
                rich_mm = [
                    b for b in mm_cand
                    if (owners_data.get(b) is None) or (owners_data[b][0] in mm_toks and owners_data[b][1] >= 20)
                ]
                min_ocnt = 20 if len(rich_mm) >= 5 else 10
                if owners_data and len(mm_cand) >= 3:
                    mm_set = set(mm_cand)
                    filtered: list[str] = []
                    for b in blks:
                        if b in nap_set:
                            filtered.append(b)
                            continue
                        own = owners_data.get(b)
                        if own and isinstance(own, list) and len(own) >= 2:
                            opref, ocnt = own[0], own[1]
                            if opref in known_stage_roots and opref not in toks:
                                continue
                            if ocnt < min_ocnt:
                                continue
                        elif not own and b not in mm_set:
                            continue
                        filtered.append(b)
                    blks[:] = filtered
                add_list = rich_mm if len(rich_mm) >= 5 else mm_cand
                for b in add_list:
                    if b not in blks:
                        blks.append(b)
        except Exception:
            pass
    return stages


_NON_MAP_SECTIONS = frozenset({
    "AudioData", "Doors", "ReflectionData", "SpecialEnvVolume",
    "Map3D", "Others", "Stage_Sound", "Env_Test",
})


def load_curated_map_index() -> dict[str, list[str]]:
    stages = load_stage_index()
    curated: dict[str, list[str]] = {}
    maps_dir = cfg.FINAL_ROOT / "Maps_Combined"
    if maps_dir.exists():
        for glb in sorted(maps_dir.glob("*_full.glb")):
            name = glb.name[:-9]
            curated[name] = stages.get(name, [])
    if not curated:
        vd = cfg.FINAL_ROOT / "Viewer" / "viewer_data.json"
        if vd.exists():
            try:
                data = json.loads(vd.read_text())
                for m in data.get("maps", []):
                    mid = m.get("id") or m.get("name")
                    if mid:
                        curated[mid] = stages.get(mid, [])
            except Exception:
                pass
    if not curated and stages:
        return {
            k: v
            for k, v in stages.items()
            if k not in _NON_MAP_SECTIONS
        }
    return curated


def load_wwise_mapper():
    global _WWISE_MAPPER_CACHE
    if _WWISE_MAPPER_CACHE is not None:
        return _WWISE_MAPPER_CACHE
    if not cfg.NAP_MAP_FILE.is_file():
        return None
    if str(cfg.ANIMEWWISE_DIR) not in sys.path:
        sys.path.insert(0, str(cfg.ANIMEWWISE_DIR))
    from mapper import Mapper
    _WWISE_MAPPER_CACHE = Mapper(str(cfg.NAP_MAP_FILE))
    return _WWISE_MAPPER_CACHE


def load_character_names() -> dict[str, str]:
    p = cfg.EXTRACTOR_ROOT / "data" / "character_names.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except Exception:
        return {}


def load_texture_sections() -> dict[str, dict]:
    if not cfg.SECTIONS_INDEX_FILE.exists():
        return {}
    try:
        return json.loads(cfg.SECTIONS_INDEX_FILE.read_text())
    except Exception:
        return {}


def has_asset_map(game_root: Path) -> bool:
    files_root = cfg.resolve_files_root(game_root)
    has_ext_chars = any(
        (cfg.EXTRACTOR_ROOT / "data" / "asset_map" / name).is_file()
        for name in ("character_index_lean.json", "character_index_rich.json")
    )
    has_ext_maps = (cfg.EXTRACTOR_ROOT / "data" / "all_maps_inventory.json").is_file()
    if has_ext_chars and has_ext_maps:
        return True
    manifest = cfg.CACHE_DIR / "asset_map" / "manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text())
            if data.get("files_root") == str(files_root):
                if cfg.CLI_EXE.is_file() and (files_root / "Blocks").is_dir():
                    if data.get("auto_characters", 0) > 0 or data.get("user_skipped"):
                        return True
                    return False
                if data.get("blk_count", 0) > 0 or data.get("pck_count", 0) > 0:
                    return True
                if not (files_root / "Blocks").exists() and not (files_root / "Audio").exists():
                    return True
        except Exception:
            pass
    return False


_AVATAR_RE = re.compile(
    r"(Avatar_(?:Male|Female)_Size\d+_[A-Za-z0-9]+(?:_(?!Ani|Body|Face|Hair|Eye|Model|UI|Weapon|Cloth|Skin|Skill|Common|Effect|NPC|Player|Story|Timeline|Mat|Tex|Mesh|LOD)[A-Za-z0-9]+)?)"
)
_STAGE_RE = re.compile(r"^(?:Stage|Scene|Map|Env)_[A-Za-z0-9_]+$")
_PROG_RE = re.compile(r"\[(\d+)/(\d+)\]\s+Processed")
_ASSET_TYPES_INTEREST = frozenset(
    ("Mesh", "Texture2D", "GameObject", "Animator", "Avatar", "AnimationClip", "SkinnedMeshRenderer", "Material")
)


def _parse_cli_asset_map(
    map_json: Path,
    map_dir: Path,
    log_cb=None,
    progress_cb=None,
) -> tuple[int, int, int]:
    total_bytes = max(1, map_json.stat().st_size)
    total_mb = max(1, total_bytes // (1024 * 1024))
    chars_auto: dict[str, set[str]] = {}
    short_blocks: dict[str, set[str]] = {}
    stages_auto: dict[str, set[str]] = {}
    tex_prefix_blocks: dict[str, dict[str, int]] = {}
    tex_key_blocks: dict[str, dict[str, int]] = {}
    tex_noun_blocks: dict[str, dict[str, int]] = {}
    tex_pathid: dict[str, list[str]] = {}
    mat_mesh_blocks: dict[str, dict[str, int]] = {}
    mesh_pref_counts: dict[str, dict[str, int]] = {}
    hlod_mesh_blks: set[str] = set()
    _HLOD_MESH_RE = re.compile(r"^(?:[A-Z]{1,3}|BRG)_[0-9]+_HLOD")
    _TECH_MESH_PREFS = frozenset((
        "curveplacetool", "interiorsegment", "combinegroundquadmesh", "volumetricfog",
        "loop", "roof", "dynamicobjects", "weathergroup", "eff", "device",
        "triangle", "mesh", "lod", "billboardmergedmesh", "scenepropcollider", "blockout",
    ))
    font_blocks: set[str] = set()
    _GENERIC_NOUNS = frozenset((
        "prop", "wall", "floor", "decal", "object", "model", "ceiling", "ground",
        "building", "light", "mask", "shadow", "general", "paper", "crack",
        "small", "large", "door", "window", "metal", "stone", "wood", "glass",
    ))

    entry_count = 0
    bytes_read = 0
    next_report = 32 * 1024 * 1024
    cur_name = ""
    cur_container = ""
    cur_source = ""
    cur_type = ""
    cur_pathid = ""

    def _flush_entry():
        nonlocal entry_count
        if not cur_source and not cur_name:
            return
        entry_count += 1
        blk_name = os.path.basename(cur_source.replace("\\", "/"))
        if not blk_name:
            return
        if cur_type == "Font":
            font_blocks.add(blk_name)
        if cur_type in _ASSET_TYPES_INTEREST:
            hay = f"{cur_name} {cur_container}"
            for m in _AVATAR_RE.findall(hay):
                chars_auto.setdefault(m, set()).add(blk_name)
            if "_" in cur_name and not cur_name.startswith(("Avatar_", "Stage_", "Scene_", "Map_", "MainCity_", "Env_")):
                short_tok = cur_name.split("_", 1)[0]
                if len(short_tok) >= 3:
                    short_blocks.setdefault(short_tok, set()).add(blk_name)
            if cur_type in ("GameObject", "Mesh") and _STAGE_RE.match(cur_name):
                stages_auto.setdefault(cur_name, set()).add(blk_name)
            if cur_type in ("Material", "Mesh") and "_" in cur_name:
                m_pref = cur_name.split("_", 1)[0].lower()
                if len(m_pref) >= 3:
                    mm = mat_mesh_blocks.setdefault(m_pref, {})
                    mm[blk_name] = mm.get(blk_name, 0) + (3 if cur_type == "Material" else 1)
                    if cur_type == "Mesh" and m_pref not in _TECH_MESH_PREFS:
                        mp = mesh_pref_counts.setdefault(blk_name, {})
                        mp[m_pref] = mp.get(m_pref, 0) + 1
                elif cur_type == "Mesh" and _HLOD_MESH_RE.match(cur_name):
                    hlod_mesh_blks.add(blk_name)
            if cur_type == "Texture2D" and cur_pathid and cur_name:
                tex_pathid[cur_pathid] = [cur_name, blk_name]
            if cur_type == "Texture2D" and "_" in cur_name:
                parts = [p.lower() for p in cur_name.split("_") if p]
                t_pref = parts[0]
                if len(t_pref) >= 3:
                    bmap = tex_prefix_blocks.setdefault(t_pref, {})
                    bmap[blk_name] = bmap.get(blk_name, 0) + 1
                if len(parts) >= 2:
                    t_key = f"{parts[0]}_{parts[1]}"
                    kmap = tex_key_blocks.setdefault(t_key, {})
                    kmap[blk_name] = kmap.get(blk_name, 0) + 1
                for tok in parts[2:]:
                    if len(tok) >= 5 and not tok.isdigit() and tok not in _GENERIC_NOUNS:
                        nmap = tex_noun_blocks.setdefault(tok.rstrip("s"), {})
                        nmap[blk_name] = nmap.get(blk_name, 0) + 1

    def _extract_str_val(line: str, key: str) -> str | None:
        idx = line.find(key)
        if idx < 0:
            return None
        rest = line[idx + len(key):].lstrip()
        if not rest.startswith('"'):
            return None
        end = rest.rfind('"')
        if end <= 0:
            return None
        return rest[1:end]

    with map_json.open("r", encoding="utf-8", errors="ignore", buffering=1024 * 1024) as f:
        for line in f:
            bytes_read += len(line.encode("utf-8", errors="ignore"))
            if progress_cb is not None and bytes_read >= next_report:
                pct = 85 + min(7, int(7 * bytes_read / total_bytes))
                progress_cb(pct, f"Parsing AssetMap ({bytes_read // (1024 * 1024)}/{total_mb} MB)...")
                next_report += 32 * 1024 * 1024

            s = line.strip()
            if s.startswith('"Name":'):
                cur_name = _extract_str_val(s, '"Name":') or ""
            elif s.startswith('"Container":'):
                cur_container = _extract_str_val(s, '"Container":') or ""
            elif s.startswith('"Source":'):
                cur_source = _extract_str_val(s, '"Source":') or ""
            elif s.startswith('"Type":'):
                cur_type = _extract_str_val(s, '"Type":') or ""
            elif s.startswith('"PathID":'):
                cur_pathid = s.split(":", 1)[1].strip().rstrip(",")
            elif s in ("}", "},"):
                _flush_entry()
                cur_name = cur_container = cur_source = cur_type = cur_pathid = ""

    for ckey, blks in chars_auto.items():
        parts = ckey.split("_")
        if ckey.startswith("Avatar_") and len(parts) == 4:
            blks.update(short_blocks.get(parts[3], ()))

    if chars_auto:
        (map_dir / "character_index_auto.json").write_text(
            json.dumps({"characters": {k: {"blocks": sorted(v)} for k, v in sorted(chars_auto.items())}}, indent=2)
        )
    if stages_auto:
        (map_dir / "stage_index_auto.json").write_text(
            json.dumps({"stages": {k: {"blocks": sorted(v)} for k, v in sorted(stages_auto.items())}}, indent=2)
        )
    if font_blocks:
        (map_dir / "fonts_index_auto.json").write_text(
            json.dumps({"blocks": sorted(font_blocks)}, indent=2)
        )
    if tex_prefix_blocks:
        compact_tex = {
            k: [b for b, _c in sorted(v.items(), key=lambda it: -it[1])[:256]]
            for k, v in sorted(tex_prefix_blocks.items())
        }
        (map_dir / "stage_prefix_blocks_auto.json").write_text(json.dumps(compact_tex))
    if tex_key_blocks:
        compact_keys = {
            k: [b for b, _c in sorted(v.items(), key=lambda it: -it[1])]
            for k, v in sorted(tex_key_blocks.items())
        }
        (map_dir / "tex_key_blocks_auto.json").write_text(json.dumps(compact_keys))
    if tex_noun_blocks:
        compact_nouns = {
            k: [b for b, _c in sorted(v.items(), key=lambda it: -it[1])[:12]]
            for k, v in sorted(tex_noun_blocks.items())
        }
        (map_dir / "tex_noun_blocks_auto.json").write_text(json.dumps(compact_nouns))
    if tex_pathid:
        (map_dir / "tex_pathid_auto.json").write_text(json.dumps(tex_pathid))
    blk_owners: dict[str, list] = {}
    for blk_name, pcounts in mesh_pref_counts.items():
        if not pcounts:
            continue
        top_pref, top_cnt = max(pcounts.items(), key=lambda it: it[1])
        tot_cnt = sum(pcounts.values())
        blk_owners[blk_name] = [top_pref, top_cnt, tot_cnt]
    if blk_owners:
        (map_dir / "stage_blk_owners_auto.json").write_text(json.dumps(blk_owners))
    if mat_mesh_blocks:
        compact_mm = {}
        for k, v in sorted(mat_mesh_blocks.items()):
            keep = []
            for b, _c in sorted(v.items(), key=lambda it: -it[1]):
                if b not in mesh_pref_counts and b not in hlod_mesh_blks:
                    continue
                own = blk_owners.get(b)
                if own is not None:
                    opref, _ocnt, tot = own
                    mcnt = mesh_pref_counts.get(b, {}).get(k, 0)
                    if k != opref and mcnt < max(2, int(0.45 * tot)):
                        continue
                keep.append(b)
                if len(keep) >= 128:
                    break
            if keep:
                compact_mm[k] = keep
        (map_dir / "stage_mat_mesh_blocks_auto.json").write_text(json.dumps(compact_mm))
    _emit(
        log_cb,
        f"Parsed AnimeStudio AssetMap ({entry_count} entries): "
        f"{len(chars_auto)} characters, {len(stages_auto)} stages, {len(font_blocks)} font blocks",
    )
    return len(chars_auto), len(stages_auto), len(font_blocks)


def resolve_material_textures(dest_dir: Path) -> tuple[dict[str, str], list[str], list[str]]:
    """Resolve Material/*.json _MainTex/_BaseMap m_PathIDs via tex_pathid_auto.json."""
    mat_dir = dest_dir / "Material"
    if not mat_dir.is_dir():
        return {}, [], []
    map_dir = cfg.CACHE_DIR / "asset_map"
    pid_p = map_dir / "tex_pathid_auto.json"
    if not pid_p.is_file():
        am = map_dir / "zzz_assets_map.json"
        if am.is_file():
            try:
                _parse_cli_asset_map(am, map_dir)
            except Exception:
                pass
    if not pid_p.is_file():
        return {}, [], []
    try:
        pid_map = json.loads(pid_p.read_text())
    except Exception:
        return {}, [], []

    mat_to_tex: dict[str, str] = {}
    needed_blks: list[str] = []
    seen_blks: set[str] = set()
    needed_tex: list[str] = []
    seen_tex: set[str] = set()
    _TEX_SLOTS = ("_MainTex", "_BaseMap", "_BaseColorMap", "_Diffuse", "_Albedo", "_MainTex0", "_BaseColor", "_ColorMap")

    for mp in mat_dir.glob("*.json"):
        try:
            d = json.loads(mp.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        tex_envs = (d.get("m_SavedProperties") or {}).get("m_TexEnvs") or {}
        if not isinstance(tex_envs, dict):
            continue
        chosen_pid = None
        for slot in _TEX_SLOTS:
            tv = tex_envs.get(slot)
            if isinstance(tv, dict):
                mt = tv.get("m_Texture")
                if isinstance(mt, dict) and not mt.get("IsNull", False):
                    pid = mt.get("m_PathID")
                    if pid:
                        chosen_pid = str(pid)
                        break
        if not chosen_pid:
            continue
        hit = pid_map.get(chosen_pid)
        if hit and len(hit) == 2:
            tname, blk = hit[0], hit[1]
            mat_to_tex[mp.stem.lower()] = tname.lower()
            if blk and blk not in seen_blks:
                seen_blks.add(blk)
                needed_blks.append(blk)
            if tname and tname not in seen_tex:
                seen_tex.add(tname)
                needed_tex.append(tname)
    return mat_to_tex, needed_blks, needed_tex


def load_stage_texture_blocks(stage_name: str) -> tuple[list[str], str]:
    map_dir = cfg.CACHE_DIR / "asset_map"
    p = map_dir / "stage_prefix_blocks_auto.json"
    if not p.is_file():
        am = map_dir / "zzz_assets_map.json"
        if am.is_file():
            try:
                _parse_cli_asset_map(am, map_dir)
            except Exception:
                pass
    if not p.is_file():
        return [], ""
    try:
        data = json.loads(p.read_text())
    except Exception:
        return [], ""
    clean = re.sub(r"^(?:Stage|Env|Scene|Map)_", "", stage_name)
    toks = [t for t in clean.split("_") if len(t) >= 3 and not t.isdigit()]
    if not toks:
        return [], ""
    seen: list[str] = []
    for t in toks:
        for b in data.get(t.lower(), []):
            if b not in seen:
                seen.append(b)
    pat = f"^(?:{'|'.join(re.escape(t) for t in toks)})_"
    return seen, pat


def load_stage_texture_blocks_for_dir(stage_name: str, stage_dir: Path) -> tuple[list[str], str]:
    """Find shared texture .blks for every mesh prefix/category exported into stage_dir."""
    map_dir = cfg.CACHE_DIR / "asset_map"
    key_p = map_dir / "tex_key_blocks_auto.json"
    noun_p = map_dir / "tex_noun_blocks_auto.json"
    pref_p = map_dir / "stage_prefix_blocks_auto.json"
    if not key_p.is_file() or not noun_p.is_file():
        am = map_dir / "zzz_assets_map.json"
        if am.is_file():
            try:
                _parse_cli_asset_map(am, map_dir)
            except Exception:
                pass

    key_data: dict[str, list[str]] = {}
    noun_data: dict[str, list[str]] = {}
    pref_data: dict[str, list[str]] = {}
    for fp, dst in ((key_p, key_data), (noun_p, noun_data), (pref_p, pref_data)):
        if fp.is_file():
            try:
                dst.update(json.loads(fp.read_text()))
            except Exception:
                pass

    clean = re.sub(r"^(?:Stage|Env|Scene|Map)_", "", stage_name)
    stage_toks = [t for t in clean.split("_") if len(t) >= 3 and not t.isdigit()]
    mesh_prefixes: set[str] = set(stage_toks)
    mesh_keys: set[str] = set()
    mesh_nouns: set[str] = set()

    if stage_dir.is_dir():
        for ext in ("*.fbx", "*.obj"):
            for mp in stage_dir.rglob(ext):
                st = re.sub(r"\s*\([0-9]+\)$", "", mp.stem)
                if any(t in st.lower() for t in ("_col_", "_proxy")):
                    continue
                parts = [p for p in st.split("_") if p]
                if len(parts) >= 1 and len(parts[0]) >= 3 and not parts[0].isdigit():
                    mesh_prefixes.add(parts[0])
                if len(parts) >= 2:
                    mesh_keys.add(f"{parts[0].lower()}_{parts[1].lower()}")
                for tok in parts[2:]:
                    tl = tok.lower().rstrip("s")
                    if len(tl) >= 5 and not tl.isdigit() and tl in noun_data:
                        mesh_nouns.add(tl)

    # Round-robin across exact nouns FIRST, then mesh_keys, then stage_toks
    seen: list[str] = []
    seen_set: set[str] = set()
    buckets: list[list[str]] = (
        [noun_data.get(n, [])[:3] for n in sorted(mesh_nouns)]
        + [key_data.get(k, [])[:4] for k in sorted(mesh_keys)]
        + [pref_data.get(t.lower(), [])[:8] for t in stage_toks]
    )
    max_len = max((len(b) for b in buckets), default=0)
    for idx_b in range(max_len):
        for bucket in buckets:
            if idx_b < len(bucket):
                blk = bucket[idx_b]
                if blk not in seen_set:
                    seen_set.add(blk)
                    seen.append(blk)
                    if len(seen) >= 96:
                        break
        if len(seen) >= 96:
            break

    if not mesh_prefixes and not mesh_nouns:
        return [], ""
    pref_pat = f"(?:{'|'.join(re.escape(t) for t in sorted(mesh_prefixes))})_" if mesh_prefixes else ""
    if mesh_nouns:
        noun_pat = f"(?:{'|'.join(re.escape(n) for n in sorted(mesh_nouns))})"
        pat = f"^(?:{pref_pat})|(?:{noun_pat})" if pref_pat else noun_pat
    else:
        pat = f"^{pref_pat}"
    return seen, pat


def build_asset_map(
    game_root: Path,
    log_cb=None,
    progress_cb=None,
    use_cli_assetmap: bool = True,
    cancel_event=None,
) -> dict:
    files_root = cfg.resolve_files_root(game_root)
    map_dir = cfg.CACHE_DIR / "asset_map"
    map_dir.mkdir(parents=True, exist_ok=True)

    def _prog(pct: int, msg: str):
        if progress_cb is not None:
            progress_cb(pct, msg)
        _emit(log_cb, msg)

    _prog(5, f"Mapping assets for {files_root}...")

    lib_dir = cfg.EXTRACTOR_ROOT / "lib"
    ran_scripts = []
    for script_name in ("build_asset_map.py", "map_assets.py", "build_character_catalog.py", "build_viewer_data.py"):
        s = lib_dir / script_name
        if s.is_file():
            _prog(15, f"Running {script_name}...")
            _emit(log_cb, f"$ {sys.executable} {s}")
            proc = subprocess.Popen(
                [sys.executable, str(s)],
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
            ran_scripts.append({"script": script_name, "rc": proc.wait()})

    _prog(25, "Scanning .blk containers...")
    blk_files = sorted(files_root.rglob("*.blk")) if files_root.exists() else []
    blk_names = {p.name for p in blk_files}
    blk_stems = {p.stem for p in blk_files}

    _prog(30, "Scanning .pck audio archives...")
    pck_files = sorted(files_root.rglob("*.pck")) if files_root.exists() else []

    auto_chars = auto_stages = auto_fonts = 0
    blocks_dir = files_root / "Blocks"
    cli_map_file = map_dir / "zzz_assets_map.json"
    if (
        use_cli_assetmap
        and blocks_dir.is_dir()
        and blk_files
        and cfg.CLI_EXE.is_file()
        and shutil.which("wine") is not None
    ):
        if not cli_map_file.is_file():
            _prog(35, f"Building full AnimeStudio AssetMap across {len(blk_files)} .blk files...")
            env = os.environ.copy()
            env["WINEDEBUG"] = "-all"
            cmd = [
                "wine", str(cfg.CLI_EXE),
                "Z:" + str(blocks_dir).replace("/", "\\"),
                "Z:" + str(map_dir).replace("/", "\\"),
                "--game", "ZZZ",
                "--map_op", "AssetMap",
                "--map_type", "JSON",
                "--map_name", "zzz_assets_map",
            ]
            proc = subprocess.Popen(
                cmd, cwd=str(cfg.CLI_DIR), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                if cancel_event is not None and cancel_event.is_set():
                    proc.terminate()
                    _emit(log_cb, "Full AssetMap scan skipped by user; using quick index.")
                    break
                sline = line.rstrip("\n")
                m = _PROG_RE.search(sline)
                if m:
                    cur, tot = int(m.group(1)), max(1, int(m.group(2)))
                    pct = 35 + int(50 * cur / tot)
                    _prog(pct, f"AnimeStudio AssetMap [{cur}/{tot}]")
                elif sline.strip():
                    _emit(log_cb, sline)
            proc.wait()
        if cli_map_file.is_file():
            _prog(85, "Parsing AnimeStudio AssetMap JSON...")
            auto_chars, auto_stages, auto_fonts = _parse_cli_asset_map(
                cli_map_file, map_dir, log_cb=log_cb, progress_cb=progress_cb
            )
        cab_bin = cfg.CLI_DIR / "Maps" / "ZZZ.bin"
        if not cab_bin.is_file() and (cancel_event is None or not cancel_event.is_set()):
            _prog(87, f"Building AnimeStudio CABMap dependency index (ZZZ.bin) across {len(blk_files)} .blk files...")
            env = os.environ.copy()
            env["WINEDEBUG"] = "-all"
            cab_cmd = [
                "wine", str(cfg.CLI_EXE),
                "Z:" + str(blocks_dir).replace("/", "\\"),
                "Z:" + str(map_dir).replace("/", "\\"),
                "--game", "ZZZ",
                "--map_op", "CABMap,Load",
                "--map_name", "ZZZ",
            ]
            cab_proc = subprocess.Popen(
                cab_cmd, cwd=str(cfg.CLI_DIR), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            assert cab_proc.stdout is not None
            for line in cab_proc.stdout:
                if cancel_event is not None and cancel_event.is_set():
                    cab_proc.terminate()
                    _emit(log_cb, "CABMap build skipped by user.")
                    break
                sline = line.rstrip("\n")
                m = _PROG_RE.search(sline)
                if m:
                    cur, tot = int(m.group(1)), max(1, int(m.group(2)))
                    pct = 87 + int(5 * cur / tot)
                    _prog(pct, f"AnimeStudio CABMap [{cur}/{tot}]")
                elif sline.strip():
                    _emit(log_cb, sline)
            cab_proc.wait()

    _prog(92, "Indexing sections & cross-referencing containers...")
    sections = load_texture_sections()
    chars_fallback: dict[str, dict] = {}
    stages_fallback: dict[str, dict] = {}

    for sec, info in sections.items():
        blks = [b for b in info.get("blocks", []) if (b in blk_names or Path(b).stem in blk_stems)]
        if not blks and blk_names:
            continue
        use_blks = blks or info.get("blocks", [])
        if sec.startswith("Char/"):
            found_avatars = False
            for sample in info.get("samples", []):
                for part in sample.replace("\\", "/").split("/"):
                    if part.startswith("Avatar_") and "." not in part:
                        entry = chars_fallback.setdefault(part, {"blocks": []})
                        for b in use_blks:
                            if b not in entry["blocks"]:
                                entry["blocks"].append(b)
                        found_avatars = True
            if not found_avatars:
                cname = sec.split("/", 1)[1].split("/")[0]
                if cname and cname != "Avatar":
                    entry = chars_fallback.setdefault(cname, {"blocks": []})
                    for b in use_blks:
                        if b not in entry["blocks"]:
                            entry["blocks"].append(b)
        elif sec.startswith(("Scenes/", "Stage/", "StageLogic/", "NapStreaming/")):
            sname = sec.split("/", 1)[1].split("/")[0]
            if sname and "." not in sname and sname not in _NON_MAP_SECTIONS:
                entry = stages_fallback.setdefault(sname, {"blocks": []})
                for b in use_blks:
                    if b not in entry["blocks"]:
                        entry["blocks"].append(b)

    chars_out_dir = cfg.FINAL_ROOT / "Characters"
    if chars_out_dir.is_dir():
        for ch_dir in sorted(chars_out_dir.iterdir()):
            if ch_dir.is_dir():
                chars_fallback.setdefault(ch_dir.name, {"blocks": []})

    if not (cfg.EXTRACTOR_ROOT / "data" / "asset_map" / "character_index_lean.json").is_file():
        (map_dir / "character_index_fallback.json").write_text(
            json.dumps({"characters": chars_fallback}, indent=2)
        )
    if not (cfg.EXTRACTOR_ROOT / "data" / "all_maps_inventory.json").is_file():
        (map_dir / "stage_index_fallback.json").write_text(
            json.dumps({"stages": stages_fallback}, indent=2)
        )

    manifest = {
        "files_root": str(files_root),
        "blk_count": len(blk_files),
        "pck_count": len(pck_files),
        "auto_characters": auto_chars,
        "auto_stages": auto_stages,
        "auto_fonts": auto_fonts,
        "fallback_characters": len(chars_fallback),
        "fallback_stages": len(stages_fallback),
        "user_skipped": bool(cancel_event is not None and cancel_event.is_set()),
        "ran_scripts": ran_scripts,
        "built_at": int(time.time()),
    }
    (map_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    _prog(
        100,
        f"Asset map ready: {len(blk_files)} .blk, {len(pck_files)} .pck, "
        f"{auto_chars or len(chars_fallback)} characters, {auto_stages or len(stages_fallback)} stages",
    )
    return manifest
