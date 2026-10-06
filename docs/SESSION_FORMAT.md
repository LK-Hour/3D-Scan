# Capture session format (v1)

One capture session = one folder = one room / aisle / object. The Android app writes
exactly this layout; the PC server and pipeline read it. Everything is plain JSON/JSONL/JPEG
so it can also be copied by USB cable and processed with `scanpc process <folder>`.

```
<session_id>/
  session.json        required  metadata
  intrinsics.json     required  camera calibration for the SAVED frame size
  poses.jsonl         required  one ARCore pose per saved frame
  measurements.json   optional  laser-measured distances between tags
  imu.csv             optional  raw motion log (not used by v1 pipeline)
  frames/             required  000001.jpg, 000002.jpg, ...
```

## session.json
```json
{
  "format_version": 1,
  "session_id": "2026-10-06T14-02-11_living-room",
  "project": "house-1",
  "room_name": "Living room",
  "created_at": "2026-10-06T14:02:11+07:00",
  "device": {"model": "Xiaomi Pad 6S Pro", "android": "15"},
  "image_width": 2000,
  "image_height": 1500,
  "tag_family": "tag36h11",
  "tag_size_m": 0.16,
  "tag_sizes_m": {"7": 0.10}
}
```
* `tag_size_m` is the printed side length of the black square (the outer edge of the black
  border), measured with a ruler. Print at 100 % scale and **measure the printout**.
* `tag_sizes_m` optionally overrides the size for specific tag ids.
* `tag_family` is always `tag36h11` in v1.

## intrinsics.json
Pinhole intrinsics **in pixels of the saved frames** (if the app downscales frames it must
scale fx, fy, cx, cy by the same factor).
```json
{"width": 2000, "height": 1500, "fx": 1480.2, "fy": 1480.2, "cx": 1000.0, "cy": 750.0}
```

## poses.jsonl
One JSON object per line, one line per saved frame:
```json
{"frame": "000001.jpg", "t_ns": 123456789, "tracking": "TRACKING",
 "t": [0.012, 1.431, -0.220], "q": [0.01, 0.70, 0.01, 0.71]}
```
* `t`, `q` = ARCore camera pose, **camera-to-world**, quaternion order **x, y, z, w**.
* Camera axes follow the OpenGL convention **for the saved image**: +X to the right of the
  image, +Y up in the image, camera looks along **-Z**. The app is responsible for rotating
  ARCore's sensor pose if it rotates the saved JPEG (e.g. portrait vs landscape).
* World is ARCore's world: metres, +Y up (gravity aligned). Each session has its own origin.
* `tracking` is ARCore's TrackingState name (`TRACKING`, `PAUSED`, `STOPPED`). Frames that
  are not `TRACKING` are ignored.

## measurements.json (optional but strongly recommended)
Laser-measured real distances between points on printed tags. These fix the **absolute scale**
and independently validate the result.
```json
{"distances": [
  {"a": {"tag": 1, "point": "center"}, "b": {"tag": 4, "point": "center"},
   "meters": 4.213, "sigma_m": 0.003, "use": "fit"},
  {"a": {"tag": 2, "point": "center"}, "b": {"tag": 5, "point": "center"},
   "meters": 3.871, "use": "check"}
]}
```
* `point` is `center` or `corner0`..`corner3` (corner order = tag36h11 detector order:
  top-left, top-right, bottom-right, bottom-left of the upright tag).
* `use`: `fit` (default) feeds the scale estimate, `check` is held out and only reported.
  With >= 3 unmarked distances the pipeline also reports leave-one-out residuals.

## Result folder (`<session>/work/result/`)
`model.glb` (viewer-sized), `mesh.ply`, `pointcloud.ply`, `sparse.ply`, `cameras.json`,
`report.json`, `report.md`. All in **metres**, up axis as configured (default +Y).
