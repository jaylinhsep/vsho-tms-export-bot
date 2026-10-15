import os
from typing import Set

# Cấu hình kết nối TMS Vietsun
TMS_URL = os.getenv("TMS_URL", "https://vsho.vietsunlogistics.com")
TMS_USERNAME = os.getenv("TMS_USERNAME", "trucking14")
TMS_PASSWORD = os.getenv("TMS_PASSWORD", "1")

# Telegram Bot Token (Tạo từ @BotFather)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Gemini API Key từ Google AI Studio (Dùng để đọc text/ảnh email kế hoạch đóng hàng)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Danh sách Chat ID được phép truy cập bot (Để trống là mở cho mọi người trong team)
ALLOWED_CHAT_IDS: Set[int] = {
    int(x.strip()) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if x.strip().isdigit()
}

# Cổng webhook nếu triển khai Render / VPS
PORT = int(os.getenv("PORT", "8000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
