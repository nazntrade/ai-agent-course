package com.example.privatechat.net

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import java.net.URI

object Endpoint {
    fun validate(value: String, allowLan: Boolean): HttpUrl {
        require(value.length in 1..512 && value == value.trim())
        val uri = URI(value)
        require(uri.scheme in setOf("http", "https") && uri.rawUserInfo == null)
        require(uri.rawQuery == null && uri.rawFragment == null && uri.rawPath in setOf("", "/"))
        val url = requireNotNull(value.toHttpUrlOrNull())
        require(url.username.isEmpty() && url.password.isEmpty() && url.host.isNotEmpty())
        if (url.scheme == "http") require(allowLan && privateLiteral(url.host))
        return url.newBuilder().encodedPath("/").build()
    }

    private fun privateLiteral(host: String): Boolean {
        if (host == "::1" || host.matches(Regex("(?:fc|fd)[0-9a-f]{2}:[0-9a-f:]+"))) return true
        val parts = host.split('.')
        if (parts.size != 4 || parts.any { !it.matches(Regex("0|[1-9][0-9]{0,2}")) }) return false
        val numbers = parts.map { it.toInt() }
        if (numbers.any { it !in 0..255 }) return false
        return numbers[0] == 10 || numbers[0] == 127 ||
            (numbers[0] == 192 && numbers[1] == 168) || (numbers[0] == 172 && numbers[1] in 16..31)
    }
}
