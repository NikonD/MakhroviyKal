"""
OAuth для Google Drive (загрузка DOCX от имени вашего Google-аккаунта).

Один раз: python drive_oauth_setup.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from syslog import log

# Полный доступ к Drive — чтобы грузить в существующую папку по ID
SCOPES = ["https://www.googleapis.com/auth/drive"]


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).parent / ".env", override=True)
    except ImportError:
        pass


def resolve_client_secrets_path() -> Path:
    """Путь к OAuth client JSON из .env или secrets/."""
    _load_dotenv()
    base = Path(__file__).parent
    secrets_dir = base / "secrets"

    env_path = (os.environ.get("DRIVE_OAUTH_CLIENT_JSON") or "").strip()
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
        log(f"OAuth: в .env указан файл, но не найден: {p}")

    default = secrets_dir / "oauth-client.json"
    if default.is_file():
        return default

    # Google скачивает как client_secret_<id>.apps.googleusercontent.com.json
    if secrets_dir.is_dir():
        matches = sorted(secrets_dir.glob("client_secret*.json"))
        if len(matches) == 1:
            log(f"OAuth: найден client secrets: {matches[0].name}")
            return matches[0]
        if len(matches) > 1:
            raise FileNotFoundError(
                f"В secrets/ несколько client_secret*.json — укажите один в .env:\n"
                + "\n".join(f"  DRIVE_OAUTH_CLIENT_JSON={m}" for m in matches)
            )

    hint = (
        f"Положите JSON в {secrets_dir}\\oauth-client.json\n"
        "или скачайте из Google Cloud → Credentials → OAuth client (Desktop) → Download JSON"
    )
    if secrets_dir.is_dir():
        existing = [f.name for f in secrets_dir.iterdir() if f.is_file()]
        if existing:
            hint += f"\nСейчас в secrets/: {', '.join(existing)}"
    raise FileNotFoundError(hint)


def save_credentials(creds: Credentials, token_path: str | Path) -> None:
    path = Path(token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    log(f"OAuth: токен сохранён в {path}")


def load_credentials(
    *,
    client_secrets_path: str | Path,
    token_path: str | Path,
) -> Credentials:
    """Загружает или обновляет user OAuth token."""
    client_secrets_path = Path(client_secrets_path)
    token_path = Path(token_path)
    if not client_secrets_path.is_file():
        raise FileNotFoundError(
            f"OAuth client JSON не найден: {client_secrets_path}. "
            "Скачайте из Google Cloud → Credentials → OAuth client."
        )

    creds: Credentials | None = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        log("OAuth: обновление просроченного токена…")
        creds.refresh(Request())
        save_credentials(creds, token_path)

    if creds and creds.valid:
        return creds

    log("OAuth: нужна авторизация в браузере (откроется окно)…")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    save_credentials(creds, token_path)
    log("OAuth: ✓ авторизация успешна")
    return creds


def run_interactive_setup() -> None:
    _load_dotenv()
    base = Path(__file__).parent
    client_json = str(resolve_client_secrets_path())
    token_json = (
        os.environ.get("DRIVE_OAUTH_TOKEN_JSON") or ""
    ).strip() or str(base / "secrets" / "drive-oauth-token.json")

    print("=== Настройка OAuth для Google Drive ===")
    print(f"Client secrets: {client_json}")
    print(f"Token file:     {token_json}")
    print()
    creds = load_credentials(
        client_secrets_path=client_json,
        token_path=token_json,
    )
    # Показать под каким аккаунтом вошли (если есть в token)
    try:
        data = json.loads(Path(token_json).read_text(encoding="utf-8"))
        if data.get("account"):
            print(f"Аккаунт: {data.get('account')}")
    except Exception:
        pass
    print()
    print("Готово. Добавьте в .env:")
    print("  DRIVE_UPLOAD_AUTH=oauth")
    print(f"  DRIVE_OAUTH_CLIENT_JSON={client_json}")
    print(f"  DRIVE_OAUTH_TOKEN_JSON={token_json}")
    print()
    print("Перезапустите uvicorn и повторите апрув заявления.")


if __name__ == "__main__":
    run_interactive_setup()
