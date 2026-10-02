# ACCEPTANCE — Day 24: цитаты, источники и защита от галлюцинаций

Идентификатор задачи: `day-24-rag-grounding`.
Статус: **критерии актуализированы по реализованным исправлениям D24**; итоговые PASS/FAIL и ограничения подтверждаются независимыми evidence.
Дата: 2026-10-02.
Связанные документы: `SPEC.md` (требования R-01…R-20), `PLAN.md` (шаги P-01…P-16).

Каждый критерий D24-NN трассируется к требованию `R-NN` (SPEC §18), к шагу `P-NN`
(PLAN §2) и к замороженному acceptance-contract `C-NN` (D24-NN ↔ соответствующие C-NN).

---

## 1. Легенда уровней и правило «mock/stub ≠ LIVE»

Каноническая шкала уровней: **UNIT / INT / LIVE / MANUAL / UI**.

| Уровень | Что подтверждает | Изоляция |
| --- | --- | --- |
| **UNIT** | Логика ядра на моках/fake, без сети и `.env` | `test.bat unit` |
| **INT** | Реальные процессы сервиса + реальный HTTP loopback + `harness/embed_stub.py` и `harness/chat_stub.py` (без внешней сети) | `test.bat integration`, `smoke_test.bat` |
| **LIVE** | Реальный retrieval через pull-embedding и grounded-генерация через выбранный полный chat-профиль | `test.bat scenario d24-rag-grounding`; `MODEL_CHECK_KIND: NETWORK` для remote, `LOCAL` для local |
| **UI** | Фактическая проверка интерфейса в браузере | браузер через `run_app.bat` |
| **MANUAL** | Ручной прогон Developer/Tester | вручную |

**Правило:** mock/stub/fake **никогда** не маркируется как inference и не выдаётся за LIVE.
Unit-тест с fake/stub подтверждает логику, но не реальный контракт внешнего провайдера и не
точность цитат на реальном корпусе.

**Провайдерская граница** (grounded-JSON, `finish_reason`/`usage`, streaming, лимиты):
Developer моделирует реальные граничные ответы (пустой ответ, обрыв `length`, ошибка формата,
отсутствие `usage`, ошибку/таймаут провайдера) и закрепляет их постоянными regression-тестами;
критерий считается проверенным на достаточном уровне только после LIVE-проверки внешней
границы Tester-ом на выбранном реальном chat-провайдере.

Реальный выбранный профиль D24 — **remote** (`deepseek/deepseek-flash`, `AI_TEST_LIVE_POLICY:
allowed`), поэтому LIVE маркируется `MODEL_CHECK_KIND: NETWORK`, а LOCAL-статусы не
выставляются. При `forbidden` реальные вызовы запрещены: выполняется offline-часть, LIVE
фиксируется как BLOCKED/PARTIAL, mock не выдаётся за PASS.

---

## 2. Таблица критериев D24-01…D24-20

