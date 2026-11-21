"""Character / map classification layered on top of the OZ asset indexes.

Everything here is derived from data already present in ``oz_characters_index.json`` and
``oz_maps_index.json``, and is non-destructive: it only labels, groups, and filters entries,
maps internal ZZZ codenames to recognizable character names, lets skin/outfit variants
inherit their base character's animation clips, and filters curated maps cleanly.
"""
from __future__ import annotations

import collections
import re
from pathlib import Path

_CHAR_RE = re.compile(r"^Avatar_(Male|Female)_Size(\d+)_(.+)$")
_BANGBOO_RE = re.compile(r"^(?:Avatar_)?Bangboo_(.+)$")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_GLUED_WORD_FIXES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bIrisofthe\b"), "Iris of the"),
    (re.compile(r"\bTrailsof\b"), "Trails of"),
    (re.compile(r"\bOfthe\b"), "of the"),
    (re.compile(r"\bAndthe\b"), "and the"),
)

# Proxies (non-combat protagonist rigs) vs explicit alt-form character families.
CHAR_PROXY_STEMS = frozenset({"Belle", "Wise"})
CHAR_ALT_FORM_PARENTS: dict[str, str] = {
    "SilverAnby": "Anbi",
    "SPBilly": "Billy",
    "AriaRobot": "AriaIdol",
}
_COMPANION_TOKENS = frozenset({
    "anastacia", "drusilla", "wood", "kuriaki", "kuriakiweapon",
    "cat", "pot", "ghost", "case", "motor", "maozi",
    "yuansymedium", "summonedruthlessfiend", "windeye", "windregion",
})
# Name suffixes of model parts that belong to a base character but are not outfits.
_ATTACHMENT_SUFFIXES = frozenset({"Weapon", "Cockpit", "Floater", "Mecha", "Vehicle", "Sr"})
_ATTACHMENT_TOKENS = frozenset({
    "weapon", "cockpit", "floater", "mecha", "vehicle", "sr",
    "disc", "hand", "leg", "handl", "handr", "legl", "legr",
})

# Verified internal ZZZ codename -> display name mappings.
CHAR_CODENAME_ALIASES: dict[str, str] = {
    "Anbi": "Anby",
    "Aokaku": "Soukaku",
    "Clara": "Koleda",
    "Lisa": "Grace",
    "Longinus": "Soldier 11",
    "Nostradamus": "Nicole",
    "OokumaMari": "Piper",
    "Tsubaki": "Nekomata",
    "Unagi": "Miyabi",
    "Brujas": "Orphie & Magus",
    "Norano": "Komano Manato",
    "Zhenzhen": "Ye Shunguang",
    "SilverAnby": "Soldier 0 - Anby",
    "SPBilly": "Billy (SP)",
    "QingYi": "Qingyi",
    "YiXuan": "Yixuan",
    "Jufufu": "Ju Fufu",
    "XiaoZhao": "Zhao",
    "BanYue": "Banyue",
    "PanYinhu": "Pan Yinhu",
    "NangongYu": "Nangong Yu",
    "HugoVlad": "Hugo Vlad",
    "JaneDoe": "Jane Doe",
    "ZhuYuan": "Zhu Yuan",
}

# Characters that ship at least this many animation clips own a full gameplay animation set.
FULL_ANIM_THRESHOLD = 90

CHAR_KIND_LABELS = {
    "character": "Playable agent",
    "proxy": "Proxy",
    "alt_form": "Alt form",
    "variant": "Skin / outfit",
    "companion": "Companion / summon",
    "bangboo": "Bangboo",
    "attachment": "Weapon / vehicle model",
    "limited": "Limited animations",
    "model": "Model only",
    "stub": "Empty stub",
}

CHAR_PLAYABLE_KINDS = frozenset({"character", "proxy", "alt_form", "variant", "bangboo"})

MAP_CATEGORY_LABELS = {
    "open": "Open area (streaming)",
    "hub": "Hub / city",
    "combat": "Combat stage",
    "boss": "Boss arena",
    "zero": "Zero Abyss / Complex room",
    "npc": "NPC worksite",
    "vr": "VR / training",
    "minigame": "Minigame",
    "internal": "Internal / test",
}

# Curated map categories shown by default; internal/minigame/VR/NPC/Zero are excluded.
MAP_CURATED_CATEGORIES = frozenset({"open", "hub", "boss", "combat"})
MAP_TOP_CATEGORIES = frozenset({"open", "hub", "boss"})
MAP_NON_CURATED = frozenset({"minigame", "internal", "vr", "npc", "zero"})

_NON_TEXTURE_SECTION_PREFIXES = (
    "VoiceBlocks/",
    "Kodama/",
    "Data/",
    "LevelProcess/",
    "ChessboardProcess/",
)


