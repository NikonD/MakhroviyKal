"""Дебаг — пробуем каждую free vision модель OpenRouter на 1 странице."""
import os
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass

import fitz
from analyzer import (
    APPLICATION_PROMPT,
    CERTIFICATE_PROMPT,
    OPENROUTER_MODELS,
    OpenRouterProvider,
)

pdf_path = Path(sys.argv[1])
page_idx = int(sys.argv[2])  # 1-based

with fitz.open(pdf_path) as doc:
    page = doc[page_idx - 1]
    png = page.get_pixmap(dpi=150).tobytes("png")
    print(f"Image: {len(png)} bytes, page rotation: {page.rotation}", file=sys.stderr)

prompt = APPLICATION_PROMPT if page_idx == 1 else CERTIFICATE_PROMPT

key = os.environ.get("OPENROUTER_API_KEY")
if not key:
    print("ERROR: OPENROUTER_API_KEY not set", file=sys.stderr)
    sys.exit(1)

print(f"\nTrying {len(OPENROUTER_MODELS)} models one by one:\n")
for model in OPENROUTER_MODELS:
    print(f"=== {model} ===")
    provider = OpenRouterProvider(api_key=key, models=[model])
    t0 = time.time()
    try:
        data = provider.analyze(prompt, png)
        elapsed = time.time() - t0
        print(f"  OK in {elapsed:.1f}s")
        import json
        print(json.dumps(data, ensure_ascii=False, indent=2)[:800])
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  FAIL in {elapsed:.1f}s: {str(e)[:200]}")
    print()
