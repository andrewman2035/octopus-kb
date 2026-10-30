import pytest

from octopus.media import MediaError, NoSpeechError
from octopus.media.asr import Transcript, WhisperTranscriber, paragraphs


def seg(text, start, end):
    return {"text": text, "start": start, "end": end}


def test_paragraphs_break_on_gap_and_length():
    segs = [seg("复利", 0, 1), seg("需要时间。", 1, 2), seg("Second part", 5, 6), seg("continues", 6, 7)]
    assert paragraphs(segs) == "复利需要时间。\n\nSecond part continues"
    long = [seg("字" * 200, 0, 1), seg("字" * 200, 1, 2), seg("尾", 2, 3)]
    assert paragraphs(long, max_chars=300).count("\n\n") == 1


def test_paragraphs_fallback():
    assert paragraphs([], fallback=" 全文 ") == "全文"


def test_transcriber_converts_traditional_chinese(tmp_path):
    calls = []

    def fake(path, **kw):
        calls.append((path, kw))
        return {"text": "復利需要時間", "language": "zh", "segments": [seg("復利需要時間", 0, 2)]}

    t = WhisperTranscriber("m", transcribe_fn=fake).transcribe(tmp_path / "a.m4a")
    assert t == Transcript("复利需要时间", "zh")
    path, kw = calls[0]
    assert path == str(tmp_path / "a.m4a")
    assert kw["path_or_hf_repo"] == "m" and kw["condition_on_previous_text"] is False


def test_transcriber_keeps_english(tmp_path):
    fake = lambda path, **kw: {"text": "Hello world", "language": "en", "segments": [seg("Hello world", 0, 1)]}
    assert WhisperTranscriber("m", transcribe_fn=fake).transcribe(tmp_path / "a.mp3").text == "Hello world"


def test_transcriber_errors(tmp_path):
    def boom(path, **kw):
        raise RuntimeError("ffmpeg missing")

    with pytest.raises(MediaError, match="转写失败"):
        WhisperTranscriber("m", transcribe_fn=boom).transcribe(tmp_path / "a.mp3")
    empty = lambda path, **kw: {"text": " ", "language": "zh", "segments": []}
    with pytest.raises(NoSpeechError, match="没有识别出"):  # a MediaError too; P29 tells it apart
        WhisperTranscriber("m", transcribe_fn=empty).transcribe(tmp_path / "a.mp3")


def test_transcriber_converts_traditional_even_when_language_misdetected(tmp_path):
    fake = lambda path, **kw: {"text": "復利", "language": "en", "segments": [seg("復利", 0, 1)]}
    assert WhisperTranscriber("m", transcribe_fn=fake).transcribe(tmp_path / "a.mp3").text == "复利"


def test_transcriber_leaves_japanese_alone(tmp_path):
    fake = lambda path, **kw: {"text": "復利の話", "language": "ja", "segments": [seg("復利の話", 0, 1)]}
    assert WhisperTranscriber("m", transcribe_fn=fake).transcribe(tmp_path / "a.mp3").text == "復利の話"


def test_transcriber_drops_silence_hallucinations(tmp_path):
    ghost = {**seg("谢谢观看", 5, 6), "no_speech_prob": 0.9, "avg_logprob": -1.5}
    quiet_but_confident = {**seg("真话", 0, 1), "no_speech_prob": 0.9, "avg_logprob": -0.2}
    fake = lambda path, **kw: {"text": "x", "language": "zh", "segments": [quiet_but_confident, ghost]}
    assert WhisperTranscriber("m", transcribe_fn=fake).transcribe(tmp_path / "a.mp3").text == "真话"


@pytest.mark.live
def test_real_whisper_on_generated_speech(tmp_path):
    import subprocess

    audio = tmp_path / "zh.aiff"
    subprocess.run(["say", "-v", "Tingting", "-o", str(audio), "复利需要时间和耐心"], check=True)
    t = WhisperTranscriber("mlx-community/whisper-large-v3-turbo").transcribe(audio)
    assert "复利" in t.text and t.language == "zh"


# ---- P22: faster-whisper on Linux ----

