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
CHECK_INTERVAL = 30  
PROCESSED_ORDERS_FILE = "processed_orders.json"
TARGET_STATUSES = ["received", "processing", "custom-133340"]  
AUTO_ACCEPT_NEW = True

class OrderProcessor:
    def __init__(self):
        self.prom_clients = [PromClient(token) for token in PROM_API_TOKENS]
        logger.info(f"Loaded {len(self.prom_clients)} Prom.ua shops/tokens.")
        
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN)
        self.processed_orders = self._load_processed_orders()
        self.local_notes = self._load_local_notes()
        self.suppliers_map = self._load_suppliers_map() # Загрузка словаря поставщиков
        self.last_update_id = 0 
        self.startup_mode = True 
        
        if not self.prom_clients:
            logger.warning("No Prom API tokens found! Please check .env file.")

        if not self.processed_orders:
            logger.info("First run detected. Marking existing orders as processed to avoid spam.")
            self._mark_current_orders_processed()

    def _load_suppliers_map(self):
        """Загрузка словаря для замены длинных имен поставщиков на адреса"""
        map_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "suppliers_map.json")
        if not os.path.exists(map_path):
            map_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "suppliers_map.json")
            
        if os.path.exists(map_path):
            try:
                with open(map_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logger.info(f"Loaded {len(data)} suppliers from map.")
                    return data
            except Exception as e:
                logger.error(f"Failed to load suppliers_map.json: {e}")
        else:
            logger.warning("suppliers_map.json not found. Raw supplier names will be used.")
        return {}

    def _get_json_db_path(self):
        env_path = os.getenv("SHARED_DATA_PATH")
        if env_path:
            return env_path
            
        sibling_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 
                                 "prom_automation", "prom_import_data.json")
        sibling_dir = os.path.dirname(sibling_path)
        if os.path.exists(sibling_dir):
            return sibling_path
            
        return "prom_import_data.json"

    def _load_local_notes(self):
        notes = {}
        json_path = self._get_json_db_path()
        
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    notes = json.load(f)
                return notes
            except Exception as e:
                pass
        else:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(json_path)), exist_ok=True)
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump({}, f, ensure_ascii=False, indent=2)
            except Exception as e:
                pass

        file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 
                                 "prom_automation", "prom_import_fixed.xlsx")
        
        if os.path.exists(file_path):
            try:
                df = pd.read_excel(file_path)
                if 'Код_товару' in df.columns and 'Личные_заметки' in df.columns:
                    for _, row in df.iterrows():
                        sku = str(row['Код_товару']).strip()
                        note = str(row['Личные_заметки'])
                        if sku and note and note.lower() != 'nan':
                            notes[sku] = note
            except Exception as e:
                pass
                
        return notes

    def _mark_current_orders_processed(self):
        for client in self.prom_clients:
            try:
                for status in TARGET_STATUSES:
                    orders = client.get_orders(status=status)
                    if orders:
                        for order in orders:
                            self.processed_orders.add(str(order.get("id")))
            except Exception as e:
                pass
        
        with open(PROCESSED_ORDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(self.processed_orders), f)

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
        delivery_data = order.get("delivery_provider_data", {})
        if delivery_data:
            for key in ["declaration_number", "ttn", "invoice_number"]:
                if val := delivery_data.get(key):
                    return val
        return order.get("delivery_note", "")

    def _parse_private_note(self, note):
        data = {}
        if not note: return data
            
        parts = [p.strip() for p in note.split("|")]
        supplier_parts = []
        
        for part in parts:
            part_lower = part.lower()
            if part_lower.startswith("price:") or part_lower.startswith("цена:"):
                data["purchase_price"] = part.split(":", 1)[1].strip()
            elif part_lower.startswith("art:") or part_lower.startswith("арт:"):
                data["model"] = part.split(":", 1)[1].strip()
            else:
                clean_part = part
                if part_lower.startswith("supplier:") or part_lower.startswith("поставщик:"):
                    clean_part = part.split(":", 1)[1].strip()
                if clean_part:
                    supplier_parts.append(clean_part)
        
        data["supplier"] = " | ".join(supplier_parts)
        return data

    async def auto_accept_new_orders(self):
        if not AUTO_ACCEPT_NEW: return

        for client in self.prom_clients:
            try:
                orders = client.get_orders(status="pending")
                for order in orders:
                    order_id = order.get("id")
                    target_status = "custom-133340"
                    if not client.set_order_status(order_id, target_status):
                        client.set_order_status(order_id, "received")
            except Exception as e:
                pass

    async def process_orders(self):
        for client in self.prom_clients:
            try:
                for status in TARGET_STATUSES:
                    orders = client.get_orders(status=status)
                    for order in orders:
                        await self._process_single_order(client, order)
            except Exception as e:
                pass

    async def _process_single_order(self, client, order):
        order_id = str(order.get("id"))
        if order_id in self.processed_orders:
            return

        ttn = self._extract_ttn(order)
        if not ttn: return 

        if self.startup_mode:
            self._save_processed_order(order_id)
            return

        client_first_name = order.get("client_first_name", "")
        client_last_name = order.get("client_last_name", "")
        client_name = f"{client_first_name} {client_last_name}".strip()
        
        for item in order.get("products", []):
            product_id = item.get("id")
            product_data = client.get_product(product_id)
            private_note = ""
            
            if product_data:
                private_note = product_data.get("private_note") or product_data.get("personal_notes") or ""
                if not private_note and product_data.get("variation_base_id"):
                    parent_id = product_data.get("variation_base_id")
                    parent_data = client.get_product(parent_id)
                    if parent_data:
                        private_note = parent_data.get("private_note") or parent_data.get("personal_notes") or ""

            if not private_note:
                sku = item.get("sku")
                if sku:
                    private_note = self.local_notes.get(sku, "")
                    if not private_note and "-" in sku:
                        base_sku = sku.rsplit("-", 1)[0]
                        for db_sku, db_note in self.local_notes.items():
                            if db_sku.startswith(base_sku):
                                private_note = db_note
                                break

            note_data = self._parse_private_note(private_note)
            
            # --- ЗАМЕНА ИМЕНИ ПОСТАВЩИКА ПО СЛОВАРЮ ---
            raw_supplier = note_data.get("supplier", "Неизвестный").strip()
            supplier_address = raw_supplier
            
            for map_key, map_value in self.suppliers_map.items():
                if map_key.lower() in raw_supplier.lower():
                    supplier_address = map_value
                    break

            model = note_data.get("model") or item.get("sku") or "Арт. не найден"
            
            # --- БЕРЕМ ЦЕНУ ЗАКУПКИ ИЗ ЗАМЕТОК ---
            purchase_price = note_data.get("purchase_price", "Не указана")

            item_name = item.get("name", "")
            quantity = item.get("quantity", 1)
            
            # --- НОВЫЙ УМНЫЙ ПАРСЕР РАЗМЕРА И ЦВЕТА ---
            match = re.search(r'\(([^)]+)\)([^()]*)$', item_name)
            
            if match:
                inside_parens = match.group(1).strip()
                after_parens = match.group(2).strip()
                
                if after_parens:
                    size_color_line = after_parens
                else:
                    size_color_line = inside_parens
            else:
                parts = [p.strip() for p in item_name.split(',')]
                size_color_line = " - ".join(parts[-2:]) if len(parts) >= 3 else item_name
                
            if quantity > 1:
                size_color_line += f" ({quantity} шт.)"

            # ИДЕАЛЬНЫЙ ФОРМАТ СООБЩЕНИЯ С ПУСТОЙ СТРОКОЙ
            message = (
                f"{supplier_address}\n"
                f"{size_color_line}\n"
                f"Мод: {model}\n"
                f"Цена: {purchase_price}\n\n"
                f"{ttn} {client_name}"
            )
            
            image_url = None
            if product_data:
                images = product_data.get("images", [])
                if images:
                    image_url = images[0].get("url")

            # Отправка в Telegram
            try:
                sent_photo = False
                if image_url:
                    try:
                        await self.bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=image_url, caption=message)
                        sent_photo = True
                    except Exception:
                        pass
                
                if not sent_photo:
                        await self.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
            except Exception as e:
                logger.error(f"Failed to send Telegram message: {e}")
        
        current_status = order.get("status")
        target_status = "custom-133340"
        if current_status != target_status:
            client.set_order_status(order_id, target_status)

        self._save_processed_order(order_id)

    async def sync_products_from_prom(self):
        started = time.time()
        products_map = {}

        for client in self.prom_clients:
            page = 1
            list_has_notes = None
            while True:
                items = client.list_products(page=page, limit=100)
                if not items: break

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
                    if not product_id: continue

                    product = client.get_product(product_id)
                    if not product: continue

                    sku2 = sku or str(product.get("sku") or "").strip()
                    if not sku2: continue

                    note2 = product.get("private_note") or product.get("personal_notes") or ""
                    note2 = str(note2).strip() if note2 is not None else ""
                    if not note2 and product.get("variation_base_id"):
                        parent_id = product.get("variation_base_id")
                        parent = client.get_product(parent_id)
                        if parent:
                            note2 = parent.get("private_note") or parent.get("personal_notes") or ""
                            note2 = str(note2).strip() if note2 is not None else ""

                    if not note2: continue
                    products_map[sku2] = note2

                page += 1
                if page > 500 or len(items) < 100: break

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
        try:
            updates = await self.bot.get_updates(offset=self.last_update_id + 1, timeout=5)
            for update in updates:
                self.last_update_id = update.update_id
                
                msg = update.message or update.channel_post
                if not msg: continue

                if msg.text and msg.text.strip().startswith("/products"):
                    count = len(self.local_notes)
                    await self.bot.send_message(chat_id=msg.chat_id, text=f"📦 В базе загружено товаров: {count}")

                if msg.text and msg.text.strip().startswith("/sync_products"):
                    await self.bot.send_message(chat_id=msg.chat_id, text="🔄 Начинаю синхронизацию товаров с Prom...")
                    try:
                        res = await self.sync_products_from_prom()
                        await self.bot.send_message(chat_id=msg.chat_id, text=f"✅ Синхронизация завершена. Обновлено: {res['changed']}. Получено: {res['fetched']}. Всего: {res['total']}. Время: {res['elapsed_s']}с.")
                    except Exception as e:
                        await self.bot.send_message(chat_id=msg.chat_id, text=f"❌ Ошибка синхронизации с Prom: {e}")

                if msg.document:
                    doc = msg.document
                    if doc.file_name == "prom_import_data.json":
                        temp_path = "temp_import.json"
                        file_obj = await self.bot.get_file(doc.file_id)
                        await file_obj.download_to_drive(custom_path=temp_path)
                        try:
                            with open(temp_path, "r", encoding="utf-8") as f:
                                new_data = json.load(f)
                            
                            current_data = self.local_notes.copy()
                            current_data.update(new_data)
                            
                            json_path = self._get_json_db_path()
                            os.makedirs(os.path.dirname(os.path.abspath(json_path)), exist_ok=True)
                            with open(json_path, "w", encoding="utf-8") as f:
                                json.dump(current_data, f, ensure_ascii=False, indent=2)
                                
                            self.local_notes = current_data
                            if os.path.exists(temp_path): os.remove(temp_path)
                            
                            await self.bot.send_message(chat_id=msg.chat_id, text=f"✅ База обновлена! Добавлено/обновлено: {len(new_data)}. Всего товаров: {len(self.local_notes)}.")
                        except Exception as e:
                            await self.bot.send_message(chat_id=msg.chat_id, text=f"❌ Ошибка при обновлении базы: {e}")
                            
        except Exception as e:
            pass

    async def run(self):
        try:
            me = await self.bot.get_me()
            await self.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, 
                text=f"🤖 Бот {me.first_name} запущен!\nУмный парсер скобок и цена закупки активированы."
            )
        except Exception as e:
            pass

        while True:
            try:
                await self.check_telegram_updates()
                await self.auto_accept_new_orders()
                await self.process_orders()
                
                if self.startup_mode:
                    self.startup_mode = False

            except Exception as e:
                pass
            
            await asyncio.sleep(5)

if __name__ == "__main__":
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()
    
    processor = OrderProcessor()
    processor_ref = processor
    asyncio.run(processor.run())
