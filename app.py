"""
FastAPI приложение: загрузка PDF -> анализ -> предпросмотр/редактирование -> docx.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import traceback
import urllib.parse
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import analyzer
import doc_generator
from drive_client import DriveClient
from queue_store import QueueStore
from syslog import log

app = FastAPI(title="Экспертное заключение — генератор")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

DATA_DIR = Path(__file__).parent / ".data"
STORE = QueueStore(DATA_DIR / "queue.sqlite3")

_drive: DriveClient | None = None
_poll_task: asyncio.Task | None = None

# Последний статус Drive (для логов и /api/health)
_drive_status: dict[str, Any] = {
    "enabled": False,
    "configured": False,
    "connected": False,
    "service_account_email": "",
    "input_folder_id": "",
    "output_folder_id": "",
    "last_check_at": None,
    "last_check_message": "",
    "last_poll_at": None,
    "last_poll_ok": None,
    "last_poll_message": "",
    "last_error": None,
    "output_connected": False,
    "output_shared_drive": False,
    "output_check_message": "",
    "upload_auth": "",
    "oauth_token_ready": False,
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class CertOut(BaseModel):
    page: int
    course_title: str = ""
    provider: str = ""
    hours: str = ""
    grade: str = ""
    date: str = ""
    student_name: str = ""
    certificate_id: str = ""


class AnalyzeResponse(BaseModel):
    student_name: str = ""
    group: str = ""
    program_code: str = ""
    course_year: str = ""
    phone: str = ""
    application_date: str = ""
    disciplines: list[str] = Field(default_factory=list)
    certificates: list[CertOut] = Field(default_factory=list)
    mapping: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class CourseIn(BaseModel):
    course_title: str = ""
    provider: str = ""
    hours: str = ""
    grade: str = ""
    date: str = ""


class RowIn(BaseModel):
    discipline: str = ""
    plan_credits: str = ""
    total_hours: str = ""
    grade_points: str = ""
    compliance: str = "полное"
    note: str = ""
    final_grade: str = ""
    courses: list[CourseIn] = Field(default_factory=list)


class GenerateRequest(BaseModel):
    student_name: str = ""
    program_code: str = ""
    course_year: str = ""
    rows: list[RowIn] = Field(default_factory=list)


class QueueEditState(BaseModel):
    student: dict[str, Any] = Field(default_factory=dict)
    disciplines: list[dict[str, Any]] = Field(default_factory=list)
    certificates: list[dict[str, Any]] = Field(default_factory=list)


def drive_file_view_url(drive_file_id: str) -> str:
    fid = (drive_file_id or "").strip()
    if not fid:
        return ""
    return f"https://drive.google.com/file/d/{fid}/view"


class QueueListItem(BaseModel):
    id: int
    drive_file_id: str
    filename: str
    status: str
    student_name: str = ""
    group: str = ""
    updated_at: int
    drive_file_url: str = ""


class QueueListResponse(BaseModel):
    items: list[QueueListItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
def _resolve_keys(
    gemini_form: str | None, openrouter_form: str | None
) -> tuple[str | None, str | None]:
    gemini = (gemini_form or "").strip() or os.environ.get("GEMINI_API_KEY") or None
    openrouter = (
        (openrouter_form or "").strip()
        or os.environ.get("OPENROUTER_API_KEY")
        or None
    )
    if not gemini and not openrouter:
        raise HTTPException(
            status_code=400,
            detail=(
                "Не указан ни один API ключ. Введите OpenRouter или Gemini в форме "
                "(или установите в окружении). OpenRouter: https://openrouter.ai/keys, "
                "Gemini: https://aistudio.google.com/apikey"
            ),
        )
    return gemini, openrouter


def _env_llm_keys() -> tuple[str | None, str | None]:
    return (
        os.environ.get("GEMINI_API_KEY") or None,
        os.environ.get("OPENROUTER_API_KEY") or None,
    )


def _result_to_queue_payload(result, mapping: dict[int, int]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Результат analyzer → analyze_json + edit_json для очереди."""
    analyze_json = {
        "student_name": result.application.student_name,
        "group": result.application.group,
        "program_code": doc_generator.derive_program(result.application.group),
        "course_year": result.application.course_year,
        "phone": result.application.phone,
        "application_date": result.application.application_date,
        "disciplines": result.application.disciplines,
        "certificates": [
            {k: getattr(c, k) for k in CertOut.model_fields} for c in result.certificates
        ],
        "mapping": {str(k): v for k, v in mapping.items()},
        "errors": result.errors,
    }
    edit_json = {
        "student": {
            "name": analyze_json.get("student_name", ""),
            "group": analyze_json.get("group", ""),
            "program_code": analyze_json.get("program_code", ""),
            "course_year": analyze_json.get("course_year", ""),
        },
        "disciplines": [
            {
                "name": d,
                "plan_credits": "5",
                "total_hours": "",
                "grade_points": "",
                "compliance": "полное",
                "note": "",
                "final_grade": "",
            }
            for d in (analyze_json.get("disciplines") or [])
        ],
        "certificates": [
            {**c, "_bound": None} for c in (analyze_json.get("certificates") or [])
        ],
    }
    for cert_idx_str, disc_idx in (analyze_json.get("mapping") or {}).items():
        try:
            ci = int(cert_idx_str)
            if 0 <= ci < len(edit_json["certificates"]):
                if 0 <= int(disc_idx) < len(edit_json["disciplines"]):
                    edit_json["certificates"][ci]["_bound"] = int(disc_idx)
        except Exception:
            pass
    return analyze_json, edit_json


