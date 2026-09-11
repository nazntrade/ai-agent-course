import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import common
from days.day2 import validate_recipe
from days.day3 import OPERATIONS, calculate_optimum, parse_final_answer, validate_answer
from days.day4 import count_words
from days.day5 import (
    DAY5_MODELS,
    FLASH_COST,
    calculate_cost,
    compute_tokens_per_second,
    is_peak_time,
    parse_answer,
    parse_timings,
    parse_usage,
    validate_answer as validate_day5_answer,
)


class ValidateRecipeTest(unittest.TestCase):
    def test_valid_recipe_returns_none(self):
        data = {
            "title": "Завтрак",
            "ingredients": [{"name": "Яйца", "amount": "2 шт"}],
            "steps": [{"order": 1, "text": "Сварить яйца"}],
        }
        self.assertIsNone(validate_recipe(data))

    def test_non_dict(self):
        self.assertEqual(validate_recipe("не объект"), "корень должен быть объектом")

    def test_missing_title(self):
        self.assertEqual(
            validate_recipe({"ingredients": [], "steps": []}),
            "поле 'title' должно быть строкой",
        )

    def test_ingredients_not_list(self):
        self.assertEqual(
            validate_recipe({"title": "x"}),
            "поле 'ingredients' должно быть списком",
        )

    def test_ingredient_name_not_string(self):
        self.assertEqual(
            validate_recipe(
                {"title": "x", "ingredients": [{"name": 1, "amount": "2"}], "steps": []}
            ),
            "в 'ingredients' поле 'name' должно быть строкой",
        )

    def test_ingredient_amount_not_string(self):
        self.assertEqual(
            validate_recipe(
                {"title": "x", "ingredients": [{"name": "n", "amount": 2}], "steps": []}
            ),
            "в 'ingredients' поле 'amount' должно быть строкой",
        )

    def test_steps_not_list(self):
        self.assertEqual(
            validate_recipe({"title": "x", "ingredients": []}),
            "поле 'steps' должно быть списком",
        )

    def test_step_order_not_int(self):
        self.assertEqual(
            validate_recipe(
                {"title": "x", "ingredients": [], "steps": [{"order": "1", "text": "t"}]}
            ),
            "в 'steps' поле 'order' должно быть целым числом",
        )

    def test_step_order_bool(self):
        self.assertEqual(
            validate_recipe(
                {"title": "x", "ingredients": [], "steps": [{"order": True, "text": "t"}]}
            ),
            "в 'steps' поле 'order' должно быть целым числом",
        )

    def test_step_text_not_string(self):
        self.assertEqual(
            validate_recipe(
                {"title": "x", "ingredients": [], "steps": [{"order": 1, "text": 5}]}
            ),
            "в 'steps' поле 'text' должно быть строкой",
        )


class CalculateOptimumTest(unittest.TestCase):
    def test_returns_optimum_set_and_value(self):
        ops, value = calculate_optimum()
        self.assertEqual(ops, frozenset({"A", "C", "F"}))
        self.assertEqual(value, 32)

    def test_solution_respects_constraints(self):
        ops, value = calculate_optimum()
        selected = [op for op in OPERATIONS if op["name"] in ops]
        energy = sum(op["energy"] for op in selected)
        work_time = sum(op["time"] for op in selected)
        self.assertLessEqual(energy, 14)
        self.assertLessEqual(work_time, 10)
        for op in selected:
            if op["requires"]:
                self.assertIn(op["requires"], ops)
            if op["conflicts"]:
                self.assertNotIn(op["conflicts"], ops)


class ParseFinalAnswerTest(unittest.TestCase):
    def test_upper_final(self):
        self.assertEqual(
            parse_final_answer("FINAL: A, C, F = 32"),
            (frozenset({"A", "C", "F"}), 32),
        )

    def test_lower_final(self):
        self.assertEqual(
            parse_final_answer("final: A, C, F = 32"),
            (frozenset({"A", "C", "F"}), 32),
        )

    def test_empty_text(self):
        self.assertIsNone(parse_final_answer(""))

    def test_no_equals(self):
        self.assertIsNone(parse_final_answer("FINAL: A C F 32"))

    def test_no_letters(self):
        self.assertIsNone(parse_final_answer("FINAL: = 32"))

    def test_letters_outside_range(self):
        self.assertIsNone(parse_final_answer("FINAL: A, G = 32"))

    def test_last_non_empty_line_not_final(self):
        self.assertIsNone(parse_final_answer("просто текст\nбез финальной строки"))


