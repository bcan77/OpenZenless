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

from . import catalog
from . import config as cfg

_WWISE_MAPPER_CACHE = None

_OZ_CHAR_BASE_RE = re.compile(
    r"^(Avatar_(?:Male|Female)_Size\d+_[A-Za-z0-9]+|Avatar_Bangboo_[A-Za-z0-9]+)$"
)
_OZ_CHAR_US_MODEL_RE = re.compile(
    r"^(Avatar_(?:Male|Female)_Size\d+_[A-Za-z0-9]+)_([A-Za-z0-9]+)(?:_([A-Za-z0-9]+))?_Model$"
)
_OZ_CHAR_COMP_BARE_RE = re.compile(
    r"^(Avatar_(?:Male|Female)_Size\d+_[A-Za-z0-9]+)_([A-Za-z0-9]+)(?:_([A-Za-z0-9]+))?$"
)
_OZ_BANGBOO_BASE_RE = re.compile(
    r"^((?:Avatar_)?Bangboo_[A-Z][A-Za-z0-9]*)$"
)
_OZ_IGNORE_MODEL_SUFFIXES = frozenset({
    "ui", "npc", "maincity", "lod1", "lod2", "lod3", "transform",
    "unitcompmanager", "chatplus", "chatplustest", "mindmaze", "musicactivity",
    "quest", "ld", "show", "story", "activity", "camera", "default", "fx",
    "fighting", "fishing", "surfing", "sensorypose", "timeline", "sneak",
    "aircombat", "tranfer", "airobot", "acc", "drone", "horse", "armored", "funnel",
})
_OZ_COMPANION_WEAPON_TOKENS = frozenset({
    "weapon", "anastacia", "drusilla", "wood", "kuriaki", "kuriakiweapon",
    "cat", "pot", "ghost", "case", "motor", "maozi", "yuansymedium",
    "disc", "cockpit", "floater", "sr", "summonedruthlessfiend",
    "windeye", "windregion", "hand", "leg",
})
_OZ_ANI_CHAR_RE = re.compile(
    r"^(Avatar_(?:Male|Female)_Size\d+_[A-Za-z0-9]+(?:_[A-Za-z0-9]+){0,2}|(?:Avatar_)?Bangboo_[A-Za-z0-9]+)_Ani_"
)
_OZ_ENT_ANI_RE = re.compile(r"^((?:Monster|NPC)_[A-Za-z0-9_]+?)_Ani_")

_UI_SPRITE_BUCKETS: tuple[tuple[str, str, str], ...] = (
    ("portraits", "Dialogue Portraits & Character Art", r"^(?:Gal[Gg]ame_|Role|Avatar|IconRole)"),
    ("mindscape", "Mindscape Cinema Art", r"^Mindscape"),
    ("wengines", "W-Engine Icons", r"^Weapon_"),
    ("monsters", "Monster & Boss Icons", r"^(?:Monster_|IconMonster_)"),
    ("loading", "Loading Screens", r"^Loading_"),
    ("buffs_items", "Buff & Item Icons", r"^(?:IconBuff_|[Ii]tem_)"),
    ("stages_ui", "Stage, Collection & UI Icons", r"^(?:Stage_|Collection_|Journey_|SideBar_|Tutorial_|Zenkov_)"),
    (
        "other_sprites",
        "Other UI Sprites",
        r"^(?!(?:Gal[Gg]ame_|Role|Avatar|IconRole|Mindscape|Weapon_|Monster_|IconMonster_|Loading_|IconBuff_|[Ii]tem_|Stage_|Collection_|Journey_|SideBar_|Tutorial_|Zenkov_))",
    ),
)
_UI_SPRITE_BUCKET_RES = tuple(
    (k, lbl, pat, re.compile(pat) if k != "other_sprites" else None)
    for k, lbl, pat in _UI_SPRITE_BUCKETS
)

_ENTITY_NOISE_SUFFIXES = (
    "_lod1", "_lod2", "_lod3", "_hairshadow", "_col", "_collider", "_proxy", "_shadow", "_fx",
)
_PROP_MODEL_PREFIXES = (
    "Item_", "Device_", "Destruction_", "Pinball_", "PinballBall_",
    "Gacha_Prop_", "ScaryNight_", "Remielle_FaceMask", "Laboratory_",
    "Cs_Item_", "Interface_Prop_", "NPCShare_Prop_", "Follower_", "Summon_",
)
_PROP_BARE_RE = re.compile(
    r"^(?:Item|Device|Destruction|ScaryNight|BangbooPark_Prop|Common_Prop|SkyScraper_Prop|Drop_Item|MainCity_Prop|InLevelItem_Prop)_[A-Za-z0-9]+(?:_[A-Za-z0-9]+){0,2}$"
)
_SEP_DEV_RE = re.compile(
    r"^SeparateMesh_((?:Device|Destruction)_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)?)(?:_\d+)?_"
)


def _is_companion_token(seg: str) -> bool:
    low = seg.lower()
    if low in _OZ_COMPANION_WEAPON_TOKENS:
        return True
    m = re.match(r"^[A-Za-z][a-z]*", seg)
    return bool(m and m.group(0).lower() in ("wood", "kuriaki"))


def _resolve_char_model_key(aname: str) -> tuple[str, str] | None:
    """Resolve an Avatar/Bangboo/companion GameObject/Animator name -> (canonical_id, slot)."""
    if not aname or "(" in aname:
        return None
    if _OZ_CHAR_BASE_RE.match(aname):
        return aname, "base"
    if _OZ_BANGBOO_BASE_RE.match(aname):
        tok = aname.split("_", 1)[1].lower()
        if tok not in _OZ_IGNORE_MODEL_SUFFIXES:
            return aname, "base"
        return None
    if aname.endswith("_Model"):
        stem_m = aname[:-6]
        if _OZ_CHAR_BASE_RE.match(stem_m):
            return stem_m, "model"
        if _OZ_BANGBOO_BASE_RE.match(stem_m):
            tok = stem_m.split("_", 1)[1].lower()
            if tok not in _OZ_IGNORE_MODEL_SUFFIXES:
                return stem_m, "model"
            return None
        um = _OZ_CHAR_US_MODEL_RE.match(aname)
        if um:
            segs = [s for s in (um.group(2), um.group(3)) if s]
            if any(s.isdigit() or s.lower() in _OZ_IGNORE_MODEL_SUFFIXES for s in segs):
                return None
            if any(_is_companion_token(s) for s in segs):
                return f"{um.group(1)}_{'_'.join(segs)}", "model"
            if len(segs) == 1:
                return f"{um.group(1)}{segs[0]}", "model"
        return None
    um2 = _OZ_CHAR_COMP_BARE_RE.match(aname)
    if um2:
        segs = [s for s in (um2.group(2), um2.group(3)) if s]
        if any(s.isdigit() or s.lower() in _OZ_IGNORE_MODEL_SUFFIXES for s in segs):
            return None
        if any(_is_companion_token(s) for s in segs):
            norm_segs: list[str] = []
            for s in segs:
                m_wk = re.match(r"^(Wood|Kuriaki)([A-Z][A-Za-z0-9]*)$", s)
                if m_wk:
                    norm_segs.extend((m_wk.group(1), m_wk.group(2)))
                else:
                    norm_segs.append(s)
            return f"{um2.group(1)}_{'_'.join(norm_segs)}", "base"
    return None


