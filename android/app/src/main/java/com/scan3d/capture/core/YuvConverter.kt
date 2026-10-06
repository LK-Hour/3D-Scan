package com.scan3d.capture.core

/** Planes copied out of an android.media.Image (YUV_420_888) so the Image can be closed immediately on the GL thread. */
class YuvPlanes(
    val width: Int,
    val height: Int,
    val y: ByteArray,
    val yRowStride: Int,
    val u: ByteArray,
    val v: ByteArray,
    val uvRowStride: Int,
    val uvPixelStride: Int,
)

object YuvConverter {
    /**
     * YUV_420_888 -> NV21 (full Y plane, then interleaved V,U at half resolution) which android.graphics.YuvImage
     * compresses to JPEG. Handles row padding and both planar (pixelStride 1) and semi-planar (pixelStride 2) chroma.
     */
    fun toNv21(p: YuvPlanes): ByteArray {
        val w = p.width
        val h = p.height
        val out = ByteArray(w * h * 3 / 2)
        if (p.yRowStride == w) {
            System.arraycopy(p.y, 0, out, 0, w * h)
        } else {
            for (row in 0 until h) System.arraycopy(p.y, row * p.yRowStride, out, row * w, w)
        }
        var o = w * h
        val cw = w / 2
        val ch = h / 2
        for (row in 0 until ch) {
            val base = row * p.uvRowStride
            for (col in 0 until cw) {
                val idx = base + col * p.uvPixelStride
                // the last chroma sample of a plane buffer can be one byte short when planes overlap: clamp
                out[o++] = p.v[minOf(idx, p.v.size - 1)]
                out[o++] = p.u[minOf(idx, p.u.size - 1)]
            }
        }
        return out
    }
}