class ValidateAnswerTest(unittest.TestCase):
    def test_correct(self):
        ok, reason = validate_answer("FINAL: A, C, F = 32")
        self.assertTrue(ok)
        self.assertEqual(reason, "совпадает с оптимальным решением")

    def test_wrong_set(self):
        ok, reason = validate_answer("FINAL: A, B = 32")
        self.assertFalse(ok)
        self.assertEqual(reason, "набор операций не совпадает с оптимальным")

    def test_wrong_points(self):
        ok, reason = validate_answer("FINAL: A, C, F = 31")
        self.assertFalse(ok)
        self.assertEqual(reason, "баллы не совпадают с оптимальными")


class CountWordsTest(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(count_words(""), 0)

    def test_none(self):
        self.assertEqual(count_words(None), 0)

    def test_three_words(self):
        self.assertEqual(count_words("a b c"), 3)


class IsPeakTimeTest(unittest.TestCase):
    # 2026-09-07 — понедельник; 2026-09-12 — суббота.
    def test_monday_0100_utc_true(self):
        self.assertTrue(is_peak_time(datetime(2026, 9, 7, 1, 0, tzinfo=timezone.utc)))

    def test_monday_0359_utc_true(self):
        self.assertTrue(is_peak_time(datetime(2026, 9, 7, 3, 59, tzinfo=timezone.utc)))

    def test_monday_0400_utc_false(self):
        self.assertFalse(is_peak_time(datetime(2026, 9, 7, 4, 0, tzinfo=timezone.utc)))

    def test_monday_0600_utc_true(self):
        self.assertTrue(is_peak_time(datetime(2026, 9, 7, 6, 0, tzinfo=timezone.utc)))

    def test_monday_0959_utc_true(self):
        self.assertTrue(is_peak_time(datetime(2026, 9, 7, 9, 59, tzinfo=timezone.utc)))

    def test_monday_1000_utc_false(self):
        self.assertFalse(is_peak_time(datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)))

    def test_monday_0000_utc_false(self):
        self.assertFalse(is_peak_time(datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)))

    def test_monday_0500_utc_false(self):
        self.assertFalse(is_peak_time(datetime(2026, 9, 7, 5, 0, tzinfo=timezone.utc)))

    def test_saturday_0200_utc_false(self):
        self.assertFalse(is_peak_time(datetime(2026, 9, 12, 2, 0, tzinfo=timezone.utc)))


class CalculateCostTest(unittest.TestCase):
    def test_flash_off_peak(self):
        usage = {"prompt_tokens": 1000, "completion_tokens": 500}
        cost = calculate_cost(usage, FLASH_COST, False)
        self.assertAlmostEqual(cost["input_usd"], 1000 * 0.22 / 1e6)
        self.assertAlmostEqual(cost["output_usd"], 500 * 0.66 / 1e6)
        self.assertAlmostEqual(cost["total_usd"], (1000 * 0.22 + 500 * 0.66) / 1e6)

    def test_flash_peak(self):
        usage = {"prompt_tokens": 1000, "completion_tokens": 500}
        cost = calculate_cost(usage, FLASH_COST, True)
        self.assertAlmostEqual(cost["input_usd"], 1000 * 0.44 / 1e6)
        self.assertAlmostEqual(cost["output_usd"], 500 * 1.32 / 1e6)
        self.assertAlmostEqual(cost["total_usd"], (1000 * 0.44 + 500 * 1.32) / 1e6)

    def test_usage_none_returns_none(self):
        self.assertIsNone(calculate_cost(None, FLASH_COST, False))

    def test_zero_tokens_returns_zero(self):
        cost = calculate_cost(
            {"prompt_tokens": 0, "completion_tokens": 0}, FLASH_COST, False
        )
        self.assertAlmostEqual(cost["total_usd"], 0.0)

    def test_cache_split(self):
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "prompt_cache_hit_tokens": 800,
            "prompt_cache_miss_tokens": 200,
        }
        cost = calculate_cost(usage, FLASH_COST, False)
        self.assertAlmostEqual(cost["input_usd"], (200 * 0.22 + 800 * 0.007) / 1e6)
        self.assertEqual(
            cost["assumption"], "учтена разбивка cache hit/miss из usage"
        )


