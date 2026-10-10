package com.example.privatechat.api

import org.json.JSONArray
import org.json.JSONObject

// Generated from the actual OpenAPI document. Do not edit manually.
private fun JSONObject.fields(required: Set<String>) { require(keys().asSequence().toSet() == required) }
private fun JSONObject.stringValue(key: String): String = get(key).let { require(it is String); it }
private fun JSONObject.longValue(key: String): Long = get(key).let { require(it is Int || it is Long); (it as Number).toLong() }
private fun JSONObject.doubleValue(key: String): Double = get(key).let { require(it is Number); it.toDouble().also { n -> require(n.isFinite()) } }
private fun JSONObject.booleanValue(key: String): Boolean = get(key).let { require(it is Boolean); it }
private fun JSONObject.objectValue(key: String): JSONObject = get(key).let { require(it is JSONObject); it }
private fun JSONObject.arrayValue(key: String): JSONArray = get(key).let { require(it is JSONArray); it }

data class Conversation(
    val id: String,
    val title: String,
    val revision: Long,
    val activeJobId: String?,
    val createdAt: Double,
    val updatedAt: Double
) {
    fun toJson(): JSONObject = JSONObject()
        .put("id", id)
        .put("title", title)
        .put("revision", revision)
        .put("active_job_id", if (activeJobId == null) JSONObject.NULL else activeJobId)
        .put("created_at", createdAt)
        .put("updated_at", updatedAt)
    companion object {
        fun fromJson(value: JSONObject): Conversation {
            value.fields(setOf("id", "title", "revision", "active_job_id", "created_at", "updated_at"))
            return Conversation(
                id = value.stringValue("id"),
                title = value.stringValue("title"),
                revision = value.longValue("revision"),
                activeJobId = if (value.isNull("active_job_id")) null else value.stringValue("active_job_id"),
                createdAt = value.doubleValue("created_at"),
                updatedAt = value.doubleValue("updated_at")
            )
        }
    }
}

data class ConversationPage(
    val items: List<Conversation>,
    val nextOffset: Long?
) {
    fun toJson(): JSONObject = JSONObject()
        .put("items", JSONArray(items.map { it.toJson() }))
        .put("next_offset", if (nextOffset == null) JSONObject.NULL else nextOffset)
    companion object {
        fun fromJson(value: JSONObject): ConversationPage {
            value.fields(setOf("items", "next_offset"))
            return ConversationPage(
                items = value.arrayValue("items").let { list -> (0 until list.length()).map { index -> Conversation.fromJson(list.getJSONObject(index)) } },
                nextOffset = if (value.isNull("next_offset")) null else value.longValue("next_offset")
            )
        }
    }
}

data class CreateConversation(
    val title: String
) {
    init { require(title.length >= 1); require(title.length <= 80) }
    fun toJson(): JSONObject = JSONObject()
        .put("title", title)
    companion object {
        fun fromJson(value: JSONObject): CreateConversation {
            value.fields(setOf("title"))
            return CreateConversation(
                title = value.stringValue("title")
            )
        }
    }
}

data class ErrorDetail(
    val code: String
) {
    fun toJson(): JSONObject = JSONObject()
        .put("code", code)
    companion object {
        fun fromJson(value: JSONObject): ErrorDetail {
            value.fields(setOf("code"))
            return ErrorDetail(
                code = value.stringValue("code")
            )
        }
    }
}

data class ErrorResponse(
    val error: ErrorDetail
) {
    fun toJson(): JSONObject = JSONObject()
        .put("error", error.toJson())
    companion object {
        fun fromJson(value: JSONObject): ErrorResponse {
            value.fields(setOf("error"))
            return ErrorResponse(
                error = ErrorDetail.fromJson(value.objectValue("error"))
            )
        }
    }
}

data class Identity(
    val ownerId: String,
    val deviceId: String
) {
    fun toJson(): JSONObject = JSONObject()
        .put("owner_id", ownerId)
        .put("device_id", deviceId)
    companion object {
        fun fromJson(value: JSONObject): Identity {
            value.fields(setOf("owner_id", "device_id"))
            return Identity(
                ownerId = value.stringValue("owner_id"),
                deviceId = value.stringValue("device_id")
            )
        }
    }
}

data class Message(
    val id: Long,
    val role: String,
    val text: String,
    val jobId: String,
    val completionStatus: String,
    val createdAt: Double
) {
    init { require(role in setOf("user", "assistant")) }
    fun toJson(): JSONObject = JSONObject()
        .put("id", id)
        .put("role", role)
        .put("text", text)
        .put("job_id", jobId)
        .put("completion_status", completionStatus)
        .put("created_at", createdAt)
    companion object {
        fun fromJson(value: JSONObject): Message {
            value.fields(setOf("id", "role", "text", "job_id", "completion_status", "created_at"))
            return Message(
                id = value.longValue("id"),
                role = value.stringValue("role"),
                text = value.stringValue("text"),
                jobId = value.stringValue("job_id"),
                completionStatus = value.stringValue("completion_status"),
                createdAt = value.doubleValue("created_at")
            )
        }
    }
}

data class MessagePage(
    val items: List<Message>,
    val nextOffset: Long?
) {
    fun toJson(): JSONObject = JSONObject()
        .put("items", JSONArray(items.map { it.toJson() }))
        .put("next_offset", if (nextOffset == null) JSONObject.NULL else nextOffset)
    companion object {
        fun fromJson(value: JSONObject): MessagePage {
            value.fields(setOf("items", "next_offset"))
            return MessagePage(
                items = value.arrayValue("items").let { list -> (0 until list.length()).map { index -> Message.fromJson(list.getJSONObject(index)) } },
                nextOffset = if (value.isNull("next_offset")) null else value.longValue("next_offset")
            )
        }
    }
}

