package com.personcorp.zcoderemote

import android.annotation.SuppressLint
import android.graphics.Bitmap
import android.os.Bundle
import android.util.Log
import android.view.View
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import androidx.webkit.WebViewCompat
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

import com.personcorp.zcoderemote.BuildConfig

/**
 * Single-activity shell. Home = saved links; tapping one opens the relay
 * page in a WebView that survives app restarts because the URL itself is
 * persisted. The desktop side keeps the device session alive via its own
 * stored deviceSid, so an old link keeps working until it is revoked.
 *
 * Smoothness: one WebView lives for the whole process. Leaving to home
 * only hides it (never about:blank), back/forward state is snapshotted,
 * and the renderer is warmed at startup so the first open paints fast.
 */
class MainActivity : AppCompatActivity(), QrScanFragment.Listener {

    private lateinit var repository: RemoteRepository
    private lateinit var homeView: LinearLayout
    private lateinit var webView: WebView
    private var currentUrl: String? = null

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        repository = RemoteRepository(applicationContext)
        setContentView(R.layout.activity_main)

        homeView = findViewById(R.id.home_container)
        webView = findViewById(R.id.web_view)
        configureWebView(webView)

        findViewById<TextView>(R.id.scan_button).setOnClickListener {
            supportFragmentManager.beginTransaction()
                .replace(R.id.fragment_host, QrScanFragment())
                .addToBackStack(null)
                .commit()
        }

        if (savedInstanceState != null) {
            val url = savedInstanceState.getString(STATE_URL)
            if (url != null && backState.isEmpty) {
                openLink(url)
            } else if (url != null) {
                currentUrl = url
                homeView.visibility = View.GONE
                webView.visibility = View.VISIBLE
                webView.restoreState(backState)
            } else {
                lifecycleScope.launch { renderLinks() }
            }
            return
        }

