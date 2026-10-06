package com.scan3d.capture.capture

import android.app.Activity
import android.content.Context
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.media.Image
import android.os.Build
import android.os.PowerManager
import com.google.ar.core.ArCoreApk
import com.google.ar.core.Config
import com.google.ar.core.Frame
import com.google.ar.core.Session
import com.google.ar.core.TrackingState
import com.google.ar.core.CameraConfig
import com.google.ar.core.CameraConfigFilter
import com.google.ar.core.exceptions.CameraNotAvailableException
import com.google.ar.core.exceptions.NotYetAvailableException
import com.google.ar.core.exceptions.UnavailableApkTooOldException
import com.google.ar.core.exceptions.UnavailableArcoreNotInstalledException
import com.google.ar.core.exceptions.UnavailableDeviceNotCompatibleException
import com.google.ar.core.exceptions.UnavailableSdkTooOldException
import com.google.ar.core.exceptions.UnavailableUserDeclinedInstallationException
import com.scan3d.capture.core.FrameGate
import com.scan3d.capture.core.Intrinsics
import com.scan3d.capture.core.PoseSample
import com.scan3d.capture.core.SessionMeta
import com.scan3d.capture.core.SessionWriter
import com.scan3d.capture.core.YuvConverter
import com.scan3d.capture.core.YuvPlanes
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

data class ScanUiState(
    val ready: Boolean = false,          // ARCore session is running
    val message: String = "Starting camera…",
    val tracking: String = "NONE",       // TRACKING | PAUSED | STOPPED | NONE
    val trackingReason: String = "",
    val recording: Boolean = false,
    val frames: Int = 0,
    val bytes: Long = 0,
    val dropped: Int = 0,
    val tooFast: Boolean = false,
    val warning: String = "",
    val imageSize: String = "",
)

/**
 * Owns the ARCore session. Threading: [resume]/[pause]/[close]/[start]/[stop] come from the main thread,
 * [update]/[process] from the GL thread, JPEG encoding runs on a private worker.
 *
 * Compiled with the Android SDK, but pose orientation vs. the unrotated CPU image has not been checked on a real
 * device; see docs/STATUS.md.
 */
class ScanController(private val appContext: Context, private val displayRotation: () -> Int) {
    private val _state = MutableStateFlow(ScanUiState())
    val state: StateFlow<ScanUiState> = _state

    private var session: Session? = null
    private var installRequested = false
    private var textureId = -1
    private var textureSet = false
    @Volatile private var viewportW = 0
    @Volatile private var viewportH = 0
    @Volatile private var viewportDirty = false

    private val gate = FrameGate()

    // JPEG encoding: one thread, small queue, frames are dropped (and counted) instead of piling up in RAM.
    private val queue = LinkedBlockingQueue<Runnable>(MAX_QUEUE)
    private val worker = ThreadPoolExecutor(1, 1, 0L, TimeUnit.MILLISECONDS, queue)

    private class Recording(val writer: SessionWriter, val meta: SessionMeta) {
        val saved = AtomicInteger(0)
        val bytes = AtomicLong(0)
        val dropped = AtomicInteger(0)
        @Volatile var intrinsicsWritten = false
        @Volatile var failed: String? = null
        var gateChecks = 0
    }

    @Volatile private var rec: Recording? = null

    // ---- lifecycle (main thread) -------------------------------------------------------------------------------

    /** Returns null if fine (or if the Google Play Services for AR install dialog is showing), else a user-readable error. */
    fun resume(activity: Activity): String? {
        try {
            if (session == null) {
                when (ArCoreApk.getInstance().requestInstall(activity, !installRequested)) {
                    ArCoreApk.InstallStatus.INSTALL_REQUESTED -> {
                        installRequested = true
                        post(message = "Installing Google Play Services for AR…")
                        return null
                    }
                    ArCoreApk.InstallStatus.INSTALLED -> Unit
                }
                val s = Session(activity)
                configure(s)
                session = s
                textureSet = false
            }
            session?.resume()
            post(ready = true, message = "Point the camera at the room and move slowly")
            return null
        } catch (e: UnavailableUserDeclinedInstallationException) {
            return fail("Google Play Services for AR is required. Install it from the Play Store.")
        } catch (e: UnavailableArcoreNotInstalledException) {
            return fail("Google Play Services for AR is not installed.")
        } catch (e: UnavailableApkTooOldException) {
            return fail("Update Google Play Services for AR from the Play Store.")
        } catch (e: UnavailableSdkTooOldException) {
            return fail("This app is too old for the installed ARCore. Update the app.")
        } catch (e: UnavailableDeviceNotCompatibleException) {
            return fail("This device does not support ARCore, so it cannot be used for scanning.")
        } catch (e: CameraNotAvailableException) {
            session?.close()
            session = null
            return fail("The camera is in use by another app. Close it and try again.")
        } catch (e: SecurityException) {
            return fail("Camera permission is required.")
        } catch (e: Exception) {
            return fail("Could not start ARCore: ${e.message ?: e.javaClass.simpleName}")
        }
    }

