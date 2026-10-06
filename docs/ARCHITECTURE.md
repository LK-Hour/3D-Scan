# Architecture

```
 Android tablet                         Local network / USB (no internet)                 PC (GTX 1650, 4 GB)
┌──────────────────────┐   QR pairing   ┌─────────────────────────┐   job queue   ┌──────────────────────────────┐
│ Compose UI (adaptive)│──────────────▶ │ FastAPI server          │─────────────▶ │ Pipeline (one job at a time) │
│ ARCore poses + frames│  resumable     │ token auth, checksums   │               │ filter → features → matching │
│ CameraX / IMU        │  chunked HTTP  │ session store on disk   │               │ → sparse SfM → tags → scale  │
│ coverage guide       │◀────────────── │ job status + results    │◀───────────── │ → dense → mesh → export      │
│ viewer + measuring   │  model + report└─────────────────────────┘   progress    └──────────────────────────────┘
└──────────────────────┘
```

## Why these choices (details in DECISIONS.md)
- **Kotlin native, not Flutter:** the hard part (ARCore frame/pose sync, sensors) is native; Flutter would still need a Kotlin core.
- **COLMAP on the PC:** best open-source accuracy, runs offline, CUDA on the GTX 1650.
- **ARCore poses as hints, not truth:** they choose which image pairs to match, give gravity ("up") and a rough
  scale. Final geometry comes from bundle adjustment; final scale from tags and laser distances.
- **Tags + laser distances:** the cheapest way to get trustworthy absolute scale and an *independent* accuracy check.

## Accuracy model (what the report means)
- Absolute scale error: from laser residuals (check / leave-one-out) when available, else from tag-size spread, else
  ARCore (~2-5 %).
- Local quality: COLMAP mean reprojection error and tag edge-length error after scaling.
- Not modelled: drift across many rooms. Stitching (Phase 2) uses shared tags; report residuals there too.

## Pipeline stages and cache keys
`filter → features → matching → sparse → tags → align → dense → export`. Each stage key = hash(previous key + its
config), stored in `<session>/work/state.json`; unchanged stages are skipped.

## Server API (v1, planned in `server.py`)
`GET /v1/health` (no auth) · `POST /v1/sessions` (manifest) · `HEAD|PUT /v1/sessions/{id}/files/{path}` (offset
resume, sha256 on completion) · `POST /v1/sessions/{id}/finish` · `GET /v1/jobs/{id}` · `GET /v1/sessions/{id}/result/{name}`.
Auth: `Authorization: Bearer <token>` from the pairing QR.

## Android (planned layout)
`android/app` (Compose, `WindowSizeClass` for phone/tablet) · `capture/` (ARCore session, frame+pose writer) ·
`upload/` (WorkManager resumable uploader) · `viewer/` (GLB/PLY viewer, measure tool) · `pairing/` (QR + mDNS).
