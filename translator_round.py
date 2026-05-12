#!/usr/bin/env python3
"""
Vietnamese Phrase Lookup + Pronunciation Practice
For Raspberry Pi 5 + Waveshare 4-inch round HDMI display (720×720).

This version: clean UI with rounded pill buttons, floating-dots listening
animation, breathing idle state. The on-screen "Hold to Speak" button is
gone — just use the physical GPIO button (or hold the spacebar for testing).

ENV
    GROQ_API_KEY=gsk_...          # required for fast cloud Whisper
    ANTHROPIC_API_KEY=sk-ant-...  # required for context-aware correction

To check that env is loaded BEFORE running:
    set -a; source ~/.translator_env; set +a
"""

import io
import math
import os
import re
import sys
import time
import socket
import threading

import numpy as np
import sounddevice as sd
import tkinter as tk
from tkinter import font as tkfont

from gpiozero import Button, LED
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator
import argostranslate.package
import argostranslate.translate

import pygame
from gtts import gTTS
import pyttsx3

try:
    import anthropic
    ANTHROPIC_AVAILABLE = bool(os.getenv("ANTHROPIC_API_KEY"))
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    from groq import Groq
    GROQ_AVAILABLE = bool(os.getenv("GROQ_API_KEY"))
except ImportError:
    GROQ_AVAILABLE = False


# ─── Configuration ────────────────────────────────────────────────────────────

BUTTON_PIN     = 17
LED_PIN        = 27
SAMPLE_RATE    = 16000
WHISPER_MODEL  = "small"
WHISPER_CT     = "int8"
MIC_DEVICE     = None
MIN_DURATION_S = 0.3
CLAUDE_MODEL   = "claude-haiku-4-5-20251001"

SCREEN_SIZE    = 720
FULLSCREEN     = True
CENTER         = SCREEN_SIZE // 2

# Modern Catppuccin-inspired palette
COLOR_BG       = "#11111b"
COLOR_PANEL    = "#1e1e2e"
COLOR_PANEL_HI = "#313244"
COLOR_RING     = "#1e1e2e"
COLOR_ACCENT   = "#89b4fa"
COLOR_ACCENT2  = "#cba6f7"
COLOR_TEXT     = "#cdd6f4"
COLOR_TEXT_DIM = "#bac2de"
COLOR_MUTED    = "#7f849c"
COLOR_GOOD     = "#a6e3a1"
COLOR_WARN     = "#f9e2af"
COLOR_BAD      = "#f38ba8"
COLOR_WORD     = "#fab387"
COLOR_WORD_HOV = "#f9e2af"


# ─── Network ──────────────────────────────────────────────────────────────────

def is_online():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(("8.8.8.8", 53))
        s.close()
        return True
    except OSError:
        return False


# ─── Audio recording ──────────────────────────────────────────────────────────

class Recorder:
    def __init__(self):
        self._stream = None
        self._chunks = []
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            self._chunks = []
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                device=MIC_DEVICE, callback=self._on_audio,
            )
            self._stream.start()

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        self._chunks.append(indata.copy())

    def stop(self):
        with self._lock:
            if self._stream is None:
                return np.zeros(0, dtype=np.float32)
            self._stream.stop()
            self._stream.close()
            self._stream = None
            if not self._chunks:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._chunks, axis=0).flatten()


# ─── Whisper / transcription ──────────────────────────────────────────────────

def make_whisper():
    print(f"Loading local Whisper '{WHISPER_MODEL}' ({WHISPER_CT}) for offline fallback...")
    return WhisperModel(WHISPER_MODEL, device="cpu", compute_type=WHISPER_CT)


def transcribe_groq(audio, language=None, sample_rate=16000):
    """Cloud Whisper via Groq — fast, accurate, free tier."""
    import wave
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes((audio * 32767).astype(np.int16).tobytes())
    buf.seek(0)
    client = Groq()
    kwargs = {"file": ("audio.wav", buf.read(), "audio/wav"),
              "model": "whisper-large-v3"}
    if language:
        kwargs["language"] = language
        if language == "vi":
            kwargs["prompt"] = "Đây là tiếng Việt."
    result = client.audio.transcriptions.create(**kwargs)
    return result.text.strip(), language or "vi"