def is_texture_section(sec_name: str) -> bool:
    """Return True if a texture_sections_index entry holds actual visual/UI assets."""
    return not str(sec_name or "").startswith(_NON_TEXTURE_SECTION_PREFIXES)


def split_camel(text: str) -> str:
    s = _CAMEL_RE.sub(" ", text).strip()
    for rx, rep in _GLUED_WORD_FIXES:
        s = rx.sub(rep, s)
    return re.sub(r"\s+", " ", s).strip()


def _raw_token_classify(tok: str) -> str:
    low = tok.lower()
    if low in _ATTACHMENT_TOKENS:
        return "attachment"
    if low in _COMPANION_TOKENS:
        return "companion"
    m = re.match(r"^[a-z]+", low)
    if m and m.group(0) in _COMPANION_TOKENS:
        return "companion"
    return ""


# ---------------------------------------------------------------------------
# Characters & Families
# ---------------------------------------------------------------------------
def _parse_char(cid: str) -> tuple[str, str, str] | None:
    m = _CHAR_RE.match(cid)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def _is_renamed_alias(raw: str, alias: str) -> bool:
    return split_camel(raw).lower().replace(" ", "") != alias.lower().replace(" ", "")


def find_variant_bases(names) -> dict[str, str]:
    """Map variant/companion/attachment id -> base id using CamelCase or underscore boundaries."""
    parsed: dict[str, tuple[str, str, str]] = {}
    by_name_ci: dict[str, list[str]] = {}
    for n in names:
        p = _parse_char(n)
        if p:
            parsed[n] = p
            by_name_ci.setdefault(p[2].lower(), []).append(n)
    out: dict[str, str] = {}
    for n, (g, s, nm) in parsed.items():
        nm_nous = nm.replace("_", "")
        best = None
        best_blen = -1
        for b, (bg, bs, bnm) in parsed.items():
            if b == n or bg != g or bs != s or len(bnm) >= len(nm):
                continue
            direct_ok = nm.startswith(bnm) and (nm[len(bnm)] == "_" or nm[len(bnm)].isupper())
            nous_ok = (not direct_ok) and nm_nous.startswith(bnm) and len(nm_nous) > len(bnm) and nm_nous[len(bnm)].isupper()
            if direct_ok or nous_ok:
                if len(bnm) > best_blen:
                    best = b
                    best_blen = len(bnm)
        if best is not None:
            # Prefer Avatar root over intermediate Traversing skin only when attachment belongs to skin -> keep longest base.
            out[n] = best
            continue
        # Handle cross-size/gender attachments like SeedSr -> Avatar_Female_Size02_Seed
        for sfx in _ATTACHMENT_SUFFIXES | _COMPANION_TOKENS:
            if len(nm) > len(sfx) and nm.lower().endswith(sfx.lower()):
                stem = nm[: -len(sfx)].rstrip("_")
                cands = [b for b in by_name_ci.get(stem.lower(), ()) if b != n]
                if len(cands) == 1:
                    out[n] = cands[0]
                    break
    return out


