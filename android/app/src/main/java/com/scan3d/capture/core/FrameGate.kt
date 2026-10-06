package com.scan3d.capture.core

import kotlin.math.abs
import kotlin.math.acos
import kotlin.math.sqrt

/**
 * Decides, per ARCore frame, whether to save it. Mirrors the PC filter (docs/SESSION_FORMAT.md, scanpc/filtering.py)
 * so the phone does not store frames the PC would throw away, and refuses frames taken while moving fast
 * (motion blur is the most common reason a scan fails).
 */
class FrameGate(
    private val minTranslationM: Double = 0.04,
    private val minRotationDeg: Double = 3.0,
    private val minIntervalNs: Long = 150_000_000L,
    private val maxLinearSpeed: Double = 0.9,        // metres per second
    private val maxAngularSpeedDegPerSec: Double = 45.0,
) {
    enum class Decision { SAVE, STILL, TOO_FAST, TOO_SOON }

    private class Sample(val tNs: Long, val p: DoubleArray, val q: DoubleArray)

    private var prev: Sample? = null
    private var lastSaved: Sample? = null

    /** True if the most recent evaluated frame was rejected for speed (drive a "move slower" hint from this). */
    var movingTooFast: Boolean = false
        private set

    fun reset() {
        prev = null
        lastSaved = null
        movingTooFast = false
    }

    fun evaluate(tNs: Long, position: DoubleArray, quat: DoubleArray): Decision {
        val cur = Sample(tNs, position, quat)
        val before = prev
        prev = cur
        var tooFast = false
        if (before != null) {
            val dt = (tNs - before.tNs) / 1e9
            if (dt > 0) {
                tooFast = dist(before.p, cur.p) / dt > maxLinearSpeed ||
                    angleDeg(before.q, cur.q) / dt > maxAngularSpeedDegPerSec
            }
        }
        movingTooFast = tooFast
        if (tooFast) return Decision.TOO_FAST
        val last = lastSaved ?: return Decision.SAVE
        if (tNs - last.tNs < minIntervalNs) return Decision.TOO_SOON
        val moved = dist(last.p, cur.p) >= minTranslationM
        val turned = angleDeg(last.q, cur.q) >= minRotationDeg
        return if (moved || turned) Decision.SAVE else Decision.STILL
    }

    /** Call when the frame was actually accepted for saving. */
    fun markSaved(tNs: Long, position: DoubleArray, quat: DoubleArray) {
        lastSaved = Sample(tNs, position, quat)
    }

    companion object {
        fun dist(a: DoubleArray, b: DoubleArray): Double {
            val dx = a[0] - b[0]
            val dy = a[1] - b[1]
            val dz = a[2] - b[2]
            return sqrt(dx * dx + dy * dy + dz * dz)
        }

        /** Angle between two unit quaternions (x, y, z, w) in degrees. */
        fun angleDeg(a: DoubleArray, b: DoubleArray): Double {
            val dot = abs(a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]).coerceAtMost(1.0)
            return Math.toDegrees(2.0 * acos(dot))
        }
    }
}