def transcribe(model, audio, language=None):
    if GROQ_AVAILABLE and is_online():
        try:
            return transcribe_groq(audio, language)
        except Exception as e:
            print(f"Groq failed: {e}; falling back to local Whisper")

    segments, info = model.transcribe(
        audio, beam_size=5, language=language,
        vad_filter=True, vad_parameters=dict(min_silence_duration_ms=300),
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    return text, info.language


# ─── Translation ──────────────────────────────────────────────────────────────

def ensure_argos_packages():
    try:
        argostranslate.translate.translate("test", "en", "vi")
        argostranslate.translate.translate("xin chào", "vi", "en")
        return
    except Exception:
        pass
    print("Installing offline Argos packages (one-time)...")
    argostranslate.package.update_package_index()
    available = argostranslate.package.get_available_packages()
    for src, tgt in [("en", "vi"), ("vi", "en")]:
        pkg = next((p for p in available if p.from_code == src and p.to_code == tgt), None)
        if pkg:
            print(f"  installing {src} → {tgt}")
            argostranslate.package.install_from_path(pkg.download())


def translate_phrase(text, src, tgt):
    if is_online():
        try:
            out = GoogleTranslator(source=src, target=tgt).translate(text)
            if out:
                return out
        except Exception as e:
            print(f"online translation failed: {e}")
    try:
        return argostranslate.translate.translate(text, src, tgt)
    except Exception as e:
        return f"[translation failed: {e}]"


# ─── Conservative correction (Llama via Groq) ─────────────────────────────────

def correct_vietnamese_transcription(text):
    if not GROQ_AVAILABLE or not is_online() or not text:
        return text
    try:
        client = Groq()
        result = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{
                "role": "user",
                "content": (
                    f"Vietnamese speech transcription: \"{text}\"\n\n"
                    f"Rules:\n"
                    f"- Only fix obvious phonetic errors.\n"
                    f"- NEVER add new words or expand the phrase.\n"
                    f"- If short, fragmentary, or already plausible, return UNCHANGED.\n"
                    f"- Preserve the exact length and meaning.\n\n"
                    f"Return ONLY the Vietnamese phrase, no quotes, no explanation."
                ),
            }],
            max_tokens=200,
            temperature=0,
        )
        corrected = result.choices[0].message.content.strip().strip('"').strip()
        if corrected and len(corrected) < len(text) * 3:
            return corrected
        return text
    except Exception as e:
        print(f"Correction failed: {e}")
        return text


# ─── Claude: combined correction + translation ────────────────────────────────

_anthropic_client = None
def get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None and ANTHROPIC_AVAILABLE:
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def correct_and_translate_with_claude(text):
    if not ANTHROPIC_AVAILABLE or not is_online() or not text:
        return None
    try:
        msg = get_anthropic_client().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": (
                    f"A Vietnamese phrase from speech recognition: \"{text}\"\n\n"
                    f"Rules:\n"
                    f"- Only fix obvious phonetic errors (clearly wrong similar-sounding words).\n"
                    f"- NEVER add new words, expand the phrase, or guess missing context.\n"
                    f"- If the phrase is short, fragmentary, or already plausible, leave it UNCHANGED.\n"
                    f"- Preserve the exact length and meaning of what was said.\n\n"
                    f"Then translate the (possibly unchanged) Vietnamese to natural English.\n\n"
                    f"Return EXACTLY two lines, no quotes, no explanation:\n"
                    f"Line 1: Vietnamese (corrected only if needed)\n"
                    f"Line 2: English translation"
                ),
            }],
        )
        lines = [l.strip() for l in msg.content[0].text.strip().split("\n") if l.strip()]
        if len(lines) >= 2:
            return lines[0], lines[1]
    except Exception as e:
        print(f"Claude correction+translation failed: {e}")
    return None


def define_word_in_context(word, sentence):
    word_clean = word.strip().strip(".,!?;:\"'").lower()
    if not word_clean:
        return ("", "")
    if ANTHROPIC_AVAILABLE and is_online():
        try:
            msg = get_anthropic_client().messages.create(
                model=CLAUDE_MODEL,
                max_tokens=120,
                messages=[{
                    "role": "user",
                    "content": (
                        f"In the English sentence: \"{sentence}\"\n\n"
                        f"What does the word \"{word_clean}\" mean here?\n\n"
                        f"Reply with EXACTLY two lines, no extra text:\n"
                        f"Line 1: the Vietnamese translation of the word as used "
                        f"in this sentence (just the word/phrase, no explanation)\n"
                        f"Line 2: a very short usage note in Vietnamese (under 12 words)"
                    ),
                }],
            )
            text = msg.content[0].text.strip()
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            return (lines[0] if lines else "",
                    lines[1] if len(lines) > 1 else "")
        except Exception as e:
            print(f"Claude lookup failed: {e}")
    try:
        return (translate_phrase(word_clean, "en", "vi"), "")
    except Exception:
        return ("(không tìm thấy)", "")