        val sharedUrl = intent?.data?.toString()
        lifecycleScope.launch {
            when {
                sharedUrl != null && sharedUrl.startsWith("https://") ->
                    addAndOpen(sharedUrl, labelFor(sharedUrl))
                else -> {
                    renderLinks()
                    // Auto-restore: reopen the most recent link so the usual
                    // path is launch -> already connected.
                    repository.lastUsed()?.let { openLink(it.url) }
                }
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView(webView: WebView) {
        with(webView.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            // Cache static assets on disk so reopening a link skips most
            // of the JS/CSS download even after process death.
            cacheMode = WebSettings.LOAD_DEFAULT
            databaseEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            mediaPlaybackRequiresUserGesture = false
        }
        // Default layer type only. An explicit hardware layer stopped
        // compositing on some devices and rendered a connected-but-blank
        // page; do not re-add it.
        webView.setLayerType(View.LAYER_TYPE_NONE, null)
        webView.webChromeClient = RelayChromeClient()
        webView.webViewClient = RelayWebViewClient()
        if (BuildConfig.DEBUG) {
            WebView.setWebContentsDebuggingEnabled(true)
        }
        runCatching {
            WebViewCompat.getCurrentWebViewPackage(applicationContext)?.let { pkg ->
                Log.i(TAG, "webview provider: ${pkg.packageName} ${pkg.versionName}")
            }
        }
    }

    override fun onQrScanned(url: String) {
        lifecycleScope.launch {
            repository.save(url, labelFor(url))
            Toast.makeText(this@MainActivity, R.string.link_saved, Toast.LENGTH_SHORT).show()
            openLink(url)
        }
    }

    private fun addAndOpen(url: String, label: String) {
        lifecycleScope.launch {
            repository.save(url, label)
            openLink(url)
        }
    }

    private fun openLink(url: String) {
        currentUrl = url
        homeView.visibility = View.GONE
        webView.visibility = View.VISIBLE
        if (webView.url != url) {
            webView.loadUrl(url)
        }
    }

    private fun showHome() {
        currentUrl = null
        // Keep the page alive in the hidden WebView: reopening is then a
        // visibility flip instead of a full reload + relay reconnect.
        webView.visibility = View.GONE
        homeView.visibility = View.VISIBLE
        lifecycleScope.launch { renderLinks() }
    }

    private fun labelFor(url: String): String {
        val host = url.removePrefix("https://").substringBefore('/')
        return getString(R.string.link_label_default, host)
    }

    private suspend fun renderLinks() {
        val container = findViewById<LinearLayout>(R.id.link_list)
        container.removeAllViews()
        val links = repository.links().first()
        findViewById<TextView>(R.id.empty_hint).visibility =
            if (links.isEmpty()) View.VISIBLE else View.GONE
        links.forEach { link -> container.addView(makeLinkRow(link)) }
    }

    private fun makeLinkRow(link: RemoteLink): View {
        val row = layoutInflater.inflate(R.layout.item_link, homeView, false)
        row.findViewById<TextView>(R.id.link_label).text = link.label
        row.findViewById<TextView>(R.id.link_url).text = link.url
        row.findViewById<View>(R.id.link_open).setOnClickListener { openLink(link.url) }
        row.findViewById<View>(R.id.link_delete).setOnClickListener {
            lifecycleScope.launch {
                repository.delete(link.id)
                renderLinks()
            }
        }
        return row
    }

    @Deprecated("Deprecated in Java")
    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        when {
            webView.visibility == View.VISIBLE && webView.canGoBack() -> webView.goBack()
            webView.visibility == View.VISIBLE -> showHome()
            else -> super.onBackPressed()
        }
    }

    override fun onPause() {
        if (::webView.isInitialized && webView.visibility == View.VISIBLE) {
            webView.saveState(backState)
        }
        super.onPause()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(STATE_URL, currentUrl)
    }

    private inner class RelayWebViewClient : WebViewClient() {
        override fun shouldOverrideUrlLoading(
            view: WebView?,
            request: WebResourceRequest?,
        ): Boolean {
            val url = request?.url?.toString() ?: return false
            if (url.startsWith("https://zcode.z.ai") || url.startsWith("https://z.ai") ||
                url.startsWith("https://chat.z.ai")
            ) {
                return false
            }
            // Everything else (docs, login help) leaves to the browser —
            // actually open it; returning true alone would dead-end the link.
            runCatching {
                startActivity(android.content.Intent(android.content.Intent.ACTION_VIEW, request.url))
            }
            return true
        }

        override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
            Log.i(TAG, "page start: $url")
            hideErrorBanner()
        }

        override fun onPageFinished(view: WebView?, url: String?) {
            Log.i(TAG, "page finished: $url")
        }

        override fun onReceivedError(
            view: WebView?,
            request: WebResourceRequest?,
            error: android.webkit.WebResourceError?,
        ) {
            if (request?.isForMainFrame != true) return
            val desc = error?.description?.toString() ?: "unknown error"
            Log.e(TAG, "main frame error: ${request.url} $desc")
            showErrorBanner(getString(R.string.load_error_banner, desc))
        }
    }

    private inner class RelayChromeClient : android.webkit.WebChromeClient() {
        override fun onConsoleMessage(consoleMessage: android.webkit.ConsoleMessage?): Boolean {
            Log.i(
                TAG,
                "console[${consoleMessage?.messageLevel()}] ${consoleMessage?.message()} " +
                    "(${consoleMessage?.sourceId()}:${consoleMessage?.lineNumber()})",
            )
            return true
        }
    }

    private fun showErrorBanner(text: String) {
        findViewById<TextView>(R.id.error_banner)?.apply {
            this.text = text
            visibility = View.VISIBLE
        }
    }

    private fun hideErrorBanner() {
        findViewById<TextView>(R.id.error_banner)?.visibility = View.GONE
    }

    private companion object {
        const val TAG = "ZCodeRemote"
        const val STATE_URL = "current_url"
        val backState = Bundle()
    }
}
