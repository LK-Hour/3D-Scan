package com.scan3d.capture.ui

import android.content.ActivityNotFoundException
import android.content.Intent
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.text.KeyboardOptions
import androidx.core.content.FileProvider
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import androidx.activity.compose.rememberLauncherForActivityResult
import com.scan3d.capture.core.LaserMeasurement
import com.scan3d.capture.core.SessionInfo
import java.io.File
import java.text.DateFormat
import java.util.Date

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(vm: AppViewModel) {
    val wide = LocalConfiguration.current.screenWidthDp >= 600
    var showNew by remember { mutableStateOf(false) }
    val sel = vm.selected

    BackHandler(enabled = !wide && sel != null) { vm.select(null) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(if (!wide && sel != null) sel.roomName else "Scan3D") },
                actions = {
                    TextButton(onClick = { vm.screen = Screen.PAIR }) {
                        Text(if (vm.pc != null) "PC: ${vm.pc?.name?.ifBlank { vm.pc?.host } }" else "Pair PC")
                    }
                },
            )
        },
    ) { pad ->
        Row(Modifier.padding(pad).fillMaxSize()) {
            if (wide || sel == null) {
                Column(Modifier.then(if (wide) Modifier.width(360.dp) else Modifier.fillMaxWidth()).fillMaxHeight().padding(12.dp)) {
                    Button(onClick = { showNew = true }, modifier = Modifier.fillMaxWidth()) { Text("New scan") }
                    Spacer(Modifier.height(8.dp))
                    if (vm.sessions.isEmpty()) {
                        Text("No scans yet. Tap New scan, then walk slowly through the room.", style = MaterialTheme.typography.bodyMedium)
                    }
                    LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        items(vm.sessions, key = { it.id }) { s -> SessionRow(s, s.id == vm.selectedId) { vm.select(s) } }
                    }
                }
            }
            if (wide || sel != null) {
                Column(Modifier.weight(1f).fillMaxHeight().verticalScroll(rememberScrollState()).padding(12.dp)) {
                    if (sel == null) Text("Select a scan on the left", style = MaterialTheme.typography.bodyLarge)
                    else SessionDetail(vm, sel)
                }
            }
        }
    }

    if (showNew) NewScanDialog(vm.settings.tagSizeM, onDismiss = { showNew = false }) { name, tag ->
        showNew = false
        vm.prepareScan(name, tag)
    }
}

@Composable
private fun SessionRow(s: SessionInfo, selected: Boolean, onClick: () -> Unit) {
    Card(
        Modifier.fillMaxWidth().clickable(onClick = onClick),
        colors = CardDefaults.cardColors(containerColor = if (selected) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surfaceVariant),
    ) {
        Column(Modifier.padding(12.dp)) {
            Text(s.roomName, style = MaterialTheme.typography.titleMedium)
            Text(
                "${s.frameCount} frames · ${"%.0f".format(s.bytes / 1e6)} MB · " + DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT).format(Date(s.lastModified)),
                style = MaterialTheme.typography.bodySmall,
            )
            Text(
                when {
                    s.jobId != null -> "Sent to PC · processing started"
                    s.uploaded -> "Uploaded"
                    else -> "On this device only"
                },
                style = MaterialTheme.typography.labelMedium,
            )
        }
    }
}

