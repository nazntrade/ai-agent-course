# LAN, HTTPS и восстановление

Loopback является исходным режимом. Network mode включает сетевой bind только gateway; model proxy и административная панель остаются loopback. Android LAN-вариант разрешает HTTP только проверенному локальному URL. HTTPS-вариант запрещает cleartext и проверяет обычную цепочку сертификата.

Gateway стартует только после явного ручного запуска. Открытие панели или перезапуск компьютера сами по себе его не запускают. Задачи планировщика, Windows service и startup hooks этим модулем не создаются.

Вручную вызовите `run_app.bat network` при уже работающем AI-SERVER. Если launcher не получил полный разрешённый model profile, необходим заранее явно выбранный `.runtime/center-test-selection.json` схемы `center-test-selection-v1`: allowed policy, selected model ID и точный loopback Center origin. Файл не содержит credentials и исключён из Git. Отсутствующий selection, partial profile и forbidden/неизвестный policy не запускают сервис.

Launcher обращается только к `private-llm-service`, получает его run ID и ждёт health readiness. Provider credentials остаются в дочерней среде Центра; никакой самостоятельный model manager не создаётся. При Ctrl+C stop защищён expected run ID и не затрагивает чужой запуск. Если профиль уже работает, launcher сообщает об этом и возвращается, сохраняя существующий процесс. Центр должен быть запущен пользователем заранее; launcher не запускает его автоматически. Без подтверждённой ownership после ошибки транспорта cleanup отмечается как unconfirmed, а чужие процессы сохраняются.

Для домашней сети разрешите входящие подключения только к порту gateway в нужном private network profile и задайте в клиенте `http://<gateway-lan-host>:<gateway-port>`. Предпочтительна адресная область домашней сети; administrative/model endpoints не публикуются. Из эмулятора адрес хоста зависит от его NAT, а не является внешним интернет-адресом.

Внешний маршрут настраивается отдельно через reverse proxy или согласованную частную сеть. Пример шаблона Caddy; синтаксис proxy описан в [официальной документации](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy):

```text
chat.example.invalid {
    reverse_proxy 127.0.0.1:<gateway-port>
}
```

Нужны собственный домен/доступный маршрут и настоящий запрос из другой сети. Пример не доказывает настройки сертификата или внешнего доступа. Не отключайте проверку TLS, не включайте redirects с передачей token и не публикуйте model proxy/control API.

Если сервис вернул 429, дождитесь Retry-After и повторите прежний idempotency key с прежним payload. Изменённый payload требует явно нового пользовательского сообщения. При обрыве соединения сначала выполните lookup по key или восстановите snapshot; второй POST с тем же key не создаёт новое сообщение.

При слишком большом новом prompt принятое задание завершится failed `context_too_long` до inference. Старые сообщения сохраняются. При длинном ответе finish reason length отмечает output limit. Частичный failed/cancelled output показывается отдельно и исключается из следующего prompt.

Перед резервной копией штатно остановите сервис и используйте SQLite backup либо скопируйте остановленную БД вместе с учётом WAL. Не включайте keys и pairing payload в архив исходников. Восстановленный сервис не продолжает старые незавершённые jobs и не генерирует повторный ответ автоматически.
