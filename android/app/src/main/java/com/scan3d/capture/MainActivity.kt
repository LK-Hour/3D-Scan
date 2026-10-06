package com.scan3d.capture

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.runtime.Composable
import com.scan3d.capture.ui.AppViewModel
import com.scan3d.capture.ui.HomeScreen
import com.scan3d.capture.ui.PairScreen
import com.scan3d.capture.ui.ScanScreen
import com.scan3d.capture.ui.Screen

class MainActivity : ComponentActivity() {
    private val vm: AppViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = if (isSystemInDarkTheme()) darkColorScheme() else lightColorScheme()) {
                Surface { AppRoot(vm) }
            }
        }
    }
}

@Composable
private fun AppRoot(vm: AppViewModel) {
    when (vm.screen) {
        Screen.HOME -> HomeScreen(vm)
        Screen.SCAN -> ScanScreen(vm)
        Screen.PAIR -> PairScreen(vm)
    }
}
