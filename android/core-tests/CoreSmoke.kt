import com.scan3d.capture.core.*
import java.io.File
import java.nio.file.Files
import java.util.Locale
import kotlin.math.cos
import kotlin.math.sin
import kotlin.system.exitProcess

/**
 * JVM-only checks of the `core` package (no Android SDK needed). Run: android/core-tests/run.sh
 *   offline part : JSON, FrameGate, pairing, session writer, upload whitelist
 *   online part  : (when args are given)  <baseUrl> <token> <session dir made by scanpc.synth>
 *                  real upload (chunked + interrupted + resumed) -> finish -> poll job -> download results
 */
var failures = 0

fun check(name: String, ok: Boolean, detail: String = "") {
    println((if (ok) "PASS  " else "FAIL  ") + name + (if (!ok && detail.isNotEmpty()) "  -> $detail" else ""))
    if (!ok) failures++
}

fun quatY(deg: Double): DoubleArray {
    val h = Math.toRadians(deg) / 2
    return doubleArrayOf(0.0, sin(h), 0.0, cos(h))
}

fun testJson() {
    val obj = linkedMapOf<String, Any?>(
        "s" to "quote\" back\\ nl\n tab\t unié ctrl\u0001", "i" to 42, "l" to 1234567890123L, "d" to 0.000123,
        "b" to true, "n" to null, "arr" to listOf(1, 2.5, "x"), "nested" to linkedMapOf("k" to doubleArrayOf(1.0, 2.0)),
    )
    val back = Json.parse(Json.encode(obj)).asMap()
    check("json round trip strings", back["s"] == obj["s"])
    check("json round trip numbers", back["i"].asLong() == 42L && back["l"].asLong() == 1234567890123L && back["d"].asDouble() == 0.000123)
    check("json round trip misc", back["b"] == true && back["n"] == null && back["arr"].asList().size == 3 &&
        back["nested"].asMap()["k"].asList()[1].asDouble() == 2.0)
    check("json parses python style output", Json.parse("""{"a": [1, 2.5e-3, -4], "b": "é😀"}""").asMap()["a"].asList()[1].asDouble() == 0.0025)
    check("json rejects garbage", runCatching { Json.parse("{oops") }.isFailure && runCatching { Json.parse("[1,2] x") }.isFailure)
    check("json rejects NaN", runCatching { Json.encode(Double.NaN) }.isFailure)
}

fun testFrameGate() {
    val g = FrameGate()
    val q0 = quatY(0.0)
    fun p(x: Double) = doubleArrayOf(x, 1.4, 0.0)
    fun ms(t: Int) = t * 1_000_000L
    check("gate: first frame saved", g.evaluate(ms(0), p(0.0), q0) == FrameGate.Decision.SAVE)
    g.markSaved(ms(0), p(0.0), q0)
    check("gate: too soon", g.evaluate(ms(50), p(0.01), q0) == FrameGate.Decision.TOO_SOON)
    check("gate: standing still is skipped", g.evaluate(ms(200), p(0.01), q0) == FrameGate.Decision.STILL)
    check("gate: moved 6 cm is saved", g.evaluate(ms(400), p(0.06), q0) == FrameGate.Decision.SAVE)
    g.markSaved(ms(400), p(0.06), q0)
    check("gate: fast motion refused and flagged", g.evaluate(ms(500), p(0.56), q0) == FrameGate.Decision.TOO_FAST && g.movingTooFast)
    check("gate: recovers after slowing", g.evaluate(ms(900), p(0.57), q0) != FrameGate.Decision.TOO_FAST && !g.movingTooFast)
    val g2 = FrameGate()
    g2.evaluate(0, p(0.0), q0); g2.markSaved(0, p(0.0), q0)
    check("gate: 5 degree turn is saved", g2.evaluate(ms(500), p(0.0), quatY(5.0)) == FrameGate.Decision.SAVE)
    check("gate: fast spin refused", g2.evaluate(ms(550), p(0.0), quatY(60.0)) == FrameGate.Decision.TOO_FAST)
    check("quat angle 90deg", Math.abs(FrameGate.angleDeg(q0, quatY(90.0)) - 90.0) < 1e-9)
}

fun testYuv() {
    // 4x4 image, Y rows padded to stride 6, chroma semi-planar (pixelStride 2, rowStride 4, last byte missing like real buffers)
    val y = ByteArray(6 * 4) { -1 }
    for (r in 0 until 4) for (c in 0 until 4) y[r * 6 + c] = (r * 4 + c).toByte()
    val uBuf = byteArrayOf(10, 99, 11, 99, 12, 99, 13)      // U at even offsets (4 samples), trailing V byte dropped
    val vBuf = byteArrayOf(20, 99, 21, 99, 22, 99, 23, 99)
    val nv = YuvConverter.toNv21(YuvPlanes(4, 4, y, 6, uBuf, vBuf, 4, 2))
    check("nv21 size", nv.size == 24)
    check("nv21 luma de-padded", (0 until 16).all { nv[it].toInt() == it })
    check("nv21 chroma is V,U interleaved", nv.slice(16 until 24).map { it.toInt() } == listOf(20, 10, 21, 11, 22, 12, 23, 13), nv.slice(16 until 24).toString())
    val planar = YuvConverter.toNv21(YuvPlanes(2, 2, byteArrayOf(1, 2, 3, 4), 2, byteArrayOf(7), byteArrayOf(9), 1, 1))
    check("nv21 planar chroma", planar.toList() == listOf<Byte>(1, 2, 3, 4, 9, 7))
}

