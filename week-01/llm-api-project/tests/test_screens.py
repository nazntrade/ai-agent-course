import os
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"

DAY2_LABEL = "День 2 — Формат ответа"
DAY3_LABEL = "День 3 — Способы рассуждения"
DAY4_LABEL = "День 4 — Температура"
DAY5_LABEL = "День 5 — Версии моделей"

NO_KEY_ERROR = "Добавьте DEEPSEEK_API_KEY в файл .env"


class ScreensTest(unittest.TestCase):
    def setUp(self):
        # Гарантированно убираем ключи, чтобы ни один тест не выполнил реальный
        # API-запрос. load_dotenv() уже отработал при импорте пакета days.
        os.environ.pop("DEEPSEEK_API_KEY", None)
        os.environ.pop("LOCAL_LLM_API_KEY", None)

    def test_startup_renders_last_day(self):
        at = AppTest.from_file(str(APP), default_timeout=30).run()
        self.assertFalse(at.exception)

        day_selector = at.radio(key="day_selector")
        self.assertEqual(day_selector.value, DAY5_LABEL)
        self.assertEqual(
            day_selector.options, [DAY2_LABEL, DAY3_LABEL, DAY4_LABEL, DAY5_LABEL]
        )

        subheaders = [subheader.value for subheader in at.subheader]
        self.assertIn("Версии моделей", subheaders)
        self.assertEqual(at.button[0].label, "Сравнить модели")

    def test_switch_to_day3(self):
        at = AppTest.from_file(str(APP), default_timeout=30).run()
        at = at.radio(key="day_selector").set_value(DAY3_LABEL).run()
        self.assertFalse(at.exception)

        subheaders = [subheader.value for subheader in at.subheader]
        self.assertIn("Космическая задача", subheaders)
        self.assertEqual(at.button[0].label, "Запустить сравнение")

    def test_switch_to_day4(self):
        at = AppTest.from_file(str(APP), default_timeout=30).run()
        at = at.radio(key="day_selector").set_value(DAY4_LABEL).run()
        self.assertFalse(at.exception)
        self.assertEqual(at.button[0].label, "Сравнить температуры")

    def test_switch_to_day5(self):
        at = AppTest.from_file(str(APP), default_timeout=30).run()
        at = at.radio(key="day_selector").set_value(DAY5_LABEL).run()
        self.assertFalse(at.exception)
        self.assertEqual(at.button[0].label, "Сравнить модели")

    def test_day5_no_keys_click_shows_messages(self):
        at = AppTest.from_file(str(APP), default_timeout=30).run()
        at = at.radio(key="day_selector").set_value(DAY5_LABEL).run()
        at = at.button[0].click().run()
        self.assertFalse(at.exception)

        errors = [error.value for error in at.error]
        self.assertTrue(any("DEEPSEEK_API_KEY" in error for error in errors))
        self.assertTrue(any("LOCAL_LLM_API_KEY" in error for error in errors))

    def test_no_api_key_click_shows_error(self):
        at = AppTest.from_file(str(APP), default_timeout=30).run()
        at = at.radio(key="day_selector").set_value(DAY2_LABEL).run()
        # Единственный допустимый клик по API-кнопке: при отсутствии ключа он не
        # делает реального запроса, а только показывает сообщение об ошибке.
        at = at.button[0].click().run()
        self.assertFalse(at.exception)

        errors = [error.value for error in at.error]
        self.assertIn(NO_KEY_ERROR, errors)


if __name__ == "__main__":
    unittest.main()
