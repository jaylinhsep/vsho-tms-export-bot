import re
import asyncio
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Set, Tuple
import httpx

import config

logger = logging.getLogger(__name__)

# Múi giờ Việt Nam (UTC+7)
VN_TZ = timezone(timedelta(hours=7))

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
        """Đăng nhập hệ thống Vietsun TMS và chuyển sang chế độ XUẤT - ALL (kieuXemTrucking = 2)"""
        client = await self.get_client()
        login_url = f"{config.TMS_URL}/0login.php"
        login_payload = {
            "username": config.TMS_USERNAME,
            "password": config.TMS_PASSWORD,
            "login": "ĐĂNG NHẬP"
        }
        try:
            logger.info("Đang đăng nhập vào VSHO Vietsun TMS...")
            resp = await client.post(login_url, data=login_payload)
            if resp.status_code == 200:
                m = re.search(r'id="userString"\s+value="([^"]+)"', resp.text)
                if m:
                    self.user_string = m.group(1)
                self.is_logged_in = True
                logger.info(f"Đăng nhập thành công! userString = {self.user_string}")

                # Chuyển tab hiển thị sang XUẤT - ALL (giá trị 2)
                await self.ensure_export_view()
                return True
            else:
                logger.error(f"Đăng nhập thất bại: HTTP {resp.status_code}")
                return False
        except Exception as e:
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
        """Phân tích HTML 1 dòng tr trong tab XUẤT - ALL"""
        m_row = re.search(r'id="tr(\d+)"', row_chunk)
        if not m_row:
            return None
        row_id = m_row.group(1)

        # Lấy row timestamp từ data-id (thường nằm ở cuối data-id)
        m_ts = re.search(r'data-id="b18[^"]*<<\|(\d{9,11})"', row_chunk)
        row_timestamp = m_ts.group(1) if m_ts else ""

        # 1. Mã Booking (cột Booking - input readonly name="name15")
        m_booking = re.search(r'name="name15">([^<]*)</span>', row_chunk) or re.search(r'<td[^>]*><input[^>]*value="([^"]*)"[^>]*readonly', row_chunk)
        booking_id = m_booking.group(1).strip() if m_booking else ""

        # 2. Tàu, Chuyến, Lines
        m_tau = re.search(r'tauPo[^>]*value="([^"]*)"', row_chunk)
        tau = m_tau.group(1).strip() if m_tau else ""

        m_chuyen = re.search(r'chuyenPo[^>]*value="([^"]*)"', row_chunk)
        chuyen = m_chuyen.group(1).strip() if m_chuyen else ""

        m_line = re.search(r'linePoDh[^>]*value="([^"]*)"', row_chunk)
        lines = m_line.group(1).strip() if m_line else ""

        # 3. Ngày đóng & Ngày tàu chạy
        m_ngay_dong = re.search(r'ngayTraHang[^>]*value="([^"]*)"', row_chunk)
        ngay_dong = m_ngay_dong.group(1).strip() if m_ngay_dong else ""

        m_ngay_chay = re.search(r'ngayTauChayPo[^>]*value="([^"]*)"', row_chunk)
        ngay_tau_chay = m_ngay_chay.group(1).strip() if m_ngay_chay else ""

        # 4. Khách hàng, Địa chỉ kho, Tỉnh
        m_khach = re.search(r'tenKhachPo[^>]*value="([^"]*)"', row_chunk)
        khach_hang = m_khach.group(1).strip() if m_khach else ""

        m_kho = re.search(r'diaChiNhanPo[^>]*value="([^"]*)"', row_chunk)
        kho_dong = m_kho.group(1).strip() if m_kho else ""

        m_tinh = re.search(r'tinhNhanPo[^>]*value="([^"]*)"', row_chunk)
        tinh_dong = m_tinh.group(1).strip() if m_tinh else ""

        # 5. CỘT THỨ 14 - POD / TỈNH XUẤT HÀNG (tinhXuatHangPo: CLO, HPH, DNG...)
        m_pod_14 = re.search(r'tinhXuatHangPo[^>]*value="([^"]*)"', row_chunk)
        pod_cot_14 = m_pod_14.group(1).strip() if m_pod_14 else ""

        # 6. Chủ hàng (chuHangPo: VSV...)
        m_chu = re.search(r'chuHangPo[^>]*value="([^"]*)"', row_chunk)
        chu_hang = m_chu.group(1).strip() if m_chu else ""

        # 7. Sản lượng 20 / 40
        m_sl20 = re.search(r'cont20Po[^>]*value="([^"]*)"', row_chunk)
        sl_20 = m_sl20.group(1).strip() if m_sl20 else ""

        m_sl40 = re.search(r'cont40Po[^>]*value="([^"]*)"', row_chunk)
        sl_40 = m_sl40.group(1).strip() if m_sl40 else ""

        # 8. Hàng hóa & Trọng lượng GW
        m_hang = re.search(r'tenHangHoaPo[^>]*value="([^"]*)"', row_chunk)
        ten_hang = m_hang.group(1).strip() if m_hang else ""

        m_gw = re.search(r'trongLuongPo[^>]*value="([^"]*)"', row_chunk)
        gw = m_gw.group(1).strip() if m_gw else ""

        # 9. Số container & Số seal
        m_cont = re.search(r'soContPo[^>]*value="([^"]*)"', row_chunk)
        raw_cont = m_cont.group(1).strip() if m_cont else ""
        clean_cont = re.sub(r"\s+", "", raw_cont).upper()

        m_seal = re.search(r'soSealPo[^>]*value="([^"]*)"', row_chunk)
        so_seal = m_seal.group(1).strip() if m_seal else ""

        # 10. Nơi hạ hàng / Port hạ hàng & Cảng đến POD phụ
        m_cang_di = re.search(r'cangDiPo[^>]*value="([^"]*)"', row_chunk)
        port_ha_hang = m_cang_di.group(1).strip() if m_cang_di else ""

        m_cang_den = re.search(r'cangDenPo[^>]*value="([^"]*)"', row_chunk)
        cang_den_pod = m_cang_den.group(1).strip() if m_cang_den else ""

        # 11. Đơn vị vận chuyển & Số xe
        m_dvvc = re.search(r'donViVcPo[^>]*value="([^"]*)"', row_chunk)
        don_vi_vc = m_dvvc.group(1).strip() if m_dvvc else ""

        m_xe = re.search(r'soXePo[^>]*value="([^"]*)"', row_chunk)
        so_xe = m_xe.group(1).strip() if m_xe else ""

        # 12. Liên hệ kho đóng (ở cột gần cuối)
        m_lh = re.search(r'<td><input[^>]*value="([^"]*)"\s+readonly><span class="inputTitle2">([^<]*)</span></td>\s*<td><input[^>]*noiDungPo', row_chunk)
        lien_he = m_lh.group(1).strip() if m_lh else ""

        # 13. Ghi chú đóng hàng (noiDungPo)
        m_note = re.search(r'noiDungPo[^>]*value="([^"]*)"', row_chunk)
        ghi_chu = m_note.group(1).strip() if m_note else ""

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

    async def fetch_table_for_date(self, date_str: str, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Lấy danh sách các dòng trong tab XUẤT - ALL của 1 ngày (YYYY-MM-DD)"""
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
            if "login2.html" in resp.text or resp.status_code != 200:
                await self.login()
                resp = await client.post(ajax_url, data=payload)

            chunks = resp.text.split('<tr id="tr')
            rows = []
            for chunk in chunks[1:]:
                row_data = self._parse_row(f'<tr id="tr{chunk}', date_queried=date_str)
                if row_data:
                    rows.append(row_data)

            self.cache_date[date_str] = rows
            self.cache_time[date_str] = now
            return rows
        except Exception as e:
            logger.warning(f"Lỗi khi tải bảng XUẤT - ALL cho ngày {date_str}: {e}")
            return []

    async def find_container_or_booking(self, query: str) -> List[Dict[str, Any]]:
        """
        Tìm kiếm cont hoặc booking trong phạm vi [-3 đến +5] ngày tính từ hôm nay.
        Trả về danh sách các bản ghi khớp.
        """
        clean_q = re.sub(r"\s+", "", str(query)).upper()
        if not clean_q:
            return []

        today = datetime.now(VN_TZ)
        # Các ngày quét ưu tiên: Hôm nay, +1, +2, +3, +4, +5, -1, -2
        dates_to_scan = []
        for d_offset in [0, 1, 2, 3, 4, 5, -1, -2]:
            d_str = (today + timedelta(days=d_offset)).strftime("%Y-%m-%d")
            dates_to_scan.append(d_str)

        # Quét song song tất cả các ngày
        tasks = [self.fetch_table_for_date(d) for d in dates_to_scan]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        matched: List[Dict[str, Any]] = []
        for day_rows in results:
            if isinstance(day_rows, list):
                for r in day_rows:
                    cont = r.get("so_cont", "")
                    booking = r.get("booking_id", "")
                    row_id = r.get("row_id", "")

                    if (clean_q in cont) or (clean_q == booking) or (clean_q == row_id):
                        matched.append(r)

        return matched

    async def update_field(self, row_id: str, row_timestamp: str, field_name: str, new_value: str) -> bool:
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
                # Xóa cache ngày để lượt tra cứu sau lấy dữ liệu tươi mới
                self.cache_date.clear()
                return True
            else:
                logger.error(f"Lỗi cập nhật HTTP {resp.status_code}")
                return False
        except Exception as e:
            logger.exception(f"Lỗi khi gửi CRUD cập nhật: {e}")
            return False

    async def create_export_booking_row(self, item: Dict[str, Any]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Quy trình tự động bấm Dấu cộng [+] và tạo dòng đóng hàng xuất:
        1. Tính timestamp của ngày đóng hàng (hoặc ngày hôm nay).
        2. Bấm [+]: POST /index-crud.php (cv: 1, loai: 72, thongtin: timestamp).
        3. Tải lại danh sách dòng ngày đó để tìm dòng mới tạo (ID PVT lớn nhất).
        4. Gán Booking ID: POST /index-crud.php (cv: 14, loai: 7, iddt: row_id, thongtin: booking).
        5. Cập nhật đồng loạt 15+ trường (Tàu, chuyến, khách, kho, POD CỘT 14, GW, cont, ...).
        """
        client = await self.get_client()
        crud_url = f"{config.TMS_URL}/index-crud.php"

        raw_ngay_dong = item.get("ngay_dong", "")
        # Chuyển ngày dạng YYYY-MM-DD
        dt_obj = None
        if raw_ngay_dong:
            m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", raw_ngay_dong)
            if m:
                query_date_str = f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
                formatted_ngay_dong = f"{m.group(1).zfill(2)}/{m.group(2).zfill(2)}/{m.group(3)}"
            else:
                m_iso = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw_ngay_dong)
                if m_iso:
                    query_date_str = raw_ngay_dong
                    formatted_ngay_dong = f"{m_iso.group(3).zfill(2)}/{m_iso.group(2).zfill(2)}/{m_iso.group(1)}"
                else:
                    query_date_str = datetime.now(VN_TZ).strftime("%Y-%m-%d")
                    formatted_ngay_dong = datetime.now(VN_TZ).strftime("%d/%m/%Y")
        else:
            query_date_str = datetime.now(VN_TZ).strftime("%Y-%m-%d")
            formatted_ngay_dong = datetime.now(VN_TZ).strftime("%d/%m/%Y")

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
        row_ts = new_row["row_timestamp"] or str(int(time.time()))
        logger.info(f"🎉 Đã tạo thành công dòng mới! ID PVT = {new_row_id}, timestamp = {row_ts}")

        # BƯỚC 3: GÁN MÃ BOOKING (cv: 14, loai: 7)
        booking_id = str(item.get("booking", "")).strip()
        if booking_id:
            logger.info(f"🔖 Đang gán Booking ID '{booking_id}' cho dòng {new_row_id}...")
            booking_payload = {
                "TenDangNhap": config.TMS_USERNAME,
                "userString": self.user_string,
                "cv": 14,
                "loai": 7,
                "iddt": new_row_id,
                "cot": new_row_id,
                "thongtin": booking_id,
                "minData": "",
                "maxData": "",
            }
            await client.post(crud_url, data=booking_payload)

        # BƯỚC 4: CẬP NHẬT ĐỒNG LOẠT CÁC CỘT CHI TIẾT
        # Chuẩn bị danh sách trường cần cập nhật
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
            ("chu_hang", item.get("phong_ban", "VSV")),
            ("sl_20", str(item.get("sl_20", "1"))),
            ("sl_40", str(item.get("sl_40", "0"))),
            ("ten_hang", item.get("ten_hang", "Gạo")),
            ("gw", str(item.get("gw", ""))),
            ("port_ha_hang", item.get("port_ha_hang", "BẾN NGHÉ")),
            ("cang_den_pod", item.get("pod", "")),
            ("ghi_chu", item.get("ghi_chu", "")),
            ("lien_he", item.get("lien_he", "")),
        ]

        # Nếu có số cont và số seal
        if item.get("so_cont"):
            fields_to_update.append(("so_cont", item.get("so_cont")))
        if item.get("so_seal"):
            fields_to_update.append(("so_seal", item.get("so_seal")))
        if item.get("so_xe"):
            fields_to_update.append(("so_xe", item.get("so_xe")))
        if item.get("don_vi_vc"):
            fields_to_update.append(("don_vi_vc", item.get("don_vi_vc")))

        # Gửi cập nhật song song siêu tốc
        update_tasks = [
            self.update_field(new_row_id, row_ts, f_name, f_val)
            for f_name, f_val in fields_to_update
            if str(f_val).strip()
        ]
        await asyncio.gather(*update_tasks, return_exceptions=True)

        # Lấy lại dòng thông tin đã hoàn thiện
        final_rows = await self.fetch_table_for_date(query_date_str, force_refresh=True)
        final_row = next((r for r in final_rows if r.get("row_id") == new_row_id), new_row)

        return True, f"Tạo thành công ID PVT {new_row_id}", final_row
