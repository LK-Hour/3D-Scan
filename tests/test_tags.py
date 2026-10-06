import cv2
import numpy as np

from scanpc import synth
from scanpc.tags import detect_frames


def test_synthetic_tag_is_detected_with_correct_corner_order(tmp_path):
    rng = np.random.default_rng(0)
    textures = {k: np.full((64, 64, 3), 120, np.uint8) for k in synth.FACES}
    dic = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    markers = {t: cv2.aruco.generateImageMarker(dic, t, 256) for t in synth.TAGS}
    pos = np.array([1.0, 1.5, 2.5])                       # inside the room, looking at wall z=0 where tag 1 hangs
    R = synth._lookat(pos, np.array([1.1, 1.5, 0.0]))
    W, H, f = 800, 600, 600.0
    img = synth.render(R, pos, textures, markers, 0.20, W, H, f, f, W / 2, H / 2)
    p = tmp_path / "a.jpg"; cv2.imwrite(str(p), img)
    dets = detect_frames({"a.jpg": p})["a.jpg"]
    assert any(d.tag_id == 1 for d in dets)
    d = next(d for d in dets if d.tag_id == 1)
    # corner order: top-left, top-right, bottom-right, bottom-left of the upright tag
    tl, tr, br, bl = d.corners
    assert tl[0] < tr[0] and bl[0] < br[0] and tl[1] < bl[1] and tr[1] < br[1]
    # and the 3D truth projects onto the detected pixels (checks synth geometry and detector together)
    K = np.array([[f, 0, W / 2], [0, f, H / 2], [0, 0, 1.0]])
    corners3d = synth.tag_corners_world(1, 0.20)
    R_cv = R @ synth.GL_TO_CV if hasattr(synth, "GL_TO_CV") else R @ np.diag([1, -1, -1])
    Xc = (corners3d - pos) @ R_cv                         # world -> OpenCV camera
    proj = (Xc @ K.T); proj = proj[:, :2] / proj[:, 2:3]
    assert np.abs(proj - d.corners).max() < 1.5
