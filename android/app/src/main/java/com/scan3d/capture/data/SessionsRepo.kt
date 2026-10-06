package com.scan3d.capture.data

import android.content.Context
import com.scan3d.capture.core.SessionInfo
import com.scan3d.capture.core.SessionInspector
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Session folders live in app-specific external storage (no permission needed, survives app restarts, `adb pull`-able). */
class SessionsRepo(context: Context) {
    val root: File = (context.getExternalFilesDir("sessions") ?: File(context.filesDir, "sessions")).also { it.mkdirs() }

    fun list(): List<SessionInfo> =
        (root.listFiles { f -> f.isDirectory } ?: emptyArray())
            .mapNotNull { SessionInspector.inspect(it) }
            .sortedByDescending { it.lastModified }

    /** New session folder named `yyyy-MM-dd'T'HH-mm-ss_<slug>` (ids are safe for URLs and file systems). */
    fun create(roomName: String): File {
        val stamp = SimpleDateFormat("yyyy-MM-dd'T'HH-mm-ss", Locale.US).format(Date())
        val dir = File(root, "${stamp}_${slug(roomName)}")
        dir.mkdirs()
        return dir
    }

    fun delete(dir: File): Boolean = dir.canonicalPath.startsWith(root.canonicalPath + File.separator) && dir.deleteRecursively()

    fun resultsDir(session: SessionInfo): File = File(session.dir, "results").also { it.mkdirs() }

    companion object {
        fun slug(name: String): String {
            val s = name.trim().lowercase(Locale.US).replace(Regex("[^a-z0-9]+"), "-").trim('-')
            return s.take(40).ifEmpty { "scan" }
        }
    }
}
