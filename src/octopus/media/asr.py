"""Speech-to-text (runs in a worker thread): mlx-whisper on macOS, faster-whisper (CPU, int8) on Linux (P22).
Both give the same Transcript; the packages are imported on first use only."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from octopus import host
from octopus.i18n import t
from octopus.media import MediaError, NoSpeechError
from octopus.text import is_cjk, join_text, to_simplified

if TYPE_CHECKING:
    from octopus.config import MediaConfig


@dataclass
class Transcript:
    text: str
    language: str | None


def _is_silence_hallucination(seg: dict) -> bool:
    return seg.get("no_speech_prob", 0.0) > 0.6 and seg.get("avg_logprob", 0.0) < -1.0


def paragraphs(segments: list[dict], fallback: str = "", max_chars: int = 300, gap: float = 1.5) -> str:
    paras: list[str] = []
    buf, last_end = "", None
    for s in segments:
        said = str(s.get("text", "")).strip()
        if not said:
            continue
        start = float(s.get("start", 0.0))
        if buf and ((last_end is not None and start - last_end > gap) or len(buf) >= max_chars):
            paras.append(buf)
            buf = ""
        buf = join_text(buf, said)
        last_end = float(s.get("end", start))
    if buf:
        paras.append(buf)
    return "\n\n".join(paras) if paras else fallback.strip()


class WhisperTranscriber:
    def __init__(self, model: str, transcribe_fn: Callable[..., dict] | None = None):
        self.model = model
        self._fn = transcribe_fn

    def _load(self) -> Callable[..., dict]:
        if self._fn is None:
            import mlx_whisper

            self._fn = mlx_whisper.transcribe
        return self._fn

    def transcribe(self, path: Path) -> Transcript:
        try:
            r = self._load()(str(path), path_or_hf_repo=self.model, condition_on_previous_text=False, verbose=None)
        except Exception as e:
            raise MediaError(t("media.asr_failed", error=e)) from e
        return _transcript(r)


class FasterWhisperTranscriber:
    """faster-whisper on the CPU with int8 weights: the Linux path. The model downloads on first use (to the
    Hugging Face cache) and is loaded once."""

    def __init__(self, model: str, model_fn: Callable[[str], Any] | None = None):
        self.model = model
        self._model_fn = model_fn
        self._loaded = None
        self._lock = threading.Lock()  # two jobs at once must not load it twice

    def _load(self):
        with self._lock:
            if self._loaded is None:
                if self._model_fn is None:
                    from faster_whisper import WhisperModel

                    self._model_fn = lambda name: WhisperModel(name, device="cpu", compute_type="int8")
                self._loaded = self._model_fn(self.model)
            return self._loaded

    def transcribe(self, path: Path) -> Transcript:
        try:
            # greedy (beam_size=1), as mlx-whisper decodes by default; about half the CPU time of faster-whisper's 5
            segments, info = self._load().transcribe(str(path), beam_size=1, condition_on_previous_text=False)
            segs = [{"text": s.text, "start": s.start, "end": s.end, "no_speech_prob": s.no_speech_prob,
                     "avg_logprob": s.avg_logprob} for s in segments]  # decoding happens here, lazily
        except Exception as e:
            raise MediaError(t("media.asr_failed", error=e)) from e
        return _transcript({"language": info.language, "segments": segs, "text": ""})


def asr_backend(choice: str) -> str:
    """"mlx" or "faster-whisper": what media.asr_backend = auto means on this system."""
    if choice != "auto":
        return choice
    return "mlx" if host.system() == host.MAC else "faster-whisper"


def make_transcriber(media: MediaConfig) -> Callable[[Path], Transcript]:
    if asr_backend(media.asr_backend) == "mlx":
        return WhisperTranscriber(media.whisper_model).transcribe
    return FasterWhisperTranscriber(media.faster_whisper_model).transcribe


def _transcript(r: dict) -> Transcript:
    """A whisper result ({language, segments, text}) as clean text: silence dropped, Simplified Chinese."""
    lang = r.get("language")
    segments = [s for s in r.get("segments") or [] if not _is_silence_hallucination(s)]
    text = paragraphs(segments, fallback="" if r.get("segments") else str(r.get("text", "")))
    # Whisper detects the language from the first 30 s, so key off the text itself
    # (Cantonese "yue" also comes back in Traditional); ja/ko keep their kanji.
    if lang not in ("ja", "ko") and any(is_cjk(c) for c in text):
        text = to_simplified(text)
    if not text.strip():
        raise NoSpeechError(t("media.no_speech"))
    return Transcript(text, lang)
