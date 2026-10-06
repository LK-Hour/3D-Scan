"""Command line: process a session folder, run the upload server, check the installation."""
from __future__ import annotations

import argparse
import importlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__


def _data_dir(arg: str | None) -> Path:
    return Path(arg).expanduser() if arg else Path.home() / ".scanpc"


def cmd_process(a) -> int:
    from .pipeline import PipelineConfig, process_folder
    cfg = PipelineConfig(dense=not a.no_dense, backend=a.backend, colmap_exe=a.colmap, max_image_size=a.max_image_size,
                         output_up=a.up, use_gpu=not a.cpu)
    rep = process_folder(a.session, cfg, progress=lambda s, f, m: print(f"[{f * 100:5.1f}%] {s}: {m}", flush=True))
    print(f"\nGrade: {rep['accuracy']['grade']}. Results in {Path(a.session) / 'work' / 'result'}")
    for w in rep["warnings"]:
        print("warning:", w)
    return 0


def cmd_serve(a) -> int:
    import uvicorn
    from .pairing import Advertiser, lan_addresses, load_or_create_token, pairing_payload, qr_ascii
    from .server import create_app

    data = _data_dir(a.data)
    token = load_or_create_token(data / "config", rotate=a.rotate_token)
    host = a.advertise or lan_addresses()[0]
    opts = {"dense": False} if a.no_dense else {}
    app = create_app(data, token, name=a.name, default_options=opts, pair_host=host, pair_port=a.port)
    payload = pairing_payload(host, a.port, token, a.name)
    print(qr_ascii(payload))
    print(f"Scan this QR in the app (or open http://localhost:{a.port}/pair on this PC).")
    print(f"Address: http://{host}:{a.port}   data folder: {data}")
    print("Windows: allow Python through the firewall for PRIVATE networks when asked.")
    adv = Advertiser(a.name, a.port)
    print("mDNS discovery:", "on" if adv.start() else "off (QR pairing still works)")
    try:
        uvicorn.run(app, host=a.bind, port=a.port, log_level="info")
    finally:
        adv.stop()
        app.state.jobs.stop()
    return 0


def cmd_doctor(a) -> int:
    ok = True

    def line(good: bool | None, text: str) -> None:
        nonlocal ok
        mark = {True: "OK  ", False: "FAIL", None: "info"}[good]
        if good is False:
            ok = False
        print(f"[{mark}] {text}")

    line(None, f"scanpc {__version__}, Python {platform.python_version()} on {platform.system()} {platform.machine()}")
    for mod, why in [("numpy", ""), ("scipy", ""), ("cv2", "opencv-python-headless"), ("PIL", "pillow"), ("trimesh", ""),
                     ("fastapi", ""), ("uvicorn", ""), ("qrcode", ""), ("zeroconf", "optional: auto-discovery"),
                     ("fast_simplification", "optional: smaller GLB"), ("pycolmap", "optional: CPU fallback / tests")]:
        try:
            importlib.import_module(mod)
            line(True, f"python package {mod}")
        except ImportError:
            optional = "optional" in why
            line(None if optional else False, f"python package {mod} missing {('(' + why + ')') if why else ''}")
    try:
        import cv2
        has_aruco = hasattr(cv2, "aruco") and hasattr(cv2.aruco, "ArucoDetector")
        line(has_aruco, f"OpenCV {cv2.__version__} AprilTag detector (needs OpenCV >= 4.7)")
    except ImportError:
        pass

    from .colmap_runner import ColmapCli, find_colmap
    exe = find_colmap(a.colmap)
    if exe:
        c = ColmapCli(exe)
        line(True, f"COLMAP binary: {exe}")
        line(None, c.version_line)
        line(c.has_cuda, "COLMAP built with CUDA (required for dense reconstruction)")
        for cmd, opts in [("feature_extractor", ["--FeatureExtraction.use_gpu", "--SiftExtraction.use_gpu"]),
                          ("matches_importer", ["--match_list_path"]), ("mapper", ["--database_path"]),
                          ("patch_match_stereo", ["--workspace_path"]), ("poisson_mesher", ["--input_path"])]:
            found = c._pick(cmd, *opts)
            line(found is not None, f"colmap {cmd}: option {found or ' / '.join(opts)}")
    else:
        line(None, "COLMAP binary not found (set COLMAP_EXE or add to PATH); only the pycolmap CPU fallback is available")

    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run([smi, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=15).stdout.strip()
            line(True, f"NVIDIA GPU: {out}")
        except Exception as e:
            line(None, f"nvidia-smi failed: {e}")
    else:
        line(None, "nvidia-smi not found")
    d = _data_dir(a.data)
    d.mkdir(parents=True, exist_ok=True)
    line(None, f"data folder {d}, free disk {shutil.disk_usage(d).free / 1e9:.0f} GB")
    print("\nAll required checks passed." if ok else "\nSome required checks failed, see above.")
    return 0 if ok else 1


def cmd_token(a) -> int:
    from .pairing import load_or_create_token
    tok = load_or_create_token(_data_dir(a.data) / "config", rotate=a.rotate)
    print(tok)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scanpc", description="PC side of the offline 3D scanner")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("process", help="reconstruct a capture session folder")
    p.add_argument("session")
    p.add_argument("--no-dense", action="store_true", help="sparse model only (fast)")
    p.add_argument("--backend", default="auto", choices=["auto", "cli", "pycolmap"])
    p.add_argument("--colmap", help="path to the COLMAP executable")
    p.add_argument("--max-image-size", type=int, default=1600)
    p.add_argument("--up", default="y", choices=["y", "z"], help="output up axis (y: glTF/ARCore, z: CAD)")
    p.add_argument("--cpu", action="store_true", help="do not use the GPU")
    p.set_defaults(fn=cmd_process)

    p = sub.add_parser("serve", help="run the upload server and show the pairing QR")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--bind", default="0.0.0.0", help="interface to listen on")
    p.add_argument("--advertise", help="IP address to put in the QR (default: auto-detected LAN address)")
    p.add_argument("--name", default=platform.node() or "scanpc")
    p.add_argument("--data", help="data folder (default ~/.scanpc)")
    p.add_argument("--no-dense", action="store_true", help="default jobs to sparse-only")
    p.add_argument("--rotate-token", action="store_true", help="invalidate previous pairings")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("doctor", help="check dependencies, COLMAP, CUDA")
    p.add_argument("--colmap"); p.add_argument("--data")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("token", help="print (or rotate) the pairing token")
    p.add_argument("--data"); p.add_argument("--rotate", action="store_true")
    p.set_defaults(fn=cmd_token)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
