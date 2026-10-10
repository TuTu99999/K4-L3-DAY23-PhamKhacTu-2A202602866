# Runbook — Region chính down

**Phạm vi:** bare mode, primary A, target B, snapshot backend `fs`. Chạy mọi lệnh từ
thư mục gốc repository. Trên máy drill hiện tại Edge dùng cổng 8081 vì cổng 8080 đang
bị Apache chiếm.

## Điều kiện trước khi bắt đầu

- Có ít nhất một snapshot thành công trong `reports/replication.jsonl`.
- Region B còn sống qua `curl.exe http://127.0.0.1:8002/healthz`.
- Không tiếp tục nếu cả hai region đều không phản hồi; mở incident cấp cao hơn để tránh
  tạo double outage.

| # | Bước | Lệnh copy-paste | Biết là xong khi | Owner |
|---:|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | Chạy 3 lần cách nhau 5 giây; `a.ready=false` cả 3 lần và `b.alive=true` | Primary on-call |
| 2 | Mở incident, ghi mốc RTO | `python dr/runbook.py --primary a --target b --backend fs` | Có dòng `thong_bao_incident` trong `reports/runbook-run.jsonl`; operator nhập `y` sau khi xác nhận target còn sống | Incident commander |
| 3 | Restore state và scale pool | Lệnh bước 2 tự gọi `failover()` đúng một lần; với diễn tập/CI dùng `python dr/runbook.py --primary a --target b --backend fs --auto` | `reports/failover-events.jsonl` lần lượt có `1_verify_target`, `2_restore_snapshot`, `3_scale_pool`; restore event có `rpo_seconds`, `docs_lost`, model version | DR operator |
| 4 | Chờ Region B ready | `curl.exe -i http://127.0.0.1:8002/readyz` | HTTP 200, `ready:true`, pool `full`, vector count > 0, không còn reason lỗi | ML serving on-call |
| 5 | Xác minh DNS/LB cutover | `curl.exe http://127.0.0.1:8081/edge/state` | Sau tối đa TTL 5s thấy `active_region:"b"`; failover log có `5_dns_cutover` sau `4_wait_ready` | Network/on-call |
| 6 | Kiểm tra golden signals | `1..10 | ForEach-Object { curl.exe -sS "http://127.0.0.1:8081/v1/infer?q=golden-$($_)" }` | 10/10 request HTTP 200, response có `edge_region:"b"`; runbook log có `error_rate:0.0`, p95 được ghi lại | Service owner |
| 7 | Đo RTO và đóng incident | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, `warnings:[]`, `recovered_by_region:"b"`, `rto_verdict:"PASS"` | Incident commander |

## Abort và rollback

- **Abort trước cutover:** nếu restore lỗi, model version thiếu/không khớp, vector count bằng
  0, hoặc `/readyz` của B không trả 200 trong 60s. `failover.py` phải trả `ok:false` và
  không được sửa `edge/active_region`. DR operator báo Incident Commander.
- **Rollback từ B về A:** chỉ thực hiện khi Region A đã được khôi phục, `/readyz` trả 200
  ổn định ít nhất 3 lần cách nhau 5s, state đã đồng bộ ngược và golden signals của A đạt.
  **Incident Commander** là người duy nhất phê duyệt rollback.
- Sau phê duyệt, chạy `python dr/failover.py --target a --backend fs`; không sửa tay
  `edge/active_region`. Theo dõi thêm ít nhất 10 request và giữ B sẵn sàng làm fallback.
- Nếu cả A và B cùng không ready, không failover qua lại. Giữ traffic ở region phục vụ
  được gần nhất, mở incident SEV-1 và phục hồi state trước.

## Bằng chứng của lần chạy này

- RTO 24.4s; request đầu tiên phục hồi được phục vụ bởi B
  (`reports/drill-2-withdr.jsonl:24`).
- RPO 24.01s / 12 docs; snapshot restore đúng model version
  `embed-model=vi-e5-base@v3` (`reports/failover-events.jsonl:2`).
- Traffic tiếp tục thành công từ B đến cuối cửa sổ đo
  (`reports/drill-2-withdr.jsonl:151`).
