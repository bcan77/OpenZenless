"""Asset indexing, character/stage/section loaders, and startup asset mapping."""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config as cfg

_WWISE_MAPPER_CACHE = None

_OZ_CHAR_BASE_RE = re.compile(
    r"^(Avatar_(?:Male|Female)_Size\d+_[A-Za-z0-9]+|Avatar_Bangboo_[A-Za-z0-9]+)$"
)
_OZ_ANI_CHAR_RE = re.compile(
    r"^(Avatar_(?:Male|Female)_Size\d+_[A-Za-z0-9]+|Avatar_Bangboo_[A-Za-z0-9]+)_Ani_"
)


def xxh64(data: bytes, seed: int = 0) -> int:
    """Pure-Python standard XXH64 (used by Unity/ZZZ for AssetBundle container hashes)."""
    P1 = 0x9E3779B185EBCA87
    P2 = 0xC2B2AE3D27D4EB4F
    P3 = 0x165667B19E3779F9
    P4 = 0x85EBCA77C2B2AE63
    P5 = 0x27D4EB2F165667C5
    M64 = 0xFFFFFFFFFFFFFFFF

    def rotl64(x: int, r: int) -> int:
        return ((x << r) | (x >> (64 - r))) & M64

    def _round(acc: int, lane: int) -> int:
        acc = (acc + lane * P2) & M64
        acc = rotl64(acc, 31)
        return (acc * P1) & M64

    def _merge(acc: int, val: int) -> int:
        acc ^= _round(0, val)
        return ((acc * P1) + P4) & M64

    n = len(data)
    off = 0
    if n >= 32:
        v1 = (seed + P1 + P2) & M64
        v2 = (seed + P2) & M64
        v3 = seed & M64
        v4 = (seed - P1) & M64
        limit = n - 32
        while off <= limit:
            k1, k2, k3, k4 = struct.unpack_from("<4Q", data, off)
            v1 = _round(v1, k1)
            v2 = _round(v2, k2)
            v3 = _round(v3, k3)
            v4 = _round(v4, k4)
            off += 32
        h = (rotl64(v1, 1) + rotl64(v2, 7) + rotl64(v3, 12) + rotl64(v4, 18)) & M64
        h = _merge(h, v1)
        h = _merge(h, v2)
        h = _merge(h, v3)
        h = _merge(h, v4)
    else:
        h = (seed + P5) & M64

    h = (h + n) & M64
    while off + 8 <= n:
        lane = struct.unpack_from("<Q", data, off)[0]
        h ^= _round(0, lane)
        h = (rotl64(h, 27) * P1 + P4) & M64
        off += 8
    while off + 4 <= n:
        lane = struct.unpack_from("<I", data, off)[0]
        h ^= (lane * P1) & M64
        h = (rotl64(h, 23) * P2 + P3) & M64
        off += 4
    while off < n:
        h ^= (data[off] * P5) & M64
        h = (rotl64(h, 11) * P1) & M64
        off += 1

    h ^= h >> 33
    h = (h * P2) & M64
    h ^= h >> 29
    h = (h * P3) & M64
    h ^= h >> 32
    return h


def load_oz_map_index() -> dict[str, dict]:
    p = cfg.CACHE_DIR / "asset_map" / "oz_maps_index.json"
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            maps = data.get("maps")
            if isinstance(maps, dict):
                return maps
        except Exception:
            pass
    return {}


def load_oz_character_index() -> dict[str, dict]:
    p = cfg.CACHE_DIR / "asset_map" / "oz_characters_index.json"
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            chars = data.get("characters")
            if isinstance(chars, dict):
                return chars
        except Exception:
            pass
    return {}


def _emit(log_cb, msg: str):
    if log_cb is not None:
        log_cb(msg)
    else:
        print(msg, flush=True)


