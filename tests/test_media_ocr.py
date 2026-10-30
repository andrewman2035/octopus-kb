import sys
from pathlib import Path

import pytest

from octopus.media import MediaError
from octopus.media.ocr import OCR_LANGS, ocr_image

FONT = Path("/System/Library/Fonts/STHeiti Medium.ttc")


def test_ocr_joins_lines_and_drops_blanks():
    fake = lambda img: [("第一行", 1.0, [0, 0, 1, 1]), ("  ", 0.5, [0, 0, 1, 1]), ("Second", 0.9, [0, 0, 1, 1])]
    assert ocr_image("x.png", recognize_fn=fake) == "第一行\nSecond"


def test_ocr_error_wrapped():
    def boom(img):
        raise OSError("bad image")

    with pytest.raises(MediaError, match="识别图片失败"):
        ocr_image("x.png", recognize_fn=boom)


def test_ocr_langs():
    assert OCR_LANGS == ["zh-Hans", "zh-Hant", "en-US"]


@pytest.mark.skipif(sys.platform != "darwin" or not FONT.exists(), reason="needs macOS Vision and a CJK font")
def test_real_ocr_chinese_and_english(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 220), "white")
    d = ImageDraw.Draw(img)
    d.text((20, 30), "复利需要时间和耐心", fill="black", font=ImageFont.truetype(str(FONT), 56))
    d.text((20, 130), "Compound interest needs time", fill="black", font=ImageFont.truetype(str(FONT), 48))
    path = tmp_path / "shot.png"
    img.save(path)
    text = ocr_image(path)
    assert "复利需要时间和耐心" in text
    assert "Compound interest needs time" in text


@pytest.mark.skipif(sys.platform != "darwin" or not FONT.exists(), reason="needs macOS Vision and a CJK font")
def test_real_ocr_accepts_pil_image():
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 120), "white")
    ImageDraw.Draw(img).text((20, 20), "复利需要时间和耐心", fill="black", font=ImageFont.truetype(str(FONT), 56))
    assert "复利需要时间和耐心" in ocr_image(img)


# ---- P22: RapidOCR on Linux ----

class FakeOutput:
    def __init__(self, txts, scores, boxes):
        self.txts, self.scores, self.boxes = txts, scores, boxes


class FakeEngine:
    def __init__(self, output):
        self.output, self.seen = output, []

    def __call__(self, image):
        self.seen.append(image)
        return self.output


def test_rapidocr_results_read_like_ocrmac_ones(tmp_path):
    from PIL import Image

    from octopus.media.ocr import _rapidocr

    engine = FakeEngine(FakeOutput(("第一行", " ", "Second"), (0.9, 0.5, 0.8), ([[0, 0]], [[1, 1]], [[2, 2]])))
    image = Image.new("RGB", (20, 10), "white")
    assert ocr_image(image, recognize_fn=lambda img: _rapidocr(img, engine)) == "第一行\nSecond"
    assert engine.seen == [image]  # a PIL image (a PDF page) goes in as it is
    assert _rapidocr(image, FakeEngine(FakeOutput(None, None, None))) == []  # nothing found


def test_rapidocr_opens_files_itself_turned_upright(tmp_path):
    from PIL import Image

    from octopus.media.ocr import _rapidocr

    path = tmp_path / "shot.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # a phone photo taken sideways: stored 20x10, shown 10x20
    Image.new("RGB", (20, 10), "white").save(path, exif=exif)
    engine = FakeEngine(FakeOutput(("字",), (0.9,), ([[0, 0]],)))
    assert _rapidocr(str(path), engine) == [("字", 0.9, [[0, 0]])]
    seen = engine.seen[0]
    assert isinstance(seen, Image.Image) and seen.size == (10, 20)  # never the path: no URL fetching by name


def test_ocr_backend_follows_the_system_unless_set(monkeypatch):
    from octopus import host
    from octopus.media import ocr

    assert ocr.ocr_backend("auto") == "ocrmac"
    monkeypatch.setattr(host, "system", lambda: host.LINUX)
    assert ocr.ocr_backend("auto") == "rapidocr" and ocr.ocr_backend("ocrmac") == "ocrmac"
    seen = []
    monkeypatch.setitem(ocr.RECOGNIZERS, "rapidocr", lambda img: seen.append(img) or [("好", 1.0, None)])
    assert ocr.make_ocr("auto")("x.png") == "好"
    assert ocr_image("y.png") == "好"  # the default (a PDF page's OCR) follows the system too
    assert seen == ["x.png", "y.png"]


def _has(module):
    import importlib.util

    return importlib.util.find_spec(module) is not None


@pytest.mark.skipif(not _has("rapidocr") or not _has("onnxruntime"), reason="RapidOCR is installed on Linux only")
def test_real_rapidocr_reads_english(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    from octopus.media.ocr import _rapidocr

    img = Image.new("RGB", (900, 120), "white")
    ImageDraw.Draw(img).text((20, 20), "Compound interest needs time", fill="black", font=ImageFont.load_default(56))
    path = tmp_path / "shot.png"
    img.save(path)
    text = ocr_image(path, recognize_fn=_rapidocr)
    assert "Compound" in text and "interest" in text  # the default font is spaced out: maybe one box per word


def _transparent_with_black_text():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (60, 30), (0, 0, 0, 0))  # transparent pixels stored as black, the usual export
    ImageDraw.Draw(img).rectangle([10, 10, 20, 20], fill=(0, 0, 0, 255))  # the "text"
    return img


def test_rapidocr_puts_a_transparent_png_on_white(tmp_path):
    from octopus.media.ocr import _rapidocr

    path = tmp_path / "sticker.png"
    _transparent_with_black_text().save(path)
    engine = FakeEngine(FakeOutput(None, None, None))
    _rapidocr(str(path), engine)
    seen = engine.seen[0]
    assert seen.mode == "RGB" and seen.getpixel((2, 2)) == (255, 255, 255) and seen.getpixel((15, 15)) == (0, 0, 0)
    engine.seen.clear()
    _rapidocr(_transparent_with_black_text(), engine)  # an image handed over directly too
    assert engine.seen[0].getpixel((2, 2)) == (255, 255, 255)


def test_ocr_image_puts_transparent_images_on_white_for_either_backend():
    from PIL import Image

    seen = []
    ocr_image(_transparent_with_black_text(), recognize_fn=lambda img: seen.append(img) or [])
    assert seen[0].mode == "RGB" and seen[0].getpixel((2, 2)) == (255, 255, 255)
    rgb = Image.new("RGB", (5, 5), "white")
    ocr_image(rgb, recognize_fn=lambda img: seen.append(img) or [])
    assert seen[1] is rgb  # an opaque image (a PDF page) goes in untouched
    ocr_image("x.png", recognize_fn=lambda img: seen.append(img) or [])
    assert seen[2] == "x.png"  # a path to ocrmac: Vision reads the file itself, as before
