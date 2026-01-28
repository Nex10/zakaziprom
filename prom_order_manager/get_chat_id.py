import os
import requests
from dotenv import load_dotenv

def get_chat_id():
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not token or token == "your_bot_token_here":
        print("Ошибка: Сначала укажите TELEGRAM_BOT_TOKEN в файле .env")
        return

    print(f"Используем токен: {token[:5]}...{token[-5:]}")
    
    # Check Bot Identity
    try:
        me_url = f"https://api.telegram.org/bot{token}/getMe"
        me_response = requests.get(me_url)
        me_data = me_response.json()
        if me_data.get("ok"):
            bot_username = me_data["result"]["username"]
            print(f"Бот: @{bot_username} (Убедитесь, что именно этого бота вы добавили в группу)")
        else:
            print(f"Ошибка проверки токена: {me_data}")
    except Exception as e:
        print(f"Ошибка подключения: {e}")

    print("Попытка получить обновления...")
    
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        response = requests.get(url)
        data = response.json()
        
        if not data.get("ok"):
            print(f"Ошибка API: {data.get('description')}")
            return

        updates = data.get("result", [])
        if not updates:
            print("Нет новых сообщений. Добавьте бота в группу и напишите любое сообщение (например 'test').")
            return

        print("\nНайдены чаты:")
        seen_chats = set()
        for update in reversed(updates):
            if "message" in update:
                chat = update["message"]["chat"]
            elif "my_chat_member" in update:
                chat = update["my_chat_member"]["chat"]
            else:
                continue
                
            chat_id = chat["id"]
            chat_title = chat.get("title", chat.get("username", "Личный чат"))
            chat_type = chat["type"]
            
            if chat_id not in seen_chats:
                print(f"ID: {chat_id} | Название: {chat_title} | Тип: {chat_type}")
                seen_chats.add(chat_id)
                
    except Exception as e:
        print(f"Произошла ошибка: {e}")

if __name__ == "__main__":
    get_chat_id()
