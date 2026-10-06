package com.scan3d.capture.core

import java.io.BufferedWriter
import java.io.File
import java.io.FileOutputStream
import java.io.OutputStreamWriter
import java.security.MessageDigest
import java.util.Locale

/** Writes exactly the capture-session format in docs/SESSION_FORMAT.md (v1). Change both or neither. */
object SessionLayout {
    const val FORMAT_VERSION = 1
    const val SESSION_JSON = "session.json"
    const val INTRINSICS_JSON = "intrinsics.json"
    const val POSES_JSONL = "poses.jsonl"
    const val MEASUREMENTS_JSON = "measurements.json"
    const val IMU_CSV = "imu.csv"
    const val FRAMES_DIR = "frames"
    const val APP_STATE_JSON = "app_state.json" // phone-only bookkeeping, never uploaded

    fun frameName(index: Int): String = String.format(Locale.US, "%06d.jpg", index)

    /** Files the PC server accepts. Anything else in the folder (app_state.json, *.part, work/) is never uploaded. */
    fun isUploadable(relPath: String): Boolean {
        if (relPath == SESSION_JSON || relPath == INTRINSICS_JSON || relPath == POSES_JSONL ||
            relPath == MEASUREMENTS_JSON || relPath == IMU_CSV
        ) return true
        return relPath.startsWith("$FRAMES_DIR/") && relPath.endsWith(".jpg") &&
            relPath.removePrefix("$FRAMES_DIR/").matches(Regex("[A-Za-z0-9_\\-]+\\.jpg"))
    }
}

object Hashing {
    fun sha256Hex(file: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buf = ByteArray(1 shl 20)
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { String.format("%02x", it) }
    }

    fun sha256Hex(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { String.format("%02x", it) }
}

data class Intrinsics(
    val width: Int,
    val height: Int,
    val fx: Double,
    val fy: Double,
    val cx: Double,
    val cy: Double,
) {
    /** Rescale for an image of a different size than ARCore reported (should not normally happen). */
    fun scaledTo(newWidth: Int, newHeight: Int): Intrinsics {
        val sx = newWidth.toDouble() / width
        val sy = newHeight.toDouble() / height
        return Intrinsics(newWidth, newHeight, fx * sx, fy * sy, cx * sx, cy * sy)
    }

    fun toJson(): String = Json.encode(
        linkedMapOf("width" to width, "height" to height, "fx" to fx, "fy" to fy, "cx" to cx, "cy" to cy)
    )
}

/** One line of poses.jsonl. `t`/`q` = ARCore camera pose, camera-to-world, quaternion x,y,z,w. */
data class PoseSample(
    val frame: String,
    val tNs: Long,
    val tracking: String,
    val t: DoubleArray,
    val q: DoubleArray,
) {
    init {
        require(t.size == 3 && q.size == 4) { "pose needs 3 translation and 4 quaternion numbers" }
    }

    fun toJsonLine(): String = Json.encode(
        linkedMapOf(
            "frame" to frame, "t_ns" to tNs, "tracking" to tracking,
            "t" to t.map { round6(it) }, "q" to q.map { round6(it) },
        )
    )

    private fun round6(v: Double): Double = Math.round(v * 1e6) / 1e6
}

data class SessionMeta(
    val sessionId: String,
    val project: String,
    val roomName: String,
    val createdAt: String,
    val deviceModel: String,
    val androidVersion: String,
    val imageWidth: Int,
    val imageHeight: Int,
    val tagSizeM: Double,
) {
    fun toJson(): String = Json.encode(
        linkedMapOf(
            "format_version" to SessionLayout.FORMAT_VERSION,
            "session_id" to sessionId,
            "project" to project,
            "room_name" to roomName,
            "created_at" to createdAt,
            "device" to linkedMapOf("model" to deviceModel, "android" to androidVersion),
            "image_width" to imageWidth,
            "image_height" to imageHeight,
            "tag_family" to "tag36h11",
            "tag_size_m" to tagSizeM,
        )
    )
}

data class LaserMeasurement(
    val tagA: Int,
    val tagB: Int,
    val meters: Double,
    val pointA: String = "center",
    val pointB: String = "center",
    val sigmaM: Double = 0.003,
    val use: String = "fit", // "fit" feeds the scale, "check" is held out and only reported
)

object MeasurementsFile {
    fun toJson(list: List<LaserMeasurement>): String = Json.encode(
        linkedMapOf(
            "distances" to list.map {
                linkedMapOf(
                    "a" to linkedMapOf("tag" to it.tagA, "point" to it.pointA),
                    "b" to linkedMapOf("tag" to it.tagB, "point" to it.pointB),
                    "meters" to it.meters, "sigma_m" to it.sigmaM, "use" to it.use,
                )
            }
        )
    )

