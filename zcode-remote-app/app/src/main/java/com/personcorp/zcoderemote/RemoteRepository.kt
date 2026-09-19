package com.personcorp.zcoderemote

import android.content.Context
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "remote_links")

/** One saved remote-control link. */
data class RemoteLink(
    val id: String,
    val url: String,
    val label: String,
    val savedAtEpochMs: Long,
)

/**
 * Persists scanned remote-control links locally so a scan happens once,
 * not every time the browser tab dies. DataStore only — no account, no sync.
 */
class RemoteRepository(private val context: Context) {

    suspend fun save(url: String, label: String): RemoteLink {
        val id = linkIdFromUrl(url)
        val now = System.currentTimeMillis()
        context.dataStore.edit { prefs ->
            prefs[stringPreferencesKey("link_$id")] = "$url\n$label\n$now"
        }
        return RemoteLink(id, url, label, now)
    }

    suspend fun delete(id: String) {
        context.dataStore.edit { prefs -> prefs.remove(stringPreferencesKey("link_$id")) }
    }

    fun links(): Flow<List<RemoteLink>> =
        context.dataStore.data.map(::parseLinks)

    suspend fun lastUsed(): RemoteLink? {
        val prefs = context.dataStore.data.first()
        return parseLinks(prefs).maxByOrNull(RemoteLink::savedAtEpochMs)
    }

    companion object {
        /**
         * Stable per-link id: strip scheme+query noise and hash the path.
         * The relay session token lives in the path segment; identical
         * paths overwrite themselves instead of accumulating duplicates.
         */
        fun linkIdFromUrl(url: String): String {
            val path = url.removePrefix("https://").substringAfter('/')
                .substringBefore('?')
            val digest = java.security.MessageDigest
                .getInstance("SHA-256")
                .digest(path.toByteArray())
                .joinToString("") { "%02x".format(it) }
            return digest.take(16)
        }
    }
}

private const val PARTS_URL = 0
private const val PARTS_LABEL = 1
private const val PARTS_SAVED_AT = 2

private fun parseLinks(prefs: Preferences): List<RemoteLink> =
    prefs.asMap().values
        .filterIsInstance<String>()
        .mapNotNull(::parseEntry)
        .sortedByDescending(RemoteLink::savedAtEpochMs)

private fun parseEntry(raw: String): RemoteLink? {
    val parts = raw.split("\n")
    if (parts.size < 3 || !parts[PARTS_URL].startsWith("https://")) return null
    val savedAt = parts[PARTS_SAVED_AT].toLongOrNull() ?: return null
    return RemoteLink(
        id = RemoteRepository.linkIdFromUrl(parts[PARTS_URL]),
        url = parts[PARTS_URL],
        label = parts[PARTS_LABEL],
        savedAtEpochMs = savedAt,
    )
}
