"""Asset indexing, character/stage/section loaders, and startup asset mapping."""
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
import time
import zipfile
from pathlib import Path
from typing import Sequence

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
_OZ_ENT_ANI_RE = re.compile(r"^((?:Monster|NPC|Weapon|Device|Item|Destruction)_[A-Za-z0-9_]+?)_Ani_")
_WENGINE_ANI_RE = re.compile(
    r"^(Weapon_[A-Z]_(?:Common_\d+|\d+))_(?:Ani_)?(?:Idle|Transform|Show)(?:_[A-Za-z0-9_]+)?$"
)

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
    "_lod1", "_lod2", "_lod3", "_hairshadow", "_col", "_collider", "_collier",
    "_proxy", "_shadow", "_fx", "_effect", "_maincity", "_npc", "_ui",
    "_timeline", "_transform", "_fighting",
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


_CHAR_MAINCITY_FALLBACK_RE = re.compile(
    r"^Avatar_MainCity_Size(\d+)_(Belle|Wise)([A-Za-z0-9]+)_MainCity$"
)
_CHAR_FB_BASE_RE = re.compile(
    r"^(Avatar_(?:Male|Female)_Size\d+_[A-Za-z0-9]+|Avatar_Bangboo_[A-Za-z0-9]+)_(?:Fighting|MainCity|UI|NPC)$"
)
_CHAR_FB_US_RE = re.compile(
    r"^(Avatar_(?:Male|Female)_Size\d+_[A-Za-z0-9]+)_([A-Za-z0-9]+)(?:_([A-Za-z0-9]+))?_(?:Fighting|MainCity|UI|NPC)$"
)


def _resolve_char_model_key(aname: str) -> tuple[str, str] | None:
    """Resolve an Avatar/Bangboo/companion GameObject/Animator name -> (canonical_id, slot)."""
    if not aname or "(" in aname:
        return None
    if aname == "Avatar_Female_Size02_OldSeedSr_NPC":
        return "Avatar_Male_Size03_SeedSr", "fallback"
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
        if not any(s.isdigit() or s.lower() in _OZ_IGNORE_MODEL_SUFFIXES for s in segs):
            if any(_is_companion_token(s) for s in segs):
                norm_segs: list[str] = []
                for s in segs:
                    m_wk = re.match(r"^(Wood|Kuriaki)([A-Z][A-Za-z0-9]*)$", s)
                    if m_wk:
                        norm_segs.extend((m_wk.group(1), m_wk.group(2)))
                    else:
                        norm_segs.append(s)
                return f"{um2.group(1)}_{'_'.join(norm_segs)}", "base"
    mc = _CHAR_MAINCITY_FALLBACK_RE.match(aname)
    if mc:
        gender = "Female" if mc.group(2) == "Belle" else "Male"
        return f"Avatar_{gender}_Size{mc.group(1)}_{mc.group(2)}{mc.group(3)}", "fallback"
    fb = _CHAR_FB_BASE_RE.match(aname)
    if fb:
        return fb.group(1), "fallback"
    fb2 = _CHAR_FB_US_RE.match(aname)
    if fb2:
        segs = [s for s in (fb2.group(2), fb2.group(3)) if s]
        if not any(s.isdigit() or s.lower() in _OZ_IGNORE_MODEL_SUFFIXES for s in segs):
            if any(_is_companion_token(s) for s in segs):
                return f"{fb2.group(1)}_{'_'.join(segs)}", "fallback"
            if len(segs) == 1:
                return f"{fb2.group(1)}{segs[0]}", "fallback"
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
    if "_Ani_" in aname:
        return None
    is_model = aname.endswith("_Model")
    stem = aname[:-6] if is_model else aname
    stem_low = stem.lower()
    if (
        stem_low.endswith(_ENTITY_NOISE_SUFFIXES)
        or stem_low.endswith(("collider", "collier"))
        or re.search(r"\(\d+\)$", stem)
    ):
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
    """Map Monster_/NPC_/Weapon_/Device_/Item_ animation prefix -> candidate (category, entity_id) pairs."""
    wm = _WENGINE_ANI_RE.match(cbase)
    if wm:
        return [("weapons", wm.group(1))]
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
    if cbase.startswith("Weapon_"):
        return [("weapons", cbase)]
    if cbase.startswith(("Device_", "Item_", "Destruction_")):
        return [("props", cbase)]
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


_INDEX_JSON_CACHE: dict[str, tuple[tuple[int, int], object]] = {}
NAP_INDEX_BLK_NAME = "2837526243.blk"
PREBUILT_INDEX_ZIP = cfg.ASSETS_DIR / "prebuilt_index.zip"
_PREBUILT_READ_CACHE: dict[str, dict] = {}


def invalidate_index_cache() -> None:
    """Clear in-memory cached parsed asset indexes so updated files are re-read from disk."""
    _INDEX_JSON_CACHE.clear()
    _PREBUILT_READ_CACHE.clear()
    cfg.invalidate_blk_cache()