| ID (D24-NN) | Контракт | R (SPEC §18) | P (PLAN §2) | Критерий | Компонент | Уровень | Способ проверки | Порог/наблюдение |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D24-01 | C01, C02 | R-01 | P-01, P-02, P-06, P-07 | RAG-ответ показывает текст, источники (`source`, `section`, `chunk_id`) и точные цитаты; источниками считаются только `passed`-фрагменты | `ChatService`, `answer.grounding`, `retrieval.passed` | UNIT + INT + LIVE | UNIT/INT: сравнение `sources` с `passed`, не с `candidates`; LIVE-сценарий и `grounding-results.json` | каждый источник ∈ `passed_chunk_ids`; `sources` не содержат кандидатов; цитаты привязаны к `passed`; `grounding-results.json.results ≥ 10` |
| D24-02 | C03 | R-02 | P-02, P-06, P-12 | Сохранены индексирование D21, без-RAG D22, rewrite/фильтрация/настройки и четыре режима D23 | D21/D22/D23 API и сервис, `knowledge_agent/__main__.py`, тесты | UNIT + INT | `test.bat unit`, `test.bat integration`, `smoke_test.bat` | D21–D23 тесты зелёные; `/api/search`, `/api/index/*`, `/api/compare` не изменены; `POST /api/chat/compare` остаётся `rag-v1`/`plain-v1` без `answer.grounding`; `/api/chat/compare-modes` сохраняет имена/настройки/трассу (grounded-слой аддитивен); `chunk_id`-фильтр чанков аддитивен; `RAG_GROUNDING_ENABLED=0`/`grounding_enabled=False` восстанавливает D22 `rag-v1`-форму; app-run INT аддитивны: grounded `/api/chat` покрыт `tests/integration/test_d24_grounding_end_to_end.py` при `RAG_GROUNDING_ENABLED=1`, D22/D23 app-run INT (`test_selected_profile_end_to_end.py`, `test_chat_end_to_end.py`, `test_d23_end_to_end.py`) пиннятся `RAG_GROUNDING_ENABLED=0`; `knowledge_agent/__main__.py` пробрасывает `RAG_GROUNDING_ENABLED` в `ChatService(grounding_enabled=...)` |
| D24-03 | C04, C05 | R-03, R-04 | P-01, P-05, P-07 | Модель ссылается только на переданные `chunk_id`; неизвестный не подтверждается; `source`/`section` — из `metadata` индекса | `GroundingVerifier`, `Citation` | UNIT + INT | UNIT/INT с fake/stub: неизвестный `chunk_id`; сверка `source`/`section` с `metadata` | `source_exists=false`, `status="unknown_chunk_id"`; `source`=`source_label`, `section`=`section_path` не из текста модели |
| D24-04 | C06 | R-05 | P-01, P-05 | Цитата реально присутствует во фрагменте; нормализация пробелов описана; пересказ/приблизительное совпадение отклоняются | `normalize_whitespace`, `GroundingVerifier` | UNIT | UNIT: точная цитата; цитата с иным числом/типом пробелов; пересказ; пустая цитата | точная и нормализованно-эквивалентная проходят (`quote_verbatim=true`); пересказ/пустая → `quote_mismatch` |
| D24-05 | C07 | R-06 | P-01, P-05 | Перевод обозначен как перевод, оригинал сохранён | `Citation` | UNIT + inspection | UNIT: цитата с `translation`; inspection структуры | `is_translation=true`, `quote` = оригинал; перевод без оригинала не `verified` |
| D24-06 | C08 | R-07 | P-01, P-05, P-08 | Выдуманная цитата не подменяется; некорректный ответ не отображается как успешно проверенный | `GroundingVerifier`, UI | UNIT + INT + UI | UNIT/INT: выдуманная цитата при настоящем ID; UI: `Verification failed` | цитата `quote_mismatch`; `grounding.status ≠ verified`; подстановки нет; UI показывает ошибку проверки |
| D24-07 | C09 | R-08 | P-01, P-03, P-05 | Различаются три проверки; текст не доказывает смысл; нет обещания смысловой проверки | `GroundingVerifier`, prompt, UI/docs | UNIT + inspection | UNIT: независимые поля; inspection UI/документации | `source_exists`/`quote_verbatim` отдельны; `meaning_supported=null`, `meaning_check="not_performed"`; формальная валидация не выставляет смысловую метку |
| D24-08 | C10 | R-09 | P-03, P-05 | Найденный текст — данные, не команды | `grounded-rag-v1`, регрессия | UNIT + INT | UNIT/INT: документ с инструкцией внутри фрагмента | инструкция не исполняется; контекст в отдельном `<context>`-блоке; шаблон сохраняет предупреждение |
| D24-09 | C11 | R-10 | P-06, P-08 | Ниже порога — честный отказ и полезное уточнение, без общих знаний | `ChatService`, UI | UNIT + INT + UI | UNIT/INT: высокий порог → пустой `selected`; UI: сообщение | `insufficient_sources=true`, `grounding.status="refused"`, `reason="below_threshold"`; модель не вызвана; повторного запроса без фильтра нет |
| D24-10 | C12 | R-11 | P-06, P-08 | Высокий `score` не доказывает факт; отказ или `partial` с `limitation` | `ChatService`, `GroundingResult` | UNIT + LIVE | UNIT: `insufficient=true`/`limitation`; LIVE: высокий score без факта | отказ либо `status="partial"` с непустым `limitation`, не `verified` |
| D24-11 | C13 | R-12 | P-06 | Честный отказ допускает пустые источники/цитаты без выдумывания; сохранены причина и порог | `ChatService`, артефакты | UNIT + inspection | UNIT + inspection `grounding-results.json` | `sources=[]`/`citations=[]` допустимы; `refusal.reason` и `refusal.threshold` непусты; поля не заполнены фиктивно |
| D24-12 | C14 | R-13 | P-02, P-06, P-12 | Смысл режимов D23 не подменяется скрытой фильтрацией | `ChatService`, трасса | UNIT + INT | D23-регрессия четырёх режимов и настроек | режимы A–D, трасса, top-K, порог и rewrite не изменены; скрытой фильтрации нет |
| D24-13 | C15 | R-14 | P-01, P-04, P-06 | Пустой/обрезанный ответ, ошибка формата и ошибка провайдера — технические ошибки, не честный отказ; порядок «формат → truncated» | `parse_grounded_response`, `ChatService` | UNIT + INT (+LIVE границы) | UNIT/INT с fake/stub; LIVE границы провайдера | пустой/формат (в т. ч. обрезанный JSON) → `chat_invalid_response`; `finish_reason=length` при **разобранном** JSON → `truncated=true`, `status="failed"`, `reason="truncated_generation"`; провайдер → `chat_*` 503; ни одно не `insufficient_sources=true`; формат имеет приоритет над truncated |
| D24-14 | C16, C17, C18, C19 | R-15 | P-07, P-08 | UI: чат первым блоком, поля/настройки/кнопки/метрики/индикатор; источники, разделы, `chunk_id`, цитаты; раскрытие фрагмента; перенос строк; раздельные состояния; язык | `ui/*` | UI + MANUAL | `run_app.bat`; inspection UI-кода | чат — первая увеличенная панель; `Sources & citations`, `Show fragment`, перенос строк; `Verification failed` ≠ `No relevant sources found`; новые элементы англ., существующие русские панели не переведены; язык ответа — по вопросу |
| D24-15 | C20 | R-16 | P-08 | UI проверен доступными средствами с честным пределом | `ui/*` | UI + MANUAL | Developer, затем независимо Tester | фактические шаги и наблюдения зафиксированы; предел проверки указан явно (без автоматизации браузера) |
| D24-16 | C21, C22 | R-17 | P-10, P-14 | 10 вопросов (9 по документу + 1 вне корпуса) из существующего корпуса и `eval/d22/questions.json`; эталоны сверены с документом | `eval/d22/questions.json`, harness | MANUAL + inspection | inspection harness и LIVE-сценария; сверка с фрагментами источника | ровно 10 вопросов, 1 `answerable:false`; `expected_facts` не приняты за истину без сверки; расхождения исправлены по источнику |
| D24-17 | C23, C25 | R-18 | P-10 | 10 фактических результатов на одном закреплённом индексе и профиле; старые результаты/форматы сохранены; файл/JSON/ID ≠ оценка | `harness/d24_live.py`, артефакт | LIVE + inspection | `test.bat scenario d24-rag-grounding`; чтение артефакта | `grounding-results.json`: `results ≥ 10`, один `index_version_id`; D22/D23-артефакты не изменены; наличие файла/валидного `chunk_id` не заменяет оценку |
| D24-18 | C24, C26 | R-19 | P-10, P-16 | Tester независимо оценивает каждый результат (источники/цитаты, смысл, полнота, обоснованность отказа); отказ на всё — не улучшение | `quality-assessment.md` | MANUAL | Инспекция `quality-assessment.md` и фактических результатов | оценка по каждому из 10 результатов; 9 документных вопросов с отказами не объявлены улучшением; `meaning` оценён вручную |
| D24-19 | C27 | R-20 | P-09, P-10, P-13 | Проверены восемь граничных случаев | `edge-cases.json`, тесты | UNIT + INT + LIVE | Кейсы в тестах; чтение `edge-cases.json` | `cases ≥ 8` с нужными `case_id`; каждое `observed` фактическое, `status` честный |
| D24-20 | C28, C29, C30 | R-20 | P-10, P-15, P-16 | Точки входа и политика профиля соблюдены; acquire/use/release; offline ≠ LIVE; evidence; README | harness, acceptance, README | LIVE + MANUAL | Inspection политики/сценария; `acceptance-evidence-v2`; README | policy проверена до вызовов; lease освобождён в `finally`; `MODEL_CHECK_KIND` корректен; evidence покрывает C01–C30; README содержит раздел D24 |

