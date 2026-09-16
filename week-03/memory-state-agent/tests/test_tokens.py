import unittest

from tokens import estimate_tokens


class EstimateTokensTest(unittest.TestCase):
    def test_empty_returns_zero(self):
        self.assertEqual(estimate_tokens(""), 0)

    def test_non_empty_returns_at_least_one(self):
        for text in ("a", " ", "!", "Привет"):
            self.assertGreaterEqual(estimate_tokens(text), 1)

    def test_deterministic(self):
        texts = ["Привет, мир!", "hello world 123", "смешанный text with latin"]
        for text in texts:
            self.assertEqual(estimate_tokens(text), estimate_tokens(text))

    def test_monotonic(self):
        base = "Пример текста для проверки монотонности"
        self.assertLessEqual(
            estimate_tokens(base), estimate_tokens(base + " дополнение")
        )
        for text in ("a", "абв", "hello", "кириллица"):
            self.assertLessEqual(estimate_tokens(text), estimate_tokens(text + "x"))

    def test_cyrillic_counts_more_than_latin(self):
        # Four Cyrillic letters weigh 4/2 = 2; four Latin letters weigh 4/4 = 1.
        self.assertGreater(estimate_tokens("аааа"), estimate_tokens("aaaa"))

    def test_returns_int(self):
        self.assertIsInstance(estimate_tokens("какой-то текст"), int)


if __name__ == "__main__":
    unittest.main()
