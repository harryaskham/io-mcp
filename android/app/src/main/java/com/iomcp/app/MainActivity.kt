package com.iomcp.app

import android.os.Bundle
import android.media.RingtoneManager
import android.os.Vibrator
import android.os.VibrationEffect
import android.util.Log
import android.view.KeyEvent
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.lifecycle.lifecycleScope
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.*
import org.json.JSONObject
import org.json.JSONArray
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL

private const val TAG = "IoMcpApp"
// Default API endpoint — override via intent extra "api_base" or SharedPreferences
private const val DEFAULT_API_BASE = "http://localhost:8445"

// ─── Nord Color Scheme ────────────────────────────────────────────────
// Maps Nord palette (https://www.nordtheme.com) to Material3 color roles
// Matches the TUI theme from src/io_mcp/tui/themes.py

private val NordDarkColorScheme = darkColorScheme(
    // Primary = Nord accent (Frost)
    primary = Color(0xFF88C0D0),           // nord8 — accent
    onPrimary = Color(0xFF2E3440),         // nord0 — bg
    primaryContainer = Color(0xFF434C5E),  // nord2 — highlight_bg
    onPrimaryContainer = Color(0xFFECEFF4), // nord6 — fg

    // Secondary = Nord blue
    secondary = Color(0xFF81A1C1),         // nord9 — blue
    onSecondary = Color(0xFF2E3440),
    secondaryContainer = Color(0xFF3B4252), // nord1 — bg_alt
    onSecondaryContainer = Color(0xFFECEFF4),

    // Tertiary = Nord purple
    tertiary = Color(0xFFB48EAD),          // nord15 — purple
    onTertiary = Color(0xFF2E3440),
    tertiaryContainer = Color(0xFF434C5E),
    onTertiaryContainer = Color(0xFFECEFF4),

    // Error = Nord red
    error = Color(0xFFBF616A),             // nord11 — error
    onError = Color(0xFF2E3440),
    errorContainer = Color(0xFF3B4252),
    onErrorContainer = Color(0xFFBF616A),

    // Background/Surface = Nord polar night
    background = Color(0xFF2E3440),        // nord0 — bg
    onBackground = Color(0xFFECEFF4),      // nord6 — fg
    surface = Color(0xFF2E3440),           // nord0 — bg
    onSurface = Color(0xFFECEFF4),         // nord6 — fg
    surfaceVariant = Color(0xFF3B4252),    // nord1 — bg_alt
    onSurfaceVariant = Color(0xFF616E88),  // fg_dim
    surfaceTint = Color(0xFF88C0D0),

    // Outline = Nord border
    outline = Color(0xFF4C566A),           // nord3 — border
    outlineVariant = Color(0xFF434C5E),    // nord2

    // Inverse
    inverseSurface = Color(0xFFECEFF4),
    inverseOnSurface = Color(0xFF2E3440),
    inversePrimary = Color(0xFF5E81AC),    // nord10
)

fun getApiBase(context: android.content.Context): String {
    val prefs = context.getSharedPreferences("io_mcp", android.content.Context.MODE_PRIVATE)
    return prefs.getString("api_base", DEFAULT_API_BASE) ?: DEFAULT_API_BASE
}

/**
 * Stateless frontend for io-mcp.
 *
 * This app mirrors the TUI state — it shows choices, accepts selections,
 * and forwards actions to the Python server. All TTS is handled by the TUI
 * running in Termux. The Android app provides:
 * - Visual display of choices and session state
 * - Touch-based selection with haptic feedback
 * - Freeform text input for messages/replies
 * - Keyboard shortcuts (j/k/enter/space) forwarded to TUI
 * - Mic button for voice recording via TUI
 * - No TTS (avoids duplicate audio with TUI)
 */
