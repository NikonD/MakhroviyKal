"""
PDF analyzer с мульти-провайдером для распознавания изображений.

Провайдеры (пробуются в порядке env-переменной `PROVIDERS`, по умолчанию
"openrouter,gemini"):
  - openrouter — OpenRouter API, пробует список бесплатных vision-моделей,
                 пока одна не сработает (env: OPENROUTER_API_KEY)
  - gemini     — Google Gemini Vision API (env: GEMINI_API_KEY)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol

import fitz  # PyMuPDF
import httpx

# Gemini SDK — опциональный
try:
    from google import genai
    from google.genai import types as genai_types
    _HAS_GEMINI = True
except ImportError:
    _HAS_GEMINI = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROVIDER_ORDER = [
    p.strip().lower()
    for p in os.environ.get("PROVIDERS", "gemini,openrouter").split(",")
    if p.strip()
]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
OPENROUTER_MODELS = [
    m.strip()
    for m in os.environ.get(
        "OPENROUTER_MODELS",
        # Актуальные бесплатные vision-модели OpenRouter (May 2026)
        # Порядок установлен по результатам тестов на реальных сертификатах:
        "google/gemma-4-26b-a4b-it:free,"
        "google/gemma-4-31b-it:free,"
        "nvidia/nemotron-nano-12b-v2-vl:free,"
        "baidu/qianfan-ocr-fast:free",
    ).split(",")
    if m.strip()
]
RENDER_DPI = int(os.environ.get("RENDER_DPI", "150"))
MAX_WORKERS = int(os.environ.get("ANALYZER_WORKERS", "6"))
MAX_RETRIES = int(os.environ.get("ANALYZER_RETRIES", "3"))

log = logging.getLogger("analyzer")
if not log.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[analyzer] %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Certificate:
    page: int
    course_title: str = ""
    provider: str = ""
    hours: str = ""
    grade: str = ""
    date: str = ""
    student_name: str = ""
    certificate_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Application:
    student_name: str = ""
    group: str = ""
    course_year: str = ""
    phone: str = ""
    disciplines: list[str] = field(default_factory=list)
    application_date: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    application: Application
    certificates: list[Certificate]
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "application": asdict(self.application),
            "certificates": [asdict(c) for c in self.certificates],
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
APPLICATION_PROMPT = """Перед тобой ЗАЯВЛЕНИЕ студента ВУЗа (русский язык). Печатная форма + рукописные вставки.
Тщательно распознай рукописные поля и верни СТРОГО валидный JSON (без markdown, без комментариев):

{
  "student_name": "Фамилия Имя Отчество — аккуратно как написано рукой (фио может быть написано не в одну строку, а в несколько)",
  "group": "номер группы, например 'АПО-23', 'ИС-у-25'",
  "course_year": "курс, цифра как строка, например '3'",
  "phone": "телефон как написан",
  "application_date": "дата заявления, например '04.05.2026'",
  "disciplines": ["дисциплина 1", "дисциплина 2"]
}

Правила:
- ФИО в этой форме написано после слова "ФИО" (может занимать несколько строк) — внимательно прочитай рукописный текст. Имя из заявления является приоритетным. Если буквы абсолютно не ясны — оставь пустую строку.
- Дисциплины — пронумерованный список (1-5) после фразы "Прошу Вас признать результаты неформального обучения, по дисциплинам:". Распознай каждую написанную строку. Опечатки рукописного текста исправляй на корректные академические названия (например "Обектно ориентрованное программирование" -> "Объектно-ориентированное программирование", если написано неразборчиво похоже на "Программные средства информационных систем" -> "Графические средства информационных систем", "Композиционное моделирование вычислительных систем" -> "Компьютерное моделирование вычислительных систем").
- Если поле не разобрать — оставь пустую строку. НЕ ВЫДУМЫВАЙ.
- Никаких комментариев и markdown — только JSON."""


CERTIFICATE_PROMPT = """На изображении — сертификат о прохождении онлайн-курса (Coursera, Simplilearn, Codio, Google, ИНТУИТ, Stepik и т.п.).
Изображение может быть повёрнуто на 90° — мысленно поверни и читай.

Верни СТРОГО валидный JSON (без markdown, без комментариев):

