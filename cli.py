#!/usr/bin/env python3
"""
CLI: PDF (заявление + сертификаты) → готовый .docx с таблицей.

Использование:
    python cli.py <student.pdf> [-o output.docx] [--no-mapping] [--json]

Примеры:
    python cli.py "Сычёв В.А.pdf"
    python cli.py student.pdf -o report.docx
    python cli.py *.pdf -O ./reports/

Ключ Gemini берётся из переменной окружения GEMINI_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    # .env имеет приоритет над уже выставленными env-переменными — иначе
    # старые `export GEMINI_API_KEY=...` из shell перекрывают актуальный ключ.
    load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass

import analyzer
import doc_generator


def colored(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def safe_filename(name: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", name.strip())
    return s.strip("_") or "report"


def process_one(pdf_path: Path, out_path: Path, *, auto_map: bool, dump_json: bool) -> int:
    print(colored(f"→ {pdf_path.name}", "1;36"))
    if not pdf_path.exists():
        print(colored(f"   файл не найден", "31"))
        return 1

    pdf_bytes = pdf_path.read_bytes()

    state = {"total": 0, "done": 0}
    def on_progress(event: str, info: dict) -> None:
        if event == "start":
            state["total"] = info["total_pages"]
            print(colored(f"   распознаю {state['total']} страниц…", "2"))
        elif event == "page_done":
            state["done"] = info["done"]
            tag = "✓" if info.get("ok") else "✗"
            color = "32" if info.get("ok") else "31"
            extra = "" if info.get("ok") else f" — {info.get('error', '')}"
            label = "заявление" if info["page"] == 1 else f"сертификат стр.{info['page']}"
            print(colored(
                f"   [{state['done']}/{state['total']}] {tag} {label}{extra}",
                color,
            ), flush=True)

    try:
        result = analyzer.analyze_pdf(pdf_bytes, on_progress=on_progress)
    except Exception as e:
        print(colored(f"   ошибка анализа: {e}", "31"))
        return 1

    app = result.application
    print(f"   ФИО:     {colored(app.student_name or '(пусто)', '1')}")
    print(f"   Группа:  {app.group}  Курс: {app.course_year}")
    print(f"   Дисциплин: {len(app.disciplines)}, сертификатов: {len(result.certificates)}")

    if result.errors:
        for e in result.errors:
            print(colored(f"   ⚠ {e}", "33"))

    for c in result.certificates:
        print(f"     · стр.{c.page:>2} {c.course_title!r:50.50} — {c.provider or '?'} ({c.hours or '?'})")

    # Сопоставление сертификатов с дисциплинами
    mapping: dict[int, int] = {}
    if auto_map and app.disciplines and result.certificates:
        print(colored("   сопоставляю сертификаты с дисциплинами…", "2"))
        try:
            mapping = analyzer.suggest_mapping(result)
        except Exception as e:
            print(colored(f"   ⚠ не удалось сопоставить: {e}", "33"))

    # Сборка строк таблицы
    rows: list[doc_generator.DisciplineRow] = []
    program_code = doc_generator.derive_program(app.group)
    for di, disc in enumerate(app.disciplines):
        courses = [
            doc_generator.CourseEntry(
                course_title=c.course_title,
                provider=c.provider,
                hours=c.hours,
                grade=c.grade,
                date=c.date,
            )
            for ci, c in enumerate(result.certificates)
            if mapping.get(ci, -1) == di
        ]
        rows.append(doc_generator.DisciplineRow(
            discipline=disc,
            plan_credits="5",
            courses=courses,
            compliance="полное",
        ))

    # Сертификаты, которые не привязались — отдельной строкой "не сопоставлено"
    unmapped = [
        c for ci, c in enumerate(result.certificates)
        if mapping.get(ci, -1) < 0 or mapping.get(ci, -1) >= len(app.disciplines)
    ]
    if unmapped:
        rows.append(doc_generator.DisciplineRow(
            discipline="(не сопоставлено)",
            courses=[
                doc_generator.CourseEntry(
                    course_title=c.course_title, provider=c.provider,
                    hours=c.hours, grade=c.grade, date=c.date,
                )
                for c in unmapped
            ],
            compliance="",
        ))

    if not rows:
        print(colored("   ⚠ нечего записывать — ни одной строки", "33"))
        return 1

    payload = doc_generator.ReportPayload(
        student_name=app.student_name,
        program_code=program_code,
        course_year=app.course_year,
        rows=rows,
    )
    docx_bytes = doc_generator.build_report(payload)
    out_path.write_bytes(docx_bytes)
    print(colored(f"   ✓ {out_path} ({len(docx_bytes) // 1024} KB)", "32"))

    if dump_json:
        json_path = out_path.with_suffix(".json")
        json_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(colored(f"   + {json_path}", "2"))

    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Распознавание сертификатов из PDF и генерация экспертного заключения.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("pdfs", nargs="+", help="один или несколько PDF файлов")
    p.add_argument("-o", "--output", help="имя выходного .docx (только при одном входе)")
    p.add_argument("-O", "--output-dir", default=".",
                   help="каталог для выходных файлов (имя берётся из ФИО студента)")
    p.add_argument("--no-mapping", action="store_true",
                   help="не использовать LLM для авто-сопоставления курс→дисциплина")
    p.add_argument("--json", action="store_true",
                   help="дополнительно сохранить распознанные данные в .json")
    p.add_argument(
        "--provider",
        choices=["gemini", "openrouter", "auto"],
        default="auto",
        help="какой провайдер использовать: 'gemini' — только Gemini, "
             "'openrouter' — только OpenRouter, 'auto' — оба с фейловером (по умолчанию)",
    )
    args = p.parse_args()

    if args.provider == "gemini":
        os.environ["PROVIDERS"] = "gemini"
    elif args.provider == "openrouter":
        os.environ["PROVIDERS"] = "openrouter"
    # переинициализируем порядок провайдеров в модуле analyzer
    analyzer.PROVIDER_ORDER = [
        p.strip().lower()
        for p in os.environ.get("PROVIDERS", "openrouter,gemini").split(",")
        if p.strip()
    ]

    # Проверка нужных ключей в зависимости от провайдера
    need_gemini = "gemini" in analyzer.PROVIDER_ORDER
    need_openrouter = "openrouter" in analyzer.PROVIDER_ORDER
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openrouter = bool(os.environ.get("OPENROUTER_API_KEY"))

    if need_gemini and need_openrouter and not (has_gemini or has_openrouter):
        print(colored("ERROR: ни OPENROUTER_API_KEY, ни GEMINI_API_KEY не заданы.", "31"), file=sys.stderr)
        print(
            "Получить ключ:\n"
            "  OpenRouter: https://openrouter.ai/keys\n"
            "  Gemini:     https://aistudio.google.com/apikey",
            file=sys.stderr,
        )
        return 2
    if analyzer.PROVIDER_ORDER == ["gemini"] and not has_gemini:
        print(colored("ERROR: GEMINI_API_KEY не задан (--provider gemini)", "31"), file=sys.stderr)
        return 2
    if analyzer.PROVIDER_ORDER == ["openrouter"] and not has_openrouter:
        print(colored("ERROR: OPENROUTER_API_KEY не задан (--provider openrouter)", "31"), file=sys.stderr)
        return 2

    print(colored(f"провайдеры: {', '.join(analyzer.PROVIDER_ORDER)}", "2"))

    pdf_paths = [Path(p) for p in args.pdfs]
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.output and len(pdf_paths) > 1:
        print(colored("ERROR: -o работает только с одним входным файлом, используйте -O dir", "31"))
        return 2

    failed = 0
    for pdf in pdf_paths:
        if args.output and len(pdf_paths) == 1:
            out_path = Path(args.output).resolve()
        else:
            tmp_result_stem = f"ЭЗ_{safe_filename(pdf.stem)}"
            out_path = out_dir / f"{tmp_result_stem}.docx"

        rc = process_one(
            pdf, out_path,
            auto_map=not args.no_mapping,
            dump_json=args.json,
        )
        if rc != 0:
            failed += 1

    if failed:
        print(colored(f"\n{failed} из {len(pdf_paths)} файлов с ошибками", "31"))
        return 1
    print(colored(f"\nГотово: {len(pdf_paths)} файл(ов)", "32"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
