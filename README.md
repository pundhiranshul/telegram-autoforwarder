# Telegram AutoForwarder Engine 🚀

An asynchronous, event-driven Telegram message routing and forwarding engine built with Python. 

This project bridges **Aiogram 3** (for a responsive, non-blocking bot management UI) and **Telethon** (for client-side message scraping and routing) into a single, cohesive application. It is designed to handle high-volume message filtering, dynamic RegEx parsing, and automatic session recovery without memory leaks.

## 🌟 Key Features

* **Dual-Engine Architecture:** Uses `aiogram` to manage user states, FSM (Finite State Machines), and inline dashboards, while `telethon` handles background listener tasks dynamically.
* **Forum Topic Support:** Full support for Telegram Supergroup `message_thread_id` routing (capture and forward to specific topics seamlessly).
* **Advanced Pipeline Filtering:**
  * Sender whitelisting and blacklisting.
  * Media & Text stripping (including Image scraping for restricted channels).
  * RegEx pattern matching & automatic Find/Replace transformations.
* **Resilience & Memory Management:** 
  * Implements `TTLCache` to prevent memory leaks during long-running background tasks.
  * Auto-detects dead/revoked API sessions, cleans up orphaned database locks, and notifies the user via DM to securely re-authenticate.
  * Safely handles `FloodWaitError` constraints across parallel worker tasks.
* **Filter Simulator (Dry Run):** An integrated testing suite allowing users to simulate their RegEx and routing logic before deploying to live channels.

## 🛠️ Tech Stack

* **Language:** Python 3.9+
* **Frameworks:** `aiogram` (Bot API), `Telethon` (MTProto API)
* **Database:** `aiosqlite` (Async SQLite3 for non-blocking I/O)
* **Optimization:** `uvloop` for ultra-fast event loop processing, `psutil` for hardware load-balancing.

## ⚙️ Installation & Setup

### 1. Clone and Install Dependencies
```bash
git clone [https://github.com/pundhiranshul/telegram-autoforwarder.git](https://github.com/pundhiranshul/telegram-autoforwarder.git)
cd telegram-autoforwarder
python3 -m venv venv
source venv/bin/activate  # On Windows use: venv\Scripts\activate
pip install -r requirements.txt
