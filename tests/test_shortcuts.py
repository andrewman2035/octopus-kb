"""P23: the macOS Shortcuts (Safari share sheet, Services menu) post what the endpoint takes, without the password."""
import importlib.util
import json
import plistlib
from pathlib import Path

import pytest

from octopus.extension import parse_request

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_shortcuts", ROOT / "extension" / "shortcuts" / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def _params(wf, identifier):
    return [a["WFWorkflowActionParameters"] for a in wf["WFWorkflowActions"]
            if a["WFWorkflowActionIdentifier"] == identifier]


def _fields(dictionary):
    return {item["WFKey"]["Value"]["string"]: item["WFValue"]["Value"] for item in
            dictionary["Value"]["WFDictionaryFieldValueItems"]}


@pytest.mark.parametrize("kind", ["page", "selection"])
def test_the_password_is_asked_on_import_never_stored(kind):
    wf = build.workflow(kind)
    texts = _params(wf, "is.workflow.actions.gettext")
    assert texts[0]["WFTextActionText"] == "" and texts[1]["WFTextActionText"] == build.ADDRESS
    questions = wf["WFWorkflowImportQuestions"]
    assert [(q["ActionIndex"], q["ParameterKey"]) for q in questions] == [(0, "WFTextActionText"),
                                                                         (1, "WFTextActionText")]
    assert build.ADDRESS == "http://127.0.0.1:8765/save"


@pytest.mark.parametrize("kind", ["page", "selection"])
def test_it_posts_json_with_the_bearer_password(kind):
    wf = build.workflow(kind)
    password = wf["WFWorkflowActions"][0]["WFWorkflowActionParameters"]["UUID"]
    (post,) = _params(wf, "is.workflow.actions.downloadurl")
    assert post["WFHTTPMethod"] == "POST" and post["WFHTTPBodyType"] == "JSON"
    auth = _fields(post["WFHTTPHeaders"])["Authorization"]
    assert auth["string"] == "Bearer " + build.OBJ
    assert auth["attachmentsByRange"] == {"{7, 1}": {"OutputUUID": password, "Type": "ActionOutput",
                                                     "OutputName": "Text"}}
    body = _fields(post["WFJSONValues"])
    assert body["kind"]["string"] == kind
    # what the endpoint gets once the variables are filled in is a request it accepts
    filled = {"kind": kind, "url": "https://a.com/x"} if kind == "page" else {"kind": kind, "text": "选中的字"}
    assert set(body) == set(filled)
    assert parse_request(json.dumps(filled).encode()).kind == kind


def test_where_each_shortcut_shows_up():
    page, selection = build.workflow("page"), build.workflow("selection")
    assert page["WFWorkflowTypes"] == ["ActionExtension"]  # the share sheet (Safari)
    assert "WFSafariWebPageContentItem" in page["WFWorkflowInputContentItemClasses"]
    assert selection["WFWorkflowTypes"] == ["QuickActions"] and selection["WFQuickActionSurfaces"] == ["Services"]
    assert "WFStringContentItem" in selection["WFWorkflowInputContentItemClasses"]


def test_the_plists_serialize():
    for kind in ("page", "selection"):
        assert plistlib.loads(plistlib.dumps(build.workflow(kind), fmt=plistlib.FMT_BINARY))["WFWorkflowActions"]


def _signed(names):
    """The signed files carry the signer's Apple certificate, so a public export always leaves them out
    (scripts/export_public.py, no override); users build and sign their own with build.py."""
    paths = [ROOT / "extension" / "shortcuts" / f"{name}.shortcut" for name in names]
    if not any(p.exists() for p in paths):
        pytest.skip("no signed shortcuts in this tree (a public export): build them with build.py")
    return paths


def test_the_signed_files_are_in_the_repo():
    for path in _signed((build.PAGE_NAME, build.SELECTION_NAME)):
        assert path.read_bytes()[:4] == b"AEA1"  # signed (Apple Encrypted Archive): importable on any Mac


def test_what_the_notifications_say():
    """M7: the 202 means queued, not saved; the other branch is a refused request (Octopus not running at all stops
    the shortcut with Shortcuts' own error)."""
    wf = build.workflow("page")
    shown = [p["WFNotificationActionBody"] for p in _params(wf, "is.workflow.actions.notification")]
    assert shown == [build.SAVED, build.NOT_SAVED] and "收到" in build.SAVED and "已存" not in build.SAVED


# ---- P25: the English variants ----

@pytest.mark.parametrize("kind", ["page", "selection"])
def test_the_english_shortcut_is_the_same_shortcut_in_english(kind):
    zh, en = build.workflow(kind, "zh"), build.workflow(kind, "en")
    assert [a["WFWorkflowActionIdentifier"] for a in zh["WFWorkflowActions"]] == \
        [a["WFWorkflowActionIdentifier"] for a in en["WFWorkflowActions"]]
    shown = [p["WFNotificationActionBody"] for p in _params(en, "is.workflow.actions.notification")]
    assert shown == [build.TEXTS["en"]["saved"], build.TEXTS["en"]["not_saved"]]
    questions = [q["Text"] for q in en["WFWorkflowImportQuestions"]]
    assert questions == [build.TEXTS["en"]["ask_password"], build.TEXTS["en"]["ask_address"]]
    texts = json.dumps(en, default=str) + "".join(build.TEXTS["en"].values())
    assert not any("一" <= c <= "鿿" for c in texts)
    zh_default = build.workflow(kind)  # Chinese as before
    assert [q["Text"] for q in zh_default["WFWorkflowImportQuestions"]] == [build.ASK_PASSWORD, build.ASK_ADDRESS]


def test_the_signed_english_files_are_in_the_repo():
    for path in _signed([build.TEXTS["en"][kind] for kind in ("page", "selection")]):
        assert path.read_bytes()[:4] == b"AEA1"
