package com.example.privatechat.net

import com.example.privatechat.api.*
import kotlinx.coroutines.suspendCancellableCoroutine
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import okio.Buffer
import org.json.JSONObject
import java.io.IOException
import java.net.Proxy
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

class ApiError(val status: Int, val code: String, val retryAfter: Long? = null) : IOException("Request failed")

class ApiClient(val origin: HttpUrl, private val token: String) {
    private val client = OkHttpClient.Builder().proxy(Proxy.NO_PROXY).followRedirects(false).followSslRedirects(false)
        .connectTimeout(10, TimeUnit.SECONDS).readTimeout(20, TimeUnit.SECONDS).writeTimeout(20, TimeUnit.SECONDS).build()
    private fun request(method: String, path: List<String>, body: String? = null, key: String? = null, query: Map<String, String> = emptyMap()): Request {
        val url = origin.newBuilder().apply { path.forEach { addPathSegment(it) }; query.forEach { (key, value) -> addQueryParameter(key, value) } }.build()
        val builder = Request.Builder().url(url).header("Authorization", "Bearer $token").header("Accept", "application/json")
        if (key != null) builder.header("Idempotency-Key", key)
        val content = body?.toRequestBody("application/json; charset=utf-8".toMediaType())
            ?: if (method == "POST") ByteArray(0).toRequestBody(null) else null
        return builder.method(method, content).build()
    }
    private suspend fun response(request: Request): Response = suspendCancellableCoroutine { continuation ->
        val call = client.newCall(request)
        continuation.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, error: IOException) { if (continuation.isActive) continuation.resumeWithException(IOException("Connection interrupted")) }
            override fun onResponse(call: Call, response: Response) {
                if (continuation.isActive) continuation.resume(response, onCancellation = { _, value, _ -> value.close() }) else response.close()
            }
        })
    }
    private fun boundedBody(response: Response): String {
        val source = requireNotNull(response.body).source()
        val buffer = Buffer()
        while (true) {
            val count = source.read(buffer, 8192)
            if (count < 0) break
            require(buffer.size <= 4194304)
        }
        return Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(buffer.readByteArray())).toString()
    }
    private fun error(response: Response): ApiError {
        val code = try { ErrorResponse.fromJson(JSONObject(boundedBody(response))).error.code.takeIf { it.matches(Regex("[a-z0-9_]{1,64}")) } ?: "request_failed" }
            catch (_: Exception) { "request_failed" }
        val retry = response.header("Retry-After")?.toLongOrNull()?.takeIf { it in 1..3600 }
        return ApiError(response.code, code, retry)
    }
    private suspend fun json(request: Request, statuses: Set<Int> = setOf(200)): JSONObject = response(request).use { result ->
        if (result.code !in statuses) throw error(result)
        require(result.header("Content-Type")?.substringBefore(';') == "application/json")
        JSONObject(boundedBody(result))
    }
    suspend fun me() = Identity.fromJson(json(request("GET", listOf("v1", "me"))))
    suspend fun status() = ServiceStatus.fromJson(json(request("GET", listOf("v1", "status"))))
    suspend fun conversations(offset: Long = 0) = ConversationPage.fromJson(json(request("GET", listOf("v1", "conversations"), query = mapOf("offset" to "$offset", "limit" to "50"))))
    suspend fun conversation(id: String) = Conversation.fromJson(json(request("GET", listOf("v1", "conversations", id))))
    suspend fun create(title: String) = Conversation.fromJson(json(request("POST", listOf("v1", "conversations"), CreateConversation(title).toJson().toString()), setOf(201)))
    suspend fun rename(id: String, title: String, revision: Long) = Conversation.fromJson(json(request("PATCH", listOf("v1", "conversations", id), RenameConversation(title, revision).toJson().toString())))
    suspend fun delete(id: String, revision: Long) { response(request("DELETE", listOf("v1", "conversations", id), query = mapOf("expected_revision" to "$revision"))).use { if (it.code != 204) throw error(it) } }
    suspend fun messages(id: String, offset: Long = 0) = MessagePage.fromJson(json(request("GET", listOf("v1", "conversations", id, "messages"), query = mapOf("offset" to "$offset", "limit" to "20"))))
    suspend fun submit(id: String, payload: String, key: String) = Snapshot.fromJson(json(request("POST", listOf("v1", "conversations", id, "requests"), payload, key), setOf(200, 202)))
    suspend fun byKey(key: String) = Snapshot.fromJson(json(request("GET", listOf("v1", "requests", "by-key", key))))
    suspend fun snapshot(id: String) = Snapshot.fromJson(json(request("GET", listOf("v1", "requests", id, "snapshot"))))
    suspend fun cancel(id: String) = Snapshot.fromJson(json(request("POST", listOf("v1", "requests", id, "cancel"))))
    suspend fun events(snapshot: Snapshot, cursor: String?, update: (SnapshotEvent) -> Unit) = suspendCancellableCoroutine<Unit> { continuation ->
        val request = request("GET", listOf("v1", "requests", snapshot.id, "events")).newBuilder().header("Accept", "text/event-stream")
        if (cursor != null) request.header("Last-Event-ID", cursor)
        val call = client.newCall(request.build())
        continuation.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, error: IOException) { if (continuation.isActive) continuation.resumeWithException(IOException("Stream interrupted")) }
            override fun onResponse(call: Call, response: Response) {
                response.use {
                    try {
                        if (response.code != 200) throw error(response)
                        require(response.header("Content-Type")?.substringBefore(';') == "text/event-stream")
                        val parser = SseParser(snapshot.id, snapshot.stateVersion, cursor)
                        val source = requireNotNull(response.body).source()
                        val line = Buffer()
                        while (continuation.isActive && !source.exhausted()) {
                            val byte = source.readByte()
                            if (byte.toInt() == 10) {
                                val text = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                                    .decode(ByteBuffer.wrap(line.readByteArray())).toString().removeSuffix("\r")
                                parser.line(text)?.let(update)
                            } else { line.writeByte(byte.toInt()); require(line.size <= 65536) }
                        }
                        require(line.size == 0L)
                        if (continuation.isActive) continuation.resume(Unit)
                    } catch (failure: Exception) { if (continuation.isActive) continuation.resumeWithException(failure) }
                }
            }
        })
    }
}