    fun parse(text: String): List<LaserMeasurement> =
        Json.parse(text).asMap()["distances"].asList().map { e ->
            val m = e.asMap()
            LaserMeasurement(
                tagA = m["a"].asMap()["tag"].asLong().toInt(),
                tagB = m["b"].asMap()["tag"].asLong().toInt(),
                meters = m["meters"].asDouble(),
                pointA = m["a"].asMap()["point"].asString("center"),
                pointB = m["b"].asMap()["point"].asString("center"),
                sigmaM = m["sigma_m"].asDouble(0.003),
                use = m["use"].asString("fit"),
            )
        }
}

/**
 * Appends frames and poses to a session folder. poses.jsonl lines are only written after the JPEG exists,
 * so the folder is always a valid session even if the app is killed mid-scan.
 */
class SessionWriter(val dir: File) : AutoCloseable {
    val framesDir = File(dir, SessionLayout.FRAMES_DIR)
    private val poses: BufferedWriter
    private var nextIndex: Int

    init {
        framesDir.mkdirs()
        nextIndex = (framesDir.list()?.mapNotNull { it.removeSuffix(".jpg").toIntOrNull() }?.maxOrNull() ?: 0) + 1
        poses = BufferedWriter(
            OutputStreamWriter(FileOutputStream(File(dir, SessionLayout.POSES_JSONL), true), Charsets.UTF_8)
        )
    }

    /** Reserves the next frame file name (thread-safe). */
    @Synchronized
    fun reserveFrame(): Pair<Int, File> {
        val idx = nextIndex++
        return idx to File(framesDir, SessionLayout.frameName(idx))
    }

    @Synchronized
    fun appendPose(sample: PoseSample) {
        poses.write(sample.toJsonLine())
        poses.newLine()
        poses.flush()
    }

    fun writeMeta(meta: SessionMeta) = File(dir, SessionLayout.SESSION_JSON).writeText(meta.toJson())

    fun writeIntrinsics(i: Intrinsics) = File(dir, SessionLayout.INTRINSICS_JSON).writeText(i.toJson())

    fun writeMeasurements(list: List<LaserMeasurement>) =
        File(dir, SessionLayout.MEASUREMENTS_JSON).writeText(MeasurementsFile.toJson(list))

    @Synchronized
    override fun close() {
        poses.flush()
        poses.close()
    }
}

/** What the home screen shows for a session folder on the phone. */
data class SessionInfo(
    val dir: File,
    val id: String,
    val roomName: String,
    val project: String,
    val frameCount: Int,
    val bytes: Long,
    val tagSizeM: Double,
    val measurementCount: Int,
    val uploaded: Boolean,
    val jobId: String?,
    val lastModified: Long,
)

object SessionInspector {
    fun inspect(dir: File): SessionInfo? {
        val metaFile = File(dir, SessionLayout.SESSION_JSON)
        val meta = if (metaFile.exists()) runCatching { Json.parse(metaFile.readText()).asMap() }.getOrNull() else null
        val frames = File(dir, SessionLayout.FRAMES_DIR).listFiles { f -> f.name.endsWith(".jpg") } ?: emptyArray()
        if (meta == null && frames.isEmpty()) return null
        val state = File(dir, SessionLayout.APP_STATE_JSON).let {
            if (it.exists()) runCatching { Json.parse(it.readText()).asMap() }.getOrNull() ?: emptyMap() else emptyMap()
        }
        val measurements = File(dir, SessionLayout.MEASUREMENTS_JSON).let {
            if (it.exists()) runCatching { MeasurementsFile.parse(it.readText()).size }.getOrDefault(0) else 0
        }
        return SessionInfo(
            dir = dir,
            id = meta?.get("session_id").asString(dir.name),
            roomName = meta?.get("room_name").asString(dir.name),
            project = meta?.get("project").asString(""),
            frameCount = frames.size,
            bytes = dir.walkTopDown().filter { it.isFile }.sumOf { it.length() },
            tagSizeM = meta?.get("tag_size_m").asDouble(0.0),
            measurementCount = measurements,
            uploaded = state["uploaded"] == true,
            jobId = (state["job_id"] as? String),
            lastModified = dir.lastModified(),
        )
    }

    fun saveState(dir: File, uploaded: Boolean, jobId: String?) {
        File(dir, SessionLayout.APP_STATE_JSON).writeText(Json.encode(linkedMapOf("uploaded" to uploaded, "job_id" to jobId)))
    }
}