def load_character_index() -> dict[str, list[str]]:
    oz_chars = load_oz_character_index()
    if oz_chars:
        out: dict[str, list[str]] = {}
        for k, v in oz_chars.items():
            if not isinstance(v, dict):
                continue
            blks: list[str] = []
            for b in [v.get("prefab_blk", ""), *v.get("mesh_blks", ())]:
                if b and b not in blks:
                    blks.append(b)
            out[k] = blks
        return out
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
    oz_maps = load_oz_map_index()
    if oz_maps:
        out: dict[str, list[str]] = {}
        for k, v in oz_maps.items():
            if not isinstance(v, dict):
                continue
            blks: list[str] = []
            st = v.get("streaming") or {}
            for b in st.get("blks", ()):
                if b and b not in blks:
                    blks.append(b)
            bs = v.get("base_scene") or {}
            if bs.get("blk") and bs["blk"] not in blks:
                blks.append(bs["blk"])
            out[k] = blks
        return out
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
                max_add = 12 if nap_set else 14
                added = 0
                for b in add_list:
                    if b not in blks:
                        blks.append(b)
                        added += 1
                        if added >= max_add:
                            break
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
                if (cfg.CACHE_DIR / "asset_map" / "oz_maps_index.json").is_file() and (cfg.CACHE_DIR / "asset_map" / "oz_characters_index.json").is_file() and data.get("oz_index_version", 0) >= 2:
                    return True
                if data.get("user_skipped"):
                    return True
                if not cfg.CLI_EXE.is_file():
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


def _write_csharp_str(f, s: str):
    """Write a .NET BinaryWriter 7-bit length-prefixed UTF-8 string."""
    b = s.encode("utf-8")
    n = len(b)
    while n >= 0x80:
        f.write(bytes(((n & 0x7F) | 0x80,)))
        n >>= 7
    f.write(bytes((n,)))
    if b:
        f.write(b)


def _write_cab_map_bin(
    cab_entries: dict[str, tuple[tuple[int, float], str, str, int, list[str]]],
    base_folder_win: str,
    out_bin: Path,
):
    """Write AnimeStudio AssetsHelper CABMap binary (Maps/ZZZ.bin) directly from indexed CAB rows."""
    out_bin.parent.mkdir(parents=True, exist_ok=True)
    tmp_bin = out_bin.with_suffix(".bin.tmp")
    ordered = sorted(cab_entries.values(), key=lambda x: x[1].lower())
    with tmp_bin.open("wb") as f:
        _write_csharp_str(f, base_folder_win)
        f.write(struct.pack("<i", len(ordered)))
        for _, cab_name, blk_rel, offset, deps in ordered:
            _write_csharp_str(f, cab_name)
            _write_csharp_str(f, blk_rel.replace("/", "\\"))
            f.write(struct.pack("<q", int(offset)))
            _write_csharp_str_deps = deps or ()
            f.write(struct.pack("<i", len(_write_csharp_str_deps)))
            for dep in _write_csharp_str_deps:
                _write_csharp_str(f, dep)
    os.replace(tmp_bin, out_bin)