def _run_pdf_analysis(pdf_bytes: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    gemini_key, openrouter_key = _env_llm_keys()
    if not (gemini_key or openrouter_key):
        raise RuntimeError(
            "Не задан GEMINI_API_KEY или OPENROUTER_API_KEY (в .env или в форме на главной)"
        )
    result = analyzer.analyze_pdf(
        pdf_bytes,
        gemini_api_key=gemini_key,
        openrouter_api_key=openrouter_key,
    )
    mapping: dict[int, int] = {}
    try:
        mapping = analyzer.suggest_mapping(
            result,
            gemini_api_key=gemini_key,
            openrouter_api_key=openrouter_key,
        )
    except Exception:
        mapping = {}
    return _result_to_queue_payload(result, mapping)


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze(
    pdf: UploadFile = File(...),
    api_key: str | None = Form(None),               # back-compat: Gemini
    gemini_api_key: str | None = Form(None),
    openrouter_api_key: str | None = Form(None),
    auto_map: bool = Form(True),
):
    if not pdf.filename or not pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Файл должен быть .pdf")
    log(f"API /analyze: файл «{pdf.filename}», auto_map={auto_map}")
    gemini_key, openrouter_key = _resolve_keys(
        gemini_api_key or api_key, openrouter_api_key
    )
    pdf_bytes = await pdf.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Пустой PDF")
    log(f"API /analyze: PDF {len(pdf_bytes)} байт, запуск распознавания…")
    try:
        result = analyzer.analyze_pdf(
            pdf_bytes,
            gemini_api_key=gemini_key,
            openrouter_api_key=openrouter_key,
        )
    except Exception as e:
        log(f"API /analyze: ошибка — {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка анализа: {e}")

    mapping: dict[int, int] = {}
    if auto_map and result.application.disciplines and result.certificates:
        log("API /analyze: авто-сопоставление курсов…")
        try:
            mapping = analyzer.suggest_mapping(
                result,
                gemini_api_key=gemini_key,
                openrouter_api_key=openrouter_key,
            )
        except Exception as ex:
            log(f"API /analyze: сопоставление не удалось — {ex}")
            mapping = {}

    log(
        f"API /analyze: готово — {result.application.student_name or '—'}, "
        f"дисциплин={len(result.application.disciplines)}, сертификатов={len(result.certificates)}"
    )
    return AnalyzeResponse(
        student_name=result.application.student_name,
        group=result.application.group,
        program_code=doc_generator.derive_program(result.application.group),
        course_year=result.application.course_year,
        phone=result.application.phone,
        application_date=result.application.application_date,
        disciplines=result.application.disciplines,
        certificates=[CertOut(**{k: getattr(c, k) for k in CertOut.model_fields}) for c in result.certificates],
        mapping={str(k): v for k, v in mapping.items()},
        errors=result.errors,
    )


@app.post("/api/generate")
async def generate(req: GenerateRequest):
    log(f"API /generate: {req.student_name or '—'}, строк таблицы={len(req.rows)}")
    payload = doc_generator.ReportPayload(
        student_name=req.student_name,
        program_code=req.program_code,
        course_year=req.course_year,
        rows=[
            doc_generator.DisciplineRow(
                discipline=r.discipline,
                plan_credits=r.plan_credits,
                total_hours=r.total_hours,
                grade_points=r.grade_points,
                compliance=r.compliance or "полное",
                note=r.note,
                final_grade=r.final_grade,
                courses=[
                    doc_generator.CourseEntry(
                        course_title=c.course_title,
                        provider=c.provider,
                        hours=c.hours,
                        grade=c.grade,
                        date=c.date,
                    )
                    for c in r.courses
                ],
            )
            for r in req.rows
        ],
    )
    data = doc_generator.build_report(payload)
    safe_name = re.sub(r"[^\w\-]+", "_", req.student_name) or "report"
    filename = f"ЭЗ_{safe_name}.docx"
    log(f"API /generate: DOCX {len(data)} байт → «{filename}»")
    encoded_filename = urllib.parse.quote(filename)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f'attachment; filename="report.docx"; filename*=utf-8\'\'{encoded_filename}',
        },
    )


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "providers": analyzer.PROVIDER_ORDER,
        "has_gemini_key": bool(os.environ.get("GEMINI_API_KEY")),
        "has_openrouter_key": bool(os.environ.get("OPENROUTER_API_KEY")),
        "gemini_model": analyzer.GEMINI_MODEL,
        "openrouter_models": analyzer.OPENROUTER_MODELS,
        "drive": dict(_drive_status),
        "queue_pending": len(STORE.list(status="pending_approval", limit=500)),
    }


