package com.example.privatechat

import android.app.Instrumentation
import android.content.Intent
import android.net.LocalServerSocket
import android.net.LocalSocket
import android.net.LocalSocketAddress
import android.os.Bundle
import android.os.Process
import android.view.View
import android.widget.EditText
import android.widget.TextView
import com.example.privatechat.net.Endpoint
import com.example.privatechat.storage.TokenVault
import com.example.privatechat.storage.VaultState
import java.util.concurrent.atomic.AtomicBoolean
import org.json.JSONObject
import java.io.DataInputStream
import java.io.DataOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Test APK only: never part of the main application's dex. */
class ProbeInstrumentation : Instrumentation() {
    private var socketName = ""
    private var mode = "probe"
    override fun onCreate(arguments: Bundle?) {
        super.onCreate(arguments)
        socketName = arguments?.getString("socket") ?: ""
        mode = arguments?.getString("mode") ?: "probe"
        start()
    }
    private fun write(socket: LocalSocket, value: JSONObject) {
        val bytes = value.toString().toByteArray(Charsets.UTF_8)
        require(bytes.size in 1..4096)
        val output = DataOutputStream(socket.outputStream)
        output.writeInt(bytes.size); output.write(bytes); output.flush()
    }
    private fun read(socket: LocalSocket): JSONObject {
        val input = DataInputStream(socket.inputStream)
        val count = input.readInt()
        require(count in 1..4096)
        val bytes = ByteArray(count); input.readFully(bytes)
        return JSONObject(String(bytes, Charsets.UTF_8))
    }
    private fun waitUi(seconds: Long=30,condition: ()->Boolean) {
        val deadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(seconds)
        while(System.nanoTime()<deadline) {
            val ready=AtomicBoolean(false)
            runOnMainSync { ready.set(condition()) }
            if(ready.get()) return
            Thread.sleep(50)
        }
        error("UI operation deadline")
    }
    private fun pair(host: LocalSocket,outcome: Bundle) {
        val expectedPackage=if(mode in setOf("e2e","persistence")) "com.example.privatechat.e2e" else "com.example.privatechat"
        require(targetContext.packageName==expectedPackage)
        require(read(host).let { it.length()==1 && it.getString("kind")=="check_configuration" })
        val vault=TokenVault(targetContext)
        val configuration=vault.state()
        write(host,JSONObject().put("kind","configuration_state").put("state",configuration.name))
        require(configuration!=VaultState.UNREADABLE)
        val saved=if(configuration==VaultState.CONFIGURED) vault.load() else null
        if(mode=="persistence") {
            requireNotNull(saved)
            val expected=read(host)
            require(expected.keys().asSequence().toSet()==setOf("kind","device_id") && expected.getString("kind")=="verify_persistence")
            require(saved.device==expected.getString("device_id"))
            val restarted=startActivitySync(Intent().setClassName(expectedPackage,MainActivity::class.java.name).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
            val checks=UiE2E(this,restarted) { error("No E2E control in persistence phase") }.verifyPersistence()
            write(host,JSONObject().put("kind","persistence_pass").put("checks",checks))
            outcome.putString("persistence_status","PASS")
            return
        }
        if(saved!=null) {
            require(mode=="pairing")
            outcome.putString("pairing_status","EXISTING_CONFIGURATION_PRESERVED")
            return
        }
        val payload=read(host)
        require(payload.keys().asSequence().toSet()==setOf("kind","server_url","token","expected_device_id"))
        require(payload.getString("kind")=="pairing")
        val server=Endpoint.validate(payload.getString("server_url"),BuildConfig.FLAVOR=="lan").toString()
        val token=payload.getString("token")
        val expectedDevice=payload.getString("expected_device_id")
        require(token.matches(Regex("[A-Za-z0-9_-]{43}")) && expectedDevice.matches(Regex("[a-f0-9]{32}")))
        val activity=startActivitySync(Intent().setClassName(expectedPackage,MainActivity::class.java.name).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity
        waitForIdleSync()
        runOnMainSync {
            if(!activity.findViewById<View>(R.id.settings_scroll).isShown) check(activity.findViewById<View>(R.id.settings).performClick())
        }
        waitUi { activity.findViewById<View>(R.id.settings_scroll).isShown && activity.findViewById<View>(R.id.save_settings).isEnabled }
        runOnMainSync {
            activity.findViewById<EditText>(R.id.server_input).setText(server)
            activity.findViewById<EditText>(R.id.token_input).setText(token)
            check(activity.findViewById<View>(R.id.save_settings).performClick())
        }
        waitUi {
            activity.findViewById<TextView>(R.id.device_id).text.toString()=="Device: $expectedDevice" &&
                activity.findViewById<TextView>(R.id.connection_status).text.toString()=="Connected" &&
                activity.findViewById<View>(R.id.busy).visibility==View.GONE
        }
        runOnMainSync {
            check(activity.findViewById<EditText>(R.id.token_input).text.isEmpty())
            check(activity.findViewById<View>(R.id.settings_done).performClick())
        }
        waitUi { activity.findViewById<View>(R.id.conversations_screen).isShown }
        write(host,JSONObject().put("kind","paired").put("device_id",expectedDevice))
        require(read(host).let { it.length()==1 && it.getString("kind")=="confirmed" })
        outcome.putString("pairing_status","PAIRED")
        outcome.putString("device_id",expectedDevice)
        if(mode=="e2e") {
            host.soTimeout=10000
            val checks=UiE2E(this,activity) { action ->
                write(host,JSONObject().put("kind","control").put("action",action))
                read(host).also { check(it.getString("kind")=="control_result" && it.getString("action")==action) }
            }.run()
            write(host,JSONObject().put("kind","e2e_done").put("checks",checks))
            require(read(host).let { it.length()==1 && it.getString("kind")=="e2e_confirmed" })
            outcome.putString("full_ui_e2e","PASS")
        }
    }
    override fun onStart() {
        var server: LocalServerSocket? = null
        var pendingHost: LocalSocket? = null
        var negativeFailed = false
        val finished = CountDownLatch(1)
        val outcome = Bundle()
        var stage = "socket_create"
        try {
            DomainChecks.run(targetContext)
            outcome.putString("domain_checks", "PASS")
            require(socketName.matches(Regex("privatechat_[a-f0-9]{24}")))
            require(mode in setOf("probe","pairing","e2e","persistence"))
            require(Process.myUid() >= 10000)
            server = LocalServerSocket(socketName)
            val activeServer = server
            Thread {
                if (!finished.await(if(mode=="e2e") 1500 else 60, TimeUnit.SECONDS)) {
                    try { activeServer.close() } catch (_: Exception) { }
                }
            }.start()
            stage = "ordinary_uid_accept"
            val negativeFinished = CountDownLatch(1)
            var negativeRejected = false
            Thread {
                try {
                    LocalSocket().use { ordinary ->
                        ordinary.connect(LocalSocketAddress(socketName, LocalSocketAddress.Namespace.ABSTRACT))
                        ordinary.soTimeout = 5000
                        negativeRejected = read(ordinary).optString("code") == "ordinary_uid_rejected"
                    }
                } catch (_: Exception) {
                    negativeFailed = true
                    try { activeServer.close() } catch (_: Exception) { }
                } finally { negativeFinished.countDown() }
            }.start()
            var rejectedOrdinary = false
            while (!rejectedOrdinary || pendingHost == null) {
                val accepted = activeServer.accept()
                var retained = false
                try {
                    accepted.soTimeout = 5000
                    val uid = accepted.peerCredentials.uid
                    if (uid == Process.myUid() && uid >= 10000) {
                        require(!rejectedOrdinary)
                        // No input is read from a rejected application UID.
                        write(accepted, JSONObject().put("code", "ordinary_uid_rejected"))
                        rejectedOrdinary = true
                    } else {
                        require((uid == 0 || uid == 2000) && pendingHost == null)
                        // An early allowed host waits without any payload read
                        // until the ordinary application rejection is proven.
                        pendingHost = accepted
                        retained = true
                    }
                } finally {
                    if (!retained) accepted.close()
                }
            }
            stage = "ordinary_uid_confirmation"
            require(negativeFinished.await(5, TimeUnit.SECONDS) && negativeRejected)
            stage = "host_uid_accept"
            requireNotNull(pendingHost).use { host ->
                host.soTimeout = 5000
                val uid = host.peerCredentials.uid
                require(uid == 0 || uid == 2000)
                stage = "public_handshake"
                write(host, JSONObject().put("kind", "probe_ready").put("peer_uid", uid)
                    .put("ordinary_uid", Process.myUid()).put("ordinary_uid_rejected", true))
                stage = "public_payload"
                val message = read(host)
                require(message.length() == 1 && message.getString("kind") == "nonsecret_probe")
                write(host, JSONObject().put("kind", "probe_pass").put("peer_uid", uid).put("ordinary_uid_rejected", true))
                if(mode!="probe") {
                    stage="settings_pairing"
                    pair(host,outcome)
                }
            }
            outcome.putString("probe_status", "PASS")
            finish(0, outcome)
        } catch (_: Exception) {
            outcome.putString("probe_status", "FAIL")
            outcome.putString("probe_stage", stage)
            if (negativeFailed) outcome.putString("negative_probe_error", "SOCKET_OPERATION_FAILED")
            finish(1, outcome)
        } finally {
            finished.countDown()
            try { pendingHost?.close() } catch (_: Exception) { }
            try { server?.close() } catch (_: Exception) { }
        }
    }
}
