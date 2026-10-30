"""P26: the files that would go public carry no personal data: no email address outside the reserved example domains,
no real home-directory path, and no binary file outside the export's allowlist. The owner's identity and private
markers stay outside the repository; scripts/export_public.py checks them when a public tree is built."""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("export_public", ROOT / "scripts" / "export_public.py")
export_public = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = export_public  # dataclasses look their module up
_spec.loader.exec_module(export_public)


def _public_files() -> list[str]:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("needs a git checkout (the Docker test image has none)")
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p and not export_public.is_private(p)]


def test_public_files_have_no_generic_private_markers():
    hits = [h for rel in _public_files() if (ROOT / rel).is_file()
            for h in export_public.scan_file(ROOT / rel, rel, [])
            if "owner TODO" not in h]  # a release gate for the export, not personal data
    assert hits == []


def test_public_markdown_links_only_to_public_files():
    """A link from a public document to an internal one (docs/superpowers/, docs/smoke-*) would break once exported."""
    public = set(_public_files())
    broken = []
    for rel in sorted(p for p in public if p.endswith(".md")):
        for n, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            for target in re.findall(r"\]\(([^)#\s]+)", line):
                if "://" in target or target.startswith("mailto:"):
                    continue
                resolved = os.path.normpath(os.path.join(os.path.dirname(rel), target))
                if export_public.is_private(resolved) or not (ROOT / resolved).exists():
                    broken.append(f"{rel}:{n} {target}")
    assert broken == []


@pytest.mark.parametrize("text", [
    # put together here, so this file does not trip its own scan
    "write to someone" + "@mail-provider.com", "C:\\Users\\" + "alice\\notes", "/Users/" + "alice/Octopus",
    "/home/" + "bob/.config",
])
def test_the_generic_scan_catches_real_looking_data(text):
    assert export_public.generic_hits(text) != []


@pytest.mark.parametrize("text", [
    "me@example.com", "news@news.example.com", "a@b.example", "x@evil.test", "owner@im.wechat", "b@im.bot",
    "noreply@anthropic.com", "/Users/me/.local/bin", "/home/me", "T@p.Rw", "a@b.c", "me@example.co.uk",
])
def test_the_generic_scan_allows_placeholders(text):
    assert export_public.generic_hits(text) == []

