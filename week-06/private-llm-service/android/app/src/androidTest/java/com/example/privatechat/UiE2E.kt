package com.example.privatechat

import android.app.Activity
import android.app.Application
import android.app.Instrumentation
import android.content.Intent
import android.os.Bundle
import android.view.View
import android.view.inputmethod.InputMethodManager
import android.view.accessibility.AccessibilityNodeInfo
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.lifecycle.ViewModelProvider
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicReference
import java.util.concurrent.TimeUnit

/** Real controls against the owned gateway; fault controls exist only in this test APK. */
class UiE2E(private val instrumentation: Instrumentation, initial:MainActivity,
    private val command:(String)->JSONObject) {
    private val current=AtomicReference(initial)
    private val app=initial.application
    private val wholeDeadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(1200)
    private val callbacks=object:Application.ActivityLifecycleCallbacks {
        override fun onActivityCreated(a:Activity,b:Bundle?) { if(a is MainActivity) current.set(a) }
        override fun onActivityStarted(a:Activity) {}
        override fun onActivityResumed(a:Activity) { if(a is MainActivity) current.set(a) }
        override fun onActivityPaused(a:Activity) {}
        override fun onActivityStopped(a:Activity) {}
        override fun onActivitySaveInstanceState(a:Activity,b:Bundle) {}
        override fun onActivityDestroyed(a:Activity) {}
    }
    private fun ui(block:()->Unit)=instrumentation.runOnMainSync(block)
    private fun activity()=current.get()
    private fun state()=ViewModelProvider(activity())[ChatModel::class.java].state.value
    private fun waitFor(seconds:Long=30,predicate:()->Boolean) {
        val deadline=minOf(wholeDeadline,System.nanoTime()+TimeUnit.SECONDS.toNanos(seconds))
        while(System.nanoTime()<deadline) {
            var success=false; ui { success=predicate() }
            if(success) return
            Thread.sleep(50)
        }
        error("E2E condition deadline")
    }
    private fun click(id:Int) {
        waitFor { activity().findViewById<View>(id).isShown && activity().findViewById<View>(id).isEnabled }
        ui { check(activity().findViewById<View>(id).performClick()) }
    }
    private fun text(value:String)=ui { activity().findViewById<EditText>(R.id.message_input).setText(value) }
    private fun dialog(value:String?=null) {
        val deadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(10)
        while(System.nanoTime()<deadline) {
            val root=instrumentation.uiAutomation.rootInActiveWindow
            if(root!=null) {
                val button=root.findAccessibilityNodeInfosByViewId("android:id/button1").firstOrNull()
                val input=root.findAccessibilityNodeInfosByViewId(activity().packageName+":id/dialog_title").firstOrNull()
                if(button!=null && (value==null || input!=null)) {
                    if(value!=null) check(requireNotNull(input).performAction(AccessibilityNodeInfo.ACTION_SET_TEXT,
                        Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,value) }))
                    check(button.performAction(AccessibilityNodeInfo.ACTION_CLICK)); return
                }
            }
            Thread.sleep(50)
        }
        error("E2E dialog deadline")
    }
    private fun create(title:String):String {
        click(R.id.new_conversation); dialog(title)
        waitFor { !state().busy && state().selected?.title==title && state().screen=="chat" }
        return requireNotNull(state().selected).id
    }
    private fun open(id:String) {
        if(state().screen!="conversations") click(R.id.back)
        waitFor { !state().busy && state().conversations.any { it.id==id } }
        ui {
            val rows=activity().findViewById<LinearLayout>(R.id.conversation_items)
            val button=(0 until rows.childCount).map { rows.getChildAt(it) }.first { it.tag==id }
            check(button.performClick())
        }
        waitFor { !state().busy && state().selected?.id==id && state().screen=="chat" }
    }
    private fun send(value:String) { text(value); click(R.id.send); waitFor { !state().busy && state().pending!=null } }
    private fun completed() {
        waitFor(310) { !state().busy && state().pending?.snapshot?.state=="completed" }
        waitFor { state().messages.any { it.role=="assistant" && it.text.isNotEmpty() } }
    }
    private fun admitted() {
        waitFor(310) {
            val retry=activity().findViewById<Button>(R.id.retry)
            if(state().pending?.snapshot==null && !state().busy && retry.isShown && retry.isEnabled) retry.performClick()
            state().pending?.snapshot!=null && !state().busy
        }
    }
    fun run():JSONObject {
        check(activity().packageName=="com.example.privatechat.e2e")
        app.registerActivityLifecycleCallbacks(callbacks)
        val result=JSONObject()
        try {
            val a=create("Fixture A"); text("Draft A")
            waitFor {
                val root=activity().findViewById<View>(R.id.root_container)
                val bars=ViewCompat.getRootWindowInsets(root)?.getInsets(WindowInsetsCompat.Type.systemBars())
                bars!=null && bars.top>0 && root.paddingTop>=bars.top
            }
            result.put("system_bar_insets",true)
            ui {
                val input=activity().findViewById<EditText>(R.id.message_input)
                input.requestFocus()
                activity().getSystemService(InputMethodManager::class.java).showSoftInput(input,InputMethodManager.SHOW_IMPLICIT)
            }
            waitFor {
                val root=activity().findViewById<View>(R.id.root_container)
                ViewCompat.getRootWindowInsets(root)?.isVisible(WindowInsetsCompat.Type.ime())==true
            }
            ui {
                val root=activity().findViewById<View>(R.id.root_container)
                val keyboard=requireNotNull(ViewCompat.getRootWindowInsets(root)).getInsets(WindowInsetsCompat.Type.ime())
                val position=IntArray(2); val send=activity().findViewById<View>(R.id.send)
                send.getLocationInWindow(position)
                check(position[1]+send.height<=activity().window.decorView.height-keyboard.bottom)
                activity().getSystemService(InputMethodManager::class.java).hideSoftInputFromWindow(send.windowToken,0)
            }
            waitFor { ViewCompat.getRootWindowInsets(activity().findViewById(R.id.root_container))?.isVisible(WindowInsetsCompat.Type.ime())==false }
            result.put("keyboard_ime_insets",true)
            click(R.id.back); val b=create("Fixture B"); text("Draft B")
            open(a); waitFor { state().draft=="Draft A" }
            val old=activity(); ui { old.recreate() }
            waitFor { activity()!==old && state().selected?.id==a && state().draft=="Draft A" }
            val recreated=activity()
            ui { recreated.finish() }
            waitFor { recreated.isDestroyed }
            current.set(instrumentation.startActivitySync(Intent().setClassName("com.example.privatechat.e2e",MainActivity::class.java.name)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as MainActivity)
            waitFor { state().configured && !state().busy && state().conversations.any { it.id==a } }
            open(a); waitFor { state().draft=="Draft A" }
            open(b); waitFor { state().draft=="Draft B" }
            result.put("draft_partition_switch_rotation_relaunch",true).put("keystore_fresh_activity_relaunch",true)
            open(a)
            val before=command("job_count").getInt("posts")
            command("arm_drop")
            send("Reply with exactly APK_OK.")
            waitFor { !state().busy && state().pending?.snapshot==null && state().error!=null }
            val key=requireNotNull(state().pending).key
            click(R.id.retry); completed()
            check(requireNotNull(state().pending).key==key)
            val after=command("job_count")
            check(after.getInt("posts")==before+1 && after.getInt("dropped")==1)
            result.put("actual_transport_loss_same_key_one_post",true)
            val answer=state().messages.last { it.role=="assistant" }.text
            result.put("fixture_answer",answer.take(200))
            ui {
                val rows=activity().findViewById<LinearLayout>(R.id.message_items)
                check((0 until rows.childCount).map { rows.getChildAt(it) }.filterIsInstance<TextView>().any { it.isTextSelectable && it.text.contains(answer) })
            }
            result.put("actual_reply_selectable",true)
            command("arm_rate")
            send("Reply with exactly RETRY_OK.")
            waitFor { !state().busy && state().error=="Please wait before retrying." && !activity().findViewById<Button>(R.id.retry).isEnabled }
            val posts=command("job_count").getInt("posts")
            // No UI interaction while Retry-After expires.
            Thread.sleep(2300)
            waitFor { activity().findViewById<Button>(R.id.retry).isEnabled }
            check(command("job_count").getInt("posts")==posts)
            click(R.id.dismiss); waitFor { !state().busy && state().pending?.phase=="dismissed" }
            result.put("controlled_retry_after_no_touch_no_post",true)
            command("arm_history_hold")
            send("Reply with exactly OLD_VIEW."); admitted()
            val heldDeadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(310)
            while(!command("history_held").getBoolean("held")) {
                check(System.nanoTime()<heldDeadline); Thread.sleep(100)
            }
            open(b)
            command("release_history"); Thread.sleep(500)
            check(state().selected?.id==b && state().draft=="Draft B" && state().messages.none { it.text.contains("OLD_VIEW") })
            result.put("held_old_terminal_after_navigation_guard",true)
            send("Write an extremely long numbered list of 200 detailed items about planets. Do not stop early.")
            admitted(); waitFor(310) { state().pending?.snapshot?.state=="running" && state().pending?.snapshot?.partialContent?.isNotEmpty()==true }
            click(R.id.rename); dialog("Fixture B renamed")
            waitFor { !state().busy && state().selected?.title=="Fixture B renamed" }
            click(R.id.stop); waitFor { !state().busy && state().pending?.snapshot?.state=="cancelled" }
            result.put("active_rename_then_stop_revision",true)
            send("Reply with exactly AFTER_STOP."); admitted(); completed()
            check(state().error==null)
            result.put("stop_then_new_send_revision",true)
            send("Write an extremely long numbered list of 200 detailed items about oceans. Do not stop early.")
            admitted(); waitFor(310) { state().pending?.snapshot?.state=="running" }
            click(R.id.delete); dialog()
            waitFor { !state().busy && state().screen=="conversations" && state().conversations.none { it.id==b } }
            click(R.id.refresh); waitFor { !state().busy && state().conversations.none { it.id==b } }
            result.put("active_delete_revision_no_revive",true)
            open(a); check(state().messages.any { it.role=="assistant" })
            text("Fresh process draft")
            return result
        } finally { app.unregisterActivityLifecycleCallbacks(callbacks) }
    }
    fun verifyPersistence():JSONObject {
        waitFor { state().configured && !state().busy && state().status=="Connected" }
        val a=state().conversations.first { it.title=="Fixture A" }.id
        open(a)
        waitFor { state().draft=="Fresh process draft" && state().messages.any { it.role=="assistant" && it.text=="APK_OK" } }
        return JSONObject().put("keystore_after_process_restart",true).put("sqlite_draft_after_process_restart",true)
            .put("authoritative_history_after_process_restart",true)
    }
}
