"""Voice input mixin for IoMcpApp.

Contains voice recording, transcription, and STT-related methods.
Mixed into IoMcpApp via multiple inheritance.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Optional, TYPE_CHECKING

from textual.widgets import Input, Label, ListView

from ..tts import _find_binary

from .widgets import SubmitTextArea, _safe_action

if TYPE_CHECKING:
    from .app import IoMcpApp


class VoiceMixin:
    """Mixin providing voice input and recording action methods."""

    def _safe_call_from_thread(self, fn, *args) -> None:
        """Call a function via call_from_thread, silencing errors if app is shutting down."""
        try:
            self.call_from_thread(fn, *args)
        except Exception:
            pass

    def action_voice_input(self) -> None:
        """Toggle voice recording mode.
        Works both for choice selection and message queueing (when _message_mode is True).
        """
        session = self._focused()
        if not session:
            return
        # Allow voice input even when session is not active (for message queueing)
        if not session.active and not self._message_mode:
            return
        if session.voice_recording:
            self._stop_voice_recording()
        else:
            # Hide the freeform input if we're in message mode
            if self._message_mode:
                inp = self.query_one("#freeform-input", SubmitTextArea)
                inp.styles.display = "none"
            self._start_voice_recording()

    def _start_voice_recording(self) -> None:
        """Start recording audio via termux-microphone-record.

        Uses termux-exec to invoke termux-microphone-record in native Termux
        (outside proot) which has access to Android mic hardware. On stop,
        the recorded file is converted via ffmpeg and piped to stt --stdin.
        """
        session = self._focused()
        if not session:
            return
        session.voice_recording = True
        session.reading_options = False

        # Audio cue for recording start
        self._tts.play_chime("record_start")

        # Emit recording state for remote frontends
        try:
            frontend_api.emit_recording_state(session.session_id, True)
        except Exception:
            pass

        # Mute TTS — stops current audio and prevents any new playback
        # until unmute() is called in _stop_voice_recording.
        # Graceful fallback if TTSEngine predates mute() (pre-reload).
        if hasattr(self._tts, 'mute'):
            self._tts.mute()
        else:
            self._tts.stop()
            self._tts._muted = True

        # UI update
        self.query_one("#choices").display = False
        self.query_one("#dwell-bar").display = False
        status = self.query_one("#status", Label)
        status.update(f"[bold {self._cs['error']}]o REC[/bold {self._cs['error']}] Recording... [dim](space to stop)[/dim]")
        status.display = True

        # Find binaries
        termux_exec_bin = _find_binary("termux-exec")
        stt_bin = _find_binary("stt")

        if not termux_exec_bin:
            session.voice_recording = False
            self._tts.speak_async("termux-exec not found — cannot record audio")
            self._restore_choices()
            return

        if not stt_bin:
            session.voice_recording = False
            self._tts.speak_async("stt tool not found")
            self._restore_choices()
            return

        # Record to shared storage (accessible from both native Termux and proot)
        rec_dir = "/sdcard/io-mcp"
        os.makedirs(rec_dir, exist_ok=True)
        self._voice_rec_file = os.path.join(rec_dir, "voice-recording.ogg")
        # Native Termux sees /storage/emulated/0 instead of /sdcard
        native_rec_file = "/storage/emulated/0/io-mcp/voice-recording.ogg"

        try:
            # Start recording via termux-exec (runs in native Termux context)
            self._voice_process = subprocess.Popen(
                [termux_exec_bin, "termux-microphone-record",
                 "-f", native_rec_file,
                 "-e", "opus", "-r", "24000", "-c", "1"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            session.voice_recording = False
            self._tts.speak_async(f"Voice input failed: {str(e)[:80]}")
            self._voice_process = None
            self._restore_choices()

    def _stop_voice_recording(self) -> None:
        """Stop recording and process transcription.

        Stops termux-microphone-record, then runs ffmpeg to convert the
        recorded opus file to raw PCM16 24kHz mono, piped into stt --stdin.
        """
        session = self._focused()
        if not session:
            return
        session.voice_recording = False

        # Audio cue for recording stop
        self._tts.play_chime("record_stop")
        proc = self._voice_process
        self._voice_process = None

        # Emit recording state for remote frontends
        try:
            frontend_api.emit_recording_state(session.session_id, False)
        except Exception:
            pass

        status = self.query_one("#status", Label)
        status.update(f"[{self._cs['blue']}]⧗[/{self._cs['blue']}] Transcribing...")

        def _process():
            termux_exec_bin = _find_binary("termux-exec")
            stt_bin = _find_binary("stt")
            rec_file = getattr(self, '_voice_rec_file', None)

            # Stop the recording
            if termux_exec_bin:
                try:
                    subprocess.run(
                        [termux_exec_bin, "termux-microphone-record", "-q"],
                        timeout=5, capture_output=True,
                    )
                except Exception:
                    pass

            # Wait for the record process to finish
            if proc:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            # Unmute TTS now that recording is stopped
            if hasattr(self._tts, 'unmute'):
                self._tts.unmute()
            else:
                self._tts._muted = False

            # Check file exists
            if not rec_file or not os.path.isfile(rec_file):
                self._tts.speak_async("No recording file found. Back to choices.")
                self._safe_call_from_thread(self._restore_choices)
                return

            # Convert and transcribe: ffmpeg → stt --stdin
            env = os.environ.copy()
            env["PULSE_SERVER"] = os.environ.get("PULSE_SERVER", "127.0.0.1")
            env["LD_LIBRARY_PATH"] = PORTAUDIO_LIB

            try:
                # Convert recorded audio to WAV for direct API upload
                ffmpeg_bin = _find_binary("ffmpeg")
                if not ffmpeg_bin:
                    self._tts.speak_async("ffmpeg not found")
                    self._safe_call_from_thread(self._restore_choices)
                    return

                # Convert to WAV (for direct API upload, not piped through VAD)
                import tempfile
                wav_file = os.path.join(tempfile.gettempdir(), "io-mcp-stt.wav")
                ffmpeg_result = subprocess.run(
                    [ffmpeg_bin, "-y", "-i", rec_file,
                     "-ar", "24000", "-ac", "1", wav_file],
                    capture_output=True, timeout=30,
                )
                if ffmpeg_result.returncode != 0:
                    self._tts.speak_async("Audio conversion failed")
                    self._safe_call_from_thread(self._restore_choices)
                    return

                # Try direct API transcription first (faster, no VAD chunking)
                transcript = ""
                stderr_text = ""
                if self._config and self._config.stt_api_key:
                    transcript = self._transcribe_via_api(wav_file)

                # Fallback to stt CLI if API call failed
                if not transcript:
                    ffmpeg_proc = subprocess.Popen(
                        [ffmpeg_bin, "-y", "-i", rec_file,
                         "-f", "s16le", "-ar", "24000", "-ac", "1", "-"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )

                    # Build stt command from config (explicit flags)
                    if self._config:
                        stt_args = [stt_bin] + self._config.stt_cli_args()
                    else:
                        stt_args = [stt_bin, "--stdin"]

                    stt_proc = subprocess.Popen(
                        stt_args,
                        stdin=ffmpeg_proc.stdout,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                    )
                    ffmpeg_proc.stdout.close()
                    stdout, stderr = stt_proc.communicate(timeout=120)
                    transcript = stdout.decode("utf-8", errors="replace").strip()
                    stderr_text = stderr.decode("utf-8", errors="replace").strip() if stderr else ""

                # Clean up WAV
                try:
                    os.unlink(wav_file)
                except Exception:
                    pass
            except Exception as e:
                transcript = ""
                stderr_text = str(e)
            finally:
                # Clean up recording file
                try:
                    os.unlink(rec_file)
                except Exception:
                    pass

            if transcript:
                self._tts.stop()

                # If in message queue mode, queue instead of selecting
                if self._message_mode:
                    self._message_mode = False
                    msgs = getattr(session, 'pending_messages', None)
                    if msgs is not None:
                        msgs.append(transcript)
                    count = len(msgs) if msgs else 1
                    self._tts.speak_async(f"Message queued: {transcript[:50]}. {count} pending.")
                    if session.active:
                        self._safe_call_from_thread(self._restore_choices)
                    else:
                        self._safe_call_from_thread(self._show_session_waiting, session)
                else:
                    self._tts.speak_async(f"Got: {transcript}")

                    wrapped = (
                        f"<transcription>\n{transcript}\n</transcription>\n"
                        "Note: This is a speech-to-text transcription that may contain "
                        "slight errors or similar-sounding words. Please interpret "
                        "charitably. If completely uninterpretable, present the same "
                        "options again and ask the user to retry."
                    )
                    self._resolve_selection(session, {"selected": wrapped, "summary": "(voice input)"})
                    self._safe_call_from_thread(self._show_waiting, f"🎙 {transcript[:50]}")
            else:
                if stderr_text:
                    self._tts.speak_async(f"Recording failed: {stderr_text[:100]}")
                else:
                    self._tts.speak_async("No speech detected. Back to choices.")
                self._safe_call_from_thread(self._restore_choices)

        threading.Thread(target=_process, daemon=True).start()

    def _restore_choices(self) -> None:
        """Restore the choices UI after voice/settings/input mode.

        If the focused session has active choices, rebuilds the choice list
        via _show_choices() (choices may have arrived while input was open).
        Otherwise just re-shows the existing list.
        """
        session = self._focused()
        if session and session.active and session.choices:
            self._show_choices()
            return

        self.query_one("#status").display = False
        self.query_one("#choices").display = True
        list_view = self.query_one("#choices", ListView)
        list_view.focus()
        if self._dwell_time > 0:
            self.query_one("#dwell-bar").display = True
            self._start_dwell()

    def _show_notifications(self) -> None:
        """Fetch and display Android notifications via termux-notification-list.

        Shows notifications in the UI and reads a summary via TTS.
        """
        import json as json_mod

        termux_exec_bin = _find_binary("termux-exec")
        if not termux_exec_bin:
            self._tts.speak_async("termux-exec not found. Can't check notifications.")
            return

        self._tts.speak_async("Checking notifications")

        # Show loading state
        status = self.query_one("#status", Label)
        status.update(f"[{self._cs['blue']}]⧗[/{self._cs['blue']}] Checking notifications...")
        status.display = True
        self.query_one("#choices").display = False

        def _fetch():
            try:
                result = subprocess.run(
                    [termux_exec_bin, "termux-notification-list"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode != 0:
                    self._tts.speak_async("Failed to get notifications")
                    self._safe_call_from_thread(self._restore_choices)
                    return

                notifications = json_mod.loads(result.stdout)
                if not notifications:
                    self._tts.speak_async("No notifications")
                    self._safe_call_from_thread(self._restore_choices)
                    return

                # Filter to interesting notifications (skip system/ongoing)
                interesting = []
                for n in notifications:
                    title = n.get("title", "")
                    content = n.get("content", "")
                    pkg = n.get("packageName", "")
                    # Skip io-mcp's own and common system notifications
                    if any(skip in pkg for skip in ["termux", "android.system", "inputmethod"]):
                        continue
                    if title or content:
                        # Shorten package name for readability
                        app_name = pkg.split(".")[-1] if pkg else "unknown"
                        interesting.append({
                            "app": app_name,
                            "title": title,
                            "content": content,
                        })

                if not interesting:
                    self._tts.speak_async("No new notifications")
                    self._safe_call_from_thread(self._restore_choices)
                    return

                # Read out notifications — batch into one TTS call for speed
                count = len(interesting)
                parts = [f"{count} notification{'s' if count != 1 else ''}."]

                for n in interesting[:5]:  # limit to 5
                    title = n['title'][:60] if n['title'] else ""
                    content = n['content'][:40] if n['content'] and n['content'] != n['title'] else ""
                    text = f"{n['app']}: {title}"
                    if content:
                        text += f". {content}"
                    parts.append(text)

                self._tts.speak(" ".join(parts))
                self._safe_call_from_thread(self._restore_choices)

            except Exception as e:
                self._tts.speak_async(f"Notification check failed: {str(e)[:60]}")
                self._safe_call_from_thread(self._restore_choices)

        threading.Thread(target=_fetch, daemon=True).start()

    def _transcribe_via_api(self, wav_path: str) -> str:
        """Send a WAV file directly to the transcription API.

        Bypasses the stt tool's VAD pipeline — sends the entire recording
        as a single API request for faster, more reliable transcription
        of pre-recorded audio.

        Returns the transcript text, or empty string on failure.
        """
        import urllib.request
        import uuid

        if not self._config:
            return ""

        model = self._config.stt_model_name
        api_key = self._config.stt_api_key
        base_url = self._config.stt_base_url

        if not api_key:
            return ""

        # mai-ears-1 uses a different API endpoint (chat completions)
        # Fall back to stt CLI for that model
        if model == "mai-ears-1":
            return ""

        try:
            with open(wav_path, "rb") as f:
                wav_data = f.read()

            # Build multipart/form-data
            boundary = uuid.uuid4().hex
            body = (
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                    f"Content-Type: audio/wav\r\n\r\n"
                ).encode()
                + wav_data
                + (
                    f"\r\n--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="model"\r\n\r\n'
                    f"{model}\r\n"
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="response_format"\r\n\r\n'
                    f"json\r\n"
                    f"--{boundary}--\r\n"
                ).encode()
            )

            url = f"{base_url.rstrip('/')}/v1/audio/transcriptions"
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                method="POST",
            )

            import json as json_mod
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json_mod.loads(resp.read())
                return result.get("text", "").strip()

        except Exception:
            return ""

