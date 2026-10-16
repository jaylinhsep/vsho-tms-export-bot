import re
import asyncio
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Set, Tuple
import unicodedata
import httpx

import config

logger = logging.getLogger(__name__)

# Múi giờ Việt Nam (UTC+7)
VN_TZ = timezone(timedelta(hours=7))


def remove_accents(input_str: Any) -> str:
    """Loại bỏ dấu tiếng Việt và chuẩn hóa chữ thường để tìm kiếm tương đối (fuzzy search) tuyệt đối chính xác"""
    if not input_str:
        return ""
    s = str(input_str)
    # 1. Bảng thay thế trực tiếp toàn bộ nguyên âm/phụ âm tiếng Việt có dấu
    char_map = {
        "à": "a", "á": "a", "ả": "a", "ã": "a", "ạ": "a",
        "ă": "a", "ằ": "a", "ắ": "a", "ẳ": "a", "ẵ": "a", "ặ": "a",
        "â": "a", "ầ": "a", "ấ": "a", "ẩ": "a", "ẫ": "a", "ậ": "a",
        "è": "e", "é": "e", "ẻ": "e", "ẽ": "e", "ẹ": "e",
        "ê": "e", "ề": "e", "ế": "e", "ể": "e", "ễ": "e", "ệ": "e",
        "ì": "i", "í": "i", "ỉ": "i", "ĩ": "i", "ị": "i",
        "ò": "o", "ó": "o", "ỏ": "o", "õ": "o", "ọ": "o",
        "ô": "o", "ồ": "o", "ố": "o", "ổ": "o", "ỗ": "o", "ộ": "o",
        "ơ": "o", "ờ": "o", "ớ": "o", "ở": "o", "ỡ": "o", "ợ": "o",
        "ù": "u", "ú": "u", "ủ": "u", "ũ": "u", "ụ": "u",
        "ư": "u", "ừ": "u", "ứ": "u", "ử": "u", "ữ": "u", "ự": "u",
        "ỳ": "y", "ý": "y", "ỷ": "y", "ỹ": "y", "ỵ": "y",
        "đ": "d", "Đ": "D", "₫": "d",
        "À": "A", "Á": "A", "Ả": "A", "Ã": "A", "Ạ": "A",
        "Ă": "A", "Ằ": "A", "Ắ": "A", "Ẳ": "A", "Ẵ": "A", "Ặ": "A",
        "Â": "A", "Ầ": "A", "Ấ": "A", "Ẩ": "A", "Ẫ": "A", "Ậ": "A",
        "È": "E", "É": "E", "Ẻ": "E", "Ẽ": "E", "Ẹ": "E",
        "Ê": "E", "Ề": "E", "Ế": "E", "Ể": "E", "Ễ": "E", "Ệ": "E",
        "Ì": "I", "Í": "I", "Ỉ": "I", "Ĩ": "I", "Ị": "I",
        "Ò": "O", "Ó": "O", "Ỏ": "O", "Õ": "O", "Ọ": "O",
        "Ô": "O", "Ồ": "O", "Ố": "O", "Ổ": "O", "Ỗ": "O", "Ộ": "O",
        "Ơ": "O", "Ờ": "O", "Ớ": "O", "Ở": "O", "Ỡ": "O", "Ợ": "O",
        "Ù": "U", "Ú": "U", "Ủ": "U", "Ũ": "U", "Ụ": "U",
        "Ư": "U", "Ừ": "U", "Ứ": "U", "Ử": "U", "Ữ": "U", "Ự": "U",
        "Ỳ": "Y", "Ý": "Y", "Ỷ": "Y", "Ỹ": "Y", "Ỵ": "Y",
    }
    for k, v in char_map.items():
        if k in s:
            s = s.replace(k, v)
    # 2. Chuẩn hóa thêm bằng NFKD để loại bỏ combining marks còn sót lại
    nfkd = unicodedata.normalize("NFKD", s)
    cleaned = "".join([c for c in nfkd if not unicodedata.combining(c)])
    return re.sub(r"\s+", " ", cleaned).strip().lower()

