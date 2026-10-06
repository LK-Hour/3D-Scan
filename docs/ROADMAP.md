# ROADMAP: from working MVP to a tool you use every day

Status of each item lives in `docs/STATUS.md`. This file is the plan and the exit criteria. Numbers below are
**targets to be proven with real scans**, not claims. Lanes: **C** = Claude, **X** = Codex, **U** = user (needs your hands/devices).

Definition of "daily usable": you can scan a room in under 15 minutes, upload without babysitting the app, get a
metric model the next morning with a report you trust, and know from the report whether to rescan.

---------------------------------------------------------------------------------------------------------------

## Phase 0: MVP (done)
ARCore capture, session format, resumable upload, QR pairing, COLMAP sparse pipeline, AprilTag + laser scale, report.
Proven on a synthetic room (4 mm RMS). The first Pad 6S Pro run is still awaiting device verification.

## Phase 1: Capture quality (camera)  ~1-2 weeks
Goal: every saved frame is sharp, well exposed, high resolution; the user is told when it is not.

| # | Item | Lane | Done when |
|---|------|------|-----------|
| 1.1 | On-device blur + brightness gate (`ImageQuality`, `QualityTracker`): skip frames much blurrier than recent ones, reject dark frames, "hold steadier / more light" hints | C | JVM test: blurred image scores < 20 % of sharp; first real scan shows < 5 % blurry frames reaching the PC |
| 1.2 | Resolution presets (Standard 2 MP / High 4 MP / Max 9 MP) chosen per scan, size estimate shown | C | Preset changes CPU image size on the tablet; upload size per 100 frames displayed |
| 1.3 | **High-res stills via ARCore Shared Camera + Camera2** (ARCore tracks on its stream, app captures 12 MP+ JPEG with locked focus/exposure). Biggest single quality win; ARCore's CPU image is usually <= 1080p-4 MP | X (C reviews) | A 12 MP still is saved with an ARCore pose taken at the same timestamp; pose-time offset measured < 10 ms |
| 1.4 | Focus + exposure policy: lock AF at a chosen distance (or per-scan autofocus with per-frame intrinsics), lock AE/AWB during a scan to avoid brightness jumps | X | Brightness variation between consecutive frames < 5 %; focus mode recorded in `session.json` |
| 1.5 | Save IMU (`imu.csv` already in the format) for blur/motion estimates on the PC | X | PC `filtering.py` uses it; documented in SESSION_FORMAT |
| 1.6 | Live coverage guide: grid of "seen well / seen once / not seen" using ARCore poses; "tags visible" indicator via a lightweight on-device tag check | X | After a scan, a coverage % is shown and correlates with PC holes |
| 1.7 | Per-device **lens calibration** (print a calibration board or use the tag sheet); store `distortion` in `intrinsics.json` v2 | C + U | Reprojection error with calibrated intrinsics lower than with ARCore intrinsics on 3 test scans |

Exit: sharp-frame ratio >= 95 %, median frame >= 4 MP (stills 12 MP if 1.3 works), no brightness flicker.

## Phase 2: Scanning accuracy  ~2-3 weeks
Goal: a known, stable error budget you can state per scan.

| # | Item | Lane | Done when |
|---|------|------|-----------|
| 2.1 | **Validation protocol + benchmark room**: one room with 10 tags, 8 laser distances, 2 held out. Repeatable `docs/VALIDATION.md` and a script that scores a scan | U + C | 5 real scans scored; results table in STATUS |
| 2.2 | Tag-aware bundle adjustment: tag corners as shared 3D points with known edge length, laser distances as soft constraints, ARCore gravity as a weak prior | C | On real scans, held-out laser error median <= 10 mm over 3-6 m, max <= 20 mm |
| 2.3 | Loop closure + drift check: detect when the walk returns to the start, report gap before/after | C | Report shows drift number; warns > 2 cm |
| 2.4 | Robustness: outlier frame/tag rejection, wrong tag-size detector (tag vs laser disagreement), duplicate tag id detection | C | Unit tests with injected faults |
| 2.5 | Rolling-shutter and lens model choice per device (OPENCV vs FULL_OPENCV), auto-select by lowest reprojection error | C | Chosen model recorded in report |
| 2.6 | Better scale sources: multiple lasers with outlier rejection, optional known object (A4, door) as fallback | C | Scale uncertainty (sigma) in report |
| 2.7 | Accuracy grade tied to measured numbers, not heuristics; plain-language "rescan this wall" advice from the residual map | C | Grade matches held-out error on all benchmark scans |

Exit targets (to be confirmed or revised by 2.1 data): <= 1 cm error over 5 m in a single room; <= 5 cm over a 20 m
warehouse aisle with tags every ~5 m. If real data says otherwise, change the targets here and say why.

## Phase 3: PC reliability and dense quality  ~1-2 weeks
| # | Item | Lane | Done when |
|---|------|------|-----------|
| 3.1 | COLMAP CUDA install guide + `doctor --fix` hints for Linux and Windows | C + U | `doctor` all green on your PC |
| 3.2 | Run dense + Poisson on the GTX 1650 (4 GB): tune `max_image_size`, patch-match window, chunk per room, fall back to CPU/sparse automatically | C + U | One 150-frame room finishes dense in < 60 min without VRAM errors |
| 3.3 | Job resume after PC restart, queue persistence, disk-space check, log file per job, clean cancel | C | Kill the server mid-job: restart resumes or fails with a clear message |
| 3.4 | Mesh cleanup: remove floaters, fill small holes, keep room-scale detail; optional textured mesh (OpenMVS) | C | Visual check + file sizes documented |
| 3.5 | Windows pass: paths, service start at login (`scanpc serve --install`), firewall prompt help | X + U | Same scan processes identically on Windows and Linux |
| 3.6 | Auto-process: upload finished -> job starts without tapping | C | Setting in app + server option |