class MainActivity : ComponentActivity() {
    var currentSessionId: String = ""
    private val currentApiBase: String get() = getApiBase(this)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            val apiBase = currentApiBase
            MaterialTheme(colorScheme = NordDarkColorScheme) {
                IoMcpScreen(
                    onSessionIdChanged = { currentSessionId = it },
                    apiBase = apiBase,
                )
            }
        }
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        val sid = currentSessionId
        if (sid.isEmpty()) return super.onKeyDown(keyCode, event)

        val key = when (keyCode) {
            KeyEvent.KEYCODE_J, KeyEvent.KEYCODE_DPAD_DOWN, KeyEvent.KEYCODE_VOLUME_DOWN -> "j"
            KeyEvent.KEYCODE_K, KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_VOLUME_UP -> "k"
            KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_DPAD_CENTER -> "enter"
            KeyEvent.KEYCODE_SPACE -> "space"
            else -> return super.onKeyDown(keyCode, event)
        }

        // Fire and forget — send key to TUI
        lifecycleScope.launch(Dispatchers.IO) {
            sendKey(currentApiBase, sid, key)
        }
        return true
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun IoMcpScreen(
    onSessionIdChanged: (String) -> Unit = {},
    apiBase: String = DEFAULT_API_BASE,
) {
    val scope = rememberCoroutineScope()
    val haptic = LocalHapticFeedback.current
    val context = LocalContext.current

    // Notification sound for new choices
    val notificationSound = remember {
        try {
            val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
            RingtoneManager.getRingtone(context, uri)
        } catch (_: Exception) { null }
    }

    // State
    var connected by remember { mutableStateOf(false) }
    var preamble by remember { mutableStateOf("") }
    var choices by remember { mutableStateOf<List<Choice>>(emptyList()) }
    var selectedIndex by remember { mutableIntStateOf(0) }
    var sessionId by remember { mutableStateOf("") }
    var statusText by remember { mutableStateOf("Connecting to io-mcp...") }
    var sessions by remember { mutableStateOf<List<SessionInfo>>(emptyList()) }
    var messageText by remember { mutableStateOf("") }
    var speechLog by remember { mutableStateOf<List<String>>(emptyList()) }
    var isRecording by remember { mutableStateOf(false) }
    var showSettings by remember { mutableStateOf(false) }

    // SSE connection
    LaunchedEffect(Unit) {
        scope.launch(Dispatchers.IO) {
            connectToSSE(
                apiBase = apiBase,
                onConnected = {
                    connected = true
                    statusText = "Connected"
                },
                onChoicesPresented = { sid, p, c ->
                    sessionId = sid
                    preamble = p
                    choices = c
                    selectedIndex = 0
                    statusText = ""
                    // Play notification sound
                    try { notificationSound?.play() } catch (_: Exception) {}
                },
                onSpeechRequested = { _, text, _, _ ->
                    // Add to speech log (visual only — TUI handles audio)
                    speechLog = (speechLog + text).takeLast(5)
                },
                onSelectionMade = { _, label, _ ->
                    // Clear choices when selection is made (from TUI)
                    choices = emptyList()
                    preamble = ""
                    statusText = "Selected: $label — waiting..."
                },
                onRecordingState = { _, recording ->
                    isRecording = recording
                },
                onSessionCreated = { sid, name ->
                    sessions = sessions + SessionInfo(sid, name, false)
                },
                onSessionRemoved = { sid ->
                    sessions = sessions.filter { it.id != sid }
                    if (sessionId == sid) {
                        sessionId = sessions.firstOrNull()?.id ?: ""
                    }
                },
                onDisconnected = {
                    connected = false
                    statusText = "Disconnected. Reconnecting..."
                },
            )
        }
    }

    // Periodically fetch sessions and set sessionId if empty
    LaunchedEffect(connected) {
        if (connected) {
            while (true) {
                try {
                    sessions = fetchSessions(apiBase)
                    // Auto-set sessionId to first active session if not set
                    if (sessionId.isEmpty() && sessions.isNotEmpty()) {
                        val active = sessions.firstOrNull { it.active } ?: sessions.first()
                        sessionId = active.id
                        onSessionIdChanged(active.id)
                    }
                } catch (_: Exception) {}
                delay(3000)
            }
        }
    }

    // Notify activity when sessionId changes
    LaunchedEffect(sessionId) {
        if (sessionId.isNotEmpty()) {
            onSessionIdChanged(sessionId)
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("io-mcp") },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.primaryContainer,
                ),
                actions = {
                    IconButton(onClick = { showSettings = true }) {
                        Icon(
                            Icons.Default.Settings,
                            contentDescription = "Settings",
                            tint = MaterialTheme.colorScheme.onPrimaryContainer,
                        )
                    }
                },
            )
        },
    ) { padding ->

        // Settings dialog
        if (showSettings) {
            SettingsDialog(
                currentApiBase = apiBase,
                context = context,
                onDismiss = { showSettings = false },
            )
        }
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(16.dp),
        ) {
            // Status / Connection
            if (statusText.isNotEmpty()) {
                Text(
                    text = statusText,
                    color = if (connected) MaterialTheme.colorScheme.primary
                           else MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(bottom = 8.dp),
                )
            }

            // Session tabs
            if (sessions.size > 1) {
                ScrollableTabRow(
                    selectedTabIndex = sessions.indexOfFirst { it.id == sessionId }.coerceAtLeast(0),
                    modifier = Modifier.padding(bottom = 8.dp),
                ) {
                    sessions.forEachIndexed { _, session ->
                        Tab(
                            selected = session.id == sessionId,
                            onClick = { sessionId = session.id },
                            text = { Text(session.name) },
                        )
                    }
                }
            }

            // Speech log (last 5 TTS messages, visual only)
            if (speechLog.isNotEmpty() && choices.isEmpty()) {
                Card(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(bottom = 8.dp),
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f),
                    ),
                ) {
                    Column(modifier = Modifier.padding(12.dp)) {
                        speechLog.forEach { text ->
                            Text(
                                text = "💬 $text",
                                fontSize = 13.sp,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                modifier = Modifier.padding(vertical = 2.dp),
                            )
                        }
                    }
                }
            }

            // Preamble
            if (preamble.isNotEmpty()) {
                Text(
                    text = preamble,
                    style = MaterialTheme.typography.titleMedium,
                    modifier = Modifier.padding(bottom = 16.dp),
                )
            }

            // Choices list
            if (choices.isNotEmpty()) {
                val listState = rememberLazyListState()

                // Send highlight events when scroll position changes
                // This triggers TUI TTS readout for the centered item
                val centerIndex by remember {
                    derivedStateOf {
                        val info = listState.layoutInfo
                        val viewportCenter = (info.viewportStartOffset + info.viewportEndOffset) / 2
                        info.visibleItemsInfo.minByOrNull {
                            kotlin.math.abs((it.offset + it.size / 2) - viewportCenter)
                        }?.index ?: 0
                    }
                }

                LaunchedEffect(centerIndex) {
                    if (sessionId.isNotEmpty() && centerIndex != selectedIndex) {
                        selectedIndex = centerIndex
                        haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                        // Send highlight to TUI (1-based index)
                        launch(Dispatchers.IO) {
                            sendHighlight(apiBase, sessionId, centerIndex + 1)
                        }
                    }
                }

                LazyColumn(
                    state = listState,
                    modifier = Modifier.weight(1f),
                ) {
                    itemsIndexed(choices) { index, choice ->
                        ChoiceCard(
                            choice = choice,
                            index = index + 1,
                            isSelected = index == selectedIndex,
                            onClick = {
                                selectedIndex = index
                                haptic.performHapticFeedback(HapticFeedbackType.LongPress)

                                // Send selection to server (TUI handles TTS)
                                scope.launch(Dispatchers.IO) {
                                    sendSelection(apiBase, sessionId, choice.label, choice.summary)
                                    withContext(Dispatchers.Main) {
                                        choices = emptyList()
                                        preamble = ""
                                        statusText = "Selected: ${choice.label} — waiting..."
                                    }
                                }
                            },
                        )
                    }
                }
            } else if (statusText.isEmpty()) {
                // Waiting state
                Box(
                    modifier = Modifier.weight(1f),
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        text = "Waiting for agent...",
                        style = MaterialTheme.typography.bodyLarge,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            } else {
                Spacer(modifier = Modifier.weight(1f))
            }

            // Bottom bar: message input + mic + send
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(top = 8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                // Mic button (toggles voice recording via TUI)
                IconButton(
                    onClick = {
                        if (sessionId.isNotEmpty()) {
                            haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                            scope.launch(Dispatchers.IO) { sendKey(apiBase, sessionId, "space") }
                        }
                    },
                    colors = IconButtonDefaults.iconButtonColors(
                        containerColor = if (isRecording)
                            MaterialTheme.colorScheme.error
                        else
                            MaterialTheme.colorScheme.secondaryContainer,
                    ),
                ) {
                    Text(if (isRecording) "⏹" else "🎤", fontSize = 20.sp)
                }
                Spacer(modifier = Modifier.width(8.dp))
                OutlinedTextField(
                    value = messageText,
                    onValueChange = { messageText = it },
                    placeholder = { Text("Message...") },
                    modifier = Modifier.weight(1f),
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                    keyboardActions = KeyboardActions(
                        onSend = {
                            if (messageText.isNotBlank() && sessionId.isNotEmpty()) {
                                val msg = messageText
                                messageText = ""
                                haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                                scope.launch(Dispatchers.IO) {
                                    sendMessage(apiBase, sessionId, msg)
                                }
                            }
                        },
                    ),
                )
                Spacer(modifier = Modifier.width(8.dp))
                Button(
                    onClick = {
                        if (messageText.isNotBlank() && sessionId.isNotEmpty()) {
                            val msg = messageText
                            messageText = ""
                            haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                            scope.launch(Dispatchers.IO) {
                                sendMessage(apiBase, sessionId, msg)
                            }
                        }
                    },
                ) {
                    Text("Send")
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChoiceCard(
    choice: Choice,
    index: Int,
    isSelected: Boolean,
    onClick: () -> Unit,
) {
    Card(
        onClick = onClick,
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp),
        colors = CardDefaults.cardColors(
            containerColor = if (isSelected)
                MaterialTheme.colorScheme.primaryContainer
            else
                MaterialTheme.colorScheme.surfaceVariant,
        ),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(
                text = "$index. ${choice.label}",
                fontWeight = FontWeight.Bold,
                fontSize = 16.sp,
            )
            if (choice.summary.isNotEmpty()) {
                Text(
                    text = choice.summary,
                    fontSize = 14.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }
        }
    }
}

// ─── Settings Dialog ──────────────────────────────────────────────────

@Composable
fun SettingsDialog(
    currentApiBase: String,
    context: android.content.Context,
    onDismiss: () -> Unit,
) {
    var urlText by remember { mutableStateOf(currentApiBase) }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Settings") },
        text = {
            Column {
                Text(
                    text = "Server URL",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(bottom = 8.dp),
                )
                OutlinedTextField(
                    value = urlText,
                    onValueChange = { urlText = it },
                    placeholder = { Text(DEFAULT_API_BASE) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Text(
                    text = "Requires app restart to take effect",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 8.dp),
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = {
                    val prefs = context.getSharedPreferences("io_mcp", android.content.Context.MODE_PRIVATE)
                    prefs.edit().putString("api_base", urlText.trim()).apply()
                    onDismiss()
                },
            ) {
                Text("Save")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("Cancel")
            }
        },
    )
}

// ─── Data classes ─────────────────────────────────────────────────────

data class Choice(val label: String, val summary: String)
data class SessionInfo(val id: String, val name: String, val active: Boolean)

// ─── Network functions ────────────────────────────────────────────────

suspend fun connectToSSE(
    apiBase: String,
    onConnected: () -> Unit,
    onChoicesPresented: (sessionId: String, preamble: String, choices: List<Choice>) -> Unit,
    onSpeechRequested: (sessionId: String, text: String, blocking: Boolean, priority: Int) -> Unit,
    onSelectionMade: (sessionId: String, label: String, summary: String) -> Unit,
    onRecordingState: (sessionId: String, recording: Boolean) -> Unit,
    onSessionCreated: (sessionId: String, name: String) -> Unit,
    onSessionRemoved: (sessionId: String) -> Unit,
    onDisconnected: () -> Unit,
) {
    while (true) {
        try {
            val url = URL("$apiBase/api/events")
            val connection = url.openConnection() as HttpURLConnection
            connection.setRequestProperty("Accept", "text/event-stream")
            connection.connectTimeout = 5000
            connection.readTimeout = 0

            val reader = BufferedReader(InputStreamReader(connection.inputStream))
            withContext(Dispatchers.Main) { onConnected() }

            var eventType = ""
            val dataBuilder = StringBuilder()

            while (true) {
                val line = reader.readLine() ?: break

                when {
                    line.startsWith("event: ") -> {
                        eventType = line.removePrefix("event: ")
                    }
                    line.startsWith("data: ") -> {
                        dataBuilder.append(line.removePrefix("data: "))
                    }
                    line.isEmpty() && dataBuilder.isNotEmpty() -> {
                        try {
                            val data = JSONObject(dataBuilder.toString())
                            val sid = data.optString("session_id", "")
                            val payload = data.optJSONObject("data") ?: JSONObject()

                            when (eventType) {
                                "choices_presented" -> {
                                    val p = payload.optString("preamble", "")
                                    val choicesArr = payload.optJSONArray("choices") ?: JSONArray()
                                    val choiceList = mutableListOf<Choice>()
                                    for (i in 0 until choicesArr.length()) {
                                        val c = choicesArr.getJSONObject(i)
                                        choiceList.add(Choice(
                                            label = c.optString("label", ""),
                                            summary = c.optString("summary", ""),
                                        ))
                                    }
                                    withContext(Dispatchers.Main) {
                                        onChoicesPresented(sid, p, choiceList)
                                    }
                                }
                                "speech_requested" -> {
                                    val text = payload.optString("text", "")
                                    val blocking = payload.optBoolean("blocking", false)
                                    val priority = payload.optInt("priority", 0)
                                    withContext(Dispatchers.Main) {
                                        onSpeechRequested(sid, text, blocking, priority)
                                    }
                                }
                                "selection_made" -> {
                                    val label = payload.optString("label", "")
                                    val summary = payload.optString("summary", "")
                                    withContext(Dispatchers.Main) {
                                        onSelectionMade(sid, label, summary)
                                    }
                                }
                                "recording_state" -> {
                                    val recording = payload.optBoolean("recording", false)
                                    withContext(Dispatchers.Main) {
                                        onRecordingState(sid, recording)
                                    }
                                }
                                "session_created" -> {
                                    val name = payload.optString("name", "Agent")
                                    withContext(Dispatchers.Main) {
                                        onSessionCreated(sid, name)
                                    }
                                }
                                "session_removed" -> {
                                    withContext(Dispatchers.Main) {
                                        onSessionRemoved(sid)
                                    }
                                }
                            }
                        } catch (e: Exception) {
                            Log.w(TAG, "Failed to parse SSE event: ${e.message}")
                        }
                        dataBuilder.clear()
                        eventType = ""
                    }
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "SSE connection failed: ${e.message}")
        }

        withContext(Dispatchers.Main) { onDisconnected() }
        delay(3000)
    }
}

suspend fun sendSelection(apiBase: String, sessionId: String, label: String, summary: String) {
    try {
        val url = URL("$apiBase/api/sessions/$sessionId/select")
        val connection = url.openConnection() as HttpURLConnection
        connection.requestMethod = "POST"
        connection.setRequestProperty("Content-Type", "application/json")
        connection.doOutput = true

        val body = JSONObject().apply {
            put("label", label)
            put("summary", summary)
        }
        connection.outputStream.write(body.toString().toByteArray())
        connection.responseCode
    } catch (e: Exception) {
        Log.e(TAG, "Failed to send selection: ${e.message}")
    }
}

suspend fun sendHighlight(apiBase: String, sessionId: String, index: Int) {
    try {
        val url = URL("$apiBase/api/sessions/$sessionId/highlight")
        val connection = url.openConnection() as HttpURLConnection
        connection.requestMethod = "POST"
        connection.setRequestProperty("Content-Type", "application/json")
        connection.doOutput = true

        val body = JSONObject().apply {
            put("index", index)
        }
        connection.outputStream.write(body.toString().toByteArray())
        connection.responseCode
    } catch (e: Exception) {
        Log.e(TAG, "Failed to send highlight: ${e.message}")
    }
}

suspend fun sendKey(apiBase: String, sessionId: String, key: String) {
    if (sessionId.isEmpty()) return
    try {
        val url = URL("$apiBase/api/sessions/$sessionId/key")
        val connection = url.openConnection() as HttpURLConnection
        connection.requestMethod = "POST"
        connection.setRequestProperty("Content-Type", "application/json")
        connection.doOutput = true

        val body = JSONObject().apply {
            put("key", key)
        }
        connection.outputStream.write(body.toString().toByteArray())
        connection.responseCode
    } catch (e: Exception) {
        Log.e(TAG, "Failed to send key: ${e.message}")
    }
}

suspend fun sendMessage(apiBase: String, sessionId: String, text: String) {
    try {
        val url = URL("$apiBase/api/sessions/$sessionId/message")
        val connection = url.openConnection() as HttpURLConnection
        connection.requestMethod = "POST"
        connection.setRequestProperty("Content-Type", "application/json")
        connection.doOutput = true

        val body = JSONObject().apply {
            put("text", text)
        }
        connection.outputStream.write(body.toString().toByteArray())
        connection.responseCode
    } catch (e: Exception) {
        Log.e(TAG, "Failed to send message: ${e.message}")
    }
}

suspend fun fetchSessions(apiBase: String): List<SessionInfo> {
    val url = URL("$apiBase/api/sessions")
    val connection = url.openConnection() as HttpURLConnection
    connection.connectTimeout = 3000

    val response = connection.inputStream.bufferedReader().readText()
    val json = JSONObject(response)
    val sessionsArr = json.optJSONArray("sessions") ?: return emptyList()

    return (0 until sessionsArr.length()).map { i ->
        val s = sessionsArr.getJSONObject(i)
        SessionInfo(
            id = s.optString("id", ""),
            name = s.optString("name", ""),
            active = s.optBoolean("active", false),
        )
    }
}
