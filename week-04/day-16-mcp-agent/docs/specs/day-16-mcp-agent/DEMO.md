# DEMO — Day 16: локальный MCP-агент

## 1. Установка

```
cd week-04\day-16-mcp-agent
setup.bat
```

Скрипт создаёт `.venv`, ставит зависимости из `requirements.txt` и напоминает
про `.env.example`. Файл `.env` не обязателен: без него используются значения по
умолчанию (loopback MCP `127.0.0.1:8765`, модель `127.0.0.1:8080/v1`).

## 2. Запуск

```
run_app.bat
```

Режимы:

* `run_app.bat all` (по умолчанию) — MCP-сервер + backend + UI в браузере;
* `run_app.bat mcp` — только MCP-сервер в текущем окне;
* `run_app.bat backend` — только backend;
* `run_app.bat tools` — discovery CLI по MCP.

Порты: MCP `127.0.0.1:8765`, backend `127.0.0.1:8600`. Если MCP уже запущен,
`run_app.bat all` его не перезапускает. По завершении останавливаются только
процессы, запущенные самим скриптом.

В окне браузера: поле ввода, кнопка `Send`, Enter — отправка, Shift+Enter —
новая строка. Справа — блок `MCP status` (connected/disconnected, protocol
version, tools count, раскрываемый список tools).

## 3. Проверки

```
SETUP:        setup.bat
UNIT:         test.bat                       (unit + in-process MCP)
MCP SMOKE:    smoke_test.bat                 (реальный MCP + backend + CLI)
LIVE LLM E2E: test.bat live                  (нужен запущенный локальный Qwen)
LIVE + UI:    test.bat live ui               (плюс Playwright + системный браузер)
ACCEPTANCE:   test.bat acceptance            (unit → live MCP → live E2E)
OPENAPI:      test.bat openapi               (обновить docs/openapi.json)
```

Артефакты каждого прогона: `.runs/<timestamp>-<label>/` — `trace.jsonl`,
`mcp_server.log`, `backend.log`, `report.json`, `screenshots/`.

## 4. Локальная модель

Ожидается OpenAI-compatible endpoint на `http://127.0.0.1:8080/v1`
(по умолчанию модель `qwen3.8-27b-local`). Если сервер уже работает, harness его
использует и не перезапускает; если нет — пытается поднять его через
существующий механизм репозитория (`qa/lib/local_llm.py`, `qa/lib/discovery.py`),
а при невозможности печатает `LIVE_LLM_STATUS: BLOCKED` с точной причиной.
Процесс, который запустил harness, он же и останавливает.

## 5. Ручная проверка UI (если Playwright/браузер недоступны)

1. `run_app.bat` и дождаться открытия страницы.
2. Убедиться, что блок `MCP status` показывает `connected` и `Tools (2)`.
3. Ввести `What is 23 multiplied by 17?` и нажать Enter.
4. Ожидаемо: сразу появляется loader, затем потоково текст с финальным ответом
   `391`. Вызов `MCP tool call: calculate …` и строка результата лежат в
   раскрывающемся блоке `Technical details` под ответом (закрыт по умолчанию).
5. Остановить MCP-сервер и повторить запрос: ожидается понятная ошибка про MCP,
   а не «успешный» ответ.
6. Нажать `Refresh` в блоке MCP status: ожидается `disconnected` и категория ошибки.

## 6. Известные ограничения

* Внешняя облачная модель как runtime не используется.
* Хранение истории — только в памяти backend; перезапуск процесса очищает сессии.
* MCP tools read-only: файлов, shell и Git у сервера нет.