fun testPairing() {
    val tok = "x".repeat(43)
    val c = Pairing.parseQr("""{"v":1,"name":"my-pc","host":"192.168.1.50","port":8765,"token":"$tok"}""")
    check("pairing parses QR", c.host == "192.168.1.50" && c.port == 8765 && c.baseUrl == "http://192.168.1.50:8765" && c.name == "my-pc")
    check("pairing wraps IPv6", Pairing.build("fe80::1", 8765, tok).baseUrl == "http://[fe80::1]:8765")
    check("pairing rejects junk", listOf(
        "hello", """{"v":2,"host":"a","port":1,"token":"$tok"}""", """{"v":1,"host":"a b","port":1,"token":"$tok"}""",
        """{"v":1,"host":"a","port":0,"token":"$tok"}""", """{"v":1,"host":"a","port":5,"token":"short"}""",
    ).all { runCatching { Pairing.parseQr(it) }.isFailure })
}

fun testSessionWriter(tmp: File) {
    val originalLocale = Locale.getDefault()
    try {
        Locale.setDefault(Locale.forLanguageTag("ar-EG"))
        check("frame names use ASCII digits in any locale", SessionLayout.frameName(12) == "000012.jpg")
    } finally {
        Locale.setDefault(originalLocale)
    }
    val dir = File(tmp, "writer-test").apply { mkdirs() }
    SessionWriter(dir).use { w ->
        w.writeMeta(SessionMeta("2026-10-06T14-02-11_test", "house", "Living \"room\"", "2026-10-06T14:02:11+07:00", "Pad", "15", 1920, 1080, 0.16))
        w.writeIntrinsics(Intrinsics(1920, 1080, 1500.5, 1499.0, 960.0, 540.0))
        for (i in 0 until 3) {
            val (idx, f) = w.reserveFrame()
            f.writeBytes(byteArrayOf(1, 2, 3))
            w.appendPose(PoseSample(f.name, 1000L * idx, "TRACKING", doubleArrayOf(0.1 * idx, 1.4, -0.2), doubleArrayOf(0.0, 0.0, 0.0, 1.0)))
        }
        w.writeMeasurements(listOf(LaserMeasurement(1, 2, 4.213), LaserMeasurement(2, 5, 3.871, use = "check")))
    }
    val lines = File(dir, "poses.jsonl").readLines()
    val first = Json.parse(lines[0]).asMap()
    check("writer: poses.jsonl has one line per frame", lines.size == 3 && first["frame"] == "000001.jpg" && first["tracking"] == "TRACKING")
    check("writer: pose arrays", first["t"].asList().size == 3 && first["q"].asList().size == 4 && first["t_ns"].asLong() == 1000L)
    val meta = Json.parse(File(dir, "session.json").readText()).asMap()
    check("writer: session.json fields", meta["format_version"].asLong() == 1L && meta["tag_size_m"].asDouble() == 0.16 &&
        meta["room_name"] == "Living \"room\"" && meta["tag_family"] == "tag36h11" && meta["device"].asMap()["model"] == "Pad")
    val intr = Json.parse(File(dir, "intrinsics.json").readText()).asMap()
    check("writer: intrinsics.json", intr["width"].asLong() == 1920L && intr["fx"].asDouble() == 1500.5)
    val ms = MeasurementsFile.parse(File(dir, "measurements.json").readText())
    check("writer: measurements round trip", ms.size == 2 && ms[1].use == "check" && ms[0].meters == 4.213 && ms[1].tagB == 5)
    SessionWriter(dir).use { w -> check("writer: resumes numbering after reopen", w.reserveFrame().first == 4) }
    check("intrinsics rescale", Intrinsics(2000, 1000, 1000.0, 1000.0, 1000.0, 500.0).scaledTo(1000, 500).fx == 500.0)

    File(dir, "app_state.json").writeText("{}")
    File(dir, "work").mkdirs(); File(dir, "work/state.json").writeText("{}")
    File(dir, "frames/000009.jpg.part").writeBytes(byteArrayOf(1))
    File(dir, "frames/notes.txt").writeText("x")
    val names = Uploader(PcClient(PcConfig("127.0.0.1", 1, "t".repeat(40)))).collect(dir).map { it.rel }.toSet()
    check("upload whitelist", names == setOf("session.json", "intrinsics.json", "poses.jsonl", "measurements.json",
        "frames/000001.jpg", "frames/000002.jpg", "frames/000003.jpg"), names.toString())
    val info = SessionInspector.inspect(dir)!!
    check("inspector", info.frameCount == 3 && info.roomName == "Living \"room\"" && info.measurementCount == 2 && !info.uploaded)
    SessionInspector.saveState(dir, true, "abc")
    check("inspector state", SessionInspector.inspect(dir)!!.let { it.uploaded && it.jobId == "abc" })
}