# ─── TTS ──────────────────────────────────────────────────────────────────────

class TTS:
    def __init__(self):
        pygame.mixer.init()
        self._offline = pyttsx3.init()
        self._lock = threading.Lock()

    def speak(self, text, lang="en"):
        with self._lock:
            if is_online():
                try:
                    fp = io.BytesIO()
                    gTTS(text=text, lang=lang).write_to_fp(fp)
                    fp.seek(0)
                    pygame.mixer.music.load(fp, "mp3")
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy():
                        time.sleep(0.05)
                    return
                except Exception as e:
                    print(f"online TTS failed: {e}")
            voices = self._offline.getProperty("voices")
            for v in voices:
                if lang in (v.id or "").lower() or lang in (v.name or "").lower():
                    self._offline.setProperty("voice", v.id)
                    break
            self._offline.setProperty("rate", 140)
            self._offline.say(text)
            self._offline.runAndWait()


# ─── Pronunciation scoring ────────────────────────────────────────────────────

def normalize(text):
    return re.sub(r"[^\w\s]", "", text.lower()).split()


def word_match_score(target, attempt):
    tgt = normalize(target)
    att = set(normalize(attempt))
    if not tgt:
        return 0
    return round(100 * sum(1 for w in tgt if w in att) / len(tgt))


# ─── UI: Pill-shaped button ───────────────────────────────────────────────────

class PillButton:
    """A rounded pill-shaped button using a Canvas. No boxy edges."""

    def __init__(self, parent, text, command, *,
                 bg=COLOR_PANEL_HI, fg=COLOR_TEXT,
                 hover_bg=COLOR_ACCENT, hover_fg=COLOR_BG,
                 width=170, height=44, font=None):
        parent_bg = parent.cget("bg")
        self.canvas = tk.Canvas(
            parent, width=width, height=height,
            bg=parent_bg, highlightthickness=0, bd=0,
        )
        self.command = command
        self.bg = bg
        self.fg = fg
        self.hover_bg = hover_bg
        self.hover_fg = hover_fg
        self.font = font
        self.text = text
        self.width = width
        self.height = height
        self._draw(self.bg, self.fg)
        self.canvas.bind("<Enter>", lambda e: self._draw(self.hover_bg, self.hover_fg))
        self.canvas.bind("<Leave>", lambda e: self._draw(self.bg, self.fg))
        self.canvas.bind("<Button-1>", lambda e: self._on_click())

    def _draw(self, bg, fg):
        self.canvas.delete("all")
        h = self.height
        w = self.width
        # Two end circles + center rectangle = perfect pill shape
        self.canvas.create_oval(0, 0, h, h, fill=bg, outline="")
        self.canvas.create_oval(w - h, 0, w, h, fill=bg, outline="")
        self.canvas.create_rectangle(h / 2, 0, w - h / 2, h, fill=bg, outline="")
        self.canvas.create_text(w / 2, h / 2, text=self.text, fill=fg,
                                font=self.font)

    def _on_click(self):
        if self.command:
            self.command()

    def place(self, **kwargs):
        self.canvas.place(**kwargs)
        return self

    def configure_text(self, text):
        self.text = text
        self._draw(self.bg, self.fg)


# ─── UI: Floating dots animation ──────────────────────────────────────────────

class FloatingDots:
    def __init__(self, parent, x, y, color=COLOR_ACCENT, size=14, spacing=34):
        self.size = size
        self.spacing = spacing
        canvas_w = spacing * 3
        canvas_h = size * 4
        self.canvas = tk.Canvas(
            parent, width=canvas_w, height=canvas_h,
            bg=COLOR_BG, highlightthickness=0, bd=0,
        )
        self.canvas.place(x=x, y=y, anchor="center")
        self.dots = []
        for i in range(3):
            cx = i * spacing + spacing / 2
            cy = canvas_h / 2
            dot = self.canvas.create_oval(
                cx - size / 2, cy - size / 2,
                cx + size / 2, cy + size / 2,
                fill=color, outline="",
            )
            self.dots.append((dot, cx, cy))
        self.tick = 0
        self.running = False
        tk.Misc.lower(self.canvas)
        self._after_id = None

    def show(self):
        if self.running:
            return
        self.running = True
        tk.Misc.lift(self.canvas)
        self._animate()

    def hide(self):
        self.running = False
        if self._after_id:
            try:
                self.canvas.after_cancel(self._after_id)
            except Exception:
                pass
        tk.Misc.lower(self.canvas)

    def _animate(self):
        if not self.running:
            return
        for i, (dot, cx, cy) in enumerate(self.dots):
            phase = (self.tick + i * 6) * 0.25
            offset = math.sin(phase) * 10
            self.canvas.coords(
                dot,
                cx - self.size / 2, cy - self.size / 2 + offset,
                cx + self.size / 2, cy + self.size / 2 + offset,
            )
        self.tick += 1
        self._after_id = self.canvas.after(50, self._animate)


