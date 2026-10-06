package com.scan3d.capture.core

import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.net.HttpRetryException
import java.net.HttpURLConnection
import java.net.URL

/** Error reply from the PC server (or a failure to understand it). `offset` is set for resumable-upload conflicts. */
class PcException(val status: Int, message: String, val offset: Long? = null) : Exception(message)

data class ManifestEntry(val rel: String, val file: File, val size: Long, val sha256: String)

data class FileState(val offset: Long, val complete: Boolean)

data class PutResult(val status: Int, val offset: Long, val complete: Boolean, val detail: String)

data class JobInfo(
    val id: String,
    val sessionId: String,
    val state: String,
    val stage: String,
    val progress: Double,
    val message: String,
    val error: String?,
    val raw: Map<String, Any?>,
) {
    val finished: Boolean get() = state == "done" || state == "failed" || state == "cancelled"

    companion object {
        fun from(m: Map<String, Any?>) = JobInfo(
            id = m["id"].asString(), sessionId = m["session_id"].asString(), state = m["state"].asString("unknown"),
            stage = m["stage"].asString(), progress = m["progress"].asDouble(), message = m["message"].asString(),
            error = m["error"] as? String, raw = m,
        )
    }
}

/**
 * HTTP client for the PC server (docs/ARCHITECTURE.md, "Server API"). JDK-only (HttpURLConnection) so it runs both
 * on Android and on a plain JVM for testing against the real server.
 */
