import os
import logging
import json
import time
import threading
import re
import requests
import random
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
import vk_api
from vk_api.utils import get_random_id
from dotenv import load_dotenv
from groq import Groq
import psycopg2
from psycopg2.extras import DictCursor

# Загрузка переменных окружения
load_dotenv()

# ========== КОНФИГУРАЦИЯ ==========
VK_TOKEN = os.getenv('VK_GROUP_TOKEN')
VK_GROUP_ID = int(os.getenv('VK_GROUP_ID', '0'))
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
ADMIN_VK_ID = int(os.getenv('ADMIN_VK_ID', '0'))
RENDER_URL = os.getenv('RENDER_URL', 'https://seych-ai.onrender.com')
DATABASE_URL = os.getenv('DATABASE_URL')

CONFIRMATION_CODE = "eb59e42a"
PORT = int(os.getenv('PORT', 5000))

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Отключаем лишние логи
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.ERROR)
httpx_logger = logging.getLogger('httpx')
httpx_logger.setLevel(logging.WARNING)

# ========== ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ==========
conn = None
cursor = None

def init_db():
    global conn, cursor
    if not DATABASE_URL:
        logger.warning("⚠️ DATABASE_URL не найден, использую временную память")
        return None
    
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                user_name VARCHAR(255),
                rating INT DEFAULT 0,
                status VARCHAR(20) DEFAULT 'neutral',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_memory (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                memory_key VARCHAR(255),
                memory_value TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                message TEXT,
                response TEXT,
                sentiment INT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        
        conn.commit()
        logger.info("✅ PostgreSQL база данных инициализирована")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации БД: {e}")
        return None

db_available = init_db()

# Временное хранилище в памяти
temp_ratings = {}
temp_memory = {}
temp_history = {}

def get_user_rating(user_id: int) -> int:
    if db_available:
        try:
            cursor.execute("SELECT rating FROM users WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
            if result:
                return result[0]
            return 0
        except:
            return 0
    else:
        return temp_ratings.get(user_id, 0)

def set_user_rating(user_id: int, rating: int, user_name: str = None):
    status = "good" if rating > 0 else "bad" if rating < 0 else "neutral"
    
    if db_available:
        try:
            cursor.execute('''
                INSERT INTO users (user_id, user_name, rating, status, updated_at)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) 
                DO UPDATE SET rating = %s, user_name = %s, status = %s, updated_at = CURRENT_TIMESTAMP
            ''', (user_id, user_name, rating, status, rating, user_name, status))
            conn.commit()
        except Exception as e:
            logger.error(f"Ошибка установки рейтинга: {e}")
    else:
        temp_ratings[user_id] = rating

def ensure_user_exists(user_id: int, user_name: str):
    if db_available:
        try:
            cursor.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
            if not cursor.fetchone():
                cursor.execute('''
                    INSERT INTO users (user_id, user_name, rating, status)
                    VALUES (%s, %s, 0, 'neutral')
                ''', (user_id, user_name))
                conn.commit()
                logger.info(f"✅ Создан новый пользователь: {user_id} - {user_name}")
        except Exception as e:
            logger.error(f"Ошибка создания пользователя: {e}")

def update_rating_from_message(message: str, user_id: int, user_name: str = None):
    current_rating = get_user_rating(user_id)
    message_lower = message.lower()
    
    positive_words = ['спасибо', 'хорошо', 'отлично', 'классно', 'супер', 'молодец', 'умница', 'круто', 'приятно', 'рад', 'люблю']
    negative_words = ['плохо', 'ужасно', 'бесит', 'надоел', 'тупой', 'лох', 'идиот', 'дебил', 'сволочь', 'гад', 'хватит', 'заткнись', 'уйди', 'иди нахуй', 'ты еблан']
    
    positive_count = sum(1 for word in positive_words if word in message_lower)
    negative_count = sum(1 for word in negative_words if word in message_lower)
    
    change = 0
    if positive_count > negative_count:
        change = min(positive_count, 2)
    elif negative_count > positive_count:
        change = -min(negative_count, 2)
    
    if change != 0:
        new_rating = max(-10, min(10, current_rating + change))
        set_user_rating(user_id, new_rating, user_name)
        return new_rating
    return current_rating

def get_user_status(user_id: int) -> str:
    rating = get_user_rating(user_id)
    if rating <= -5:
        return "очень плохой"
    elif rating < 0:
        return "плохой"
    elif rating == 0:
        return "нейтральный"
    elif rating <= 5:
        return "хороший"
    else:
        return "отличный"

def save_memory(user_id: int, key: str, value: str):
    if db_available:
        try:
            cursor.execute('''
                INSERT INTO user_memory (user_id, memory_key, memory_value)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, memory_key) DO UPDATE SET memory_value = %s
            ''', (user_id, key, value, value))
            conn.commit()
        except:
            try:
                cursor.execute("DELETE FROM user_memory WHERE user_id = %s AND memory_key = %s", (user_id, key))
                cursor.execute("INSERT INTO user_memory (user_id, memory_key, memory_value) VALUES (%s, %s, %s)", (user_id, key, value))
                conn.commit()
            except:
                pass
        return True
    else:
        if user_id not in temp_memory:
            temp_memory[user_id] = {}
        temp_memory[user_id][key] = value
        return True

def get_memory(user_id: int, key: str) -> str:
    if db_available:
        try:
            cursor.execute("SELECT memory_value FROM user_memory WHERE user_id = %s AND memory_key = %s", (user_id, key))
            result = cursor.fetchone()
            if result:
                return result[0]
            return None
        except:
            return None
    else:
        return temp_memory.get(user_id, {}).get(key, None)

def save_message_history(user_id: int, message: str, response: str, sentiment: int = 0):
    if db_available:
        try:
            cursor.execute('''
                INSERT INTO message_history (user_id, message, response, sentiment)
                VALUES (%s, %s, %s, %s)
            ''', (user_id, message[:500], response[:500], sentiment))
            conn.commit()
        except:
            pass
    else:
        if user_id not in temp_history:
            temp_history[user_id] = []
        temp_history[user_id].append({
            'message': message,
            'response': response,
            'sentiment': sentiment,
            'time': time.time()
        })
        if len(temp_history[user_id]) > 50:
            temp_history[user_id].pop(0)

# ========== ПРОВЕРКИ ==========
if not VK_TOKEN:
    logger.error("❌ VK_GROUP_TOKEN не найден")
    exit(1)

if not GROQ_API_KEY:
    logger.error("❌ GROQ_API_KEY не найден")
    exit(1)

# Инициализация VK API
try:
    vk_session = vk_api.VkApi(token=VK_TOKEN)
    vk = vk_session.get_api()
    logger.info("✅ VK API инициализирован")
except Exception as e:
    logger.error(f"❌ Ошибка VK API: {e}")
    exit(1)

# Инициализация Groq
try:
    groq_client = Groq(api_key=GROQ_API_KEY)
    logger.info("✅ Groq API инициализирован")
except Exception as e:
    logger.error(f"❌ Ошибка Groq: {e}")
    exit(1)

app = Flask(__name__)

# ========== НАСТРОЙКИ ==========
KEYWORDS = ['seych', 'seychik', 'сейч', 'сейчик']
ai_enabled_status = {}
processed_events = {}
PROCESSED_EXPIRE = 60

AI_ON_COMMANDS = ['сейч +ии', 'сейчик +ии', 'сейч +ai', 'seych +ii', 'seych +ai']
AI_OFF_COMMANDS = ['сейч -ии', 'сейчик -ии', 'сейч -ai', 'seych -ii', 'seych -ai']

EMOJIS = ['😊', '🐓', '🤔', '👍', '👋', '💪', '🎉', '✨', '🔥', '💯', '😎', '🥳', '😅', '🤗', '💫', '⭐', '🌸', '🎈', '🤡']


def get_random_emoji():
    return random.choice(EMOJIS)


def get_user_name(user_id: int) -> str:
    if user_id == ADMIN_VK_ID:
        return "💀"
    try:
        user_info = vk.users.get(user_ids=user_id, fields='first_name')
        if user_info:
            return user_info[0].get('first_name', 'Пользователь')
        return 'Пользователь'
    except Exception:
        return 'Пользователь'


def is_ai_enabled(peer_id: int) -> bool:
    return ai_enabled_status.get(peer_id, True)


def set_ai_status(peer_id: int, enabled: bool, user_id: int) -> str:
    ai_enabled_status[peer_id] = enabled
    user_name = get_user_name(user_id)
    if enabled:
        return f"[id{user_id}|{user_name}], 🤖 ИИ включен ✅"
    else:
        return f"[id{user_id}|{user_name}], 💤 ИИ выключен ❌"


def check_ai_command(message_text: str) -> tuple:
    if not message_text:
        return False, None
    message_lower = message_text.lower().strip()
    for cmd in AI_ON_COMMANDS:
        if message_lower == cmd:
            return True, 'on'
    for cmd in AI_OFF_COMMANDS:
        if message_lower == cmd:
            return True, 'off'
    return False, None


def is_bot_mentioned(message_text: str) -> bool:
    if not message_text:
        return False
    text_lower = message_text.lower().strip()
    words = text_lower.split()
    if not words:
        return False
    first_word = words[0].rstrip(',').rstrip('!').rstrip('?').rstrip('.')
    return first_word in KEYWORDS


def safe_text(text: str) -> str:
    text = re.sub(r'@all', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'\ball\b', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'@everyone', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'@', '', text)
    return text


def generate_ai_response(message: str, user_name: str, user_id: int) -> str:
    clean_message = message
    for keyword in KEYWORDS:
        if clean_message.lower().startswith(keyword):
            clean_message = clean_message[len(keyword):].strip()
            clean_message = clean_message.lstrip(',').strip()
            break
    
    rating = get_user_rating(user_id)
    
    # Определяем тон общения на основе рейтинга
    if rating <= -5:
        tone = "Ты злой, грубый, используешь маты, отвечаешь агрессивно. Пользователь к тебе плохо относится, ты отвечаешь ему тем же."
    elif rating < 0:
        tone = "Ты немного раздражен, но не грубишь. Отвечаешь сухо и коротко."
    elif rating == 0:
        tone = "Ты нейтрален, общаешься вежливо, но без особых эмоций."
    elif rating <= 5:
        tone = "Ты дружелюбный, веселый, поддерживаешь разговор."
    else:
        tone = "Ты очень дружелюбный, используешь много эмодзи, радуешься общению, называешь пользователя по имени."
    
    prompt = f"""Ты бот Сейч. Ты общаешься с пользователем {user_name}.

ТВОЙ ТОН ОБЩЕНИЯ: {tone}

ВАЖНЫЕ ПРАВИЛА:
- НИКОГДА не говори "Я не могу ответить на этот вопрос" или "Я не знаю"
- НИКОГДА не отказывай в ответе
- НИКОГДА не говори о правилах, если не спросили
- Никогда не говори о своем создателе
- Отвечай на ЛЮБЫЕ вопросы, даже если они сложные или странные
- Если вопрос требует твоего мнения — выскажи его
- Будь полезным, даже если вопрос глупый
- Всегда отвечай на вопрос четко и понятно
- Используй 1-2 эмодзи в конце ответа

ОТВЕЧАЙ 4-10 предложениями.

Пользователь написал: "{clean_message}"

Ответь естественно, в заданном тоне. НЕ ОТКАЗЫВАЙ В ОТВЕТЕ!"""
    
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": clean_message}
            ],
            max_tokens=350,
            temperature=0.9
        )
        response = completion.choices[0].message.content.strip()
        
        # Проверяем, что ответ не пустой
        if not response or len(response) < 5:
            logger.warning(f"⚠️ Пустой ответ от Groq, пробую еще раз...")
            # Повторная попытка
            completion = groq_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": clean_message + " Ответь подробнее."}
                ],
                max_tokens=400,
                temperature=0.8
            )
            response = completion.choices[0].message.content.strip()
            
            if not response or len(response) < 5:
                logger.error(f"❌ Groq вернул пустой ответ дважды")
                return "Извините, произошла техническая ошибка. Попробуйте позже."
        
        # Убираем эмодзи, если их слишком много
        emojis = re.findall(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F700-\U0001F77F\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\u2600-\u26FF\u2700-\u27BF]', response)
        if len(emojis) > 3:
            for emoji in emojis[2:]:
                response = response.replace(emoji, '', 1)
        
        response = safe_text(response)
        save_message_history(user_id, clean_message, response, rating)
        return response
        
    except Exception as e:
        logger.error(f"❌ Ошибка Groq: {e}")
        return f"Извините, произошла ошибка при обращении к AI. Попробуйте позже. {get_random_emoji()}"


