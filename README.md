# Telegram AutoForwarder - Free Community Edition

An advanced, high-performance, fully asynchronous Telegram auto-forwarding engine and management bot. Designed with scalability and reliability in mind, this project allows users to securely link their Telegram accounts, configure highly customizable routing rules, and automatically forward messages across standard channels, supergroups, and forum topics.

All sensitive configuration values and administrator identifiers are securely loaded via environment variables, ensuring this repository is safe for public or private deployment.

---

## 🚀 Core Engine Capabilities

The heavy lifting is handled by the underlying Telethon-based client engine, which is built for speed, reliability, and responsible use of the Telegram API.

- **Memory Optimization:** Uses a `TTLCache` for temporary message mappings to reduce memory usage and prevent unbounded memory growth.
- **Advanced Chat Support:** Supports standard channels, supergroups, Telegram Forums, and Topics, including accurate handling of `message_thread_id` and `reply_to` relationships.
- **Session State Management:** Automatically detects dead, expired, or unregistered user sessions and notifies the manager bot so processing can be halted safely.
- **Safer Entity Resolution:** Uses controlled entity-resolution and dialog-search mechanisms to reduce unnecessary Telegram API requests.
- **Restricted Content Handling:** Provides specialized handling for restricted channels, limiting extraction to lightweight image scraping while skipping heavy media processing.
- **Dynamic Content Transformation:** Supports dynamic text placeholders together with comprehensive Find & Replace rules for modifying forwarded messages.

---

## 🎛️ Manager Bot & Interactive UI

The user-facing bot is built with Aiogram 3.x and provides an intuitive and spam-resistant control center.

- **Interactive Inline Dashboard:** Provides a fully interactive interface for configuring forwarding settings without requiring complex commands.
- **Granular Route Control:** Users can enable or disable individual forwarding routes using `/onroute` and `/offroute`.
- **Automated Cleanup & Alerts:** Detects dead user sessions, safely removes broken session files when appropriate, and alerts affected users through Telegram.
- **Resilient Error Handling:** Includes robust exception handling and can report system failures to a designated bug-reporting channel.
- **Optimized Performance:** Heavy media scrapers and unnecessary processing components have been removed from the community edition to keep the processing footprint lightweight.

---

## 💎 Advanced Filters & Customization

Users have access to an extensive suite of filtering and formatting commands to precisely control what gets forwarded.

- `/filter <id> <Find> | <Replace>` — Replace specific words, phrases, or links within message content.
- `/setbegin <id> <text>` — Add a custom header to outgoing messages.
- `/setend <id> <text>` — Add a custom footer to outgoing messages.
- `/ignoretext <id>` — Remove text from forwarded messages.
- `/ignoremedia <id>` — Remove media from forwarded messages.
- `/nativeforward <id>` — Forward messages using Telegram's native forwarding mechanism.
- `/linkpreview <id>` — Enable or disable URL preview generation.
- `/setkeywords <id> <words>` — Configure keyword-based inclusion rules.
- `/setblacklist <id> <words>` — Configure keyword-based exclusion rules.
- `/whitelistuser <id> <users>` — Restrict forwarding to specific senders.
- `/setpattern <id> <regex>` — Filter messages using Regular Expressions.
- `/setdelay <id> <sec>` — Introduce a configurable delay before forwarding.
- `/setcooldown <id> <sec>` — Introduce a cooldown period to reduce repeated processing.
- `/autoupdate <id>` — Automatically synchronize edited and deleted messages.

---

## 🛠️ Superuser & Admin Controls

System administrators are equipped with specialized commands to monitor system health and manage users.

- `/sysload` — View live server metrics including CPU, RAM, and disk usage.
- `/broadcast <message>` — Send a global announcement to connected users.
- `/showusers` — Retrieve a detailed list of connected users and their route counts.
- `/ban <id>` — Restrict access for a specific user.
- `/unban <id>` — Restore access for a previously banned user.
- `/wipeuser <id>` — Permanently delete a user's data and session files.
- `/testbug` — Initiate a controlled system failure to verify crash reporting.

---

## ⚙️ Installation & Setup

### 1. Prerequisites

- Python 3.9 or newer
- A Telegram API ID and API Hash from [my.telegram.org](https://my.telegram.org)
- A Bot Token from [@BotFather](https://t.me/BotFather)

### 2. Clone the Repository

```bash
git clone https://github.com/pundhiranshul/telegram-autoforwarder.git
cd telegram-autoforwarder
```

### 3. Install Dependencies

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Environment Configuration

Create a `.env` file in the root directory. This project keeps all sensitive identifiers completely isolated from the codebase.

```dotenv
# Bot Credentials
BOT_TOKEN=your_bot_token_here

# Telegram API Credentials
API_ID=your_api_id_here
API_HASH=your_api_hash_here

# Admin & Logging Configuration
BUG_CHANNEL_ID=your_bug_channel_id_here
SUPERUSERS=your_superuser_id_1,your_superuser_id_2
```

### 5. Protect Your Environment File

Do not commit `.env` or Telegram session files to the repository.

Add the following entries to `.gitignore`:

```gitignore
.env
*.session
*.session-journal
__pycache__/
*.py[cod]
venv/
```

### 6. Launch the Engine

```bash
python manager_bot.py
```

---

## 🔐 Security Notes

- Never publish your `BOT_TOKEN`, `API_ID`, or `API_HASH`.
- Never commit `.env` files containing real credentials.
- Treat Telegram session files as sensitive authentication credentials.
- Restrict administrator identifiers to trusted accounts only.
- Rotate credentials immediately if they are accidentally exposed.
- Keep production logs and runtime files protected from unauthorized access.

---

## 📁 Recommended Project Structure

```text
telegram-autoforwarder/
├── manager_bot.py
├── forwarder_core.py
├── requirements.txt
├── README.md
├── .env
├── .gitignore
└── ...
```

Runtime-generated session files, logs, caches, and other sensitive artifacts should not be committed to version control.

---

## 📄 License

This project is licensed under the MIT License.
`````````
