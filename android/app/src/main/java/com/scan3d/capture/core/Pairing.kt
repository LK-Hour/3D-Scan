package com.scan3d.capture.core

/** Connection details for the PC server (from the pairing QR or typed in by hand). */
data class PcConfig(val host: String, val port: Int, val token: String, val name: String = "") {
    val baseUrl: String
        get() = "http://" + (if (host.contains(':') && !host.startsWith("[")) "[$host]" else host) + ":$port"
}

object Pairing {
    private val HOST_RE = Regex("^[A-Za-z0-9.\\-:\\[\\]_]{1,253}$")

    /** QR payload produced by `python -m scanpc serve`: {"v":1,"name":..,"host":..,"port":..,"token":..} */
    fun parseQr(text: String): PcConfig {
        val m = try {
            Json.parse(text.trim()).asMap()
        } catch (e: IllegalArgumentException) {
            throw IllegalArgumentException("This QR code is not a Scan3D pairing code")
        }
        if (m["v"].asLong() != 1L) throw IllegalArgumentException("Unsupported pairing code version; update the app or the PC software")
        return build(m["host"].asString(), m["port"].asLong().toInt(), m["token"].asString(), m["name"].asString())
    }

    fun build(host: String, port: Int, token: String, name: String = ""): PcConfig {
        val h = host.trim()
        val t = token.trim()
        if (!HOST_RE.matches(h)) throw IllegalArgumentException("Invalid PC address")
        if (port !in 1..65535) throw IllegalArgumentException("Invalid port")
        if (t.length < 32) throw IllegalArgumentException("The token looks too short (copy it from `python -m scanpc token`)")
        return PcConfig(h, port, t, name.trim())
    }
}
