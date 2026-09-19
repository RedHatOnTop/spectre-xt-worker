package com.personcorp.zcoderemote

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/**
 * Real DataStore on a Robolectric context: proves save -> list -> delete
 * round-trips and that lastUsed follows the most recent save.
 */
@RunWith(RobolectricTestRunner::class)
class RemoteRepositoryTest {

    private lateinit var repository: RemoteRepository

    @Before
    fun setUp() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        repository = RemoteRepository(context)
        // DataStore is a process-wide singleton; wipe leftovers from prior
        // tests in the same JVM so each test starts empty.
        repository.links().first().forEach { repository.delete(it.id) }
    }

    @Test
    fun save_then_list_returns_link() = runBlocking {
        val saved = repository.save(URL_A, "zenbook")

        val links = repository.links().first()
        assertEquals(listOf(saved), links)
        assertEquals(URL_A, links.single().url)
    }

    @Test
    fun delete_removes_only_that_link() = runBlocking {
        val a = repository.save(URL_A, "zenbook")
        val b = repository.save(URL_B, "spectre")

        repository.delete(a.id)

        val remaining = repository.links().first()
        assertEquals(listOf(b.id), remaining.map(RemoteLink::id))
    }

    @Test
    fun delete_is_idempotent() = runBlocking {
        val a = repository.save(URL_A, "zenbook")
        repository.delete(a.id)
        repository.delete(a.id)
        assertTrue(repository.links().first().isEmpty())
    }

    @Test
    fun saving_same_url_twice_overwrites_not_duplicates() = runBlocking {
        repository.save(URL_A, "first")
        Thread.sleep(2) // distinct savedAt timestamps
        repository.save(URL_A, "second")

        val links = repository.links().first()
        assertEquals(1, links.size)
        assertEquals("second", links.single().label)
    }

    @Test
    fun last_used_returns_most_recent_and_null_when_empty() = runBlocking {
        assertNull(repository.lastUsed())

        repository.save(URL_A, "older")
        Thread.sleep(2)
        val newer = repository.save(URL_B, "newer")

        assertEquals(newer.id, repository.lastUsed()?.id)
    }

    @Test
    fun link_ids_differ_between_distinct_urls() {
        val idA = RemoteRepository.linkIdFromUrl(URL_A)
        val idB = RemoteRepository.linkIdFromUrl(URL_B)
        assertTrue(idA != idB)
        assertEquals(idA, RemoteRepository.linkIdFromUrl("$URL_A?x=1"))
    }

    private companion object {
        const val URL_A = "https://zcode.z.ai/remote/sess-aaa"
        const val URL_B = "https://zcode.z.ai/remote/sess-bbb"
    }
}