class ParseUsageTest(unittest.TestCase):
    def test_object_fields(self):
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        self.assertEqual(
            parse_usage(usage),
            {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )

    def test_dict_fields(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        self.assertEqual(
            parse_usage(usage),
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    def test_cached_tokens_details(self):
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            prompt_tokens_details=SimpleNamespace(cached_tokens=30),
        )
        parsed = parse_usage(usage)
        self.assertEqual(parsed["prompt_cache_hit_tokens"], 30)
        self.assertEqual(parsed["prompt_cache_miss_tokens"], 70)

    def test_empty_object_returns_none(self):
        self.assertIsNone(parse_usage(SimpleNamespace()))

    def test_none_returns_none(self):
        self.assertIsNone(parse_usage(None))


class ParseTimingsTest(unittest.TestCase):
    def test_dict_fields(self):
        timings = {
            "prompt_n": 10,
            "predicted_n": 5,
            "predicted_per_second": 20.0,
            "prompt_per_second": 100.0,
        }
        self.assertEqual(
            parse_timings(timings),
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "predicted_per_second": 20.0,
                "prompt_per_second": 100.0,
            },
        )

    def test_object_fields(self):
        timings = SimpleNamespace(
            prompt_n=10,
            predicted_n=5,
            predicted_per_second=20.0,
            prompt_per_second=100.0,
        )
        self.assertEqual(
            parse_timings(timings),
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "predicted_per_second": 20.0,
                "prompt_per_second": 100.0,
            },
        )

    def test_none_returns_none(self):
        self.assertIsNone(parse_timings(None))

    def test_empty_dict_returns_none(self):
        self.assertIsNone(parse_timings({}))

    def test_empty_object_returns_none(self):
        self.assertIsNone(parse_timings(SimpleNamespace()))

    def test_only_predicted_n(self):
        self.assertEqual(parse_timings({"predicted_n": 5}), {"completion_tokens": 5})


class TokensPerSecondTest(unittest.TestCase):
    def test_normal(self):
        usage = {"completion_tokens": 100}
        self.assertAlmostEqual(compute_tokens_per_second(usage, 1.0, 5.0), 100 / 4.0)

    def test_without_ttft(self):
        usage = {"completion_tokens": 100}
        self.assertAlmostEqual(compute_tokens_per_second(usage, None, 5.0), 20.0)

    def test_without_usage(self):
        self.assertIsNone(compute_tokens_per_second(None, 1.0, 5.0))

    def test_completion_zero(self):
        usage = {"completion_tokens": 0}
        self.assertEqual(compute_tokens_per_second(usage, 1.0, 5.0), 0.0)

    def test_elapsed_leq_ttft(self):
        usage = {"completion_tokens": 100}
        self.assertIsNone(compute_tokens_per_second(usage, 5.0, 5.0))
        self.assertIsNone(compute_tokens_per_second(usage, 5.0, 4.0))


class ParseAnswerTest(unittest.TestCase):
    def test_upper(self):
        self.assertEqual(parse_answer("ANSWER: 23"), 23.0)

    def test_lower(self):
        self.assertEqual(parse_answer("answer: 23"), 23.0)

    def test_negative_decimal(self):
        self.assertEqual(parse_answer("ANSWER: -23.5"), -23.5)

    def test_last_non_empty_line(self):
        self.assertEqual(parse_answer("рассуждения...\n\nANSWER: 23\n"), 23.0)

    def test_no_answer(self):
        self.assertIsNone(parse_answer("просто текст без ответа"))

    def test_empty(self):
        self.assertIsNone(parse_answer(""))

    def test_trailing_garbage(self):
        self.assertIsNone(parse_answer("ANSWER: 23 extra"))

    def test_ignores_earlier_answer_line(self):
        self.assertIsNone(parse_answer("ANSWER: 23\nпоследняя строка не в формате"))


class ValidateDay5AnswerTest(unittest.TestCase):
    def test_correct(self):
        ok, reason = validate_day5_answer("ANSWER: 23")
        self.assertTrue(ok)
        self.assertEqual(reason, "ответ верный")

    def test_wrong_number(self):
        ok, reason = validate_day5_answer("ANSWER: 22")
        self.assertFalse(ok)
        self.assertEqual(reason, "число не совпадает с верным ответом (ожидалось 23)")

    def test_no_answer(self):
        ok, reason = validate_day5_answer("нет ответа")
        self.assertFalse(ok)
        self.assertEqual(reason, "нет строки ANSWER в корректном формате")


class ModelIdentifierTest(unittest.TestCase):
    def test_common_model_is_canonical(self):
        self.assertEqual(common.MODEL, "deepseek-flash")

    def test_day5_flash_spec_is_canonical(self):
        flash = next(spec for spec in DAY5_MODELS if spec["key"] == "deepseek_flash")
        self.assertEqual(flash["model"], "deepseek-flash")
        self.assertEqual(flash["label"], "DeepSeek V4.1 Flash (средняя)")
        self.assertEqual(flash["official_name"], "DeepSeek-V4.1-Flash")

    def test_day5_pro_spec_unchanged(self):
        pro = next(spec for spec in DAY5_MODELS if spec["key"] == "deepseek_pro")
        self.assertEqual(pro["model"], "deepseek-v4-pro")
        self.assertEqual(pro["label"], "DeepSeek V4 Pro (сильная)")
        self.assertEqual(pro["official_name"], "DeepSeek-V4-Pro-0813")


if __name__ == "__main__":
    unittest.main()
