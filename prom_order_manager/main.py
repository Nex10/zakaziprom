import time
import logging
import json
import os
import asyncio
import re
import threading
from flask import Flask
import pandas as pd
from telegram import Bot
from config import PROM_API_TOKENS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from prom_client import PromClient

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask App for Render Health Check
app = Flask(__name__)

@app.route('/')
def health_check():
    # Log health check to verify UptimeRobot is hitting it
    logger.info("Health check ping received!")
    return "Bot is running!", 200

@app.route('/upload_db', methods=['POST'])
def upload_db():
    from flask import request
    try:
        if 'file' not in request.files:
            return "No file part", 400
        file = request.files['file']
        if file.filename == '':
            return "No selected file", 400

        global processor_ref
        save_path = "prom_import_data.json"
        if processor_ref:
            save_path = processor_ref._get_json_db_path()

        try:
            new_data = json.load(file)
        except Exception:
            return "Invalid JSON", 400

        existing = {}
        if os.path.exists(save_path):
            try:
                with open(save_path, "r", encoding="utf-8") as f:
                    existing = json.load(f) or {}
            except Exception:
                existing = {}

        if not isinstance(existing, dict) or not isinstance(new_data, dict):
            return "JSON must be an object (dict)", 400

        changed = 0
        for k, v in new_data.items():
            if existing.get(k) != v:
                changed += 1
        existing.update(new_data)

        os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        logger.info(f"Received DB update via HTTP. Merged {changed} items. Total {len(existing)}. Path {save_path}")

        if processor_ref:
            processor_ref.local_notes = existing
            logger.info("Triggered hot-reload of notes in processor.")

        return f"OK. Changed {changed}. Total {len(existing)}", 200
    except Exception as e:
        logger.error(f"Error in /upload_db: {e}")
        return str(e), 500

processor_ref = None


def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# Constants
CHECK_INTERVAL = 30  # Check every 30 seconds (faster response)
PROCESSED_ORDERS_FILE = "processed_orders.json"
TARGET_STATUSES = ["received", "processing", "custom-133340"]  # Added custom "In Work" status
AUTO_ACCEPT_NEW = True

