# scan3d-pc

PC side of an offline Android 3D scanner (houses, warehouses, objects). The tablet captures frames and ARCore
poses; this package reconstructs them with COLMAP on your GPU, scales the result to real metres using printed
AprilTags and laser-measured distances, and writes a model plus an honest accuracy report.

> Status: work in progress. See `docs/STATUS.md` for what is built and what is unverified.
> AI agents (Codex, Claude Code) should start with `AGENTS.md`.

## What accuracy to expect
Phone-only scanning with a $30 kit gives roughly 1-3 cm locally and 2-5 cm over a whole building, **if** you scan
slowly with overlap, good light and use tags + at least three laser distances. The report tells you the real number
for each scan. Millimetre accuracy over a building needs a survey-grade laser scanner.

## Install (PC)
```
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m scanpc doctor
```
**COLMAP with CUDA** (needed for dense reconstruction on the GTX 1650):
- Windows: download the prebuilt `colmap-x64-windows-cuda` zip from the COLMAP GitHub releases, unzip, and either
  add its folder to `PATH` or set `COLMAP_EXE` to `COLMAP.bat`.
- Linux: build COLMAP from source with CUDA (distro packages are often CPU-only).
Without it the pipeline still runs the sparse stages through `pycolmap` (CPU) and skips dense with a warning.

## Quick start (no phone needed)
```
python -m scanpc.synth /tmp/room --frames 48        # synthetic room with tags + laser distances
python -m scanpc process /tmp/room --no-dense
cat /tmp/room/work/result/report.md
```

## Receiving scans from the tablet
```
python -m scanpc serve        # shows a QR code; scan it once in the Android app
```
Wi-Fi (5 GHz) is fine for a room at a time; use a USB cable (`adb reverse`) for big transfers.

## Print and measure checklist (the $30 kit)
1. Print AprilTags (family **tag36h11**, ids 1..8) at **100 %**, mount flat on walls/shelves, ~1 per 3-4 m and at every doorway.
2. **Measure the printed black square with a ruler** and put it in `session.json` as `tag_size_m`.
3. With a laser meter, measure at least **3** distances between tag centres in different directions; mark one or two
   as `"use": "check"` so the report can validate the scale independently.

## Docs
`docs/SESSION_FORMAT.md` (contract with the app) · `docs/ARCHITECTURE.md` · `docs/DECISIONS.md` · `docs/STATUS.md`
