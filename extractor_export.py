import re
import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

import config

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM_PROMPT = """
Bạn là trợ lý AI chuyên gia bóc tách dữ liệu kế hoạch đóng hàng xuất (Logistics / Container Shipping Vietsun TMS).
Nhiệm vụ của bạn là đọc nội dung email (text hoặc ảnh chụp màn hình email/bảng biểu) và trích xuất danh sách các booking kế hoạch đóng hàng thành mảng JSON.

Mỗi phần tử trong mảng JSON phải có cấu trúc chính xác sau:
{
  "booking": "Mã booking (VD: 300167)",
  "phong_ban": "Phòng ban / PIC (VD: VSV)",
  "chuyen": "Số chuyến tàu (VD: 2636N, 1235N)",
  "tau": "Tên tàu (VD: VIETSUN RELIANCE, VIETSUN DYNAMIC)",
  "lines": "Hãng tàu (VD: VIETSUN)",
  "ngay_dong": "Ngày đóng hàng định dạng DD/MM/YYYY (VD: 15/10/2026)",
  "ngay_tau_chay": "Ngày tàu chạy định dạng DD/MM/YYYY (VD: 19/10/2026)",
  "khach_hang": "Tên khách hàng FCL (VD: Gạo Chú Hải)",
  "kho_dong": "Kho đóng hàng / Địa chỉ chi tiết (VD: Xã Hội Cư, Đồng Tháp hoặc Kho Vĩnh Khang - Cai Lậy)",
  "lien_he": "Thông tin liên hệ kho đóng (VD: Mrs. Loan 0982926575)",
  "tinh_dong": "Tỉnh đóng hàng (VD: ĐỒNG THÁP, TÂY NINH)",
  "ghi_chu": "Ghi chú đóng hàng (VD: Sáng, Chiều, Tận dụng vỏ...)",
  "sl_20": 1,
  "sl_40": 0,
  "gw": "Trọng lượng GW thực tế (VD: 28.12)",
  "ten_hang": "Tên hàng hóa (VD: Gạo, Bánh kẹo...)",
  "pol": "Cảng đi (VD: HCM)",
  "pod": "Cảng đến POD - CỘT 14 (VD: CLO, HPH, DNG)",
  "port_ha_hang": "Port hạ hàng (VD: BẾN NGHÉ, CÁT LÁI)",
  "so_cont": "Số container nếu có trong email hoặc kèm theo (VD: DRYU3022599, GAOU2111920), nếu không có thì để rỗng ''",
  "so_seal": "Số seal nếu có, không có thì để rỗng ''"
}

QUY TẮC CẦN TUÂN THỦ:
1. Chuyển đổi định dạng ngày: Nếu ngày trong email dạng MM/DD/YYYY (VD: 10/15/2026) hoặc DD-Mon-YY, hãy chuyển thành chuẩn DD/MM/YYYY (VD: 15/10/2026).
2. POD là trường cực kỳ quan trọng (sẽ gán vào Cột thứ 14 trong TMS). Hãy lấy đúng mã POD (như CLO, HPH, DNG, HPH-V...).
3. Nếu 1 booking có nhiều cont (ví dụ SL 20 = 2), tạo ra 2 phần tử tương ứng với 2 dòng.
4. Chỉ trả về duy nhất khối JSON hợp lệ bắt đầu bằng [ và kết thúc bằng ], không giải thích gì thêm.
"""

def extract_with_gemini(text_content: Optional[str] = None, image_bytes: Optional[bytes] = None) -> List[Dict[str, Any]]:
    """Sử dụng Google Gemini API để bóc tách kế hoạch đóng hàng"""
    if not config.GEMINI_API_KEY:
        logger.warning("Chưa cấu hình GEMINI_API_KEY, chuyển sang phân tích Regex")
        if text_content:
            return fallback_regex_extractor(text_content)
        return []

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=config.GEMINI_API_KEY)
        contents = []

        if image_bytes:
            contents.append(types.Part.from_bytes(data=image_bytes, mime_type="image/png"))

        prompt_text = text_content or ""
        prompt_text += "\nHãy trích xuất toàn bộ các dòng booking kế hoạch đóng hàng trong nội dung trên thành mảng JSON."
        contents.append(prompt_text)

        # Thử các model Flash nhanh nhất
        candidate_models = [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
            "gemini-1.5-flash",
        ]

        response = None
        for m_name in candidate_models:
            try:
                response = client.models.generate_content(
                    model=m_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=EXTRACTION_SYSTEM_PROMPT,
                        temperature=0.1,
                    )
                )
                if response and response.text:
                    break
            except Exception as e_sub:
                logger.debug(f"Model {m_name} không khả dụng: {e_sub}")
                continue

        if not response or not response.text:
            logger.error("Không nhận được phản hồi từ Gemini API")
            if text_content:
                return fallback_regex_extractor(text_content)
            return []

        raw_out = response.text.strip()
        # Làm sạch markdown ```json ... ```
        if "```" in raw_out:
            m_block = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw_out, re.DOTALL)
            if m_block:
                raw_out = m_block.group(1).strip()
            else:
                raw_out = re.sub(r"^```(?:json)?", "", raw_out)
                raw_out = re.sub(r"```$", "", raw_out).strip()

        data = json.loads(raw_out)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return [data]
        return []

    except Exception as e:
        logger.exception(f"Lỗi khi trích xuất bằng Gemini: {e}")
        if text_content:
            return fallback_regex_extractor(text_content)
        return []


