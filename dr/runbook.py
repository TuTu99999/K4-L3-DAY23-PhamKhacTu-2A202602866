"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
CHAOS_LOG = pathlib.Path("chaos/chaos-events.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
OUTAGE_PROBES = 3
OUTAGE_PROBE_INTERVAL = 5.0


def step(n, name, **kw):
    """Ghi một bước runbook có timestamp vào JSONL."""
    now = time.time()
    record = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
        "step": n,
        "name": name,
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(record, ensure_ascii=False))
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Tự xác nhận trong CI, hoặc hỏi operator theo mặc định an toàn y/N."""
    if auto:
        return True
    answer = input(f"{msg} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _latest_outage_ts(primary: str):
    if not CHAOS_LOG.exists():
        return None
    latest = None
    for line in CHAOS_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("action") == "kill" and event.get("region") == primary:
            latest = event.get("ts")
    return latest


def _target_alive(region: str) -> tuple[bool, str]:
    try:
        response = httpx.get(f"{URL[region]}/healthz", timeout=2.0)
        return response.status_code == 200, f"status={response.status_code}"
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _golden_signals(target: str, requests: int = 10) -> dict:
    latencies = []
    failures = 0
    statuses = []
    for i in range(requests):
        started = time.monotonic()
        try:
            response = httpx.get(
                f"{URL[target]}/v1/infer",
                params={"q": f"golden signal {i}"},
                timeout=3.0,
            )
            statuses.append(response.status_code)
            if response.status_code != 200:
                failures += 1
        except httpx.HTTPError as exc:
            statuses.append(type(exc).__name__)
            failures += 1
        latencies.append(round((time.monotonic() - started) * 1000, 1))

    ordered = sorted(latencies)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    p95 = ordered[p95_index]
    error_rate = round(failures / requests, 3)
    return {
        "requests": requests,
        "failures": failures,
        "error_rate": error_rate,
        "p95_latency_ms": p95,
        "statuses": statuses,
        "ok": error_rate == 0,
    }


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Thực thi bảy bước incident response bán tự động."""
    if primary not in URL or target not in URL:
        raise ValueError("primary and target must be 'a' or 'b'")
    if primary == target:
        raise ValueError("primary and target must be different regions")

    run_started = time.time()

    # 1. Ba probe cách nhau theo interval: một failure đơn lẻ không phải outage.
    primary_probes = []
    target_probes = []
    for attempt in range(OUTAGE_PROBES):
        cycle_started = time.monotonic()
        primary_ready, primary_reason = hc.probe(primary, timeout=2.0)
        target_ready, target_reason = hc.probe(target, timeout=2.0)
        target_alive, target_alive_reason = _target_alive(target)
        primary_probes.append({"ready": primary_ready, "reason": primary_reason})
        target_probes.append({
            "ready": target_ready,
            "reason": target_reason,
            "alive": target_alive,
            "alive_reason": target_alive_reason,
        })
        if attempt < OUTAGE_PROBES - 1:
            elapsed = time.monotonic() - cycle_started
            time.sleep(max(0.0, OUTAGE_PROBE_INTERVAL - elapsed))

    outage_confirmed = all(not probe["ready"] for probe in primary_probes)
    target_alive = all(probe["alive"] for probe in target_probes)
    step(
        1,
        "xac_nhan_outage",
        ok=outage_confirmed and target_alive,
        primary=primary,
        target=target,
        required_consecutive_fails=OUTAGE_PROBES,
        interval_s=OUTAGE_PROBE_INTERVAL,
        primary_probes=primary_probes,
        target_probes=target_probes,
    )

    if not outage_confirmed or not target_alive:
        reason = (
            "primary outage was not confirmed"
            if not outage_confirmed
            else "target process is not alive"
        )
        return {"ok": False, "failed_step": "1_xac_nhan_outage", "reason": reason}

    # 2. Alert + operator confirmation. Timestamp này là lúc operator biết incident.
    approved = confirm(auto, f"Region {primary} down. Fail over sang region {target}?")
    outage_ts = _latest_outage_ts(primary)
    incident = step(
        2,
        "thong_bao_incident",
        ok=approved,
        operator_confirmed=approved,
        auto=auto,
        outage_ts=outage_ts,
        notification_delay_s=(
            None if outage_ts is None else round(time.time() - outage_ts, 2)
        ),
    )
    if not approved:
        return {
            "ok": False,
            "failed_step": "2_thong_bao_incident",
            "reason": "operator declined failover",
        }

    # 3. Lời gọi failover DUY NHẤT trong toàn bộ runbook.
    try:
        failover_result = fo.failover(target, backend, wait=60.0)
    except Exception as exc:
        failover_result = {
            "ok": False,
            "failed_step": "exception",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    step(
        3,
        "scale_gpu_pool",
        ok=bool(failover_result.get("ok")),
        failover_ok=bool(failover_result.get("ok")),
        failed_step=failover_result.get("failed_step"),
        reason=failover_result.get("reason"),
        waited_s=failover_result.get("waited_s"),
        rpo_seconds=failover_result.get("rpo_seconds"),
        docs_lost=failover_result.get("docs_lost"),
    )

    # 4. Chỉ đọc dict của bước 3; tuyệt đối không gọi failover lần hai.
    target_state = failover_result.get("target_state") or {}
    replica_ok = bool(
        failover_result.get("ok")
        and target_state.get("weights")
        and (target_state.get("count") or 0) > 0
    )
    step(
        4,
        "verify_state_replica",
        ok=replica_ok,
        skipped=not failover_result.get("ok", False),
        target=target,
        target_state=target_state,
        rpo_seconds=failover_result.get("rpo_seconds"),
        docs_lost=failover_result.get("docs_lost"),
        embed_model_version=failover_result.get("embed_model_version"),
    )

    # 5. Đọc lại pointer; không tự ghi DNS thêm lần nữa.
    active_file = pathlib.Path("edge/active_region")
    active_region = active_file.read_text(encoding="utf-8").strip() if active_file.exists() else None
    cutover_ok = bool(failover_result.get("ok") and active_region == target)
    step(
        5,
        "dns_cutover",
        ok=cutover_ok,
        skipped=not failover_result.get("ok", False),
        expected_region=target,
        active_region=active_region,
    )

    # 6. Chỉ bắn golden signals khi restore và cutover đã an toàn.
    if replica_ok and cutover_ok:
        golden = _golden_signals(target, requests=10)
        step(6, "verify_golden_signals", **golden)
    else:
        golden = {"ok": False, "skipped": True, "reason": "failover not complete"}
        step(6, "verify_golden_signals", **golden)

    # 7. Timeline kết thúc và chỉ rõ lệnh tái lập phép đo RTO.
    overall_ok = bool(failover_result.get("ok") and replica_ok and cutover_ok and golden["ok"])
    elapsed = round(time.time() - incident["ts"], 2)
    step(
        7,
        "post_incident",
        ok=overall_ok,
        elapsed_s=elapsed,
        rto_command=(
            "python tools/measure_rto.py --loadgen "
            "reports/drill-2-withdr.jsonl --target-rto 300"
        ),
    )

    return {
        "ok": overall_ok,
        "primary": primary,
        "target": target,
        "elapsed_s": elapsed,
        "failover": failover_result,
        "replica_ok": replica_ok,
        "cutover_ok": cutover_ok,
        "golden_signals": golden,
        "run_started_ts": run_started,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
