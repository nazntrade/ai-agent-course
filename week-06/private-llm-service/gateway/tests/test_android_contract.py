"""Contract changes fail closed before regenerating an incompatible client."""
import copy
import json
from pathlib import Path
import pytest
from harness.generate_android_contract import generate,SCHEMA,OUTPUT,check_saved,verify

pytestmark=pytest.mark.unit


def test_actual_contract_generates_both_replay_and_new_snapshot_models():
    doc=json.loads(SCHEMA.read_text())
    generated=generate(doc)
    assert "data class Snapshot(" in generated and "val stateVersion: Long" in generated
    assert "val activeJobId: String?" in generated and "value.fields(setOf(" in generated
    assert "JSONObject.NULL" in generated
    check_saved(doc,OUTPUT.read_bytes())
    verify()


def test_manually_changed_or_stale_saved_dto_fails_without_rewriting(tmp_path):
    doc=json.loads(SCHEMA.read_text())
    output=tmp_path/"saved.kt"
    original=generate(doc).encode("utf-8")
    output.write_bytes(original+b"// manual edit\n")
    changed=output.read_bytes()
    with pytest.raises(ValueError,match="saved_android_contract_stale_or_modified"):
        check_saved(doc,changed)
    assert output.read_bytes()==changed


@pytest.mark.parametrize("change",["missing_replay","wrong_replay","unknown_model","unknown_union","optional_field"])
def test_incompatible_actual_contract_rejected(change):
    doc=json.loads(SCHEMA.read_text())
    responses=doc["paths"]["/v1/conversations/{identifier}/requests"]["post"]["responses"]
    if change=="missing_replay":
        responses.pop("200")
    elif change=="wrong_replay":
        responses["200"]["content"]["application/json"]["schema"]={"type":"string"}
    elif change=="unknown_model":
        doc["components"]["schemas"]["Unreviewed"]={}
    elif change=="unknown_union":
        doc["components"]["schemas"]["Conversation"]["properties"]["active_job_id"]["anyOf"].append({"type":"integer"})
    else:
        doc["components"]["schemas"]["Conversation"]["required"].remove("id")
    with pytest.raises((ValueError,KeyError)):
        generate(doc)
