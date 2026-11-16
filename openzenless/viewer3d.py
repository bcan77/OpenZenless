"""Native embedded OpenGL 3D Viewer for OpenZenless (.glb / .obj) without HTML/WebEngine."""
from __future__ import annotations

import io
import json
import math
import re
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from pygltflib import GLTF2
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from OpenGL import GL as gl

from . import config as cfg

_COMP_DTYPES = {
    5120: np.int8,
    5121: np.uint8,
    5122: np.int16,
    5123: np.uint16,
    5125: np.uint32,
    5126: np.float32,
}

_TYPE_COUNTS = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}


@dataclass
class GLSubMesh:
    positions: np.ndarray
    normals: np.ndarray
    uvs: np.ndarray | None
    indices: np.ndarray
    color: tuple[float, float, float, float]
    tex_rgba: bytes | None = None
    tex_w: int = 0
    tex_h: int = 0
    alpha_mode: str = "OPAQUE"
    alpha_cutoff: float = 0.5
    double_sided: bool = False
    is_sky: bool = False
    is_weapon: bool = False
    center: tuple[float, float, float] = (0.0, 0.0, 0.0)
    toon_smoothness: float = 0.08
    toon_specular: float = 0.15
    rim_glow: float = 0.35
    rim_color: tuple[float, float, float] = (0.85, 0.92, 1.0)
    shadow_tint: tuple[float, float, float] = (0.62, 0.60, 0.72)


@dataclass
class GLBone:
    name: str
    parent: int
    translation: np.ndarray
    rotation: np.ndarray
    scale: np.ndarray
    rest_world: np.ndarray


@dataclass
class UnityAnimClip:
    name: str
    path: Path
    duration: float
    rot_curves: dict[str, tuple[np.ndarray, np.ndarray]]
    pos_curves: dict[str, tuple[np.ndarray, np.ndarray]]
    scale_curves: dict[str, tuple[np.ndarray, np.ndarray]]


@dataclass
class GLScene:
    source_path: Path
    submeshes: list[GLSubMesh]
    center: np.ndarray
    radius: float
    bmin_y: float
    vertex_count: int
    triangle_count: int
    bones: list[GLBone] | None = None


def _parse_unity_anim_section(lines: list[str], start_idx: int, is_quat: bool) -> tuple[dict[str, tuple[np.ndarray, np.ndarray]], int]:
    """Parse one Unity YAML curve list (m_RotationCurves / m_PositionCurves / m_ScaleCurves)."""
    curves: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    i = start_idx + 1
    n = len(lines)
    base_indent = len(lines[start_idx]) - len(lines[start_idx].lstrip())
    cur_times: list[float] = []
    cur_vals: list[tuple[float, ...]] = []
    cur_time: float | None = None

    while i < n:
        ln = lines[i]
        stripped = ln.strip()
        if not stripped:
            i += 1
            continue
        indent = len(ln) - len(ln.lstrip())
        if indent <= base_indent and not stripped.startswith("-"):
            break
        if stripped.startswith("- curve:"):
            cur_times, cur_vals, cur_time = [], [], None
        elif stripped.startswith("time:"):
            try:
                cur_time = float(stripped.split(":", 1)[1].strip())
            except ValueError:
                cur_time = None
        elif stripped.startswith("value:") and cur_time is not None:
            m = re.findall(r"([xyzw]):\s*([-+0-9.eE]+)", stripped)
            if m:
                d = {k: float(v) for k, v in m}
                cur_times.append(cur_time)
                if is_quat:
                    cur_vals.append((d.get("x", 0.0), d.get("y", 0.0), d.get("z", 0.0), d.get("w", 1.0)))
                else:
                    cur_vals.append((d.get("x", 0.0), d.get("y", 0.0), d.get("z", 0.0)))
            cur_time = None
        elif stripped.startswith("path:"):
            raw_path = stripped.split(":", 1)[1].strip().strip("'\"")
            if cur_times:
                t_arr = np.asarray(cur_times, dtype=np.float32)
                v_arr = np.asarray(cur_vals, dtype=np.float32)
                curves[raw_path] = (t_arr, v_arr)
                leaf = raw_path.rsplit("/", 1)[-1]
                if leaf and leaf not in curves:
                    curves[leaf] = (t_arr, v_arr)
            cur_times, cur_vals, cur_time = [], [], None
        i += 1
    return curves, i


