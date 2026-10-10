# Postmortem — DR Drill Lab 23

**Ngày diễn tập:** 10/10/2026

**Phạm vi ảnh hưởng:** Inference qua Edge tới Region A

**Kết quả:** Failover sang Region B thành công; RTO và RPO đều trong mục tiêu 300s.

## 1. Timeline

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
| 2026-10-10T14:08:28 | Region A bị dừng; Region B được xác nhận còn alive | `chaos/chaos-events.jsonl:3` |
| 2026-10-10T14:08:28 | User đầu tiên nhận lỗi, +0.1s | `reports/drill-2-withdr.jsonl:14` |
| 2026-10-10T14:08:41 | Failover verify Region B tồn tại, +13.4s | `reports/failover-events.jsonl:1` |
| 2026-10-10T14:08:41 | Snapshot restore vào B hoàn tất; RPO 24.01s / 12 docs | `reports/failover-events.jsonl:2` |
| 2026-10-10T14:08:43 | Health checker đánh dấu A `UNHEALTHY`, +15.1s | `reports/health-events.jsonl:2` |
| 2026-10-10T14:08:48 | B ready sau warm-up 6.84s | `reports/failover-events.jsonl:4` |
| 2026-10-10T14:08:48 | DNS/LB pointer chuyển sang B | `reports/failover-events.jsonl:5` |
| 2026-10-10T14:08:52 | Request qua Edge đầu tiên thành công từ B, RTO 24.4s | `reports/drill-2-withdr.jsonl:24` |

## 2. RTO/RPO so với mục tiêu và gap analysis

- **RTO:** mục tiêu 300s; đo được **24.4s**; tốt hơn mục tiêu **275.6s**.
- **RPO:** mục tiêu 300s; đo được **24.01s / 12 docs mất**; tốt hơn mục tiêu
  **275.99s**, nhưng 12 docs vẫn là tác động dữ liệu cần truyền đạt cho khách hàng.
- **Bước tốn nhiều thời gian nhất:** xác nhận outage. Automation verify target tại
  +13.4s; health checker độc lập phát hiện tại +15.1s. Detect floor cấu hình là
  5s × 3 = 15s.
- Snapshot restore chỉ khoảng 0.02s và GPU warm-up 6.84s. Từ DNS cutover đến request
  thành công đầu tiên mất khoảng 4.09s, nằm trong TTL 5s của Edge.
- Hai luồng detection chạy song song. Lần này runbook bắt đầu failover trước event
  `UNHEALTHY` của health checker khoảng 1.7s; đây không phải sửa log, mà là hệ quả runbook
  tự thực hiện ba probe riêng. Nên hợp nhất nguồn tín hiệu trong bản production.

## 3. Root cause — 5 whys

1. **Vì sao user nhận lỗi?** Edge vẫn route request tới Region A sau khi A ngừng phản hồi.
2. **Vì sao Edge chưa chuyển ngay?** Edge chỉ đọc pointer `active_region` và cache 5s;
   nó không tự quyết định failover dựa trên readiness.
3. **Vì sao phải chờ trước khi đổi pointer?** Một failure đơn lẻ có thể là transient;
   hệ thống yêu cầu ba probe liên tiếp để tránh flapping.
4. **Vì sao không thể route ngay sang B?** B khởi đầu ở trạng thái warm, không có weights
   và vector DB; phải restore snapshot rồi warm pool trước khi `/readyz` trả 200.
5. **Vì sao có RPO 24.01s / 12 docs?** Replication là snapshot bất đồng bộ mỗi 30s;
   dữ liệu ingest sau snapshot gần nhất chưa có trong bản được restore.

Root cause hệ thống là kiến trúc active-passive với replica bất đồng bộ và target không
được pre-warm, cộng với detection/cutover có chủ đích để chống flapping. Chaos chỉ kích
hoạt và đo lường các đặc tính này, không phải root cause.

## 4. Action items

| # | Action item | Owner | Deadline | Tác động dự kiến |
|---:|---|---|---|---|
| 1 | Hợp nhất runbook với event của health checker, thêm circuit breaker và yêu cầu target ready trước cutover | SRE lead | 17/10/2026 | Loại bỏ probe trùng lặp; giảm khoảng 1–5s mà không tăng flapping |
| 2 | Giảm chu kỳ replication từ 30s xuống 10s và cảnh báo khi snapshot lag > 15s | Data platform owner | 24/10/2026 | RPO lý thuyết giảm tối đa khoảng 20s; giảm số docs mất |
| 3 | Giữ Region B ở mức pre-warm với weights và snapshot gần nhất | ML platform owner | 24/10/2026 | Giảm phần warm-up hiện đo được 6.84s |
| 4 | Chuyển Edge sang health-aware load balancer và kiểm thử TTL 1–2s | Network owner | 31/10/2026 | Giảm khoảng 2–4s sau cutover; cần canary để tránh flapping |

## 5. Câu hỏi bắt buộc

1. `interval × threshold = 5s × 3 = 15s`, tương đương **61.5%** RTO 24.4s.
2. Nếu interval giảm từ 5s xuống 1s và giữ threshold 3, detect floor giảm từ 15s xuống
   3s, tức RTO lý thuyết giảm khoảng **12s**. Đổi lại số probe tăng 5 lần, nhạy hơn với
   transient network failure và nguy cơ flapping cao hơn nếu không có circuit breaker.
3. Nếu outage kéo dài 6 giờ và Region A mất dữ liệu vĩnh viễn, `docs_lost=12` nghĩa là
   12 document đã được primary nhận nhưng không tồn tại trong snapshot khôi phục. Với khách
   hàng, đó có thể là ticket/giao dịch không tìm thấy hoặc kết quả inference dùng dữ liệu cũ;
   cần công bố phạm vi ảnh hưởng và replay từ nguồn sự kiện gốc nếu còn khả dụng.

## 6. Điều đã hoạt động tốt

- Failover giữ đúng thứ tự restore → scale → ready → cutover.
- Drill hợp lệ, không có warning, recovery được phục vụ bởi B.
- Golden signals đạt 10/10 request, error rate 0%, p95 469ms.
- RTO 24.4s và RPO 24.01s đều thấp hơn mục tiêu 300s.
