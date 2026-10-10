# RTO/RPO Evidence — Lab 23

Mọi số liệu dưới đây lấy từ drill chạy ngày 10/10/2026. Drill dùng Windows bare mode;
Edge chạy ở cổng 8081 vì Apache trên máy đang chiếm cổng 8080. Việc đổi cổng không thay
đổi logic đo RTO vì load generator vẫn đi qua Edge Proxy.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---:|---|---|
| `t_outage` | 2026-10-10T13:52:59Z | Chaos event `action:kill`, Region A | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.1s | Request `ok:false` đầu tiên sau `t_outage` | `reports/drill-1-nodr.jsonl:15` |
| Số request thất bại | 13 | Tất cả request từ dòng 15 đến hết file đều lỗi | `reports/drill-1-nodr.jsonl:27` |
| Request thành công sau lỗi | Không có | Không có dòng `ok:true` nào sau dòng 15 | `reports/drill-1-nodr.jsonl:27` |
| RTO | `NO_RECOVERY` | Từ request fail đầu tiên đến hết cửa sổ không có recovery | `reports/drill-1-nodr.jsonl:27` |

## 2. Drill 2 — có DR

| Mốc | +giây từ `t_outage` | Cách đo | Evidence |
|---|---:|---|---|
| `t_outage` | 0.0s | Chaos event `action:kill`, Region A | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | 0.1s | Request `ok:false` đầu tiên | `reports/drill-2-withdr.jsonl:14` |
| Health checker phát hiện | 15.1s | `region:a`, `to:UNHEALTHY`, threshold 3 | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 13.4s | `step:2_restore_snapshot`, RPO đã được tính | `reports/failover-events.jsonl:2` |
| Region B ready | 20.3s | `/readyz` trả 200 sau warm-up 6.84s | `reports/failover-events.jsonl:4` |
| DNS cutover | 20.3s | `step:5_dns_cutover`, target B | `reports/failover-events.jsonl:5` |
| **Request đầu tiên thành công từ B (RTO)** | **24.4s** | Dòng `ok:true`, `served_by:b` đầu tiên sau chuỗi lỗi | `reports/drill-2-withdr.jsonl:24` |

| Chỉ số | Đo được | Mục tiêu | Verdict |
|---|---:|---:|---|
| RTO — Inference API | 24.4s | 300s | **PASS** |
| RPO — Vector DB | 24.01s / 12 docs | 300s | **PASS** |
| Request lỗi trong outage | 10 | — | Đã phục hồi |
| Region phục hồi | B | Khác region bị kill | **PASS** |

RPO không phải số ước lượng: tại thời điểm restore, Region A có thêm 12 document mới hơn
document cuối cùng trong snapshot. Model version được restore cùng snapshot là
`embed-model=vi-e5-base@v3` (`reports/failover-events.jsonl:2`).

## 3. RTO gồm những gì

Các tác vụ detection chạy song song: automation đã verify target tại +13.40s, trong khi
health checker phát event chính thức tại +15.1s. Vì failover bắt đầu từ luồng automation,
critical path thực tế dùng timestamp failover, không cộng thêm 15.1s lần thứ hai.

| Thành phần trên critical path | Giây | Nguồn số liệu | Cách giảm |
|---|---:|---|---|
| Xác nhận outage và verify target | 13.40s | `1_verify_target.ts - t_outage` | Giảm interval nhưng phải giữ threshold/circuit breaker để tránh flapping (`reports/failover-events.jsonl:1`) |
| Restore snapshot | 0.02s | `2_restore_snapshot.ts - 1_verify_target.ts` | Snapshot nhỏ hơn hoặc giữ warm replica (`reports/failover-events.jsonl:2`) |
| Scale và GPU pool warm-up | 6.86s | `4_wait_ready.ts - 2_restore_snapshot.ts` (`waited_s=6.84`) | Duy trì warm capacity hoặc pre-warm model (`reports/failover-events.jsonl:4`) |
| DNS/LB TTL và lần request kế tiếp | 4.10s | `t_recovered - 5_dns_cutover.ts` | Giảm TTL hoặc dùng global LB health routing (`reports/drill-2-withdr.jsonl:24`) |
| **Tổng critical path (sai số làm tròn)** | **24.38s ≈ 24.4s** | Timestamp thật | — |

Health-check detect floor cấu hình là **5.0s × 3 = 15.0s**, được ghi trực tiếp trong
event phát hiện (`reports/health-events.jsonl:2`). Thời điểm quan sát thực tế là +15.1s.

## 4. Kết luận

Drill hợp lệ, không có warning, Region A bị kill và request phục hồi được phục vụ bởi
Region B. RTO 24.4s thấp hơn mục tiêu 300s một khoảng 275.6s.
