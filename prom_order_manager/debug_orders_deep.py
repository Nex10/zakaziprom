import logging
import json
import requests
from config import PROM_API_TOKENS, PROM_API_HOST

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    if not PROM_API_TOKENS:
        print("No tokens found.")
        return

    token = PROM_API_TOKENS[0]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    # 1. Fetch recent orders (ANY status)
    print("--- Fetching recent orders (limit 5) ---")
    url = f"{PROM_API_HOST}/orders/list"
    try:
        # Just get list, usually returns recent first
        response = requests.get(url, headers=headers, params={"limit": 5})
        response.raise_for_status()
        orders = response.json().get("orders", [])
        
        for order in orders:
            print(f"Order ID: {order['id']} | Status: {order['status']} | Date: {order['date_created']}")
            for item in order.get("products", []):
                print(f"  - Product: {item['name']} (SKU: {item['sku']}, ID: {item['id']})")
                
                # Check product details immediately
                p_url = f"{PROM_API_HOST}/products/{item['id']}"
                p_res = requests.get(p_url, headers=headers)
                if p_res.status_code == 200:
                    p_data = p_res.json().get("product", {})
                    note = p_data.get("private_note") or p_data.get("personal_notes") or "None"
                    print(f"    -> Product Private Note: '{note}'")
                    
                    # If empty, check parent
                    if not note or note == "None":
                        parent_id = p_data.get("variation_base_id")
                        if parent_id:
                            print(f"    -> Checking Parent {parent_id}...")
                            parent_res = requests.get(f"{PROM_API_HOST}/products/{parent_id}", headers=headers)
                            if parent_res.status_code == 200:
                                parent_data = parent_res.json().get("product", {})
                                p_note = parent_data.get("private_note") or parent_data.get("personal_notes") or "None"
                                print(f"    -> Parent Private Note: '{p_note}'")
                            else:
                                print(f"    -> Parent fetch failed: {parent_res.status_code}")
                        else:
                            print("    -> No parent (not a variation or base)")
                else:
                    print(f"    -> Fetch failed: {p_res.status_code}")

            print("-" * 30)

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
