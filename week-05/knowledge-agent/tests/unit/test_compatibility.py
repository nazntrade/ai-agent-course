"""check_index_compatibility is a pure function (D21-08, SPEC 10)."""

from __future__ import annotations

import pytest

from knowledge_agent.domain.contracts import (
    COMPATIBILITY_FIELDS,
    CORPUS_SCHEMA_VERSION,
    check_index_compatibility,
)
from knowledge_agent.domain.errors import IndexIncompatible
from knowledge_agent.text.normalize import NORMALIZATION_VERSION


def _identity(**overrides):
    base = {
        "model": "embeddinggemma:300m",
        "digest": "abc",
        "dimension": 768,
        "normalization": "l2",
        "normalization_version": NORMALIZATION_VERSION,
        "dtype": "float32",
        "document_prefix": "",
        "query_prefix": "q: ",
        "corpus_schema_version": CORPUS_SCHEMA_VERSION,
    }
    base.update(overrides)
    return base


def test_matching_identity_passes():
    check_index_compatibility(_identity(), _identity())


@pytest.mark.parametrize("field", COMPATIBILITY_FIELDS)
def test_each_field_mismatch_raises_with_details(field):
    expected = _identity()
    actual = _identity(**{field: "changed" if field != "dimension" else 1024})
    with pytest.raises(IndexIncompatible) as error:
        check_index_compatibility(expected, actual)
    details = error.value.details
    assert field in details["expected"]
    assert field in details["actual"]
    assert error.value.code == "index_incompatible"
    assert error.value.http_status == 409
