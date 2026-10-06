import numpy as np

from scanpc.poses import (GL_TO_CV, R_to_quat_wxyz, arcore_c2w_gl, c2w_gl_to_cv_w2c, chordal_mean_rotation,
                          cv_w2c_to_c2w_gl, quat_xyzw_to_R, select_pairs, view_direction)


def rand_rot(rng):
    q = rng.normal(size=4)
    return quat_xyzw_to_R(q / np.linalg.norm(q))


def test_quaternion_roundtrip():
    rng = np.random.default_rng(1)
    for _ in range(50):
        R = rand_rot(rng)
        w, x, y, z = R_to_quat_wxyz(R)
        assert np.allclose(quat_xyzw_to_R(np.array([x, y, z, w])), R, atol=1e-9)


def test_arcore_colmap_roundtrip_and_depth_sign():
    rng = np.random.default_rng(2)
    R = rand_rot(rng)
    C = rng.normal(size=3)
    R_w2c, t = c2w_gl_to_cv_w2c(R, C)
    R_back, C_back = cv_w2c_to_c2w_gl(R_w2c, t)
    assert np.allclose(R_back, R) and np.allclose(C_back, C)
    # a point one metre in front of the camera must have +1 depth in the COLMAP camera frame
    P = C + view_direction(R) * 1.0
    assert np.isclose((R_w2c @ P + t)[2], 1.0)


def test_identity_gl_camera_looks_down_minus_z():
    R_w2c, t = c2w_gl_to_cv_w2c(np.eye(3), np.zeros(3))
    assert np.allclose(R_w2c, GL_TO_CV)           # x right, y down, z forward in CV terms
    assert np.allclose(view_direction(np.eye(3)), [0, 0, -1])


def test_select_pairs_sequential_radius_and_angle():
    # camera 0,1,2 near each other looking +x; camera 3 near but looking -x; camera 4 far away
    C = np.array([[0, 0, 0], [0.1, 0, 0], [0.2, 0, 0], [0.3, 0, 0], [50, 0, 0]], float)
    D = np.array([[1, 0, 0]] * 3 + [[-1, 0, 0], [1, 0, 0]], float)
    pairs = set(select_pairs(C, D, radius=3.0, max_angle_deg=60, max_neighbors=10, sequential=0))
    assert (0, 1) in pairs and (0, 2) in pairs and (1, 2) in pairs
    assert (0, 3) not in pairs and not any(4 in p for p in pairs)
    # sequential window forces neighbours in time regardless of geometry
    seq = set(select_pairs(C, D, radius=0.01, max_angle_deg=1, max_neighbors=1, sequential=1))
    assert {(0, 1), (1, 2), (2, 3), (3, 4)} <= seq


def test_chordal_mean_recovers_rotation():
    rng = np.random.default_rng(3)
    R0 = rand_rot(rng)
    noisy = []
    for _ in range(40):
        a = rng.normal(0, 0.01, 3)
        K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
        noisy.append((np.eye(3) + K) @ R0)
    R = chordal_mean_rotation(np.array(noisy))
    assert np.degrees(np.arccos(np.clip((np.trace(R.T @ R0) - 1) / 2, -1, 1))) < 0.5
