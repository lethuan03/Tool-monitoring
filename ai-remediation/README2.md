# README2 — AI có runbook + đề xuất lệnh sửa lỗi (người duyệt, có audit)

Phần mở rộng của [README.md](../README.md). README gốc mô tả hệ thống shop + monitoring + AI **chẩn đoán**. File này mô tả phần **thêm vào** để AI phân tích nhanh hơn và có thể **đề xuất lệnh sửa lỗi**, chạy sau khi người duyệt, mọi bước đều có log để kiểm tra lại.

Mục lục: [1. Ý tưởng](#1-ý-tưởng) · [1b. Vòng lặp AI ↔ người duyệt](#1b-vòng-lặp-ai--người-duyệt-chat-qua-telegram) · [2. Đã làm](#2-đã-làm) · [3. File rule](#3-file-rule-remediatorpolicyjson) · [4. Audit log](#4-audit-log) · [5. Bật / tắt](#5-bật--tắt) · [6. Bảo mật](#6-bảo-mật--vì-sao-thiết-kế-thế-này) · [7. Kiểm thử](#7-kiểm-thử) · [8. Sandbox test cục bộ](#8-sandbox-test-cục-bộ) · [9. Chưa làm](#9-chưa-làm--lưu-ý)

---

## 1. Ý tưởng

Trước đây: `Alertmanager → holmes-bridge → HolmesGPT (Bedrock) → Telegram` chỉ cho ra **chẩn đoán + gợi ý bằng chữ**.

Bây giờ thêm 3 thứ:

| # | Thêm gì | Để làm gì |
| - | ------- | --------- |
| 1 | **Runbook markdown** theo từng cảnh báo | AI biết cần query gì trước, nguyên nhân hay gặp, và việc nào được/không được đề xuất → phân tích nhanh và đúng hướng hơn |
| 2 | **File rule** `ai-remediation/remediator/policy.json` | Quy định lệnh nào được chạy, container nào được đụng |
| 3 | **Remediator + audit log** | Thực thi lệnh sau khi người duyệt, ghi lại mọi việc |

Nguyên tắc chính: **AI chỉ ĐỀ XUẤT, không bao giờ tự chạy.** Lệnh là mẫu cố định trong file rule, AI chỉ chọn tên (`action_id`) và container. Người bấm **Duyệt** trên Telegram thì code (không phải AI) mới chạy.

```
Alertmanager ──► holmes-bridge ──► HolmesGPT (đọc runbook + Prometheus + Loki)
                      │                     │
                      │ tin phân tích ◄─────┘
                      ▼                                           ┌──────────────────────────┐
                 Telegram                                         │ ai-remediation/remediator/policy.json   │
                      ▲                                           │ (action + container được │
                      │ 2 nút: ✅ Duyệt / ❌ Từ chối              │  phép, giới hạn)         │
holmes-bridge ──► remediator ──kiểm tra policy──►                 └──────────────────────────┘
 "restart_container     │
  ecommerce-backend"    │ người bấm Duyệt (đúng chat + đúng người)
                        ▼
                  docker-proxy (chỉ cho API container) ──► Docker ──► restart container
                        │
                        └──► audit log (file JSONL + stdout → Alloy → Loki)
```

## 1b. Vòng lặp AI ↔ người duyệt ("chat" qua Telegram)

Không có ô chat tự do; thay vào đó AI và người trao đổi qua **nút bấm**, lặp lại tới khi xong hoặc người dừng:

```
cảnh báo ─► AI (vòng 1: runbook + Prometheus + Loki) ─► đề xuất lệnh ─► policy ─► Telegram [✅ Duyệt][❌ Từ chối][⏹ Dừng]
   ▲                                                         │ bị chặn → 💡 lệnh thay thế (mỗi cái 1 nút Duyệt, vẫn qua policy)
   │                                                         ▼ Duyệt
   │                                        chạy lệnh (docker-proxy) ─► kiểm tra Prometheus ─► báo kết quả
   │                                                         │ chưa ổn / vừa xem log / người bấm "🔁 Hỏi AI cách khác"
   └──── vòng n+1: AI nhận LỊCH SỬ lệnh + kết quả ◄──────────┘ (tối đa AI_MAX_ROUNDS vòng, mặc định 3)
```

| Ý của bạn | Cách làm |
| --------- | -------- |
| 1. Alert gửi cho AI | Alertmanager → `holmes-bridge` → HolmesGPT (như cũ) |
| 2. AI đọc markdown (lỗi, nguyên nhân, lệnh xử lý) | `holmes-bridge/runbooks/<alertname>.md` chèn vào prompt; vòng sau chèn lại + lịch sử lệnh đã chạy |
| 3. Hỏi xác nhận Telegram trước khi chạy | Nút ✅ Duyệt (ký HMAC, hết hạn 10 phút, dùng 1 lần, đúng chat/người) |
| 4. Kiểm tra lệnh có bị denied không, báo admin + lệnh thay thế | `check_proposal` theo `policy.json`; chặn → tin "🚫 Policy chặn" kèm **mục `alternatives`** (bảng viết tay, không tốn Bedrock). Dòng AI viết sai dạng (vd `docker exec …`) cũng được báo, không còn bị bỏ im lặng |
| 5. Chạy xong báo giải quyết được chưa | Đợi 15 giây, hỏi Prometheus `up == 0`: ổn → "🏁 đã ổn" (đợi tin RESOLVED để chắc chắn); chưa ổn → quay lại AI |
| 6. Vòng lặp, thoát khi người muốn | Nút **⏹ Dừng** trên mọi tin, lệnh **`/stop`** (hoặc `/stop <tên_cảnh_báo>`), nút **🔁 Hỏi AI cách khác** sau khi từ chối |

**Gõ lệnh tay trong chat (không cần AI):** bạn gõ lệnh, bot hiện lại đúng lệnh đó kèm nút ✅ Duyệt / ❌ Từ chối rồi mới chạy.

| Gõ | Việc làm |
| -- | -------- |
| `/restart <container>` | Khởi động lại (`restart_container`) |
| `/start <container>` | Bật lại container đã dừng (`start_container`) |
| `/logs <container>` | Xem 100 dòng log cuối (`logs_tail`) |
| `/stop [tên_cảnh_báo]` | Dừng vòng xử lý tự động của AI |
| `/help` | Liệt kê lệnh |

Tên có thể viết tắt: `/restart backend` = `ecommerce-backend`. Gõ kiểu terminal cũng nhận: `docker restart backend`, `docker logs --tail 50 nginx` (cờ bị bỏ qua, lệnh thật luôn lấy từ mẫu cố định trong policy).

**Lệnh bị chặn → báo "⛔ KHÔNG ĐƯỢC PHÉP", không chạy gì, không chìa nút khác:** container trong `containers_forbidden` (vd `/restart db`, `/logs db`), container lạ, tên chứa ký tự lạ (`;`, `$(...)`, `../`), và mọi động từ docker ngoài restart/start/logs (`docker stop|rm|kill|exec|system prune|compose down`...). Mỗi lần ghi audit `denied` (có `raw` = lệnh đã gõ). Người lạ gõ lệnh thì bị im lặng bỏ qua và ghi `unauthorized_command`. Chữ thường không phải lệnh (vd "xin chào") bị bỏ qua. AI không có đường nào chạy lệnh: nó chỉ đề xuất, và đề xuất của AI bị chặn cũng theo cùng policy. Lệnh gõ tay đi qua **đúng đường của đề xuất AI**: kiểm tra `policy.json` (container cấm vẫn bị chặn kèm gợi ý thay thế), cooldown/giới hạn giờ, nút ký HMAC dùng 1 lần, chỉ đúng chat và `REMEDIATOR_APPROVER_IDS` gõ được (người lạ ghi audit `unauthorized_command`), ghi audit `manual_command` + `proposed` + `executed`. Khác với đề xuất của AI: **không gọi Bedrock, không có vòng lặp AI** và không có nút "Hỏi AI cách khác". Hạn chế: chưa có ô chat tự do cho AI (hỏi bằng chữ như "vì sao backend chậm?" cần thêm `/ask`, tốn Bedrock, chưa làm).

**Khi nào vòng kết thúc** (luôn có tin Telegram nói rõ): bấm ⏹ / gõ `/stop` · cảnh báo RESOLVED (bridge gọi `POST /close`, đề xuất đang chờ bị huỷ) · hệ thống đã ổn sau lệnh · hết `AI_MAX_ROUNDS` · hết hạn duyệt (10 phút) · AI trả `không có` · chế độ dry-run (không chạy thật thì không có kết quả để AI xem) · từ chối mà không chọn "hỏi cách khác".

**Cách hai service nói chuyện:** remediator → `POST holmes-bridge:8080/continue` `{fingerprint, round, history[], status, down_jobs[]}`; bridge → `POST remediator:8090/propose` (có `round`, và `raw` khi AI viết sai dạng) và `POST remediator:8090/close`. Chỉ trong mạng nội bộ `monitoringnet`.

**Chi phí:** mỗi vòng = **1 lượt Bedrock** (tính phí) và tính vào `AI_MAX_PER_HOUR`. Trần mỗi sự cố = `AI_MAX_ROUNDS` lượt (mặc định 3; đặt `1` để tắt vòng lặp, quay về hành vi 1 đề xuất). Lệnh thay thế **không** gọi AI.

**Lưu ý khi dùng `/stop`:** trong group, bot chỉ thấy tin dạng lệnh (`/stop`) khi bật privacy mode mặc định; trong chat riêng thì thấy hết. Chỉ chat `TELEGRAM_CHAT_ID` và (nếu đặt) user trong `REMEDIATOR_APPROVER_IDS` mới dừng được; người khác bị ghi audit `unauthorized_stop`.

## 2. Đã làm

### 2.1. Runbook cho AI — `ai-remediation/holmes-bridge/runbooks/`

9 file markdown, mỗi file một cảnh báo trong `monitoring/prometheus/rules/alerts.yml`:
`TargetDown`, `NginxDown`, `MySQLDown`, `High5xxRate`, `HighLatencyP95`, `MySQLTooManyConnections`, `HostHighCpu`, `HostHighMemory`, `HostDiskAlmostFull` (dùng chung cho `HostDiskFull`).

Mỗi runbook gồm: **điều tra trước** (query PromQL/LogQL cụ thể, chỉ đọc) · **nguyên nhân thường gặp** · **hành động có thể đề xuất** (và những việc **cấm** đề xuất, ví dụ restart `ecommerce-db`).

Cách hoạt động: `ai-remediation/holmes-bridge/bridge.py` đọc file theo `alertname` rồi chèn vào prompt trước khi gọi HolmesGPT (hàm `load_runbook`, `build_prompt`). Không phụ thuộc cơ chế runbook riêng của HolmesGPT nên không bị vỡ khi đổi phiên bản. `alertname` chỉ được dùng nếu gồm chữ/số/`_`, không thể đọc file ngoài thư mục runbook. Thêm cảnh báo mới → thêm 1 file `<alertname>.md`.

### 2.2. Bridge đề xuất lệnh — `ai-remediation/holmes-bridge/bridge.py`

- Khi `REMEDIATOR_URL` có giá trị, prompt yêu cầu AI thêm 1 dòng cuối: `HÀNH ĐỘNG ĐỀ XUẤT: <action_id> <container>` hoặc `không có`.
- `extract_action` bóc dòng đó khỏi tin Telegram phân tích, chỉ nhận đúng dạng `action_id ecommerce-xxx`, rồi `send_proposal` gửi cho remediator.
- Remediator lỗi/tắt **không** làm hỏng tin phân tích. Bỏ trống `REMEDIATOR_URL` = hành vi y như trước.
- Không thêm lần gọi Bedrock nào (dòng đề xuất nằm trong cùng 1 lần phân tích).

### 2.3. Remediator — `ai-remediation/remediator/`

| File | Vai trò |
| ---- | ------- |
| `remediator.py` | Nhận đề xuất `POST /propose`, kiểm tra policy, gửi Telegram xin duyệt, thực thi, ghi audit. Chỉ dùng thư viện chuẩn Python |
| `policy.json` | File rule (mục 3) |
| `Dockerfile` | Image `thuanperry/ecommerce-remediator`, chạy user thường (uid 10002) |
| `tests/test_remediator.py` | 44 test chạy cục bộ (mục 7) |

Luồng một đề xuất:

1. Kiểm tra policy: action có khai báo? container nằm trong danh sách cho phép và không bị cấm? Không → từ chối + ghi audit + báo Telegram "Policy chặn".
2. Kiểm tra: công tắc tắt khẩn cấp, trùng đề xuất, số đề xuất đang chờ, cooldown/giới hạn theo giờ.
3. Gửi Telegram kèm nút **✅ Duyệt / ❌ Từ chối**. Nút được ký HMAC, hết hạn sau 10 phút, dùng **một lần**.
4. Chỉ chấp nhận bấm từ đúng chat `TELEGRAM_CHAT_ID` (và đúng user trong `REMEDIATOR_APPROVER_IDS` nếu có đặt). Bấm sai/giả chữ ký → ghi audit, không chạy.
5. Duyệt → kiểm tra lại policy và giới hạn → chạy qua `docker-proxy`. Sau khi restart đợi 15 giây, hỏi lại Prometheus (`up == 0`) và báo kết quả lên Telegram.

### 2.4. docker-proxy — `docker-compose.monitoring.yml`

Dùng image `tecnativa/docker-socket-proxy:0.3.0`. Chỉ cho API container (xem, start, restart); tắt hẳn exec, image, volume, network, swarm, stop... Nằm trên mạng `dockernet` (`internal: true`, không ra internet) chỉ có remediator. Holmes và bridge **không** có đường tới Docker.

### 2.5. File đã sửa/thêm

| File | Thay đổi |
| ---- | -------- |
| `ai-remediation/holmes-bridge/runbooks/*.md` (9 file) | Mới |
| `ai-remediation/holmes-bridge/bridge.py`, `ai-remediation/holmes-bridge/Dockerfile` | Chèn runbook; bóc và gửi đề xuất; `COPY runbooks` |
| `ai-remediation/remediator/` (4 file) | Mới |
| `docker-compose.monitoring.yml` | Thêm `docker-proxy`, `remediator` (profile `ai`), mạng `dockernet`, volume `remediator-audit`, biến `REMEDIATOR_URL` cho bridge |
| `.env.monitoring.example` | Thêm 5 biến (mục 5) |
| `scripts/push-images.ps1` | Thêm image `ecommerce-remediator` |
| `monitoring/alloy/config.remote.alloy` | Thêm `remediator` vào danh sách service được gom log; parse audit JSON, gắn nhãn `event`, đặt `level` (mục 4.1) |
| `monitoring/grafana/dashboards/shop-logs.json` | Thêm hàng **Remediation** gồm 7 ô (mục 4.2) |
| `ai-remediation/sandbox/` (mới) | Môi trường test cục bộ trên Docker local: compose, 2 mock, policy sandbox, script kịch bản (mục 8) |
| `README.md`, `TRA-CUU-NHANH.md` | Thêm trỏ sang README2, dòng lệnh, container mới, cấu trúc thư mục |
| `CLAUDE.md` | Quy tắc làm việc cho Claude Code trên project này |

## 3. File rule — `ai-remediation/remediator/policy.json`

Nguồn sự thật duy nhất. Sửa xong phải **build lại image remediator** (không bind-mount, đúng phong cách các service khác).

| Mục | Ý nghĩa | Hiện tại |
| --- | ------- | -------- |
| `containers_allowed` | Chỉ container trong danh sách này được đụng | backend, frontend, nginx, 3 exporter, alloy, prometheus, loki, grafana, alertmanager |
| `containers_forbidden` | **Luôn bị từ chối**, thắng cả `allowed` | `ecommerce-db`, `holmes-bridge`, `remediator`, `docker-proxy` |
| `actions` | Các `action_id` hợp lệ; mỗi cái là **1 mẫu lệnh Docker API cố định** + giới hạn | xem bảng dưới |
| `approval_ttl_seconds` | Nút duyệt hết hạn sau | 600 |
| `actions.<id>.allow_on_forbidden` | **Ngoại lệ hẹp**: action này được chạy trên đúng các container liệt kê dù chúng nằm trong `containers_forbidden` (và không cần có trong `containers_allowed`). Chỉ dùng cho action không phá hoại | `start_container` → `ecommerce-db` (chỉ **bật lại** DB đã dừng; `restart_container` và `logs_tail` trên DB vẫn bị cấm) |
| `max_pending` | Số đề xuất tối đa đang chờ duyệt | 10 |
| `alternatives` | **Lệnh gợi ý** khi đề xuất bị chặn: khoá = container bị chặn (hoặc `_default`), `{container}` = container AI đề xuất. Mỗi gợi ý **vẫn phải qua `check_proposal` + cooldown** (không nới luật) và vẫn cần người bấm Duyệt | `ecommerce-db` → `logs_tail` backend, `restart_container` backend; mặc định → `logs_tail` (chỉ đọc) |

| action_id | Lệnh Docker API | Cooldown | Tối đa/giờ/container | Ghi chú |
| --------- | --------------- | -------- | -------------------- | ------- |
| `restart_container` | `POST /containers/{container}/restart?t=10` | 300 giây | 3 | |
| `start_container` | `POST /containers/{container}/start` | 120 giây | 3 | |
| `logs_tail` | `GET /containers/{container}/logs?tail=100` | 0 | 10 | Chỉ đọc, kết quả gửi Telegram |

Vì sao cấm `ecommerce-db`: restart MySQL có rủi ro mất/hỏng dữ liệu, cần người quyết định. Riêng việc **bật lại DB đang dừng** (`start_container`) được cho phép qua `allow_on_forbidden`, vì DB không chạy thì không còn gì để mất thêm, dữ liệu nằm trong volume, và vẫn cần người bấm Duyệt. Runbook `MySQLDown` dặn AI chỉ đề xuất việc này khi có bằng chứng DB đã dừng (log "Shutdown complete", exporter báo `lookup db: no such host`). Vì sao cấm `holmes-bridge`, `remediator`, `docker-proxy`: để AI không tự tắt chính hệ thống kiểm soát nó.

Action nào **không khai báo** thì không chạy được (kể cả `rm`, `exec`, `prune`, `down -v`...). Muốn thêm action mới: thêm vào `policy.json` **và** cho phép đúng API tương ứng ở `docker-proxy`.

## 4. Audit log

Mỗi sự kiện là 1 dòng JSON, ghi vào **2 nơi**: file `/audit/audit.jsonl` (volume `remediator-audit`) và stdout của container (Alloy thu vào Loki, xem được ở Grafana với `{container="ecommerce-remediator"}`).

| `event` | Khi nào |
| ------- | ------- |
| `proposed` | AI đề xuất hợp lệ, đã gửi Telegram xin duyệt |
| `denied` | Bị chặn: policy / kill switch / quá giới hạn / quá nhiều đang chờ / không gửi được Telegram |
| `rejected` | Người bấm Từ chối |
| `expired` | Hết hạn không ai bấm |
| `approved_dry_run` | Đã duyệt nhưng chế độ dry-run nên **không chạy** |
| `executed` | Đã chạy thật (kèm `http_status`, `success`, `duration_s`, `output_tail`) |
| `unauthorized_click` / `invalid_click` | Bấm từ sai chat / sai người / sai chữ ký |
| `alternatives_offered` | Đề xuất bị chặn, đã gửi kèm các lệnh thay thế (`alternatives=[…]`) |
| `loop_continue` | Đã gửi kết quả lệnh cho AI để đề xuất vòng sau |
| `loop_ended` | Vòng kết thúc (`why`: hệ thống đã ổn / hết số vòng / AI không nhận yêu cầu) |
| `loop_stopped` | Người bấm ⏹ hoặc gõ `/stop` (`approver` = user id) |
| `loop_closed` | Bridge báo cảnh báo đã RESOLVED, đề xuất đang chờ bị huỷ (`cancelled_pending`) |
| `followup_requested` | Sau khi từ chối, người bấm "🔁 Hỏi AI cách khác" |
| `unauthorized_stop` | Gõ `/stop` từ sai chat / sai người |
| `manual_command` | Người gõ `/restart` / `/start` / `/logs` trong chat (`approver`, `action_id`, `container`) |
| `unauthorized_command` | Gõ các lệnh trên từ sai chat / sai người |

Các trường: `ts` (giờ VN), `mode`, `id`, `round` (vòng thứ mấy), `raw` (dòng AI viết sai dạng, ≤100 ký tự), `alert`, `fingerprint`, `action_id`, `container`, `ai_reason` (lý do AI nêu), `approver` (Telegram user id người bấm), `command`, `why` (lý do bị chặn). Không ghi token/khoá (có test kiểm tra).

Xem nhanh trên EC2: `docker exec ecommerce-remediator tail -n 20 /audit/audit.jsonl`

### 4.1. Đường đi của audit tới Loki (Alloy)

`monitoring/alloy/config.remote.alloy` trước đây chỉ gom log các service của app compose, nên log `remediator` sẽ **không** vào Loki. Đã sửa:

- Thêm `remediator` vào regex `keep` (nhớ: service mới thì phải thêm vào đây, xem README mục 8).
- Block parse riêng cho `{service="remediator"}`: bóc `event` và `ts` từ JSON, dùng `ts` làm thời gian của dòng log, gắn nhãn **`event`** (chỉ ~16 giá trị cố định nên không gây cardinality explosion, đúng nguyên tắc README mục 4.6c).
- `level` = `warn` cho `denied`/`rejected`/`expired`/`unauthorized_click`/`invalid_click`/`unauthorized_stop`, còn lại `info`. Các `event` mới của vòng lặp (`loop_*`, `alternatives_offered`, `followup_requested`) chưa có ô riêng trên dashboard; xem bằng `{service="remediator", event=~"loop_.*|alternatives_offered"} | json`. Các trường khác (`alert`, `container`, `approver`...) bóc lúc truy vấn bằng `| json`.
- Dòng log thường của remediator (không phải JSON) vẫn đi qua, mức `info`.

LogQL mẫu: `{service="remediator", event!=""} | json` · chỉ lệnh đã chạy thật: `{service="remediator", event="executed"} | json`.

### 4.2. Panel Grafana — dashboard **Shop Logs**, hàng "Remediation"

Thêm 7 ô ở cuối `monitoring/grafana/dashboards/shop-logs.json` (id 40–46):

| Ô | Cho biết |
| -- | -------- |
| AI đề xuất lệnh | Số đề xuất hợp lệ trong khung thời gian |
| Đã duyệt (chạy / dry-run) | Số lệnh người đã bấm Duyệt |
| Bị chặn / từ chối / hết hạn | Vàng khi ≥ 1. Tăng nhiều = AI đề xuất không hợp lý, cần chỉnh runbook |
| Bấm trái phép / sai chữ ký / `/stop` trái phép | **Đỏ khi ≥ 1**, bình thường phải bằng 0 |
| Sự kiện audit theo loại | Biểu đồ cột theo `event` mỗi 5 phút |
| Nhật ký audit lệnh sửa lỗi | Dòng log đã định dạng: `event \| alert \| action container \| approver \| lệnh/lý do \| mode` |

Cần build lại image `ecommerce-grafana` thì Grafana mới thấy hàng này (dashboard nằm trong image).

## 5. Bật / tắt

Mặc định **tắt hoàn toàn**: bỏ trống `REMEDIATOR_URL` thì bridge không đề xuất gì.

Biến trong `.env.monitoring` (mẫu ở `.env.monitoring.example`):

| Biến | Ý nghĩa |
| ---- | ------- |
| `REMEDIATOR_URL` | `http://remediator:8090` để bật đề xuất. Trống = tắt |
| `REMEDIATION_MODE` | `dry-run` (mặc định): duyệt xong **chỉ ghi nhận**, không chạy. `execute`: chạy thật |
| `REMEDIATION_ENABLED` | `false` = công tắc tắt khẩn cấp, từ chối mọi đề xuất mới |
| `REMEDIATOR_SECRET` | Khoá ký nút bấm (chữ + số, ≥ 32 ký tự). Trống = sinh ngẫu nhiên mỗi lần khởi động |
| `REMEDIATOR_APPROVER_IDS` | Telegram user id được bấm Duyệt / gõ `/stop`. **Nên điền** (trong group chat, trống nghĩa là ai cũng bấm được) |
| `AI_MAX_ROUNDS` | Số vòng AI tối đa cho 1 sự cố (mặc định 3, **mỗi vòng = 1 lượt Bedrock**). `1` = tắt vòng lặp. Compose truyền cho cả `holmes-bridge` và `remediator` |

Cách triển khai an toàn, từng bước:

1. Build + push image (máy local): `.\scripts\push-images.ps1`. Các image bị ảnh hưởng: `holmes-bridge` (runbook + đề xuất), `remediator` (mới), `alloy` (thu log audit), `grafana` (panel mới).
2. Trên EC2: `$APP pull && $APP up -d` (lấy Alloy mới), `$MON pull`. Điền biến ở trên, giữ `REMEDIATION_MODE=dry-run`, rồi bật: `$MON --profile ai up -d --force-recreate holmes-bridge remediator docker-proxy grafana`.
3. Chạy dry-run vài hôm, đọc audit xem AI đề xuất gì, có hợp lý không.
4. Chuyển `REMEDIATION_MODE=execute`, thử trước trên container ít rủi ro.
5. Tắt khẩn cấp: `REMEDIATION_ENABLED=false` rồi `$MON up -d --force-recreate remediator`, hoặc `$MON stop remediator`.

## 6. Bảo mật — vì sao thiết kế thế này

| Rủi ro | Cách giảm |
| ------ | --------- |
| AI bị lừa (log chứa lệnh giả, prompt injection) hoặc suy luận sai | AI chỉ chọn `action_id` có sẵn; không có chỗ nào để chèn lệnh tự do; **người duyệt từng lệnh** |
| `docker.sock` = quyền root | Chỉ `docker-proxy` mount socket, chặn gần hết API, mạng nội bộ không ra internet; AI/Holmes không chạm Docker |
| Duyệt giả / bấm nhầm người | Nút ký HMAC, hết hạn, dùng 1 lần, kiểm tra chat id và user id |
| Restart lặp vô hạn | Cooldown + giới hạn theo giờ cho mỗi (action, container), kiểm tra lại cả lúc duyệt |
| Sửa nhầm DB / tự tắt giám sát | Danh sách `containers_forbidden` |
| Không biết ai đã làm gì | Audit đầy đủ cả đề xuất bị chặn / từ chối / hết hạn |
| Tốn tiền Bedrock | Vòng lặp tối đa `AI_MAX_ROUNDS` lượt/sự cố, mỗi lượt tính vào `AI_MAX_PER_HOUR`; lệnh thay thế không gọi AI; ⏹ Dừng, RESOLVED, dry-run đều chặn vòng tiếp |
| AI lặp lại lệnh vô ích | Prompt vòng sau ghi rõ lịch sử + "chỉ đề xuất lệnh KHÁC"; cooldown/giới hạn giờ trong policy vẫn chặn, và remediator báo lý do lên Telegram |
| Người lạ dừng/điều khiển vòng lặp | `/stop` và nút ⏹ kiểm tra chat id + user id như nút Duyệt; sai → audit `unauthorized_stop` |
| Gợi ý thay thế thành lỗ hổng | Gợi ý chỉ là *đề xuất*: phải qua `check_proposal`, cooldown, và người bấm Duyệt đúng lệnh đó (nút ký HMAC, dùng 1 lần) |

Hạn chế cần biết (**đã xác nhận bằng sandbox, mục 8**): `docker-proxy` bật `CONTAINERS=1` + `POST=1` nên client của nó còn gọi được vài API container khác, **kể cả tạo container mới** (HTTP 201). Vì vậy chỉ remediator được nối vào `dockernet`, và code remediator chỉ gọi đúng đường dẫn mẫu trong `policy.json`. Cần bảo vệ image/mã remediator như bảo vệ quyền root.

## 7. Kiểm thử

Chạy cục bộ, không cần mạng/Docker/Telegram/AWS:

```bash
python -m unittest discover -s ai-remediation/remediator/tests -v
```

```bash
python -m unittest discover -s ai-remediation/holmes-bridge/tests -v
```

**44 test remediator + 14 test bridge, pass hết.** Phần vòng lặp / lệnh thay thế: bảng `alternatives` không chứa gợi ý bị policy chặn; bị chặn → có nút thay thế, bấm 1 cái thì các gợi ý còn lại bị huỷ; dòng AI sai dạng vẫn được báo; lệnh chưa ổn → gọi bridge kèm lịch sử; ổn / dry-run / hết vòng → không gọi; ⏹ và `/stop` huỷ đề xuất và chặn vòng sau (HTTP 409), người lạ bị bỏ qua; từ chối → menu "hỏi cách khác"; `/close` huỷ đề xuất; bridge: `extract_action`, prompt vòng sau, `/continue` chống trùng + giới hạn vòng + giới hạn giờ; mọi `action_id` trong runbook tồn tại trong policy và runbook không dạy lệnh trên container bị cấm. Phủ từ trước: policy (container cấm / lạ / chèn lệnh `; rm -rf`, `../`), cooldown + giới hạn giờ, chữ ký nút, đề xuất → duyệt → dry-run không gọi Docker, execute chỉ chạy 1 lần dù bấm 2 lần, từ chối, hết hạn, sai chat/sai người, giả chữ ký, công tắc tắt khẩn cấp, audit không lộ token, giải mã log Docker.

Ngoài ra đã kiểm tra: `bridge.py` biên dịch được, `extract_action` bóc đúng/từ chối dòng sai dạng, prompt chèn runbook đúng, file compose hợp lệ về cú pháp YAML.

## 8. Sandbox test cục bộ

Thư mục [sandbox/](sandbox/) dựng một bản thu nhỏ **chạy trên Docker local**, để thử cả luồng mà không cần EC2, Telegram, Bedrock hay AWS.

| Thành phần | Thật hay giả |
| ---------- | ------------ |
| `holmes-bridge` (`bridge.py` + 9 runbook) | **Thật** (cùng file với production) |
| `remediator` + `policy` | **Thật** (cùng code; dùng `ai-remediation/sandbox/policy.sandbox.json` chỉ cho phép 2 container đồ chơi) |
| `docker-proxy` (`tecnativa/docker-socket-proxy:0.3.0`, cùng biến môi trường, mạng `dockernet` nội bộ) | **Thật**, nói chuyện với Docker local |
| "AI" (`ai-remediation/sandbox/mock/mock_holmes.py`) | **Giả**: trả sẵn phân tích + dòng `HÀNH ĐỘNG ĐỀ XUẤT`, báo lại prompt có chứa runbook / lịch sử lệnh không; vòng n lấy hành động từ nhãn `sbx_action<n>`. **Không gọi Bedrock, không tốn tiền** |
| Telegram + Prometheus (`ai-remediation/sandbox/mock/server.py`) | **Giả**: ghi lại tin nhắn, giả lập người bấm nút (`/_click`), người gõ chữ (`/_say`), job `up == 0` (`/_down`) |
| Nạn nhân | 3 container `alpine sleep` tên `ecommerce-sbx-backend`, `ecommerce-sbx-nginx`, `ecommerce-sbx-db` (tên khác hẳn container thật của project) |

```bash
python ai-remediation/sandbox/run_sandbox.py          # dựng + chạy toàn bộ kịch bản (lần đầu pull/build vài phút)
python ai-remediation/sandbox/run_sandbox.py up       # chỉ dựng
python ai-remediation/sandbox/run_sandbox.py test     # chỉ chạy kịch bản (sandbox đã dựng)
python ai-remediation/sandbox/run_sandbox.py down     # dọn sạch container/mạng/volume của sandbox
```

Chỉ cần Python + Docker; chỉ publish port trên `127.0.0.1` (18080 bridge, 18081 mock). Sandbox chạy theo project compose riêng `ai-sandbox`, `down` xoá đúng phần đó.

**Kịch bản (62 kiểm tra, đã chạy: 62/62 đạt):**

| Pha | Kịch bản | Kiểm tra |
| --- | -------- | -------- |
| Dry-run | A1–A2 | Cảnh báo → tin phân tích **có runbook**, dòng hành động bị bóc khỏi tin; tin xin duyệt có 3 nút (Duyệt / Từ chối / Dừng); bấm Duyệt chỉ ghi `approved_dry_run`, **container không bị restart** |
| | A3 | AI đòi restart DB → policy chặn, không có nút duyệt cho DB, **có lệnh thay thế** (không cái nào đụng DB), DB không bị đụng |
| | A4 | Sai người / sai chat bấm Duyệt → `unauthorized_click` ×2; người đúng bấm Từ chối → `rejected` |
| | A5 | AI nói "không có" hành động → chỉ có tin phân tích |
| Execute | B1 | Duyệt restart → docker-proxy trả HTTP 204, **container thật sự restart** (`StartedAt` đổi), báo kiểm tra lại Prometheus, audit `executed` đúng lệnh + người duyệt |
| | B2 | Đề xuất restart lại ngay → chặn bởi cooldown, không restart lần 2 |
| | B3 | `logs_tail` → log gửi lên Telegram, không restart |
| | B4 | DB vẫn bị cấm khi ở chế độ execute |
| | B5 | Gọi thẳng remediator với container của project khác (`freshmart-app-1`) → HTTP 403, không bị đụng |
| | B6 | Từ bên trong mạng `dockernet`, proxy chặn `/images`, `/volumes`, `/info`, `/exec/<id>/start` (403) |
| Vòng lặp | C1 | Bị chặn → bấm 1 lệnh thay thế → **chạy thật** (log backend), DB không bị đụng |
| | C2 | AI viết lệnh sai dạng → admin vẫn được báo "Policy chặn" + gợi ý (không im lặng) |
| | C3 | 3 vòng: restart nginx (Prometheus giả báo backend vẫn chết) → AI nhận **lịch sử lệnh** → đề xuất `logs_tail` (vòng 2/3) → duyệt → AI hết cách, vòng kết thúc; audit ghi `round` 1 rồi 2 |
| | C4 | Bấm **⏹ Dừng** → huỷ đề xuất, báo dừng, bấm Duyệt cũ không chạy gì |
| | C5 | Người lạ gõ `/stop` → `unauthorized_stop`, vòng vẫn chạy; người duyệt gõ `/stop` → dừng |
| | C6 | Cảnh báo RESOLVED → `loop_closed`, đề xuất đang chờ bị huỷ, vẫn có tin "đã hoạt động bình thường" |

**Điều sandbox đã xác nhận cho production:** biến `ALLOW_RESTARTS=1` trong `tecnativa/docker-socket-proxy:0.3.0` hoạt động (restart thật trả 204); đường audit/Telegram/dry-run/execute chạy đúng từ đầu tới cuối.

**Điều sandbox phát hiện:** docker-proxy với `CONTAINERS=1` + `POST=1` **cho phép tạo container mới** (`POST /containers/create` → 201). Đây là lỗ hổng *độ sâu phòng thủ*: bình thường remediator chỉ gọi 3 đường dẫn trong `policy.json`, nhưng nếu mã remediator bị chiếm thì có thể tạo + start container tuỳ ý (ví dụ mount `/` của máy). Script test in ra dòng `GAP`/`LỖ HỔNG ĐÃ BIẾT` thay vì tính là đạt. Hướng xử lý đề xuất: thay `tecnativa/docker-socket-proxy` bằng proxy allowlist chặt (chỉ khớp đúng `POST /containers/<tên cho phép>/restart|start` và `GET .../logs`). Xem mục 9.

**Lỗi của chính sandbox tìm ra trong lúc làm** (đã sửa): mock Telegram từng đặt lại bộ đếm `update_id` về 1 khi reset nên remediator bỏ qua nút bấm mới; Telegram thật không bao giờ làm vậy.

Giới hạn của sandbox: không kiểm tra Telegram/Bedrock thật (**chất lượng đề xuất của AI thật qua nhiều vòng chỉ biết khi chạy dry-run trên môi trường thật**), không kiểm tra `start_container` (chỉ `restart` và `logs`), `alloy`/`grafana` không nằm trong sandbox nên **panel audit và parse Alloy vẫn chưa được thử**.

## 9. Chưa làm / lưu ý

- **Chưa chạy thử thật** trên EC2/Telegram/Bedrock (bị cấm trong `CLAUDE.md` vì có thể tốn tiền hoặc đụng AWS). Đã thử cục bộ bằng sandbox (mục 8). Việc build image, push, triển khai và gọi `holmes ask` thật do người vận hành tự làm.
- **Lỗ hổng docker-proxy cho phép tạo container** (mục 8) chưa được vá. Trước khi bật `REMEDIATION_MODE=execute` trên EC2 nên thay bằng proxy allowlist chặt, hoặc chấp nhận rủi ro này một cách có ý thức.
- `start_container` chưa được thử với docker-proxy (biến `ALLOW_START`); `restart_container` và `logs_tail` đã được thử.
- **Cấu hình Alloy và dashboard Grafana mới chỉ kiểm tra cú pháp ở mức tĩnh** (JSON dashboard load được, id không trùng; Alloy sửa theo đúng mẫu các block có sẵn nhưng chưa có `alloy fmt` để chạy thử). Sau khi triển khai hãy mở Shop Logs → hàng Remediation và `docker logs ecommerce-alloy` để chắc không có lỗi parse.
- Panel Grafana dùng nhãn `event` do Alloy gắn: nếu quên rebuild/pull image `alloy` thì các ô sẽ hiện 0 / trống dù remediator có ghi audit (file audit vẫn đầy đủ).
- **Vòng lặp mới chỉ được thử với AI giả** (sandbox C1–C6 + unit test). Chưa biết AI thật (Bedrock) có đề xuất lệnh *khác* ở vòng sau hay lặp lại; cooldown và `AI_MAX_ROUNDS` là lưới an toàn. Nên chạy dry-run trước.
- Trạng thái vòng lặp (`loops`, `pending`, hàng đợi bridge) nằm trong bộ nhớ: restart `remediator`/`holmes-bridge` giữa chừng thì vòng đang chạy mất (audit vẫn còn). Nút Telegram cũ cũng mất hiệu lực nếu không đặt `REMEDIATOR_SECRET`.
- Gợi ý thay thế là **bảng viết tay** trong `policy.json`, không thông minh bằng AI; container nào chưa có khoá riêng chỉ nhận gợi ý `_default`.
- Chỉ kiểm tra được "có thành phần nào `up == 0` không"; lỗi logic (5xx, chậm) không nằm trong kiểm tra sau lệnh. "Đã ổn" chỉ chắc chắn khi Alertmanager gửi RESOLVED.
- `/stop` trong group cần bot thấy được tin lệnh (privacy mode); chưa thử với Telegram thật.
- Chất lượng đề xuất của AI phụ thuộc vào model và runbook; cần theo dõi giai đoạn dry-run để chỉnh runbook.
- Đề xuất lặp trong cùng 1 sự cố bị bỏ qua (đã chống trùng); cảnh báo mới (startsAt khác) là một sự cố mới.
- `logs_tail` gửi tối đa 1500 ký tự log lên Telegram: log có thể chứa thông tin nhạy cảm, cân nhắc trước khi duyệt.