### 2.1. Must / Later

* **Must** — D24-01…D24-20 и всё из SPEC §2.1.
* **Later** — SPEC §2.2 и §19: MCP, Wikipedia importer, ANN/векторная БД, ML-reranker,
  отдельная entailment/NLI-модель для автоматической смысловой проверки, OCR,
  многосессионная память, полный многошаговый чат, resume, удаление версий,
  multi-process/Postgres/Docker, автоматическое скачивание моделей.

### 2.2. Трассировка D24→R→P→контракт

| D24 | R | P | Контракт |
| --- | --- | --- | --- |
| D24-01 | R-01 | P-01, P-02, P-06, P-07 | C01, C02 |
| D24-02 | R-02 | P-02, P-06, P-12 | C03 |
| D24-03 | R-03, R-04 | P-01, P-05, P-07 | C04, C05 |
| D24-04 | R-05 | P-01, P-05 | C06 |
| D24-05 | R-06 | P-01, P-05 | C07 |
| D24-06 | R-07 | P-01, P-05, P-08 | C08 |
| D24-07 | R-08 | P-01, P-03, P-05 | C09 |
| D24-08 | R-09 | P-03, P-05 | C10 |
| D24-09 | R-10 | P-06, P-08 | C11 |
| D24-10 | R-11 | P-06, P-08 | C12 |
| D24-11 | R-12 | P-06 | C13 |
| D24-12 | R-13 | P-02, P-06, P-12 | C14 |
| D24-13 | R-14 | P-01, P-04, P-06 | C15 |
| D24-14 | R-15 | P-07, P-08 | C16, C17, C18, C19 |
| D24-15 | R-16 | P-08 | C20 |
| D24-16 | R-17 | P-10, P-14 | C21, C22 |
| D24-17 | R-18 | P-10 | C23, C25 |
| D24-18 | R-19 | P-10, P-16 | C24, C26 |
| D24-19 | R-20 | P-09, P-10, P-13 | C27 |
| D24-20 | R-20 | P-10, P-15, P-16 | C28, C29, C30 |

