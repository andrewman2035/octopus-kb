"""P25: no CJK in the code outside the Chinese catalog (i18n/zh.py), except in statements marked "# zh: <reason>"
with one of the reasons below. P26: comments and docstrings count too (they are for contributors, in English); a
comment or docstring that must quote Chinese (the input a pattern matches, third-party text) is marked the same way,
and a comment on a marked statement may quote what that statement matches."""
import io
import re
import tokenize
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "octopus"
CJK = re.compile("[　-〿㐀-䶿一-鿿豈-﫿＀-￯]")
MARK = re.compile(r"#\s*zh:\s*([a-z]+)")
CJK_REASONS = {
    "input": "matches what users type or send (Chinese command words, yes/no words, their regexes): input is "
             "understood in both languages whatever `language` says",
    "content": "matches or cleans third-party content (login walls, WeChat notices, forwarded-mail headers, CJK "
               "punctuation, Chinese stop words)",
    "log": "the in/out log lines of spec v1.9, pinned by their tests: logs are not translated",
    "wire": "the save states the Chrome popup before P25 matched, kept on the wire beside the language-free code",
}
EXEMPT = {SRC / "i18n" / "zh.py"}
MAX_MARKED = 70


def _statements(path: Path):
    """(first line, tokens with CJK, the marks in its comments) per logical line that has CJK or a mark. The tokens
    are the code's strings, the docstring (a logical line made only of strings) and the comments, including the
    comment lines just above the statement."""
    toks, out = [], []
    for tok in tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline):
        if tok.type not in (tokenize.NEWLINE, tokenize.ENDMARKER):
            toks.append(tok)
            continue
        code = [x for x in toks if x.type not in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)]
        texts = [x for x in toks if x.type in (tokenize.STRING, tokenize.FSTRING_MIDDLE, tokenize.COMMENT)
                 and CJK.search(x.string)]
        marks = [m.group(1) for x in toks if x.type == tokenize.COMMENT for m in MARK.finditer(x.string)]
        if texts or marks:
            out.append(((code or toks)[0].start[0], texts, marks))
        toks = []
    return out


def _source_files():
    return sorted(p for p in SRC.rglob("*.py") if p not in EXEMPT)


def test_no_cjk_in_the_code_outside_the_chinese_catalog():
    """User-facing text lives in i18n/zh.py (and its English twin), comments and docstrings are English; what stays
    is marked with its reason, and a mark on a statement without CJK is stale."""
    unmarked, unknown, stale = [], [], []
    for path in _source_files():
        for line, texts, marks in _statements(path):
            where = f"{path.relative_to(SRC)}:{line}"
            if not texts:
                stale.append(where)
            elif not marks:
                unmarked.append(f"{where} {texts[0].string[:60]}")
            elif any(m not in CJK_REASONS for m in marks):
                unknown.append(f"{where} {marks}")
    assert unmarked == [] and unknown == [] and stale == []


def test_the_marked_statements_stay_few():
    """The allow-list is small: a rise means text that belongs in the catalog was marked instead."""
    counts: dict[str, int] = {}
    for path in _source_files():
        for _, texts, marks in _statements(path):
            for m in set(marks):
                counts[m] = counts.get(m, 0) + 1
    assert sum(counts.values()) <= MAX_MARKED, counts
