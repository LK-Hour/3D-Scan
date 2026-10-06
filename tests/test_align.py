import numpy as np

from scanpc.align import Sim3, estimate_scale, umeyama
from scanpc.session import Intrinsics, Measurement, Session
from scanpc.tags import TagModel


def make_session(measurements, tag_size=0.2):
    return Session(root=None, meta={"tag_size_m": tag_size}, intrinsics=Intrinsics(10, 10, 1, 1, 5, 5), poses=[],
                   measurements=measurements)


def make_tags(positions, model_scale, tag_size=0.2):
    """Tags whose model-space size is tag_size / model_scale (model units are arbitrary)."""
    tags = {}
    h = tag_size / model_scale / 2
    for tid, c in positions.items():
        corners = np.array([[-h, h, 0], [h, h, 0], [h, -h, 0], [-h, -h, 0]]) + np.asarray(c) / model_scale
        tags[tid] = TagModel(tid, corners, 5, 0.3, np.full(4, 2 * h))
    return tags


def test_umeyama_recovers_similarity():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(30, 3))
    a = 0.7
    R = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    Y = 2.5 * X @ R.T + np.array([1, 2, 3])
    sim = umeyama(X, Y)
    assert np.isclose(sim.s, 2.5) and np.allclose(sim.R, R) and np.allclose(sim.apply(X), Y)


def test_sim3_then_composes():
    a, b = Sim3(2.0, np.eye(3), np.array([1.0, 0, 0])), Sim3(3.0, np.eye(3), np.array([0, 1.0, 0]))
    X = np.array([[1.0, 1, 1]])
    assert np.allclose(a.then(b).apply(X), b.apply(a.apply(X)))


def test_scale_from_laser_beats_tag_edges_and_reports_loo():
    pos = {1: (0, 0, 0), 2: (4, 0, 0), 3: (0, 3, 0), 4: (4, 3, 2), 5: (1, 1, 5)}
    true_scale = 0.31
    tags = make_tags(pos, true_scale)
    meas = []
    for a, b in [(1, 2), (1, 3), (2, 4), (3, 5)]:
        d = float(np.linalg.norm(np.array(pos[a]) - np.array(pos[b])))
        meas.append(Measurement(a, "center", b, "center", d + 0.002, 0.003))
    est = estimate_scale(make_session(meas), tags, scale_arcore=0.33)
    assert est.source == "laser" and abs(est.scale / true_scale - 1) < 0.002
    assert any(r["kind"] == "leave-one-out" for r in est.laser_residuals)
    assert est.expected_error_pct < 0.5


def test_check_measurements_are_held_out():
    pos = {1: (0, 0, 0), 2: (4, 0, 0), 3: (0, 3, 0)}
    tags = make_tags(pos, 0.5)
    meas = [Measurement(1, "center", 2, "center", 4.0, 0.003, "fit"),
            Measurement(1, "center", 3, "center", 3.05, 0.003, "check")]  # deliberately 5 cm wrong
    est = estimate_scale(make_session(meas), tags, 0.5)
    chk = [r for r in est.laser_residuals if r["kind"] == "check"]
    assert len(chk) == 1 and abs(chk[0]["residual_mm"] + 50) < 1.0   # the held-out error is visible, not hidden


def test_fallbacks_and_warnings():
    pos = {1: (0, 0, 0), 2: (4, 0, 0)}
    tags = make_tags(pos, 0.25)
    est = estimate_scale(make_session([]), tags, 0.26)
    assert est.source == "tags" and abs(est.scale / 0.25 - 1) < 1e-6 and est.warnings
    est = estimate_scale(make_session([]), {}, 0.26)
    assert est.source == "arcore" and est.scale == 0.26
    one = [Measurement(1, "center", 2, "center", 4.0)]
    est = estimate_scale(make_session(one), tags, 0.25)
    assert est.source == "laser" and any("at least 3" in w for w in est.warnings)


def test_laser_tag_disagreement_is_flagged():
    pos = {1: (0, 0, 0), 2: (4, 0, 0)}
    tags = make_tags(pos, 0.25)
    meas = [Measurement(1, "center", 2, "center", 4.0 * 1.05)]      # laser says 5 % bigger than tag size implies
    est = estimate_scale(make_session(meas), tags, 0.25)
    assert any("disagree" in w for w in est.warnings)
