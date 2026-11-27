"""Native embedded OpenGL 3D Viewer for OpenZenless (.glb / .obj) without HTML/WebEngine."""
from __future__ import annotations

import concurrent.futures
import ctypes
import gc
import io
import json
import math
import os
import re
import struct
import sys
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from pygltflib import GLTF2
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QOpenGLContext
from PySide6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import config as cfg

cfg.configure_gpu_env()

import OpenGL.contextdata as _gl_ctxdata
import OpenGL.platform as _gl_plat
from OpenGL import GL as gl

_orig_plat_get_ctx = getattr(_gl_plat.PLATFORM, "GetCurrentContext", None) or getattr(_gl_plat, "GetCurrentContext", None)


def _safe_gl_get_current_context():
    try:
        c = _orig_plat_get_ctx() if _orig_plat_get_ctx is not None else None
    except Exception:
        c = None
    if not c:
        qctx = QOpenGLContext.currentContext()
        return id(qctx) if qctx is not None else 1
    return c


def _safe_gl_get_context(context=None):
    if context is not None:
        return context
    return _safe_gl_get_current_context()


_gl_ctxdata.getContext = _safe_gl_get_context
_gl_plat.GetCurrentContext = _safe_gl_get_current_context
_gl_plat.CurrentContextIsValid = _safe_gl_get_current_context
if _gl_plat.PLATFORM is not None:
    try:
        _gl_plat.PLATFORM.GetCurrentContext = _safe_gl_get_current_context
        _gl_plat.PLATFORM.CurrentContextIsValid = _safe_gl_get_current_context
    except Exception:
        pass


def _release_os_memory() -> None:
    """Run Python GC and reclaim freed C-heap arenas across Linux and Windows."""
    cfg.release_os_memory()


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
    bound_radius: float = 0.0
    toon_smoothness: float = 0.05
    toon_specular: float = 0.18
    rim_glow: float = 0.35
    rim_color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    shallow_tint: tuple[float, float, float] = (0.94, 0.91, 0.93)
    shadow_tint: tuple[float, float, float] = (0.84, 0.80, 0.86)
    spec_color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    is_face: bool = False
    node_name: str = ""
    mat_name: str = ""
    bind_positions: np.ndarray | None = None
    bind_normals: np.ndarray | None = None
    skin_joints: np.ndarray | None = None
    skin_weights: np.ndarray | None = None
    skin_idx: int = -1
    is_180y_obj: bool = False


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
    skins: list[tuple[list[int], np.ndarray]] | None = None


def _parse_unity_anim_section(lines: list[str], start_idx: int, is_quat: bool) -> tuple[dict[str, tuple[np.ndarray, np.ndarray]], int]:
    """Parse one Unity YAML curve list (m_RotationCurves / m_EulerCurves / m_PositionCurves / m_ScaleCurves)."""
    curves: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    leaf_entries: list[tuple[str, np.ndarray, np.ndarray]] = []
    leaf_counts: dict[str, int] = {}
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
                if leaf:
                    leaf_counts[leaf] = leaf_counts.get(leaf, 0) + 1
                    leaf_entries.append((leaf, t_arr, v_arr))
            cur_times, cur_vals, cur_time = [], [], None
        i += 1
    for leaf, t_arr, v_arr in leaf_entries:
        if leaf_counts.get(leaf, 0) == 1 and leaf not in curves:
            curves[leaf] = (t_arr, v_arr)
    return curves, i


def _unity_euler_deg_to_quat(vals: np.ndarray) -> np.ndarray:
    """Convert Unity intrinsic ZXY Euler degrees (N, 3) to quaternions (N, 4) in xyzw order."""
    rad = np.deg2rad(vals.astype(np.float64)) * 0.5
    cx, cy, cz = np.cos(rad[:, 0]), np.cos(rad[:, 1]), np.cos(rad[:, 2])
    sx, sy, sz = np.sin(rad[:, 0]), np.sin(rad[:, 1]), np.sin(rad[:, 2])
    q = np.empty((len(vals), 4), dtype=np.float32)
    q[:, 0] = cy * sx * cz + sy * cx * sz
    q[:, 1] = sy * cx * cz - cy * sx * sz
    q[:, 2] = cy * cx * sz - sy * sx * cz
    q[:, 3] = cy * cx * cz + sy * sx * sz
    return q


