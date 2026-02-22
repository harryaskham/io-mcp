package com.iomcp.app

import android.os.Bundle
import android.speech.tts.TextToSpeech
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.*
import org.json.JSONObject
import org.json.JSONArray
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.util.Locale

private const val TAG = "IoMcpApp"
private const val API_BASE = "http://localhost:8445"

class MainActivity : ComponentActivity(), TextToSpeech.OnInitListener {
    private var tts: TextToSpeech? = null
    private var ttsReady = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        tts = TextToSpeech(this, this)

        setContent {
            MaterialTheme(colorScheme = darkColorScheme()) {
                IoMcpScreen(
                    onSpeak = { text -> speak(text) },
                    onStopSpeaking = { tts?.stop() },
                )
            }
        }
    }

    override fun onInit(status: Int) {
        if (status == TextToSpeech.SUCCESS) {
            tts?.language = Locale.US
            tts?.setSpeechRate(1.3f)
            ttsReady = true
            Log.i(TAG, "TTS initialized")
        }
    }

    private fun speak(text: String) {
        if (ttsReady) {
            tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "io-mcp")
        }
    }

    override fun onDestroy() {
        tts?.shutdown()
        super.onDestroy()
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun IoMcpScreen(
    onSpeak: (String) -> Unit,
    onStopSpeaking: () -> Unit,
) {
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
                    onSpeak(p)
                },
                onSpeechRequested = { _, text, _, _ ->
                    onSpeak(text)
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

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("io-mcp") },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.primaryContainer,
                ),
            )
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
                    sessions.forEachIndexed { index, session ->
                        Tab(
                            selected = session.id == sessionId,
                            onClick = { sessionId = session.id },
                            text = { Text(session.name) },
                        )
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
                                onStopSpeaking()
                                onSpeak("Selected: ${choice.label}")

                                // Send selection to server
                                scope.launch(Dispatchers.IO) {
                                    sendSelection(sessionId, choice.label, choice.summary)
                                    withContext(Dispatchers.Main) {
                                        choices = emptyList()
                                        preamble = ""
                                        statusText = "Selected: ${choice.label} — waiting..."
                                    }
                                }
                            },
                            onFocus = {
                                if (index != selectedIndex) {
                                    selectedIndex = index
                                    haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                    onStopSpeaking()
                                    onSpeak("${index + 1}. ${choice.label}. ${choice.summary}")
                                }
                            },
                        )
                    }
                }
            } else if (statusText.isEmpty()) {
                // Waiting state
                Box(
                    modifier = Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        text = "Waiting for agent...",
                        style = MaterialTheme.typography.bodyLarge,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
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
    onFocus: () -> Unit,
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
    onDisconnected: () -> Unit,
) {
    while (true) {
        try {
            val url = URL("$API_BASE/api/events")
            val connection = url.openConnection() as HttpURLConnection
            connection.setRequestProperty("Accept", "text/event-stream")
            connection.connectTimeout = 5000
            connection.readTimeout = 0 // no timeout for SSE

            val reader = BufferedReader(InputStreamReader(connection.inputStream))
            onConnected()

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
                        // Process complete event
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

        withContext(Dispatchers.Main) {
            onDisconnected()
        }
        delay(3000) // Reconnect after 3 seconds
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
        connection.responseCode // trigger the request
    } catch (e: Exception) {
        Log.e(TAG, "Failed to send selection: ${e.message}")
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
