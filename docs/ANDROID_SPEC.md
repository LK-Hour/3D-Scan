# Android app spec (for whoever builds `android/`, Codex or Claude)

Goal of Phase 1: a capture app that writes a valid session folder (docs/SESSION_FORMAT.md) and uploads it to the PC
server. Nothing else is required to validate accuracy on a real device. **None of the ARCore notes below were tested on
a device; verify them on the Pad 6S Pro first (see "First device experiment").**

## Stack
Kotlin, Jetpack Compose (Material 3, `WindowSizeClass` for phone vs tablet), ARCore (`com.google.ar:core`), CameraX is
NOT needed for capture because ARCore owns the camera. OkHttp for HTTP, WorkManager for background upload, ML Kit or
ZXing for QR scanning, `minSdk 26`.

## Capture
- Create an ARCore `Session`, `Config.UpdateMode.LATEST_CAMERA_IMAGE`, focus mode FIXED (autofocus hunting breaks the
  calibration; ARCore normally fixes this by default).
- Pick the largest CPU image size via `CameraConfigFilter` (the pipeline caps images at ~1600 px, so 1920x1080 is enough).
- Per saved frame: `frame.acquireCameraImage()` (YUV_420_888) -> JPEG quality >= 92, **do not rotate it**.
  Intrinsics: `camera.imageIntrinsics` (scale if you downsample) -> `intrinsics.json` (written once).
  Pose: `camera.pose` (sensor-oriented, OpenGL axes) -> `poses.jsonl` with `tracking = camera.trackingState.name`.
  Save only when `TRACKING`; the PC also ignores other states, but saving them wastes space.
- Rate: save a frame when the camera moved >= 5 cm or turned >= 4 deg since the last saved one (matches the PC
  filter) and the device is not moving fast. Target 300-600 frames per room.
- Guidance UI: speed warning, light warning, "tracking lost" banner, a simple coverage map (cells visited from camera
  positions/directions). Remind the user to put tags in view, including at doorways.
- Write files to `getExternalFilesDir("sessions")/<session_id>/` so they can be pulled with adb as a fallback.
- Session id: `yyyy-MM-dd'T'HH-mm-ss_<slug of room name>`.

## Upload (docs/ARCHITECTURE.md, server API)
1. Scan the PC's QR (JSON: `{"v":1,"name","host","port","token"}`); store host/port/token. Optionally resolve `_scanpc._tcp` via NSD.
2. For a session: SHA-256 every file, `POST /v1/sessions` with the manifest.
3. For each file: `HEAD` to get `Upload-Offset`, then `PUT` the remainder with header `Upload-Offset`, streaming in
   chunks of ~4 MB. 409 means "continue from the `Upload-Offset` in the response". 422 means checksum failed: restart that file.
4. Run uploads in a WorkManager job (survives app restarts); upload a finished room while the next one is being scanned.
5. `POST /v1/sessions/{id}/finish` (optional `{"options": {"dense": true}}`), then poll `GET /v1/jobs/{id}` every 2 s.
6. Download `model.glb`, `report.md`/`report.json` from `/v1/sessions/{id}/result/{name}`.
USB fallback: `adb reverse tcp:8765 tcp:8765` and use host `127.0.0.1`.

## Screens (adaptive: single pane on phone, list/detail on tablet)
Projects & rooms list - Capture - Upload/Job status - Result (3D viewer, report, measure tool in Phase 2) - Settings (pair PC, tag size, units).

## First device experiment (do this before building more UI)
1. Minimal app: start ARCore, save 100 frames + poses for one wall with tags, write the session folder.
2. `adb pull` it and run `python -m scanpc process <folder> --no-dense`.
3. Check in `report.json`: `arcore_alignment.orientation_rms_deg` should be small (< 2 deg). If it is huge or the
   registered-frame count is low, the pose/image orientation convention is wrong: fix it in the app (not in `poses.py`)
   and record the finding in docs/DECISIONS.md.
