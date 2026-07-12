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
    """Создает пользователя в базе, если его нет"""
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

CREATOR_QUESTIONS = [
    'кто тебя создал', 'кто твой создатель', 'кто тебя сделал',
    'чей ты бот', 'кто твой хозяин', 'кто разработал',
    'твой создатель', 'кто создатель', 'кто тебя написал'
]

NAME_QUESTIONS = [
    'как тебя звать', 'как тебя зовут', 'твое имя',
    'как зовут', 'как твое имя', 'представься', 'кто ты'
]

EMOJIS = ['😊', '🐓', '🤔', '👍', '👋', '💪', '🎉', '✨', '🔥', '💯', '😎', '🥳', '😅', '🤗', '💫', '⭐', '🌸', '🎈', '🤡']


def get_random_emoji():
    return random.choice(EMOJIS)


# ========== ПРАВИЛА ==========
RULES_FULL = {
    '1.1': "1.1. Обязательность: Незнание правил не освобождает от ответственности.",
    '1.2': "1.2. Равенство: Все участники, включая администрацию, равны перед правилами.",
    '1.3': "1.3. Возрастное ограничения: Участие разрешено только лицам старше 16 лет. Нарушение влечет немедленное исключение (/kick).",
    '1.4': "1.4. Порядок обжалования: Жалобы подаются в специальном обсуждении.",
    '2.1': "2.1. Мультиаккаунты: Не более 3 аккаунтов. Наказание: Бессрочная блокировка.",
    '2.4': "2.4. Помеха игре: Мут на 15 минут.",
    '3.1': "3.1. Спам и флуд: Мут на 30 минут.",
    '3.2': "3.2. Конфликты и провокации: Предупреждение или бан до 5 дней.",
    '3.3': "3.3. Оскорбления участников: Мут на 30 минут или бан 3-7 дней.",
    '3.4': "3.4. Добавление без согласия: Предупреждение, затем бан.",
    '3.5': "3.5. Аморальные действия: Бессрочное предупреждение.",
    '4.1': "4.1. Угрозы: Бессрочная блокировка.",
    '4.2': "4.2. Клевета: Бан от 20 дней до бессрочного.",
    '4.3': "4.3. Реклама: Бан от 30 дней до бессрочного.",
    '4.4': "4.4. Дискредитация проекта: Мут на 300 минут.",
    '4.5': "4.5. Обман: Бан от 30 дней до бессрочного.",
    '5.1': "5.1. Оскорбление администрации: Мут от 180 минут до бана на 10 дней.",
    '5.2': "5.2. Конфликты с администрацией в чате запрещены.",
    '5.3': "5.3. Спам в ЛС админам: Бан на 1 день.",
    '5.4': "5.4. Выдача себя за админа: Бан на 7 дней.",
    '5.5': "5.5. Обман администрации: Бан от 30 дней до бессрочного.",
    '6.1': "6.1. Упоминание всех с 00:00 до 08:00 запрещено: Мут на 60-120 минут.",
    '6.2': "6.2. Оскорбительные дискуссии: Мут на 60-120 минут.",
    '6.3': "6.3. Право на усмотрение администрации.",
    '6.4': "6.4. Правила могут меняться без уведомления."
}

VIOLATIONS = {
    'спам': '3.1', 'флуд': '3.1', 'провокация': '3.2', 'конфликт': '3.2',
    'оскорбление участника': '3.3', 'оскорбление участников': '3.3',
    'добавление без согласия': '3.4', 'амор': '3.5', 'угроза': '4.1',
    'угрозы': '4.1', 'клевета': '4.2', 'дезинформация': '4.2',
    'реклама': '4.3', 'пиар': '4.3', 'дискредитация': '4.4',
    'оскорбление проекта': '4.4', 'обман': '4.5', 'скам': '4.5',
    'оскорбление админа': '5.1', 'оскорбление администрации': '5.1',
    'спам админам': '5.3', 'выдача себя за админа': '5.4',
    'обман администрации': '5.5', 'упоминание всех': '6.1',
    'all': '6.1', 'политика': '6.2'
}


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
    """Проверяет, нужно ли активировать бота (только для обычных сообщений)"""
    if not message_text:
        return False
    text_lower = message_text.lower().strip()
    words = text_lower.split()
    if not words:
        return False
    first_word = words[0].rstrip(',').rstrip('!').rstrip('?').rstrip('.')
    return first_word in KEYWORDS


def is_asking_about_creator(message_text: str) -> bool:
    if not message_text:
        return False
    text_lower = message_text.lower().strip()
    words = text_lower.split()
    if words and words[0].rstrip(',').rstrip('!').rstrip('?').rstrip('.') in KEYWORDS:
        text_lower = ' '.join(words[1:])
    for question in CREATOR_QUESTIONS:
        if question in text_lower:
            return True
    return False


