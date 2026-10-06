package com.scan3d.capture.data

import android.content.Context
import com.scan3d.capture.core.PcConfig

/** Small persistent settings (paired PC, defaults). SharedPreferences is plenty for this. */
class AppSettings(context: Context) {
    private val prefs = context.getSharedPreferences("scan3d", Context.MODE_PRIVATE)

    var pc: PcConfig?
        get() {
            val host = prefs.getString("pc_host", null) ?: return null
            val token = prefs.getString("pc_token", null) ?: return null
            return PcConfig(host, prefs.getInt("pc_port", 8765), token, prefs.getString("pc_name", "") ?: "")
        }
        set(v) {
            prefs.edit().apply {
                if (v == null) {
                    remove("pc_host"); remove("pc_port"); remove("pc_token"); remove("pc_name")
                } else {
                    putString("pc_host", v.host); putInt("pc_port", v.port)
                    putString("pc_token", v.token); putString("pc_name", v.name)
                }
            }.apply()
        }

    /** Printed AprilTag side length in metres (black square edge, not including the white border). */
    var tagSizeM: Double
        get() = prefs.getFloat("tag_size_m", 0.16f).toDouble()
        set(v) = prefs.edit().putFloat("tag_size_m", v.toFloat()).apply()

    var project: String
        get() = prefs.getString("project", "My scans") ?: "My scans"
        set(v) = prefs.edit().putString("project", v).apply()

    var dense: Boolean
        get() = prefs.getBoolean("dense", true)
        set(v) = prefs.edit().putBoolean("dense", v).apply()
}