class OrderProcessor:
    def __init__(self):
        self.prom_clients = [PromClient(token) for token in PROM_API_TOKENS]
        logger.info(f"Loaded {len(self.prom_clients)} Prom.ua shops/tokens.")
        
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN)
        self.processed_orders = self._load_processed_orders()
        self.local_notes = self._load_local_notes()
        self.last_update_id = 0 # For Telegram polling
        self.startup_mode = True # Flag to silent first run
        
        if not self.prom_clients:
            logger.warning("No Prom API tokens found! Please check .env file.")

        # Initialize: mark all currently valid orders as processed to avoid spamming old ones
        # Only do this if we have no history (first run)
        if not self.processed_orders:
            logger.info("First run detected. Marking existing orders as processed to avoid spam.")
            self._mark_current_orders_processed()

    def _get_json_db_path(self):
        """
        Determines the path for the JSON database.
        Priority:
        1. Environment Variable SHARED_DATA_PATH
        2. Sibling directory (../prom_automation/...) - for local dev
        3. Current directory - for server deployment
        """
        env_path = os.getenv("SHARED_DATA_PATH")
        if env_path:
            return env_path
            
        # Check sibling folder (Local Dev)
        sibling_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 
                                 "prom_automation", "prom_import_data.json")
        # We check if the DIRECTORY exists, not the file, because we might need to write to it
        sibling_dir = os.path.dirname(sibling_path)
        if os.path.exists(sibling_dir):
            return sibling_path
            
        # Default to current directory (Server/Render)
        return "prom_import_data.json"

    def _load_local_notes(self):
        """
        Loads private notes from a JSON map file (server-friendly).
        Supports fallback to local Excel for backward compatibility during dev.
        """
        notes = {}
        
        # Priority 1: JSON file
        json_path = self._get_json_db_path()
        
        if os.path.exists(json_path):
            try:
                logger.info(f"Loading fallback notes from JSON: {json_path}")
                with open(json_path, "r", encoding="utf-8") as f:
                    notes = json.load(f)
                logger.info(f"Loaded {len(notes)} notes from JSON.")
                return notes
            except Exception as e:
                logger.error(f"Failed to load JSON notes: {e}")
        else:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(json_path)), exist_ok=True)
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump({}, f, ensure_ascii=False, indent=2)
                logger.info(f"Created empty notes DB: {json_path}")
            except Exception as e:
                logger.warning(f"Failed to create empty notes DB at {json_path}: {e}")

        # Priority 2: Excel file (Legacy/Local Dev)
        file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 
                                 "prom_automation", "prom_import_fixed.xlsx")
        
        if os.path.exists(file_path):
            try:
                logger.info(f"Loading fallback notes from Excel: {file_path}")
                df = pd.read_excel(file_path)
                if 'Код_товару' in df.columns and 'Личные_заметки' in df.columns:
                    for _, row in df.iterrows():
                        sku = str(row['Код_товару']).strip()
                        note = str(row['Личные_заметки'])
                        if sku and note and note.lower() != 'nan':
                            notes[sku] = note
                logger.info(f"Loaded {len(notes)} notes from Excel.")
            except Exception as e:
                logger.error(f"Failed to load local Excel notes: {e}")
        else:
            logger.warning(f"No fallback data found (checked {json_path} and {file_path})")
            
        return notes

    def _mark_current_orders_processed(self):
        for client in self.prom_clients:
            try:
                for status in TARGET_STATUSES:
                    # Fetch multiple pages if needed, but usually last 100 is enough to cover active ones
                    # Prom API defaults are tricky, let's just rely on default page size
                    orders = client.get_orders(status=status)
                    if orders:
                        for order in orders:
                            self.processed_orders.add(str(order.get("id")))
                    else:
                         # logger.info(f"No existing orders found for status {status}") # Reduce log spam
                         pass
            except Exception as e:
                logger.error(f"Error initializing processed orders: {e}")
        
        # Save to file immediately
        with open(PROCESSED_ORDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(self.processed_orders), f)
        logger.info(f"Marked {len(self.processed_orders)} existing orders as processed.")

    def _load_processed_orders(self):
        if os.path.exists(PROCESSED_ORDERS_FILE):
            try:
                with open(PROCESSED_ORDERS_FILE, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            except json.JSONDecodeError:
                return set()
        return set()

    def _save_processed_order(self, order_id):
        self.processed_orders.add(str(order_id))
        with open(PROCESSED_ORDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(self.processed_orders), f)

    def _extract_ttn(self, order):
        # Try to find TTN in delivery_provider_data
        delivery_data = order.get("delivery_provider_data", {})
        if delivery_data:
             # Common keys: declaration_number, ttn
            for key in ["declaration_number", "ttn", "invoice_number"]:
                if val := delivery_data.get(key):
                    return val
        
        # Fallback: check general delivery_note or similar
        return order.get("delivery_note")

    def _parse_private_note(self, note):
        """
        Parse note format: "Price: 123 UAH | Supplier: Title (username) | Art: 123"
        Also supports Russian: "Цена: ... | Поставщик: ... | Арт: ..."
        Returns dict with extracted fields.
        """
        data = {}
        if not note:
            return data
            
        parts = [p.strip() for p in note.split("|")]
        supplier_parts = []
        
        for part in parts:
            part_lower = part.lower()
            if part_lower.startswith("price:") or part_lower.startswith("цена:"):
                data["purchase_price"] = part.split(":", 1)[1].strip()
            elif part_lower.startswith("art:") or part_lower.startswith("арт:"):
                data["model"] = part.split(":", 1)[1].strip()
            else:
                # Assuming this is part of the supplier info
                # Remove "Supplier:" prefix if present
                clean_part = part
                if part_lower.startswith("supplier:") or part_lower.startswith("поставщик:"):
                    clean_part = part.split(":", 1)[1].strip()
                
                if clean_part:
                    supplier_parts.append(clean_part)
        
        data["supplier"] = " | ".join(supplier_parts)
        return data

    async def auto_accept_new_orders(self):
        """
        Check for new orders and auto-accept them (status -> received).
        Only if AUTO_ACCEPT_NEW is True.
        """
        if not AUTO_ACCEPT_NEW:
            return

        for client in self.prom_clients:
            try:
                orders = client.get_orders(status="pending")
                for order in orders:
                    order_id = order.get("id")
                    logger.info(f"Auto-accepting new order {order_id}")
                    # Step 1: Try to set 'In Work' (custom-133340) directly
                    # If that fails (e.g. not allowed from pending), fallback to 'received'
                    target_status = "custom-133340"
                    if client.set_order_status(order_id, target_status):
                        logger.info(f"Order {order_id} accepted successfully (set to '{target_status}')")
                    else:
                        logger.warning(f"Failed to set {target_status} directly. Falling back to 'received'...")
                        if client.set_order_status(order_id, "received"):
                            logger.info(f"Order {order_id} set to 'received' (fallback)")
                        else:
                            logger.error(f"Failed to accept order {order_id} (both targets failed)")
            except Exception as e:
                logger.error(f"Error auto-accepting orders: {e}")

    async def process_orders(self):
        logger.info("Checking for new orders...")
        
        for client in self.prom_clients:
            try:
                for status in TARGET_STATUSES:
                    orders = client.get_orders(status=status)
                    
                    for order in orders:
                        await self._process_single_order(client, order)
            except Exception as e:
                logger.error(f"Error checking orders for a client: {e}")

    async def _process_single_order(self, client, order):
        order_id = str(order.get("id"))
        if order_id in self.processed_orders:
            return

        ttn = self._extract_ttn(order)
        if not ttn:
            return # Skip if no TTN yet

        # Found a new order with TTN!
        logger.info(f"Processing order {order_id} with TTN {ttn}")
        
        # SILENT STARTUP CHECK
        if self.startup_mode:
            logger.info(f"Startup Mode: Silently marking order {order_id} as processed (no notification).")
            self._save_processed_order(order_id)
            return

        # Extract Client Info
        client_first_name = order.get("client_first_name", "")
        client_last_name = order.get("client_last_name", "")
        client_name = f"{client_first_name} {client_last_name}".strip()
        
        for item in order.get("products", []):
            product_id = item.get("id")
            
            # Fetch product to get private note using the SAME client that found the order
            product_data = client.get_product(product_id)
            private_note = ""
            if product_data:
                private_note = product_data.get("private_note") or product_data.get("personal_notes") or ""
                
                # If no note, check if it's a variation and try fetching parent
                if not private_note and product_data.get("variation_base_id"):
                    parent_id = product_data.get("variation_base_id")
                    logger.info(f"Checking parent product {parent_id} for note...")
                    parent_data = client.get_product(parent_id)
                    if parent_data:
                        private_note = parent_data.get("private_note") or parent_data.get("personal_notes") or ""
                        if private_note:
                            logger.info(f"Found note in parent product: {private_note}")

                # Fallback: Check local Excel notes by SKU
                if not private_note:
                    sku = item.get("sku")
                    if sku:
                        logger.info(f"Checking local notes for SKU {sku}...")
                        private_note = self.local_notes.get(sku, "")
                        
                        # Fuzzy match: Try to find sibling variations if exact match fails
                        # e.g. if we have MIN-123-4 but DB only has MIN-123-1, they share the same supplier info
                        if not private_note and "-" in sku:
                            base_sku = sku.rsplit("-", 1)[0] # "MIN-123-4" -> "MIN-123"
                            logger.info(f"Direct match failed. Trying fuzzy match for base SKU: {base_sku}...")
                            
                            # Scan keys for partial match
                            for db_sku, db_note in self.local_notes.items():
                                if db_sku.startswith(base_sku):
                                    private_note = db_note
                                    logger.info(f"Fuzzy match success! Found similar SKU {db_sku}")
                                    break

                        if private_note:
                            logger.info(f"Found note in local fallback: {private_note}")
                        else:
                            logger.warning(f"SKU {sku} not found in local DB (loaded {len(self.local_notes)} items).")
            
            logger.info(f"Product {product_id} private note: '{private_note}'")
            note_data = self._parse_private_note(private_note)
            
            supplier = note_data.get("supplier", "Неизвестный поставщик")
            purchase_price = note_data.get("purchase_price", "Цена не указана")
            model = note_data.get("model") or item.get("sku") or "Артикул не найден"
        
            # Size/Color: Extract from name or variation info
            # Prom often puts it in 'name' like "Name, Color: Red, Size: S"
            item_name = item.get("name", "")
            quantity = item.get("quantity", 1)
            
            # Format: 2nd line size/color + quantity if > 1
            size_color_line = item_name
            if quantity > 1:
                size_color_line += f" ({quantity} ед.)"

            message = (
                f"{supplier}\n"
                f"{size_color_line}\n"
                f"{model}\n"
                f"{purchase_price}\n"
                f"{ttn} {client_name}"
            )
            
            # Extract image URL
            image_url = None
            if product_data:
                images = product_data.get("images", [])
                if images:
                    # Try to find the largest image or just take the first one
                    # Prom API 'url' is usually the main image
                    image_url = images[0].get("url")

            # Send to Telegram
            try:
                sent_photo = False
                if image_url:
                    try:
                        await self.bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=image_url, caption=message)
                        logger.info(f"Sent notification with photo for order {order_id}, item {product_id}")
                        sent_photo = True
                    except Exception as e_photo:
                        logger.warning(f"Failed to send photo for order {order_id}: {e_photo}. Falling back to text.")
                
                if not sent_photo:
                        await self.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
                        logger.info(f"Sent text notification for order {order_id}, item {product_id}")

            except Exception as e:
                logger.error(f"Failed to send Telegram message: {e}")
        
        # After sending notification, set status to 'custom-133340' (In Work)
        # Only if it's not already in that status
        current_status = order.get("status")
        target_status = "custom-133340"
        
        if current_status != target_status:
            if client.set_order_status(order_id, target_status):
                logger.info(f"Automatically updated order {order_id} status to '{target_status}'")
            else:
                logger.error(f"Failed to update status for order {order_id}")
        else:
            logger.info(f"Order {order_id} is already in status '{target_status}', skipping update.")

        self._save_processed_order(order_id)

    async def sync_products_from_prom(self):
        started = time.time()
        products_map = {}

        for client in self.prom_clients:
            page = 1
            list_has_notes = None
            while True:
                items = client.list_products(page=page, limit=100)
                if not items:
                    break

                if list_has_notes is None:
                    list_has_notes = any((p.get("private_note") or p.get("personal_notes")) for p in items)

                for p in items:
                    product_id = p.get("id") or p.get("product_id")
                    sku = str(p.get("sku") or "").strip()
                    note = p.get("private_note") or p.get("personal_notes") or ""
                    note = str(note).strip() if note is not None else ""

                    if list_has_notes and sku and note:
                        products_map[sku] = note
                        continue

                    if not product_id:
                        continue

                    product = client.get_product(product_id)
                    if not product:
                        continue

                    sku2 = sku or str(product.get("sku") or "").strip()
                    if not sku2:
                        continue

                    note2 = product.get("private_note") or product.get("personal_notes") or ""
                    note2 = str(note2).strip() if note2 is not None else ""
                    if not note2 and product.get("variation_base_id"):
                        parent_id = product.get("variation_base_id")
                        parent = client.get_product(parent_id)
                        if parent:
                            note2 = parent.get("private_note") or parent.get("personal_notes") or ""
                            note2 = str(note2).strip() if note2 is not None else ""

                    if not note2:
                        continue

                    products_map[sku2] = note2

                page += 1
                if page > 500:
                    break
                if len(items) < 100:
                    break

        merged = self.local_notes.copy()
        changed = 0
        for k, v in products_map.items():
            if merged.get(k) != v:
                merged[k] = v
                changed += 1

        json_path = self._get_json_db_path()
        os.makedirs(os.path.dirname(os.path.abspath(json_path)), exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)

        self.local_notes = merged
        elapsed = int(time.time() - started)
        return {"changed": changed, "fetched": len(products_map), "total": len(merged), "elapsed_s": elapsed}

    async def check_telegram_updates(self):
        """
        Check for new files (prom_import_data.json) sent to the bot/chat
        and update local_notes if found.
        """
        try:
            updates = await self.bot.get_updates(offset=self.last_update_id + 1, timeout=5)
            for update in updates:
                self.last_update_id = update.update_id
                
                # Check for Message or Channel Post
                msg = update.message or update.channel_post
                if not msg:
                    continue

                # Handle /products command
                if msg.text and msg.text.strip().startswith("/products"):
                    count = len(self.local_notes)
                    await self.bot.send_message(
                        chat_id=msg.chat_id, 
                        text=f"📦 В базе загружено товаров: {count}"
                    )
                    logger.info(f"Responded to /products command. Count: {count}")

                if msg.text and msg.text.strip().startswith("/sync_products"):
                    await self.bot.send_message(chat_id=msg.chat_id, text="🔄 Начинаю синхронизацию товаров с Prom...")
                    try:
                        res = await self.sync_products_from_prom()
                        await self.bot.send_message(
                            chat_id=msg.chat_id,
                            text=f"✅ Синхронизация завершена. Обновлено: {res['changed']}. Получено: {res['fetched']}. Всего: {res['total']}. Время: {res['elapsed_s']}с."
                        )
                    except Exception as e:
                        logger.error(f"Sync from Prom failed: {e}")
                        await self.bot.send_message(chat_id=msg.chat_id, text=f"❌ Ошибка синхронизации с Prom: {e}")

                # Handle File Upload
                if msg.document:
                    doc = msg.document
                    logger.info(f"Checking document: {doc.file_name}")
                    
                    if doc.file_name == "prom_import_data.json":
                        logger.info("📥 Received new database update from Telegram!")
                        
                        # Download file to a temporary location first
                        temp_path = "temp_import.json"
                        file_obj = await self.bot.get_file(doc.file_id)
                        await file_obj.download_to_drive(custom_path=temp_path)
                        
                        # Load new data
                        try:
                            with open(temp_path, "r", encoding="utf-8") as f:
                                new_data = json.load(f)
                            
                            # Load existing data
                            current_data = self.local_notes.copy()
                            
                            # Merge: update existing keys, add new ones
                            current_data.update(new_data)
                            
                            # Save merged data back to the main file
                            json_path = self._get_json_db_path()
                            # Ensure dir exists
                            os.makedirs(os.path.dirname(os.path.abspath(json_path)), exist_ok=True)
                            
                            with open(json_path, "w", encoding="utf-8") as f:
                                json.dump(current_data, f, ensure_ascii=False, indent=2)
                                
                            # Update memory
                            self.local_notes = current_data
                            
                            # Cleanup temp file
                            if os.path.exists(temp_path):
                                os.remove(temp_path)
                            
                            # Confirm receipt
                            chat_id = msg.chat_id
                            await self.bot.send_message(
                                chat_id=chat_id,
                                text=f"✅ База обновлена! Добавлено/обновлено: {len(new_data)}. Всего товаров: {len(self.local_notes)}."
                            )
                        except Exception as e:
                            logger.error(f"Failed to merge JSON: {e}")
                            await self.bot.send_message(chat_id=msg.chat_id, text=f"❌ Ошибка при обновлении базы: {e}")
                    else:
                        logger.info(f"Ignored document with name: {doc.file_name}")
                        
        except Exception as e:
            # Don't crash on Telegram network errors
            logger.error(f"Error checking Telegram updates: {e}")

    async def run(self):
        # Send startup notification
        try:
            me = await self.bot.get_me()
            await self.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, 
                text=f"🤖 Бот {me.first_name} запущен и готов к работе!\nМониторинг заказов активирован.\n📥 Отправьте мне 'prom_import_data.json' для обновления базы."
            )
            logger.info("Startup message sent successfully")
        except Exception as e:
            logger.error(f"Failed to send startup message: {e}")

        while True:
            try:
                # Parallel check is risky without async safety, but sequential is fine
                await self.check_telegram_updates()
                await self.auto_accept_new_orders()
                await self.process_orders()
                
                # Disable startup mode after first full cycle
                if self.startup_mode:
                    self.startup_mode = False
                    logger.info("Startup phase complete. Normal monitoring active.")

            except Exception as e:
                logger.error(f"Error in main loop: {e}")
            
            # Reduce sleep interval since we are polling now
            # Telegram long polling (timeout=5 in get_updates) acts as the sleep
            # But we still need a small sleep to prevent CPU spin if polling fails fast
            await asyncio.sleep(5)

if __name__ == "__main__":
    # Start Web Server in a separate thread (for Render)
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()
    
    processor = OrderProcessor()
    processor_ref = processor
    asyncio.run(processor.run())
