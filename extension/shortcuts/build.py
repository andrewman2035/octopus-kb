"""Builds the two macOS Shortcuts that save to Octopus through the Chrome extension's endpoint (P23).

    python3 extension/shortcuts/build.py            # writes the unsigned files and signs them (macOS, network)
    python3 extension/shortcuts/build.py --no-sign  # only the unsigned plists

Each comes in Chinese and in English (P25): the same shortcut, its name, questions and notifications in that
language (README: import the ones in your language).

存到 Octopus（网页）: Safari's share sheet (any app's shared link) → POST /save {"kind": "page", "url"}.
存到 Octopus（选中文字）: the Services menu on selected text in any app → POST /save {"kind": "selection", "text"}.
The password is never in the file: each shortcut asks for it once, when it is added (an import question), together
with the address (default http://127.0.0.1:8765/save). Signed with `shortcuts sign --mode anyone`.
"""
from __future__ import annotations

import plistlib
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADDRESS = "http://127.0.0.1:8765/save"
OBJ = "￼"  # where a variable sits inside a text field
ASK_PASSWORD = "Octopus 的密码（终端里运行 uv run octopus extension token --copy，然后粘贴）"
ASK_ADDRESS = "Octopus 的保存地址（没改过端口就不用动）"
SAVED = "Octopus 已收到，正在存"  # the 202 only says it was queued
# shown for an answer without an id (wrong password, a bad request). When Octopus is not running at all, "Get Contents
# of URL" fails and Shortcuts stops with its own error dialog: a shortcut has no way to catch an action's error
NOT_SAVED = "没存上：密码不对或内容有误（重新导入这个快捷指令再填一次密码）"
PAGE_NAME = "存到 Octopus（网页）"
SELECTION_NAME = "存到 Octopus（选中文字）"
TEXTS = {
    "zh": {"ask_password": ASK_PASSWORD, "ask_address": ASK_ADDRESS, "saved": SAVED, "not_saved": NOT_SAVED,
           "page": PAGE_NAME, "selection": SELECTION_NAME},
    "en": {"ask_password": "Octopus password (run uv run octopus extension token --copy in a terminal, then paste it)",
           "ask_address": "Octopus save address (leave it unless you changed the port)",
           "saved": "Octopus got it and is saving it",
           "not_saved": "Not saved: wrong password or bad content (import this shortcut again and enter the password)",
           "page": "Save to Octopus (page)", "selection": "Save to Octopus (selected text)"},
}


def _uuid() -> str:
    return str(uuid.uuid4()).upper()


def _output(action_uuid: str, name: str) -> dict:
    return {"OutputUUID": action_uuid, "Type": "ActionOutput", "OutputName": name}


def _text(string: str, attachments: dict[int, dict] | None = None) -> dict:
    """A text field; attachments: the position of each OBJ in string -> the variable there."""
    value: dict = {"string": string}
    if attachments:
        value["attachmentsByRange"] = {f"{{{pos}, 1}}": var for pos, var in attachments.items()}
    return {"Value": value, "WFSerializationType": "WFTextTokenString"}


def _variable(var: dict) -> dict:
    return {"Value": var, "WFSerializationType": "WFTextTokenAttachment"}


def _dictionary(items: list[tuple[str, dict]]) -> dict:
    return {"Value": {"WFDictionaryFieldValueItems": [
        {"WFItemType": 0, "WFKey": _text(key), "WFValue": value} for key, value in items]},
        "WFSerializationType": "WFDictionaryFieldValue"}


def _action(identifier: str, **params) -> dict:
    return {"WFWorkflowActionIdentifier": identifier, "WFWorkflowActionParameters": params}


