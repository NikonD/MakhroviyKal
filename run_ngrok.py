"""
Запуск приложения через ngrok для внешнего доступа.

Использование:
    python run_ngrok.py
    .venv/Scripts/python run_ngrok.py   (рекомендуется на Windows)

Переменные окружения (берутся из .env или окружения):
    NGROK_AUTHTOKEN  — токен авторизации ngrok (обязательно)
                       Получить: https://dashboard.ngrok.com/get-started/your-authtoken
    NGROK_DOMAIN     — статический домен ngrok (например: one-mutual-vulture.ngrok-free.app)
    PORT             — порт приложения (по умолчанию 8765)
"""
from __future__ import annotations

import os
import sys


def _read_env_file() -> dict[str, str]:
    """Читаем .env файл вручную, чтобы не зависеть от python-dotenv."""
    env: dict[str, str] = {}
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return env
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def main():
    # Загружаем .env
    file_env = _read_env_file()

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or file_env.get(key) or default

    port = int(get("PORT", "8765"))
    auth_token = get("NGROK_AUTHTOKEN")
    domain = get("NGROK_DOMAIN")

    # --- Проверяем наличие pyngrok ---
    try:
        from pyngrok import ngrok
    except ImportError:
        print("Пакет pyngrok не установлен. Устанавливаю...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyngrok"])
        from pyngrok import ngrok

    # --- Проверяем NGROK_AUTHTOKEN ---
    if not auth_token:
        print("=" * 60)
        print("ОШИБКА: NGROK_AUTHTOKEN не задан!")
        print()
        print("Добавьте в .env файл:")
        print("  NGROK_AUTHTOKEN=ваш_токен")
        print()
        print("Получить токен: https://dashboard.ngrok.com/get-started/your-authtoken")
        print("=" * 60)
        sys.exit(1)

    # --- Убиваем зависшие процессы ngrok, если они есть ---
    # Это нужно, чтобы не получать ошибку ERR_NGROK_334 (домен уже используется)
    try:
        ngrok.kill() # убивает процесс, запущенный текущим pyngrok
        if sys.platform == "win32":
            os.system("taskkill /F /IM ngrok.exe >nul 2>&1")
        else:
            os.system("pkill -9 ngrok >/dev/null 2>&1")
    except Exception:
        pass
    import time
    time.sleep(1)

    # --- Устанавливаем токен ---
    ngrok.set_auth_token(auth_token)

    # --- Открываем туннель ---
    if domain:
        print(f"Открываю ngrok туннель на домен: {domain} -> порт {port}...")
        tunnel = ngrok.connect(port, domain=domain)
    else:
        print(f"Открываю ngrok туннель на порт {port} (случайный домен)...")
        tunnel = ngrok.connect(port)

    public_url = tunnel.public_url
    # ngrok иногда возвращает http:// для https домена — исправляем
    if domain and not public_url.startswith("https://"):
        public_url = f"https://{domain}"

    print()
    print("=" * 60)
    print("  ngrok туннель активен!")
    print(f"  Публичный URL: {public_url}")
    print(f"  Локальный:     http://127.0.0.1:{port}")
    print("=" * 60)
    print()

    # --- Определяем Python из .venv (несёт все зависимости проекта) ---
    base_dir = os.path.dirname(os.path.abspath(__file__))
    venv_python_win = os.path.join(base_dir, ".venv", "Scripts", "python.exe")
    venv_python_unix = os.path.join(base_dir, ".venv", "bin", "python")

    if os.path.exists(venv_python_win):
        python_exe = venv_python_win
    elif os.path.exists(venv_python_unix):
        python_exe = venv_python_unix
    else:
        python_exe = sys.executable  # fallback к текущему интерпретатору
        print("ВНИМАНИЕ: виртуальное окружение .venv не найдено, использую системный Python.")

    # --- Запускаем uvicorn через .venv ---
    import subprocess
    cmd = [python_exe, "-m", "uvicorn", "app:app",
           "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"]
    print(f"Запускаю: {' '.join(cmd)}\n")
    try:
        subprocess.run(cmd, cwd=base_dir)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nЗакрываю ngrok туннель...")
        try:
            ngrok.disconnect(tunnel.public_url)
            ngrok.kill()
        except Exception:
            pass
        print("Готово.")


if __name__ == "__main__":
    main()