def _resolve_char_anim_target(cbase: str) -> tuple[str, bool] | None:
    """Resolve an AnimationClip root prefix -> (canonical_char_id, is_default_fallback)."""
    if _OZ_BANGBOO_BASE_RE.match(cbase):
        tok = cbase.split("_", 1)[1].lower()
        return (cbase, False) if tok not in _OZ_IGNORE_MODEL_SUFFIXES else None
    m = re.match(r"^(Avatar_(?:Male|Female)_Size\d+_)([A-Za-z0-9]+)(?:_([A-Za-z0-9_]+))?$", cbase)
    if not m:
        return None
    prefix, head, tail = m.group(1), m.group(2), m.group(3) or ""
    if head == "Seed2":
        head = "Seed"
    parts = [p for p in tail.split("_") if p]
    is_default = False
    if parts and parts[-1].lower() == "default":
        is_default = True
        parts.pop()
    elif parts and parts[-1].lower() in ("face", "electirc", "electric"):
        parts.pop()
    if any(p.isdigit() or p.lower() in _OZ_IGNORE_MODEL_SUFFIXES for p in parts):
        return None
    if not parts:
        return f"{prefix}{head}", is_default
    if any(_is_companion_token(p) for p in parts):
        return f"{prefix}{head}_{'_'.join(parts)}", is_default
    if len(parts) == 1:
        return f"{prefix}{head}{parts[0]}", is_default
    return f"{prefix}{head}_{'_'.join(parts)}", is_default


def _classify_entity_gameobject(aname: str) -> tuple[str, str, str, str] | None:
    """Classify non-Avatar 3D objects -> (category, subtype, canonical_id, slot)."""
    if not aname or "(" in aname:
        return None
    is_model = aname.endswith("_Model")
    stem = aname[:-6] if is_model else aname
    if stem.lower().endswith(_ENTITY_NOISE_SUFFIXES) or re.search(r"\(\d+\)$", stem):
        return None
    if stem.startswith("Monster_"):
        if is_model or re.match(r"^Monster_(?:Boss_)?[A-Za-z0-9]+$", stem):
            sub = "boss" if stem.startswith("Monster_Boss_") else "enemy"
            return "monsters", sub, stem, ("model" if is_model else "base")
        return None
    if stem.startswith("NPC_"):
        if is_model or re.match(r"^NPC_[A-Za-z0-9]+$", stem):
            sub = "numeric" if stem[4:5].isdigit() else "named"
            return "npcs", sub, stem, ("model" if is_model else "base")
        return None
    if stem.startswith("Weapon_"):
        if is_model or re.match(r"^Weapon_[A-Z]_[A-Za-z0-9_]+$", stem):
            return "weapons", "wengine", stem, ("model" if is_model else "base")
        return None
    if is_model and stem.startswith(("Belle_", "Wise_")):
        return "weapons", "accessory", stem, "model"
    if is_model and stem.startswith(_PROP_MODEL_PREFIXES):
        sub = (
            "item" if stem.startswith("Item_")
            else "device" if stem.startswith("Device_")
            else "destruction" if stem.startswith("Destruction_")
            else "prop"
        )
        return "props", sub, stem, "model"
    if not is_model and _PROP_BARE_RE.match(stem):
        sub = (
            "item" if stem.startswith("Item_")
            else "device" if stem.startswith("Device_")
            else "destruction" if stem.startswith("Destruction_")
            else "prop"
        )
        return "props", sub, stem, "base"
    return None


def _resolve_entity_anim_targets(cbase: str) -> list[tuple[str, str]]:
    """Map Monster_/NPC_ animation prefix -> candidate (category, entity_id) pairs."""
    if cbase.startswith("Monster_"):
        out = [("monsters", cbase)]
        no_tag = re.sub(r"^Monster_(?:Boss_|Elite_|Normal_)", "Monster_", cbase)
        if no_tag != cbase:
            out.append(("monsters", no_tag))
        return out
    if cbase.startswith("NPC_"):
        out = [("npcs", cbase)]
        norm = re.sub(r"^NPC_(?:(?:Male|Female|Boy|Girl|Child)_)?(?:Size\d+_)?", "NPC_", cbase)
        if norm != cbase:
            out.append(("npcs", norm))
        return out
    return []


def _norm_char_key(name: str) -> str:
    low = name.strip().lower()
    if low.startswith("avatar_bangboo_"):
        low = low[len("avatar_"):]
    if low.startswith("bangboo_"):
        if low.endswith("001") and len(low) > len("bangboo_") + 3:
            low = low[:-3]
        return low
    m = re.match(r"^(avatar_(?:male|female)_size\d+_[a-z0-9]+)_([a-z0-9]+)$", low)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return low


def _merge_streaming_layer_scenes(maps_dict: dict[str, dict]) -> dict[str, dict]:
    if not maps_dict:
        return maps_dict
    low_to_key = {k.lower(): k for k in maps_dict}
    to_remove: list[str] = []
    for mname, entry in maps_dict.items():
        if not isinstance(entry, dict) or not mname.lower().endswith("_streaming") or "streaming" in entry:
            continue
        parent_name = low_to_key.get(mname.lower()[: -len("_streaming")])
        if not parent_name or parent_name == mname:
            continue
        bs = entry.get("base_scene")
        if isinstance(bs, dict) and bs.get("blk"):
            pentry = maps_dict[parent_name]
            pbs = pentry.get("base_scene") or {}
            extras = list(pentry.get("extra_scenes") or [])
            seen = {
                (pbs.get("blk", ""), pbs.get("cab", "")),
                *((e.get("blk", ""), e.get("cab", "")) for e in extras if isinstance(e, dict)),
            }
            key = (bs.get("blk", ""), bs.get("cab", ""))
            if key not in seen:
                extras.append({"blk": bs.get("blk", ""), "cab": bs.get("cab", "")})
                pentry["extra_scenes"] = extras
            to_remove.append(mname)
    for mname in to_remove:
        maps_dict.pop(mname, None)
    return maps_dict


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
                return _merge_streaming_layer_scenes(maps)
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
                return catalog.apply_variant_animations(chars)
        except Exception:
            pass
    return {}


