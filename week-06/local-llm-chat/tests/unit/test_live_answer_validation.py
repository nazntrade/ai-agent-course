from harness.d26_live import valid_city_json, _is_json_object


def test_valid_json_has_exact_fields_types_and_requested_city():
    assert valid_city_json('{"city":"Paris","country":"France","population_millions":2.1}')[0]
    for text in (
        '{"city":"London","country":"France","population_millions":2.1}',
        '{"city":"Paris","country":"France","population_millions":true}',
        '{"city":"Paris","country":"France","population_millions":"2.1"}',
        '{"city":"Paris","country":"France","population_millions":2.1,"extra":0}',
    ):
        assert not valid_city_json(text)[0]


def test_fenced_or_repaired_json_is_not_a_valid_raw_json_answer():
    assert not _is_json_object('```json\n{"city":"Paris"}\n```')[0]
    assert not _is_json_object('{"city":"Paris",}')[0]
