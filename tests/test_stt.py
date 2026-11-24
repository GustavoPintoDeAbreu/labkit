import io
import wave
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("av")
from labkit import stt  # noqa: E402


def _wav(path, seconds=1.0, rate=48000):
    t = np.arange(int(seconds * rate)) / rate
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes((np.sin(2 * np.pi * 440 * t) * 16000).astype("<i2").tobytes())


def test_decode_audio_gives_whisper_16k_mono_float(tmp_path):
    _wav(tmp_path / "a.wav")
    audio = stt.decode_audio(str(tmp_path / "a.wav"))
    assert audio.dtype == np.float32 and abs(len(audio) - 16000) < 200
    assert 0.3 < float(np.abs(audio).max()) <= 1.0


class FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        return iter([SimpleNamespace(text=" hello "), SimpleNamespace(text="lab ")]), SimpleNamespace(duration=1.0)


def test_voice_note_and_clip_use_the_same_model(tmp_path):
    t = stt.Transcriber()
    t._model = FakeModel()
    _wav(tmp_path / "a.wav")
    out = t.transcribe((tmp_path / "a.wav").read_bytes(), "audio/wav", prompt="Kaya, labwatch")
    assert out.text == "hello lab" and out.audio_s == 1.0
    assert t._model.calls[0]["language"] == "en" and t._model.calls[0]["initial_prompt"] == "Kaya, labwatch"
    assert t.transcribe_clip(tmp_path / "a.wav") == "hello lab"
    assert t._model.calls[1]["language"] is None and "initial_prompt" not in t._model.calls[1]