## Phase 4: Whole houses and warehouses  ~2-3 weeks
| # | Item | Lane | Done when |
|---|------|------|-----------|
| 4.1 | Project model: many sessions (rooms) under one project, shared tag ids define the common frame | C + X | Project screen in app; `project.json` on PC |
| 4.2 | Multi-room stitching via shared tags, then ICP/BA refinement; per-room accuracy preserved | C | Two adjacent rooms with 3 shared tags merge within 2 cm at the door |
| 4.3 | Large-scene handling: tile dense by area, streaming PLY/LAS, LOD GLB | C | 10-room house produces one merged, openable model |
| 4.4 | Warehouse mode: long aisles, tag every N metres guide, drift watchdog, rack-friendly keyframing (repetitive texture warning) | C + U | One real aisle >= 30 m scanned and checked against laser |
| 4.5 | Floor plan export (2D top-down + DXF/SVG) with true dimensions | C | Plan dimensions match laser within 1 % |

## Phase 5: Daily-use experience  ~2-3 weeks (can run alongside 3-4)
| # | Item | Lane | Done when |
|---|------|------|-----------|
| 5.1 | Background upload: WorkManager + foreground service, survives app switch, screen off, Wi-Fi drops | X | Upload continues for a 5 GB session with the screen off |
| 5.2 | mDNS discovery (no QR after first pairing), PC IP change handled | X | App finds the PC by name on a new network |
| 5.3 | Notifications: upload done, processing done, processing failed with reason | X | Appear on the tablet while the app is closed |
| 5.4 | Tag sheet generator (`python -m scanpc tags --size 16cm --ids 0-15` -> printable PDF at true scale, with scale-check ruler) | C | Printed sheet measures correct with a ruler |
| 5.5 | Guided onboarding + pre-scan checklist (tags placed? light ok? battery/storage?) and post-scan summary | X | A new user completes a scan with no help |
| 5.6 | In-app viewer: orbit the GLB, tap-to-measure two points, show distance | X | Measured distance matches laser on the model |
| 5.7 | Storage management: show sizes, delete after confirmed upload, optional auto-clean | X | One tap frees space safely |
| 5.8 | Export formats: GLB, PLY, OBJ, STL, LAS/LAZ, Z-up option, units option; open-in-Blender instructions | C | Each format opens in Blender/MeshLab with correct scale |
| 5.9 | PC desktop dashboard (local web page): sessions, jobs, results, open folder, re-run with other options | C | Everything the app can do is visible on the PC |
| 5.10 | Local logs and a "Share diagnostics" file (no cloud) for debugging | X | Bug reports include device, ARCore version, settings, last errors |

## Phase 6: Quality assurance and release  (continuous, finish ~1 week)
| # | Item | Lane |
|---|------|------|
| 6.1 | CI (GitHub Actions): `pytest`, `core-tests`, Gradle `assembleDebug` + `lint` on every push | X |
| 6.2 | Instrumented tests on the tablet: capture start/stop, kill-and-resume, upload resume | X |
| 6.3 | Regression benchmark: scored scans re-run on every pipeline change; accuracy never silently gets worse | C |
| 6.4 | Signed release APK, version numbers, changelog, session `format_version` migration tests | X |
| 6.5 | User manual (scanning technique, tag placement, laser use, troubleshooting) with photos | C + U |
| 6.6 | Backup: copy sessions + results to an external drive, restore test | C |

## Phase 7: Optional extras (only after Phase 1-5 hold up)
- Optional online AI helper (DeepSeek or other) that explains the report and suggests where to rescan; strictly opt-in, text only, no images or models leave the device by default.
- Semantic labels (walls, doors, racks), change detection between two scans of the same space.
- Gaussian splat / photoreal viewer (needs a stronger GPU than 4 GB).
- Cheap add-ons within the $30 budget: clamp/tripod mount for stable stills, battery pack, printed scale bar.

---------------------------------------------------------------------------------------------------------------

## Order of work (dependencies)
1. Phase 1.1, 1.2 now (small, testable) -> 2.1 benchmark room scans (needs you) in parallel.
2. 3.1-3.2 as soon as COLMAP CUDA is installed: until dense has run on your GPU, quality claims are only sparse.
3. 1.3 (high-res stills) and 2.2 (tag-aware BA) are the two biggest accuracy/quality levers; do them next.
4. 5.1-5.3 before long scans: nobody wants to babysit a 10 GB upload.
5. Phase 4 only after single-room accuracy meets its exit targets.

## What the user needs to provide
- A benchmark space and ~20 minutes for each validation scan; laser readings written down.
- COLMAP with CUDA on the PC (guide in 3.1).
- Report of which realme/Xiaomi camera configs ARCore lists (app will show them in a diagnostics screen, 5.10).
- Decide the main design app (Blender / Fusion / SketchUp / other) for export priority in 5.8.

## Risks
- ARCore may expose only low-res CPU images on the realme C85 (or not support ARCore at all): then it is a viewer/capture-only device.
- Shared Camera API behaviour differs per vendor; keep the Phase-1.2 path as fallback.
- Dense reconstruction in 4 GB VRAM is the main unknown for quality; budget tuning time.
- Repetitive warehouse racks and blank walls defeat feature matching: tags and guidance are the mitigation, not magic.
- Each phase's code written without a device or SDK must be listed under "Unverified" in STATUS until proven.
