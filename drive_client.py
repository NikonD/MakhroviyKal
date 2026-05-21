from __future__ import annotations

import io
import traceback
from dataclasses import dataclass

from google.oauth2 import service_account

from syslog import log

try:
    # googleapiclient is optional until enabled via requirements
    from googleapiclient.discovery import build  # type: ignore
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload  # type: ignore
except Exception:  # pragma: no cover
    build = None  # type: ignore
    MediaIoBaseDownload = None  # type: ignore
    MediaIoBaseUpload = None  # type: ignore


SCOPES = ["https://www.googleapis.com/auth/drive"]

# Service account не может владеть файлами в «Мой диск» — только Shared Drive (общий диск).
_SA_UPLOAD_HINT = (
    "Сервисный аккаунт не имеет квоты Google Drive. "
    "Папку для загрузки DOCX нужно создать на Общем диске (Shared Drive), "
    "не в личном «Мой диск». Добавьте {email} участником общего диска "
    "(роль: Менеджер контента или Редактор) и укажите ID папки в DRIVE_OUTPUT_FOLDER_ID."
)


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mimeType: str | None = None
    modifiedTime: str | None = None


class DriveClient:
    def __init__(
        self,
        credentials,
        *,
        auth_kind: str = "service_account",
        service_account_email: str = "",
        label: str = "",
    ) -> None:
        if build is None:
            raise RuntimeError(
                "google-api-python-client is not installed. Add it to requirements.txt."
            )
        self.auth_kind = auth_kind
        self.service_account_email = service_account_email or ""
        self.svc = build("drive", "v3", credentials=credentials, cache_discovery=False)
        who = (
            self.service_account_email
            if auth_kind == "service_account"
            else "OAuth (ваш Google-аккаунт)"
        )
        log(f"Drive: клиент создан | {label or auth_kind} | {who}")

    @classmethod
    def from_service_account(cls, *, service_account_json_path: str) -> "DriveClient":
        creds = service_account.Credentials.from_service_account_file(
            service_account_json_path, scopes=SCOPES
        )
        email = getattr(creds, "service_account_email", "") or ""
        return cls(
            creds,
            auth_kind="service_account",
            service_account_email=email,
            label="service account",
        )

    def list_pdfs_in_folder(self, folder_id: str) -> list[DriveFile]:
        log(f"Drive: список PDF в папке {folder_id[:12]}…")
        q = (
            f"'{folder_id}' in parents and trashed=false and mimeType='application/pdf'"
        )
        files: list[DriveFile] = []
        page_token = None
        while True:
            resp = (
                self.svc.files()
                .list(
                    q=q,
                    fields="nextPageToken, files(id,name,mimeType,modifiedTime)",
                    pageToken=page_token,
                    pageSize=200,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for f in resp.get("files", []) or []:
                files.append(
                    DriveFile(
                        id=f["id"],
                        name=f.get("name") or "",
                        mimeType=f.get("mimeType"),
                        modifiedTime=f.get("modifiedTime"),
                    )
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        if files:
            names = ", ".join(f.name for f in files[:5])
            more = f" (+ещё {len(files) - 5})" if len(files) > 5 else ""
            log(f"Drive: OK — в папке {len(files)} PDF: {names}{more}")
        else:
            log("Drive: OK — папка доступна, PDF не найдено (папка пустая или нет .pdf)")
        return files

    def verify_folder_access(
        self, folder_id: str, *, label: str = "папка", for_upload: bool = False
    ) -> dict:
        """Проверяет, что папка существует и сервисный аккаунт её видит."""
        folder_id = (folder_id or "").strip()
        if not folder_id:
            return {
                "ok": False,
                "name": "",
                "shared_drive": False,
                "message": f"{label}: ID папки пустой",
            }
        try:
            meta = (
                self.svc.files()
                .get(
                    fileId=folder_id,
                    fields="id,name,mimeType,capabilities,driveId",
                    supportsAllDrives=True,
                )
                .execute()
            )
            name = meta.get("name") or "—"
            mime = meta.get("mimeType") or ""
            drive_id = meta.get("driveId") or ""
            is_shared = bool(drive_id)
            if mime != "application/vnd.google-apps.folder":
                return {
                    "ok": False,
                    "name": name,
                    "shared_drive": is_shared,
                    "message": (
                        f"{label}: ID {folder_id} — это не папка ({mime}). "
                        "В .env нужен ID папки из URL Google Drive."
                    ),
                }
            caps = meta.get("capabilities") or {}
            can_add = caps.get("canAddChildren", True)
            if can_add is False:
                return {
                    "ok": False,
                    "name": name,
                    "shared_drive": is_shared,
                    "message": (
                        f"{label} «{name}»: нет права загружать файлы. "
                        f"Дайте сервисному аккаунту роль Редактор: {self.service_account_email}"
                    ),
                }
            if for_upload and not is_shared and self.auth_kind == "service_account":
                hint = _SA_UPLOAD_HINT.format(email=self.service_account_email)
                log(f"Drive: ✗ {label} «{name}» — это «Мой диск», загрузка SA невозможна")
                return {
                    "ok": False,
                    "name": name,
                    "shared_drive": False,
                    "message": f"{label} «{name}»: {hint}",
                }
            if for_upload and self.auth_kind == "oauth":
                where = "Мой диск (OAuth)" if not is_shared else "общий диск (OAuth)"
            else:
                where = "общий диск" if is_shared else "Мой диск (только чтение для SA)"
            log(f"Drive: ✓ {label} «{name}» — доступ есть ({where})")
            return {
                "ok": True,
                "name": name,
                "shared_drive": is_shared,
                "message": f"{label} «{name}» доступна ({where})",
            }
        except Exception as e:
            err = str(e)
            hint = (
                f"Расшарьте эту папку на {self.service_account_email} с ролью Редактор "
                f"и проверьте DRIVE_*_FOLDER_ID в .env (id из URL папки)."
            )
            if "404" in err or "notFound" in err or "File not found" in err:
                msg = f"{label}: папка не найдена (id={folder_id}). {hint}"
            else:
                msg = f"{label}: {err}. {hint}"
            log(f"Drive: ✗ {msg}")
            return {"ok": False, "name": "", "shared_drive": False, "message": msg}

    def probe_folder(self, folder_id: str) -> dict:
        """Проверка входной папки: доступ + число PDF."""
        access = self.verify_folder_access(folder_id, label="Входная папка")
        if not access["ok"]:
            return {
                "ok": False,
                "pdf_count": 0,
                "files": [],
                "message": access["message"],
            }
        try:
            files = self.list_pdfs_in_folder(folder_id)
            return {
                "ok": True,
                "pdf_count": len(files),
                "files": [{"id": f.id, "name": f.name} for f in files[:20]],
                "message": f"Входная папка доступна, PDF: {len(files)}",
            }
        except Exception as e:
            log(f"Drive: ОШИБКА чтения PDF — {e}")
            log(traceback.format_exc().rstrip())
            return {
                "ok": False,
                "pdf_count": 0,
                "files": [],
                "message": str(e),
            }

    def download_file_bytes(self, file_id: str) -> bytes:
        log(f"Drive: скачивание file_id={file_id}")
        request = self.svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        data = fh.getvalue()
        if len(data) < 100:
            log(f"Drive: ВНИМАНИЕ — скачано всего {len(data)} байт (возможно пустой/битый файл)")
        else:
            log(f"Drive: OK — скачано {len(data)} байт")
        return data

    def upload_docx_to_folder(self, *, folder_id: str, filename: str, data: bytes) -> str:
        # OAuth может грузить в «Мой диск»; SA — только общий диск
        require_shared = self.auth_kind == "service_account"
        access = self.verify_folder_access(
            folder_id,
            label="Выходная папка",
            for_upload=require_shared,
        )
        if not access["ok"]:
            raise RuntimeError(access["message"])
        log(
            f"Drive: загрузка «{filename}» ({len(data)} байт) → "
            f"«{access.get('name') or folder_id}»"
        )
        media = MediaIoBaseUpload(
            io.BytesIO(data),
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            resumable=False,
        )
        meta = {
            "name": filename,
            "parents": [folder_id],
            "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        try:
            created = (
                self.svc.files()
                .create(
                    body=meta,
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except Exception as e:
            err = str(e)
            if (
                "storageQuotaExceeded" in err
                or "storage quota" in err.lower()
                or "Service Accounts do not have storage quota" in err
            ):
                raise RuntimeError(
                    _SA_UPLOAD_HINT.format(email=self.service_account_email)
                ) from e
            if "404" in err or "notFound" in err:
                raise RuntimeError(
                    f"Не удалось загрузить в папку {folder_id}: нет доступа. "
                    f"Расшарьте выходную папку на {self.service_account_email} (Редактор). "
                    f"Оригинал: {err}"
                ) from e
            raise
        out_id = created["id"]
        log(f"Drive: ✓ загружено «{filename}», file_id={out_id}")
        return out_id

