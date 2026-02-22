package com.iomcp.app

import android.os.Bundle
import android.util.Log
import android.view.KeyEvent
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.focusable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.key.*
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
private const val API_BASE = "http://localhost:8445"

/**
 * Stateless frontend for io-mcp.
 *
 * This app mirrors the TUI state — it shows choices, accepts selections,
 * and forwards actions to the Python server. All TTS is handled by the TUI
 * running in Termux. The Android app provides:
 * - Visual display of choices and session state
 * - Touch-based selection with haptic feedback
 * - Freeform text input for messages/replies
 * - No TTS (avoids duplicate audio with TUI)
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme()) {
                IoMcpScreen()
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun IoMcpScreen() {
    val scope = rememberCoroutineScope()
    val haptic = LocalHapticFeedback.current

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

    // SSE connection
    LaunchedEffect(Unit) {
        scope.launch(Dispatchers.IO) {
            connectToSSE(
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
                onDisconnected = {
                    connected = false
                    statusText = "Disconnected. Reconnecting..."
                },
            )
        }
    }

    // Periodically fetch sessions
    LaunchedEffect(connected) {
        if (connected) {
            while (true) {
                delay(5000)
                try {
                    sessions = fetchSessions()
                } catch (_: Exception) {}
            }
        }
    }

    // Focus requester for key events
    val focusRequester = remember { FocusRequester() }

    // Request focus on mount so key events work
    LaunchedEffect(Unit) {
        focusRequester.requestFocus()
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("io-mcp") },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.primaryContainer,
                ),
            )
        },
        modifier = Modifier
            .focusRequester(focusRequester)
            .focusable()
            .onKeyEvent { event ->
                if (event.type == KeyEventType.KeyDown) {
                    when (event.key) {
                        Key.J, Key.DirectionDown -> {
                            haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            scope.launch(Dispatchers.IO) { sendKey(sessionId, "j") }
                            true
                        }
                        Key.K, Key.DirectionUp -> {
                            haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            scope.launch(Dispatchers.IO) { sendKey(sessionId, "k") }
                            true
                        }
                        Key.Enter -> {
                            haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                            scope.launch(Dispatchers.IO) { sendKey(sessionId, "enter") }
                            true
                        }
                        Key.Spacebar -> {
                            haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                            scope.launch(Dispatchers.IO) { sendKey(sessionId, "space") }
                            true
                        }
                        else -> false
                    }
                } else false
            },
    ) { padding ->
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
                            sendHighlight(sessionId, centerIndex + 1)
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
                                    sendSelection(sessionId, choice.label, choice.summary)
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
                            scope.launch(Dispatchers.IO) { sendKey(sessionId, "space") }
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
                                    sendMessage(sessionId, msg)
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
                                sendMessage(sessionId, msg)
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

// ─── Data classes ─────────────────────────────────────────────────────

data class Choice(val label: String, val summary: String)
data class SessionInfo(val id: String, val name: String, val active: Boolean)

// ─── Network functions ────────────────────────────────────────────────

suspend fun connectToSSE(
    onConnected: () -> Unit,
    onChoicesPresented: (sessionId: String, preamble: String, choices: List<Choice>) -> Unit,
    onSpeechRequested: (sessionId: String, text: String, blocking: Boolean, priority: Int) -> Unit,
    onSelectionMade: (sessionId: String, label: String, summary: String) -> Unit,
    onRecordingState: (sessionId: String, recording: Boolean) -> Unit,
    onDisconnected: () -> Unit,
) {
    while (true) {
        try {
            val url = URL("$API_BASE/api/events")
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

suspend fun sendSelection(sessionId: String, label: String, summary: String) {
    try {
        val url = URL("$API_BASE/api/sessions/$sessionId/select")
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

suspend fun sendHighlight(sessionId: String, index: Int) {
    try {
        val url = URL("$API_BASE/api/sessions/$sessionId/highlight")
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

suspend fun sendKey(sessionId: String, key: String) {
    if (sessionId.isEmpty()) return
    try {
        val url = URL("$API_BASE/api/sessions/$sessionId/key")
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

suspend fun sendMessage(sessionId: String, text: String) {
    try {
        val url = URL("$API_BASE/api/sessions/$sessionId/message")
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

suspend fun fetchSessions(): List<SessionInfo> {
    val url = URL("$API_BASE/api/sessions")
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
