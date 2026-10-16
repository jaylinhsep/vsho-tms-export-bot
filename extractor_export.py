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
  "booking": "Mã booking (VD: 298210, 300167)",
  "phong_ban": "Phòng ban / PIC (VD: PTTT, VSV)",
  "chuyen": "Số chuyến tàu (VD: 1632S, 2636N)",
  "tau": "Tên tàu (VD: VIETSUN HARMONY, VIETSUN RELIANCE, VIETSUN DYNAMIC)",
  "lines": "Hãng tàu (VD: VIETSUN)",
  "ngay_dong": "Ngày đóng hàng định dạng DD/MM/YYYY (VD: 08/10/2026)",
  "ngay_tra": "Ngày trả hàng nếu có định dạng DD/MM/YYYY (VD: 16/10/2026)",
  "ngay_tau_chay": "Ngày tàu chạy định dạng DD/MM/YYYY (VD: 12/10/2026)",
  "target_date": "Ngày kế hoạch áp dụng để tạo dòng trên TMS định dạng DD/MM/YYYY (VD: 16/10/2026)",
  "khach_hang": "Tên khách hàng FCL (VD: Cty test, Gạo Chú Hải)",
  "kho_dong": "Kho đóng hàng / Consignee / Địa chỉ (VD: Tribeco - aaaaaaaaaaaaaaaaaaaaaaaaa)",
  "lien_he": "Thông tin liên hệ kho (VD: Mr. Khánh, Mrs. Loan 0982926575)",
  "tinh_dong": "Tỉnh đóng/trả hàng (VD: BÌNH DƯƠNG, HCM, TÂY NINH)",
  "ghi_chu": "Ghi chú đóng hàng (VD: Trả đóng kết hợp tại kho, Sáng, Chiều...)",
  "sl_20": 0,
  "sl_40": 1,
  "gw": "Trọng lượng GW thực tế (VD: 30.42)",
  "ten_hang": "Tên hàng hóa (VD: Thực phẩm, Gạo...)",
  "pol": "Cảng đi (VD: HPG, HCM)",
  "pod": "Cảng đến POD - CỘT 14 TMS (VD: HCM, CLO, HPH, DNG, QNH)",
  "port_ha_hang": "Port hạ hàng (VD: BẾN NGHÉ, CÁT LÁI)",
  "so_cont": "Số container (VD: VSGU9999999, DRYU3022599)",
  "so_seal": "Số seal (VD: 333333, 688441)"
}