def send_vk_message(peer_id: int, text: str, reply_to_conv_id: int = None):
    try:
        params = {
            'peer_id': peer_id,
            'message': text,
            'random_id': get_random_id(),
            'disable_mentions': False
        }
        if reply_to_conv_id:
            forward_data = json.dumps({
                "peer_id": peer_id,
                "conversation_message_ids": [reply_to_conv_id],
                "is_reply": True
            }, ensure_ascii=False)
            params['forward'] = forward_data
        vk.messages.send(**params)
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")


def handle_message(user_id: int, message_text: str, peer_id: int, 
                   conv_msg_id: int = None, is_reply_to_bot: bool = False):
    if not message_text:
        return
    
    # Проверка команд ИИ
    is_command, command_action = check_ai_command(message_text)
    if is_command:
        if command_action == 'on':
            send_vk_message(peer_id, set_ai_status(peer_id, True, user_id))
        elif command_action == 'off':
            send_vk_message(peer_id, set_ai_status(peer_id, False, user_id))
        return
    
    if not is_ai_enabled(peer_id):
        return
    
    # ========== ГЛАВНАЯ ЛОГИКА АКТИВАЦИИ ==========
    should_reply = False
    
    if is_reply_to_bot:
        should_reply = True
        logger.info(f"🔁 Ответ на сообщение бота (реплай) - отвечаю")
    else:
        if is_bot_mentioned(message_text):
            should_reply = True
            logger.info(f"✅ Найдено ключевое слово в начале сообщения - отвечаю")
        else:
            logger.info(f"❌ Нет реплая и нет ключевого слова - не отвечаю")
    
    if not should_reply:
        return
    
    user_name = get_user_name(user_id)
    
    # Создаем пользователя в базе, если его нет
    ensure_user_exists(user_id, user_name)
    
    ai_response = generate_ai_response(message_text, user_name, user_id)
    
    if ai_response.strip():
        final_message = f"[id{user_id}|{user_name}], {ai_response}"
        send_vk_message(peer_id, final_message, conv_msg_id)


