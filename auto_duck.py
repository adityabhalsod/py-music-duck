from __future__ import annotations

import argparse
import queue
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
import webrtcvad
from pycaw.pycaw import AudioUtilities


@dataclass
class Config:
    # This gets replaced automatically based on selected microphone.
    sample_rate: int = 16_000

    # WebRTC supports 10, 20, or 30 ms frames.
    frame_duration_ms: int = 30

    channels: int = 1

    # 0 = least aggressive
    # 3 = most aggressive
    vad_mode: int = 2

    # Speech must also be louder than this RMS value.
    min_rms: float = 350.0

    # Recent frames used for speech confirmation.
    speech_window_frames: int = 8

    # Minimum positive speech frames.
    required_speech_frames: int = 4

    # Wait this long after speech stops before restoring volume.
    silence_restore_seconds: float = 1.5

    # Volume while somebody is talking.
    duck_volume_percent: float = 15.0

    # Smooth fade.
    fade_duration_seconds: float = 0.25
    fade_steps: int = 10

    max_queue_size: int = 100


class WindowsVolumeController:
    """
    Controls the Windows default output device.

    Uses EndpointVolume directly instead of AudioDevice.volume_percent
    because volume_percent is not available in every pycaw version/API
    combination.
    """

    def __init__(self, config: Config):
        self.config = config

        self.device = AudioUtilities.GetSpeakers()

        # Current pycaw exposes the Windows endpoint-volume interface here.
        self.endpoint_volume = self.device.EndpointVolume

        self._lock = threading.RLock()

        print(f"Output device : {self.device.FriendlyName}")

        # Test that volume control is available immediately.
        try:
            current = self.get_volume()

            print(f"Current volume: {current:.0f}%")

        except Exception as exc:
            raise RuntimeError(
                "Unable to access Windows master volume.\n" f"Error: {exc}"
            ) from exc

    def get_volume(self) -> float:
        """
        Get Windows master volume as percentage.

        Windows Core Audio scalar range:
            0.0 = 0%
            1.0 = 100%
        """

        with self._lock:
            scalar = self.endpoint_volume.GetMasterVolumeLevelScalar()
            return float(scalar) * 100.0

    def set_volume(self, percent: float) -> None:
        """
        Set Windows master volume percentage.
        """

        percent = max(
            0.0,
            min(100.0, percent),
        )

        scalar = percent / 100.0

        with self._lock:

            self.endpoint_volume.SetMasterVolumeLevelScalar(
                scalar,
                None,
            )

    def fade_to(
        self,
        target_percent: float,
    ) -> None:
        """
        Smoothly change Windows volume.
        """

        target_percent = max(
            0.0,
            min(100.0, target_percent),
        )

        start_percent = self.get_volume()

        if abs(start_percent - target_percent) < 0.5:
            return

        steps = max(
            1,
            self.config.fade_steps,
        )

        interval = self.config.fade_duration_seconds / steps

        for step in range(
            1,
            steps + 1,
        ):

            value = start_percent + (target_percent - start_percent) * step / steps

            self.set_volume(value)

            time.sleep(interval)