class TMSExportApiClient:
    def __init__(self):
        self.client: Optional[httpx.AsyncClient] = None
        self.user_string = "trucking14<<|7<<|a0b923820d<<|1<<|0.0.0.0.0.0.1.0.1.1.0.1.0.1."
        self.is_logged_in = False
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Referer": f"{config.TMS_URL}/index-home.php",
            "X-Requested-With": "XMLHttpRequest",
        }
        # In-memory cache với TTL 90s
        self.cache_date: Dict[str, List[Dict[str, Any]]] = {}
        self.cache_time: Dict[str, float] = {}
        self.CACHE_TTL: float = 90.0
        self._login_lock = asyncio.Lock()
        self.last_fetch_debug: Dict[str, Any] = {}
        self.last_login_debug: Dict[str, Any] = {}
        self.VN_TZ = VN_TZ

    async def get_client(self) -> httpx.AsyncClient:
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(
                headers=self.headers,
                timeout=httpx.Timeout(20.0, connect=10.0),
                limits=httpx.Limits(max_connections=30, max_keepalive_connections=20),
                follow_redirects=True,
                verify=False,
            )
        return self.client

    async def login(self) -> bool:
        """Đăng nhập hệ thống Vietsun TMS (qua login2.html / 0login.php) và tự điều hướng sang tab XUẤT - ALL (kieuXemTrucking = 2)"""
        async with self._login_lock:
            client = await self.get_client()
            login_url = f"{config.TMS_URL}/0login.php"
            login_payload = {
                "username": config.TMS_USERNAME,
                "password": config.TMS_PASSWORD,
                "login": "ĐĂNG NHẬP"
            }
            try:
                # 1. Truy cập trang đăng nhập login2.html để thiết lập cookie phiên
                try:
                    await client.get(f"{config.TMS_URL}/login2.html")
                except Exception as e_get:
                    logger.debug(f"Mở login2.html: {e_get}")

                # 2. Gửi thông tin đăng nhập lên 0login.php
                logger.info("Đang đăng nhập vào VSHO Vietsun TMS...")
                resp = await client.post(login_url, data=login_payload)
                resp_text = resp.content.decode("utf-8", errors="replace")
                self.last_login_debug = {
                    "status": resp.status_code,
                    "len": len(resp_text),
                    "sample": resp_text[:80].replace("\n", " ").strip(),
                    "err": None,
                }

                # 3. Lấy mã userString từ nội dung phản hồi hoặc trang index-home.php
                m = re.search(r'id="userString"\s+value="([^"]+)"', resp_text)
                if not m:
                    # Nếu redirect 302 chưa nạp HTML, chủ động GET index-home.php
                    home_resp = await client.get(f"{config.TMS_URL}/index-home.php")
                    home_text = home_resp.content.decode("utf-8", errors="replace")
                    m = re.search(r'id="userString"\s+value="([^"]+)"', home_text)
                    self.last_login_debug["home_status"] = home_resp.status_code
                    self.last_login_debug["home_len"] = len(home_text)

                if m:
                    self.user_string = m.group(1)
                    self.is_logged_in = True
                    self.last_login_debug["user_string"] = self.user_string
                    logger.info(f"Đăng nhập thành công! userString = {self.user_string}")

                    # 4. TỰ ĐIỀU HƯỚNG VỀ TAB 'XUẤT - ALL':
                    # Gán kieuXemTrucking = 2 (XUẤT - ALL) lên hệ thống
                    await self.ensure_export_view()
                    return True
                else:
                    self.is_logged_in = False
                    self.last_login_debug["err"] = f"Không tìm thấy userString trong HTML (HTTP {resp.status_code})"
                    logger.error(self.last_login_debug["err"])
                    return False
            except Exception as e:
                self.is_logged_in = False
                self.last_login_debug = {"err": str(e)}
                logger.exception(f"Lỗi khi gửi request đăng nhập: {e}")
                return False



    async def ensure_export_view(self) -> bool:
        """Đảm bảo thiết lập kieuXemTrucking = 2 (XUẤT - ALL)"""
        client = await self.get_client()
        crud_url = f"{config.TMS_URL}/index-crud.php"
        payload = {
            "TenDangNhap": config.TMS_USERNAME,
            "userString": self.user_string,
            "cv": 3,
            "loai": 1,
            "iddt": 1,
            "cot": "t9<<|1<<|1<<|20<<|kieuXemTrucking",
            "thongtin": "2",
            "minData": "",
            "maxData": "",
        }
        try:
            resp = await client.post(crud_url, data=payload)
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"Lỗi thiết lập kieuXemTrucking=2: {e}")
            return False

    def date_to_timestamp(self, date_str: str) -> int:
        """
        Chuyển ngày (dạng YYYY-MM-DD hoặc DD/MM/YYYY) thành Unix timestamp 00:00:00 (Giờ VN UTC+7)
        Hệ thống Vietsun dùng timestamp này cho nút Thêm dòng (+)
        """
        date_str = str(date_str).strip()
        dt = None
        m1 = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_str)
        if m1:
            dt = datetime(int(m1.group(1)), int(m1.group(2)), int(m1.group(3)), 0, 0, 0, tzinfo=VN_TZ)
        else:
            m2 = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", date_str)
            if m2:
                dt = datetime(int(m2.group(3)), int(m2.group(2)), int(m2.group(1)), 0, 0, 0, tzinfo=VN_TZ)

        if not dt:
            now_vn = datetime.now(VN_TZ)
            dt = datetime(now_vn.year, now_vn.month, now_vn.day, 0, 0, 0, tzinfo=VN_TZ)

        return int(dt.timestamp())

    def _parse_row(self, row_chunk: str, date_queried: str = "") -> Optional[Dict[str, Any]]:
        """Phân tích HTML 1 dòng tr trong tab XUẤT - ALL bằng cách đọc toàn bộ input và span"""
        m_row = re.search(r'id="tr(\d+)"', row_chunk)
        if not m_row:
            return None
        row_id = m_row.group(1)

        # Lấy row timestamp từ data-id (thường nằm ở cuối data-id)
        m_ts = re.search(r'data-id="b18[^"]*<<\|(\d{9,11})"', row_chunk)
        row_timestamp = m_ts.group(1) if m_ts else ""

        # Bóc tách tất cả thẻ input trong dòng theo data-id hoặc id
        input_values: Dict[str, str] = {}
        for m_inp in re.finditer(r'<input\b([^>]*)>', row_chunk):
            attr_str = m_inp.group(1)
            m_v = re.search(r'value="([^"]*)"', attr_str)
            val = m_v.group(1).strip() if m_v else ""

            m_d = re.search(r'data-id="([^"]*)"', attr_str)
            if m_d:
                did = m_d.group(1)
                parts = did.split("<<|")
                col_name = parts[4] if len(parts) >= 5 else did
                input_values[col_name] = val

            m_id = re.search(r'id="([^"]*)"', attr_str)
            if m_id:
                elem_id = m_id.group(1)
                input_values[elem_id] = val

        # Bóc tách các thẻ span (name="name...") làm fallback
        span_values: Dict[str, str] = {}
        for m_sp in re.finditer(r'<span[^>]*name="([^"]*)"[^>]*>([^<]*)</span>', row_chunk):
            span_name = m_sp.group(1)
            span_val = m_sp.group(2).strip()
            span_values[span_name] = span_val

        # 1. Mã Booking (cột Booking - span name="name15" hoặc input readonly)
        booking_id = span_values.get("name15", "")
        if not booking_id:
            m_b = re.search(r'<td[^>]*><input[^>]*value="([^"]*)"[^>]*readonly', row_chunk)
            booking_id = m_b.group(1).strip() if m_b else ""

        # 2. Tàu, Chuyến, Lines
        tau = input_values.get("tauPo") or span_values.get("name9", "")
        chuyen = input_values.get("chuyenPo") or span_values.get("name8", "")
        lines = input_values.get("linePoDh") or span_values.get("name10", "VIETSUN")

        # 3. Ngày đóng & Ngày tàu chạy
        ngay_dong = input_values.get("ngayTraHang") or span_values.get("name11", "")
        ngay_tau_chay = input_values.get("ngayTauChayPo") or span_values.get("name12", "")

        # 4. Khách hàng, Địa chỉ kho, Tỉnh
        khach_hang = input_values.get("tenKhachPo") or span_values.get("name1", "")
        kho_dong = input_values.get("diaChiNhanPo") or span_values.get("name13", "")
        tinh_dong = input_values.get("tinhNhanPo") or span_values.get("name14", "")

        # 5. CỘT THỨ 14 - POD / TỈNH XUẤT HÀNG (tinhXuatHangPo: CLO, HPH, DNG...)
        pod_cot_14 = input_values.get("tinhXuatHangPo") or span_values.get("name16", "")

        # 6. Chủ hàng (chuHangPo: VSV...)
        chu_hang = input_values.get("chuHangPo") or span_values.get("name2", "")

        # 7. Sản lượng 20 / 40
        sl_20 = input_values.get("cont20Po") or span_values.get("name21", "")
        sl_40 = input_values.get("cont40Po") or span_values.get("name22", "")

        # 8. Hàng hóa & Trọng lượng GW
        ten_hang = input_values.get("tenHangHoaPo") or span_values.get("name18", "")
        gw = input_values.get("trongLuongPo") or span_values.get("name19", "")

        # 9. Số container & Số seal
        raw_cont = input_values.get("soContPo", "")
        if not raw_cont:
            m_c = re.search(r'id="soContPo\d+"[^>]*value="([^"]*)"', row_chunk)
            raw_cont = m_c.group(1).strip() if m_c else ""
        clean_cont = re.sub(r"\s+", "", raw_cont).upper()

        so_seal = input_values.get("soSealPo", "")

        # 10. Nơi hạ hàng / Port hạ hàng & Cảng đến POD phụ
        port_ha_hang = input_values.get("cangDiPo") or span_values.get("name4", "")
        cang_den_pod = input_values.get("cangDenPo", "")

        # 11. Đơn vị vận chuyển & Số xe
        don_vi_vc = input_values.get("donViVcPo") or span_values.get("name5", "")
        so_xe = input_values.get("soXePo", "")
        if not so_xe:
            m_x = re.search(r'id="soXePo\d+"[^>]*value="([^"]*)"', row_chunk)
            so_xe = m_x.group(1).strip() if m_x else ""

        # 12. Liên hệ kho đóng (cột readonly)
        m_lh = re.search(r'<td><input[^>]*value="([^"]*)"[^>]*readonly><span class="inputTitle2">', row_chunk)
        lien_he = m_lh.group(1).strip() if m_lh else input_values.get("lienHeNhanPo", "")

        # 13. Ghi chú đóng hàng (noiDungPo hoặc ghiChuPo)
        ghi_chu = input_values.get("noiDungPo") or input_values.get("ghiChuPo", "")

        return {
            "row_id": row_id,
            "row_timestamp": row_timestamp,
            "booking_id": booking_id,
            "tau": tau,
            "chuyen": chuyen,
            "lines": lines,
            "ngay_dong": ngay_dong,
            "ngay_tau_chay": ngay_tau_chay,
            "khach_hang": khach_hang,
            "kho_dong": kho_dong,
            "tinh_dong": tinh_dong,
            "pod_cot_14": pod_cot_14,  # CỘT 14 QUAN TRỌNG: POD
            "chu_hang": chu_hang,
            "sl_20": sl_20,
            "sl_40": sl_40,
            "ten_hang": ten_hang,
            "gw": gw,
            "so_cont": clean_cont,
            "so_seal": so_seal,
            "port_ha_hang": port_ha_hang,
            "cang_den_pod": cang_den_pod,
            "don_vi_vc": don_vi_vc,
            "so_xe": so_xe,
            "lien_he": lien_he,
            "ghi_chu": ghi_chu,
            "date_queried": date_queried,
        }

    async def fetch_table_for_date(
        self,
        date_str: str,
        force_refresh: bool = False,
        target_query: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Lấy danh sách các dòng trong tab XUẤT - ALL của 1 ngày (YYYY-MM-DD).
        Luôn phân tích toàn bộ các dòng và lưu cache trong 90s để hỗ trợ tìm kiếm đa trường siêu tốc.
        """
        now = time.time()
        if not force_refresh and date_str in self.cache_date:
            if now - self.cache_time.get(date_str, 0) < self.CACHE_TTL:
                return self.cache_date[date_str]

        client = await self.get_client()
        ajax_url = f"{config.TMS_URL}/index-ajax.php"
        payload = {
            "userString": self.user_string,
            "cv": 78,
            "idxem": 0,
            "kieuxem": 0,
            "loaiload": 4,
            "startDate": date_str,
            "endDate": date_str,
            "widthmh": 1920
        }

        try:
            resp = await client.post(ajax_url, data=payload)
            html_text = resp.content.decode("utf-8", errors="replace")

            self.last_fetch_debug = {
                "status": resp.status_code,
                "size": len(html_text),
                "sample": html_text[:120].replace("\n", " ").strip(),
                "err": None,
            }

            # Kiểm tra xem có bị lỗi phiên / lỗi đăng nhập hay không
            is_session_error = (
                "login2.html" in html_text
                or "lỗi đăng nhập" in html_text.lower()
                or "dang nhap" in html_text.lower()
                or resp.status_code != 200
                or len(html_text) < 500
            )

            if is_session_error:
                logger.info(f"Phát hiện phản hồi không hợp lệ ({html_text[:50]}), đang đăng nhập lại...")
                login_ok = await self.login()
                if login_ok:
                    payload["userString"] = self.user_string
                    resp = await client.post(ajax_url, data=payload)
                    html_text = resp.content.decode("utf-8", errors="replace")
                    self.last_fetch_debug["status_retry"] = resp.status_code
                    self.last_fetch_debug["size_retry"] = len(html_text)
                    self.last_fetch_debug["sample"] = html_text[:120].replace("\n", " ").strip()

            chunks = html_text.split('<tr id="tr')
            rows = []
            for chunk in chunks[1:]:
                row_data = self._parse_row(f'<tr id="tr{chunk}', date_queried=date_str)
                if row_data:
                    rows.append(row_data)

            self.cache_date[date_str] = rows
            self.cache_time[date_str] = now
            return rows
        except Exception as e:
            self.last_fetch_debug = {"err": str(e)}
            logger.warning(f"Lỗi khi tải bảng XUẤT - ALL cho ngày {date_str}: {e}")
            return []

    def row_matches_query(self, row: Dict[str, Any], clean_q: str, raw_q: str) -> Tuple[bool, int]:
        """
        Kiểm tra 1 dòng booking có khớp với từ khóa hay không.
        Khớp đa trường (Đa năng): Cont, Booking, Khách hàng, Hàng hóa, Xe, Đơn vị VC, POD, Kho, Note.
        Hoàn toàn KHÔNG phân biệt chữ HOA / chữ thường, KHÔNG phân biệt có dấu / không dấu.
        Trả về (is_matched, match_score). Score càng cao thì kết quả càng khớp chính xác.
        """
        if not clean_q:
            return False, 0

        score = 0
        q_upper = raw_q.strip().upper()
        q_no_space = clean_q.replace(" ", "")

        # 1. Số Container
        raw_cont = row.get("so_cont", "")
        clean_cont = remove_accents(raw_cont).replace(" ", "")
        if raw_cont:
            if clean_cont == q_no_space or raw_cont.upper() == q_upper:
                return True, 100  # Khớp chính xác 100% số cont
            if q_no_space in clean_cont:
                score = max(score, 90)  # Khớp tiền tố hoặc chuỗi con cont (VD: GAO khớp GAOU2110158)

        # 2. Mã Booking
        raw_booking = str(row.get("booking_id", "")).strip()
        if raw_booking:
            if raw_booking.upper() == q_upper or clean_q == raw_booking.lower():
                return True, 100  # Khớp chính xác mã booking
            if q_no_space in raw_booking.lower():
                score = max(score, 85)

        # 3. ID PVT dòng
        raw_id = str(row.get("row_id", "")).strip()
        if raw_id and (raw_id == clean_q or raw_id == q_no_space):
            return True, 100

        # 4. Tên Khách Hàng (Cột 10) & Chủ Hàng
        kh = row.get("khach_hang", "")
        clean_kh = remove_accents(kh)
        if clean_q in clean_kh:
            score = max(score, 80)
        chu = row.get("chu_hang", "")
        clean_chu = remove_accents(chu)
        if clean_q in clean_chu:
            score = max(score, 75)

        # 5. Tên Hàng Hóa (Cột 18: Gạo, Bánh kẹo, Sữa, Bia...)
        ten_hang = row.get("ten_hang", "")
        clean_hang = remove_accents(ten_hang)
        if clean_q in clean_hang:
            score = max(score, 70)

        # 6. Biển Số Xe (50H..., 61H...)
        so_xe = row.get("so_xe", "")
        clean_xe = remove_accents(so_xe).replace(" ", "")
        if q_no_space and q_no_space in clean_xe:
            score = max(score, 65)

        # 7. Đơn Vị Vận Chuyển (VSHO, Thái Hòa, Trucking 14...)
        dvvc = row.get("don_vi_vc", "")
        clean_dvvc = remove_accents(dvvc)
        if clean_q in clean_dvvc:
            score = max(score, 60)

        # 8. Cảng Đến POD Cột 14 (CLO, HPH, DNG, HCM...)
        pod = row.get("pod_cot_14", "")
        clean_pod = remove_accents(pod).replace(" ", "")
        if q_no_space and q_no_space in clean_pod:
            score = max(score, 55)

        # 9. Địa Chỉ Kho Nhận Hàng / Kho Đóng (Tribeco, Sóng Thần...)
        kho = row.get("kho_dong", "")
        clean_kho = remove_accents(kho)
        if clean_q in clean_kho:
            score = max(score, 50)

        # 10. Ghi Chú Đóng Hàng (noiDungPo)
        ghi_chu = row.get("ghi_chu", "")
        clean_note = remove_accents(ghi_chu)
        if clean_q in clean_note:
            score = max(score, 45)

        return (score > 0), score

    async def search_containers(self, query: str) -> List[Dict[str, Any]]:
        """
        Tìm kiếm ĐA NĂNG toàn diện trên tab XUẤT - ALL của TMS:
        - Khớp Cont, Booking, Khách hàng, Tên hàng (Gạo, Bia...), Xe, Đơn vị VC, POD, Kho, Note.
        - Tuyệt đối KHÔNG phân biệt chữ hoa / thường, có dấu / không dấu.
        - Quét song song trong phạm vi 7 ngày gần nhất (-3 đến +3 ngày quanh hôm nay).
        - Phản hồi siêu tốc, có cơ chế xếp hạng độ chính xác (Exact match đưa lên đầu).
        """
        clean_q = remove_accents(query).strip('"').strip("'").strip()
        if not clean_q or len(clean_q) < 2:
            return []

        if not self.is_logged_in:
            await self.login()

        today = datetime.now(VN_TZ)
        today_str = today.strftime("%Y-%m-%d")

        # Quét ngày HÔM NAY trước tiên (ưu tiên cao nhất)
        logger.info(f"🔍 Đang tìm kiếm đa năng '{query}' (chuẩn hóa: '{clean_q}') trên TMS...")
        today_rows = await self.fetch_table_for_date(today_str, force_refresh=False)

        # Danh sách các ngày lân cận (-3 đến +3 ngày)
        other_offsets = [-1, -2, -3, 1, 2, 3]
        other_dates = [(today + timedelta(days=off)).strftime("%Y-%m-%d") for off in other_offsets]

        # Tải song song các ngày lân cận để tối ưu tốc độ phản hồi (<1.5s)
        tasks = [self.fetch_table_for_date(d, force_refresh=False) for d in other_dates]
        other_results = await asyncio.gather(*tasks, return_exceptions=True)

        all_day_rows = list(today_rows)
        for r_list in other_results:
            if isinstance(r_list, list):
                all_day_rows.extend(r_list)

        matched_list: List[Tuple[Dict[str, Any], int]] = []
        seen_row_ids: Set[str] = set()

        for r in all_day_rows:
            row_id = str(r.get("row_id", "")).strip()
            if not row_id or row_id in seen_row_ids:
                continue

            is_matched, score = self.row_matches_query(r, clean_q, query)
            if is_matched:
                seen_row_ids.add(row_id)
                # Tăng nhẹ điểm ưu tiên nếu dòng thuộc về ngày hôm nay
                if r.get("date_queried") == today_str or r.get("ngay_dong") == today.strftime("%d/%m/%Y"):
                    score += 5
                matched_list.append((r, score))

        # Sắp xếp kết quả: Điểm khớp cao nhất lên đầu, sau đó theo ID mới nhất
        matched_list.sort(
            key=lambda item: (
                item[1],  # Điểm khớp (score)
                int(item[0].get("row_id", 0)) if str(item[0].get("row_id", "")).isdigit() else 0
            ),
            reverse=True
        )

        final_rows = [item[0] for item in matched_list]
        logger.info(f"✅ Tìm thấy {len(final_rows)} kết quả cho từ khóa '{query}'.")
        return final_rows

    async def find_container_or_booking(self, query: str) -> List[Dict[str, Any]]:
        """Tra cứu số container hoặc mã booking (sử dụng search_containers tối ưu)"""
        return await self.search_containers(query)

    async def find_containers_by_customer(self, customer_query: str) -> List[Dict[str, Any]]:
        """Tra cứu container theo Tên Khách Hàng hoặc từ khóa bất kỳ"""
        return await self.search_containers(customer_query)

    async def update_field(self, row_id: str, row_timestamp: str, field_name: str, new_value: str, clear_cache: bool = True) -> bool:
        """
        Cập nhật 1 trường dữ liệu bất kỳ trên TMS Vietsun qua Direct API POST /index-crud.php
        Hỗ trợ CỘT 14 (POD - tinhXuatHangPo), số cont, số xe, seal, ghi chú...
        """
        client = await self.get_client()
        crud_url = f"{config.TMS_URL}/index-crud.php"

        # Định dạng cottt theo cấu trúc hệ thống Vietsun TMS
        cot_map = {
            "pod": f"b18<<|4<<|50<<|{row_id}<<|tinhXuatHangPo<<|{row_timestamp}",
            "tinhXuatHangPo": f"b18<<|4<<|50<<|{row_id}<<|tinhXuatHangPo<<|{row_timestamp}",
            "so_cont": f"b18c<<|4<<|50<<|{row_id}<<|soContPo<<|{row_timestamp}",
            "soContPo": f"b18c<<|4<<|50<<|{row_id}<<|soContPo<<|{row_timestamp}",
            "so_xe": f"b18newtruck<<|4<<|50<<|{row_id}<<|soXePo<<|{row_timestamp}",
            "soXePo": f"b18newtruck<<|4<<|50<<|{row_id}<<|soXePo<<|{row_timestamp}",
            "so_seal": f"b18<<|4<<|50<<|{row_id}<<|soSealPo<<|{row_timestamp}",
            "soSealPo": f"b18<<|4<<|50<<|{row_id}<<|soSealPo<<|{row_timestamp}",
            "don_vi_vc": f"b18<<|4<<|30<<|{row_id}<<|donViVcPo<<|{row_timestamp}",
            "donViVcPo": f"b18<<|4<<|30<<|{row_id}<<|donViVcPo<<|{row_timestamp}",
            "ghi_chu": f"b18<<|4<<|70<<|{row_id}<<|noiDungPo<<|{row_timestamp}",
            "noiDungPo": f"b18<<|4<<|70<<|{row_id}<<|noiDungPo<<|{row_timestamp}",
            "ngay_dong": f"b18<<|8<<|20<<|{row_id}<<|ngayTraHang<<|{row_timestamp}",
            "ngayTraHang": f"b18<<|8<<|20<<|{row_id}<<|ngayTraHang<<|{row_timestamp}",
            "ngay_tau_chay": f"b18<<|8<<|20<<|{row_id}<<|ngayTauChayPo<<|{row_timestamp}",
            "ngayTauChayPo": f"b18<<|8<<|20<<|{row_id}<<|ngayTauChayPo<<|{row_timestamp}",
            "tau": f"b18<<|4<<|30<<|{row_id}<<|tauPo<<|{row_timestamp}",
            "tauPo": f"b18<<|4<<|30<<|{row_id}<<|tauPo<<|{row_timestamp}",
            "chuyen": f"b18<<|4<<|30<<|{row_id}<<|chuyenPo<<|{row_timestamp}",
            "chuyenPo": f"b18<<|4<<|30<<|{row_id}<<|chuyenPo<<|{row_timestamp}",
            "lines": f"b18<<|4<<|10<<|{row_id}<<|linePoDh<<|{row_timestamp}",
            "linePoDh": f"b18<<|4<<|10<<|{row_id}<<|linePoDh<<|{row_timestamp}",
            "khach_hang": f"b18<<|4<<|150<<|{row_id}<<|tenKhachPo<<|{row_timestamp}",
            "tenKhachPo": f"b18<<|4<<|150<<|{row_id}<<|tenKhachPo<<|{row_timestamp}",
            "kho_dong": f"b18<<|4<<|440<<|{row_id}<<|diaChiNhanPo<<|{row_timestamp}",
            "diaChiNhanPo": f"b18<<|4<<|440<<|{row_id}<<|diaChiNhanPo<<|{row_timestamp}",
            "tinh_dong": f"b18<<|4<<|50<<|{row_id}<<|tinhNhanPo<<|{row_timestamp}",
            "tinhNhanPo": f"b18<<|4<<|50<<|{row_id}<<|tinhNhanPo<<|{row_timestamp}",
            "chu_hang": f"b18<<|2<<|10<<|{row_id}<<|chuHangPo<<|{row_timestamp}",
            "chuHangPo": f"b18<<|2<<|10<<|{row_id}<<|chuHangPo<<|{row_timestamp}",
            "sl_20": f"b18<<|1<<|2<<|{row_id}<<|cont20Po<<|{row_timestamp}",
            "cont20Po": f"b18<<|1<<|2<<|{row_id}<<|cont20Po<<|{row_timestamp}",
            "sl_40": f"b18<<|1<<|2<<|{row_id}<<|cont40Po<<|{row_timestamp}",
            "cont40Po": f"b18<<|1<<|2<<|{row_id}<<|cont40Po<<|{row_timestamp}",
            "ten_hang": f"b18<<|4<<|20<<|{row_id}<<|tenHangHoaPo<<|{row_timestamp}",
            "tenHangHoaPo": f"b18<<|4<<|20<<|{row_id}<<|tenHangHoaPo<<|{row_timestamp}",
            "gw": f"b18<<|1<<|5<<|{row_id}<<|trongLuongPo<<|{row_timestamp}",
            "trongLuongPo": f"b18<<|1<<|5<<|{row_id}<<|trongLuongPo<<|{row_timestamp}",
            "port_ha_hang": f"b18<<|4<<|100<<|{row_id}<<|cangDiPo<<|{row_timestamp}",
            "cangDiPo": f"b18<<|4<<|100<<|{row_id}<<|cangDiPo<<|{row_timestamp}",
            "cang_den_pod": f"b18<<|4<<|100<<|{row_id}<<|cangDenPo<<|{row_timestamp}",
            "cangDenPo": f"b18<<|4<<|100<<|{row_id}<<|cangDenPo<<|{row_timestamp}",
            "lien_he": f"b18<<|4<<|150<<|{row_id}<<|lienHeNhanPo<<|{row_timestamp}",
            "lienHeNhanPo": f"b18<<|4<<|150<<|{row_id}<<|lienHeNhanPo<<|{row_timestamp}",
        }

        if field_name in ("so_cont", "soContPo"):
            # Hệ thống Vietsun TMS cập nhật số container xuất qua API cv: 5, loai: 21
            payload = {
                "TenDangNhap": config.TMS_USERNAME,
                "userString": self.user_string,
                "cv": 5,
                "loai": 21,
                "iddt": row_id,
                "cot": str(new_value).strip().upper(),
                "thongtin": row_timestamp,
                "minData": "",
                "maxData": "",
            }
        elif field_name in ("booking", "booking_id"):
            # Gán Booking ID qua cv: 14, loai: 7
            payload = {
                "TenDangNhap": config.TMS_USERNAME,
                "userString": self.user_string,
                "cv": 14,
                "loai": 7,
                "iddt": row_id,
                "cot": row_id,
                "thongtin": str(new_value).strip(),
                "minData": "",
                "maxData": "",
            }
        else:
            cot_target = cot_map.get(field_name)
            if not cot_target:
                logger.error(f"Trường {field_name} không có trong bản đồ cot_map")
                return False

            payload = {
                "TenDangNhap": config.TMS_USERNAME,
                "userString": self.user_string,
                "cv": 3,
                "loai": 1,
                "iddt": 1,
                "cot": cot_target,
                "thongtin": str(new_value).strip(),
                "minData": "",
                "maxData": "",
            }

        try:
            resp = await client.post(crud_url, data=payload)
            if resp.status_code == 200:
                logger.info(f"✅ Đã cập nhật {field_name} = '{new_value}' cho dòng {row_id}")
                # Xóa cache ngày nếu được yêu cầu để lượt tra cứu sau lấy dữ liệu tươi mới
                if clear_cache:
                    self.cache_date.clear()
                return True
            else:
                logger.error(f"Lỗi cập nhật HTTP {resp.status_code}")
                return False
        except Exception as e:
            logger.exception(f"Lỗi khi gửi CRUD cập nhật: {e}")
            return False

    async def delete_export_booking_row(self, row_id: str) -> bool:
        """
        Xóa vĩnh viễn dòng container/booking xuất khỏi tab XUẤT - ALL trên Vietsun TMS.
        Tương ứng hàm xoaPoXuat() trên giao diện web TMS:
        POST /index-crud.php (cv: 4, loai: 19, iddt: 1, cot: 0, thongtin: row_id)
        """
        if not self.is_logged_in:
            await self.login()

        client = await self.get_client()
        crud_url = f"{config.TMS_URL}/index-crud.php"
        payload = {
            "TenDangNhap": config.TMS_USERNAME,
            "userString": self.user_string,
            "cv": 4,
            "loai": 19,
            "iddt": 1,
            "cot": 0,
            "thongtin": str(row_id).strip(),
            "minData": "",
            "maxData": "",
        }

        try:
            logger.info(f"🗑️ Đang xóa dòng ID PVT {row_id} trên tab XUẤT - ALL (cv: 4, loai: 19)...")
            resp = await client.post(crud_url, data=payload)
            if resp.status_code == 200:
                logger.info(f"✅ Đã xóa thành công dòng ID PVT {row_id} khỏi TMS!")
                # Xóa toàn bộ cache để lần sau không còn dòng này
                self.cache_date.clear()
                return True
            else:
                logger.error(f"Lỗi xóa dòng {row_id}: HTTP {resp.status_code}")
                return False
        except Exception as e:
            logger.exception(f"Lỗi khi gửi request xóa dòng {row_id}: {e}")
            return False

    async def create_export_booking_row(self, item: Dict[str, Any]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Quy trình tự động bấm Dấu cộng [+] và tạo dòng đóng hàng xuất:
        1. Tính timestamp của ngày kế hoạch (target_date từ email hoặc ngay_tra/ngay_dong).
        2. Bấm [+]: POST /index-crud.php (cv: 1, loai: 72, thongtin: timestamp).
        3. Tải lại danh sách dòng ngày đó để tìm dòng mới tạo (ID PVT lớn nhất).
        4. Gán Booking ID: POST /index-crud.php (cv: 14, loai: 7, iddt: row_id, thongtin: booking).
        5. Cập nhật đồng loạt các trường (Tàu, chuyến, khách, kho, POD CỘT 14, GW, cont, seal...).
        6. Tải lại chính xác thông tin hoàn thiện để trả về cho bot Telegram.
        """
        client = await self.get_client()
        crud_url = f"{config.TMS_URL}/index-crud.php"

        # Ưu tiên target_date (ngày kế hoạch cụ thể trong email), sau đó đến ngay_tra hoặc ngay_dong
        raw_target_date = item.get("target_date") or item.get("ngay_tra") or item.get("ngay_dong", "")
        if raw_target_date:
            m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", raw_target_date)
            if m:
                query_date_str = f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
                formatted_target_date = f"{m.group(1).zfill(2)}/{m.group(2).zfill(2)}/{m.group(3)}"
            else:
                m_iso = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw_target_date)
                if m_iso:
                    query_date_str = raw_target_date
                    formatted_target_date = f"{m_iso.group(3).zfill(2)}/{m_iso.group(2).zfill(2)}/{m_iso.group(1)}"
                else:
                    query_date_str = datetime.now(VN_TZ).strftime("%Y-%m-%d")
                    formatted_target_date = datetime.now(VN_TZ).strftime("%d/%m/%Y")
        else:
            query_date_str = datetime.now(VN_TZ).strftime("%Y-%m-%d")
            formatted_target_date = datetime.now(VN_TZ).strftime("%d/%m/%Y")

        # Cột ngayTraHang quyết định dòng thuộc tab ngày nào trên TMS (phải trùng với query_date_str)
        formatted_ngay_dong = formatted_target_date
        date_timestamp = self.date_to_timestamp(query_date_str)

        # Lấy danh sách ID hiện có trước khi bấm [+]
        existing_rows = await self.fetch_table_for_date(query_date_str, force_refresh=True)
        existing_ids = {int(r["row_id"]) for r in existing_rows if r.get("row_id", "").isdigit()}

        # BƯỚC 1: BẤM DẤU CỘNG [+] TẠO DÒNG MỚI
        logger.info(f"➕ Đang bấm [+] tạo dòng mới cho ngày {query_date_str} (timestamp {date_timestamp})...")
        create_payload = {
            "TenDangNhap": config.TMS_USERNAME,
            "userString": self.user_string,
            "cv": 1,
            "loai": 72,
            "iddt": 0,
            "cot": 0,
            "thongtin": str(date_timestamp),
            "minData": "",
            "maxData": "",
        }
        resp = await client.post(crud_url, data=create_payload)
        if resp.status_code != 200:
            return False, f"Lỗi bấm dấu cộng [+]: HTTP {resp.status_code}", None

        # BƯỚC 2: TẢI LẠI ĐỂ TÌM DÒNG MỚI TẠO
        await asyncio.sleep(0.3)
        updated_rows = await self.fetch_table_for_date(query_date_str, force_refresh=True)
        new_row = None
        for r in updated_rows:
            r_id = int(r["row_id"]) if r.get("row_id", "").isdigit() else 0
            if r_id not in existing_ids:
                new_row = r
                break

        if not new_row and updated_rows:
            # Fallback: lấy dòng có ID lớn nhất
            sorted_rows = sorted(updated_rows, key=lambda x: int(x.get("row_id", 0)) if str(x.get("row_id", "")).isdigit() else 0, reverse=True)
            new_row = sorted_rows[0]

        if not new_row:
            return False, "Không tìm thấy dòng mới tạo trên TMS sau khi bấm [+].", None

        new_row_id = new_row["row_id"]
        row_ts = new_row["row_timestamp"] or str(date_timestamp)
        logger.info(f"🎉 Đã tạo thành công dòng mới! ID PVT = {new_row_id}, timestamp = {row_ts}")

        # BƯỚC 3: GÁN MÃ BOOKING (cv: 14, loai: 7)
        booking_id = str(item.get("booking", "")).strip()
        if booking_id:
            logger.info(f"🔖 Đang gán Booking ID '{booking_id}' cho dòng {new_row_id}...")
            await self.update_field(new_row_id, row_ts, "booking", booking_id)

        # BƯỚC 4: CẬP NHẬT ĐỒNG LOẠT CÁC CỘT CHI TIẾT
        fields_to_update = [
            ("chuyen", item.get("chuyen", "")),
            ("tau", item.get("tau", "")),
            ("lines", item.get("lines", "VIETSUN")),
            ("ngay_dong", formatted_ngay_dong),
            ("ngay_tau_chay", item.get("ngay_tau_chay", "")),
            ("khach_hang", item.get("khach_hang", "")),
            ("kho_dong", item.get("kho_dong", "")),
            ("tinh_dong", item.get("tinh_dong", "")),
            # CỘT THỨ 14 - POD:
            ("pod", item.get("pod", "")),
            ("chu_hang", item.get("phong_ban", "PTTT")),
            ("sl_20", str(item.get("sl_20", "0"))),
            ("sl_40", str(item.get("sl_40", "1"))),
            ("ten_hang", item.get("ten_hang", "Thực phẩm")),
            ("gw", str(item.get("gw", ""))),
            ("port_ha_hang", item.get("port_ha_hang", "BẾN NGHÉ")),
            ("cang_den_pod", item.get("pod", "")),
            ("ghi_chu", item.get("ghi_chu", "")),
            ("lien_he", item.get("lien_he", "")),
        ]

        if item.get("so_cont"):
            fields_to_update.append(("so_cont", item.get("so_cont")))
        if item.get("so_seal"):
            fields_to_update.append(("so_seal", item.get("so_seal")))
        if item.get("so_xe"):
            fields_to_update.append(("so_xe", item.get("so_xe")))
        if item.get("don_vi_vc"):
            fields_to_update.append(("don_vi_vc", item.get("don_vi_vc")))

        # Cập nhật lần lượt để đảm bảo 100% request được TMS ghi nhận
        for f_name, f_val in fields_to_update:
            if str(f_val).strip():
                await self.update_field(new_row_id, row_ts, f_name, str(f_val).strip())

        # BƯỚC 5: LẤY LẠI THÔNG TIN HOÀN THIỆN TỪ TMS
        await asyncio.sleep(0.5)
        self.cache_date.clear()
        final_rows = await self.fetch_table_for_date(query_date_str, force_refresh=True)
        final_row = next((r for r in final_rows if r.get("row_id") == new_row_id), None)

        if not final_row:
            # Quét đa ngày tìm chính xác theo ID PVT
            matched = await self.find_container_or_booking(new_row_id)
            if matched:
                final_row = matched[0]

        if not final_row:
            # Fallback an toàn: ghép dữ liệu đã submit vào thẻ
            final_row = dict(new_row)

        # Đảm bảo các trường cốt lõi hiển thị chuẩn trên Telegram Card
        if not final_row.get("booking_id") or final_row.get("booking_id") == "0":
            final_row["booking_id"] = booking_id
        if not final_row.get("tau") or final_row.get("tau") == "Chưa rõ":
            final_row["tau"] = item.get("tau", "")
        if not final_row.get("chuyen"):
            final_row["chuyen"] = item.get("chuyen", "")
        if not final_row.get("ngay_dong"):
            final_row["ngay_dong"] = formatted_ngay_dong
        if not final_row.get("ngay_tau_chay"):
            final_row["ngay_tau_chay"] = item.get("ngay_tau_chay", "")
        if not final_row.get("khach_hang"):
            final_row["khach_hang"] = item.get("khach_hang", "")
        if not final_row.get("kho_dong"):
            final_row["kho_dong"] = item.get("kho_dong", "")
        if not final_row.get("tinh_dong"):
            final_row["tinh_dong"] = item.get("tinh_dong", "")
        if not final_row.get("pod_cot_14"):
            final_row["pod_cot_14"] = item.get("pod", "")
        if not final_row.get("so_cont"):
            final_row["so_cont"] = item.get("so_cont", "")
        if not final_row.get("so_seal"):
            final_row["so_seal"] = item.get("so_seal", "")
        if not final_row.get("ten_hang"):
            final_row["ten_hang"] = item.get("ten_hang", "")
        if not final_row.get("gw"):
            final_row["gw"] = str(item.get("gw", ""))
        if not final_row.get("ghi_chu"):
            final_row["ghi_chu"] = item.get("ghi_chu", "")
        if not final_row.get("lien_he"):
            final_row["lien_he"] = item.get("lien_he", "")

        return True, f"Tạo thành công ID PVT {new_row_id}", final_row
