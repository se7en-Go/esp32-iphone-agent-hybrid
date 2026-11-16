#!/usr/bin/env python3
"""Build a secret-free staged clipboard Shortcut; optionally sign the template on macOS.

Only import questions carry pairing configuration. Never read environment secrets,
user Shortcut libraries or clipboard contents. Signing sends the template to Apple.

Envelope/action evidence: Apple's shipped WorkflowKit Gallery/*.wflow files;
https://github.com/electrikmilk/cherri/blob/main/shortcutgen.go (import questions/bool fields);
https://github.com/findlaywebb/shortcut-lib/blob/main/docs/wire-format-quirks.md (HTTP token fields).
"""

import argparse
import json
import plistlib
import subprocess
import uuid
from pathlib import Path


# WorkflowKit conditional operators for these operand types.
TEXT_EQUALS = 4
NUMBER_GREATER_THAN = 2


def _uuid(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "esp32-iphone-agent/clipboard-staged-v1/" + name)).upper()


def _reference(name):
    return {"OutputUUID": _uuid(name), "Type": "ActionOutput", "OutputName": name}


def _attachment(name):
    return {"WFSerializationType": "WFTextTokenAttachment", "Value": _reference(name)}


def _text(literal="", *, output=None, suffix=""):
    value = {"string": literal}
    if output is not None:
        value = {"string": literal + "\ufffc" + suffix,
                 "attachmentsByRange": {f"{{{len(literal)}, 1}}": _reference(output)}}
    return {"WFSerializationType": "WFTextTokenString", "Value": value}


def _dictionary(values):
    entries = []
    for key, value in values.items():
        is_bool = isinstance(value, bool)
        encoded = ({"WFSerializationType": "WFNumberSubstitutableState", "Value": value} if is_bool else value)
        entries.append({"WFItemType": 4 if is_bool else 0, "WFKey": _text(key), "WFValue": encoded})
    return {"WFSerializationType": "WFDictionaryFieldValue", "Value": {"WFDictionaryFieldValueItems": entries}}


def build_template():
    actions = []

    def action(kind, name, **parameters):
        actions.append({"WFWorkflowActionIdentifier": "is.workflow.actions." + kind,
                        "WFWorkflowActionParameters": {"UUID": _uuid(name), "CustomOutputName": name,
                                                       **parameters}})

    def value(name, source, key):
        action("getvalueforkey", name, WFInput=_attachment(source), WFDictionaryKey=key)

    def begin_if(name, source, condition, *, coercion=None, **comparison):
        variable = _attachment(source)
        if coercion is not None:
            variable["Value"]["Aggrandizements"] = [{"Type": "WFCoercionVariableAggrandizement",
                                                     "CoercionItemClass": coercion}]
        action("conditional", name, GroupingIdentifier=_uuid(name + "-group"), WFControlFlowMode=0,
               WFCondition=condition, WFInput={"Type": "Variable", "Variable": variable}, **comparison)

    def end_if(name):
        action("conditional", name + "-end", GroupingIdentifier=_uuid(name + "-group"), WFControlFlowMode=2)

    action("comment", "About", WFCommentActionText=(
        "ESP32 Agent staged clipboard. Set server URL and pairing key at import. "
        "Only downloads a pending payload, copies it locally and acknowledges the actual copy. "
        "The host separately verifies the destination and inserted text. No alert or send action."))
    action("gettext", "Server URL", WFTextActionText="http://computer.local:8891")
    action("gettext", "Pairing key", WFTextActionText="")
    action("downloadurl", "Pending", WFURL=_text(output="Server URL", suffix="/api/clipboard/pending"),
           WFHTTPMethod="GET", ShowHeaders=True,
           WFHTTPHeaders=_dictionary({"Authorization": _text("Bearer ", output="Pairing key")}))
    value("Protocol", "Pending", "protocol")
    action("gettext", "Protocol text", WFTextActionText=_text(output="Protocol"))
    begin_if("Staged protocol", "Protocol text", TEXT_EQUALS, WFConditionalActionString="staged-v1")
    value("Payload URL", "Pending", "payload_url")
    value("Copy receipt URL", "Pending", "ack_url")
    action("downloadurl", "Payload", WFURL=_text(output="Payload URL"), WFHTTPMethod="GET")
    value("Copy allowed", "Payload", "copy_allowed")
    # Convert the JSON boolean to 0 or 1 before comparing the number.
    begin_if("Valid payload", "Copy allowed", NUMBER_GREATER_THAN, coercion="WFNumberContentItem",
             WFNumberValue="0")
    value("Payload text", "Payload", "text")
    # Text coercion preserves leading zeroes; never pass through a Number action.
    action("gettext", "Exact text", WFTextActionText=_text(output="Payload text"))
    action("setclipboard", "Copy locally", WFInput=_attachment("Exact text"), WFLocalOnly=True)
    # This action runs only after Copy succeeds. It does not claim focus or insertion.
    action("downloadurl", "Acknowledge copy", WFURL=_text(output="Copy receipt URL"), WFHTTPMethod="POST",
           WFHTTPBodyType="JSON", WFJSONValues=_dictionary({"copied": True}))
    end_if("Valid payload")
    end_if("Staged protocol")
    action("nothing", "Finish silently")
    return {
        "WFWorkflowClientVersion": "3036.0.4.2", "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900", "WFWorkflowName": "ESP32 Agent Clipboard",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowActions": actions, "WFWorkflowTypes": ["WFWorkflowTypeShowInSearch"],
        "WFWorkflowInputContentItemClasses": [], "WFWorkflowOutputContentItemClasses": [],
        "WFQuickActionSurfaces": [], "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowImportQuestions": [
            {"Category": "Parameter", "ActionIndex": 1, "ParameterKey": "WFTextActionText",
             "Text": "Clipboard server URL reachable from this iPhone (no trailing slash)",
             "DefaultValue": "http://computer.local:8891"},
            {"Category": "Parameter", "ActionIndex": 2, "ParameterKey": "WFTextActionText",
             "Text": "Pairing key from your local ESP32 Agent setup (not a model API key)", "DefaultValue": ""},
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="unsigned secret-free template .shortcut path")
    parser.add_argument("--sign", action="store_true", help="send this secret-free template to Apple for signing")
    args = parser.parse_args(argv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(plistlib.dumps(build_template(), fmt=plistlib.FMT_XML, sort_keys=False))
    result = {"template": str(args.output.resolve()), "contains_pairing_key": False}
    if args.sign:
        signed = args.output.with_name(args.output.stem + "-signed.shortcut")
        subprocess.run(["/usr/bin/shortcuts", "sign", "--mode", "anyone", "--input", str(args.output),
                        "--output", str(signed)], check=True, timeout=60)
        result["signed"] = str(signed.resolve())
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