    fun pause() {
        stop()
        session?.pause()
        post(ready = false, message = "Camera paused")
    }

    fun close() {
        stop()
        session?.close()
        session = null
        worker.shutdown()
    }

    private fun configure(s: Session) {
        // Highest-resolution CPU image we can afford. More pixels = better accuracy but bigger uploads/slower saving.
        val filter = CameraConfigFilter(s).setTargetFps(FPS_30)
        val configs = s.getSupportedCameraConfigs(filter).ifEmpty { s.getSupportedCameraConfigs(CameraConfigFilter(s)) }
        val best = pickCameraConfig(configs)
        if (best != null) s.cameraConfig = best
        val c = Config(s)
        c.updateMode = Config.UpdateMode.LATEST_CAMERA_IMAGE
        c.focusMode = Config.FocusMode.FIXED         // constant focus keeps intrinsics valid for the whole scan
        c.planeFindingMode = Config.PlaneFindingMode.DISABLED
        c.lightEstimationMode = Config.LightEstimationMode.DISABLED
        c.depthMode = Config.DepthMode.DISABLED
        c.instantPlacementMode = Config.InstantPlacementMode.DISABLED
        s.configure(c)
        val sz = (best ?: s.cameraConfig).imageSize
        post(imageSize = "${sz.width}×${sz.height}")
    }

    private fun pickCameraConfig(configs: List<CameraConfig>): CameraConfig? {
        if (configs.isEmpty()) return null
        val ok = configs.filter { it.imageSize.width.toLong() * it.imageSize.height <= MAX_PIXELS }
        return (ok.ifEmpty { configs.sortedBy { it.imageSize.width.toLong() * it.imageSize.height }.take(1) })
            .maxByOrNull { it.imageSize.width.toLong() * it.imageSize.height }
    }

    // ---- GL thread -------------------------------------------------------------------------------------------

    fun setCameraTexture(id: Int) {
        textureId = id
        textureSet = false
    }

    fun setViewport(w: Int, h: Int) {
        viewportW = w
        viewportH = h
        viewportDirty = true
    }

    fun update(): Frame? {
        val s = session ?: return null
        if (textureId < 0) return null
        if (!textureSet) {
            s.setCameraTextureName(textureId)
            textureSet = true
        }
        if (viewportDirty && viewportW > 0) {
            s.setDisplayGeometry(displayRotation(), viewportW, viewportH)
            viewportDirty = false
        }
        return try {
            s.update()
        } catch (e: CameraNotAvailableException) {
            post(ready = false, message = "Camera not available")
            null
        } catch (e: Exception) {
            null
        }
    }

    fun process(frame: Frame) {
        val camera = frame.camera
        val ts = camera.trackingState
        val r = rec
        val cur = _state.value
        val trackName = ts.name
        val reason = if (ts == TrackingState.TRACKING) "" else camera.trackingFailureReason.name
        if (cur.tracking != trackName || cur.trackingReason != reason) post(tracking = trackName, trackingReason = reason)
        if (r == null || ts != TrackingState.TRACKING) {
            if (r == null && cur.tooFast) post(tooFast = false)
            return
        }

        val pose = camera.pose
        val p = doubleArrayOf(pose.tx().toDouble(), pose.ty().toDouble(), pose.tz().toDouble())
        val q = doubleArrayOf(pose.qx().toDouble(), pose.qy().toDouble(), pose.qz().toDouble(), pose.qw().toDouble())
        val tNs = frame.timestamp
        val decision = gate.evaluate(tNs, p, q)
        if (gate.movingTooFast != cur.tooFast) post(tooFast = gate.movingTooFast)
        if (decision != FrameGate.Decision.SAVE) return

        if (queue.remainingCapacity() == 0) { // encoder is behind: skip rather than buffer more raw frames in RAM
            r.dropped.incrementAndGet()
            publish(r)
            return
        }
        val planes = try {
            frame.acquireCameraImage().use { copyPlanes(it) }
        } catch (e: NotYetAvailableException) {
            return
        } catch (e: Exception) {
            return
        }
        gate.markSaved(tNs, p, q)

        if (!r.intrinsicsWritten) {
            val ci = camera.imageIntrinsics
            val dims = ci.imageDimensions
            val base = Intrinsics(
                dims[0], dims[1],
                ci.focalLength[0].toDouble(), ci.focalLength[1].toDouble(),
                ci.principalPoint[0].toDouble(), ci.principalPoint[1].toDouble(),
            )
            val i = if (dims[0] != planes.width || dims[1] != planes.height) base.scaledTo(planes.width, planes.height) else base
            r.writer.writeIntrinsics(i)
            r.writer.writeMeta(r.meta.copy(imageWidth = planes.width, imageHeight = planes.height))
            r.intrinsicsWritten = true
        }

        val (_, file) = r.writer.reserveFrame()
        val sample = PoseSample(file.name, tNs, "TRACKING", p, q)
        try {
            worker.execute { encode(r, planes, file, sample) }
        } catch (e: Exception) {
            r.dropped.incrementAndGet()
        }
        publish(r)
    }

