"""Gate-раннер спайка Ozon (README §5, M1).

Гоняет выбранный публичный источник по списку SKU последовательно
(1 SKU = 1 запрос), интервал >= 2 с + джиттер, backoff 2^n (кап 30 мин)
на challenge/429. Пишет JSONL-лог и считает строгий gate-вердикт.

Запуск (из корня репо, venv активен):
    python scripts/spike_ozon.py --source identity --hours 24     # метод идентичности (M1)
    python scripts/spike_ozon.py --source composer --hours 24     # прямой источник (legacy)
    python scripts/spike_ozon.py --source identity --hours 0.01   # smoke, не gate
    python scripts/spike_ozon.py --resume                         # продолжить тот же run
    python scripts/spike_ozon.py --report                         # сводка по run
    python scripts/spike_ozon.py --probe                          # разовая проверка источников

Источники `identity` (curl_cffi-реплей идентичности, прошедшей антибот) и
`identity-inpage` (fetch из живой вкладки Chromium) используют транспорты
провайдера (src/providers/ozon_transport.py). Идентичность добывается браузером
на этом же хосте (extra [browser] + `patchright install chromium`) либо берётся
готовой из OZON_COOKIE/OZON_USER_AGENT — ОБЯЗАТЕЛЬНО с того же egress-IP, где
она получена. OZON_BROWSER_HEADLESS=false показывает окно браузера (отладка).

Прокси (опционально): OZON_PROXY_URL или --proxy. Флаг --no-proxy запрещает
прокси и игнорирует OZON_PROXY_URL. Без явно заданного прокси раннер также
отключает env HTTP(S)_PROXY через NOPROXY, чтобы случайный прокси не использовался.
В манифест пишется только факт наличия прокси, сам URL/секреты не логируются.

Важно: api.ozon.ru отдаёт сертификат УЦ Минцифры РФ — раннер собирает
CA-бандл certifi + scripts/certs/russian_trusted_root_ca.crt.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any, Literal

import certifi
from curl_cffi import requests
from curl_cffi.const import CurlOpt

from src.providers.ozon import classify_response
from src.providers.ozon_antibot import detect_antibot
from src.providers.ozon_browser import BrowserOptions
from src.providers.ozon_identity import (
    BrowserMinter,
    IdentityMinter,
    IdentityStore,
    StaticIdentityMinter,
)
from src.providers.ozon_page import PageParseError, parse_snapshot, product_not_found
from src.providers.ozon_provider import PAGE_API, product_url
from src.providers.ozon_transport import CurlIdentityTransport, InPageTransport, Transport

ROOT = Path(__file__).resolve().parent
BACKOFF_CAP_SECONDS = 1800.0
REQUEST_TIMEOUT_SECONDS = 15.0
GATE_THRESHOLD = 0.90

SourceKind = Literal["composer", "product", "identity"]


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    kind: SourceKind
    mobile: bool = False
    #: Для kind="identity": True — fetch из вкладки Chromium, False — curl-реплей.
    inpage: bool = False


SOURCES: dict[str, Source] = {
    "composer": Source("composer", "https://api.ozon.ru/composer-api.bx/page/json/v2", "composer"),
    "www-composer": Source(
        "www-composer", "https://www.ozon.ru/api/composer-api.bx/page/json/v2", "composer"
    ),
    "entrypoint": Source(
        "entrypoint", "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2", "composer"
    ),
    "api-entrypoint": Source(
        "api-entrypoint", "https://api.ozon.ru/entrypoint-api.bx/page/json/v2", "composer"
    ),
    "mobile-composer": Source(
        "mobile-composer", "https://api.ozon.ru/composer-api.bx/page/json/v2", "composer", True
    ),
    "mobile-entrypoint": Source(
        "mobile-entrypoint",
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2",
        "composer",
        True,
    ),
    "product": Source("product", "https://www.ozon.ru/product/{segment}/", "product"),
    "mobile-product": Source("mobile-product", "https://m.ozon.ru/product/{segment}/", "product"),
    # Метод идентичности (omkarcloud/ozon-scraper + eduard256/shopper-mcp):
    # браузер один раз проходит антибот, дальше запросы идут с его идентичностью.
    "identity": Source("identity", PAGE_API, "identity"),
    "identity-inpage": Source("identity-inpage", PAGE_API, "identity", inpage=True),
}
GATE_SOURCES = {name: src for name, src in SOURCES.items() if src.kind in ("composer", "identity")}

MOBILE_HEADERS = {
    "User-Agent": "ozonapp_android/16.28.1+2401",
    "X-O3-App-Name": "ozonapp_android",
    "X-O3-App-Version": "16.28.1(2401)",
    "X-O3-Device-Type": "mobile",
    "Accept": "application/json; charset=utf-8",
}

_STOP = False


def _handle_stop(signum: int, frame: FrameType | None) -> None:
    global _STOP
    _STOP = True


def _sleep(seconds: int | float) -> None:
    """Сон с возможностью прерваться по SIGTERM/SIGINT."""
    deadline = time.monotonic() + float(seconds)
    while not _STOP:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.5))


def build_ca_bundle(out_dir: Path) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = out_dir / "ca_bundle.pem"
    root_ca = ROOT / "certs" / "russian_trusted_root_ca.crt"
    bundle.write_text(
        Path(certifi.where()).read_text(encoding="utf-8")
        + "\n"
        + root_ca.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return str(bundle)


def load_segments(path: Path) -> list[str]:
    segments = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not segments:
        raise ValueError("SKU-лист пуст")
    skus = []
    for seg in segments:
        match = re.search(r"(\d+)$", seg)
        if not match:
            raise ValueError(f"сегмент без числового SKU: {seg!r}")
        skus.append(match.group(1))
    if len(set(skus)) != len(skus):
        raise ValueError("SKU-лист содержит дубликаты")
    return segments


def _sku(segment: str) -> str:
    match = re.search(r"(\d+)$", segment)
    if not match:
        raise ValueError(f"сегмент без числового SKU: {segment!r}")
    return match.group(1)


def _classify_attempt(sku: str, status: int, body: str) -> str:
    """Классификация попытки gate тем же парсером, что использует провайдер.

    Наличие `webPrice` само по себе не успех: gate требует полную карточку
    (цена, название, SKU запрошенного товара). Иначе сломанная вёрстка или
    редирект на другой товар могли бы дать ложный gate_passed=true.
    """
    if detect_antibot(status, body) is not None:
        return "challenge"
    if status == 404:
        return "dead"
    if status != 200:
        return classify_response(status, body)
    try:
        page = json.loads(body)
    except ValueError:
        return "error"
    if not isinstance(page, dict):
        return "error"
    # Явный error-виджет pdp — отсутствие товара даже без webPrice.
    if product_not_found(page):
        return "dead"
    if classify_response(status, body) != "success":
        return "error"
    try:
        parse_snapshot(sku, page)
    except PageParseError:
        return "error"
    return "success"


def _sha256(segments: list[str]) -> str:
    return hashlib.sha256("\n".join(segments).encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"ожидался JSON-объект: {path}")
    return data


def summarize(run_dir: Path) -> dict[str, Any]:
    """Строгая сводка gate. Не объявляет pass без 24 ч, покрытия и успеха по всем SKU."""
    manifest = _read_json(run_dir / "manifest.json")
    expected = set(manifest["skus"])
    run_id = manifest["run_id"]
    threshold = float(manifest.get("threshold", GATE_THRESHOLD))
    interval = float(manifest.get("min_interval_seconds", 2.0))
    hours = float(manifest["hours"])
    started = _parse_iso(manifest["started_at"])
    planned_end = _parse_iso(manifest["planned_end_at"])
    max_gap_limit = BACKOFF_CAP_SECONDS + interval + REQUEST_TIMEOUT_SECONDS + 15.0

    records: list[dict[str, Any]] = []
    invalid = 0
    foreign = 0
    log_path = run_dir / "gate_log.jsonl"
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                invalid += 1
                continue
            if not isinstance(rec, dict):
                invalid += 1
                continue
            if rec.get("run_id") != run_id:
                foreign += 1
                continue
            records.append(rec)

    counts: dict[str, int] = {}
    skus: set[str] = set()
    success_skus: set[str] = set()
    timestamps: list[datetime] = []
    for rec in records:
        cls = str(rec.get("cls", "error"))
        counts[cls] = counts.get(cls, 0) + 1
        sku = str(rec.get("sku", ""))
        if sku:
            skus.add(sku)
            if cls == "success":
                success_skus.add(sku)
        ts = rec.get("ts")
        if isinstance(ts, str):
            try:
                timestamps.append(_parse_iso(ts))
            except ValueError:
                invalid += 1
    timestamps.sort()
    total = len(records)
    success = counts.get("success", 0)
    rate = success / total if total else 0.0
    max_gap = 0.0
    for prev, cur in zip(timestamps, timestamps[1:], strict=False):
        max_gap = max(max_gap, (cur - prev).total_seconds())

    state_path = run_dir / "state.json"
    state = _read_json(state_path) if state_path.exists() else {}
    finished_raw = state.get("finished_at")
    finished = _parse_iso(finished_raw) if isinstance(finished_raw, str) else None
    completed = bool(state.get("completed", False)) and state.get("run_id") == run_id

    reasons: list[str] = []
    if not records:
        reasons.append("no_attempts")
    if invalid:
        reasons.append("invalid_records")
    if foreign:
        reasons.append("foreign_records")
    if len(expected) < 50:
        reasons.append("sku_count_too_small")
    if skus != expected:
        reasons.append("sku_coverage")
    if success_skus != expected:
        reasons.append("success_coverage")
    if rate < threshold:
        reasons.append("success_rate")
    if threshold < GATE_THRESHOLD:
        reasons.append("threshold_too_low")
    if hours < 24.0:
        reasons.append("gate_window_too_short")
    if not completed:
        reasons.append("not_completed")
    if finished is None:
        reasons.append("missing_finish")
    else:
        duration = (finished - started).total_seconds()
        if duration < hours * 3600 - 1:
            reasons.append("short_duration")
    if timestamps:
        if timestamps[0] > started + timedelta(seconds=60):
            reasons.append("late_start")
        if timestamps[-1] < planned_end - timedelta(seconds=max_gap_limit):
            reasons.append("early_finish")
    else:
        reasons.append("no_timestamps")
    if max_gap > max_gap_limit:
        reasons.append("coverage_gap")

    return {
        "run_id": run_id,
        "source": manifest.get("source"),
        "attempts": total,
        "counts": counts,
        "unique_skus": len(skus),
        "expected_skus": len(expected),
        "successful_skus": len(success_skus),
        "success_rate": rate,
        "threshold": threshold,
        "first_ts": _iso(timestamps[0]) if timestamps else None,
        "last_ts": _iso(timestamps[-1]) if timestamps else None,
        "max_gap_seconds": max_gap,
        "max_gap_limit_seconds": max_gap_limit,
        "started_at": manifest["started_at"],
        "planned_end_at": manifest["planned_end_at"],
        "finished_at": finished_raw,
        "completed": completed,
        "invalid_records": invalid,
        "foreign_records": foreign,
        "reasons": reasons,
        "gate_passed": not reasons,
    }


def print_summary(run_dir: Path) -> None:
    print(json.dumps(summarize(run_dir), ensure_ascii=False))


def _session(source: Source, bundle: str, proxy: str | None) -> requests.Session[requests.Response]:
    curl_options: dict[CurlOpt, Any] = {CurlOpt.CAINFO: bundle}
    if proxy is None:
        # Запрещаем libcurl использовать HTTP(S)_PROXY/ALL_PROXY из окружения.
        curl_options[CurlOpt.NOPROXY] = "*"
    session: requests.Session[requests.Response] = requests.Session(
        impersonate="chrome",
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=True,
        proxy=proxy,
        curl_options=curl_options,
    )
    session.headers.update({"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"})
    if source.mobile:
        session.headers.update(MOBILE_HEADERS)
    elif source.kind == "composer":
        session.headers.update({"Accept": "application/json"})
    return session


def _request(
    source: Source, session: requests.Session[requests.Response], segment: str
) -> tuple[int, str, str | None]:
    if source.kind == "composer":
        resp = session.get(source.url, params={"url": f"/product/{segment}/"})
    else:
        resp = session.get(source.url.format(segment=segment))
    return resp.status_code, resp.text, None


def _env_headless() -> bool:
    """OZON_BROWSER_HEADLESS=false — показать окно браузера (отладка mint)."""
    return os.environ.get("OZON_BROWSER_HEADLESS", "true").strip().lower() != "false"


#: Запас сверх таймаута запроса: mint идентичности браузером занимает до ~40 с.
_MINT_GRACE_SECONDS = 60.0


class IdentityFetcher:
    """Мост sync-раннера к async-транспорту метода идентичности.

    Транспорт (curl-реплей или вкладка Chromium) живёт в отдельном event loop
    на всё время run: идентичность кэшируется, вкладка переиспользуется.
    Rate-limit делает сам раннер (interval >= 2 с + джиттер), поэтому берётся
    транспорт напрямую, без OzonProvider с его собственным pace/backoff.
    """

    def __init__(
        self,
        source: Source,
        *,
        proxy: str | None,
        ca_bundle: str,
        profile_dir: Path,
        cookie: str | None = None,
        user_agent: str | None = None,
        headless: bool = True,
        transport: Transport | None = None,
    ) -> None:
        self._timeout = REQUEST_TIMEOUT_SECONDS
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="ozon-identity", daemon=True
        )
        self._thread.start()
        if transport is not None:
            self._transport: Transport = transport
        elif source.inpage:
            self._transport = InPageTransport(
                BrowserOptions(profile_dir=profile_dir, headless=headless, proxy=proxy),
                timeout=self._timeout,
            )
        else:
            minter: IdentityMinter
            if cookie and user_agent:
                minter = StaticIdentityMinter(cookie, user_agent)
            else:
                minter = BrowserMinter(profile_dir=profile_dir, headless=headless, proxy=proxy)
            self._transport = CurlIdentityTransport(
                IdentityStore(minter),
                proxy=proxy,
                timeout=self._timeout,
                ca_bundle=ca_bundle,
            )

    def request(self, segment: str) -> tuple[int, str, str | None]:
        """Один запрос страницы товара. Сбой (mint/сеть) — (0, "", причина)."""
        url = product_url(_sku(segment))
        future = asyncio.run_coroutine_threadsafe(self._transport.get(url), self._loop)
        try:
            response = future.result(self._timeout + _MINT_GRACE_SECONDS)
        except Exception as exc:  # noqa: BLE001 — спайк логирует любые сбои
            return 0, "", f"{type(exc).__name__}: {str(exc)[:160]}"
        return response.status, response.body, None

    def close(self) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._transport.aclose(), self._loop).result(30.0)
        except Exception:  # noqa: BLE001 — закрытие не должно ронять сводку
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10.0)


def _identity_fetcher(
    source: Source, proxy: str | None, bundle: str, run_dir: Path
) -> IdentityFetcher:
    return IdentityFetcher(
        source,
        proxy=proxy,
        ca_bundle=bundle,
        profile_dir=run_dir / "ozon-profile",
        cookie=os.environ.get("OZON_COOKIE") or None,
        user_agent=os.environ.get("OZON_USER_AGENT") or None,
        headless=_env_headless(),
    )


def _should_backoff(cls: str, exc: str | None, source: Source) -> bool:
    """Challenge — всегда backoff; метод идентичности — ещё и на сбой mint/сети.

    Иначе заблокированный выход долбился бы каждые 2 с: mint не удался —
    следующая попытка тут же снова запускает браузер и флагует IP сильнее.
    """
    return cls == "challenge" or (source.kind == "identity" and exc is not None)


def probe_sources(segments: list[str], proxy: str | None, out_dir: Path) -> None:
    """Разовая проверка всех кандидатов без gate. Тела ответов не сохраняются."""
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = build_ca_bundle(out_dir)
    segment = segments[0]
    results: list[dict[str, Any]] = []
    for source in SOURCES.values():
        started = time.monotonic()
        record: dict[str, Any] = {
            "ts": _iso(_now()),
            "source": source.name,
            "sku": _sku(segment),
            "proxy_configured": proxy is not None,
            "env_proxies_disabled": proxy is None,
        }
        try:
            if source.kind == "identity":
                fetcher = _identity_fetcher(source, proxy, bundle, out_dir)
                try:
                    status, body, exc = fetcher.request(segment)
                finally:
                    fetcher.close()
            else:
                session = _session(source, bundle, proxy)
                status, body, exc = _request(source, session, segment)
            cls: str | None
            if exc is not None:
                cls = "error"
                record["error"] = exc
            elif source.kind == "product":
                cls = None
            else:
                cls = _classify_attempt(_sku(segment), status, body)
            record.update(
                {
                    "status": status,
                    "ms": int((time.monotonic() - started) * 1000),
                    "bytes": len(body),
                    "has_widgets": "widgetStates" in body,
                    "has_webprice": "webPrice" in body,
                    "cls": cls,
                }
            )
        except Exception as exc:  # noqa: BLE001 — спайк логирует любые сбои
            record["error"] = type(exc).__name__
        results.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        _sleep(2.2)
    _write_json(out_dir / "source_probe.json", {"results": results})


def run(args: argparse.Namespace) -> None:
    global _STOP
    _STOP = False
    segments = load_segments(Path(args.skus))
    proxy = None if args.no_proxy else (args.proxy or os.environ.get("OZON_PROXY_URL") or None)
    run_dir = Path(args.run_dir) if args.run_dir else ROOT.parent / "spike_out" / args.source
    manifest_path = run_dir / "manifest.json"
    state_path = run_dir / "state.json"
    log_path = run_dir / "gate_log.jsonl"

    if args.resume:
        if not manifest_path.exists():
            sys.exit(f"нет манифеста для --resume: {manifest_path}")
        manifest = _read_json(manifest_path)
        if _sha256(segments) != manifest["sku_sha256"]:
            sys.exit("SKU-лист изменился с момента старта run; продолжение запрещено")
        source = SOURCES[manifest["source"]]
        interval = float(manifest["min_interval_seconds"])
        started = _parse_iso(manifest["started_at"])
        planned_end = _parse_iso(manifest["planned_end_at"])
        run_id = manifest["run_id"]
    else:
        if manifest_path.exists():
            sys.exit(f"run уже существует: {run_dir}; используйте --resume или новый --run-dir")
        if args.source not in GATE_SOURCES:
            sys.exit(f"источник не поддерживается gate: {args.source}")
        source = GATE_SOURCES[args.source]
        interval = max(2.0, float(args.interval))
        run_id = uuid.uuid4().hex
        started = _now()
        planned_end = started + timedelta(hours=float(args.hours))
        manifest = {
            "version": 1,
            "run_id": run_id,
            "source": source.name,
            "kind": source.kind,
            "endpoint": source.url,
            "hours": float(args.hours),
            "min_interval_seconds": interval,
            "threshold": GATE_THRESHOLD,
            "started_at": _iso(started),
            "planned_end_at": _iso(planned_end),
            "skus": [_sku(seg) for seg in segments],
            "sku_count": len(segments),
            "sku_sha256": _sha256(segments),
            "impersonate": "chrome",
            "proxy_configured": proxy is not None,
            "no_proxy_requested": bool(args.no_proxy),
        }
        if source.kind == "identity":
            manifest["identity_inpage"] = source.inpage
            manifest["identity_mode"] = (
                "static-cookie"
                if os.environ.get("OZON_COOKIE") and os.environ.get("OZON_USER_AGENT")
                else "browser-mint"
            )
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(manifest_path, manifest)

    bundle = build_ca_bundle(run_dir)
    fetcher: IdentityFetcher | None = None
    session: requests.Session[requests.Response] | None = None
    if source.kind == "identity":
        fetcher = _identity_fetcher(source, proxy, bundle, run_dir)
    else:
        session = _session(source, bundle, proxy)
    remaining = max(0.0, (planned_end - _now()).total_seconds())
    deadline = time.monotonic() + remaining
    consecutive_challenges = 0
    attempt = 0
    print(
        f"spike start: source={source.name} skus={len(segments)} hours={manifest['hours']} "
        f"interval>={interval}s proxy={'yes' if proxy else 'no'} "
        f"identity={manifest.get('identity_mode', '-') if source.kind == 'identity' else '-'} "
        f"run_dir={run_dir}",
        flush=True,
    )
    try:
        with log_path.open("a", encoding="utf-8") as log:
            while not _STOP and time.monotonic() < deadline:
                for segment in segments:
                    if _STOP or time.monotonic() >= deadline:
                        break
                    sku = _sku(segment)
                    attempt += 1
                    started_req = time.monotonic()
                    status, body, exc = 0, "", None
                    try:
                        if fetcher is not None:
                            status, body, exc = fetcher.request(segment)
                        else:
                            assert session is not None
                            status, body, exc = _request(source, session, segment)
                    except Exception as e:  # noqa: BLE001 — спайк логирует любые сбои
                        exc = type(e).__name__
                    cls = _classify_attempt(sku, status, body) if exc is None else "error"
                    record = {
                        "run_id": run_id,
                        "ts": _iso(_now()),
                        "attempt": attempt,
                        "sku": sku,
                        "source": source.name,
                        "status": status,
                        "cls": cls,
                        "ms": int((time.monotonic() - started_req) * 1000),
                        "bytes": len(body),
                        "exc": exc,
                    }
                    log.write(json.dumps(record, ensure_ascii=False) + "\n")
                    log.flush()
                    if _should_backoff(cls, exc, source):
                        consecutive_challenges += 1
                        backoff = min(2.0**consecutive_challenges, BACKOFF_CAP_SECONDS)
                        reason = f"status={status}" if exc is None else exc
                        print(
                            f"[{attempt}] {sku}: {cls} ({reason}), backoff {backoff:.0f}s",
                            flush=True,
                        )
                        _sleep(min(backoff, max(0.0, deadline - time.monotonic())))
                    else:
                        consecutive_challenges = 0
                        if attempt % len(segments) == 0:
                            print_summary(run_dir)
                        _sleep(
                            min(
                                interval + random.uniform(0.0, 0.5),
                                max(0.0, deadline - time.monotonic()),
                            )
                        )
    finally:
        if fetcher is not None:
            fetcher.close()
        finished = _now()
        completed = finished >= planned_end
        _write_json(
            state_path,
            {
                "run_id": run_id,
                "finished_at": _iso(finished),
                "completed": completed,
                "stop_requested": _STOP,
            },
        )
    print("spike finished", flush=True)
    print_summary(run_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skus", default=str(ROOT / "spike_skus.txt"))
    parser.add_argument("--source", default="composer", choices=sorted(GATE_SOURCES))
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--interval", type=float, default=2.0, help="минимум 2 с")
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--proxy", default=None)
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="запретить прокси, включая OZON_PROXY_URL и env HTTP(S)_PROXY",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--report", action="store_true", help="только сводка по run")
    mode.add_argument("--resume", action="store_true", help="продолжить существующий run")
    mode.add_argument("--probe", action="store_true", help="разовая проверка всех источников")
    args = parser.parse_args()
    if args.no_proxy and args.proxy:
        parser.error("--no-proxy нельзя использовать вместе с --proxy")
    if args.hours <= 0:
        parser.error("--hours должен быть > 0")
    if args.report:
        run_dir = Path(args.run_dir) if args.run_dir else ROOT.parent / "spike_out" / args.source
        print_summary(run_dir)
        return
    if args.probe:
        run_dir = Path(args.run_dir) if args.run_dir else ROOT.parent / "spike_out" / "probe"
        proxy = None if args.no_proxy else (args.proxy or os.environ.get("OZON_PROXY_URL") or None)
        probe_sources(load_segments(Path(args.skus)), proxy, run_dir)
        return
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    run(args)


if __name__ == "__main__":
    main()
