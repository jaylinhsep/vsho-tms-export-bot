import time
import asyncio
import logging
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Dict, Any, Optional

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
from extractor_export import extract_with_gemini
from tms_export_api import TMSExportApiClient

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
    sl20 = r.get("sl_20", "0")
    sl40 = r.get("sl_40", "0")
    sl_str = f"{sl20}x20ft" if sl20 and sl20 != "0" else (f"{sl40}x40ft" if sl40 and sl40 != "0" else "Chưa rõ")
    gw = r.get("gw", "")
    cont = r.get("so_cont", "") or "*(Chưa gán)*"
    seal = r.get("so_seal", "") or "*(Chưa có)*"
    dvvc = r.get("don_vi_vc", "") or ""
    so_xe = r.get("so_xe", "") or "*(Chưa gán)*"
    port_ha = r.get("port_ha_hang", "") or "BẾN NGHÉ"
    lien_he = r.get("lien_he", "") or ""
    ghi_chu = r.get("ghi_chu", "") or "*(Trống)*"

    card = (
        f"📦 **CHI TIẾT CONTAINER XUẤT**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• **ID PVT:** `{row_id}` | **Booking:** `{booking}`\n"
        f"• **Tàu / Chuyến:** {tau} ({chuyen})\n"
        f"• **Ngày đóng:** `{ngay_dong}` | **Ngày tàu chạy:** `{ngay_chay}`\n"
        f"• **Khách hàng:** {khach}\n"
        f"• **Kho đóng:** {kho} {f'({tinh})' if tinh else ''}\n"
        f"• **Sản lượng:** {sl_str} {f'| **GW:** {gw}T' if gw else ''}\n"
        f"• **Số Cont:** `{cont}`\n"
        f"• **Số Seal:** `{seal}`\n"
        f"• **Đơn vị VC / Xe:** {f'{dvvc} - ' if dvvc else ''}`{so_xe}`\n"
        f"• ⚓ **Cảng đến (POD - Cột 14):** `{pod_14}`\n"
        f"• **Port hạ hàng:** {port_ha}\n"
        f"{f'• **Liên hệ kho:** {lien_he}\n' if lien_he else ''}"
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
            InlineKeyboardButton("📝 Sửa Ghi Chú", callback_data=f"edit_note_{row_id}_{row_timestamp}"),
            InlineKeyboardButton("🔄 Làm Mới", callback_data=f"refresh_{row_id}_{row_timestamp}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = (
        "🚢 **Bot Quản Lý Hàng Xuất (VSHO TMS Vietsun)**\n\n"
        "⚡ **Tính năng chính:**\n"
        "1️⃣ **Nhập kế hoạch tự động:** Chỉ cần copy toàn bộ nội dung email kế hoạch đóng hàng (hoặc gửi ảnh chụp màn hình email) dán vào đây ➔ Bot tự động bấm `[+]` và điền toàn bộ thông tin lên TMS.\n\n"
        "2️⃣ **Tra cứu cont xuất:** Gõ trực tiếp số cont (VD: `GAOU2111920`) hoặc mã booking (VD: `300170`), hoặc gõ lệnh `/tim <từ khóa>`.\n\n"
        "3️⃣ **Chỉnh sửa & Submit ngược lại TMS:** Có sẵn nút bấm trực tiếp dưới mỗi kết quả tra cứu:\n"
        "   • ⚓ **Đổi POD (Gán vào Cột 14 TMS)**\n"
        "   • ✏️ **Gán / Đổi số cont**\n"
        "   • 🚚 **Gán biển số xe**\n"
        "   • 🔒 **Gán số seal**\n"
        "   • 📝 **Sửa ghi chú đóng hàng**\n\n"
        "📌 **Lệnh hữu ích:**\n"
        "• `/status` — Kiểm tra kết nối tài khoản TMS & số lượng cont xuất hôm nay\n"
        "• `/tim <từ khóa>` — Tra cứu cont xuất theo cont hoặc booking\n"
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
            await wait_msg.edit_text("❌ Đăng nhập thất bại, vui lòng kiểm tra tài khoản/mật khẩu TMS.")
            return

        from datetime import datetime
        today_str = datetime.now(tms_api.VN_TZ if hasattr(tms_api, "VN_TZ") else None).strftime("%Y-%m-%d") if hasattr(tms_api, "VN_TZ") else datetime.now().strftime("%Y-%m-%d")
        rows = await tms_api.fetch_table_for_date(today_str, force_refresh=True)
        elapsed = round(time.time() - t0, 2)

        await wait_msg.edit_text(
            f"✅ **Hệ thống VSHO TMS Hoạt Động Bình Thường!**\n\n"
            f"👤 Tài khoản: `{config.TMS_USERNAME}`\n"
            f"📊 Tab đang kết nối: **XUẤT - ALL** (kieuXemTrucking=2)\n"
            f"📦 Số lượng cont xuất hôm nay ({today_str}): `{len(rows)}` cont\n"
            f"⚡ Tốc độ phản hồi: `{elapsed}s`\n"
            f"🔑 Gemini AI: `{'Đã cấu hình' if config.GEMINI_API_KEY else 'Chưa có key (dùng regex)'}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Lỗi status")
        await wait_msg.edit_text(f"❌ Lỗi kiểm tra kết nối: {e}")


async def tim_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("👉 Vui lòng nhập số cont hoặc mã booking. Ví dụ: `/tim 300170` hoặc `/tim GAOU2111920`", parse_mode="Markdown")
        return

    query = " ".join(context.args).strip()
    await handle_query_search(update, query)


async def handle_query_search(update: Update, query: str):
    """Tìm kiếm và hiển thị thẻ thông tin container xuất kèm nút chỉnh sửa"""
    wait_msg = await update.message.reply_text(f"🔍 Đang tra cứu `{query}` trên tab XUẤT - ALL của TMS...")
    try:
        if not tms_api.is_logged_in:
            await tms_api.login()

        results = await tms_api.find_container_or_booking(query)
        if not results:
            await wait_msg.edit_text(
                f"❌ Không tìm thấy thông tin nào khớp với `{query}` trong phạm vi [-2 đến +5 ngày].\n\n"
                f"💡 Bạn có thể kiểm tra lại số cont hoặc mã booking.",
                parse_mode="Markdown"
            )
            return

        await wait_msg.delete()
        for r in results[:4]:  # Hiển thị tối đa 4 kết quả khớp nhất
            card_text = build_container_card(r)
            kb = build_container_keyboard(r.get("row_id", ""), r.get("row_timestamp", ""))
            await update.message.reply_text(card_text, reply_markup=kb, parse_mode="Markdown")

    except Exception as e:
        logger.exception(f"Lỗi khi tra cứu: {e}")
        await wait_msg.edit_text(f"❌ Đã xảy ra lỗi khi tra cứu: {e}")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý sự kiện khi bấm các nút Inline Keyboard"""
    query = update.callback_query
    await query.answer()

    data = query.data
    parts = data.split("_")
    action = parts[0]

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

    if action == "edit":
        field_type = parts[1]  # pod, cont, truck, seal, note
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

    # 2. Nhận diện nếu là Email kế hoạch đóng hàng (chứa từ khóa đặc trưng)
    email_keywords = ["BOOKING", "ĐÓNG HÀNG", "CHUYẾN", "TÀU", "KH ĐÓNG HÀNG", "SẢN LƯỢNG"]
    is_email = any(k in text.upper() for k in email_keywords) and ("---" in text or "CHI TIẾT" in text.upper() or "\t" in text or "|" in text)

    if is_email:
        await handle_email_import(update, text_content=text)
        return

    # 3. Nếu là 1 chuỗi ngắn (Số cont hoặc Booking) ➔ Tra cứu nhanh
    clean_text = text.replace(" ", "").upper()
    if len(clean_text) <= 15:
        await handle_query_search(update, text)
        return

    # 4. Nếu không rõ, thử phân tích qua AI xem có phải email đóng hàng không
    await handle_email_import(update, text_content=text)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý khi người dùng gửi ảnh chụp màn hình email kế hoạch đóng hàng"""
    if not is_authorized(update):
        return

    photo = update.message.photo[-1]
    wait_msg = await update.message.reply_text("📸 Đang tải ảnh và dùng Gemini AI phân tích bảng kế hoạch đóng hàng...")
    try:
        photo_file = await photo.get_file()
        img_buffer = BytesIO()
        await photo_file.download_to_memory(img_buffer)
        image_bytes = img_buffer.getvalue()

        caption = update.message.caption or ""
        await wait_msg.delete()
        await handle_email_import(update, text_content=caption, image_bytes=image_bytes)
    except Exception as e:
        logger.exception(f"Lỗi tải ảnh: {e}")
        await wait_msg.edit_text(f"❌ Lỗi xử lý ảnh: {e}")


async def handle_email_import(update: Update, text_content: Optional[str] = None, image_bytes: Optional[bytes] = None):
    """Trích xuất và tự động bấm [+] tạo dòng trên tab XUẤT - ALL"""
    wait_msg = await update.message.reply_text("🤖 Đang phân tích kế hoạch đóng hàng xuất...")
    try:
        bookings = extract_with_gemini(text_content=text_content, image_bytes=image_bytes)
        if not bookings:
            await wait_msg.edit_text("❌ Không nhận diện được booking nào trong nội dung bạn gửi. Vui lòng kiểm tra lại nội dung email.")
            return

        total = len(bookings)
        await wait_msg.edit_text(f"📋 **Tìm thấy {total} cont/booking cần tạo kế hoạch:**\nĐang tự động bấm `[+]` và đẩy dữ liệu lên TMS Vietsun...", parse_mode="Markdown")

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
    app.add_handler(CommandHandler("tim", tim_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    tg_app = app
    return app


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Khởi động FastAPI server...")
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


if __name__ == "__main__":
    # Chạy trực tiếp qua uvicorn trên cổng config.PORT
    uvicorn.run("app:web_app", host="0.0.0.0", port=config.PORT, reload=False)
