package com.dafeiyu.dafeiyu_chat

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import kotlin.math.min

/**
 * 后台消息守护：自己登录 AstrBot、订阅 Hub 的 SSE，App 不在前台时发系统通知。
 *
 * 为什么不用 Flutter 侧的通知：App 退到后台后 Flutter 的网络回调不可靠，
 * 所以这条链路完全放在原生前台服务里，进程被 MIUI 冻结也能收消息。
 */
class MessageWatchService : Service() {
    companion object {
        private const val TAG = "DafeiyuWatch"
        private const val PREFS = "message_watch"
        private const val SERVICE_CHANNEL = "dafeiyu_watch_v1"
        private const val COMPANION_CHANNEL = "dafeiyu_companion_v1"
        private const val SERVICE_NOTI_ID = 2100
        private const val CLIENT_ID = "watch"
        private const val ACTION_STOP = "com.dafeiyu.dafeiyu_chat.STOP_MESSAGE_WATCH"
        private const val COMPANION_REVERT_MS = 45_000L

        @Volatile
        var appForeground = false

        @Volatile
        private var instance: MessageWatchService? = null

        /** App 前后台切换时刷新陪伴卡片：前台收敛成普通服务通知，后台升成实时更新。 */
        fun refresh() {
            instance?.handler?.post { instance?.onForegroundChanged() }
        }
    }

    private data class WatchConfig(
        val baseUrl: String,
        val hubUrl: String,
        val username: String,
        val password: String,
        val sessionId: String,
    )

    private val handler = Handler(Looper.getMainLooper())
    private val pending = mutableMapOf<String, StringBuilder>()
    private val flushRuns = mutableMapOf<String, Runnable>()
    private val notified = mutableSetOf<String>()

    @Volatile
    private var running = false

    @Volatile
    private var currentConfig: WatchConfig? = null

    private var worker: Thread? = null

    private var companionMessage: String? = null

    private val companionRevert = Runnable {
        companionMessage = null
        renderServiceNotification()
    }

