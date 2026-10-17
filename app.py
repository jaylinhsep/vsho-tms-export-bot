import re
import time
import asyncio
import logging
from contextlib import asynccontextmanager
from io import BytesIO
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Set, Tuple

import uvicorn
from fastapi import FastAPI
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import config
from extractor_export import extract_with_gemini, extract_pod_assignments
from tms_export_api import TMSExportApiClient, remove_accents
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

tms_api = TMSExportApiClient()
tg_app: Optional[Application] = None


def is_authorized(update: Update) -> bool:
    """Kiểm tra quyền sử dụng bot"""
    if not config.ALLOWED_CHAT_IDS:
        return True
    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None
    return (chat_id in config.ALLOWED_CHAT_IDS) or (user_id in config.ALLOWED_CHAT_IDS)


def build_container_card(r: Dict[str, Any]) -> str:
    """Tạo giao diện thẻ tóm tắt thông tin dòng container xuất trực quan"""
    row_id = r.get("row_id", "")
    booking = r.get("booking_id", "") or "Chưa có"
    tau = r.get("tau", "") or "Chưa rõ"
    chuyen = r.get("chuyen", "") or ""
    ngay_dong = r.get("ngay_dong", "") or "Chưa có"
    ngay_chay = r.get("ngay_tau_chay", "") or "Chưa có"
    khach = r.get("khach_hang", "") or "Chưa có"
    kho = r.get("kho_dong", "") or "Chưa có"
    tinh = r.get("tinh_dong", "") or ""
    pod_14 = r.get("pod_cot_14", "") or "*(Chưa gán)*"
    sl20 = str(r.get("sl_20", "")).strip()
    sl40 = str(r.get("sl_40", "")).strip()
    sl_parts = []
    if sl20 and sl20 != "0":
        sl_parts.append(f"{sl20}x20ft")
    if sl40 and sl40 != "0":
        sl_parts.append(f"{sl40}x40ft")
    sl_str = " + ".join(sl_parts) if sl_parts else "Chưa rõ"
    gw = r.get("gw", "")
    cont = r.get("so_cont", "") or "*(Chưa gán)*"
    seal = r.get("so_seal", "") or "*(Chưa có)*"
    dvvc = r.get("don_vi_vc", "") or ""
    so_xe = r.get("so_xe", "") or "*(Chưa gán)*"
    port_ha = r.get("port_ha_hang", "") or "BẾN NGHÉ"
    lien_he = r.get("lien_he", "") or ""
    ghi_chu = r.get("ghi_chu", "") or "*(Trống)*"

    tinh_suffix = f"({tinh})" if tinh else ""
    gw_suffix = f"| **GW:** {gw}T" if gw else ""
    lien_he_line = f"• **Liên hệ kho:** {lien_he}\n" if lien_he else ""

    card = (
        f"📦 **CHI TIẾT CONTAINER XUẤT**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• **ID PVT:** `{row_id}` | **Booking:** `{booking}`\n"
        f"• **Tàu / Chuyến:** {tau} ({chuyen})\n"
        f"• **Ngày đóng:** `{ngay_dong}` | **Ngày tàu chạy:** `{ngay_chay}`\n"
        f"• **Khách hàng:** {khach}\n"
        f"• **Hàng hóa:** {r.get('ten_hang') or 'Chưa rõ'}\n"
        f"• **Kho đóng:** {kho} {tinh_suffix}\n"
        f"• **Sản lượng:** {sl_str} {gw_suffix}\n"
        f"• **Số Cont:** `{cont}`\n"
        f"• **Số Seal:** `{seal}`\n"
        f"• **Đơn vị VC:** `{dvvc or '*(Chưa gán)*'}` | **Số Xe:** `{so_xe}`\n"
        f"• ⚓ **Cảng đến (POD - Cột 14):** `{pod_14}`\n"
        f"• **Port hạ hàng:** {port_ha}\n"
        f"{lien_he_line}"
        f"• **Ghi chú đóng:** {ghi_chu}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👇 *Bấm nút bên dưới để chỉnh sửa và submit ngược lên TMS:*"
    )
    return card