def find_family_roots(index: dict[str, dict], bases: dict[str, str] | None = None) -> dict[str, dict]:
    """Resolve top-level character family grouping, primary member, and tier for every non-Bangboo char."""
    if bases is None:
        bases = find_variant_bases(index.keys())
    parsed = {cid: _parse_char(cid) for cid in index}

    # Lookup codename -> canonical id (preferring same gender/size or exact match)
    by_raw: dict[str, list[str]] = {}
    for cid, p in parsed.items():
        if p:
            by_raw.setdefault(p[2], []).append(cid)

    def _alt_parent_id(cid: str) -> str | None:
        p = parsed.get(cid)
        if not p:
            return None
        target_raw = CHAR_ALT_FORM_PARENTS.get(p[2])
        if not target_raw:
            return None
        cands = by_raw.get(target_raw, [])
        return cands[0] if cands else None

    def _chain_root(cid: str) -> str:
        cur = cid
        seen: set[str] = set()
        while cur in bases and bases[cur] not in seen:
            seen.add(cur)
            cur = bases[cur]
        return cur

    # Detect sibling-only families (e.g. RemielleOrigin / RemiellePasSeul / RemielleRamiel)
    sibling_groups: dict[tuple[str, str, str], list[str]] = {}
    for cid in index:
        if cid in bases or _BANGBOO_RE.match(cid):
            continue
        p = parsed.get(cid)
        if not p:
            continue
        if _alt_parent_id(cid) is not None:
            continue
        m = re.match(r"^([A-Z][a-z]+)[A-Z].*$", p[2])
        if m:
            sibling_groups.setdefault((p[0], p[1], m.group(1)), []).append(cid)

    sibling_primary: dict[str, str] = {}
    sibling_stem_of: dict[str, str] = {}
    for (_g, _s, stem), members in sibling_groups.items():
        if len(members) < 2:
            continue
        def _richness(mcid: str):
            e = index.get(mcid) or {}
            anims = len(e.get("anim_keys") or [])
            meshes = len(e.get("mesh_blks") or []) + int(e.get("embedded_meshes") or 0) + len(e.get("extra_cabs") or [])
            return (1 if anims > 0 else 0, anims, meshes, -ord(mcid[-1:]))
        prim = max(members, key=lambda m: (_richness(m), m))
        for mc in members:
            sibling_primary[mc] = prim
            sibling_stem_of[mc] = stem

    out: dict[str, dict] = {}
    for cid in index:
        if _BANGBOO_RE.match(cid):
            continue
        base_id = bases.get(cid)
        root_id = _chain_root(cid)
        alt_top = _alt_parent_id(root_id)
        is_alt = False
        is_alt_skin = False
        if alt_top is not None:
            if cid == root_id:
                is_alt = True
            else:
                is_alt_skin = True
            family_root = alt_top
        elif root_id in sibling_primary:
            family_root = sibling_primary[root_id]
        else:
            family_root = root_id

        p_fam = parsed.get(family_root)
        if family_root in sibling_stem_of:
            fam_stem = sibling_stem_of[family_root]
            fam_display = CHAR_CODENAME_ALIASES.get(fam_stem, split_camel(fam_stem))
        elif p_fam and p_fam[2] in ("AriaIdol",):
            fam_display = "Aria"
        else:
            fam_display = CHAR_CODENAME_ALIASES.get(p_fam[2], split_camel(p_fam[2])) if p_fam else display_name(family_root)
        fam_key = fam_display.lower()

        # Classify tail relation across the full ancestry down to family_root (so Lucy_Wood_Princess stays companion)
        p_cur = parsed.get(cid)
        rel_kind = ""
        ref_parent = family_root if (base_id and parsed.get(family_root) and p_cur and p_cur[2].lower().startswith(parsed[family_root][2].lower())) else base_id
        if ref_parent and p_cur:
            p_b = parsed.get(ref_parent)
            tail = p_cur[2][len(p_b[2]):].lstrip("_") if (p_b and p_cur[2].lower().startswith(p_b[2].lower())) else p_cur[2]
            toks = [t for t in tail.split("_") if t]
            for t in toks:
                rk = _raw_token_classify(t)
                if rk == "attachment":
                    rel_kind = "attachment"
                    break
                if rk == "companion":
                    rel_kind = "companion"
            if not rel_kind:
                rk = _raw_token_classify(tail.replace("_", ""))
                if rk:
                    rel_kind = rk

        e = index.get(cid) or {}
        has_model = bool((e.get("mesh_blks") or []) or (e.get("extra_cabs") or []) or int(e.get("embedded_meshes") or 0) > 0)
        anim_n = len(e.get("anim_keys") or [])

        if cid == family_root:
            tier = 0
        elif rel_kind == "attachment":
            tier = 5
        elif rel_kind == "companion":
            tier = 4
        elif is_alt:
            tier = 2
        elif is_alt_skin:
            tier = 3
        elif base_id or (root_id in sibling_primary and cid != family_root):
            tier = 1
        elif not has_model and anim_n == 0:
            tier = 6
        else:
            tier = 0

        out[cid] = {
            "family_key": fam_key,
            "family_display": fam_display,
            "family_root": family_root,
            "base_id": base_id or (alt_top if is_alt else (family_root if cid in sibling_primary and cid != family_root else None)),
            "is_alt": is_alt,
            "rel_kind": rel_kind,
            "tier": tier,
        }
    return out


def apply_variant_animations(index: dict[str, dict]) -> dict[str, dict]:
    """Return ``index`` where skin/outfit variants inherit their base character's clips."""
    bases = find_variant_bases(index.keys())
    fam = find_family_roots(index, bases)
    for vid, fmeta in fam.items():
        if fmeta["rel_kind"] in ("attachment", "companion") or fmeta["is_alt"]:
            continue
        bid = bases.get(vid) or (fmeta["family_root"] if fmeta["family_root"] != vid else None)
        if not bid:
            continue
        v, b = index.get(vid), index.get(bid)
        if not isinstance(v, dict) or not isinstance(b, dict):
            continue
        if v.get("anim_blks") or v.get("anim_keys"):
            continue
        if not (b.get("anim_blks") or b.get("anim_keys")):
            continue
        v["anim_blks"] = list(b.get("anim_blks") or [])
        v["anim_keys"] = list(b.get("anim_keys") or [])
        v["anim_inherited_from"] = bid
    return index