class SpeechDuckController:
    SUPPORTED_VAD_SAMPLE_RATES = {
        8000,
        16000,
        32000,
        48000,
    }

    def __init__(
        self,
        config: Config,
        input_device: int | None = None,
        requested_sample_rate: int | None = None,
    ):
        self.config = config
        self.input_device = input_device
        self.requested_sample_rate = requested_sample_rate

        self.running = threading.Event()
        self.running.set()

        self.detect_microphone_sample_rate()

        self.vad = webrtcvad.Vad(self.config.vad_mode)

        self.volume = WindowsVolumeController(self.config)

        self.audio_queue: queue.Queue[bytes] = queue.Queue(
            maxsize=self.config.max_queue_size
        )

        self.speech_history: deque[bool] = deque(
            maxlen=self.config.speech_window_frames
        )

        self.ducked = False

        self.original_volume: float | None = None

        self.last_voice_time = 0.0

        self.frames_per_buffer = int(
            self.config.sample_rate * self.config.frame_duration_ms / 1000
        )

    def detect_microphone_sample_rate(
        self,
    ) -> None:
        """
        Determine a WebRTC-compatible sample rate
        supported by the selected microphone.
        """

        device_info = sd.query_devices(
            self.input_device,
            "input",
        )

        detected_rate = int(device_info["default_samplerate"])

        print(f"Detected microphone sample rate: " f"{detected_rate} Hz")

        if self.requested_sample_rate is not None:

            try:

                sd.check_input_settings(
                    device=self.input_device,
                    channels=self.config.channels,
                    dtype="int16",
                    samplerate=self.requested_sample_rate,
                )

                self.config.sample_rate = self.requested_sample_rate

                print(
                    "Using requested sample rate: "
                    f"{self.requested_sample_rate} Hz"
                )

                return

            except sd.PortAudioError as exc:
                raise RuntimeError(
                    "The requested sample rate could not be opened for "
                    "this microphone.\n\n"
                    f"Requested: {self.requested_sample_rate} Hz\n"
                    f"Device default: {detected_rate} Hz"
                ) from exc

        # Best case:
        # microphone already uses WebRTC-supported rate.
        if detected_rate in self.SUPPORTED_VAD_SAMPLE_RATES:

            try:

                sd.check_input_settings(
                    device=self.input_device,
                    channels=self.config.channels,
                    dtype="int16",
                    samplerate=detected_rate,
                )

                self.config.sample_rate = detected_rate

                print(f"Using microphone sample rate: " f"{detected_rate} Hz")

                return

            except sd.PortAudioError:
                pass

        # For 44100 devices, find a compatible
        # WebRTC VAD rate that PortAudio accepts.
        preferred_rates = [
            16000,
            48000,
            32000,
            8000,
        ]

        for rate in preferred_rates:

            try:

                sd.check_input_settings(
                    device=self.input_device,
                    channels=self.config.channels,
                    dtype="int16",
                    samplerate=rate,
                )

                self.config.sample_rate = rate

                print(f"Using compatible sample rate: " f"{rate} Hz")

                return

            except sd.PortAudioError:
                continue

        raise RuntimeError(
            "\nNo WebRTC-compatible sample rate "
            "could be opened for this microphone.\n\n"
            f"Device default: {detected_rate} Hz\n"
            "Tried: 8000, 16000, 32000, 48000 Hz\n"
        )

    @staticmethod
    def calculate_rms(
        pcm_data: bytes,
    ) -> float:
        """
        Calculate microphone signal energy.
        """

        samples = np.frombuffer(
            pcm_data,
            dtype=np.int16,
        ).astype(np.float32)

        if samples.size == 0:
            return 0.0

        return float(np.sqrt(np.mean(samples * samples)))

    def is_speech(
        self,
        pcm_data: bytes,
    ) -> tuple[bool, float]:
        """
        Use RMS + WebRTC VAD.

        RMS filters very quiet noise.
        WebRTC determines whether signal resembles speech.
        """

        rms = self.calculate_rms(pcm_data)

        if rms < self.config.min_rms:
            return False, rms

        try:

            speech = self.vad.is_speech(
                pcm_data,
                self.config.sample_rate,
            )

            return speech, rms

        except Exception as exc:

            print(
                f"VAD error: {exc}",
                file=sys.stderr,
            )

            return False, rms

    def audio_callback(
        self,
        indata,
        frames,
        time_info,
        status,
    ):
        """
        PortAudio callback.

        Don't do volume processing here because this
        callback should remain fast.
        """

        if status:

            print(
                f"Audio status: {status}",
                file=sys.stderr,
            )

        try:

            self.audio_queue.put_nowait(bytes(indata))

        except queue.Full:

            # Remove oldest packet.
            try:

                self.audio_queue.get_nowait()

            except queue.Empty:
                pass

            try:

                self.audio_queue.put_nowait(bytes(indata))

            except queue.Full:
                pass

    def confirmed_speech(
        self,
    ) -> bool:
        """
        Prevent one random noise frame from
        immediately changing volume.
        """

        return sum(self.speech_history) >= self.config.required_speech_frames

    def duck_volume(
        self,
    ) -> None:
        """
        Lower the Windows master volume.
        """

        if self.ducked:
            return

        try:

            current_volume = self.volume.get_volume()

        except Exception as exc:

            print(f"\n❌ Unable to read volume: {exc}")

            return

        self.original_volume = current_volume

        target = self.config.duck_volume_percent

        print()
        print("🗣 Speech detected")

        if current_volume <= target:

            print(f"   Volume already " f"{current_volume:.0f}%")

            self.ducked = True

            return

        print(f"   {current_volume:.0f}%" f" -> " f"{target:.0f}%")

        try:

            self.volume.fade_to(target)

            self.ducked = True

        except Exception as exc:

            print(f"❌ Unable to lower volume: {exc}")

            self.original_volume = None

    def restore_volume(
        self,
    ) -> None:
        """
        Restore volume after conversation stops.
        """

        if not self.ducked:
            return

        if self.original_volume is None:
            return

        target = self.original_volume

        print()
        print("🔊 Conversation finished")

        print(f"   restoring -> " f"{target:.0f}%")

        try:

            self.volume.fade_to(target)

        except Exception as exc:

            print(f"❌ Unable to restore volume: {exc}")

        finally:

            self.original_volume = None
            self.ducked = False

            self.speech_history.clear()

    def process_audio(
        self,
    ) -> None:
        """
        Main detector loop.
        """

        while self.running.is_set():

            try:

                pcm_data = self.audio_queue.get(timeout=0.1)

            except queue.Empty:

                self.check_restore()

                continue

            speech, rms = self.is_speech(pcm_data)

            self.speech_history.append(speech)

            if speech:

                self.last_voice_time = time.monotonic()

            if self.confirmed_speech():

                self.last_voice_time = time.monotonic()

                if not self.ducked:

                    self.duck_volume()

            else:

                self.check_restore()

    def check_restore(
        self,
    ) -> None:
        """
        Restore volume after enough silence.
        """

        if not self.ducked:
            return

        silence_duration = time.monotonic() - self.last_voice_time

        if silence_duration >= self.config.silence_restore_seconds:

            self.restore_volume()

    def print_configuration(
        self,
    ) -> None:

        device_info = sd.query_devices(
            self.input_device,
            "input",
        )

        print()
        print("=" * 60)

        print("Automatic Conversation Volume Ducking")

        print("=" * 60)

        print(f"Input device  : " f"{device_info['name']}")

        print(f"Sample rate   : " f"{self.config.sample_rate} Hz")

        print(f"Frame         : " f"{self.config.frame_duration_ms} ms")

        print(f"VAD mode      : " f"{self.config.vad_mode}")

        print(f"RMS threshold : " f"{self.config.min_rms}")

        print(f"Duck volume   : " f"{self.config.duck_volume_percent:.0f}%")

        print(f"Restore delay : " f"{self.config.silence_restore_seconds}s")

        print("=" * 60)

        print()
        print("Listening...")

        print("Press Ctrl+C to exit.")

        print()

    def run(
        self,
    ) -> None:

        self.print_configuration()

        try:

            sd.check_input_settings(
                device=self.input_device,
                channels=self.config.channels,
                dtype="int16",
                samplerate=self.config.sample_rate,
            )

            with sd.RawInputStream(
                samplerate=self.config.sample_rate,
                blocksize=self.frames_per_buffer,
                device=self.input_device,
                channels=self.config.channels,
                dtype="int16",
                callback=self.audio_callback,
            ):

                self.process_audio()

        except KeyboardInterrupt:
            pass

        except sd.PortAudioError as exc:

            print()
            print("❌ Microphone / PortAudio error:")

            print(exc)

            print()

            print("Check devices using:")

            print("python auto_duck.py --list-devices")

        finally:

            self.stop()

    def stop(
        self,
    ) -> None:

        if not self.running.is_set():
            return

        self.running.clear()

        print()
        print("Stopping...")

        if self.ducked:

            self.restore_volume()

        print("Stopped.")