    private fun encode(r: Recording, planes: YuvPlanes, file: File, sample: PoseSample) {
        try {
            val nv21 = YuvConverter.toNv21(planes)
            val tmp = File(file.path + ".part")
            FileOutputStream(tmp).use { out ->
                YuvImage(nv21, ImageFormat.NV21, planes.width, planes.height, null)
                    .compressToJpeg(Rect(0, 0, planes.width, planes.height), JPEG_QUALITY, out)
            }
            if (!tmp.renameTo(file)) throw java.io.IOException("rename failed")
            r.writer.appendPose(sample) // only after the JPEG exists
            r.saved.incrementAndGet()
            r.bytes.addAndGet(file.length())
        } catch (e: Exception) {
            r.dropped.incrementAndGet()
            r.failed = e.message ?: e.javaClass.simpleName
        }
        publish(r)
    }

    private fun copyPlanes(img: Image): YuvPlanes {
        val p = img.planes
        return YuvPlanes(
            width = img.width, height = img.height,
            y = bytes(p[0].buffer), yRowStride = p[0].rowStride,
            u = bytes(p[1].buffer), v = bytes(p[2].buffer),
            uvRowStride = p[1].rowStride, uvPixelStride = p[1].pixelStride,
        )
    }

    private fun bytes(b: java.nio.ByteBuffer): ByteArray {
        val a = ByteArray(b.remaining())
        b.duplicate().get(a)
        return a
    }

    // ---- recording (main thread) -----------------------------------------------------------------------------

    fun start(dir: File, meta: SessionMeta): Boolean {
        if (rec != null || session == null || !_state.value.ready) return false
        dir.mkdirs()
        if (dir.usableSpace < MIN_FREE_BYTES) {
            post(warning = "Less than ${MIN_FREE_BYTES / 1_000_000} MB free on the device. Free some space first.")
            return false
        }
        val writer = SessionWriter(dir)
        writer.writeMeta(meta)
        gate.reset()
        rec = Recording(writer, meta)
        post(recording = true, frames = 0, bytes = 0, dropped = 0, warning = "", message = "Recording")
        return true
    }

    /** Stops recording, waits for the encoder to drain on a background thread, then calls [onDone]. */
    fun stop(onDone: () -> Unit = {}) {
        val r = rec ?: run { onDone(); return }
        rec = null
        post(recording = false, tooFast = false, message = "Saving last frames…")
        try {
            worker.execute {
                r.writer.close()
                publish(r)
                post(message = "Saved ${r.saved.get()} frames")
                onDone()
            }
        } catch (e: Exception) {
            r.writer.close()
            onDone()
        }
    }

    // ---- helpers ---------------------------------------------------------------------------------------------

    private fun publish(r: Recording) {
        var warn = ""
        r.failed?.let { warn = "Saving a frame failed: $it" }
        if (++r.gateChecks % 20 == 0) {
            if (r.writer.dir.usableSpace < LOW_FREE_BYTES) warn = "Storage almost full. Finish the scan soon."
            if (thermalHot()) warn = "Device is hot. Pause and let it cool to keep tracking stable."
        }
        _state.update {
            it.copy(
                frames = r.saved.get(), bytes = r.bytes.get(), dropped = r.dropped.get(),
                warning = if (warn.isNotEmpty()) warn else it.warning,
            )
        }
    }

    private fun thermalHot(): Boolean {
        if (Build.VERSION.SDK_INT < 29) return false
        val pm = appContext.getSystemService(Context.POWER_SERVICE) as? PowerManager ?: return false
        return pm.currentThermalStatus >= PowerManager.THERMAL_STATUS_SEVERE
    }

    private fun fail(msg: String): String {
        post(ready = false, message = msg)
        return msg
    }

    private fun post(
        ready: Boolean? = null, message: String? = null, tracking: String? = null, trackingReason: String? = null,
        recording: Boolean? = null, frames: Int? = null, bytes: Long? = null, dropped: Int? = null,
        tooFast: Boolean? = null, warning: String? = null, imageSize: String? = null,
    ) {
        _state.update { c ->
            c.copy(
                ready = ready ?: c.ready, message = message ?: c.message, tracking = tracking ?: c.tracking,
                trackingReason = trackingReason ?: c.trackingReason, recording = recording ?: c.recording,
                frames = frames ?: c.frames, bytes = bytes ?: c.bytes, dropped = dropped ?: c.dropped,
                tooFast = tooFast ?: c.tooFast, warning = warning ?: c.warning, imageSize = imageSize ?: c.imageSize,
            )
        }
    }

    private companion object {
        const val MAX_QUEUE = 4
        const val MAX_PIXELS = 4_200_000L
        const val JPEG_QUALITY = 92
        const val MIN_FREE_BYTES = 500_000_000L
        const val LOW_FREE_BYTES = 300_000_000L
        val FPS_30: java.util.EnumSet<CameraConfig.TargetFps> = java.util.EnumSet.of(CameraConfig.TargetFps.TARGET_FPS_30)
    }
}
