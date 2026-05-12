# 🇻🇳 Vietnamese Phrase Translator

A voice-powered Vietnamese → English translator and pronunciation learning device built on a Raspberry Pi 5 with a 4-inch round touchscreen display.

Designed for Vietnamese-speaking parents who want to understand English — speak a Vietnamese phrase, hear the English translation instantly, tap any word to see its meaning, and practice pronunciation.

---

## Demo

**Speak** a Vietnamese phrase → **see** the English translation with tappable words → **tap** any word for its Vietnamese meaning → **practice** saying it in English.

---

## Features

- 🎙 **Voice input** — hold the button, speak Vietnamese, release
- 🔊 **Auto-speaks** the English translation immediately
- 👆 **Tap any English word** for its Vietnamese meaning in context
- 🎯 **Pronunciation practice** — repeat the phrase and get a match score
- 🌐 **Hybrid online/offline** — fast cloud APIs when connected, local fallback when not
- 💡 **Animated UI** — breathing idle state, floating dots while listening
- 🔁 **Auto-restarts** on crash, launches on boot

---

## Hardware

| Part | Details |
|---|---|
| Raspberry Pi 5 | 8GB recommended |
| Display | Waveshare 4-inch Round HDMI, 720×720 |
| Microphone | USB microphone |
| Speaker | USB sound card + speaker |
| Button | Momentary push button → GPIO 17 + GND |
| Optional | LED → GPIO 27 + 220Ω resistor + GND |

---

## How It Works

```
Hold button → speak Vietnamese
     ↓
Groq Whisper (cloud) or local Whisper (offline)
     ↓
Claude AI — corrects phonetic errors + translates to English
     ↓
Display English translation + auto-speak via gTTS
     ↓
Tap any word → Claude gives Vietnamese meaning in context
```

---

## Software Stack

| Component | Technology |
|---|---|
| Speech-to-text | Groq Whisper large-v3 (online) / faster-whisper small (offline) |
| Correction + Translation | Claude Haiku 4.5 (online) / Llama 3.3 70B + Google Translate (fallback) |
| Text-to-speech | gTTS (online) / pyttsx3 espeak (offline) |
| GUI | Tkinter on Raspberry Pi OS Bookworm |
| Offline translation | Argos Translate |

---

## Installation

### 1. Hardware setup

Wire the push button between **GPIO 17 (pin 11)** and **Ground (pin 9)**.

Configure the round display in `/boot/firmware/config.txt`:
```
hdmi_group=2
hdmi_mode=87
hdmi_cvt 720 720 60 6 0 0 0
hdmi_drive=1
hdmi_force_hotplug=1
```

### 2. Install dependencies

```bash
sudo apt update
sudo apt install -y python3-pip python3-tk libportaudio2 ffmpeg mpg123 espeak-ng

pip install --break-system-packages \
    gpiozero sounddevice numpy pygame \
    faster-whisper deep-translator argostranslate \
    gTTS pyttsx3 anthropic groq
```

### 3. Set up API keys

Create `~/.translator_env`:
```
GROQ_API_KEY=gsk_...
ANTHROPIC_API_KEY=sk-ant-...
```

- **Groq** (free) — sign up at [console.groq.com](https://console.groq.com)
- **Anthropic** — sign up at [console.anthropic.com](https://console.anthropic.com). You need your own API key — cost is approximately $0.001 per translation (fractions of a cent). New accounts receive free credits that last months for typical family use.

### 4. Copy the files

```bash
mkdir -p ~/translator
cp translator_round.py setup_autostart.sh ~/translator/
```

### 5. Run manually to test

```bash
cd ~/translator
set -a; source ~/.translator_env; set +a
python3 translator_round.py
```

### 6. Set up autostart

```bash
cd ~/translator
chmod +x setup_autostart.sh
./setup_autostart.sh

sudo raspi-config
# System Options → Boot/Auto Login → Desktop Autologin
sudo reboot
```

---

## Usage

| Action | Result |
|---|---|
| Hold the button + speak Vietnamese | Transcribes and translates |
| Tap any English word | Shows Vietnamese meaning |
| Tap 🔊 Listen | Plays English pronunciation |
| Tap 🎯 Practice | Records and scores your pronunciation |
| Hold button during practice | Records your repeat attempt |
| Hold spacebar | Same as button (for testing) |

---

## Configuration

Edit the constants at the top of `translator_round.py`:

```python
BUTTON_PIN    = 17       # GPIO pin for push button
LED_PIN       = 27       # GPIO pin for status LED (or None)
WHISPER_MODEL = "small"  # tiny / base / small / medium
FULLSCREEN    = True     # False for windowed development mode
MIC_DEVICE    = None     # None = system default
```

---

## Logs

Autostart logs are saved to `~/translator/logs/`. Check them if something isn't working:

```bash
tail -f ~/translator/logs/translator-*.log
```

---

## Project Structure

```
translator/
├── translator_round.py   # Main application
├── setup_autostart.sh    # One-time autostart setup script
└── logs/                 # Runtime logs (git-ignored)
```

---

## Built With

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [Groq API](https://console.groq.com)
- [Anthropic Claude](https://console.anthropic.com)
- [Argos Translate](https://github.com/argosopentech/argos-translate)
- [gTTS](https://github.com/pndurette/gTTS)
- [gpiozero](https://gpiozero.readthedocs.io)
