package com.example.privatechat.net

import com.example.privatechat.api.Snapshot
import org.json.JSONObject
import java.math.BigInteger

data class SnapshotEvent(val sequence: String, val snapshot: Snapshot)

class SseParser(private val job: String, initialVersion: Long = -1, initialSequence: String? = null) {
    private var event = "snapshot"
    private var id: String? = null
    private val data = StringBuilder()
    private var version = initialVersion
    private var sequence = initialSequence?.let { BigInteger(it) }
    fun line(value: String): SnapshotEvent? {
        require(value.length <= 65536 && !value.contains('\u0000'))
        if (value.isEmpty()) {
            if (data.isEmpty()) { reset(); return null }
            require(event in setOf("snapshot", "reset", "terminal"))
            val currentId = requireNotNull(id)
            require(currentId.matches(Regex("[0-9]{1,20}")))
            val currentSeq = BigInteger(currentId)
            val snapshot = Snapshot.fromJson(JSONObject(data.toString().removeSuffix("\n")))
            require(snapshot.id == job && snapshot.stateVersion >= 1)
            val previousSequence = sequence
            if ((previousSequence != null && currentSeq <= previousSequence) || snapshot.stateVersion < version) { reset(); return null }
            version = snapshot.stateVersion
            sequence = currentSeq
            reset()
            return SnapshotEvent(currentId, snapshot)
        }
        if (value.startsWith(':')) return null
        val split = value.indexOf(':')
        val name = if (split < 0) value else value.substring(0, split)
        val content = if (split < 0) "" else value.substring(split + 1).removePrefix(" ")
        when (name) {
            "event" -> event = content
            "id" -> { require(content.matches(Regex("[0-9]{1,20}"))); id = content }
            "data" -> { require(data.length + content.length + 1 <= 131072); data.append(content).append('\n') }
        }
        return null
    }
    private fun reset() { event = "snapshot"; id = null; data.setLength(0) }
}