# ─── UI: Pulsing label ────────────────────────────────────────────────────────

class PulseLabel:
    def __init__(self, label, low_color, high_color, period_ms=2400):
        self.label = label
        self.low = self._hex_to_rgb(low_color)
        self.high = self._hex_to_rgb(high_color)
        self.period = period_ms
        self.running = False
        self.start_time = None
        self._after_id = None

    @staticmethod
    def _hex_to_rgb(c):
        return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)

    @staticmethod
    def _rgb_to_hex(rgb):
        return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

    def start(self):
        if self.running:
            return
        self.running = True
        self.start_time = time.time()
        self._animate()

    def stop(self):
        self.running = False
        if self._after_id:
            try:
                self.label.after_cancel(self._after_id)
            except Exception:
                pass

    def _animate(self):
        if not self.running:
            return
        elapsed_ms = (time.time() - self.start_time) * 1000
        t = (math.sin(2 * math.pi * elapsed_ms / self.period) + 1) / 2
        rgb = tuple(int(self.low[i] + (self.high[i] - self.low[i]) * t)
                    for i in range(3))
        try:
            self.label.config(fg=self._rgb_to_hex(rgb))
        except tk.TclError:
            self.running = False
            return
        self._after_id = self.label.after(50, self._animate)


# ─── Main App ─────────────────────────────────────────────────────────────────

