package com.example.privatechat.storage

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import com.example.privatechat.BuildConfig
import com.example.privatechat.net.Endpoint

enum class VaultState { EMPTY, CONFIGURED, UNREADABLE }

data class SavedConnection(val server: String, val owner: String, val device: String, val token: String) {
    override fun toString(): String = "SavedConnection(redacted)"
    val partition: String get() = server + "|" + owner
}

class TokenVault(context: Context) {
    private val preferences = context.getSharedPreferences("connection", Context.MODE_PRIVATE)
    private val alias = "privatechat.device-token.v1"
    companion object {
        fun classify(fields:Map<String,*>,loader:()->SavedConnection?):VaultState {
            if(fields.isEmpty()) return VaultState.EMPTY
            if(fields.keys!=setOf("server","owner","device","iv","ciphertext") || fields.values.any { it !is String || it.isEmpty() }) return VaultState.UNREADABLE
            return try { if(loader()!=null) VaultState.CONFIGURED else VaultState.UNREADABLE }
            catch(_:Exception) { VaultState.UNREADABLE }
        }
    }
    @Synchronized fun state():VaultState=classify(preferences.all,::load)
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true).build())
        }.generateKey()
    }
    @Synchronized fun save(connection: SavedConnection) {
        require(connection.token.matches(Regex("[A-Za-z0-9_-]{43}")))
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        cipher.updateAAD(connection.partition.toByteArray(Charsets.UTF_8))
        val encrypted = cipher.doFinal(connection.token.toByteArray(Charsets.UTF_8))
        check(preferences.edit().putString("server", connection.server).putString("owner", connection.owner)
            .putString("device", connection.device).putString("iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .putString("ciphertext", Base64.encodeToString(encrypted, Base64.NO_WRAP)).commit())
    }
    @Synchronized fun load(): SavedConnection? {
        return try {
            val server = preferences.getString("server", null) ?: return null
            val owner = preferences.getString("owner", null) ?: return null
            val device = preferences.getString("device", null) ?: return null
            require(owner.matches(Regex("[A-Za-z0-9_-]{1,64}")) && device.matches(Regex("[a-f0-9]{32}")))
            Endpoint.validate(server,BuildConfig.FLAVOR=="lan")
            val iv = Base64.decode(preferences.getString("iv", ""), Base64.NO_WRAP)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, iv))
            cipher.updateAAD((server + "|" + owner).toByteArray(Charsets.UTF_8))
            val token = String(cipher.doFinal(Base64.decode(preferences.getString("ciphertext", ""), Base64.NO_WRAP)), Charsets.UTF_8)
            require(token.matches(Regex("[A-Za-z0-9_-]{43}")))
            SavedConnection(server, owner, device, token)
        } catch (_: Exception) { null }
    }
    @Synchronized fun clear() { check(preferences.edit().clear().commit()) }
}