    override fun onCreate() {
        super.onCreate()
        instance = this
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        val fromIntent = intent?.let { configFromIntent(it) }
        val config = fromIntent ?: loadConfig()
        if (config == null) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (fromIntent != null) saveConfig(fromIntent)

        createServiceChannel()
        startForeground(SERVICE_NOTI_ID, serviceNotification(promoted = !appForeground))

        if (worker?.isAlive != true || config != currentConfig) {
            running = false
            worker?.interrupt()
            worker = null
            currentConfig = config
            running = true
            worker = Thread({ streamLoop(config) }, "dafeiyu-message-watch").apply { start() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        worker?.interrupt()
        worker = null
        handler.removeCallbacksAndMessages(null)
        instance = null
        super.onDestroy()
    }

    // ------------------------------------------------------------------ 配置

    private fun configFromIntent(intent: Intent): WatchConfig? {
        val base = intent.getStringExtra("base_url")?.trim().orEmpty()
        if (base.isEmpty()) return null
        return WatchConfig(
            baseUrl = base,
            hubUrl = intent.getStringExtra("hub_url")?.trim().orEmpty(),
            username = intent.getStringExtra("username")?.trim().orEmpty(),
            password = intent.getStringExtra("password").orEmpty(),
            sessionId = intent.getStringExtra("session_id")?.trim().orEmpty().ifEmpty { "shared" },
        )
    }

    private fun loadConfig(): WatchConfig? {
        val sp = getSharedPreferences(PREFS, MODE_PRIVATE)
        val base = sp.getString("base_url", "").orEmpty()
        val hub = sp.getString("hub_url", "").orEmpty()
        val user = sp.getString("username", "").orEmpty()
        val pass = sp.getString("password", "").orEmpty()
        if (base.isEmpty() || hub.isEmpty() || user.isEmpty() || pass.isEmpty()) return null
        return WatchConfig(
            baseUrl = base,
            hubUrl = hub,
            username = user,
            password = pass,
            sessionId = sp.getString("session_id", "shared").orEmpty().ifEmpty { "shared" },
        )
    }

    private fun saveConfig(config: WatchConfig) {
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
            .putString("base_url", config.baseUrl)
            .putString("hub_url", config.hubUrl)
            .putString("username", config.username)
            .putString("password", config.password)
            .putString("session_id", config.sessionId)
            .apply()
    }

    // ------------------------------------------------------------------ 通知

    private fun createServiceChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(NotificationManager::class.java) ?: return
        manager.createNotificationChannel(
            NotificationChannel(
                SERVICE_CHANNEL,
                "消息通知服务",
                NotificationManager.IMPORTANCE_MIN,
            ).apply {
                description = "保持后台消息通知可用"
                setShowBadge(false)
            },
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
            manager.createNotificationChannel(
                NotificationChannel(
                    COMPANION_CHANNEL,
                    "大肥鱼陪伴",
                    NotificationManager.IMPORTANCE_LOW,
                ).apply {
                    description = "后台陪伴状态与实时消息"
                    setShowBadge(false)
                    setSound(null, null)
                    enableVibration(false)
                },
            )
        }
        MessageNotifier.ensureChannel(this)
    }

    private fun serviceNotification(promoted: Boolean): Notification {
        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val channel = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
            COMPANION_CHANNEL
        } else {
            SERVICE_CHANNEL
        }
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, channel)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        val message = companionMessage
        builder
            .setSmallIcon(R.drawable.ic_stat_avatar)
            .setLargeIcon(BitmapFactory.decodeResource(resources, R.drawable.ic_avatar))
            .setContentTitle(if (message != null) "大肥鱼" else if (promoted) "大肥鱼陪伴中" else "大肥鱼")
            .setContentText(message ?: if (promoted) "在后台陪着你" else "消息通知已开启")
            .setOngoing(true)
            .setShowWhen(false)
            .setContentIntent(pendingIntent)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setCategory(Notification.CATEGORY_SERVICE)
            .setPriority(if (promoted) Notification.PRIORITY_LOW else Notification.PRIORITY_MIN)
        if (message != null) {
            builder.setStyle(Notification.BigTextStyle().bigText(message))
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
            if (promoted) {
                builder.setShortCriticalText(if (message != null) "新消息" else "陪伴中")
                // 平台 SDK 没有 setter，官方要求走这个 extra 请求提升
                builder.addExtras(android.os.Bundle().apply {
                    putBoolean("android.requestPromotedOngoing", true)
                })
            }
        }
        val stopIntent = Intent(this, MessageWatchService::class.java).apply {
            action = ACTION_STOP
        }
        val stopPending = PendingIntent.getService(
            this,
            1,
            stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        builder.addAction(
            Notification.Action.Builder(
                android.R.drawable.ic_menu_close_clear_cancel,
                "停止通知",
                stopPending,
            ).build(),
        )
        val notification = builder.build()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
            Log.i(
                TAG,
                "service notification promoted=$promoted " +
                    "canPost=${getSystemService(NotificationManager::class.java)?.canPostPromotedNotifications()} " +
                    "promotable=${notification.hasPromotableCharacteristics()} " +
                    "flagPromoted=${notification.flags and Notification.FLAG_PROMOTED_ONGOING != 0}",
            )
        }
        return notification
    }

    private fun onForegroundChanged() {
        if (appForeground) {
            companionMessage = null
            handler.removeCallbacks(companionRevert)
        }
        renderServiceNotification()
    }

    private fun renderServiceNotification() {
        if (!running) return
        val promoted = !appForeground
        getSystemService(NotificationManager::class.java)
            ?.notify(SERVICE_NOTI_ID, serviceNotification(promoted))
    }

    // ------------------------------------------------------------------ 网络

    private fun streamLoop(config: WatchConfig) {
        var backoff = 2
        while (running) {
            var connection: HttpURLConnection? = null
            try {
                val token = login(config)
                if (token == null) {
                    Log.w(TAG, "login failed, retry in ${backoff}s")
                } else {
                    val hub = config.hubUrl.trimEnd('/')
                    val url = URL(
                        "$hub/hub/subscribe?session_id=${encode(config.sessionId)}&client_id=$CLIENT_ID",
                    )
                    connection = (url.openConnection() as HttpURLConnection).apply {
                        requestMethod = "GET"
                        connectTimeout = 10_000
                        readTimeout = 0
                        setRequestProperty("Authorization", "Bearer $token")
                        setRequestProperty("Accept", "text/event-stream")
                    }
                    val code = connection.responseCode
                    if (code == HttpURLConnection.HTTP_OK) {
                        backoff = 2
                        Log.i(TAG, "subscribed session=${config.sessionId}")
                        connection.inputStream.bufferedReader(StandardCharsets.UTF_8).use { reader ->
                            while (running) {
                                val line = reader.readLine() ?: break
                                if (line.startsWith("data:")) {
                                    val raw = line.substring(5).trim()
                                    if (raw.isNotEmpty()) handler.post { handleEvent(raw) }
                                }
                            }
                        }
                    } else if (code == 401 || code == 403) {
                        Log.i(TAG, "subscribe unauthorized, will login again")
                    } else {
                        Log.w(TAG, "subscribe HTTP $code")
                    }
                }
            } catch (interrupted: InterruptedException) {
                break
            } catch (failure: Exception) {
                if (running) Log.w(TAG, "stream error: ${failure.message}")
            } finally {
                runCatching { connection?.disconnect() }
            }

            if (!running) break
            try {
                Thread.sleep(backoff * 1000L)
            } catch (interrupted: InterruptedException) {
                break
            }
            backoff = min(backoff * 2, 60)
        }
    }

    private fun login(config: WatchConfig): String? {
        val base = config.baseUrl.trimEnd('/')
        val connection = (URL("$base/api/auth/login").openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = 10_000
            readTimeout = 15_000
            doOutput = true
            setRequestProperty("Content-Type", "application/json; charset=utf-8")
        }
        return try {
            val body = JSONObject()
                .put("username", config.username)
                .put("password", config.password)
                .toString()
            connection.outputStream.use { it.write(body.toByteArray(StandardCharsets.UTF_8)) }
            if (connection.responseCode != HttpURLConnection.HTTP_OK) return null
            val text = connection.inputStream.bufferedReader(StandardCharsets.UTF_8).readText()
            val root = JSONObject(text)
            if (root.optString("status") != "ok") return null
            root.optJSONObject("data")?.optString("token")?.takeIf { it.isNotBlank() }
        } catch (failure: Exception) {
            Log.w(TAG, "login error: ${failure.message}")
            null
        } finally {
            runCatching { connection.disconnect() }
        }
    }

    // ------------------------------------------------------------------ 事件

    private fun handleEvent(raw: String) {
        val obj = runCatching { JSONObject(raw) }.getOrNull() ?: return
        when (obj.optString("type")) {
            "bot_event" -> {
                val event = obj.optJSONObject("event") ?: return
                if (event.optString("type") != "plain") return
                val chain = event.optString("chain_type")
                if (chain == "reasoning" || chain.startsWith("tool")) return
                val text = event.optString("data")
                if (text.isBlank()) return
                val turn = obj.optString("turn_id").ifBlank { "live" }
                append(turn, text)
            }

            "complete" -> {
                val turn = obj.optString("turn_id")
                if (turn.isBlank()) {
                    pending.keys.toList().forEach { flush(it) }
                } else {
                    flush(turn)
                }
            }

            "mirror" -> {
                if (obj.optString("role") == "bot") {
                    val text = obj.optString("text")
                    if (text.isNotBlank()) maybeNotify(text)
                }
            }
        }
    }

    private fun append(turn: String, text: String) {
        pending.getOrPut(turn) { StringBuilder() }.append(text)
        flushRuns.remove(turn)?.let { handler.removeCallbacks(it) }
        val runnable = Runnable { flush(turn) }
        flushRuns[turn] = runnable
        handler.postDelayed(runnable, 900)
    }

    private fun flush(turn: String) {
        flushRuns.remove(turn)?.let { handler.removeCallbacks(it) }
        val builder = pending.remove(turn) ?: return
        val text = builder.toString().trim()
        if (text.isEmpty()) return
        if (notified.add(turn)) maybeNotify(text)
        if (notified.size > 400) notified.clear()
    }

    private fun maybeNotify(raw: String) {
        if (appForeground) return
        val text = clean(raw)
        if (text.isEmpty()) return
        MessageNotifier.show(this, "大肥鱼", text)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.BAKLAVA) {
            companionMessage = text
            handler.removeCallbacks(companionRevert)
            renderServiceNotification()
            handler.postDelayed(companionRevert, COMPANION_REVERT_MS)
        }
    }

    private fun clean(raw: String): String {
        var text = raw.replace(Regex("\\s+"), " ").trim()
        text = text.replace(Regex("[*_`#>]+"), "")
        if (text.length > 180) text = text.take(180) + "…"
        return text
    }

    private fun encode(value: String): String =
        URLEncoder.encode(value, StandardCharsets.UTF_8.name())
}
