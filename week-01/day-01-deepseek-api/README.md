# День 1. Первый запрос к LLM через API

Небольшое Web-приложение на Python и Streamlit отправляет запрос в DeepSeek API и показывает ответ модели.

## Что реализовано

- ввод запроса через Web-интерфейс;
- подключение к DeepSeek API;
- индикатор «DeepSeek думает...» во время ожидания;
- потоковый вывод ответа по мере его получения;
- безопасное хранение API-ключа в `.env`;
- запуск приложения двойным щелчком через `run_app.bat`.

## Первый запуск

Откройте PowerShell в папке проекта и выполните:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

В открывшемся файле замените `your_api_key_here` своим ключом DeepSeek и сохраните файл.

## Запуск приложения

После первоначальной настройки запустите двойным щелчком:

```text
run_app.bat
```

Либо выполните в PowerShell:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Приложение откроется по адресу `http://localhost:8501`.

> Файл `.env` содержит секретный API-ключ и не загружается в GitHub благодаря `.gitignore`.