def ensure_prebuilt_index_loaded(force: bool = False) -> bool:
    """Unpack bundled `prebuilt_index.zip` into `CACHE_DIR/asset_map` when core indexes are absent."""
    map_dir = cfg.CACHE_DIR / "asset_map"
    has_core = (map_dir / "oz_characters_index.json").is_file() and (map_dir / "oz_maps_index.json").is_file()
    if has_core and not force:
        return True
    if not PREBUILT_INDEX_ZIP.is_file():
        return False
    map_dir.mkdir(parents=True, exist_ok=True)
    unpacked = 0
    try:
        with zipfile.ZipFile(PREBUILT_INDEX_ZIP, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                bname = Path(info.filename.replace("\\", "/")).name
                if not bname.endswith(".json"):
                    continue
                dst = map_dir / bname
                if force or not dst.is_file():
                    tmp = dst.with_suffix(".json.tmp")
                    tmp.write_bytes(zf.read(info.filename))
                    os.replace(tmp, dst)
                    unpacked += 1
    except Exception:
        return (map_dir / "oz_characters_index.json").is_file()
    if unpacked:
        _INDEX_JSON_CACHE.clear()
    return (map_dir / "oz_characters_index.json").is_file()


def _read_prebuilt_json(filename: str) -> dict:
    """Read a JSON table from `PREBUILT_INDEX_ZIP` (cached in memory for partial index merges)."""
    if filename in _PREBUILT_READ_CACHE:
        return _PREBUILT_READ_CACHE[filename]
    if not PREBUILT_INDEX_ZIP.is_file():
        return {}
    try:
        with zipfile.ZipFile(PREBUILT_INDEX_ZIP, "r") as zf:
            for name in zf.namelist():
                if Path(name.replace("\\", "/")).name == filename:
                    data = json.loads(zf.read(name).decode("utf-8", errors="ignore"))
                    if isinstance(data, dict):
                        _PREBUILT_READ_CACHE[filename] = data
                        return data
    except Exception:
        pass
    return {}


def load_blk_bundle_hashes() -> dict[str, list[str]]:
    """Load `{blk_basename_lower: [bundleHashName_str, ...]}` translation table from cache or prebuilt snapshot."""
    p = cfg.CACHE_DIR / "asset_map" / "oz_blk_bundle_hashes.json"
    if not p.is_file():
        ensure_prebuilt_index_loaded()
    res = _load_json_cached(
        p,
        f"blk_bhs:{p}",
        postprocess=lambda d: d.get("blks", d) if isinstance(d, dict) else {},
        default={},
    )
    return res if isinstance(res, dict) else {}


def _load_json_cached(path: Path, cache_key: str, postprocess=None, default=None):
    if not path.is_file():
        return default
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return default
    cached = _INDEX_JSON_CACHE.get(cache_key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        res = postprocess(data) if postprocess is not None else data
        if res is not None:
            _INDEX_JSON_CACHE[cache_key] = (sig, res)
            return res
    except Exception:
        pass
    return default


def load_oz_map_index() -> dict[str, dict]:
    p = cfg.CACHE_DIR / "asset_map" / "oz_maps_index.json"
    if not p.is_file():
        ensure_prebuilt_index_loaded()
    res = _load_json_cached(
        p,
        f"oz_maps:{p}",
        postprocess=lambda d: _merge_streaming_layer_scenes(d["maps"]) if isinstance(d.get("maps"), dict) else {},
        default={},
    )
    return res if isinstance(res, dict) else {}


def load_oz_character_index() -> dict[str, dict]:
    p = cfg.CACHE_DIR / "asset_map" / "oz_characters_index.json"
    if not p.is_file():
        ensure_prebuilt_index_loaded()
    res = _load_json_cached(
        p,
        f"oz_chars:{p}",
        postprocess=lambda d: catalog.apply_variant_animations(d["characters"]) if isinstance(d.get("characters"), dict) else {},
        default={},
    )
    return res if isinstance(res, dict) else {}


def load_oz_entities_index() -> dict[str, dict[str, dict]]:
    p = cfg.CACHE_DIR / "asset_map" / "oz_entities_index.json"
    if not p.is_file():
        ensure_prebuilt_index_loaded()
    def _pp(data):
        out: dict[str, dict[str, dict]] = {"monsters": {}, "npcs": {}, "weapons": {}, "props": {}}
        if isinstance(data, dict):
            for k in out:
                if isinstance(data.get(k), dict):
                    out[k] = data[k]
        return out
    res = _load_json_cached(
        p,
        f"oz_entities:{p}",
        postprocess=_pp,
        default={"monsters": {}, "npcs": {}, "weapons": {}, "props": {}},
    )
    return res if isinstance(res, dict) else {"monsters": {}, "npcs": {}, "weapons": {}, "props": {}}


def load_oz_media_index() -> dict:
    p = cfg.CACHE_DIR / "asset_map" / "oz_media_index.json"
    if not p.is_file():
        ensure_prebuilt_index_loaded()
    res = _load_json_cached(
        p,
        f"oz_media:{p}",
        postprocess=lambda d: d if isinstance(d, dict) else {},
        default={},
    )
    return res if isinstance(res, dict) else {}


def load_font_blocks() -> list[str]:
    p = cfg.CACHE_DIR / "asset_map" / "fonts_index_auto.json"
    if not p.is_file():
        ensure_prebuilt_index_loaded()
    res = _load_json_cached(
        p,
        f"fonts:{p}",
        postprocess=lambda d: [str(b) for b in d.get("blocks", ()) if b] if isinstance(d.get("blocks"), list) else [],
        default=[],
    )
    return res if isinstance(res, list) else []


def load_shader_blocks() -> list[str]:
    p = cfg.CACHE_DIR / "asset_map" / "shaders_index_auto.json"
    if not p.is_file():
        ensure_prebuilt_index_loaded()
    res = _load_json_cached(
        p,
        f"shaders:{p}",
        postprocess=lambda d: [str(b) for b in d.get("blocks", ()) if b] if isinstance(d.get("blocks"), list) else [],
        default=[],
    )
    return res if isinstance(res, list) else []


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
                data = json.loads(cand.read_text(encoding="utf-8", errors="ignore"))
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
                data = json.loads(inv.read_text(encoding="utf-8", errors="ignore"))
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
            owners_data = json.loads(owners_p.read_text(encoding="utf-8", errors="ignore"))
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
            mm_data = json.loads(mm_p.read_text(encoding="utf-8", errors="ignore"))
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
                data = json.loads(vd.read_text(encoding="utf-8", errors="ignore"))
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
        data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except Exception:
        return {}


def load_texture_sections() -> dict[str, dict]:
    res = _load_json_cached(
        cfg.SECTIONS_INDEX_FILE,
        f"tex_sec:{cfg.SECTIONS_INDEX_FILE}",
        postprocess=lambda d: d if isinstance(d, dict) else {},
        default={},
    )
    return res if isinstance(res, dict) else {}


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
            data = json.loads(manifest.read_text(encoding="utf-8", errors="ignore"))
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
    # On zero-install or partial On-Demand CDN workspaces, unpack the bundled index snapshot automatically.
    blocks_dir = files_root / "Blocks"
    local_blks = list(blocks_dir.glob("*.blk"))[:200] if blocks_dir.is_dir() else []
    if cfg.is_cdn_workspace_root(game_root) or len(local_blks) < 200:
        if ensure_prebuilt_index_loaded():
            return True
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
            json.dumps({"characters": {k: {"blocks": sorted(v)} for k, v in sorted(chars_auto.items())}}, indent=2),
            encoding="utf-8",
        )
    if stages_auto:
        (map_dir / "stage_index_auto.json").write_text(
            json.dumps({"stages": {k: {"blocks": sorted(v)} for k, v in sorted(stages_auto.items())}}, indent=2),
            encoding="utf-8",
        )
    if font_blocks:
        (map_dir / "fonts_index_auto.json").write_text(
            json.dumps({"blocks": sorted(font_blocks)}, indent=2),
            encoding="utf-8",
        )
    if tex_prefix_blocks:
        compact_tex = {
            k: [b for b, _c in sorted(v.items(), key=lambda it: -it[1])[:256]]
            for k, v in sorted(tex_prefix_blocks.items())
        }
        (map_dir / "stage_prefix_blocks_auto.json").write_text(json.dumps(compact_tex), encoding="utf-8")
    if tex_key_blocks:
        compact_keys = {
            k: [b for b, _c in sorted(v.items(), key=lambda it: -it[1])]
            for k, v in sorted(tex_key_blocks.items())
        }
        (map_dir / "tex_key_blocks_auto.json").write_text(json.dumps(compact_keys), encoding="utf-8")
    if tex_noun_blocks:
        compact_nouns = {
            k: [b for b, _c in sorted(v.items(), key=lambda it: -it[1])[:12]]
            for k, v in sorted(tex_noun_blocks.items())
        }
        (map_dir / "tex_noun_blocks_auto.json").write_text(json.dumps(compact_nouns), encoding="utf-8")
    if tex_pathid:
        (map_dir / "tex_pathid_auto.json").write_text(json.dumps(tex_pathid), encoding="utf-8")
    blk_owners: dict[str, list] = {}
    for blk_name, pcounts in mesh_pref_counts.items():
        if not pcounts:
            continue
        top_pref, top_cnt = max(pcounts.items(), key=lambda it: it[1])
        tot_cnt = sum(pcounts.values())
        blk_owners[blk_name] = [top_pref, top_cnt, tot_cnt]
    if blk_owners:
        (map_dir / "stage_blk_owners_auto.json").write_text(json.dumps(blk_owners), encoding="utf-8")
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
        (map_dir / "stage_mat_mesh_blocks_auto.json").write_text(json.dumps(compact_mm), encoding="utf-8")
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
        pid_map = json.loads(pid_p.read_text(encoding="utf-8", errors="ignore"))
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
        ensure_prebuilt_index_loaded()
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
        data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
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
        ensure_prebuilt_index_loaded()
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
                dst.update(json.loads(fp.read_text(encoding="utf-8", errors="ignore")))
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


def _read_csharp_str(buf: bytes | memoryview, pos: int) -> tuple[str, int]:
    """Read a .NET BinaryReader 7-bit length-prefixed UTF-8 string."""
    ln = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        ln |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    s = bytes(buf[pos : pos + ln]).decode("utf-8", errors="replace")
    return s, pos + ln


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


def _compute_transitive_dep_blks(
    seed_cabs: Sequence[str],
    cab_to_blk: dict[str, str],
    cab_to_deps: dict[str, list[str]],
) -> list[str]:
    """Walk transitive CAB dependencies via BFS and return sorted unique `.blk` filenames."""
    visited = {str(c).lower() for c in seed_cabs if c}
    queue = list(visited)
    while queue:
        cab = queue.pop()
        for dep in cab_to_deps.get(cab, ()):
            dl = str(dep).lower()
            if dl and dl not in visited:
                visited.add(dl)
                queue.append(dl)
    return sorted({cab_to_blk[c] for c in visited if c in cab_to_blk})


def parse_nap_res_dat(raw: bytes) -> dict:
    """Parse raw `NapAssetBundleIndexAsset` (`Res.dat`) bytes into fast lookup tables and `.blk` resolver."""
    mv = memoryview(raw)
    n_len, = struct.unpack_from("<i", mv, 0)
    pos = 4 + max(0, n_len)
    if pos % 4:
        pos += 4 - (pos % 4)

    n_assets, = struct.unpack_from("<i", mv, pos)
    pos += 4
    assets_start = pos
    pos += n_assets * 12

    n_bundles, = struct.unpack_from("<i", mv, pos)
    pos += 4
    bundles_start = pos
    pos += n_bundles * 44

    n_blocks, = struct.unpack_from("<i", mv, pos)
    pos += 4
    blocks_start = pos
    pos += n_blocks * 9
    if pos % 4:
        pos += 4 - (pos % 4)

    n_children, = struct.unpack_from("<i", mv, pos)
    pos += 4
    children_start = pos

    blocks = [
        f"{bh}.blk"
        for bh, _loc in struct.iter_unpack("<QB", mv[blocks_start : blocks_start + n_blocks * 9])
    ]

    bundle_meta: list[tuple[int, int, int]] = []
    bhname_to_bundle: dict[int, int] = {}
    blk_off_to_bundle: dict[tuple[str, int], tuple[int, int]] = {}
    blk_to_bhnames: dict[str, list[int]] = {}
    for b_idx, (bi, bhname, _bhash, off, cs, ce, _u1, _u2, _fsz) in enumerate(
        struct.iter_unpack("<IQQIIIIII", mv[bundles_start : bundles_start + n_bundles * 44])
    ):
        bundle_meta.append((bi, cs, ce))
        bhname_to_bundle[bhname] = b_idx
        if bi < n_blocks:
            bname = blocks[bi]
            blk_off_to_bundle[(bname, off)] = (b_idx, bhname)
            blk_to_bhnames.setdefault(bname, []).append(bhname)

    ph_to_bundle: dict[int, int] = {}
    mask64 = (1 << 64) - 1
    for b_idx, ph in struct.iter_unpack("<Iq", mv[assets_start : assets_start + n_assets * 12]):
        ph_to_bundle[ph & mask64] = b_idx

    children_arr = mv[children_start : children_start + n_children * 4].cast("I")

    def resolve_blks(
        path_hashes: Sequence[int | str] = (),
        bundle_hashes: Sequence[int | str] = (),
        include_deps: bool = True,
    ) -> list[str]:
        visited_b: set[int] = set()
        queue: list[int] = []
        for k in path_hashes:
            if k is None or k == "":
                continue
            try:
                u = int(k) & mask64
            except ValueError:
                continue
            b_idx = ph_to_bundle.get(u)
            if b_idx is not None and b_idx < n_bundles and b_idx not in visited_b:
                visited_b.add(b_idx)
                queue.append(b_idx)
        for bh in bundle_hashes:
            if bh is None or bh == "":
                continue
            try:
                u = int(bh) & mask64
            except ValueError:
                continue
            b_idx = bhname_to_bundle.get(u)
            if b_idx is not None and b_idx < n_bundles and b_idx not in visited_b:
                visited_b.add(b_idx)
                queue.append(b_idx)
        if include_deps:
            while queue:
                cur_b = queue.pop()
                _, cs, ce = bundle_meta[cur_b]
                if ce <= cs or ce > n_children:
                    continue
                for ci in range(cs, ce):
                    dep_b = children_arr[ci]
                    if dep_b < n_bundles and dep_b not in visited_b:
                        visited_b.add(dep_b)
                        queue.append(dep_b)
        out_blks = {
            blocks[bundle_meta[vb][0]]
            for vb in visited_b
            if bundle_meta[vb][0] < n_blocks
        }
        return sorted(out_blks)

    return {
        "n_assets": n_assets,
        "n_bundles": n_bundles,
        "n_blocks": n_blocks,
        "n_children": n_children,
        "blocks": blocks,
        "ph_to_bundle": ph_to_bundle,
        "bhname_to_bundle": bhname_to_bundle,
        "blk_off_to_bundle": blk_off_to_bundle,
        "blk_to_bhnames": blk_to_bhnames,
        "resolve_blks": resolve_blks,
    }


def extract_nap_res_dat(blk_path: Path, cache_file: Path | None = None) -> bytes | None:
    """Extract raw `NapAssetBundleIndexAsset` (`Res.dat`) bytes from `2837526243.blk` via AnimeStudio.CLI."""
    if cache_file is not None and cache_file.is_file() and cache_file.stat().st_size > 1_000_000:
        return cache_file.read_bytes()
    if not blk_path.is_file() or not cfg.can_run_cli():
        return None
    tmp_dir = Path(tempfile.mkdtemp(prefix="oz_res_dat_"))
    try:
        res = subprocess.run(
            cfg.build_cli_cmd([
                cfg.to_cli_path(blk_path),
                cfg.to_cli_path(tmp_dir),
                "--game", "ZZZ",
                "--types", "NapAssetBundleIndexAsset",
                "--export_type", "Raw",
            ]),
            cwd=str(cfg.CLI_DIR),
            env=cfg.build_cli_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **cfg.subprocess_no_window_kwargs(),
        )
        if res.returncode != 0:
            return None
        dat_file = next((p for p in tmp_dir.rglob("*.dat") if p.is_file()), None)
        if dat_file is None:
            return None
        raw = dat_file.read_bytes()
        if cache_file is not None and len(raw) > 1_000_000:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_c = cache_file.with_suffix(".dat.tmp")
            tmp_c.write_bytes(raw)
            os.replace(tmp_c, cache_file)
        return raw
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _read_cab_map_entries(bin_path: Path) -> tuple[str, dict[str, tuple[str, int, list[str]]]]:
    """Read an existing `Maps/ZZZ.bin` into `(base_win, {cab_low: (blk_rel, offset, deps)})`."""
    raw = bin_path.read_bytes()
    mv = memoryview(raw)
    pos = 0
    base_win, pos = _read_csharp_str(mv, pos)
    count, = struct.unpack_from("<i", mv, pos)
    pos += 4
    entries: dict[str, tuple[str, int, list[str]]] = {}
    for _ in range(count):
        cab, pos = _read_csharp_str(mv, pos)
        blk_rel, pos = _read_csharp_str(mv, pos)
        off, dep_cnt = struct.unpack_from("<qi", mv, pos)
        pos += 12
        deps: list[str] = []
        for _ in range(dep_cnt):
            d, pos = _read_csharp_str(mv, pos)
            deps.append(d)
        entries[cab.lower()] = (blk_rel, off, deps)
    return base_win, entries


def _retarget_cab_map_base(cab_bin: Path, target_base_win: str) -> bool:
    """Rewrite only the leading `BaseFolder` string of `Maps/ZZZ.bin` cleanly in ~15 ms."""
    if not cab_bin.is_file():
        return False
    raw = cab_bin.read_bytes()
    cur_base, after_pos = _read_csharp_str(raw, 0)
    if cur_base.lower() == target_base_win.lower():
        return True
    tmp_bin = cab_bin.with_suffix(".bin.tmp")
    with tmp_bin.open("wb") as f:
        _write_csharp_str(f, target_base_win)
        f.write(memoryview(raw)[after_pos:])
    os.replace(tmp_bin, cab_bin)
    return True


def _ws_hash(files_root: Path) -> str:
    return hashlib.md5(str(files_root).encode("utf-8", errors="ignore")).hexdigest()[:12]


def _stash_active_cab_map_if_needed(target_files_root: Path) -> None:
    """Back up active `Maps/ZZZ.bin` into `CACHE_DIR/cab_maps` when switching workspaces so full-game CABMaps aren't lost."""
    cab_bin = cfg.CLI_DIR / "Maps" / "ZZZ.bin"
    cab_meta = cfg.CLI_DIR / "Maps" / "ZZZ.ozmeta.json"
    if not cab_bin.is_file() or not cab_meta.is_file():
        return
    try:
        meta = json.loads(cab_meta.read_text(encoding="utf-8", errors="ignore"))
        prev_root = str(meta.get("files_root") or "")
    except Exception:
        return
    if not prev_root or prev_root == str(target_files_root):
        return
    # Stash large/full or valid previous workspace CABMap cleanly
    stash_dir = cfg.CACHE_DIR / "cab_maps"
    stash_dir.mkdir(parents=True, exist_ok=True)
    h = _ws_hash(Path(prev_root))
    shutil.copy2(cab_bin, stash_dir / f"{h}.bin")
    shutil.copy2(cab_meta, stash_dir / f"{h}.ozmeta.json")


def _try_restore_stashed_cab_map(target_files_root: Path, common_base: Path) -> bool:
    """Restore a previously stashed `ZZZ.bin` for `target_files_root` in milliseconds if present."""
    stash_dir = cfg.CACHE_DIR / "cab_maps"
    h = _ws_hash(target_files_root)
    st_bin = stash_dir / f"{h}.bin"
    st_meta = stash_dir / f"{h}.ozmeta.json"
    if not st_bin.is_file() or not st_meta.is_file():
        return False
    _stash_active_cab_map_if_needed(target_files_root)
    cab_bin = cfg.CLI_DIR / "Maps" / "ZZZ.bin"
    cab_meta = cfg.CLI_DIR / "Maps" / "ZZZ.ozmeta.json"
    cab_bin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(st_bin, cab_bin)
    shutil.copy2(st_meta, cab_meta)
    _retarget_cab_map_base(cab_bin, cfg.to_cli_path(common_base))
    return True


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
    precomputed_prio: dict[str, tuple[int, float]] | None = None,
    merge_prebuilt: bool = False,
) -> tuple[int, int, int]:
    """Parse lightweight AnimeStudio AS_DUMP_OZ_INDEX JSONL shard(s) -> OZ v5 indexes & Maps/ZZZ.bin."""
    paths = [jsonl_paths] if isinstance(jsonl_paths, Path) else list(jsonl_paths)
    files_root = cfg.resolve_files_root(game_root)
    if precomputed_prio is None:
        _, _, precomputed_prio = cfg.collect_ordered_blks_with_prio(game_root)
    order_map = cfg.load_res_version_order(files_root) if not precomputed_prio else {}
    prio_cache: dict[str, tuple[int, float]] = {}

    def _blk_prio(blk_rel: str) -> tuple[int, float]:
        cached = prio_cache.get(blk_rel)
        if cached is not None:
            return cached
        norm = blk_rel.replace("\\", "/")
        rel_in_blocks = norm.split("Blocks/", 1)[-1] if "Blocks/" in norm else norm
        base = Path(rel_in_blocks).name
        hit = (
            precomputed_prio.get(norm)
            or precomputed_prio.get(rel_in_blocks)
            or precomputed_prio.get("Blocks/" + rel_in_blocks)
            or precomputed_prio.get(base)
        )
        if hit is not None:
            prio_cache[blk_rel] = hit
            return hit
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
    lods_by_folder: dict[str, list[tuple[str, str]]] = {}
    lods_by_folder_seen: dict[str, set[tuple[str, str]]] = {}
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
                    if atype in ("GameObject", "Animator") and aname:
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
                    if aname and key:
                        if "_Ani_" in aname:
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
                        else:
                            wm = _WENGINE_ANI_RE.match(aname)
                            if wm:
                                emap = ent_anis.setdefault(("weapons", wm.group(1)), {})
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
                        mp_low = mesh_path.lower()
                        if "/discretemeshassets/" in mp_low:
                            folder = mp_low.split("/discretemeshassets/", 1)[1].split("/", 1)[0]
                            if folder:
                                pair = (go_name, mesh_path)
                                fseen = lods_by_folder_seen.setdefault(folder, set())
                                if pair not in fseen:
                                    fseen.add(pair)
                                    lods_by_folder.setdefault(folder, []).append(pair)

                elif k == "font":
                    media_collector.font_blks.add(blk)

                elif k == "shader":
                    media_collector.shader_blks.add(blk)

    cab_to_blk = {
        cl: Path(blk_rel.replace("\\", "/")).name
        for cl, (_, _, blk_rel, _, _) in cab_entries.items()
    }
    cab_to_deps = {
        cl: [str(d).lower() for d in (deps or ())]
        for cl, (_, _, _, _, deps) in cab_entries.items()
    }

    scenes_ci = {s.lower(): s for s in scenes}
    paired_scenes: set[str] = set()
    maps_dict: dict[str, dict] = {}

    for mname in sorted(streams):
        _, m_blk, m_cab, _, prefab_dir, sectors = streams[mname]
        stream_blks: list[str] = []
        stream_blk_set: set[str] = set()
        stream_cabs: list[str] = []
        stream_cab_set: set[str] = set()
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
                p_cab = hit[2]
                if p_blk not in stream_blk_set:
                    stream_blk_set.add(p_blk)
                    stream_blks.append(p_blk)
                if p_cab and p_cab not in stream_cab_set:
                    stream_cab_set.add(p_cab)
                    stream_cabs.append(p_cab)
        entry: dict = {
            "streaming": {
                "manifest_blk": m_blk,
                "manifest_cab": m_cab,
                "blks": stream_blks,
                "cabs": stream_cabs,
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
    for _mname, m_entry in maps_dict.items():
        m_seed_cabs: list[str] = []
        bs = m_entry.get("base_scene")
        if isinstance(bs, dict) and bs.get("cab"):
            m_seed_cabs.append(str(bs["cab"]))
        for es in m_entry.get("extra_scenes") or ():
            if isinstance(es, dict) and es.get("cab"):
                m_seed_cabs.append(str(es["cab"]))
        st = m_entry.get("streaming")
        if isinstance(st, dict):
            if st.get("manifest_cab"):
                m_seed_cabs.append(str(st["manifest_cab"]))
            m_seed_cabs.extend(str(c) for c in (st.get("cabs") or ()) if c)
        m_entry["dep_blks"] = _compute_transitive_dep_blks(m_seed_cabs, cab_to_blk, cab_to_deps)

    ani_primary_by_norm: dict[str, dict[str, tuple[tuple[int, float], str, str]]] = {}
    ani_default_by_norm: dict[str, dict[str, tuple[tuple[int, float], str, str]]] = {}
    for src_map, dst_norm in ((char_anis_primary, ani_primary_by_norm), (char_anis_default, ani_default_by_norm)):
        for cbase, cmap in src_map.items():
            dst = dst_norm.setdefault(_norm_char_key(cbase), {})
            for aname, aval in cmap.items():
                prev_a = dst.get(aname)
                if prev_a is None or aval[0] < prev_a[0]:
                    dst[aname] = aval

    cab_name_by_low = {cl: cab for cl, (_, cab, _, _, _) in cab_entries.items()}
    cab_mesh_counts_ci = {(blk.lower(), cab.lower()): mc for (blk, cab), mc in cab_mesh_counts.items()}

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
        slot_rank = 2 if slot == "base" else (1 if slot == "model" else 0)
        score = (
            1 if non_weapon else 0,
            len(non_weapon),
            len(valid_lods),
            1 if emb > 0 else 0,
            emb,
            slot_rank,
            -bp[0],
            -bp[1],
        )
        return score, lods, emb

    def _resolve_model_entry(cname: str, cands: dict, all_cands: dict, ani_map: dict, allow_variant_fallback: bool = True) -> dict | None:
        best_tuple = None
        for slot in ("base", "model", "fallback"):
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
                for oslot in ("base", "model", "fallback"):
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
        if not chosen_lods:
            for fkey in (pname.lower(), f"{cname.lower()}_model"):
                if fkey in lods_by_folder:
                    chosen_lods = lods_by_folder[fkey]
                    break

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
            pname_core = re.sub(r"_(?:Model|Fighting|MainCity|UI|NPC)$", "", pname)
            prefixes = tuple(dict.fromkeys((
                f"SeparateMesh_{cname}_Model_",
                f"SeparateMesh_{cname}_",
                f"SeparateMesh_{pname_core}_Model_",
                f"SeparateMesh_{pname_core}_",
            )))
            short_cands = []
            for base_tok in (cname, pname_core):
                if base_tok.startswith(("Weapon_", "Device_", "Item_")):
                    continue
                stripped = re.sub(r"^Avatar_(?:Male|Female)_Size\d+_", "", base_tok)
                if len(stripped) >= 3 and stripped not in short_cands:
                    short_cands.append(stripped)
                tail_tok = base_tok.rsplit("_", 1)[-1]
                if len(tail_tok) >= 3 and tail_tok not in short_cands:
                    short_cands.append(tail_tok)
            short_prefixes = tuple(
                pf for sc in short_cands for pf in (f"{sc}_Body", f"{sc}_Face", f"{sc}_Hair", f"{sc}_Neck", f"{sc}_Bag")
            )
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

        if not extra_cabs and emb_meshes == 0 and p_cab:
            for dcl in cab_to_deps.get(p_cab.lower(), ()):
                m_blk_bn = cab_to_blk.get(dcl)
                if not m_blk_bn:
                    continue
                m_blk_full = f"Blocks/{m_blk_bn}"
                mc = (
                    cab_mesh_counts_ci.get((m_blk_full.lower(), dcl), 0)
                    or cab_mesh_counts_ci.get((m_blk_bn.lower(), dcl), 0)
                )
                if mc > 0:
                    orig_cab = cab_name_by_low.get(dcl, dcl)
                    if orig_cab not in extra_cab_set and orig_cab != p_cab:
                        extra_cab_set.add(orig_cab)
                        extra_cabs.append(orig_cab)
                    if m_blk_full not in mesh_blk_set:
                        mesh_blk_set.add(m_blk_full)
                        mesh_blks.append(m_blk_full)

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

        dep_blks = _compute_transitive_dep_blks([p_cab, *extra_cabs], cab_to_blk, cab_to_deps)
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
            "anim_count": len(ani_map),
            "dep_blks": dep_blks,
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
        cands_c = char_cands[cname]
        if "base" not in cands_c and "model" not in cands_c:
            continue
        ani_map = _collect_char_anis(cname)
        rec = _resolve_model_entry(cname, cands_c, char_cands, ani_map, allow_variant_fallback=True)
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

    if merge_prebuilt and PREBUILT_INDEX_ZIP.is_file():
        pre_maps = (_read_prebuilt_json("oz_maps_index.json").get("maps") or {})
        for mk, mv in pre_maps.items():
            if not isinstance(mv, dict):
                continue
            if mk not in maps_dict:
                maps_dict[mk] = mv
            elif mv.get("bundle_hashes") and not maps_dict[mk].get("bundle_hashes"):
                maps_dict[mk]["bundle_hashes"] = mv["bundle_hashes"]

        pre_chars = (_read_prebuilt_json("oz_characters_index.json").get("characters") or {})
        for ck, cv in pre_chars.items():
            if not isinstance(cv, dict):
                continue
            if ck not in chars_dict:
                chars_dict[ck] = cv
            elif cv.get("bundle_hashes") and not chars_dict[ck].get("bundle_hashes"):
                chars_dict[ck]["bundle_hashes"] = cv["bundle_hashes"]

        pre_ents = _read_prebuilt_json("oz_entities_index.json")
        for cat_k, dst_cat in entities_out.items():
            src_cat = pre_ents.get(cat_k)
            if not isinstance(src_cat, dict):
                continue
            for ek, ev in src_cat.items():
                if not isinstance(ev, dict):
                    continue
                if ek not in dst_cat:
                    dst_cat[ek] = ev
                elif ev.get("bundle_hashes") and not dst_cat[ek].get("bundle_hashes"):
                    dst_cat[ek]["bundle_hashes"] = ev["bundle_hashes"]

        pre_media = _read_prebuilt_json("oz_media_index.json")
        if isinstance(pre_media, dict):
            for section_k in ("spine", "ui_sprites", "data_packs"):
                pre_sec = pre_media.get(section_k)
                cur_sec = media_dict.get(section_k)
                if isinstance(pre_sec, dict) and isinstance(cur_sec, dict):
                    for sk, sv in pre_sec.items():
                        cur_sec.setdefault(sk, sv)

        pre_fonts = _read_prebuilt_json("fonts_index_auto.json").get("blocks") or []
        font_seen = set(sorted_fonts)
        for fb in pre_fonts:
            if fb and fb not in font_seen:
                font_seen.add(fb)
                sorted_fonts.append(fb)

        pre_shaders = _read_prebuilt_json("shaders_index_auto.json").get("blocks") or []
        sh_seen = set(sorted_shaders)
        for sb in pre_shaders:
            if sb and sb not in sh_seen:
                sh_seen.add(sb)
                sorted_shaders.append(sb)

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
        _stash_active_cab_map_if_needed(files_root)
        base_win = cfg.to_cli_path(common_base)
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

    validate_and_repair_indexes(
        map_dir=map_dir,
        game_root=game_root,
        fix=True,
        log_cb=log_cb,
        allow_cli_scan=False,
    )
    invalidate_index_cache()
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
        elif cur_type == "AnimationClip" and cur_name:
            if "_Ani_" in cur_name:
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
            else:
                wm = _WENGINE_ANI_RE.match(cur_name)
                if wm:
                    ent_anis.setdefault(("weapons", wm.group(1)), []).append((cur_name, blk, cur_cont))

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

    validate_and_repair_indexes(
        map_dir=mdir,
        game_root=cfg.GAME_ROOT,
        fix=True,
        log_cb=log_cb,
        allow_cli_scan=False,
    )
    try:
        repaired_ents = json.loads((mdir / "oz_entities_index.json").read_text(encoding="utf-8", errors="ignore"))
        for cat_k in entities_final:
            if isinstance(repaired_ents.get(cat_k), dict):
                entities_final[cat_k] = repaired_ents[cat_k]
    except Exception:
        pass
    summary = {
        "monsters": len(entities_final["monsters"]),
        "npcs": len(entities_final["npcs"]),
        "weapons": len(entities_final["weapons"]),
        "props": len(entities_final["props"]),
        "spine": len(media_dict["spine"]),
        "ui_sprite_packs": {k: v["count"] for k, v in media_dict["ui_sprites"].items()},
        "shaders": len(sorted_shaders),
    }
    invalidate_index_cache()
    cfg.release_os_memory()
    _emit(log_cb, f"Supplementary OZ v4 indexes ready: {summary}")
    return summary


def _blk_rel_for_state(p: Path, common_base: Path) -> str:
    try:
        return p.resolve().relative_to(common_base).as_posix()
    except Exception:
        return p.name


def _merge_incremental_records(
    old_records: Path,
    shard_jsonls: list[Path],
    unchanged_rels: set[str],
    out_records: Path,
) -> None:
    """Write combined JSONL rows keeping unchanged .blk entries plus newly scanned shard outputs."""
    tmp_out = out_records.with_suffix(".jsonl.tmp")
    unchanged_low = {r.lower() for r in unchanged_rels}
    with tmp_out.open("w", encoding="utf-8", errors="ignore") as fout:
        if old_records.is_file() and unchanged_low:
            with old_records.open("r", encoding="utf-8", errors="ignore") as fin:
                for line in fin:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        blk_val = json.loads(s).get("blk", "")
                    except Exception:
                        continue
                    if str(blk_val).replace("\\", "/").lower() in unchanged_low:
                        fout.write(s + "\n")
        for sj in shard_jsonls:
            if not sj.is_file():
                continue
            with sj.open("r", encoding="utf-8", errors="ignore") as fin:
                for line in fin:
                    s = line.strip()
                    if s:
                        fout.write(s + "\n")
    os.replace(tmp_out, out_records)


def build_asset_map(
    game_root: Path,
    log_cb=None,
    progress_cb=None,
    use_cli_assetmap: bool = True,
    cancel_event=None,
    force_full: bool = False,
    max_workers: int | None = None,
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
                **cfg.subprocess_no_window_kwargs(),
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                _emit(log_cb, line.rstrip("\n"))
            ran_scripts.append({"script": script_name, "rc": proc.wait()})

    _prog(25, "Scanning .blk containers (res_version_persist + APK priority)...")
    common_base, blk_files, precomputed_prio = cfg.collect_ordered_blks_with_prio(game_root, use_cache=False)
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
    records_cache_file = map_dir / "oz_index_records.jsonl"
    state_cache_file = map_dir / "oz_index_blk_state.json"

    prev_ver = 0
    if manifest_file.is_file():
        try:
            prev_ver = int(json.loads(manifest_file.read_text(encoding="utf-8", errors="ignore")).get("oz_index_version", 0))
        except Exception:
            prev_ver = 0
    cab_bin = cfg.CLI_DIR / "Maps" / "ZZZ.bin"
    cab_meta = cfg.CLI_DIR / "Maps" / "ZZZ.ozmeta.json"
    need_cab = not cab_bin.is_file()
    if not need_cab:
        try:
            cm = json.loads(cab_meta.read_text(encoding="utf-8", errors="ignore")) if cab_meta.is_file() else {}
            if cm.get("version", 0) < 2 or cm.get("files_root") != str(files_root) or cm.get("blk_count") != len(blk_files):
                need_cab = True
        except Exception:
            need_cab = True

    cur_blk_state: dict[str, list[int]] = {}
    for p in blk_files:
        rel = _blk_rel_for_state(p, common_base)
        try:
            st = p.stat()
            cur_blk_state[rel] = [st.st_size, st.st_mtime_ns]
        except OSError:
            cur_blk_state[rel] = [0, 0]

    delta_blks: list[Path] = blk_files
    unchanged_rels: set[str] = set()
    can_incremental = (
        not force_full
        and records_cache_file.is_file()
        and state_cache_file.is_file()
    )
    if can_incremental:
        try:
            prev_state_doc = json.loads(state_cache_file.read_text(encoding="utf-8", errors="ignore"))
            if (
                prev_state_doc.get("version") == 1
                and prev_state_doc.get("files_root") == str(files_root)
                and isinstance(prev_state_doc.get("blks"), dict)
            ):
                prev_blks = prev_state_doc["blks"]
                delta_blks = []
                for p in blk_files:
                    rel = _blk_rel_for_state(p, common_base)
                    if prev_blks.get(rel) == cur_blk_state.get(rel):
                        unchanged_rels.add(rel)
                    else:
                        delta_blks.append(p)
        except Exception:
            delta_blks = blk_files
            unchanged_rels = set()

    if (
        use_cli_assetmap
        and (cancel_event is None or not cancel_event.is_set())
        and blk_files
        and cfg.can_run_cli()
        and (
            force_full
            or bool(delta_blks)
            or not oz_maps_file.is_file()
            or not oz_chars_file.is_file()
            or not oz_ents_file.is_file()
            or not oz_media_file.is_file()
            or prev_ver < 5
            or need_cab
        )
    ):
        if records_cache_file.is_file() and not delta_blks and len(unchanged_rels) == len(blk_files):
            _prog(86, f"All {len(blk_files)} .blk containers unchanged; rebuilding indexes from cached records...")
            auto_chars, auto_stages, auto_fonts = _build_oz_indexes_from_jsonl(
                records_cache_file,
                map_dir,
                game_root,
                common_base=common_base,
                write_cab_map=True,
                total_blk_count=len(blk_files),
                log_cb=log_cb,
                precomputed_prio=precomputed_prio,
                merge_prebuilt=cfg.is_cdn_workspace_root(game_root),
            )
        else:
            scan_blks = blk_files if (force_full or not unchanged_rels) else delta_blks
            w_cap = max(1, int(max_workers)) if max_workers is not None else 6
            workers = min(cfg.adaptive_worker_count("index", max_workers=w_cap), len(scan_blks))
            mode_tag = (
                f"incremental ({len(scan_blks)} changed / {len(unchanged_rels)} cached)"
                if (len(scan_blks) < len(blk_files) and unchanged_rels)
                else f"{len(scan_blks)} .blk files"
            )
            _prog(35, f"Indexing {mode_tag} across {workers} AnimeStudio worker(s)...")
            shard_lists: list[Path] = []
            shard_jsonls: list[Path] = []
            for wi in range(workers):
                sblks = scan_blks[wi::workers]
                sl = map_dir / f"_oz_shard_{wi}.blklist"
                sj = map_dir / f"_oz_shard_{wi}.jsonl"
                sl.write_text(
                    "\n".join(cfg.to_cli_path(p) for p in sblks) + "\n",
                    encoding="utf-8",
                )
                shard_lists.append(sl)
                shard_jsonls.append(sj)

            done_counts = [0] * workers
            done_lock = threading.Lock()
            oz_re = re.compile(r"\[OzIndex\]\s+(\d+)/(\d+)")
            procs: list[subprocess.Popen] = []

            def _run_shard(wi: int):
                env = cfg.build_cli_env({
                    "AS_DUMP_OZ_INDEX": cfg.to_cli_path(shard_jsonls[wi]),
                    "AS_OZ_INDEX_BASE_DIR": cfg.to_cli_path(common_base),
                })
                cmd = cfg.build_cli_cmd([
                    cfg.to_cli_path(shard_lists[wi]),
                    cfg.to_cli_path(map_dir),
                    "--game", "ZZZ",
                ])
                proc = subprocess.Popen(
                    cmd, cwd=str(cfg.CLI_DIR), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
                    **cfg.subprocess_no_window_kwargs(),
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
                        pct = 35 + int(50 * tot_done / max(1, len(scan_blks)))
                        _prog(pct, f"AnimeStudio Index [{tot_done}/{len(scan_blks)}]")
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
                    _prog(86, "Merging incremental cache & building OZ v5 indexes + Maps/ZZZ.bin...")
                    _merge_incremental_records(
                        records_cache_file,
                        shard_jsonls,
                        unchanged_rels if len(scan_blks) < len(blk_files) else set(),
                        records_cache_file,
                    )
                    state_cache_file.write_text(
                        json.dumps({"version": 1, "files_root": str(files_root), "blks": cur_blk_state}),
                        encoding="utf-8",
                    )
                    auto_chars, auto_stages, auto_fonts = _build_oz_indexes_from_jsonl(
                        records_cache_file,
                        map_dir,
                        game_root,
                        common_base=common_base,
                        write_cab_map=True,
                        total_blk_count=len(blk_files),
                        log_cb=log_cb,
                        precomputed_prio=precomputed_prio,
                        merge_prebuilt=cfg.is_cdn_workspace_root(game_root),
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
            json.dumps({"characters": chars_fallback}, indent=2),
            encoding="utf-8",
        )
    if not (cfg.EXTRACTOR_ROOT / "data" / "all_maps_inventory.json").is_file():
        (map_dir / "stage_index_fallback.json").write_text(
            json.dumps({"stages": stages_fallback}, indent=2),
            encoding="utf-8",
        )

    was_cancelled = bool(cancel_event is not None and cancel_event.is_set())
    if not was_cancelled and oz_chars_file.is_file():
        validate_and_repair_indexes(
            map_dir=map_dir,
            game_root=game_root,
            fix=True,
            log_cb=log_cb,
            allow_cli_scan=True,
        )
    if oz_chars_file.is_file() and oz_maps_file.is_file():
        try:
            d_c = json.loads(oz_chars_file.read_text(encoding="utf-8", errors="ignore"))
            auto_chars = len(d_c.get("characters", d_c))
            d_m = json.loads(oz_maps_file.read_text(encoding="utf-8", errors="ignore"))
            auto_stages = len(d_m.get("maps", d_m))
        except Exception:
            pass
    prev_good = (oz_chars_file.is_file() and oz_maps_file.is_file() and auto_chars > 0 and auto_stages > 0)

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
        "user_skipped": False if prev_good else was_cancelled,
        "ran_scripts": ran_scripts,
        "built_at": int(time.time()),
    }
    (map_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    invalidate_index_cache()
    cfg.release_os_memory()
    _prog(
        100,
        f"Asset map ready: {len(blk_files)} .blk, {len(pck_files)} .pck, "
        f"{auto_chars or len(chars_fallback)} characters, {auto_stages or len(stages_fallback)} stages",
    )
    return manifest


def sync_ondemand_blks_index(
    game_root: Path,
    blk_paths: Sequence[Path] = (),
    log_cb=None,
) -> bool:
    """Incrementally index newly downloaded/local `.blk` files and sync `Maps/ZZZ.bin` for `game_root`."""
    files_root = cfg.resolve_files_root(game_root)
    map_dir = cfg.CACHE_DIR / "asset_map"
    map_dir.mkdir(parents=True, exist_ok=True)
    common_base, ordered_blks, precomputed_prio = cfg.collect_ordered_blks_with_prio(game_root, use_cache=False)

    cand_map: dict[Path, Path] = {}
    for p in ordered_blks:
        if p.is_file() and p.name != NAP_INDEX_BLK_NAME:
            try:
                cand_map[p.resolve()] = p
            except Exception:
                cand_map[p] = p
    for bp in blk_paths:
        if bp and bp.is_file() and bp.name != NAP_INDEX_BLK_NAME:
            try:
                cand_map[bp.resolve()] = bp
            except Exception:
                cand_map[bp] = bp

    scan_candidates = list(cand_map.values())
    if not scan_candidates:
        return False

    main_state_file = map_dir / "oz_index_blk_state.json"
    use_main_state = False
    if main_state_file.is_file() and not cfg.is_cdn_workspace_root(game_root):
        try:
            st_doc = json.loads(main_state_file.read_text(encoding="utf-8", errors="ignore"))
            if st_doc.get("files_root") == str(files_root):
                use_main_state = True
        except Exception:
            pass

    if use_main_state:
        ws_dir = map_dir
        records_file = map_dir / "oz_index_records.jsonl"
        state_file = main_state_file
    else:
        ws_dir = cfg.CACHE_DIR / "workspaces" / _ws_hash(files_root)
        ws_dir.mkdir(parents=True, exist_ok=True)
        records_file = ws_dir / "oz_index_records.jsonl"
        state_file = ws_dir / "oz_index_blk_state.json"

    cur_blk_state: dict[str, list[int]] = {}
    delta_blks: list[Path] = list(scan_candidates)
    unchanged_rels: set[str] = set()
    for p in scan_candidates:
        rel = _blk_rel_for_state(p, common_base)
        try:
            st = p.stat()
            cur_blk_state[rel] = [st.st_size, st.st_mtime_ns]
        except OSError:
            cur_blk_state[rel] = [0, 0]

    if records_file.is_file() and state_file.is_file():
        try:
            prev_doc = json.loads(state_file.read_text(encoding="utf-8", errors="ignore"))
            if prev_doc.get("version") == 1 and prev_doc.get("files_root") == str(files_root):
                prev_blks = prev_doc.get("blks") or {}
                delta_blks = []
                for p in scan_candidates:
                    rel = _blk_rel_for_state(p, common_base)
                    if prev_blks.get(rel) == cur_blk_state.get(rel):
                        unchanged_rels.add(rel)
                    else:
                        delta_blks.append(p)
        except Exception:
            delta_blks = list(scan_candidates)
            unchanged_rels = set()

    cab_bin = cfg.CLI_DIR / "Maps" / "ZZZ.bin"
    cab_meta = cfg.CLI_DIR / "Maps" / "ZZZ.ozmeta.json"
    active_matches = False
    meta_blk_count = 0
    if cab_bin.is_file() and cab_meta.is_file():
        try:
            meta_doc = json.loads(cab_meta.read_text(encoding="utf-8", errors="ignore"))
            active_matches = meta_doc.get("files_root") == str(files_root)
            meta_blk_count = int(meta_doc.get("blk_count") or 0)
        except Exception:
            active_matches = False

    # If ZZZ.bin already covers all local .blks for this root and no incremental state cache exists yet, skip full rescan.
    if (
        active_matches
        and not cfg.is_cdn_workspace_root(game_root)
        and not state_file.is_file()
        and meta_blk_count >= len(scan_candidates)
    ):
        return _retarget_cab_map_base(cab_bin, cfg.to_cli_path(common_base))

    if not delta_blks:
        if active_matches:
            return _retarget_cab_map_base(cab_bin, cfg.to_cli_path(common_base))
        if _try_restore_stashed_cab_map(files_root, common_base):
            return True
        if records_file.is_file():
            _build_oz_indexes_from_jsonl(
                records_file,
                map_dir,
                game_root,
                common_base=common_base,
                write_cab_map=True,
                total_blk_count=len(scan_candidates),
                log_cb=log_cb,
                precomputed_prio=precomputed_prio,
                merge_prebuilt=True,
            )
            return True
        return False

    if not cfg.can_run_cli():
        return False

    _emit(log_cb, f"[Index] Scanning {len(delta_blks)} newly available .blk container(s) -> Maps/ZZZ.bin...")
    workers = 1 if len(delta_blks) < 8 else min(cfg.adaptive_worker_count("index", max_workers=6), len(delta_blks))
    shard_lists: list[Path] = []
    shard_jsonls: list[Path] = []
    for wi in range(workers):
        sblks = delta_blks[wi::workers]
        sl = ws_dir / f"_ondemand_shard_{wi}.blklist"
        sj = ws_dir / f"_ondemand_shard_{wi}.jsonl"
        sl.write_text("\n".join(cfg.to_cli_path(p) for p in sblks) + "\n", encoding="utf-8")
        shard_lists.append(sl)
        shard_jsonls.append(sj)

    def _run_shard(wi: int):
        env = cfg.build_cli_env({
            "AS_DUMP_OZ_INDEX": cfg.to_cli_path(shard_jsonls[wi]),
            "AS_OZ_INDEX_BASE_DIR": cfg.to_cli_path(common_base),
        })
        cmd = cfg.build_cli_cmd([
            cfg.to_cli_path(shard_lists[wi]),
            cfg.to_cli_path(ws_dir),
            "--game", "ZZZ",
        ])
        proc = subprocess.run(
            cmd,
            cwd=str(cfg.CLI_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **cfg.subprocess_no_window_kwargs(),
        )
        return proc.returncode

    try:
        if workers == 1:
            _run_shard(0)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(_run_shard, range(workers)))

        _merge_incremental_records(
            records_file,
            shard_jsonls,
            unchanged_rels,
            records_file,
        )
        state_file.write_text(
            json.dumps({"version": 1, "files_root": str(files_root), "blks": cur_blk_state}),
            encoding="utf-8",
        )
        _build_oz_indexes_from_jsonl(
            records_file,
            map_dir,
            game_root,
            common_base=common_base,
            write_cab_map=True,
            total_blk_count=len(scan_candidates),
            log_cb=log_cb,
            precomputed_prio=precomputed_prio,
            merge_prebuilt=True,
        )
    finally:
        for sl in shard_lists:
            sl.unlink(missing_ok=True)
        for sj in shard_jsonls:
            sj.unlink(missing_ok=True)
    return cab_bin.is_file()


def _write_json_atomic(path: Path, obj: dict, indent: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(obj, indent=indent), encoding="utf-8")
    os.replace(tmp, path)


def _norm_blk_ref(b: str) -> str:
    s = str(b or "").replace("\\", "/").strip()
    if not s:
        return ""
    low = s.lower()
    if low.startswith("blocks/"):
        return "Blocks/" + s[7:]
    if "/blocks/" in low:
        return "Blocks/" + s[low.rfind("/blocks/") + 8:]
    return f"Blocks/{Path(s).name}"


def _blk_bn(b: str) -> str:
    return Path(str(b or "").replace("\\", "/")).name


def _is_zero_mesh_rec(rec: dict) -> bool:
    return not bool(
        (rec.get("mesh_blks") or ())
        or (rec.get("extra_cabs") or ())
        or int(rec.get("embedded_meshes") or 0) > 0
    )


def _scan_blks_to_jsonl(
    blk_paths: Sequence[Path],
    common_base: Path,
    work_dir: Path,
    max_workers: int | None = None,
) -> list[dict]:
    """Run targeted `AS_DUMP_OZ_INDEX` over a small `.blk` list and return parsed JSONL rows."""
    valid_blks: list[Path] = [p for p in blk_paths if p and p.is_file() and p.name != NAP_INDEX_BLK_NAME]
    if not valid_blks or not cfg.can_run_cli():
        return []
    workers = 1 if len(valid_blks) < 8 else min(max_workers or cfg.adaptive_worker_count("index", max_workers=6), len(valid_blks))
    shard_lists: list[Path] = []
    shard_jsonls: list[Path] = []
    rows: list[dict] = []
    for wi in range(workers):
        sblks = valid_blks[wi::workers]
        sl = work_dir / f"_val_shard_{wi}.blklist"
        sj = work_dir / f"_val_shard_{wi}.jsonl"
        sl.write_text("\n".join(cfg.to_cli_path(p) for p in sblks) + "\n", encoding="utf-8")
        shard_lists.append(sl)
        shard_jsonls.append(sj)

    def _run_s(wi: int):
        env = cfg.build_cli_env({
            "AS_DUMP_OZ_INDEX": cfg.to_cli_path(shard_jsonls[wi]),
            "AS_OZ_INDEX_BASE_DIR": cfg.to_cli_path(common_base),
        })
        cmd = cfg.build_cli_cmd([
            cfg.to_cli_path(shard_lists[wi]),
            cfg.to_cli_path(work_dir),
            "--game", "ZZZ",
        ])
        subprocess.run(
            cmd,
            cwd=str(cfg.CLI_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **cfg.subprocess_no_window_kwargs(),
        )

    try:
        if workers == 1:
            _run_s(0)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(_run_s, range(workers)))
        for sj in shard_jsonls:
            if not sj.is_file():
                continue
            with sj.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        rows.append(json.loads(s))
                    except Exception:
                        continue
    finally:
        for sl in shard_lists:
            sl.unlink(missing_ok=True)
        for sj in shard_jsonls:
            sj.unlink(missing_ok=True)
    return rows


def validate_and_repair_indexes(
    map_dir: Path | None = None,
    game_root: Path | None = None,
    fix: bool = True,
    log_cb=None,
    allow_cli_scan: bool = True,
) -> dict:
    """Audit active OZ indexes and Maps/ZZZ.bin for missing meshes, leaked animations/colliders, stubs, and metadata drift."""
    mdir = map_dir or (cfg.CACHE_DIR / "asset_map")
    groot = game_root or cfg.GAME_ROOT
    files_root = cfg.resolve_files_root(groot) if groot else None

    chars_p = mdir / "oz_characters_index.json"
    ents_p = mdir / "oz_entities_index.json"
    maps_p = mdir / "oz_maps_index.json"
    compat_chars_p = mdir / "character_index_auto.json"
    compat_stages_p = mdir / "stage_index_auto.json"
    bhs_p = mdir / "oz_blk_bundle_hashes.json"

    repaired: dict[str, int] = {
        "cab_map_retargeted": 0,
        "entity_leaks_removed": 0,
        "wengine_anims_folded": 0,
        "prop_anims_folded": 0,
        "aliases_merged": 0,
        "chars_repaired_models": 0,
        "entities_repaired_mesh_deps": 0,
        "stubs_pruned": 0,
        "dep_blks_fixed": 0,
        "bundle_hashes_fixed": 0,
        "anim_counts_fixed": 0,
        "maps_merged": 0,
        "compat_synced": 0,
    }
    report: dict = {
        "map_dir": str(mdir),
        "checked": {},
        "repaired": repaired,
        "pruned_keys": [],
        "remaining_issues": {},
    }

    chars_doc: dict = {}
    chars_dict: dict[str, dict] = {}
    if chars_p.is_file():
        try:
            chars_doc = json.loads(chars_p.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(chars_doc.get("characters"), dict):
                chars_dict = chars_doc["characters"]
        except Exception:
            chars_doc = {}

    ents_doc: dict = {}
    if ents_p.is_file():
        try:
            ents_doc = json.loads(ents_p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            ents_doc = {}

    maps_doc: dict = {}
    maps_dict: dict[str, dict] = {}
    if maps_p.is_file():
        try:
            maps_doc = json.loads(maps_p.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(maps_doc.get("maps"), dict):
                maps_dict = maps_doc["maps"]
        except Exception:
            maps_doc = {}

    blk_bhs_map: dict[str, list[str]] = {}
    if bhs_p.is_file():
        try:
            bd = json.loads(bhs_p.read_text(encoding="utf-8", errors="ignore"))
            blk_bhs_map = bd.get("blks", bd) if isinstance(bd, dict) else {}
        except Exception:
            blk_bhs_map = {}

    common_base: Path | None = None
    precomputed_prio: dict[str, tuple[int, float]] = {}
    if groot and groot.exists():
        common_base, _, precomputed_prio = cfg.collect_ordered_blks_with_prio(groot, use_cache=True)

    cab_bin = cfg.CLI_DIR / "Maps" / "ZZZ.bin"
    cab_to_blk: dict[str, str] = {}
    cab_to_deps: dict[str, list[str]] = {}
    cab_name_by_low: dict[str, str] = {}
    cab_to_off: dict[str, tuple[str, int]] = {}
    if cab_bin.is_file():
        try:
            cur_base, raw_cabs = _read_cab_map_entries(cab_bin)
            if fix and common_base is not None:
                target_base = cfg.to_cli_path(common_base)
                if cur_base.lower() != target_base.lower():
                    if _retarget_cab_map_base(cab_bin, target_base):
                        repaired["cab_map_retargeted"] += 1
            for cl, (blk_rel, off, deps) in raw_cabs.items():
                bn = _blk_bn(blk_rel)
                cab_to_blk[cl] = bn
                cab_to_off[cl] = (bn, off)
                cab_to_deps[cl] = [str(d).lower() for d in (deps or ())]
                cab_name_by_low[cl] = f"CAB-{cl[4:]}" if cl.startswith("cab-") else cl
        except Exception:
            pass

    dirty_chars = False
    dirty_ents = False
    dirty_maps = False

    def _fold_anim_into(parent: dict, src: dict) -> bool:
        changed_a = False
        ablks = list(parent.get("anim_blks") or [])
        ab_set = {_norm_blk_ref(b).lower(): _norm_blk_ref(b) for b in ablks if b}
        ablks = list(ab_set.values())
        for raw_b in [src.get("prefab_blk", ""), *(src.get("anim_blks") or ())]:
            nb = _norm_blk_ref(raw_b)
            if nb and nb.lower() not in ab_set:
                ab_set[nb.lower()] = nb
                ablks.append(nb)
                changed_a = True
        akeys = list(parent.get("anim_keys") or [])
        ak_set = set(akeys)
        for raw_k in [src.get("prefab_key", ""), *(src.get("anim_keys") or ())]:
            sk = str(raw_k or "").strip()
            if sk and sk not in ak_set:
                ak_set.add(sk)
                akeys.append(sk)
                changed_a = True
        if changed_a:
            parent["anim_blks"] = ablks
            parent["anim_keys"] = akeys
            parent["anim_count"] = max(int(parent.get("anim_count") or 0), len(akeys))
        return changed_a

    # Pass 1: Remove leaked animations, colliders, and state wrappers from entities (folding anims to parent)
    if fix and isinstance(ents_doc, dict):
        weapons = ents_doc.get("weapons")
        if isinstance(weapons, dict):
            for k in list(weapons):
                v = weapons.get(k)
                if not isinstance(v, dict) or not _is_zero_mesh_rec(v):
                    continue
                wm = _WENGINE_ANI_RE.match(k)
                if wm or "_Ani_" in k:
                    parent_k = wm.group(1) if wm else k.split("_Ani_", 1)[0]
                    if parent_k in weapons and isinstance(weapons[parent_k], dict):
                        _fold_anim_into(weapons[parent_k], v)
                        repaired["wengine_anims_folded"] += 1
                    weapons.pop(k, None)
                    repaired["entity_leaks_removed"] += 1
                    report["pruned_keys"].append(f"weapons:{k}")
                    dirty_ents = True
        props = ents_doc.get("props")
        if isinstance(props, dict):
            for k in list(props):
                v = props.get(k)
                if not isinstance(v, dict) or not _is_zero_mesh_rec(v):
                    continue
                kl = k.lower()
                is_leak = (
                    "_Ani_" in k
                    or kl.endswith((
                        "collider", "collier", "_effect", "_fx",
                        "_maincity", "_npc", "_ui", "_timeline", "_transform", "_fighting",
                    ))
                )
                if is_leak:
                    if "_Ani_" in k:
                        parent_k = k.split("_Ani_", 1)[0]
                        if parent_k in props and isinstance(props[parent_k], dict):
                            _fold_anim_into(props[parent_k], v)
                            repaired["prop_anims_folded"] += 1
                    props.pop(k, None)
                    repaired["entity_leaks_removed"] += 1
                    report["pruned_keys"].append(f"props:{k}")
                    dirty_ents = True
        for cat_k in ("monsters", "npcs"):
            bucket = ents_doc.get(cat_k)
            if isinstance(bucket, dict):
                for k in list(bucket):
                    v = bucket.get(k)
                    if not isinstance(v, dict) or not _is_zero_mesh_rec(v):
                        continue
                    if "_Ani_" in k:
                        parent_k = k.split("_Ani_", 1)[0]
                        if parent_k in bucket and isinstance(bucket[parent_k], dict):
                            _fold_anim_into(bucket[parent_k], v)
                        bucket.pop(k, None)
                        repaired["entity_leaks_removed"] += 1
                        report["pruned_keys"].append(f"{cat_k}:{k}")
                        dirty_ents = True

    # Pass 2: Merge duplicate character alias keys safely
    if fix and chars_dict:
        explicit_dups = [
            ("Avatar_Female_Size02_Seed_Cockpit", "Avatar_Female_Size02_SeedCockpit"),
            ("Avatar_Female_Size02_LindverneMainStory", "Avatar_Female_Size02_Lindverne"),
        ]
        by_cab_key: dict[tuple[str, str], list[str]] = {}
        for ck, cv in sorted(chars_dict.items()):
            if not isinstance(cv, dict):
                continue
            pcab = str(cv.get("prefab_cab") or "").lower()
            pkey = str(cv.get("prefab_key") or "")
            if pcab and pkey:
                by_cab_key.setdefault((pcab, pkey), []).append(ck)
        for group in by_cab_key.values():
            if len(group) >= 2:
                base_c = min(group, key=len)
                for dup_c in group:
                    if dup_c != base_c and dup_c.lower().startswith(base_c.lower()):
                        explicit_dups.append((dup_c, base_c))
        for dup_c, canon_c in explicit_dups:
            if dup_c in chars_dict and canon_c in chars_dict:
                _fold_anim_into(chars_dict[canon_c], chars_dict[dup_c])
                chars_dict.pop(dup_c, None)
                repaired["aliases_merged"] += 1
                report["pruned_keys"].append(f"char_alias:{dup_c}->{canon_c}")
                dirty_chars = True

    # Pass 3: Repair zero-mesh characters & entities using targeted .blk inspection + Maps/ZZZ.bin
    zero_chars = [ck for ck, cv in chars_dict.items() if isinstance(cv, dict) and _is_zero_mesh_rec(cv)]
    zero_ent_items: list[tuple[str, str, dict]] = []
    for cat_k in ("monsters", "npcs", "weapons", "props"):
        bucket = ents_doc.get(cat_k) if isinstance(ents_doc, dict) else None
        if isinstance(bucket, dict):
            for ek, ev in bucket.items():
                if isinstance(ev, dict) and _is_zero_mesh_rec(ev):
                    zero_ent_items.append((cat_k, ek, ev))

    if fix and (zero_chars or zero_ent_items) and allow_cli_scan and cfg.can_run_cli() and groot and common_base is not None:
        scan_blk_map: dict[str, Path] = {}

        def _add_blk_cand(blk_ref: str):
            bn = _blk_bn(blk_ref)
            if not bn or bn == NAP_INDEX_BLK_NAME or bn.lower() in scan_blk_map:
                return
            rp = cfg.resolve_blk_path(groot, bn)
            if rp is not None and rp.is_file():
                scan_blk_map[bn.lower()] = rp

        # Zero-mesh characters + their family/base siblings
        char_stems = set()
        for ck in zero_chars:
            rec = chars_dict[ck]
            _add_blk_cand(rec.get("prefab_blk", ""))
            for b in (rec.get("dep_blks") or ()):
                _add_blk_cand(b)
            for b in (rec.get("anim_blks") or ()):
                _add_blk_cand(b)
            m_st = re.match(r"^(Avatar_(?:Male|Female)_Size\d+_[A-Z][a-z]+)", ck)
            if m_st:
                char_stems.add(m_st.group(1))
        for ck, cv in chars_dict.items():
            if not isinstance(cv, dict):
                continue
            if any(ck.startswith(st) for st in char_stems) or ck in ("Avatar_Female_Size02_Seed", "Avatar_Female_Size03_SigridTraversing"):
                _add_blk_cand(cv.get("prefab_blk", ""))
                for b in (cv.get("mesh_blks") or ()):
                    _add_blk_cand(b)
                for b in (cv.get("dep_blks") or ())[:8]:
                    _add_blk_cand(b)

        # Zero-mesh entities (direct CAB deps + prefab_blk)
        for cat_k, _ek, ev in zero_ent_items:
            _add_blk_cand(ev.get("prefab_blk", ""))
            pcab = str(ev.get("prefab_cab") or "").lower()
            for dc in cab_to_deps.get(pcab, ()):
                dbn = cab_to_blk.get(dc)
                if dbn:
                    _add_blk_cand(dbn)
            if cat_k in ("monsters", "npcs") and len(scan_blk_map) < 350:
                for b in (ev.get("dep_blks") or ())[:6]:
                    _add_blk_cand(b)
            if len(scan_blk_map) >= 380:
                break

        res_info: dict | None = None
        idx_blk = cfg.resolve_blk_path(groot, NAP_INDEX_BLK_NAME)
        if idx_blk is not None:
            try:
                raw_res = extract_nap_res_dat(idx_blk, cache_file=cfg.CACHE_DIR / "cdn_manifests" / "local_res.dat")
                if raw_res:
                    res_info = parse_nap_res_dat(raw_res)
            except Exception:
                res_info = None

        scan_rows: list[dict] = []
        if scan_blk_map:
            tmp_scan = Path(tempfile.mkdtemp(prefix="oz_val_"))
            try:
                scan_rows.extend(_scan_blks_to_jsonl(list(scan_blk_map.values()), common_base, tmp_scan))

                def _blk_prio_val(bref: str) -> tuple[int, float]:
                    norm = bref.replace("\\", "/")
                    rel_b = norm.split("Blocks/", 1)[-1] if "Blocks/" in norm else norm
                    bn = Path(rel_b).name
                    hit = (
                        precomputed_prio.get(norm)
                        or precomputed_prio.get(rel_b)
                        or precomputed_prio.get("Blocks/" + rel_b)
                        or precomputed_prio.get(bn)
                    )
                    return hit if hit is not None else (999999, 0.0)

                cab_mc: dict[tuple[str, str], int] = {}
                ab_by_key: dict[str, tuple[tuple[int, float], str, str, str, str]] = {}
                mesh_by_name: dict[str, tuple[tuple[int, float], str, str, str]] = {}
                mesh_by_name_ci: dict[str, tuple[tuple[int, float], str, str, str]] = {}
                lods_by_cab: dict[tuple[str, str], list[tuple[str, str]]] = {}
                lods_by_folder: dict[str, list[tuple[str, str]]] = {}
                lods_fseen: dict[str, set[tuple[str, str]]] = {}
                char_cands: dict[str, dict[str, list[tuple[tuple[int, float], str, str, str, str]]]] = {}

                def _ingest_rows(rows_in: list[dict]):
                    for r in rows_in:
                        rk = r.get("k")
                        rblk = str(r.get("blk") or "")
                        if not rblk:
                            continue
                        rbp = _blk_prio_val(rblk)
                        if rk == "cab":
                            rcab = str(r.get("cab") or "")
                            if rcab:
                                cl = rcab.lower()
                                mc = int(r.get("meshes") or 0)
                                cab_mc[(rblk.lower(), cl)] = max(cab_mc.get((rblk.lower(), cl), 0), mc)
                                cab_mc[(_blk_bn(rblk).lower(), cl)] = max(cab_mc.get((_blk_bn(rblk).lower(), cl), 0), mc)
                                if cl not in cab_to_blk:
                                    cab_to_blk[cl] = _blk_bn(rblk)
                                    cab_name_by_low[cl] = rcab
                                if cl not in cab_to_deps:
                                    cab_to_deps[cl] = [str(d).lower() for d in (r.get("deps") or ()) if d]
                        elif rk == "ab":
                            key = str(r.get("key") or "")
                            rcab = str(r.get("cab") or "")
                            atype = str(r.get("type") or "")
                            aname = str(r.get("name") or "")
                            if key:
                                prev = ab_by_key.get(key)
                                if prev is None or rbp < prev[0]:
                                    ab_by_key[key] = (rbp, rblk, rcab, atype, aname)
                            if atype in ("GameObject", "Animator") and aname:
                                cres = _resolve_char_model_key(aname)
                                if cres is not None:
                                    ckey, slot = cres
                                    char_cands.setdefault(ckey, {}).setdefault(slot, []).append((rbp, rblk, rcab, key, aname))
                            if atype == "Mesh" and aname:
                                prev_m = mesh_by_name.get(aname)
                                if prev_m is None or rbp < prev_m[0]:
                                    mesh_by_name[aname] = (rbp, rblk, rcab, key)
                                alow = aname.lower()
                                prev_ci = mesh_by_name_ci.get(alow)
                                if prev_ci is None or rbp < prev_ci[0]:
                                    mesh_by_name_ci[alow] = (rbp, rblk, rcab, key)
                        elif rk == "lod":
                            rcab = str(r.get("cab") or "")
                            go_n = str(r.get("go") or "")
                            mpath = str(r.get("mesh_path") or "")
                            if rcab and mpath:
                                lods_by_cab.setdefault((rblk.lower(), rcab.lower()), []).append((go_n, mpath))
                                mpl = mpath.lower()
                                if "/discretemeshassets/" in mpl:
                                    folder = mpl.split("/discretemeshassets/", 1)[1].split("/", 1)[0]
                                    if folder:
                                        pair = (go_n, mpath)
                                        fs = lods_fseen.setdefault(folder, set())
                                        if pair not in fs:
                                            fs.add(pair)
                                            lods_by_folder.setdefault(folder, []).append(pair)

                _ingest_rows(scan_rows)

                # Round 2: resolve any missing LOD .mesh path hashes to their .blk via Res.dat and scan those too
                if res_info is not None:
                    missing_hashes: list[str] = []
                    seen_mh: set[str] = set()
                    cand_lod_lists: list[tuple[str, str]] = []
                    for ck in zero_chars:
                        rec = chars_dict[ck]
                        for slot in ("base", "model", "fallback"):
                            for _rbp, rblk, rcab, _k, _pn in char_cands.get(ck, {}).get(slot, ()):
                                cand_lod_lists.extend(lods_by_cab.get((rblk.lower(), rcab.lower()), ()))
                        pname_l = str(rec.get("prefab_name") or "").lower()
                        for fkey in (pname_l, f"{ck.lower()}_model"):
                            cand_lod_lists.extend(lods_by_folder.get(fkey, ()))
                    for gn, mp in cand_lod_lists:
                        if "hairshadow" in gn.lower() or "hairshadow" in mp.lower() or gn.lower().endswith("_lod3") or "_lod3.mesh" in mp.lower():
                            continue
                        mh = str(xxh64(mp.lower().encode("utf-8"), 0))
                        if mh not in ab_by_key and mh not in seen_mh:
                            seen_mh.add(mh)
                            missing_hashes.append(mh)
                    if missing_hashes:
                        r2_blks: dict[str, Path] = {}
                        for bn in res_info["resolve_blks"](missing_hashes, include_deps=False):
                            if bn.lower() not in scan_blk_map:
                                rp = cfg.resolve_blk_path(groot, bn)
                                if rp is not None and rp.is_file():
                                    scan_blk_map[bn.lower()] = rp
                                    r2_blks[bn.lower()] = rp
                        if r2_blks:
                            r2_rows = _scan_blks_to_jsonl(list(r2_blks.values()), common_base, tmp_scan)
                            _ingest_rows(r2_rows)

                def _resolve_repair_entry(key_name: str, rec: dict, cand_slots: dict | None) -> bool:
                    changed_r = False
                    cur_blk = _norm_blk_ref(rec.get("prefab_blk", ""))
                    cur_cab = str(rec.get("prefab_cab") or "")
                    cur_key = str(rec.get("prefab_key") or "")
                    cur_pname = str(rec.get("prefab_name") or "")

                    chosen_lods: list[tuple[str, str]] = []
                    emb_m = int(rec.get("embedded_meshes") or 0)
                    if cand_slots:
                        best = None
                        for slot in ("base", "model", "fallback"):
                            for cand in cand_slots.get(slot, ()):
                                rbp, rblk, rcab, rkey, rpname = cand
                                clods = lods_by_cab.get((rblk.lower(), rcab.lower()), [])
                                valid_l = [
                                    (gn, mp) for gn, mp in clods
                                    if "hairshadow" not in gn.lower() and "hairshadow" not in mp.lower()
                                    and not gn.lower().endswith("_lod3") and "_lod3.mesh" not in mp.lower()
                                ]
                                non_w = [
                                    (gn, mp) for gn, mp in valid_l
                                    if "weapon" not in gn.lower() and "weapon" not in Path(mp).stem.lower()
                                ]
                                c_emb = cab_mc.get((rblk.lower(), rcab.lower()), 0) or cab_mc.get((_blk_bn(rblk).lower(), rcab.lower()), 0)
                                srank = 2 if slot == "base" else (1 if slot == "model" else 0)
                                sc = (1 if non_w else 0, len(non_w), len(valid_l), 1 if c_emb > 0 else 0, c_emb, srank, -rbp[0], -rbp[1])
                                if best is None or sc > best[0]:
                                    best = (sc, cand, clods, c_emb)
                        if best is not None and (best[2] or best[3] > 0):
                            _, (_rbp, rblk, rcab, rkey, rpname), chosen_lods, emb_m = best
                            nb = _norm_blk_ref(rblk)
                            if nb != cur_blk or rcab != cur_cab or rkey != cur_key or rpname != cur_pname:
                                rec["prefab_blk"] = nb
                                rec["prefab_cab"] = rcab
                                rec["prefab_key"] = rkey
                                rec["prefab_name"] = rpname
                                cur_blk, cur_cab, cur_key, cur_pname = nb, rcab, rkey, rpname
                                changed_r = True
                            if emb_m > int(rec.get("embedded_meshes") or 0):
                                rec["embedded_meshes"] = emb_m
                                changed_r = True

                    if not chosen_lods:
                        for fkey in (cur_pname.lower(), f"{key_name.lower()}_model"):
                            if fkey in lods_by_folder:
                                chosen_lods = lods_by_folder[fkey]
                                break

                    extra_cabs = list(rec.get("extra_cabs") or [])
                    extra_set = {c.lower() for c in extra_cabs if c}
                    mesh_blks = [_norm_blk_ref(b) for b in (rec.get("mesh_blks") or []) if b]
                    mesh_set = {b.lower() for b in mesh_blks}

                    for go_name, mp in chosen_lods:
                        gl = go_name.lower()
                        ml = mp.lower()
                        if "hairshadow" in gl or "hairshadow" in ml or gl.endswith("_lod3") or "_lod3.mesh" in ml:
                            continue
                        mh = str(xxh64(ml.encode("utf-8"), 0))
                        hit = ab_by_key.get(mh)
                        if hit is None:
                            stem = Path(mp).stem
                            hm = mesh_by_name.get(stem) or mesh_by_name_ci.get(stem.lower())
                            if hm is not None:
                                hit = (hm[0], hm[1], hm[2], "Mesh", stem)
                        if hit is not None:
                            _, m_blk, m_cab, _, _ = hit
                            m_blk_n = _norm_blk_ref(m_blk)
                            if m_cab and m_cab.lower() not in extra_set and m_cab.lower() != cur_cab.lower():
                                extra_set.add(m_cab.lower())
                                extra_cabs.append(m_cab)
                                changed_r = True
                            if m_blk_n and m_blk_n.lower() not in mesh_set:
                                mesh_set.add(m_blk_n.lower())
                                mesh_blks.append(m_blk_n)
                                changed_r = True

                    if not extra_cabs and int(rec.get("embedded_meshes") or 0) == 0:
                        p_core = re.sub(r"_(?:Model|Fighting|MainCity|UI|NPC)$", "", cur_pname)
                        pfs = tuple(dict.fromkeys((
                            f"SeparateMesh_{key_name}_Model_",
                            f"SeparateMesh_{key_name}_",
                            f"SeparateMesh_{p_core}_Model_",
                            f"SeparateMesh_{p_core}_",
                        )))
                        sc_list = []
                        for bt in (key_name, p_core):
                            if bt.startswith(("Weapon_", "Device_", "Item_")):
                                continue
                            s1 = re.sub(r"^Avatar_(?:Male|Female)_Size\d+_", "", bt)
                            if len(s1) >= 3 and s1 not in sc_list:
                                sc_list.append(s1)
                            s2 = bt.rsplit("_", 1)[-1]
                            if len(s2) >= 3 and s2 not in sc_list:
                                sc_list.append(s2)
                        short_pfs = tuple(
                            pf for sc in sc_list for pf in (f"{sc}_Body", f"{sc}_Face", f"{sc}_Hair", f"{sc}_Neck", f"{sc}_Bag")
                        )
                        for mname, (_, m_blk, m_cab, _) in sorted(mesh_by_name.items()):
                            is_s = any(mname.startswith(pf) for pf in pfs)
                            is_sh = bool(short_pfs and any(mname.startswith(pf) for pf in short_pfs))
                            if not (is_s or is_sh):
                                continue
                            low = mname.lower()
                            if low.endswith(("_lod1", "_lod2", "_lod3")) or "hairshadow" in low:
                                continue
                            m_blk_n = _norm_blk_ref(m_blk)
                            if m_cab and m_cab.lower() not in extra_set and m_cab.lower() != cur_cab.lower():
                                extra_set.add(m_cab.lower())
                                extra_cabs.append(m_cab)
                                changed_r = True
                            if m_blk_n and m_blk_n.lower() not in mesh_set:
                                mesh_set.add(m_blk_n.lower())
                                mesh_blks.append(m_blk_n)
                                changed_r = True

                    if not extra_cabs and int(rec.get("embedded_meshes") or 0) == 0 and cur_cab:
                        for dcl in cab_to_deps.get(cur_cab.lower(), ()):
                            mbn = cab_to_blk.get(dcl)
                            if not mbn:
                                continue
                            mc = cab_mc.get((mbn.lower(), dcl), 0) or cab_mc.get((f"blocks/{mbn.lower()}", dcl), 0)
                            if mc > 0:
                                orig_c = cab_name_by_low.get(dcl, dcl)
                                m_blk_n = f"Blocks/{mbn}"
                                if orig_c.lower() not in extra_set and dcl != cur_cab.lower():
                                    extra_set.add(orig_c.lower())
                                    extra_cabs.append(orig_c)
                                    changed_r = True
                                if m_blk_n.lower() not in mesh_set:
                                    mesh_set.add(m_blk_n.lower())
                                    mesh_blks.append(m_blk_n)
                                    changed_r = True

                    if changed_r:
                        rec["extra_cabs"] = extra_cabs
                        rec["mesh_blks"] = mesh_blks
                    return changed_r

                for ck in zero_chars:
                    rec = chars_dict[ck]
                    if _resolve_repair_entry(ck, rec, char_cands.get(ck)):
                        if not _is_zero_mesh_rec(rec):
                            repaired["chars_repaired_models"] += 1
                        dirty_chars = True

                for cat_k, ek, ev in zero_ent_items:
                    if _resolve_repair_entry(ek, ev, None):
                        if not _is_zero_mesh_rec(ev):
                            repaired["entities_repaired_mesh_deps"] += 1
                        dirty_ents = True
            finally:
                shutil.rmtree(tmp_scan, ignore_errors=True)

    # Pass 4: Prune verified empty VFX/region stubs & 0-mesh duplicate monster wrappers
    if fix and chars_dict:
        for ck in list(chars_dict):
            rec = chars_dict.get(ck)
            if not isinstance(rec, dict):
                continue
            if _is_zero_mesh_rec(rec) and not rec.get("anim_keys") and not rec.get("anim_blks"):
                chars_dict.pop(ck, None)
                repaired["stubs_pruned"] += 1
                report["pruned_keys"].append(f"char_stub:{ck}")
                dirty_chars = True
    if fix and isinstance(ents_doc, dict) and isinstance(ents_doc.get("monsters"), dict):
        mon = ents_doc["monsters"]
        if (
            "Monster_DeadEndButcher" in mon
            and isinstance(mon["Monster_DeadEndButcher"], dict)
            and _is_zero_mesh_rec(mon["Monster_DeadEndButcher"])
            and ("Monster_NotoriousDeadEndButcher" in mon or "Monster_EtherealDeadEndButcher" in mon)
        ):
            target_mon = mon.get("Monster_NotoriousDeadEndButcher") or mon.get("Monster_EtherealDeadEndButcher")
            if isinstance(target_mon, dict):
                _fold_anim_into(target_mon, mon["Monster_DeadEndButcher"])
            mon.pop("Monster_DeadEndButcher", None)
            repaired["stubs_pruned"] += 1
            report["pruned_keys"].append("monsters:Monster_DeadEndButcher")
            dirty_ents = True

    # Pass 5: Normalize paths, dep_blks, bundle_hashes, anim_counts, maps merge & compat sync
    def _reconcile_record(rec: dict, flag_ref: str) -> None:
        nonlocal dirty_chars, dirty_ents
        changed = False
        pb = _norm_blk_ref(rec.get("prefab_blk", ""))
        if pb != rec.get("prefab_blk", ""):
            rec["prefab_blk"] = pb
            changed = True
        for lk in ("mesh_blks", "anim_blks"):
            orig_l = list(rec.get(lk) or ())
            new_l: list[str] = []
            seen_l: set[str] = set()
            for item in orig_l:
                ni = _norm_blk_ref(item)
                if ni and ni.lower() not in seen_l:
                    seen_l.add(ni.lower())
                    new_l.append(ni)
            if new_l != orig_l:
                rec[lk] = new_l
                changed = True

        dep_set = {_blk_bn(d) for d in (rec.get("dep_blks") or ()) if d}
        before_n = len(dep_set)
        if pb:
            dep_set.add(_blk_bn(pb))
        for mb in rec.get("mesh_blks") or ():
            dep_set.add(_blk_bn(mb))
        if cab_to_blk:
            seeds = [rec.get("prefab_cab", ""), *(rec.get("extra_cabs") or ())]
            for b in _compute_transitive_dep_blks(seeds, cab_to_blk, cab_to_deps):
                dep_set.add(b)
        sorted_deps = sorted(dep_set)
        if sorted_deps != list(rec.get("dep_blks") or ()) or len(dep_set) != before_n:
            rec["dep_blks"] = sorted_deps
            changed = True
            repaired["dep_blks_fixed"] += 1

        if blk_bhs_map and not rec.get("bundle_hashes"):
            bhs: list[str] = []
            bhs_seen: set[str] = set()
            for candidate_b in [pb, *(rec.get("mesh_blks") or ()), *sorted_deps]:
                bn = _blk_bn(candidate_b).lower()
                for bh in blk_bhs_map.get(bn, ())[:4]:
                    if bh not in bhs_seen:
                        bhs_seen.add(bh)
                        bhs.append(bh)
                if bhs:
                    break
            if bhs:
                rec["bundle_hashes"] = bhs
                changed = True
                repaired["bundle_hashes_fixed"] += 1

        akeys = list(rec.get("anim_keys") or ())
        if "anim_count" in rec or akeys:
            cur_ac = int(rec.get("anim_count") or 0)
            target_ac = max(cur_ac, len(akeys)) if akeys else cur_ac
            if target_ac != rec.get("anim_count"):
                rec["anim_count"] = target_ac
                changed = True
                repaired["anim_counts_fixed"] += 1

        if changed:
            if flag_ref == "chars":
                dirty_chars = True
            else:
                dirty_ents = True

    if fix:
        for cv in chars_dict.values():
            if isinstance(cv, dict):
                _reconcile_record(cv, "chars")
        for cat_k in ("monsters", "npcs", "weapons", "props"):
            bucket = ents_doc.get(cat_k) if isinstance(ents_doc, dict) else None
            if isinstance(bucket, dict):
                for ev in bucket.values():
                    if isinstance(ev, dict):
                        _reconcile_record(ev, "ents")

        if maps_dict:
            before_m = len(maps_dict)
            _merge_streaming_layer_scenes(maps_dict)
            if len(maps_dict) != before_m:
                repaired["maps_merged"] += before_m - len(maps_dict)
                dirty_maps = True
            for m_entry in maps_dict.values():
                if not isinstance(m_entry, dict):
                    continue
                dep_s = {_blk_bn(d) for d in (m_entry.get("dep_blks") or ()) if d}
                bs = m_entry.get("base_scene")
                if isinstance(bs, dict) and bs.get("blk"):
                    dep_s.add(_blk_bn(bs["blk"]))
                for es in m_entry.get("extra_scenes") or ():
                    if isinstance(es, dict) and es.get("blk"):
                        dep_s.add(_blk_bn(es["blk"]))
                st = m_entry.get("streaming")
                if isinstance(st, dict):
                    if st.get("manifest_blk"):
                        dep_s.add(_blk_bn(st["manifest_blk"]))
                    for sb in st.get("blks") or ():
                        dep_s.add(_blk_bn(sb))
                nsdeps = sorted(dep_s)
                if nsdeps != list(m_entry.get("dep_blks") or ()):
                    m_entry["dep_blks"] = nsdeps
                    repaired["dep_blks_fixed"] += 1
                    dirty_maps = True

        if dirty_chars and chars_p.is_file():
            _write_json_atomic(chars_p, {"version": 4, "characters": chars_dict})
        if dirty_ents and ents_p.is_file():
            out_e = {"version": int(ents_doc.get("version") or 4)}
            for cat_k in ("monsters", "npcs", "weapons", "props"):
                out_e[cat_k] = ents_doc.get(cat_k) if isinstance(ents_doc.get(cat_k), dict) else {}
            _write_json_atomic(ents_p, out_e)
        if dirty_maps and maps_p.is_file():
            _write_json_atomic(maps_p, {"version": 5, "maps": maps_dict})

        if chars_dict and (dirty_chars or not compat_chars_p.is_file()):
            compat_c = {}
            for cn, info in chars_dict.items():
                if not isinstance(info, dict):
                    continue
                blks_c = []
                for b in [info.get("prefab_blk", ""), *(info.get("mesh_blks") or ()), *(info.get("anim_blks") or ())]:
                    if b and b not in blks_c:
                        blks_c.append(b)
                compat_c[cn] = {"blocks": blks_c}
            _write_json_atomic(compat_chars_p, {"characters": compat_c})
            repaired["compat_synced"] += 1

        if maps_dict and (dirty_maps or not compat_stages_p.is_file()):
            compat_m = {}
            for mn, info in maps_dict.items():
                if not isinstance(info, dict):
                    continue
                blks_m = []
                for b in (info.get("streaming") or {}).get("blks", ()):
                    if b and b not in blks_m:
                        blks_m.append(b)
                bs_blk = (info.get("base_scene") or {}).get("blk")
                if bs_blk and bs_blk not in blks_m:
                    blks_m.append(bs_blk)
                for es in info.get("extra_scenes") or ():
                    if isinstance(es, dict) and es.get("blk") and es["blk"] not in blks_m:
                        blks_m.append(es["blk"])
                compat_m[mn] = {"blocks": blks_m}
            _write_json_atomic(compat_stages_p, {"stages": compat_m})
            repaired["compat_synced"] += 1

        if any(v > 0 for v in repaired.values()):
            invalidate_index_cache()

    rem_zero_chars = [ck for ck, cv in chars_dict.items() if isinstance(cv, dict) and _is_zero_mesh_rec(cv)]
    rem_zero_ents = {
        cat_k: sum(
            1 for ev in (ents_doc.get(cat_k) or {}).values()
            if isinstance(ev, dict) and _is_zero_mesh_rec(ev)
        )
        for cat_k in ("monsters", "npcs", "weapons", "props")
        if isinstance(ents_doc, dict)
    }
    report["checked"] = {
        "characters": len(chars_dict),
        "monsters": len(ents_doc.get("monsters") or {}) if isinstance(ents_doc, dict) else 0,
        "npcs": len(ents_doc.get("npcs") or {}) if isinstance(ents_doc, dict) else 0,
        "weapons": len(ents_doc.get("weapons") or {}) if isinstance(ents_doc, dict) else 0,
        "props": len(ents_doc.get("props") or {}) if isinstance(ents_doc, dict) else 0,
        "maps": len(maps_dict),
    }
    report["remaining_issues"] = {
        "zero_mesh_characters": rem_zero_chars,
        "zero_mesh_entities": rem_zero_ents,
    }
    _emit(
        log_cb,
        f"[Validate] Checked {report['checked']['characters']} chars, {report['checked']['maps']} maps | "
        f"repaired: chars={repaired['chars_repaired_models']}, leaks_removed={repaired['entity_leaks_removed']}, "
        f"wengine_anims={repaired['wengine_anims_folded']}, stubs_pruned={repaired['stubs_pruned']}, "
        f"ent_deps={repaired['entities_repaired_mesh_deps']}, remaining_zero_chars={len(rem_zero_chars)}",
    )
    return report


def bundle_prebuilt_index_snapshot(out_zip: Path | None = None) -> Path:
    """Enrich active JSON indexes with `dep_blks` & cross-platform `bundle_hashes` and pack `prebuilt_index.zip`."""
    map_dir = cfg.CACHE_DIR / "asset_map"
    validate_and_repair_indexes(
        map_dir=map_dir,
        game_root=cfg.GAME_ROOT,
        fix=True,
        log_cb=None,
        allow_cli_scan=False,
    )
    target_zip = out_zip or PREBUILT_INDEX_ZIP
    target_zip.parent.mkdir(parents=True, exist_ok=True)

    cab_bin = cfg.CLI_DIR / "Maps" / "ZZZ.bin"
    cab_to_blk: dict[str, str] = {}
    cab_to_deps: dict[str, list[str]] = {}
    cab_to_off: dict[str, tuple[str, int]] = {}
    if cab_bin.is_file():
        _, raw_cabs = _read_cab_map_entries(cab_bin)
        for cl, (blk_rel, off, deps) in raw_cabs.items():
            bn = Path(blk_rel.replace("\\", "/")).name
            cab_to_blk[cl] = bn
            cab_to_off[cl] = (bn, off)
            cab_to_deps[cl] = [str(d).lower() for d in (deps or ())]

    res_info: dict | None = None
    idx_blk = cfg.resolve_blk_path(cfg.GAME_ROOT, NAP_INDEX_BLK_NAME)
    if idx_blk is not None:
        raw_res = extract_nap_res_dat(idx_blk, cache_file=cfg.CACHE_DIR / "cdn_manifests" / "local_res.dat")
        if raw_res:
            res_info = parse_nap_res_dat(raw_res)

    cab_to_bh: dict[str, str] = {}
    blk_to_bhnames: dict[str, list[int]] = {}
    if res_info is not None:
        blk_off_to_bundle = res_info["blk_off_to_bundle"]
        blk_to_bhnames = res_info["blk_to_bhnames"]
        for cl, (bn, off) in cab_to_off.items():
            hit = blk_off_to_bundle.get((bn, off))
            if hit is not None:
                cab_to_bh[cl] = str(hit[1])

    def _bhs_for_cabs(cabs: Sequence[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for c in cabs:
            if not c:
                continue
            bh = cab_to_bh.get(str(c).lower())
            if bh and bh not in seen:
                seen.add(bh)
                out.append(bh)
        return out

    # 1. Enrich oz_characters_index.json
    chars_p = map_dir / "oz_characters_index.json"
    if chars_p.is_file():
        cdoc = json.loads(chars_p.read_text(encoding="utf-8", errors="ignore"))
        cdict = cdoc.get("characters") or {}
        for rec in cdict.values():
            if not isinstance(rec, dict):
                continue
            seed_cabs = [rec.get("prefab_cab", ""), *(rec.get("extra_cabs") or ())]
            if cab_to_blk:
                rec["dep_blks"] = _compute_transitive_dep_blks(seed_cabs, cab_to_blk, cab_to_deps)
            bhs = _bhs_for_cabs(seed_cabs)
            if bhs:
                rec["bundle_hashes"] = bhs
        chars_p.write_text(json.dumps(cdoc, indent=2), encoding="utf-8")

    # 2. Enrich oz_entities_index.json
    ents_p = map_dir / "oz_entities_index.json"
    if ents_p.is_file():
        edoc = json.loads(ents_p.read_text(encoding="utf-8", errors="ignore"))
        for cat_k in ("monsters", "npcs", "weapons", "props"):
            bucket = edoc.get(cat_k)
            if not isinstance(bucket, dict):
                continue
            for rec in bucket.values():
                if not isinstance(rec, dict):
                    continue
                seed_cabs = [rec.get("prefab_cab", ""), *(rec.get("extra_cabs") or ())]
                if cab_to_blk:
                    rec["dep_blks"] = _compute_transitive_dep_blks(seed_cabs, cab_to_blk, cab_to_deps)
                bhs = _bhs_for_cabs(seed_cabs)
                if bhs:
                    rec["bundle_hashes"] = bhs
        ents_p.write_text(json.dumps(edoc, indent=2), encoding="utf-8")

    # 3. Enrich oz_maps_index.json
    maps_p = map_dir / "oz_maps_index.json"
    if maps_p.is_file():
        mdoc = json.loads(maps_p.read_text(encoding="utf-8", errors="ignore"))
        mdict = mdoc.get("maps") or {}
        for m_entry in mdict.values():
            if not isinstance(m_entry, dict):
                continue
            seed_cabs = []
            bs = m_entry.get("base_scene")
            if isinstance(bs, dict) and bs.get("cab"):
                seed_cabs.append(str(bs["cab"]))
            for es in m_entry.get("extra_scenes") or ():
                if isinstance(es, dict) and es.get("cab"):
                    seed_cabs.append(str(es["cab"]))
            st = m_entry.get("streaming")
            if isinstance(st, dict) and st.get("manifest_cab"):
                seed_cabs.append(str(st["manifest_cab"]))
            # Also capture Streaming sector CABs if present in ZZZ.bin for map dep_blks
            all_stream_cabs = list(seed_cabs)
            if isinstance(st, dict):
                for c in st.get("cabs") or ():
                    if c:
                        all_stream_cabs.append(str(c))
            if cab_to_blk:
                m_entry["dep_blks"] = _compute_transitive_dep_blks(all_stream_cabs, cab_to_blk, cab_to_deps)
            bhs = _bhs_for_cabs(all_stream_cabs)
            if bhs:
                m_entry["bundle_hashes"] = bhs
        maps_p.write_text(json.dumps(mdoc, indent=2), encoding="utf-8")

    # 4. Build oz_blk_bundle_hashes.json for non-CAB block lists (media, fonts, shaders, stage textures)
    if blk_to_bhnames:
        ref_blks: set[str] = set()
        for fname in (
            "fonts_index_auto.json",
            "shaders_index_auto.json",
            "stage_prefix_blocks_auto.json",
            "stage_mat_mesh_blocks_auto.json",
            "tex_key_blocks_auto.json",
            "tex_noun_blocks_auto.json",
            "stage_index_auto.json",
        ):
            fp = map_dir / fname
            if fp.is_file():
                try:
                    raw_obj = json.loads(fp.read_text(encoding="utf-8", errors="ignore"))
                    stack = [raw_obj]
                    while stack:
                        cur = stack.pop()
                        if isinstance(cur, dict):
                            stack.extend(cur.values())
                        elif isinstance(cur, list):
                            stack.extend(cur)
                        elif isinstance(cur, str) and cur.lower().endswith(".blk"):
                            ref_blks.add(Path(cur.replace("\\", "/")).name)
                except Exception:
                    pass
        media_p = map_dir / "oz_media_index.json"
        if media_p.is_file():
            try:
                mdata = json.loads(media_p.read_text(encoding="utf-8", errors="ignore"))
                for sec in ("spine", "ui_sprites", "data_packs"):
                    for item in (mdata.get(sec) or {}).values():
                        if isinstance(item, dict):
                            for b in item.get("blks") or ():
                                ref_blks.add(Path(str(b).replace("\\", "/")).name)
            except Exception:
                pass
        sec_p = cfg.SECTIONS_INDEX_FILE
        if sec_p.is_file():
            try:
                sdata = json.loads(sec_p.read_text(encoding="utf-8", errors="ignore"))
                for item in sdata.values():
                    if isinstance(item, dict):
                        for b in item.get("blocks") or ():
                            ref_blks.add(Path(str(b).replace("\\", "/")).name)
            except Exception:
                pass
        out_bhs: dict[str, list[str]] = {}
        for bn in sorted(ref_blks):
            bh_list = blk_to_bhnames.get(bn)
            if bh_list:
                if len(bh_list) > 24:
                    step = max(1, len(bh_list) // 24)
                    sampled = bh_list[::step][:24]
                else:
                    sampled = bh_list
                out_bhs[bn.lower()] = [str(h) for h in sampled]
        (map_dir / "oz_blk_bundle_hashes.json").write_text(
            json.dumps({"version": 1, "blks": out_bhs}),
            encoding="utf-8",
        )

    tmp_zip = target_zip.with_suffix(".zip.tmp")
    skip_names = {"zzz_assets_map.json", "oz_index_blk_state.json", "manifest.json"}
    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for jp in sorted(map_dir.glob("*.json")):
            if jp.name in skip_names:
                continue
            zf.write(jp, arcname=jp.name)
    os.replace(tmp_zip, target_zip)
    invalidate_index_cache()
    return target_zip