class PhraseTranslatorApp:
    IDLE        = "idle"
    LISTENING   = "listening"
    TRANSLATION = "translation"
    WORD        = "word"
    PRACTICE    = "practice"

    def __init__(self, root, whisper, tts, recorder):
        self.root = root
        self.whisper = whisper
        self.tts = tts
        self.recorder = recorder

        self.state = self.IDLE
        self.original_vi = ""
        self.translated_en = ""
        self.current_word = ""
        self.practice_target = ""
        self.practice_return_state = self.TRANSLATION
        self._space_held = False  # for spacebar testing

        self._build_ui()
        self.show(self.IDLE)

    def _build_ui(self):
        self.root.title("Vietnamese Phrase Lookup")
        self.root.geometry(f"{SCREEN_SIZE}x{SCREEN_SIZE}")
        self.root.configure(bg=COLOR_BG)
        if FULLSCREEN:
            self.root.attributes("-fullscreen", True)
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        # Spacebar = press-and-hold-to-record (for testing without GPIO)
        self.root.bind("<KeyPress-space>", self._on_space_press)
        self.root.bind("<KeyRelease-space>", self._on_space_release)

        # Background canvas with decorative ring
        self.bg_canvas = tk.Canvas(
            self.root, width=SCREEN_SIZE, height=SCREEN_SIZE,
            bg=COLOR_BG, highlightthickness=0, bd=0,
        )
        self.bg_canvas.place(x=0, y=0)
        self.bg_canvas.create_oval(
            2, 2, SCREEN_SIZE - 2, SCREEN_SIZE - 2,
            outline=COLOR_RING, width=2,
        )

        # Fonts
        self.f_status = tkfont.Font(family="DejaVu Sans", size=11)
        self.f_small  = tkfont.Font(family="DejaVu Sans", size=12)
        self.f_med    = tkfont.Font(family="DejaVu Sans", size=15)
        self.f_big    = tkfont.Font(family="DejaVu Sans", size=20, weight="bold")
        self.f_huge   = tkfont.Font(family="DejaVu Sans", size=28, weight="bold")
        self.f_title  = tkfont.Font(family="DejaVu Sans", size=10, weight="bold")
        self.f_btn    = tkfont.Font(family="DejaVu Sans", size=12, weight="bold")

        # Status bar at top
        self.status_label = tk.Label(
            self.root, text="●  Ready", fg=COLOR_GOOD, bg=COLOR_BG,
            font=self.f_status,
        )
        self.status_label.place(x=CENTER, y=78, anchor="center")

        # Container frames for each state
        self.frame_idle        = tk.Frame(self.root, bg=COLOR_BG)
        self.frame_listening   = tk.Frame(self.root, bg=COLOR_BG)
        self.frame_translation = tk.Frame(self.root, bg=COLOR_BG)
        self.frame_word        = tk.Frame(self.root, bg=COLOR_BG)
        self.frame_practice    = tk.Frame(self.root, bg=COLOR_BG)

        for f in (self.frame_idle, self.frame_listening,
                  self.frame_translation, self.frame_word, self.frame_practice):
            f.place(x=0, y=0, width=SCREEN_SIZE, height=SCREEN_SIZE)

        self._build_idle()
        self._build_listening()
        self._build_translation()
        self._build_word()
        self._build_practice()

        # Floating dots overlay (visible during listening/processing)
        self.dots = FloatingDots(self.root, x=CENTER, y=370, color=COLOR_ACCENT)
        self.dots.hide()

        # Subtle hint at the bottom — replaces the old "Hold to Speak" button
        self.bottom_hint = tk.Label(
            self.root, text="🎙   Hold the button to speak",
            bg=COLOR_BG, fg=COLOR_MUTED, font=self.f_small,
        )
        self.bottom_hint.place(x=CENTER, y=635, anchor="center")

    # ─── Idle screen ─────────────────────────────────────────────────────────

    def _build_idle(self):
        f = self.frame_idle
        self.idle_icon = tk.Label(
            f, text="🎙", bg=COLOR_BG, fg=COLOR_ACCENT,
            font=tkfont.Font(family="DejaVu Sans", size=92),
        )
        self.idle_icon.place(x=CENTER, y=300, anchor="center")
        self.idle_pulse = PulseLabel(self.idle_icon, COLOR_ACCENT, COLOR_ACCENT2)

        tk.Label(
            f, text="VIETNAMESE  →  ENGLISH",
            bg=COLOR_BG, fg=COLOR_MUTED, font=self.f_title,
        ).place(x=CENTER, y=435, anchor="center")

        tk.Label(
            f, text="Speak a phrase in Vietnamese\nto see its English meaning",
            bg=COLOR_BG, fg=COLOR_TEXT_DIM, font=self.f_med, justify="center",
        ).place(x=CENTER, y=485, anchor="center")

    # ─── Listening screen ────────────────────────────────────────────────────

    def _build_listening(self):
        f = self.frame_listening
        tk.Label(
            f, text="LISTENING", bg=COLOR_BG, fg=COLOR_ACCENT,
            font=self.f_title,
        ).place(x=CENTER, y=300, anchor="center")
        # Dots are drawn on root by FloatingDots at y=370
        tk.Label(
            f, text="Speak now — release the button when done",
            bg=COLOR_BG, fg=COLOR_MUTED, font=self.f_small,
        ).place(x=CENTER, y=470, anchor="center")

    # ─── Translation screen ──────────────────────────────────────────────────

    def _build_translation(self):
        f = self.frame_translation

        tk.Label(f, text="🇻🇳", bg=COLOR_BG, fg=COLOR_MUTED,
                 font=self.f_small).place(x=CENTER, y=125, anchor="center")
        self.vi_label = tk.Label(
            f, text="—", fg=COLOR_TEXT_DIM, bg=COLOR_BG, font=self.f_small,
            wraplength=540, justify="center",
        )
        self.vi_label.place(x=CENTER, y=158, anchor="center")

        # English translation card
        self.en_card = tk.Frame(f, bg=COLOR_PANEL)
        self.en_card.place(x=CENTER, y=320, anchor="center", width=580, height=220)

        tk.Label(self.en_card, text="🇬🇧  ENGLISH",
                 bg=COLOR_PANEL, fg=COLOR_MUTED, font=self.f_title).place(
            x=290, y=18, anchor="center")

        self.en_text = tk.Text(
            self.en_card, bg=COLOR_PANEL, fg=COLOR_TEXT, font=self.f_big,
            wrap="word", relief="flat", bd=0, highlightthickness=0,
            cursor="hand2", padx=20, pady=10,
        )
        self.en_text.place(x=290, y=120, anchor="center", width=560, height=170)
        self.en_text.tag_configure("center", justify="center")
        self.en_text.config(state="disabled")

        # Pill buttons for Listen + Practice
        PillButton(f, "🔊  Listen",
                   command=lambda: self._speak(self.translated_en, "en"),
                   bg=COLOR_PANEL_HI, fg=COLOR_TEXT,
                   hover_bg=COLOR_ACCENT, hover_fg=COLOR_BG,
                   width=170, height=44, font=self.f_btn,
                   ).place(x=CENTER - 95, y=560, anchor="center")

        PillButton(f, "🎯  Practice",
                   command=lambda: self.start_practice(self.translated_en, self.TRANSLATION),
                   bg=COLOR_PANEL_HI, fg=COLOR_TEXT,
                   hover_bg=COLOR_ACCENT2, hover_fg=COLOR_BG,
                   width=170, height=44, font=self.f_btn,
                   ).place(x=CENTER + 95, y=560, anchor="center")

    def _render_translation_words(self):
        self.en_text.config(state="normal")
        self.en_text.delete("1.0", "end")
        for tag in self.en_text.tag_names():
            if tag.startswith("word_"):
                self.en_text.tag_delete(tag)

        tokens = re.findall(r"\w+|[^\w\s]+|\s+", self.translated_en)
        word_idx = 0
        for tok in tokens:
            if re.match(r"^\w+$", tok):
                tag = f"word_{word_idx}"
                self.en_text.insert("end", tok, ("center", tag))
                self.en_text.tag_configure(tag, foreground=COLOR_WORD)
                self.en_text.tag_bind(tag, "<Enter>",
                    lambda e, t=tag: self.en_text.tag_configure(
                        t, foreground=COLOR_WORD_HOV, underline=True))
                self.en_text.tag_bind(tag, "<Leave>",
                    lambda e, t=tag: self.en_text.tag_configure(
                        t, foreground=COLOR_WORD, underline=False))
                self.en_text.tag_bind(tag, "<Button-1>",
                    lambda e, w=tok: self.on_word_tap(w))
                word_idx += 1
            else:
                self.en_text.insert("end", tok, ("center",))
        self.en_text.config(state="disabled")

    # ─── Word detail screen ──────────────────────────────────────────────────

    def _build_word(self):
        f = self.frame_word

        # Back as a small pill button
        PillButton(f, "←  Back",
                   command=lambda: self.show(self.TRANSLATION),
                   bg=COLOR_BG, fg=COLOR_MUTED,
                   hover_bg=COLOR_PANEL_HI, hover_fg=COLOR_TEXT,
                   width=110, height=36, font=self.f_btn,
                   ).place(x=120, y=120, anchor="center")

        # English word card
        self.word_card = tk.Frame(f, bg=COLOR_PANEL)
        self.word_card.place(x=CENTER, y=240, anchor="center", width=540, height=130)

        tk.Label(self.word_card, text="🇬🇧  ENGLISH",
                 bg=COLOR_PANEL, fg=COLOR_MUTED, font=self.f_title).place(
            x=270, y=18, anchor="center")
        self.word_label = tk.Label(
            self.word_card, text="—", fg=COLOR_TEXT, bg=COLOR_PANEL,
            font=self.f_huge, wraplength=510, justify="center",
        )
        self.word_label.place(x=270, y=80, anchor="center")

        # Vietnamese card
        self.word_vi_card = tk.Frame(f, bg=COLOR_PANEL)
        self.word_vi_card.place(x=CENTER, y=380, anchor="center", width=540, height=140)

        tk.Label(self.word_vi_card, text="🇻🇳  TIẾNG VIỆT",
                 bg=COLOR_PANEL, fg=COLOR_MUTED, font=self.f_title).place(
            x=270, y=18, anchor="center")
        self.word_vi_label = tk.Label(
            self.word_vi_card, text="—", fg=COLOR_ACCENT, bg=COLOR_PANEL,
            font=self.f_big, wraplength=510, justify="center",
        )
        self.word_vi_label.place(x=270, y=70, anchor="center")
        self.word_note_label = tk.Label(
            self.word_vi_card, text="", fg=COLOR_MUTED, bg=COLOR_PANEL,
            font=self.f_small, wraplength=510, justify="center",
        )
        self.word_note_label.place(x=270, y=115, anchor="center")

        PillButton(f, "🔊  Listen",
                   command=lambda: self._speak(self.current_word, "en"),
                   bg=COLOR_PANEL_HI, fg=COLOR_TEXT,
                   hover_bg=COLOR_ACCENT, hover_fg=COLOR_BG,
                   width=170, height=44, font=self.f_btn,
                   ).place(x=CENTER - 95, y=560, anchor="center")

        PillButton(f, "🎯  Practice",
                   command=lambda: self.start_practice(self.current_word, self.WORD),
                   bg=COLOR_PANEL_HI, fg=COLOR_TEXT,
                   hover_bg=COLOR_ACCENT2, hover_fg=COLOR_BG,
                   width=170, height=44, font=self.f_btn,
                   ).place(x=CENTER + 95, y=560, anchor="center")

    # ─── Practice screen ─────────────────────────────────────────────────────

    def _build_practice(self):
        f = self.frame_practice

        tk.Label(f, text="🎯  PRACTICE", bg=COLOR_BG, fg=COLOR_ACCENT2,
                 font=self.f_title).place(x=CENTER, y=125, anchor="center")

        self.practice_target_card = tk.Frame(f, bg=COLOR_PANEL)
        self.practice_target_card.place(x=CENTER, y=210, anchor="center",
                                        width=540, height=110)
        tk.Label(self.practice_target_card, text="REPEAT",
                 bg=COLOR_PANEL, fg=COLOR_MUTED, font=self.f_title).place(
            x=270, y=18, anchor="center")
        self.practice_target_label = tk.Label(
            self.practice_target_card, text="—", fg=COLOR_TEXT, bg=COLOR_PANEL,
            font=self.f_big, wraplength=510, justify="center",
        )
        self.practice_target_label.place(x=270, y=68, anchor="center")

        self.practice_score_label = tk.Label(
            f, text="", fg=COLOR_GOOD, bg=COLOR_BG, font=self.f_huge,
        )
        self.practice_score_label.place(x=CENTER, y=370, anchor="center")

        self.practice_result_label = tk.Label(
            f, text="Listen, then hold the button and repeat",
            fg=COLOR_MUTED, bg=COLOR_BG, font=self.f_small,
            wraplength=540, justify="center",
        )
        self.practice_result_label.place(x=CENTER, y=445, anchor="center")

        PillButton(f, "✓  Done",
                   command=self.exit_practice,
                   bg=COLOR_PANEL_HI, fg=COLOR_TEXT,
                   hover_bg=COLOR_ACCENT, hover_fg=COLOR_BG,
                   width=170, height=44, font=self.f_btn,
                   ).place(x=CENTER, y=560, anchor="center")

    # ─── State management ────────────────────────────────────────────────────

    def show(self, state):
        self.state = state
        for s, frame in [
            (self.IDLE,        self.frame_idle),
            (self.LISTENING,   self.frame_listening),
            (self.TRANSLATION, self.frame_translation),
            (self.WORD,        self.frame_word),
            (self.PRACTICE,    self.frame_practice),
        ]:
            if s == state:
                frame.lift()
            else:
                frame.lower()

        self.status_label.lift()
        self.bottom_hint.lift()

        if state == self.IDLE:
            self.idle_pulse.start()
        else:
            self.idle_pulse.stop()
            self.idle_icon.config(fg=COLOR_ACCENT)

        if state == self.LISTENING:
            self.dots.show()
        else:
            self.dots.hide()

    def set_status(self, text, color=COLOR_GOOD):
        self.root.after(0, lambda: self.status_label.config(text=text, fg=color))

    def _speak(self, text, lang):
        if not text:
            return
        threading.Thread(target=self.tts.speak, args=(text, lang), daemon=True).start()

    # ─── Recording flow ──────────────────────────────────────────────────────

    def start_record(self):
        # CRITICAL: start recording first, defer all UI updates to main thread
        self.recorder.start()
        if self.state == self.PRACTICE:
            self.set_status("●  Recording your repeat", COLOR_BAD)
            self.root.after(0, self.dots.show)
        else:
            self.set_status("●  Recording", COLOR_BAD)
            self.root.after(0, lambda: self.show(self.LISTENING))

    def stop_record(self):
        audio = self.recorder.stop()
        if len(audio) / SAMPLE_RATE < MIN_DURATION_S:
            if self.state == self.LISTENING:
                self.root.after(0, lambda: self.show(self.IDLE))
            self.set_status("●  Ready", COLOR_GOOD)
            return
        threading.Thread(target=self._process_audio, args=(audio,),
                         daemon=True).start()

    def _process_audio(self, audio):
        if self.state == self.PRACTICE:
            self._handle_practice_attempt(audio)
        else:
            self._handle_translation(audio)

    def _handle_translation(self, audio):
        self.set_status("⌛  Hearing what you said...", COLOR_ACCENT)
        text, lang = transcribe(self.whisper, audio, language="vi")
        if not text:
            self.root.after(0, lambda: self.show(self.IDLE))
            self.set_status("●  Nothing heard — try again", COLOR_MUTED)
            return

        self.set_status("⌛  Processing...", COLOR_ACCENT)

        claude_result = correct_and_translate_with_claude(text)
        if claude_result:
            self.original_vi, self.translated_en = claude_result
        else:
            corrected = correct_vietnamese_transcription(text)
            self.original_vi = corrected
            self.translated_en = translate_phrase(corrected, "vi", "en")

        self.root.after(0, self._show_translation_result)

    def _show_translation_result(self):
        self.vi_label.config(text=self.original_vi)
        self._render_translation_words()
        self.show(self.TRANSLATION)
        self.set_status("●  Tap any word for its meaning", COLOR_GOOD)
        self._speak(self.translated_en, "en")

    # ─── Word tap ────────────────────────────────────────────────────────────

    def on_word_tap(self, word):
        self.current_word = word
        self.word_label.config(text=word)
        self.word_vi_label.config(text="...")
        self.word_note_label.config(text="")
        self.show(self.WORD)
        self.set_status("⌛  Looking up...", COLOR_ACCENT)
        threading.Thread(target=self._fetch_word_definition,
                         args=(word, self.translated_en), daemon=True).start()

    def _fetch_word_definition(self, word, sentence):
        vi, note = define_word_in_context(word, sentence)
        self.root.after(0, lambda: self.word_vi_label.config(text=vi or "—"))
        self.root.after(0, lambda: self.word_note_label.config(text=note))
        self.set_status("●  Ready", COLOR_GOOD)
        self._speak(word, "en")

    # ─── Practice ────────────────────────────────────────────────────────────

    def start_practice(self, target, return_to):
        if not target:
            return
        self.practice_target = target
        self.practice_return_state = return_to
        self.practice_target_label.config(text=target)
        self.practice_result_label.config(
            text="Listen, then hold the button and repeat", fg=COLOR_MUTED)
        self.practice_score_label.config(text="")
        self.show(self.PRACTICE)
        self.set_status("🎯  Practice mode", COLOR_ACCENT2)
        threading.Thread(target=self.tts.speak, args=(target, "en"),
                         daemon=True).start()

    def exit_practice(self):
        self.show(self.practice_return_state)
        self.set_status("●  Ready", COLOR_GOOD)

    def _handle_practice_attempt(self, audio):
        self.set_status("⌛  Checking...", COLOR_ACCENT)
        self.dots.hide()
        attempt, _ = transcribe(self.whisper, audio, language="en")
        target = self.practice_target
        pct = word_match_score(target, attempt)

        if pct >= 80:
            score_text, score_color = f"{pct}%", COLOR_GOOD
            result = f"Great! You said: \"{attempt}\""
        elif pct >= 50:
            score_text, score_color = f"{pct}%", COLOR_WARN
            result = f"Close. You said: \"{attempt}\""
        else:
            score_text, score_color = f"{pct}%", COLOR_BAD
            result = f"Try again. You said: \"{attempt}\""

        self.root.after(0, lambda: self.practice_score_label.config(
            text=score_text, fg=score_color))
        self.root.after(0, lambda: self.practice_result_label.config(
            text=result, fg=COLOR_TEXT))
        self.set_status("●  Ready", COLOR_GOOD)

    # ─── Spacebar testing ────────────────────────────────────────────────────

    def _on_space_press(self, event):
        if not self._space_held:
            self._space_held = True
            self.start_record()

    def _on_space_release(self, event):
        if self._space_held:
            self._space_held = False
            self.stop_record()