def build_container_keyboard(row_id: str, row_timestamp: str) -> InlineKeyboardMarkup:
    """Tạo hàng nút bấm tương tác cho từng container"""
    keyboard = [
        [
            InlineKeyboardButton("⚓ Đổi POD (Cột 14)", callback_data=f"edit_pod_{row_id}_{row_timestamp}"),
            InlineKeyboardButton("✏️ Gán/Đổi Cont", callback_data=f"edit_cont_{row_id}_{row_timestamp}"),
        ],
        [
            InlineKeyboardButton("🚚 Gán Số Xe", callback_data=f"edit_truck_{row_id}_{row_timestamp}"),
            InlineKeyboardButton("🔒 Gán Số Seal", callback_data=f"edit_seal_{row_id}_{row_timestamp}"),
        ],
        [
            InlineKeyboardButton("🏢 Gán Đơn Vị VC", callback_data=f"edit_carrier_{row_id}_{row_timestamp}"),
            InlineKeyboardButton("📝 Sửa Ghi Chú", callback_data=f"edit_note_{row_id}_{row_timestamp}"),
        ],
        [
            InlineKeyboardButton("🔄 Làm Mới", callback_data=f"refresh_{row_id}_{row_timestamp}"),
            InlineKeyboardButton("🗑️ Xóa Booking", callback_data=f"delask_{row_id}_{row_timestamp}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = (
        "🚢 **Bot Quản Lý Hàng Xuất (VSHO TMS Vietsun)**\n\n"
        "⚡ **Tính năng chính:**\n"
        "1️⃣ **Nhập kế hoạch tự động:** Chỉ cần copy nội dung email kế hoạch đóng hàng (hoặc gửi ảnh email) dán vào đây ➔ Bot tự động bấm `[+]` và điền toàn bộ lên TMS.\n\n"
        "2️⃣ **Xem kế hoạch theo ngày (Hôm nay, +1, +2, +3):**\n"
        "   • Gõ lệnh `/ngay` — Hiện bảng tổng quan 4 ngày kế hoạch kèm 4 nút bấm chọn ngày.\n"
        "   • Hoặc gõ chữ trực tiếp: `hôm nay`, `mai`, `mốt`, `kia`, hoặc ngày `17/10` ➔ Bot hiện ngay danh sách cont ngày đó!\n"
        "   • Có thể tìm theo ngày + khách: `mai Tiền`, `17/10 Jotun`...\n\n"
        "3️⃣ **Tra cứu cont xuất đơn lẻ:** Gõ trực tiếp số cont (VD: `GAOU2111920`) hoặc mã booking (VD: `300170`), hoặc gõ lệnh `/tim <từ khóa>`.\n\n"
        "4️⃣ **Tra cứu hàng loạt theo Khách hàng:** Gõ `KH: <từ khóa>` (VD: `KH: Tiền`, `KH: Phong`) hoặc `/kh <từ khóa>`.\n\n"
        "5️⃣ **Chỉnh sửa & Submit ngược lại TMS:** Có sẵn nút bấm trực tiếp dưới mỗi container:\n"
        "   • ⚓ **Đổi POD (Gán vào Cột 14 TMS)**\n"
        "   • ✏️ **Gán / Đổi số cont**\n"
        "   • 🚚 **Gán biển số xe** | 🔒 **Gán số seal**\n"
        "   • 🏢 **Gán đơn vị VC** | 📝 **Sửa ghi chú** | 🗑️ **Xóa booking**\n\n"
        "6️⃣ **Đổi POD (Cột 14) hàng loạt:**\n"
        "   • Gửi ảnh bảng điều độ kèm caption là `POD`\n"
        "   • Hoặc dán danh sách text (dòng 1 ghi `POD`, các dòng dưới ghi `DRYU2901546 DAN`)\n"
        "   ➔ Bot tự động nhận diện và cập nhật Cột 14 hàng loạt lên TMS!\n\n"
        "📌 **Lệnh hữu ích:**\n"
        "• `/ngay` — Bảng tổng quan kế hoạch 4 ngày (Hôm nay, Ngày mai, Ngày mốt, Ngày kia)\n"
        "• `/pod` — Hướng dẫn đổi POD hàng loạt\n"
        "• `/models` — Kiểm tra kết nối AI Google Gemini\n"
        "• `/status` — Kiểm tra kết nối TMS & số lượng cont 4 ngày tới\n"
        "• `/tim <từ khóa>` — Tra cứu cont xuất theo cont hoặc booking\n"
        "• `/kh <tên khách>` — Tra cứu tất cả các cont của khách hàng (VD: `/kh Tiền`)\n"
        "• `/xoa <id>` — Xóa booking khỏi TMS theo ID PVT (VD: `/xoa 208371`)\n"
        f"• Chat ID của bạn: `{chat_id}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("⛔ Bạn không có quyền sử dụng bot này.")
        return

    wait_msg = await update.message.reply_text("🔄 Đang kiểm tra kết nối tới VSHO TMS tab XUẤT - ALL...")
    try:
        t0 = time.time()
        ok = await tms_api.login()
        if not ok:
            dbg_login = getattr(tms_api, "last_login_debug", {})
            err_detail = dbg_login.get("err", "Không rõ nguyên nhân")
            await wait_msg.edit_text(f"❌ Đăng nhập TMS thất bại: `{err_detail}`\nVui lòng kiểm tra tài khoản/mật khẩu TMS.")
            return

        summary_days = await tms_api.get_plan_summary_4_days()
        elapsed = round(time.time() - t0, 2)

        gemini_info = (
            f"Đã nhận key ({config.GEMINI_API_KEY[:6]}...)"
            if (config.GEMINI_API_KEY and config.GEMINI_API_KEY != "key_gemini_cua_ban")
            else "Chưa có key hoặc đang dùng key mẫu ❌ (Gõ /models để xem)"
        )

        sum_lines = []
        buttons = []
        row_btn = []
        for d in summary_days:
            sum_lines.append(f"• 📅 **{d['label']}** (`{d['date_dmy']}`): **{d['count']}** cont")
            short_lbl = d['label'].split(' (')[0]
            btn_txt = f"{short_lbl} ({d['date_dm']}): {d['count']}c"
            row_btn.append(InlineKeyboardButton(btn_txt, callback_data=f"viewday_{d['offset']}_{d['date_iso']}"))
            if len(row_btn) == 2:
                buttons.append(row_btn)
                row_btn = []
        if row_btn:
            buttons.append(row_btn)

        sum_text = "\n".join(sum_lines)
        kb = InlineKeyboardMarkup(buttons)

        await wait_msg.edit_text(
            f"✅ **Hệ thống VSHO TMS Hoạt Động Bình Thường!**\n\n"
            f"👤 Tài khoản: `{config.TMS_USERNAME}`\n"
            f"📊 Tab đang kết nối: **XUẤT - ALL** (kieuXemTrucking=2)\n\n"
            f"📦 **Kế hoạch đóng hàng 4 ngày tới:**\n"
            f"{sum_text}\n\n"
            f"⚡ Tốc độ phản hồi: `{elapsed}s`\n"
            f"🔑 Gemini AI: `{gemini_info}`\n\n"
            f"👇 *Bấm nút bên dưới để xem chi tiết danh sách từng ngày:*",
            reply_markup=kb,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Lỗi status")
        await wait_msg.edit_text(f"❌ Lỗi kiểm tra kết nối: {e}")


async def models_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kiểm tra kết nối và danh sách models từ Google Gemini API"""
    if not is_authorized(update):
        return
    if not config.GEMINI_API_KEY or config.GEMINI_API_KEY == "key_gemini_cua_ban":
        await update.message.reply_text(
            "❌ **Chưa cấu hình GEMINI_API_KEY hợp lệ!**\n\n"
            "Vui lòng mở file `.env` trên VPS và thêm API Key từ Google AI Studio:\n"
            "`GEMINI_API_KEY=AIzaSy...`",
            parse_mode="Markdown"
        )
        return

    msg = await update.message.reply_text("🔍 Đang kết nối tới Google Gemini API để kiểm tra...")
    try:
        from google import genai
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        models = []
        for m in client.models.list():
            raw = getattr(m, "name", "").replace("models/", "")
            models.append(raw)

        flash_models = [m for m in models if "flash" in m]
        await msg.edit_text(
            f"✅ **Kết nối Google Gemini AI Thành Công!**\n\n"
            f"🔑 Key: `{config.GEMINI_API_KEY[:6]}...{config.GEMINI_API_KEY[-4:]}`\n"
            f"⚡ Số lượng models: {len(models)}\n"
            f"🌟 Các model Flash khả dụng:\n• " + "\n• ".join(flash_models[:6]),
            parse_mode="Markdown"
        )
    except Exception as e:
        await msg.edit_text(f"❌ Kết nối Gemini thất bại:\n`{e}`\n\nVui lòng kiểm tra lại GEMINI_API_KEY trong file .env.")


async def tim_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("👉 Vui lòng nhập số cont hoặc mã booking. Ví dụ: `/tim 300170` hoặc `/tim GAOU2111920`", parse_mode="Markdown")
        return

    query = " ".join(context.args).strip()
    await handle_query_search(update, query)


async def kh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh tra cứu hàng loạt container theo Tên Khách Hàng: /kh <tên khách>"""
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text(
            "👉 Vui lòng nhập tên khách hàng cần tìm.\n"
            "Ví dụ:\n"
            "• `/kh Tiền`\n"
            "• `/kh Phong`\n"
            "• `KH: Tiền Phong`\n"
            "• `KH: \"Cty Tiền Phong MN\"`",
            parse_mode="Markdown"
        )
        return

    query = " ".join(context.args).strip().strip('"').strip("'").strip()
    await handle_customer_search(update, query)


async def xoa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh xóa booking trực tiếp theo ID PVT: /xoa <row_id>"""
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("👉 Vui lòng nhập ID PVT cần xóa. Ví dụ: `/xoa 208371`", parse_mode="Markdown")
        return

    row_id = context.args[0].strip()
    confirm_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑️ XÁC NHẬN XÓA", callback_data=f"delconfirm_{row_id}_"),
            InlineKeyboardButton("❌ Hủy Bỏ", callback_data=f"delcancel_{row_id}_"),
        ]
    ])
    await update.message.reply_text(
        f"⚠️ **CẢNH BÁO XÓA BOOKING KHỎI TMS**\n\n"
        f"Bạn có chắc chắn muốn **xóa vĩnh viễn dòng ID PVT `{row_id}`** trên tab XUẤT - ALL không?\n\n"
        f"🚨 *Lưu ý: Dòng này sẽ bị xóa trực tiếp khỏi hệ thống TMS Vietsun và không thể khôi phục!*",
        reply_markup=confirm_kb,
        parse_mode="Markdown",
    )


async def pod_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /pod: Đổi POD (Cột 14) hàng loạt từ văn bản hoặc xem hướng dẫn"""
    if not is_authorized(update):
        return
    args_text = " ".join(context.args).strip() if context.args else ""
    if not args_text:
        msg = (
            "⚓ **HƯỚNG DẪN ĐỔI CẢNG ĐẾN (POD - CỘT 14) HÀNG LOẠT:**\n\n"
            "📸 **Cách 1: Gửi ảnh bảng điều độ**\n"
            "• Gửi ảnh chụp bảng điều độ (Excel/Zalo) vào bot.\n"
            "• Kèm caption là: `POD`\n"
            "➔ Bot tự động dùng Gemini AI nhận diện Số cont và mã POD (CLO, DAN, HPG...) rồi cập nhật Cột 14 trên TMS!\n\n"
            "📝 **Cách 2: Gõ hoặc dán danh sách text**\n"
            "Gõ theo mẫu:\n"
            "```\n"
            "POD\n"
            "DRYU2901546 DAN\n"
            "DRYU3095229 CLO\n"
            "DRYU3095450 CLO\n"
            "```\n"
            "hoặc: `/pod DRYU2901546 DAN, DRYU3095229 CLO`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return
    await handle_batch_pod_update(update, text_content=args_text)


def parse_date_query(text: str):
    """
    Phân tích chuỗi tìm kiếm để nhận diện ngày kế hoạch:
    Trả về: (date_iso, date_dmy, label) hoặc None
    """
    clean = text.strip().lower()
    today = datetime.now(tms_api.VN_TZ)

    # 1. Các từ khóa tương đối
    if clean in ("hom nay", "hôm nay", "nay", "today", "day0", "0", "+0", "d0"):
        d = today
        return d.strftime("%Y-%m-%d"), d.strftime("%d/%m/%Y"), "Hôm nay"

    if clean in ("mai", "ngay mai", "ngày mai", "tomorrow", "day1", "day+1", "1", "+1", "d1"):
        d = today + timedelta(days=1)
        return d.strftime("%Y-%m-%d"), d.strftime("%d/%m/%Y"), "Ngày mai (+1)"

    if clean in ("mot", "mốt", "ngay mot", "ngày mốt", "day2", "day+2", "2", "+2", "d2"):
        d = today + timedelta(days=2)
        return d.strftime("%Y-%m-%d"), d.strftime("%d/%m/%Y"), "Ngày mốt (+2)"

    if clean in ("kia", "ngay kia", "ngày kia", "day3", "day+3", "3", "+3", "d3"):
        d = today + timedelta(days=3)
        return d.strftime("%Y-%m-%d"), d.strftime("%d/%m/%Y"), "Ngày kia (+3)"

    # 2. Định dạng ngày cụ thể DD/MM hoặc DD/MM/YYYY
    m = re.match(r"^(\d{1,2})[-/](\d{1,2})(?:[-/](\d{4}))?$", clean)
    if m:
        day_n = int(m.group(1))
        mon_n = int(m.group(2))
        yr_n = int(m.group(3)) if m.group(3) else today.year
        try:
            target_dt = datetime(yr_n, mon_n, day_n, tzinfo=tms_api.VN_TZ)
            diff_days = (target_dt.date() - today.date()).days
            if diff_days == 0:
                lbl = "Hôm nay"
            elif diff_days == 1:
                lbl = "Ngày mai (+1)"
            elif diff_days == 2:
                lbl = "Ngày mốt (+2)"
            elif diff_days == 3:
                lbl = "Ngày kia (+3)"
            else:
                lbl = f"Ngày {day_n:02d}/{mon_n:02d}/{yr_n}"
            return target_dt.strftime("%Y-%m-%d"), target_dt.strftime("%d/%m/%Y"), lbl
        except Exception:
            return None

    return None


def extract_date_and_query(text: str):
    """Tách phần ngày và phần từ khóa phụ nếu có (VD: 'mai Tiền' -> (ngay_mai, 'Tiền'))"""
    parts = text.strip().split()
    if len(parts) >= 2:
        if len(parts) >= 3:
            dt_res = parse_date_query(" ".join(parts[:2]))
            if dt_res:
                extra = " ".join(parts[2:]).strip()
                return dt_res, extra
        dt_res = parse_date_query(parts[0])
        if dt_res:
            extra = " ".join(parts[1:]).strip()
            return dt_res, extra
        dt_res = parse_date_query(parts[-1])
        if dt_res:
            extra = " ".join(parts[:-1]).strip()
            return dt_res, extra

    dt_res = parse_date_query(text)
    if dt_res:
        return dt_res, ""

    return None, text


async def handle_plan_overview(update: Update):
    """Hiển thị bảng tổng quan kế hoạch 4 ngày kèm 4 nút bấm chọn ngày"""
    wait_msg = await update.message.reply_text("🔄 Đang lấy dữ liệu kế hoạch 4 ngày từ TMS Vietsun...")
    try:
        t0 = time.time()
        summary_days = await tms_api.get_plan_summary_4_days()
        elapsed = round(time.time() - t0, 2)

        sum_lines = []
        buttons = []
        row_btn = []
        for d in summary_days:
            sum_lines.append(f"• 📅 **{d['label']}** (`{d['date_dmy']}`): **{d['count']}** cont")
            short_lbl = d['label'].split(' (')[0]
            btn_txt = f"{short_lbl} ({d['date_dm']}): {d['count']}c"
            row_btn.append(InlineKeyboardButton(btn_txt, callback_data=f"viewday_{d['offset']}_{d['date_iso']}"))
            if len(row_btn) == 2:
                buttons.append(row_btn)
                row_btn = []
        if row_btn:
            buttons.append(row_btn)

        sum_text = "\n".join(sum_lines)
        kb = InlineKeyboardMarkup(buttons)

        await wait_msg.edit_text(
            f"📅 **KẾ HOẠCH ĐÓNG HÀNG 4 NGÀY TỚI (XUẤT - ALL):**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{sum_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ Tốc độ quét: `{elapsed}s`\n\n"
            f"👇 *Bấm nút bên dưới để xem danh sách & chỉnh sửa từng container:*",
            reply_markup=kb,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Lỗi xem kế hoạch 4 ngày")
        await wait_msg.edit_text(f"❌ Lỗi khi lấy kế hoạch: {e}")


async def handle_date_plan_display(
    target,
    date_iso: str,
    date_dmy: str,
    label: str,
    filter_keyword: str = "",
    wait_msg=None,
    is_callback: bool = False
):
    """Hiển thị danh sách container cho 1 ngày cụ thể kèm thẻ chi tiết và nút bấm chỉnh sửa"""
    send_msg = wait_msg
    prefix = f" cho từ khóa '{filter_keyword}'" if filter_keyword else ""
    if not send_msg:
        if is_callback:
            await target.answer()
            send_msg = await target.message.reply_text(f"⏳ Đang tải kế hoạch {label} ({date_dmy}){prefix} từ TMS...")
        else:
            send_msg = await target.message.reply_text(f"⏳ Đang tải kế hoạch {label} ({date_dmy}){prefix} từ TMS...")

    try:
        raw_rows = await tms_api.fetch_rows_for_date_clean(date_iso, force_refresh=False)

        # Lọc theo từ khóa phụ nếu có (VD: 'mai Tiền' -> chỉ lấy cont khách Tiền)
        if filter_keyword:
            clean_fk = remove_accents(filter_keyword).strip()
            matched_rows = []
            for r in raw_rows:
                is_m, _ = tms_api.row_matches_query(r, clean_fk, filter_keyword)
                if is_m:
                    matched_rows.append(r)
            results = matched_rows
        else:
            results = raw_rows

        if not results:
            msg = f"ℹ️ Không có container nào trong kế hoạch đóng hàng **{label} ({date_dmy})**"
            if filter_keyword:
                msg += f" khớp với từ khóa **'{filter_keyword}'**."
            else:
                msg += " trên TMS."
            await send_msg.edit_text(msg, parse_mode="Markdown")
            return

        total = len(results)
        summary_lines = []
        for i, r in enumerate(results, start=1):
            cont = r.get("so_cont") or "Chưa có cont"
            booking = r.get("booking_id") or "N/A"
            tau = r.get("tau") or "Chưa rõ tàu"
            chuyen = r.get("chuyen") or ""
            tau_str = f"{tau} ({chuyen})" if chuyen else tau
            pod = r.get("pod_cot_14") or "Chưa gán POD"
            khach = r.get("khach_hang") or "Chưa có tên"
            ten_hang = r.get("ten_hang") or ""
            xe = r.get("so_xe") or "Chưa gán xe"
            hang_str = f" | 🌾 _{ten_hang}_" if ten_hang else ""
            line = (
                f"{i}. 📦 Cont: `{cont}` | 🔖 `{booking}` | ⚓ POD: `{pod}`\n"
                f"   🚢 {tau_str} | 🚚 `{xe}`\n"
                f"   🏢 _{khach}_{hang_str}"
            )
            summary_lines.append(line)

        summary_text = "\n".join(summary_lines[:15])
        if total > 15:
            summary_text += f"\n... và còn {total - 15} container khác."

        filter_suffix = f" (LỌC THEO: '{filter_keyword.upper()}')" if filter_keyword else ""
        header = (
            f"📅 **KẾ HOẠCH {label.upper()} ({date_dmy}){filter_suffix}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Tổng cộng: **{total}** container\n\n"
            f"{summary_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👇 *Dưới đây là chi tiết từng container. Bấm nút để chỉnh sửa và submit ngược lên TMS:*"
        )
        await send_msg.edit_text(header, parse_mode="Markdown")

        # Gửi tối đa 8 container card
        max_cards = min(8, total)
        for r in results[:max_cards]:
            card_text = build_container_card(r)
            kb = build_container_keyboard(r.get("row_id", ""), r.get("row_timestamp", ""))
            if is_callback:
                await target.message.reply_text(card_text, reply_markup=kb, parse_mode="Markdown")
            else:
                await target.message.reply_text(card_text, reply_markup=kb, parse_mode="Markdown")
            await asyncio.sleep(0.15)

        if total > max_cards:
            note_msg = f"ℹ️ *Đã hiển thị {max_cards}/{total} container.* Bạn có thể gõ trực tiếp số cont hoặc mã booking để chỉnh sửa các cont còn lại."
            if is_callback:
                await target.message.reply_text(note_msg, parse_mode="Markdown")
            else:
                await target.message.reply_text(note_msg, parse_mode="Markdown")

    except Exception as e:
        logger.exception(f"Lỗi hiển thị kế hoạch ngày {date_iso}: {e}")
        await send_msg.edit_text(f"❌ Lỗi khi tải dữ liệu ngày {date_dmy}: {e}")


async def ngay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /ngay hoặc /plan hoặc /kehoach: Xem kế hoạch đóng hàng theo ngày"""
    if not is_authorized(update):
        return

    args_text = " ".join(context.args).strip() if context.args else ""
    if not args_text:
        await handle_plan_overview(update)
        return

    dt_res, extra = extract_date_and_query(args_text)
    if dt_res:
        date_iso, date_dmy, label = dt_res
        await handle_date_plan_display(
            update,
            date_iso=date_iso,
            date_dmy=date_dmy,
            label=label,
            filter_keyword=extra,
            is_callback=False
        )
    else:
        await update.message.reply_text(
            "👉 Vui lòng nhập ngày cần xem hợp lệ. Ví dụ:\n"
            "• `/ngay` — Xem tổng quan 4 ngày kèm nút chọn\n"
            "• `/ngay mai` hoặc `/mai` — Xem ngày mai (+1)\n"
            "• `/ngay mot` hoặc `/mot` — Xem ngày mốt (+2)\n"
            "• `/ngay kia` hoặc `/kia` — Xem ngày kia (+3)\n"
            "• `/ngay 17/10` — Xem ngày cụ thể\n"
            "• `/ngay mai Tiền` — Xem cont khách Tiền ngày mai",
            parse_mode="Markdown"
        )


def is_valid_packing_date(r: Dict[str, Any]) -> bool:
    """Chỉ chấp nhận các dòng có ngày đóng hàng là Hôm nay, Ngày mai, Ngày mốt, Ngày kia (Tổng 4 ngày)"""
    today = datetime.now(tms_api.VN_TZ)
    valid_dates = {(today + timedelta(days=off)).strftime("%d/%m/%Y") for off in [0, 1, 2, 3]}
    valid_iso = {(today + timedelta(days=off)).strftime("%Y-%m-%d") for off in [0, 1, 2, 3]}

    raw_ngay = str(r.get("ngay_dong") or "").strip()
    if raw_ngay:
        m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", raw_ngay)
        if m:
            std_d = f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{int(m.group(3))}"
            return std_d in valid_dates
    date_q = str(r.get("date_queried") or "").strip()
    if date_q:
        return date_q in valid_iso
    return True


async def handle_query_search(update: Update, query: str):
    """Tìm kiếm thông minh đa năng: số cont, mã booking, tên khách hàng, tên hàng hóa (Gạo, Bia...), số xe, POD..."""
    wait_msg = await update.message.reply_text(f"🔍 Đang tra cứu `{query}` trên tab XUẤT - ALL của TMS...")
    try:
        if not tms_api.is_logged_in:
            await tms_api.login()

        raw_results = await tms_api.search_containers(query)
        results = [r for r in raw_results if is_valid_packing_date(r)]
        if not results:
            await wait_msg.edit_text(
                f"❌ Không tìm thấy container/booking nào khớp với **'{query}'** trong phạm vi kế hoạch (Hôm nay, Ngày mai, Ngày mốt, Ngày kia).\n\n"
                f"💡 **Gợi ý tra cứu linh hoạt:**\n"
                f"• **Số Cont / Mã Booking:** VD `GAOU2111920`, `300170`\n"
                f"• **Tên Khách Hàng:** VD `Liwayway`, `liwayway`, `Tiền`, `Phong`\n"
                f"• **Hàng Hóa / Cont Gạo:** VD `Gạo`, `gao`, `GAO`, `Bia`, `Sữa`\n"
                f"• **Số Xe / Cảng POD:** VD `50H`, `61H`, `CLO`, `HPH`\n"
                f"• Hoặc gõ `/status` để kiểm tra kết nối TMS.",
                parse_mode="Markdown"
            )
            return

        # Nếu chỉ có duy nhất 1 container khớp chính xác
        if len(results) == 1:
            await wait_msg.delete()
            r = results[0]
            card_text = build_container_card(r)
            kb = build_container_keyboard(r.get("row_id", ""), r.get("row_timestamp", ""))
            await update.message.reply_text(card_text, reply_markup=kb, parse_mode="Markdown")
            return

        # Nếu có từ 2 container trở lên -> Hiển thị danh sách tổng quan + các thẻ chi tiết
        await handle_customer_search(update, query, wait_msg=wait_msg, preloaded_results=results)

    except Exception as e:
        logger.exception(f"Lỗi khi tra cứu: {e}")
        await wait_msg.edit_text(f"❌ Đã xảy ra lỗi khi tra cứu: {e}")


async def handle_customer_search(
    update: Update,
    query_kh: str,
    wait_msg=None,
    preloaded_results: Optional[list] = None
):
    """Tìm kiếm và hiển thị danh sách container theo từ khóa (Khách hàng, Hàng hóa, Cảng, Cont...)"""
    clean_q = query_kh.strip().strip('"').strip("'").strip()
    if len(clean_q) < 2:
        if wait_msg:
            await wait_msg.edit_text("⚠️ Từ khóa tìm kiếm cần ít nhất 2 ký tự (Ví dụ: `Gạo`, `Liwayway`, `Tiền`, `Phong`).", parse_mode="Markdown")
        else:
            await update.message.reply_text(
                "⚠️ Từ khóa tìm kiếm cần ít nhất 2 ký tự.\n"
                "👉 Ví dụ: `KH: Tiền`, `Gạo`, `liwayway` hoặc `GAOU`",
                parse_mode="Markdown"
            )
        return

    if not wait_msg:
        wait_msg = await update.message.reply_text(
            f"🔍 Đang tìm kiếm các container khớp với **'{clean_q}'** trên tab XUẤT - ALL của TMS..."
        )
    try:
        if not tms_api.is_logged_in:
            await tms_api.login()

        if preloaded_results is not None:
            raw_results = preloaded_results
        else:
            raw_results = await tms_api.search_containers(clean_q)

        results = [r for r in raw_results if is_valid_packing_date(r)]

        if not results:
            await wait_msg.edit_text(
                f"❌ Không tìm thấy container nào khớp với **'{clean_q}'** trong phạm vi kế hoạch (Hôm nay, Ngày mai, Ngày mốt, Ngày kia).\n\n"
                f"💡 **Gợi ý:**\n"
                f"• Thử từ khóa ngắn hơn: `liwayway`, `Gạo`, `gao`, `Tiền`, `Phong`.\n"
                f"• Kiểm tra gõ `/status` để xem TMS có đang kết nối bình thường không.\n"
                f"• Hoặc tra cứu trực tiếp theo Số Cont hoặc Mã Booking.",
                parse_mode="Markdown"
            )
            return

        total = len(results)
        summary_lines = []
        for i, r in enumerate(results, start=1):
            cont = r.get("so_cont") or "Chưa có cont"
            booking = r.get("booking_id") or "N/A"
            tau = r.get("tau") or "Chưa rõ tàu"
            chuyen = r.get("chuyen") or ""
            tau_str = f"{tau} ({chuyen})" if chuyen else tau
            pod = r.get("pod_cot_14") or "Chưa gán POD"
            khach = r.get("khach_hang") or "Chưa có tên"
            ten_hang = r.get("ten_hang") or ""
            ngay = r.get("ngay_dong") or r.get("date_queried") or ""
            xe = r.get("so_xe") or "Chưa gán xe"

            hang_str = f" | 🌾 _{ten_hang}_" if ten_hang else ""
            line = (
                f"{i}. 📦 Cont: `{cont}` | 🔖 `{booking}` | ⚓ POD: `{pod}`\n"
                f"   📅 `{ngay}` | 🚢 {tau_str} | 🚚 `{xe}`\n"
                f"   🏢 _{khach}_{hang_str}"
            )
            summary_lines.append(line)

        # Hiển thị tối đa 12 dòng tóm tắt trong tin nhắn tổng quan
        summary_text = "\n".join(summary_lines[:12])
        if total > 12:
            summary_text += f"\n... và còn {total - 12} container khác."

        await wait_msg.edit_text(
            f"🔍 **TÌM THẤY {total} CONTAINER KHỚP VỚI: '{clean_q.upper()}'**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{summary_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👇 *Dưới đây là chi tiết từng container. Bấm nút để chỉnh sửa và submit ngược lên TMS:*",
            parse_mode="Markdown"
        )

        # Gửi thẻ chi tiết kèm hàng nút bấm tương tác (hiển thị tối đa 8 container)
        max_cards = min(8, total)
        for r in results[:max_cards]:
            card_text = build_container_card(r)
            kb = build_container_keyboard(r.get("row_id", ""), r.get("row_timestamp", ""))
            await update.message.reply_text(card_text, reply_markup=kb, parse_mode="Markdown")
            await asyncio.sleep(0.15)  # Tránh spam rate limit của Telegram

        if total > max_cards:
            await update.message.reply_text(
                f"ℹ️ *Đã hiển thị {max_cards}/{total} thẻ chi tiết gần nhất.* Để chỉnh sửa các cont còn lại, bạn có thể gõ trực tiếp số cont hoặc mã booking đó.",
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.exception(f"Lỗi tìm kiếm: {e}")
        await wait_msg.edit_text(f"❌ Đã xảy ra lỗi khi tìm kiếm: {e}")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý sự kiện khi bấm các nút Inline Keyboard"""
    query = update.callback_query
    await query.answer()

    data = query.data
    parts = data.split("_")
    action = parts[0]

    if action == "viewday":
        off = int(parts[1])
        date_iso = parts[2]
        today = datetime.now(tms_api.VN_TZ)
        d_obj = today + timedelta(days=off)
        labels = ["Hôm nay", "Ngày mai (+1)", "Ngày mốt (+2)", "Ngày kia (+3)"]
        lbl = labels[off] if off < len(labels) else f"Ngày {d_obj.strftime('%d/%m')}"
        await handle_date_plan_display(
            query,
            date_iso=date_iso,
            date_dmy=d_obj.strftime("%d/%m/%Y"),
            label=lbl,
            is_callback=True
        )
        return

    if action == "refresh":
        row_id = parts[1]
        await query.edit_message_text("🔄 Đang làm mới dữ liệu từ TMS Vietsun...")
        results = await tms_api.find_container_or_booking(row_id)
        if results:
            r = results[0]
            card_text = build_container_card(r)
            kb = build_container_keyboard(r.get("row_id", ""), r.get("row_timestamp", ""))
            await query.edit_message_text(card_text, reply_markup=kb, parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ Không tìm thấy thông tin dòng {row_id} sau khi làm mới.")
        return

    if action == "delask":
        row_id = parts[1]
        row_ts = parts[2] if len(parts) > 2 else ""

        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🗑️ XÁC NHẬN XÓA", callback_data=f"delconfirm_{row_id}_{row_ts}"),
                InlineKeyboardButton("❌ Hủy Bỏ", callback_data=f"delcancel_{row_id}_{row_ts}"),
            ]
        ])
        await query.message.reply_text(
            f"⚠️ **CẢNH BÁO XÓA BOOKING KHỎI TMS**\n\n"
            f"Bạn có chắc chắn muốn **xóa vĩnh viễn dòng ID PVT `{row_id}`** trên tab XUẤT - ALL không?\n\n"
            f"🚨 *Lưu ý: Dòng này sẽ bị xóa trực tiếp khỏi hệ thống TMS Vietsun và không thể khôi phục!*",
            reply_markup=confirm_kb,
            parse_mode="Markdown",
        )
        return

    if action == "delconfirm":
        row_id = parts[1]
        row_ts = parts[2] if len(parts) > 2 else ""
        await query.edit_message_text(f"⏳ Đang thực hiện xóa dòng ID PVT `{row_id}` trên TMS Vietsun...")

        if not tms_api.is_logged_in:
            await tms_api.login()

        ok = await tms_api.delete_export_booking_row(row_id)
        if ok:
            await query.edit_message_text(
                f"✅ **ĐÃ XÓA THÀNH CÔNG KHỎI TMS!**\n\n"
                f"• ID PVT: `{row_id}`\n"
                f"• Dòng booking đã được xóa hoàn toàn khỏi tab XUẤT - ALL của TMS Vietsun.",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"❌ Xóa dòng `{row_id}` thất bại. Vui lòng kiểm tra lại kết nối TMS hoặc đăng nhập lại.",
                parse_mode="Markdown",
            )
        return

    if action == "delcancel":
        row_id = parts[1]
        await query.edit_message_text(f"❌ Đã hủy thao tác xóa dòng ID PVT `{row_id}`.")
        return

    if action == "edit":
        field_type = parts[1]  # pod, cont, truck, seal, note, carrier
        row_id = parts[2]
        row_ts = parts[3] if len(parts) > 3 else ""

        # Lưu trạng thái chờ người dùng nhập
        context.user_data["pending_edit"] = {
            "row_id": row_id,
            "row_timestamp": row_ts,
            "field_type": field_type,
            "original_message_id": query.message.message_id,
            "chat_id": query.message.chat_id,
        }

        field_names = {
            "pod": "⚓ **Cảng đến (POD - Cột 14)** (VD: `CLO`, `HPH`, `DNG`, `QNH`)",
            "cont": "✏️ **Số Container** (VD: `GAOU2111920`, `DRYU3022599`)",
            "truck": "🚚 **Biển Số Xe** (VD: `50H10224`, `61H10617`)",
            "seal": "🔒 **Số Seal** (VD: `688441`)",
            "carrier": "🏢 **Đơn Vị Vận Chuyển (Đơn vị VC)** (VD: `VSHO`, `THAI HOA`, `TRUCKING 14`)",
            "note": "📝 **Ghi Chú Đóng Hàng** (VD: `Sáng`, `Chiều`, `Lên trưa`)",
        }

        prompt_str = field_names.get(field_type, field_type)
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy bỏ", callback_data="cancel_edit")]])

        await query.message.reply_text(
            f"✏️ Đang sửa thông tin dòng ID PVT `{row_id}`:\n\n"
            f"👉 Vui lòng nhập giá trị mới cho {prompt_str}:",
            reply_markup=cancel_kb,
            parse_mode="Markdown",
        )
        return

    if action == "cancel":
        context.user_data.pop("pending_edit", None)
        await query.edit_message_text("❌ Đã hủy thao tác chỉnh sửa.")
        return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý tin nhắn văn bản (Email kế hoạch đóng hàng, từ khóa tra cứu, hoặc giá trị chỉnh sửa)"""
    if not is_authorized(update):
        return

    text = update.message.text.strip()

    # 1. Kiểm tra xem người dùng có đang trong trạng thái chờ nhập giá trị chỉnh sửa không
    if "pending_edit" in context.user_data:
        edit_info = context.user_data.pop("pending_edit")
        row_id = edit_info["row_id"]
        row_ts = edit_info["row_timestamp"]
        field_type = edit_info["field_type"]

        # Ánh xạ tên trường sang API
        field_map = {
            "pod": "pod",      # CỘT 14
            "cont": "so_cont",
            "truck": "so_xe",
            "seal": "so_seal",
            "carrier": "don_vi_vc",
            "dvvc": "don_vi_vc",
            "note": "ghi_chu",
        }
        target_field = field_map.get(field_type, field_type)

        wait_msg = await update.message.reply_text(f"⏳ Đang cập nhật `{target_field}` = `{text}` lên TMS Vietsun...")
        if not tms_api.is_logged_in:
            await tms_api.login()

        ok = await tms_api.update_field(row_id, row_ts, target_field, text)
        if ok:
            await wait_msg.edit_text(
                f"✅ **Đã cập nhật thành công lên TMS Vietsun!**\n"
                f"• ID PVT: `{row_id}`\n"
                f"• Mục: `{target_field.upper()}`\n"
                f"• Giá trị mới: `{text}`\n\n"
                f"🔄 Đang tải lại dữ liệu...",
                parse_mode="Markdown"
            )
            # Tải lại và hiển thị thẻ cập nhật mới
            results = await tms_api.find_container_or_booking(row_id)
            if results:
                card_text = build_container_card(results[0])
                kb = build_container_keyboard(results[0].get("row_id", ""), results[0].get("row_timestamp", ""))
                await update.message.reply_text(card_text, reply_markup=kb, parse_mode="Markdown")
        else:
            await wait_msg.edit_text(f"❌ Cập nhật thất bại. Vui lòng thử lại sau.")
        return

    # 2. Kiểm tra nếu tin nhắn yêu cầu đổi POD hàng loạt bằng văn bản
    first_line = text.splitlines()[0].strip().upper()
    if first_line == "POD" or first_line.startswith("POD:") or first_line.startswith("/POD"):
        await handle_batch_pod_update(update, text_content=text)
        return

    # Nếu dán nhiều dòng bảng cont và có mã POD
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) >= 2 and any(re.search(r"\b(CLO|DAN|HPG|HPH|HCM|QNH|CLA|VPH|DNG|CMT)\b", l, re.I) for l in lines):
        if any(re.search(r"\b[A-Z]{4}\d{7}\b", l, re.I) for l in lines) and not any(k in text.upper() for k in ["SẢN LƯỢNG", "KẾ HOẠCH", "CHI TIẾT THEO BOOKING"]):
            await handle_batch_pod_update(update, text_content=text)
            return

    # 3. Kiểm tra cú pháp tìm kiếm theo Tên Khách Hàng (VD: KH: Tiền, KH: Phong, KH: "Cty Tiền Phong MN")
    if "\n" not in text:
        m_kh = re.match(
            r"^(?:(?:TÌM|TIM)\s+)?(?:KH|KHACH|KHÁCH|KHACH HANG|KHÁCH HÀNG)\s*[:：\s]\s*(.+)$",
            text,
            re.IGNORECASE,
        )
        if m_kh:
            query_kh = m_kh.group(1).strip().strip('"').strip("'").strip()
            await handle_customer_search(update, query_kh)
            return

    # 4. Kiểm tra từ khóa xem kế hoạch đóng hàng theo ngày (VD: mai, mốt, kia, 17/10, mai Tiền, plan...)
    if "\n" not in text:
        clean_low = text.lower().strip()
        if clean_low in ("ke hoach", "kế hoạch", "kehoach", "plan", "lich", "lịch"):
            await handle_plan_overview(update)
            return

        dt_res, extra = extract_date_and_query(text)
        if dt_res:
            date_iso, date_dmy, label = dt_res
            await handle_date_plan_display(
                update,
                date_iso=date_iso,
                date_dmy=date_dmy,
                label=label,
                filter_keyword=extra,
                is_callback=False,
            )
            return

    # 5. Nếu là văn bản 1 dòng (Số cont, Mã booking, Tên khách hàng, Hàng hóa, Xe...) ➔ Tra cứu đa năng
    if "\n" not in text:
        await handle_query_search(update, text)
        return

    # 5. Nhận diện nếu là Email kế hoạch đóng hàng / trả hàng
    email_keywords = ["BOOKING", "ĐÓNG HÀNG", "TRẢ HÀNG", "CHUYẾN", "TÀU", "KH ĐÓNG HÀNG", "KH TRẢ HÀNG", "SẢN LƯỢNG", "KẾ HOẠCH"]
    has_keywords = any(k in text.upper() for k in email_keywords)
    has_structure = "---" in text or "CHI TIẾT" in text.upper() or "\t" in text or "|" in text or len(text.splitlines()) >= 3
    is_email = (has_keywords and has_structure) or (bool(re.search(r"\b\d{6}\b", text)) and len(text.splitlines()) >= 3)

    if is_email:
        await handle_email_import(update, text_content=text)
        return

    # 6. Mặc định: Thử tra cứu nếu ngắn, hoặc phân tích kế hoạch nếu nhiều dòng
    if len(text.splitlines()) > 1:
        await handle_email_import(update, text_content=text)
    else:
        await handle_query_search(update, text)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý khi người dùng gửi ảnh (kèm caption POD để đổi cảng đến, hoặc ảnh email để nhập kế hoạch)"""
    if not is_authorized(update):
        return

    photo = update.message.photo[-1]
    caption = (update.message.caption or "").strip()

    is_pod_mode = bool(re.search(r"\bpod\b", caption, re.IGNORECASE))

    wait_msg = await update.message.reply_text(
        "📸 Đang tải ảnh và nhận diện danh sách Số Cont & POD (Cột 14)..."
        if is_pod_mode else
        "📸 Đang tải ảnh và dùng Gemini AI phân tích bảng kế hoạch đóng hàng..."
    )
    try:
        photo_file = await photo.get_file()
        img_buffer = BytesIO()
        await photo_file.download_to_memory(img_buffer)
        image_bytes = img_buffer.getvalue()

        await wait_msg.delete()
        if is_pod_mode:
            await handle_batch_pod_update(update, text_content=caption, image_bytes=image_bytes)
        else:
            await handle_email_import(update, text_content=caption, image_bytes=image_bytes)
    except Exception as e:
        logger.exception(f"Lỗi tải ảnh: {e}")
        await wait_msg.edit_text(f"❌ Lỗi xử lý ảnh: {e}")


async def handle_batch_pod_update(
    update: Update,
    text_content: Optional[str] = None,
    image_bytes: Optional[bytes] = None
):
    """
    Nhận diện danh sách (Số cont, Mã POD) từ ảnh hoặc văn bản,
    sau đó tra cứu trên TMS và cập nhật hàng loạt Cột 14 (tinhXuatHangPo).
    """
    wait_msg = await update.message.reply_text("🤖 Đang dùng Gemini AI nhận diện danh sách Số Cont & Cảng đến (POD)...")
    try:
        assignments, err_detail = extract_pod_assignments(text_content=text_content, image_bytes=image_bytes)
        if not assignments:
            err_msg = "❌ **Không nhận diện được cặp Số Container & Cảng đến (POD) nào từ nội dung bạn gửi.**\n\n"
            if err_detail:
                err_msg += f"🔍 **Chi tiết kỹ thuật từ hệ thống:**\n`{err_detail}`\n\n"
            err_msg += (
                "💡 **Gợi ý kiểm tra & Khắc phục:**\n"
                "• Gõ `/models` để kiểm tra bot đã kết nối được Google Gemini API chưa.\n"
                "• Đảm bảo file `.env` trên VPS đã điền `GEMINI_API_KEY=...` chuẩn của Google AI Studio.\n"
                "• Hình ảnh bảng điều độ rõ nét (nhìn rõ cột cont và cột cảng đến CLO, DAN, HPG...).\n"
                "• Hoặc bạn có thể dán trực tiếp danh sách text theo định dạng:\n"
                "```\n"
                "POD\n"
                "DRYU2853598 CLO\n"
                "DRYU2923340 CLO\n"
                "CAIU6870817 DAN\n"
                "```"
            )
            await wait_msg.edit_text(err_msg, parse_mode="Markdown")
            return

        total = len(assignments)
        preview_lines = []
        for a in assignments[:10]:
            c_label = a.get("so_cont") or (f"Bkg {a.get('booking')}" if a.get("booking") else "Cont ?")
            preview_lines.append(f"• `{c_label}` ➔ ⚓ **{a.get('pod')}**")
        if total > 10:
            preview_lines.append(f"... và {total - 10} container khác.")
        preview_text = "\n".join(preview_lines)

        await wait_msg.edit_text(
            f"📋 **ĐÃ NHẬN DIỆN {total} CONTAINER CẦN ĐỔI POD:**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{preview_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ Đang tra cứu trên TMS và cập nhật Cột 14 (tinhXuatHangPo)...",
            parse_mode="Markdown"
        )

        if not tms_api.is_logged_in:
            await tms_api.login()

        success_list = []
        not_found_list = []
        failed_list = []

        for item in assignments:
            so_cont = (item.get("so_cont") or "").strip().upper()
            target_pod = (item.get("pod") or "").strip().upper()
            booking = str(item.get("booking") or "").strip()
            so_seal = str(item.get("so_seal") or "").strip()
            so_xe = (item.get("so_xe") or "").strip().upper()

            query_key = so_cont if so_cont else booking
            if not query_key:
                continue

            rows = await tms_api.search_containers(query_key)
            target_row = None
            if rows:
                for r in rows:
                    clean_r_cont = (r.get("so_cont") or "").replace(" ", "").upper()
                    if so_cont and clean_r_cont == so_cont:
                        target_row = r
                        break
                    if booking and str(r.get("booking_id") or "").strip() == booking:
                        target_row = r
                        break
                if not target_row:
                    target_row = rows[0]

            if not target_row:
                not_found_list.append(f"`{query_key}`")
                continue

            row_id = target_row.get("row_id", "")
            row_ts = target_row.get("row_timestamp", "")
            old_pod = target_row.get("pod_cot_14", "") or "Trống"

            # Cập nhật Cột 14 (tinhXuatHangPo)
            ok = await tms_api.update_field(row_id, row_ts, "pod", target_pod, clear_cache=False)
            if ok:
                # Nếu có số seal hoặc số xe mà trên TMS chưa có -> cập nhật luôn
                if so_seal and not target_row.get("so_seal"):
                    await tms_api.update_field(row_id, row_ts, "so_seal", so_seal, clear_cache=False)
                if so_xe and not target_row.get("so_xe"):
                    await tms_api.update_field(row_id, row_ts, "so_xe", so_xe, clear_cache=False)

                success_list.append({
                    "cont": target_row.get("so_cont") or so_cont,
                    "booking": target_row.get("booking_id") or booking,
                    "old_pod": old_pod,
                    "new_pod": target_pod,
                    "row_id": row_id,
                })
            else:
                failed_list.append(f"`{query_key}`")

        # Xóa cache sau khi hoàn tất toàn bộ cập nhật hàng loạt để tra cứu sau luôn lấy dữ liệu mới
        tms_api.cache_date.clear()

        # Tổng kết báo cáo
        res_lines = [
            f"🎉 **HOÀN TẤT ĐỔI CẢNG ĐẾN (POD - CỘT 14) HÀNG LOẠT!**\n",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"✅ **Thành công:** `{len(success_list)}/{total}` container\n"
        ]

        if success_list:
            for s in success_list[:25]:
                c_lbl = s['cont'] if s['cont'] else f"Bkg {s['booking']}"
                old_p = s['old_pod']
                res_lines.append(f"• `{c_lbl}`: `{old_p}` ➔ ⚓ **`{s['new_pod']}`** (ID `{s['row_id']}`)")
            if len(success_list) > 25:
                res_lines.append(f"... và {len(success_list) - 25} container khác.")

        if not_found_list:
            res_lines.append(f"\n⚠️ **Không tìm thấy trên TMS ({len(not_found_list)}):**")
            res_lines.extend([f"• {x}" for x in not_found_list[:15]])

        if failed_list:
            res_lines.append(f"\n❌ **Lỗi khi cập nhật ({len(failed_list)}):**")
            res_lines.extend([f"• {x}" for x in failed_list[:15]])

        res_lines.append("\n━━━━━━━━━━━━━━━━━━━━")
        res_lines.append("💡 *Dữ liệu Cột 14 trên TMS tab XUẤT - ALL đã được đồng bộ mới nhất.*")

        await wait_msg.edit_text("\n".join(res_lines), parse_mode="Markdown")

    except Exception as e:
        logger.exception(f"Lỗi đổi POD hàng loạt: {e}")
        await wait_msg.edit_text(f"❌ Đã xảy ra lỗi khi đổi POD: {e}")


async def handle_email_import(update: Update, text_content: Optional[str] = None, image_bytes: Optional[bytes] = None):
    """Trích xuất và tự động bấm [+] tạo dòng trên tab XUẤT - ALL"""
    wait_msg = await update.message.reply_text("🤖 Đang phân tích kế hoạch đóng hàng xuất...")
    try:
        bookings = extract_with_gemini(text_content=text_content, image_bytes=image_bytes)
        if not bookings:
            await wait_msg.edit_text("❌ Không nhận diện được booking nào trong nội dung bạn gửi. Vui lòng kiểm tra lại nội dung email.")
            return

        total = len(bookings)
        summary_lines = []
        for i, b in enumerate(bookings, start=1):
            b_id = b.get("booking", "N/A")
            b_tau = b.get("tau", "")
            b_chuyen = b.get("chuyen", "")
            b_cont = b.get("so_cont") or "Chưa có"
            b_pod = b.get("pod") or "N/A"
            b_khach = b.get("khach_hang", "")
            summary_lines.append(f"{i}. 🔖 `{b_id}` | {b_tau} ({b_chuyen}) | Cont: `{b_cont}` | ⚓ POD: `{b_pod}`{f' | {b_khach}' if b_khach else ''}")

        preview_text = "\n".join(summary_lines[:15])
        if total > 15:
            preview_text += f"\n... và {total - 15} booking khác."

        await wait_msg.edit_text(
            f"📋 **ĐÃ NHẬN DIỆN {total} BOOKING TỪ EMAIL:**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{preview_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ Đang tự động bấm `[+]` và đẩy từng dòng lên TMS...",
            parse_mode="Markdown"
        )

        if not tms_api.is_logged_in:
            await tms_api.login()

        created_success = []
        for idx, item in enumerate(bookings, start=1):
            booking_id = item.get("booking", "N/A")
            tau = item.get("tau", "")
            chuyen = item.get("chuyen", "")
            pod = item.get("pod", "")

            logger.info(f"Đang xử lý [{idx}/{total}]: Booking {booking_id} - Tàu {tau} - POD {pod}...")
            ok, msg, new_row = await tms_api.create_export_booking_row(item)
            if ok and new_row:
                created_success.append(new_row)
            else:
                logger.error(f"Thất bại booking {booking_id}: {msg}")

        if not created_success:
            await wait_msg.edit_text("❌ Không thể tạo dòng trên TMS. Vui lòng thử lại hoặc kiểm tra lại đăng nhập TMS.")
            return

        await wait_msg.edit_text(
            f"🎉 **ĐÃ TẠO THÀNH CÔNG {len(created_success)}/{total} DÒNG LÊN TAB XUẤT - ALL!**\n\n"
            f"Dưới đây là chi tiết từng dòng vừa tạo, bạn có thể bấm nút để đổi POD (Cột 14), gán số xe, số cont ngay:",
            parse_mode="Markdown"
        )

        for r in created_success:
            card_text = build_container_card(r)
            kb = build_container_keyboard(r.get("row_id", ""), r.get("row_timestamp", ""))
            await update.message.reply_text(card_text, reply_markup=kb, parse_mode="Markdown")

    except Exception as e:
        logger.exception(f"Lỗi nhập kế hoạch: {e}")
        await wait_msg.edit_text(f"❌ Đã xảy ra lỗi khi tạo kế hoạch: {e}")


def create_telegram_app() -> Application:
    """Khởi tạo bot telegram application"""
    global tg_app
    if not config.TELEGRAM_BOT_TOKEN:
        logger.warning("⚠️ Chưa cấu hình TELEGRAM_BOT_TOKEN trong file config.py hoặc biến môi trường!")
        return None

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("models", models_cmd))
    app.add_handler(CommandHandler("tim", tim_cmd))
    app.add_handler(CommandHandler("kh", kh_cmd))
    app.add_handler(CommandHandler("xoa", xoa_cmd))
    app.add_handler(CommandHandler("pod", pod_cmd))
    for cmd_name in ["ngay", "plan", "kehoach", "lich", "mai", "mot", "kia", "homnay"]:
        app.add_handler(CommandHandler(cmd_name, ngay_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    tg_app = app
    return app


async def tms_keepalive_worker():
    """Chạy ngầm định kỳ mỗi 3 phút để duy trì phiên TMS và giữ tab XUẤT - ALL luôn sẵn sàng 24/7"""
    while True:
        try:
            await asyncio.sleep(180)  # 3 phút (thay vì 8 phút)
            logger.info("⏰ Ping keepalive: duy trì phiên TMS Vietsun tab XUẤT - ALL...")
            if not tms_api.is_logged_in:
                await tms_api.login()
            else:
                await tms_api.ensure_export_view()
                today_str = datetime.now(tms_api.VN_TZ).strftime("%Y-%m-%d")
                await tms_api.fetch_table_for_date(today_str, force_refresh=True)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Lỗi keepalive worker: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Khởi động FastAPI server...")
    # Khởi động và đăng nhập TMS trước
    try:
        await tms_api.login()
        logger.info("✅ Đã kết nối và đăng nhập sẵn sàng vào VSHO TMS tab XUẤT - ALL!")
    except Exception as e:
        logger.warning(f"⚠️ Chưa thể đăng nhập TMS lúc khởi động (sẽ tự thử lại khi có yêu cầu): {e}")

    # Bật worker duy trì phiên TMS ngầm
    keepalive_task = asyncio.create_task(tms_keepalive_worker())

    if config.TELEGRAM_BOT_TOKEN:
        tg = create_telegram_app()
        if tg:
            await tg.initialize()
            if config.WEBHOOK_URL:
                webhook_url = f"{config.WEBHOOK_URL}/webhook"
                logger.info(f"Đang cài đặt Telegram Webhook: {webhook_url}")
                await tg.bot.set_webhook(webhook_url)
            else:
                logger.info("Chạy ở chế độ Polling...")
                await tg.start()
                await tg.updater.start_polling()

    yield
    keepalive_task.cancel()
    try:
        await keepalive_task
    except asyncio.CancelledError:
        pass

    if tg_app:
        logger.info("Dừng bot Telegram...")
        if tg_app.updater and tg_app.updater.running:
            await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


web_app = FastAPI(title="VSHO TMS Export Bot", lifespan=lifespan)

@web_app.get("/")
async def root():
    return {"status": "ok", "service": "VSHO TMS Export Bot", "mode": "XUAT - ALL"}

@web_app.post("/webhook")
async def telegram_webhook(update_data: dict):
    if tg_app:
        update = Update.de_json(update_data, tg_app.bot)
        await tg_app.process_update(update)
    return {"status": "ok"}


async def main_polling():
    """Chạy độc lập chế độ Polling trên VPS: Không cần mở cổng web, không sợ đụng cổng, tự động ngắt webhook Render cũ"""
    logger.info("Khởi động bot ở chế độ Polling trên VPS...")
    try:
        await tms_api.login()
        logger.info("✅ Đã kết nối và đăng nhập sẵn sàng vào VSHO TMS tab XUẤT - ALL!")
    except Exception as e:
        logger.warning(f"⚠️ Chưa thể đăng nhập TMS lúc khởi động (sẽ tự thử lại khi có yêu cầu): {e}")

    # Bật worker duy trì phiên TMS ngầm
    asyncio.create_task(tms_keepalive_worker())

    if config.TELEGRAM_BOT_TOKEN:
        tg = create_telegram_app()
        if tg:
            await tg.initialize()
            # Tự động ngắt Webhook cũ của Render để VPS giành quyền nhận tin nhắn ngay lập tức
            try:
                await tg.bot.delete_webhook(drop_pending_updates=True)
                logger.info("🚀 Đã ngắt Webhook Render cũ thành công! VPS đã giành quyền nhận tin nhắn.")
            except Exception as e_del:
                logger.warning(f"Lỗi xóa webhook: {e_del}")

            await tg.start()
            await tg.updater.start_polling()
            logger.info("🎉 Bot đã sẵn sàng nhận tin nhắn trên Telegram 24/7!")

            # Giữ tiến trình chạy vĩnh viễn
            stop_event = asyncio.Event()
            await stop_event.wait()


if __name__ == "__main__":
    if config.WEBHOOK_URL:
        # Nếu có cấu hình WEBHOOK_URL -> chạy web server FastAPI bằng uvicorn
        uvicorn.run("app:web_app", host="0.0.0.0", port=config.PORT, reload=False)
    else:
        # Nếu chạy Polling trên VPS -> chạy trực tiếp KHÔNG CẦN CỔNG WEB, KHÔNG LO BỊ LỖI [Errno 98]!
        asyncio.run(main_polling())