def _build_oz_indexes_from_jsonl(
    jsonl_paths: Path | list[Path],
    map_dir: Path,
    game_root: Path,
    common_base: Path | None = None,
    write_cab_map: bool = True,
    total_blk_count: int | None = None,
    log_cb=None,
) -> tuple[int, int, int]:
    """Parse lightweight AnimeStudio AS_DUMP_OZ_INDEX JSONL shard(s) -> oz_maps_index, oz_characters_index, Maps/ZZZ.bin."""
    paths = [jsonl_paths] if isinstance(jsonl_paths, Path) else list(jsonl_paths)
    files_root = cfg.resolve_files_root(game_root)
    order_map = cfg.load_res_version_order(files_root)
    prio_cache: dict[str, tuple[int, float]] = {}

    def _blk_prio(blk_rel: str) -> tuple[int, float]:
        cached = prio_cache.get(blk_rel)
        if cached is not None:
            return cached
        norm = blk_rel.replace("\\", "/")
        rel_in_blocks = norm.split("Blocks/", 1)[-1] if "Blocks/" in norm else norm
        base = Path(rel_in_blocks).name
        p = order_map.get(rel_in_blocks, order_map.get(base, 999999))
        mt = 0.0
        resolved = cfg.resolve_blk_path(game_root, blk_rel)
        if resolved is not None:
            try:
                mt = resolved.stat().st_mtime
            except OSError:
                pass
        val = (p, -mt)
        prio_cache[blk_rel] = val
        return val

    cab_entries: dict[str, tuple[tuple[int, float], str, str, int, list[str]]] = {}
    ab_by_key: dict[str, tuple[tuple[int, float], str, str, str, str]] = {}
    prefabs_by_name: dict[str, tuple[tuple[int, float], str, str, str]] = {}
    sep_mesh_by_name: dict[str, tuple[tuple[int, float], str, str, str]] = {}
    char_anis: dict[str, dict[str, tuple[tuple[int, float], str, str]]] = {}
    scenes: dict[str, tuple[tuple[int, float], str, str]] = {}
    streams: dict[str, tuple[tuple[int, int, int, float], str, str, str, str, list[str]]] = {}
    lods_by_cab: dict[tuple[str, str], list[tuple[str, str]]] = {}
    font_blks_set: set[str] = set()

    for jp in paths:
        if not jp.is_file():
            continue
        with jp.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                k = row.get("k")
                blk = row.get("blk", "")
                if not blk:
                    continue
                bp = _blk_prio(blk)

                if k == "cab":
                    cab = str(row.get("cab") or "")
                    if cab:
                        cl = cab.lower()
                        prev_c = cab_entries.get(cl)
                        if prev_c is None or bp < prev_c[0]:
                            deps = [str(d) for d in (row.get("deps") or []) if d]
                            cab_entries[cl] = (bp, cab, blk, int(row.get("off") or 0), deps)

                elif k == "ab":
                    key = str(row.get("key") or "")
                    cab = str(row.get("cab") or "")
                    atype = str(row.get("type") or "")
                    aname = str(row.get("name") or "")
                    if key:
                        prev = ab_by_key.get(key)
                        if prev is None or bp < prev[0]:
                            ab_by_key[key] = (bp, blk, cab, atype, aname)
                    if atype == "GameObject" and aname:
                        if _OZ_CHAR_BASE_RE.match(aname) or (aname.endswith("_Model") and _OZ_CHAR_BASE_RE.match(aname[:-6])):
                            prev_p = prefabs_by_name.get(aname)
                            if prev_p is None or bp < prev_p[0]:
                                prefabs_by_name[aname] = (bp, blk, cab, key)
                    if atype == "Mesh" and aname.startswith("SeparateMesh_"):
                        prev_m = sep_mesh_by_name.get(aname)
                        if prev_m is None or bp < prev_m[0]:
                            sep_mesh_by_name[aname] = (bp, blk, cab, key)
                    if aname and ("_Ani_" in aname):
                        am = _OZ_ANI_CHAR_RE.match(aname)
                        if am and key:
                            cbase = am.group(1)
                            cmap = char_anis.setdefault(cbase, {})
                            prev_a = cmap.get(aname)
                            if prev_a is None or bp < prev_a[0]:
                                cmap[aname] = (bp, blk, key)

                elif k == "scene":
                    cab = str(row.get("cab") or "")
                    if cab.lower().startswith("buildplayer-") and not cab.lower().endswith(".sharedassets"):
                        sname = cab[len("BuildPlayer-"):]
                        prev_s = scenes.get(sname)
                        if prev_s is None or bp < prev_s[0]:
                            scenes[sname] = (bp, blk, cab)

                elif k == "stream":
                    mname = str(row.get("map") or "").strip()
                    if not mname:
                        continue
                    sectors = [str(s) for s in (row.get("sectors") or []) if s]
                    prefab_dir = str(row.get("prefab_dir") or f"Assets/NapResources/NapStreaming/{mname}/Prefab").rstrip("/")
                    size = int(row.get("size") or 0)
                    cab = str(row.get("cab") or "")
                    key = str(row.get("key") or "")
                    sp = (bp[0], -len(sectors), -size, bp[1])
                    prev_st = streams.get(mname)
                    if prev_st is None or sp < prev_st[0]:
                        streams[mname] = (sp, blk, cab, key, prefab_dir, sectors)

                elif k == "lod":
                    cab = str(row.get("cab") or "")
                    go_name = str(row.get("go") or "")
                    mesh_path = str(row.get("mesh_path") or "")
                    if cab and mesh_path:
                        lods_by_cab.setdefault((blk, cab), []).append((go_name, mesh_path))

                elif k == "font":
                    font_blks_set.add(blk)

    scenes_ci = {s.lower(): s for s in scenes}
    paired_scenes: set[str] = set()
    maps_dict: dict[str, dict] = {}

    for mname in sorted(streams):
        _, m_blk, m_cab, _, prefab_dir, sectors = streams[mname]
        stream_blks: list[str] = []
        stream_blk_set: set[str] = set()
        hashes: list[str] = []
        non_hlod_total = 0
        for sec in sectors:
            if sec.endswith("_HLOD"):
                continue
            non_hlod_total += 1
            path_lower = f"{prefab_dir}/{sec}.prefab".lower().encode("utf-8")
            h = str(xxh64(path_lower, 0))
            hit = ab_by_key.get(h)
            if hit is not None:
                hashes.append(h)
                p_blk = hit[1]
                if p_blk not in stream_blk_set:
                    stream_blk_set.add(p_blk)
                    stream_blks.append(p_blk)
        entry: dict = {
            "streaming": {
                "manifest_blk": m_blk,
                "manifest_cab": m_cab,
                "blks": stream_blks,
                "hashes": hashes,
                "sector_count": non_hlod_total,
                "resolved_count": len(hashes),
            }
        }
        sc_key = mname if mname in scenes else scenes_ci.get(mname.lower())
        if sc_key is not None:
            _, s_blk, s_cab = scenes[sc_key]
            entry["base_scene"] = {"blk": s_blk, "cab": s_cab}
            paired_scenes.add(sc_key)
        maps_dict[mname] = entry

    maps_ci = {m.lower(): m for m in maps_dict}
    for sname in sorted(scenes):
        if sname in paired_scenes:
            continue
        _, s_blk, s_cab = scenes[sname]
        mk = maps_ci.get(sname.lower())
        if mk is not None and "base_scene" not in maps_dict[mk]:
            maps_dict[mk]["base_scene"] = {"blk": s_blk, "cab": s_cab}
        elif mk is None:
            maps_dict[sname] = {"base_scene": {"blk": s_blk, "cab": s_cab}}

    char_cands: dict[str, dict[str, tuple[tuple[int, float], str, str, str, str]]] = {}
    for pname, pinfo in prefabs_by_name.items():
        if pname.endswith("_Model"):
            cname = pname[:-6]
            char_cands.setdefault(cname, {})["model"] = (*pinfo, pname)
        else:
            cname = pname
            char_cands.setdefault(cname, {})["base"] = (*pinfo, pname)

    chars_dict: dict[str, dict] = {}
    for cname in sorted(char_cands):
        cands = char_cands[cname]
        base = cands.get("base")
        model = cands.get("model")
        base_lods = lods_by_cab.get((base[1], base[2]), []) if base else []
        model_lods = lods_by_cab.get((model[1], model[2]), []) if model else []

        if base and (base_lods or not model):
            chosen = base
            chosen_lods = base_lods
        else:
            chosen = model
            chosen_lods = model_lods
        if not chosen:
            continue

        _, p_blk, p_cab, p_key, pname = chosen
        extra_cabs: list[str] = []
        extra_cab_set: set[str] = set()
        mesh_blks: list[str] = []
        mesh_blk_set: set[str] = set()

        for go_name, mesh_path in chosen_lods:
            gl = go_name.lower()
            ml = mesh_path.lower()
            if "hairshadow" in gl or "hairshadow" in ml or gl.endswith("_lod3") or "_lod3.mesh" in ml:
                continue
            mh = str(xxh64(ml.encode("utf-8"), 0))
            hit = ab_by_key.get(mh)
            if hit is None:
                stem = Path(mesh_path).stem
                hit_m = sep_mesh_by_name.get(stem)
                if hit_m is not None:
                    hit = (hit_m[0], hit_m[1], hit_m[2], "Mesh", stem)
            if hit is not None:
                _, m_blk, m_cab, _, _ = hit
                if m_cab and m_cab not in extra_cab_set and m_cab != p_cab:
                    extra_cab_set.add(m_cab)
                    extra_cabs.append(m_cab)
                if m_blk and m_blk not in mesh_blk_set:
                    mesh_blk_set.add(m_blk)
                    mesh_blks.append(m_blk)

        if not extra_cabs:
            # Fallback for *_Model prefabs without NapLodController rows
            prefixes = (f"SeparateMesh_{cname}_Model_", f"SeparateMesh_{cname}_")
            for mname, (_, m_blk, m_cab, _) in sorted(sep_mesh_by_name.items()):
                if not any(mname.startswith(pf) for pf in prefixes):
                    continue
                low = mname.lower()
                if low.endswith(("_lod1", "_lod2", "_lod3")) or "hairshadow" in low:
                    continue
                if m_cab and m_cab not in extra_cab_set and m_cab != p_cab:
                    extra_cab_set.add(m_cab)
                    extra_cabs.append(m_cab)
                if m_blk and m_blk not in mesh_blk_set:
                    mesh_blk_set.add(m_blk)
                    mesh_blks.append(m_blk)

        ani_map = char_anis.get(cname, {})
        anim_blks: list[str] = []
        anim_blk_set: set[str] = set()
        anim_keys: list[str] = []
        anim_key_set: set[str] = set()
        for _, (_, a_blk, a_key) in sorted(ani_map.items()):
            if a_blk and a_blk not in anim_blk_set:
                anim_blk_set.add(a_blk)
                anim_blks.append(a_blk)
            if a_key and a_key not in anim_key_set:
                anim_key_set.add(a_key)
                anim_keys.append(a_key)

        chars_dict[cname] = {
            "prefab_blk": p_blk,
            "prefab_cab": p_cab,
            "prefab_key": p_key,
            "prefab_name": pname,
            "extra_cabs": extra_cabs,
            "mesh_blks": mesh_blks,
            "anim_blks": anim_blks,
            "anim_keys": anim_keys,
        }

    (map_dir / "oz_maps_index.json").write_text(
        json.dumps({"version": 2, "maps": maps_dict}, indent=2),
        encoding="utf-8",
    )
    (map_dir / "oz_characters_index.json").write_text(
        json.dumps({"version": 2, "characters": chars_dict}, indent=2),
        encoding="utf-8",
    )

    compat_chars = {}
    for cname, info in chars_dict.items():
        blks: list[str] = []
        for b in [info["prefab_blk"], *info["mesh_blks"], *info["anim_blks"]]:
            if b and b not in blks:
                blks.append(b)
        compat_chars[cname] = {"blocks": blks}
    (map_dir / "character_index_auto.json").write_text(
        json.dumps({"characters": compat_chars}, indent=2),
        encoding="utf-8",
    )

    compat_stages = {}
    for mname, info in maps_dict.items():
        blks = []
        for b in (info.get("streaming") or {}).get("blks", ()):
            if b and b not in blks:
                blks.append(b)
        bs_blk = (info.get("base_scene") or {}).get("blk")
        if bs_blk and bs_blk not in blks:
            blks.append(bs_blk)
        compat_stages[mname] = {"blocks": blks}
    (map_dir / "stage_index_auto.json").write_text(
        json.dumps({"stages": compat_stages}, indent=2),
        encoding="utf-8",
    )

    sorted_fonts = sorted(font_blks_set, key=lambda b: (_blk_prio(b), b))
    (map_dir / "fonts_index_auto.json").write_text(
        json.dumps({"blocks": sorted_fonts}, indent=2),
        encoding="utf-8",
    )

    if write_cab_map and cab_entries and common_base is not None:
        cab_bin = cfg.CLI_DIR / "Maps" / "ZZZ.bin"
        cab_meta = cfg.CLI_DIR / "Maps" / "ZZZ.ozmeta.json"
        base_win = ("Z:" + str(common_base.resolve())).replace("/", "\\")
        _write_cab_map_bin(cab_entries, base_win, cab_bin)
        try:
            cab_meta.write_text(
                json.dumps({
                    "version": 2,
                    "files_root": str(files_root),
                    "blk_count": total_blk_count if total_blk_count is not None else len(prio_cache),
                    "cab_count": len(cab_entries),
                }),
                encoding="utf-8",
            )
        except Exception:
            pass
        _emit(log_cb, f"Wrote CABMap {cab_bin.name} ({len(cab_entries)} CAB entries)")

    _emit(
        log_cb,
        f"Built OZ v2 indexes: {len(chars_dict)} characters, "
        f"{len(maps_dict)} maps ({sum(1 for v in maps_dict.values() if 'streaming' in v)} streaming), "
        f"{len(sorted_fonts)} font blocks",
    )
    return len(chars_dict), len(maps_dict), len(sorted_fonts)


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

    _prog(25, "Scanning .blk containers (res_version_persist + APK priority)...")
    common_base, blk_files = cfg.collect_ordered_blks(game_root)
    if not blk_files and files_root.exists():
        blk_files = sorted(files_root.rglob("*.blk"))
        common_base = files_root
    blk_names = {p.name for p in blk_files}
    blk_stems = {p.stem for p in blk_files}

    _prog(30, "Scanning .pck audio archives...")
    pck_files = sorted(files_root.rglob("*.pck")) if files_root.exists() else []

    auto_chars = auto_stages = auto_fonts = 0
    oz_maps_file = map_dir / "oz_maps_index.json"
    oz_chars_file = map_dir / "oz_characters_index.json"
    cab_bin = cfg.CLI_DIR / "Maps" / "ZZZ.bin"
    cab_meta = cfg.CLI_DIR / "Maps" / "ZZZ.ozmeta.json"
    need_cab = not cab_bin.is_file()
    if not need_cab:
        try:
            cm = json.loads(cab_meta.read_text()) if cab_meta.is_file() else {}
            if cm.get("version", 0) < 2 or cm.get("files_root") != str(files_root) or cm.get("blk_count") != len(blk_files):
                need_cab = True
        except Exception:
            need_cab = True

    if (
        use_cli_assetmap
        and blk_files
        and cfg.CLI_EXE.is_file()
        and shutil.which("wine") is not None
        and (not oz_maps_file.is_file() or not oz_chars_file.is_file() or need_cab)
    ):
        workers = min(6, max(1, (os.cpu_count() or 4) - 2), len(blk_files))
        _prog(35, f"Indexing {len(blk_files)} .blk files across {workers} AnimeStudio worker(s)...")
        shard_lists: list[Path] = []
        shard_jsonls: list[Path] = []
        for wi in range(workers):
            sblks = blk_files[wi::workers]
            sl = map_dir / f"_oz_shard_{wi}.blklist"
            sj = map_dir / f"_oz_shard_{wi}.jsonl"
            sl.write_text(
                "\n".join(("Z:" + str(p.resolve())).replace("/", "\\") for p in sblks) + "\n",
                encoding="utf-8",
            )
            shard_lists.append(sl)
            shard_jsonls.append(sj)

        done_counts = [0] * workers
        done_lock = threading.Lock()
        oz_re = re.compile(r"\[OzIndex\]\s+(\d+)/(\d+)")
        procs: list[subprocess.Popen] = []

        def _run_shard(wi: int):
            env = os.environ.copy()
            env["WINEDEBUG"] = "-all"
            env["AS_DUMP_OZ_INDEX"] = ("Z:" + str(shard_jsonls[wi].resolve())).replace("/", "\\")
            env["AS_OZ_INDEX_BASE_DIR"] = ("Z:" + str(common_base.resolve())).replace("/", "\\")
            cmd = [
                "wine", str(cfg.CLI_EXE),
                ("Z:" + str(shard_lists[wi].resolve())).replace("/", "\\"),
                ("Z:" + str(map_dir.resolve())).replace("/", "\\"),
                "--game", "ZZZ",
            ]
            proc = subprocess.Popen(
                cmd, cwd=str(cfg.CLI_DIR), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            with done_lock:
                procs.append(proc)
            assert proc.stdout is not None
            for line in proc.stdout:
                if cancel_event is not None and cancel_event.is_set():
                    with done_lock:
                        for p in procs:
                            try:
                                p.terminate()
                            except Exception:
                                pass
                    break
                sline = line.rstrip("\n")
                m = oz_re.search(sline)
                if m:
                    cur = int(m.group(1))
                    with done_lock:
                        done_counts[wi] = cur
                        tot_done = sum(done_counts)
                    pct = 35 + int(50 * tot_done / max(1, len(blk_files)))
                    _prog(pct, f"AnimeStudio Index [{tot_done}/{len(blk_files)}]")
                elif "[Error]" in sline or "[Warning]" in sline:
                    _emit(log_cb, sline)
            proc.wait()

        threads = [threading.Thread(target=_run_shard, args=(wi,), daemon=True) for wi in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            if cancel_event is None or not cancel_event.is_set():
                _prog(86, "Building oz_maps_index, oz_characters_index & Maps/ZZZ.bin...")
                auto_chars, auto_stages, auto_fonts = _build_oz_indexes_from_jsonl(
                    shard_jsonls, map_dir, game_root, common_base=common_base, write_cab_map=True, total_blk_count=len(blk_files), log_cb=log_cb
                )
        finally:
            for sl in shard_lists:
                sl.unlink(missing_ok=True)
            for sj in shard_jsonls:
                sj.unlink(missing_ok=True)

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
        "oz_index_version": 2,
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
