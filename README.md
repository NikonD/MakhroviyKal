# Экспертное заключение — генератор

Веб-приложение и CLI для автоматического заполнения таблицы «Экспертного заключения» по результатам неформального обучения. На входе — PDF с заявлением студента и сертификатами курсов (Coursera, Simplilearn, Codio, Google, ИНТУИТ, Stepik и т.п.), на выходе — `.docx` с готовой таблицей.

## Как это работает

```
┌──────────────┐   ┌──────────────────┐   ┌──────────────────────┐   ┌──────────────┐
│  PDF файл    │──▶│ PyMuPDF: страница│──▶│ Vision LLM           │──▶│ Редактор UI  │
│ (заявление + │   │   → PNG (150 DPI)│   │ OpenRouter (4 модели)│   │ или CLI →    │
│  сертификаты)│   └──────────────────┘   │ ↓ failover           │   │ docx         │
└──────────────┘                          │ Gemini (резерв)      │   └──────────────┘
                                          └──────────────────────┘
```

- **Заявление (стр. 1)** — рукописное, vision-модель распознаёт ФИО, группу, курс, телефон, список дисциплин и дату.
- **Сертификаты (стр. 2+)** — даже если повернуты на 90°, модель извлекает название курса, провайдера, часы, оценку и дату.
- **Cross-validation** — печатное ФИО из сертификатов используется для коррекции рукописного из заявления.
- **Авто-сопоставление** — LLM подсказывает какой сертификат к какой дисциплине относится.
- **Failover** — если OpenRouter упёрся в дневной лимит модели, пробуем следующую (4 модели подряд), потом откатываемся на Gemini.

## Стек

- **Backend:** Python 3 + FastAPI + Uvicorn
- **PDF → картинки:** PyMuPDF
- **Vision-провайдеры:** OpenRouter (несколько `:free` моделей) + Google Gemini как резерв
- **Генерация DOCX:** python-docx
- **Frontend:** vanilla HTML/CSS/JS

## Установка

```bash
cd /home/nikon/Proj/ddd
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Настройка ключей

Рекомендуется **OpenRouter** — даёт доступ к 4 бесплатным vision-моделям с авто-фейловером, итого ~50–100 запросов в день суммарно:

1. Зайти на <https://openrouter.ai/keys> (вход через Google/GitHub)
2. Нажать **«Create Key»** → название любое
3. Скопировать `sk-or-v1-...`

Опционально **Gemini** как резерв:
1. <https://aistudio.google.com/apikey>
2. **«Create API key»**
3. Скопировать `AIza...`

Записать в `.env` файл в корне проекта:

```dotenv
OPENROUTER_API_KEY=sk-or-v1-...
GEMINI_API_KEY=AIza...        # необязательно
```

Либо в переменные окружения:

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
export GEMINI_API_KEY="AIza..."
```

Либо ввести в полях UI — они сохранятся в `localStorage` браузера.

## Запуск

### Веб-приложение

```bash
./run.sh
# или
.venv/bin/python -m uvicorn app:app --port 8765
```

Открыть <http://127.0.0.1:8765>. Workflow:

1. **Шаг 1.** Перетащить PDF в дропзону, нажать «Распознать». 30–90 секунд.
2. **Шаг 2.** Проверить и поправить распознанные поля: ФИО, группа, ОП, курс. Код ОП (например `6B06105`) подставляется автоматически по префиксу группы (АПО, ИС, ВТ, ИКТ).
3. **Сертификаты** — карточки с распознанными курсами. Можно:
   - редактировать поля прямо в карточке (название, провайдер, часы);
   - перетаскивать в нужную дисциплину (drag-and-drop) или жать «→ дисциплина».
4. **Дисциплины** — для каждой задать кредиты, часы (или авто-сумма), баллы, соответствие, примечание, итоговую оценку.
5. **«Сгенерировать DOCX»** — скачивается как `ЭЗ_<ФИО>.docx`.

### CLI (без браузера)

```bash
# один файл → ЭЗ_<имя>.docx в текущей папке
.venv/bin/python cli.py "/path/to/Сычёв В.А.pdf"

# с явным именем выходного файла
.venv/bin/python cli.py student.pdf -o report.docx

# несколько PDF разом в указанную папку
.venv/bin/python cli.py *.pdf -O ~/reports/

# без LLM-сопоставления курс→дисциплина (экономит запросы)
.venv/bin/python cli.py student.pdf --no-mapping

# дополнительно сохранить распознанные данные .json для отладки
.venv/bin/python cli.py student.pdf --json
```

Удобный алиас:

```bash
echo 'alias ez="cd /home/nikon/Proj/ddd && .venv/bin/python cli.py"' >> ~/.bashrc
source ~/.bashrc
ez "Сычёв В.А.pdf"
```

## Структура проекта

```
ddd/
├── app.py                # FastAPI: /api/analyze, /api/generate, /api/health
├── analyzer.py           # PDF → vision-провайдеры (OpenRouter + Gemini) → JSON
├── doc_generator.py      # python-docx → готовая таблица
├── cli.py                # CLI для batch-обработки
├── run.sh                # Однострочный запуск веб-приложения
├── requirements.txt
├── .env                  # API ключи (в gitignore)
├── static/{index.html,app.js,style.css}
├── test_analyzer.py      # CLI отладка: показывает распознанное
└── debug_analyzer.py     # CLI отладка: сырой ответ vision-модели
```

## Переменные окружения

| Переменная | По умолчанию | Описание |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Ключ OpenRouter (рекомендуется) |
| `GEMINI_API_KEY` | — | Ключ Gemini (резерв) |
| `PROVIDERS` | `openrouter,gemini` | Порядок попыток провайдеров |
| `OPENROUTER_MODELS` | 4 модели (см. ниже) | Список моделей OpenRouter через запятую, в порядке попыток |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Модель Gemini |
| `RENDER_DPI` | `150` | DPI рендера страниц PDF |
| `ANALYZER_WORKERS` | `2` | Параллельных запросов к API |
| `ANALYZER_RETRIES` | `2` | Повторных попыток при transient ошибках |

**Дефолтный список моделей OpenRouter:**
```
google/gemini-2.0-flash-exp:free
qwen/qwen-2.5-vl-72b-instruct:free
meta-llama/llama-3.2-90b-vision-instruct:free
mistralai/pixtral-12b:free
```

Когда дневной лимит на одной модели заканчивается — автоматически пробуется следующая.

## Endpoints

- `GET  /` — UI
- `GET  /api/health` — статус, какие провайдеры готовы, какие ключи подхвачены
- `POST /api/analyze` — multipart с полями `pdf` (файл), опц. `openrouter_api_key`, `gemini_api_key`, `auto_map`
- `POST /api/generate` — JSON с распознанными данными → docx файл

## Примечания

- Таблица в DOCX содержит 12 колонок (как в образце), форматирование намеренно упрощено — Word/LibreOffice откроют без проблем.
- Если префикс группы не в `PROGRAM_MAP` внутри `doc_generator.py`, поле ОП можно ввести вручную.
- Соответствие по умолчанию — `«полное»`, можно менять.
- `.env` в `.gitignore` — ключи в репозиторий не утекут.