{
  "is_certificate": true,
  "course_title": "точное название курса как на сертификате",
  "provider": "платформа выдачи (Coursera, Simplilearn, Codio, Google, ИНТУИТ, Stepik, ...)",
  "hours": "количество часов как строка, например '29 часов', '8 ч', '72 часа'. Если не указано — пустая строка.",
  "grade": "оценка/балл если есть, например '92%', '88.09%', '4.4'. Если нет — пустая строка.",
  "date": "дата выдачи или период обучения как написано",
  "student_name": "ФИО владельца сертификата",
  "certificate_id": "номер/ID сертификата если указан"
}

Правила:
- Если на странице несколько визуальных блоков (например слева скриншот Coursera-страницы курса, а справа сам сертификат) — извлекай данные из обоих и склеивай: название курса бери одинаковое, часы из скриншота описания курса, провайдер по логотипу (Simplilearn/Codio/Google/...).
- Если название содержит "Композиционное моделирование вычислительных систем", обязательно исправь на "Компьютерное моделирование вычислительных систем".
- is_certificate=false ТОЛЬКО если изображение явно не сертификат.
- Никаких комментариев и markdown — только JSON."""


# ---------------------------------------------------------------------------
# Provider interface and implementations
# ---------------------------------------------------------------------------
class QuotaError(RuntimeError):
    """Дневной/общий лимит провайдера исчерпан — фейловерим на следующий."""


class TransientError(RuntimeError):
    """Временная ошибка (rate limit, 5xx) — стоит подождать или попробовать другую модель."""


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    # Случай: ответ обрезан посередине — пытаемся восстановить простым закрытием скобок
    start = text.find("{")
    if start >= 0:
        partial = text[start:]
        for closer in ('"}', '"', '}', '"}}', '"}}}',):
            try:
                return json.loads(partial + closer)
            except json.JSONDecodeError:
                continue
        # Регекс-фоллбэк: ищем "key": "value" пары
        pairs = re.findall(r'"([^"]+)"\s*:\s*"([^"\n]*?)(?:"|$)', partial)
        if pairs:
            return {k: v for k, v in pairs}
    raise ValueError(f"No JSON found in response: {text[:200]}")


class VisionProvider(Protocol):
    name: str

    def analyze(self, prompt: str, png_bytes: bytes) -> dict[str, Any]:
        ...

    def is_available(self) -> bool:
        ...


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str | None = None, model: str = GEMINI_MODEL):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model
        self.daily_quota_hit = False
        self._client: Any = None

    def is_available(self) -> bool:
        return _HAS_GEMINI and bool(self.api_key) and not self.daily_quota_hit

    def _client_lazy(self):
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def analyze(self, prompt: str, png_bytes: bytes) -> dict[str, Any]:
        if self.daily_quota_hit:
            raise QuotaError("Gemini daily quota already exhausted")
        client = self._client_lazy()
        try:
            response = client.models.generate_content(
                model=self.model,
                contents=[
                    genai_types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
                    prompt,
                ],
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                    max_output_tokens=2000,
                ),
            )
            return _extract_json(response.text or "")
        except Exception as e:
            msg = str(e)
            low = msg.lower()
            if "PerDay" in msg or "RequestsPerDay" in msg:
                self.daily_quota_hit = True
                raise QuotaError(f"Gemini daily quota exhausted: {msg[:200]}") from e
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                raise TransientError(f"Gemini rate limit: {msg[:200]}") from e
            if "503" in msg or "UNAVAILABLE" in msg:
                raise TransientError(f"Gemini unavailable: {msg[:200]}") from e
            # SSL / network / connection — транзиентно, имеет смысл повторить
            transient_markers = (
                "ssl", "eof", "timeout", "timed out", "connection",
                "remote_disconnected", "bad record mac", "broken pipe",
                "reset by peer", "connection refused", "name or service",
            )
            if any(k in low for k in transient_markers):
                raise TransientError(f"Gemini network error: {msg[:200]}") from e
            raise


class OpenRouterProvider:
    """OpenRouter — пробует список бесплатных vision-моделей последовательно."""

    name = "openrouter"
    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
    MODELS_ENDPOINT = "https://openrouter.ai/api/v1/models"

    # Глобальные кэши на уровне процесса
    _dead_models_global: set[str] = set()
    _cooldown_until: dict[str, float] = {}  # model -> unix timestamp когда снова можно
    _COOLDOWN_SECONDS = float(os.environ.get("OPENROUTER_COOLDOWN", "30"))

    def __init__(self, api_key: str | None = None, models: list[str] | None = None):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.models = models or list(OPENROUTER_MODELS)
        self.dead_models: set[str] = set(self._dead_models_global)

    @classmethod
    def discover_free_vision_models(cls, api_key: str) -> list[str]:
        """Запрашивает у OpenRouter актуальный список бесплатных vision-моделей."""
        try:
            with httpx.Client(timeout=15.0) as c:
                r = c.get(
                    cls.MODELS_ENDPOINT,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                r.raise_for_status()
                data = r.json().get("data", [])
        except Exception as e:
            log.warning("model discovery failed: %s", e)
            return []
        found: list[str] = []
        for m in data:
            pricing = m.get("pricing", {}) or {}
            arch = m.get("architecture", {}) or {}
            modalities = arch.get("input_modalities") or [arch.get("modality", "")]
            has_image = any("image" in str(x).lower() for x in modalities)
            try:
                prompt_p = float(pricing.get("prompt", 1))
                completion_p = float(pricing.get("completion", 1))
            except (TypeError, ValueError):
                continue
            if has_image and prompt_p == 0 and completion_p == 0:
                mid = m.get("id", "")
                # отфильтровываем явно музыкальные/неподходящие
                if mid and "lyria" not in mid.lower() and "music" not in mid.lower():
                    found.append(mid)
        return found

    def _mark_dead(self, model: str) -> None:
        self.dead_models.add(model)
        type(self)._dead_models_global.add(model)

    def _is_cooled(self, model: str) -> bool:
        """True если модель ещё на cooldown — пока пропустить."""
        until = self._cooldown_until.get(model, 0.0)
        return time.time() < until

    def _put_cooldown(self, model: str, seconds: float | None = None) -> None:
        s = seconds if seconds is not None else self._COOLDOWN_SECONDS
        self._cooldown_until[model] = time.time() + s

    def is_available(self) -> bool:
        if not self.api_key or not self.models:
            return False
        return any(
            (m not in self.dead_models) and not self._is_cooled(m) for m in self.models
        )

    def _alive_models(self) -> list[str]:
        return [
            m for m in self.models
            if m not in self.dead_models and not self._is_cooled(m)
        ]

    def analyze(self, prompt: str, png_bytes: bytes) -> dict[str, Any]:
        models = self._alive_models()
        if not models:
            raise QuotaError("All OpenRouter free models exhausted")

        b64 = base64.b64encode(png_bytes).decode("ascii")
        image_url = f"data:image/png;base64,{b64}"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/expert-conclusion-app",
            "X-Title": "Expert Conclusion Generator",
        }
        last_err: Exception | None = None
        for model in models:
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": image_url}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                "temperature": 0.1,
                "max_tokens": 4000,
                # Просим JSON-формат (поддерживается не всеми моделями, но не навредит)
                "response_format": {"type": "json_object"},
            }
            try:
                with httpx.Client(timeout=120.0) as client:
                    r = client.post(self.ENDPOINT, headers=headers, json=payload)
            except (httpx.TimeoutException, httpx.HTTPError) as e:
                last_err = TransientError(f"{model}: network error {e}")
                log.warning("OpenRouter network error on %s: %s", model, e)
                continue

            if r.status_code == 200:
                try:
                    data = r.json()
                    msg = data["choices"][0]["message"]
                    content = msg.get("content") or msg.get("reasoning") or ""
                    if not content or not content.strip():
                        last_err = TransientError(f"{model}: empty response")
                        log.warning("OpenRouter %s: empty content", model)
                        continue
                    parsed = _extract_json(content)
                    log.info("OpenRouter %s OK", model)
                    return parsed
                except (KeyError, IndexError, ValueError, json.JSONDecodeError) as e:
                    last_err = TransientError(f"{model}: bad response {e}")
                    log.warning("OpenRouter %s: bad response: %s", model, e)
                    continue

            # ошибки HTTP
            body = r.text[:300]
            if r.status_code == 401:
                raise QuotaError(f"OpenRouter: invalid API key (401)")
            if r.status_code == 402:
                log.warning("OpenRouter %s: payment required (402), dropping", model)
                self._mark_dead(model)
                last_err = QuotaError(f"{model}: 402 payment required")
                continue
            if r.status_code == 404:
                log.warning("OpenRouter %s: 404 (no endpoints), dropping permanently", model)
                self._mark_dead(model)
                last_err = QuotaError(f"{model}: 404 no endpoints")
                continue
            if r.status_code == 400 and ("image" in body.lower() or "modal" in body.lower() or "unsupported" in body.lower()):
                log.warning("OpenRouter %s: 400 (doesn't accept images), dropping", model)
                self._mark_dead(model)
                last_err = QuotaError(f"{model}: doesn't accept images")
                continue
            if r.status_code == 429:
                if "daily" in body.lower() or "per day" in body.lower():
                    log.warning("OpenRouter %s: daily quota exhausted, dropping", model)
                    self._mark_dead(model)
                else:
                    # rate-limit upstream — ставим cooldown, дальше эту модель не пытаемся
                    self._put_cooldown(model)
                    log.warning(
                        "OpenRouter %s: 429 rate-limited, cooldown %.0fs",
                        model, self._COOLDOWN_SECONDS,
                    )
                last_err = TransientError(f"{model}: 429")
                continue
            if r.status_code in (502, 503, 504):
                log.warning("OpenRouter %s: %d (transient)", model, r.status_code)
                last_err = TransientError(f"{model}: {r.status_code}")
                continue
            log.warning("OpenRouter %s: HTTP %d: %s", model, r.status_code, body)
            last_err = TransientError(f"{model}: HTTP {r.status_code}: {body}")
        # Все модели не сработали
        if all(m in self.dead_models for m in self.models):
            raise QuotaError(f"All OpenRouter models exhausted: {last_err}")
        raise TransientError(f"All OpenRouter models failed this round: {last_err}")


# ---------------------------------------------------------------------------
# Provider chain
# ---------------------------------------------------------------------------
def _build_providers(
    *,
    gemini_key: str | None = None,
    openrouter_key: str | None = None,
) -> list[VisionProvider]:
    available: list[VisionProvider] = []
    for name in PROVIDER_ORDER:
        if name == "gemini":
            p = GeminiProvider(api_key=gemini_key)
            if p.is_available():
                available.append(p)
        elif name == "openrouter":
            p = OpenRouterProvider(api_key=openrouter_key)
            if p.is_available():
                available.append(p)
    return available


def _call_with_failover(
    providers: list[VisionProvider],
    prompt: str,
    png_bytes: bytes,
    *,
    require_nonempty: tuple[str, ...] = (),
    label: str = "",
) -> dict[str, Any]:
    """Перебирает провайдеров; на каждом — несколько retry на transient ошибки."""
    last_err: Exception | None = None
    last_data: dict[str, Any] | None = None
    for p in providers:
        if not p.is_available():
            continue
        for attempt in range(MAX_RETRIES + 1):
            t0 = time.time()
            try:
                data = p.analyze(prompt, png_bytes)
                elapsed = time.time() - t0
                if require_nonempty and all(
                    not str(data.get(k, "")).strip() for k in require_nonempty
                ):
                    log.info(
                        "%s [%s] attempt %d: empty %s (%.1fs)",
                        label, p.name, attempt + 1, require_nonempty, elapsed,
                    )
                    last_data = data
                    time.sleep(1.0)
                    continue
                log.info("%s [%s] OK in %.1fs (attempt %d)",
                         label, p.name, elapsed, attempt + 1)
                return data
            except QuotaError as e:
                log.warning("%s [%s]: quota exhausted: %s", label, p.name, e)
                last_err = e
                break  # переходим к следующему провайдеру
            except TransientError as e:
                elapsed = time.time() - t0
                msg = str(e)
                delay = 1.0 + attempt * 2.0
                m = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", msg, re.IGNORECASE)
                if m:
                    delay = min(30.0, float(m.group(1)) + 0.5)
                log.warning(
                    "%s [%s] attempt %d failed in %.1fs (sleeping %.1fs): %s",
                    label, p.name, attempt + 1, elapsed, delay, msg[:200],
                )
                last_err = e
                if attempt < MAX_RETRIES:
                    time.sleep(delay)
            except Exception as e:
                # Любое непредвиденное исключение трактуем как транзиентное
                # (SSL, разрыв соединения, временные сбои SDK), даём шанс ретраю
                elapsed = time.time() - t0
                msg = str(e)
                delay = 2.0 + attempt * 2.0
                log.warning(
                    "%s [%s] attempt %d unexpected error in %.1fs (sleeping %.1fs): %s",
                    label, p.name, attempt + 1, elapsed, delay, msg[:200],
                )
                last_err = e
                if attempt < MAX_RETRIES:
                    time.sleep(delay)
    if last_data is not None:
        log.warning("%s: returning best-effort partial data", label)
        return last_data
    raise last_err or RuntimeError("All providers failed")


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------
def render_pdf_pages(pdf_bytes: bytes, dpi: int = RENDER_DPI) -> list[bytes]:
    images: list[bytes] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            images.append(pix.tobytes("png"))
    return images


# ---------------------------------------------------------------------------
# High-level analysis
# ---------------------------------------------------------------------------
def _short_err(e: Exception) -> str:
    msg = str(e)
    if "quota" in msg.lower() or "RESOURCE_EXHAUSTED" in msg or "429" in msg:
        if "PerDay" in msg or "daily" in msg.lower():
            return "Исчерпан дневной лимит провайдера"
        return "Лимит провайдера временно исчерпан"
    if "503" in msg or "UNAVAILABLE" in msg:
        return "Vision-API временно недоступен"
    if "401" in msg or "invalid API key" in msg:
        return "Невалидный API ключ"
    return msg[:200]


def analyze_pdf(
    pdf_bytes: bytes,
    *,
    api_key: str | None = None,           # back-compat: считается как Gemini key
    gemini_api_key: str | None = None,
    openrouter_api_key: str | None = None,
    on_progress: Callable[[str, dict], None] | None = None,  # callback для прогресса
) -> AnalysisResult:
    """Анализирует PDF: 1 страница → заявление, остальные → сертификаты."""
    gemini_key = gemini_api_key or api_key
    providers = _build_providers(
        gemini_key=gemini_key,
        openrouter_key=openrouter_api_key,
    )
    if not providers:
        raise RuntimeError(
            "Ни один провайдер не настроен. Задайте OPENROUTER_API_KEY или GEMINI_API_KEY."
        )

    log.info("providers chain: %s", [p.name for p in providers])

    pages = render_pdf_pages(pdf_bytes)
    if not pages:
        return AnalysisResult(Application(), [])

    errors: list[str] = []
    log.info("analyzing PDF: %d pages, dpi=%d, workers=%d",
             len(pages), RENDER_DPI, MAX_WORKERS)

    if on_progress:
        on_progress("start", {"total_pages": len(pages)})

    from concurrent.futures import as_completed
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        app_future = ex.submit(
            _call_with_failover, providers, APPLICATION_PROMPT, pages[0],
            label="app-page",
        )
        cert_futures = {
            ex.submit(
                _call_with_failover, providers, CERTIFICATE_PROMPT, page_bytes,
                require_nonempty=("course_title",),
                label=f"cert-page-{idx}",
            ): idx
            for idx, page_bytes in enumerate(pages[1:], start=2)
        }
        all_futures = {app_future: 1, **cert_futures}
        results: dict[int, dict[str, Any] | None] = {}
        done_count = 0
        for fut in as_completed(all_futures):
            idx = all_futures[fut]
            done_count += 1
            try:
                results[idx] = fut.result()
                if on_progress:
                    on_progress("page_done", {
                        "page": idx, "done": done_count, "total": len(pages),
                        "ok": True,
                    })
            except Exception as e:
                results[idx] = None
                kind = "Заявление" if idx == 1 else "Сертификат"
                errors.append(f"{kind} (стр. {idx}): {_short_err(e)}")
                log.error("page %d failed: %s", idx, e)
                if on_progress:
                    on_progress("page_done", {
                        "page": idx, "done": done_count, "total": len(pages),
                        "ok": False, "error": _short_err(e),
                    })

        app_data = results.get(1) or {}
        certs: list[Certificate] = []
        for idx in sorted(k for k in results if k >= 2):
            data = results[idx]
            if data is None:
                continue
            if data.get("is_certificate") is False:
                continue
            cert = Certificate(
                page=idx,
                course_title=str(data.get("course_title", "")).strip(),
                provider=str(data.get("provider", "")).strip(),
                hours=str(data.get("hours", "")).strip(),
                grade=str(data.get("grade", "")).strip(),
                date=str(data.get("date", "")).strip(),
                student_name=str(data.get("student_name", "")).strip(),
                certificate_id=str(data.get("certificate_id", "")).strip(),
                raw=data,
            )
            certs.append(cert)
            if not cert.course_title:
                errors.append(
                    f"Сертификат на стр. {idx}: не удалось распознать название курса"
                )

    application = Application(
        student_name=str(app_data.get("student_name", "")).strip(),
        group=str(app_data.get("group", "")).strip(),
        course_year=str(app_data.get("course_year", "")).strip(),
        phone=str(app_data.get("phone", "")).strip(),
        application_date=str(app_data.get("application_date", "")).strip(),
        disciplines=[
            d.strip() for d in app_data.get("disciplines", []) if str(d).strip()
        ],
        raw=app_data,
    )

    cert_name = _consensus_name([c.student_name for c in certs])
    if cert_name:
        application.student_name = cert_name
        log.info(f"ФИО студента распознано из сертификатов: {application.student_name}")
        print(f"ФИО студента распознано из сертификатов: {application.student_name}")
    else:
        log.warning("ФИО студента не удалось распознать ни из заявления, ни из сертификатов.")
        print("ФИО студента не удалось распознать ни из заявления, ни из сертификатов.")

    return AnalysisResult(application=application, certificates=certs, errors=errors)


# ---------------------------------------------------------------------------
# Suggest mapping cert -> discipline (текстовая задача, не нужен vision)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Локальный (без LLM) маппинг сертификат → дисциплина по ключевым словам.
# Используется как fallback когда LLM недоступен или квота исчерпана.
# ---------------------------------------------------------------------------
# Тематические группы. Каждая группа — list of ru/en маркеров.
# Курс попадает в группу X, дисциплина попадает в группу Y → если X==Y, score+5.
_TOPIC_GROUPS: dict[str, tuple[str, ...]] = {
    "design_ui": (
        "figma", "ui", "ux", "wireframing", "wireframe", "prototype",
        "prototyping", "design", "дизайн", "интерфейс", "проектирование",
        "web design", "веб-дизайн", "графические", "графика", "graphic", "adobe",
    ),
    "data": (
        "data analysis", "data analytics", "data science", "анализ данных",
        "статистика", "статистический", "machine learning", "машинное обучение",
        "ml", "ai", "pandas", "numpy", "powerbi", "tableau", "excel",
    ),
    "programming": (
        "python", "java ", "javascript", "c++", "c#", "golang", "kotlin",
        "swift", "программирование", "разработ", "ооп", "object-oriented",
        "objectoriented", "алгоритм", "code", "coding",
    ),
    "web": (
        "html", "css", "react", "angular", "vue", "frontend", "backend",
        "node.js", "веб-приложен", "веб-разработ", "web development",
        "django", "flask", "express", "веб-",
    ),
    "ict": (
        "икт", "информационно-коммуникацион", "коммуникационн",
        "информационно коммуникац", "ict", "сетевые технологии",
    ),
    "industrial": (
        "промышленн", "автоматизац", "industrial", "scada", "plc",
        "оптимизац", "linear programming", "optimization",
    ),
    "is": (
        "информационные системы", "is ", "ис ", "проектирование систем",
        "архитектура систем",
    ),
    "math": (
        "matemati", "матема", "calculus", "algebra", "discrete",
    ),
    "english": (
        "english", "ielts", "toefl", "английск",
    ),
    "db": (
        "database", "sql", "postgres", "mysql", "mongo", "база данных", "бд ",
    ),
    "security": (
        "security", "cybersec", "кибербез", "безопасност",
    ),
}

_STOP_WORDS = {
    "и", "в", "на", "с", "по", "для", "от", "к", "о", "у",
    "the", "of", "for", "and", "in", "to", "with", "a", "an",
    "&", "—", "-", "—",
}


def _topic_of(text: str) -> str | None:
    """Возвращает имя темы, к которой относится текст, либо None."""
    s = text.lower()
    best: tuple[int, str | None] = (0, None)
    for topic, markers in _TOPIC_GROUPS.items():
        score = sum(1 for m in markers if m in s)
        if score > best[0]:
            best = (score, topic)
    return best[1]


def _tokens(text: str) -> set[str]:
    s = text.lower()
    raw = re.split(r"[\s\-\.,;:!?()\[\]/\\«»\"'’]+", s)
    return {t for t in raw if len(t) > 3 and t not in _STOP_WORDS}


def _local_keyword_mapping(
    disciplines: list[str], certificates: list[Certificate],
) -> dict[int, int]:
    """Сопоставляет cert→disc по ключевым словам + тематическим группам.

    Возвращает {cert_index: disc_index | -1}. Каждый сертификат
    привязывается к лучшей дисциплине; в одну дисциплину может попасть
    несколько сертификатов.
    """
    if not disciplines or not certificates:
        return {i: -1 for i in range(len(certificates))}

    disc_tokens = [_tokens(d) for d in disciplines]
    disc_topics = [_topic_of(d) for d in disciplines]
    mapping: dict[int, int] = {}
    for ci, cert in enumerate(certificates):
        cert_text = f"{cert.course_title} {cert.provider}"
        c_tokens = _tokens(cert_text)
        c_topic = _topic_of(cert_text)
        best_score = 0
        best_di = -1
        for di, _disc in enumerate(disciplines):
            overlap = len(c_tokens & disc_tokens[di])
            score = overlap * 2
            if c_topic and disc_topics[di] and c_topic == disc_topics[di]:
                score += 5
            if score > best_score:
                best_score = score
                best_di = di
        # минимальный порог: либо общая тема, либо хотя бы одно общее слово
        mapping[ci] = best_di if best_score >= 2 else -1
    return mapping


def suggest_mapping(
    result: AnalysisResult,
    *,
    api_key: str | None = None,
    gemini_api_key: str | None = None,
    openrouter_api_key: str | None = None,
) -> dict[int, int]:
    disciplines = result.application.disciplines
    if not disciplines or not result.certificates:
        return {}

    gemini_key = gemini_api_key or api_key
    providers = _build_providers(
        gemini_key=gemini_key, openrouter_key=openrouter_api_key
    )
    if not providers:
        log.info("mapping: no providers, using local keyword mapping")
        return _local_keyword_mapping(disciplines, result.certificates)

    cert_lines = [
        f"  {i}: \"{c.course_title}\" — {c.provider} ({c.hours})"
        for i, c in enumerate(result.certificates)
    ]
    disc_lines = [f"  {i}: {d}" for i, d in enumerate(disciplines)]
    prompt = (
        "У студента список дисциплин учебного плана и список пройденных онлайн-курсов "
        "(сертификатов). Сопоставь каждый сертификат с одной подходящей дисциплиной "
        "по смыслу. Один сертификат — одна дисциплина. Если ни одна не подходит — -1.\n\n"
        f"Дисциплины:\n{chr(10).join(disc_lines)}\n\n"
        f"Сертификаты:\n{chr(10).join(cert_lines)}\n\n"
        'Верни СТРОГО JSON: {"mapping": [индекс_для_сертификата_0, ...]} '
        "(длина массива = числу сертификатов)."
    )

    # Используем 1×1 пустую PNG-картинку чтобы переиспользовать инфраструктуру provider'ов
    # Однако для текста проще вызвать только Gemini напрямую (если есть) или OpenRouter
    for p in providers:
        try:
            if isinstance(p, GeminiProvider) and p.is_available():
                client = p._client_lazy()
                response = client.models.generate_content(
                    model=p.model,
                    contents=[prompt],
                    config=genai_types.GenerateContentConfig(
                        response_mime_type="application/json", temperature=0.0,
                    ),
                )
                data = _extract_json(response.text or "")
                mapping_list = data.get("mapping", [])
                return {i: int(v) for i, v in enumerate(mapping_list)}
            if isinstance(p, OpenRouterProvider) and p.is_available():
                headers = {
                    "Authorization": f"Bearer {p.api_key}",
                    "Content-Type": "application/json",
                }
                for model in p._alive_models():
                    try:
                        with httpx.Client(timeout=60.0) as client:
                            r = client.post(
                                p.ENDPOINT,
                                headers=headers,
                                json={
                                    "model": model,
                                    "messages": [{"role": "user", "content": prompt}],
                                    "temperature": 0.0,
                                    "max_tokens": 500,
                                    "response_format": {"type": "json_object"},
                                },
                            )
                        if r.status_code == 200:
                            content = r.json()["choices"][0]["message"]["content"]
                            data = _extract_json(content)
                            return {i: int(v) for i, v in enumerate(data.get("mapping", []))}
                    except Exception:
                        continue
        except Exception as e:
            log.warning("mapping via %s failed: %s", p.name, e)
            continue
    log.info("mapping: all LLM providers failed, falling back to keyword mapping")
    return _local_keyword_mapping(disciplines, result.certificates)


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()


def _consensus_name(names: list[str]) -> str:
    cleaned = [_normalize_name(n) for n in names if n and _normalize_name(n)]
    if not cleaned:
        return ""
    counter = Counter(cleaned)
    most_common, _ = counter.most_common(1)[0]
    return most_common
