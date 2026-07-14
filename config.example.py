import os

# Telegram bot credentials (create via @BotFather)
# For GitHub Actions, set BOT_TOKEN and CHAT_ID as repository secrets.
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# Check interval in seconds (600 = 10 minutes)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "600"))

# ADY route: Tbilisi → Baku
FROM_STATION_ID = 170  # TBILISI-PASS
TO_STATION_ID = 232    # BAKU RWS
FROM_STATION_NAME = "Tbilisi"
TO_STATION_NAME = "Baku"

# ADY API
ADY_BASE_URL = "https://ticket.ady.az"
ADY_SEARCH_PAGE = f"{ADY_BASE_URL}/en/ticket-search-en"
ADY_LOGIN_PAGE = f"{ADY_BASE_URL}/en/login"
ADY_LOGIN_PAGE = f"{ADY_BASE_URL}/en/login"
ADY_TRIP_DATES_URL = f"{ADY_BASE_URL}/ticket-api/get_trip_dates"
RECAPTCHA_SITE_KEY = "6LecJSYtAAAAAMSGKGKhA72oiCfAWr8EoAUzEMgj"

# Local persistence for notified dates
NOTIFIED_DATES_FILE = os.getenv("NOTIFIED_DATES_FILE", "notified_dates.json")
BOT_TOKEN = "8885037099:AAEpfZc_vEcevzl6u_BlVxzIssDeyiXp3xs"
CHAT_ID = "8595785253"