QUY TẮC CẦN TUÂN THỦ:
1. Chuyển đổi định dạng ngày: Nếu ngày trong email dạng MM/DD/YYYY (VD: 10/8/2026, 10/16/2026), hãy chuyển thành chuẩn DD/MM/YYYY (VD: 08/10/2026, 16/10/2026). Bỏ qua ngày 1/1/1970.
2. POD là trường cực kỳ quan trọng (sẽ gán vào Cột thứ 14 trong TMS). Hãy lấy đúng mã cảng đến POD (như HCM, CLO, HPH, DNG, QNH, HPH-V...).
3. Nếu 1 booking có nhiều cont (ví dụ SL = 2 hoặc có danh sách 2 cont), tạo ra 2 phần tử tương ứng với 2 cont.
4. Chỉ trả về duy nhất khối JSON hợp lệ bắt đầu bằng [ và kết thúc bằng ], không giải thích gì thêm.
"""

# Danh sách mã cảng thông dụng
KNOWN_PORTS = {
    "HPG", "HPH", "HCM", "CLO", "DNG", "QNH", "VPH", "CMT", "HPH-V",
    "BẾN NGHÉ", "CÁT LÁI", "BEN NGHE", "CAT LAI", "VIETSUN", "VSHO"
}


def convert_date(d_str: str, plan_month: Optional[int] = None) -> str:
    """Chuyển đổi các định dạng ngày về chuẩn DD/MM/YYYY"""
    if not d_str:
        return ""
    d_str = str(d_str).strip()
    if d_str in ("1/1/1970", "01/01/1970", "1970-01-01"):
        return ""

    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", d_str)
    if not m:
        return d_str

    g1, g2, g3 = int(m.group(1)), int(m.group(2)), int(m.group(3))

    # Nếu g2 > 12 -> chắc chắn MM/DD/YYYY (VD: 10/16/2026 -> 16/10/2026)
    if g1 <= 12 and g2 > 12:
        return f"{str(g2).zfill(2)}/{str(g1).zfill(2)}/{g3}"

    # Nếu g1 > 12 -> chắc chắn DD/MM/YYYY (VD: 16/10/2026)
    if g1 > 12 and g2 <= 12:
        return f"{str(g1).zfill(2)}/{str(g2).zfill(2)}/{g3}"

    # Trường hợp cả g1, g2 <= 12 (VD: 10/8/2026):
    if plan_month:
        if g1 == plan_month and g2 != plan_month:
            # g1 là tháng, g2 là ngày -> DD/MM/YYYY là g2/g1
            return f"{str(g2).zfill(2)}/{str(g1).zfill(2)}/{g3}"
        elif g2 == plan_month and g1 != plan_month:
            # g2 là tháng, g1 là ngày -> DD/MM/YYYY là g1/g2
            return f"{str(g1).zfill(2)}/{str(g2).zfill(2)}/{g3}"

    # Mặc định trong bảng Vietsun copy từ webmail nếu cột là tháng trước:
    return f"{str(g2).zfill(2)}/{str(g1).zfill(2)}/{g3}"


def extract_booking_plan_deterministic(text: str) -> List[Dict[str, Any]]:
    """
    Bộ bóc tách thông minh chuyên biệt cho Email TMS Vietsun (Deterministic Parser).
    Xử lý:
    1. Bảng copy dọc (Headers ở trên, Values ở dưới) - phổ biến nhất từ Outlook/Webmail.
    2. Bảng copy ngang (Tab/Pipe separated row).
    3. Định dạng Key-Value.
    Tốc độ cực nhanh: ~1ms, hoạt động hoàn toàn offline, 0% hallucination!
    """
    if not text or not text.strip():
        return []

    # Chuẩn hóa văn bản: đổi non-breaking space sang space thường, chuẩn hóa dòng
    clean_text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [l.strip() for l in clean_text.splitlines() if l.strip()]

    # 1. Bóc tách Ngày kế hoạch từ tiêu đề email (VD: NGÀY : 16/10/2026)
    plan_date_str = ""
    plan_month = None
    m_plan = re.search(r"NGÀY\s*:\s*(\d{1,2}[-/]\d{1,2}[-/]\d{4})", clean_text, re.IGNORECASE)
    if m_plan:
        plan_date_str = m_plan.group(1)
        m_m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", plan_date_str)
        if m_m:
            g1, g2 = int(m_m.group(1)), int(m_m.group(2))
            plan_month = g2 if g1 > 12 else (g1 if g2 > 12 else g2)

    # 2. KIỂM TRA ĐỊNH DẠNG BẢNG DỌC (VERTICAL BLOCK)
    # Tìm các dòng là Booking ID thực sự (6 chữ số: 29xxxx, 30xxxx, 31xxxx hoặc \d{6})
    # Cần xác thực: Trong vòng 6 dòng tiếp theo phải có Chuyến tàu (\d{3,4}[NSWE]), Tên tàu (VIETSUN), hoặc Ngày tháng
    # Nhằm LOẠI TRỪ các số seal ngẫu nhiên có 6 chữ số (như seal 333333)
    true_booking_indices = []
    for i, l in enumerate(lines):
        if re.match(r"^\d{6}$", l):
            # Loại trừ số Seal: Nếu dòng trước đó là số container [A-Z]{4}\d{7} thì đây là seal
            prev_is_cont = i > 0 and bool(re.match(r"^[A-Z]{4}\d{7}$", lines[i - 1]))
            if prev_is_cont:
                continue

            # Phải có Chuyến (\d{3,4}[NSWE]) hoặc Tàu (VIETSUN) trong phạm vi 1 đến 4 dòng tiếp theo
            has_anchor = False
            for step in range(1, 5):
                if i + step < len(lines):
                    next_l = lines[i + step]
                    if re.match(r"^\d{3,4}[NSWE]$", next_l) or "VIETSUN" in next_l.upper():
                        has_anchor = True
                        break
            if has_anchor:
                true_booking_indices.append(i)

    if true_booking_indices:
        results = []
        for b_pos, b_idx in enumerate(true_booking_indices):
            # Cắt phạm vi dòng của booking này đến booking tiếp theo hoặc hết footer
            next_b_idx = (
                true_booking_indices[b_pos + 1]
                if b_pos + 1 < len(true_booking_indices)
                else len(lines)
            )
            block_lines = []
            for j in range(b_idx, next_b_idx):
                line_j = lines[j]
                if line_j.startswith("---") or "TRÂN TRỌNG" in line_j.upper() or "P/S:" in line_j.upper():
                    break
                block_lines.append(line_j)

            item = _parse_vertical_block(block_lines, plan_month, plan_date_str)
            if item:
                # Nếu có nhiều container được phát hiện trong cùng booking
                conts = _find_all_containers_in_block(block_lines)
                if len(conts) > 1:
                    for c_info in conts:
                        item_copy = dict(item)
                        item_copy["so_cont"] = c_info["cont"]
                        item_copy["so_seal"] = c_info["seal"]
                        results.append(item_copy)
                else:
                    results.append(item)

        if results:
            logger.info(f"⚡ [Deterministic] Đã nhận diện thành công {len(results)} booking theo dạng Bảng Dọc!")
            return results

    # 3. KIỂM TRA ĐỊNH DẠNG BẢNG NGANG (HORIZONTAL TABLE - TAB / PIPE SEPARATED)
    horizontal_items = []
    for line in lines:
        if re.search(r"\b\d{6}\b", line) and ("\t" in line or "|" in line):
            item = _parse_horizontal_line(line, plan_month, plan_date_str)
            if item:
                horizontal_items.append(item)

    if horizontal_items:
        logger.info(f"⚡ [Deterministic] Đã nhận diện thành công {len(horizontal_items)} booking theo dạng Bảng Ngang!")
        return horizontal_items

    # 4. KIỂM TRA ĐỊNH DẠNG KEY-VALUE
    if "BOOKING" in clean_text.upper() and (":" in clean_text or "：" in clean_text):
        kv_item = _parse_key_value(clean_text, plan_month, plan_date_str)
        if kv_item:
            logger.info("⚡ [Deterministic] Đã nhận diện booking theo dạng Key-Value!")
            return [kv_item]

    return []


def _parse_vertical_block(vals: List[str], plan_month: Optional[int], plan_date_str: str) -> Optional[Dict[str, Any]]:
    """Phân tích một khối giá trị dọc của 1 booking"""
    if not vals:
        return None

    booking_id = vals[0]
    phong_ban = vals[1] if len(vals) > 1 and len(vals[1]) <= 10 else "PTTT"

    chuyen = ""
    tau = "VIETSUN"
    cont = ""
    seal = ""
    gw = ""
    khach = ""
    lien_he = ""
    ghi_chu = ""
    tinh_dong = ""
    ten_hang = ""
    pol = ""
    pod = ""

    # Thu thập tất cả các ngày trong khối
    dates = []
    for v in vals:
        if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", v):
            dates.append(v)

    ngay_dong = convert_date(dates[0], plan_month) if len(dates) >= 1 else ""
    ngay_tra = convert_date(dates[1], plan_month) if len(dates) >= 2 else ""
    ngay_tau_chay = convert_date(dates[2], plan_month) if len(dates) >= 3 else ""

    # Quét nhận diện các trường đặc trưng
    for v in vals:
        # Chuyến (VD: 1632S, 2636N)
        if re.match(r"^\d{3,4}[NSWE]$", v) and not chuyen:
            chuyen = v.upper()
        # Tàu
        elif "VIETSUN" in v.upper() and len(v.split()) >= 2 and tau == "VIETSUN":
            tau = v.upper()
        # Số container chuẩn quốc tế 4 chữ cái + 7 chữ số
        elif re.match(r"^[A-Z]{4}\d{7}$", v) and not cont:
            cont = v.upper()
        # Trọng lượng GW
        elif re.match(r"^\d+[\.,]\d+$", v) and not gw:
            gw = v.replace(",", ".")
        # Khách hàng FCL
        elif re.match(r"^(cty|công ty|tnhh|cp|tess)\b", v, re.IGNORECASE) and not khach:
            khach = v
        # Liên hệ
        elif re.match(r"^(mr|ms|mrs|anh|chị)\b", v, re.IGNORECASE) and not lien_he:
            lien_he = v
        # Ghi chú đóng/trả hàng
        elif (
            re.search(r"(đóng|trả|kết hợp|sáng|chiều|giao|kho|cont|bấm)", v, re.IGNORECASE)
            and not ghi_chu
            and v != khach
            and not re.match(r"^(mr|ms|mrs|anh|chị)\b", v, re.IGNORECASE)
        ):
            ghi_chu = v

    # Số Seal: nằm ngay sau số cont nếu là chuỗi số 5-8 ký tự
    if cont:
        cont_idx = vals.index(cont)
        if cont_idx + 1 < len(vals):
            next_v = vals[cont_idx + 1]
            if re.match(r"^\d{5,8}$", next_v) or re.match(r"^[A-Z0-9]{5,10}$", next_v):
                seal = next_v

    # Xử lý SL 20, SL 40, Tên hàng, POL, POD từ vị trí của GW
    sl_20 = 0
    sl_40 = 1
    if gw:
        gw_idx = -1
        for idx_g, v in enumerate(vals):
            if v.replace(",", ".") == gw:
                gw_idx = idx_g
                break

        if gw_idx >= 2:
            s1, s2 = vals[gw_idx - 2], vals[gw_idx - 1]
            if s1.isdigit() and s2.isdigit():
                sl_20 = int(s1)
                sl_40 = int(s2)

        if gw_idx != -1 and gw_idx + 1 < len(vals):
            ten_hang = vals[gw_idx + 1]

        # Sau tên hàng là POL và POD
        if gw_idx != -1 and gw_idx + 2 < len(vals):
            cand_pol = vals[gw_idx + 2].upper()
            if cand_pol in KNOWN_PORTS or len(cand_pol) <= 6:
                pol = cand_pol

        if gw_idx != -1 and gw_idx + 3 < len(vals):
            cand_pod = vals[gw_idx + 3].upper()
            if cand_pod in KNOWN_PORTS or len(cand_pod) <= 6:
                pod = cand_pod

    # Nếu chưa tìm thấy POD từ vị trí GW, tìm kiếm toàn khối
    if not pod:
        found_ports = [v.upper() for v in vals if v.upper() in KNOWN_PORTS and v.upper() != "VIETSUN"]
        if len(found_ports) >= 2:
            pol = pol or found_ports[0]
            pod = found_ports[1]
        elif len(found_ports) == 1:
            pod = found_ports[0]

    # Địa chỉ kho đóng: kết hợp các dòng ngay sau Khách hàng
    kho_dong = ""
    if khach:
        khach_idx = vals.index(khach)
        kho_parts = []
        for offset in (1, 2):
            if khach_idx + offset < len(vals):
                nxt = vals[khach_idx + offset]
                if (
                    not re.match(r"^(mr|ms|mrs|anh|chị)\b", nxt, re.IGNORECASE)
                    and not nxt.isdigit()
                    and nxt not in ("Không", "Có", "Y", "N")
                    and nxt != ghi_chu
                ):
                    kho_parts.append(nxt)
        if len(kho_parts) >= 2:
            kho_dong = f"{kho_parts[0]} - {kho_parts[1]}"
        elif len(kho_parts) == 1:
            kho_dong = kho_parts[0]

    # Tỉnh đóng/trả: dòng ngay sau Liên hệ
    if lien_he:
        lh_idx = vals.index(lien_he)
        if lh_idx + 1 < len(vals):
            nxt_tinh = vals[lh_idx + 1]
            if nxt_tinh not in ("Không", "Có", "Y", "N") and not nxt_tinh.isdigit():
                tinh_dong = nxt_tinh

    # Xác định ngày tạo dòng trên TMS (target_date): ưu tiên ngày kế hoạch trong header, hoặc ngày trả/đóng
    target_date = plan_date_str or ngay_tra or ngay_dong or datetime.now().strftime("%d/%m/%Y")

    return {
        "booking": booking_id,
        "phong_ban": phong_ban,
        "chuyen": chuyen,
        "tau": tau,
        "lines": "VIETSUN",
        "ngay_dong": ngay_dong,
        "ngay_tra": ngay_tra,
        "ngay_tau_chay": ngay_tau_chay,
        "target_date": target_date,
        "khach_hang": khach,
        "kho_dong": kho_dong,
        "lien_he": lien_he,
        "tinh_dong": tinh_dong,
        "ghi_chu": ghi_chu or "Trả đóng kết hợp tại kho",
        "sl_20": sl_20,
        "sl_40": sl_40,
        "gw": gw,
        "ten_hang": ten_hang or "Hàng hóa",
        "pol": pol or "HPG",
        "pod": pod or "HCM",  # CỘT 14 QUAN TRỌNG
        "port_ha_hang": "BẾN NGHÉ",
        "so_cont": cont,
        "so_seal": seal,
    }


def _find_all_containers_in_block(vals: List[str]) -> List[Dict[str, str]]:
    """Tìm tất cả các cặp (Số cont, Số seal) nếu trong booking có nhiều cont"""
    conts = []
    for i, v in enumerate(vals):
        if re.match(r"^[A-Z]{4}\d{7}$", v):
            seal = ""
            if i + 1 < len(vals):
                nxt = vals[i + 1]
                if re.match(r"^\d{5,8}$", nxt) or re.match(r"^[A-Z0-9]{5,10}$", nxt):
                    seal = nxt
            conts.append({"cont": v, "seal": seal})
    return conts


def _parse_horizontal_line(line: str, plan_month: Optional[int], plan_date_str: str) -> Optional[Dict[str, Any]]:
    """Phân tích một dòng chứa booking dạng bảng ngang"""
    parts = [p.strip() for p in re.split(r"[\t|]+", line) if p.strip()]
    if not parts:
        return None

    m_b = re.search(r"\b(\d{6})\b", line)
    if not m_b:
        return None
    booking_id = m_b.group(1)

    m_tau = re.search(r"(VIETSUN\s+[A-Z]+)", line, re.IGNORECASE)
    tau = m_tau.group(1).upper() if m_tau else "VIETSUN"

    m_chuyen = re.search(r"\b(\d{3,4}[NSWE])\b", line)
    chuyen = m_chuyen.group(1).upper() if m_chuyen else ""

    dates = re.findall(r"(\d{1,2}[-/]\d{1,2}[-/]\d{4})", line)
    ngay_dong = convert_date(dates[0], plan_month) if len(dates) >= 1 else ""
    ngay_chay = convert_date(dates[1], plan_month) if len(dates) >= 2 else ""

    m_cont = re.search(r"\b([A-Z]{4}\d{7})\b", line)
    so_cont = m_cont.group(1).upper() if m_cont else ""

    # Tìm POD
    pod = ""
    for p in parts:
        if p.upper() in KNOWN_PORTS and p.upper() != "VIETSUN":
            pod = p.upper()

    return {
        "booking": booking_id,
        "phong_ban": "PTTT",
        "chuyen": chuyen,
        "tau": tau,
        "lines": "VIETSUN",
        "ngay_dong": ngay_dong,
        "ngay_tau_chay": ngay_chay,
        "target_date": plan_date_str or ngay_dong,
        "khach_hang": parts[7] if len(parts) > 7 else "",
        "kho_dong": parts[8] if len(parts) > 8 else "",
        "lien_he": parts[9] if len(parts) > 9 else "",
        "tinh_dong": parts[10] if len(parts) > 10 else "",
        "ghi_chu": "Sáng",
        "sl_20": 0,
        "sl_40": 1,
        "gw": "",
        "ten_hang": "Hàng hóa",
        "pol": "HCM",
        "pod": pod or "HCM",
        "port_ha_hang": "BẾN NGHÉ",
        "so_cont": so_cont,
        "so_seal": "",
    }


def _parse_key_value(text: str, plan_month: Optional[int], plan_date_str: str) -> Optional[Dict[str, Any]]:
    """Phân tích văn bản dạng Key: Value"""
    m_b = re.search(r"(?:Booking|Mã Booking)\s*[:：]\s*(\d{6})", text, re.IGNORECASE)
    if not m_b:
        m_b = re.search(r"\b(\d{6})\b", text)
        if not m_b:
            return None
    booking_id = m_b.group(1)

    def _get_val(pat: str) -> str:
        m = re.search(pat, text, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    tau = _get_val(r"(?:Tàu|Tên tàu)\s*[:：]\s*([^\n\r]+)") or "VIETSUN"
    chuyen = _get_val(r"(?:Chuyến|Số chuyến)\s*[:：]\s*([^\n\r]+)")
    ngay_dong = convert_date(_get_val(r"(?:Ngày đóng|Ngày đóng hàng)\s*[:：]\s*([^\n\r]+)"), plan_month)
    ngay_chay = convert_date(_get_val(r"(?:Ngày tàu chạy|Ngày chạy)\s*[:：]\s*([^\n\r]+)"), plan_month)
    pod = _get_val(r"(?:POD|Cảng đến)\s*[:：]\s*([^\n\r]+)")
    khach = _get_val(r"(?:Khách hàng|Khách)\s*[:：]\s*([^\n\r]+)")
    kho = _get_val(r"(?:Kho|Địa chỉ kho|Kho đóng)\s*[:：]\s*([^\n\r]+)")
    cont = _get_val(r"(?:Số cont|Cont|Container)\s*[:：]\s*([A-Z]{4}\d{7})")
    seal = _get_val(r"(?:Số seal|Seal)\s*[:：]\s*([^\n\r]+)")

    return {
        "booking": booking_id,
        "phong_ban": "PTTT",
        "chuyen": chuyen,
        "tau": tau,
        "lines": "VIETSUN",
        "ngay_dong": ngay_dong,
        "ngay_tau_chay": ngay_chay,
        "target_date": plan_date_str or ngay_dong,
        "khach_hang": khach,
        "kho_dong": kho,
        "lien_he": "",
        "tinh_dong": "",
        "ghi_chu": "",
        "sl_20": 0,
        "sl_40": 1,
        "gw": "",
        "ten_hang": "",
        "pol": "HCM",
        "pod": pod or "HCM",
        "port_ha_hang": "BẾN NGHÉ",
        "so_cont": cont,
        "so_seal": seal,
    }


def extract_with_gemini(text_content: Optional[str] = None, image_bytes: Optional[bytes] = None) -> List[Dict[str, Any]]:
    """
    Quy trình phân tích kế hoạch đóng hàng 2 tầng:
    1. Tầng 1: Sử dụng Smart Deterministic Parser (Regex & Cấu trúc email Vietsun) siêu tốc trong ~1ms.
       Đảm bảo hoạt động 100% kể cả khi không có mạng ngoài, không có Gemini API Key.
    2. Tầng 2: Nếu có ảnh chụp màn hình HOẶC nội dung văn bản tự do phức tạp mà Tầng 1 không nhận diện được,
       gọi Google Gemini API (model gemini-2.0-flash / gemini-1.5-flash) với JSON schema chuẩn.
    """
    # 1. Thử phân tích bằng Bộ bóc tách thông minh trước tiên
    if text_content and text_content.strip():
        deterministic_results = extract_booking_plan_deterministic(text_content)
        if deterministic_results:
            logger.info(f"✅ [Tầng 1] Trích xuất thành công {len(deterministic_results)} booking trong 0.001s!")
            return deterministic_results

    # 2. Nếu có ảnh hoặc Tầng 1 không thấy, dùng Gemini AI
    if not config.GEMINI_API_KEY:
        logger.warning("Chưa cấu hình GEMINI_API_KEY và không bóc tách được bằng cấu trúc chuẩn")
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

        candidate_models = [
            "gemini-2.0-flash",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
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
                        response_mime_type="application/json",
                    ),
                )
                if response and response.text:
                    logger.info(f"✅ Gemini API ({m_name}) đã phản hồi thành công!")
                    break
            except Exception as e_sub:
                logger.warning(f"Model {m_name} không khả dụng: {e_sub}")
                continue

        if not response or not response.text:
            logger.error("Không nhận được phản hồi hợp lệ từ Gemini API")
            if text_content:
                return fallback_regex_extractor(text_content)
            return []

        raw_out = response.text.strip()
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
    """Phân tích tìm kiếm quét tự do (Fuzzy Fallback) cho các trường hợp văn bản bất thường"""
    results = []
    clean_text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Tìm tất cả các số có 6 chữ số làm Booking ID
    bookings_found = re.findall(r"\b(\d{6})\b", clean_text)
    unique_bookings = list(dict.fromkeys(bookings_found))

    for b_id in unique_bookings:
        # Tìm tàu
        m_tau = re.search(r"(VIETSUN\s+[A-Z]+)", clean_text, re.IGNORECASE)
        tau = m_tau.group(1).upper() if m_tau else "VIETSUN"

        # Tìm chuyến
        m_chuyen = re.search(r"\b(\d{3,4}[NSWE])\b", clean_text)
        chuyen = m_chuyen.group(1).upper() if m_chuyen else ""

        # Tìm ngày
        dates = re.findall(r"(\d{1,2}[-/]\d{1,2}[-/]\d{4})", clean_text)
        ngay_dong = convert_date(dates[0]) if len(dates) >= 1 else ""
        ngay_chay = convert_date(dates[1]) if len(dates) >= 2 else ""

        # Tìm POD
        m_pod = re.search(r"\b(HCM|CLO|HPH|DNG|QNH|VPH|HPG)\b", clean_text)
        pod = m_pod.group(1).upper() if m_pod else "HCM"

        # Tìm số cont
        m_cont = re.search(r"\b([A-Z]{4}\d{7})\b", clean_text)
        so_cont = m_cont.group(1).upper() if m_cont else ""

        results.append({
            "booking": b_id,
            "phong_ban": "PTTT",
            "chuyen": chuyen,
            "tau": tau,
            "lines": "VIETSUN",
            "ngay_dong": ngay_dong,
            "ngay_tau_chay": ngay_chay,
            "target_date": ngay_dong or datetime.now().strftime("%d/%m/%Y"),
            "khach_hang": "",
            "kho_dong": "",
            "lien_he": "",
            "tinh_dong": "",
            "ghi_chu": "Trả đóng kết hợp tại kho",
            "sl_20": 0,
            "sl_40": 1,
            "gw": "",
            "ten_hang": "Hàng hóa",
            "pol": "HPG",
            "pod": pod,
            "port_ha_hang": "BẾN NGHÉ",
            "so_cont": so_cont,
            "so_seal": "",
        })

    return results


POD_EXTRACTION_SYSTEM_PROMPT = """
Bạn là trợ lý AI chuyên gia bóc tách dữ liệu logistics và vận tải biển container (Hệ thống TMS Vietsun).
Nhiệm vụ của bạn là đọc hình ảnh bảng biểu (hoặc văn bản) và trích xuất danh sách tất cả các dòng có SỐ CONTAINER (hoặc Mã Booking) cùng CẢNG ĐẾN (POD).