---

## 3. Сохраняемые артефакты (из замороженного контракта)

| Artifact id | Путь | Формат | Кол-во | Обязательные assertions |
| --- | --- | --- | --- | --- |
| `result-json` | `week-05/knowledge-agent/local-data/d24/grounding-results.json` | json | 1..1 | `/schema_version` type string; `/results` type array; `/results` minItems 10 |
| `edge-json` | `week-05/knowledge-agent/local-data/d24/edge-cases.json` | json | 1..1 | `/schema_version` type string; `/cases` type array; `/cases` minItems 8 |
| `quality-md` | `week-05/knowledge-agent/local-data/d24/quality-assessment.md` | text | 1..1 | — |

Артефакты сохраняются Developer-ом (`grounding-results.json`, `edge-cases.json`) и независимо
Tester-ом (`quality-assessment.md`); сохраняются между прогонами; Tester читает фактическое
содержимое (не только факт существования) и даёт `artifact`-ссылки на каждый совпавший файл.
`local-data/` в Git не попадает — это не освобождает от сохранения и предъявления артефактов.
Старые артефакты D22/D23 и их форматы чтения сохраняются без изменений.

---

## 4. Граничные и ошибочные сценарии

| Сценарий | Ожидаемое поведение | Уровень |
| --- | --- | --- |
| Точная цитата из `passed`-фрагмента | `source_exists=true`, `quote_verbatim=true`, `status="verified"` | UNIT |
| Цитата с иным числом/типом пробелов (в т. ч. NBSP, перенос строки) | проходит по явной нормализации; `quote_verbatim=true` | UNIT |
| Пересказ/приблизительное совпадение | `quote_mismatch`, `quote_verbatim=false`, не `verified` | UNIT |
| Пустая цитата | отклоняется (`quote_mismatch`) | UNIT |
| Неизвестный `chunk_id` | `source_exists=false`, `status="unknown_chunk_id"`, не подтверждается | UNIT + INT |
| Выдуманная цитата при настоящем ID | `quote_mismatch`; текста не подставляется; `status ≠ verified` | UNIT + INT + UI |
| Перевод цитаты | `is_translation=true`, `quote` = оригинал; перевод без оригинала не `verified` | UNIT |
| `use_filter=true`, фильтр отсекает всё | `200`, детерминированный отказ, `reason="below_threshold"`, `threshold` сохранён, модель не вызвана, `sources=[]`/`citations=[]` | UNIT + INT + UI |
| Высокий `score` без нужного факта | отказ либо `status="partial"` с непустым `limitation`, не `verified` | UNIT + LIVE |
| Инструкция внутри документа | текст — данные; инструкция не исполняется; шаблон предупреждает | UNIT + INT |
| Пустой ответ модели | `chat_invalid_response` (503), не отказ | UNIT + INT |
| Ошибка формата grounded-JSON (в т. ч. обрезанный/незавершённый JSON) | `chat_invalid_response` (503) с `details.format="grounded_json"` | UNIT + INT |
| `finish_reason=length`, JSON разобран | `truncated=true`, `status="failed"`, `reason="truncated_generation"`, не `insufficient_sources` | UNIT + INT + LIVE |
| `finish_reason=length`, JSON не разобран | ошибка формата (§6.1); формат имеет приоритет над truncated | UNIT + INT |
| Ошибка/таймаут/недоступность провайдера | `chat_*` 503, не маскируется отказом | UNIT + INT + LIVE |
| `RAG_GROUNDING_ENABLED=0` или `grounding=false` | D22-форма `rag-v1`, `answer.grounding` отсутствует | UNIT + INT |
| `mode=without_rag` | `plain-v1`, нет источников/цитат/`grounding` | UNIT + INT |
| Три проверки | `source_exists`/`quote_verbatim` формальны; `meaning_supported=null`, `meaning_check="not_performed"` | UNIT + inspection |
| Регрессия D21–D23 | существующие UNIT/INT/smoke зелёные; D23-трасса/режимы/настройки сохранены | UNIT + INT |
| Пользовательская БД/индекс | не затронуты; тесты/harness работают в TEMP; артефакты в `local-data/d24` | INT |

