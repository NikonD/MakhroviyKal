#!/usr/bin/env bash
# Запуск веб-приложения "Экспертное заключение"
#
# Использование:
#   ./run.sh                    # авто-фейловер OpenRouter → Gemini
#   ./run.sh gemini             # только Gemini (быстро, без rate limits если платно)
#   ./run.sh openrouter         # только OpenRouter (бесплатно но дольше)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${PORT:-8765}"
HOST="${HOST:-127.0.0.1}"

case "${1:-auto}" in
    gemini)      export PROVIDERS="gemini" ;;
    openrouter)  export PROVIDERS="openrouter" ;;
    auto|"")     export PROVIDERS="${PROVIDERS:-openrouter,gemini}" ;;
    *)           echo "unknown provider: $1 (use: gemini|openrouter|auto)"; exit 1 ;;
esac

if [ ! -d ".venv" ]; then
    echo "==> Создаю виртуальное окружение..."
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip -q
    .venv/bin/pip install -r requirements.txt -q
fi

if [ -z "${GEMINI_API_KEY:-}" ]; then
    echo "ВНИМАНИЕ: GEMINI_API_KEY не задан в окружении."
    echo "         Можно ввести в форме UI, либо: export GEMINI_API_KEY=AIza..."
    echo "         Получить ключ: https://aistudio.google.com/apikey"
    echo
fi

URL="http://${HOST}:${PORT}"
echo "==> Запуск на ${URL}, провайдеры: ${PROVIDERS}"

# Открыть браузер через 2 сек после старта
( sleep 2 && command -v xdg-open >/dev/null && xdg-open "$URL" >/dev/null 2>&1 ) &

exec .venv/bin/python -m uvicorn app:app --host "$HOST" --port "$PORT"
