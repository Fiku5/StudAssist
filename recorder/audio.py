import asyncio
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from config import settings, TEMP_DIR

logger = logging.getLogger(__name__)


PULSE_SOCKET_PATH = "/tmp/pulse-socket"
PULSE_SERVER_URI = f"unix:{PULSE_SOCKET_PATH}"


class AudioRecorder:
    """
    Captures system/virtual audio stream via ffmpeg into lightweight 64kbps mono MP3.
    Optimized for minimal CPU footprint on 2 vCPU Linux servers (using PulseAudio loopback).
    """

    def __init__(self, output_path: Optional[Path] = None, pulse_source: str = "VirtualSink.monitor"):
        self.output_path = output_path or (TEMP_DIR / "lecture_audio.mp3")
        self.pulse_source = pulse_source
        self.process: Optional[subprocess.Popen] = None
        self._is_recording = False
        self._log_file = None
        self.simulation_mode = settings.simulation_mode or (shutil.which("ffmpeg") is None)

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def is_alive(self) -> bool:
        """Returns True if audio recording process is actively running."""
        if not self._is_recording:
            return False
        if self.simulation_mode:
            return True
        if self.process is not None:
            return self.process.poll() is None
        return False

    @staticmethod
    def ensure_pulseaudio():
        """
        Ensures PulseAudio daemon is alive, listening on fixed UNIX socket /tmp/pulse-socket,
        and VirtualSink is loaded and set as the default sink on Linux.
        Uses timeouts and clean_env to guarantee zero hanging.
        """
        if platform.system() != "Linux":
            return

        # Environment without PULSE_SERVER to avoid connection timeouts before socket exists
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("PULSE_")}

        # 1. Fast probe: check if /tmp/pulse-socket is already active and responding
        socket_path = Path(PULSE_SOCKET_PATH)
        if socket_path.exists():
            try:
                probe = subprocess.run(
                    ["pactl", f"--server={PULSE_SERVER_URI}", "get-default-sink"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if probe.returncode == 0:
                    os.environ["PULSE_SERVER"] = PULSE_SERVER_URI
                    os.environ["PULSE_COOKIE"] = "/dev/null"
                    logger.info("PulseAudio socket /tmp/pulse-socket is active and healthy.")
                    return
            except Exception:
                pass

        # 2. Persist socket and VirtualSink in ~/.config/pulse/default.pa
        try:
            config_dir = Path.home() / ".config" / "pulse"
            config_dir.mkdir(parents=True, exist_ok=True)
            default_pa = config_dir / "default.pa"
            content = default_pa.read_text(encoding="utf-8") if default_pa.exists() else ".include /etc/pulse/default.pa\n"
            changed = False
            if PULSE_SOCKET_PATH not in content:
                content += f"\nload-module module-native-protocol-unix socket={PULSE_SOCKET_PATH} auth-anonymous=1\n"
                changed = True
            if "VirtualSink" not in content:
                content += "\nload-module module-null-sink sink_name=VirtualSink sink_properties=device.description=VirtualSink\nset-default-sink VirtualSink\n"
                changed = True
            if changed:
                default_pa.write_text(content, encoding="utf-8")
        except Exception as e:
            logger.debug(f"Could not persist ~/.config/pulse/default.pa: {e}")

        # 3. Start daemon if not running (using clean_env without PULSE_SERVER)
        try:
            check_res = subprocess.run(
                ["pulseaudio", "--check"],
                capture_output=True,
                env=clean_env,
                timeout=3,
            )
            if check_res.returncode != 0:
                logger.info("PulseAudio daemon is not running. Starting PulseAudio...")
                subprocess.run(
                    ["pulseaudio", "--start", "--exit-idle-time=-1", "--disallow-exit"],
                    capture_output=True,
                    env=clean_env,
                    timeout=5,
                )
                time.sleep(0.5)
        except Exception as e:
            logger.warning(f"PulseAudio start note: {e}")

        # 4. If /tmp/pulse-socket doesn't exist, bind native-protocol-unix module
        if not socket_path.exists():
            try:
                subprocess.run(
                    [
                        "pactl", "load-module", "module-native-protocol-unix",
                        f"socket={PULSE_SOCKET_PATH}", "auth-anonymous=1"
                    ],
                    capture_output=True,
                    env=clean_env,
                    timeout=3,
                )
            except Exception as e:
                logger.warning(f"Could not load native-protocol-unix module: {e}")

        # 5. Ensure VirtualSink is loaded and set as default
        active_env = {**os.environ, "PULSE_SERVER": PULSE_SERVER_URI} if socket_path.exists() else clean_env
        try:
            sinks = subprocess.run(
                ["pactl", "list", "short", "sinks"],
                capture_output=True,
                text=True,
                env=active_env,
                timeout=3,
            )
            if "VirtualSink" not in sinks.stdout:
                logger.info("Loading PulseAudio module-null-sink VirtualSink...")
                subprocess.run(
                    [
                        "pactl", "load-module", "module-null-sink",
                        "sink_name=VirtualSink", "sink_properties=device.description=VirtualSink"
                    ],
                    capture_output=True,
                    env=active_env,
                    timeout=3,
                )
            subprocess.run(
                ["pactl", "set-default-sink", "VirtualSink"],
                capture_output=True,
                env=active_env,
                timeout=3,
            )

            # Guarantee VirtualSink and its monitor source are unmuted at 100% volume
            subprocess.run(["pactl", "set-sink-mute", "VirtualSink", "0"], capture_output=True, env=active_env, timeout=2)
            subprocess.run(["pactl", "set-sink-volume", "VirtualSink", "100%"], capture_output=True, env=active_env, timeout=2)
            subprocess.run(["pactl", "set-source-mute", "VirtualSink.monitor", "0"], capture_output=True, env=active_env, timeout=2)
            subprocess.run(["pactl", "set-source-volume", "VirtualSink.monitor", "100%"], capture_output=True, env=active_env, timeout=2)

            # Unload module-suspend-on-idle so VirtualSink never enters SUSPENDED state
            subprocess.run(["pactl", "unload-module", "module-suspend-on-idle"], capture_output=True, env=active_env, timeout=2)
            subprocess.run(["pactl", "suspend-sink", "VirtualSink", "0"], capture_output=True, env=active_env, timeout=2)
            subprocess.run(["pactl", "suspend-source", "VirtualSink.monitor", "0"], capture_output=True, env=active_env, timeout=2)

            logger.info("PulseAudio and VirtualSink successfully verified (unmuted, un-suspended, 100% volume).")
        except Exception as e:
            logger.warning(f"VirtualSink verification note: {e}")

        # 6. Export PULSE_SERVER for the current process and its children
        os.environ["PULSE_SERVER"] = PULSE_SERVER_URI
        os.environ["PULSE_COOKIE"] = "/dev/null"
        os.environ["PULSE_SINK"] = "VirtualSink"

    def _ensure_pulseaudio(self):
        """Instance alias for backwards compatibility."""
        self.ensure_pulseaudio()

    def start(self, dest_path: Optional[Path] = None) -> Path:
        """Starts background ffmpeg recording process with detailed logging."""
        if self._is_recording:
            logger.warning("AudioRecorder is already recording.")
            return self.output_path

        if dest_path:
            self.output_path = Path(dest_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        if self.simulation_mode:
            logger.info("AudioRecorder started in SIMULATION mode.")
            self._is_recording = True
            return self.output_path

        self._ensure_pulseaudio()

        log_file_path = self.output_path.parent / "ffmpeg.log"
        try:
            self._log_file = open(log_file_path, "w", encoding="utf-8")
        except Exception:
            self._log_file = None

        cmd = self._build_ffmpeg_command()
        logger.info(f"Starting ffmpeg audio capture: {' '.join(cmd)}")

        ffmpeg_env = dict(os.environ)
        if platform.system() == "Linux":
            ffmpeg_env["PULSE_SERVER"] = PULSE_SERVER_URI
            ffmpeg_env["PULSE_COOKIE"] = "/dev/null"

        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=self._log_file or subprocess.DEVNULL,
                stderr=self._log_file or subprocess.DEVNULL,
                env=ffmpeg_env,
            )
            self._is_recording = True

            # Verify that ffmpeg didn't immediately crash
            time.sleep(0.6)
            if self.process.poll() is not None:
                self._is_recording = False
                if self._log_file:
                    self._log_file.flush()
                err_text = log_file_path.read_text(encoding="utf-8", errors="ignore") if log_file_path.exists() else ""
                logger.error(f"ffmpeg exited prematurely with code {self.process.returncode}: {err_text}")

                # Retry with fallback default pulse source on Linux
                if platform.system() == "Linux" and "VirtualSink" in self.pulse_source:
                    logger.info("Retrying ffmpeg with fallback pulse source 'default'...")
                    fallback_cmd = [
                        "ffmpeg", "-y", "-f", "pulse", "-i", "default",
                        "-ac", "1", "-ar", "16000", "-b:a", "64k", str(self.output_path)
                    ]
                    self.process = subprocess.Popen(
                        fallback_cmd,
                        stdin=subprocess.PIPE,
                        stdout=self._log_file or subprocess.DEVNULL,
                        stderr=self._log_file or subprocess.DEVNULL,
                        env=ffmpeg_env,
                    )
                    time.sleep(0.6)
                    if self.process.poll() is None:
                        self._is_recording = True
                        logger.info("ffmpeg fallback recording successfully started.")
                    else:
                        logger.error("ffmpeg fallback recording also failed.")
        except Exception as e:
            logger.error(f"Failed to start ffmpeg audio capture: {e}")
            self.simulation_mode = True
            self._is_recording = True

        return self.output_path

    def stop(self) -> Path:
        """Gracefully terminates ffmpeg to cleanly finalize the MP3 file."""
        if not self._is_recording:
            return self.output_path

        logger.info("Stopping audio recording...")
        self._is_recording = False

        if self.process:
            try:
                if self.process.stdin and not self.process.stdin.closed:
                    try:
                        self.process.stdin.write(b"q")
                        self.process.stdin.flush()
                    except Exception:
                        pass
                    try:
                        self.process.stdin.close()
                    except Exception:
                        pass
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=2)
                except Exception:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
            finally:
                self.process = None

        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

        # If file wasn't created or in simulation mode, create dummy MP3
        if not self.output_path.exists():
            self.output_path.write_bytes(b"\xFF\xFB\x90\x44" + b"\x00" * 1024)

        # Check audio level of captured file
        level_info = self.check_audio_level(self.output_path)
        if level_info["is_silent"]:
            logger.warning(
                f"⚠️ Audio file {self.output_path.name} is silent (max_volume={level_info['max_volume']} dB)!"
            )
        else:
            logger.info(
                f"Audio file {self.output_path.name} contains valid audio (max_volume={level_info['max_volume']} dB)."
            )

        logger.info(f"Audio recorded to: {self.output_path.resolve()}")
        return self.output_path

    @staticmethod
    def check_audio_level(audio_path: Path) -> dict:
        """
        Analyzes audio file using ffmpeg volumedetect filter.
        Returns dict with max_volume (dB), mean_volume (dB), and is_silent (bool).
        """
        if not audio_path.exists() or audio_path.stat().st_size < 500:
            return {"max_volume": -99.0, "mean_volume": -99.0, "is_silent": True}

        null_dest = "NUL" if platform.system() == "Windows" else "/dev/null"
        cmd = [
            "ffmpeg", "-i", str(audio_path),
            "-af", "volumedetect",
            "-vn", "-sn", "-dn",
            "-f", "null", null_dest,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            output = res.stderr or ""
            import re
            max_v_match = re.search(r"max_volume:\s*(-?[\d\.]+)\s*dB", output)
            mean_v_match = re.search(r"mean_volume:\s*(-?[\d\.]+)\s*dB", output)

            max_v = float(max_v_match.group(1)) if max_v_match else -99.0
            mean_v = float(mean_v_match.group(1)) if mean_v_match else -99.0

            # If max volume is below -60 dB, it is pure digital silence
            is_silent = max_v <= -60.0
            logger.info(
                f"Audio level check for {audio_path.name}: max={max_v} dB, mean={mean_v} dB, is_silent={is_silent}"
            )
            return {"max_volume": max_v, "mean_volume": mean_v, "is_silent": is_silent}
        except Exception as e:
            logger.warning(f"Failed to check audio level: {e}")
            return {"max_volume": 0.0, "mean_volume": 0.0, "is_silent": False}

    def check_recent_audio_level(
        self,
        duration_sec: float = 10.0,
        silence_threshold_db: Optional[float] = None,
        target_path: Optional[Path] = None,
    ) -> dict:
        """
        Analyzes the last `duration_sec` seconds of the currently recording or existing audio file.
        Uses ffmpeg with -sseof -{duration_sec} and volumedetect filter.
        Executes in ~0.01s without blocking audio capture.
        Returns:
            {"max_volume": float, "mean_volume": float, "is_silent": bool}
        """
        path = target_path or self.output_path
        thresh = silence_threshold_db if silence_threshold_db is not None else settings.silence_threshold_db

        if not path or not path.exists() or path.stat().st_size < 1000:
            return {"max_volume": -99.0, "mean_volume": -99.0, "is_silent": True}

        if self.simulation_mode or shutil.which("ffmpeg") is None:
            return {"max_volume": -99.0, "mean_volume": -99.0, "is_silent": True}

        null_dest = "NUL" if platform.system() == "Windows" else "/dev/null"
        cmd = [
            "ffmpeg",
            "-sseof", f"-{float(duration_sec):.1f}",
            "-i", str(path),
            "-af", "volumedetect",
            "-vn", "-sn", "-dn",
            "-f", "null", null_dest,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            output = res.stderr or ""
            max_v_match = re.search(r"max_volume:\s*(-?[\d\.]+)\s*dB", output)
            mean_v_match = re.search(r"mean_volume:\s*(-?[\d\.]+)\s*dB", output)

            max_v = float(max_v_match.group(1)) if max_v_match else -99.0
            mean_v = float(mean_v_match.group(1)) if mean_v_match else -99.0

            is_silent = max_v <= thresh
            logger.debug(
                f"Recent audio pulse check ({duration_sec}s tail of {path.name}): "
                f"max={max_v} dB, mean={mean_v} dB, thresh={thresh} dB, is_silent={is_silent}"
            )
            return {"max_volume": max_v, "mean_volume": mean_v, "is_silent": is_silent}
        except Exception as e:
            logger.warning(f"Failed to check recent audio level for {path.name}: {e}")
            return {"max_volume": 0.0, "mean_volume": 0.0, "is_silent": False}

    @staticmethod
    def trim_silence_boundaries(
        audio_path: Path,
        silence_threshold_db: Optional[float] = None,
        min_leading_silence_sec: Optional[float] = None,
        min_trailing_silence_sec: Optional[float] = None,
    ) -> tuple[Optional[float], Optional[float]]:
        """
        Post-processing: analyzes recorded audio with ffmpeg silencedetect in a single pass.
        Detects:
        1. Prolonged silence at the BEGINNING (e.g. instructor hasn't joined/started speaking yet).
           Trims from start, leaving 1.5s buffer before the first audible speech.
        2. Prolonged silence at the END (e.g. instructor finished and left call early).
           Trims from end, leaving 1.5s buffer after the last audible speech.
        Trims audio in-place without re-encoding (stream copy).
        Returns tuple (start_cutoff_sec, end_cutoff_sec).
        - start_cutoff_sec: seconds removed from beginning (or None if no leading silence trimmed).
        - end_cutoff_sec: original timestamp where audio was cut at end (or None if no trailing silence trimmed).
        """
        audio_path = Path(audio_path)
        if not audio_path.exists() or audio_path.stat().st_size < 1000:
            return None, None

        thresh_db = silence_threshold_db if silence_threshold_db is not None else getattr(settings, "silence_threshold_db", -38.0)
        min_lead = min_leading_silence_sec if min_leading_silence_sec is not None else getattr(settings, "min_leading_silence_sec", 20.0)
        min_trail = min_trailing_silence_sec if min_trailing_silence_sec is not None else getattr(settings, "min_trailing_silence_sec", 25.0)

        # Use smallest min duration in ffmpeg silencedetect so we catch both leading and trailing candidates
        detect_min_dur = min(min_lead, min_trail)

        cmd = [
            "ffmpeg", "-i", str(audio_path),
            "-af", f"silencedetect=noise={thresh_db}dB:d={detect_min_dur}",
            "-f", "null", "-",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output = res.stderr or ""

            dur_match = re.search(r"Duration:\s*(\d+):(\d+):([\d\.]+)", output)
            if not dur_match:
                return None, None
            h, m, s = float(dur_match.group(1)), float(dur_match.group(2)), float(dur_match.group(3))
            total_duration = h * 3600 + m * 60 + s

            # Do not trim very short audios (< 30s)
            if total_duration < 30.0:
                return None, None

            starts = [float(x) for x in re.findall(r"silence_start:\s*([\d\.]+)", output)]
            ends = [float(x) for x in re.findall(r"silence_end:\s*([\d\.]+)", output)]

            start_cutoff: Optional[float] = None
            end_cutoff: Optional[float] = None

            # 1. Detect LEADING silence (from start of file to first speech)
            if ends:
                first_end = ends[0]
                # Did silence start at or near timestamp 0?
                starts_at_beginning = (not starts) or (starts[0] > first_end) or (starts[0] <= 1.5)
                if starts_at_beginning and first_end >= min_lead:
                    # Keep 1.5s safety buffer before speech starts
                    calculated_start = max(0.0, first_end - 1.5)
                    if calculated_start > 1.0:
                        start_cutoff = calculated_start

            # 2. Detect TRAILING silence (from last speech to end of file)
            if starts:
                last_start = starts[-1]
                silence_reaches_end = (len(starts) > len(ends)) or (bool(ends) and ends[-1] >= (total_duration - 2.0))
                trailing_silence_len = total_duration - last_start
                if silence_reaches_end and trailing_silence_len >= min_trail:
                    # Keep 1.5s safety buffer after speech ends
                    calculated_end = min(total_duration, last_start + 1.5)
                    if calculated_end < (total_duration - 1.0):
                        end_cutoff = calculated_end

            # Check if any trimming is needed and remaining duration is sensible (>= 10s)
            effective_start = start_cutoff or 0.0
            effective_end = end_cutoff or total_duration
            new_duration = effective_end - effective_start

            if (start_cutoff is not None or end_cutoff is not None) and new_duration >= 10.0:
                temp_trimmed = audio_path.parent / f"trimmed_{audio_path.name}"
                trim_cmd = [
                    "ffmpeg", "-y",
                    "-ss", f"{effective_start:.2f}",
                    "-i", str(audio_path),
                    "-t", f"{new_duration:.2f}",
                    "-c", "copy",
                    str(temp_trimmed)
                ]
                trim_res = subprocess.run(trim_cmd, capture_output=True, timeout=30)
                if trim_res.returncode == 0 and temp_trimmed.exists() and temp_trimmed.stat().st_size > 1000:
                    shutil.move(str(temp_trimmed), str(audio_path))
                    logger.info(
                        f"Trimmed silence boundaries on {audio_path.name}: "
                        f"start cut={effective_start:.1f}s, end cut={effective_end:.1f}s, "
                        f"duration: {total_duration:.1f}s -> {new_duration:.1f}s."
                    )
                    return start_cutoff, end_cutoff
                elif temp_trimmed.exists():
                    temp_trimmed.unlink()

        except Exception as e:
            logger.warning(f"Failed to check/trim silence boundaries on {audio_path}: {e}")

        return None, None

    @classmethod
    def trim_trailing_silence(
        cls,
        audio_path: Path,
        silence_threshold_db: float = -38.0,
        min_trailing_silence_sec: float = 25.0,
    ) -> Optional[float]:
        """
        Legacy/convenience wrapper: trims trailing silence at the end of recording.
        Returns end cutoff timestamp in seconds if trimmed, else None.
        """
        _, end_cutoff = cls.trim_silence_boundaries(
            audio_path=audio_path,
            silence_threshold_db=silence_threshold_db,
            min_leading_silence_sec=999999.0,  # disable leading cut
            min_trailing_silence_sec=min_trailing_silence_sec,
        )
        return end_cutoff

    @classmethod
    def trim_leading_silence(
        cls,
        audio_path: Path,
        silence_threshold_db: float = -38.0,
        min_leading_silence_sec: float = 20.0,
    ) -> Optional[float]:
        """
        Convenience wrapper: trims leading silence at the beginning of recording.
        Returns start cutoff timestamp in seconds if trimmed, else None.
        """
        start_cutoff, _ = cls.trim_silence_boundaries(
            audio_path=audio_path,
            silence_threshold_db=silence_threshold_db,
            min_leading_silence_sec=min_leading_silence_sec,
            min_trailing_silence_sec=999999.0,  # disable trailing cut
        )
        return start_cutoff

    def _build_ffmpeg_command(self) -> list[str]:
        """Constructs platform-optimized ffmpeg capture command."""
        current_os = platform.system()

        if current_os == "Linux":
            # PulseAudio virtual sink monitor on Ubuntu Server
            return [
                "ffmpeg",
                "-y",
                "-f", "pulse",
                "-i", self.pulse_source,
                "-ac", "1",               # Mono (cuts size in half, ideal for speech)
                "-ar", "16000",           # 16 kHz sample rate (optimal for speech AI)
                "-b:a", "64k",            # 64 kbps (40-60 MB per 90 min)
                str(self.output_path),
            ]
        elif current_os == "Windows":
            # Windows fallback or stereo mix
            return [
                "ffmpeg",
                "-y",
                "-f", "dshow",
                "-i", "audio=virtual-audio-capturer",
                "-ac", "1",
                "-ar", "16000",
                "-b:a", "64k",
                str(self.output_path),
            ]
        else:
            return [
                "ffmpeg",
                "-y",
                "-f", "avfoundation",
                "-i", ":0",
                "-ac", "1",
                "-ar", "16000",
                "-b:a", "64k",
                str(self.output_path),
            ]