def display_name(cid: str, base_id: str | None = None) -> str:
    bm = _BANGBOO_RE.match(cid)
    if bm:
        raw_bb = re.sub(r"001$", "", bm.group(1)) or bm.group(1)
        return f"{split_camel(raw_bb.replace('_', ' '))} (Bangboo)"
    p = _parse_char(cid)
    if p:
        raw = p[2]
        if base_id and base_id != cid:
            pb = _parse_char(base_id)
            if pb:
                braw = pb[2]
                bdisp = display_name(base_id)
                base_clean = re.sub(r"\s*\([^)]*\)$", "", bdisp) if f"({braw})" in bdisp else bdisp
                if raw.lower().startswith(braw.lower()) and len(raw) > len(braw):
                    rest = split_camel(raw[len(braw):].lstrip("_").replace("_", " "))
                else:
                    rest = split_camel(raw.replace("_", " "))
                if base_raw_alias := (f" ({braw})" if _is_renamed_alias(braw, base_clean) else ""):
                    return f"{base_clean} — {rest}{base_raw_alias}"
                return f"{base_clean} — {rest}"
        if raw in CHAR_CODENAME_ALIASES:
            alias = CHAR_CODENAME_ALIASES[raw]
            if _is_renamed_alias(raw, alias):
                return f"{alias} ({raw})"
            return alias
        best_ak = None
        for ak in CHAR_CODENAME_ALIASES:
            if len(raw) > len(ak) and raw.startswith(ak) and (raw[len(ak)] == "_" or raw[len(ak)].isupper()):
                if best_ak is None or len(ak) > len(best_ak):
                    best_ak = ak
        if best_ak is not None:
            alias = CHAR_CODENAME_ALIASES[best_ak]
            rest = split_camel(raw[len(best_ak):].lstrip("_").replace("_", " "))
            if _is_renamed_alias(best_ak, alias):
                return f"{alias} — {rest} ({raw})"
            return f"{alias} — {rest}"
        if "_" in raw:
            head, tail = raw.split("_", 1)
            return f"{split_camel(head)} — {split_camel(tail.replace('_', ' '))}"
        return split_camel(raw)
    return split_camel(cid.removeprefix("Avatar_").replace("_", " "))


def short_child_label(cid: str, parent_id: str | None = None, kind: str = "") -> str:
    """Concise child label shown under an expanded parent character row."""
    full = display_name(cid, base_id=parent_id if kind not in ("alt_form",) else None)
    if kind == "alt_form":
        return f"{display_name(cid)} (Alt Form)"
    if parent_id:
        p = _parse_char(cid)
        pb = _parse_char(parent_id)
        if p and pb and p[2].lower().startswith(pb[2].lower()) and len(p[2]) > len(pb[2]):
            rest = split_camel(p[2][len(pb[2]):].lstrip("_").replace("_", " "))
            if kind == "companion":
                return f"{rest} (Companion)"
            if kind == "attachment":
                return f"{rest} (Weapon / Part)"
            return rest
    if " — " in full:
        tail = full.split(" — ", 1)[1]
        tail = re.sub(r"\s*\([^)]*\)$", "", tail).strip()
        if kind == "companion":
            return f"{tail} (Companion)"
        if kind == "attachment":
            return f"{tail} (Weapon / Part)"
        return tail
    return full