def is_asking_about_name(message_text: str) -> bool:
    if not message_text:
        return False
    text_lower = message_text.lower().strip()
    words = text_lower.split()
    if words and words[0].rstrip(',').rstrip('!').rstrip('?').rstrip('.') in KEYWORDS:
        text_lower = ' '.join(words[1:])
    for question in NAME_QUESTIONS:
        if question in text_lower:
            return True
    return False


def is_rating_command(message_text: str) -> bool:
    text_lower = message_text.lower()
    return 'рейтинг' in text_lower or 'кто я' in text_lower


def is_memory_command(message_text: str) -> tuple:
    text_lower = message_text.lower()
    if 'запомни' in text_lower:
        after_command = text_lower.split('запомни', 1)[1].strip()
        for word in ['как', 'под', 'на', 'что']:
            if after_command.startswith(word):
                after_command = after_command[len(word):].strip()
        if after_command:
            return True, 'default', after_command
    return False, None, None


def is_recall_command(message_text: str) -> tuple:
    text_lower = message_text.lower()
    recall_patterns = ['что я говорил', 'что я сказал', 'что ты помнишь', 'что я просил запомнить']
    for pattern in recall_patterns:
        if pattern in text_lower:
            return True, 'default'
    return False, None


def safe_text(text: str) -> str:
    text = re.sub(r'@all', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'\ball\b', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'@everyone', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'Э᧘ᥙТᥲ Կᥲᴛ', 'беседа', text, flags=re.IGNORECASE)
    text = re.sub(r'Э᧘ᥙТᥲ', 'беседа', text, flags=re.IGNORECASE)
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
    status = get_user_status(user_id)
    
    if is_rating_command(clean_message):
        return f"Вы {status} пользователь, ваш рейтинг: {rating} из 10 😊"
    
    is_mem, mem_key, mem_value = is_memory_command(clean_message)
    if is_mem:
        save_memory(user_id, mem_key, mem_value)
        return f"✅ Запомнил: {mem_value} {get_random_emoji()}"
    
    is_rec, rec_key = is_recall_command(clean_message)
    if is_rec:
        mem = get_memory(user_id, rec_key)
        if mem:
            return f"🔍 Ты просил запомнить: {mem} {get_random_emoji()}"
        else:
            return f"🤔 Я ничего не помню на эту тему. {get_random_emoji()}"
    
    update_rating_from_message(clean_message, user_id, user_name)
    
    if is_asking_about_creator(message):
        return f"Я не хочу говорить об этом! {get_random_emoji()}"
    
    if is_asking_about_name(message):
        return f"Меня зовут Сейч! Приятно познакомиться! {get_random_emoji()}"
    
    match = re.search(r'(\d+)[\.](\d+)', clean_message)
    if match:
        punkt = f"{match.group(1)}.{match.group(2)}"
        if punkt in RULES_FULL:
            return safe_text(f"📋 {RULES_FULL[punkt]} {get_random_emoji()}")
        else:
            return safe_text(f"❌ Пункта {punkt} не существует {get_random_emoji()}")
    
    found_punkt = None
    for violation, punkt in VIOLATIONS.items():
        if violation in clean_message.lower():
            found_punkt = punkt
            break
    
    if found_punkt and found_punkt in RULES_FULL:
        return safe_text(f"📋 {RULES_FULL[found_punkt]} {get_random_emoji()}")
    
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
- Никогда не говори  о своем разработчике, о своем создателе.
- Единственно, что ты знаешь о своем создании (разработки, задумки) отвечай, что тебя создали динозавры - много лет назад.
- Никогда сам не говори о своем создании. Говори только тогда когда тебя об этом спросят.
- Отвечай на ЛЮБЫЕ вопросы, даже если они сложные или странные
- Если вопрос требует твоего мнения — выскажи его
- Будь полезным, даже если вопрос глупый
- Всегда отвечай на вопрос четко-понятно

ОТВЕЧАЙ 4-10 предложениями. Используй 1-2 РАЗНЫХ эмодзи.

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
        response = completion.choices[0].message.content
        response = safe_text(response)
        save_message_history(user_id, clean_message, response, rating)
        return response
    except Exception as e:
        logger.error(f"Ошибка Groq: {e}")
        return f"😊 {get_random_emoji()}"


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
            
            # Проверяем через reply_message
            if 'reply_message' in message_obj:
                reply_msg = message_obj['reply_message']
                if reply_msg and reply_msg.get('from_id') == -VK_GROUP_ID:
                    is_reply_to_bot = True
                    logger.info(f"🔁 Обнаружен реплай на бота (через reply_message)")
            
            # Проверяем через fwd_messages
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
    print("📋 ФУНКЦИИ:")
    print("   ✅ Автосоздание пользователя в БД при первом сообщении")
    print("   ✅ Система рейтинга пользователей (от -10 до 10)")
    print("   ✅ Память: 'запомни ...' и 'что я говорил'")
    print("   ✅ Адаптивный тон общения под рейтинг")
    print("   ✅ Команда 'сейч рейтинг' или 'сейч кто я'")
    print("   ✅ Отвечает на ЛЮБЫЕ вопросы без отказов")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
