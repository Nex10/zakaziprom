import logging
import json
import sys
from config import PROM_API_TOKENS
from prom_client import PromClient

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _parse_private_note(note):
    data = {}
    if not note:
        return data
        
    parts = [p.strip() for p in note.split("|")]
    for part in parts:
        part_lower = part.lower()
        if part_lower.startswith("price:") or part_lower.startswith("цена:"):
            data["purchase_price"] = part.split(":", 1)[1].strip()
        elif part_lower.startswith("supplier:") or part_lower.startswith("поставщик:"):
            data["supplier"] = part.split(":", 1)[1].strip()
        elif part_lower.startswith("art:") or part_lower.startswith("арт:"):
            data["model"] = part.split(":", 1)[1].strip()
    return data

def main():
    if not PROM_API_TOKENS:
        print("No tokens found.")
        return

    client = PromClient(PROM_API_TOKENS[0])
    
    # Try different statuses to find the latest order
    statuses = ["pending", "received", "processing"]
    all_orders = []
    
    for status in statuses:
        orders = client.get_orders(status=status)
        if orders:
            print(f"Found {len(orders)} orders with status '{status}'")
            all_orders.extend(orders)
            
    if not all_orders:
        print("No orders found.")
        return

    # Sort by ID descending to get the newest
    all_orders.sort(key=lambda x: x.get("id"), reverse=True)
    latest_order = all_orders[0]
    
    order_id = latest_order.get("id")
    print(f"\n--- Latest Order ID: {order_id} ---")
    print(f"Status: {latest_order.get('status')}")
    print(f"Client: {latest_order.get('client_first_name')} {latest_order.get('client_last_name')}")
    
    delivery_data = latest_order.get("delivery_provider_data", {})
    ttn = None
    for key in ["declaration_number", "ttn", "invoice_number"]:
        if val := delivery_data.get(key):
            ttn = val
            break
    print(f"TTN: {ttn if ttn else 'NOT FOUND (Message requires TTN)'}")

    for item in latest_order.get("products", []):
        product_id = item.get("id")
        name = item.get("name")
        print(f"\nProcessing Product: {name} (ID: {product_id})")
        
        product_data = client.get_product(product_id)
        private_note = ""
        if product_data:
            private_note = product_data.get("private_note") or product_data.get("personal_notes") or ""
            if not private_note and product_data.get("variation_base_id"):
                 parent_id = product_data.get("variation_base_id")
                 print(f"Checking parent {parent_id}...")
                 parent_data = client.get_product(parent_id)
                 if parent_data:
                     private_note = parent_data.get("private_note") or parent_data.get("personal_notes") or ""

        print(f"Raw Private Note: '{private_note}'")
        
        # Dump full product data for debugging
        print("Full Product Data Keys:", product_data.keys() if product_data else "None")
        if product_data:
            print(f"private_note field: '{product_data.get('private_note')}'")
            print(f"personal_notes field: '{product_data.get('personal_notes')}'")
            print(json.dumps(product_data, indent=2, ensure_ascii=False))
        
        parsed = _parse_private_note(private_note)
        print("Parsed Data:")
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
        
        if parsed.get("supplier"):
            print("✅ Supplier found!")
        else:
            print("❌ Supplier NOT found in note.")

if __name__ == "__main__":
    main()