class FakeSegment:
    def __init__(self, text, start, end, no_speech_prob=0.0, avg_logprob=-0.2):
        self.text, self.start, self.end = text, start, end
        self.no_speech_prob, self.avg_logprob = no_speech_prob, avg_logprob


class FakeInfo:
    def __init__(self, language):
        self.language = language


class FakeModel:
    def __init__(self, segments, language="zh", error=None):
        self.segments, self.language, self.error, self.calls = segments, language, error, []

    def transcribe(self, path, **kw):
        self.calls.append((path, kw))

        def gen():  # faster-whisper decodes lazily: errors come while iterating
            if self.error:
                raise self.error
            yield from self.segments

        return gen(), FakeInfo(self.language)


def test_faster_whisper_gives_the_same_transcript(tmp_path):
    from octopus.media.asr import FasterWhisperTranscriber

    model = FakeModel([FakeSegment("復利", 0, 1), FakeSegment("需要時間。", 1, 2), FakeSegment("Next", 5, 6)])
    loaded = []
    t = FasterWhisperTranscriber("small", model_fn=lambda name: loaded.append(name) or model)
    assert loaded == []  # loads on first use, never at construction
    assert t.transcribe(tmp_path / "a.m4a") == Transcript("复利需要时间。\n\nNext", "zh")
    t.transcribe(tmp_path / "a.m4a")
    assert loaded == ["small"]  # once
    path, kw = model.calls[0]
    assert path == str(tmp_path / "a.m4a") and kw["condition_on_previous_text"] is False
    assert kw["beam_size"] == 1  # greedy, like the MLX path (and ~2x faster on a CPU than the default 5)


def test_faster_whisper_drops_silence_and_keeps_japanese(tmp_path):
    from octopus.media.asr import FasterWhisperTranscriber

    ghost = FakeSegment("谢谢观看", 5, 6, no_speech_prob=0.9, avg_logprob=-1.5)
    model = FakeModel([FakeSegment("復利の話", 0, 1), ghost], language="ja")
    assert FasterWhisperTranscriber("m", model_fn=lambda n: model).transcribe(tmp_path / "a.mp3").text == "復利の話"


def test_faster_whisper_errors(tmp_path):
    from octopus.media.asr import FasterWhisperTranscriber

    broken = FakeModel([], error=RuntimeError("bad audio"))
    with pytest.raises(MediaError, match="转写失败"):
        FasterWhisperTranscriber("m", model_fn=lambda n: broken).transcribe(tmp_path / "a.mp3")
    with pytest.raises(MediaError, match="没有识别出"):
        FasterWhisperTranscriber("m", model_fn=lambda n: FakeModel([])).transcribe(tmp_path / "a.mp3")

    def no_model(name):
        raise OSError("no network")

    with pytest.raises(MediaError, match="转写失败"):
        FasterWhisperTranscriber("m", model_fn=no_model).transcribe(tmp_path / "a.mp3")


def test_the_backend_follows_the_system_unless_set(monkeypatch):
    from octopus import host
    from octopus.config import MediaConfig
    from octopus.media.asr import FasterWhisperTranscriber, asr_backend, make_transcriber

    assert asr_backend("auto") == "mlx"
    t = make_transcriber(MediaConfig())
    assert isinstance(t.__self__, WhisperTranscriber) and t.__self__.model == "mlx-community/whisper-large-v3-turbo"
    t = make_transcriber(MediaConfig(asr_backend="faster-whisper", faster_whisper_model="medium"))
    assert isinstance(t.__self__, FasterWhisperTranscriber) and t.__self__.model == "medium"
    monkeypatch.setattr(host, "system", lambda: host.LINUX)
    assert asr_backend("auto") == "faster-whisper" and asr_backend("mlx") == "mlx"
    t = make_transcriber(MediaConfig())
    assert isinstance(t.__self__, FasterWhisperTranscriber) and t.__self__.model == "small"


def test_importing_the_media_modules_needs_no_mac_packages():
    import subprocess
    import sys

    code = ("import sys; sys.modules['mlx_whisper'] = None; sys.modules['ocrmac'] = None; "
            "sys.modules['faster_whisper'] = None; sys.modules['rapidocr'] = None; "
            "import octopus.media.asr, octopus.media.ocr, octopus.media.pdf, octopus.bootstrap, octopus.cli")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