@Composable
private fun NewScanDialog(tagSize: Double, onDismiss: () -> Unit, onStart: (String, Double) -> Unit) {
    var name by remember { mutableStateOf("") }
    var tag by remember { mutableStateOf("%.1f".format(tagSize * 100)) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("New scan") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(name, { name = it }, label = { Text("Room / object name") }, singleLine = true)
                OutlinedTextField(
                    tag, { tag = it }, label = { Text("Printed tag size (cm, black square)") }, singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                )
                Text("Place 8+ printed tags on walls/floor at different heights before you start. Measure two or more tag-to-tag distances with the laser afterwards.", style = MaterialTheme.typography.bodySmall)
            }
        },
        confirmButton = {
            TextButton(onClick = {
                val cm = tag.replace(',', '.').toDoubleOrNull()
                if (cm != null && cm in 2.0..100.0) onStart(name.ifBlank { "Scan" }, cm / 100.0)
            }) { Text("Open camera") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun SessionDetail(vm: AppViewModel, s: SessionInfo) {
    val context = LocalContext.current
    var dense by remember { mutableStateOf(vm.settings.dense) }
    var confirmDelete by remember { mutableStateOf(false) }
    var tagText by remember(s.id, s.tagSizeM) { mutableStateOf("%.1f".format(s.tagSizeM * 100)) }

    Text(s.roomName, style = MaterialTheme.typography.headlineSmall)
    Text("${s.frameCount} frames · ${"%.0f".format(s.bytes / 1e6)} MB · ${s.id}", style = MaterialTheme.typography.bodySmall)
    Spacer(Modifier.height(12.dp))

    // ---- tag size + laser measurements (this is what gives real-world scale)
    Text("Scale", style = MaterialTheme.typography.titleMedium)
    Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedTextField(
            tagText, { tagText = it }, label = { Text("Tag size (cm)") }, singleLine = true, modifier = Modifier.width(150.dp),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
        )
        OutlinedButton(onClick = { tagText.replace(',', '.').toDoubleOrNull()?.takeIf { it in 2.0..100.0 }?.let { vm.setTagSize(it / 100.0) } }) { Text("Save") }
    }
    MeasurementsEditor(vm.measurements) { vm.saveMeasurements(it) }
    Spacer(Modifier.height(12.dp))
    HorizontalDivider()
    Spacer(Modifier.height(12.dp))

    // ---- PC steps
    Text("PC", style = MaterialTheme.typography.titleMedium)
    if (vm.pc == null) {
        Text("Pair with your PC first (top right).", style = MaterialTheme.typography.bodyMedium)
    }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
        Switch(dense, { dense = it })
        Text(if (dense) "Dense model (slow, best)" else "Fast preview (sparse only)")
    }
    Spacer(Modifier.height(8.dp))
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Button(onClick = { vm.upload() }, enabled = !vm.busy.running && vm.pc != null) { Text(if (s.uploaded) "Upload again" else "1. Upload") }
        Button(onClick = { vm.process(dense) }, enabled = !vm.busy.running && vm.pc != null && s.uploaded) { Text("2. Process") }
        Button(onClick = { vm.download() }, enabled = !vm.busy.running && vm.pc != null && s.jobId != null) { Text("3. Download") }
    }
    if (vm.busy.running) {
        Spacer(Modifier.height(8.dp))
        Text(vm.busy.label, style = MaterialTheme.typography.bodyMedium)
        if (vm.busy.progress >= 0f) LinearProgressIndicator(progress = { vm.busy.progress }, modifier = Modifier.fillMaxWidth())
        else LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
        Row {
            TextButton(onClick = { vm.cancel() }) { Text("Stop waiting") }
            if (vm.job?.finished == false) TextButton(onClick = { vm.cancelJob() }) { Text("Cancel on PC") }
        }
    }
    vm.job?.let { j ->
        if (!vm.busy.running && j.state == "failed") Text("Processing failed: ${j.error ?: j.message}", color = MaterialTheme.colorScheme.error)
    }
    if (vm.message.isNotBlank()) {
        Spacer(Modifier.height(6.dp))
        Text(vm.message, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.primary)
    }

    // ---- results
    if (vm.results.isNotEmpty()) {
        Spacer(Modifier.height(12.dp))
        HorizontalDivider()
        Spacer(Modifier.height(12.dp))
        Text("Results (${vm.results.size})", style = MaterialTheme.typography.titleMedium)
        vm.results.find { it.name == "report.md" }?.let { f ->
            Card(Modifier.fillMaxWidth().padding(vertical = 6.dp)) { Text(f.readText().take(3000), Modifier.padding(12.dp), style = MaterialTheme.typography.bodySmall) }
        }
        vm.results.forEach { f ->
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                Text("${f.name}  (${"%.1f".format(f.length() / 1e6)} MB)", style = MaterialTheme.typography.bodySmall)
                TextButton(onClick = { openFile(context, f) }) { Text("Open / share") }
            }
        }
        Text(
            "Best viewing: copy the PC's results folder (or these files) into Blender, MeshLab or any GLB/PLY viewer.",
            style = MaterialTheme.typography.bodySmall,
        )
    }

    Spacer(Modifier.height(16.dp))
    TextButton(onClick = { confirmDelete = true }, enabled = !vm.busy.running) { Text("Delete scan from this device", color = MaterialTheme.colorScheme.error) }
    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text("Delete this scan?") },
            text = { Text("Removes ${s.frameCount} frames from the device. Copies already on the PC are kept.") },
            confirmButton = { TextButton(onClick = { confirmDelete = false; vm.delete(s) }) { Text("Delete") } },
            dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun MeasurementsEditor(list: List<LaserMeasurement>, onChange: (List<LaserMeasurement>) -> Unit) {
    var a by remember { mutableStateOf("") }
    var b by remember { mutableStateOf("") }
    var m by remember { mutableStateOf("") }
    var check by remember { mutableStateOf(false) }
    Text("Laser distances between tag centres (metres)", style = MaterialTheme.typography.bodyMedium)
    list.forEachIndexed { i, d ->
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
            Text("Tag ${d.tagA} ↔ Tag ${d.tagB}: ${"%.3f".format(d.meters)} m" + if (d.use == "check") "  (check)" else "")
            TextButton(onClick = { onChange(list.filterIndexed { j, _ -> j != i }) }) { Text("Remove") }
        }
    }
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
        val num = KeyboardOptions(keyboardType = KeyboardType.Decimal)
        OutlinedTextField(a, { a = it.filter(Char::isDigit) }, label = { Text("Tag A") }, singleLine = true, modifier = Modifier.width(80.dp), keyboardOptions = num)
        OutlinedTextField(b, { b = it.filter(Char::isDigit) }, label = { Text("Tag B") }, singleLine = true, modifier = Modifier.width(80.dp), keyboardOptions = num)
        OutlinedTextField(m, { m = it }, label = { Text("Metres") }, singleLine = true, modifier = Modifier.width(100.dp), keyboardOptions = num)
        FilterChip(check, { check = !check }, label = { Text("check only") })
        OutlinedButton(onClick = {
            val ta = a.toIntOrNull()
            val tb = b.toIntOrNull()
            val dist = m.replace(',', '.').toDoubleOrNull()
            if (ta != null && tb != null && ta != tb && dist != null && dist > 0.05 && dist < 100) {
                onChange(list + LaserMeasurement(ta, tb, dist, use = if (check) "check" else "fit"))
                a = ""; b = ""; m = ""
            }
        }) { Text("Add") }
    }
    Text("Use 'check only' for one distance you do NOT want used for scaling; the report shows how well it agrees.", style = MaterialTheme.typography.bodySmall)
}

