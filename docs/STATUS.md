# STATUS (task board + handoff log)

Update this at the end of every work session. Newest handoff entry goes at the top of the log.
Owner values: `claude`, `codex`, `user`, `-` (unclaimed).

## Phase 1: feasibility (PC side first, then Android capture)
| # | Task | Owner | State | Verified how |
|---|------|-------|-------|--------------|
| 1 | Session format spec + loader/validator (`session.py`) | claude | done | pytest |
| 2 | Pose conversion, pair selection, frame filter | claude | done | pytest |
| 3 | Tag detection/triangulation, scale + ARCore alignment | claude | done | pytest + synthetic room vs ground truth |
| 4 | Pipeline with stage caching + accuracy report | claude | done (sparse) | `--runslow` e2e, pycolmap CPU |
| 5 | Upload server, job queue, QR pairing | claude | done | pytest + e2e over HTTP + real uvicorn smoke test |
| 6 | CLI (`process`, `serve`, `doctor`, `token`) | claude | done | doctor + serve smoke tested |
| 7 | Dense + Poisson on the real GPU | user/claude | **untested** | needs COLMAP CUDA on the GTX 1650 PC |
| 8 | Android: skeleton, ARCore capture, session writer, Compose UI | claude | written, **never compiled with the Android SDK** | `core/` tested on a JVM (32 checks); capture/ui/data only syntax-checked |
| 9 | Android: uploader (resumable, QR pairing) | claude | done in `core/`, wired in UI | Kotlin client vs the real Python server: interrupted+resumed upload, job, GLB download, Range resume |
| 10 | Real-device scan of one room vs laser distances | user | not started | first device experiment in ANDROID_SPEC |

## Measured so far (synthetic only, so optimistic)
Synthetic room 5x2.6x4 m, 48 frames, 8 tags, 7 laser distances (3 mm noise), deliberately wrong ARCore scale (+3.5 %)
and yaw/drift: inter-tag distances (28 pairs, up to ~6 m) error 4.1 mm RMS / 8.2 mm max; vertical axis error 0.4 mm;
mean reprojection 0.28 px. Real phones add lens distortion, rolling shutter, blank walls, auto-exposure: expect worse.

## Phase 2 (planned)
Multi-room stitching via shared tags (+ ICP refine), coverage heatmap in the app, 3D viewer + measure tool,
OBJ/STL/LAS export and floor-origin option, textured mesh (OpenMVS) if wanted.

## Phase 3 (planned)
Semantic detection (walls, racks), floor-plan export, change detection between scans, optional online AI helper.

## Unverified / risks (keep this list honest)
- Dense + Poisson on the real GTX 1650 (4 GB): never executed. Expect to tune `max_image_size`/chunking.
- Real COLMAP CLI: options are probed from `-h` at runtime, but only the `pycolmap` backend has actually run.
- ARCore pose vs saved-image orientation, image size limits and intrinsics on the Pad 6S Pro.
- realme C85 ARCore support unknown.
- Tags must be printed at 100 % and measured; a wrong `tag_size_m` silently scales everything if no laser distances are given
  (the report warns when laser and tag scales disagree by > 2 %).
- Server is plain HTTP + bearer token on the LAN; do not expose it to the internet.

## Handoff log
- 2026-10-06 Claude (Android): `android/` is complete as code: `core/` (pure Kotlin, JVM-tested via `android/core-tests/run.sh`),
  `capture/` (ARCore session, GL preview, frame gate, async JPEG save), `data/`, `ui/` (Compose: home/detail two-pane, scan,
  pair). The sandbox has no Android SDK/Maven, so capture/ui/data were NOT compiled: the first Android Studio sync/build may
  show errors (versions in `android/build.gradle.kts` are unverified). Fix and note here. Open items: pose-vs-image
  orientation on a real device, FIXED focus vs close-range sharpness, upload only while app is in foreground (no WorkManager),
  no IMU file written yet. See `android/BUILD.md`.
- 2026-10-06 Claude: PC side complete and tested (34 tests pass, incl. slow e2e). Next for Codex or Claude: Android app
  per docs/ANDROID_SPEC.md, starting with the "first device experiment". Next for the user: install COLMAP CUDA on the PC and
  run `python -m scanpc doctor`, then `python -m scanpc process <session> ` on a real scan to test dense.
