"""
Mô hình giả cho CHAT TRỰC TIẾP trong sandbox (không Bedrock, không tốn tiền). holmes-bridge nạp module này khi đặt
AI_CHAT_LLM_MODULE=mock_llm, thay cho litellm. complete(messages) trả về từng đoạn chữ (giả lập streaming).

- Câu hỏi về "bất thường / hiện tại / bây giờ" -> trả [[OPS]] (giả lập mô hình tự thấy cần dữ liệu thật => bridge chuyển sang Holmes).
- Còn lại: trả lời mẫu, kèm báo lại prompt có luật [[OPS]], mô tả khả năng của bot và lịch sử hội thoại không.
"""
import re
import time


def complete(messages):
    last = messages[-1]["content"]
    system = messages[0]["content"]
    if re.search(r"bất thường|hiện tại|bây giờ", last):
        yield "[[OPS]]"
        return
    rules = "CÓ" if "[[OPS]]" in system and "KHÔNG có số liệu trực tiếp" in system else "KHÔNG"
    cap = "CÓ" if "/status" in system and "Chuỗi phụ thuộc" in system else "KHÔNG"
    hist = "CÓ" if len(messages) > 2 else "KHÔNG"
    for part in ("(mock-chat) Tôi là trợ lý vận hành. ", f"Luật OPS: {rules}. Khả năng bot và bản đồ: {cap}. ", f"Lịch sử: {hist}."):
        time.sleep(0.3)
        yield part