def classify_characters(index: dict[str, dict]) -> dict[str, dict]:
    """Classify every character/Bangboo id with family grouping, roles, and clean sort keys."""
    bases = find_variant_bases(index.keys())
    fam_info = find_family_roots(index, bases)
    cabs_by_id: dict[str, list[str]] = {
        k: list((v or {}).get("extra_cabs") or []) for k, v in index.items() if isinstance(v, dict)
    }

    out: dict[str, dict] = {}
    for cid, entry in index.items():
        entry = entry if isinstance(entry, dict) else {}
        mesh_n = len(entry.get("mesh_blks") or [])
        extra_cabs = entry.get("extra_cabs") or []
        emb_n = int(entry.get("embedded_meshes") or 0)
        has_model = bool(mesh_n > 0 or extra_cabs or emb_n > 0)
        anim_n = len(entry.get("anim_keys") or [])
        inherited = entry.get("anim_inherited_from")
        own_anims = 0 if inherited else anim_n
        base = bases.get(cid)
        fmeta = fam_info.get(cid)

        p = _parse_char(cid)
        raw_name = p[2] if p else cid

        if _BANGBOO_RE.match(cid):
            kind = "bangboo"
            base = None
            parent_id = None
            fam_key = display_name(cid).lower()
            fam_display = display_name(cid)
            fam_root = cid
            tier = 0
        else:
            parent_id = fmeta["base_id"] if fmeta else base
            fam_key = fmeta["family_key"] if fmeta else display_name(cid).lower()
            fam_display = fmeta["family_display"] if fmeta else display_name(cid)
            fam_root = fmeta["family_root"] if fmeta else cid
            tier = fmeta["tier"] if fmeta else 0
            rel_kind = fmeta["rel_kind"] if fmeta else ""
            is_alt = bool(fmeta and fmeta["is_alt"])
            root_p = _parse_char(fam_root)
            is_proxy = bool(root_p and root_p[2] in CHAR_PROXY_STEMS)

            if not has_model and anim_n == 0:
                kind = "stub"
            elif rel_kind == "attachment":
                kind = "attachment"
            elif rel_kind == "companion":
                kind = "companion"
            elif is_alt:
                kind = "alt_form"
            elif parent_id and fam_root != cid:
                kind = "variant"
            elif is_proxy:
                kind = "proxy"
            elif own_anims >= FULL_ANIM_THRESHOLD:
                kind = "character"
            elif own_anims > 0:
                kind = "limited"
            else:
                kind = "model"

        issues: list[str] = []
        if kind == "stub":
            issues.append("empty stub (no mesh or animation data)")
        else:
            if not has_model:
                issues.append("no model/mesh data found in index")
            if kind in ("attachment", "companion"):
                pass
            elif kind == "variant" and anim_n == 0:
                issues.append("base character has no animations to inherit")
            elif kind in ("limited", "model") and anim_n == 0:
                issues.append("no animation clips found")
            if kind == "limited":
                issues.append(f"only {own_anims} animation clips (full sets have {FULL_ANIM_THRESHOLD}+)")
            if kind in ("character", "proxy", "alt_form", "limited") and mesh_n == 1 and len(extra_cabs) == 1:
                has_fuller = any(
                    b == cid and len(cabs_by_id.get(ocid, ())) > 1
                    for ocid, b in bases.items()
                )
                if has_fuller:
                    issues.append("only 1 mesh CAB in base prefab; exporter will fall back to fuller variant if needed")

        disp_base = None
        cand_disp_base = base or (parent_id if cid != fam_root else None)
        if cand_disp_base and cand_disp_base != cid and kind in ("variant", "companion", "attachment"):
            pb_chk = _parse_char(cand_disp_base)
            if p and pb_chk and p[2].lower().startswith(pb_chk[2].lower()):
                disp_base = cand_disp_base
        disp = display_name(cid, base_id=disp_base)
        short_parent = fam_root if (base and fam_root != base and kind in ("variant", "companion", "attachment") and not _raw_token_classify(raw_name.split("_")[-1])) else (parent_id or base)
        if short_parent and _parse_char(short_parent) and p and not p[2].lower().startswith(_parse_char(short_parent)[2].lower()):
            # Handles AriaRobotNote under AriaIdol -> "Robot — Note"
            pa_root = CHAR_ALT_FORM_PARENTS.get(_parse_char(base)[2]) if base and _parse_char(base) else None
            if pa_root == "AriaIdol" and p[2].startswith("Aria"):
                short_lbl = split_camel(p[2][len("Aria"):])
                short_lbl = f"Robot — {short_lbl[5:].strip()}" if short_lbl.startswith("Robot ") else short_lbl
            else:
                short_lbl = disp
        else:
            short_lbl = short_child_label(cid, parent_id=short_parent, kind=kind) if cid != fam_root else disp

        out[cid] = {
            "id": cid,
            "display": disp,
            "short_label": short_lbl,
            "kind": kind,
            "base": base or (parent_id if cid != fam_root else None),
            "family_key": fam_key,
            "family_display": fam_display,
            "family_root": fam_root,
            "mesh_count": mesh_n,
            "embedded_meshes": emb_n,
            "has_model": has_model,
            "anim_count": anim_n,
            "anim_inherited_from": inherited,
            "issues": issues,
            "sort": (fam_key, tier, disp.lower()),
        }
    return out