def load_unity_anim(path: Path) -> UnityAnimClip:
    """Parse a Unity YAML .anim file exported by AnimeStudio/AssetStudio."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    name = path.stem
    stop_time = 0.0
    rot_curves: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    pos_curves: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    scale_curves: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    i = 0
    n = len(lines)
    while i < n:
        s = lines[i].strip()
        if s.startswith("m_Name:") and name == path.stem:
            val = s.split(":", 1)[1].strip().strip("'\"")
            if val:
                name = val
        elif s.startswith("m_StopTime:"):
            try:
                stop_time = max(stop_time, float(s.split(":", 1)[1].strip()))
            except ValueError:
                pass
        elif s.startswith("m_RotationCurves:"):
            c, i = _parse_unity_anim_section(lines, i, is_quat=True)
            rot_curves.update(c)
            continue
        elif s.startswith("m_PositionCurves:"):
            c, i = _parse_unity_anim_section(lines, i, is_quat=False)
            pos_curves.update(c)
            continue
        elif s.startswith("m_ScaleCurves:"):
            c, i = _parse_unity_anim_section(lines, i, is_quat=False)
            scale_curves.update(c)
            continue
        i += 1

    max_t = stop_time
    for d in (rot_curves, pos_curves, scale_curves):
        for t_arr, _ in d.values():
            if len(t_arr):
                max_t = max(max_t, float(t_arr[-1]))
    return UnityAnimClip(
        name=name,
        path=path,
        duration=max(max_t, 0.001),
        rot_curves=rot_curves,
        pos_curves=pos_curves,
        scale_curves=scale_curves,
    )


def _eval_vec3_curve(times: np.ndarray, vals: np.ndarray, t: float) -> np.ndarray:
    if len(times) == 1 or t <= times[0]:
        return vals[0]
    if t >= times[-1]:
        return vals[-1]
    idx = int(np.searchsorted(times, t, side="right")) - 1
    idx = max(0, min(idx, len(times) - 2))
    dt = float(times[idx + 1] - times[idx])
    a = (t - float(times[idx])) / dt if dt > 1e-6 else 0.0
    return vals[idx] * (1.0 - a) + vals[idx + 1] * a


def _eval_quat_curve(times: np.ndarray, vals: np.ndarray, t: float) -> np.ndarray:
    if len(times) == 1 or t <= times[0]:
        q = vals[0]
    elif t >= times[-1]:
        q = vals[-1]
    else:
        idx = int(np.searchsorted(times, t, side="right")) - 1
        idx = max(0, min(idx, len(times) - 2))
        dt = float(times[idx + 1] - times[idx])
        a = (t - float(times[idx])) / dt if dt > 1e-6 else 0.0
        q0, q1 = vals[idx], vals[idx + 1]
        if float(np.dot(q0, q1)) < 0.0:
            q1 = -q1
        q = q0 * (1.0 - a) + q1 * a
    norm = float(np.linalg.norm(q))
    return q / norm if norm > 1e-6 else np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)


def _trs_to_mat4(t: np.ndarray, q: np.ndarray, s: np.ndarray) -> np.ndarray:
    x, y, z, w = float(q[0]), float(q[1]), float(q[2]), float(q[3])
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    m = np.eye(4, dtype=np.float32)
    m[0, 0] = (1.0 - 2.0 * (yy + zz)) * s[0]
    m[0, 1] = (2.0 * (xy - wz)) * s[1]
    m[0, 2] = (2.0 * (xz + wy)) * s[2]
    m[1, 0] = (2.0 * (xy + wz)) * s[0]
    m[1, 1] = (1.0 - 2.0 * (xx + zz)) * s[1]
    m[1, 2] = (2.0 * (yz - wx)) * s[2]
    m[2, 0] = (2.0 * (xz - wy)) * s[0]
    m[2, 1] = (2.0 * (yz + wx)) * s[1]
    m[2, 2] = (1.0 - 2.0 * (xx + yy)) * s[2]
    m[:3, 3] = t
    return m


def _read_accessor(gltf: GLTF2, blob: bytes, acc_idx: int) -> np.ndarray:
    acc = gltf.accessors[acc_idx]
    bv = gltf.bufferViews[acc.bufferView]
    dtype = _COMP_DTYPES[acc.componentType]
    ncomp = _TYPE_COUNTS[acc.type]
    item_size = np.dtype(dtype).itemsize * ncomp
    stride = bv.byteStride or item_size
    offset = (bv.byteOffset or 0) + (acc.byteOffset or 0)
    count = acc.count

    if stride == item_size:
        arr = np.frombuffer(blob, dtype=dtype, count=count * ncomp, offset=offset)
        arr = arr.reshape((count, ncomp)) if ncomp > 1 else arr.reshape((count,))
    else:
        raw = np.frombuffer(blob, dtype=np.uint8, count=count * stride, offset=offset)
        raw = raw.reshape((count, stride))[:, :item_size].copy()
        arr = raw.view(dtype=dtype)
        arr = arr.reshape((count, ncomp)) if ncomp > 1 else arr.reshape((count,))

    if acc.normalized and acc.componentType != 5126:
        info = np.iinfo(dtype)
        arr = arr.astype(np.float32) / float(info.max)
    return arr


def _node_local_matrix(node) -> np.ndarray:
    if node.matrix:
        return np.array(node.matrix, dtype=np.float32).reshape((4, 4)).T
    mat = np.eye(4, dtype=np.float32)
    if node.translation:
        mat[0:3, 3] = node.translation
    if node.rotation:
        x, y, z, w = node.rotation
        rot = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ], dtype=np.float32)
        mat[0:3, 0:3] = rot
    if node.scale:
        sx, sy, sz = node.scale
        mat[0:3, 0] *= sx
        mat[0:3, 1] *= sy
        mat[0:3, 2] *= sz
    return mat


def _decode_gltf_texture(
    gltf: GLTF2,
    blob: bytes,
    tex_idx: int,
    base_dir: Path,
    cache: dict[int, tuple[bytes, int, int] | None],
) -> tuple[bytes, int, int] | None:
    if tex_idx in cache:
        return cache[tex_idx]
    try:
        tex = gltf.textures[tex_idx]
        if tex.source is None:
            cache[tex_idx] = None
            return None
        img_obj = gltf.images[tex.source]
        raw_bytes = None
        if img_obj.bufferView is not None:
            bv = gltf.bufferViews[img_obj.bufferView]
            off = bv.byteOffset or 0
            raw_bytes = blob[off : off + bv.byteLength]
        elif img_obj.uri and not img_obj.uri.startswith("data:"):
            p = base_dir / img_obj.uri
            if p.is_file():
                raw_bytes = p.read_bytes()
        if not raw_bytes:
            cache[tex_idx] = None
            return None
        im = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
        if max(im.size) > 1024:
            im.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        res = (im.tobytes(), im.width, im.height)
        cache[tex_idx] = res
        return res
    except Exception:
        cache[tex_idx] = None
        return None


def _load_game_materials(model_dir: Path) -> dict[str, dict]:
    mat_dir = model_dir / "Material"
    if not mat_dir.is_dir():
        return {}
    out: dict[str, dict] = {}
    for p in mat_dir.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            sp = d.get("m_SavedProperties") or {}
            fl = sp.get("m_Floats") or {}
            cl = sp.get("m_Colors") or {}
            col_d = cl.get("_Color") or cl.get("_BaseColor")
            fres_d = cl.get("_FresnelColor")
            out[p.stem.lower()] = {
                "color": (
                    float(col_d.get("r", 1.0)),
                    float(col_d.get("g", 1.0)),
                    float(col_d.get("b", 1.0)),
                    float(col_d.get("a", 1.0)),
                ) if isinstance(col_d, dict) else None,
                "alpha_clip": float(fl.get("_AlphaClip", 0.0)) >= 0.5,
                "cutoff": float(fl.get("_Cutoff", 0.5)),
                "blend": float(fl.get("_Blend", 0.0)) >= 0.5,
                "double_sided": float(fl.get("_Cull", 2.0)) < 0.5,
                "smoothness": max(0.01, min(0.5, float(fl.get("_ShadowmapSmoothness", 0.5)) * 0.2)),
                "specular": max(0.0, min(1.0, float(fl.get("_ToonSpecular", 0.01)) * 10.0)),
                "rim_glow": max(0.0, min(1.0, float(fl.get("_RimGlow", 0.35)))),
                "rim_color": (
                    min(1.0, float(fres_d.get("r", 0.85))),
                    min(1.0, float(fres_d.get("g", 0.92))),
                    min(1.0, float(fres_d.get("b", 1.0))),
                ) if isinstance(fres_d, dict) else (0.85, 0.92, 1.0),
            }
        except Exception:
            pass
    return out


def _match_game_material(mat_map: dict[str, dict], *names: str) -> dict | None:
    if not mat_map:
        return None
    for nm in names:
        if not nm:
            continue
        nl = re.sub(r"\.[0-9]+$", "", nm.lower())
        if nl in mat_map:
            return mat_map[nl]
        toks = [t for t in nl.split("_") if len(t) >= 3]
        for k, v in mat_map.items():
            if nl in k or k in nl or (len(toks) >= 2 and sum(1 for t in toks if t in k) >= 2):
                return v
    return None


def load_glb_scene(path: Path) -> GLScene:
    gltf = GLTF2().load(str(path))
    blob = gltf.binary_blob() or b""
    if not blob and gltf.buffers and gltf.buffers[0].uri:
        buf_path = path.parent / gltf.buffers[0].uri
        if buf_path.is_file():
            blob = buf_path.read_bytes()

    tex_cache: dict[int, tuple[bytes, int, int] | None] = {}
    game_mats = _load_game_materials(path.parent)
    submeshes: list[GLSubMesh] = []
    bmin = np.array([np.inf, np.inf, np.inf], dtype=np.float32)
    bmax = np.array([-np.inf, -np.inf, -np.inf], dtype=np.float32)
    total_verts = 0
    total_tris = 0

    def _visit(node_idx: int, parent_mat: np.ndarray):
        nonlocal bmin, bmax, total_verts, total_tris
        node = gltf.nodes[node_idx]
        world = parent_mat @ _node_local_matrix(node)
        if node.mesh is not None:
            mesh = gltf.meshes[node.mesh]
            rot3 = world[:3, :3]
            try:
                norm_mat = np.linalg.inv(rot3).T.astype(np.float32)
            except np.linalg.LinAlgError:
                norm_mat = rot3
            for prim in mesh.primitives:
                if prim.attributes.POSITION is None:
                    continue
                pos = _read_accessor(gltf, blob, prim.attributes.POSITION).astype(np.float32)
                pos_w = (pos @ rot3.T) + world[:3, 3]
                if prim.attributes.NORMAL is not None:
                    nrm = _read_accessor(gltf, blob, prim.attributes.NORMAL).astype(np.float32)
                    nrm_w = nrm @ norm_mat.T
                    lens = np.linalg.norm(nrm_w, axis=1, keepdims=True)
                    nrm_w = np.divide(nrm_w, np.maximum(lens, 1e-6))
                else:
                    nrm_w = np.tile(np.array([0.0, 1.0, 0.0], dtype=np.float32), (len(pos_w), 1))

                uvs = None
                if prim.attributes.TEXCOORD_0 is not None:
                    uvs = _read_accessor(gltf, blob, prim.attributes.TEXCOORD_0).astype(np.float32)
                elif getattr(prim.attributes, "TEXCOORD_1", None) is not None:
                    uvs = _read_accessor(gltf, blob, prim.attributes.TEXCOORD_1).astype(np.float32)

                if prim.indices is not None:
                    idx = _read_accessor(gltf, blob, prim.indices).astype(np.uint32).reshape(-1)
                else:
                    idx = np.arange(len(pos_w), dtype=np.uint32)

                color = (0.78, 0.80, 0.85, 1.0)
                tex_data = None
                alpha_mode = "OPAQUE"
                alpha_cutoff = 0.5
                double_sided = False
                mat_name = ""
                if prim.material is not None and prim.material < len(gltf.materials):
                    mat = gltf.materials[prim.material]
                    mat_name = mat.name or ""
                    alpha_mode = (mat.alphaMode or "OPAQUE").upper()
                    if mat.alphaCutoff is not None:
                        alpha_cutoff = float(mat.alphaCutoff)
                    double_sided = bool(mat.doubleSided)
                    pbr = mat.pbrMetallicRoughness
                    if pbr is not None:
                        if pbr.baseColorFactor:
                            c = pbr.baseColorFactor
                            color = (float(c[0]), float(c[1]), float(c[2]), float(c[3]))
                        if pbr.baseColorTexture is not None:
                            tex_data = _decode_gltf_texture(
                                gltf, blob, pbr.baseColorTexture.index, path.parent, tex_cache
                            )

                hint = f"{node.name or ''} {mesh.name or ''} {mat_name}".lower()
                is_weapon = "weapon" in hint
                is_sky = bool(re.search(r"(?:^|[_\s.])(skybox|skydome|sky_?dome|sky_?sphere|sky_?plane|sky|cloud|dome|backdrop)(?:$|[_\s.0-9])", hint))
                is_cutout = any(t in hint for t in ("hair", "lash", "leaf", "tree", "plant", "shrub", "bush", "grass", "foliage"))
                if is_sky:
                    double_sided = True
                    if alpha_mode == "OPAQUE" and tex_data is not None:
                        alpha_mode = "BLEND"
                elif alpha_mode == "OPAQUE" and tex_data is not None and is_cutout:
                    a_min = int(np.frombuffer(tex_data[0], dtype=np.uint8)[3::4].min())
                    if a_min < 250:
                        alpha_mode = "MASK"

                gm = _match_game_material(game_mats, mat_name, mesh.name or "", node.name or "")
                toon_smooth = 0.08
                toon_spec = 0.15
                rim_glow = 0.35
                rim_col = (0.85, 0.92, 1.0)
                if gm is not None:
                    if gm["color"] is not None and tex_data is None:
                        color = gm["color"]
                    if gm["alpha_clip"]:
                        alpha_mode = "MASK"
                        alpha_cutoff = gm["cutoff"]
                    elif gm["blend"]:
                        alpha_mode = "BLEND"
                    if gm["double_sided"]:
                        double_sided = True
                    toon_smooth = gm["smoothness"]
                    toon_spec = gm["specular"]
                    rim_glow = gm["rim_glow"]
                    rim_col = gm["rim_color"]

                if alpha_mode == "BLEND" and tex_data is not None and not is_sky and not any(t in hint for t in ("glass", "window", "water", "eff", "fx", "decal", "trans")):
                    a_mean = float(np.frombuffer(tex_data[0], dtype=np.uint8)[3::4].mean())
                    if a_mean < 5.0:
                        alpha_mode = "OPAQUE"

                if len(pos_w) > 0:
                    pmin = pos_w.min(axis=0)
                    pmax = pos_w.max(axis=0)
                    bmin = np.minimum(bmin, pmin)
                    bmax = np.maximum(bmax, pmax)
                    sm_center = tuple(((pmin + pmax) * 0.5).tolist())
                else:
                    sm_center = (0.0, 0.0, 0.0)
                total_verts += len(pos_w)
                total_tris += len(idx) // 3
                submeshes.append(
                    GLSubMesh(
                        positions=np.ascontiguousarray(pos_w, dtype=np.float32),
                        normals=np.ascontiguousarray(nrm_w, dtype=np.float32),
                        uvs=np.ascontiguousarray(uvs, dtype=np.float32) if uvs is not None else None,
                        indices=np.ascontiguousarray(idx, dtype=np.uint32),
                        color=color,
                        tex_rgba=tex_data[0] if tex_data else None,
                        tex_w=tex_data[1] if tex_data else 0,
                        tex_h=tex_data[2] if tex_data else 0,
                        alpha_mode=alpha_mode,
                        alpha_cutoff=alpha_cutoff,
                        double_sided=double_sided,
                        is_sky=is_sky,
                        is_weapon=is_weapon,
                        center=sm_center,
                        toon_smoothness=toon_smooth,
                        toon_specular=toon_spec,
                        rim_glow=rim_glow,
                        rim_color=rim_col,
                    )
                )
        for ch in (node.children or ()):
            _visit(ch, world)

    bones: list[GLBone] = []

    def _visit_bone(node_idx: int, parent_world: np.ndarray, parent_bone: int) -> int:
        node = gltf.nodes[node_idx]
        if node.matrix:
            loc = np.array(node.matrix, dtype=np.float32).reshape(4, 4).T
            t = loc[:3, 3].copy()
            s = np.linalg.norm(loc[:3, :3], axis=0).astype(np.float32)
            q = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
        else:
            t = np.array(node.translation or [0.0, 0.0, 0.0], dtype=np.float32)
            q = np.array(node.rotation or [0.0, 0.0, 0.0, 1.0], dtype=np.float32)
            s = np.array(node.scale or [1.0, 1.0, 1.0], dtype=np.float32)
            loc = _trs_to_mat4(t, q, s)
        world = parent_world @ loc
        b_idx = -1
        if node.mesh is None:
            b_idx = len(bones)
            bones.append(GLBone(
                name=node.name or f"node_{node_idx}",
                parent=parent_bone,
                translation=t,
                rotation=q,
                scale=s,
                rest_world=world,
            ))
        for ch in (node.children or ()):
            _visit_bone(ch, world, b_idx if b_idx >= 0 else parent_bone)
        return b_idx

    if gltf.scenes:
        sc = gltf.scenes[gltf.scene or 0]
        root_nodes = sc.nodes or []
    else:
        root_nodes = list(range(len(gltf.nodes)))

    ident = np.eye(4, dtype=np.float32)
    for rn in root_nodes:
        _visit(rn, ident)
        _visit_bone(rn, ident, -1)

    if not submeshes or not np.all(np.isfinite(bmin)):
        center = np.zeros(3, dtype=np.float32)
        radius = 1.0
        bmin_y = 0.0
    else:
        center = ((bmin + bmax) * 0.5).astype(np.float32)
        radius = max(0.05, float(0.5 * np.linalg.norm(bmax - bmin)))
        bmin_y = float(bmin[1])

    return GLScene(
        source_path=path,
        submeshes=submeshes,
        center=center,
        radius=radius,
        bmin_y=bmin_y,
        vertex_count=total_verts,
        triangle_count=total_tris,
        bones=bones,
    )


def load_obj_scene(path: Path) -> GLScene:
    verts: list[tuple[float, float, float]] = []
    faces: list[int] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("v "):
            parts = line.split()
            if len(parts) >= 4:
                verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif line.startswith("f "):
            idxs = [int(tok.split("/")[0]) - 1 for tok in line.split()[1:]]
            for i in range(1, len(idxs) - 1):
                faces.extend((idxs[0], idxs[i], idxs[i + 1]))
    pos = np.array(verts, dtype=np.float32) if verts else np.zeros((0, 3), dtype=np.float32)
    idx = np.array(faces, dtype=np.uint32)
    nrm = np.tile(np.array([0.0, 1.0, 0.0], dtype=np.float32), (len(pos), 1))
    if len(pos) > 0:
        bmin = pos.min(axis=0)
        bmax = pos.max(axis=0)
        center = ((bmin + bmax) * 0.5).astype(np.float32)
        radius = max(0.05, float(0.5 * np.linalg.norm(bmax - bmin)))
        bmin_y = float(bmin[1])
    else:
        center = np.zeros(3, dtype=np.float32)
        radius = 1.0
        bmin_y = 0.0
    sub = GLSubMesh(pos, nrm, None, idx, (0.78, 0.80, 0.85, 1.0)) if len(idx) else None
    return GLScene(
        source_path=path,
        submeshes=[sub] if sub else [],
        center=center,
        radius=radius,
        bmin_y=bmin_y,
        vertex_count=len(pos),
        triangle_count=len(idx) // 3,
    )


class _SceneLoadWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, path: Path):
        super().__init__()
        self.path = path

    def run(self):
        try:
            if self.path.suffix.lower() == ".obj":
                scene = load_obj_scene(self.path)
            else:
                scene = load_glb_scene(self.path)
            self.loaded.emit(scene)
        except Exception as exc:
            self.failed.emit(str(exc))


_TOON_VERT = """
#version 120
varying vec3 v_world_pos;
varying vec3 v_normal;
varying vec2 v_uv;
void main() {
    v_world_pos = gl_Vertex.xyz;
    v_normal = normalize(gl_Normal);
    v_uv = gl_MultiTexCoord0.xy;
    gl_Position = gl_ModelViewProjectionMatrix * gl_Vertex;
}
"""

_TOON_FRAG = """
#version 120
uniform sampler2D u_tex;
uniform int u_use_tex;
uniform vec4 u_color;
uniform int u_alpha_mode;
uniform float u_alpha_cutoff;
uniform int u_is_sky;
uniform int u_toon_enabled;
uniform vec3 u_cam_pos;
uniform vec3 u_light_dir;
uniform float u_toon_smooth;
uniform float u_toon_spec;
uniform float u_rim_glow;
uniform vec3 u_rim_color;
uniform vec3 u_shadow_tint;
varying vec3 v_world_pos;
varying vec3 v_normal;
varying vec2 v_uv;