def load_oz_entities_index() -> dict[str, dict[str, dict]]:
    p = cfg.CACHE_DIR / "asset_map" / "oz_entities_index.json"
    empty: dict[str, dict[str, dict]] = {"monsters": {}, "npcs": {}, "weapons": {}, "props": {}}
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            for k in empty:
                if isinstance(data.get(k), dict):
                    empty[k] = data[k]
        except Exception:
            pass
    return empty


def load_oz_media_index() -> dict:
    p = cfg.CACHE_DIR / "asset_map" / "oz_media_index.json"
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def load_font_blocks() -> list[str]:
    p = cfg.CACHE_DIR / "asset_map" / "fonts_index_auto.json"
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            blks = data.get("blocks")
            if isinstance(blks, list):
                return [str(b) for b in blks if b]
        except Exception:
            pass
    return []


def load_shader_blocks() -> list[str]:
    p = cfg.CACHE_DIR / "asset_map" / "shaders_index_auto.json"
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            blks = data.get("blocks")
            if isinstance(blks, list):
                return [str(b) for b in blks if b]
        except Exception:
            pass
    return []


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
            for es in v.get("extra_scenes") or ():
                if isinstance(es, dict) and es.get("blk") and es["blk"] not in blks:
                    blks.append(es["blk"])
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
        oz = load_oz_map_index()
        cls = catalog.classify_maps(oz) if oz else {}
        return {
            k: v
            for k, v in stages.items()
            if k not in _NON_MAP_SECTIONS
            and v
            and (catalog.is_curated_map(k, cls.get(k)) if cls else True)
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
                has_core = (cfg.CACHE_DIR / "asset_map" / "oz_maps_index.json").is_file() and (cfg.CACHE_DIR / "asset_map" / "oz_characters_index.json").is_file()
                has_supp = (cfg.CACHE_DIR / "asset_map" / "oz_entities_index.json").is_file() and (cfg.CACHE_DIR / "asset_map" / "oz_media_index.json").is_file()
                can_rebuild_cli = (files_root / "Blocks").exists() and cfg.CLI_EXE.is_file()
                min_ver = 5 if can_rebuild_cli else 2
                if has_core and data.get("oz_index_version", 0) >= min_ver and (not can_rebuild_cli or has_supp):
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
    _HLOD_MESH_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}_[0-9]+_HLOD")
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


_SPINE_EXT_RE = re.compile(r"\.(skel|atlas)(?:\.(?:txt|bytes))?$", re.IGNORECASE)


class _MediaCollector:
    """Shared collector for Spine 2D rigs, curated UI Sprite packs, and Data packs."""

    def __init__(self):
        self.spine: dict[str, dict] = {}
        self.tex_blks_by_stem: dict[str, set[str]] = {}
        self.sprites: dict[str, dict] = {
            k: {"label": lbl, "count": 0, "blks": set(), "name_pattern": pat, "samples": []}
            for k, lbl, pat, _ in _UI_SPRITE_BUCKET_RES
        }
        self.data_packs: dict[str, dict] = {
            "subtitles": {
                "label": "Cutscene & Dialogue Subtitles",
                "count": 0,
                "blks": set(),
                "types": ["TextAsset"],
                "name_pattern": "(?i)subtitle",
                "samples": [],
            },
            "cloth_configs": {
                "label": "Cloth Physics Configs (.cloth)",
                "count": 0,
                "blks": set(),
                "types": ["TextAsset"],
                "name_pattern": r"\.cloth$",
                "samples": [],
            },
            "human_configs": {
                "label": "Human / Rig Configs (.human)",
                "count": 0,
                "blks": set(),
                "types": ["TextAsset"],
                "name_pattern": r"\.human$",
                "samples": [],
            },
            "story_graphs": {
                "label": "Dialogue & Story Graphs",
                "count": 0,
                "blks": set(),
                "types": ["TextAsset"],
                "name_pattern": r"^(?:GroupGraph|DateGraph|DateBubbleGraph)_",
                "samples": [],
            },
        }
        self.font_blks: set[str] = set()
        self.shader_blks: set[str] = set()

    def feed(self, atype: str, aname: str, blk: str, key: str = ""):
        if atype == "Font" and blk:
            self.font_blks.add(blk)
            return
        if atype == "Shader" and blk:
            self.shader_blks.add(blk)
            return
        if not aname:
            return
        if atype == "TextAsset":
            sm = _SPINE_EXT_RE.search(aname)
            if sm:
                stem = _SPINE_EXT_RE.sub("", aname).strip()
                if stem:
                    sk = stem.lower()
                    sp = self.spine.setdefault(sk, {
                        "name": stem,
                        "skel_names": [],
                        "atlas_names": [],
                        "blks": set(),
                        "keys": [],
                        "_seen_skel": set(),
                        "_seen_atlas": set(),
                        "_seen_keys": set(),
                    })
                    kind = sm.group(1).lower()
                    if kind == "skel" and aname not in sp["_seen_skel"]:
                        sp["_seen_skel"].add(aname)
                        sp["skel_names"].append(aname)
                    elif kind == "atlas" and aname not in sp["_seen_atlas"]:
                        sp["_seen_atlas"].add(aname)
                        sp["atlas_names"].append(aname)
                    if blk:
                        sp["blks"].add(blk)
                    if key and key not in sp["_seen_keys"]:
                        sp["_seen_keys"].add(key)
                        sp["keys"].append(key)
                return
            low = aname.lower()
            if low.endswith(".cloth"):
                dp = self.data_packs["cloth_configs"]
            elif low.endswith(".human"):
                dp = self.data_packs["human_configs"]
            elif "subtitle" in low or (key and "subtitle" in key.lower()):
                dp = self.data_packs["subtitles"]
            elif aname.startswith(("GroupGraph_", "DateGraph_", "DateBubbleGraph_")):
                dp = self.data_packs["story_graphs"]
            else:
                return
            dp["count"] += 1
            if blk:
                dp["blks"].add(blk)
            if len(dp["samples"]) < 8 and aname not in dp["samples"]:
                dp["samples"].append(aname)
        elif atype == "Sprite":
            for bkey, _lbl, _pat, cre in _UI_SPRITE_BUCKET_RES:
                if cre is None or cre.match(aname):
                    sb = self.sprites[bkey]
                    sb["count"] += 1
                    if blk:
                        sb["blks"].add(blk)
                    if len(sb["samples"]) < 8 and aname not in sb["samples"]:
                        sb["samples"].append(aname)
                    break
        elif atype == "Texture2D" and blk:
            low = aname.lower()
            if "spine" in low or "ui" in low or len(self.tex_blks_by_stem) < 120000:
                self.tex_blks_by_stem.setdefault(low, set()).add(blk)

    def finalize(self, sort_blk_fn=None) -> tuple[dict, list[str], list[str]]:
        key_fn = sort_blk_fn or (lambda b: b)
        spine_out: dict[str, dict] = {}
        for sk, sp in sorted(self.spine.items()):
            blks = set(sp["blks"])
            # Attach paired Texture2D .blks sharing the spine stem or stem_suffix
            t_hit = self.tex_blks_by_stem.get(sk)
            if t_hit:
                blks.update(t_hit)
            for idx_tex in (2, 3, 4):
                t_hit2 = self.tex_blks_by_stem.get(f"{sk}_{idx_tex}")
                if t_hit2:
                    blks.update(t_hit2)
            name = sp["name"]
            if name in spine_out:
                name = f"{name}_{sk}"
            spine_out[name] = {
                "name": sp["name"],
                "skel_names": sorted(sp["skel_names"]),
                "atlas_names": sorted(sp["atlas_names"]),
                "blks": sorted(blks, key=key_fn),
                "keys": sp["keys"],
            }

        sprites_out: dict[str, dict] = {}
        for bkey, sb in self.sprites.items():
            sprites_out[bkey] = {
                "label": sb["label"],
                "count": sb["count"],
                "blks": sorted(sb["blks"], key=key_fn),
                "name_pattern": sb["name_pattern"],
                "samples": sb["samples"],
            }

        # Merge known CameraAnim/SubtitleJSON blocks from sections index when present
        sub_sec = load_texture_sections().get("CameraAnim/SubtitleJSON")
        if isinstance(sub_sec, dict) and sub_sec.get("blocks"):
            self.data_packs["subtitles"]["blks"].update(
                f"Blocks/{os.path.basename(b)}" if "/" not in str(b) else str(b)
                for b in sub_sec["blocks"]
            )
            for smp in sub_sec.get("samples", ())[:5]:
                if len(self.data_packs["subtitles"]["samples"]) < 8:
                    self.data_packs["subtitles"]["samples"].append(os.path.basename(smp))

        sorted_shaders = sorted(self.shader_blks, key=key_fn)
        sorted_fonts = sorted(self.font_blks, key=key_fn)

        data_out: dict[str, dict] = {}
        for dk, dp in self.data_packs.items():
            data_out[dk] = {
                "label": dp["label"],
                "count": dp["count"],
                "blks": sorted(dp["blks"], key=key_fn),
                "types": dp["types"],
                "name_pattern": dp["name_pattern"],
                "samples": dp["samples"],
            }
        data_out["shaders"] = {
            "label": "Unity Shaders",
            "count": len(sorted_shaders),
            "blks": sorted_shaders,
            "types": ["Shader"],
            "name_pattern": "",
            "samples": [],
        }

        media = {
            "version": 4,
            "spine": spine_out,
            "ui_sprites": sprites_out,
            "data_packs": data_out,
        }
        return media, sorted_fonts, sorted_shaders