def build_character_tree(index: dict[str, dict], include_bangboo: bool = False) -> list[dict]:
    """Group non-Bangboo characters into collapsible families: Base -> Skins, Alt Forms, Companions, Weapons."""
    cls = classify_characters(index)
    families: dict[str, list[dict]] = {}
    for cid, info in cls.items():
        if not include_bangboo and info["kind"] == "bangboo":
            continue
        families.setdefault(info["family_key"], []).append(info)

    groups: list[dict] = []
    for fkey, members in families.items():
        members.sort(key=lambda m: m["sort"])
        primary = next((m for m in members if m["id"] == m["family_root"]), members[0])
        children = [m for m in members if m["id"] != primary["id"]]
        skin_cnt = sum(1 for c in children if c["kind"] == "variant")
        alt_cnt = sum(1 for c in children if c["kind"] == "alt_form")
        comp_cnt = sum(1 for c in children if c["kind"] == "companion")
        att_cnt = sum(1 for c in children if c["kind"] == "attachment")
        p_parsed = _parse_char(primary["id"])
        role = "proxy" if (primary["kind"] == "proxy" or (p_parsed and p_parsed[2] in CHAR_PROXY_STEMS)) else "agent"
        all_issues = list(dict.fromkeys(iss for m in members for iss in m.get("issues", [])))
        groups.append({
            "family_key": fkey,
            "display": primary["display"],
            "family_display": primary["family_display"],
            "primary_id": primary["id"],
            "primary_info": primary,
            "role": role,
            "children": children,
            "member_ids": [primary["id"], *(c["id"] for c in children)],
            "skin_count": skin_cnt,
            "alt_count": alt_cnt,
            "companion_count": comp_cnt,
            "attachment_count": att_cnt,
            "issues": all_issues,
        })
    groups.sort(key=lambda g: g["display"].lower())
    return groups


def classify_bangboo(index: dict[str, dict]) -> dict[str, dict]:
    """Return deduplicated Bangboo entries mapped from oz_characters_index."""
    by_stem: dict[str, tuple[str, dict]] = {}
    for cid, entry in index.items():
        bm = _BANGBOO_RE.match(cid)
        if not bm:
            continue
        raw = bm.group(1)
        if not raw or not raw[0].isupper():
            continue
        key = re.sub(r"001$", "", raw).lower()
        e = entry if isinstance(entry, dict) else {}
        score = (
            1 if cid.startswith("Bangboo_") else 0,
            len(e.get("extra_cabs") or []),
            len(e.get("anim_keys") or []),
            len(e.get("mesh_blks") or []),
        )
        prev = by_stem.get(key)
        if prev is None or score > prev[0]:
            by_stem[key] = (score, cid, e)

    out: dict[str, dict] = {}
    for _key, (_sc, cid, e) in by_stem.items():
        mesh_n = len(e.get("mesh_blks") or [])
        cabs = len(e.get("extra_cabs") or [])
        emb = int(e.get("embedded_meshes") or 0)
        anim_n = len(e.get("anim_keys") or [])
        has_model = bool(mesh_n > 0 or cabs > 0 or emb > 0)
        issues = []
        if not has_model:
            issues.append("no model/mesh data found in index")
        if anim_n == 0:
            issues.append("no animation clips found")
        disp = display_name(cid).removesuffix(" (Bangboo)")
        out[cid] = {
            "id": cid,
            "display": disp,
            "kind": "bangboo" if has_model else "stub",
            "mesh_count": mesh_n,
            "anim_count": anim_n,
            "has_model": has_model,
            "issues": issues,
        }
    return dict(sorted(out.items(), key=lambda kv: kv[1]["display"].lower()))