private fun openFile(context: android.content.Context, f: File) {
    val uri = FileProvider.getUriForFile(context, "${context.packageName}.files", f)
    val mime = when (f.extension.lowercase()) {
        "glb" -> "model/gltf-binary"
        "json" -> "application/json"
        "md" -> "text/markdown"
        else -> "application/octet-stream"
    }
    val intent = Intent(Intent.ACTION_VIEW).setDataAndType(uri, mime).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    try {
        context.startActivity(Intent.createChooser(intent, f.name))
    } catch (e: ActivityNotFoundException) {
        val send = Intent(Intent.ACTION_SEND).setType(mime).putExtra(Intent.EXTRA_STREAM, uri).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        context.startActivity(Intent.createChooser(send, f.name))
    }
}

@Composable
fun PairScreen(vm: AppViewModel) {
    var host by remember { mutableStateOf(vm.pc?.host ?: "") }
    var port by remember { mutableStateOf((vm.pc?.port ?: 8765).toString()) }
    var token by remember { mutableStateOf("") }
    val scanner = rememberLauncherForActivityResult(ScanContract()) { r ->
        r.contents?.let { if (vm.pairFromQr(it)) vm.screen = Screen.HOME }
    }
    BackHandler { vm.screen = Screen.HOME }
    Scaffold { pad ->
        Column(Modifier.padding(pad).padding(16.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("Pair with your PC", style = MaterialTheme.typography.headlineSmall)
            Text("On the PC run:  python -m scanpc serve   (a QR code opens in your browser). PC and device must be on the same Wi-Fi/hotspot.")
            Button(onClick = {
                scanner.launch(ScanOptions().setDesiredBarcodeFormats(ScanOptions.QR_CODE).setPrompt("Scan the QR code shown by the PC").setBeepEnabled(false).setOrientationLocked(false))
            }, modifier = Modifier.fillMaxWidth()) { Text("Scan QR code") }
            HorizontalDivider()
            Text("…or type it in", style = MaterialTheme.typography.titleMedium)
            OutlinedTextField(host, { host = it }, label = { Text("PC address (e.g. 192.168.1.20)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(port, { port = it.filter(Char::isDigit) }, label = { Text("Port") }, singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number))
            OutlinedTextField(token, { token = it }, label = { Text("Token (python -m scanpc token)") }, modifier = Modifier.fillMaxWidth())
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { vm.pairManual(host, port, token) }) { Text("Save & test") }
                OutlinedButton(onClick = { vm.testConnection() }, enabled = vm.pc != null) { Text("Test connection") }
                if (vm.pc != null) TextButton(onClick = { vm.forgetPc() }) { Text("Forget PC") }
            }
            if (vm.pcStatus.isNotBlank()) Text(vm.pcStatus, color = if (vm.pcStatus.startsWith("Connected")) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.error)
            TextButton(onClick = { vm.screen = Screen.HOME }) { Text("Done") }
        }
    }
}