---

### 4.1. Обязательные регрессии исправленных дефектов

Уточняют исходные требования источников/цитат и сохранения бюджетной защиты D22/D23;
не заменяют и не ослабляют C01–C30. Постоянный набор —
`tests/unit/test_d24_defect_regressions.py`, запускается через `test.bat unit`.

| Сценарий | Ожидаемое поведение | Уровень |
| --- | --- | --- |
| Короткий RAG.system помещается, фактический GROUNDED_RAG.system + вопрос превышает бюджет | grounded даёт context_overflow (422) до модели; обычный RAG с тем же бюджетом сохраняет прежний путь | UNIT |
| Точная граница бюджета grounded | prompt_tokens_estimated включает фактический grounded system + вопрос; эвристика не выдаётся за точный tokenizer | UNIT |
| Верная структурированная цитата + неизвестная inline-ссылка | не verified; partial/unsupported_citation; ID в inline_unsupported | UNIT |
| Inline-ссылка на passed-чанк без verified структурированной цитаты к нему | не verified; missing_quote, ID в inline_missing_quote; partial при наличии другой verified цитаты, иначе failed | UNIT |
| Нет citations, limitation null, пустой либо непустой | failed/no_citations; limitation не повышает до partial | UNIT |
| Все цитаты верны, limitation состоит из пробелов | verified при отсутствии других нарушений | UNIT |
| Есть подтверждённая цитата и непустое ограничение | partial; смысл ограничения оценивается отдельно | UNIT |
| Те же нарушения через ChatService | сериализованные статусы и диагностические массивы соответствуют verifier; старый D22 путь сохраняется | UNIT |