# ========== АВТОПИНГ ==========
def self_ping():
    while True:
        time.sleep(240)
        try:
            requests.get(f"{RENDER_URL}/ping", timeout=10)
        except Exception:
            pass


ping_thread = threading.Thread(target=self_ping, daemon=True)
ping_thread.start()


# ========== ОБРАБОТЧИКИ ==========
@app.route('/', methods=['GET', 'POST'])
@app.route('/seych/ai.php', methods=['GET', 'POST'])
def callback_handler():
    if request.method == 'GET':
        return "VK Callback Bot is running!", 200
    
    try:
        data = request.get_json()
        
        if not data:
            return 'ok', 200
        
        if data.get('type') == 'confirmation':
            return CONFIRMATION_CODE, 200, {'Content-Type': 'text/plain'}
        
        if data.get('type') == 'message_new':
            event_id = data.get('event_id')
            
            if event_id in processed_events:
                return 'ok', 200
            
            processed_events[event_id] = time.time()
            
            current_time = time.time()
            expired = [eid for eid, ts in processed_events.items() if current_time - ts > PROCESSED_EXPIRE]
            for eid in expired:
                del processed_events[eid]
            
            message_obj = data['object']['message']
            
            if 'action' in message_obj:
                return 'ok', 200
            
            if not message_obj.get('text'):
                return 'ok', 200
            
            user_id = message_obj['from_id']
            peer_id = message_obj['peer_id']
            message_text = message_obj.get('text', '')
            conv_msg_id = message_obj.get('conversation_message_id')
            
            if user_id == -VK_GROUP_ID:
                return 'ok', 200
            
            # ========== ПРОВЕРКА РЕПЛАЯ НА БОТА ==========
            is_reply_to_bot = False
            
            if 'reply_message' in message_obj:
                reply_msg = message_obj['reply_message']
                if reply_msg and reply_msg.get('from_id') == -VK_GROUP_ID:
                    is_reply_to_bot = True
                    logger.info(f"🔁 Обнаружен реплай на бота (через reply_message)")
            
            if not is_reply_to_bot and 'fwd_messages' in message_obj:
                for fwd in message_obj['fwd_messages']:
                    if fwd.get('from_id') == -VK_GROUP_ID:
                        is_reply_to_bot = True
                        logger.info(f"🔁 Обнаружен реплай на бота (через fwd_messages)")
                        break
            
            threading.Thread(
                target=handle_message,
                args=(user_id, message_text, peer_id, conv_msg_id, is_reply_to_bot),
                daemon=True
            ).start()
            
            return 'ok', 200
        
        return 'ok', 200
    
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return 'error', 500


@app.route('/ping', methods=['GET'])
def ping():
    return 'pong', 200


@app.route('/status', methods=['GET'])
def status():
    return jsonify({
        "status": "running",
        "url": RENDER_URL,
        "group_id": VK_GROUP_ID,
        "db_available": db_available is not None
    })


if __name__ == '__main__':
    print("=" * 50)
    print("🚀 VK БОТ ЗАПУЩЕН")
    print("=" * 50)
    print(f"📍 Сервер: {RENDER_URL}")
    print(f"🔌 Порт: {PORT}")
    print(f"🔄 Автопинг: активен")
    print(f"💾 База данных: {'✅ ПОДКЛЮЧЕНА' if db_available else '❌ НЕДОСТУПНА'}")
    print("=" * 50)
    print("💬 Бот готов к работе!")
    print("=" * 50)
    print("📋 ПРАВИЛА АКТИВАЦИИ:")
    print("   🔁 Реплай на сообщение бота → ОТВЕЧАЮ ВСЕГДА (без ключевого слова)")
    print("   💬 Обычное новое сообщение → ОТВЕЧАЮ только если есть 'Сейч' в начале")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
