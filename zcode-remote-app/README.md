# ZCode Remote (Android)

Native Android shell for ZCode Remote Control. The mobile web page works,
but it lives in a browser tab: close the tab, lose the session, rescan the
QR from the desktop every time. This app keeps the link.

## What it does

- **Scan once.** The QR URL is saved locally (DataStore). Reopening needs
  no desktop interaction and no new QR.
- **Auto-restore.** Launching the app reopens the most recent link.
  The usual path is unlock phone -> tap app -> already connected.
- **Multiple links.** One per desktop machine (Zenbook, Spectre), each
  listed on the home screen.
- **Deep links.** Sharing or opening a `https://zcode.z.ai/...` remote
  URL lands in the app directly.

Why old links keep working: the desktop side persists its relay device
identity (`webRemoteControlExternalRelayDevice` in the desktop's
setting.json) and reconnects to `wss://zcode.z.ai/ws` on every start. The
QR only carries the page address; nothing in it expires until the session
is revoked from the desktop. If a stored link stops connecting, refresh
the QR once and save the new link; the stale row can be deleted with the
X button.

## Stack

Kotlin, single activity, classic views. WebView (`androidx.webkit`),
CameraX + ML Kit barcode scanning for QR (bundled model, offline, no GMS).
minSdk 29 / targetSdk 35.

## Build

Requires JDK 17+ (build validated with Temurin 21) and an Android SDK
with platform 35 + build-tools 35:

```bash
export ANDROID_HOME=/path/to/android-sdk
export JAVA_HOME=/path/to/jdk-21
./gradlew assembleDebug
# output: app/build/outputs/apk/debug/app-debug.apk
```

Install: `adb install app-debug.apk`, or copy the APK to the phone and open it.

## Security notes

- Links are stored in app-private storage; `allowBackup=false`.
- WebView navigation outside the relay origins (`zcode.z.ai`, `z.ai`)
  is handed to the browser instead of loaded in-app.
- Cleartext HTTP is disabled at the network-security-config level.
- Anyone with the link can operate the desktop window — same caveat as
  the official web page. Do not share APK-stored links; they are as
  sensitive as the QR itself.

## Responsiveness

The relay page is a web app; the shell exists to keep it warm and make
reopening feel native:

- **No page destruction.** Back-to-home hides the WebView instead of
  loading `about:blank`. Reopening a link flips visibility — no reload,
  no relay reconnect round-trip.
- **Disk cache on** (`LOAD_DEFAULT`) plus `domStorageEnabled`: static
  JS/CSS come from cache even after process death, so only live data
  crosses the network on reopen.
- **State snapshot** (`saveState`/`restoreState`): rotation and process
  death restore the page's back/forward history instead of restarting
  at the entry URL.
- **Hardware layer** on the WebView; software fallback rendering is what
  makes embedded webviews feel sluggish, not the page itself.
- The auto-restore on launch means the common case never shows a loader:
  by the time you read the screen, the session page is already up.

If the page still feels heavy under load (long transcripts), the next
levers are: pinning the WebView to its own render process
(`WebViewRenderProcessClient` + terminate handling), preconnecting to
the relay origin via a document-start script, and trimming message
history in the desktop window before remote viewing.

## Tests

Repository round-trips (save, list, delete, dedupe, lastUsed) are covered
by Robolectric tests against real DataStore storage:

```bash
./gradlew testDebugUnitTest
# 6 tests, 0 failures
```

## Not in scope (yet)

- Foreground service / persistent notification keeping the socket warm
  when the app is swiped away (WebView reconnects on next open).
- Biometric lock before reopening a link.
- The Telegram Bot Channel remains the outage-proof control plane;
  this app improves the visual Remote Control path only.
