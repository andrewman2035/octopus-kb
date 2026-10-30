"""The release metadata the owner chose: MIT, author AndrewMan2035, distribution name octopus-kb. The import package,
the command and everything an existing install depends on keep the name octopus."""
import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("export_public", ROOT / "scripts" / "export_public.py")
export_public = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = export_public  # dataclasses look their module up
_spec.loader.exec_module(export_public)
REPO = "https://github.com/andrewman2035/octopus-kb"


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_distribution_name_license_and_author():
    project = _project()
    assert project["name"] == "octopus-kb"
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["authors"] == [{"name": "AndrewMan2035"}]  # a public handle, no email address
    # a PEP 639 license expression replaces the License :: classifiers; build tools may refuse both together
    assert not [c for c in project["classifiers"] if c.startswith("License ::")]


def test_command_and_import_package_keep_the_name_octopus():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["scripts"] == {"octopus": "octopus.cli:main"}
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/octopus"]


def test_project_urls_point_at_the_public_repository():
    urls = _project()["urls"]
    assert set(urls) == {"Homepage", "Repository", "Issues", "Changelog"}
    assert all(url.startswith(REPO) for url in urls.values())


def test_license_file_is_mit_with_the_holder_line():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License\n")
    assert "Copyright (c) 2026 AndrewMan2035\n" in text
    assert 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND' in text


def test_lock_names_the_editable_project_octopus_kb():
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    editable = [p["name"] for p in lock["package"] if p.get("source") == {"editable": "."}]
    assert editable == ["octopus-kb"]


def _ignore_lines(name: str) -> list[str]:
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"no {name} here (the Docker build context leaves it out)")
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]


def test_git_ignores_signed_shortcuts_and_the_public_export_keeps_the_rule():
    """Nothing signed with someone's Apple account gets committed by accident (the owner's four tracked files stay
    in the private repository), and the exported public tree carries the same rule."""
    assert "*.shortcut" in _ignore_lines(".gitignore")
    assert not export_public.is_private(".gitignore")


def test_signed_shortcuts_never_enter_a_docker_build_context():
    lines = [line for line in _ignore_lines(".dockerignore") if line and not line.startswith("#")]
    # .dockerignore: the last matching line wins, so the exclusion must come after the folder is let in
    assert lines.index("**/*.shortcut") > lines.index("!extension/shortcuts")
