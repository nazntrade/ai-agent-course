# Amendment — явная первоначальная настройка обычного LAN APK

Класс: PRODUCT. Стадия: SPECIFICATION.
Область: target selection и первоначальный pairing.
C01–C16 неизменны. Это отдельное дополнение; исходные SPEC, PLAN, integration amendment и acceptance JSON не изменяются.

## 1. Два фиксированных target scopes

Gradle property `privatechat.testTarget` принимает только:
- e2e — default; testBuildType=e2e;
- normal — только явно; testBuildType=debug.

Неизвестное значение завершает сборку ошибкой. Property не принимает package name, runner class или произвольный build type.

Fixed mappings:
- E2E: main com.example.privatechat.e2e, test com.example.privatechat.e2e.test;
- NORMAL: main com.example.privatechat, test com.example.privatechat.test.

Оба используют тот же main source и соответствующий same-certificate androidTest APK. Main APK не содержит instrumentation declaration, test listener или test-only pairing API.

NORMAL LAN artifacts:
- app-lan-debug.apk;
- app-lan-debug-androidTest.apk, compiled targetPackage=com.example.privatechat.

E2E paths/target остаются прежними. Identity validators получают typed scope и проверяют точную tuple main package, test package, compiled runner/targetPackage и signing certificate. Автоматический переход между scopes запрещён.

## 2. Разделение testing и пользовательской настройки

Finite Android/integration profiles всегда используют E2E и TEMP data/pairing. NORMAL scope не допускается в их fixture context.

NORMAL — явная административная первоначальная настройка и delivery demonstration. Сервис предварительно запускается вручную через существующий network entrypoint. Pairing helper не запускает Center, gateway или модель.

Предусмотреть fixed trusted administrative scenario:
`test.bat scenario android-normal-setup`.

Manifest kind=offline: этот сценарий не использует model profile и не выполняет inference. Он не входит в unit/integration/all/smoke и не выдаёт модельную LIVE-приёмку.

Явный ignored setup request определяет NORMAL initial setup, выбранный server URL и expected device ID. Допустимые поля и значения строго валидируются; arbitrary package/APK/runner/path/credentials fields запрещены. Административная библиотека использует утверждённые normal data/pairing locations.

Создание устройства выполняется существующим явным device-provision flow. Existing pending DPAPI payload не перезаписывается. Helper потребляет только payload с совпадающим expected device ID.

## 3. Сохранение normal installation и данных

Свежий main package можно установить. Existing normal package принимается только после проверки ожидаемой identity/signing certificate; compatible update сохраняет app data.

NORMAL helper никогда не выполняет uninstall main, clear data или wipe. Чужой/неподтверждённый package сохраняется; операция завершается ошибкой.

Если normal app уже настроено, initial-setup flow не заменяет его token/server configuration. Возвращается безопасный результат «existing configuration preserved». Повторная настройка существующего устройства требует отдельного явно согласованного repair scope.

Test APK устанавливается только при подтверждённой собственной identity. Чужой/preexisting неподтверждённый test package не заменяется и не удаляется.

## 4. Fresh proof перед каждым NORMAL token transfer

До DPAPI decrypt выполнить текущим normal test APK:
- compiled target/certificate validation;
- loopback-only adb forward verification;
- ordinary application UID rejection до чтения payload;
- подтверждение adbd peer UID 0 или 2000;
- bounded non-secret handshake.

Предыдущий E2E report не заменяет fresh NORMAL proof.

Только затем helper расшифровывает выбранный DPAPI payload в памяти и передаёт его по проверенному LocalSocket channel. Device token отсутствует в process arguments, Intent extras, shell input, plaintext files, clipboard, logs и публичных результатах.

Instrumentation использует настоящие Settings EditText/Button на UI thread. Save проходит обычные validation, Keystore storage и production `/v1/me`. Успех требует совпадающего expected device ID.

После подтверждения использовать существующий confirm_pairing flow и удалить именно выбранный одноразовый payload. Ошибка не становится подтверждением pairing; helper не удаляет посторонние pending files или устройства.

## 5. Завершение и фиксация результата

Во время token phase запрещены recording, screenshots и hierarchy dumps.

Закрыть sockets и собственный adb forward. Завершить instrumentation и удалить только принадлежащий текущему setup normal test APK. Normal main APK оставить установленным; открыть его обычным launcher flow и проверить сохранённое подключение.

Capture допускается только после выхода из Settings и завершения instrumentation. Subsequent normal demonstration — отдельное явно инициированное действие; reconnect/pairing не запускают модель.

Evidence различает:
- fresh E2E boundary;
- fresh NORMAL boundary;
- actual normal pairing;
- normal relaunch;
- последующую демонстрацию.

Report включает run ID, APK hashes, точные безопасные package/target identities и результаты cleanup. Device token, DPAPI payload и пользовательская история отсутствуют. Только probe/build success не означает normal pairing PASS.

## 6. Scope реализации

- android/app/build.gradle.kts: строгий target enum, default=e2e;
- harness/android_build.py: fixed artifact mappings и typed identity validation;
- harness/pairing_bridge.py: typed target, fresh proof, normal preservation и cleanup;
- текущий androidTest ProbeInstrumentation.kt: actual Settings pairing после fresh proof;
- tests/scenarios/android-normal-setup.py/.json: явная administrative route;
- отдельный normal-setup request validator и эксплуатационная инструкция.

Protected bat/scenario_runner, Center startup, finite integration isolation и P02 auth/admin semantics не меняются.