data class RenameConversation(
    val title: String,
    val expectedRevision: Long
) {
    init { require(title.length >= 1); require(title.length <= 80); require(expectedRevision >= 1L) }
    fun toJson(): JSONObject = JSONObject()
        .put("title", title)
        .put("expected_revision", expectedRevision)
    companion object {
        fun fromJson(value: JSONObject): RenameConversation {
            value.fields(setOf("title", "expected_revision"))
            return RenameConversation(
                title = value.stringValue("title"),
                expectedRevision = value.longValue("expected_revision")
            )
        }
    }
}

data class ServiceStatus(
    val accepting: Boolean,
    val mode: String,
    val waitingJobs: Long,
    val activeJobs: Long,
    val queueCapacity: Long,
    val rateBurst: Double,
    val rateRefillPerMinute: Double,
    val outputCap: Long,
    val promptCap: Long,
    val pairingCleanupRequired: Boolean
) {
    fun toJson(): JSONObject = JSONObject()
        .put("accepting", accepting)
        .put("mode", mode)
        .put("waiting_jobs", waitingJobs)
        .put("active_jobs", activeJobs)
        .put("queue_capacity", queueCapacity)
        .put("rate_burst", rateBurst)
        .put("rate_refill_per_minute", rateRefillPerMinute)
        .put("output_cap", outputCap)
        .put("prompt_cap", promptCap)
        .put("pairing_cleanup_required", pairingCleanupRequired)
    companion object {
        fun fromJson(value: JSONObject): ServiceStatus {
            value.fields(setOf("accepting", "mode", "waiting_jobs", "active_jobs", "queue_capacity", "rate_burst", "rate_refill_per_minute", "output_cap", "prompt_cap", "pairing_cleanup_required"))
            return ServiceStatus(
                accepting = value.booleanValue("accepting"),
                mode = value.stringValue("mode"),
                waitingJobs = value.longValue("waiting_jobs"),
                activeJobs = value.longValue("active_jobs"),
                queueCapacity = value.longValue("queue_capacity"),
                rateBurst = value.doubleValue("rate_burst"),
                rateRefillPerMinute = value.doubleValue("rate_refill_per_minute"),
                outputCap = value.longValue("output_cap"),
                promptCap = value.longValue("prompt_cap"),
                pairingCleanupRequired = value.booleanValue("pairing_cleanup_required")
            )
        }
    }
}

data class Snapshot(
    val id: String,
    val conversationId: String,
    val state: String,
    val stateVersion: Long,
    val partialContent: String,
    val errorCode: String?,
    val finishReason: String?,
    val historyTruncated: Boolean,
    val promptTokens: Long?,
    val promptBudget: Long?,
    val queuePosition: Long?,
    val createdAt: Double,
    val updatedAt: Double
) {
    fun toJson(): JSONObject = JSONObject()
        .put("id", id)
        .put("conversation_id", conversationId)
        .put("state", state)
        .put("state_version", stateVersion)
        .put("partial_content", partialContent)
        .put("error_code", if (errorCode == null) JSONObject.NULL else errorCode)
        .put("finish_reason", if (finishReason == null) JSONObject.NULL else finishReason)
        .put("history_truncated", historyTruncated)
        .put("prompt_tokens", if (promptTokens == null) JSONObject.NULL else promptTokens)
        .put("prompt_budget", if (promptBudget == null) JSONObject.NULL else promptBudget)
        .put("queue_position", if (queuePosition == null) JSONObject.NULL else queuePosition)
        .put("created_at", createdAt)
        .put("updated_at", updatedAt)
    companion object {
        fun fromJson(value: JSONObject): Snapshot {
            value.fields(setOf("id", "conversation_id", "state", "state_version", "partial_content", "error_code", "finish_reason", "history_truncated", "prompt_tokens", "prompt_budget", "queue_position", "created_at", "updated_at"))
            return Snapshot(
                id = value.stringValue("id"),
                conversationId = value.stringValue("conversation_id"),
                state = value.stringValue("state"),
                stateVersion = value.longValue("state_version"),
                partialContent = value.stringValue("partial_content"),
                errorCode = if (value.isNull("error_code")) null else value.stringValue("error_code"),
                finishReason = if (value.isNull("finish_reason")) null else value.stringValue("finish_reason"),
                historyTruncated = value.booleanValue("history_truncated"),
                promptTokens = if (value.isNull("prompt_tokens")) null else value.longValue("prompt_tokens"),
                promptBudget = if (value.isNull("prompt_budget")) null else value.longValue("prompt_budget"),
                queuePosition = if (value.isNull("queue_position")) null else value.longValue("queue_position"),
                createdAt = value.doubleValue("created_at"),
                updatedAt = value.doubleValue("updated_at")
            )
        }
    }
}

data class SubmitRequest(
    val text: String,
    val expectedRevision: Long
) {
    init { require(text.length >= 1); require(text.length <= 32000); require(expectedRevision >= 1L) }
    fun toJson(): JSONObject = JSONObject()
        .put("text", text)
        .put("expected_revision", expectedRevision)
    companion object {
        fun fromJson(value: JSONObject): SubmitRequest {
            value.fields(setOf("text", "expected_revision"))
            return SubmitRequest(
                text = value.stringValue("text"),
                expectedRevision = value.longValue("expected_revision")
            )
        }
    }
}
