"""Links to PDF files: download only; the pipeline extracts the text (OCR fallback) and deletes the download."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlsplit

from octopus.fetchers import http
from octopus.fetchers.base import FetchResult


class PDFFetcher:
    name = "file"

    def __init__(self, download_dir: Path, max_bytes: int):
        self.download_dir = download_dir
        self.max_bytes = max_bytes

    def can_handle(self, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path.lower()
        # arxiv serves PDFs at /pdf/<id> with no extension
        arxiv = parts.netloc.lower().split(":")[0] in ("arxiv.org", "www.arxiv.org") and path.startswith("/pdf/")
        return path.endswith(".pdf") or arxiv

    def fetch(self, url: str) -> FetchResult:
        path = http.download(url, self.download_dir, max_bytes=self.max_bytes, suffix=".pdf")
        name = unquote(Path(urlsplit(url).path).name)
        return FetchResult(url=url, source=self.name, title=name, doc_path=path)
