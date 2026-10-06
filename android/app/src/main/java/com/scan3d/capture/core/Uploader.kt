package com.scan3d.capture.core

import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

data class UploadProgress(
    val phase: String,           // "preparing" | "uploading" | "done"
    val filesDone: Int,
    val filesTotal: Int,
    val bytesDone: Long,
    val bytesTotal: Long,
)

class UploadCancelled : Exception("upload cancelled")

/**
 * Resumable, checksummed upload of a session folder (protocol: docs/ARCHITECTURE.md).
 * Safe to call again after a failure or app restart: files the server already has are skipped and partial files
 * continue from the offset the server reports.
 */
class Uploader(
    private val client: PcClient,
    private val chunkSize: Int = 4 * 1024 * 1024,
    private val parallel: Int = 3,
    private val maxAttempts: Int = 6,
    private val retryDelayMs: (attempt: Int) -> Long = { minOf(8_000L, 500L shl it) },
) {
    fun collect(dir: File): List<ManifestEntry> {
        val out = ArrayList<ManifestEntry>()
        val root = dir.canonicalFile
        root.walkTopDown().filter { it.isFile }.forEach { f ->
            val rel = f.relativeTo(root).path.replace(File.separatorChar, '/')
            if (SessionLayout.isUploadable(rel)) out.add(ManifestEntry(rel, f, f.length(), Hashing.sha256Hex(f)))
        }
        // small metadata files first so a half-finished upload is still inspectable on the PC
        return out.sortedWith(compareBy({ it.rel.startsWith("frames/") }, { it.rel }))
    }

    fun upload(
        dir: File,
        sessionId: String,
        isCancelled: () -> Boolean = { false },
        onProgress: (UploadProgress) -> Unit = {},
    ): UploadProgress {
        onProgress(UploadProgress("preparing", 0, 0, 0, 0))
        val files = collect(dir)
        if (files.none { it.rel == SessionLayout.POSES_JSONL }) throw IllegalStateException("This session has no poses.jsonl; nothing was captured")
        val status = client.createSession(sessionId, files)
        val serverFiles = status["files"].asMap()
        val total = files.sumOf { it.size }
        val doneBytes = AtomicLong(0)
        val doneFiles = AtomicInteger(0)
        var lastReport = 0L
        fun report(force: Boolean = false) {
            val now = System.currentTimeMillis()
            synchronized(this) {
                if (force || now - lastReport >= 200) {
                    lastReport = now
                    onProgress(UploadProgress("uploading", doneFiles.get(), files.size, doneBytes.get(), total))
                }
            }
        }

        val todo = ArrayList<ManifestEntry>()
        for (f in files) {
            val st = serverFiles[f.rel].asMap()
            if (st["complete"] == true) {
                doneBytes.addAndGet(f.size)
                doneFiles.incrementAndGet()
            } else {
                todo.add(f)
            }
        }
        report(true)

        val failed = AtomicBoolean(false)
        val pool = Executors.newFixedThreadPool(parallel.coerceIn(1, 8))
        try {
            val futures = todo.map { entry ->
                pool.submit(Callable {
                    if (failed.get()) return@Callable
                    try {
                        uploadFile(sessionId, entry, doneBytes, isCancelled, failed)
                        doneFiles.incrementAndGet()
                        report()
                    } catch (e: Exception) {
                        failed.set(true)
                        throw e
                    }
                })
            }
            var firstError: Throwable? = null
            for (f in futures) {
                try {
                    f.get()
                } catch (e: java.util.concurrent.ExecutionException) {
                    if (firstError == null || firstError is UploadCancelled) firstError = e.cause ?: e
                }
            }
            if (firstError != null) throw firstError
        } finally {
            pool.shutdownNow()
        }
        val result = UploadProgress("done", files.size, files.size, total, total)
        onProgress(result)
        return result
    }

    private fun uploadFile(
        sessionId: String,
        entry: ManifestEntry,
        bytesAcc: AtomicLong,
        isCancelled: () -> Boolean,
        failed: AtomicBoolean,
    ) {
        var counted = 0L
        fun setCounted(newValue: Long) {
            bytesAcc.addAndGet(newValue - counted)
            counted = newValue
        }

        var attempt = 0
        var offset = 0L
        var needHead = true
        RandomAccessFile(entry.file, "r").use { raf ->
            val buf = ByteArray(chunkSize)
            while (true) {
                if (isCancelled() || failed.get()) throw UploadCancelled()
                try {
                    if (needHead) {
                        val st = client.head(sessionId, entry.rel)
                        if (st.complete) { setCounted(entry.size); return }
                        offset = st.offset
                        setCounted(offset)
                        needHead = false
                    }
                    val len = minOf(chunkSize.toLong(), entry.size - offset).toInt()
                    raf.seek(offset)
                    raf.readFully(buf, 0, len)
                    val res = client.put(sessionId, entry.rel, offset, buf, 0, len)
                    when {
                        res.status in 200..299 -> {
                            offset = res.offset
                            setCounted(offset)
                            attempt = 0
                            if (res.complete) return
                        }
                        res.status == 409 -> { // we and the server disagree about the offset: continue from the server's
                            offset = res.offset
                            setCounted(offset)
                        }
                        else -> { // 422: checksum mismatch, the server discarded the file; send it again from the start
                            if (++attempt >= maxAttempts) throw PcException(res.status, res.detail)
                            offset = 0
                            setCounted(0)
                        }
                    }
                } catch (e: IOException) { // network trouble: wait, ask the server where we are, continue
                    if (++attempt >= maxAttempts) throw e
                    sleepOrCancel(retryDelayMs(attempt), isCancelled)
                    needHead = true
                } catch (e: PcException) {
                    if (e.status in 500..599 && ++attempt < maxAttempts) {
                        sleepOrCancel(retryDelayMs(attempt), isCancelled)
                        needHead = true
                    } else {
                        throw e
                    }
                }
            }
        }
    }

    private fun sleepOrCancel(ms: Long, isCancelled: () -> Boolean) {
        var left = ms
        while (left > 0) {
            if (isCancelled()) throw UploadCancelled()
            val step = minOf(left, 200L)
            Thread.sleep(step)
            left -= step
        }
    }
}
