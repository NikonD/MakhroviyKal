"""
FastAPI приложение: загрузка PDF -> анализ -> предпросмотр/редактирование -> docx.
"""
from __future__ import annotations

import os
import re
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

app = FastAPI(title="Экспертное заключение — генератор")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


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
    gemini_key, openrouter_key = _resolve_keys(
        gemini_api_key or api_key, openrouter_api_key
    )
    pdf_bytes = await pdf.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Пустой PDF")
    try:
        result = analyzer.analyze_pdf(
            pdf_bytes,
            gemini_api_key=gemini_key,
            openrouter_api_key=openrouter_key,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка анализа: {e}")

    mapping: dict[int, int] = {}
    if auto_map and result.application.disciplines and result.certificates:
        try:
            mapping = analyzer.suggest_mapping(
                result,
                gemini_api_key=gemini_key,
                openrouter_api_key=openrouter_key,
            )
        except Exception:
            mapping = {}

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
    }


# Статика и индекс
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