def fallback_regex_extractor(text: str) -> List[Dict[str, Any]]:
    """Phân tích fallback dựa trên Regex khi không có internet/Gemini API"""
    results = []
    lines = text.splitlines()

    # Nhận diện dòng chứa Booking ID (thường là số 6 chữ số: 300167, 300170...)
    for line in lines:
        m_booking = re.search(r"\b(300\d{3}|299\d{3})\b", line)
        if not m_booking:
            continue

        booking_id = m_booking.group(1)
        # Tách theo tab hoặc ký tự gạch đứng |
        parts = [p.strip() for p in re.split(r"[\t|]+", line) if p.strip()]

        # Tìm tàu
        m_tau = re.search(r"(VIETSUN\s+[A-Z]+)", line, re.IGNORECASE)
        tau = m_tau.group(1).upper() if m_tau else "VIETSUN"

        # Tìm chuyến (dạng 2636N, 1235N...)
        m_chuyen = re.search(r"\b(\d{3,4}[NSWE])\b", line)
        chuyen = m_chuyen.group(1).upper() if m_chuyen else ""

        # Tìm ngày
        dates = re.findall(r"(\d{1,2}[-/]\d{1,2}[-/]\d{4})", line)
        ngay_dong = ""
        ngay_chay = ""
        if len(dates) >= 1:
            ngay_dong = convert_date(dates[0])
        if len(dates) >= 2:
            ngay_chay = convert_date(dates[1])

        # Tìm POD (CLO, HPH, DNG...)
        m_pod = re.search(r"\b(CLO|HPH|DNG|QNH|HCM)\b", line)
        pod = m_pod.group(1).upper() if m_pod else ""

        # Tìm số cont nếu có
        m_cont = re.search(r"\b([A-Z]{4}\s*\d{7})\b", line)
        so_cont = re.sub(r"\s+", "", m_cont.group(1)).upper() if m_cont else ""

        results.append({
            "booking": booking_id,
            "phong_ban": "VSV",
            "chuyen": chuyen,
            "tau": tau,
            "lines": "VIETSUN",
            "ngay_dong": ngay_dong,
            "ngay_tau_chay": ngay_chay,
            "khach_hang": parts[7] if len(parts) > 7 else "",
            "kho_dong": parts[9] if len(parts) > 9 else (parts[8] if len(parts) > 8 else ""),
            "lien_he": parts[10] if len(parts) > 10 else "",
            "tinh_dong": parts[11] if len(parts) > 11 else "",
            "ghi_chu": "Sáng",
            "sl_20": 1,
            "sl_40": 0,
            "gw": "",
            "ten_hang": "Gạo",
            "pol": "HCM",
            "pod": pod,
            "port_ha_hang": "BẾN NGHÉ",
            "so_cont": so_cont,
            "so_seal": "",
        })

    return results

def convert_date(d_str: str) -> str:
    """Chuyển đổi các định dạng ngày về chuẩn DD/MM/YYYY"""
    d_str = str(d_str).strip()
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", d_str)
    if not m:
        return d_str
    g1, g2, g3 = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # Nếu g1 > 12 -> chắc chắn là ngày: g1/g2/g3
    # Nếu g2 > 12 -> g2 là ngày: g2/g1/g3
    # Mặc định trong email Vietsun nếu MM/DD/YYYY: tháng/ngày/năm
    if g1 <= 12 and g2 > 12:
        return f"{str(g2).zfill(2)}/{str(g1).zfill(2)}/{g3}"
    return f"{str(g1).zfill(2)}/{str(g2).zfill(2)}/{g3}"
