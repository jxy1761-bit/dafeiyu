package com.dafeiyu.dafeiyu_chat

import android.content.ContentValues
import android.content.Intent
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.astrbot.android.core.ServiceId
import io.astrbot.android.hub.HubController
import io.astrbot.android.runtime.LanAccess
import io.astrbot.android.runtime.RuntimeService
import io.astrbot.android.runtime.RuntimeStore
import org.json.JSONObject
import io.flutter.plugin.common.MethodChannel
import java.io.File

/**
 * 把附件存进系统的「下载」目录。
 *
 * API 29+ 走 MediaStore.Downloads —— 不需要任何存储权限，文件管理器里能看到，
 * 卸载 App 也不会跟着删。API 28 及以下退回公共 Downloads 目录直接写
 * （需要 WRITE_EXTERNAL_STORAGE，清单里限了 maxSdkVersion=28）。
 */
class MainActivity : FlutterActivity() {
    private val channel = "dafeiyu/downloads"

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        window.decorView.post { applyFrameRate(storedFrameRate()) }
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "dafeiyu/full")
            .setMethodCallHandler { call, result ->
                try {
                    when (call.method) {
                        "localStatus" -> result.success(localStatus())
                        "startLocal" -> {
                            RuntimeService.command(this, "start", ServiceId.ASTRBOT)
                            result.success(true)
                        }
                        // App 刚启动时运行时状态还没加载完，这里直接看磁盘标记，避免漏启动。
                        "ensureLocal" -> {
                            val marker = File(
                                io.astrbot.android.runtime.BootstrapRuntime(this).rootfs,
                                ".astrbot-bootstrap.json",
                            )
                            if (marker.isFile) {
                                RuntimeService.command(this, "start", ServiceId.ASTRBOT)
                            }
                            result.success(true)
                        }
                        "stopLocal" -> {
                            RuntimeService.command(this, "stop", ServiceId.ASTRBOT)
                            result.success(true)
                        }
                        "initialize" -> {
                            RuntimeService.command(this, "initialize")
                            result.success(true)
                        }
                        "setLan" -> {
                            val enabled = call.argument<Boolean>("enabled") == true
                            RuntimeService.command(this, if (enabled) "enable-lan" else "disable-lan", ServiceId.ASTRBOT)
                            result.success(true)
                        }
                        "openManager" -> {
                            startActivity(Intent(this, io.astrbot.android.MainActivity::class.java))
                            result.success(true)
                        }
                        "hubUrl" -> result.success("http://127.0.0.1:6186")
                        "setFrameRate" -> {
                            applyFrameRate(call.argument<Int>("rate") ?: 60)
                            result.success(true)
                        }
                        else -> result.notImplemented()
                    }
                } catch (e: Exception) {
                    result.error("full_failed", e.message ?: "操作失败", null)
                }
            }
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "dafeiyu/notifications")
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "requestPermission" -> {
                        MessageNotifier.requestPermission(this)
                        result.success(true)
                    }
                    "startWatch" -> {
                        val args = call.arguments as? Map<*, *> ?: emptyMap<String, Any?>()
                        val intent = Intent(this, MessageWatchService::class.java).apply {
                            putExtra("base_url", args["baseUrl"] as? String ?: "")
                            putExtra("hub_url", args["hubUrl"] as? String ?: "")
                            putExtra("username", args["username"] as? String ?: "")
                            putExtra("password", args["password"] as? String ?: "")
                            putExtra("session_id", args["sessionId"] as? String ?: "shared")
                        }
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            startForegroundService(intent)
                        } else {
                            startService(intent)
                        }
                        result.success(true)
                    }
                    "stopWatch" -> {
                        stopService(Intent(this, MessageWatchService::class.java))
                        result.success(true)
                    }
                    else -> result.notImplemented()
                }
            }
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, channel)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "save" -> {
                        val name = call.argument<String>("name") ?: "file.bin"
                        val mime = call.argument<String>("mime") ?: "application/octet-stream"
                        val bytes = call.argument<ByteArray>("bytes")
                        if (bytes == null) {
                            result.error("no_bytes", "没有内容", null)
                            return@setMethodCallHandler
                        }
                        try {
                            result.success(save(name, mime, bytes))
                        } catch (e: Exception) {
                            result.error("save_failed", e.message ?: "保存失败", null)
                        }
                    }
                    else -> result.notImplemented()
                }
            }
    }

    private fun storedFrameRate(): Int =
        getSharedPreferences("FlutterSharedPreferences", MODE_PRIVATE)
            .getLong("flutter.frame_rate", 60L).toInt().coerceIn(30, 144)

    /** 选择不高于目标帧率的最高显示模式；HyperOS 会按应用给刷新率，所以要主动请求。 */
    private fun applyFrameRate(target: Int) {
        try {
            @Suppress("DEPRECATION")
            val display = windowManager.defaultDisplay ?: return
            val modes = display.supportedModes ?: return
            val chosen = modes.filter { it.refreshRate <= target + 0.5f }
                .maxByOrNull { it.refreshRate }
                ?: modes.minByOrNull { kotlin.math.abs(it.refreshRate - target) }
                ?: return
            window.attributes = window.attributes.apply {
                preferredDisplayModeId = chosen.modeId
                preferredRefreshRate = chosen.refreshRate
            }
        } catch (_: Exception) {
        }
    }

    private fun localStatus(): Map<String, Any?> {        val state = RuntimeStore.state.value
        val service = state.services[ServiceId.ASTRBOT]
        val webUi = state.webUis[ServiceId.ASTRBOT]
        return mapOf(
            "installed" to state.environmentInstalled,
            "componentsReady" to state.componentsReady,
            "running" to (service?.ready == true),
            "desired" to (service?.desiredRunning == true),
            "phase" to state.phase,
            "port" to (webUi?.endpoint?.port ?: ServiceId.ASTRBOT.defaultPort),
            "lan" to (webUi?.lanEnabled == true),
            "ip" to (LanAccess.addresses(this).firstOrNull() ?: ""),
            "hubPort" to 6186,
            "hubRunning" to HubController.isRunning(),
        )
    }

    private fun save(name: String, mime: String, bytes: ByteArray): String {
        val folder = "大肥鱼"
        val safe = sanitize(name)

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, safe)
                put(MediaStore.Downloads.MIME_TYPE, mime)
                put(MediaStore.Downloads.RELATIVE_PATH, "${Environment.DIRECTORY_DOWNLOADS}/$folder")
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val resolver = contentResolver
            val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IllegalStateException("无法创建下载项")
            resolver.openOutputStream(uri)?.use { it.write(bytes) }
                ?: throw IllegalStateException("无法写入下载项")
            values.clear()
            values.put(MediaStore.Downloads.IS_PENDING, 0)
            resolver.update(uri, values, null, null)
            return "Download/$folder/$safe"
        }

        @Suppress("DEPRECATION")
        val dir = File(
            Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
            folder,
        )
        if (!dir.exists() && !dir.mkdirs()) throw IllegalStateException("无法创建下载目录")
        var target = File(dir, safe)
        if (target.exists()) {
            val dot = safe.lastIndexOf('.')
            val base = if (dot > 0) safe.substring(0, dot) else safe
            val ext = if (dot > 0) safe.substring(dot) else ""
            var i = 1
            while (target.exists()) {
                target = File(dir, "$base($i)$ext")
                i++
            }
        }
        target.writeBytes(bytes)
        return target.absolutePath
    }

    /** 只留文件名本身，去掉路径和非法字符。 */
    private fun sanitize(name: String): String {
        val base = name.replace('\\', '/')
            .substringAfterLast('/')
            .replace("\u0000", "")
            .trim()
        return if (base.isEmpty() || base == "." || base == "..") "file.bin" else base
    }

    override fun onResume() {
        super.onResume()
        applyFrameRate(storedFrameRate())
        MessageWatchService.appForeground = true
        MessageWatchService.refresh()
    }

    override fun onPause() {
        MessageWatchService.appForeground = false
        MessageWatchService.refresh()
        super.onPause()
    }
}