class PcClient(
    val cfg: PcConfig,
    private val connectTimeoutMs: Int = 8_000,
    private val readTimeoutMs: Int = 60_000,
) {
    class HttpResult(val status: Int, val headers: Map<String, String>, val body: ByteArray) {
        val ok: Boolean get() = status in 200..299
        val text: String get() = body.toString(Charsets.UTF_8)
    }

    private fun open(path: String): HttpURLConnection {
        val c = URL(cfg.baseUrl + path).openConnection() as HttpURLConnection
        c.connectTimeout = connectTimeoutMs
        c.readTimeout = readTimeoutMs
        c.useCaches = false
        c.instanceFollowRedirects = false
        return c
    }

    fun request(
        method: String,
        path: String,
        body: ByteArray? = null,
        bodyOff: Int = 0,
        bodyLen: Int = body?.size ?: 0,
        headers: Map<String, String> = emptyMap(),
        auth: Boolean = true,
        contentType: String? = null,
    ): HttpResult {
        val conn = open(path)
        try {
            conn.requestMethod = method
            if (auth) conn.setRequestProperty("Authorization", "Bearer ${cfg.token}")
            for ((k, v) in headers) conn.setRequestProperty(k, v)
            if (body != null) {
                conn.doOutput = true
                conn.setFixedLengthStreamingMode(bodyLen.toLong())
                if (contentType != null) conn.setRequestProperty("Content-Type", contentType)
                conn.outputStream.use { it.write(body, bodyOff, bodyLen) }
            }
            val status = try {
                conn.responseCode
            } catch (e: HttpRetryException) { // streaming-mode request answered with 401
                e.responseCode()
            }
            val hdrs = HashMap<String, String>()
            for ((k, v) in conn.headerFields) if (k != null && v.isNotEmpty()) hdrs[k.lowercase()] = v.last()
            val stream = if (status >= 400) conn.errorStream else runCatching { conn.inputStream }.getOrNull()
            val bytes = stream?.use { it.readBytes() } ?: ByteArray(0)
            return HttpResult(status, hdrs, bytes)
        } catch (e: IOException) {
            conn.disconnect()
            throw e
        }
    }

    private fun describe(res: HttpResult): String {
        val parsed = runCatching { Json.parse(res.text) }.getOrNull()
        val detail = parsed.asMap()["detail"]
        val msg = when (detail) {
            is String -> detail
            is Map<*, *> -> (detail["detail"] as? String ?: "error") +
                ((detail["errors"] as? List<*>)?.let { ": " + it.joinToString("; ") } ?: "") +
                ((detail["missing"] as? List<*>)?.let { " (missing: " + it.take(5).joinToString() + ")" } ?: "")
            else -> if (res.text.isNotBlank()) res.text.take(200) else "HTTP ${res.status}"
        }
        return if (res.status == 401) "The PC rejected the pairing token. Pair again by scanning the QR code." else msg
    }

    private fun check(res: HttpResult): HttpResult {
        if (!res.ok) throw PcException(res.status, describe(res))
        return res
    }

    private fun jsonBody(v: Any?): ByteArray = Json.encode(v).toByteArray(Charsets.UTF_8)

    // ---- API -------------------------------------------------------------------------------------------------

    fun health(): Map<String, Any?> = Json.parse(check(request("GET", "/v1/health", auth = false)).text).asMap()

    /** Registers the manifest. Returns the server's per-file state so the caller can skip finished files. */
    fun createSession(sessionId: String, files: List<ManifestEntry>): Map<String, Any?> {
        val body = jsonBody(
            linkedMapOf(
                "session_id" to sessionId,
                "files" to files.map { linkedMapOf("path" to it.rel, "size" to it.size, "sha256" to it.sha256) },
            )
        )
        return Json.parse(check(request("POST", "/v1/sessions", body, contentType = "application/json")).text).asMap()
    }

    fun head(sessionId: String, rel: String): FileState {
        val r = check(request("HEAD", "/v1/sessions/$sessionId/files/$rel"))
        return FileState(r.headers["upload-offset"]?.toLongOrNull() ?: 0L, r.headers["upload-complete"] == "1")
    }

    /** Sends bytes [off, off+len) of a file as the part starting at `offset`. 409/422 are returned, not thrown. */
    fun put(sessionId: String, rel: String, offset: Long, data: ByteArray, off: Int, len: Int): PutResult {
        val r = request(
            "PUT", "/v1/sessions/$sessionId/files/$rel", data, off, len,
            headers = mapOf("Upload-Offset" to offset.toString()), contentType = "application/octet-stream",
        )
        val newOffset = r.headers["upload-offset"]?.toLongOrNull()
        return when {
            r.ok -> PutResult(r.status, newOffset ?: (offset + len), r.headers["upload-complete"] == "1", "")
            r.status == 409 || r.status == 422 -> PutResult(r.status, newOffset ?: 0L, false, describe(r))
            else -> throw PcException(r.status, describe(r))
        }
    }

    fun finish(sessionId: String, options: Map<String, Any?>): Map<String, Any?> {
        val body = jsonBody(linkedMapOf("options" to options))
        return Json.parse(check(request("POST", "/v1/sessions/$sessionId/finish", body, contentType = "application/json")).text).asMap()
    }

    fun job(jobId: String): Map<String, Any?> = Json.parse(check(request("GET", "/v1/jobs/$jobId")).text).asMap()

    fun cancelJob(jobId: String) {
        check(request("POST", "/v1/jobs/$jobId/cancel", ByteArray(0)))
    }

    fun results(sessionId: String): List<Pair<String, Long>> =
        Json.parse(check(request("GET", "/v1/sessions/$sessionId/results")).text).asList()
            .map { it.asMap()["name"].asString() to it.asMap()["size"].asLong() }

    /** Downloads a result file; a partial `<dest>.part` is resumed with an HTTP Range request. */
    fun download(sessionId: String, name: String, dest: File, onProgress: (done: Long, total: Long) -> Unit = { _, _ -> }) {
        val part = File(dest.path + ".part")
        dest.parentFile?.mkdirs()
        var have = if (part.exists()) part.length() else 0L
        val conn = open("/v1/sessions/$sessionId/result/$name")
        try {
            conn.requestMethod = "GET"
            conn.setRequestProperty("Authorization", "Bearer ${cfg.token}")
            if (have > 0) conn.setRequestProperty("Range", "bytes=$have-")
            val status = conn.responseCode
            if (status == 416) { // our partial file is not valid for this content; start over
                part.delete()
                conn.disconnect()
                return download(sessionId, name, dest, onProgress)
            }
            if (status != 200 && status != 206) {
                val msg = describe(HttpResult(status, emptyMap(), conn.errorStream?.use { it.readBytes() } ?: ByteArray(0)))
                throw PcException(status, msg)
            }
            val append = status == 206
            if (!append) have = 0L
            val remaining = conn.getHeaderField("Content-Length")?.toLongOrNull() ?: -1L
            val total = if (remaining >= 0) have + remaining else -1L
            conn.inputStream.use { input ->
                FileOutputStream(part, append).use { out ->
                    val buf = ByteArray(256 * 1024)
                    var done = have
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        done += n
                        onProgress(done, total)
                    }
                }
            }
            if (dest.exists()) dest.delete()
            if (!part.renameTo(dest)) throw IOException("could not move ${part.name} into place")
        } catch (e: IOException) {
            conn.disconnect()
            throw e
        }
    }
}

object JobWatcher {
    /** Polls until the job finishes. Tolerates short network drops (the PC may be busy or Wi-Fi may blip). */
    fun wait(
        client: PcClient,
        jobId: String,
        pollMs: Long = 2_000,
        maxConsecutiveErrors: Int = 30,
        isCancelled: () -> Boolean = { false },
        onUpdate: (JobInfo) -> Unit = {},
    ): JobInfo {
        var errors = 0
        while (true) {
            if (isCancelled()) throw InterruptedException("cancelled")
            try {
                val info = JobInfo.from(client.job(jobId))
                errors = 0
                onUpdate(info)
                if (info.finished) return info
            } catch (e: IOException) {
                if (++errors >= maxConsecutiveErrors) throw e
            }
            Thread.sleep(pollMs)
        }
    }
}
