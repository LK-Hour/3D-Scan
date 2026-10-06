package com.scan3d.capture.ui

import android.app.Application
import android.os.Build
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.scan3d.capture.capture.ScanController
import com.scan3d.capture.core.Json
import com.scan3d.capture.core.JobInfo
import com.scan3d.capture.core.JobWatcher
import com.scan3d.capture.core.LaserMeasurement
import com.scan3d.capture.core.MeasurementsFile
import com.scan3d.capture.core.Pairing
import com.scan3d.capture.core.PcClient
import com.scan3d.capture.core.PcConfig
import com.scan3d.capture.core.SessionInfo
import com.scan3d.capture.core.SessionInspector
import com.scan3d.capture.core.SessionLayout
import com.scan3d.capture.core.SessionMeta
import com.scan3d.capture.core.UploadCancelled
import com.scan3d.capture.core.Uploader
import com.scan3d.capture.core.asMap
import com.scan3d.capture.data.AppSettings
import com.scan3d.capture.data.SessionsRepo
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

enum class Screen { HOME, SCAN, PAIR }

/** What the detail panel shows about the long-running operation for the selected session. */
data class BusyState(
    val label: String = "",
    val progress: Float = -1f,   // 0..1, or -1 for indeterminate
    val running: Boolean = false,
)

class AppViewModel(app: Application) : AndroidViewModel(app) {
    val settings = AppSettings(app)
    val repo = SessionsRepo(app)

    @Suppress("DEPRECATION")
    val controller = ScanController(app) {
        (app.getSystemService(android.content.Context.WINDOW_SERVICE) as android.view.WindowManager).defaultDisplay.rotation
    }

    var screen by mutableStateOf(Screen.HOME)
    var sessions by mutableStateOf<List<SessionInfo>>(emptyList())
    var selectedId by mutableStateOf<String?>(null)
    var pc by mutableStateOf(settings.pc)
    var pcStatus by mutableStateOf("")          // result of the last connection test
    var busy by mutableStateOf(BusyState())
    var job by mutableStateOf<JobInfo?>(null)
    var message by mutableStateOf("")           // transient snackbar-ish text
    var results by mutableStateOf<List<File>>(emptyList())
    var measurements by mutableStateOf<List<LaserMeasurement>>(emptyList())

    /** Directory/meta of the scan currently being recorded. */
    var scanDir: File? = null
    private var scanMeta: SessionMeta? = null
    private var worker: Job? = null
    @Volatile private var cancelRequested = false

    val selected: SessionInfo? get() = sessions.firstOrNull { it.id == selectedId }

    init {
        refresh()
    }

    fun refresh() {
        sessions = repo.list()
        if (selectedId != null && selected == null) selectedId = null
        selected?.let { loadDetail(it) }
    }

    fun select(info: SessionInfo?) {
        selectedId = info?.id
        job = null
        busy = BusyState()
        message = ""
        info?.let { loadDetail(it) } ?: run { results = emptyList(); measurements = emptyList() }
    }

    private fun loadDetail(info: SessionInfo) {
        measurements = File(info.dir, SessionLayout.MEASUREMENTS_JSON).let {
            if (it.exists()) runCatching { MeasurementsFile.parse(it.readText()) }.getOrDefault(emptyList()) else emptyList()
        }
        results = File(info.dir, "results").listFiles { f -> f.isFile && !f.name.endsWith(".part") }?.sortedBy { it.name } ?: emptyList()
    }

    // ---- scanning --------------------------------------------------------------------------------------------