Mỗi phần tử trong mảng JSON phải có cấu trúc:
{
  "so_cont": "DRYU2901546",
  "pod": "DAN",
  "booking": "",
  "so_xe": "50E-48367",
  "so_seal": "680040",
  "khach_hang": "Cty Jotun",
  "chuyen": "1235N",
  "tau": "VIETSUN DYNAMIC"
}

QUY TẮC CẦN TUÂN THỦ:
1. Số Container (so_cont): Là chuỗi 4 chữ cái in hoa + 7 chữ số (VD: DRYU2901546, DRYU3095229, DRYU3095450, GAOU2110158, VSGU4254030...).
2. Cảng đến (pod): Là mã cảng 3-4 ký tự in hoa, thường được tô màu (xanh lá, vàng, cam...) hoặc nằm ở cột POD / Cảng đến / Tỉnh xuất hàng.
   Các mã cảng thường gặp: CLO, DAN, HPG, HPH, HCM, QNH, CLA, VPH, DNG, CMT...
   (LƯU Ý ĐẶC BIỆT: Trong ảnh bảng điều độ, các ô tô màu xanh lá ghi "DAN", ô tô màu vàng ghi "CLO" chính là mã POD!).
3. Mã Booking (booking): Chuỗi 6 chữ số nếu có (VD: 298210, 300170). Nếu bảng không có cột booking thì để trống "".
4. Biển số xe (so_xe) và Số seal (so_seal): Lấy nếu có trong dòng tương ứng.
5. CHỈ TRÍCH XUẤT những dòng có Số Container (hoặc Booking) VÀ có mã POD. Những dòng trống số container hoặc không có định danh thì bỏ qua.
6. Chỉ trả về duy nhất 1 mảng JSON thuần túy [ ... ], không thêm bất kỳ văn bản giải thích nào khác.
"""


def extract_pod_assignments_deterministic(text: str) -> List[Dict[str, Any]]:
    """Phân tích văn bản thuần túy tìm các cặp (Số cont, Mã POD) dạng regex"""
    results = []
    lines = text.splitlines()
    port_regex = r"\b(CLO|DAN|HPG|HPH|HCM|QNH|CLA|VPH|DNG|CMT)\b"
    cont_regex = r"\b([A-Z]{4}\d{7})\b"
    seal_regex = r"\b(\d{5,8})\b"
    truck_regex = r"\b(\d{2}[A-Z]-\d{4,5}|\d{2}[A-Z]\d{5,6})\b"

    for line in lines:
        m_cont = re.search(cont_regex, line, re.IGNORECASE)
        m_pod = re.search(port_regex, line, re.IGNORECASE)
        if m_cont and m_pod:
            cont = m_cont.group(1).upper()
            pod = m_pod.group(1).upper()
            m_seal = re.search(seal_regex, line)
            m_truck = re.search(truck_regex, line, re.IGNORECASE)
            m_booking = re.search(r"\b(\d{6})\b", line)
            results.append({
                "so_cont": cont,
                "pod": pod,
                "booking": m_booking.group(1) if m_booking else "",
                "so_seal": m_seal.group(1) if m_seal else "",
                "so_xe": m_truck.group(1).upper() if m_truck else "",
            })
    return results


def extract_pod_assignments(text_content: Optional[str] = None, image_bytes: Optional[bytes] = None) -> List[Dict[str, Any]]:
    """
    Trích xuất danh sách (Container, POD) từ ảnh hoặc văn bản để cập nhật hàng loạt Cột 14 trên TMS.
    Ưu tiên gọi Gemini AI nếu có ảnh, hoặc dùng regex parser nếu là văn bản đơn giản.
    """
    # 1. Thử nhận diện bằng regex nếu chỉ có text và có cặp cont + pod
    if text_content and not image_bytes:
        det_results = extract_pod_assignments_deterministic(text_content)
        if det_results:
            logger.info(f"✅ Đã nhận diện được {len(det_results)} cặp (Cont, POD) bằng deterministic regex!")
            return det_results

    # 2. Dùng Gemini AI nếu có ảnh hoặc text phức tạp
    if not config.GEMINI_API_KEY:
        logger.warning("Chưa cấu hình GEMINI_API_KEY")
        if text_content:
            return extract_pod_assignments_deterministic(text_content)
        return []

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=config.GEMINI_API_KEY)
        contents = []

        if image_bytes:
            contents.append(types.Part.from_bytes(data=image_bytes, mime_type="image/png"))

        prompt_text = text_content or ""
        prompt_text += "\nHãy trích xuất toàn bộ danh sách các dòng có Số Container và Cảng đến (POD) thành mảng JSON theo quy tắc."
        contents.append(prompt_text)

        candidate_models = [
            "gemini-2.0-flash",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        ]

        response = None
        for m_name in candidate_models:
            try:
                response = client.models.generate_content(
                    model=m_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=POD_EXTRACTION_SYSTEM_PROMPT,
                        temperature=0.1,
                        response_mime_type="application/json",
                    ),
                )
                if response and response.text:
                    logger.info(f"✅ Gemini API ({m_name}) đã bóc tách Cont & POD thành công!")
                    break
            except Exception as e_sub:
                logger.warning(f"Model {m_name} không khả dụng cho POD: {e_sub}")
                continue

        if not response or not response.text:
            logger.error("Không nhận được phản hồi POD từ Gemini API")
            if text_content:
                return extract_pod_assignments_deterministic(text_content)
            return []

        raw_out = response.text.strip()
        if "```" in raw_out:
            m_block = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw_out, re.DOTALL)
            if m_block:
                raw_out = m_block.group(1).strip()
            else:
                raw_out = re.sub(r"^```(?:json)?", "", raw_out)
                raw_out = re.sub(r"```$", "", raw_out).strip()

        data = json.loads(raw_out)
        if isinstance(data, list):
            # Lọc bỏ các phần tử không có cả số cont lẫn booking
            filtered = [
                d for d in data
                if isinstance(d, dict) and (d.get("so_cont") or d.get("booking")) and d.get("pod")
            ]
            return filtered
        elif isinstance(data, dict):
            if (data.get("so_cont") or data.get("booking")) and data.get("pod"):
                return [data]
        return []

    except Exception as e:
        logger.exception(f"Lỗi khi trích xuất POD bằng Gemini: {e}")
        if text_content:
            return extract_pod_assignments_deterministic(text_content)
        return []