def _build_oz_indexes_from_jsonl(
    jsonl_paths: Path | list[Path],
    map_dir: Path,
    game_root: Path,
    common_base: Path | None = None,
    write_cab_map: bool = True,
    total_blk_count: int | None = None,
    log_cb=None,
) -> tuple[int, int, int]:
    """Parse lightweight AnimeStudio AS_DUMP_OZ_INDEX JSONL shard(s) -> OZ v5 indexes & Maps/ZZZ.bin."""
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
    cab_mesh_counts: dict[tuple[str, str], int] = {}
    ab_by_key: dict[str, tuple[tuple[int, float], str, str, str, str]] = {}
    char_cands: dict[str, dict[str, list[tuple[tuple[int, float], str, str, str, str]]]] = {}
    ent_cands: dict[str, dict[str, dict[str, list[tuple[tuple[int, float], str, str, str, str]]]]] = {
        "monsters": {}, "npcs": {}, "weapons": {}, "props": {},
    }
    ent_subtypes: dict[str, dict[str, str]] = {
        "monsters": {}, "npcs": {}, "weapons": {}, "props": {},
    }
    mesh_by_name: dict[str, tuple[tuple[int, float], str, str, str]] = {}
    mesh_by_name_ci: dict[str, tuple[tuple[int, float], str, str, str]] = {}
    char_anis_primary: dict[str, dict[str, tuple[tuple[int, float], str, str]]] = {}
    char_anis_default: dict[str, dict[str, tuple[tuple[int, float], str, str]]] = {}
    ent_anis: dict[tuple[str, str], dict[str, tuple[tuple[int, float], str, str]]] = {}
    scenes: dict[str, tuple[tuple[int, float], str, str]] = {}
    streams: dict[str, tuple[tuple[int, int, int, float], str, str, str, str, list[str]]] = {}
    lods_by_cab: dict[tuple[str, str], list[tuple[str, str]]] = {}
    media_collector = _MediaCollector()

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
                        mc = int(row.get("meshes") or 0)
                        prev_mc = cab_mesh_counts.get((blk, cab), 0)
                        if mc > prev_mc:
                            cab_mesh_counts[(blk, cab)] = mc
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
                    if atype in ("TextAsset", "Sprite", "Texture2D", "Font", "Shader"):
                        media_collector.feed(atype, aname, blk, key)
                    if atype == "GameObject" and aname:
                        cand_row = (bp, blk, cab, key, aname)
                        char_res = _resolve_char_model_key(aname)
                        if char_res is not None:
                            ckey, slot = char_res
                            char_cands.setdefault(ckey, {}).setdefault(slot, []).append(cand_row)
                        else:
                            ent_res = _classify_entity_gameobject(aname)
                            if ent_res is not None:
                                cat_k, sub_t, ekey, slot = ent_res
                                ent_subtypes[cat_k].setdefault(ekey, sub_t)
                                ent_cands[cat_k].setdefault(ekey, {}).setdefault(slot, []).append(cand_row)
                    if atype == "Mesh" and aname:
                        prev_m = mesh_by_name.get(aname)
                        if prev_m is None or bp < prev_m[0]:
                            mesh_by_name[aname] = (bp, blk, cab, key)
                        alow = aname.lower()
                        prev_ci = mesh_by_name_ci.get(alow)
                        if prev_ci is None or bp < prev_ci[0]:
                            mesh_by_name_ci[alow] = (bp, blk, cab, key)
                    if aname and ("_Ani_" in aname) and key:
                        am = _OZ_ANI_CHAR_RE.match(aname)
                        if am:
                            cres = _resolve_char_anim_target(am.group(1))
                            if cres is not None:
                                target_k, is_def = cres
                                bucket = char_anis_default if is_def else char_anis_primary
                                cmap = bucket.setdefault(target_k, {})
                                prev_a = cmap.get(aname)
                                if prev_a is None or bp < prev_a[0]:
                                    cmap[aname] = (bp, blk, key)
                        else:
                            em = _OZ_ENT_ANI_RE.match(aname)
                            if em:
                                for ecat, ename in _resolve_entity_anim_targets(em.group(1)):
                                    emap = ent_anis.setdefault((ecat, ename), {})
                                    prev_a = emap.get(aname)
                                    if prev_a is None or bp < prev_a[0]:
                                        emap[aname] = (bp, blk, key)

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
                    media_collector.font_blks.add(blk)

                elif k == "shader":
                    media_collector.shader_blks.add(blk)

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

    maps_dict = _merge_streaming_layer_scenes(maps_dict)

    ani_primary_by_norm: dict[str, dict[str, tuple[tuple[int, float], str, str]]] = {}
    ani_default_by_norm: dict[str, dict[str, tuple[tuple[int, float], str, str]]] = {}
    for src_map, dst_norm in ((char_anis_primary, ani_primary_by_norm), (char_anis_default, ani_default_by_norm)):
        for cbase, cmap in src_map.items():
            dst = dst_norm.setdefault(_norm_char_key(cbase), {})
            for aname, aval in cmap.items():
                prev_a = dst.get(aname)
                if prev_a is None or aval[0] < prev_a[0]:
                    dst[aname] = aval

    def _score_cand(slot: str, cand: tuple[tuple[int, float], str, str, str, str]):
        bp, b_blk, b_cab, _k, _pn = cand
        lods = lods_by_cab.get((b_blk, b_cab), [])
        valid_lods = [
            (gn, mp) for gn, mp in lods
            if "hairshadow" not in gn.lower()
            and "hairshadow" not in mp.lower()
            and not gn.lower().endswith("_lod3")
            and "_lod3.mesh" not in mp.lower()
        ]
        non_weapon = [
            (gn, mp) for gn, mp in valid_lods
            if "weapon" not in gn.lower() and "weapon" not in Path(mp).stem.lower()
        ]
        emb = cab_mesh_counts.get((b_blk, b_cab), 0)
        score = (
            1 if non_weapon else 0,
            len(non_weapon),
            len(valid_lods),
            1 if emb > 0 else 0,
            emb,
            1 if slot == "base" else 0,
            -bp[0],
            -bp[1],
        )
        return score, lods, emb

    def _resolve_model_entry(cname: str, cands: dict, all_cands: dict, ani_map: dict, allow_variant_fallback: bool = True) -> dict | None:
        best_tuple = None
        for slot in ("base", "model"):
            for cand in cands.get(slot, ()):
                sc, lods, emb = _score_cand(slot, cand)
                if best_tuple is None or sc > best_tuple[0]:
                    best_tuple = (sc, cand, lods, emb)
        if best_tuple is None:
            return None
        _, chosen, chosen_lods, emb_meshes = best_tuple

        if allow_variant_fallback and not chosen_lods and emb_meshes == 0:
            cname_low = cname.lower()
            model_hint = f"{cname_low}_model"
            alt = None
            for ocands in all_cands.values():
                for oslot in ("base", "model"):
                    for cand in ocands.get(oslot, ()):
                        op_bp, op_blk, op_cab, _ok, oname = cand
                        if oname == chosen[4] or not oname.lower().startswith(cname_low):
                            continue
                        olods = lods_by_cab.get((op_blk, op_cab), [])
                        if not olods:
                            continue
                        if not any(model_hint in mesh_path.lower() for _, mesh_path in olods):
                            continue
                        score = (len(olods), -op_bp[0], -op_bp[1])
                        if alt is None or score > alt[0]:
                            alt = (score, cand, olods)
            if alt is not None:
                chosen = alt[1]
                chosen_lods = alt[2]
                emb_meshes = cab_mesh_counts.get((chosen[1], chosen[2]), 0)
                _emit(
                    log_cb,
                    f"Index: {cname} prefab has no LODs; using variant {chosen[4]} "
                    f"({len(chosen_lods)} LODs)",
                )

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
                hit_m = mesh_by_name.get(stem) or mesh_by_name_ci.get(stem.lower())
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

        if not extra_cabs and emb_meshes == 0:
            prefixes = (f"SeparateMesh_{cname}_Model_", f"SeparateMesh_{cname}_")
            short_name = cname.rsplit("_", 1)[-1]
            short_prefixes = (
                f"{short_name}_Body",
                f"{short_name}_Face",
                f"{short_name}_Hair",
            ) if len(short_name) >= 3 and not cname.startswith(("Weapon_", "Device_", "Item_")) else ()
            for mname, (_, m_blk, m_cab, _) in sorted(mesh_by_name.items()):
                is_sep = any(mname.startswith(pf) for pf in prefixes)
                is_short = bool(short_prefixes and any(mname.startswith(pf) for pf in short_prefixes))
                if not (is_sep or is_short):
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

        if cname.startswith("Bangboo_"):
            has_model_prefab = pname.endswith("_Model") or ("model" in cands)
            if not (has_model_prefab or chosen_lods or emb_meshes > 0 or extra_cabs):
                return None

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

        return {
            "prefab_blk": p_blk,
            "prefab_cab": p_cab,
            "prefab_key": p_key,
            "prefab_name": pname,
            "extra_cabs": extra_cabs,
            "mesh_blks": mesh_blks,
            "embedded_meshes": emb_meshes,
            "anim_blks": anim_blks,
            "anim_keys": anim_keys,
        }

    def _collect_char_anis(cname: str) -> dict:
        prim = char_anis_primary.get(cname) or ani_primary_by_norm.get(_norm_char_key(cname)) or {}
        dflt = char_anis_default.get(cname) or ani_default_by_norm.get(_norm_char_key(cname)) or {}
        merged: dict = dict(prim)
        seen_actions = {
            aname.split("_Ani_", 1)[1].lower()
            for aname in merged
            if "_Ani_" in aname
        }
        for aname, aval in dflt.items():
            act = aname.split("_Ani_", 1)[1].lower() if "_Ani_" in aname else aname.lower()
            if act not in seen_actions:
                merged[aname] = aval
                seen_actions.add(act)
        if not merged or "Remielle" in cname:
            m_camel = re.match(r"^(Avatar_(?:Male|Female)_Size\d+_)([A-Z][a-z]+)[A-Z].*$", cname)
            if m_camel:
                stem = m_camel.group(1) + m_camel.group(2)
                if stem not in char_cands:
                    for src_m in (char_anis_primary, char_anis_default):
                        extra = src_m.get(stem) or {}
                        for aname, aval in extra.items():
                            act = aname.split("_Ani_", 1)[1].lower() if "_Ani_" in aname else aname.lower()
                            if act not in seen_actions:
                                merged[aname] = aval
                                seen_actions.add(act)
        return merged

    chars_dict: dict[str, dict] = {}
    for cname in sorted(char_cands):
        ani_map = _collect_char_anis(cname)
        rec = _resolve_model_entry(cname, char_cands[cname], char_cands, ani_map, allow_variant_fallback=True)
        if rec is not None:
            chars_dict[cname] = rec

    entities_out: dict[str, dict[str, dict]] = {
        "monsters": {}, "npcs": {}, "weapons": {}, "props": {},
    }
    for cat_k, cands_map in ent_cands.items():
        for ekey in sorted(cands_map):
            ani_map = ent_anis.get((cat_k, ekey), {})
            rec = _resolve_model_entry(ekey, cands_map[ekey], cands_map, ani_map, allow_variant_fallback=False)
            if rec is not None:
                rec["subtype"] = ent_subtypes[cat_k].get(ekey, "")
                entities_out[cat_k][ekey] = rec

    media_dict, sorted_fonts, sorted_shaders = media_collector.finalize(
        sort_blk_fn=lambda b: (_blk_prio(b), b)
    )

    (map_dir / "oz_maps_index.json").write_text(
        json.dumps({"version": 5, "maps": maps_dict}, indent=2),
        encoding="utf-8",
    )
    (map_dir / "oz_characters_index.json").write_text(
        json.dumps({"version": 4, "characters": chars_dict}, indent=2),
        encoding="utf-8",
    )
    (map_dir / "oz_entities_index.json").write_text(
        json.dumps({"version": 4, **entities_out}, indent=2),
        encoding="utf-8",
    )
    (map_dir / "oz_media_index.json").write_text(
        json.dumps(media_dict, indent=2),
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
        for es in info.get("extra_scenes") or ():
            if isinstance(es, dict) and es.get("blk") and es["blk"] not in blks:
                blks.append(es["blk"])
        compat_stages[mname] = {"blocks": blks}
    (map_dir / "stage_index_auto.json").write_text(
        json.dumps({"stages": compat_stages}, indent=2),
        encoding="utf-8",
    )

    (map_dir / "fonts_index_auto.json").write_text(
        json.dumps({"blocks": sorted_fonts}, indent=2),
        encoding="utf-8",
    )
    (map_dir / "shaders_index_auto.json").write_text(
        json.dumps({"blocks": sorted_shaders}, indent=2),
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
        f"Built OZ v5 indexes: {len(chars_dict)} characters, "
        f"{len(entities_out['monsters'])} monsters, {len(entities_out['npcs'])} npcs, "
        f"{len(entities_out['weapons'])} weapons/acc, {len(entities_out['props'])} props, "
        f"{len(media_dict['spine'])} spine rigs, {len(maps_dict)} maps, {len(sorted_fonts)} font blocks",
    )
    return len(chars_dict), len(maps_dict), len(sorted_fonts)


def build_supplementary_indexes_from_asset_map(
    map_dir: Path | None = None,
    log_cb=None,
) -> dict:
    """Stream zzz_assets_map.json line-by-line -> build oz_entities_index, oz_media_index, shaders_index_auto,
    and upgrade existing oz_characters_index.json with missed Remielle/_Default/companion animations & models."""
    mdir = map_dir or (cfg.CACHE_DIR / "asset_map")
    am_path = mdir / "zzz_assets_map.json"
    if not am_path.is_file():
        raise FileNotFoundError(f"Asset map not found: {am_path}")

    media_collector = _MediaCollector()
    ent_records: dict[str, dict[str, dict]] = {
        "monsters": {}, "npcs": {}, "weapons": {}, "props": {},
    }
    ent_anis: dict[tuple[str, str], list[tuple[str, str, str]]] = {}

    char_new_cands: dict[str, dict] = {}
    char_anis_primary: dict[str, list[tuple[str, str, str]]] = {}
    char_anis_default: dict[str, list[tuple[str, str, str]]] = {}
    sep_mesh_blks: dict[str, set[str]] = {}

    def _blk_ref(src: str) -> str:
        norm = src.replace("\\\\", "/").replace("\\", "/")
        if "/Blocks/" in norm:
            return "Blocks/" + norm.split("/Blocks/", 1)[1]
        return os.path.basename(norm) or norm

    cur_name = ""
    cur_cont = ""
    cur_src = ""
    cur_type = ""

    def _flush():
        if not cur_type:
            return
        blk = _blk_ref(cur_src) if cur_src else ""
        if cur_type in ("TextAsset", "Sprite", "Texture2D", "Font", "Shader"):
            media_collector.feed(cur_type, cur_name, blk, cur_cont)
            return
        if cur_type == "Animator" and cur_name:
            cres = _resolve_char_model_key(cur_name)
            if cres is not None:
                ckey, slot = cres
                rec = char_new_cands.setdefault(ckey, {
                    "prefab_blk": blk, "prefab_cab": "", "prefab_key": cur_cont,
                    "prefab_name": cur_name, "extra_cabs": [], "mesh_blks": set(),
                    "embedded_meshes": 0, "anim_blks": [], "anim_keys": [],
                    "blocks": set(), "source": "asset_map",
                })
                if blk:
                    rec["blocks"].add(blk)
                    rec["mesh_blks"].add(blk)
                if slot == "model" and cur_name.endswith("_Model"):
                    rec["prefab_blk"] = blk or rec["prefab_blk"]
                    rec["prefab_key"] = cur_cont or rec["prefab_key"]
                    rec["prefab_name"] = cur_name
            else:
                eres = _classify_entity_gameobject(cur_name)
                if eres is not None:
                    cat_k, sub_t, ekey, slot = eres
                    rec = ent_records[cat_k].setdefault(ekey, {
                        "prefab_blk": blk, "prefab_cab": "", "prefab_key": cur_cont,
                        "prefab_name": cur_name, "extra_cabs": [], "mesh_blks": set(),
                        "embedded_meshes": 0, "anim_blks": set(), "anim_keys": set(),
                        "blocks": set(), "subtype": sub_t, "source": "asset_map",
                    })
                    if blk:
                        rec["blocks"].add(blk)
                        rec["mesh_blks"].add(blk)
                    if slot == "model" and cur_name.endswith("_Model"):
                        rec["prefab_blk"] = blk or rec["prefab_blk"]
                        rec["prefab_key"] = cur_cont or rec["prefab_key"]
                        rec["prefab_name"] = cur_name
        elif cur_type == "Mesh" and cur_name.startswith("SeparateMesh_"):
            if cur_name.startswith(("SeparateMesh_Monster_", "SeparateMesh_NPC_", "SeparateMesh_Device_", "SeparateMesh_Destruction_")):
                m_sd = _SEP_DEV_RE.match(cur_name)
                if m_sd:
                    dkey = m_sd.group(1)
                    sub_t = "device" if dkey.startswith("Device_") else "destruction"
                    rec = ent_records["props"].setdefault(dkey, {
                        "prefab_blk": blk, "prefab_cab": "", "prefab_key": "",
                        "prefab_name": dkey, "extra_cabs": [], "mesh_blks": set(),
                        "embedded_meshes": 0, "anim_blks": set(), "anim_keys": set(),
                        "blocks": set(), "subtype": sub_t, "source": "asset_map",
                    })
                    if blk:
                        rec["blocks"].add(blk)
                        rec["mesh_blks"].add(blk)
                for pre in ("SeparateMesh_Monster_", "SeparateMesh_NPC_"):
                    if cur_name.startswith(pre):
                        rest = cur_name[len("SeparateMesh_"):]
                        toks = rest.split("_")
                        if len(toks) >= 2:
                            cand_e = f"{toks[0]}_{toks[1]}"
                            if blk:
                                sep_mesh_blks.setdefault(cand_e, set()).add(blk)
                            if len(toks) >= 3:
                                cand_e2 = f"{toks[0]}_{toks[1]}_{toks[2]}"
                                if blk:
                                    sep_mesh_blks.setdefault(cand_e2, set()).add(blk)
        elif cur_type == "AnimationClip" and "_Ani_" in cur_name:
            am = _OZ_ANI_CHAR_RE.match(cur_name)
            if am:
                cres = _resolve_char_anim_target(am.group(1))
                if cres is not None:
                    tk, is_def = cres
                    dst = char_anis_default if is_def else char_anis_primary
                    dst.setdefault(tk, []).append((cur_name, blk, cur_cont))
            else:
                em = _OZ_ENT_ANI_RE.match(cur_name)
                if em:
                    for ecat, ename in _resolve_entity_anim_targets(em.group(1)):
                        ent_anis.setdefault((ecat, ename), []).append((cur_name, blk, cur_cont))

    with am_path.open("r", encoding="utf-8", errors="ignore", buffering=4 * 1024 * 1024) as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            c0 = s[0]
            if c0 == '"':
                if s.startswith('"Name":'):
                    i = s.find('"', 7)
                    j = s.rfind('"')
                    cur_name = s[i + 1 : j] if j > i >= 0 else ""
                elif s.startswith('"Container":'):
                    i = s.find('"', 12)
                    j = s.rfind('"')
                    cur_cont = s[i + 1 : j] if j > i >= 0 else ""
                elif s.startswith('"Source":'):
                    i = s.find('"', 9)
                    j = s.rfind('"')
                    cur_src = s[i + 1 : j] if j > i >= 0 else ""
                elif s.startswith('"Type":'):
                    i = s.find('"', 7)
                    j = s.rfind('"')
                    cur_type = s[i + 1 : j] if j > i >= 0 else ""
            elif c0 == "}":
                _flush()
                cur_name = cur_cont = cur_src = cur_type = ""

    # Finalize entities
    entities_final: dict[str, dict[str, dict]] = {}
    for cat_k, recs in ent_records.items():
        out_cat: dict[str, dict] = {}
        for ekey in sorted(recs):
            rec = recs[ekey]
            mblks = set(rec["mesh_blks"]) | sep_mesh_blks.get(ekey, set())
            ablks: set[str] = set()
            akeys: list[str] = []
            seen_k: set[str] = set()
            seen_aname: set[str] = set()
            for aname, ablk, akey in ent_anis.get((cat_k, ekey), ()):
                if aname not in seen_aname:
                    seen_aname.add(aname)
                if ablk:
                    ablks.add(ablk)
                if akey and akey not in seen_k:
                    seen_k.add(akey)
                    akeys.append(akey)
            all_b = set(rec["blocks"]) | mblks | ablks
            out_cat[ekey] = {
                "prefab_blk": rec["prefab_blk"],
                "prefab_cab": "",
                "prefab_key": rec["prefab_key"],
                "prefab_name": rec["prefab_name"],
                "extra_cabs": [],
                "mesh_blks": sorted(mblks),
                "embedded_meshes": 0,
                "anim_blks": sorted(ablks),
                "anim_keys": akeys,
                "anim_count": len(seen_aname) or len(akeys),
                "blocks": sorted(all_b),
                "subtype": rec["subtype"],
                "source": "asset_map",
            }
        entities_final[cat_k] = out_cat

    media_dict, sorted_fonts, sorted_shaders = media_collector.finalize()
    (mdir / "oz_entities_index.json").write_text(
        json.dumps({"version": 4, **entities_final}, indent=2),
        encoding="utf-8",
    )
    (mdir / "oz_media_index.json").write_text(
        json.dumps(media_dict, indent=2),
        encoding="utf-8",
    )
    (mdir / "shaders_index_auto.json").write_text(
        json.dumps({"blocks": sorted_shaders}, indent=2),
        encoding="utf-8",
    )
    if sorted_fonts and not (mdir / "fonts_index_auto.json").is_file():
        (mdir / "fonts_index_auto.json").write_text(
            json.dumps({"blocks": sorted_fonts}, indent=2),
            encoding="utf-8",
        )

    # Non-destructively upgrade oz_characters_index.json with missed v4 animations/companions/Bangboo
    oz_chars_p = mdir / "oz_characters_index.json"
    if oz_chars_p.is_file():
        try:
            oz_data = json.loads(oz_chars_p.read_text(encoding="utf-8", errors="ignore"))
            chars = oz_data.get("characters")
            if isinstance(chars, dict):
                # Remove prior asset_map-sourced additions so stale keys (e.g. Bangboo_pointalight) are replaced cleanly
                for dk in [k for k, v in chars.items() if isinstance(v, dict) and v.get("source") == "asset_map"]:
                    chars.pop(dk, None)

                def _gather_clips(ckey: str) -> tuple[list[str], list[str], int]:
                    seen_act: set[str] = set()
                    ablks: list[str] = []
                    ab_set: set[str] = set()
                    akeys: list[str] = []
                    ak_set: set[str] = set()
                    clip_n = 0
                    cands_keys = [ckey]
                    us = re.sub(r"^(Avatar_(?:Male|Female)_Size\d+_)([A-Za-z0-9]+)_([A-Za-z0-9]+)$", r"\1\2\3", ckey)
                    if us != ckey:
                        cands_keys.append(us)
                    if ckey.startswith("Avatar_Bangboo_"):
                        cands_keys.append(ckey[len("Avatar_"):])
                    for src_map in (char_anis_primary, char_anis_default):
                        for ck in cands_keys:
                            for aname, ablk, akey in src_map.get(ck, ()):
                                act = aname.split("_Ani_", 1)[1].lower() if "_Ani_" in aname else aname.lower()
                                if act in seen_act:
                                    continue
                                seen_act.add(act)
                                clip_n += 1
                                if ablk and ablk not in ab_set:
                                    ab_set.add(ablk)
                                    ablks.append(ablk)
                                if akey and akey not in ak_set:
                                    ak_set.add(akey)
                                    akeys.append(akey)
                    if "Remielle" in ckey:
                        for src_map in (char_anis_primary, char_anis_default):
                            for aname, ablk, akey in src_map.get("Avatar_Female_Size02_Remielle", ()):
                                act = aname.split("_Ani_", 1)[1].lower() if "_Ani_" in aname else aname.lower()
                                if act not in seen_act:
                                    seen_act.add(act)
                                    clip_n += 1
                                    if ablk and ablk not in ab_set:
                                        ab_set.add(ablk)
                                        ablks.append(ablk)
                                    if akey and akey not in ak_set:
                                        ak_set.add(akey)
                                        akeys.append(akey)
                    return ablks, akeys, clip_n

                for ck, entry in list(chars.items()):
                    if not isinstance(entry, dict):
                        continue
                    if (
                        entry.get("prefab_cab")
                        and not entry.get("extra_cabs")
                        and int(entry.get("embedded_meshes") or 0) == 0
                        and not str(entry.get("prefab_name") or "").endswith("_Model")
                        and entry.get("mesh_blks") == ([entry.get("prefab_blk")] if entry.get("prefab_blk") else [])
                    ):
                        entry["mesh_blks"] = []
                    ablks, akeys, _ = _gather_clips(ck)
                    if akeys and len(akeys) > len(entry.get("anim_keys") or []):
                        ex_b = list(entry.get("anim_blks") or [])
                        ex_b_set = set(ex_b)
                        for b in ablks:
                            if b not in ex_b_set:
                                ex_b_set.add(b)
                                ex_b.append(b)
                        ex_k = list(entry.get("anim_keys") or [])
                        ex_k_set = set(ex_k)
                        for ak in akeys:
                            if ak not in ex_k_set:
                                ex_k_set.add(ak)
                                ex_k.append(ak)
                        entry["anim_blks"] = ex_b
                        entry["anim_keys"] = ex_k

                existing_by_norm: dict[str, str] = {}
                for k in chars:
                    kl = k.lower()
                    existing_by_norm[kl] = k
                    existing_by_norm[kl.replace("_", "")] = k
                    if kl.startswith("avatar_bangboo_"):
                        existing_by_norm[kl[len("avatar_"):]] = k

                for nkey, nrec in sorted(char_new_cands.items()):
                    nlow = nkey.lower()
                    alt_b = nlow[len("avatar_"):] if nlow.startswith("avatar_bangboo_") else nlow
                    hit_existing = (
                        existing_by_norm.get(nlow)
                        or existing_by_norm.get(nlow.replace("_", ""))
                        or existing_by_norm.get(alt_b)
                    )
                    if hit_existing:
                        ex_entry = chars.get(hit_existing)
                        if (
                            isinstance(ex_entry, dict)
                            and nrec["mesh_blks"]
                            and not ex_entry.get("mesh_blks")
                            and str(ex_entry.get("prefab_name") or nrec["prefab_name"]).endswith("_Model")
                        ):
                            ex_entry["mesh_blks"] = sorted(nrec["mesh_blks"])
                        continue
                    ablks, akeys, _ = _gather_clips(nkey)
                    chars[nkey] = {
                        "prefab_blk": nrec["prefab_blk"],
                        "prefab_cab": "",
                        "prefab_key": nrec["prefab_key"],
                        "prefab_name": nrec["prefab_name"],
                        "extra_cabs": [],
                        "mesh_blks": sorted(nrec["mesh_blks"]),
                        "embedded_meshes": 0,
                        "anim_blks": ablks,
                        "anim_keys": akeys,
                        "blocks": sorted(set(nrec["blocks"]) | set(ablks)),
                        "source": "asset_map",
                    }
                    existing_by_norm[nlow] = nkey
                    existing_by_norm[nlow.replace("_", "")] = nkey
                oz_data["version"] = 4
                oz_chars_p.write_text(json.dumps(oz_data, indent=2), encoding="utf-8")
        except Exception as exc:
            _emit(log_cb, f"[WARN] Could not upgrade oz_characters_index.json: {exc}")

    summary = {
        "monsters": len(entities_final["monsters"]),
        "npcs": len(entities_final["npcs"]),
        "weapons": len(entities_final["weapons"]),
        "props": len(entities_final["props"]),
        "spine": len(media_dict["spine"]),
        "ui_sprite_packs": {k: v["count"] for k, v in media_dict["ui_sprites"].items()},
        "shaders": len(sorted_shaders),
    }
    _emit(log_cb, f"Supplementary OZ v4 indexes ready: {summary}")
    return summary


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
    oz_ents_file = map_dir / "oz_entities_index.json"
    oz_media_file = map_dir / "oz_media_index.json"
    manifest_file = map_dir / "manifest.json"
    prev_ver = 0
    if manifest_file.is_file():
        try:
            prev_ver = int(json.loads(manifest_file.read_text()).get("oz_index_version", 0))
        except Exception:
            prev_ver = 0
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
        and (
            not oz_maps_file.is_file()
            or not oz_chars_file.is_file()
            or not oz_ents_file.is_file()
            or not oz_media_file.is_file()
            or prev_ver < 5
            or need_cab
        )
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
        "oz_index_version": 5,
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