    fun prepareScan(roomName: String, tagSizeM: Double) {
        settings.tagSizeM = tagSizeM
        val dir = repo.create(roomName)
        val iso = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US).apply { timeZone = TimeZone.getTimeZone("UTC") }
        scanDir = dir
        scanMeta = SessionMeta(
            sessionId = dir.name, project = settings.project, roomName = roomName.ifBlank { "Scan" },
            createdAt = iso.format(Date()), deviceModel = "${Build.MANUFACTURER} ${Build.MODEL}",
            androidVersion = Build.VERSION.RELEASE ?: "", imageWidth = 0, imageHeight = 0, tagSizeM = tagSizeM,
        )
        screen = Screen.SCAN
    }

    fun toggleRecording() {
        val dir = scanDir ?: return
        val meta = scanMeta ?: return
        if (controller.state.value.recording) controller.stop() else controller.start(dir, meta)
    }

    fun finishScan() {
        val dir = scanDir
        controller.stop {
            // runs on the encoder thread after the last frame is on disk
            viewModelScope.launch(Dispatchers.Main) {
                screen = Screen.HOME
                if (dir != null) {
                    refresh()
                    val info = sessions.firstOrNull { it.dir == dir }
                    if (info != null) select(info) else dir.deleteRecursively()
                }
                scanDir = null
            }
        }
    }

    fun discardScan() {
        val dir = scanDir
        controller.stop {
            viewModelScope.launch(Dispatchers.Main) {
                screen = Screen.HOME
                dir?.let { repo.delete(it) }
                scanDir = null
                refresh()
            }
        }
    }

    // ---- session editing -------------------------------------------------------------------------------------

    fun saveMeasurements(list: List<LaserMeasurement>) {
        val info = selected ?: return
        File(info.dir, SessionLayout.MEASUREMENTS_JSON).writeText(MeasurementsFile.toJson(list))
        SessionInspector.saveState(info.dir, false, null) // changed data: it has to be uploaded again
        measurements = list
        refresh()
    }

    fun setTagSize(m: Double) {
        val info = selected ?: return
        val f = File(info.dir, SessionLayout.SESSION_JSON)
        if (!f.exists()) return
        val map = LinkedHashMap(Json.parse(f.readText()).asMap())
        map["tag_size_m"] = m
        f.writeText(Json.encode(map))
        settings.tagSizeM = m
        SessionInspector.saveState(info.dir, false, null)
        refresh()
    }

    fun delete(info: SessionInfo) {
        repo.delete(info.dir)
        if (selectedId == info.id) select(null)
        refresh()
    }

    // ---- PC connection ---------------------------------------------------------------------------------------

    fun pairFromQr(text: String): Boolean = try {
        savePc(Pairing.parseQr(text))
        true
    } catch (e: IllegalArgumentException) {
        pcStatus = e.message ?: "Invalid code"
        false
    }

    fun pairManual(host: String, port: String, token: String): Boolean = try {
        savePc(Pairing.build(host, port.trim().toIntOrNull() ?: 0, token))
        true
    } catch (e: IllegalArgumentException) {
        pcStatus = e.message ?: "Invalid input"
        false
    }

    private fun savePc(cfg: PcConfig) {
        settings.pc = cfg
        pc = cfg
        pcStatus = "Saved. Testing…"
        testConnection()
    }

    fun forgetPc() {
        settings.pc = null
        pc = null
        pcStatus = ""
    }

    fun testConnection() {
        val cfg = pc ?: run { pcStatus = "Not paired"; return }
        viewModelScope.launch(Dispatchers.IO) {
            val r = try {
                val h = PcClient(cfg, connectTimeoutMs = 4000, readTimeoutMs = 8000).health()
                // /health is open; also verify the token with an authenticated call
                PcClient(cfg, 4000, 8000).request("GET", "/v1/jobs").let {
                    if (it.status == 401) "Reached the PC but the token was rejected. Pair again."
                    else "Connected to ${h["name"] ?: cfg.host}" + (h["colmap"]?.let { c -> " · COLMAP: $c" } ?: "")
                }
            } catch (e: Exception) {
                "Cannot reach ${cfg.host}:${cfg.port} (${e.message ?: e.javaClass.simpleName}). Same Wi-Fi? Server running? Firewall?"
            }
            launch(Dispatchers.Main) { pcStatus = r }
        }
    }

    // ---- upload / process / download -------------------------------------------------------------------------

    private fun runBusy(label: String, block: suspend (PcClient) -> Unit) {
        val cfg = pc ?: run { message = "Pair with your PC first"; screen = Screen.PAIR; return }
        if (busy.running) return
        cancelRequested = false
        busy = BusyState(label, -1f, true)
        worker = viewModelScope.launch(Dispatchers.IO) {
            try {
                block(PcClient(cfg))
            } catch (e: UploadCancelled) {
                postMessage("Cancelled")
            } catch (e: InterruptedException) {
                postMessage("Cancelled")
            } catch (e: Exception) {
                postMessage(e.message ?: e.javaClass.simpleName)
            } finally {
                launch(Dispatchers.Main) { busy = BusyState(); refresh() }
            }
        }
    }

    private fun postMessage(m: String) {
        viewModelScope.launch(Dispatchers.Main) { message = m }
    }

    fun cancel() {
        cancelRequested = true
    }

    fun upload() {
        val info = selected ?: return
        runBusy("Uploading") { client ->
            Uploader(client).upload(info.dir, info.id, { cancelRequested }) { p ->
                val f = if (p.bytesTotal > 0) p.bytesDone.toFloat() / p.bytesTotal else -1f
                val label = if (p.phase == "preparing") "Checking files…" else "Uploading ${p.filesDone}/${p.filesTotal} files"
                viewModelScope.launch(Dispatchers.Main) { busy = BusyState(label, f, true) }
            }
            SessionInspector.saveState(info.dir, true, info.jobId)
            postMessage("Uploaded. Now tap Process on PC.")
        }
    }

    fun process(dense: Boolean) {
        val info = selected ?: return
        settings.dense = dense
        runBusy("Processing on PC") { client ->
            val reply = client.finish(info.id, linkedMapOf("dense" to dense))
            val jobId = reply["job_id"] as? String ?: reply["id"] as? String ?: throw IllegalStateException("The PC did not return a job id")
            SessionInspector.saveState(info.dir, true, jobId)
            val done = JobWatcher.wait(client, jobId, isCancelled = { cancelRequested }) { j ->
                viewModelScope.launch(Dispatchers.Main) {
                    job = j
                    busy = BusyState("PC: ${j.stage.ifBlank { j.state }} – ${j.message}", j.progress.toFloat().coerceIn(0f, 1f), true)
                }
            }
            viewModelScope.launch(Dispatchers.Main) { job = done }
            if (done.state == "done") postMessage("Finished. Tap Download results.")
            else postMessage("Processing ${done.state}: ${done.error ?: done.message}")
        }
        // allow the user to stop waiting: cancel() also asks the PC to stop
    }

    fun cancelJob() {
        val cfg = pc ?: return
        val id = job?.id ?: selected?.jobId ?: return
        cancelRequested = true
        viewModelScope.launch(Dispatchers.IO) { runCatching { PcClient(cfg).cancelJob(id) } }
    }

    fun download() {
        val info = selected ?: return
        runBusy("Downloading") { client ->
            val names = client.results(info.id)
            if (names.isEmpty()) throw IllegalStateException("No results on the PC yet")
            val dir = repo.resultsDir(info)
            val total = names.sumOf { it.second }.coerceAtLeast(1)
            var before = 0L
            for ((name, size) in names) {
                client.download(info.id, name, File(dir, name)) { done, _ ->
                    viewModelScope.launch(Dispatchers.Main) {
                        busy = BusyState("Downloading $name", (before + done).toFloat() / total, true)
                    }
                }
                before += size
            }
            postMessage("Downloaded ${names.size} files")
        }
    }

    override fun onCleared() {
        controller.close()
    }
}
