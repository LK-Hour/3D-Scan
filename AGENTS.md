# AGENTS.md

Shared instructions for every AI coding agent working in this repo (Codex, Claude Code, others).
`CLAUDE.md` imports this file, so there is one source of truth. Read `docs/STATUS.md` before you start.

## What this project is
An offline 3D scanning system for houses, warehouses and objects:
1. **Android app** (Kotlin + Jetpack Compose, ARCore, CameraX) captures frames + ARCore poses on a tablet/phone.
2. **PC pipeline + server** (this Python package, `scanpc/`) receives sessions over the local network/USB,
   reconstructs them with COLMAP (GPU), scales them to metres using printed AprilTags and laser-measured
   distances, and returns a model + an honest accuracy report.

No internet is required at runtime. Cloud AI (e.g. DeepSeek) is an optional, online-only extra, never in the core path.

## Target hardware (design for this, not for generic cloud GPUs)
- Tablet: Xiaomi Pad 6S Pro (ARCore present). Phone: realme C85 (ARCore support unverified, treat as capture-only).
- PC: Windows or Linux, NVIDIA GTX 1650 Max-Q, **4 GB VRAM**, CUDA 7.5. Dense reconstruction must fit 4 GB
  (images capped at ~1600 px, chunk per room). Gaussian splatting is out of scope for v1.
- Budget for extras is under $30: printed AprilTags, a cheap laser distance meter, a clamp. No LiDAR.

## Repo layout
```
README.md  AGENTS.md  CLAUDE.md
docs/      SESSION_FORMAT.md (the contract)  ARCHITECTURE.md  ANDROID_SPEC.md  STATUS.md (task board + handoff log)  DECISIONS.md
scanpc/    session.py poses.py filtering.py colmap_io.py colmap_runner.py tags.py align.py
           pipeline.py export.py synth.py  server.py jobs.py pairing.py cli.py
tests/     pytest suite (uses a synthetic room, no phone or GPU needed)
android/   Kotlin app (see android/BUILD.md). core/ = pure Kotlin, testable on a JVM: android/core-tests/run.sh (needs KOTLINC)
           capture/ = ARCore + GL + frame saving, data/ = settings+sessions, ui/ = Compose screens
```

## Commands
```
pip install -r requirements.txt
python -m pytest                          # fast tests (~2 s), must pass before you hand off
python -m pytest --runslow                # + end-to-end on a synthetic room with CPU COLMAP (~2.5 min)
python -m scanpc.synth /tmp/room --frames 48      # make a synthetic session
python -m scanpc process /tmp/room --no-dense     # run the pipeline on a session folder
python -m scanpc serve                            # start the upload server (prints a pairing QR)
python -m scanpc doctor                           # check COLMAP / CUDA / dependencies
```

## The contract: `docs/SESSION_FORMAT.md`
The Android app writes it, the PC reads it. **Never change it silently.** If you change it: bump
`format_version`, update the spec, `session.py`, `synth.py`, the tests, and note it in `docs/DECISIONS.md`.
Conventions that are easy to get wrong:
- ARCore poses are camera-to-world, quaternion order **xyzw**, OpenGL camera axes (looks down -Z), metres, +Y up.
- COLMAP is world-to-camera, quaternion order **wxyz**, OpenCV axes (looks down +Z). Conversion lives only in `poses.py`.
- Intrinsics are for the **saved** frame size.

## Working agreement (so two agents don't collide)
- **Lanes.** Check `docs/STATUS.md` "Owner" column. Do not edit files in another lane's in-progress area; leave a
  note in the Handoff log instead. Default lanes: Claude = `scanpc/` core + `docs/`; Codex = `android/` and tests it
  adds. Either may fix a bug anywhere if the fix is small and tests pass.
- **Small, reviewable commits.** One topic per commit. Do not reformat files you are not changing.
- **Tests first for logic.** Anything with maths (poses, scale, alignment, pairing) needs a pytest. Use
  `scanpc.synth` for end-to-end data.
- **Update `docs/STATUS.md`** at the end of every work session: what you finished, what is half-done, what you
  verified and how, what you could not verify.
- **Never claim untested things work.** If you could not run it (no GPU, no device, no real COLMAP CLI), say so in
  STATUS and in the code comment. Accuracy claims need a number from a test or a real scan.
- **Security basics for the server:** token auth on every endpoint except `/v1/health`, path-traversal checks on
  uploads (whitelist names), never `shell=True`, never execute uploaded content.
- **No new heavy dependencies** without a note in `docs/DECISIONS.md` (the PC may be offline; wheels must be pip-installable on Windows).

## Style
Python 3.10+, type hints, `from __future__ import annotations`, small functions, docstrings that say *why*.
Kotlin: Compose, coroutines/Flow, no AsyncTask, `minSdk 26`, ktlint defaults.

## Known unverified areas (do not assume these work)
- Real COLMAP CLI on Windows/CUDA: option names are probed from `colmap <cmd> -h` at runtime, but this was only
  exercised through `pycolmap`. Dense (patch_match_stereo) and Poisson meshing have not been run anywhere.
- ARCore pose orientation vs rotated JPEGs on the actual tablet.
- Wi-Fi/USB transfer speeds are estimates.
