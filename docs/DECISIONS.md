# Decisions log (append new entries at the bottom)

Format: date · decision · why · consequence.

- 2026-10-06 · Native Kotlin + Compose for Android (not Flutter) · ARCore/camera/IMU core is native either way; one language is faster to debug without device access · iOS would need a rewrite.
- 2026-10-06 · Offline-only core; DeepSeek (or any cloud AI) is optional and online-only · user requires offline use · on-device models (TFLite/ONNX) for any AI in the core path.
- 2026-10-06 · Reconstruction on the PC with COLMAP, tablet only captures/previews · GTX 1650 4 GB is far stronger than the tablet for this · dense per room at <= 1600 px.
- 2026-10-06 · Absolute scale from AprilTag size and laser-measured tag distances, ARCore scale only as fallback · ARCore scale is off by a few percent · user must print tags at 100 % and measure them.
- 2026-10-06 · COLMAP CLI options are discovered at runtime from `-h` output · option names changed across 3.11 / 3.12 / 4.x · unknown options are silently omitted, `doctor` reports them.
- 2026-10-06 · `pycolmap` backend exists for tests/CPU-only use; dense requires the CLI with CUDA · pycolmap CUDA wheels are Linux-only · Windows dense uses the official COLMAP CUDA zip.
- 2026-10-06 · Tag detection runs on the PC from saved full-resolution frames · avoids a second contract for corner data from the app · the app may still detect tags live for coverage feedback.
- 2026-10-06 · Server is plain HTTP with a bearer token from a QR code · local-network use; TLS pinning is a later hardening step · do not expose the port to the internet.
