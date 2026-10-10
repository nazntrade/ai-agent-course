"""Deterministic small OpenAPI 3.1 subset generator; rejects unknown schemas."""
import hashlib
import json
from pathlib import Path
import re

from harness.runner import ROOT,save_json

SCHEMA=ROOT/"docs/api/openapi.json"
OUTPUT=ROOT/"android/app/src/main/java/com/example/privatechat/api/GeneratedContract.kt"


def camel(value):
    if not re.fullmatch(r"[a-z][a-z0-9_]*",value):
        raise ValueError("unsupported_field_name")
    head,*tail=value.split("_")
    return head+"".join(part.capitalize() for part in tail)


def field(schema,key="key"):
    nullable=False
    if "anyOf" in schema:
        branches=schema["anyOf"]
        if len(branches)!=2 or sum(branch.get("type")=="null" for branch in branches)!=1:
            raise ValueError("unsupported_union")
        schema=next(branch for branch in branches if branch.get("type")!="null")
        nullable=True
    if "$ref" in schema:
        ref=schema["$ref"]
        if not re.fullmatch(r"#/components/schemas/[A-Za-z][A-Za-z0-9]*",ref):
            raise ValueError("unsupported_reference")
        kind=ref.rsplit("/",1)[1]
        read=f'{kind}.fromJson(value.objectValue({key}))'
        write="FIELD.toJson()"
    elif schema.get("type")=="array":
        kind,read_item,write_item=field(schema["items"],"index.toString()")
        if not schema["items"].get("$ref"):
            raise ValueError("unsupported_array_items")
        item=kind
        kind=f"List<{kind}>"
        read=f'value.arrayValue({key}).let {{ list -> (0 until list.length()).map {{ index -> {item}.fromJson(list.getJSONObject(index)) }} }}'
        write="JSONArray(FIELD.map { it.toJson() })"
    else:
        kind={"string":"String","integer":"Long","number":"Double","boolean":"Boolean"}.get(schema.get("type"))
        if not kind:
            raise ValueError("unsupported_schema_type")
        read=f'value.{kind.lower()}Value({key})'
        write="FIELD"
    if nullable:
        kind+="?"
        read=f'if (value.isNull({key})) null else {read}'
        write=f"if (FIELD == null) JSONObject.NULL else {write}"
    return kind,read,write


def generate(document):
    if document.get("openapi")!="3.1.0":
        raise ValueError("openapi_31_required")
    submit=document["paths"]["/v1/conversations/{identifier}/requests"]["post"]["responses"]
    for code in ("200","202"):
        if submit[code]["content"]["application/json"]["schema"]!={"$ref":"#/components/schemas/Snapshot"}:
            raise ValueError("submit_replay_contract_changed")
    models=document["components"]["schemas"]
    required={"Conversation","ConversationPage","CreateConversation","RenameConversation","SubmitRequest",
        "Snapshot","Message","MessagePage","Identity","ServiceStatus","ErrorDetail","ErrorResponse"}
    if set(models)!=required:
        raise ValueError("unsupported_contract_models")
    lines=['package com.example.privatechat.api','','import org.json.JSONArray','import org.json.JSONObject','',
        '// Generated from the actual OpenAPI document. Do not edit manually.',
        'private fun JSONObject.fields(required: Set<String>) { require(keys().asSequence().toSet() == required) }',
        'private fun JSONObject.stringValue(key: String): String = get(key).let { require(it is String); it }',
        'private fun JSONObject.longValue(key: String): Long = get(key).let { require(it is Int || it is Long); (it as Number).toLong() }',
        'private fun JSONObject.doubleValue(key: String): Double = get(key).let { require(it is Number); it.toDouble().also { n -> require(n.isFinite()) } }',
        'private fun JSONObject.booleanValue(key: String): Boolean = get(key).let { require(it is Boolean); it }',
        'private fun JSONObject.objectValue(key: String): JSONObject = get(key).let { require(it is JSONObject); it }',
        'private fun JSONObject.arrayValue(key: String): JSONArray = get(key).let { require(it is JSONArray); it }','']
    for name,schema in sorted(models.items()):
        props=schema["properties"]
        if schema.get("type")!="object" or schema.get("additionalProperties") is not False or set(schema["required"])!=set(props):
            raise ValueError("unsupported_object_model")
        definitions=[];readers=[];writers=[];checks=[]
        for key,value in props.items():
            kind,read,write=field(value,json.dumps(key))
            variable=camel(key)
            definitions.append(f"    val {variable}: {kind}")
            readers.append(f"                {variable} = {read}")
            writers.append(f'        .put({json.dumps(key)}, {write.replace("FIELD",variable)})')
            if "enum" in value:
                checks.append(f'require({variable} in setOf('+", ".join(json.dumps(v) for v in value["enum"])+'))')
            if "minLength" in value:
                checks.append(f'require({variable}.length >= {value["minLength"]})')
            if "maxLength" in value:
                checks.append(f'require({variable}.length <= {value["maxLength"]})')
            if "minimum" in value:
                checks.append(f'require({variable} >= {int(value["minimum"])}L)')
        lines += [f'data class {name}(',",\n".join(definitions),') {']
        if checks:
            lines += ['    init { '+ '; '.join(checks)+' }']
        lines += ['    fun toJson(): JSONObject = JSONObject()',*writers,
            '    companion object {',f'        fun fromJson(value: JSONObject): {name} {{',
            '            value.fields(setOf('+", ".join(json.dumps(key) for key in props)+'))',
            f'            return {name}(',",\n".join(readers),'            )','        }','    }','}','']
    return "\n".join(lines)


def check_saved(document,saved):
    expected=generate(document).encode("utf-8")
    if saved!=expected:
        raise ValueError("saved_android_contract_stale_or_modified")


def verify():
    if not OUTPUT.is_file() or OUTPUT.stat().st_size>1048576:
        raise ValueError("saved_android_contract_required")
    check_saved(json.loads(SCHEMA.read_bytes()),OUTPUT.read_bytes())


def main():
    raw=SCHEMA.read_bytes()
    result=generate(json.loads(raw))
    OUTPUT.parent.mkdir(parents=True,exist_ok=True)
    OUTPUT.write_text(result,encoding="utf-8",newline="\n")
    save_json(ROOT/"docs/artifacts/android-contract-verification.json",{
        "schema_version":"android-contract-v1","technical_status":"PASS",
        "source_sha256":hashlib.sha256(raw).hexdigest(),
        "generated_sha256":hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
        "new_status":202,"replay_status":200,"models":12,
        "acceptance_status":"PARTIAL","scope":"generated DTO contract only"})
    return 0
