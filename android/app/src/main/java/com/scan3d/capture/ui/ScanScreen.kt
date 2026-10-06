package com.scan3d.capture.ui

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import android.content.pm.PackageManager
import android.opengl.GLSurfaceView
import android.view.WindowManager
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import com.scan3d.capture.capture.ScanRenderer

private fun Context.findActivity(): Activity? {
    var c: Context? = this
    while (c is ContextWrapper) {
        if (c is Activity) return c
        c = c.baseContext
    }
    return null
}

@Composable
fun ScanScreen(vm: AppViewModel) {
    val context = LocalContext.current
    val activity = context.findActivity()
    val lifecycleOwner = LocalLifecycleOwner.current
    val ui by vm.controller.state.collectAsState()
    var hasCamera by remember {
        mutableStateOf(ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED)
    }
    var fatal by remember { mutableStateOf<String?>(null) }
    var confirmExit by remember { mutableStateOf(false) }
    val glView = remember {
        GLSurfaceView(context).apply {
            preserveEGLContextOnPause = true
            setEGLContextClientVersion(2)
            setEGLConfigChooser(8, 8, 8, 8, 16, 0)
            setRenderer(ScanRenderer(vm.controller))
            renderMode = GLSurfaceView.RENDERMODE_CONTINUOUSLY
            setWillNotDraw(false)
        }
    }
    val permissionLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { hasCamera = it }

    // keep the screen on while scanning
    DisposableEffect(Unit) {
        activity?.window?.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        onDispose { activity?.window?.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON) }
    }
    DisposableEffect(Unit) {
        if (!hasCamera) permissionLauncher.launch(Manifest.permission.CAMERA)
        onDispose { }
    }
    DisposableEffect(lifecycleOwner, hasCamera) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_RESUME -> if (hasCamera && activity != null) {
                    fatal = vm.controller.resume(activity)
                    glView.onResume()
                }
                Lifecycle.Event.ON_PAUSE -> {
                    glView.onPause()
                    vm.controller.pause()
                }
                else -> Unit
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            glView.onPause()
            vm.controller.pause()
        }
    }

    BackHandler {
        if (ui.frames > 0 || ui.recording) confirmExit = true else vm.discardScan()
    }

    Box(Modifier.fillMaxSize().background(Color.Black)) {
        AndroidView(factory = { glView }, modifier = Modifier.fillMaxSize())

        // top status bar
        Column(Modifier.align(Alignment.TopStart).padding(12.dp)) {
            val ok = ui.tracking == "TRACKING"
            Pill(
                text = if (ok) "Tracking OK" else "Tracking lost" + if (ui.trackingReason.isNotBlank() && ui.trackingReason != "NONE") " · ${niceReason(ui.trackingReason)}" else "",
                color = if (ok) Color(0xFF2E7D32) else Color(0xFFC62828),
            )
            Pill("${ui.frames} frames · ${"%.0f".format(ui.bytes / 1e6)} MB" + if (ui.dropped > 0) " · ${ui.dropped} skipped" else "", Color(0xAA000000))
            if (ui.imageSize.isNotEmpty()) Pill(ui.imageSize, Color(0xAA000000))
        }

        // centre hints
        Column(Modifier.align(Alignment.Center).padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            val hint = fatal ?: when {
                ui.warning.isNotBlank() -> ui.warning
                ui.recording && ui.tooFast -> "Move slower"
                ui.recording && ui.tracking != "TRACKING" -> "Tracking lost: go back to where it was working and move slowly"
                !ui.ready -> ui.message
                !ui.recording && ui.frames == 0 -> "Keep tags visible. Press Start, then walk slowly along the walls."
                else -> ""
            }
            if (hint.isNotBlank()) Pill(hint, if (ui.tooFast || fatal != null || ui.warning.isNotBlank()) Color(0xDDB71C1C) else Color(0xCC000000), big = true)
        }

        // bottom controls
        Row(
            Modifier.align(Alignment.BottomCenter).fillMaxWidth().padding(16.dp),
            horizontalArrangement = Arrangement.SpaceEvenly, verticalAlignment = Alignment.CenterVertically,
        ) {
            OutlinedButton(
                onClick = { if (ui.frames > 0 || ui.recording) confirmExit = true else vm.discardScan() },
                colors = ButtonDefaults.outlinedButtonColors(containerColor = Color(0x99000000), contentColor = Color.White),
            ) { Text("Back") }
            Button(
                onClick = { vm.toggleRecording() },
                enabled = ui.ready && fatal == null,
                colors = ButtonDefaults.buttonColors(containerColor = if (ui.recording) Color(0xFFC62828) else Color(0xFF2E7D32)),
            ) { Text(if (ui.recording) "Pause" else if (ui.frames > 0) "Resume" else "Start", Modifier.padding(horizontal = 20.dp, vertical = 6.dp)) }
            Button(onClick = { vm.finishScan() }, enabled = ui.frames > 0 || ui.recording) { Text("Finish") }
        }
    }

    if (confirmExit) {
        AlertDialog(
            onDismissRequest = { confirmExit = false },
            title = { Text("Leave the scan?") },
            text = { Text("Finish keeps the ${ui.frames} frames. Discard deletes them.") },
            confirmButton = { TextButton(onClick = { confirmExit = false; vm.finishScan() }) { Text("Finish & keep") } },
            dismissButton = {
                Row {
                    TextButton(onClick = { confirmExit = false; vm.discardScan() }) { Text("Discard") }
                    TextButton(onClick = { confirmExit = false }) { Text("Keep scanning") }
                }
            },
        )
    }
}

private fun niceReason(r: String) = when (r) {
    "INSUFFICIENT_LIGHT" -> "too dark"
    "EXCESSIVE_MOTION" -> "moving too fast"
    "INSUFFICIENT_FEATURES" -> "not enough texture"
    "CAMERA_UNAVAILABLE" -> "camera busy"
    else -> r.lowercase().replace('_', ' ')
}

@Composable
private fun Pill(text: String, color: Color, big: Boolean = false) {
    Text(
        text,
        color = Color.White,
        style = if (big) androidx.compose.material3.MaterialTheme.typography.titleMedium else androidx.compose.material3.MaterialTheme.typography.labelLarge,
        modifier = Modifier.padding(vertical = 3.dp).background(color, RoundedCornerShape(50)).padding(horizontal = 14.dp, vertical = 6.dp),
    )
}
