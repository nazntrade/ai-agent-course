package com.example.privatechat.storage

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteOpenHelper
import android.database.sqlite.SQLiteDatabase
import com.example.privatechat.api.*
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

data class PendingRequest(val conversation: String, val key: String, val payload: String, val snapshot: Snapshot?, val phase: String) {
    val terminal: Boolean get() = snapshot?.state in setOf("completed", "cancelled", "failed", "interrupted")
}
data class DraftEntry(val text: String,val version: Long)

class ChatStore(context: Context, databaseName: String="chat-cache.sqlite3") : SQLiteOpenHelper(context, databaseName, null, 2) {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE conversations (partition TEXT NOT NULL, id TEXT NOT NULL, json TEXT NOT NULL, PRIMARY KEY(partition,id))")
        db.execSQL("CREATE TABLE histories (partition TEXT NOT NULL, conversation TEXT NOT NULL, json TEXT NOT NULL, PRIMARY KEY(partition,conversation))")
        db.execSQL("CREATE TABLE pending (partition TEXT NOT NULL, conversation TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, snapshot TEXT, phase TEXT NOT NULL, PRIMARY KEY(partition,conversation), UNIQUE(partition,key))")
        createDrafts(db)
    }
    private fun createDrafts(db: SQLiteDatabase) { db.execSQL("CREATE TABLE drafts (partition TEXT NOT NULL, conversation TEXT NOT NULL, text TEXT NOT NULL, version INTEGER NOT NULL, PRIMARY KEY(partition,conversation))") }
    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if(oldVersion==1 && newVersion==2) createDrafts(db) else error("Unsupported cache schema")
    }
    private fun values(vararg entries: Pair<String, String?>) = ContentValues().apply { entries.forEach { (key,value) -> if (value == null) putNull(key) else put(key,value) } }
    @Synchronized fun replaceConversations(partition: String, items: List<Conversation>) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.delete("conversations", "partition=?", arrayOf(partition))
            items.forEach { db.insertOrThrow("conversations", null, values("partition" to partition, "id" to it.id, "json" to it.toJson().toString())) }
            val retained = items.map { it.id }.toSet()
            val removed = mutableListOf<String>()
            db.query("pending", arrayOf("conversation"), "partition=?", arrayOf(partition), null,null,null).use { cursor ->
                while(cursor.moveToNext()) if (cursor.getString(0) !in retained) removed.add(cursor.getString(0))
            }
            removed.forEach { removeConversation(partition,it) }
            val oldHistories=mutableListOf<String>()
            db.query("histories", arrayOf("conversation"), "partition=?", arrayOf(partition), null,null,null).use { cursor ->
                while(cursor.moveToNext()) if(cursor.getString(0) !in retained) oldHistories.add(cursor.getString(0))
            }
            oldHistories.forEach { db.delete("histories","partition=? AND conversation=?",arrayOf(partition,it)) }
            val oldDrafts=mutableListOf<String>()
            db.query("drafts",arrayOf("conversation"),"partition=?",arrayOf(partition),null,null,null).use { cursor -> while(cursor.moveToNext()) if(cursor.getString(0) !in retained) oldDrafts.add(cursor.getString(0)) }
            oldDrafts.forEach { db.delete("drafts","partition=? AND conversation=?",arrayOf(partition,it)) }
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
    }
    @Synchronized fun conversations(partition: String): List<Conversation> = readableDatabase.query("conversations", arrayOf("json"), "partition=?", arrayOf(partition),null,null,null).use { cursor ->
        buildList { while(cursor.moveToNext()) add(Conversation.fromJson(JSONObject(cursor.getString(0)))) }.sortedByDescending { it.updatedAt }
    }
    @Synchronized fun cache(partition: String, conversation: Conversation) {
        writableDatabase.insertWithOnConflict("conversations",null,values("partition" to partition,"id" to conversation.id,"json" to conversation.toJson().toString()),SQLiteDatabase.CONFLICT_REPLACE)
    }
    @Synchronized fun history(partition: String, conversation: String): List<Message> {
        readableDatabase.query("histories",arrayOf("json"),"partition=? AND conversation=?",arrayOf(partition,conversation),null,null,null).use { cursor ->
            if (!cursor.moveToFirst()) return emptyList()
            val array=JSONArray(cursor.getString(0))
            return (0 until array.length()).map { Message.fromJson(array.getJSONObject(it)) }
        }
    }
    @Synchronized fun cacheHistory(partition: String, conversation: String, messages: List<Message>) {
        writableDatabase.insertWithOnConflict("histories",null,values("partition" to partition,"conversation" to conversation,"json" to JSONArray(messages.map { it.toJson() }).toString()),SQLiteDatabase.CONFLICT_REPLACE)
    }
    @Synchronized fun prepare(partition: String, conversation: String, payload: String): PendingRequest {
        pending(partition,conversation)?.let { require(it.terminal || it.phase=="dismissed") }
        val request=PendingRequest(conversation,UUID.randomUUID().toString(),payload,null,"prepared")
        check(writableDatabase.insertWithOnConflict("pending",null,values("partition" to partition,"conversation" to conversation,"key" to request.key,"payload" to payload,"snapshot" to null,"phase" to request.phase),SQLiteDatabase.CONFLICT_REPLACE)!=-1L)
        return requireNotNull(pending(partition,conversation))
    }
    @Synchronized fun pending(partition: String, conversation: String): PendingRequest? = readableDatabase.query("pending",arrayOf("key","payload","snapshot","phase"),"partition=? AND conversation=?",arrayOf(partition,conversation),null,null,null).use { cursor ->
        if (!cursor.moveToFirst()) null else PendingRequest(conversation,cursor.getString(0),cursor.getString(1),
            if(cursor.isNull(2)) null else Snapshot.fromJson(JSONObject(cursor.getString(2))),cursor.getString(3))
    }
    @Synchronized fun pendingAll(partition: String): List<PendingRequest> {
        val ids=readableDatabase.query("pending",arrayOf("conversation"),"partition=?",arrayOf(partition),null,null,null).use { cursor -> buildList { while(cursor.moveToNext()) add(cursor.getString(0)) } }
        return ids.mapNotNull { pending(partition,it) }.filter { !it.terminal && it.phase!="dismissed" }
    }
    @Synchronized fun update(partition: String, conversation: String, snapshot: Snapshot) {
        val previous=pending(partition,conversation) ?: return
        require(snapshot.conversationId==conversation)
        if(previous.snapshot!=null && (previous.snapshot.id!=snapshot.id || previous.snapshot.stateVersion>snapshot.stateVersion)) return
        writableDatabase.update("pending",values("snapshot" to snapshot.toJson().toString(),"phase" to "submitted"),"partition=? AND conversation=? AND key=?",arrayOf(partition,conversation,previous.key))
    }
    @Synchronized fun dismiss(partition: String, conversation: String) {
        val previous=pending(partition,conversation) ?: return
        require(previous.snapshot==null || previous.terminal)
        writableDatabase.update("pending",values("phase" to "dismissed"),"partition=? AND conversation=?",arrayOf(partition,conversation))
    }
    @Synchronized fun draft(partition: String,conversation: String): DraftEntry = readableDatabase.query("drafts",arrayOf("text","version"),"partition=? AND conversation=?",arrayOf(partition,conversation),null,null,null).use { cursor -> if(cursor.moveToFirst()) DraftEntry(cursor.getString(0),cursor.getLong(1)) else DraftEntry("",0) }
    @Synchronized fun saveDraft(partition: String,conversation: String,text: String): DraftEntry {
        require(text.length<=32000)
        val saved=DraftEntry(text,draft(partition,conversation).version+1)
        val row=values("partition" to partition,"conversation" to conversation,"text" to text).apply { put("version",saved.version) }
        check(writableDatabase.insertWithOnConflict("drafts",null,row,SQLiteDatabase.CONFLICT_REPLACE)!=-1L)
        return saved
    }
    @Synchronized fun clearSentDraft(partition: String,conversation: String,sent: String,expectedVersion: Long): DraftEntry? {
        val current=draft(partition,conversation)
        if(current.text!=sent || current.version!=expectedVersion) return null
        return saveDraft(partition,conversation,"")
    }
    @Synchronized fun removeConversation(partition: String, conversation: String) {
        val db=writableDatabase
        db.beginTransaction()
        try {
            db.delete("conversations","partition=? AND id=?",arrayOf(partition,conversation))
            db.delete("histories","partition=? AND conversation=?",arrayOf(partition,conversation))
            db.delete("pending","partition=? AND conversation=?",arrayOf(partition,conversation))
            db.delete("drafts","partition=? AND conversation=?",arrayOf(partition,conversation))
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
    }
}