Примеры успешных/отказных ответов не заменяют эти противоречивые и граничные сценарии.
Runtime grounding-поля обязательны по SPEC §6.3; строгая вложенная OpenAPI response-схема
не объявляется выполненной и не является обязательной в D24.

## 5. Раздельная оценка качества (D24-18)

Оцениваются независимо (0/1/2, как D22 §15.3):

1. **Источники/retrieval** — относятся ли показанные `sources` к `passed`, совпадают ли
   `source`/`section` с `metadata`; нет ли непереданных источников.
2. **Цитаты** — `source_exists`/`quote_verbatim`; отсутствие подмены; переводы помечены.
3. **Смысл** — подтверждают ли цитаты существенные утверждения ответа (вручную; автоматическая
   метка не выставляется).
4. **Полнота** — ожидаемые факты; для `answerable:false` — отсутствие выдумывания.
5. **Обоснованность отказа** — соответствует ли отказ реальному отсутствию ответа;
   сохранены ли `reason` и `threshold`.

Явно фиксируется: `source_exists`/`quote_verbatim` доказывают только формальные проверки и
**не** смысловую правильность. Непустой ответ, допустимый `chunk_id` и успешная команда
сами по себе не доказывают качество. Протокол сохраняется в `quality-assessment.md`
независимо Tester-ом; отказ на всех девяти документных вопросах не объявляется улучшением.

---

## 6. Измерения LIVE

При реальном прогоне измеряются и указываются:

* `input_tokens`/`output_tokens`/`total_tokens` генерации — в плоском `usage`; недоступные —
  `null`/«н/д»;
* `latency_ms.{retrieval,context,chat,total}` (без смешения с rewrite D23);
* `finish_reason` и `truncated`; `grounding_status` и `grounding_reason`;
* `MODEL_CHECK_KIND: NETWORK` для выбранного remote-профиля (`deepseek/deepseek-flash`);
* для 10 результатов — агрегаты (доля grounded `verified`/`partial`/`refused`/`failed`,
  `unsupported`-цитаты, отказы, обрезания) и записи `grounding-results.json`;
* восемь кейсов `edge-cases.json` с фактическими `observed`.

Токены проверяемой модели не смешиваются с токенами OpenCode-агента; один вызов не
учитывается дважды. Output tokens и tok/s не выдумываются.

---

## 7. Итоговые статусы

* `D24_UNIT_STATUS`
* `D24_INTEGRATION_STATUS`
* `D24_GROUNDING_LIVE_STATUS` (10 результатов)
* `D24_EDGE_CASES_STATUS` (8 кейсов)
* `D24_UI_STATUS`
* `D21_D23_REGRESSION_STATUS`
* `D24_QUALITY_STATUS` (MANUAL; раннер оставляет `NOT_ASSESSED`)
* `TEST_STATUS` (PASS только при проверке всех обязательных критериев на достаточном
  уровне; иначе FAIL/BLOCKED с указанием непроверенного)

Правило: если для обязательного критерия (LIVE-граница, UI, 10 результатов, MANUAL-качество)
нет доступа/разрешения, возвращается `BLOCKED` по этому критерию, а не полный PASS.
Технический `TEST_STATUS` не заменяет общую приёмку проекта. Итоговый Tester обязан выдать
`acceptance-evidence-v2` по всем C01–C30.

---

## 8. Регрессия D21–D23

