import logging
import sys
import requests
from config import PROM_API_TOKENS, PROM_API_HOST

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

def get_product_data(product_id):
    if not PROM_API_TOKENS:
        return None
    token = PROM_API_TOKENS[0]
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{PROM_API_HOST}/products/{product_id}"
    params = {"include_private_notes": 1}
    try:
        resp = requests.get(url, headers=headers, params=params)
        if resp.status_code == 200:
            return resp.json().get("product", {})
    except Exception as e:
        print(f"Error fetching {product_id}: {e}")
    return None

def check_product_note(product_id):
    print(f"\n--- Проверка товара ID: {product_id} ---")
    data = get_product_data(product_id)
    
    if not data:
        print("❌ Товар не найден или ошибка API.")
        return

    name = data.get("name", "Неизвестно")
    print(f"Название: {name}")
    print(f"Тип: {'Вариация' if data.get('is_variation') else 'Основной товар'}")
    
    # Check notes
    note = data.get("private_note") or data.get("personal_notes")
    if note:
        print(f"✅ НАЙДЕНА Личная заметка: '{note}'")
    else:
        print("❌ Личная заметка ОТСУТСТВУЕТ в ответе API.")
        
        # Check parent if variation
        parent_id = data.get("variation_base_id")
        if parent_id:
            print(f"\n🔎 Это вариация. Проверяем родительский товар (ID: {parent_id})...")
            try:
                token = PROM_API_TOKENS[0]
                headers = {"Authorization": f"Bearer {token}"}
                params = {"include_private_notes": 1}
                url_parent = f"{PROM_API_HOST}/products/{parent_id}"
                resp_parent = requests.get(url_parent, headers=headers, params=params)
                if resp_parent.status_code == 200:
                    parent_data = resp_parent.json().get("product", {})
                    p_note = parent_data.get("private_note") or parent_data.get("personal_notes")
                    if p_note:
                        print(f"✅ НАЙДЕНА заметка в родительском товаре: '{p_note}'")
                    else:
                        print("❌ В родительском товаре заметки тоже нет.")
                else:
                    print(f"Ошибка получения родителя: {resp_parent.status_code}")
            except Exception as e:
                print(f"Ошибка проверки родителя: {e}")
        
        print("\nУбедитесь, что вы заполнили поле 'Личная заметка' в карточке товара.")

if __name__ == "__main__":
    TARGET_ID = 2898574829
    check_product_note(TARGET_ID)