def workflow(kind: str, lang: str = "zh") -> dict:
    """The shortcut as Shortcuts stores it (a WFWorkflow plist): kind "page" or "selection", in lang's words."""
    words = TEXTS[lang]
    password, address, response, value = _uuid(), _uuid(), _uuid(), _uuid()
    group = _uuid()
    actions = [
        _action("is.workflow.actions.gettext", UUID=password, WFTextActionText=""),
        _action("is.workflow.actions.gettext", UUID=address, WFTextActionText=ADDRESS),
    ]
    if kind == "page":
        links = _uuid()
        actions.append(_action("is.workflow.actions.detect.link", UUID=links,
                               WFInput=_variable({"Type": "ExtensionInput"})))
        body = [("kind", _text("page")), ("url", _text(OBJ, {0: _output(links, "URLs")}))]
    else:
        body = [("kind", _text("selection")), ("text", _text(OBJ, {0: {"Type": "ExtensionInput"}}))]
    actions += [
        _action("is.workflow.actions.downloadurl", UUID=response,
                WFURL=_text(OBJ, {0: _output(address, "Text")}), WFHTTPMethod="POST", ShowHeaders=True,
                WFHTTPHeaders=_dictionary([("Authorization", _text("Bearer " + OBJ, {7: _output(password, "Text")}))]),
                WFHTTPBodyType="JSON", WFJSONValues=_dictionary(body)),
        _action("is.workflow.actions.getvalueforkey", UUID=value, WFDictionaryKey="id",
                WFInput=_variable(_output(response, "Contents of URL"))),
        _action("is.workflow.actions.conditional", GroupingIdentifier=group, WFControlFlowMode=0, WFCondition=100,
                WFInput={"Type": "Variable", "Variable": _variable(_output(value, "Dictionary Value"))}),
        _action("is.workflow.actions.notification", WFNotificationActionBody=words["saved"]),
        _action("is.workflow.actions.conditional", GroupingIdentifier=group, WFControlFlowMode=1),
        _action("is.workflow.actions.notification", WFNotificationActionBody=words["not_saved"]),
        _action("is.workflow.actions.conditional", GroupingIdentifier=group, WFControlFlowMode=2),
    ]
    page = kind == "page"
    return {
        "WFWorkflowClientVersion": "2302.0.4",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowImportQuestions": [
            {"ActionIndex": 0, "Category": "Parameter", "DefaultValue": "", "ParameterKey": "WFTextActionText",
             "Text": words["ask_password"]},
            {"ActionIndex": 1, "Category": "Parameter", "DefaultValue": ADDRESS, "ParameterKey": "WFTextActionText",
             "Text": words["ask_address"]},
        ],
        "WFWorkflowActions": actions,
        "WFWorkflowInputContentItemClasses": (["WFSafariWebPageContentItem", "WFURLContentItem"] if page
                                              else ["WFStringContentItem", "WFRichTextContentItem"]),
        "WFWorkflowTypes": ["ActionExtension"] if page else ["QuickActions"],
        "WFQuickActionSurfaces": [] if page else ["Services"],
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowHasOutputFallback": False,
    }


def main(argv: list[str]) -> int:
    """--no-sign: only the unsigned plists; --lang zh|en: only that language's two (default: both)."""
    sign = "--no-sign" not in argv
    langs = [argv[argv.index("--lang") + 1]] if "--lang" in argv else list(TEXTS)
    for lang in langs:
        for kind in ("page", "selection"):
            _write(kind, lang, TEXTS[lang][kind], sign)
    return 0


def _write(kind: str, lang: str, name: str, sign: bool) -> None:
    unsigned = HERE / f"{name}.unsigned.shortcut"
    unsigned.write_bytes(plistlib.dumps(workflow(kind, lang), fmt=plistlib.FMT_BINARY))
    if not sign:
        print(f"written: {unsigned}")
        return
    signed = HERE / f"{name}.shortcut"
    signed.unlink(missing_ok=True)  # `shortcuts sign` will not write over a file
    subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", str(unsigned), "--output", str(signed)],
                   check=True)
    unsigned.unlink()
    print(f"signed: {signed}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
