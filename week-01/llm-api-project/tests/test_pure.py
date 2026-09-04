import unittest

from days.day2 import validate_recipe
from days.day3 import OPERATIONS, calculate_optimum, parse_final_answer, validate_answer
from days.day4 import count_words


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


if __name__ == "__main__":
    unittest.main()
