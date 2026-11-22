"""Gate-раннер спайка Ozon (README §5, M1).

Гоняет composer-API по списку SKU последовательно (1 SKU = 1 запрос),
интервал >= 2 с + джиттер, backoff 2^n (кап 30 мин) на challenge/429.
Пишет JSONL-лог и считает success rate. Gate: >= 90 % успешных fetch
на 50 SKU в течение 24 часов.

Запуск (из корня репо, venv активен):
    python scripts/spike_ozon.py --hours 24                 # полный gate
    python scripts/spike_ozon.py --hours 0.2                # быстрый smoke
    python scripts/spike_ozon.py --report                   # сводка по логу
Прокси (обязателен вне РФ/датацентров): OZON_PROXY_URL или --proxy.

Важно: api.ozon.ru отдаёт сертификат УЦ Минцифры РФ — раннер собирает
CA-бандл certifi + scripts/certs/russian_trusted_root_ca.crt.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

import certifi
from curl_cffi import requests

ROOT = Path(__file__).resolve().parent
COMPOSER = "https://api.ozon.ru/composer-api.bx/page/json/v2"
CHALLENGE_MARKERS = (
    "incidentid",
    "нет соединения",
    "выключите vpn",
    "challenge",
    "captcha",
    "доступ ограничен",
)
_STOP = False


def _handle_stop(signum: int, frame: FrameType | None) -> None:
    global _STOP
    _STOP = True


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


def classify(status: int, body: str) -> str:
    """success | challenge | dead | error. Challenge != отсутствие товара."""
    if status == 200:
        try:
            data = json.loads(body)
        except ValueError:
            return "error"
        states = data.get("widgetStates")
        if isinstance(states, dict) and any(k.startswith("webPrice") for k in states):
            return "success"
        return "error"
    if status == 404:
        return "dead"
    lowered = body.lower()
    if status in (403, 429) or any(marker in lowered for marker in CHALLENGE_MARKERS):
        return "challenge"
    return "error"


def summarize(log_path: Path) -> dict[str, Any]:
    counts: dict[str, int] = {}
    skus: set[str] = set()
    total = 0
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            total += 1
            counts[rec["cls"]] = counts.get(rec["cls"], 0) + 1
            skus.add(rec["sku"])
    success = counts.get("success", 0)
    rate = success / total if total else 0.0
    return {"attempts": total, "counts": counts, "unique_skus": len(skus), "success_rate": rate}


def print_summary(log_path: Path, sku_count: int) -> None:
    s = summarize(log_path)
    gate = s["success_rate"] >= 0.90 and s["unique_skus"] >= sku_count and s["attempts"] > 0
    print(json.dumps({**s, "gate_passed": gate}, ensure_ascii=False))


def run(args: argparse.Namespace) -> None:
    segments = [
        line.strip()
        for line in Path(args.skus).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not segments:
        sys.exit("SKU-лист пуст")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bundle = build_ca_bundle(out_path.parent)
    proxy = args.proxy or os.environ.get("OZON_PROXY_URL") or None
    session = requests.Session(impersonate="chrome", timeout=20, verify=bundle, proxy=proxy)
    session.headers.update({"accept-language": "ru-RU,ru;q=0.9,en;q=0.8"})
    interval = max(2.0, args.interval)
    deadline = time.monotonic() + args.hours * 3600
    consecutive_challenges = 0
    attempt = 0
    print(
        f"spike start: skus={len(segments)} hours={args.hours} interval>={interval}s "
        f"proxy={'yes' if proxy else 'no'} log={out_path}"
    )
    with out_path.open("a", encoding="utf-8") as log:
        while not _STOP and time.monotonic() < deadline:
            for seg in segments:
                if _STOP or time.monotonic() >= deadline:
                    break
                sku_match = re.search(r"(\d+)$", seg)
                sku = sku_match.group(1) if sku_match else seg
                attempt += 1
                started = time.monotonic()
                status, body, exc = 0, "", None
                try:
                    resp = session.get(COMPOSER, params={"url": f"/product/{seg}/"})
                    status, body = resp.status_code, resp.text
                except Exception as e:  # noqa: BLE001 — спайк логирует любые сбои
                    exc = f"{type(e).__name__}: {e}"
                cls = classify(status, body) if exc is None else "error"
                record = {
                    "ts": datetime.now(UTC).isoformat(),
                    "attempt": attempt,
                    "sku": sku,
                    "status": status,
                    "cls": cls,
                    "ms": int((time.monotonic() - started) * 1000),
                    "bytes": len(body),
                    "exc": exc,
                }
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
                log.flush()
                if cls == "challenge":
                    consecutive_challenges += 1
                    backoff = min(2.0**consecutive_challenges, 1800.0)
                    print(f"[{attempt}] {sku}: challenge (status={status}), backoff {backoff:.0f}s")
                    time.sleep(backoff)
                else:
                    consecutive_challenges = 0
                    if attempt % len(segments) == 0:
                        print_summary(out_path, len(segments))
                    time.sleep(interval + random.uniform(0.0, 0.5))
    print("spike finished")
    print_summary(out_path, len(segments))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skus", default=str(ROOT / "spike_skus.txt"))
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--interval", type=float, default=2.0, help="минимум 2 с")
    parser.add_argument("--out", default=str(ROOT.parent / "spike_out" / "gate_log.jsonl"))
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--report", action="store_true", help="только сводка по логу")
    args = parser.parse_args()
    if args.report:
        segments = [
            line
            for line in Path(args.skus).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        print_summary(Path(args.out), len(segments))
        return
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    run(args)


if __name__ == "__main__":
    main()