def list_input_devices() -> None:

    devices = sd.query_devices()

    try:
        default_input = sd.default.device[0]

    except Exception:
        default_input = None

    print()
    print("Available microphone/input devices")

    print("=" * 75)

    for index, device in enumerate(devices):

        if device["max_input_channels"] <= 0:
            continue

        marker = ""

        if index == default_input:

            marker = "  <-- DEFAULT"

        print(f"[{index:2}] " f"{device['name']}" f"{marker}")

        print(
            "     channels: "
            f"{device['max_input_channels']} "
            "| default sample rate: "
            f"{device['default_samplerate']:.0f}"
        )

    print()


def parse_arguments():

    parser = argparse.ArgumentParser(
        description=(
            "Automatically reduce Windows volume " "when nearby speech is detected."
        )
    )

    parser.add_argument(
        "--list-devices",
        action="store_true",
    )

    parser.add_argument(
        "--device-index",
        type=int,
    )

    parser.add_argument(
        "--sample-rate",
        type=int,
        choices=sorted(SpeechDuckController.SUPPORTED_VAD_SAMPLE_RATES),
        default=Config().sample_rate,
        metavar="HZ",
        help=(
            "Microphone sample rate in Hz. Must be one of 8000, 16000, "
            "32000, or 48000. Default: 16000."
        ),
    )

    parser.add_argument(
        "--duck-volume",
        type=float,
        default=15.0,
    )

    parser.add_argument(
        "--restore-delay",
        type=float,
        default=1.5,
    )

    parser.add_argument(
        "--rms",
        type=float,
        default=350.0,
    )

    parser.add_argument(
        "--vad-mode",
        type=int,
        choices=[
            0,
            1,
            2,
            3,
        ],
        default=2,
    )

    return parser.parse_args()


def main():

    args = parse_arguments()

    if args.list_devices:

        list_input_devices()

        return

    config = Config(
        sample_rate=args.sample_rate,
        duck_volume_percent=(args.duck_volume),
        silence_restore_seconds=(args.restore_delay),
        min_rms=args.rms,
        vad_mode=args.vad_mode,
    )

    controller = SpeechDuckController(
        config=config,
        input_device=args.device_index,
        requested_sample_rate=args.sample_rate,
    )

    def handle_exit(
        signum,
        frame,
    ):

        controller.stop()

    signal.signal(
        signal.SIGINT,
        handle_exit,
    )

    if hasattr(
        signal,
        "SIGTERM",
    ):

        signal.signal(
            signal.SIGTERM,
            handle_exit,
        )

    controller.run()


if __name__ == "__main__":
    main()
