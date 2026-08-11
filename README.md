# Music Duck

Music Duck automatically lowers the Windows master volume while it detects nearby speech through a microphone, then restores the previous volume after a short period of silence. It uses WebRTC VAD for speech detection and Pycaw for Windows volume control.

> This project controls the system-wide Windows output volume. Start with a modest ducking value and keep an easy way to adjust your volume available.

## Requirements

- Windows 10 or Windows 11
- Python 3.10 or newer
- A working microphone and output device

## Install

From PowerShell in the project folder:

```powershell
python -m venv env
.\env\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If PowerShell blocks activation, use `Set-ExecutionPolicy -Scope Process Bypass` for the current terminal, then run the activation command again.

## Run

```powershell
python auto_duck.py
```

Press `Ctrl+C` to stop the program. When it exits, it restores the volume that was active when speech was first detected.

### Choose a microphone

First show the available input devices:

```powershell
python auto_duck.py --list-devices
```

Then use the number shown beside the microphone:

```powershell
python auto_duck.py --device-index 2
```

### Set a custom sample rate

Music Duck uses `16000` Hz by default. To use a particular WebRTC-compatible rate, pass `--sample-rate`:

```powershell
python auto_duck.py --sample-rate 16000
```

Supported values are `8000`, `16000`, `32000`, and `48000` Hz. The selected device must support the requested rate; otherwise the program explains the error and stops before it starts listening.

## Command-line options

| Option | Default | Description |
| --- | --- | --- |
| `--list-devices` | off | List microphone/input devices and exit. |
| `--device-index INDEX` | system default | Select an input device by its listed index. |
| `--sample-rate HZ` | `16000` | Set a supported microphone rate: 8000, 16000, 32000, or 48000 Hz. |
| `--duck-volume PERCENT` | `15.0` | Volume to use while speech is confirmed. |
| `--restore-delay SECONDS` | `1.5` | Silence duration before restoring volume. |
| `--rms VALUE` | `350.0` | Minimum microphone loudness required to treat VAD output as speech. |
| `--vad-mode MODE` | `2` | WebRTC VAD aggressiveness from `0` (least) to `3` (most). |

Example for a more subtle duck with a longer recovery time:

```powershell
python auto_duck.py --device-index 2 --sample-rate 48000 --duck-volume 35 --restore-delay 2.5
```

## Tuning speech detection

The two controls most likely to need adjustment are `--rms` and `--vad-mode`.

- If background noise causes unwanted ducking, raise `--rms` (for example, `500`) or use a more aggressive `--vad-mode` (`3`).
- If normal speech is missed, lower `--rms` (for example, `250`) or use a less aggressive `--vad-mode` (`1`).
- If the volume returns too soon between sentences, increase `--restore-delay`.

Start with one setting at a time so it is clear which change improved detection.

## How it works

1. The app opens the selected microphone with 16-bit mono audio.
2. It captures at `16000` Hz by default, or at the supported rate supplied with `--sample-rate`.
3. WebRTC VAD and an RMS loudness threshold confirm speech over several short frames.
4. Confirmed speech smoothly lowers the Windows master volume.
5. After the configured silence delay, the app smoothly restores the saved volume.

## Troubleshooting

**No microphone appears or the wrong one is used**  
Run `python auto_duck.py --list-devices`, then pass its index with `--device-index`.

**A sample rate fails**  
Try another supported `--sample-rate` value. The microphone or its driver may not accept every WebRTC-compatible rate; `48000` is often a useful alternative to the default `16000`.

**Volume control is unavailable**  
Confirm that Windows has an active output device. If the issue persists, update the audio driver and run the terminal normally rather than through a remote audio session.

**It ducks too often or not often enough**  
Adjust `--rms` and `--vad-mode` as described above. Microphone gain and room noise strongly affect the best values.

## Project files

- `auto_duck.py` — application and command-line interface.
- `requirements.txt` — Python dependencies.
- `LICENSE` — project license.

## License

See [LICENSE](LICENSE).
