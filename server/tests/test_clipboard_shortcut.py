"""Inspect the generated Shortcut graph; never import or run a user's shortcuts."""

import importlib.util
import plistlib
from pathlib import Path


def builder():
    path = Path(__file__).resolve().parents[2] / "scripts" / "build_clipboard_shortcut.py"
    spec = importlib.util.spec_from_file_location("build_clipboard_shortcut", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_template_copies_only_confirmed_payload_then_reports_only_copy():
    document = builder().build_template()
    document = plistlib.loads(plistlib.dumps(document))
    actions = document["WFWorkflowActions"]
    names = [a["WFWorkflowActionIdentifier"] for a in actions]
    assert names.count("is.workflow.actions.setclipboard") == 1
    assert not set(names) & {"is.workflow.actions.alert", "is.workflow.actions.ask", "is.workflow.actions.showresult",
                             "is.workflow.actions.openapp", "is.workflow.actions.getclipboard"}
    copy_at = names.index("is.workflow.actions.setclipboard")
    requests = [a["WFWorkflowActionParameters"] for a in actions if a["WFWorkflowActionIdentifier"] ==
                "is.workflow.actions.downloadurl"]
    posts = [p for p in requests if p.get("WFHTTPMethod") == "POST"]
    assert len(posts) == 1
    assert names.index("is.workflow.actions.downloadurl", copy_at) > copy_at
    values = posts[0]["WFJSONValues"]["Value"]["WFDictionaryFieldValueItems"]
    assert len(values) == 1
    assert values[0]["WFKey"]["Value"]["string"] == "copied"
    assert values[0]["WFItemType"] == 4
    assert values[0]["WFValue"]["Value"] is True
    dictionary_keys = [a["WFWorkflowActionParameters"].get("WFDictionaryKey") for a in actions]
    assert "copy_allowed" in dictionary_keys and "payload_url" in dictionary_keys
    assert "complete_url" not in dictionary_keys


def test_template_has_import_settings_and_no_embedded_credentials(tmp_path):
    module = builder()
    output = tmp_path / "Clipboard.shortcut"
    assert module.main(["--output", str(output)]) == 0
    document = plistlib.loads(output.read_bytes())
    questions = document["WFWorkflowImportQuestions"]
    assert len(questions) == 2
    for question in questions:
        action = document["WFWorkflowActions"][question["ActionIndex"]]
        assert question["ParameterKey"] == "WFTextActionText"
        assert action["WFWorkflowActionIdentifier"] == "is.workflow.actions.gettext"
    assert {q["DefaultValue"] for q in questions} == {"http://computer.local:8891", ""}
    assert b"HIDAGENT_CLIPBOARD_KEY" not in output.read_bytes()
    assert b"focused" not in output.read_bytes() and b"pasted" not in output.read_bytes()
