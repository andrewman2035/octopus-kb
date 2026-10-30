"""P25: the message catalog. Both languages have the same keys and placeholders, t() follows the configured
language, and no module hides t() behind a local name."""
import ast
import re
import string
from pathlib import Path

import pytest

from octopus import i18n
from octopus.i18n import en, zh

SRC = Path(__file__).parents[1] / "src" / "octopus"
CJK = re.compile("[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}


def test_both_catalogs_have_the_same_keys():
    assert set(zh.MESSAGES) == set(en.MESSAGES)


@pytest.mark.parametrize("key", sorted(zh.MESSAGES))
def test_both_catalogs_have_the_same_placeholders(key):
    assert _fields(zh.MESSAGES[key]) == _fields(en.MESSAGES[key]), key


def test_the_english_catalog_has_no_cjk():
    bad = {k: v for k, v in en.MESSAGES.items() if CJK.search(v)}
    assert bad == {}


def test_t_fills_named_placeholders_and_follows_the_language():
    zh.MESSAGES["test.hello"] = "你好 {name}"
    en.MESSAGES["test.hello"] = "Hello {name}"
    try:
        assert i18n.t("test.hello", name="a") == "你好 a"
        i18n.set_language("en")
        assert i18n.t("test.hello", name="a") == "Hello a"
        assert i18n.t("test.hello") == "Hello {name}"  # no values: as written
    finally:
        del zh.MESSAGES["test.hello"], en.MESSAGES["test.hello"]


def test_unknown_language_and_key_fail_loudly():
    with pytest.raises(ValueError):
        i18n.set_language("fr")
    with pytest.raises(KeyError):
        i18n.t("no.such.key")


def test_no_module_that_uses_t_binds_a_local_t():
    """A local `t` (a loop variable, an argument) would hide the catalog's t() in that function."""
    bad = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not any(isinstance(n, ast.ImportFrom) and n.module == "octopus.i18n" and any(a.name == "t" for a in n.names)
                   for n in ast.walk(tree)):
            continue
        for node in ast.walk(tree):
            bound = ((isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == "t")
                     or (isinstance(node, ast.arg) and node.arg == "t"))
            if bound:
                bad.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert bad == []


def test_the_catalogs_have_no_key_twice():
    """A key written twice in a dict literal silently keeps the second text."""
    for path in (SRC / "i18n" / "zh.py", SRC / "i18n" / "en.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        keys = [k.value for n in ast.walk(tree) if isinstance(n, ast.Dict) for k in n.keys
                if isinstance(k, ast.Constant)]
        assert sorted(k for k in set(keys) if keys.count(k) > 1) == [], path.name


def test_no_module_imports_a_message_alias():
    """`from m import NAME` of a catalog alias is read once, at import: the text would stay in the language of that
    moment. Code calls t() instead (the aliases are for old callers and tests)."""
    import importlib

    bad = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("octopus."):
                module = importlib.import_module(node.module)
                bad += [f"{path.relative_to(SRC)}: {node.module}.{a.name}" for a in node.names
                        if a.name not in vars(module) and hasattr(module, a.name)]
    assert bad == []


def _specs(text: str) -> list[tuple[str, str | None, str]]:
    return sorted((name, conv, spec) for _, name, spec, conv in string.Formatter().parse(text) if name is not None)


@pytest.mark.parametrize("key", sorted(zh.MESSAGES))
def test_both_catalogs_convert_and_format_alike(key):
    """Review m12: the same fields, each with the same !r and format spec, as often in both languages."""
    assert _specs(zh.MESSAGES[key]) == _specs(en.MESSAGES[key]), key


def test_every_call_passes_exactly_the_placeholders_its_text_has():
    """Review m12: t("key", ...) with a literal key passes every field of the text and no other (a missing one would
    raise KeyError on that path only; with no values at all the reader would see "{title}")."""
    bad = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t"
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                continue
            key = node.args[0].value
            if key not in zh.MESSAGES:
                bad.append(f"{path.relative_to(SRC)}:{node.lineno} unknown key {key}")
                continue
            given = {kw.arg for kw in node.keywords}
            if None in given:
                continue  # **values: not checked here
            if given != _fields(zh.MESSAGES[key]):
                bad.append(f"{path.relative_to(SRC)}:{node.lineno} {key}: {sorted(given)} != "
                           f"{sorted(_fields(zh.MESSAGES[key]))}")
    assert bad == []