# ─── GPIO ─────────────────────────────────────────────────────────────────────

def wire_gpio(app, status_led):
    try:
        button = Button(BUTTON_PIN, bounce_time=0.05)
    except Exception as e:
        print(f"GPIO unavailable ({e}); spacebar still works.")
        return None

    def on_press():
        if status_led: status_led.on()
        app.start_record()

    def on_release():
        if status_led: status_led.off()
        app.stop_record()

    button.when_pressed = on_press
    button.when_released = on_release
    return button


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=== Vietnamese Phrase Lookup ===\n")
    if not GROQ_AVAILABLE:
        print("⚠  GROQ_API_KEY not set — using slower local Whisper.")
        print("   Run:  set -a; source ~/.translator_env; set +a")
    if not ANTHROPIC_AVAILABLE:
        print("⚠  ANTHROPIC_API_KEY not set — using Llama + Google Translate fallback.")
    print()

    ensure_argos_packages()
    whisper = make_whisper()
    tts = TTS()
    recorder = Recorder()

    led = None
    if LED_PIN is not None:
        try:
            led = LED(LED_PIN)
        except Exception as e:
            print(f"LED unavailable: {e}")

    root = tk.Tk()
    app = PhraseTranslatorApp(root, whisper, tts, recorder)
    gpio_button = wire_gpio(app, led)  # keep reference alive!

    print("Ready. Press Esc to exit.\n")
    try:
        root.mainloop()
    finally:
        if led: led.off()


if __name__ == "__main__":
    main()