fun testOnline(base: String, token: String, sessionDir: File, tmp: File) {
    val uri = java.net.URI(base)
    val cfg = Pairing.build(uri.host, uri.port, token)
    val client = PcClient(cfg)
    check("health", client.health()["protocol"].asLong() == 1L)
    val bad = PcClient(PcConfig(uri.host, uri.port, "z".repeat(40)))
    val e = runCatching { bad.createSession("x-test", emptyList()) }.exceptionOrNull()
    check("wrong token -> 401 with a helpful message", e is PcException && e.status == 401 && e.message!!.contains("pair", ignoreCase = true), e.toString())

    val sid = "kotlin-e2e-" + System.currentTimeMillis()
    // small chunks force multi-chunk uploads; cancel part-way, then resume
    val small = Uploader(client, chunkSize = 16 * 1024, parallel = 3)
    var sent = 0L
    val firstTry = runCatching {
        small.upload(sessionDir, sid, isCancelled = { sent > 400_000 }) { sent = it.bytesDone }
    }.exceptionOrNull()
    check("interrupted upload stops cleanly", firstTry is UploadCancelled, firstTry.toString())
    val partial = client.createSession(sid, small.collect(sessionDir))
    check("server kept a partial upload", partial["files_complete"].asLong() < partial["files_total"].asLong() && partial["bytes_received"].asLong() > 0,
        "complete=${partial["files_complete"]} total=${partial["files_total"]}")
    var last: UploadProgress? = null
    val done = small.upload(sessionDir, sid) { last = it }
    val st = client.createSession(sid, small.collect(sessionDir))
    check("resumed upload completes", done.phase == "done" && st["files_complete"] == st["files_total"] && st["state"] == "ready", st.toString().take(300))
    check("progress reached 100%", last?.bytesDone == last?.bytesTotal && (last?.bytesTotal ?: 0) > 0)

    val job = client.finish(sid, linkedMapOf("dense" to false, "use_gpu" to false, "max_image_size" to 1024, "max_features" to 4096))
    check("finish queues a job", job["state"].asString() in setOf("queued", "running"), job.toString())
    var lastMsg = ""
    val info = JobWatcher.wait(client, job["id"].asString(), pollMs = 2000) {
        val m = "${it.state} ${it.stage} ${(it.progress * 100).toInt()}%"
        if (m != lastMsg) { println("      job: $m ${it.message.take(60)}"); lastMsg = m }
    }
    check("job finished OK", info.state == "done", info.error ?: info.message)
    val report = info.raw["report"].asMap()
    check("report says laser-scaled, high grade", report["scale"].asMap()["source"] == "laser" && report["accuracy"].asMap()["grade"] == "high", report["accuracy"].toString())
    val names = client.results(sid).map { it.first }
    check("results listed", names.containsAll(listOf("model.glb", "report.md", "report.json", "sparse.ply")), names.toString())
    val glb = File(tmp, "model.glb")
    client.download(sid, "model.glb", glb)
    check("model.glb downloaded", glb.length() > 1000 && String(glb.readBytes().copyOf(4)) == "glTF", "size=${glb.length()}")
    // resume a partial download
    val full = client.results(sid).first { it.first == "sparse.ply" }.second
    val ply = File(tmp, "sparse.ply")
    client.download(sid, "sparse.ply", ply)
    check("download size matches server", ply.length() == full)
    val resumed = File(tmp, "resumed.ply")
    File(resumed.path + ".part").writeBytes(ply.readBytes().copyOf((full / 2).toInt()))
    client.download(sid, "sparse.ply", resumed)
    check("interrupted download resumes with Range", resumed.length() == full && resumed.readBytes().contentEquals(ply.readBytes()))
}

fun main(args: Array<String>) {
    val tmp = Files.createTempDirectory("scan3d-core-test").toFile()
    testJson(); testFrameGate(); testYuv(); testPairing(); testSessionWriter(tmp)
    if (args.size >= 3) testOnline(args[0], args[1], File(args[2]), tmp) else println("(online part skipped: pass <baseUrl> <token> <sessionDir>)")
    println(if (failures == 0) "\nALL PASSED" else "\n$failures FAILED")
    tmp.deleteRecursively()
    exitProcess(if (failures == 0) 0 else 1)
}
