"""OCR (runs in a worker thread): macOS Vision through ocrmac, or RapidOCR (ONNX, Chinese + English) on Linux (P22).
Both give (text, confidence, box) annotations; the packages are imported on first use only."""
from __future__ import annotations

import os
import threading
from typing import Any, Callable

from octopus import host
from octopus.i18n import t
from octopus.media import MediaError

OCR_LANGS = ["zh-Hans", "zh-Hant", "en-US"]
_rapid_engine = None
_rapid_lock = threading.Lock()


def _ocrmac(image: Any) -> list:
    from ocrmac import ocrmac

    return ocrmac.OCR(image, recognition_level="accurate", language_preference=OCR_LANGS).recognize()


def _rapid() -> Any:
    """One RapidOCR engine for the process (its models load once, ~1 s)."""
    global _rapid_engine
    with _rapid_lock:
        if _rapid_engine is None:
            from rapidocr import RapidOCR

            _rapid_engine = RapidOCR(params={"Global.log_level": "error"})  # "no text found" is no warning
        return _rapid_engine


def _has_alpha(im: Any) -> bool:
    return im.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in im.info


def _on_white(im: Any) -> Any:
    """RGB, with any transparency composited onto white: a plain convert("RGB") turns transparent pixels (usually
    stored as black) black, and dark text on them disappears."""
    from PIL import Image

    if not _has_alpha(im):
        return im.convert("RGB")
    rgba = im.convert("RGBA")
    return Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba).convert("RGB")


def _rapidocr(image: Any, engine: Any = None) -> list:
    if isinstance(image, (str, os.PathLike)):  # opened here: RapidOCR would fetch a name that looks like a URL
        from PIL import Image, ImageOps

        with Image.open(image) as im:
            image = _on_white(ImageOps.exif_transpose(im))  # a phone photo taken sideways, upright
    elif _has_alpha(image):
        image = _on_white(image)
    out = (engine or _rapid())(image)
    if not out.txts:
        return []
    return list(zip(out.txts, out.scores, out.boxes))


RECOGNIZERS: dict[str, Callable[[Any], list]] = {"ocrmac": _ocrmac, "rapidocr": _rapidocr}


def ocr_backend(choice: str) -> str:
    """"ocrmac" or "rapidocr": what media.ocr_backend = auto means on this system."""
    if choice != "auto":
        return choice
    return "ocrmac" if host.system() == host.MAC else "rapidocr"


def make_ocr(choice: str) -> Callable[[Any], str]:
    return lambda image: ocr_image(image, recognize_fn=RECOGNIZERS[ocr_backend(choice)])


def ocr_image(image: Any, recognize_fn: Callable[[Any], list] | None = None) -> str:
    try:
        recognize = recognize_fn or RECOGNIZERS[ocr_backend("auto")]
        if hasattr(image, "__fspath__"):
            image = str(image)
        elif hasattr(image, "mode") and _has_alpha(image):  # a PIL image with transparency, for either backend
            image = _on_white(image)
        annotations = recognize(image)
    except Exception as e:
        raise MediaError(t("media.ocr_failed", error=e)) from e
    return "\n".join(line.strip() for line, _conf, _box in annotations if line and line.strip())
