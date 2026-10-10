package com.example.privatechat

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.example.privatechat.api.*
import com.example.privatechat.net.ApiError
import com.example.privatechat.storage.PendingRequest
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import java.util.concurrent.atomic.AtomicLong

data class ChatState(
    val server: String="", val device: String="", val configured: Boolean=false,
    val conversations: List<Conversation> = emptyList(), val selected: Conversation?=null,
    val messages: List<Message> = emptyList(), val pending: PendingRequest?=null,
    val busy: Boolean=false, val error: String?=null, val status: String="Not connected",
    val retryAt: Long=0, val screen: String="conversations", val draft: String="",val draftVersion: Long=0
)

class ChatModel(application: Application) : AndroidViewModel(application) {
    val repository=ChatRepository(application)
    private val mutable=MutableStateFlow(repository.connection?.let { saved ->
        ChatState(server=saved.server,device=saved.device,configured=repository.api!=null,
            conversations=repository.store.conversations(saved.partition),status="Saved connection")
    } ?: ChatState(screen="settings"))
    val state=mutable.asStateFlow()
    private var observer: Job?=null
    private var rateTimer: Job?=null
    private val epoch=AtomicLong(0)
    private fun error(failure: Exception) {
        val message=when(failure) {
            is ApiError -> when(failure.code) {
                "unauthorized" -> "Device token is invalid or revoked. Update Settings."
                "rate_limited" -> "Please wait before retrying."
                "queue_full" -> "The server queue is full. Retry later."
                "owner_active_limit" -> "Two requests are already waiting or running."
                "revision_conflict" -> "The conversation changed. Refresh before sending a new request."
                "request_too_large", "context_limit" -> "This message is too large for the model context."
                "idempotency_conflict" -> "The saved request conflicts with the server."
                "not_found" -> "This conversation or request is no longer available."
                else -> "Request failed. Refresh or retry the saved request."
            }
            is IllegalArgumentException -> "Check the server address, token and input limits."
            else -> "Connection interrupted. Your saved request can be retried safely."
        }
        val retryDeadline=if(failure is ApiError && failure.retryAfter!=null) System.currentTimeMillis()+failure.retryAfter*1000 else mutable.value.retryAt
        mutable.update { it.copy(error=message,
            retryAt=retryDeadline,
            status=if(failure is ApiError && failure.status==401) "Authorization required" else it.status) }
        if(failure is ApiError && failure.retryAfter!=null) {
            rateTimer?.cancel()
            rateTimer=viewModelScope.launch {
                delay((retryDeadline-System.currentTimeMillis()).coerceAtLeast(0))
                mutable.update { if(it.retryAt==retryDeadline) it.copy(retryAt=0) else it }
            }
        }
    }
    private fun action(block: suspend () -> Unit) {
        if(mutable.value.busy) return
        mutable.update { it.copy(busy=true,error=null) }
        viewModelScope.launch {
            try { withContext(Dispatchers.IO) { block() } }
            catch(cancelled: CancellationException) { throw cancelled }
            catch(failure: Exception) { error(failure) }
            finally { mutable.update { it.copy(busy=false) } }
        }
    }
    fun screen(value: String) { require(value in setOf("conversations","chat","settings")); mutable.update { it.copy(screen=value,error=null) } }
    fun configure(server: String, token: String) = action {
        epoch.incrementAndGet(); rateTimer?.cancel()
        observer?.cancel()
        val identity=repository.configure(server,token)
        mutable.update { it.copy(server=requireNotNull(repository.connection).server,device=identity.deviceId,
            configured=true,selected=null,messages=emptyList(),pending=null,conversations=emptyList(),status="Connected",draft="",draftVersion=0,retryAt=0) }
        refreshInternal()
    }
    fun disconnect() {
        if(mutable.value.busy) return
        epoch.incrementAndGet(); rateTimer?.cancel(); observer?.cancel(); repository.disconnect()
        mutable.value=ChatState(screen="settings")
    }
    private suspend fun refreshInternal() {
        val partition=repository.partition()
        val items=repository.refresh()
        if(repository.connection?.partition!=partition) return
        mutable.update { old -> old.copy(conversations=items,selected=old.selected?.let { selected -> items.find { it.id==selected.id } },status="Connected") }
    }
    fun refresh() = action { refreshInternal() }
    fun create(title: String) = action {
        val conversation=repository.client().create(title)
        repository.store.cache(repository.partition(),conversation)
        refreshInternal(); openInternal(conversation)
    }
    fun rename(title: String) = action {
        val selected=requireNotNull(mutable.value.selected)
        val latest=syncConversation(selected.id)
        val renamed=repository.client().rename(selected.id,title,latest.revision)
        repository.store.cache(repository.partition(),renamed)
        mutable.update { it.copy(selected=renamed) }
        refreshInternal()
    }
    fun delete() = action {
        val selected=requireNotNull(mutable.value.selected)
        observer?.cancel()
        val latest=syncConversation(selected.id)
        repository.client().delete(selected.id,latest.revision)
        repository.store.removeConversation(repository.partition(),selected.id)
        epoch.incrementAndGet()
        mutable.update { it.copy(selected=null,messages=emptyList(),pending=null,screen="conversations",draft="",draftVersion=0) }
        refreshInternal()
    }
    fun open(conversation: Conversation) = action { openInternal(conversation) }
    private suspend fun openInternal(conversation: Conversation) {
        val currentEpoch=epoch.incrementAndGet()
        observer?.cancel()
        val partition=repository.partition()
        val draft=repository.store.draft(partition,conversation.id)
        mutable.update { it.copy(selected=conversation,screen="chat",messages=repository.store.history(partition,conversation.id),
            pending=repository.store.pending(partition,conversation.id),draft=draft.text,draftVersion=draft.version) }
        val latest=repository.client().conversation(conversation.id)
        val history=repository.history(conversation.id) { epoch.get()==currentEpoch && repository.connection?.partition==partition }
        val recovered=repository.recover(conversation.id)
        if(epoch.get()!=currentEpoch || repository.connection?.partition!=partition) return
        mutable.update { it.copy(selected=latest,messages=history,pending=repository.store.pending(partition,conversation.id),status="Connected") }
        if(recovered!=null && !requireNotNull(repository.store.pending(partition,conversation.id)).terminal) observe(recovered)
    }
    fun send(text: String) = action {
        val selected=requireNotNull(mutable.value.selected)
        val draftVersion=mutable.value.draftVersion
        try {
            val latest=syncConversation(selected.id)
            val snapshot=repository.submit(latest,text) { partition ->
                val cleared=repository.store.clearSentDraft(partition,selected.id,text,draftVersion)
                if(cleared!=null && repository.connection?.partition==partition) mutable.update {
                    if(it.selected?.id==selected.id && it.draftVersion==draftVersion) it.copy(draft=cleared.text,draftVersion=cleared.version) else it
                }
            }
            publish(snapshot)
            syncConversation(selected.id)
            observe(snapshot)
        } finally { updatePending(selected.id) }
    }
    fun retry() = action {
        require(System.currentTimeMillis()>=mutable.value.retryAt)
        val selected=requireNotNull(mutable.value.selected)
        val snapshot=repository.retry(selected.id)
        publish(snapshot); syncConversation(selected.id); observe(snapshot)
    }
    fun stop() = action {
        val selected=requireNotNull(mutable.value.selected)
        val currentEpoch=epoch.get()
        val partition=repository.partition()
        observer?.cancel()
        repository.cancel(selected.id)?.let { publish(it) }
        syncConversation(selected.id)
        updatePending(selected.id)
        val history=repository.history(selected.id) { epoch.get()==currentEpoch && repository.connection?.partition==partition }
        if(epoch.get()==currentEpoch && repository.connection?.partition==partition) mutable.update {
            if(it.selected?.id==selected.id) it.copy(messages=history) else it
        }
    }
    fun dismiss() = action {
        val selected=requireNotNull(mutable.value.selected)
        // Resolve the original key first: never forget a request that was admitted.
        val snapshot=repository.recover(selected.id)
        if(snapshot!=null && !requireNotNull(repository.store.pending(repository.partition(),selected.id)).terminal) {
            publish(snapshot); observe(snapshot)
        } else {
            repository.store.dismiss(repository.partition(),selected.id); updatePending(selected.id)
        }
    }
    private fun updatePending(conversation: String) {
        val partition=repository.partition()
        val pending=repository.store.pending(partition,conversation)
        mutable.update { if(it.selected?.id==conversation) it.copy(pending=pending) else it }
    }
    private suspend fun syncConversation(identifier: String): Conversation {
        val currentEpoch=epoch.get()
        val partition=repository.partition()
        val client=repository.client()
        val latest=client.conversation(identifier)
        if(repository.connection?.partition==partition && epoch.get()==currentEpoch) {
            repository.store.cache(partition,latest)
            mutable.update { if(it.selected?.id==identifier) it.copy(selected=latest) else it }
        }
        return latest
    }
    private fun publish(snapshot: Snapshot,partition: String=repository.partition(),expectedEpoch: Long=epoch.get()) {
        if(epoch.get()!=expectedEpoch || repository.connection?.partition!=partition) return
        repository.store.update(partition,snapshot.conversationId,snapshot)
        if(repository.connection?.partition==partition) updatePending(snapshot.conversationId)
    }
    private fun observe(first: Snapshot) {
        observer?.cancel()
        val saved=requireNotNull(repository.connection)
        val client=repository.client()
        val observedEpoch=epoch.get()
        observer=viewModelScope.launch(Dispatchers.IO) {
            var current=first
            var cursor: String?=null
            while(isActive && epoch.get()==observedEpoch && repository.connection?.partition==saved.partition && mutable.value.selected?.id==current.conversationId) {
                try {
                    if(current.state in setOf("completed","cancelled","failed","interrupted")) {
                        val history=repository.history(current.conversationId) { isActive && epoch.get()==observedEpoch && repository.connection?.partition==saved.partition }
                        val latest=client.conversation(current.conversationId)
                        if(isActive && epoch.get()==observedEpoch && repository.connection?.partition==saved.partition) mutable.update {
                            if(it.selected?.id==current.conversationId) it.copy(messages=history,selected=latest,status="Connected") else it
                        }
                        break
                    }
                    try {
                        client.events(current,cursor) { event ->
                            if(isActive && epoch.get()==observedEpoch && repository.connection?.partition==saved.partition && mutable.value.selected?.id==event.snapshot.conversationId) {
                                current=event.snapshot; cursor=event.sequence; publish(event.snapshot,saved.partition,observedEpoch)
                            }
                        }
                    } catch(cancelled: CancellationException) { throw cancelled }
                    catch(failure: Exception) {
                        if(failure is ApiError && failure.status==401) throw failure
                        if(isActive && epoch.get()==observedEpoch && repository.connection?.partition==saved.partition) mutable.update { it.copy(status="Reconnecting; checking saved request") }
                    }
                    if(current.state !in setOf("completed","cancelled","failed","interrupted")) {
                        delay(1500)
                        current=client.snapshot(current.id); publish(current,saved.partition,observedEpoch)
                    }
                } catch(cancelled: CancellationException) { throw cancelled }
                catch(failure: Exception) { if(isActive && epoch.get()==observedEpoch && repository.connection?.partition==saved.partition) error(failure); break }
            }
        }
    }
    fun updateDraft(text: String) {
        val connection=repository.connection ?: return
        val selected=mutable.value.selected ?: return
        if(text==mutable.value.draft) return
        val saved=repository.store.saveDraft(connection.partition,selected.id,text)
        if(repository.connection?.partition==connection.partition) mutable.update {
            if(it.selected?.id==selected.id) it.copy(draft=saved.text,draftVersion=saved.version) else it
        }
    }
    override fun onCleared() { epoch.incrementAndGet(); rateTimer?.cancel(); observer?.cancel(); repository.store.close(); super.onCleared() }
}