# Статика и индекс
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/approve")
async def approve_page():
    return FileResponse(str(STATIC_DIR / "approve.html"))


@app.get("/api/requests", response_model=QueueListResponse)
async def list_requests():
    rows = STORE.list(limit=300)
    log(f"API /requests: список ({len(rows)} записей)")
    items: list[QueueListItem] = []
    for r in rows:
        a = r.analyze_json or {}
        items.append(
            QueueListItem(
                id=r.id,
                drive_file_id=r.drive_file_id,
                filename=r.filename,
                status=r.status,
                student_name=a.get("student_name") or "",
                group=a.get("group") or "",
                updated_at=r.updated_at,
                drive_file_url=drive_file_view_url(r.drive_file_id),
            )
        )
    return QueueListResponse(items=items)


@app.get("/api/requests/{request_id}")
async def get_request(request_id: int):
    try:
        r = STORE.get(int(request_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="not found")
    log(f"API /requests/{request_id}: статус={r.status}, файл=«{r.filename}»")
    return {
        "id": r.id,
        "status": r.status,
        "filename": r.filename,
        "drive_file_id": r.drive_file_id,
        "drive_file_url": drive_file_view_url(r.drive_file_id),
        "analyze": r.analyze_json,
        "edit": r.edit_json,
        "output_drive_file_id": r.output_drive_file_id,
        "error": r.error,
    }


@app.post("/api/requests/{request_id}/edit")
async def save_request_edit(request_id: int, payload: QueueEditState):
    log(f"API /requests/{request_id}/edit: сохранение черновика")
    try:
        r = STORE.save_edit(int(request_id), payload.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True, "id": r.id, "updated_at": r.updated_at}


@app.post("/api/requests/{request_id}/reanalyze")
async def reanalyze_request(request_id: int):
    """Повторно скачать PDF с Drive и распознать (заявление + сертификаты)."""
    global _drive
    log(f"API /requests/{request_id}/reanalyze: старт")
    try:
        r = STORE.get(int(request_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="not found")
    if r.status == "sent":
        raise HTTPException(
            status_code=400,
            detail="Заявление уже отправлено в Drive — повторное распознавание недоступно",
        )

    if _drive is None:
        _drive = _make_drive_client()
    if _drive is None:
        raise HTTPException(
            status_code=400,
            detail="Drive (service account) не настроен — нельзя скачать PDF",
        )

    try:
        log(f"API /requests/{request_id}/reanalyze: скачивание «{r.filename}»…")
        pdf_bytes = _drive.download_file_bytes(r.drive_file_id)
        log(f"API /requests/{request_id}/reanalyze: распознавание ({len(pdf_bytes)} байт)…")
        analyze_json, edit_json = _run_pdf_analysis(pdf_bytes)
        STORE.update_analyze(r.id, analyze_json)
        STORE.save_edit(r.id, edit_json)
        r = STORE.reset_pending(r.id)
        log(f"API /requests/{request_id}/reanalyze: готово")
        return {
            "ok": True,
            "id": r.id,
            "status": r.status,
            "analyze": analyze_json,
            "edit": edit_json,
            "errors": analyze_json.get("errors") or [],
        }
    except HTTPException:
        raise
    except Exception as e:
        log(f"API /requests/{request_id}/reanalyze: ошибка — {e}")
        log(traceback.format_exc().rstrip())
        STORE.mark_error(r.id, str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/requests/{request_id}/approve")
async def approve_request(request_id: int):
    global _drive
    log(f"API /requests/{request_id}/approve: старт")
    try:
        r = STORE.get(int(request_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="not found")
    if r.status not in ("pending_approval", "approved", "error"):
        raise HTTPException(status_code=400, detail=f"bad status: {r.status}")

    out_folder = (os.environ.get("DRIVE_OUTPUT_FOLDER_ID") or "").strip()
    if not out_folder:
        raise HTTPException(status_code=400, detail="DRIVE_OUTPUT_FOLDER_ID is not set")
    upload_drive = _make_upload_drive_client()
    if upload_drive is None:
        mode = _upload_auth_mode()
        hint = (
            "Запустите один раз: python drive_oauth_setup.py"
            if mode == "oauth"
            else "Проверьте DRIVE_SERVICE_ACCOUNT_JSON"
        )
        raise HTTPException(status_code=400, detail=f"Drive upload не настроен. {hint}")

    require_shared = upload_drive.auth_kind == "service_account"
    out_access = upload_drive.verify_folder_access(
        out_folder,
        label="Выходная папка",
        for_upload=require_shared,
    )
    if not out_access["ok"]:
        raise HTTPException(status_code=400, detail=out_access["message"])

    edit = r.edit_json or {}
    # формируем payload для doc_generator из сохранённого черновика
    student = edit.get("student") or {}
    disciplines = edit.get("disciplines") or []
    certificates = edit.get("certificates") or []

    def _rows() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for i, d in enumerate(disciplines):
            rows.append(
                {
                    "discipline": d.get("name", ""),
                    "plan_credits": d.get("plan_credits", ""),
                    "total_hours": d.get("total_hours", ""),
                    "grade_points": d.get("grade_points", ""),
                    "compliance": d.get("compliance", "полное"),
                    "note": d.get("note", ""),
                    "final_grade": d.get("final_grade", ""),
                    "courses": [
                        {
                            "course_title": c.get("course_title", ""),
                            "provider": c.get("provider", ""),
                            "hours": c.get("hours", ""),
                            "grade": c.get("grade", ""),
                            "date": c.get("date", ""),
                        }
                        for c in certificates
                        if c.get("_bound", None) == i
                    ],
                }
            )
        return rows

    gen_req = GenerateRequest(
        student_name=str(student.get("name") or ""),
        program_code=str(student.get("program_code") or ""),
        course_year=str(student.get("course_year") or ""),
        rows=[RowIn(**row) for row in _rows()],
    )

    try:
        STORE.mark_approved(r.id)
        log(f"API /requests/{request_id}/approve: генерация DOCX…")
        payload = doc_generator.ReportPayload(
            student_name=gen_req.student_name,
            program_code=gen_req.program_code,
            course_year=gen_req.course_year,
            rows=[
                doc_generator.DisciplineRow(
                    discipline=rr.discipline,
                    plan_credits=rr.plan_credits,
                    total_hours=rr.total_hours,
                    grade_points=rr.grade_points,
                    compliance=rr.compliance or "полное",
                    note=rr.note,
                    final_grade=rr.final_grade,
                    courses=[
                        doc_generator.CourseEntry(
                            course_title=c.course_title,
                            provider=c.provider,
                            hours=c.hours,
                            grade=c.grade,
                            date=c.date,
                        )
                        for c in rr.courses
                    ],
                )
                for rr in gen_req.rows
            ],
        )
        data = doc_generator.build_report(payload)
        safe_name = re.sub(r"[^\w\-]+", "_", gen_req.student_name) or f"request_{r.id}"
        filename = f"ЭЗ_{safe_name}.docx"
        log(f"API /requests/{request_id}/approve: DOCX {len(data)} байт, загрузка в Drive…")
        out_id = upload_drive.upload_docx_to_folder(
            folder_id=out_folder, filename=filename, data=data
        )
        STORE.mark_sent(r.id, output_drive_file_id=out_id)
        log(f"API /requests/{request_id}/approve: успех")
        return {"ok": True, "output_drive_file_id": out_id}
    except Exception as e:
        log(f"API /requests/{request_id}/approve: ошибка — {e}")
        STORE.mark_error(r.id, str(e))
        detail = str(e)
        if (
            "404" in detail
            or "notFound" in detail
            or "нет доступа" in detail
            or "storage quota" in detail.lower()
            or "квоты" in detail.lower()
            or "Общем диске" in detail
        ):
            raise HTTPException(status_code=400, detail=detail)
        raise HTTPException(status_code=500, detail=detail)


# ---------------------------------------------------------------------------
# Drive polling worker
# ---------------------------------------------------------------------------
def _drive_env_summary() -> dict[str, str]:
    return {
        "enabled": (os.environ.get("DRIVE_ENABLED") or "").strip(),
        "sa_json": (os.environ.get("DRIVE_SERVICE_ACCOUNT_JSON") or "").strip(),
        "input_folder": (os.environ.get("DRIVE_INPUT_FOLDER_ID") or "").strip(),
        "output_folder": (os.environ.get("DRIVE_OUTPUT_FOLDER_ID") or "").strip(),
        "poll_interval": (os.environ.get("DRIVE_POLL_INTERVAL") or "60").strip(),
    }


def _make_drive_client() -> DriveClient | None:
    global _drive_status
    sa = (os.environ.get("DRIVE_SERVICE_ACCOUNT_JSON") or "").strip()
    _drive_status["input_folder_id"] = (os.environ.get("DRIVE_INPUT_FOLDER_ID") or "").strip()
    _drive_status["output_folder_id"] = (os.environ.get("DRIVE_OUTPUT_FOLDER_ID") or "").strip()

    if not sa:
        _drive_status["configured"] = False
        _drive_status["connected"] = False
        _drive_status["last_check_message"] = "DRIVE_SERVICE_ACCOUNT_JSON не задан"
        log("Drive: ✗ не настроен — нет пути к JSON ключу")
        return None

    sa_path = Path(sa)
    if not sa_path.is_file():
        _drive_status["configured"] = False
        _drive_status["connected"] = False
        _drive_status["last_check_message"] = f"Файл ключа не найден: {sa}"
        log(f"Drive: ✗ файл ключа не найден: {sa}")
        return None

    _drive_status["configured"] = True
    log(f"Drive: проверка ключа… ({sa})")
    try:
        client = DriveClient.from_service_account(service_account_json_path=str(sa_path))
        _drive_status["service_account_email"] = client.service_account_email
        return client
    except Exception as e:
        _drive_status["connected"] = False
        _drive_status["last_error"] = str(e)
        _drive_status["last_check_message"] = str(e)
        log(f"Drive: ✗ не удалось создать клиент — {e}")
        log(traceback.format_exc().rstrip())
        return None


def _upload_auth_mode() -> str:
    return (os.environ.get("DRIVE_UPLOAD_AUTH") or "service_account").strip().lower()


def _make_upload_drive_client() -> DriveClient | None:
    """Клиент для загрузки DOCX: oauth (Мой диск) или service account (общий диск)."""
    global _drive_status
    mode = _upload_auth_mode()
    _drive_status["upload_auth"] = mode

    if mode == "oauth":
        from drive_oauth import load_credentials, resolve_client_secrets_path

        base = Path(__file__).parent
        try:
            client_json = str(resolve_client_secrets_path())
        except FileNotFoundError as e:
            log(f"Drive OAuth: ✗ {e}")
            return None
        token_json = (
            (os.environ.get("DRIVE_OAUTH_TOKEN_JSON") or "").strip()
            or str(base / "secrets" / "drive-oauth-token.json")
        )
        _drive_status["oauth_token_ready"] = Path(token_json).is_file()
        if not Path(client_json).is_file():
            log(f"Drive OAuth: ✗ нет client JSON: {client_json}")
            return None
        if not Path(token_json).is_file():
            log(
                "Drive OAuth: ✗ нет токена. Запустите: python drive_oauth_setup.py"
            )
            return None
        try:
            creds = load_credentials(
                client_secrets_path=client_json,
                token_path=token_json,
            )
            return DriveClient(creds, auth_kind="oauth", label="OAuth upload")
        except Exception as e:
            log(f"Drive OAuth: ✗ {e}")
            log(traceback.format_exc().rstrip())
            return None

    return _make_drive_client()


def _drive_startup_check(client: DriveClient) -> bool:
    """Пробное подключение к входной и выходной папкам при старте."""
    global _drive_status
    email = _drive_status.get("service_account_email") or "(client_email в JSON)"
    log(f"Drive: сервисный аккаунт для шаринга папок: {email}")

    in_folder = _drive_status["input_folder_id"]
    out_folder = _drive_status["output_folder_id"]
    _drive_status["last_check_at"] = int(time.time())

    if not in_folder:
        _drive_status["connected"] = False
        _drive_status["last_check_message"] = "DRIVE_INPUT_FOLDER_ID не задан"
        log("Drive: ✗ входная папка не задана (DRIVE_INPUT_FOLDER_ID)")
        return False

    log(f"Drive: проверка ВХОДНОЙ папки {in_folder}…")
    probe = client.probe_folder(in_folder)
    _drive_status["connected"] = bool(probe["ok"])
    _drive_status["last_check_message"] = probe["message"]

    if probe["ok"]:
        log(f"Drive: ✓ вход — {probe['message']}")
        if probe["pdf_count"] == 0:
            log("Drive: во входной папке пока нет PDF")
    else:
        _drive_status["last_error"] = probe["message"]
        log(f"Drive: ✗ вход — {probe['message']}")
        return False

    if not out_folder:
        _drive_status["output_connected"] = False
        _drive_status["output_check_message"] = "DRIVE_OUTPUT_FOLDER_ID не задан"
        log("Drive: ⚠ выходная папка не задана — апрув с загрузкой DOCX не сработает")
        return True

    upload_mode = _upload_auth_mode()
    _drive_status["upload_auth"] = upload_mode
    log(f"Drive: загрузка DOCX — режим «{upload_mode}»")

    upload_client = _make_upload_drive_client()
    if upload_client is None:
        _drive_status["output_connected"] = False
        _drive_status["output_check_message"] = (
            "OAuth: запустите python drive_oauth_setup.py"
            if upload_mode == "oauth"
            else "Service account не настроен"
        )
        log(f"Drive: ✗ выход — {_drive_status['output_check_message']}")
        return _drive_status["connected"]

    log(f"Drive: проверка ВЫХОДНОЙ папки {out_folder}…")
    require_shared = upload_client.auth_kind == "service_account"
    out_access = upload_client.verify_folder_access(
        out_folder,
        label="Выходная папка",
        for_upload=require_shared,
    )
    _drive_status["output_connected"] = bool(out_access["ok"])
    _drive_status["output_shared_drive"] = bool(out_access.get("shared_drive"))
    _drive_status["output_check_message"] = out_access["message"]
    if out_access["ok"]:
        log(f"Drive: ✓ выход — {out_access['message']}")
    else:
        _drive_status["last_error"] = out_access["message"]
        log(f"Drive: ✗ выход — {out_access['message']}")
        if upload_mode == "service_account":
            log(
                "Drive: исправление — общий диск (Shared Drive) или OAuth: "
                "DRIVE_UPLOAD_AUTH=oauth и python drive_oauth_setup.py"
            )
        else:
            log(
                "Drive: папка должна быть в вашем Google Drive (тот же аккаунт, "
                "что при OAuth). ID — из URL папки."
            )
    return _drive_status["connected"]


async def _drive_poll_loop() -> None:
    global _drive, _drive_status
    interval_s = int((os.environ.get("DRIVE_POLL_INTERVAL") or "60").strip() or "60")
    in_folder = _drive_status["input_folder_id"] or (os.environ.get("DRIVE_INPUT_FOLDER_ID") or "").strip()
    if not in_folder:
        log("Воркер: ✗ DRIVE_INPUT_FOLDER_ID не задан — воркер остановлен")
        return

    if _drive is None:
        _drive = _make_drive_client()
    if _drive is None:
        log("Воркер: ✗ нет Drive-клиента — воркер остановлен")
        return

    if not _drive_status.get("connected"):
        _drive_startup_check(_drive)

    log(
        f"Воркер: ✓ запущен | интервал {interval_s} с | "
        f"вход: {in_folder} | выход: {_drive_status.get('output_folder_id') or '—'}"
    )
    cycle_num = 0
    while True:
        cycle_num += 1
        try:
            log(f"——— Воркер: цикл #{cycle_num} ———")
            if not _drive_status.get("connected"):
                log("Воркер: повторная проверка подключения…")
                if not _drive_startup_check(_drive):
                    _drive_status["last_poll_at"] = int(time.time())
                    _drive_status["last_poll_ok"] = False
                    _drive_status["last_poll_message"] = _drive_status.get("last_check_message") or "нет подключения"
                    log(f"Воркер: цикл #{cycle_num} пропущен — Drive недоступен")
                    await asyncio.sleep(max(10, interval_s))
                    continue

            pdfs = _drive.list_pdfs_in_folder(in_folder)
            total = len(pdfs)
            skipped = 0
            new_count = 0
            processed_ok = 0
            processed_err = 0

            for f in pdfs:
                # если уже обработано/в очереди — пропускаем без скачивания
                existing = STORE.get_by_drive_file_id(f.id)
                if existing is not None:
                    skipped += 1
                    log(
                        f"Воркер: пропуск «{f.name}» — уже в очереди "
                        f"(#{existing.id}, статус={existing.status})"
                    )
                    continue

                new_count += 1
                log(f"Воркер: ▶ НОВЫЙ файл с Drive «{f.name}» (drive_id={f.id})")
                # создаём запись (pending) сразу, чтобы дублей не было
                row = STORE.upsert_pending(
                    drive_file_id=f.id,
                    filename=f.name,
                    analyze_json={
                        "student_name": "",
                        "group": "",
                        "program_code": "",
                        "course_year": "",
                        "phone": "",
                        "application_date": "",
                        "disciplines": [],
                        "certificates": [],
                        "mapping": {},
                        "errors": [],
                    },
                )

                try:
                    pdf_bytes = _drive.download_file_bytes(f.id)
                    log(f"Воркер: #{row.id} — распознавание PDF ({len(pdf_bytes)} байт)…")
                    analyze_json, edit_json = _run_pdf_analysis(pdf_bytes)
                    STORE.update_analyze(row.id, analyze_json)
                    STORE.save_edit(row.id, edit_json)
                    processed_ok += 1
                    log(f"Воркер: ✓ #{row.id} «{f.name}» — распознано, ждёт апрува на /approve")
                except Exception as e:
                    processed_err += 1
                    log(f"Воркер: ✗ #{row.id} «{f.name}» — ошибка: {e}")
                    log(traceback.format_exc().rstrip())
                    STORE.mark_error(row.id, str(e))

            summary = (
                f"цикл #{cycle_num}: в папке PDF={total}, "
                f"новых={new_count}, уже в очереди={skipped}, "
                f"успешно обработано={processed_ok}, ошибок={processed_err}"
            )
            _drive_status["last_poll_at"] = int(time.time())
            _drive_status["last_poll_ok"] = True
            _drive_status["last_poll_message"] = summary
            _drive_status["last_error"] = None if processed_err == 0 else f"ошибок в цикле: {processed_err}"

            if total == 0:
                log("Воркер: итог — папка пустая (0 PDF на Drive)")
            elif new_count == 0:
                log(f"Воркер: итог — {summary} (с Drive ничего нового не взято)")
            else:
                log(f"Воркер: итог — {summary}")
        except asyncio.CancelledError:
            log("Воркер: остановка")
            raise
        except Exception as ex:
            _drive_status["last_poll_at"] = int(time.time())
            _drive_status["last_poll_ok"] = False
            _drive_status["last_poll_message"] = str(ex)
            _drive_status["last_error"] = str(ex)
            _drive_status["connected"] = False
            log(f"Воркер: ✗ ошибка цикла #{cycle_num} — {ex}")
            log(traceback.format_exc().rstrip())
            log("Воркер: на следующем цикле попробуем подключиться снова")

        log(f"Воркер: пауза {interval_s} с…")
        await asyncio.sleep(max(10, interval_s))


@app.on_event("startup")
async def _startup() -> None:
    global _poll_task, _drive, _drive_status
    log("========== Старт приложения ==========")
    env = _drive_env_summary()
    log(
        f"Конфиг: DRIVE_ENABLED={env['enabled'] or '—'}, "
        f"poll={env['poll_interval']}с, "
        f"вход={env['input_folder'][:16] + '…' if env['input_folder'] else '—'}, "
        f"выход={env['output_folder'][:16] + '…' if env['output_folder'] else '—'}"
    )

    enabled = env["enabled"].lower() in ("1", "true", "yes", "on")
    _drive_status["enabled"] = enabled
    if not enabled:
        log("Drive: воркер выключен (DRIVE_ENABLED не 1/true)")
        return

    _drive = _make_drive_client()
    if _drive is not None:
        _drive_startup_check(_drive)

    if _poll_task is None:
        log("Drive-воркер: фоновая задача опроса запущена")
        _poll_task = asyncio.create_task(_drive_poll_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _poll_task
    log("Остановка приложения")
    if _poll_task:
        _poll_task.cancel()
        _poll_task = None