def classify_monsters(monsters: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for mid, e in monsters.items():
        e = e if isinstance(e, dict) else {}
        is_boss = e.get("subtype") == "boss" or mid.startswith("Monster_Boss_")
        raw = mid.removeprefix("Monster_Boss_").removeprefix("Monster_")
        disp = split_camel(raw.replace("_", " "))
        anim_n = int(e.get("anim_count") or len(e.get("anim_keys") or []))
        mesh_n = len(e.get("mesh_blks") or [])
        out[mid] = {
            "id": mid,
            "display": f"{disp} (Boss)" if is_boss else disp,
            "kind": "boss" if is_boss else "enemy",
            "mesh_count": mesh_n,
            "anim_count": anim_n,
            "has_model": bool(mesh_n or e.get("blocks") or e.get("prefab_blk")),
            "issues": [],
        }
    return dict(sorted(out.items(), key=lambda kv: (0 if kv[1]["kind"] == "boss" else 1, kv[1]["display"].lower())))


def classify_npcs(npcs: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for nid, e in npcs.items():
        e = e if isinstance(e, dict) else {}
        is_num = nid[4:5].isdigit() or e.get("subtype") == "numeric"
        raw = nid.removeprefix("NPC_")
        disp = raw if is_num else split_camel(raw.replace("_", " "))
        anim_n = int(e.get("anim_count") or len(e.get("anim_keys") or []))
        mesh_n = len(e.get("mesh_blks") or [])
        out[nid] = {
            "id": nid,
            "display": disp,
            "kind": "numeric" if is_num else "named",
            "mesh_count": mesh_n,
            "anim_count": anim_n,
            "has_model": bool(mesh_n or e.get("blocks") or e.get("prefab_blk")),
            "issues": [],
        }
    return dict(sorted(out.items(), key=lambda kv: (1 if kv[1]["kind"] == "numeric" else 0, kv[1]["display"].lower())))


def classify_weapons_props(weapons: dict[str, dict], props: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for eid, e in {**weapons, **props}.items():
        e = e if isinstance(e, dict) else {}
        sub = e.get("subtype") or ""
        if eid.startswith("Weapon_") or sub == "wengine":
            kind = "wengine"
            grp = "W-Engines"
            disp = split_camel(eid.removeprefix("Weapon_").replace("_", " "))
        elif eid.startswith("Belle_"):
            kind = "accessory"
            grp = "Belle Accessories"
            disp = split_camel(eid.removeprefix("Belle_").replace("_", " "))
        elif eid.startswith("Wise_"):
            kind = "accessory"
            grp = "Wise Accessories"
            disp = split_camel(eid.removeprefix("Wise_").replace("_", " "))
        elif sub == "item" or eid.startswith("Item_"):
            kind = "item"
            grp = "Items"
            disp = split_camel(eid.removeprefix("Item_").replace("_", " "))
        elif sub == "device" or eid.startswith("Device_"):
            kind = "device"
            grp = "Devices & Interactables"
            disp = split_camel(eid.removeprefix("Device_").replace("_", " "))
        elif sub == "destruction" or eid.startswith("Destruction_"):
            kind = "destruction"
            grp = "Destructibles"
            disp = split_camel(eid.removeprefix("Destruction_").replace("_", " "))
        else:
            kind = "prop"
            grp = "Game Props"
            disp = split_camel(eid.replace("_", " "))
        mesh_n = len(e.get("mesh_blks") or [])
        out[eid] = {
            "id": eid,
            "display": disp,
            "kind": kind,
            "group_label": grp,
            "mesh_count": mesh_n,
            "anim_count": int(e.get("anim_count") or len(e.get("anim_keys") or [])),
            "has_model": bool(mesh_n or e.get("blocks") or e.get("prefab_blk")),
            "category_src": "weapons" if eid in weapons else "props",
            "issues": [],
        }
    return dict(sorted(out.items(), key=lambda kv: (kv[1]["group_label"].lower(), kv[1]["display"].lower())))


def char_status_text(info: dict) -> str:
    parts = [CHAR_KIND_LABELS.get(info["kind"], info["kind"])]
    if info.get("base") and info["kind"] not in ("character", "proxy", "stub"):
        parts.append(f"of {display_name(info['base'])}")
    if info["anim_count"]:
        tag = "inherited" if info["anim_inherited_from"] else "own"
        parts.append(f"{info['anim_count']} anims ({tag})")
    if info["kind"] != "stub" and not info.get("has_model", info["mesh_count"] > 0):
        parts.append("NO MODEL DATA")
    return " • ".join(parts)


# ---------------------------------------------------------------------------
# Maps
# ---------------------------------------------------------------------------
_MINIGAME_TOKENS = (
    "sidescroll", "cardsidescroll", "pinball", "autobattle", "defensebattle", "surfing",
    "musicbattle", "mindmaze", "savearia", "summerbattle", "shootingrange", "virtualstage",
    "autochess", "knockout", "megarobot", "platformer",
)


def classify_map(name: str, entry: dict | None = None) -> dict:
    """Return {category, region} for a map/stage id."""
    low = name.lower()
    entry = entry if isinstance(entry, dict) else {}
    core = re.sub(r"^stage_", "", low)
    region = ""
    if low in ("real_ecs", "frontendgame") or low.startswith("login_"):
        cat = "internal"
    elif entry.get("streaming"):
        cat = "open"
    elif low.startswith("bangboo") or any(t in low for t in _MINIGAME_TOKENS):
        cat = "minigame"
    elif low.startswith("stage_vr_") or "trainingroom" in low:
        cat = "vr"
    elif "npcworksite" in low:
        cat = "npc"
    elif "boss" in low:
        cat = "boss"
    elif low.startswith("stage_zero_"):
        cat = "zero"
    elif low.startswith("stage_"):
        cat = "combat"
    else:
        cat = "hub"
    if low.startswith("stage_"):
        region = split_camel(core.split("_")[0])
    return {"category": cat, "region": region}


def classify_maps(index: dict[str, dict]) -> dict[str, dict]:
    lowmap = {k.lower(): k for k in index}
    out: dict[str, dict] = {}
    for name, entry in index.items():
        info = classify_map(name, entry)
        layer_of = None
        if name.lower().endswith("_streaming"):
            parent = lowmap.get(name.lower()[: -len("_streaming")])
            if parent and parent != name:
                layer_of = parent
        st = (entry or {}).get("streaming") or {}
        sectors = int(st.get("sector_count") or 0)
        resolved = int(st.get("resolved_count") or 0)
        coverage = (resolved / sectors) if sectors else None
        extra_scenes = len((entry or {}).get("extra_scenes") or [])
        has_scene = bool((entry or {}).get("base_scene"))
        variant_of = None
        if not st and info["category"] == "hub":
            m_dup = re.match(r"^(.+)_(\d+)$", name)
            if m_dup:
                base_hub = lowmap.get(m_dup.group(1).lower())
                if base_hub and base_hub != name and not (index.get(base_hub) or {}).get("streaming"):
                    variant_of = base_hub
        issues: list[str] = []
        if sectors and resolved < sectors:
            issues.append(f"only {resolved}/{sectors} streaming sectors resolved")
        if not st and not has_scene and not extra_scenes:
            issues.append("no scene or streaming data")
        out[name] = {
            "id": name,
            "category": info["category"],
            "region": info["region"],
            "layer_of": layer_of,
            "variant_of": variant_of,
            "sectors": sectors,
            "coverage": coverage,
            "has_scene": has_scene,
            "extra_scenes": extra_scenes,
            "issues": issues,
        }
    for name, info in out.items():
        if info["layer_of"]:
            info["category"] = out[info["layer_of"]]["category"]
            info["region"] = out[info["layer_of"]]["region"]
    return out


def is_curated_map(name: str, info: dict | None) -> bool:
    if not isinstance(info, dict):
        return False
    if info.get("category") not in MAP_CURATED_CATEGORIES:
        return False
    if info.get("layer_of") or info.get("variant_of"):
        return False
    if not (info.get("sectors") or info.get("has_scene") or info.get("extra_scenes")):
        return False
    return True


def map_display_name(name: str, info: dict) -> str:
    base = name.removeprefix("Stage_")
    txt = split_camel(base).replace("_", " ")
    txt = re.sub(r"\s+", " ", txt)
    if info.get("layer_of"):
        txt = txt.replace("Streaming", "").strip() + " (streaming layer)"
    return txt


def map_status_text(info: dict) -> str:
    parts = [MAP_CATEGORY_LABELS.get(info["category"], info["category"])]
    if info.get("region") and info["category"] in ("combat", "boss", "npc", "vr"):
        parts[0] += f" – {info['region']}"
    if info.get("layer_of"):
        parts.append(f"layer of {info['layer_of']}")
    if info.get("variant_of"):
        parts.append(f"duplicate of {info['variant_of']}")
    if info["sectors"]:
        cov = info["coverage"]
        parts.append(f"{info['sectors']} sectors" + (f" ({cov * 100:.0f}% resolved)" if cov is not None and cov < 1.0 else ""))
    elif info["has_scene"]:
        parts.append("scene")
    if info.get("extra_scenes"):
        n_ex = int(info["extra_scenes"])
        parts.append(f"+{n_ex} streaming scene{'s' if n_ex > 1 else ''}")
    return " • ".join(parts)


# ---------------------------------------------------------------------------
# Export discovery
# ---------------------------------------------------------------------------
def find_exported_glb(item_id: str, roots: list[Path]) -> Path | None:
    """Locate an already-exported ``<id>.glb`` under any of the usual output layouts."""
    entity_dirs = ("Exports", "Characters", "Bangboo", "Monsters", "NPCs", "Weapons", "Props")
    for r in roots:
        cands = [
            r / "Exports" / item_id / f"{item_id}.glb",
            r / item_id / f"{item_id}.glb",
            r / "Characters" / item_id / "Model" / f"{item_id}.glb",
            r / "Characters" / item_id / f"{item_id}.glb",
            r / "Maps_Combined" / f"{item_id}_full.glb",
            r / "Maps_Combined" / f"{item_id}.glb",
        ]
        for ed in entity_dirs[2:]:
            cands.append(r / "Exports" / ed / item_id / f"{item_id}.glb")
            cands.append(r / ed / item_id / f"{item_id}.glb")
        for cand in cands:
            if cand.is_file():
                return cand
        for sub in (
            (r / "Exports" / item_id),
            (r / item_id),
            (r / "Characters" / item_id / "Model"),
            *(r / "Exports" / ed / item_id for ed in entity_dirs[2:]),
            *(r / ed / item_id for ed in entity_dirs[2:]),
        ):
            if sub.is_dir():
                g = next(iter(sorted(sub.glob("*.glb"))), None)
                if g is not None:
                    return g
    return None
