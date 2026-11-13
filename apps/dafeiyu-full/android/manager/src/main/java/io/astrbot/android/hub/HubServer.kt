package io.astrbot.android.hub

import android.content.Context
import android.util.Log
import io.astrbot.android.core.ServiceId
import io.astrbot.android.runtime.RuntimeConfiguration
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.charset.StandardCharsets
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

/**
 * 大肥鱼 WebChat Hub.
 *
 * AstrBot 的 /api/chat/send 是“谁发谁收”的 SSE，另一个客户端看不到别人的
 * 流式回复。Hub 作为唯一的上游客户端，把发送者的 SSE 原样回给发送者，
 * 同时把用户消息、Bot 片段和完成事件广播给同一会话的其他订阅者。
 *
 * 端口固定 6186，AstrBot 保持在 6185，管理器的健康检查不用改。
 */
class HubServer(
    private val context: Context,
    private val port: Int = 6186,
) {
    private val tag = "DafeiyuHub"
    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()
    private val pool = Executors.newCachedThreadPool { runnable ->
        Thread(runnable, "dafeiyu-hub").apply { isDaemon = true }
    }
    private val subscribers = ConcurrentHashMap<String, MutableSet<Subscriber>>()
    private val busySessions = ConcurrentHashMap<String, AtomicBoolean>()
    private val tokenCache = ConcurrentHashMap<String, Long>()
    private val turnCounter = AtomicLong(0)
    private var socket: ServerSocket? = null
    @Volatile private var running = false

    fun start() {
        if (running) return
        val host = if (lanEnabled()) "0.0.0.0" else "127.0.0.1"
        val server = ServerSocket()
        server.reuseAddress = true
        server.bind(InetSocketAddress(host, port), 32)
        socket = server
        running = true
        pool.execute {
            Log.i(tag, "listening on $host:$port")
            while (running) {
                val incoming = try {
                    server.accept()
                } catch (failure: Exception) {
                    if (running) Log.w(tag, "accept failed: ${failure.message}")
                    break
                }
                pool.execute { handle(incoming) }
            }
        }
    }

    fun stop() {
        running = false
        runCatching { socket?.close() }
        socket = null
        subscribers.values.flatten().forEach { it.close() }
        subscribers.clear()
        runCatching { client.dispatcher.executorService.shutdown() }
        runCatching { pool.shutdownNow() }
    }

    private fun lanEnabled(): Boolean = runCatching {
        RuntimeConfiguration(context).read(ServiceId.ASTRBOT).lanEnabled
    }.getOrDefault(false)

    private fun upstream(): String {
        val endpoint = runCatching { RuntimeConfiguration(context).read(ServiceId.ASTRBOT).endpoint }.getOrNull()
        val port = endpoint?.port ?: ServiceId.ASTRBOT.defaultPort
        return "http://127.0.0.1:$port"
    }

    // ------------------------------------------------------------------ HTTP

    private data class Request(
        val method: String,
        val path: String,
        val query: Map<String, String>,
        val headers: Map<String, String>,
        val body: ByteArray,
    )

    private data class Response(val status: Int, val reason: String, val type: String, val body: ByteArray)

    private fun handle(socket: Socket) {
        socket.use { connection ->
            try {
                val input = BufferedInputStream(connection.getInputStream())
                val request = readRequest(input) ?: return
                val output = connection.getOutputStream()
                when {
                    request.method == "GET" && request.path == "/hub/health" ->
                        writeJson(output, 200, JSONObject().put("ok", true).put("port", port))
                    request.method == "POST" && request.path == "/hub/send" ->
                        handleSend(request, output)
                    request.method == "POST" && request.path == "/hub/ingest" ->
                        handleIngest(request, output, connection)
                    request.method == "GET" && request.path == "/hub/subscribe" ->
                        handleSubscribe(request, output, connection)
                    else -> writeJson(output, 404, JSONObject().put("error", "not found"))
                }
            } catch (failure: Exception) {
                if (failure !is java.net.SocketException) Log.w(tag, "connection failed: ${failure.message}")
            }
        }
    }

    private fun readRequest(input: InputStream): Request? {
        val first = readLine(input) ?: return null
        val parts = first.split(' ')
        if (parts.size < 2) return null
        val method = parts[0].uppercase()
        val target = parts[1]
        val path = target.substringBefore('?')
        val query = target.substringAfter('?', "").split('&').filter { it.contains('=') }.associate {
            val key = it.substringBefore('=')
            val value = it.substringAfter('=')
            urlDecode(key) to urlDecode(value)
        }
        val headers = mutableMapOf<String, String>()
        while (true) {
            val line = readLine(input) ?: break
            if (line.isEmpty()) break
            val index = line.indexOf(':')
            if (index > 0) headers[line.substring(0, index).trim().lowercase()] =
                line.substring(index + 1).trim()
        }
        val length = headers["content-length"]?.toIntOrNull() ?: 0
        val body = if (length in 1..(8 * 1024 * 1024)) readExactly(input, length) else ByteArray(0)
        return Request(method, path, query, headers, body)
    }

    private fun readExactly(input: InputStream, length: Int): ByteArray {
        val bytes = ByteArray(length)
        var offset = 0
        while (offset < length) {
            val count = input.read(bytes, offset, length - offset)
            if (count < 0) break
            offset += count
        }
        return if (offset == length) bytes else bytes.copyOf(offset)
    }

    private fun readLine(input: InputStream): String? {
        val buffer = StringBuilder()
        while (true) {
            val value = input.read()
            if (value < 0) return if (buffer.isEmpty()) null else buffer.toString()
            if (value == '\n'.code) break
            if (value != '\r'.code) buffer.append(value.toChar())
            if (buffer.length > 16 * 1024) return null
        }
        return buffer.toString()
    }

    private fun writeJson(output: OutputStream, status: Int, body: JSONObject) {
        val bytes = body.toString().toByteArray(StandardCharsets.UTF_8)
        writeHead(output, status, "application/json; charset=utf-8", bytes.size)
        output.write(bytes)
        output.flush()
    }

    private fun writeHead(output: OutputStream, status: Int, type: String, length: Int? = null) {
        val reason = when (status) {
            200 -> "OK"; 400 -> "Bad Request"; 401 -> "Unauthorized"; 404 -> "Not Found"
            409 -> "Conflict"; 502 -> "Bad Gateway"; 503 -> "Service Unavailable"
            else -> "Error"
        }
        val head = buildString {
            append("HTTP/1.1 $status $reason\r\n")
            append("Content-Type: $type\r\n")
            append("Cache-Control: no-cache\r\n")
            append("Access-Control-Allow-Origin: *\r\n")
            if (length != null) append("Content-Length: $length\r\n")
            append("Connection: close\r\n\r\n")
        }
        output.write(head.toByteArray(StandardCharsets.UTF_8))
        output.flush()
    }

    // ------------------------------------------------------------------ auth

    private fun tokenOf(request: Request): String? =
        request.headers["authorization"]?.removePrefix("Bearer ")?.trim()?.takeIf { it.isNotEmpty() }

    private fun authorized(token: String?, sessionId: String): Boolean {
        if (token == null) return false
        val now = System.currentTimeMillis()
        val cached = tokenCache[token]
        if (cached != null && cached > now) return true
        val request = okhttp3.Request.Builder()
            .url("${upstream()}/api/v1/chat/sessions/${encode(sessionId)}?page=1&page_size=1")
            .header("Authorization", "Bearer $token")
            .get()
            .build()
        return runCatching {
            client.newCall(request).execute().use { response ->
                val ok = response.isSuccessful
                if (ok) tokenCache[token] = now + 15 * 60 * 1000
                ok
            }
        }.getOrDefault(false)
    }

    // ------------------------------------------------------------------ send

    private fun handleSend(request: Request, output: OutputStream) {
        val token = tokenOf(request)
        val payload = runCatching { JSONObject(String(request.body, StandardCharsets.UTF_8)) }.getOrNull()
        val sessionId = payload?.optString("session_id").orEmpty()
        val clientId = payload?.optString("client_id").orEmpty()
        val message = payload?.optJSONArray("message")
        if (sessionId.isEmpty() || message == null) {
            writeJson(output, 400, JSONObject().put("error", "session_id and message are required"))
            return
        }
        if (!authorized(token, sessionId)) {
            writeJson(output, 401, JSONObject().put("error", "unauthorized"))
            return
        }
        val busy = busySessions.computeIfAbsent(sessionId) { AtomicBoolean(false) }
        if (!busy.compareAndSet(false, true)) {
            writeJson(output, 409, JSONObject().put("error", "上一条消息还在回复"))
            return
        }
        val turnId = "hub-${System.currentTimeMillis()}-${turnCounter.incrementAndGet()}"
        try {
            writeHead(output, 200, "text/event-stream; charset=utf-8")
            broadcast(sessionId, null, JSONObject()
                .put("type", "user_message")
                .put("client_id", clientId)
                .put("turn_id", turnId)
                .put("message", message))
            val body = JSONObject()
                .put("session_id", sessionId)
                .put("message", message)
                .toString()
                .toRequestBody("application/json; charset=utf-8".toMediaType())
            val upstreamRequest = okhttp3.Request.Builder()
                .url("${upstream()}/api/chat/send")
                .header("Authorization", "Bearer ${token ?: ""}")
                .header("Accept", "text/event-stream")
                .post(body)
                .build()
            val response = client.newCall(upstreamRequest).execute()
            if (!response.isSuccessful) {
                val text = "服务端 ${response.code}"
                response.close()
                writeSse(output, JSONObject().put("type", "error").put("data", text))
                broadcast(sessionId, clientId, JSONObject()
                    .put("type", "error").put("turn_id", turnId).put("data", text))
                return
            }
            var sawEnd = false
            response.body?.source()?.use { source ->
                while (true) {
                    val line = source.readUtf8Line() ?: break
                    if (!line.startsWith("data:")) continue
                    val raw = line.substring(5).trim()
                    if (raw.isEmpty()) continue
                    writeSse(output, rawObject(raw))
                    val event = runCatching { JSONObject(raw) }.getOrNull() ?: continue
                    val type = event.optString("type")
                    if (type == "complete" || type == "end") sawEnd = true
                    broadcast(sessionId, clientId, JSONObject()
                        .put("type", "bot_event")
                        .put("turn_id", turnId)
                        .put("event", event))
                }
            }
            if (!sawEnd) {
                val done = JSONObject().put("type", "complete")
                writeSse(output, done)
                broadcast(sessionId, clientId, JSONObject()
                    .put("type", "complete").put("turn_id", turnId))
            } else {
                broadcast(sessionId, clientId, JSONObject()
                    .put("type", "complete").put("turn_id", turnId))
            }
        } catch (failure: Exception) {
            Log.w(tag, "send failed: ${failure.message}")
            runCatching {
                writeSse(output, JSONObject().put("type", "error").put("data", "Hub 转发失败"))
                broadcast(sessionId, clientId, JSONObject()
                    .put("type", "error").put("turn_id", turnId).put("data", "Hub 转发失败"))
            }
        } finally {
            busy.set(false)
        }
    }

    private fun rawObject(raw: String): JSONObject = runCatching { JSONObject(raw) }
        .getOrElse { JSONObject().put("type", "error").put("data", raw) }

    private fun writeSse(output: OutputStream, payload: JSONObject) {
        val text = "data: ${payload.toString()}\n\n".toByteArray(StandardCharsets.UTF_8)
        synchronized(output) {
            output.write(text)
            output.flush()
        }
    }

    // ------------------------------------------------------------------ ingest

    /** 只接受本机回环：TG 同步插件把镜像消息推到正在看 WebChat 的设备。 */
    private fun handleIngest(request: Request, output: OutputStream, connection: Socket) {
        if (!connection.inetAddress.isLoopbackAddress) {
            writeJson(output, 403, JSONObject().put("error", "loopback only"))
            return
        }
        val payload = runCatching { JSONObject(String(request.body, StandardCharsets.UTF_8)) }.getOrNull()
        if (payload == null) {
            writeJson(output, 400, JSONObject().put("error", "invalid json body"))
            return
        }
        val sessionId = payload.optString("session_id")
        val role = payload.optString("role")
        val text = payload.optString("text")
        if (sessionId.isEmpty() || text.isBlank() || role !in setOf("user", "bot")) {
            writeJson(output, 400, JSONObject().put("error", "session_id, role(user|bot) and text are required"))
            return
        }
        broadcast(sessionId, null, JSONObject()
            .put("type", "mirror")
            .put("role", role)
            .put("sender", payload.optString("sender"))
            .put("text", text)
            .put("ts", System.currentTimeMillis()))
        writeJson(output, 200, JSONObject().put("ok", true))
    }

    // ------------------------------------------------------------------ subscribe

    private fun handleSubscribe(request: Request, output: OutputStream, connection: Socket) {
        val token = tokenOf(request)
        val sessionId = request.query["session_id"].orEmpty()
        val clientId = request.query["client_id"].orEmpty()
        if (sessionId.isEmpty()) {
            writeJson(output, 400, JSONObject().put("error", "session_id is required"))
            return
        }
        if (!authorized(token, sessionId)) {
            writeJson(output, 401, JSONObject().put("error", "unauthorized"))
            return
        }
        writeHead(output, 200, "text/event-stream; charset=utf-8")
        output.write(": connected\n\n".toByteArray(StandardCharsets.UTF_8))
        output.flush()
        val subscriber = Subscriber(sessionId, clientId, connection, output)
        subscribers.computeIfAbsent(sessionId) { ConcurrentHashMap.newKeySet<Subscriber>() }.add(subscriber)
        try {
            while (running && !connection.isClosed) {
                val frame = subscriber.queue.poll(15, TimeUnit.SECONDS)
                if (frame == null) {
                    synchronized(output) {
                        output.write(": heartbeat\n\n".toByteArray(StandardCharsets.UTF_8))
                        output.flush()
                    }
                } else {
                    synchronized(output) {
                        output.write(frame.toByteArray(StandardCharsets.UTF_8))
                        output.flush()
                    }
                }
            }
        } catch (_: Exception) {
            // Client went away.
        } finally {
            subscribers[sessionId]?.remove(subscriber)
            subscriber.close()
        }
    }

    private inner class Subscriber(
        val sessionId: String,
        val clientId: String,
        val connection: Socket,
        val output: OutputStream,
    ) {
        val queue = LinkedBlockingQueue<String>(256)
        fun close() {
            runCatching { connection.close() }
        }
    }

    private fun broadcast(sessionId: String, excludeClientId: String?, payload: JSONObject) {
        val frame = "data: ${payload.toString()}\n\n"
        subscribers[sessionId]?.forEach { subscriber ->
            if (excludeClientId != null && subscriber.clientId == excludeClientId) return@forEach
            if (!subscriber.queue.offer(frame)) {
                // Slow consumer: drop it rather than block the sender.
                subscribers[sessionId]?.remove(subscriber)
                subscriber.close()
            }
        }
    }

    private fun encode(value: String): String =
        java.net.URLEncoder.encode(value, StandardCharsets.UTF_8.name())

    private fun urlDecode(value: String): String =
        runCatching { java.net.URLDecoder.decode(value, StandardCharsets.UTF_8.name()) }.getOrDefault(value)
}

object HubController {
    private const val tag = "DafeiyuHub"
    @Volatile private var server: HubServer? = null

    fun start(context: Context) {
        synchronized(this) {
            if (server != null) return
            val candidate = HubServer(context.applicationContext)
            runCatching { candidate.start() }
                .onSuccess { server = candidate; Log.i(tag, "hub started") }
                .onFailure { Log.e(tag, "hub start failed: ${it.message}") }
        }
    }

    fun restart(context: Context) {
        synchronized(this) {
            runCatching { server?.stop() }
            server = null
            start(context)
        }
    }

    fun isRunning(): Boolean = server != null

    fun stop() {
        synchronized(this) {
            runCatching { server?.stop() }
            server = null
        }
    }
}
