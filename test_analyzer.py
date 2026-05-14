"""Быстрый интеграционный тест analyzer.py на реальных PDF."""
import json
import sys
import time
from pathlib import Path

from analyzer import analyze_pdf, suggest_mapping


def main():
    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if not pdf_path or not pdf_path.exists():
        print(f"Usage: python test_analyzer.py <path.pdf>", file=sys.stderr)
        sys.exit(1)

    print(f"Analyzing: {pdf_path}", file=sys.stderr)
    t0 = time.time()
    result = analyze_pdf(pdf_path.read_bytes())
    t1 = time.time()
    print(f"Took {t1 - t0:.1f}s", file=sys.stderr)

    print(f"\n=== APPLICATION ===")
    print(f"  ФИО:     {result.application.student_name}")
    print(f"  Группа:  {result.application.group}")
    print(f"  Курс:    {result.application.course_year}")
    print(f"  Телефон: {result.application.phone}")
    print(f"  Дата:    {result.application.application_date}")
    print(f"  Дисциплины:")
    for i, d in enumerate(result.application.disciplines, 1):
        print(f"    {i}. {d}")

    print(f"\n=== CERTIFICATES ({len(result.certificates)}) ===")
    for c in result.certificates:
        print(
            f"  стр.{c.page:>2}: {c.course_title!r:50} | {c.provider:15} | "
            f"{c.hours:12} | оценка: {c.grade!r}"
        )

    print(f"\n=== MAPPING ===")
    t0 = time.time()
    mapping = suggest_mapping(result)
    t1 = time.time()
    print(f"(took {t1 - t0:.1f}s)")
    for cert_i, disc_i in mapping.items():
        cert = result.certificates[cert_i]
        if disc_i == -1 or disc_i >= len(result.application.disciplines):
            disc = "(не сопоставлено)"
        else:
            disc = result.application.disciplines[disc_i]
        print(f"  [{cert_i}] {cert.course_title[:40]!r:42} -> {disc}")


if __name__ == "__main__":
    main()
