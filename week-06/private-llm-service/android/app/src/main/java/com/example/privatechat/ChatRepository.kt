package com.example.privatechat

import android.content.Context
import com.example.privatechat.api.*
import com.example.privatechat.net.*
import com.example.privatechat.storage.*
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

class ChatRepository(context: Context) {
    val store=ChatStore(context)
    private val vault=TokenVault(context)
    private val operation=Mutex()
    var connection: SavedConnection? = vault.load()
        private set
    var api: ApiClient? = connection?.let { saved -> runCatching { ApiClient(Endpoint.validate(saved.server,BuildConfig.FLAVOR=="lan"),saved.token) }.getOrNull() }
        private set
    suspend fun configure(server: String, token: String): Identity = operation.withLock {
        val origin=Endpoint.validate(server,BuildConfig.FLAVOR=="lan")
        val secret=if(token.isEmpty() && connection?.server==origin.toString()) requireNotNull(connection).token else token
        require(secret.matches(Regex("[A-Za-z0-9_-]{43}")))
        val candidate=ApiClient(origin,secret)
        val identity=candidate.me()
        val saved=SavedConnection(origin.toString(),identity.ownerId,identity.deviceId,secret)
        vault.save(saved)
        connection=saved; api=candidate
        identity
    }
    fun disconnect() { vault.clear(); connection=null; api=null }
    fun partition(): String = requireNotNull(connection).partition
    fun client(): ApiClient = requireNotNull(api)
    suspend fun refresh(): List<Conversation> {
        val saved=requireNotNull(connection)
        val client=client()
        val result=mutableListOf<Conversation>()
        var offset=0L
        do {
            val page=client.conversations(offset)
            result.addAll(page.items)
            require(result.size<=5000)
            offset=page.nextOffset ?: break
        } while(true)
        if(connection?.partition==saved.partition) store.replaceConversations(saved.partition,result)
        return result.sortedByDescending { it.updatedAt }
    }
    suspend fun history(conversation: String,isCurrent: ()->Boolean={true}): List<Message> {
        val saved=requireNotNull(connection)
        val client=client()
        val result=mutableListOf<Message>()
        var offset=0L
        do {
            val page=client.messages(conversation,offset)
            result.addAll(page.items)
            require(result.size<=10000)
            offset=page.nextOffset ?: break
        } while(true)
        if(connection?.partition==saved.partition && isCurrent()) store.cacheHistory(saved.partition,conversation,result)
        return result
    }
    suspend fun submit(conversation: Conversation, text: String,onPrepared: (String)->Unit={}): Snapshot = operation.withLock {
        val partition=partition()
        val pending=store.prepare(partition,conversation.id,SubmitRequest(text,conversation.revision).toJson().toString())
        onPrepared(partition)
        // The SQLite transaction above is durable BEFORE any network submission.
        val result=client().submit(conversation.id,pending.payload,pending.key)
        store.update(partition,conversation.id,result)
        result
    }
    suspend fun retry(conversation: String): Snapshot = operation.withLock {
        val partition=partition()
        val pending=requireNotNull(store.pending(partition,conversation))
        require(!pending.terminal && pending.phase!="dismissed")
        // GET recovery never creates a generation; only this explicit Retry may POST.
        val result=try { client().byKey(pending.key) } catch(error: ApiError) {
            if(error.status!=404) throw error
            client().submit(conversation,pending.payload,pending.key)
        }
        store.update(partition,conversation,result)
        result
    }
    suspend fun recover(conversation: String): Snapshot? {
        val partition=partition()
        val pending=store.pending(partition,conversation) ?: return null
        if(pending.phase=="dismissed") return null
        val snapshot=try { if(pending.snapshot!=null) client().snapshot(pending.snapshot.id) else client().byKey(pending.key) }
            catch(error: ApiError) { if(error.status==404) return null else throw error }
        store.update(partition,conversation,snapshot)
        return snapshot
    }
    suspend fun cancel(conversation: String): Snapshot? = operation.withLock {
        val partition=partition()
        val pending=store.pending(partition,conversation) ?: return@withLock null
        val resolved=try { pending.snapshot ?: client().byKey(pending.key) } catch(error: ApiError) {
            if(error.status==404) { store.dismiss(partition,conversation); return@withLock null } else throw error
        }
        val result=client().cancel(resolved.id)
        store.update(partition,conversation,result)
        result
    }
}
