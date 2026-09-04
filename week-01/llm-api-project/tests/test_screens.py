import os
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"

DAY2_LABEL = "День 2 — Формат ответа"
DAY3_LABEL = "День 3 — Способы рассуждения"
DAY4_LABEL = "День 4 — Температура"

NO_KEY_ERROR = "Добавьте DEEPSEEK_API_KEY в файл .env"


class ScreensTest(unittest.TestCase):
    def setUp(self):
        # Гарантированно убираем ключ, чтобы ни один тест не выполнил реальный
        # API-запрос. load_dotenv() уже отработал при импорте пакета days.
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_startup_renders_day2(self):
        at = AppTest.from_file(str(APP), default_timeout=30).run()
        self.assertFalse(at.exception)

        day_selector = at.radio(key="day_selector")
        self.assertEqual(day_selector.options, [DAY2_LABEL, DAY3_LABEL, DAY4_LABEL])

        subheaders = [subheader.value for subheader in at.subheader]
        self.assertIn("Свободный режим", subheaders)
        self.assertIn("Контролируемый режим (JSON)", subheaders)
        self.assertEqual(at.button[0].label, "Отправить")

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

    def test_no_api_key_click_shows_error(self):
        at = AppTest.from_file(str(APP), default_timeout=30).run()
        # Единственный допустимый клик по API-кнопке: при отсутствии ключа он не
        # делает реального запроса, а только показывает сообщение об ошибке.
        at = at.button[0].click().run()
        self.assertFalse(at.exception)

        errors = [error.value for error in at.error]
        self.assertIn(NO_KEY_ERROR, errors)


if __name__ == "__main__":
    unittest.main()