* Существующие `tests/unit` и `tests/integration` D21–D23 остаются зелёными.
* `test.bat unit` и `test.bat integration` проходят без сети и `.env`.
* `smoke_test.bat` D21/D22/D23-сценарии продолжают работать.
* D22 API (`/api/search`, `/api/index/build`, `/api/compare`, `/api/collections`,
  `/api/chat`, `/api/chat/stream`, `/api/chat/compare`, `/api/chat-runs*`) не изменён по
  путям, параметрам и формам. Единственное аддитивное расширение D24 — опциональный
  `chunk_id` у `GET /api/index-versions/{id}/chunks` (SPEC §11.3/§11.4): остальные
  `/api/index-versions/*` не меняются, параметр опционален и сохраняет поведение D21.
  `POST /api/chat/compare` не затрагивается grounded-слоем: `comparison.prompt_templates`
  остаётся `{"with_rag":"rag-v1","without_rag":"plain-v1"}`, ветви без `answer.grounding`.
* D23: фильтр, rewrite, настройки, трасса, четыре режима A–D и раздельные метрики не
  изменены; смысл режимов не подменяется скрытой фильтрацией. `/api/chat/compare-modes`
  сохраняет имена/настройки/трассу; grounded-слой аддитивен, а
  `comparison.prompt_templates.generation` отражает фактический шаблон.
* `RAG_GROUNDING_ENABLED=0` (или `grounding=false`) возвращает плоскую D22 `rag-v1`-форму;
  `usage` и `latency_ms.chat` совместимы с существующими чтениями UI/тестов.
* D21/D22/D23 артефакты и их форматы чтения сохранены; D24 пишет только в `local-data/d24/`.

---

## 9. Чек-лист документации/реализации

- [ ] `SPEC.md`, `PLAN.md`, `ACCEPTANCE.md` прошли spec review (до реализации).
- [ ] `answer.grounding` аддитивен; `RAG_GROUNDING_ENABLED=0` восстанавливает D22-форму.
- [ ] Источники = `passed`; `source`/`section` из `metadata`.
- [ ] `GroundingVerifier`: `source_exists`, `quote_verbatim`, перевод/оригинал, запрет подмены.
- [ ] Нормализация пробелов реализована одной функцией и описана явно.
- [ ] `meaning_supported=null`/`meaning_check="not_performed"`; UI/docs не выдают формальную
      проверку за смысловую.
- [ ] `grounded-rag-v1` сохраняет защиту от инструкций внутри документов.
- [ ] Честный отказ (`below_threshold`/`model_insufficient`) отделён от технических ошибок.
- [ ] `local-data/d24/{grounding-results.json,edge-cases.json}` сохранены Developer-ом и
      предъявлены Tester-у; `quality-assessment.md` сохранён независимо Tester-ом.
- [ ] LIVE: 10 вопросов (9+1), один закреплённый индекс, один профиль; `MODEL_CHECK_KIND:
      NETWORK`; lease acquire/use/release.
- [ ] Восемь граничных кейсов с фактическими наблюдениями.
- [ ] UI: чат первым блоком, источники/цитаты, `Show fragment`, перенос строк, раздельные
      состояния; новые элементы — англ., существующие русские панели не переведены.
- [ ] Честно отмечено, что не проверено только чтением (реальный grounded-JSON, точность
      цитат на корпусе, смысловая поддержка, UI, `.bat`).
- [ ] README обновлён разделом Day 24 (Coordinator).

---

## 10. Открытые вопросы

* Конкретный вид grounded-JSON зависит от выбранной модели; при ошибке формата возвращается
  честная техническая ошибка, а не «успешный» ответ. Формат и `MAX_OUTPUT_TOKENS` могут быть
  уточнены на реализации по фактическому провайдеру.
* Смысловая проверка остаётся ручной (Tester); автоматическая entailment-модель — Later.
* Порог и качество retrieval привязаны к embedding-модели и корпусу; перенос порога без
  перекалибровки не предполагается.
* Выбранный remote-профиль и его доступность проверяются LIVE-сценарием; базовые UNIT/INT
  остаются offline.
