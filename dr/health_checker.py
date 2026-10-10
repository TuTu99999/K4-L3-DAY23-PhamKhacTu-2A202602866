"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Kiểm tra readiness của region và trả về ``(ready, reason)``."""
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except httpx.TimeoutException as exc:
        return False, f"timeout:{type(exc).__name__}"
    except httpx.HTTPError as exc:
        return False, f"http_error:{type(exc).__name__}"

    if response.status_code == 200:
        return True, "ready"

    try:
        body = response.json()
        reasons = body.get("reasons") or [body.get("error")]
        detail = ",".join(str(reason) for reason in reasons if reason)
    except (ValueError, AttributeError):
        detail = ""
    reason = f"status={response.status_code}"
    return False, f"{reason}:{detail}" if detail else reason


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll hai region, phát hiện transition và ghi event dạng JSONL."""
    if interval <= 0:
        raise ValueError("interval must be greater than zero")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if threshold < 1:
        raise ValueError("threshold must be at least one")
    if duration < 0:
        raise ValueError("duration cannot be negative")

    out.parent.mkdir(parents=True, exist_ok=True)
    states = {region: "HEALTHY" for region in URL}
    consecutive_fails = {region: 0 for region in URL}
    deadline = time.monotonic() + duration

    with out.open("a", encoding="utf-8") as log:
        while time.monotonic() < deadline:
            cycle_started = time.monotonic()
            for region in URL:
                ready, reason = probe(region, timeout)
                previous = states[region]

                if ready:
                    consecutive_fails[region] = 0
                    current = "HEALTHY"
                else:
                    consecutive_fails[region] += 1
                    current = (
                        "UNHEALTHY"
                        if consecutive_fails[region] >= threshold
                        else previous
                    )

                if current != previous:
                    states[region] = current
                    now = time.time()
                    record = {
                        "ts": now,
                        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
                        "event": "state_change",
                        "region": region,
                        "to": current,
                        "reason": reason,
                        "consecutive_fails": consecutive_fails[region],
                        "interval_s": interval,
                        "threshold": threshold,
                    }
                    log.write(json.dumps(record, ensure_ascii=False) + "\n")
                    log.flush()
                    print("HEALTH", json.dumps(record, ensure_ascii=False))

            remaining = deadline - time.monotonic()
            sleep_for = interval - (time.monotonic() - cycle_started)
            if remaining > 0 and sleep_for > 0:
                time.sleep(min(sleep_for, remaining))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