void main() {
    vec4 base = u_color;
    if (u_use_tex == 1) {
        base *= texture2D(u_tex, v_uv);
    }
    if (u_alpha_mode == 1 && base.a < u_alpha_cutoff) {
        discard;
    }
    if (u_is_sky == 1) {
        gl_FragColor = base;
        return;
    }
    vec3 N = normalize(v_normal);
    if (!gl_FrontFacing) N = -N;
    vec3 L = normalize(u_light_dir);
    vec3 V = normalize(u_cam_pos - v_world_pos);
    float ndl = dot(N, L) * 0.5 + 0.5;
    vec3 lit;
    if (u_toon_enabled == 1) {
        float cel = smoothstep(0.5 - u_toon_smooth, 0.5 + u_toon_smooth, ndl);
        vec3 diffuse_ramp = mix(u_shadow_tint, vec3(1.06), cel);
        vec3 H = normalize(L + V);
        float spec = smoothstep(0.92, 0.96, max(dot(N, H), 0.0)) * u_toon_spec;
        float rim = pow(1.0 - max(dot(N, V), 0.0), 3.0) * u_rim_glow * (0.35 + 0.65 * cel);
        lit = base.rgb * diffuse_ramp + vec3(spec) + u_rim_color * rim * 0.45;
    } else {
        float diff = max(dot(N, L), 0.0) * 0.78 + 0.25;
        lit = base.rgb * diff;
    }
    gl_FragColor = vec4(lit, base.a);
}
"""


class GLBViewport(QOpenGLWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene: GLScene | None = None
        self.tex_ids: list[int] = []
        self.wireframe = False
        self.show_textures = True
        self.show_grid = True
        self.toon_enabled = True
        self.show_skeleton = True
        self.show_weapons = True
        self.anim_clip: UnityAnimClip | None = None
        self.anim_time: float = 0.0
        self._bone_keys: list[tuple[str, ...]] = []
        self._bone_scale: float = 1.0
        self._rest_local_rel: list[np.ndarray] = []
        self._rest_pts: np.ndarray = np.zeros((0, 3), dtype=np.float32)
        self._inv_rest_worlds: list[np.ndarray] = []
        self._mesh_bind: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
        self._toon_prog: QOpenGLShaderProgram | None = None
        self.yaw = 35.0
        self.pitch = 20.0
        self.distance = 3.0
        self.target = np.zeros(3, dtype=np.float32)
        self._last_pos = None
        self._drag_mode = None
        self.setMinimumSize(400, 320)

    def set_scene(self, scene: GLScene):
        self.makeCurrent()
        self._free_textures()
        self.scene = scene
        self.anim_clip = None
        self.anim_time = 0.0
        self._bone_keys = []
        self._bone_scale = 1.0
        self._rest_local_rel = []
        self._rest_pts = np.zeros((0, 3), dtype=np.float32)
        self._inv_rest_worlds = []
        self._mesh_bind = []
        self._upload_textures()
        self.reset_camera()
        self.doneCurrent()
        self.update()

    def set_animation(self, clip: UnityAnimClip | None):
        self.anim_clip = clip
        self.anim_time = 0.0
        if clip is None:
            if self.scene and self._mesh_bind:
                for sm, (rp, rn, *_rest) in zip(self.scene.submeshes, self._mesh_bind):
                    sm.positions = rp.copy()
                    sm.normals = rn.copy()
        else:
            self._prepare_bone_binding()
            self._apply_skinning_frame()
        self.update()

    def set_anim_time(self, t: float):
        self.anim_time = max(0.0, float(t))
        if self.anim_clip is not None:
            self._apply_skinning_frame()
        self.update()

    def reset_camera(self):
        if self.scene is not None:
            self.target = self.scene.center.copy()
            self.distance = max(0.02, self.scene.radius * 2.4)
        else:
            self.target = np.zeros(3, dtype=np.float32)
            self.distance = 3.0
        self.yaw = 35.0
        self.pitch = 20.0
        self.update()

    def _free_textures(self):
        if self.tex_ids:
            try:
                gl.glDeleteTextures(self.tex_ids)
            except Exception:
                pass
            self.tex_ids.clear()

    def _upload_textures(self):
        if not self.scene:
            return
        for sm in self.scene.submeshes:
            if sm.tex_rgba and sm.tex_w > 0 and sm.tex_h > 0:
                tid = int(gl.glGenTextures(1))
                gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
                gl.glTexImage2D(
                    gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, sm.tex_w, sm.tex_h, 0,
                    gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, sm.tex_rgba,
                )
                self.tex_ids.append(tid)
            else:
                self.tex_ids.append(0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

    def initializeGL(self):
        gl.glClearColor(0.06, 0.075, 0.105, 1.0)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_NORMALIZE)
        gl.glEnable(gl.GL_LIGHTING)
        gl.glEnable(gl.GL_LIGHT0)
        gl.glEnable(gl.GL_LIGHT1)
        gl.glEnable(gl.GL_COLOR_MATERIAL)
        gl.glColorMaterial(gl.GL_FRONT_AND_BACK, gl.GL_AMBIENT_AND_DIFFUSE)
        gl.glLightfv(gl.GL_LIGHT0, gl.GL_POSITION, [4.0, 8.0, 6.0, 0.0])
        gl.glLightfv(gl.GL_LIGHT0, gl.GL_DIFFUSE, [0.95, 0.95, 0.95, 1.0])
        gl.glLightfv(gl.GL_LIGHT0, gl.GL_AMBIENT, [0.32, 0.34, 0.38, 1.0])
        gl.glLightfv(gl.GL_LIGHT1, GL_POSITION if hasattr(gl, "GL_POSITION") else gl.GL_POSITION, [-5.0, -2.0, -4.0, 0.0])
        gl.glLightfv(gl.GL_LIGHT1, gl.GL_DIFFUSE, [0.35, 0.42, 0.55, 1.0])
        prog = QOpenGLShaderProgram(self)
        if prog.addShaderFromSourceCode(QOpenGLShader.Vertex, _TOON_VERT) and prog.addShaderFromSourceCode(QOpenGLShader.Fragment, _TOON_FRAG) and prog.link():
            self._toon_prog = prog

    def _set_projection(self, w: int, h: int):
        h = max(1, h)
        aspect = w / float(h)
        scene_r = self.scene.radius if self.scene else 1.0
        near = max(0.005, min(self.distance * 0.002, scene_r * 0.002))
        far = max(500.0, self.distance * 50.0, scene_r * 25.0)
        top = near * math.tan(math.radians(45.0) * 0.5)
        right = top * aspect
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glLoadIdentity()
        gl.glFrustum(-right, right, -top, top, near, far)

    def resizeGL(self, w: int, h: int):
        h = max(1, h)
        gl.glViewport(0, 0, w, h)
        self._set_projection(w, h)
        gl.glMatrixMode(gl.GL_MODELVIEW)

    def paintGL(self):
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        self._set_projection(max(1, self.width()), max(1, self.height()))

        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glLoadIdentity()

        yr = math.radians(self.yaw)
        pr = math.radians(self.pitch)
        cx = self.target[0] + self.distance * math.cos(pr) * math.sin(yr)
        cy = self.target[1] + self.distance * math.sin(pr)
        cz = self.target[2] + self.distance * math.cos(pr) * math.cos(yr)
        self._glu_look_at(cx, cy, cz, self.target[0], self.target[1], self.target[2])

        if self.show_grid:
            self._draw_grid()

        if not self.scene:
            return

        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE if self.wireframe else gl.GL_FILL)
        gl.glEnableClientState(gl.GL_VERTEX_ARRAY)
        gl.glEnableClientState(gl.GL_NORMAL_ARRAY)

        opaque_items = []
        blend_items = []
        for i, sm in enumerate(self.scene.submeshes):
            if len(sm.indices) == 0:
                continue
            if sm.is_weapon and not self.show_weapons:
                continue
            if sm.alpha_mode == "BLEND":
                dx = sm.center[0] - cx
                dy = sm.center[1] - cy
                dz = sm.center[2] - cz
                blend_items.append((dx * dx + dy * dy + dz * dz, i, sm))
            else:
                opaque_items.append((i, sm))
        blend_items.sort(key=lambda it: it[0], reverse=True)

        use_shader = self._toon_prog is not None and not self.wireframe
        if use_shader:
            self._toon_prog.bind()
            self._toon_prog.setUniformValue("u_toon_enabled", 1 if self.toon_enabled else 0)
            self._toon_prog.setUniformValue("u_cam_pos", float(cx), float(cy), float(cz))
            self._toon_prog.setUniformValue("u_light_dir", 0.45, 0.78, 0.42)
            self._toon_prog.setUniformValue("u_tex", 0)

        def _draw_sm(i: int, sm: GLSubMesh):
            gl.glDisable(gl.GL_CULL_FACE)
            tid = self.tex_ids[i] if i < len(self.tex_ids) else 0
            has_tex = bool(self.show_textures and tid and sm.uvs is not None)
            if has_tex:
                gl.glActiveTexture(gl.GL_TEXTURE0)
                gl.glEnable(gl.GL_TEXTURE_2D)
                gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
                gl.glEnableClientState(gl.GL_TEXTURE_COORD_ARRAY)
                gl.glTexCoordPointer(2, gl.GL_FLOAT, 0, sm.uvs)
                col = (1.0, 1.0, 1.0, sm.color[3])
            else:
                gl.glDisable(gl.GL_TEXTURE_2D)
                gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
                col = sm.color

            if use_shader:
                am_int = 2 if sm.alpha_mode == "BLEND" else (1 if sm.alpha_mode == "MASK" else 0)
                self._toon_prog.setUniformValue("u_use_tex", 1 if has_tex else 0)
                self._toon_prog.setUniformValue("u_color", float(col[0]), float(col[1]), float(col[2]), float(col[3]))
                self._toon_prog.setUniformValue("u_alpha_mode", am_int)
                self._toon_prog.setUniformValue("u_alpha_cutoff", float(sm.alpha_cutoff))
                self._toon_prog.setUniformValue("u_is_sky", 1 if sm.is_sky else 0)
                self._toon_prog.setUniformValue("u_toon_smooth", float(sm.toon_smoothness))
                self._toon_prog.setUniformValue("u_toon_spec", float(sm.toon_specular))
                self._toon_prog.setUniformValue("u_rim_glow", float(sm.rim_glow))
                self._toon_prog.setUniformValue("u_rim_color", float(sm.rim_color[0]), float(sm.rim_color[1]), float(sm.rim_color[2]))
                self._toon_prog.setUniformValue("u_shadow_tint", float(sm.shadow_tint[0]), float(sm.shadow_tint[1]), float(sm.shadow_tint[2]))
            else:
                gl.glColor4f(*col)
                if sm.alpha_mode == "MASK":
                    gl.glEnable(gl.GL_ALPHA_TEST)
                    gl.glAlphaFunc(gl.GL_GREATER, sm.alpha_cutoff)
                else:
                    gl.glDisable(gl.GL_ALPHA_TEST)

            gl.glVertexPointer(3, gl.GL_FLOAT, 0, sm.positions)
            gl.glNormalPointer(gl.GL_FLOAT, 0, sm.normals)
            gl.glDrawElements(gl.GL_TRIANGLES, len(sm.indices), gl.GL_UNSIGNED_INT, sm.indices)

        gl.glDisable(gl.GL_BLEND)
        gl.glDepthMask(gl.GL_TRUE)
        for i, sm in opaque_items:
            _draw_sm(i, sm)

        if blend_items:
            gl.glEnable(gl.GL_BLEND)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
            gl.glDepthMask(gl.GL_FALSE)
            for _d2, i, sm in blend_items:
                _draw_sm(i, sm)
            gl.glDepthMask(gl.GL_TRUE)
            gl.glDisable(gl.GL_BLEND)

        if use_shader:
            self._toon_prog.release()
        gl.glDisable(gl.GL_ALPHA_TEST)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glDisableClientState(gl.GL_NORMAL_ARRAY)
        gl.glDisableClientState(gl.GL_VERTEX_ARRAY)
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)

        if self.show_skeleton and self.scene and self.scene.bones and self.anim_clip is not None:
            self._draw_skeleton()

    @staticmethod
    def _glu_look_at(ex, ey, ez, tx, ty, tz):
        f = np.array([tx - ex, ty - ey, tz - ez], dtype=np.float32)
        f /= max(1e-6, np.linalg.norm(f))
        up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        s = np.cross(f, up)
        sn = np.linalg.norm(s)
        if sn < 1e-6:
            s = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        else:
            s /= sn
        u = np.cross(s, f)
        m = np.eye(4, dtype=np.float32)
        m[0, :3] = s
        m[1, :3] = u
        m[2, :3] = -f
        t = np.eye(4, dtype=np.float32)
        t[:3, 3] = [-ex, -ey, -ez]
        gl.glMultMatrixf((m @ t).T)

    def _draw_grid(self):
        gl.glDisable(gl.GL_LIGHTING)
        gl.glDisable(gl.GL_TEXTURE_2D)
        cx = float(self.scene.center[0]) if self.scene else 0.0
        cz = float(self.scene.center[2]) if self.scene else 0.0
        y = self.scene.bmin_y if self.scene else 0.0
        ref = max(self.distance, self.scene.radius if self.scene else 1.0)
        step = max(0.05, round(ref * 0.1, 2))
        extent = step * 10
        gl.glLineWidth(1.0)
        gl.glBegin(gl.GL_LINES)
        gl.glColor4f(0.18, 0.22, 0.30, 1.0)
        for k in range(-10, 11):
            d = k * step
            gl.glVertex3f(cx + d, y, cz - extent)
            gl.glVertex3f(cx + d, y, cz + extent)
            gl.glVertex3f(cx - extent, y, cz + d)
            gl.glVertex3f(cx + extent, y, cz + d)
        gl.glEnd()
        gl.glEnable(gl.GL_LIGHTING)

    def _prepare_bone_binding(self):
        if self._mesh_bind or not self.scene or not self.scene.bones:
            return
        bones = self.scene.bones
        paths: list[str] = []
        self._bone_keys = []
        for i, b in enumerate(bones):
            p = b.name if b.parent < 0 else f"{paths[b.parent]}/{b.name}"
            paths.append(p)
            parts = p.split("/")
            keys = [b.name]
            for k in range(len(parts)):
                keys.append(f"path_{zlib.crc32('/'.join(parts[k:]).encode('utf-8'))}")
            self._bone_keys.append(tuple(keys))

        root_s = float(np.mean(np.abs(bones[0].scale))) if bones else 1.0
        if 1e-4 < root_s < 0.25:
            self._bone_scale = round(1.0 / root_s, 4)
        else:
            rest_pts = np.array([b.rest_world[:3, 3] for b in bones], dtype=np.float32)
            b_span = float(np.linalg.norm(rest_pts.max(axis=0) - rest_pts.min(axis=0))) if len(rest_pts) else 1.0
            body_pts = [sm.positions for sm in self.scene.submeshes if not sm.is_weapon and len(sm.positions)]
            if body_pts:
                bp = np.concatenate(body_pts, axis=0)
                s_span = max(0.1, float(np.linalg.norm(bp.max(axis=0) - bp.min(axis=0))))
            else:
                s_span = max(0.1, float(self.scene.radius * 2.0))
            self._bone_scale = round(s_span / max(b_span, 1e-5), 2) if (b_span > 1e-5 and b_span < s_span * 0.25) else 1.0
            if abs(self._bone_scale - 100.0) < 25.0:
                self._bone_scale = 100.0

        self._inv_rest_worlds = []
        self._rest_local_rel = []
        rest_worlds_scaled: list[np.ndarray] = []
        scaled_rest_pts = []
        for b in bones:
            rw = b.rest_world.copy()
            rw[:3, 3] *= self._bone_scale
            rot = rw[:3, :3]
            norms = np.linalg.norm(rot, axis=0, keepdims=True)
            rw[:3, :3] = np.divide(rot, np.maximum(norms, 1e-6))
            rest_worlds_scaled.append(rw)
            try:
                self._inv_rest_worlds.append(np.linalg.inv(rw).astype(np.float32))
            except np.linalg.LinAlgError:
                self._inv_rest_worlds.append(np.eye(4, dtype=np.float32))
            scaled_rest_pts.append(rw[:3, 3])
            if b.parent >= 0:
                self._rest_local_rel.append((self._inv_rest_worlds[b.parent] @ rw).astype(np.float32))
            else:
                self._rest_local_rel.append(rw.astype(np.float32))
        rest_arr = np.asarray(scaled_rest_pts, dtype=np.float32)
        self._rest_pts = rest_arr

        # Bind to primary Bip001 bone segments (exclude duplicate .001/.002 armatures & tiny finger/toe twigs)
        seg_bones = [
            i for i, b in enumerate(bones)
            if "bip001" in b.name.lower()
            and not any(t in b.name.lower() for t in ("finger", "toe"))
            and not re.search(r"\.\d+$", b.name)
            and b.parent >= 0
            and "bip001" in bones[b.parent].name.lower()
            and not re.search(r"\.\d+$", bones[b.parent].name)
        ]
        if not seg_bones:
            seg_bones = [i for i, b in enumerate(bones) if "bip001" in b.name.lower() and not re.search(r"\.\d+$", b.name)]
        if not seg_bones:
            seg_bones = list(range(len(bones)))
        seg_idx = np.array(seg_bones, dtype=np.int32)
        seg_a = np.asarray([rest_arr[bones[i].parent] if bones[i].parent >= 0 else rest_arr[i] for i in seg_bones], dtype=np.float32)
        seg_b = rest_arr[seg_idx]
        seg_ab = seg_b - seg_a
        seg_len2 = np.maximum(np.sum(seg_ab * seg_ab, axis=1), 1e-8)

        self._mesh_bind = []
        inv_sigma2 = 1.0 / (0.075 ** 2)
        for sm in self.scene.submeshes:
            rp = sm.positions.copy()
            rn = sm.normals.copy()
            n_v = len(rp)
            bi0 = np.zeros(n_v, dtype=np.int32)
            bi1 = np.zeros(n_v, dtype=np.int32)
            w0 = np.ones(n_v, dtype=np.float32)
            w1 = np.zeros(n_v, dtype=np.float32)
            if len(seg_idx) >= 2:
                for start in range(0, n_v, 1024):
                    chunk = rp[start : start + 1024]
                    ap = chunk[:, None, :] - seg_a[None, :, :]
                    t_proj = np.clip(np.sum(ap * seg_ab[None, :, :], axis=2) / seg_len2[None, :], 0.0, 1.0)
                    closest = seg_a[None, :, :] + t_proj[:, :, None] * seg_ab[None, :, :]
                    d2 = np.sum((chunk[:, None, :] - closest) ** 2, axis=2)
                    top2 = np.argpartition(d2, 1, axis=1)[:, :2]
                    d2_0 = np.take_along_axis(d2, top2[:, :1], axis=1)[:, 0]
                    d2_1 = np.take_along_axis(d2, top2[:, 1:2], axis=1)[:, 0]
                    swap = d2_1 < d2_0
                    i0 = np.where(swap, top2[:, 1], top2[:, 0])
                    i1 = np.where(swap, top2[:, 0], top2[:, 1])
                    d_near = np.minimum(d2_0, d2_1)
                    d_far = np.maximum(d2_0, d2_1)
                    e0 = np.ones_like(d_near)
                    e1 = np.exp(-np.maximum(0.0, d_far - d_near) * inv_sigma2)
                    e1 = np.where(d_far > d_near * 3.5 + 0.005, 0.0, e1)
                    s_w = e0 + e1
                    sl = slice(start, start + 1024)
                    bi0[sl] = seg_idx[i0]
                    bi1[sl] = seg_idx[i1]
                    w0[sl] = e0 / s_w
                    w1[sl] = e1 / s_w
            elif len(seg_idx) == 1:
                bi0.fill(seg_idx[0])
                bi1.fill(seg_idx[0])
            self._mesh_bind.append((rp, rn, bi0, bi1, w0, w1))

    def _apply_skinning_frame(self):
        if not self.scene or not self._mesh_bind or len(self._rest_pts) == 0:
            return
        worlds = self._eval_animated_bones()
        bones = self.scene.bones or []
        if not worlds or len(worlds) != len(bones):
            return
        anim_pts = np.asarray([w[:3, 3] for w in worlds], dtype=np.float32)
        n_b = len(bones)
        skin_mats = np.repeat(np.eye(4, dtype=np.float32)[None, :, :], n_b, axis=0)
        for i, b in enumerate(bones):
            p = b.parent if b.parent >= 0 else i
            d0 = self._rest_pts[i] - self._rest_pts[p]
            d1 = anim_pts[i] - anim_pts[p]
            n0 = float(np.linalg.norm(d0))
            n1 = float(np.linalg.norm(d1))
            R = np.eye(3, dtype=np.float32)
            if n0 > 1e-4 and n1 > 1e-4:
                u0 = d0 / n0
                u1 = d1 / n1
                c = float(np.clip(np.dot(u0, u1), -1.0, 1.0))
                if c < 0.9999:
                    v = np.cross(u0, u1)
                    s2 = float(np.dot(v, v))
                    vx = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]], dtype=np.float32)
                    if s2 > 1e-8:
                        R = np.eye(3, dtype=np.float32) + vx + (vx @ vx) * ((1.0 - c) / s2)
                    elif c < -0.999:
                        R = -np.eye(3, dtype=np.float32)
            skin_mats[i, :3, :3] = R
            skin_mats[i, :3, 3] = anim_pts[p] - (R @ self._rest_pts[p])

        for sm, (rp, rn, bi0, bi1, w0, w1) in zip(self.scene.submeshes, self._mesh_bind):
            if len(rp) == 0:
                continue
            M0 = skin_mats[bi0]
            M1 = skin_mats[bi1]
            p0 = np.einsum("vij,vj->vi", M0[:, :3, :3], rp) + M0[:, :3, 3]
            p1 = np.einsum("vij,vj->vi", M1[:, :3, :3], rp) + M1[:, :3, 3]
            n0 = np.einsum("vij,vj->vi", M0[:, :3, :3], rn)
            n1 = np.einsum("vij,vj->vi", M1[:, :3, :3], rn)
            sm.positions = np.ascontiguousarray(w0[:, None] * p0 + w1[:, None] * p1, dtype=np.float32)
            n_mix = w0[:, None] * n0 + w1[:, None] * n1
            n_len = np.linalg.norm(n_mix, axis=1, keepdims=True)
            sm.normals = np.ascontiguousarray(np.divide(n_mix, np.maximum(n_len, 1e-6)), dtype=np.float32)

    @staticmethod
    def _lookup_curve(curves: dict, keys: tuple[str, ...]):
        for k in keys:
            if k in curves:
                return curves[k]
        return None

    def _eval_animated_bones(self) -> list[np.ndarray]:
        bones = self.scene.bones if self.scene and self.scene.bones else []
        clip = self.anim_clip
        if not bones or clip is None:
            return []
        if not self._bone_keys:
            self._prepare_bone_binding()
        t = self.anim_time % max(clip.duration, 0.001)
        has_root_motion = any(
            "bip001" in bones[i].name.lower() and (
                self._lookup_curve(clip.pos_curves, self._bone_keys[i]) is not None
                or self._lookup_curve(clip.rot_curves, self._bone_keys[i]) is not None
            )
            for i in range(len(bones))
        )
        worlds: list[np.ndarray] = []
        ident = np.eye(4, dtype=np.float32)
        for i, b in enumerate(bones):
            keys = self._bone_keys[i] if i < len(self._bone_keys) else (b.name,)
            pc = self._lookup_curve(clip.pos_curves, keys)
            rc = self._lookup_curve(clip.rot_curves, keys)
            sc_c = self._lookup_curve(clip.scale_curves, keys)
            if b.parent < 0:
                if has_root_motion:
                    worlds.append(ident)
                else:
                    worlds.append(self._rest_local_rel[i] if i < len(self._rest_local_rel) else ident)
                continue
            pw = worlds[b.parent]
            if pc is None and rc is None and sc_c is None:
                loc = self._rest_local_rel[i] if i < len(self._rest_local_rel) else ident
                worlds.append(pw @ loc)
                continue
            if pc is not None:
                p = _eval_vec3_curve(*pc, t)
                if b.name.lower() in ("bip001", "bone_root", "root"):
                    pos = np.array([0.0, p[1], 0.0], dtype=np.float32)
                else:
                    pos = np.array([-p[0], p[1], p[2]], dtype=np.float32)
            else:
                pos = self._rest_local_rel[i][:3, 3].copy() if i < len(self._rest_local_rel) else np.zeros(3, dtype=np.float32)
            if rc is not None:
                q = _eval_quat_curve(*rc, t)
                rot = np.array([q[0], -q[1], -q[2], q[3]], dtype=np.float32)
            else:
                rot = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
            scl = _eval_vec3_curve(*sc_c, t) if sc_c is not None else np.ones(3, dtype=np.float32)
            loc = _trs_to_mat4(pos, rot, scl)
            worlds.append(pw @ loc)
        return worlds

    def _draw_skeleton(self):
        bones = self.scene.bones if self.scene and self.scene.bones else []
        if not bones:
            return
        worlds = self._eval_animated_bones()
        if not worlds:
            return
        gl.glDisable(gl.GL_LIGHTING)
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glLineWidth(2.0)
        gl.glBegin(gl.GL_LINES)
        gl.glColor4f(0.20, 0.92, 1.0, 0.95)
        for idx, b in enumerate(bones):
            if b.parent >= 0 and not re.search(r"\.\d+$", b.name):
                p0 = worlds[b.parent][:3, 3]
                p1 = worlds[idx][:3, 3]
                gl.glVertex3f(float(p0[0]), float(p0[1]), float(p0[2]))
                gl.glVertex3f(float(p1[0]), float(p1[1]), float(p1[2]))
        gl.glEnd()
        gl.glPointSize(4.5)
        gl.glBegin(gl.GL_POINTS)
        gl.glColor4f(1.0, 0.78, 0.20, 1.0)
        for w in worlds:
            p = w[:3, 3]
            gl.glVertex3f(float(p[0]), float(p[1]), float(p[2]))
        gl.glEnd()
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_LIGHTING)

    def mousePressEvent(self, event):
        self._last_pos = event.position()
        if event.button() == Qt.LeftButton and not (event.modifiers() & Qt.ShiftModifier):
            self._drag_mode = "orbit"
        else:
            self._drag_mode = "pan"

    def mouseMoveEvent(self, event):
        if self._last_pos is None:
            return
        dx = event.position().x() - self._last_pos.x()
        dy = event.position().y() - self._last_pos.y()
        self._last_pos = event.position()
        if self._drag_mode == "orbit":
            self.yaw -= dx * 0.45
            self.pitch = max(-89.0, min(89.0, self.pitch + dy * 0.45))
        else:
            yr = math.radians(self.yaw)
            right = np.array([math.cos(yr), 0.0, -math.sin(yr)], dtype=np.float32)
            up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
            scale = self.distance * 0.0018
            self.target -= right * (dx * scale)
            self.target += up * (dy * scale)
        self.update()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        factor = 0.88 if delta > 0 else 1.14
        min_dist = max(0.002, (self.scene.radius * 0.01) if self.scene else 0.05)
        self.distance = max(min_dist, self.distance * factor)
        self.update()


class NativeViewerWidget(QWidget):
    def __init__(self, choose_output_cb=None, parent=None):
        super().__init__(parent)
        self._choose_output_cb = choose_output_cb
        self._workers: list[QThread] = []
        self._anim_clip: UnityAnimClip | None = None
        self._anim_playing = False
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(33)
        self._anim_timer.timeout.connect(self._on_anim_tick)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        bar = QHBoxLayout()
        self.status_lbl = QLabel("Native 3D GLB Viewer — Select a Character or Map")
        self.status_lbl.setStyleSheet("color: #94a3b8; font-weight: 600;")
        bar.addWidget(self.status_lbl, 1)

        if choose_output_cb is not None:
            btn_out = QPushButton("Output Folder...")
            btn_out.clicked.connect(choose_output_cb)
            bar.addWidget(btn_out)

        btn_open = QPushButton("Open .glb / .obj...")
        btn_open.clicked.connect(self._open_file_dialog)
        btn_refresh = QPushButton("Refresh Catalog")
        btn_refresh.clicked.connect(self.refresh_catalog)
        btn_reset = QPushButton("Reset Camera")
        btn_reset.clicked.connect(lambda: self.viewport.reset_camera())
        self.btn_wire = QPushButton("Wireframe")
        self.btn_wire.setCheckable(True)
        self.btn_wire.toggled.connect(self._toggle_wire)
        self.btn_tex = QPushButton("Textures")
        self.btn_tex.setCheckable(True)
        self.btn_tex.setChecked(True)
        self.btn_tex.toggled.connect(self._toggle_tex)
        self.btn_toon = QPushButton("Toon Shader")
        self.btn_toon.setCheckable(True)
        self.btn_toon.setChecked(True)
        self.btn_toon.toggled.connect(self._toggle_toon)

        for b in (btn_open, btn_refresh, btn_reset, self.btn_wire, self.btn_tex, self.btn_toon):
            bar.addWidget(b)
        root.addLayout(bar)

        anim_bar = QHBoxLayout()
        anim_bar.addWidget(QLabel("Animation:"))
        self.anim_combo = QComboBox()
        self.anim_combo.setMinimumWidth(220)
        self.anim_combo.currentIndexChanged.connect(self._on_anim_combo_changed)
        anim_bar.addWidget(self.anim_combo)

        btn_load_anim = QPushButton("Load .anim...")
        btn_load_anim.clicked.connect(self._open_anim_dialog)
        anim_bar.addWidget(btn_load_anim)

        self.btn_anim_play = QPushButton("Play")
        self.btn_anim_play.setEnabled(False)
        self.btn_anim_play.clicked.connect(self._toggle_anim_play)
        anim_bar.addWidget(self.btn_anim_play)

        self.anim_slider = QSlider(Qt.Horizontal)
        self.anim_slider.setRange(0, 1000)
        self.anim_slider.setEnabled(False)
        self.anim_slider.valueChanged.connect(self._on_anim_scrub)
        anim_bar.addWidget(self.anim_slider, 1)

        self.anim_time_lbl = QLabel("0.00 / 0.00s")
        self.anim_time_lbl.setStyleSheet("color: #94a3b8;")
        anim_bar.addWidget(self.anim_time_lbl)

        self.btn_skel = QPushButton("Skeleton")
        self.btn_skel.setCheckable(True)
        self.btn_skel.setChecked(True)
        self.btn_skel.toggled.connect(self._toggle_skeleton)
        anim_bar.addWidget(self.btn_skel)

        self.btn_weapons = QPushButton("Weapons")
        self.btn_weapons.setCheckable(True)
        self.btn_weapons.setChecked(True)
        self.btn_weapons.toggled.connect(self._toggle_weapons)
        anim_bar.addWidget(self.btn_weapons)
        root.addLayout(anim_bar)

        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter exported .glb models...")
        self.search_edit.textChanged.connect(self._filter_tree)
        left_lay.addWidget(self.search_edit)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("Exported 3D Models")
        self.tree.itemClicked.connect(self._on_item_clicked)
        left_lay.addWidget(self.tree, 1)
        left.setMaximumWidth(320)

        self.viewport = GLBViewport()
        splitter.addWidget(left)
        splitter.addWidget(self.viewport)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

    def _toggle_wire(self, checked: bool):
        self.viewport.wireframe = checked
        self.viewport.update()

    def _toggle_tex(self, checked: bool):
        self.viewport.show_textures = checked
        self.viewport.update()

    def _toggle_toon(self, checked: bool):
        self.viewport.toon_enabled = checked
        self.viewport.update()

    def _open_file_dialog(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Open 3D Model", str(cfg.FINAL_ROOT), "3D Models (*.glb *.gltf *.obj)"
        )
        if f:
            self.load_model(Path(f))

    def refresh_catalog(self):
        self.tree.clear()
        roots = [
            cfg.FINAL_ROOT / "Exports",
            cfg.FINAL_ROOT / "Characters",
            cfg.FINAL_ROOT / "Maps_Combined",
            cfg.FINAL_ROOT,
        ]
        seen: set[Path] = set()
        chars_node = QTreeWidgetItem(self.tree, ["Characters"])
        maps_node = QTreeWidgetItem(self.tree, ["Maps / Stages"])
        other_node = QTreeWidgetItem(self.tree, ["Other Models"])

        for r in roots:
            if not r.is_dir():
                continue
            for glb in sorted(r.rglob("*.glb")):
                rp = glb.resolve()
                if rp in seen:
                    continue
                seen.add(rp)
                name = glb.stem
                if name.startswith("Avatar_") or glb.parent.name.startswith("Avatar_"):
                    parent = chars_node
                elif any(t in name or t in str(glb.parent) for t in ("Stage", "Map", "Env_", "Site", "Mine", "Ring", "Suburbs", "SkyScraper")):
                    parent = maps_node
                else:
                    parent = other_node
                it = QTreeWidgetItem(parent, [name])
                it.setData(0, Qt.UserRole, str(glb))
                it.setToolTip(0, str(glb))

        for node in (chars_node, maps_node, other_node):
            node.setExpanded(True)
            node.setText(0, f"{node.text(0)} ({node.childCount()})")
        self.status_lbl.setText(f"Native 3D Viewer • {len(seen)} .glb model(s) in {cfg.FINAL_ROOT}")

    def _filter_tree(self, txt: str):
        q = txt.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                ch = top.child(j)
                ch.setHidden(bool(q) and q not in ch.text(0).lower())

    def _on_item_clicked(self, item: QTreeWidgetItem, _col: int):
        p = item.data(0, Qt.UserRole)
        if p:
            self.load_model(Path(p))

    def load_model(self, path: Path):
        self.status_lbl.setText(f"Loading {path.name}...")
        w = _SceneLoadWorker(path)
        w.loaded.connect(self._on_scene_loaded)
        w.failed.connect(lambda err: self.status_lbl.setText(f"Failed to load {path.name}: {err}"))
        self._workers.append(w)
        w.start()

    def _toggle_skeleton(self, checked: bool):
        self.viewport.show_skeleton = checked
        self.viewport.update()

    def _toggle_weapons(self, checked: bool):
        self.viewport.show_weapons = checked
        self.viewport.update()

    def _populate_anim_combo(self, model_path: Path):
        self.anim_combo.blockSignals(True)
        self.anim_combo.clear()
        self.anim_combo.addItem("(None — Rest Pose)", None)
        anim_files: list[Path] = []
        for cand_dir in (model_path.parent / "AnimationClip", model_path.parent):
            if cand_dir.is_dir():
                anim_files.extend(sorted(cand_dir.glob("*.anim")))
        seen: set[Path] = set()
        for af in anim_files:
            rp = af.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            self.anim_combo.addItem(af.stem, str(af))
        self.anim_combo.blockSignals(False)

    def _open_anim_dialog(self):
        start_dir = str(self.viewport.scene.source_path.parent) if self.viewport.scene else str(cfg.FINAL_ROOT)
        f, _ = QFileDialog.getOpenFileName(
            self, "Load Unity AnimationClip (.anim)", start_dir, "AnimationClip (*.anim);;All Files (*)"
        )
        if not f:
            return
        af = Path(f)
        self.anim_combo.blockSignals(True)
        idx = self.anim_combo.findData(str(af))
        if idx < 0:
            self.anim_combo.addItem(af.stem, str(af))
            idx = self.anim_combo.count() - 1
        self.anim_combo.setCurrentIndex(idx)
        self.anim_combo.blockSignals(False)
        self.load_animation(af)

    def _on_anim_combo_changed(self, idx: int):
        p = self.anim_combo.itemData(idx)
        if not p:
            self._stop_animation()
            self._anim_clip = None
            self.viewport.set_animation(None)
            self.btn_anim_play.setEnabled(False)
            self.anim_slider.setEnabled(False)
            self.anim_slider.setValue(0)
            self.anim_time_lbl.setText("0.00 / 0.00s")
            return
        self.load_animation(Path(p))

    def load_animation(self, path: Path):
        try:
            clip = load_unity_anim(path)
        except Exception as e:
            self.status_lbl.setText(f"Failed to load .anim {path.name}: {e}")
            return
        self._anim_clip = clip
        self.viewport.set_animation(clip)
        self.btn_anim_play.setEnabled(True)
        self.anim_slider.setEnabled(True)
        self.anim_slider.blockSignals(True)
        self.anim_slider.setValue(0)
        self.anim_slider.blockSignals(False)
        self.anim_time_lbl.setText(f"0.00 / {clip.duration:.2f}s")
        self._anim_playing = True
        self.btn_anim_play.setText("Pause")
        self._anim_timer.start()
        bones_n = len(self.viewport.scene.bones) if self.viewport.scene and self.viewport.scene.bones else 0
        self.status_lbl.setText(
            f"Playing {clip.name} ({clip.duration:.2f}s) • {len(clip.rot_curves)} rot / {len(clip.pos_curves)} pos curves • {bones_n} bones"
        )

    def _stop_animation(self):
        self._anim_playing = False
        self._anim_timer.stop()
        self.btn_anim_play.setText("Play")

    def _toggle_anim_play(self):
        if not self._anim_clip:
            return
        if self._anim_playing:
            self._stop_animation()
        else:
            self._anim_playing = True
            self.btn_anim_play.setText("Pause")
            self._anim_timer.start()

    def _on_anim_tick(self):
        if not self._anim_clip or not self._anim_playing:
            return
        t = (self.viewport.anim_time + 0.033) % max(self._anim_clip.duration, 0.001)
        self.viewport.set_anim_time(t)
        self.anim_slider.blockSignals(True)
        self.anim_slider.setValue(int((t / max(self._anim_clip.duration, 0.001)) * 1000))
        self.anim_slider.blockSignals(False)
        self.anim_time_lbl.setText(f"{t:.2f} / {self._anim_clip.duration:.2f}s")

    def _on_anim_scrub(self, val: int):
        if not self._anim_clip:
            return
        t = (val / 1000.0) * self._anim_clip.duration
        self.viewport.set_anim_time(t)
        self.anim_time_lbl.setText(f"{t:.2f} / {self._anim_clip.duration:.2f}s")

    def _on_scene_loaded(self, scene: GLScene):
        self._stop_animation()
        self._anim_clip = None
        self.btn_anim_play.setEnabled(False)
        self.anim_slider.setEnabled(False)
        self.anim_slider.setValue(0)
        self.anim_time_lbl.setText("0.00 / 0.00s")
        self.viewport.set_scene(scene)
        has_wpn = any(sm.is_weapon for sm in scene.submeshes)
        has_body = any(not sm.is_weapon for sm in scene.submeshes)
        self.btn_weapons.setEnabled(has_wpn)
        self.btn_weapons.setChecked(not (has_wpn and has_body))
        self._populate_anim_combo(scene.source_path)
        self.status_lbl.setText(
            f"{scene.source_path.name} • {len(scene.submeshes)} meshes • "
            f"{scene.triangle_count:,} tris • {scene.vertex_count:,} verts "
            f"(Drag: Orbit, Shift/Right-Drag: Pan, Wheel: Zoom)"
        )
