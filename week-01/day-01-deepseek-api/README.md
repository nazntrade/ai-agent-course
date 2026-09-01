# День 1. Первый запрос к LLM через API

Минимальный Web-интерфейс отправляет введённый текст в DeepSeek API и показывает ответ модели.

## Запуск в Windows PowerShell

Откройте PowerShell в папке этого задания и выполните:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

В открывшемся файле замените `your_api_key_here` своим ключом DeepSeek, сохраните файл и запустите приложение:

```powershell
streamlit run app.py
```

Откроется страница `http://localhost:8501`.

> Файл `.env` содержит секретный API-ключ и не попадёт в GitHub благодаря `.gitignore`.

