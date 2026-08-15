# Telegram AutoForwarder - Free Community Edition

A high-performance, asynchronous Telegram auto-forwarding bot and management engine. Built with `aiogram` and `telethon`, this project allows users to seamlessly link their Telegram accounts, configure complex routing rules, and automatically forward messages across channels, groups, and forums.

## 🚀 Key Features

### Core Engine (`forwarder_core.py`)
*   **Optimized Memory Management**: Memory leaks fixed via `TTLCache` for mapping messages[cite: 1].
*   **Forum & Topic Support**: Full support for Telegram's `message_thread_id` and `reply_to` structures[cite: 1].
*   **Dead Session Handling**: Auto-detects dead or unregistered sessions and notifies the manager safely[cite: 1].
*   **Anti-Ban Entity Resolution**: Implements safer entity resolution to avoid API flood bans[cite: 1].
*   **Restricted Channels Support**: Dedicated scraping logic for images in restricted channels where heavy media is skipped[cite: 1].
*   **Dynamic Transformations**: Supports dynamic placeholders and Find & Replace rules[cite: 1].

### Bot Dashboard (`manager_bot.py`)
*   **Inline Dashboard**: Fully interactive UI built with `aiogram` for configuring routes[cite: 2].
*   **Individual Route Toggles**: Pause or resume specific routes with `/onroute` and `/offroute`[cite: 2].
*   **Resilient Error Handling**: Automatically logs massive system crashes and cleans up dead sessions[cite: 2].
*   **Safe Database Transactions**: Utilizes `aiosqlite` for non-blocking file I/O and state storage[cite: 2].

## 🛠 Tech Stack
*   **Python 3.9+**
*   **Aiogram 3.x** (Bot Father UI / Dashboard)
*   **Telethon** (Client-side scraping and forwarding engine)
*   **SQLite (aiosqlite)** (Asynchronous database management)

## ⚙️ Installation & Setup

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/yourusername/telegram-autoforwarder.git](https://github.com/yourusername/telegram-autoforwarder.git)
   cd telegram-autoforwarder