def load_unity_anim(path: Path) -> UnityAnimClip:
    """Parse a Unity YAML .anim file exported by AnimeStudio/AssetStudio."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    name = path.stem
    stop_time = 0.0
    rot_curves: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    euler_curves: dict[str, tuple[np.ndarray, np.ndarray]] = {}
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
        elif s.startswith("m_EulerCurves:"):
            c, i = _parse_unity_anim_section(lines, i, is_quat=False)
            euler_curves.update(c)
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

    for k, (t_arr, e_vals) in euler_curves.items():
        if k not in rot_curves and len(e_vals):
            rot_curves[k] = (t_arr, _unity_euler_deg_to_quat(e_vals))

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
        dot = float(np.dot(q0, q1))
        if dot < 0.0:
            q1 = -q1
            dot = -dot
        if dot > 0.9995:
            q = q0 * (1.0 - a) + q1 * a
        else:
            dot = min(1.0, max(-1.0, dot))
            omega = math.acos(dot)
            sin_o = math.sin(omega)
            q = (q0 * math.sin((1.0 - a) * omega) + q1 * math.sin(a * omega)) / sin_o
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


def _read_glb_fast(path: Path) -> tuple[dict, bytes]:
    with open(path, "rb") as f:
        header = f.read(12)
        if len(header) < 12:
            raise ValueError(f"Truncated GLB: {path}")
        magic, _ver, total_len = struct.unpack("<III", header)
        if magic != 0x46546C67:
            raise ValueError(f"Not a GLB file: {path}")
        jdata: dict = {}
        bdata = b""
        pos = 12
        while pos + 8 <= total_len:
            ch = f.read(8)
            if len(ch) < 8:
                break
            clen, ctype = struct.unpack("<II", ch)
            payload = f.read(clen)
            pos += 8 + clen
            if ctype == 0x4E4F534A:
                jdata = json.loads(payload.decode("utf-8"))
            elif ctype == 0x004E4942:
                bdata = payload
    return jdata, bdata


def _read_accessor(gltf, blob: bytes, acc_idx: int) -> np.ndarray:
    acc = gltf["accessors"][acc_idx] if isinstance(gltf, dict) else gltf.accessors[acc_idx]
    bvs = gltf["bufferViews"] if isinstance(gltf, dict) else gltf.bufferViews
    if isinstance(acc, dict):
        bv = bvs[acc["bufferView"]]
        comp_type = acc["componentType"]
        acc_type = acc["type"]
        stride_raw = bv.get("byteStride")
        offset = (bv.get("byteOffset") or 0) + (acc.get("byteOffset") or 0)
        count = acc["count"]
        normalized = bool(acc.get("normalized"))
    else:
        bv = bvs[acc.bufferView]
        comp_type = acc.componentType
        acc_type = acc.type
        stride_raw = bv.byteStride
        offset = (bv.byteOffset or 0) + (acc.byteOffset or 0)
        count = acc.count
        normalized = bool(acc.normalized)

    dtype = _COMP_DTYPES[comp_type]
    ncomp = _TYPE_COUNTS[acc_type]
    item_size = np.dtype(dtype).itemsize * ncomp
    stride = stride_raw or item_size

    if stride == item_size:
        arr = np.frombuffer(blob, dtype=dtype, count=count * ncomp, offset=offset)
        arr = arr.reshape((count, ncomp)) if ncomp > 1 else arr.reshape((count,))
    else:
        raw = np.frombuffer(blob, dtype=np.uint8, count=count * stride, offset=offset)
        raw = raw.reshape((count, stride))[:, :item_size].copy()
        arr = raw.view(dtype=dtype)
        arr = arr.reshape((count, ncomp)) if ncomp > 1 else arr.reshape((count,))

    if normalized and comp_type != 5126:
        info = np.iinfo(dtype)
        arr = arr.astype(np.float32) / float(info.max)
    return arr


def _node_local_matrix(node) -> np.ndarray:
    matrix = node.get("matrix") if isinstance(node, dict) else node.matrix
    if matrix:
        return np.array(matrix, dtype=np.float32).reshape((4, 4)).T
    mat = np.eye(4, dtype=np.float32)
    trans = node.get("translation") if isinstance(node, dict) else node.translation
    if trans:
        mat[0:3, 3] = trans
    rot_q = node.get("rotation") if isinstance(node, dict) else node.rotation
    if rot_q:
        x, y, z, w = rot_q
        rot = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ], dtype=np.float32)
        mat[0:3, 0:3] = rot
    scl = node.get("scale") if isinstance(node, dict) else node.scale
    if scl:
        sx, sy, sz = scl
        mat[0:3, 0] *= sx
        mat[0:3, 1] *= sy
        mat[0:3, 2] *= sz
    return mat


def _decode_gltf_texture(
    gltf,
    blob: bytes,
    tex_idx: int,
    base_dir: Path,
    cache: dict[int, tuple[bytes, int, int, int, float] | None],
    max_dim: int = 2048,
    img_cache: dict[int, tuple[bytes, int, int, int, float] | None] | None = None,
) -> tuple[bytes, int, int, int, float] | None:
    if tex_idx in cache:
        return cache[tex_idx]
    try:
        texs = gltf["textures"] if isinstance(gltf, dict) else gltf.textures
        imgs = gltf["images"] if isinstance(gltf, dict) else gltf.images
        bvs = gltf["bufferViews"] if isinstance(gltf, dict) else gltf.bufferViews
        tex = texs[tex_idx]
        src = tex.get("source") if isinstance(tex, dict) else tex.source
        if src is None:
            cache[tex_idx] = None
            return None
        if img_cache is not None and src in img_cache:
            res = img_cache[src]
            cache[tex_idx] = res
            return res
        img_obj = imgs[src]
        bv_idx = img_obj.get("bufferView") if isinstance(img_obj, dict) else img_obj.bufferView
        uri = img_obj.get("uri") if isinstance(img_obj, dict) else img_obj.uri
        raw_bytes = None
        if bv_idx is not None:
            bv = bvs[bv_idx]
            off = (bv.get("byteOffset") if isinstance(bv, dict) else bv.byteOffset) or 0
            blen = bv["byteLength"] if isinstance(bv, dict) else bv.byteLength
            raw_bytes = blob[off : off + blen]
        elif uri and not uri.startswith("data:"):
            p = base_dir / uri
            if p.is_file():
                raw_bytes = p.read_bytes()
        if not raw_bytes:
            if img_cache is not None:
                img_cache[src] = None
            cache[tex_idx] = None
            return None
        im = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
        if max(im.size) > max_dim:
            im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        rgba = im.tobytes()
        a_ch = np.frombuffer(rgba, dtype=np.uint8)[3::4]
        a_min = int(a_ch.min()) if len(a_ch) else 255
        a_mean = float(a_ch.mean()) if len(a_ch) else 255.0
        res = (rgba, im.width, im.height, a_min, a_mean)
        if img_cache is not None:
            img_cache[src] = res
        cache[tex_idx] = res
        return res
    except Exception:
        cache[tex_idx] = None
        return None


def _load_disk_texture(
    png_path: Path,
    cache: dict[str, tuple[bytes, int, int, int, float] | None],
    max_dim: int = 2048,
) -> tuple[bytes, int, int, int, float] | None:
    ckey = str(png_path)
    if ckey in cache:
        return cache[ckey]
    try:
        im = Image.open(png_path).convert("RGBA")
        if max(im.size) > max_dim:
            im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        rgba = im.tobytes()
        a_ch = np.frombuffer(rgba, dtype=np.uint8)[3::4]
        a_min = int(a_ch.min()) if len(a_ch) else 255
        a_mean = float(a_ch.mean()) if len(a_ch) else 255.0
        res = (rgba, im.width, im.height, a_min, a_mean)
        cache[ckey] = res
        return res
    except Exception:
        cache[ckey] = None
        return None


def _load_game_materials(model_dir: Path) -> dict[str, dict]:
    json_files: list[Path] = []
    for cand in (model_dir / "Material", model_dir / "Animator" / "Materials"):
        if cand.is_dir():
            json_files.extend(sorted(cand.glob("*.json")))
    if model_dir.is_dir():
        for mat_sub in sorted(model_dir.rglob("Materials")):
            if mat_sub.is_dir():
                json_files.extend(sorted(mat_sub.glob("*.json")))
    if not json_files:
        return {}
    out: dict[str, dict] = {}
    parsed: list[tuple[str, dict]] = []
    seen_keys: set[str] = set()
    pid_to_tex: dict[int, str] = {}
    for p in json_files:
        key = p.stem.lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        try:
            d = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        parsed.append((key, d))
        te_scan = (d.get("m_SavedProperties") or {}).get("m_TexEnvs") or {}
        if isinstance(te_scan, dict):
            for sv in te_scan.values():
                tx = sv.get("m_Texture") if isinstance(sv, dict) else None
                if not isinstance(tx, dict):
                    continue
                pid = tx.get("m_PathID")
                nm = tx.get("m_Name") or tx.get("Name")
                if (
                    isinstance(pid, int)
                    and not isinstance(pid, bool)
                    and pid != 0
                    and isinstance(nm, str)
                    and nm.strip()
                    and pid not in pid_to_tex
                ):
                    pid_to_tex[pid] = Path(nm.strip()).stem.lower()

    for key, d in parsed:
        try:
            shader = str((d.get("m_Shader") or {}).get("Name") or (d.get("m_Shader") or {}).get("m_Name") or "")
            sh_low = shader.lower()
            sp = d.get("m_SavedProperties") or {}
            fl = sp.get("m_Floats") or {}
            cl = sp.get("m_Colors") or {}
            te = sp.get("m_TexEnvs") or {}
            col_d = cl.get("_BaseColor") or cl.get("_Color")
            shallow_d = cl.get("_ShallowColor")
            deep_d = cl.get("_DeepColor")
            shadow_d = cl.get("_ShadowColor")
            spec_d = cl.get("_SpecularColor")
            rim_d = cl.get("_RimGlowLightColor")
            if isinstance(rim_d, dict):
                rr, rg, rb = float(rim_d.get("r", 1.0)), float(rim_d.get("g", 1.0)), float(rim_d.get("b", 1.0))
                max_c = max(rr, rg, rb, 1e-4)
                scale = min(1.0 / max_c, 1.8) if max_c < 0.8 else 1.0
                rim_col = (
                    max(0.0, min(1.0, rr * scale)),
                    max(0.0, min(1.0, rg * scale)),
                    max(0.0, min(1.0, rb * scale)),
                )
            else:
                rim_col = (1.0, 1.0, 1.0)
            main_tex: str | None = None
            fallback_non_diff: str | None = None
            for slot in ("_BaseMap", "_BaseColorMap", "_ColorIDMap", "_MainTex", "_DiffuseTex", "_Diffuse"):
                sv = te.get(slot)
                tx = sv.get("m_Texture") if isinstance(sv, dict) else None
                if isinstance(tx, dict):
                    nm = tx.get("m_Name") or tx.get("Name")
                    cand: str | None = None
                    if isinstance(nm, str) and nm.strip():
                        cand = Path(nm.strip()).stem.lower()
                    else:
                        pid = tx.get("m_PathID")
                        if isinstance(pid, int) and not isinstance(pid, bool) and pid != 0 and pid in pid_to_tex:
                            cand = pid_to_tex[pid]
                    if cand:
                        if cand.endswith(("_n", "_m")):
                            if fallback_non_diff is None:
                                fallback_non_diff = cand
                            continue
                        main_tex = cand
                        break
            if main_tex is None and fallback_non_diff is not None:
                main_tex = fallback_non_diff
            is_water = ("water" in sh_low) or (
                main_tex is None and "_water_" in key and isinstance(shallow_d, dict)
            )
            if isinstance(col_d, dict):
                mat_color: tuple[float, float, float, float] | None = (
                    float(col_d.get("r", 1.0)),
                    float(col_d.get("g", 1.0)),
                    float(col_d.get("b", 1.0)),
                    float(col_d.get("a", 1.0)),
                )
            else:
                mat_color = None
            if is_water and main_tex is None:
                if isinstance(shallow_d, dict) and isinstance(deep_d, dict):
                    mat_color = (
                        float(shallow_d.get("r", 0.14)) * 0.65 + float(deep_d.get("r", 0.0)) * 0.35,
                        float(shallow_d.get("g", 0.38)) * 0.65 + float(deep_d.get("g", 0.71)) * 0.35,
                        float(shallow_d.get("b", 0.48)) * 0.65 + float(deep_d.get("b", 1.0)) * 0.35,
                        0.78,
                    )
                elif isinstance(shallow_d, dict):
                    mat_color = (
                        float(shallow_d.get("r", 0.14)),
                        float(shallow_d.get("g", 0.38)),
                        float(shallow_d.get("b", 0.48)),
                        0.78,
                    )
                else:
                    mat_color = (0.14, 0.42, 0.56, 0.78)
            is_transparent_sh = "transparent" in sh_low
            is_decal = (not shader and "_BaseColorMap" in te) or ("_decal_" in key)
            raw_alpha_clip = float(fl.get("_AlphaClip", 0.0)) >= 0.5
            cutoff = float(fl.get("_Cutoff", 0.5))
            raw_blend = float(fl.get("_Blend", 0.0)) >= 0.5
            if is_transparent_sh and raw_alpha_clip and cutoff >= 0.95:
                alpha_clip = False
                blend = True
            else:
                alpha_clip = raw_alpha_clip
                blend = raw_blend or is_transparent_sh or is_water
            out[key] = {
                "shader": shader,
                "is_decal": is_decal,
                "is_water": is_water,
                "color": mat_color,
                "alpha_clip": alpha_clip,
                "cutoff": cutoff,
                "blend": blend,
                "double_sided": float(fl.get("_Cull", 2.0)) < 0.5,
                "smoothness": max(0.025, min(0.14, float(fl.get("_AlbedoSmoothness", 0.05)))),
                "specular": max(
                    0.06,
                    min(0.85, float(fl.get("_ToonSpecular", 0.015)) * 12.0 + float(fl.get("_SpecIntensity", 0.1)) * 0.55),
                ),
                "rim_glow": max(0.0, min(1.0, float(fl.get("_RimGlow", 0.35)))),
                "rim_color": rim_col,
                "shallow_tint": (
                    max(0.65, min(1.0, float(shallow_d.get("r", 0.94)))),
                    max(0.65, min(1.0, float(shallow_d.get("g", 0.91)))),
                    max(0.65, min(1.0, float(shallow_d.get("b", 0.93)))),
                ) if isinstance(shallow_d, dict) else (0.94, 0.91, 0.93),
                "shadow_tint": (
                    max(0.62, min(1.0, float(shadow_d.get("r", 0.84)))),
                    max(0.62, min(1.0, float(shadow_d.get("g", 0.80)))),
                    max(0.62, min(1.0, float(shadow_d.get("b", 0.86)))),
                ) if isinstance(shadow_d, dict) else (0.84, 0.80, 0.86),
                "spec_color": (
                    max(0.0, min(1.4, float(spec_d.get("r", 1.0)))),
                    max(0.0, min(1.4, float(spec_d.get("g", 1.0)))),
                    max(0.0, min(1.4, float(spec_d.get("b", 1.0)))),
                ) if isinstance(spec_d, dict) else (1.0, 1.0, 1.0),
                "main_tex": main_tex,
            }
        except Exception:
            pass
    return out


def _clean_mat_key(nm: str) -> str:
    nl = re.sub(r"\.[0-9]+$", "", nm.lower().strip())
    nl = re.sub(r"\s*\[[^\]]+\]$", "", nl)
    nl = re.sub(r"\s*\([0-9]+\)$", "", nl).strip()
    nl = re.sub(r"^separatemesh_.*?_model_", "", nl)
    nl = re.sub(r"^separatemesh_avatar_[^_]+_[^_]+_[^_]+_", "", nl)
    nl = re.sub(r"_lod[0-9]+$", "", nl)
    return nl


def _match_game_material(mat_map: dict[str, dict], *names: str) -> dict | None:
    if not mat_map:
        return None
    part_tokens = ("face", "body", "hair", "eye", "eyes", "eyebrow", "eyebrows", "weapon", "coat", "tail", "hand", "glass", "eyeglass", "fx")
    variant_tokens = ("_ui", "_npcfx", "_fx", "_cs")
    for nm in names:
        if not nm:
            continue
        nl = _clean_mat_key(nm)
        if not nl:
            continue
        exact_cands = [nl, f"mat_{nl}"]
        m_body_sub = re.search(r"^(.+_body_[0-9]+)_[0-9]+$", nl)
        if m_body_sub:
            bbase = m_body_sub.group(1)
            exact_cands.extend((bbase, f"mat_{bbase}"))
        for ek in exact_cands:
            if ek in mat_map:
                return mat_map[ek]

    best_score = 0.0
    best_val = None
    for nm in names:
        if not nm:
            continue
        nl = _clean_mat_key(nm)
        if not nl:
            continue
        nl_toks = [t for t in nl.split("_") if len(t) >= 2]
        nl_parts = {t for t in nl_toks if t in part_tokens}
        m_num = re.search(r"(?:_body_|_map)([0-9]+)(?:_[0-9]+|$)", nl)
        nl_num = m_num.group(1) if m_num else None

        for k, v in mat_map.items():
            k_norm = k[4:] if k.startswith("mat_") else k
            k_toks = [t for t in k_norm.split("_") if len(t) >= 2]
            k_parts = {t for t in k_toks if t in part_tokens}
            mk_num = re.search(r"(?:_body_|_map)([0-9]+)(?:_[0-9]+|$)", k_norm)
            k_num = mk_num.group(1) if mk_num else None

            if nl_num is not None and k_num is not None and nl_num != k_num:
                continue
            if nl_parts and k_parts and not (nl_parts & k_parts):
                continue

            sub_hit = (nl in k_norm or k_norm in nl)
            shared = set(nl_toks) & set(k_toks)
            part_shared = nl_parts & k_parts
            if not sub_hit and len(shared) < 2:
                continue
            if not sub_hit and not part_shared and len(shared) < 3:
                continue

            sc = 0.0
            if nl == k_norm:
                sc += 100.0
            elif sub_hit:
                sc += 40.0 + min(len(nl), len(k_norm)) * 0.5
            sc += len(shared) * 12.0 + len(part_shared) * 20.0
            if nl_num is not None and nl_num == k_num:
                sc += 25.0
            if re.search(r"_lod[0-9]+$", k_norm) and "_lod" not in nl:
                sc -= 15.0
            for vt in variant_tokens:
                if k_norm.endswith(vt) and not nl.endswith(vt):
                    sc -= 10.0
            sc -= abs(len(k_norm) - len(nl)) * 0.1
            if sc > best_score and sc >= 30.0:
                best_score = sc
                best_val = v
    return best_val


def load_glb_scene(path: Path) -> GLScene:
    path = Path(path)
    is_fast_glb = path.suffix.lower() == ".glb"
    if is_fast_glb:
        gltf, blob = _read_glb_fast(path)
        nodes = gltf.get("nodes") or []
        meshes = gltf.get("meshes") or []
        materials = gltf.get("materials") or []
        images = gltf.get("images") or []
        scenes = gltf.get("scenes") or []
        skins = gltf.get("skins") or []
        scene_idx = gltf.get("scene") or 0
    else:
        gltf = GLTF2().load(str(path))
        blob = gltf.binary_blob() or b""
        if not blob and gltf.buffers and gltf.buffers[0].uri:
            buf_path = path.parent / gltf.buffers[0].uri
            if buf_path.is_file():
                blob = buf_path.read_bytes()
        nodes = gltf.nodes or []
        meshes = gltf.meshes or []
        materials = gltf.materials or []
        images = gltf.images or []
        scenes = gltf.scenes or []
        skins = gltf.skins or []
        scene_idx = gltf.scene or 0

    def _g(obj, key, default=None):
        return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)

    parent_by_node: dict[int, int] = {}
    for i, node in enumerate(nodes):
        for ch in (_g(node, "children") or ()):
            parent_by_node[int(ch)] = i
    world_cache: dict[int, np.ndarray] = {}

    def _world_of(node_idx: int) -> np.ndarray:
        cached = world_cache.get(node_idx)
        if cached is not None:
            return cached
        local = _node_local_matrix(nodes[node_idx])
        parent = parent_by_node.get(node_idx)
        world_mat = local if parent is None else _world_of(parent) @ local
        world_cache[node_idx] = world_mat
        return world_mat

    def _skin_matrices(skin_idx: int) -> np.ndarray | None:
        if skin_idx < 0 or skin_idx >= len(skins):
            return None
        skin = skins[skin_idx]
        joints = _g(skin, "joints") or []
        if not joints:
            return None
        ibm_acc = _g(skin, "inverseBindMatrices")
        if ibm_acc is not None:
            ibm = _read_accessor(gltf, blob, ibm_acc).astype(np.float32).reshape(-1, 4, 4).transpose(0, 2, 1)
        else:
            ibm = np.tile(np.eye(4, dtype=np.float32), (len(joints), 1, 1))
        if ibm.shape[0] < len(joints):
            pad = np.tile(np.eye(4, dtype=np.float32), (len(joints) - ibm.shape[0], 1, 1))
            ibm = np.concatenate([ibm, pad], axis=0)
        mats = np.empty((len(joints), 4, 4), dtype=np.float32)
        for k, j in enumerate(joints):
            mats[k] = _world_of(int(j)) @ ibm[k]
        return mats

    max_tex_dim = cfg.adaptive_viewer_max_tex_dim(len(images))
    textures = _g(gltf, "textures") or []
    tex_cache: dict[int, tuple[bytes, int, int, int, float] | None] = {}
    img_cache: dict[int, tuple[bytes, int, int, int, float] | None] = {}
    tex_to_src: dict[int, int] = {}
    rep_tex_by_src: list[tuple[int, int]] = []
    seen_src: set[int] = set()
    for mat in materials:
        pbr = _g(mat, "pbrMetallicRoughness")
        bct = _g(pbr, "baseColorTexture") if pbr is not None else None
        if bct is not None:
            ti = _g(bct, "index")
            if ti is not None:
                ti = int(ti)
                if ti not in tex_to_src and 0 <= ti < len(textures):
                    src = _g(textures[ti], "source")
                    if src is not None:
                        src = int(src)
                        tex_to_src[ti] = src
                        if src not in seen_src:
                            seen_src.add(src)
                            rep_tex_by_src.append((src, ti))
    if len(rep_tex_by_src) > 4:
        tex_lock = threading.Lock()

        def _predecode_src(pair: tuple[int, int]):
            src_i, rep_ti = pair
            local_c: dict[int, tuple[bytes, int, int, int, float] | None] = {}
            res = _decode_gltf_texture(gltf, blob, rep_ti, path.parent, local_c, max_dim=max_tex_dim)
            with tex_lock:
                img_cache[src_i] = res

        with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.adaptive_worker_count("io", max_workers=8)) as pool:
            list(pool.map(_predecode_src, rep_tex_by_src))
        for ti, src_i in tex_to_src.items():
            if src_i in img_cache:
                tex_cache[ti] = img_cache[src_i]
    disk_tex_cache: dict[str, tuple[bytes, int, int, int, float] | None] = {}
    disk_png_map: dict[str, Path] | None = None

    def _get_disk_png_map() -> dict[str, Path]:
        nonlocal disk_png_map
        if disk_png_map is not None:
            return disk_png_map
        ranked: dict[str, tuple[tuple[int, int], Path]] = {}
        if path.parent.is_dir():
            for p_png in sorted(path.parent.rglob("*.png")):
                rel_parts = p_png.relative_to(path.parent).parts
                try:
                    fsize = p_png.stat().st_size
                except OSError:
                    fsize = 0
                prio = (0 if "Texture2D" in rel_parts else (1 if "Animator" in rel_parts else 2), -fsize)
                st = p_png.stem.lower()
                if st not in ranked or prio < ranked[st][0]:
                    ranked[st] = (prio, p_png)
        disk_png_map = {k: v[1] for k, v in ranked.items()}
        return disk_png_map

    bones: list[GLBone] = []
    node_to_bone: dict[int, int] = {}
    skin_joint_nodes: set[int] = {
        int(j) for sk in skins for j in (_g(sk, "joints") or ())
    }

    def _visit_bone(node_idx: int, parent_world: np.ndarray, parent_bone: int) -> int:
        node = nodes[node_idx]
        n_mat = _g(node, "matrix")
        if n_mat:
            loc = np.array(n_mat, dtype=np.float32).reshape(4, 4).T
            t = loc[:3, 3].copy()
            s = np.linalg.norm(loc[:3, :3], axis=0).astype(np.float32)
            q = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
        else:
            t = np.array(_g(node, "translation") or [0.0, 0.0, 0.0], dtype=np.float32)
            q = np.array(_g(node, "rotation") or [0.0, 0.0, 0.0, 1.0], dtype=np.float32)
            s = np.array(_g(node, "scale") or [1.0, 1.0, 1.0], dtype=np.float32)
            loc = _trs_to_mat4(t, q, s)
        world = parent_world @ loc
        b_idx = -1
        if _g(node, "mesh") is None or node_idx in skin_joint_nodes:
            b_idx = len(bones)
            node_to_bone[node_idx] = b_idx
            bones.append(GLBone(
                name=_g(node, "name") or f"node_{node_idx}",
                parent=parent_bone,
                translation=t,
                rotation=q,
                scale=s,
                rest_world=world,
            ))
        for ch in (_g(node, "children") or ()):
            _visit_bone(ch, world, b_idx if b_idx >= 0 else parent_bone)
        return b_idx

    if scenes:
        sc = scenes[scene_idx]
        root_nodes = _g(sc, "nodes") or []
    else:
        root_nodes = list(range(len(nodes)))

    ident = np.eye(4, dtype=np.float32)
    for rn in root_nodes:
        _visit_bone(rn, ident, -1)

    scene_skins: list[tuple[list[int], np.ndarray]] = []
    for sk in skins:
        joints = _g(sk, "joints") or []
        if not joints:
            scene_skins.append(([], np.zeros((0, 4, 4), dtype=np.float32)))
            continue
        ibm_acc = _g(sk, "inverseBindMatrices")
        if ibm_acc is not None:
            ibm = _read_accessor(gltf, blob, ibm_acc).astype(np.float32).reshape(-1, 4, 4).transpose(0, 2, 1)
        else:
            ibm = np.tile(np.eye(4, dtype=np.float32), (len(joints), 1, 1))
        if ibm.shape[0] < len(joints):
            pad = np.tile(np.eye(4, dtype=np.float32), (len(joints) - ibm.shape[0], 1, 1))
            ibm = np.concatenate([ibm, pad], axis=0)
        mats = np.empty((len(joints), 4, 4), dtype=np.float32)
        jb: list[int] = []
        for k, j in enumerate(joints):
            ji = int(j)
            jb.append(node_to_bone.get(ji, -1))
            mats[k] = _world_of(ji) @ ibm[k]
        scene_skins.append((jb, mats))

    game_mats = _load_game_materials(path.parent)
    submeshes: list[GLSubMesh] = []
    bmin = np.array([np.inf, np.inf, np.inf], dtype=np.float32)
    bmax = np.array([-np.inf, -np.inf, -np.inf], dtype=np.float32)
    total_verts = 0
    total_tris = 0

    def _visit(node_idx: int, parent_mat: np.ndarray):
        nonlocal bmin, bmax, total_verts, total_tris
        node = nodes[node_idx]
        world = parent_mat @ _node_local_matrix(node)
        n_mesh = _g(node, "mesh")
        n_name = _g(node, "name") or ""
        if n_mesh is not None:
            mesh = meshes[n_mesh]
            m_name = _g(mesh, "name") or ""
            rot3 = world[:3, :3]
            try:
                norm_mat = np.linalg.inv(rot3).T.astype(np.float32)
            except np.linalg.LinAlgError:
                norm_mat = rot3
            n_rot = _g(node, "rotation")
            is_node_180y = bool(
                n_rot
                and len(n_rot) >= 4
                and abs(float(n_rot[1])) > 0.99
                and abs(float(n_rot[0])) < 0.05
                and abs(float(n_rot[2])) < 0.05
                and abs(float(n_rot[3])) < 0.05
            )
            for prim in (_g(mesh, "primitives") or ()):
                attrs = _g(prim, "attributes")
                pos_acc = _g(attrs, "POSITION")
                if pos_acc is None:
                    continue
                pos = _read_accessor(gltf, blob, pos_acc).astype(np.float32)
                nrm_acc = _g(attrs, "NORMAL")
                nrm = _read_accessor(gltf, blob, nrm_acc).astype(np.float32) if nrm_acc is not None else None
                skin_mats = None
                skin_idx = _g(node, "skin")
                jnt_acc = _g(attrs, "JOINTS_0")
                wgt_acc = _g(attrs, "WEIGHTS_0")
                sub_skin_idx = -1
                bind_pos = None
                bind_nrm = None
                sub_joints = None
                sub_weights = None
                if skin_idx is not None and jnt_acc is not None and wgt_acc is not None:
                    sidx = int(skin_idx)
                    if 0 <= sidx < len(scene_skins) and len(scene_skins[sidx][1]) > 0:
                        skin_mats = scene_skins[sidx][1]
                        sub_skin_idx = sidx
                if skin_mats is not None:
                    J = _read_accessor(gltf, blob, jnt_acc).astype(np.int64)
                    W = _read_accessor(gltf, blob, wgt_acc).astype(np.float32)
                    if J.ndim == 1:
                        J = J.reshape(-1, 1)
                    if W.ndim == 1:
                        W = W.reshape(-1, 1)
                    valid = (J >= 0) & (J < len(skin_mats))
                    J = np.where(valid, J, 0)
                    W = np.where(valid, W, 0.0)
                    if J.shape[1] < 4:
                        pad_c = 4 - J.shape[1]
                        J = np.pad(J, ((0, 0), (0, pad_c)))
                        W = np.pad(W, ((0, 0), (0, pad_c)))
                    elif J.shape[1] > 4:
                        J = J[:, :4]
                        W = W[:, :4]
                    wsum = W.sum(axis=1, keepdims=True)
                    bad = wsum[:, 0] < 1e-8
                    if np.any(bad):
                        W[bad, 0] = 1.0
                        wsum[bad, 0] = 1.0
                    W = W / np.maximum(wsum, 1e-8)
                    pos_w = np.zeros_like(pos, dtype=np.float32)
                    nrm_w = np.zeros_like(nrm, dtype=np.float32) if nrm is not None else None
                    for c in range(4):
                        M = skin_mats[J[:, c]]
                        pos_w += W[:, c, None] * (np.einsum("vij,vj->vi", M[:, :3, :3], pos) + M[:, :3, 3])
                        if nrm_w is not None:
                            nrm_w += W[:, c, None] * np.einsum("vij,vj->vi", M[:, :3, :3], nrm)
                    if nrm_w is not None:
                        lens = np.linalg.norm(nrm_w, axis=1, keepdims=True)
                        nrm_w = np.divide(nrm_w, np.maximum(lens, 1e-6))
                    else:
                        nrm_w = np.tile(np.array([0.0, 1.0, 0.0], dtype=np.float32), (len(pos_w), 1))
                    bind_pos = np.ascontiguousarray(pos, dtype=np.float32)
                    bind_nrm = (
                        np.ascontiguousarray(nrm, dtype=np.float32)
                        if nrm is not None
                        else np.tile(np.array([0.0, 1.0, 0.0], dtype=np.float32), (len(pos), 1))
                    )
                    sub_joints = np.ascontiguousarray(J, dtype=np.int32)
                    sub_weights = np.ascontiguousarray(W, dtype=np.float32)
                else:
                    pos_w = (pos @ rot3.T) + world[:3, 3]
                    if nrm is not None:
                        nrm_w = nrm @ norm_mat.T
                        lens = np.linalg.norm(nrm_w, axis=1, keepdims=True)
                        nrm_w = np.divide(nrm_w, np.maximum(lens, 1e-6))
                    else:
                        nrm_w = np.tile(np.array([0.0, 1.0, 0.0], dtype=np.float32), (len(pos_w), 1))

                uvs = None
                uv0_acc = _g(attrs, "TEXCOORD_0")
                uv1_acc = _g(attrs, "TEXCOORD_1")
                if uv0_acc is not None:
                    uvs = _read_accessor(gltf, blob, uv0_acc).astype(np.float32)
                elif uv1_acc is not None:
                    uvs = _read_accessor(gltf, blob, uv1_acc).astype(np.float32)

                ind_acc = _g(prim, "indices")
                if ind_acc is not None:
                    idx = _read_accessor(gltf, blob, ind_acc).astype(np.uint32).reshape(-1)
                else:
                    idx = np.arange(len(pos_w), dtype=np.uint32)

                color = (0.78, 0.80, 0.85, 1.0)
                has_bcf = False
                tex_data = None
                alpha_mode = "OPAQUE"
                alpha_cutoff = 0.5
                double_sided = False
                mat_name = ""
                mat_idx = _g(prim, "material")
                if mat_idx is not None and mat_idx < len(materials):
                    mat = materials[mat_idx]
                    mat_name = _g(mat, "name") or ""
                    alpha_mode = (_g(mat, "alphaMode") or "OPAQUE").upper()
                    ac = _g(mat, "alphaCutoff")
                    if ac is not None:
                        alpha_cutoff = float(ac)
                    double_sided = bool(_g(mat, "doubleSided"))
                    pbr = _g(mat, "pbrMetallicRoughness")
                    if pbr is not None:
                        bcf = _g(pbr, "baseColorFactor")
                        if bcf:
                            color = (float(bcf[0]), float(bcf[1]), float(bcf[2]), float(bcf[3]))
                            has_bcf = True
                        bct = _g(pbr, "baseColorTexture")
                        if bct is not None:
                            bct_idx = _g(bct, "index")
                            if bct_idx is not None:
                                tex_data = _decode_gltf_texture(
                                    gltf, blob, bct_idx, path.parent, tex_cache, max_dim=max_tex_dim, img_cache=img_cache
                                )

                hint = f"{n_name} {m_name} {mat_name}".lower()
                if "hairshadow" in hint or "shadowproxy" in hint:
                    continue
                is_weapon = "weapon" in hint
                is_sky = bool(re.search(r"(?:^|[_\s.])(skybox|skydome|sky_?dome|sky_?sphere|sky_?plane|sky|cloud|dome|backdrop)(?:$|[_\s.0-9])", hint))
                is_foliage = any(t in hint for t in ("leaf", "tree", "plant", "shrub", "bush", "grass", "foliage"))
                is_hair = any(t in hint for t in ("hair", "lash"))
                is_face = bool(re.search(r"(?:^|[_\s.])face(?:$|[_\s.0-9])", hint))

                gm = _match_game_material(game_mats, mat_name, m_name, n_name) if game_mats else None
                is_decal = ("decal" in hint) or bool(gm and gm.get("is_decal"))

                # Disk texture fallback for untextured legacy meshes (e.g. SilverAnby_Face -> Anbi_Face_D.png)
                if tex_data is None and uvs is not None and gm is not None and gm.get("main_tex"):
                    png_p = _get_disk_png_map().get(gm["main_tex"])
                    if png_p is not None:
                        tex_data = _load_disk_texture(png_p, disk_tex_cache, max_dim=max_tex_dim)

                # Repair mismatched legacy OBJ texture (e.g. Lighter_Body_2_1 bound to Map1 instead of Map2)
                if (
                    sub_skin_idx < 0
                    and is_node_180y
                    and tex_data is not None
                    and mat_name
                    and not mat_name.lower().startswith("mat_")
                    and mat_name.lower().endswith("_d")
                    and game_mats
                ):
                    gm_node = _match_game_material(game_mats, n_name, m_name)
                    if gm_node is not None and gm_node.get("main_tex") and gm_node["main_tex"] != mat_name.lower():
                        png_p = _get_disk_png_map().get(gm_node["main_tex"])
                        if png_p is not None:
                            rep_tex = _load_disk_texture(png_p, disk_tex_cache, max_dim=max_tex_dim)
                            if rep_tex is not None:
                                tex_data = rep_tex
                                gm = gm_node

                # glTF 2.0 spec default baseColorFactor is (1,1,1,1) when textured; strip legacy Blender 0.8 diffuse tint
                if tex_data is not None:
                    if not has_bcf or (
                        is_node_180y
                        and sub_skin_idx < 0
                        and all(abs(color[c] - 0.8) < 0.01 for c in range(3))
                    ):
                        color = (1.0, 1.0, 1.0, 1.0)

                if is_sky:
                    double_sided = True
                    if alpha_mode == "OPAQUE" and tex_data is not None:
                        alpha_mode = "BLEND"
                elif is_decal and tex_data is not None and not (gm and gm["alpha_clip"]):
                    if tex_data[3] < 250:
                        alpha_mode = "BLEND"
                    elif alpha_mode == "BLEND":
                        alpha_mode = "OPAQUE"
                elif is_foliage and alpha_mode in ("OPAQUE", "BLEND") and tex_data is not None:
                    if tex_data[3] < 32 and (gm is None or gm["alpha_clip"]):
                        alpha_mode = "MASK"
                    elif alpha_mode == "BLEND" and tex_data[3] >= 250:
                        alpha_mode = "OPAQUE"
                elif gm is None and alpha_mode in ("OPAQUE", "BLEND") and tex_data is not None and is_hair:
                    if tex_data[3] < 64 and tex_data[4] < 245:
                        alpha_mode = "MASK"
                    elif alpha_mode == "BLEND":
                        alpha_mode = "OPAQUE"

                toon_smooth = 0.05
                toon_spec = 0.18
                rim_glow = 0.35
                rim_col = (1.0, 1.0, 1.0)
                shallow_tint = (0.94, 0.91, 0.93)
                shadow_tint = (0.84, 0.80, 0.86)
                spec_color = (1.0, 1.0, 1.0)
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
                    shallow_tint = gm["shallow_tint"]
                    shadow_tint = gm["shadow_tint"]
                    spec_color = gm["spec_color"]

                keep_blend = bool(
                    gm and ("transparent" in (gm.get("shader") or "").lower() or gm.get("is_decal") or gm.get("is_water"))
                )
                if (
                    alpha_mode == "BLEND"
                    and tex_data is not None
                    and not is_sky
                    and not keep_blend
                    and not any(t in hint for t in ("glass", "window", "water", "eff", "fx", "decal", "trans"))
                ):
                    if tex_data[4] < 5.0:
                        alpha_mode = "OPAQUE"
                elif alpha_mode == "MASK" and tex_data is not None:
                    if (alpha_cutoff >= 0.90 or tex_data[4] < 16.0) and tex_data[3] >= 0:
                        a_arr = np.frombuffer(tex_data[0], dtype=np.uint8)[3::4]
                        if float((a_arr >= int(min(1.0, alpha_cutoff) * 255)).mean()) < 0.03:
                            alpha_mode = "BLEND" if (gm and gm.get("blend")) else "OPAQUE"

                if len(pos_w) > 0:
                    pmin = pos_w.min(axis=0)
                    pmax = pos_w.max(axis=0)
                    bmin = np.minimum(bmin, pmin)
                    bmax = np.maximum(bmax, pmax)
                    sm_center = tuple(((pmin + pmax) * 0.5).tolist())
                    sm_radius = max(0.001, float(np.linalg.norm((pmax - pmin) * 0.5)))
                else:
                    sm_center = (0.0, 0.0, 0.0)
                    sm_radius = 0.001
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
                        bound_radius=sm_radius,
                        toon_smoothness=toon_smooth,
                        toon_specular=toon_spec,
                        rim_glow=rim_glow,
                        rim_color=rim_col,
                        shallow_tint=shallow_tint,
                        shadow_tint=shadow_tint,
                        spec_color=spec_color,
                        is_face=is_face,
                        node_name=n_name,
                        mat_name=mat_name,
                        bind_positions=bind_pos,
                        bind_normals=bind_nrm,
                        skin_joints=sub_joints,
                        skin_weights=sub_weights,
                        skin_idx=sub_skin_idx,
                        is_180y_obj=is_node_180y and sub_skin_idx < 0,
                    )
                )
        for ch in (_g(node, "children") or ()):
            _visit(ch, world)

    for rn in root_nodes:
        _visit(rn, ident)

    # Deduplicate exact co-planar duplicate static map submeshes (e.g. ExtraScene vs Streaming overlaps)
    if len(submeshes) > 200 and not any("bip" in b.name.lower() for b in bones[:50]):
        seen_geo: set[tuple] = set()
        dedup_sms: list[GLSubMesh] = []
        for sm in submeshes:
            if len(sm.positions) == 0 or len(sm.indices) == 0:
                continue
            pmin_s = sm.positions.min(axis=0)
            pmax_s = sm.positions.max(axis=0)
            base_mat = re.sub(r"\.\d+$", "", (sm.mat_name or "").lower())
            gkey = (
                base_mat,
                len(sm.positions),
                len(sm.indices),
                round(float(pmin_s[0]), 4), round(float(pmin_s[1]), 4), round(float(pmin_s[2]), 4),
                round(float(pmax_s[0]), 4), round(float(pmax_s[1]), 4), round(float(pmax_s[2]), 4),
            )
            if gkey in seen_geo:
                continue
            seen_geo.add(gkey)
            dedup_sms.append(sm)
        if len(dedup_sms) < len(submeshes):
            submeshes = dedup_sms
            total_verts = sum(len(sm.positions) for sm in submeshes)
            total_tris = sum(len(sm.indices) // 3 for sm in submeshes)

    # Align legacy 180°-Y OBJ character meshes to match forward-facing (+Z) skeleton
    if (
        bones
        and submeshes
        and all(sm.skin_idx < 0 for sm in submeshes)
        and any(sm.is_180y_obj for sm in submeshes)
    ):
        i_foot = next(
            (i for i, b in enumerate(bones) if re.search(r"bip\d+\s+l\s+foot$", b.name, re.I) and not re.search(r"\.\d+$", b.name)),
            None,
        )
        i_toe = next(
            (i for i, b in enumerate(bones) if re.search(r"bip\d+\s+l\s+toe0$", b.name, re.I) and not re.search(r"\.\d+$", b.name)),
            None,
        )
        skel_fwd_dir_z = 0.0
        if i_foot is not None and i_toe is not None:
            dv = bones[i_toe].rest_world[:3, 3] - bones[i_foot].rest_world[:3, 3]
            dlen = float(np.linalg.norm(dv))
            if dlen > 1e-6:
                skel_fwd_dir_z = float(dv[2] / dlen)
        face_sms = [sm for sm in submeshes if sm.is_180y_obj and "face" in sm.node_name.lower() and len(sm.positions)]
        face_z = float(np.mean([sm.center[2] for sm in face_sms])) if face_sms else 0.0
        if skel_fwd_dir_z > 0.05 and face_z < -0.005:
            bmin = np.array([np.inf, np.inf, np.inf], dtype=np.float32)
            bmax = np.array([-np.inf, -np.inf, -np.inf], dtype=np.float32)
            for sm in submeshes:
                if sm.is_180y_obj and len(sm.positions):
                    sm.positions[:, 0] *= -1.0
                    sm.positions[:, 2] *= -1.0
                    sm.positions = np.ascontiguousarray(sm.positions, dtype=np.float32)
                    sm.normals[:, 0] *= -1.0
                    sm.normals[:, 2] *= -1.0
                    sm.normals = np.ascontiguousarray(sm.normals, dtype=np.float32)
                    pmin = sm.positions.min(axis=0)
                    pmax = sm.positions.max(axis=0)
                    sm.center = tuple(((pmin + pmax) * 0.5).tolist())
                    sm.bound_radius = max(0.001, float(np.linalg.norm((pmax - pmin) * 0.5)))
                if len(sm.positions):
                    bmin = np.minimum(bmin, sm.positions.min(axis=0))
                    bmax = np.maximum(bmax, sm.positions.max(axis=0))

    if not submeshes or not np.all(np.isfinite(bmin)):
        center = np.zeros(3, dtype=np.float32)
        radius = 1.0
        bmin_y = 0.0
    else:
        center = ((bmin + bmax) * 0.5).astype(np.float32)
        radius = max(0.05, float(0.5 * np.linalg.norm(bmax - bmin)))
        bmin_y = float(bmin[1])

    tex_cache.clear()
    img_cache.clear()
    world_cache.clear()
    disk_tex_cache.clear()
    blob = b""
    gltf = None
    _release_os_memory()

    return GLScene(
        source_path=path,
        submeshes=submeshes,
        center=center,
        radius=radius,
        bmin_y=bmin_y,
        vertex_count=total_verts,
        triangle_count=total_tris,
        bones=bones,
        skins=scene_skins or None,
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
    sub = GLSubMesh(pos, nrm, None, idx, (0.78, 0.80, 0.85, 1.0), center=tuple(center.tolist()), bound_radius=radius) if len(idx) else None
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


_DEFAULT_TOON_PRESETS: dict[str, dict] = {
    "Outdoors (Day)": {
        "source": "WeatherConfig_Roscaelifer_NoonSunny (546199337.blk)",
        "light_dir": [0.46, 0.72, 0.52],
        "key_color": [1.0, 0.968, 0.905],
        "key_intensity": 1.13,
        "shallow_color": [1.0, 0.91, 0.905],
        "shadow_color": [0.815, 0.755, 0.835],
        "term_tint": [1.04, 0.9, 0.87],
        "sky_color": [0.825, 0.907, 0.986],
        "ground_color": [0.959, 0.972, 0.974],
        "rim_color": [0.96, 0.98, 1.0],
        "rim_strength": 0.38,
        "cel_threshold": 0.46,
        "cel_smooth_scale": 1.0,
        "ramp_file": "Ramp_Day.png",
        "ramp_mix": 0.18,
        "lut_file": "Photo_LUT_ConstructionSite.png",
        "lut_strength": 0.35,
        "backdrop": [0.13, 0.19, 0.27],
        "sky_tint": [1.0, 1.0, 1.0],
        "sky_zenith": [0.2, 0.5, 0.9],
        "sky_mid": [0.46, 0.74, 0.96],
        "sky_horizon": [0.84, 0.92, 0.99],
        "sky_ground": [0.58, 0.68, 0.78],
        "sun_color": [1.0, 0.96, 0.85],
        "sun_size": 1.0,
        "star_strength": 0.0,
        "cloud_lit": [1.0, 0.99, 0.97],
        "cloud_shadow": [0.7, 0.8, 0.92],
        "cloud_density": 0.72,
    },
    "Sunrise (Dawn)": {
        "source": "WeatherConfig_Roscaelifer_MorningSunny (546199337.blk) + WeatherConfig_Whitfield_Dawn (2433225145.blk)",
        "light_dir": [0.72, 0.34, 0.6],
        "key_color": [1.0, 0.895, 0.74],
        "key_intensity": 1.15,
        "shallow_color": [1.0, 0.865, 0.79],
        "shadow_color": [0.815, 0.705, 0.755],
        "term_tint": [1.08, 0.86, 0.76],
        "sky_color": [0.72, 0.84, 0.95],
        "ground_color": [0.982, 0.843, 0.703],
        "rim_color": [1.0, 0.83, 0.58],
        "rim_strength": 0.48,
        "cel_threshold": 0.47,
        "cel_smooth_scale": 1.05,
        "ramp_file": "Ramp_Warm.png",
        "ramp_mix": 0.22,
        "lut_file": "Photo_LUT_OuterRing.png",
        "lut_strength": 0.38,
        "backdrop": [0.19, 0.15, 0.2],
        "sky_tint": [1.06, 0.86, 0.74],
        "sky_zenith": [0.24, 0.36, 0.68],
        "sky_mid": [0.88, 0.58, 0.56],
        "sky_horizon": [1.0, 0.8, 0.54],
        "sky_ground": [0.56, 0.46, 0.45],
        "sun_color": [1.0, 0.86, 0.52],
        "sun_size": 1.25,
        "star_strength": 0.0,
        "cloud_lit": [1.0, 0.88, 0.72],
        "cloud_shadow": [0.64, 0.52, 0.64],
        "cloud_density": 0.68,
    },
    "Sunset (Dusk)": {
        "source": "WeatherConfig_Roscaelifer_DuskSunny (546199337.blk)",
        "light_dir": [-0.74, 0.28, 0.61],
        "key_color": [1.0, 0.79, 0.58],
        "key_intensity": 1.15,
        "shallow_color": [1.0, 0.84, 0.745],
        "shadow_color": [0.77, 0.705, 0.795],
        "term_tint": [1.12, 0.78, 0.64],
        "sky_color": [0.953, 0.85, 0.76],
        "ground_color": [0.972, 0.843, 0.731],
        "rim_color": [1.0, 0.64, 0.32],
        "rim_strength": 0.52,
        "cel_threshold": 0.48,
        "cel_smooth_scale": 1.0,
        "ramp_file": "Ramp_Warm.png",
        "ramp_mix": 0.28,
        "lut_file": "Photo_LUT_OuterRing.png",
        "lut_strength": 0.45,
        "backdrop": [0.2, 0.125, 0.145],
        "sky_tint": [1.06, 0.7, 0.52],
        "sky_zenith": [0.18, 0.22, 0.48],
        "sky_mid": [0.88, 0.46, 0.36],
        "sky_horizon": [1.0, 0.66, 0.3],
        "sky_ground": [0.44, 0.3, 0.34],
        "sun_color": [1.0, 0.72, 0.34],
        "sun_size": 1.3,
        "star_strength": 0.0,
        "cloud_lit": [1.0, 0.76, 0.52],
        "cloud_shadow": [0.5, 0.38, 0.52],
        "cloud_density": 0.75,
    },
    "In-Building (Indoor)": {
        "source": "WeatherConfig_WhitfieldTeahouse_NoonSunny (2433225145.blk) + Local_WeatherConfig_DormitoryHanger_Day (546199337.blk)",
        "light_dir": [0.24, 0.84, 0.49],
        "key_color": [1.0, 0.955, 0.895],
        "key_intensity": 1.1,
        "shallow_color": [0.975, 0.935, 0.905],
        "shadow_color": [0.835, 0.805, 0.83],
        "term_tint": [1.02, 0.95, 0.9],
        "sky_color": [0.98, 0.95, 0.89],
        "ground_color": [0.87, 0.855, 0.87],
        "rim_color": [0.98, 0.95, 0.88],
        "rim_strength": 0.26,
        "cel_threshold": 0.41,
        "cel_smooth_scale": 1.35,
        "ramp_file": "Ramp_Indoor.png",
        "ramp_mix": 0.18,
        "lut_file": "Photo_LUT_Metro.png",
        "lut_strength": 0.3,
        "backdrop": [0.13, 0.13, 0.15],
        "sky_tint": [0.96, 0.94, 0.9],
        "sky_zenith": [0.42, 0.52, 0.66],
        "sky_mid": [0.6, 0.68, 0.78],
        "sky_horizon": [0.78, 0.82, 0.86],
        "sky_ground": [0.32, 0.33, 0.36],
        "sun_color": [1.0, 0.95, 0.88],
        "sun_size": 0.4,
        "star_strength": 0.0,
        "cloud_lit": [0.94, 0.94, 0.95],
        "cloud_shadow": [0.72, 0.75, 0.8],
        "cloud_density": 0.35,
    },
    "Night (Neon City)": {
        "source": "WeatherConfig_Roscaelifer_MidnightSunny (546199337.blk)",
        "light_dir": [-0.38, 0.62, 0.69],
        "key_color": [0.96, 0.95, 0.985],
        "key_intensity": 1.08,
        "shallow_color": [0.91, 0.92, 0.99],
        "shadow_color": [0.665, 0.725, 0.855],
        "term_tint": [0.86, 0.9, 1.06],
        "sky_color": [0.806, 0.904, 0.986],
        "ground_color": [0.66, 0.74, 0.84],
        "rim_color": [0.524, 0.876, 1.0],
        "rim_strength": 0.56,
        "cel_threshold": 0.49,
        "cel_smooth_scale": 0.95,
        "ramp_file": "Ramp_Cool.png",
        "ramp_mix": 0.2,
        "lut_file": "Photo_LUT_Concert.png",
        "lut_strength": 0.42,
        "backdrop": [0.06, 0.085, 0.145],
        "sky_tint": [0.42, 0.56, 0.82],
        "sky_zenith": [0.02, 0.04, 0.11],
        "sky_mid": [0.06, 0.11, 0.24],
        "sky_horizon": [0.14, 0.24, 0.38],
        "sky_ground": [0.05, 0.07, 0.12],
        "sun_color": [0.86, 0.92, 1.0],
        "sun_size": 0.65,
        "star_strength": 0.95,
        "cloud_lit": [0.28, 0.36, 0.52],
        "cloud_shadow": [0.1, 0.14, 0.24],
        "cloud_density": 0.45,
    },
    "Overcast / Rainy": {
        "source": "WeatherConfig_Roscaelifer_NoonRainy (546199337.blk)",
        "light_dir": [0.22, 0.85, 0.48],
        "key_color": [0.94, 0.975, 1.0],
        "key_intensity": 1.09,
        "shallow_color": [0.93, 0.955, 0.985],
        "shadow_color": [0.78, 0.815, 0.88],
        "term_tint": [0.94, 0.97, 1.04],
        "sky_color": [0.671, 0.824, 0.985],
        "ground_color": [0.858, 0.86, 0.853],
        "rim_color": [0.82, 0.92, 1.0],
        "rim_strength": 0.32,
        "cel_threshold": 0.42,
        "cel_smooth_scale": 1.45,
        "ramp_file": "Ramp_Cool.png",
        "ramp_mix": 0.16,
        "lut_file": "Photo_LUT_port.png",
        "lut_strength": 0.32,
        "backdrop": [0.11, 0.15, 0.19],
        "sky_tint": [0.82, 0.88, 0.95],
        "sky_zenith": [0.34, 0.42, 0.5],
        "sky_mid": [0.56, 0.63, 0.69],
        "sky_horizon": [0.76, 0.8, 0.83],
        "sky_ground": [0.36, 0.4, 0.44],
        "sun_color": [0.9, 0.94, 0.98],
        "sun_size": 0.0,
        "star_strength": 0.0,
        "cloud_lit": [0.78, 0.82, 0.86],
        "cloud_shadow": [0.5, 0.56, 0.64],
        "cloud_density": 0.92,
    },
    "Studio (Agent UI)": {
        "source": "WeatherConfig_MainPage_Obsidian_SilverAnby_NoonSunny (1105171802.blk)",
        "light_dir": [0.35, 0.55, 0.76],
        "key_color": [1.0, 0.985, 0.96],
        "key_intensity": 1.14,
        "shallow_color": [0.97, 0.95, 0.955],
        "shadow_color": [0.845, 0.82, 0.87],
        "term_tint": [1.03, 0.93, 0.92],
        "sky_color": [0.94, 0.96, 1.0],
        "ground_color": [0.92, 0.91, 0.94],
        "rim_color": [1.0, 0.99, 0.97],
        "rim_strength": 0.36,
        "cel_threshold": 0.44,
        "cel_smooth_scale": 0.9,
        "ramp_file": "Ramp_Studio.png",
        "ramp_mix": 0.12,
        "lut_file": "",
        "lut_strength": 0.0,
        "backdrop": [0.11, 0.12, 0.145],
        "sky_tint": [1.0, 1.0, 1.0],
        "sky_zenith": [0.1, 0.115, 0.155],
        "sky_mid": [0.14, 0.16, 0.21],
        "sky_horizon": [0.22, 0.25, 0.32],
        "sky_ground": [0.08, 0.09, 0.12],
        "sun_color": [1.0, 0.98, 0.95],
        "sun_size": 0.0,
        "star_strength": 0.0,
        "cloud_lit": [0.42, 0.46, 0.55],
        "cloud_shadow": [0.2, 0.23, 0.3],
        "cloud_density": 0.15,
    },
}


def _load_toon_presets() -> dict[str, dict]:
    """Load toon presets from assets/toon_presets/presets.json merged over built-in defaults."""
    base_default = _DEFAULT_TOON_PRESETS["Outdoors (Day)"]
    presets: dict[str, dict] = {k: dict(v) for k, v in _DEFAULT_TOON_PRESETS.items()}
    p = getattr(cfg, "TOON_PRESETS_DIR", cfg.ASSETS_DIR / "toon_presets") / "presets.json"
    try:
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(raw, dict):
                merged: dict[str, dict] = {}
                for name, val in raw.items():
                    if isinstance(val, dict):
                        tmpl = _DEFAULT_TOON_PRESETS.get(name, base_default)
                        merged[name] = {**tmpl, **val}
                for name, val in _DEFAULT_TOON_PRESETS.items():
                    if name not in merged:
                        merged[name] = dict(val)
                if merged:
                    presets = merged
    except Exception:
        pass
    return presets


_SKYBOX_VERT = """
#version 120
varying vec3 v_dir;
void main() {
    mat4 rot_mv = gl_ModelViewMatrix;
    rot_mv[3] = vec4(0.0, 0.0, 0.0, 1.0);
    vec4 clip = gl_ProjectionMatrix * rot_mv * gl_Vertex;
    gl_Position = clip.xyww;
    v_dir = gl_Vertex.xyz;
}
"""

_SKYBOX_FRAG = """
#version 120
uniform vec3 u_light_dir;
uniform vec3 u_sky_zenith;
uniform vec3 u_sky_mid;
uniform vec3 u_sky_horizon;
uniform vec3 u_sky_ground;
uniform vec3 u_sun_color;
uniform float u_sun_size;
uniform float u_star_strength;
uniform vec3 u_cloud_lit;
uniform vec3 u_cloud_shadow;
uniform float u_cloud_density;
uniform sampler2D u_high_cloud_tex;
uniform sampler2D u_low_cloud_tex;
uniform int u_use_high_cloud;
uniform int u_use_low_cloud;

