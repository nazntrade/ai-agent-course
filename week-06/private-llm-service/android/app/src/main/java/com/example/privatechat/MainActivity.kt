package com.example.privatechat

import android.app.AlertDialog
import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.text.InputType
import android.view.View
import android.view.WindowManager
import android.view.inputmethod.EditorInfo
import android.widget.*
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import org.json.JSONObject
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    private lateinit var model: ChatModel
    private var previousRows: Any?=null
    private var previousConversations: Any?=null
    private var previousScreen: String?=null
    private var bindingComposer=false
    private fun <T: View> view(id: Int): T = findViewById(id)
    private fun add(container: LinearLayout,child: View) { container.addView(child,LinearLayout.LayoutParams(-1,-2)) }
    private fun label(container: LinearLayout,id: Int,text: String=""): TextView = TextView(this).apply {
        this.id=id; this.text=text; textSize=16f; setTextColor(0xFF172D45.toInt()); setPadding(8,12,8,12); add(container,this)
    }
    private fun button(container: LinearLayout,id: Int,text: String,action: ()->Unit): Button = Button(this).apply {
        this.id=id; this.text=text; isAllCaps=false; setOnClickListener { action() }; add(container,this)
    }
    private fun input(container: LinearLayout,id: Int,hint: String,type: Int,max: Int): EditText = EditText(this).apply {
        this.id=id; this.hint=hint; inputType=type; filters=arrayOf(android.text.InputFilter.LengthFilter(max))
        importantForAutofill=View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS; add(container,this)
    }
    private fun scroll(container: LinearLayout,id: Int,contentId: Int) {
        val scroll=ScrollView(this).apply { this.id=id; isFillViewport=true }
        scroll.addView(LinearLayout(this).apply { this.id=contentId; orientation=LinearLayout.VERTICAL },FrameLayout.LayoutParams(-1,-2))
        container.addView(scroll,LinearLayout.LayoutParams(-1,0,1f))
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        WindowCompat.setDecorFitsSystemWindows(window,false)
        WindowCompat.getInsetsController(window,window.decorView).apply {
            isAppearanceLightStatusBars=true; isAppearanceLightNavigationBars=true
        }
        val root=view<View>(R.id.root_container)
        val padding=intArrayOf(root.paddingLeft,root.paddingTop,root.paddingRight,root.paddingBottom)
        ViewCompat.setOnApplyWindowInsetsListener(root) { target,insets ->
            val bars=insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val keyboard=insets.getInsets(WindowInsetsCompat.Type.ime())
            target.setPadding(padding[0]+bars.left,padding[1]+bars.top,padding[2]+bars.right,
                padding[3]+maxOf(bars.bottom,keyboard.bottom))
            insets
        }
        ViewCompat.requestApplyInsets(root)
        model=ViewModelProvider(this)[ChatModel::class.java]
        buildScreens()
        onBackPressedDispatcher.addCallback(this,object: OnBackPressedCallback(true) {
            override fun handleOnBackPressed() { if(model.state.value.screen!="conversations") model.screen("conversations") else finish() }
        })
        lifecycleScope.launch { repeatOnLifecycle(Lifecycle.State.STARTED) { model.state.collect { render(it) } } }
        if(savedInstanceState==null && model.state.value.configured) model.refresh()
    }
    private fun buildScreens() {
        val list=view<LinearLayout>(R.id.conversations_screen)
        button(list,R.id.new_conversation,"New chat") { titleDialog("New conversation","",model::create) }
        button(list,R.id.refresh,"Refresh") { model.refresh() }
        button(list,R.id.settings,"Settings") { model.screen("settings") }
        label(list,R.id.empty,"No conversations yet. Create your first chat.")
        scroll(list,View.generateViewId(),R.id.conversation_items)
        val chat=view<LinearLayout>(R.id.chat_screen)
        val tools=LinearLayout(this).apply { orientation=LinearLayout.HORIZONTAL }
        add(chat,tools)
        button(tools,R.id.back,"Chats") { model.screen("conversations") }.layoutParams=LinearLayout.LayoutParams(0,-2,1f)
        button(tools,R.id.rename,"Rename") { model.state.value.selected?.let { titleDialog("Rename conversation",it.title,model::rename) } }.layoutParams=LinearLayout.LayoutParams(0,-2,1f)
        button(tools,R.id.delete,"Delete") { AlertDialog.Builder(this).setTitle("Delete conversation?").setMessage("This removes its messages and cancels any active request.")
            .setNegativeButton("Keep",null).setPositiveButton("Delete") { _,_ -> model.delete() }.show() }.layoutParams=LinearLayout.LayoutParams(0,-2,1f)
        label(chat,R.id.chat_title)
        scroll(chat,R.id.message_scroll,R.id.message_items)
        label(chat,R.id.job_status)
        val requestTools=LinearLayout(this).apply { orientation=LinearLayout.HORIZONTAL }
        add(chat,requestTools)
        button(requestTools,R.id.stop,"Stop") { model.stop() }.layoutParams=LinearLayout.LayoutParams(0,-2,1f)
        button(requestTools,R.id.retry,"Retry saved request") { model.retry() }.layoutParams=LinearLayout.LayoutParams(0,-2,2f)
        button(requestTools,R.id.dismiss,"Discard") { model.dismiss() }.layoutParams=LinearLayout.LayoutParams(0,-2,1f)
        val message=input(chat,R.id.message_input,"Message",InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES,32000).apply { minLines=2; maxLines=4; isSaveEnabled=false }
        message.addTextChangedListener(object: TextWatcher {
            override fun beforeTextChanged(s: CharSequence?,start: Int,count: Int,after: Int) {}
            override fun onTextChanged(s: CharSequence?,start: Int,before: Int,count: Int) {
                if(!bindingComposer) model.updateDraft(s?.toString() ?: "")
                renderControls(model.state.value)
            }
            override fun afterTextChanged(s: Editable?) {}
        })
        button(chat,R.id.send,"Send") { val text=message.text.toString(); if(text.isNotBlank()) model.send(text) }
        val settings=view<LinearLayout>(R.id.settings_screen)
        label(settings,View.generateViewId(),"Connection settings").textSize=20f
        label(settings,View.generateViewId(),"Start the home service manually. HTTP is limited to private LAN addresses; HTTPS validates the server certificate.")
        input(settings,R.id.server_input,"Server address",InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI,512).setSingleLine(true)
        val token=input(settings,R.id.token_input,"Device token (blank keeps saved token)",InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD,43).apply {
            setSingleLine(true); isSaveEnabled=false; imeOptions=EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING or EditorInfo.IME_FLAG_NO_EXTRACT_UI
        }
        button(settings,R.id.save_settings,"Save") { val server=view<EditText>(R.id.server_input).text.toString(); val secret=token.text.toString(); token.text.clear(); model.configure(server,secret) }
        label(settings,R.id.connection_status)
        label(settings,R.id.device_id).setTextIsSelectable(true)
        button(settings,R.id.settings_done,"Conversations") { model.screen("conversations") }
        button(settings,R.id.disconnect,"Forget connection") { AlertDialog.Builder(this).setTitle("Forget connection?").setMessage("Your conversations remain on the server. The saved token is removed from this app.")
            .setNegativeButton("Keep",null).setPositiveButton("Forget") { _,_ -> model.disconnect() }.show() }
    }
    private fun titleDialog(title: String,value: String,save: (String)->Unit) {
        val input=EditText(this).apply { id=R.id.dialog_title; setText(value); setSingleLine(true); filters=arrayOf(android.text.InputFilter.LengthFilter(80)) }
        val dialog=AlertDialog.Builder(this).setTitle(title).setView(input).setNegativeButton("Cancel",null).setPositiveButton("Save",null).create()
        dialog.setOnShowListener { dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
            val text=input.text.toString().trim(); if(text.isEmpty()) input.error="Enter a title" else { save(text); dialog.dismiss() }
        } }
        dialog.show()
    }
    private fun render(state: ChatState) {
        view<TextView>(R.id.banner).text=state.status
        view<ProgressBar>(R.id.busy).visibility=if(state.busy) View.VISIBLE else View.GONE
        view<TextView>(R.id.error).apply { text=state.error ?: ""; visibility=if(state.error==null) View.GONE else View.VISIBLE }
        view<View>(R.id.conversations_screen).visibility=if(state.screen=="conversations") View.VISIBLE else View.GONE
        view<View>(R.id.chat_screen).visibility=if(state.screen=="chat") View.VISIBLE else View.GONE
        view<View>(R.id.settings_scroll).visibility=if(state.screen=="settings") View.VISIBLE else View.GONE
        if(state.screen=="settings") window.addFlags(WindowManager.LayoutParams.FLAG_SECURE) else window.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
        if(state.screen!=previousScreen) {
            view<EditText>(R.id.token_input).text.clear()
            if(state.screen=="settings") view<EditText>(R.id.server_input).setText(state.server)
            previousScreen=state.screen
        }
        view<TextView>(R.id.connection_status).text=state.status
        view<TextView>(R.id.device_id).text=if(state.device.isEmpty()) "" else "Device: ${state.device}"
        view<TextView>(R.id.chat_title).text=state.selected?.title ?: "Chat"
        val composer=view<EditText>(R.id.message_input)
        if(composer.text.toString()!=state.draft) {
            bindingComposer=true
            try { composer.setText(state.draft); composer.setSelection(composer.length()) }
            finally { bindingComposer=false }
        }
        view<View>(R.id.empty).visibility=if(state.conversations.isEmpty()) View.VISIBLE else View.GONE
        if(previousConversations!=state.conversations) {
            val container=view<LinearLayout>(R.id.conversation_items); container.removeAllViews()
            state.conversations.forEach { conversation -> container.addView(Button(this).apply {
                text=conversation.title; isAllCaps=false; tag=conversation.id; setOnClickListener { model.open(conversation) }
            },LinearLayout.LayoutParams(-1,-2)) }
            previousConversations=state.conversations
        }
        val rows=Triple(state.selected?.id,state.messages,state.pending)
        if(previousRows!=rows) {
            val container=view<LinearLayout>(R.id.message_items); container.removeAllViews()
            state.messages.forEach { message -> bubble(container,if(message.role=="user") "You" else "Assistant",message.text,
                if(message.completionStatus=="completed") "" else " • ${message.completionStatus}") }
            val pending=state.pending
            if(pending!=null && pending.phase!="dismissed") {
                if(state.messages.none { it.jobId==pending.snapshot?.id && it.role=="user" }) bubble(container,"You",runCatching { JSONObject(pending.payload).getString("text") }.getOrDefault(""),if(pending.snapshot==null) " • saved, awaiting confirmation" else "")
                pending.snapshot?.let { snapshot -> if(state.messages.none { it.jobId==snapshot.id && it.role=="assistant" } && snapshot.partialContent.isNotEmpty()) bubble(container,"Assistant",snapshot.partialContent,if(pending.terminal) " • ${snapshot.state}" else " • generating") }
            }
            previousRows=rows
            view<ScrollView>(R.id.message_scroll).post { view<ScrollView>(R.id.message_scroll).fullScroll(View.FOCUS_DOWN) }
        }
        val snapshot=state.pending?.snapshot
        view<TextView>(R.id.job_status).text=when {
            snapshot?.finishReason=="length" -> "Answer reached the output limit."
            snapshot?.historyTruncated==true -> "Older completed turns were omitted to fit the context."
            snapshot?.state=="queued" -> "Waiting in queue${snapshot.queuePosition?.let { " • position $it" } ?: ""}"
            snapshot!=null -> "Request: ${snapshot.state}${snapshot.errorCode?.let { " • $it" } ?: ""}"
            state.pending!=null && state.pending.phase!="dismissed" -> "Saved request. Retry uses the same request key."
            else -> ""
        }
        renderControls(state)
    }
    private fun renderControls(state: ChatState) {
        val pending=state.pending; val active=pending!=null && !pending.terminal && pending.phase!="dismissed"
        view<Button>(R.id.send).isEnabled=state.configured && !state.busy && state.selected!=null && !active && view<EditText>(R.id.message_input).text.isNotBlank()
        view<Button>(R.id.stop).apply { visibility=if(active) View.VISIBLE else View.GONE; isEnabled=!state.busy }
        view<Button>(R.id.retry).apply { visibility=if(active) View.VISIBLE else View.GONE; isEnabled=!state.busy && System.currentTimeMillis()>=state.retryAt }
        view<Button>(R.id.dismiss).apply { visibility=if(active && pending?.snapshot==null) View.VISIBLE else View.GONE; isEnabled=!state.busy }
        listOf(R.id.refresh,R.id.new_conversation,R.id.rename,R.id.delete).forEach { view<Button>(it).isEnabled=state.configured && !state.busy }
        view<Button>(R.id.save_settings).isEnabled=!state.busy
        view<Button>(R.id.disconnect).isEnabled=!state.busy
    }
    private fun bubble(container: LinearLayout,role: String,content: String,status: String) {
        container.addView(TextView(this).apply { text="$role$status\n$content"; textSize=16f; setTextColor(0xFF172D45.toInt()); setTextIsSelectable(true); setPadding(20,16,20,16); setBackgroundColor(if(role=="You") 0xFFE4EEFA.toInt() else 0xFFFFFFFF.toInt()) },LinearLayout.LayoutParams(-1,-2).apply { bottomMargin=12 })
    }
}
