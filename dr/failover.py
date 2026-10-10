"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append một failover event có timestamp và in ra stdout."""
    now = time.time()
    record = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("FAILOVER", json.dumps(record, ensure_ascii=False))
    return record


def state_of(region: str) -> dict:
    """Lấy state hiện tại của region hoặc raise nếu region không phản hồi."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore và cut over sang ``target`` theo đúng năm bước an toàn."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if backend not in {"fs", "minio"}:
        raise ValueError(f"unsupported snapshot backend: {backend}")
    if wait < 0:
        raise ValueError("wait cannot be negative")

    completed = []
    result = {
        "ok": False,
        "target": target,
        "backend": backend,
        "steps": completed,
        "rpo_seconds": None,
        "docs_lost": None,
        "embed_model_version": None,
    }

    # 1. Region phụ phải tồn tại và trả lời API trước khi restore.
    try:
        initial_state = state_of(target)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="1_verify_target", ok=False, target=target, reason=reason)
        result["failed_step"] = "1_verify_target"
        result["reason"] = reason
        return result
    emit(step="1_verify_target", ok=True, target=target, state=initial_state)
    completed.append("1_verify_target")

    # 2. Restore state, sau đó so sánh DB primary với bản vừa restore để đo RPO.
    primary = "b" if target == "a" else "a"
    try:
        restore_meta = snapshot.get(target, backend)
        rpo = snapshot.rpo(
            pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
    except (Exception, SystemExit) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="2_restore_snapshot", ok=False, target=target, reason=reason)
        result["failed_step"] = "2_restore_snapshot"
        result["reason"] = reason
        return result

    result.update(
        rpo_seconds=rpo["rpo_seconds"],
        docs_lost=rpo["docs_lost"],
        embed_model_version=restore_meta.get("embed_model_version"),
    )
    emit(
        step="2_restore_snapshot",
        ok=True,
        target=target,
        snapshot_at=restore_meta.get("snapshot_at"),
        restored_at=restore_meta.get("restored_at"),
        rpo_seconds=result["rpo_seconds"],
        docs_lost=result["docs_lost"],
        embed_model_version=result["embed_model_version"],
    )
    completed.append("2_restore_snapshot")

    # 3. Scale pool. serving/app.py sẽ bắt đầu warm-up khi quan sát transition này.
    try:
        pool_file = pathlib.Path(f"state/region-{target}/pool_state")
        pool_file.parent.mkdir(parents=True, exist_ok=True)
        pool_file.write_text("full", encoding="utf-8")
    except OSError as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="3_scale_pool", ok=False, target=target, reason=reason)
        result["failed_step"] = "3_scale_pool"
        result["reason"] = reason
        return result
    emit(step="3_scale_pool", ok=True, target=target, pool_state="full")
    completed.append("3_scale_pool")

    # 4. Không cut over cho đến khi readiness trả 200.
    started = time.monotonic()
    deadline = started + wait
    last_reason = "readiness deadline expired"
    ready_body = None
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            response = httpx.get(
                f"{URL[target]}/readyz",
                timeout=max(0.01, min(2.0, remaining)),
            )
            if response.status_code == 200:
                ready_body = response.json()
                break
            try:
                body = response.json()
                last_reason = ",".join(body.get("reasons", [])) or f"status={response.status_code}"
            except (ValueError, AttributeError):
                last_reason = f"status={response.status_code}"
        except httpx.HTTPError as exc:
            last_reason = f"{type(exc).__name__}: {exc}"

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.5, remaining))

    waited = round(time.monotonic() - started, 2)
    if ready_body is None:
        emit(
            step="4_wait_ready",
            ok=False,
            target=target,
            waited_s=waited,
            reason=last_reason,
        )
        result["failed_step"] = "4_wait_ready"
        result["reason"] = last_reason
        result["waited_s"] = waited
        return result

    emit(step="4_wait_ready", ok=True, target=target, waited_s=waited, readiness=ready_body)
    completed.append("4_wait_ready")

    # 5. Chỉ readiness thành công mới được thay đổi con trỏ traffic.
    try:
        pathlib.Path("edge/active_region").write_text(target, encoding="utf-8")
    except OSError as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="5_dns_cutover", ok=False, target=target, reason=reason)
        result["failed_step"] = "5_dns_cutover"
        result["reason"] = reason
        return result
    emit(step="5_dns_cutover", ok=True, target=target)
    completed.append("5_dns_cutover")

    vectors = ready_body.get("vectors", {})
    result.update(
        ok=True,
        waited_s=waited,
        target_state={
            "region": target,
            "pool_state": ready_body.get("pool_state"),
            "weights": True,  # /readyz=200 guarantees weights_ok() in serving/app.py
            "count": vectors.get("count"),
            "latest_doc_ts": vectors.get("latest_doc_ts"),
        },
        active_region=target,
    )
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
