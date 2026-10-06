"""Write metric results: PLY point clouds / meshes, a viewer-sized GLB and camera poses."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .align import Sim3
from .colmap_io import Model


def clean_sparse_mask(pts, k: int = 8, sigma: float = 3.0) -> np.ndarray:
    """Keep well-observed points (track >= 3, low reprojection error) and drop isolated strays (kNN distance outliers)."""
    from scipy.spatial import cKDTree
    ok = (pts.track_len >= 3) & (pts.error <= 2.0)
    if ok.sum() > 3 * k:
        idx = np.flatnonzero(ok)
        d = cKDTree(pts.xyz[idx]).query(pts.xyz[idx], k=k + 1)[0][:, -1]
        thr = d.mean() + sigma * d.std()
        ok[idx[d > thr]] = False
    return ok


def _decimate_with_colors(mesh, max_faces: int):
    """Quadric decimation, then re-colour vertices from the nearest original vertex."""
    if len(mesh.faces) <= max_faces:
        return mesh
    try:
        import trimesh
        from scipy.spatial import cKDTree
        small = mesh.simplify_quadric_decimation(face_count=max_faces)
        colors = getattr(mesh.visual, "vertex_colors", None)
        if colors is not None and len(colors) == len(mesh.vertices):
            _, idx = cKDTree(mesh.vertices).query(small.vertices)
            small.visual = trimesh.visual.ColorVisuals(small, vertex_colors=np.asarray(colors)[idx])
        return small
    except Exception:
        return mesh  # decimation is a nicety; never fail the export over it


def export_results(out_dir: Path, model: Model, sim: Sim3, fused_ply: Path | None, mesh_ply: Path | None,
                   glb_max_faces: int = 300_000) -> dict[str, Path]:
    import trimesh

    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}

    # sparse cloud (always available; quick preview on the tablet)
    pts = model.points
    keep = clean_sparse_mask(pts) if len(pts.ids) else np.zeros(0, bool)
    sparse_xyz = sim.apply(pts.xyz[keep]) if len(pts.ids) else np.zeros((0, 3))
    sparse_col = np.c_[pts.rgb[keep], np.full(int(keep.sum()), 255, np.uint8)] if len(pts.ids) else None
    if len(pts.ids):
        trimesh.PointCloud(sparse_xyz, colors=sparse_col).export(out_dir / "sparse.ply")
        outputs["sparse_ply"] = out_dir / "sparse.ply"

    dense_cloud = None
    if fused_ply and fused_ply.exists():
        dense_cloud = trimesh.load(fused_ply, process=False)
        dense_cloud.vertices = sim.apply(np.asarray(dense_cloud.vertices))
        dense_cloud.export(out_dir / "pointcloud.ply")
        outputs["pointcloud_ply"] = out_dir / "pointcloud.ply"

    mesh = None
    if mesh_ply and mesh_ply.exists():
        mesh = trimesh.load(mesh_ply, process=False)
        mesh.vertices = sim.apply(np.asarray(mesh.vertices))
        mesh.export(out_dir / "mesh.ply")
        outputs["mesh_ply"] = out_dir / "mesh.ply"

    # viewer-sized GLB: mesh if we have one, else the densest point cloud we have
    try:
        if mesh is not None:
            scene_obj = _decimate_with_colors(mesh, glb_max_faces)
        elif dense_cloud is not None:
            scene_obj = _thin_cloud(dense_cloud, 2_000_000)
        elif len(pts.ids):
            scene_obj = trimesh.PointCloud(sparse_xyz, colors=sparse_col)
        else:
            scene_obj = None
        if scene_obj is not None:
            scene_obj.export(out_dir / "model.glb")
            outputs["model_glb"] = out_dir / "model.glb"
    except Exception as e:  # GLB is for the viewer only
        (out_dir / "glb_error.txt").write_text(str(e))

    # camera poses in the output frame (for drawing the capture path / frustums in the app)
    from .poses import cv_w2c_to_c2w_gl
    cams = []
    for im in sorted(model.images.values(), key=lambda i: i.name):
        R_gl, C = cv_w2c_to_c2w_gl(im.R(), im.tvec)
        cams.append({"frame": im.name, "t": sim.apply(C.reshape(1, 3))[0].tolist(), "R_c2w_gl": (sim.R @ R_gl).tolist()})
    (out_dir / "cameras.json").write_text(json.dumps({"convention": "OpenGL camera-to-world, metres", "cameras": cams}))
    outputs["cameras_json"] = out_dir / "cameras.json"
    return outputs


def _thin_cloud(pc, max_points: int):
    import trimesh
    v = np.asarray(pc.vertices)
    if len(v) <= max_points:
        return pc
    idx = np.random.default_rng(0).choice(len(v), max_points, replace=False)
    col = getattr(pc, "colors", None)
    return trimesh.PointCloud(v[idx], colors=None if col is None or len(col) != len(v) else np.asarray(col)[idx])
