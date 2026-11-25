# README2 — AI có runbook + đề xuất lệnh sửa lỗi (người duyệt, có audit)

Phần mở rộng của [README.md](../README.md). README gốc mô tả hệ thống shop + monitoring + AI **chẩn đoán**. File này mô tả phần **thêm vào** để AI phân tích nhanh hơn và có thể **đề xuất lệnh sửa lỗi**, chạy sau khi người duyệt, mọi bước đều có log để kiểm tra lại.

Mục lục: [1. Ý tưởng](#1-ý-tưởng) · [1b. Vòng lặp AI ↔ người duyệt](#1b-vòng-lặp-ai--người-duyệt-chat-qua-telegram) · [1c. Cách 2: AI gọi công cụ](#1c-cách-2-ai-gọi-công-cụ-tool-api) · [2. Đã làm](#2-đã-làm) · [3. File rule](#3-file-rule-remediatorpolicyjson) · [4. Audit log](#4-audit-log) · [5. Bật / tắt](#5-bật--tắt) · [6. Bảo mật](#6-bảo-mật--vì-sao-thiết-kế-thế-này) · [7. Kiểm thử](#7-kiểm-thử) · [8. Sandbox test cục bộ](#8-sandbox-test-cục-bộ) · [9. Chưa làm](#9-chưa-làm--lưu-ý)

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

## 1c. Cách 2: AI gọi công cụ (tool API)

Cách 1 (cũ): AI ghi 1 dòng chữ `HÀNH ĐỘNG ĐỀ XUẤT: action container`, bridge bóc ra rồi gửi cho remediator. Cách 2 (mới, mặc định khi có `REMEDIATOR_TOOL_TOKEN`): AI **gọi công cụ** và nhận phản hồi luật **ngay trong phiên**, nhưng nguyên tắc an toàn không đổi: **AI không chạy được lệnh nào**; mọi thứ qua `policy.json` (check_proposal, cooldown) và lệnh thay đổi hệ thống vẫn cần bạn bấm ✅ trên Telegram.

```
Alertmanager ─► bridge ─► holmes ask (Bedrock) ──► [3 công cụ] ──► remediator /tool/*  (Bearer token)
                              env: AI_INCIDENT_FP/ROUND/ALERT        ├ read_status  (chỉ đọc, chạy ngay, kết quả đã LỌC trường)
                              (bridge đặt, AI không tự đặt được)      └ propose_fix  (tạo nút Duyệt; trả queued/denied+gợi ý)
Telegram: câu nói tự nhiên ─► remediator ─ từ khoá ─► nút Duyệt   |  câu lạ ─► bridge /chat ─► phiên AI nhỏ dùng công cụ
```

| Công cụ (AI thấy) | Làm gì | Qua remediator |
| ----------------- | ------ | -------------- |
| `list_allowed_actions` | Xem kho lệnh + container được phép | `GET /tool/catalog` |
| `read_status(action, container)` | `logs_tail`, `inspect_container` (chạy/dừng, mã thoát, OOM, health, số lần restart), `container_stats` (CPU/RAM). Chạy ngay, **không cần duyệt** | `POST /tool/read` |
| `propose_fix(action, container, reason)` | `restart_container`, `start_container`. **Không chạy ngay**: tạo nút ✅❌⏹ trên Telegram. Tối đa 1 đề xuất mỗi phiên. Bị chặn thì trả `denied` + lý do + `alternatives` (kèm `via`: dùng `read_status` hay `propose_fix`) để AI chọn lại | `POST /tool/propose` |

**Vì sao vẫn an toàn:**
- Xác thực `Authorization: Bearer REMEDIATOR_TOOL_TOKEN` (so sánh hằng thời gian). Token trống = `/tool/*` tắt hẳn (HTTP 503). Audit `tool_denied_auth`.
- **Danh tính sự cố (fingerprint, vòng, cảnh báo) không phải tham số của AI**: bridge đặt vào biến môi trường của tiến trình `holmes`, script công cụ đọc từ đó. AI không giả mạo được sự cố khác.
- AI chỉ chọn `action_id` từ danh sách (YAML giới hạn bằng `enum`, script và remediator đều kiểm tra lại); tham số vào shell đều qua `shlex.quote` của Holmes (đã đọc mã nguồn 0.45.0); **không bật bash** (`--bash-always-deny`).
- `read_status` chỉ nhận action `approval: none`; `propose_fix` từ chối action chỉ đọc. **`inspect_container` chỉ trả allowlist trường** (không bao giờ trả `Config.Env`, nơi chứa mật khẩu/khoá); có test khẳng định. Áp dụng cả khi người gõ `/status` (audit và Telegram cũng chỉ thấy bản đã lọc).
- Giới hạn mỗi phiên: 12 lần đọc (`TOOL_MAX_READS_PER_SESSION`), 4 lần bị chặn (`TOOL_MAX_DENIED_PER_SESSION`), 1 đề xuất; cooldown/giờ trong policy vẫn áp dụng.
- Lệnh phá hoại (stop, rm, exec, restart DB...) vẫn không có đường nào trong kho lệnh.

**Phiên AI không nhớ gì giữa các lần chạy** (`holmes ask` là tiến trình chạy một lần). Khi AI gọi `propose_fix` xong thì phiên kết thúc; bạn duyệt xong, lệnh chạy, kết quả quay lại bằng **phiên mới**. Vì vậy bridge lưu **bản tóm tắt phân tích** (Vấn đề / Nguyên nhân / Bằng chứng, ≤ 1.500 ký tự) của mỗi vòng và đưa vào prompt vòng sau, kèm lịch sử lệnh và bản đồ hệ thống: AI chỉ kiểm tra lại thay vì điều tra lại từ đầu.

**Bản đồ hệ thống cho AI:** `holmes-bridge/knowledge/system.md` (chuỗi phụ thuộc db → backend → nginx, bảng container ↔ job Prometheus ↔ nhãn Loki, điều "trông lạ nhưng bình thường", kho lệnh) chèn vào mọi prompt (giới hạn `AI_KNOWLEDGE_MAX_CHARS`, mặc định 4.500). Có test đối chiếu với `policy.json`, compose và `prometheus.yml` để file không dạy sai khi hệ thống đổi.

**Monitor qua Telegram (hỏi tình trạng, bot lấy số liệu báo lại ngay, không AI, không tốn token):** gõ `/status`, `docker ps`, hoặc nói `các container hiện tại thế nào?`, `hệ thống ổn không?`, `tổng quan`. Bot đọc song song `inspect_container` của từng container (đã lọc trường, không có biến môi trường) và hỏi Prometheus, rồi trả:

```
📊 Tình trạng hệ thống · 14:32:10 25/11
🟢 backend · running · chạy 2 giờ 5 phút · health healthy
🔴 db · exited · mã thoát 137
🟡 grafana · running · health unhealthy · đã restart 3 lần
...
Chạy: 10/12 container
🖥 Máy: CPU 12% · RAM 48% · ổ đĩa 33%
🌐 Web: 2.5 req/s · lỗi 5xx 0%
🚨 Cảnh báo đang bật: MySQLDown, High5xxRate
```
Hỏi 1 container: `/status db`, `/logs nginx`, "xem log backend" → **chạy ngay, không cần nút Duyệt** vì chỉ đọc (policy `approval: none`); người gõ không bị tính vào giới hạn đọc của AI. Container thuộc `containers_forbidden` vẫn bị chặn (vd `/logs db` → ⛔; riêng `/status db` được vì `inspect_container` có ngoại lệ). Lệnh làm thay đổi hệ thống (`/restart`, `/start`) vẫn hỏi duyệt. Prometheus lỗi thì các số hiện `n/a`, phần Docker vẫn có. Audit: `monitor_query`.

**Phiên làm việc với AI (`/ai`) — trò chuyện tự nhiên, AI nhớ ngữ cảnh:** gõ `/ai` để mở phiên (hoặc `/ai tôi muốn chạy lại container của backend` để mở và hỏi luôn). Trong phiên, **mọi tin nhắn thường đều tới AI** (kể cả "ok", "cái đó thì sao?"); AI tự đọc trạng thái hệ thống, hiểu "container của backend" là `ecommerce-backend`, đọc `inspect_container` rồi chọn `start_container` (đang dừng) hay `restart_container` (đang chạy), và **đề xuất kèm nút Duyệt** (AI không tự duyệt; nếu bạn nói "ok làm đi" nó chỉ nhắc bấm nút ✅).

| Lệnh | Việc |
| ---- | ---- |
| `/ai` · `/ai <câu hỏi>` | Mở phiên (có câu hỏi thì hỏi luôn). Chưa tốn token cho đến khi bạn nói |
| `/ai off` · `/exit` · `/thoat` | Kết thúc phiên, xoá trí nhớ |
| `/ai reset` | Quên hội thoại, giữ phiên mở |
| `/ai status` | Xem phiên đang mở hay đóng |

- **Trí nhớ:** vì mỗi lần chạy AI không nhớ gì, bridge giữ `AI_CHAT_HISTORY_TURNS` (8) lượt hỏi-đáp gần nhất theo mã phiên (`sid`) và nhét vào prompt, kèm **tình trạng hiện tại** (cảnh báo đang bật, đề xuất đang chờ duyệt, lệnh thay đổi vừa chạy) lấy từ Prometheus và `GET /tool/state`. Dữ liệu chỉ nằm trong bộ nhớ bridge, tự xoá khi đóng phiên hoặc sau `AI_CHAT_SESSION_TTL_SECONDS` (1 giờ).
- **Tự đóng** sau `AI_SESSION_IDLE_SECONDS` (900 giây = 15 phút) không nhắn; bot báo "phiên AI đã kết thúc".
- **Hỏi tổng quan chung** ("các container thế nào?") trong phiên vẫn được trả ngay bằng báo cáo miễn phí, không tốn lượt AI. Lệnh có cấu trúc (`/restart`, `/status`...) vẫn dùng được trong phiên và vẫn qua policy + nút Duyệt.
- **Chi phí:** mỗi tin trong phiên = 1 phiên AI nhỏ (tối đa `AI_CHAT_MAX_STEPS` = 6 bước). Có **ngân sách riêng** `AI_CHAT_MAX_PER_HOUR` (30), không ăn vào `AI_MAX_PER_HOUR` của phân tích sự cố; hết lượt thì bot báo. Chat chạy ở **luồng riêng** nên không phải chờ AI phân tích alert (và ngược lại).
- **An toàn như cũ:** chỉ đúng chat + `REMEDIATOR_APPROVER_IDS` mở/dùng phiên (người lạ bị bỏ qua, ghi `unauthorized_command`); AI chỉ có công cụ đọc và `propose_fix` (nút Duyệt); mọi lệnh qua `policy.json`. Mỗi tin có danh tính sự cố riêng nên giới hạn "1 đề xuất mỗi phiên AI" tính theo từng tin. Cần `REMEDIATOR_TOOL_TOKEN` (không có thì `/ai` báo chưa bật). Công cụ thêm: `system_overview` (báo cáo tổng quan trong 1 lần gọi).

**Chat 2 lớp (nói chuyện thì nhanh, việc vận hành thì dùng công cụ):** `holmes ask` mỗi tin mất 13-19 giây (khởi động tiến trình, nạp toolset, prompt dài, nhiều vòng gọi Bedrock) nên **chỉ dùng cho câu cần số liệu thật hoặc lệnh**. Câu còn lại đi lớp chat trực tiếp:

| Loại câu | Đường | Dự kiến |
| -------- | ----- | ------- |
| Lệnh rõ ràng ("khởi động lại back end"), hỏi tổng quan, lệnh nguy hiểm | Từ khoá trong remediator, không AI | ~0 giây |
| Hỏi về bot / cách dùng ("bạn làm được gì?"), kiến thức chung ("restart policy của docker là gì?"), "cảm ơn" | **Chat trực tiếp**: gọi thẳng mô hình đã cấu hình, không công cụ, chữ hiện dần | ~1-3 giây |
| Cần trạng thái/log/số liệu thật, điều tra sự cố, muốn bật/restart | **Holmes + công cụ** (đề xuất kèm nút Duyệt) | như trước |

- **Cùng một mô hình**: lớp chat trực tiếp dùng biến `MODEL` (= `BEDROCK_MODEL_ID`) giống Holmes qua `litellm` ngay trong bridge, **không có mô hình phụ**. Thư viện được nạp sẵn lúc khởi động để tin đầu tiên không chậm. Tắt bằng `AI_CHAT_DIRECT=false` (mọi tin đi Holmes).
- **Định tuyến** (`classify_route` trong `bridge.py`): tên chắc chắn của hệ thống (`ecommerce-*`, backend, db, tên cảnh báo...) hoặc cụm chỉ trạng thái ("sao rồi", "xem log", "cpu"...) → Holmes. Tên "chung" như nginx/prometheus/mysql chỉ coi là hỏi về hệ thống khi đi kèm từ chỉ trạng thái/hành động ("nginx sao rồi" → Holmes, "giải thích prometheus" → chat). Câu nối tiếp ngắn ("ok làm đi", "cái đó thì sao") sau một lượt vận hành vẫn đi Holmes.
- **Mô hình tự báo khi cần nâng cấp**: lớp chat được dặn "không có số liệu trực tiếp, không chạy được lệnh"; nếu câu cần dữ liệu thật hoặc lệnh, nó trả đúng `[[OPS]]` ở đầu câu → bridge chuyển sang Holmes và **không bao giờ hiện dấu hiệu** cho người dùng (đoán sai chỉ tốn thêm ~1-2 giây). Mô hình lỗi hoặc trả rỗng cũng tự chuyển sang Holmes.
- **Chữ hiện dần**: bridge gửi 1 tin rồi sửa (`editMessageText`) tối đa ~1 lần/giây (`AI_CHAT_STREAM_EDIT_SECONDS`) vì Telegram giới hạn tần suất sửa tin.
- **Trí nhớ dùng chung** cho cả hai đường (cùng lịch sử phiên `/ai`): hỏi chat xong rồi hỏi về hệ thống, Holmes vẫn thấy câu trước; ngược lại chat thấy cả câu đã hỏi Holmes.
- **An toàn**: lớp chat trực tiếp **không có công cụ nào** nên không thể đề xuất hay chạy lệnh dù bị gài prompt; không có số liệu thật nên được dặn không bịa; mô tả khả năng của bot lấy từ một hằng số có test đối chiếu với lệnh thật. Giới hạn `AI_CHAT_MAX_PER_HOUR` vẫn áp dụng cho mọi tin chat.
- **Bẫy đã gặp**: `Dockerfile` bridge từng ghi cứng `FROM ...holmes:0.45.0`, nên bridge dựng trên image holmes CŨ (không có toolset) và AI báo "công cụ không khả dụng". Nay mặc định `:latest` (khớp tag `push-images.ps1`), có test canh. Muốn chắc: `docker exec ecommerce-holmes-bridge holmes-entrypoint.sh toolset refresh` phải thấy `remediation/tools` **enabled**.

**Hiểu câu nói tự nhiên trên Telegram** (chỉ đúng chat + `REMEDIATOR_APPROVER_IDS`):
1. *Đường A, miễn phí, tất định*: bảng từ khoá tiếng Việt/Anh (bật/chạy/start, khởi động lại/restart, xem log, trạng thái; xóa/tắt/stop/kill → ⛔ không được phép) + tên container (viết tắt, không dấu). Hiểu rõ → hiện **đúng lệnh đã hiểu** kèm nút Duyệt; mơ hồ (nhiều container/việc, tối đa 3 cách hiểu) → mỗi cách hiểu một nút, bấm 1 nút thì các nút còn lại bị huỷ.
2. *Đường B, tốn 1 phiên AI nhỏ*: câu lạ nhưng có vẻ là yêu cầu hoặc câu hỏi ("vì sao backend chậm?") → remediator gọi `POST bridge /chat` (Bearer token; bridge chỉ nhận từ remediator) → phiên AI tối đa `AI_CHAT_MAX_STEPS` (6) bước, tính vào `AI_MAX_PER_HOUR` → AI dùng công cụ và trả lời lên Telegram; nếu đề xuất lệnh thì vẫn qua nút Duyệt.
Câu xã giao ("ok", "cảm ơn") và người lạ trong chat bị bỏ qua im lặng (không tốn token, không ghi audit).

**Đường lùi:** đặt `AI_USE_TOOLS=false` hoặc bỏ trống `REMEDIATOR_TOOL_TOKEN` (cả hai service) → quay về cách 1 (dòng chữ). Bridge ghi vào log khởi động đang dùng cách nào.

**Thứ tự tin Telegram:** vì AI gọi công cụ *trong* phiên, tin "AI ĐỀ XUẤT" có thể xuất hiện **trước** tin "AI PHÂN TÍCH" (tin phân tích gửi sau khi phiên kết thúc). Nội dung đề xuất đã có alert + lý do nên vẫn đủ ngữ cảnh.

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

**Proxy allowlist tự viết** (`ai-remediation/docker-proxy/proxy.py`, image `thuanperry/ecommerce-docker-proxy`), thay `tecnativa/docker-socket-proxy` (bật `POST=1` ở đó là cho **tạo và xoá container**, ghi file vào container... = root trên máy nếu remediator bị chiếm). Chỉ chuyển đúng 7 lời gọi, mọi thứ khác trả 403:

| Cho phép | Ràng buộc |
| -------- | --------- |
| `GET /_ping`, `GET /version` | kiểm tra sống |
| `GET /containers/<tên>/json` | inspect (remediator lọc trường) |
| `GET /containers/<tên>/logs` | chỉ `stdout`, `stderr`, `timestamps`, `tail` ≤ 1000; **cấm `follow`** |
| `GET /containers/<tên>/stats` | luôn ép `stream=false` |
| `POST /containers/<tên>/restart` | `t` ≤ 60; **cấm với container trong `RESTART_DENY`** (`ecommerce-db`) |
| `POST /containers/<tên>/start` | không tham số |

Tên phải bắt đầu bằng `ALLOW_PREFIX` (`ecommerce-`) và không nằm trong `DENY_CONTAINERS` (bridge, remediator, chính proxy). Không nhận thân yêu cầu, chặn `%`, `..`, `//`, tham số lặp. Container chạy `read_only`, `cap_drop: ALL`, `no-new-privileges`. Đây là lớp chặn **thứ hai**: luật chính vẫn ở `policy.json`, nhưng dù remediator bị chiếm cũng không làm được gì ngoài các việc trên. Test: `python -m unittest discover -s ai-remediation/docker-proxy/tests`. Nằm trên mạng `dockernet` (`internal: true`, không ra internet) chỉ có remediator. Holmes và bridge **không** có đường tới Docker.

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
| `actions.inspect_container` | Xem trạng thái container; **được phép trên `ecommerce-db`** qua `allow_on_forbidden` (biết DB còn chạy không; chỉ trả trường an toàn) | xem bảng action |
| `actions.<id>.allow_on_forbidden` | **Ngoại lệ hẹp**: action này được chạy trên đúng các container liệt kê dù chúng nằm trong `containers_forbidden` (và không cần có trong `containers_allowed`). Chỉ dùng cho action không phá hoại | `start_container` → `ecommerce-db` (chỉ **bật lại** DB đã dừng; `restart_container` và `logs_tail` trên DB vẫn bị cấm) |
| `max_pending` | Số đề xuất tối đa đang chờ duyệt | 10 |
| `actions.<id>.approval` | `none` = **chỉ đọc, không cần duyệt** (AI đọc qua `read_status`; người gõ vẫn hỏi duyệt); mặc định `required` | `logs_tail`, `inspect_container`, `container_stats` = `none`; `restart_container`, `start_container` = `required` |
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
| `tool_read` | AI đọc trạng thái/log qua công cụ (`action_id`, `container`, `http_status`) |
| `tool_propose` | AI đề xuất lệnh qua công cụ và đã gửi xin duyệt (`result=queued`) |
| `tool_denied_auth` | Gọi `/tool/*` thiếu/sai token, hoặc tool API đang tắt |
| `chat_request` | Câu nói tự nhiên được chuyển cho AI (đường B): `result=queued` hoặc lỗi |
| `monitor_query` | Người hỏi tình trạng tổng quan (`kind=overview`) |
| `ai_session_start` / `ai_session_end` / `ai_session_reset` | Mở / đóng (kèm `why`: người dùng hoặc hết hạn) / xoá trí nhớ phiên `/ai` (`session` = mã phiên) |
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
| `REMEDIATOR_TOOL_TOKEN` | Token chung của `holmes-bridge` và `remediator` cho tool API (chữ + số, ≥ 32 ký tự). Trống = tool API tắt, AI dùng cách 1. **Tự thêm vào `.env.monitoring`** (cùng giá trị cho cả hai service, compose đã truyền) |
| `AI_USE_TOOLS` | `true` (mặc định) dùng cách 2 khi có token; `false` ép dùng cách 1 |
| `AI_CHAT_MAX_STEPS`, `AI_KNOWLEDGE_MAX_CHARS` | Số bước tối đa của mỗi tin chat (6); độ dài tối đa bản đồ hệ thống chèn vào prompt (4500) |
| `AI_CHAT_MAX_PER_HOUR`, `AI_CHAT_HISTORY_TURNS`, `AI_CHAT_SESSION_TTL_SECONDS` | Lượt chat tối đa mỗi giờ, **riêng** với phân tích sự cố (30); số lượt hỏi-đáp AI nhớ trong phiên `/ai` (8); thời gian giữ trí nhớ (3600) — đặt ở service `holmes-bridge` |
| `AI_CHAT_DIRECT`, `AI_CHAT_DIRECT_MAX_TOKENS`, `AI_CHAT_STREAM_EDIT_SECONDS` | Bật chat trực tiếp (true), số token tối đa mỗi câu trả lời (700), khoảng sửa tin khi hiện chữ dần (1 giây) — đặt ở service `holmes-bridge` |
| `AI_SESSION_IDLE_SECONDS` | Phiên `/ai` tự đóng sau bấy nhiêu giây không nhắn (900) — đặt ở service `remediator` |
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
| `docker.sock` = quyền root | Chỉ `docker-proxy` (allowlist 7 lời gọi, mục 2.4) mount socket cho remediator; Alloy đọc qua `alloy-docker-proxy` (chỉ GET), không mount socket nữa; mạng nội bộ không ra internet; AI/Holmes không chạm Docker |
| Container khác trong mạng giả cảnh báo / đề xuất / huỷ đề xuất | Khi có `REMEDIATOR_TOOL_TOKEN`: bridge `/alert`, `/continue`, `/chat` và remediator `/propose`, `/close`, `/tool/*` đều bắt `Authorization: Bearer`; Alertmanager gửi token kèm webhook (đọc từ file trong container). Cờ `manual`/`tool` gửi qua HTTP bị bỏ (không giả được "LỆNH BẠN VỪA GÕ") |
| AI bị gài lệnh qua log (prompt injection) rồi gửi dữ liệu ra ngoài | Tắt toolset `internet` (tải URL bất kỳ), `connectivity_check` (dò mạng nội bộ), `bash` của Holmes trong `monitoring/holmes/config.yaml` |
| Ai trong nhóm Telegram cũng duyệt được | Chat nhóm (id âm) mà chưa có `REMEDIATOR_APPROVER_IDS` thì remediator **chặn mọi lệnh và nút duyệt** và báo lên Telegram; chỉ mở khi cố ý đặt `REMEDIATOR_ALLOW_ANY_GROUP_MEMBER=true` |
| Đổi nút "Từ chối" thành "Duyệt" | Chữ ký HMAC tính trên cả loại nút lẫn mã đề xuất |
| Lệnh cũ chạy bất ngờ sau khi khởi động lại | Lúc khởi động remediator bỏ qua mọi tin/nút gửi trong lúc nó tắt |
| Log chứa mật khẩu, token, email đi ra Telegram / Bedrock / audit | `redact()` che mật khẩu, token, khoá AWS/Bedrock, token Telegram, JWT, cookie, email trước khi gửi |
| Kết nối treo giữ luồng | Server HTTP của bridge, remediator, docker-proxy đặt timeout 20 giây |
| Duyệt giả / bấm nhầm người | Nút ký HMAC, hết hạn, dùng 1 lần, kiểm tra chat id và user id |
| Restart lặp vô hạn | Cooldown + giới hạn theo giờ cho mỗi (action, container), kiểm tra lại cả lúc duyệt |
| Sửa nhầm DB / tự tắt giám sát | Danh sách `containers_forbidden` |
| Không biết ai đã làm gì | Audit đầy đủ cả đề xuất bị chặn / từ chối / hết hạn |
| Tốn tiền Bedrock | Vòng lặp tối đa `AI_MAX_ROUNDS` lượt/sự cố, mỗi lượt tính vào `AI_MAX_PER_HOUR`; lệnh thay thế không gọi AI; ⏹ Dừng, RESOLVED, dry-run đều chặn vòng tiếp |
| AI lặp lại lệnh vô ích | Prompt vòng sau ghi rõ lịch sử + "chỉ đề xuất lệnh KHÁC"; cooldown/giới hạn giờ trong policy vẫn chặn, và remediator báo lý do lên Telegram |
| Người lạ dừng/điều khiển vòng lặp | `/stop` và nút ⏹ kiểm tra chat id + user id như nút Duyệt; sai → audit `unauthorized_stop` |
| Gợi ý thay thế thành lỗ hổng | Gợi ý chỉ là *đề xuất*: phải qua `check_proposal`, cooldown, và người bấm Duyệt đúng lệnh đó (nút ký HMAC, dùng 1 lần) |

**Đã vá** (trước đây sandbox xác nhận `tecnativa/docker-socket-proxy` với `POST=1` cho **tạo container mới**, HTTP 201): nay dùng proxy allowlist (mục 2.4); sandbox B6 dò cả tạo, xoá, ghi file, stop, kill, exec, restart DB, container ngoài sandbox, `logs?follow` → đều 403.

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
| `docker-proxy` (proxy allowlist **giống hệt production**, build từ `ai-remediation/docker-proxy`, chỉ cho tên `ecommerce-sbx-*`, mạng `dockernet` nội bộ) | **Thật**, nói chuyện với Docker local; nhờ tiền tố, sandbox không thể đụng container thật |
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

**Điều sandbox đã xác nhận cho production:** proxy allowlist cho restart/start/logs/inspect thật (restart trả 204) và chặn mọi thứ khác; đường audit/Telegram/dry-run/execute chạy đúng từ đầu tới cuối.

**Điều sandbox từng phát hiện (đã vá):** `tecnativa/docker-socket-proxy` với `CONTAINERS=1` + `POST=1` cho tạo container mới (`POST /containers/create` → 201), tức nếu mã remediator bị chiếm thì tạo + start được container tuỳ ý (mount `/` của máy). Đã thay bằng proxy allowlist (mục 2.4).

**Lỗi của chính sandbox tìm ra trong lúc làm** (đã sửa): mock Telegram từng đặt lại bộ đếm `update_id` về 1 khi reset nên remediator bỏ qua nút bấm mới; Telegram thật không bao giờ làm vậy.

Giới hạn của sandbox: không kiểm tra Telegram/Bedrock thật (**chất lượng đề xuất của AI thật qua nhiều vòng chỉ biết khi chạy dry-run trên môi trường thật**), không kiểm tra `start_container` (chỉ `restart` và `logs`), `alloy`/`grafana` không nằm trong sandbox nên **panel audit và parse Alloy vẫn chưa được thử**.

## 9. Chưa làm / lưu ý

- **Chưa chạy thử thật** trên EC2/Telegram/Bedrock (bị cấm trong `CLAUDE.md` vì có thể tốn tiền hoặc đụng AWS). Đã thử cục bộ bằng sandbox (mục 8). Việc build image, push, triển khai và gọi `holmes ask` thật do người vận hành tự làm.
- **HTTPS chưa có** (cần làm ở hạ tầng, ngoài repo): trên EC2 đặt ALB + chứng chỉ ACM (hoặc certbot), rồi đặt `FORCE_HTTPS=true` trong `.env.app`. Code đã sẵn: nginx lấy giao thức thật từ `X-Forwarded-Proto` của ALB, gửi HSTS khi đang HTTPS, chuyển HTTP → HTTPS (trừ `/health` và ACME), cookie đăng nhập tự có cờ `Secure`. Chưa có HTTPS thì cookie admin và mật khẩu Basic Auth đi dạng chữ trơn trên Internet.
- Alloy vẫn chạy `user: root` (để đọc được volume `alloy-data` đã tạo trước đây) nhưng **không còn docker.sock**: chỉ thấy Docker qua `alloy-docker-proxy` (chỉ GET).
- `start_container` qua proxy allowlist mới được thử bằng unit test; `restart_container`, `logs_tail`, `inspect_container` đã chạy thật trong sandbox.
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
