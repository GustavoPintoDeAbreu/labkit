"""Speech to text on the CPU (faster-whisper), shared by the lab's projects (``labkit[stt]``).

Lifted from lab-assistant's ``assistant/stt.py`` (2026-11-24), which ran it in production for his
WhatsApp voice notes and saved reels, so every project that needs speech as text uses one copy:
lab-assistant, the idea pipeline (reels and videos sent with an idea), and any next one.

On the CPU on purpose: the GPUs belong to the broker. The model loads lazily, once per process.

    t = Transcriber()                                   # large-v3-turbo, int8, 8 threads
    t.transcribe(ogg_bytes, "audio/ogg", language="en", prompt="Kaya, labwatch")   # a voice note
    t.transcribe_clip(Path("reel.m4a"))                 # any language, detected, no prompt

``decode_audio`` decodes with PyAV itself: faster-whisper 1.2's own decoder passes
``av.open(metadata_errors=...)``, which PyAV 19 removed (2026-11-23).
"""
from __future__ import annotations

import gc
import logging
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

DEFAULT_MODEL = "large-v3-turbo"
EXT = {"audio/ogg": ".ogg", "audio/opus": ".opus", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
       "audio/aac": ".aac", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/webm": ".webm"}


def decode_audio(path: str, rate: int = 16000):
    """The file as 16 kHz mono float32, what Whisper takes (faster-whisper's own decode, without the
    ``metadata_errors`` argument PyAV 19 removed)."""
    import av
    import numpy as np

    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=rate)
    chunks = []
    with av.open(path, mode="r") as container:
        frames = container.decode(audio=0)
        while True:
            try:
                frame = next(frames)
            except StopIteration:
                break
            except av.error.InvalidDataError:   # a broken frame is skipped, as faster-whisper does
                continue
            frame.pts = None
            chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
        chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    del resampler
    gc.collect()   # PyAV keeps resampler buffers until a collection (faster-whisper #390)
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


@dataclass
class Transcript:
    text: str
    audio_s: float
    ms: int


def join(parts: Iterable[str]) -> str:
    return " ".join(p.strip() for p in parts if p.strip()).strip()


class Transcriber:
    """One faster-whisper model on the CPU, loaded on first use; thread-safe loading."""

    def __init__(self, model: str = DEFAULT_MODEL, threads: int = 8, compute_type: str = "int8") -> None:
        self.model_name, self.threads, self.compute_type = model, threads, compute_type
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                started = time.monotonic()
                self._model = WhisperModel(self.model_name, device="cpu", compute_type=self.compute_type,
                                           cpu_threads=self.threads)
                log.info("Whisper %s loaded on CPU in %.1fs", self.model_name, time.monotonic() - started)
        return self._model

    def warm(self) -> None:
        self._load()

    def transcribe(self, audio: bytes, mime: str = "audio/ogg", *, language: Optional[str] = "en",
                   prompt: Optional[str] = None) -> Transcript:
        """A voice note's words. ``prompt`` biases Whisper towards names that matter (Whisper's
        ``initial_prompt``); ``language=None`` detects it."""
        model = self._load()
        suffix = EXT.get((mime or "").split(";")[0].strip(), ".ogg")
        started = time.monotonic()
        with tempfile.NamedTemporaryFile(suffix=suffix) as f:
            f.write(audio)
            f.flush()
            segments, info = model.transcribe(decode_audio(f.name), language=language, beam_size=5, vad_filter=True,
                                              initial_prompt=prompt, condition_on_previous_text=False)
            text = join(s.text for s in segments)
        return Transcript(text, float(info.duration), int((time.monotonic() - started) * 1000))

    def transcribe_clip(self, path: Path) -> str:
        """The speech of a saved video or reel (``labkit.media``'s ``transcribe``): any language,
        detected, no prompt (a game clip is not about the lab)."""
        model = self._load()
        segments, _info = model.transcribe(decode_audio(str(path)), language=None, beam_size=5, vad_filter=True,
                                           condition_on_previous_text=False)
        return join(s.text for s in segments)
