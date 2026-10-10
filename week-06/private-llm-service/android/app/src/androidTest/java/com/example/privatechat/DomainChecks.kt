package com.example.privatechat

import android.content.Context
import com.example.privatechat.api.*
import com.example.privatechat.net.Endpoint
import com.example.privatechat.net.SseParser
import com.example.privatechat.storage.ChatStore
import com.example.privatechat.storage.TokenVault
import com.example.privatechat.storage.VaultState
import com.example.privatechat.storage.SavedConnection
import org.json.JSONObject
import java.util.UUID

object DomainChecks {
    private fun snapshot(version: Long,text: String,id: String="job") = Snapshot(id,"conv","running",version,text,null,null,false,null,null,null,0.0,1.0)
    private fun fails(action: ()->Unit) { check(runCatching(action).isFailure) }
    fun run(context: Context) {
        val preferenceName="owned-vault-state-"+UUID.randomUUID()
        val scratch=context.getSharedPreferences(preferenceName,Context.MODE_PRIVATE)
        try {
            check(TokenVault.classify(scratch.all) { error("Empty state must not decrypt") }==VaultState.EMPTY)
            check(scratch.edit().putString("server","public-fixture").commit())
            check(TokenVault.classify(scratch.all) { error("Partial state must not decrypt") }==VaultState.UNREADABLE)
            listOf("owner","device","iv","ciphertext").forEach { check(scratch.edit().putString(it,"public-fixture").commit()) }
            val original=scratch.all.toMap()
            check(TokenVault.classify(scratch.all) { error("Controlled unreadable encrypted payload") }==VaultState.UNREADABLE)
            check(scratch.all==original)
            check(TokenVault.classify(scratch.all) { SavedConnection("http://10.0.2.2/","fixture","a".repeat(32),"x".repeat(43)) }==VaultState.CONFIGURED)
        } finally { context.deleteSharedPreferences(preferenceName) }
        val allowed=listOf("http://10.0.2.2:8791","http://192.168.1.2","http://172.31.0.2","https://example.com")
        allowed.forEach { check(Endpoint.validate(it,true).encodedPath=="/") }
        listOf("http://example.com","http://8.8.8.8","http://172.32.0.1","http://192.168.1.2/path",
            "http://user:secret@10.0.2.2","http://10.0.2.2?query=1","https://example.com#fragment",
            "ftp://10.0.2.2","http://999.0.0.1").forEach { url -> fails { Endpoint.validate(url,true) } }
        fails { Endpoint.validate("http://10.0.2.2",false) }
        val parser=SseParser("job")
        fun event(sequence: String,value: Snapshot,event: String="snapshot") = run {
            parser.line(": heartbeat")
            parser.line("id: $sequence"); parser.line("event: $event")
            parser.line("data: ${value.toJson()}"); parser.line("")
        }
        check(event("1",snapshot(1,"long original content"))?.snapshot?.partialContent=="long original content")
        check(event("2",snapshot(2,"short"),"reset")?.snapshot?.partialContent=="short")
        check(event("2",snapshot(2,"duplicate"))==null)
        check(event("3",snapshot(1,"stale"))==null)
        fails { event("4",snapshot(3,"wrong job","other")) }
        fails { SseParser("job").line("id: -1") }
        fails { SseParser("job").line("data: "+"x".repeat(131073)) }
        fails { Identity.fromJson(JSONObject("{\"owner_id\":123,\"device_id\":\"x\"}")) }
        fails { Identity.fromJson(JSONObject("{\"owner_id\":\"x\",\"device_id\":\"y\",\"extra\":true}")) }
        val name="owned-domain-"+UUID.randomUUID()+".sqlite3"
        try {
            val payload=SubmitRequest("Persist exact payload 😀",1).toJson().toString()
            val store=ChatStore(context,name)
            val request=store.prepare("serverA|ownerA","conv",payload)
            val draftA=store.saveDraft("serverA|ownerA","conv","Draft A")
            store.saveDraft("serverA|ownerA","convB","Draft B")
            check(request.payload==payload && request.key.matches(Regex("[A-Za-z0-9_-]{16,128}")))
            store.close()
            val reopened=ChatStore(context,name)
            val persisted=requireNotNull(reopened.pending("serverA|ownerA","conv"))
            check(persisted.key==request.key && persisted.payload==payload)
            check(reopened.pending("serverB|ownerA","conv")==null)
            check(reopened.pending("serverA|ownerB","conv")==null)
            check(reopened.draft("serverA|ownerA","conv").text=="Draft A")
            check(reopened.draft("serverA|ownerA","convB").text=="Draft B")
            check(reopened.draft("serverB|ownerA","conv").text.isEmpty())
            check(reopened.draft("serverA|ownerB","conv").text.isEmpty())
            val newerDraft=reopened.saveDraft("serverA|ownerA","conv","Draft A")
            check(newerDraft.version>draftA.version)
            check(reopened.clearSentDraft("serverA|ownerA","conv","Draft A",draftA.version)==null)
            check(reopened.draft("serverA|ownerA","conv")==newerDraft)
            fails { reopened.prepare("serverA|ownerA","conv","different") }
            reopened.update("serverA|ownerA","conv",snapshot(2,"new"))
            reopened.update("serverA|ownerA","conv",snapshot(1,"old"))
            check(reopened.pending("serverA|ownerA","conv")?.snapshot?.partialContent=="new")
            reopened.removeConversation("serverA|ownerA","conv")
            check(reopened.pending("serverA|ownerA","conv")==null)
            check(reopened.draft("serverA|ownerA","conv").text.isEmpty())
            check(reopened.draft("serverA|ownerA","convB").text=="Draft B")
            reopened.close()
        } finally { check(context.deleteDatabase(name)) }
    }
}