varying vec3 v_dir;

void main() {
    vec3 d = normalize(v_dir);
    float y = d.y;
    vec3 col;
    if (y >= 0.0) {
        float t1 = smoothstep(0.0, 0.24, y);
        float t2 = smoothstep(0.18, 0.78, y);
        col = mix(u_sky_horizon, u_sky_mid, t1);
        col = mix(col, u_sky_zenith, t2);
    } else {
        float tg = smoothstep(0.0, 0.28, -y);
        col = mix(u_sky_horizon, u_sky_ground, tg);
    }

    vec3 L = normalize(u_light_dir);
    float sun_dot = max(dot(d, L), 0.0);

    if (u_star_strength > 0.01 && y > 0.01) {
        vec3 p = d * 185.0;
        vec3 cell = floor(p);
        vec3 f = fract(p) - 0.5;
        float h = fract(sin(dot(cell, vec3(127.1, 311.7, 74.7))) * 43758.5453);
        float h2 = fract(sin(dot(cell, vec3(269.5, 183.3, 246.1))) * 28461.1234);
        if (h > 0.965) {
            float dist = length(f - vec3(h2 - 0.5, fract(h * 17.3) - 0.5, fract(h2 * 13.7) - 0.5) * 0.4);
            float star = smoothstep(0.32, 0.02, dist) * smoothstep(0.965, 1.0, h);
            col += vec3(0.85, 0.92, 1.0) * star * u_star_strength * smoothstep(0.02, 0.25, y) * 1.4;
        }
    }

    if (u_sun_size > 0.001) {
        float glow = pow(sun_dot, 8.0) * 0.28 + pow(sun_dot, 48.0) * 0.45;
        float disc = smoothstep(1.0 - 0.0018 * u_sun_size, 1.0 - 0.0005 * u_sun_size, sun_dot);
        float horiz_fade = smoothstep(-0.08, 0.08, y);
        col += u_sun_color * (glow * 0.55 + disc * 1.35) * horiz_fade;
    }

    if (u_use_high_cloud == 1 && u_cloud_density > 0.01 && y > 0.01) {
        vec2 huv = d.xz / (y + 0.24) * 0.42 + vec2(0.15, 0.25);
        vec3 hc = texture2D(u_high_cloud_tex, huv).rgb;
        vec3 hc2 = texture2D(u_high_cloud_tex, huv * 1.85 + vec2(0.37, 0.61)).gbr;
        float mask = 0.52 * hc.b + 0.30 * hc.g + 0.18 * hc.r;
        float detail = 0.50 * hc2.b + 0.32 * hc2.g + 0.18 * hc2.r;
        float c_cov = clamp(mask * 0.72 + detail * 0.45, 0.0, 1.0);
        float thresh = mix(0.68, 0.18, u_cloud_density);
        float c_alpha = smoothstep(thresh, thresh + 0.32, c_cov) * smoothstep(0.01, 0.18, y) * 0.78;
        float c_light = clamp(detail * 0.65 + pow(sun_dot, 6.0) * 0.45 + 0.22, 0.0, 1.0);
        vec3 c_col = mix(u_cloud_shadow, u_cloud_lit, c_light);
        col = mix(col, c_col, c_alpha);
    }

    if (u_use_low_cloud == 1 && u_cloud_density > 0.01 && y > -0.04 && y < 0.36) {
        float az = atan(d.x, d.z) * 0.15915494 + 0.5;
        float elev = clamp(1.0 - (y / 0.36), 0.02, 0.98);
        vec4 lc = texture2D(u_low_cloud_tex, vec2(az, elev));
        float a_low = lc.a * smoothstep(-0.04, 0.02, y) * smoothstep(0.36, 0.22, y) * clamp(u_cloud_density * 1.15, 0.0, 1.0);
        float silver = pow(sun_dot, 5.0) * 0.35;
        vec3 c_col = mix(u_cloud_shadow, u_cloud_lit, clamp(lc.r * 1.15 + silver, 0.0, 1.0));
        col = mix(col, c_col, a_low * 0.9);
    }

    gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
"""

_TOON_VERT = """
#version 120
uniform sampler2D u_bone_tex;
uniform float u_bone_tex_h;
uniform int u_skin_enabled;
varying vec3 v_world_pos;
varying vec3 v_normal;
varying vec2 v_uv;
mat4 fetch_bone(float idx) {
    float v = (idx + 0.5) / u_bone_tex_h;
    return mat4(
        texture2D(u_bone_tex, vec2(0.125, v)),
        texture2D(u_bone_tex, vec2(0.375, v)),
        texture2D(u_bone_tex, vec2(0.625, v)),
        texture2D(u_bone_tex, vec2(0.875, v))
    );
}
void main() {
    vec4 pos = gl_Vertex;
    vec3 nrm = gl_Normal;
    if (u_skin_enabled == 1) {
        vec4 j = gl_MultiTexCoord1;
        vec4 w = gl_MultiTexCoord2;
        mat4 m = fetch_bone(j.x) * w.x + fetch_bone(j.y) * w.y + fetch_bone(j.z) * w.z + fetch_bone(j.w) * w.w;
        pos = m * pos;
        nrm = mat3(m) * nrm;
    }
    v_world_pos = pos.xyz;
    v_normal = normalize(nrm);
    v_uv = gl_MultiTexCoord0.xy;
    gl_Position = gl_ModelViewProjectionMatrix * pos;
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
uniform int u_is_face;
uniform int u_toon_enabled;
uniform vec3 u_cam_pos;
uniform vec3 u_light_dir;
uniform float u_toon_smooth;
uniform float u_toon_spec;
uniform float u_rim_glow;
uniform vec3 u_rim_color;
uniform vec3 u_shallow_tint;
uniform vec3 u_shadow_tint;
uniform vec3 u_spec_color;

uniform vec3 u_key_color;
uniform float u_key_intensity;
uniform vec3 u_preset_shallow;
uniform vec3 u_preset_shadow;
uniform vec3 u_term_tint;
uniform vec3 u_sky_color;
uniform vec3 u_ground_color;
uniform vec3 u_preset_rim_color;
uniform float u_preset_rim_strength;
uniform float u_cel_threshold;
uniform float u_cel_smooth_scale;
uniform vec3 u_sky_tint;
uniform sampler2D u_ramp_tex;
uniform int u_use_ramp;
uniform float u_ramp_mix;
uniform sampler2D u_lut_tex;
uniform int u_use_lut;
uniform float u_lut_strength;

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
        vec3 sky_rgb = (u_toon_enabled == 1) ? (base.rgb * u_sky_tint) : base.rgb;
        gl_FragColor = vec4(clamp(sky_rgb, 0.0, 1.0), u_alpha_mode == 2 ? base.a : 1.0);
        return;
    }
    vec3 N = normalize(v_normal);
    if (!gl_FrontFacing) N = -N;
    vec3 L = normalize(u_light_dir);
    vec3 V = normalize(u_cam_pos - v_world_pos);
    vec3 lit;
    if (u_toon_enabled == 1) {
        float ndl;
        if (u_is_face == 1) {
            vec3 Nf = normalize(vec3(N.x, N.y * 0.40, N.z));
            ndl = dot(Nf, L) * 0.5 + 0.58;
        } else {
            ndl = dot(N, L) * 0.5 + 0.5;
        }
        float sm = clamp(u_toon_smooth * u_cel_smooth_scale, 0.02, 0.14);
        float band_lit = smoothstep(u_cel_threshold - sm, u_cel_threshold + sm, ndl);
        float band_mid = smoothstep(u_cel_threshold - 0.16 - sm, u_cel_threshold - 0.16 + sm, ndl);
        float term = band_mid * (1.0 - band_lit);

        vec3 shd = mix(u_preset_shadow, u_preset_shadow * u_shadow_tint, 0.35);
        shd = max(shd, u_preset_shadow * 0.85);
        vec3 shl = mix(u_preset_shallow, u_preset_shallow * u_shallow_tint, 0.35);
        vec3 key_lit = u_key_color * u_key_intensity;
        vec3 cel_col = mix(shd, shl, band_mid);
        cel_col = mix(cel_col, key_lit, band_lit);
        cel_col = mix(cel_col, cel_col * u_term_tint, term * 0.55);

        if (u_use_ramp == 1) {
            vec3 ramp_s = texture2D(u_ramp_tex, vec2(clamp(ndl, 0.02, 0.98), 0.5)).rgb;
            cel_col *= mix(vec3(1.0), clamp(ramp_s * 1.08, 0.65, 1.15), u_ramp_mix);
        }

        vec3 hemi = mix(u_ground_color, u_sky_color, N.y * 0.5 + 0.5);
        cel_col = mix(cel_col, cel_col * hemi * 1.06, 0.22 * (1.0 - band_lit * 0.65));

        vec3 H = normalize(L + V);
        float spec_mask = (u_is_face == 1) ? 0.12 : 1.0;
        float spec = smoothstep(0.90, 0.935, max(dot(N, H), 0.0)) * u_toon_spec * spec_mask * (0.30 + 0.70 * band_lit);
        vec3 spec_add = u_spec_color * u_key_color * spec * 0.45;

        float fres = 1.0 - max(dot(N, V), 0.0);
        float rim_band = smoothstep(0.60, 0.78, fres) * u_rim_glow * u_preset_rim_strength * (0.35 + 0.65 * band_lit);
        vec3 rim_add = u_preset_rim_color * u_rim_color * rim_band * (u_is_face == 1 ? 0.55 : 1.0);

        lit = base.rgb * cel_col + spec_add + rim_add;
        float lum = dot(lit, vec3(0.299, 0.587, 0.114));
        lit = mix(vec3(lum), lit, 1.08);
        lit = lit / (vec3(1.0) + max(lit - vec3(0.92), vec3(0.0)) * 0.45);
        lit = clamp(lit, 0.0, 1.0);

        if (u_use_lut == 1) {
            float b_sc = lit.b * 31.0;
            float b0 = floor(b_sc);
            float b1 = min(31.0, b0 + 1.0);
            float bf = b_sc - b0;
            float v_lut = 1.0 - (lit.g * 31.0 + 0.5) / 32.0;
            float u0 = (b0 * 32.0 + lit.r * 31.0 + 0.5) / 1024.0;
            float u1 = (b1 * 32.0 + lit.r * 31.0 + 0.5) / 1024.0;
            vec3 graded = mix(
                texture2D(u_lut_tex, vec2(u0, v_lut)).rgb,
                texture2D(u_lut_tex, vec2(u1, v_lut)).rgb,
                bf
            );
            lit = mix(lit, graded, u_lut_strength);
        }
    } else {
        vec3 Nn = (u_is_face == 1) ? normalize(vec3(N.x, N.y * 0.30, N.z)) : N;
        vec3 L_flat = normalize(vec3(0.20, 0.85, 0.48));
        float diff = 0.86 + 0.14 * (dot(Nn, L_flat) * 0.5 + 0.5);
        lit = base.rgb * diff;
    }
    gl_FragColor = vec4(clamp(lit, 0.0, 1.0), u_alpha_mode == 2 ? base.a : 1.0);
}
"""


class GLBViewport(QOpenGLWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene: GLScene | None = None
        self.tex_ids: list[int] = []
        self._tex_max_mip: dict[int, int] = {}
        self._active_tex_lod: int = 0
        self._vbo_chunks: list[tuple[int, int]] = []
        self._sm_vbo_idx: list[int] = []
        self._sm_idx_bytes: list[int] = []
        self._sm_idx_counts: list[int] = []
        self._gpu_renderer: str = ""
        self._gpu_skin_supported: bool = False
        self._gpu_skin_active: bool = False
        self._max_aniso: float = 1.0
        self._max_tex_size: int = 4096
        self._skin_vbo_chunks: list[tuple[int, int]] = []
        self._skin_sm_vbo_idx: list[int] = []
        self._skin_sm_idx_bytes: list[int] = []
        self._skin_sm_idx_counts: list[int] = []
        self._bone_tex_id: int = 0
        self._bone_tex_h: int = 1
        self._skin_palette_offsets: list[int] = []
        self._skin_ident_row: int = 0
        self._bone_palette: np.ndarray | None = None
        self._bone_tex_dirty: bool = False
        self._cull_centers: np.ndarray = np.zeros((0, 3), dtype=np.float32)
        self._cull_radii: np.ndarray = np.zeros((0,), dtype=np.float32)
        self._cull_is_blend: np.ndarray = np.zeros((0,), dtype=bool)
        self._cull_is_weapon: np.ndarray = np.zeros((0,), dtype=bool)
        self._cull_is_sky: np.ndarray = np.zeros((0,), dtype=bool)
        self._cull_valid: np.ndarray = np.zeros((0,), dtype=bool)
        self._ref_distance: float = 3.0
        self.culling_enabled = True
        self.zoom_lod_enabled = False
        self.last_render_stats: dict = {"drawn": 0, "total": 0, "culled": 0, "tex_lod": 0}
        self._stats_cb = None
        self.wireframe = False
        self.show_textures = True
        self.show_grid = True
        self.show_skybox = False
        self.toon_enabled = True
        self._toon_presets: dict[str, dict] = _load_toon_presets()
        self.toon_preset: str = next(iter(self._toon_presets))
        self._preset_ramp_tids: dict[str, int] = {}
        self._preset_lut_tids: dict[str, int] = {}
        self._sky_prog: QOpenGLShaderProgram | None = None
        self._sky_u_locs: dict[str, int] = {}
        self._sky_vbo: int = 0
        self._sky_ibo: int = 0
        self._sky_idx_count: int = 0
        self._sky_cloud_high_tid: int = 0
        self._sky_cloud_low_tid: int = 0
        self.show_skeleton = False
        self.show_weapons = True
        self.anim_clip: UnityAnimClip | None = None
        self.anim_time: float = 0.0
        self._bone_keys: list[tuple[str, ...]] = []
        self._root_of: list[int] = []
        self._root_scale: dict[int, float] = {}
        self._primary_root: int = 0
        self._primary_bones: list[int] = []
        self._primary_bone_set: set[int] = set()
        self._display_scale: float = 1.0
        self._u_rest: list[np.ndarray] = []
        self._inv_u_rest: list[np.ndarray] = []
        self._u_rest_local: list[np.ndarray] = []
        self._rest_mesh_poses: list[tuple[np.ndarray, np.ndarray]] = []
        self._legacy_bind: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None] = []
        self._anim_y_offset_bone: int = -1
        self._anim_y_offset: float = 0.0
        self._anim_pos_scale: float = 1.0
        self._anim_bip_root: int = -1
        self._anim_bip_xz_offset: tuple[float, float] = (0.0, 0.0)
        self._anim_bip_lock_xz: bool = False
        self._last_anim_worlds: list[np.ndarray] = []
        self._toon_prog: QOpenGLShaderProgram | None = None
        self._u_locs: dict[str, int] = {}
        self.yaw = 35.0
        self.pitch = 20.0
        self.distance = 3.0
        self.target = np.zeros(3, dtype=np.float32)
        self._last_pos = None
        self._drag_mode = None
        self.fly_mode: bool = False
        self._fly_mode_cb = None
        self._keys_held: set[int] = set()
        self._nav_last_t: float = 0.0
        self._fly_speed_mul: float = 1.0
        self._nav_timer = QTimer(self)
        self._nav_timer.setInterval(16)
        self._nav_timer.timeout.connect(self._on_nav_tick)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(400, 320)

    def set_toon_preset(self, name: str):
        """Select a calibrated ZZZ toon lighting preset and repaint."""
        if name in self._toon_presets:
            self.toon_preset = name
            self.update()

    def _reset_scene_state(self):
        self.scene = None
        self.anim_clip = None
        self.anim_time = 0.0
        self._bone_keys = []
        self._root_of = []
        self._root_scale = {}
        self._primary_root = 0
        self._primary_bones = []
        self._primary_bone_set = set()
        self._display_scale = 1.0
        self._u_rest = []
        self._inv_u_rest = []
        self._u_rest_local = []
        self._rest_mesh_poses = []
        self._legacy_bind = []
        self._anim_y_offset_bone = -1
        self._anim_y_offset = 0.0
        self._anim_pos_scale = 1.0
        self._anim_bip_root = -1
        self._anim_bip_xz_offset = (0.0, 0.0)
        self._anim_bip_lock_xz = False
        self._last_anim_worlds = []

    def set_scene(self, scene: GLScene):
        gl_ok = self.isValid()
        if gl_ok:
            self.makeCurrent()
            self._free_vbos()
            self._free_textures()
        else:
            self._free_skin_vbos(delete_gl=False)
            self._vbo_chunks.clear()
            self._sm_vbo_idx.clear()
            self._sm_idx_bytes.clear()
            self._sm_idx_counts.clear()
            self.tex_ids.clear()
            self._tex_max_mip.clear()
            self._active_tex_lod = -1
        self._reset_scene_state()
        self.scene = scene
        if gl_ok:
            self._upload_textures()
            self._build_static_vbos()
        self._build_cull_arrays()
        self.reset_camera()
        if gl_ok:
            self.doneCurrent()
        _release_os_memory()
        self.update()

    def clear_scene(self) -> bool:
        """Unload active 3D scene and release all CPU arrays and GPU buffers/textures."""
        had = bool(
            self.scene is not None
            or self._vbo_chunks
            or self._skin_vbo_chunks
            or self.tex_ids
            or self._bone_tex_id
        )
        self._nav_timer.stop()
        self._keys_held.clear()
        if self.isValid():
            self.makeCurrent()
            self._free_vbos()
            self._free_textures()
            try:
                gl.glFlush()
                gl.glFinish()
            except Exception:
                pass
            self.doneCurrent()
        else:
            self._free_skin_vbos(delete_gl=False)
            self._vbo_chunks.clear()
            self._sm_vbo_idx.clear()
            self._sm_idx_bytes.clear()
            self._sm_idx_counts.clear()
            self.tex_ids.clear()
            self._tex_max_mip.clear()
            self._active_tex_lod = -1
        self._reset_scene_state()
        self._build_cull_arrays()
        self.last_render_stats = {"drawn": 0, "total": 0, "culled": 0, "tex_lod": 0}
        self.update()
        return had

    def set_animation(self, clip: UnityAnimClip | None):
        self.anim_clip = clip
        self.anim_time = 0.0
        self._anim_y_offset_bone = -1
        self._anim_y_offset = 0.0
        self._anim_pos_scale = 1.0
        self._anim_bip_root = -1
        self._anim_bip_xz_offset = (0.0, 0.0)
        self._anim_bip_lock_xz = False
        if clip is None:
            self._last_anim_worlds = []
            if self.scene and self._rest_mesh_poses:
                for sm, (rp, rn) in zip(self.scene.submeshes, self._rest_mesh_poses):
                    sm.positions = rp.copy()
                    sm.normals = rn.copy()
        else:
            self._prepare_bone_binding()
            if self.scene and self.scene.bones:
                bones = self.scene.bones
                for bi in self._primary_bones:
                    if re.fullmatch(r"bip\d+", bones[bi].name.lower()):
                        keys = self._bone_keys[bi] if bi < len(self._bone_keys) else (bones[bi].name,)
                        pc0 = self._lookup_curve(clip.pos_curves, keys)
                        if pc0 is not None and len(pc0[1]) > 0:
                            arr = pc0[1]
                            ry = float(self._u_rest[bi][1, 3]) if bi < len(self._u_rest) else 0.0
                            y0 = float(arr[0, 1])
                            if ry > 0.2 and abs(y0) > 20.0 * ry:
                                self._anim_pos_scale = 0.01
                                y0 *= 0.01
                            ps = self._anim_pos_scale
                            if abs(y0) < 0.05 and ry > 0.35:
                                self._anim_y_offset_bone = bi
                                self._anim_y_offset = -ry
                            elif abs(y0 - ry) > 1.5:
                                self._anim_y_offset_bone = bi
                                self._anim_y_offset = float(y0 - ry)
                            self._anim_bip_root = bi
                            self._anim_bip_xz_offset = (float(-arr[0, 0] * ps), float(arr[0, 2] * ps))
                            xz_pts = arr[:, [0, 2]] * ps
                            span_xz = float(np.linalg.norm(xz_pts.max(axis=0) - xz_pts.min(axis=0)))
                            self._anim_bip_lock_xz = span_xz > 0.65
                        break
            if self._gpu_skin_supported and not self._gpu_skin_active and self.isValid():
                self.makeCurrent()
                self._build_skin_vbos()
                self.doneCurrent()
            self._apply_skinning_frame()
        self.update()

    def set_anim_time(self, t: float):
        self.anim_time = max(0.0, float(t))
        if self.anim_clip is not None:
            self._apply_skinning_frame()
        self.update()

    def reset_camera(self):
        if self.scene is not None:
            non_sky = [sm.center for sm in self.scene.submeshes if not sm.is_sky and len(sm.indices)]
            if len(non_sky) > 20:
                pts = np.asarray(non_sky, dtype=np.float32)
                p10 = np.percentile(pts, 10, axis=0)
                p90 = np.percentile(pts, 90, axis=0)
                self.target = ((p10 + p90) * 0.5).astype(np.float32)
                self.distance = max(0.05, float(np.linalg.norm(p90 - p10)) * 0.85)
            else:
                self.target = self.scene.center.copy()
                self.distance = max(0.02, self.scene.radius * 2.4)
        else:
            self.target = np.zeros(3, dtype=np.float32)
            self.distance = 3.0
        self._ref_distance = max(0.05, float(self.distance))
        self.yaw = 35.0
        self.pitch = 20.0
        self.update()

    def _free_skin_vbos(self, delete_gl: bool = True):
        if self._skin_vbo_chunks:
            if delete_gl:
                try:
                    ids = [bid for pair in self._skin_vbo_chunks for bid in pair if bid]
                    if ids:
                        gl.glDeleteBuffers(len(ids), ids)
                except Exception:
                    pass
            self._skin_vbo_chunks.clear()
        self._skin_sm_vbo_idx.clear()
        self._skin_sm_idx_bytes.clear()
        self._skin_sm_idx_counts.clear()
        self._skin_palette_offsets.clear()
        if self._bone_tex_id:
            if delete_gl:
                try:
                    gl.glDeleteTextures([self._bone_tex_id])
                except Exception:
                    pass
            self._bone_tex_id = 0
        self._bone_tex_h = 1
        self._bone_palette = None
        self._bone_tex_dirty = False
        self._gpu_skin_active = False

    def _build_skin_vbos(self):
        self._free_skin_vbos()
        if not self._gpu_skin_supported or not self.scene or not self.scene.submeshes or not self.scene.bones:
            return
        self._prepare_bone_binding()
        if not self._rest_mesh_poses:
            return
        bones = self.scene.bones
        skins = self.scene.skins or []
        n_b = len(bones)
        palette_off = n_b
        self._skin_palette_offsets = []
        for jbones, _rest_mats in skins:
            self._skin_palette_offsets.append(palette_off)
            palette_off += len(jbones)
        self._skin_ident_row = palette_off
        self._bone_tex_h = max(1, palette_off + 1)

        max_verts_per_chunk = 900_000
        v_parts: list[np.ndarray] = []
        i_parts: list[np.ndarray] = []
        v_count = 0
        i_byte_off = 0
        chunk_idx = 0

        def _flush_skin():
            nonlocal v_parts, i_parts, v_count, i_byte_off, chunk_idx
            if not v_parts:
                return
            v_arr = np.ascontiguousarray(np.concatenate(v_parts, axis=0), dtype=np.float32)
            i_arr = np.ascontiguousarray(np.concatenate(i_parts, axis=0), dtype=np.uint32)
            vbo, ibo = gl.glGenBuffers(2)
            vbo = int(vbo)
            ibo = int(ibo)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, v_arr.nbytes, v_arr, gl.GL_STATIC_DRAW)
            gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, ibo)
            gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER, i_arr.nbytes, i_arr, gl.GL_STATIC_DRAW)
            self._skin_vbo_chunks.append((vbo, ibo))
            v_parts = []
            i_parts = []
            v_count = 0
            i_byte_off = 0
            chunk_idx += 1

        try:
            lbinds = self._legacy_bind if len(self._legacy_bind) == len(self.scene.submeshes) else [None] * len(self.scene.submeshes)
            for sm, (rp, rn), lbind in zip(self.scene.submeshes, self._rest_mesh_poses, lbinds):
                if sm.skin_idx >= 0 and sm.bind_positions is not None and sm.skin_joints is not None and sm.skin_weights is not None and sm.skin_idx < len(skins):
                    pos = sm.bind_positions
                    nrm = sm.bind_normals if sm.bind_normals is not None else rn
                    joints = sm.skin_joints.astype(np.float32) + float(self._skin_palette_offsets[sm.skin_idx])
                    weights = sm.skin_weights.astype(np.float32)
                elif lbind is not None:
                    pos, nrm = rp, rn
                    bi0, bi1, w0, w1 = lbind
                    joints = np.zeros((len(rp), 4), dtype=np.float32)
                    weights = np.zeros((len(rp), 4), dtype=np.float32)
                    joints[:, 0] = bi0
                    joints[:, 1] = bi1
                    weights[:, 0] = w0
                    weights[:, 1] = w1
                else:
                    pos, nrm = rp, rn
                    joints = np.zeros((len(rp), 4), dtype=np.float32)
                    joints[:, 0] = float(self._skin_ident_row)
                    weights = np.zeros((len(rp), 4), dtype=np.float32)
                    weights[:, 0] = 1.0

                n_v = len(pos)
                n_i = len(sm.indices)
                if n_v == 0 or n_i == 0:
                    self._skin_sm_vbo_idx.append(-1)
                    self._skin_sm_idx_bytes.append(0)
                    self._skin_sm_idx_counts.append(0)
                    continue
                if v_count > 0 and (v_count + n_v > max_verts_per_chunk):
                    _flush_skin()
                uvs = sm.uvs if (sm.uvs is not None and len(sm.uvs) == n_v) else np.zeros((n_v, 2), dtype=np.float32)
                packed = np.empty((n_v, 16), dtype=np.float32)
                packed[:, 0:3] = pos
                packed[:, 3:6] = nrm
                packed[:, 6:8] = uvs
                packed[:, 8:12] = joints[:, :4]
                packed[:, 12:16] = weights[:, :4]
                v_parts.append(packed)
                i_parts.append(sm.indices.astype(np.uint32) + np.uint32(v_count))
                self._skin_sm_vbo_idx.append(chunk_idx)
                self._skin_sm_idx_bytes.append(i_byte_off)
                self._skin_sm_idx_counts.append(n_i)
                v_count += n_v
                i_byte_off += n_i * 4
            _flush_skin()

            pal = np.repeat(np.eye(4, dtype=np.float32)[None, :, :], self._bone_tex_h, axis=0)
            while gl.glGetError() != gl.GL_NO_ERROR:
                pass
            gl.glActiveTexture(gl.GL_TEXTURE3)
            tid = int(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            gl.glTexImage2D(
                gl.GL_TEXTURE_2D, 0, 0x8814, 4, self._bone_tex_h, 0,
                gl.GL_RGBA, gl.GL_FLOAT, pal,
            )
            gl_err = gl.glGetError()
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
            gl.glActiveTexture(gl.GL_TEXTURE0)
            if gl_err != gl.GL_NO_ERROR or not tid:
                if tid:
                    try:
                        gl.glDeleteTextures([tid])
                    except Exception:
                        pass
                self._gpu_skin_supported = False
                self._free_skin_vbos()
                return
            self._bone_tex_id = tid
            self._bone_palette = pal
            self._bone_tex_dirty = True
            self._gpu_skin_active = bool(self._skin_vbo_chunks and tid)
        except Exception:
            self._free_skin_vbos()
        finally:
            try:
                gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
                gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, 0)
                gl.glActiveTexture(gl.GL_TEXTURE0)
            except Exception:
                pass

    def _free_vbos(self):
        self._free_skin_vbos()
        if self._vbo_chunks:
            try:
                ids = [bid for pair in self._vbo_chunks for bid in pair if bid]
                if ids:
                    gl.glDeleteBuffers(len(ids), ids)
            except Exception:
                pass
            self._vbo_chunks.clear()
        self._sm_vbo_idx.clear()
        self._sm_idx_bytes.clear()
        self._sm_idx_counts.clear()

    def _build_static_vbos(self):
        self._free_vbos()
        if not self.scene or not self.scene.submeshes:
            return
        max_verts_per_chunk = 900_000
        v_parts: list[np.ndarray] = []
        i_parts: list[np.ndarray] = []
        v_count = 0
        i_byte_off = 0
        chunk_idx = 0

        def _flush():
            nonlocal v_parts, i_parts, v_count, i_byte_off, chunk_idx
            if not v_parts:
                return
            v_arr = np.ascontiguousarray(np.concatenate(v_parts, axis=0), dtype=np.float32)
            i_arr = np.ascontiguousarray(np.concatenate(i_parts, axis=0), dtype=np.uint32)
            vbo, ibo = gl.glGenBuffers(2)
            vbo = int(vbo)
            ibo = int(ibo)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, v_arr.nbytes, v_arr, gl.GL_STATIC_DRAW)
            gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, ibo)
            gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER, i_arr.nbytes, i_arr, gl.GL_STATIC_DRAW)
            self._vbo_chunks.append((vbo, ibo))
            v_parts = []
            i_parts = []
            v_count = 0
            i_byte_off = 0
            chunk_idx += 1

        try:
            for sm in self.scene.submeshes:
                n_v = len(sm.positions)
                n_i = len(sm.indices)
                if n_v == 0 or n_i == 0:
                    self._sm_vbo_idx.append(-1)
                    self._sm_idx_bytes.append(0)
                    self._sm_idx_counts.append(0)
                    continue
                if v_count > 0 and (v_count + n_v > max_verts_per_chunk):
                    _flush()
                uvs = sm.uvs if (sm.uvs is not None and len(sm.uvs) == n_v) else np.zeros((n_v, 2), dtype=np.float32)
                packed = np.empty((n_v, 8), dtype=np.float32)
                packed[:, 0:3] = sm.positions
                packed[:, 3:6] = sm.normals
                packed[:, 6:8] = uvs
                v_parts.append(packed)
                i_parts.append(sm.indices.astype(np.uint32) + np.uint32(v_count))
                self._sm_vbo_idx.append(chunk_idx)
                self._sm_idx_bytes.append(i_byte_off)
                self._sm_idx_counts.append(n_i)
                v_count += n_v
                i_byte_off += n_i * 4
            _flush()
        except Exception:
            self._free_vbos()
        finally:
            try:
                gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
                gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, 0)
            except Exception:
                pass

    def _build_cull_arrays(self):
        if not self.scene or not self.scene.submeshes:
            self._cull_centers = np.zeros((0, 3), dtype=np.float32)
            self._cull_radii = np.zeros((0,), dtype=np.float32)
            self._cull_is_blend = np.zeros((0,), dtype=bool)
            self._cull_is_weapon = np.zeros((0,), dtype=bool)
            self._cull_is_sky = np.zeros((0,), dtype=bool)
            self._cull_valid = np.zeros((0,), dtype=bool)
            return
        sms = self.scene.submeshes
        self._cull_centers = np.asarray([sm.center for sm in sms], dtype=np.float32)
        radii = []
        for sm in sms:
            r = sm.bound_radius
            if r <= 0.0 and len(sm.positions):
                pmin = sm.positions.min(axis=0)
                pmax = sm.positions.max(axis=0)
                r = max(0.001, float(np.linalg.norm((pmax - pmin) * 0.5)))
            radii.append(max(0.001, float(r)))
        self._cull_radii = np.asarray(radii, dtype=np.float32)
        self._cull_is_blend = np.asarray([sm.alpha_mode == "BLEND" for sm in sms], dtype=bool)
        self._cull_is_weapon = np.asarray([sm.is_weapon for sm in sms], dtype=bool)
        self._cull_is_sky = np.asarray([sm.is_sky for sm in sms], dtype=bool)
        self._cull_valid = np.asarray([len(sm.indices) > 0 and len(sm.positions) > 0 for sm in sms], dtype=bool)

    def _free_textures(self):
        if self.tex_ids:
            try:
                uniq_ids = [tid for tid in set(self.tex_ids) if tid]
                if uniq_ids:
                    gl.glDeleteTextures(uniq_ids)
            except Exception:
                pass
            self.tex_ids.clear()
        self._tex_max_mip.clear()
        self._active_tex_lod = -1

    def _upload_textures(self):
        if not self.scene:
            return
        gl.glActiveTexture(gl.GL_TEXTURE0)
        uploaded: dict[int, int] = {}
        for sm in self.scene.submeshes:
            if sm.tex_rgba and sm.tex_w > 0 and sm.tex_h > 0:
                key = id(sm.tex_rgba)
                tid = uploaded.get(key)
                if tid is None:
                    tid = int(gl.glGenTextures(1))
                    gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
                    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
                    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
                    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
                    if self._max_aniso > 1.0:
                        try:
                            gl.glTexParameterf(gl.GL_TEXTURE_2D, 0x84FE, self._max_aniso)
                        except Exception:
                            pass
                    tw, th = int(sm.tex_w), int(sm.tex_h)
                    rgba_buf = sm.tex_rgba
                    if max(tw, th) > self._max_tex_size:
                        scale = self._max_tex_size / float(max(tw, th))
                        nw = max(1, int(round(tw * scale)))
                        nh = max(1, int(round(th * scale)))
                        im_up = Image.frombytes("RGBA", (tw, th), rgba_buf).resize((nw, nh), Image.BILINEAR)
                        tw, th = nw, nh
                        rgba_buf = im_up.tobytes()
                    gl.glTexImage2D(
                        gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, tw, th, 0,
                        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, rgba_buf,
                    )
                    max_mip = int(math.floor(math.log2(max(1, tw, th))))
                    try:
                        gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
                        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR)
                        self._tex_max_mip[tid] = max_mip
                    except Exception:
                        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
                        self._tex_max_mip[tid] = 0
                    uploaded[key] = tid
                self.tex_ids.append(tid)
            else:
                self.tex_ids.append(0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        if uploaded:
            for sm in self.scene.submeshes:
                sm.tex_rgba = None
        self._active_tex_lod = 0

    def _update_tex_lod(self):
        if not self._tex_max_mip:
            self._active_tex_lod = 0
            return
        if not self.zoom_lod_enabled or self.fly_mode:
            target_lvl = 0
        else:
            ref = max(0.05, self._ref_distance)
            zr = self.distance / ref
            if zr < 1.8:
                target_lvl = 0
            elif zr < 3.2:
                target_lvl = 1
            else:
                target_lvl = 2
        if target_lvl == self._active_tex_lod:
            return
        try:
            gl.glActiveTexture(gl.GL_TEXTURE0)
            for tid, max_mip in self._tex_max_mip.items():
                if max_mip > 0:
                    eff = min(target_lvl, max(0, max_mip - 4))
                    gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
                    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_BASE_LEVEL, eff)
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        except Exception:
            pass
        self._active_tex_lod = target_lvl

    @staticmethod
    def _upload_2d_lut(path: Path, wrap_s=None, wrap_t=None) -> int:
        """Upload a non-mipmapped ramp, LUT, or cloud map PNG to an OpenGL 2D texture."""
        if not path.is_file():
            return 0
        ws = wrap_s if wrap_s is not None else gl.GL_CLAMP_TO_EDGE
        wt = wrap_t if wrap_t is not None else gl.GL_CLAMP_TO_EDGE
        try:
            im = Image.open(path).convert("RGBA")
            rgba = im.tobytes()
            gl.glActiveTexture(gl.GL_TEXTURE0)
            tid = int(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, ws)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, wt)
            gl.glTexImage2D(
                gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, im.width, im.height, 0,
                gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, rgba,
            )
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
            return tid
        except Exception:
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
            return 0

    def _build_skybox_sphere(self):
        if self._sky_vbo:
            try:
                gl.glDeleteBuffers(2, [self._sky_vbo, self._sky_ibo])
            except Exception:
                pass
            self._sky_vbo = 0
            self._sky_ibo = 0
        n_lat, n_lon = 16, 32
        verts: list[tuple[float, float, float]] = []
        idxs: list[int] = []
        for i in range(n_lat + 1):
            theta = math.pi * i / n_lat
            st, ct = math.sin(theta), math.cos(theta)
            for j in range(n_lon + 1):
                phi = 2.0 * math.pi * j / n_lon
                verts.append((st * math.sin(phi), ct, st * math.cos(phi)))
        stride = n_lon + 1
        for i in range(n_lat):
            for j in range(n_lon):
                a = i * stride + j
                b = a + stride
                idxs.extend((a, b, a + 1, a + 1, b, b + 1))
        v_arr = np.ascontiguousarray(verts, dtype=np.float32)
        i_arr = np.ascontiguousarray(idxs, dtype=np.uint32)
        try:
            vbo, ibo = gl.glGenBuffers(2)
            self._sky_vbo = int(vbo)
            self._sky_ibo = int(ibo)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._sky_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, v_arr.nbytes, v_arr, gl.GL_STATIC_DRAW)
            gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, self._sky_ibo)
            gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER, i_arr.nbytes, i_arr, gl.GL_STATIC_DRAW)
            self._sky_idx_count = len(i_arr)
        except Exception:
            self._sky_vbo = 0
            self._sky_ibo = 0
            self._sky_idx_count = 0
        finally:
            try:
                gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
                gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, 0)
            except Exception:
                pass

    def _load_preset_textures(self):
        """Load and cache unique ZZZ ramp, 3D LUT, and skybox cloud textures."""
        presets_dir = getattr(cfg, "TOON_PRESETS_DIR", cfg.ASSETS_DIR / "toon_presets")
        for mapping in (self._preset_ramp_tids, self._preset_lut_tids):
            old_ids = [tid for tid in mapping.values() if tid]
            if old_ids:
                try:
                    gl.glDeleteTextures(old_ids)
                except Exception:
                    pass
            mapping.clear()
        old_clouds = [tid for tid in (self._sky_cloud_high_tid, self._sky_cloud_low_tid) if tid]
        if old_clouds:
            try:
                gl.glDeleteTextures(old_clouds)
            except Exception:
                pass
        self._sky_cloud_high_tid = 0
        self._sky_cloud_low_tid = 0
        if not presets_dir.is_dir():
            return
        for pr in self._toon_presets.values():
            rf = (pr.get("ramp_file") or "").strip()
            if rf and rf not in self._preset_ramp_tids:
                self._preset_ramp_tids[rf] = self._upload_2d_lut(presets_dir / rf)
            lf = (pr.get("lut_file") or "").strip()
            if lf and lf not in self._preset_lut_tids:
                self._preset_lut_tids[lf] = self._upload_2d_lut(presets_dir / lf)
        self._sky_cloud_high_tid = self._upload_2d_lut(
            presets_dir / "HighCloudMap_01.png", wrap_s=gl.GL_REPEAT, wrap_t=gl.GL_REPEAT
        )
        self._sky_cloud_low_tid = self._upload_2d_lut(
            presets_dir / "LowCloudMap_02.png", wrap_s=gl.GL_REPEAT, wrap_t=gl.GL_CLAMP_TO_EDGE
        )

    def initializeGL(self):
        try:
            raw_r = gl.glGetString(gl.GL_RENDERER)
            r_str = raw_r.decode("utf-8", errors="ignore") if isinstance(raw_r, bytes) else str(raw_r or "")
            self._gpu_renderer = r_str.replace("/PCIe/SSE2", "").replace("Laptop GPU", "").strip()
        except Exception:
            self._gpu_renderer = ""
        try:
            self._gpu_skin_supported = int(gl.glGetIntegerv(gl.GL_MAX_VERTEX_TEXTURE_IMAGE_UNITS)) >= 1
        except Exception:
            self._gpu_skin_supported = False
        try:
            self._max_aniso = min(16.0, float(gl.glGetFloatv(0x84FF)))
        except Exception:
            self._max_aniso = 1.0
        try:
            max_ts = int(gl.glGetIntegerv(gl.GL_MAX_TEXTURE_SIZE))
            self._max_tex_size = max(1024, min(8192, max_ts)) if max_ts > 0 else 4096
        except Exception:
            self._max_tex_size = 4096
        rend_low = self._gpu_renderer.lower()
        if any(tok in rend_low for tok in ("gdi generic", "llvmpipe", "softpipe", "microsoft basic render")):
            self._max_tex_size = min(self._max_tex_size, 2048)
            if "gdi generic" in rend_low:
                self._gpu_skin_supported = False

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
        gl.glLightfv(gl.GL_LIGHT1, gl.GL_POSITION, [-5.0, -2.0, -4.0, 0.0])
        gl.glLightfv(gl.GL_LIGHT1, gl.GL_DIFFUSE, [0.35, 0.42, 0.55, 1.0])
        gl.glLightfv(gl.GL_LIGHT0, gl.GL_SPECULAR, [0.0, 0.0, 0.0, 1.0])
        gl.glLightfv(gl.GL_LIGHT1, gl.GL_SPECULAR, [0.0, 0.0, 0.0, 1.0])
        gl.glMaterialfv(gl.GL_FRONT_AND_BACK, gl.GL_SPECULAR, [0.0, 0.0, 0.0, 1.0])
        gl.glMaterialf(gl.GL_FRONT_AND_BACK, gl.GL_SHININESS, 0.0)
        prog = QOpenGLShaderProgram(self)
        if prog.addShaderFromSourceCode(QOpenGLShader.Vertex, _TOON_VERT) and prog.addShaderFromSourceCode(QOpenGLShader.Fragment, _TOON_FRAG) and prog.link():
            self._toon_prog = prog
            self._u_locs = {
                name: prog.uniformLocation(name)
                for name in (
                    "u_tex", "u_use_tex", "u_color", "u_alpha_mode", "u_alpha_cutoff",
                    "u_is_sky", "u_is_face", "u_toon_enabled", "u_cam_pos", "u_light_dir",
                    "u_toon_smooth", "u_toon_spec", "u_rim_glow", "u_rim_color",
                    "u_shallow_tint", "u_shadow_tint", "u_spec_color",
                    "u_key_color", "u_key_intensity", "u_preset_shallow", "u_preset_shadow",
                    "u_term_tint", "u_sky_color", "u_ground_color", "u_preset_rim_color",
                    "u_preset_rim_strength", "u_cel_threshold", "u_cel_smooth_scale",
                    "u_sky_tint", "u_ramp_tex", "u_use_ramp", "u_ramp_mix",
                    "u_lut_tex", "u_use_lut", "u_lut_strength",
                    "u_bone_tex", "u_bone_tex_h", "u_skin_enabled",
                )
            }
        sprog = QOpenGLShaderProgram(self)
        if sprog.addShaderFromSourceCode(QOpenGLShader.Vertex, _SKYBOX_VERT) and sprog.addShaderFromSourceCode(QOpenGLShader.Fragment, _SKYBOX_FRAG) and sprog.link():
            self._sky_prog = sprog
            self._sky_u_locs = {
                name: sprog.uniformLocation(name)
                for name in (
                    "u_light_dir", "u_sky_zenith", "u_sky_mid", "u_sky_horizon", "u_sky_ground",
                    "u_sun_color", "u_sun_size", "u_star_strength", "u_cloud_lit", "u_cloud_shadow",
                    "u_cloud_density", "u_high_cloud_tex", "u_low_cloud_tex",
                    "u_use_high_cloud", "u_use_low_cloud",
                )
            }
        self._build_skybox_sphere()
        self._load_preset_textures()
        if self.scene is not None:
            if not self.tex_ids:
                self._upload_textures()
            if not self._vbo_chunks:
                self._build_static_vbos()
            if self.anim_clip is not None and self._gpu_skin_supported and not self._gpu_skin_active:
                self._build_skin_vbos()
            if len(self._cull_centers) == 0:
                self._build_cull_arrays()

    def _draw_skybox(self, pr_cfg: dict):
        if not self._sky_prog or not self._sky_vbo or not self._sky_ibo or not self._sky_idx_count:
            return
        ul = self._sky_u_locs
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(gl.GL_FALSE)
        gl.glDisable(gl.GL_BLEND)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        self._sky_prog.bind()

        def _su3(name: str, key: str, default: tuple[float, float, float]):
            loc = ul.get(name, -1)
            if loc >= 0:
                v = pr_cfg.get(key, default)
                gl.glUniform3f(loc, float(v[0]), float(v[1]), float(v[2]))

        def _su1(name: str, key: str, default: float):
            loc = ul.get(name, -1)
            if loc >= 0:
                gl.glUniform1f(loc, float(pr_cfg.get(key, default)))

        _su3("u_light_dir", "light_dir", (0.46, 0.72, 0.52))
        _su3("u_sky_zenith", "sky_zenith", (0.20, 0.50, 0.90))
        _su3("u_sky_mid", "sky_mid", (0.46, 0.74, 0.96))
        _su3("u_sky_horizon", "sky_horizon", (0.84, 0.92, 0.99))
        _su3("u_sky_ground", "sky_ground", (0.58, 0.68, 0.78))
        _su3("u_sun_color", "sun_color", (1.0, 0.96, 0.85))
        _su1("u_sun_size", "sun_size", 1.0)
        _su1("u_star_strength", "star_strength", 0.0)
        _su3("u_cloud_lit", "cloud_lit", (1.0, 0.99, 0.97))
        _su3("u_cloud_shadow", "cloud_shadow", (0.70, 0.80, 0.92))
        _su1("u_cloud_density", "cloud_density", 0.72)

        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._sky_cloud_high_tid)
        if ul.get("u_high_cloud_tex", -1) >= 0:
            gl.glUniform1i(ul["u_high_cloud_tex"], 0)
        if ul.get("u_use_high_cloud", -1) >= 0:
            gl.glUniform1i(ul["u_use_high_cloud"], 1 if self._sky_cloud_high_tid else 0)

        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._sky_cloud_low_tid)
        if ul.get("u_low_cloud_tex", -1) >= 0:
            gl.glUniform1i(ul["u_low_cloud_tex"], 1)
        if ul.get("u_use_low_cloud", -1) >= 0:
            gl.glUniform1i(ul["u_use_low_cloud"], 1 if self._sky_cloud_low_tid else 0)

        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glEnableClientState(gl.GL_VERTEX_ARRAY)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._sky_vbo)
        gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, self._sky_ibo)
        gl.glVertexPointer(3, gl.GL_FLOAT, 12, ctypes.c_void_p(0))
        gl.glDrawElements(gl.GL_TRIANGLES, self._sky_idx_count, gl.GL_UNSIGNED_INT, ctypes.c_void_p(0))
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, 0)
        gl.glDisableClientState(gl.GL_VERTEX_ARRAY)
        self._sky_prog.release()
        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glEnable(gl.GL_DEPTH_TEST)

    def _set_projection(self, w: int, h: int) -> np.ndarray:
        h = max(1, h)
        aspect = w / float(h)
        scene_r = self.scene.radius if self.scene else 1.0
        near = max(0.0005, min(self.distance * 0.002, scene_r * 0.001))
        far = max(200.0, self.distance * 50.0, scene_r * 25.0)
        tan_half = math.tan(math.radians(45.0) * 0.5)
        top = near * tan_half
        right = top * aspect
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glLoadIdentity()
        gl.glFrustum(-right, right, -top, top, near, far)
        f = 1.0 / tan_half
        proj = np.zeros((4, 4), dtype=np.float32)
        proj[0, 0] = f / aspect
        proj[1, 1] = f
        proj[2, 2] = -(far + near) / (far - near)
        proj[2, 3] = -(2.0 * far * near) / (far - near)
        proj[3, 2] = -1.0
        return proj

    def resizeGL(self, w: int, h: int):
        h = max(1, h)
        gl.glViewport(0, 0, w, h)
        self._set_projection(w, h)
        gl.glMatrixMode(gl.GL_MODELVIEW)

    @staticmethod
    def _extract_frustum_planes(clip: np.ndarray) -> np.ndarray:
        planes = np.empty((6, 4), dtype=np.float32)
        planes[0] = clip[3] + clip[0]
        planes[1] = clip[3] - clip[0]
        planes[2] = clip[3] + clip[1]
        planes[3] = clip[3] - clip[1]
        planes[4] = clip[3] + clip[2]
        planes[5] = clip[3] - clip[2]
        norms = np.linalg.norm(planes[:, :3], axis=1, keepdims=True)
        planes /= np.maximum(norms, 1e-8)
        return planes

    def paintGL(self):
        pr_cfg = self._toon_presets.get(self.toon_preset) or next(iter(self._toon_presets.values()))
        if self.toon_enabled:
            bg = pr_cfg.get("backdrop", (0.13, 0.19, 0.27))
            gl.glClearColor(float(bg[0]), float(bg[1]), float(bg[2]), 1.0)
        else:
            gl.glClearColor(0.07, 0.085, 0.12, 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        w = max(1, self.width())
        h = max(1, self.height())
        proj_m = self._set_projection(w, h)

        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glLoadIdentity()

        yr = math.radians(self.yaw)
        pr = math.radians(self.pitch)
        cx = float(self.target[0] + self.distance * math.cos(pr) * math.sin(yr))
        cy = float(self.target[1] + self.distance * math.sin(pr))
        cz = float(self.target[2] + self.distance * math.cos(pr) * math.cos(yr))
        mv_m = self._glu_look_at(cx, cy, cz, self.target[0], self.target[1], self.target[2])

        if self.show_skybox and not self.wireframe:
            self._draw_skybox(pr_cfg)

        if self.show_grid:
            self._draw_grid()

        if not self.scene or not self.scene.submeshes:
            return

        if len(self._cull_centers) != len(self.scene.submeshes):
            self._build_cull_arrays()

        self._update_tex_lod()

        sms = self.scene.submeshes
        n_total = len(sms)
        vis = self._cull_valid.copy()
        if not self.show_weapons:
            vis &= ~self._cull_is_weapon
        if self.show_skybox:
            vis &= ~self._cull_is_sky

        d2: np.ndarray | None = None
        if n_total > 64 and self.anim_clip is None:
            planes = self._extract_frustum_planes(proj_m @ mv_m)
            signed = self._cull_centers @ planes[:, :3].T + planes[:, 3]
            vis &= np.all(signed >= -self._cull_radii[:, None], axis=1)

        if self.culling_enabled and n_total > 250 and np.any(vis):
            diff = self._cull_centers - np.array([cx, cy, cz], dtype=np.float32)
            d2 = np.einsum("ij,ij->i", diff, diff)
            px_scale = float(h) / math.tan(math.radians(45.0) * 0.5)
            proj_px = (self._cull_radii / np.sqrt(np.maximum(d2, 1e-6))) * px_scale
            if self.fly_mode:
                min_px = 1.0
            else:
                zr = self.distance / max(0.05, self._ref_distance)
                min_px = 0.8 if zr < 1.0 else (1.4 if zr < 2.0 else 2.2)
            vis &= (self._cull_is_sky | (proj_px >= min_px))

        opaque_idx = np.flatnonzero(vis & ~self._cull_is_blend)
        blend_idx = np.flatnonzero(vis & self._cull_is_blend)

        use_shader = self._toon_prog is not None and not self.wireframe
        if use_shader and self.anim_clip is not None and self._gpu_skin_supported and not self._gpu_skin_active and self.scene.bones:
            self._build_skin_vbos()
            if self._gpu_skin_active:
                self._apply_skinning_frame()
        use_gpu_skin = bool(
            use_shader
            and self.anim_clip is not None
            and self._gpu_skin_active
            and self._bone_tex_id
            and self._skin_vbo_chunks
            and len(self._skin_sm_vbo_idx) == n_total
        )
        use_static_vbo = bool(self._vbo_chunks) and self.anim_clip is None and len(self._sm_vbo_idx) == n_total
        use_vbo = use_static_vbo or use_gpu_skin
        sm_vbo = self._skin_sm_vbo_idx if use_gpu_skin else self._sm_vbo_idx
        sm_idx_bytes = self._skin_sm_idx_bytes if use_gpu_skin else self._sm_idx_bytes
        sm_idx_counts = self._skin_sm_idx_counts if use_gpu_skin else self._sm_idx_counts
        vbo_chunks = self._skin_vbo_chunks if use_gpu_skin else self._vbo_chunks
        vbo_stride = 64 if use_gpu_skin else 32

        tex_ids = self.tex_ids
        n_tids = len(tex_ids)
        opaque_list = opaque_idx.tolist()
        if use_vbo:
            opaque_list.sort(key=lambda i: (sm_vbo[i], tex_ids[i] if i < n_tids else 0))
        else:
            opaque_list.sort(key=lambda i: (tex_ids[i] if i < n_tids else 0))

        blend_list: list[int] = []
        if len(blend_idx):
            if d2 is None:
                diff = self._cull_centers[blend_idx] - np.array([cx, cy, cz], dtype=np.float32)
                bd2 = np.einsum("ij,ij->i", diff, diff)
            else:
                bd2 = d2[blend_idx]
            order = np.argsort(-bd2)
            blend_list = blend_idx[order].tolist()

        drawn_n = len(opaque_list) + len(blend_list)
        total_valid = int(self._cull_valid.sum())
        self.last_render_stats = {
            "drawn": drawn_n,
            "total": total_valid,
            "culled": max(0, total_valid - drawn_n),
            "tex_lod": self._active_tex_lod,
            "gpu": self._gpu_renderer,
        }
        if self._stats_cb is not None:
            self._stats_cb(self.last_render_stats)

        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE if self.wireframe else gl.GL_FILL)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glEnableClientState(gl.GL_VERTEX_ARRAY)
        gl.glEnableClientState(gl.GL_NORMAL_ARRAY)

        ul = self._u_locs
        if use_shader:
            self._toon_prog.bind()
            if ul.get("u_toon_enabled", -1) >= 0:
                gl.glUniform1i(ul["u_toon_enabled"], 1 if self.toon_enabled else 0)
            if ul.get("u_skin_enabled", -1) >= 0:
                gl.glUniform1i(ul["u_skin_enabled"], 1 if use_gpu_skin else 0)
            if use_gpu_skin:
                gl.glActiveTexture(gl.GL_TEXTURE3)
                gl.glBindTexture(gl.GL_TEXTURE_2D, self._bone_tex_id)
                if self._bone_tex_dirty and self._bone_palette is not None:
                    gl.glTexSubImage2D(
                        gl.GL_TEXTURE_2D, 0, 0, 0, 4, self._bone_tex_h,
                        gl.GL_RGBA, gl.GL_FLOAT, self._bone_palette,
                    )
                    self._bone_tex_dirty = False
                if ul.get("u_bone_tex", -1) >= 0:
                    gl.glUniform1i(ul["u_bone_tex"], 3)
                if ul.get("u_bone_tex_h", -1) >= 0:
                    gl.glUniform1f(ul["u_bone_tex_h"], float(self._bone_tex_h))
                gl.glClientActiveTexture(gl.GL_TEXTURE1)
                gl.glEnableClientState(gl.GL_TEXTURE_COORD_ARRAY)
                gl.glClientActiveTexture(gl.GL_TEXTURE2)
                gl.glEnableClientState(gl.GL_TEXTURE_COORD_ARRAY)
                gl.glClientActiveTexture(gl.GL_TEXTURE0)
            if ul.get("u_cam_pos", -1) >= 0:
                gl.glUniform3f(ul["u_cam_pos"], cx, cy, cz)
            ld = pr_cfg.get("light_dir", (0.46, 0.72, 0.52))
            if ul.get("u_light_dir", -1) >= 0:
                gl.glUniform3f(ul["u_light_dir"], float(ld[0]), float(ld[1]), float(ld[2]))
            if ul.get("u_tex", -1) >= 0:
                gl.glUniform1i(ul["u_tex"], 0)

            def _u3(name: str, key: str, default: tuple[float, float, float]):
                loc = ul.get(name, -1)
                if loc >= 0:
                    v = pr_cfg.get(key, default)
                    gl.glUniform3f(loc, float(v[0]), float(v[1]), float(v[2]))

            def _u1(name: str, key: str, default: float):
                loc = ul.get(name, -1)
                if loc >= 0:
                    gl.glUniform1f(loc, float(pr_cfg.get(key, default)))

            _u3("u_key_color", "key_color", (1.0, 0.968, 0.905))
            _u1("u_key_intensity", "key_intensity", 1.13)
            _u3("u_preset_shallow", "shallow_color", (1.0, 0.91, 0.905))
            _u3("u_preset_shadow", "shadow_color", (0.815, 0.755, 0.835))
            _u3("u_term_tint", "term_tint", (1.04, 0.9, 0.87))
            _u3("u_sky_color", "sky_color", (0.825, 0.907, 0.986))
            _u3("u_ground_color", "ground_color", (0.959, 0.972, 0.974))
            _u3("u_preset_rim_color", "rim_color", (0.96, 0.98, 1.0))
            _u1("u_preset_rim_strength", "rim_strength", 0.38)
            _u1("u_cel_threshold", "cel_threshold", 0.46)
            _u1("u_cel_smooth_scale", "cel_smooth_scale", 1.0)
            _u3("u_sky_tint", "sky_tint", (1.0, 1.0, 1.0))
            _u1("u_ramp_mix", "ramp_mix", 0.18)
            lut_strength = float(pr_cfg.get("lut_strength", 0.0))
            if ul.get("u_lut_strength", -1) >= 0:
                gl.glUniform1f(ul["u_lut_strength"], lut_strength)

            rf = (pr_cfg.get("ramp_file") or "").strip()
            ramp_tid = self._preset_ramp_tids.get(rf, 0) if (self.toon_enabled and rf) else 0
            gl.glActiveTexture(gl.GL_TEXTURE1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, ramp_tid)
            if ul.get("u_ramp_tex", -1) >= 0:
                gl.glUniform1i(ul["u_ramp_tex"], 1)
            if ul.get("u_use_ramp", -1) >= 0:
                gl.glUniform1i(ul["u_use_ramp"], 1 if ramp_tid else 0)

            lf = (pr_cfg.get("lut_file") or "").strip()
            lut_tid = self._preset_lut_tids.get(lf, 0) if (self.toon_enabled and lf and lut_strength > 0.0) else 0
            gl.glActiveTexture(gl.GL_TEXTURE2)
            gl.glBindTexture(gl.GL_TEXTURE_2D, lut_tid)
            if ul.get("u_lut_tex", -1) >= 0:
                gl.glUniform1i(ul["u_lut_tex"], 2)
            if ul.get("u_use_lut", -1) >= 0:
                gl.glUniform1i(ul["u_use_lut"], 1 if lut_tid else 0)

            gl.glActiveTexture(gl.GL_TEXTURE0)

        bound_chunk = -2
        bound_tid = -1
        tex_state_on = None
        last_u = {}

        def _draw_sm(i: int):
            nonlocal bound_chunk, bound_tid, tex_state_on
            sm = sms[i]
            tid = tex_ids[i] if i < n_tids else 0
            has_tex = bool(self.show_textures and tid and sm.uvs is not None)
            if has_tex:
                if tex_state_on is not True:
                    gl.glActiveTexture(gl.GL_TEXTURE0)
                    gl.glClientActiveTexture(gl.GL_TEXTURE0)
                    gl.glEnable(gl.GL_TEXTURE_2D)
                    gl.glEnableClientState(gl.GL_TEXTURE_COORD_ARRAY)
                    tex_state_on = True
                if tid != bound_tid:
                    gl.glActiveTexture(gl.GL_TEXTURE0)
                    gl.glBindTexture(gl.GL_TEXTURE_2D, tid)
                    bound_tid = tid
                if not use_vbo:
                    gl.glClientActiveTexture(gl.GL_TEXTURE0)
                    gl.glTexCoordPointer(2, gl.GL_FLOAT, 0, sm.uvs)
            else:
                if tex_state_on is not False:
                    gl.glActiveTexture(gl.GL_TEXTURE0)
                    gl.glDisable(gl.GL_TEXTURE_2D)
                    if not use_gpu_skin:
                        gl.glClientActiveTexture(gl.GL_TEXTURE0)
                        gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
                    tex_state_on = False
                    bound_tid = 0

            col = sm.color
            if use_shader:
                am_int = 2 if sm.alpha_mode == "BLEND" else (1 if sm.alpha_mode == "MASK" else 0)
                ut = 1 if has_tex else 0
                if last_u.get("use_tex") != ut and ul.get("u_use_tex", -1) >= 0:
                    gl.glUniform1i(ul["u_use_tex"], ut)
                    last_u["use_tex"] = ut
                if last_u.get("col") != col and ul.get("u_color", -1) >= 0:
                    gl.glUniform4f(ul["u_color"], float(col[0]), float(col[1]), float(col[2]), float(col[3]))
                    last_u["col"] = col
                if last_u.get("am") != am_int and ul.get("u_alpha_mode", -1) >= 0:
                    gl.glUniform1i(ul["u_alpha_mode"], am_int)
                    last_u["am"] = am_int
                if am_int == 1 and last_u.get("ac") != sm.alpha_cutoff and ul.get("u_alpha_cutoff", -1) >= 0:
                    gl.glUniform1f(ul["u_alpha_cutoff"], float(sm.alpha_cutoff))
                    last_u["ac"] = sm.alpha_cutoff
                sky_i = 1 if sm.is_sky else 0
                if last_u.get("sky") != sky_i and ul.get("u_is_sky", -1) >= 0:
                    gl.glUniform1i(ul["u_is_sky"], sky_i)
                    last_u["sky"] = sky_i
                face_i = 1 if sm.is_face else 0
                if last_u.get("face") != face_i and ul.get("u_is_face", -1) >= 0:
                    gl.glUniform1i(ul["u_is_face"], face_i)
                    last_u["face"] = face_i
                toon_key = (
                    sm.toon_smoothness, sm.toon_specular, sm.rim_glow,
                    sm.rim_color, sm.shallow_tint, sm.shadow_tint, sm.spec_color,
                )
                if last_u.get("toon") != toon_key:
                    if ul.get("u_toon_smooth", -1) >= 0:
                        gl.glUniform1f(ul["u_toon_smooth"], float(sm.toon_smoothness))
                    if ul.get("u_toon_spec", -1) >= 0:
                        gl.glUniform1f(ul["u_toon_spec"], float(sm.toon_specular))
                    if ul.get("u_rim_glow", -1) >= 0:
                        gl.glUniform1f(ul["u_rim_glow"], float(sm.rim_glow))
                    if ul.get("u_rim_color", -1) >= 0:
                        gl.glUniform3f(ul["u_rim_color"], float(sm.rim_color[0]), float(sm.rim_color[1]), float(sm.rim_color[2]))
                    if ul.get("u_shallow_tint", -1) >= 0:
                        gl.glUniform3f(ul["u_shallow_tint"], float(sm.shallow_tint[0]), float(sm.shallow_tint[1]), float(sm.shallow_tint[2]))
                    if ul.get("u_shadow_tint", -1) >= 0:
                        gl.glUniform3f(ul["u_shadow_tint"], float(sm.shadow_tint[0]), float(sm.shadow_tint[1]), float(sm.shadow_tint[2]))
                    if ul.get("u_spec_color", -1) >= 0:
                        gl.glUniform3f(ul["u_spec_color"], float(sm.spec_color[0]), float(sm.spec_color[1]), float(sm.spec_color[2]))
                    last_u["toon"] = toon_key
            else:
                gl.glColor4f(*col)
                if sm.alpha_mode == "MASK":
                    gl.glEnable(gl.GL_ALPHA_TEST)
                    gl.glAlphaFunc(gl.GL_GREATER, sm.alpha_cutoff)
                else:
                    gl.glDisable(gl.GL_ALPHA_TEST)

            if use_vbo:
                v_idx = sm_vbo[i]
                if v_idx < 0:
                    return
                if v_idx != bound_chunk:
                    vbo, ibo = vbo_chunks[v_idx]
                    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
                    gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, ibo)
                    gl.glVertexPointer(3, gl.GL_FLOAT, vbo_stride, ctypes.c_void_p(0))
                    gl.glNormalPointer(gl.GL_FLOAT, vbo_stride, ctypes.c_void_p(12))
                    gl.glClientActiveTexture(gl.GL_TEXTURE0)
                    gl.glTexCoordPointer(2, gl.GL_FLOAT, vbo_stride, ctypes.c_void_p(24))
                    if use_gpu_skin:
                        gl.glClientActiveTexture(gl.GL_TEXTURE1)
                        gl.glTexCoordPointer(4, gl.GL_FLOAT, 64, ctypes.c_void_p(32))
                        gl.glClientActiveTexture(gl.GL_TEXTURE2)
                        gl.glTexCoordPointer(4, gl.GL_FLOAT, 64, ctypes.c_void_p(48))
                        gl.glClientActiveTexture(gl.GL_TEXTURE0)
                    bound_chunk = v_idx
                gl.glDrawElements(
                    gl.GL_TRIANGLES,
                    sm_idx_counts[i],
                    gl.GL_UNSIGNED_INT,
                    ctypes.c_void_p(sm_idx_bytes[i]),
                )
            else:
                gl.glVertexPointer(3, gl.GL_FLOAT, 0, sm.positions)
                gl.glNormalPointer(gl.GL_FLOAT, 0, sm.normals)
                gl.glDrawElements(gl.GL_TRIANGLES, len(sm.indices), gl.GL_UNSIGNED_INT, sm.indices)

        try:
            gl.glDisable(gl.GL_BLEND)
            gl.glDepthMask(gl.GL_TRUE)
            for i in opaque_list:
                _draw_sm(i)

            if blend_list:
                gl.glEnable(gl.GL_BLEND)
                gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
                gl.glDepthMask(gl.GL_FALSE)
                gl.glEnable(gl.GL_POLYGON_OFFSET_FILL)
                gl.glPolygonOffset(-1.0, -1.0)
                for i in blend_list:
                    _draw_sm(i)
                gl.glDisable(gl.GL_POLYGON_OFFSET_FILL)
                gl.glDepthMask(gl.GL_TRUE)
                gl.glDisable(gl.GL_BLEND)
        finally:
            if use_vbo and bound_chunk >= 0:
                gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
                gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, 0)

        if use_shader:
            self._toon_prog.release()
        if use_gpu_skin:
            gl.glClientActiveTexture(gl.GL_TEXTURE2)
            gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
            gl.glClientActiveTexture(gl.GL_TEXTURE1)
            gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        gl.glClientActiveTexture(gl.GL_TEXTURE0)
        gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
        for unit in (gl.GL_TEXTURE3, gl.GL_TEXTURE2, gl.GL_TEXTURE1, gl.GL_TEXTURE0):
            gl.glActiveTexture(unit)
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl.glDisable(gl.GL_ALPHA_TEST)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glDisableClientState(gl.GL_NORMAL_ARRAY)
        gl.glDisableClientState(gl.GL_VERTEX_ARRAY)
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)

        if self.show_skeleton and self.scene and self.scene.bones and self.anim_clip is not None:
            self._draw_skeleton()

    @staticmethod
    def _glu_look_at(ex, ey, ez, tx, ty, tz) -> np.ndarray:
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
        mv = (m @ t).astype(np.float32)
        gl.glMultMatrixf(mv.T)
        return mv

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
        if self._rest_mesh_poses or not self.scene or not self.scene.bones:
            return
        bones = self.scene.bones
        paths: list[str] = []
        self._bone_keys = []
        self._root_of = []
        self._root_scale = {}
        for i, b in enumerate(bones):
            p = b.name if b.parent < 0 else f"{paths[b.parent]}/{b.name}"
            paths.append(p)
            parts = p.split("/")
            keys: list[str] = []
            # Most-specific multi-segment paths first, then CRC32 hashes, leaf, and normalized fallbacks
            for k in range(len(parts) - 1):
                keys.append("/".join(parts[k:]))
            for k in range(len(parts)):
                keys.append(f"path_{zlib.crc32('/'.join(parts[k:]).encode('utf-8'))}")
            keys.append(b.name)
            clean_leaf = re.sub(r"\.\d+$", "", b.name)
            if clean_leaf != b.name:
                keys.append(clean_leaf)
            canon_leaf = re.sub(r"\bbip\d+\b", "Bip001", clean_leaf, flags=re.I)
            if canon_leaf != clean_leaf and canon_leaf != b.name:
                keys.append(canon_leaf)
            for k in range(len(parts)):
                sub_p = re.sub(r"\.\d+(?=/|$)", "", "/".join(parts[k:]))
                canon_p = re.sub(r"\bbip\d+\b", "Bip001", sub_p, flags=re.I)
                if canon_p != "/".join(parts[k:]):
                    keys.append(f"path_{zlib.crc32(canon_p.encode('utf-8'))}")
            self._bone_keys.append(tuple(dict.fromkeys(keys)))
            if b.parent < 0:
                self._root_of.append(i)
                rs = float(np.mean(np.abs(b.scale)))
                self._root_scale[i] = round(1.0 / rs, 4) if (1e-4 < rs < 0.25) else 1.0
            else:
                self._root_of.append(self._root_of[b.parent])

        roots = [i for i, b in enumerate(bones) if b.parent < 0]
        primary_root = roots[0] if roots else 0
        used_skin_roots: list[int] = []
        if self.scene.skins:
            for sm in self.scene.submeshes:
                if 0 <= sm.skin_idx < len(self.scene.skins):
                    for jb in self.scene.skins[sm.skin_idx][0]:
                        if 0 <= jb < len(self._root_of):
                            used_skin_roots.append(self._root_of[jb])
                            break
        if used_skin_roots:
            primary_root = used_skin_roots[0]
        elif len(roots) > 1:
            def _rscore(r: int):
                idxs = [i for i, ro in enumerate(self._root_of) if ro == r]
                bips = sum(
                    1 for i in idxs
                    if re.search(r"bip\d+", bones[i].name, re.I) and not re.search(r"\.\d+$", bones[i].name)
                )
                nl = bones[r].name.lower()
                pen = -1 if any(t in nl for t in ("_npc", "_ui", "dream", "collider")) else 0
                return (1 if bips > 0 else 0, pen, bips, -len(nl))
            primary_root = max(roots, key=_rscore)

        self._primary_root = primary_root
        self._primary_bones = [i for i, ro in enumerate(self._root_of) if ro == primary_root]
        self._primary_bone_set = set(self._primary_bones)

        rx90 = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        self._u_rest = []
        self._inv_u_rest = []
        self._u_rest_local = []
        is_zup: list[bool] = [False] * len(bones)
        for i, b in enumerate(bones):
            bs = self._root_scale.get(self._root_of[i], 1.0)
            rw = b.rest_world.copy()
            rw[:3, :3] *= bs
            rw[:3, 3] *= bs
            rot = rw[:3, :3]
            norms = np.linalg.norm(rot, axis=0, keepdims=True)
            safe_norms = np.where((norms > 0.2) & (norms < 5.0), norms, 1.0)
            rw[:3, :3] = rot / safe_norms
            pos_zero = float(np.linalg.norm(rw[:3, 3])) < 1e-4
            rot_rx90 = float(np.linalg.norm(rw[:3, :3] - rx90)) < 0.05
            if (b.parent < 0 and rot_rx90) or (b.parent >= 0 and is_zup[b.parent] and pos_zero and rot_rx90):
                is_zup[i] = True
                ur = np.eye(4, dtype=np.float32)
                ur[:3, 3] = rw[:3, 3]
            else:
                ur = rw
            self._u_rest.append(ur)
            try:
                inv_u = np.linalg.inv(ur).astype(np.float32)
            except np.linalg.LinAlgError:
                inv_u = np.eye(4, dtype=np.float32)
            self._inv_u_rest.append(inv_u)
            if b.parent >= 0:
                self._u_rest_local.append((self._inv_u_rest[b.parent] @ ur).astype(np.float32))
            else:
                self._u_rest_local.append(ur.astype(np.float32))

        i_pelvis_chk = next((bi for bi in self._primary_bones if re.fullmatch(r"bip\d+\s+pelvis", bones[bi].name, re.I)), None)
        i_lthigh_chk = next((bi for bi in self._primary_bones if re.fullmatch(r"bip\d+\s+l\s+thigh", bones[bi].name, re.I)), None)
        if i_pelvis_chk is not None and i_lthigh_chk is not None and bones[i_lthigh_chk].parent == i_pelvis_chk:
            t_lt = self._u_rest_local[i_lthigh_chk][:3, 3]
            if abs(float(t_lt[1])) > abs(float(t_lt[2])) * 2.0 and abs(float(t_lt[1])) > 0.02:
                c_mat = np.array(
                    [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]]
                    if float(t_lt[1]) > 0.0
                    else [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]],
                    dtype=np.float32,
                )
                for bi in self._primary_bones:
                    if bones[bi].parent >= 0 and not is_zup[bi]:
                        ur = self._u_rest[bi].copy()
                        ur[:3, :3] = (ur[:3, :3] @ c_mat).astype(np.float32)
                        self._u_rest[bi] = ur
                        try:
                            self._inv_u_rest[bi] = np.linalg.inv(ur).astype(np.float32)
                        except np.linalg.LinAlgError:
                            self._inv_u_rest[bi] = np.eye(4, dtype=np.float32)
                for bi in self._primary_bones:
                    if bones[bi].parent >= 0:
                        self._u_rest_local[bi] = (self._inv_u_rest[bones[bi].parent] @ self._u_rest[bi]).astype(np.float32)

        p_ys = [float(self._u_rest[i][1, 3]) for i in self._primary_bones if re.search(r"bip\d+", bones[i].name, re.I)]
        skel_h = max(0.1, (max(p_ys) - min(p_ys)) if p_ys else 1.5)
        body_pts = [sm.positions for sm in self.scene.submeshes if not sm.is_weapon and len(sm.positions) > 100]
        if body_pts:
            bp = np.concatenate(body_pts, axis=0)
            mesh_h = float(bp[:, 1].max() - bp[:, 1].min())
        else:
            mesh_h = skel_h
        p_rs = self._root_scale.get(self._primary_root, 1.0)
        self._display_scale = (1.0 / p_rs) if (mesh_h / skel_h < 0.1 and p_rs > 5.0) else 1.0

        self._rest_mesh_poses = [(sm.positions.copy(), sm.normals.copy()) for sm in self.scene.submeshes]
        self._legacy_bind = []
        if not any(sm.skin_idx < 0 for sm in self.scene.submeshes):
            self._legacy_bind = [None] * len(self.scene.submeshes)
            return

        disp = self._display_scale
        disp_pts = np.asarray([self._u_rest[i][:3, 3] * disp for i in range(len(bones))], dtype=np.float32)
        seg_a_l: list[np.ndarray] = []
        seg_b_l: list[np.ndarray] = []
        seg_owner_l: list[int] = []
        anat_re = re.compile(
            r"^bip\d+\s+(?:pelvis|spine\d*|neck\d*|head|[lr]\s+(?:clavicle|upperarm|forearm|hand|thigh|calf|foot|toe0))$",
            re.I,
        )
        for i in self._primary_bones:
            b = bones[i]
            p = b.parent
            if p >= 0 and p in self._primary_bone_set and anat_re.match(b.name) and anat_re.match(bones[p].name):
                pa, pb = disp_pts[p], disp_pts[i]
                if float(np.linalg.norm(pb - pa)) > 1e-4:
                    seg_a_l.append(pa)
                    seg_b_l.append(pb)
                    seg_owner_l.append(p)

        def _find_pbone(pat: str) -> int | None:
            for bi in self._primary_bones:
                if re.search(pat, bones[bi].name, re.I) and not re.search(r"\.\d+$", bones[bi].name):
                    return bi
            return None

        i_head = _find_pbone(r"bip\d+\s+head$")
        i_lh = _find_pbone(r"bip\d+\s+l\s+hand$")
        i_rh = _find_pbone(r"bip\d+\s+r\s+hand$")
        i_lf = _find_pbone(r"bip\d+\s+l\s+foot$")
        i_lt = _find_pbone(r"bip\d+\s+l\s+toe0$")
        i_rf = _find_pbone(r"bip\d+\s+r\s+foot$")
        i_rt = _find_pbone(r"bip\d+\s+r\s+toe0$")
        i_prop = _find_pbone(r"bip\d+\s+prop1$")
        if i_head is not None:
            pa = disp_pts[i_head]
            seg_a_l.append(pa)
            seg_b_l.append(pa + np.array([0.0, 0.18 * disp, 0.0], dtype=np.float32))
            seg_owner_l.append(i_head)
        finger_re = re.compile(r"^bip\d+\s+[lr]\s+finger\d+$", re.I)
        for ih in (i_lh, i_rh):
            if ih is None:
                continue
            f_bones: list[int] = []
            for fb in self._primary_bones:
                if not finger_re.match(bones[fb].name):
                    continue
                cur = bones[fb].parent
                while cur >= 0:
                    if cur == ih:
                        f_bones.append(fb)
                        break
                    cur = bones[cur].parent
            if f_bones:
                finger_parents = {bones[fb].parent for fb in f_bones}
                for fb in f_bones:
                    p_fb = bones[fb].parent
                    pa, pb = disp_pts[p_fb], disp_pts[fb]
                    dvec = pb - pa
                    dn = float(np.linalg.norm(dvec))
                    if dn > 1e-4:
                        seg_a_l.append(pa)
                        seg_b_l.append(pb)
                        seg_owner_l.append(ih)
                        if fb not in finger_parents:
                            seg_a_l.append(pb)
                            seg_b_l.append(pb + (dvec / dn) * (0.025 * disp))
                            seg_owner_l.append(ih)
            elif bones[ih].parent >= 0:
                pa = disp_pts[ih]
                dvec = pa - disp_pts[bones[ih].parent]
                dn = float(np.linalg.norm(dvec))
                if dn > 1e-4:
                    seg_a_l.append(pa)
                    seg_b_l.append(pa + (dvec / dn) * (0.14 * disp))
                    seg_owner_l.append(ih)
        for ift, ito in ((i_lf, i_lt), (i_rf, i_rt)):
            if ito is not None and ift is not None:
                pa, pb = disp_pts[ift], disp_pts[ito]
                dvec = pb - pa
                dn = float(np.linalg.norm(dvec))
                if dn > 1e-4:
                    seg_a_l.append(pb)
                    seg_b_l.append(pb + (dvec / dn) * (0.06 * disp))
                    seg_owner_l.append(ito)
            elif ift is not None:
                pa = disp_pts[ift]
                seg_a_l.append(pa)
                seg_b_l.append(pa + np.array([0.0, 0.0, 0.12 * disp], dtype=np.float32))
                seg_owner_l.append(ift)

        if not seg_owner_l:
            for i in self._primary_bones:
                if bones[i].parent >= 0 and bones[i].parent in self._primary_bone_set:
                    seg_a_l.append(disp_pts[bones[i].parent])
                    seg_b_l.append(disp_pts[i])
                    seg_owner_l.append(bones[i].parent)
        if not seg_owner_l:
            seg_a_l.append(disp_pts[self._primary_root])
            seg_b_l.append(disp_pts[self._primary_root])
            seg_owner_l.append(self._primary_root)

        seg_owner = np.asarray(seg_owner_l, dtype=np.int32)
        seg_a = np.asarray(seg_a_l, dtype=np.float32)
        seg_b = np.asarray(seg_b_l, dtype=np.float32)
        seg_ab = seg_b - seg_a
        seg_len2 = np.maximum(np.sum(seg_ab * seg_ab, axis=1), 1e-8)

        uniq_owners, seg_to_u = np.unique(seg_owner, return_inverse=True)
        n_uniq = len(uniq_owners)
        parent_of = np.asarray([b.parent for b in bones], dtype=np.int32)
        inv_sigma2 = 1.0 / ((0.035 * max(disp, 1e-4)) ** 2)
        for sm, (rp, _rn) in zip(self.scene.submeshes, self._rest_mesh_poses):
            if sm.skin_idx >= 0 and sm.bind_positions is not None:
                self._legacy_bind.append(None)
                continue
            n_v = len(rp)
            bi0 = np.zeros(n_v, dtype=np.int32)
            bi1 = np.zeros(n_v, dtype=np.int32)
            w0 = np.ones(n_v, dtype=np.float32)
            w1 = np.zeros(n_v, dtype=np.float32)
            if sm.is_weapon:
                ctr = np.array(sm.center, dtype=np.float32)
                hand_cands = []
                for ih in (i_lh, i_rh):
                    if ih is not None:
                        hand_cands.append((float(np.linalg.norm(ctr - disp_pts[ih])), ih))
                hand_cands.sort(key=lambda x: x[0])
                if hand_cands and hand_cands[0][0] < 0.35 * disp:
                    owner = hand_cands[0][1]
                elif i_prop is not None:
                    owner = i_prop
                elif hand_cands:
                    owner = hand_cands[-1][1]
                else:
                    owner = int(uniq_owners[0])
                bi0.fill(owner)
                bi1.fill(owner)
            elif sm.bound_radius < 0.02 * disp and len(seg_owner) > 0:
                ctr = np.array(sm.center, dtype=np.float32)[None, :]
                ap = ctr[:, None, :] - seg_a[None, :, :]
                t_proj = np.clip(np.sum(ap * seg_ab[None, :, :], axis=2) / seg_len2[None, :], 0.0, 1.0)
                closest = seg_a[None, :, :] + t_proj[:, :, None] * seg_ab[None, :, :]
                d2 = np.sum((ctr[:, None, :] - closest) ** 2, axis=2)
                owner = int(seg_owner[int(np.argmin(d2[0]))])
                bi0.fill(owner)
                bi1.fill(owner)
            elif n_uniq >= 2:
                for start in range(0, n_v, 1024):
                    chunk = rp[start : start + 1024]
                    ap = chunk[:, None, :] - seg_a[None, :, :]
                    t_proj = np.clip(np.sum(ap * seg_ab[None, :, :], axis=2) / seg_len2[None, :], 0.0, 1.0)
                    closest = seg_a[None, :, :] + t_proj[:, :, None] * seg_ab[None, :, :]
                    d2_seg = np.sum((chunk[:, None, :] - closest) ** 2, axis=2)
                    d2 = np.full((len(chunk), n_uniq), np.inf, dtype=np.float32)
                    for s_idx, u_idx in enumerate(seg_to_u):
                        d2[:, u_idx] = np.minimum(d2[:, u_idx], d2_seg[:, s_idx])
                    top2 = np.argpartition(d2, 1, axis=1)[:, :2]
                    d2_0 = np.take_along_axis(d2, top2[:, :1], axis=1)[:, 0]
                    d2_1 = np.take_along_axis(d2, top2[:, 1:2], axis=1)[:, 0]
                    swap = d2_1 < d2_0
                    i0 = np.where(swap, top2[:, 1], top2[:, 0])
                    i1 = np.where(swap, top2[:, 0], top2[:, 1])
                    b0 = uniq_owners[i0]
                    b1 = uniq_owners[i1]
                    d_near = np.minimum(d2_0, d2_1)
                    d_far = np.maximum(d2_0, d2_1)
                    is_adj = (parent_of[b0] == b1) | (parent_of[b1] == b0)
                    e0 = np.ones_like(d_near)
                    e1 = np.exp(-np.maximum(0.0, d_far - d_near) * inv_sigma2)
                    e1 = np.where(is_adj & (d_far <= d_near * 3.0 + 0.0025 * (disp ** 2)), e1, 0.0)
                    s_w = e0 + e1
                    sl = slice(start, start + 1024)
                    bi0[sl] = b0
                    bi1[sl] = np.where(e1 > 0.0, b1, b0)
                    w0[sl] = e0 / s_w
                    w1[sl] = e1 / s_w
            elif n_uniq == 1:
                bi0.fill(int(uniq_owners[0]))
                bi1.fill(int(uniq_owners[0]))
            self._legacy_bind.append((bi0, bi1, w0, w1))

    def _apply_skinning_frame(self):
        if not self.scene or not self._rest_mesh_poses or not self.scene.bones:
            return
        worlds = self._eval_animated_bones()
        self._last_anim_worlds = worlds
        bones = self.scene.bones
        if not worlds or len(worlds) != len(bones):
            return
        n_b = len(bones)
        delta_mats = np.repeat(np.eye(4, dtype=np.float32)[None, :, :], n_b, axis=0)
        disp = self._display_scale
        for i in self._primary_bones:
            m = (worlds[i] @ self._inv_u_rest[i]).astype(np.float32)
            if disp != 1.0:
                m[:3, 3] *= disp
            delta_mats[i] = m

        skins = self.scene.skins or []
        if self._gpu_skin_active and self._bone_palette is not None and not self.wireframe:
            pal = self._bone_palette
            if len(pal) >= n_b:
                pal[:n_b] = delta_mats.transpose(0, 2, 1)
                ident4 = np.eye(4, dtype=np.float32)
                for sidx, off in enumerate(self._skin_palette_offsets):
                    if sidx >= len(skins):
                        break
                    jbones, rest_skin_mats = skins[sidx]
                    n_j = len(jbones)
                    if n_j == 0 or off + n_j > len(pal):
                        continue
                    jb_arr = np.asarray(jbones, dtype=np.int32)
                    valid = (jb_arr >= 0) & (jb_arr < n_b)
                    d_sel = np.where(valid[:, None, None], delta_mats[np.clip(jb_arr, 0, max(0, n_b - 1))], ident4)
                    mskin = np.matmul(d_sel, rest_skin_mats)
                    pal[off : off + n_j] = mskin.transpose(0, 2, 1)
                if 0 <= self._skin_ident_row < len(pal):
                    pal[self._skin_ident_row] = ident4
                self._bone_tex_dirty = True
                return

        skin_mats_cache: dict[int, np.ndarray] = {}
        for sm, (rp, rn), lbind in zip(self.scene.submeshes, self._rest_mesh_poses, self._legacy_bind):
            if sm.skin_idx >= 0 and sm.bind_positions is not None and sm.skin_joints is not None and sm.skin_weights is not None and sm.skin_idx < len(skins):
                bp = sm.bind_positions
                bn = sm.bind_normals if sm.bind_normals is not None else rn
                if len(bp) == 0:
                    continue
                mskin = skin_mats_cache.get(sm.skin_idx)
                if mskin is None:
                    jbones, rest_skin_mats = skins[sm.skin_idx]
                    mskin = np.empty_like(rest_skin_mats)
                    for k, bi in enumerate(jbones):
                        if 0 <= bi < n_b:
                            mskin[k] = delta_mats[bi] @ rest_skin_mats[k]
                        else:
                            mskin[k] = rest_skin_mats[k]
                    skin_mats_cache[sm.skin_idx] = mskin
                J = sm.skin_joints
                W = sm.skin_weights
                pos_out = np.zeros_like(bp, dtype=np.float32)
                nrm_out = np.zeros_like(bn, dtype=np.float32)
                for c in range(J.shape[1]):
                    wc = W[:, c : c + 1]
                    if not np.any(wc > 1e-6):
                        continue
                    M = mskin[J[:, c]]
                    pos_out += wc * (np.einsum("vij,vj->vi", M[:, :3, :3], bp) + M[:, :3, 3])
                    nrm_out += wc * np.einsum("vij,vj->vi", M[:, :3, :3], bn)
                n_len = np.linalg.norm(nrm_out, axis=1, keepdims=True)
                sm.positions = np.ascontiguousarray(pos_out, dtype=np.float32)
                sm.normals = np.ascontiguousarray(np.divide(nrm_out, np.maximum(n_len, 1e-6)), dtype=np.float32)
            elif lbind is not None:
                if len(rp) == 0:
                    continue
                bi0, bi1, w0, w1 = lbind
                M0 = delta_mats[bi0]
                M1 = delta_mats[bi1]
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
        if not self._u_rest:
            self._prepare_bone_binding()
        t = self.anim_time % max(clip.duration, 0.001)
        ps = self._anim_pos_scale
        ox, oz = self._anim_bip_xz_offset
        worlds: list[np.ndarray] = []
        ident = np.eye(4, dtype=np.float32)
        for i, b in enumerate(bones):
            if b.parent < 0:
                worlds.append(self._u_rest[i] if i < len(self._u_rest) else ident)
                continue
            pw = worlds[b.parent]
            if self._root_of and self._root_of[i] != self._primary_root:
                loc = self._u_rest_local[i] if i < len(self._u_rest_local) else ident
                worlds.append(pw @ loc)
                continue
            keys = self._bone_keys[i] if i < len(self._bone_keys) else (b.name,)
            pc = self._lookup_curve(clip.pos_curves, keys)
            rc = self._lookup_curve(clip.rot_curves, keys)
            sc_c = self._lookup_curve(clip.scale_curves, keys)
            if pc is None and rc is None and sc_c is None:
                loc = self._u_rest_local[i] if i < len(self._u_rest_local) else ident
                worlds.append(pw @ loc)
                continue
            if pc is not None:
                p = _eval_vec3_curve(*pc, t) * ps
                bl = b.name.lower()
                if bl in ("bone_root", "root"):
                    y_val = float(p[1]) - (self._anim_y_offset if i == self._anim_y_offset_bone else 0.0)
                    pos = np.array([0.0, y_val, 0.0], dtype=np.float32)
                elif i == self._anim_bip_root or bool(re.fullmatch(r"bip\d+", bl)):
                    y_val = float(p[1]) - (self._anim_y_offset if i == self._anim_y_offset_bone else 0.0)
                    if self._anim_bip_lock_xz:
                        pos = np.array([0.0, y_val, 0.0], dtype=np.float32)
                    else:
                        pos = np.array([float(-p[0]) - ox, y_val, float(p[2]) - oz], dtype=np.float32)
                else:
                    pos = np.array([-p[0], p[1], p[2]], dtype=np.float32)
            else:
                pos = self._u_rest_local[i][:3, 3].copy() if i < len(self._u_rest_local) else np.zeros(3, dtype=np.float32)
            scl = _eval_vec3_curve(*sc_c, t) if sc_c is not None else np.ones(3, dtype=np.float32)
            if rc is not None:
                q = _eval_quat_curve(*rc, t)
                rot = np.array([q[0], -q[1], -q[2], q[3]], dtype=np.float32)
                loc = _trs_to_mat4(pos, rot, scl)
            else:
                loc = self._u_rest_local[i].copy() if i < len(self._u_rest_local) else np.eye(4, dtype=np.float32)
                loc[:3, 3] = pos
                if sc_c is not None:
                    loc[:3, 0] *= scl[0]
                    loc[:3, 1] *= scl[1]
                    loc[:3, 2] *= scl[2]
            worlds.append(pw @ loc)
        return worlds

    def _draw_skeleton(self):
        bones = self.scene.bones if self.scene and self.scene.bones else []
        if not bones:
            return
        worlds = self._last_anim_worlds if (len(self._last_anim_worlds) == len(bones)) else self._eval_animated_bones()
        if not worlds:
            return
        disp = self._display_scale
        p_set = self._primary_bone_set or set(range(len(bones)))
        p_list = self._primary_bones or list(range(len(bones)))
        skip_toks = (
            "collider", "camerabasepoint", "hairshadow", "_lod",
            "branch_04_center", "normal_03_origin", "weaponstartpoint",
        )
        gl.glDisable(gl.GL_LIGHTING)
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glLineWidth(2.0)
        gl.glBegin(gl.GL_LINES)
        gl.glColor4f(0.20, 0.92, 1.0, 0.95)
        for idx in p_list:
            b = bones[idx]
            nl = b.name.lower()
            if (
                b.parent >= 0
                and b.parent in p_set
                and not re.search(r"\.\d+$", b.name)
                and not any(t in nl for t in skip_toks)
            ):
                p0 = worlds[b.parent][:3, 3] * disp
                p1 = worlds[idx][:3, 3] * disp
                gl.glVertex3f(float(p0[0]), float(p0[1]), float(p0[2]))
                gl.glVertex3f(float(p1[0]), float(p1[1]), float(p1[2]))
        gl.glEnd()
        gl.glPointSize(4.5)
        gl.glBegin(gl.GL_POINTS)
        gl.glColor4f(1.0, 0.78, 0.20, 1.0)
        for idx in p_list:
            nl = bones[idx].name.lower()
            if re.search(r"\.\d+$", bones[idx].name) or any(t in nl for t in skip_toks):
                continue
            p = worlds[idx][:3, 3] * disp
            gl.glVertex3f(float(p[0]), float(p[1]), float(p[2]))
        gl.glEnd()
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_LIGHTING)

    def _camera_basis(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        yr = math.radians(self.yaw)
        pr = math.radians(self.pitch)
        off = np.array(
            [
                self.distance * math.cos(pr) * math.sin(yr),
                self.distance * math.sin(pr),
                self.distance * math.cos(pr) * math.cos(yr),
            ],
            dtype=np.float32,
        )
        cam_pos = self.target + off
        fwd = -off
        fn = float(np.linalg.norm(fwd))
        fwd = (fwd / fn) if fn > 1e-6 else np.array([0.0, 0.0, -1.0], dtype=np.float32)
        world_up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        right = np.cross(fwd, world_up)
        rn = float(np.linalg.norm(right))
        right = (right / rn) if rn > 1e-6 else np.array([math.cos(yr), 0.0, -math.sin(yr)], dtype=np.float32)
        up = np.cross(right, fwd)
        return cam_pos.astype(np.float32), fwd.astype(np.float32), right.astype(np.float32), up.astype(np.float32)

    @staticmethod
    def _is_move_key(key: int) -> bool:
        return key in (
            Qt.Key_W, Qt.Key_S, Qt.Key_A, Qt.Key_D,
            Qt.Key_Up, Qt.Key_Down, Qt.Key_Left, Qt.Key_Right,
            Qt.Key_E, Qt.Key_Space, Qt.Key_Q, Qt.Key_C,
        )

    def _on_nav_tick(self):
        now = time.perf_counter()
        dt = min(0.05, max(0.001, now - self._nav_last_t)) if self._nav_last_t > 0.0 else 0.016
        self._nav_last_t = now
        if not self._keys_held:
            self._nav_timer.stop()
            return
        mf = 0.0
        mr = 0.0
        mu = 0.0
        if Qt.Key_W in self._keys_held or Qt.Key_Up in self._keys_held:
            mf += 1.0
        if Qt.Key_S in self._keys_held or Qt.Key_Down in self._keys_held:
            mf -= 1.0
        if Qt.Key_D in self._keys_held or Qt.Key_Right in self._keys_held:
            mr += 1.0
        if Qt.Key_A in self._keys_held or Qt.Key_Left in self._keys_held:
            mr -= 1.0
        if Qt.Key_E in self._keys_held or Qt.Key_Space in self._keys_held:
            mu += 1.0
        if Qt.Key_Q in self._keys_held or Qt.Key_C in self._keys_held:
            mu -= 1.0
        if mf == 0.0 and mr == 0.0 and mu == 0.0:
            self._nav_timer.stop()
            return

        _cam_pos, fwd, right, _up = self._camera_basis()
        world_up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        move = fwd * mf + right * mr + world_up * mu
        mn = float(np.linalg.norm(move))
        if mn > 1.0:
            move /= mn
        rad = self.scene.radius if self.scene else 3.0
        base_speed = max(0.4, min(rad * 0.45, 20.0)) * self._fly_speed_mul
        if Qt.Key_Shift in self._keys_held:
            base_speed *= 3.5
        elif Qt.Key_Control in self._keys_held:
            base_speed *= 0.25
        self.target = self.target + move * float(base_speed * dt)
        self.update()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_F and not event.isAutoRepeat():
            self.fly_mode = not self.fly_mode
            if self._fly_mode_cb is not None:
                self._fly_mode_cb(self.fly_mode)
            self.update()
            event.accept()
            return
        if key in (Qt.Key_R, Qt.Key_Home) and not event.isAutoRepeat():
            self.reset_camera()
            event.accept()
            return
        if key in (Qt.Key_Shift, Qt.Key_Control) or self._is_move_key(key):
            self._keys_held.add(key)
            if self._is_move_key(key) and not self._nav_timer.isActive():
                self._nav_last_t = time.perf_counter()
                self._nav_timer.start()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        key = event.key()
        self._keys_held.discard(key)
        if not any(self._is_move_key(k) for k in self._keys_held):
            self._nav_timer.stop()
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self._keys_held.clear()
        self._nav_timer.stop()
        super().focusOutEvent(event)

    def mousePressEvent(self, event):
        self.setFocus()
        self._last_pos = event.position()
        if event.button() == Qt.MiddleButton:
            self._drag_mode = "pan"
        elif self.fly_mode:
            self._drag_mode = "look" if event.button() in (Qt.LeftButton, Qt.RightButton) else "pan"
        elif event.modifiers() & Qt.ShiftModifier:
            self._drag_mode = "pan"
        elif event.button() == Qt.LeftButton:
            self._drag_mode = "orbit"
        else:
            self._drag_mode = "pan"

    def mouseMoveEvent(self, event):
        if self._last_pos is None:
            return
        dx = event.position().x() - self._last_pos.x()
        dy = event.position().y() - self._last_pos.y()
        self._last_pos = event.position()
        if self._drag_mode == "look":
            cam_pos, _fwd, _right, _up = self._camera_basis()
            self.yaw -= dx * 0.35
            self.pitch = max(-89.0, min(89.0, self.pitch + dy * 0.30))
            yr = math.radians(self.yaw)
            pr = math.radians(self.pitch)
            off = np.array(
                [
                    self.distance * math.cos(pr) * math.sin(yr),
                    self.distance * math.sin(pr),
                    self.distance * math.cos(pr) * math.cos(yr),
                ],
                dtype=np.float32,
            )
            self.target = cam_pos - off
        elif self._drag_mode == "orbit":
            self.yaw -= dx * 0.45
            self.pitch = max(-89.0, min(89.0, self.pitch + dy * 0.45))
        else:
            yr = math.radians(self.yaw)
            right = np.array([math.cos(yr), 0.0, -math.sin(yr)], dtype=np.float32)
            up = np.array([0.0, 1.0, 0.0], dtype=np.float32)
            scale = max(0.005, self.distance) * 0.0018
            self.target -= right * (dx * scale)
            self.target += up * (dy * scale)
        self.update()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            return
        if self.fly_mode:
            if event.modifiers() & Qt.ShiftModifier:
                self._fly_speed_mul = max(0.1, min(10.0, self._fly_speed_mul * (1.15 if delta > 0 else 0.87)))
            else:
                _cam_pos, fwd, _right, _up = self._camera_basis()
                rad = self.scene.radius if self.scene else 3.0
                step = max(0.15, min(rad * 0.12, 5.0)) * self._fly_speed_mul * (delta / 120.0)
                self.target = self.target + fwd * float(step)
        else:
            factor = 0.88 if delta > 0 else 1.14
            min_dist = max(0.002, (self.scene.radius * 0.01) if self.scene else 0.05)
            self.distance = max(min_dist, self.distance * factor)
        self.update()


class NativeViewerWidget(QWidget):
    def __init__(self, choose_output_cb=None, parent=None):
        super().__init__(parent)
        self._choose_output_cb = choose_output_cb
        self._workers: list[QThread] = []
        self._load_generation: int = 0
        self._last_model_path: Path | None = None
        self._anim_clip: UnityAnimClip | None = None
        self._anim_playing = False
        self._anim_last_wall: float = 0.0
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._on_anim_tick)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        bar = QHBoxLayout()
        self.status_lbl = QLabel("Native 3D GLB Viewer — Select a Character or Map")
        self.status_lbl.setStyleSheet("color: #94a3b8; font-weight: 600;")
        # Long status text must never force the window wider than the screen.
        self.status_lbl.setMinimumWidth(0)
        self.status_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
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
        self.btn_unload = QPushButton("Unload")
        self.btn_unload.setEnabled(False)
        self.btn_unload.setToolTip("Unload current 3D scene to free RAM and GPU VRAM")
        self.btn_unload.clicked.connect(lambda: self.unload_scene("manual"))
        self.btn_wire = QPushButton("Wireframe")
        self.btn_wire.setCheckable(True)
        self.btn_wire.toggled.connect(self._toggle_wire)
        self.btn_tex = QPushButton("Textures")
        self.btn_tex.setCheckable(True)
        self.btn_tex.setChecked(True)
        self.btn_tex.toggled.connect(self._toggle_tex)
        self.btn_skybox = QPushButton("Skybox")
        self.btn_skybox.setCheckable(True)
        self.btn_skybox.setChecked(False)
        self.btn_skybox.setToolTip("Render calibrated ZZZ atmospheric skybox (auto-enabled on maps)")
        self.btn_skybox.toggled.connect(self._toggle_skybox)
        self.btn_fly = QPushButton("Fly Cam")
        self.btn_fly.setCheckable(True)
        self.btn_fly.setChecked(False)
        self.btn_fly.setToolTip("Toggle WASD free-look fly camera (hotkey: F, auto-enabled on maps)")
        self.btn_fly.toggled.connect(self._toggle_fly_mode)
        self.btn_toon = QPushButton("Toon Shader")
        self.btn_toon.setCheckable(True)
        self.btn_toon.setChecked(True)
        self.btn_toon.toggled.connect(self._toggle_toon)
        self.toon_preset_combo = QComboBox()
        self.toon_preset_combo.setToolTip("Toon lighting preset calibrated from authentic ZZZ in-game WeatherConfig & LUT textures")
        self.toon_preset_combo.addItems(list(_load_toon_presets().keys()))
        self.toon_preset_combo.currentTextChanged.connect(self._on_toon_preset_changed)
        self.btn_cull = QPushButton("Culling")
        self.btn_cull.setCheckable(True)
        self.btn_cull.setChecked(True)
        self.btn_cull.setToolTip("Cull sub-pixel distant objects (<1-2 px) for faster map navigation")
        self.btn_cull.toggled.connect(self._toggle_cull)
        self.btn_lod = QPushButton("Zoom LOD")
        self.btn_lod.setCheckable(True)
        self.btn_lod.setChecked(False)
        self.btn_lod.setToolTip("Optional low-VRAM mipmap reduction when zoomed far out in Orbit mode (OFF = full native texture resolution with GPU mipmaps)")
        self.btn_lod.toggled.connect(self._toggle_zoom_lod)

        for b in (btn_open, btn_refresh, btn_reset, self.btn_unload):
            bar.addWidget(b)
        root.addLayout(bar)

        view_bar = QHBoxLayout()
        for b in (self.btn_wire, self.btn_tex, self.btn_skybox, self.btn_fly, self.btn_toon, self.toon_preset_combo, self.btn_cull, self.btn_lod):
            view_bar.addWidget(b)
        view_bar.addStretch(1)
        self.toon_preset_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.toon_preset_combo.setMinimumContentsLength(14)
        root.addLayout(view_bar)

        anim_bar = QHBoxLayout()
        anim_bar.addWidget(QLabel("Animation:"))
        self.anim_combo = QComboBox()
        self.anim_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.anim_combo.setMinimumContentsLength(18)
        self.anim_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
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
        self.btn_skel.setChecked(False)
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
        self.viewport._stats_cb = self._on_render_stats
        self.viewport._fly_mode_cb = self._on_fly_mode_hotkey
        splitter.addWidget(left)
        splitter.addWidget(self.viewport)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

    def _toggle_wire(self, checked: bool):
        self.viewport.wireframe = checked
        if self.viewport.anim_clip is not None:
            self.viewport._apply_skinning_frame()
        self.viewport.update()

    def _toggle_tex(self, checked: bool):
        self.viewport.show_textures = checked
        self.viewport.update()

    def _toggle_skybox(self, checked: bool):
        self.viewport.show_skybox = checked
        self.viewport.update()

    def _toggle_fly_mode(self, checked: bool):
        self.viewport.fly_mode = checked
        self.viewport.setFocus()
        self.viewport.update()

    def _on_fly_mode_hotkey(self, active: bool):
        self.btn_fly.blockSignals(True)
        self.btn_fly.setChecked(active)
        self.btn_fly.blockSignals(False)

    @staticmethod
    def _lookup_oz_kind(path: Path) -> str | None:
        from .indexer import load_oz_character_index, load_oz_entities_index, load_oz_map_index

        stem = path.stem
        stem_no_full = stem[:-5] if stem.lower().endswith("_full") else stem
        parent = path.parent.name
        par2 = path.parent.parent.name if path.parent.parent != path.parent else ""
        cands = tuple(c.lower() for c in (stem, stem_no_full, parent, par2) if c)

        oz_maps = load_oz_map_index()
        if oz_maps:
            map_ci = {k.lower() for k in oz_maps}
            if any(c in map_ci for c in cands):
                return "map"
        oz_chars = load_oz_character_index()
        if oz_chars:
            char_ci = {k.lower() for k in oz_chars}
            if any(c in char_ci for c in cands):
                return "char"
        oz_ents = load_oz_entities_index()
        if oz_ents:
            ent_ci = {k.lower() for cat_m in oz_ents.values() if isinstance(cat_m, dict) for k in cat_m}
            if any(c in ent_ci for c in cands):
                return "char"
        return None

    @staticmethod
    def _is_map_scene(scene: GLScene) -> bool:
        oz_kind = NativeViewerWidget._lookup_oz_kind(scene.source_path)
        if oz_kind == "map":
            return True
        if oz_kind == "char":
            return False
        nm = scene.source_path.stem.lower()
        pr = str(scene.source_path.parent).lower()
        if nm.startswith("avatar_") or "avatar_" in pr:
            return False
        if (nm.startswith("bangboo_") or "bangboo_" in pr) and not any(
            t in nm or t in pr for t in ("amusement", "autochess", "knockout", "megarobot", "platformer", "pinball")
        ) and len(scene.submeshes) <= 40:
            return False
        has_char_rig = bool(
            scene.bones
            and len(scene.submeshes) <= 120
            and (
                any(b.name.lower() in ("bip001", "bone_root") for b in scene.bones[:120])
                or any(len(jb) >= 10 for jb, _ in (scene.skins or []))
            )
        )
        if has_char_rig:
            return False
        if len(scene.submeshes) > 40:
            return True
        return any(
            t in nm or t in pr
            for t in (
                "stage", "map", "env_", "site", "mine", "ring", "suburbs",
                "skyscraper", "roscaelifer", "whitfield", "maincity", "street",
                "fortune", "reverbarena", "handoffice", "beach", "amusementpark",
                "hiddenreef", "airshipport", "belltowersquare",
            )
        )

    def _toggle_toon(self, checked: bool):
        self.toon_preset_combo.setEnabled(checked)
        self.viewport.toon_enabled = checked
        self.viewport.update()

    def _on_toon_preset_changed(self, name: str):
        self.viewport.set_toon_preset(name)

    def _toggle_cull(self, checked: bool):
        self.viewport.culling_enabled = checked
        self.viewport.update()

    def _toggle_zoom_lod(self, checked: bool):
        self.viewport.zoom_lod_enabled = checked
        self.viewport.update()

    def _on_render_stats(self, st: dict):
        sc = self.viewport.scene
        if not sc or self._anim_playing:
            return
        lod_div = 1 << max(0, int(st.get("tex_lod", 0)))
        gpu = st.get("gpu") or self.viewport._gpu_renderer
        gpu_tag = f" • {gpu}" if gpu else ""
        self.status_lbl.setText(
            f"{sc.source_path.name} • {st['drawn']:,}/{st['total']:,} meshes • "
            f"{sc.triangle_count:,} tris • tex 1/{lod_div}x{gpu_tag} • "
            f"WASD: Move • Q/E: Up/Down • Shift: Fast • F: Fly/Orbit • Drag: Look/Orbit"
        )

    def _open_file_dialog(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Open 3D Model", str(cfg.FINAL_ROOT), "3D Models (*.glb *.gltf *.obj)"
        )
        if f:
            self.load_model(Path(f))

    def refresh_catalog(self):
        self.tree.clear()
        roots = cfg.get_export_search_roots()
        from . import catalog as _cat
        from .indexer import load_oz_character_index, load_oz_entities_index, load_oz_map_index

        map_ci = {k.lower() for k in load_oz_map_index()}
        oz_chars = load_oz_character_index()
        bangboo_ci = {k.lower() for k in _cat.classify_bangboo(oz_chars)}
        char_ci = {k.lower() for k in oz_chars} - bangboo_ci
        ents = load_oz_entities_index()
        monster_ci = {k.lower() for k in (ents.get("monsters") or {})}
        npc_ci = {k.lower() for k in (ents.get("npcs") or {})}
        weapon_ci = {
            k.lower()
            for ck in ("weapons", "props")
            for k in (ents.get(ck) or {})
        }
        map_tokens = (
            "stage", "map", "env_", "site", "mine", "ring", "suburbs",
            "skyscraper", "roscaelifer", "whitfield", "maincity", "fortune",
            "reverbarena", "handoffice", "beach", "amusementpark", "hiddenreef",
            "airshipport", "belltowersquare", "bangboo",
        )
        seen: set[Path] = set()
        chars_node = QTreeWidgetItem(self.tree, ["Characters"])
        bangboo_node = QTreeWidgetItem(self.tree, ["Bangboo"])
        monsters_node = QTreeWidgetItem(self.tree, ["Monsters & Bosses"])
        npcs_node = QTreeWidgetItem(self.tree, ["NPCs"])
        weapons_node = QTreeWidgetItem(self.tree, ["W-Engines & Props"])
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
                nlow = name.lower()
                n_nofull = nlow[:-5] if nlow.endswith("_full") else nlow
                plow = glb.parent.name.lower()
                par_full = str(glb.parent).lower()
                cands = (nlow, plow)
                is_bb_minigame = any(t in nlow or t in par_full for t in ("amusement", "autochess", "knockout", "megarobot", "platformer", "pinball"))
                if any(c in map_ci for c in (nlow, n_nofull, plow)):
                    parent = maps_node
                elif any(c in bangboo_ci for c in cands) or (name.startswith(("Bangboo_", "Avatar_Bangboo_")) and not is_bb_minigame):
                    parent = bangboo_node
                elif any(c in monster_ci for c in cands) or name.startswith("Monster_"):
                    parent = monsters_node
                elif any(c in npc_ci for c in cands) or name.startswith("NPC_"):
                    parent = npcs_node
                elif any(c in weapon_ci for c in cands) or name.startswith(("Weapon_", "Device_", "Item_", "Destruction_")):
                    parent = weapons_node
                elif any(c in char_ci for c in cands) or name.startswith("Avatar_") or glb.parent.name.startswith("Avatar_"):
                    parent = chars_node
                elif "maps_combined" in par_full or any(t in nlow or t in par_full for t in map_tokens):
                    parent = maps_node
                else:
                    parent = other_node
                it = QTreeWidgetItem(parent, [name])
                it.setData(0, Qt.UserRole, str(glb))
                it.setToolTip(0, str(glb))

        for node in (chars_node, bangboo_node, monsters_node, npcs_node, weapons_node, maps_node, other_node):
            node.setExpanded(True)
            node.setText(0, f"{node.text(0)} ({node.childCount()})")
            if node not in (chars_node, maps_node) and node.childCount() == 0:
                node.setHidden(True)
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
        if self.viewport.scene is not None:
            self._stop_animation()
            self._anim_clip = None
            if self.viewport.clear_scene():
                _release_os_memory()
        self.btn_unload.setEnabled(False)
        self._load_generation += 1
        gen = self._load_generation
        self.status_lbl.setText(f"Loading {path.name}...")
        w = _SceneLoadWorker(path)
        w.loaded.connect(lambda sc, g=gen: self._on_scene_loaded(sc, g))
        w.failed.connect(lambda err, g=gen, p=path: self._on_scene_failed(p, err, g))
        w.finished.connect(lambda w_ref=w: (self._workers.remove(w_ref) if w_ref in self._workers else None, w_ref.deleteLater()))
        self._workers.append(w)
        w.start()

    def _on_scene_failed(self, path: Path, err: str, gen: int | None = None):
        if gen is not None and gen != self._load_generation:
            return
        self.status_lbl.setText(f"Failed to load {path.name}: {err}")

    def has_loaded_scene(self) -> bool:
        return bool(self.viewport.scene is not None or any(w.isRunning() for w in self._workers))

    def unload_scene(self, reason: str = "") -> bool:
        """Unload active 3D scene and cancel any pending load so RAM/VRAM is freed."""
        self._load_generation += 1
        worker_running = any(w.isRunning() for w in self._workers)
        self._stop_animation()
        self._anim_clip = None
        self.anim_combo.blockSignals(True)
        self.anim_combo.clear()
        self.anim_combo.addItem("(None — Rest Pose)", None)
        self.anim_combo.blockSignals(False)
        self.btn_anim_play.setEnabled(False)
        self.anim_slider.setEnabled(False)
        self.anim_slider.setValue(0)
        self.anim_time_lbl.setText("0.00 / 0.00s")
        self.btn_weapons.setEnabled(True)
        self.btn_unload.setEnabled(False)
        had = self.viewport.clear_scene()
        if had or worker_running:
            _release_os_memory()
        if had:
            name = self._last_model_path.name if self._last_model_path is not None else "3D scene"
            self.status_lbl.setText(f"Unloaded {name} (freed RAM/VRAM) — select a model to view")
        return had

    def shutdown(self):
        """Release all viewer resources and wait briefly for background loader threads."""
        self.unload_scene("closing")
        for w in list(self._workers):
            if w.isRunning():
                w.wait(3000)

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
        for cand_dir in (model_path.parent / "AnimationClip", model_path.parent / "Animations"):
            if cand_dir.is_dir():
                anim_files.extend(sorted(cand_dir.rglob("*.anim")))
        if model_path.parent.is_dir():
            anim_files.extend(sorted(model_path.parent.glob("*.anim")))
        seen_paths: set[Path] = set()
        seen_stems: set[str] = set()
        for af in anim_files:
            rp = af.resolve()
            sl = af.stem.lower()
            if rp in seen_paths or sl in seen_stems:
                continue
            seen_paths.add(rp)
            seen_stems.add(sl)
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
        self._anim_last_wall = time.perf_counter()
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
            self._anim_last_wall = time.perf_counter()
            self.btn_anim_play.setText("Pause")
            self._anim_timer.start()

    def _on_anim_tick(self):
        if not self._anim_clip or not self._anim_playing:
            return
        now = time.perf_counter()
        dt = min(0.1, max(0.001, now - self._anim_last_wall)) if self._anim_last_wall > 0.0 else 0.016
        self._anim_last_wall = now
        t = (self.viewport.anim_time + dt) % max(self._anim_clip.duration, 0.001)
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

    def _on_scene_loaded(self, scene: GLScene, gen: int | None = None):
        if gen is not None and gen != self._load_generation:
            del scene
            _release_os_memory()
            return
        self._last_model_path = scene.source_path
        self._stop_animation()
        self._anim_clip = None
        self.btn_anim_play.setEnabled(False)
        self.anim_slider.setEnabled(False)
        self.anim_slider.setValue(0)
        self.anim_time_lbl.setText("0.00 / 0.00s")
        is_map = self._is_map_scene(scene)
        self.viewport.show_skybox = is_map
        self.viewport.fly_mode = is_map
        self.btn_skybox.blockSignals(True)
        self.btn_skybox.setChecked(is_map)
        self.btn_skybox.blockSignals(False)
        self.btn_fly.blockSignals(True)
        self.btn_fly.setChecked(is_map)
        self.btn_fly.blockSignals(False)
        self.viewport.set_scene(scene)
        self.btn_unload.setEnabled(True)
        self.viewport.setFocus()
        has_wpn = any(sm.is_weapon for sm in scene.submeshes)
        has_body = any(not sm.is_weapon for sm in scene.submeshes)
        self.btn_weapons.setEnabled(has_wpn)
        self.btn_weapons.setChecked(not (has_wpn and has_body))
        self._populate_anim_combo(scene.source_path)
        gpu_tag = f" • {self.viewport._gpu_renderer}" if self.viewport._gpu_renderer else ""
        self.status_lbl.setText(
            f"{scene.source_path.name} • {len(scene.submeshes)} meshes • "
            f"{scene.triangle_count:,} tris • {scene.vertex_count:,} verts{gpu_tag} • "
            f"WASD: Move • Q/E: Up/Down • Shift: Fast • F: Fly/Orbit • Drag: Look/Orbit"
        )
