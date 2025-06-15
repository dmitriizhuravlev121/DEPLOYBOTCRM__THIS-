import os
import requests
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.enums.parse_mode import ParseMode
from aiogram.filters import Command, StateFilter
import asyncio
import time

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Настройки
API_TOKEN = os.getenv('TELEGRAM_API_TOKEN')
AIRTABLE_API_KEY = os.getenv('AIRTABLE_API_KEY')
AIRTABLE_BASE_ID = os.getenv('AIRTABLE_BASE_ID')
TEAMLEAD_ID = os.getenv('TEAMLEAD_ID')

# Проверка наличия переменных окружения
if not all([API_TOKEN, AIRTABLE_API_KEY, AIRTABLE_BASE_ID, TEAMLEAD_ID]):
    logger.critical("Отсутствуют необходимые переменные окружения")
    raise ValueError("Необходимо задать TELEGRAM_API_TOKEN, AIRTABLE_API_KEY, AIRTABLE_BASE_ID, TEAMLEAD_ID")

# Инициализация бота
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Словарь для хранения пользователей (Telegram ID -> {Record ID, Отдел})
ALLOWED_USERS = {}

# Словарь для отслеживания статуса заявок (record_id -> {'status': status, 'tracking_number': tracking_number})
REQUEST_STATUSES = {}

# Словарь для маппинга record_id пользователя в Airtable на Telegram ID
RECORD_ID_TO_TELEGRAM_ID = {}

# Состояния для FSM
class CreateRequest(StatesGroup):
    choosing_type = State()
    entering_custom_name = State()
    searching_product = State()
    selecting_product = State()
    entering_quantity = State()
    entering_fio = State()
    entering_phone = State()
    entering_address = State()
    entering_index = State()
    choosing_delivery = State()
    entering_custom_delivery = State()

# Загрузка пользователей из Airtable
def load_users():
    try:
        headers = {'Authorization': f'Bearer {AIRTABLE_API_KEY}'}
        url = f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Пользователи'
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        users = response.json().get('records', [])
        allowed_users = {}
        for user in users:
            fields = user.get('fields', {})
            telegram_id = fields.get('Telegram_ID')
            record_id = user.get('id')
            department = fields.get('Отдел', 'Без отдела')
            if telegram_id and record_id:
                allowed_users[str(telegram_id)] = {'record_id': record_id, 'department': department}
        logger.info(f"Загружено {len(allowed_users)} пользователей.")
        return allowed_users
    except Exception as e:
        logger.error(f"Ошибка загрузки пользователей: {e}")
        return {}

# Поиск товаров в Airtable с фильтром по остатку и отделу
def search_products(query, department):
    try:
        headers = {'Authorization': f'Bearer {AIRTABLE_API_KEY}'}
        url = f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Товары'
        if department == 'Администратор':
            filter_formula = f"AND(SEARCH(LOWER('{query}'), LOWER({{Название}})), {{Текущий остаток}} >= 1)"
        else:
            filter_formula = f"AND(SEARCH(LOWER('{query}'), LOWER({{Название}})), {{Текущий остаток}} >= 1, OR({{Отдел}} = '{department}', {{Отдел}} = 'Общее'))"
        params = {'filterByFormula': filter_formula}
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json().get('records', [])
    except Exception as e:
        logger.error(f"Ошибка поиска товаров: {e}")
        return []

# Получение товара по ID
def get_product_by_id(product_id):
    try:
        headers = {'Authorization': f'Bearer {AIRTABLE_API_KEY}'}
        url = f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Товары/{product_id}'
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Ошибка получения товара: {e}")
        return None

# Проверка доступа
def check_access(user_id, require_admin=False):
    user_id_str = str(user_id)
    if user_id_str not in ALLOWED_USERS:
        return False
    if require_admin and ALLOWED_USERS[user_id_str]['department'] != 'Администратор':
        return False
    return True

# Уведомление тимлиду о создании заявки
async def notify_teamlead(user_id, request_type, request_number):
    try:
        message = f"Новая заявка {request_number} от {user_id} (Тип: {request_type})."
        await bot.send_message(chat_id=TEAMLEAD_ID, text=message)
    except Exception as e:
        logger.error(f"Ошибка уведомления тимлида: {e}")

# Функция для получения всех заявок
async def fetch_all_requests():
    headers = {'Authorization': f'Bearer {AIRTABLE_API_KEY}'}
    requests_data = {}
    for table in ['Заявки', 'Кастомные_заказы']:
        url = f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table}'
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        records = response.json().get('records', [])
        for record in records:
            record_id = record['id']
            fields = record['fields']
            status = fields.get('Статус', 'Неизвестно')
            tracking_number = fields.get('Трек-номер', None)
            user_record_id = fields.get('Пользователь', [None])[0]
            requests_data[record_id] = {
                'status': status,
                'tracking_number': tracking_number,
                'user_record_id': user_record_id
            }
    return requests_data

# Фоновая задача для проверки обновлений заявок
async def check_request_updates():
    global REQUEST_STATUSES
    while True:
        try:
            logger.debug("Starting request updates check")
            headers = {'Authorization': f'Bearer {AIRTABLE_API_KEY}'}
            requests_data = {}

            def get_telegram_id(user_record_id):
                logger.debug(f"Fetching Telegram_ID for user_record_id {user_record_id}")
                response = requests.get(
                    f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Пользователи/{user_record_id}',
                    headers=headers
                )
                response.raise_for_status()
                fields = response.json().get('fields', {})
                telegram_id = fields.get('Telegram_ID')
                logger.debug(f"Retrieved Telegram_ID {telegram_id} for user_record_id {user_record_id}")
                return telegram_id

            for table in ['Заявки', 'Кастомные_заказы']:
                logger.debug(f"Fetching records from table {table}")
                url = f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table}'
                response = requests.get(url, headers=headers)
                response.raise_for_status()
                records = response.json().get('records', [])
                logger.debug(f"Fetched {len(records)} records from {table}")
                for record in records:
                    record_id = record['id']
                    fields = record['fields']
                    status = fields.get('Статус', 'Неизвестно')
                    tracking_number = fields.get('Трек-номер', None)
                    request_number = fields.get('Номер_заявки', 'Неизвестно')
                    user_record_id = fields.get('Пользователь', [None])[0]
                    requests_data[record_id] = {
                        'status': status,
                        'tracking_number': tracking_number,
                        'user_record_id': user_record_id,
                        'request_number': request_number
                    }

            for record_id, data in requests_data.items():
                if record_id not in REQUEST_STATUSES:
                    logger.debug(f"New request detected: {record_id}, request_number: {data['request_number']}")
                    REQUEST_STATUSES[record_id] = {
                        'status': data['status'],
                        'tracking_number': data['tracking_number'],
                        'request_number': data['request_number']
                    }
                else:
                    prev_status = REQUEST_STATUSES[record_id]['status']
                    prev_tracking = REQUEST_STATUSES[record_id]['tracking_number']
                    current_status = data['status']
                    current_tracking = data['tracking_number']
                    request_number = data['request_number']
                    user_record_id = data['user_record_id']

                    if user_record_id:
                        try:
                            telegram_id = get_telegram_id(user_record_id)
                            if telegram_id:
                                if current_status != prev_status:
                                    logger.info(
                                        f"Sending status update for request {request_number} to user {telegram_id}: "
                                        f"{prev_status} -> {current_status}"
                                    )
                                    await bot.send_message(
                                        chat_id=telegram_id,
                                        text=f"Статус вашей заявки №{request_number} изменился с '{prev_status}' на '{current_status}'."
                                    )
                                if current_tracking and current_tracking != prev_tracking:
                                    logger.info(
                                        f"Sending tracking update for request {request_number} to user {telegram_id}: "
                                        f"{current_tracking}"
                                    )
                                    await bot.send_message(
                                        chat_id=telegram_id,
                                        text=f"Трек-номер для вашей заявки №{request_number}: {current_tracking}"
                                    )
                            else:
                                logger.warning(f"No Telegram_ID found for user_record_id {user_record_id}")
                        except requests.exceptions.HTTPError as http_err:
                            logger.error(f"Error fetching Telegram_ID for {user_record_id}: {http_err}")
                    else:
                        logger.warning(f"No user_record_id found for request {record_id}")

                    REQUEST_STATUSES[record_id] = {
                        'status': current_status,
                        'tracking_number': current_tracking,
                        'request_number': request_number
                    }

            for record_id in list(REQUEST_STATUSES.keys()):
                if record_id not in requests_data:
                    logger.debug(f"Removing deleted request {record_id}")
                    del REQUEST_STATUSES[record_id]

            logger.debug("Request updates check completed")
        except Exception as e:
            logger.error(f"Error in check_request_updates: {e}")
        await asyncio.sleep(60)  # Проверка каждые 20 минут

# Создание клавиатуры главного меню
def get_main_menu():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Создать заявку"), KeyboardButton(text="История")]
        ],
        resize_keyboard=True
    )
    return keyboard

# Обработчик /start
@dp.message(Command("start"))
async def send_welcome(message: types.Message, state: FSMContext):
    await state.clear()
    await message.reply(
        "Добро пожаловать! Используйте:\n"
        "/create_request — для создания заявки\n"
        "/history — для просмотра истории",
        reply_markup=get_main_menu()
    )

# Обработчики для кнопок главного меню
@dp.message(lambda message: message.text == "Создать заявку")
async def handle_create_request(message: types.Message, state: FSMContext):
    await create_request(message, state)

@dp.message(lambda message: message.text == "История")
async def handle_history(message: types.Message):
    await show_history(message)

# Обработчик /history
@dp.message(Command("history"))
async def show_history(message: types.Message):
    user_id = str(message.from_user.id)
    logger.info(f"show_history called for user {user_id}")
    if not check_access(user_id):
        logger.warning(f"User {user_id} access denied")
        await message.reply("❌ Доступ запрещен", reply_markup=get_main_menu())
        return
    try:
        headers = {'Authorization': f'Bearer {AIRTABLE_API_KEY}'}
        history = []

        async def fetch_records(table_name):
            logger.debug(f"Fetching records from table {table_name}")
            response = requests.get(
                f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_name}',
                headers=headers
            )
            response.raise_for_status()
            records = response.json().get('records', [])
            logger.debug(f"Fetched {len(records)} records from {table_name}")
            return records

        def get_telegram_id(user_record_id):
            logger.debug(f"Fetching user data for record_id {user_record_id}")
            response = requests.get(
                f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Пользователи/{user_record_id}',
                headers=headers
            )
            response.raise_for_status()
            fields = response.json().get('fields', {})
            telegram_id = fields.get('Telegram_ID')
            logger.debug(f"Got Telegram_ID {telegram_id} for user_record_id {user_record_id}")
            return telegram_id

        orders = await fetch_records('Заявки')
        custom_orders = await fetch_records('Кастомные_заказы')

        for record in orders + custom_orders:
            fields = record['fields']
            user_record_ids = fields.get('Пользователь', [])
            logger.debug(f"Processing record {record['id']} with user_record_ids {user_record_ids}")

            for user_record_id in user_record_ids:
                try:
                    telegram_id = get_telegram_id(user_record_id)
                    if telegram_id == user_id:
                        logger.debug(f"Match found: record {record['id']} belongs to user {user_id}")
                        order_type = '📦 Заявка' if 'Товар' in fields else '🎨 Кастом'
                        product_info = 'Нет данных'
                        if 'Товар' in fields and fields['Товар']:
                            product_ids = fields['Товар']
                            products = []
                            for product_id in product_ids:
                                product = get_product_by_id(product_id)
                                if product:
                                    products.append(product['fields'].get('Название', product_id))
                            product_info = ", ".join(products)
                        history.append(
                            f"**{order_type}**\n"
                            f"**Номер заявки**: {fields.get('Номер_заявки', '-')}\n"
                            f"**Товар**: {product_info}\n"
                            f"**Количество**: {fields.get('Количество', '-')}\n"
                            f"**Сумма**: {fields.get('Общая_сумма', 0)} руб.\n"
                            f"**Статус**: {fields.get('Статус', '-')}\n"
                            f"**Дата**: {fields.get('Дата_создания', '-')}\n"
                            "--------------------"
                        )
                        break
                except requests.exceptions.HTTPError as http_err:
                    logger.error(f"Error fetching user data for {user_record_id}: {http_err}")
                    continue

        history_text = f"🛒 **История заявок**:\n\n" + "\n".join(history) if history else "У вас пока нет заявок."
        logger.info(f"History for user {user_id}: {len(history)} records found")
        MAX_MESSAGE_LENGTH = 4000
        if len(history_text) > MAX_MESSAGE_LENGTH:
            parts = []
            current_part = ""
            for line in history_text.split("\n"):
                if len(current_part) + len(line) + 1 > MAX_MESSAGE_LENGTH:
                    parts.append(current_part)
                    current_part = line
                else:
                    if current_part:
                        current_part += "\n" + line
                    else:
                        current_part = line
            if current_part:
                parts.append(current_part)
            for part in parts:
                await message.reply(part, parse_mode=ParseMode.MARKDOWN)
        else:
            await message.reply(history_text, parse_mode=ParseMode.MARKDOWN)
    except requests.exceptions.HTTPError as e:
        logger.error(f"Airtable error: {e.response.status_code} - {e.response.text}")
        await message.reply("Ошибка доступа к данным. Попробуйте позже.", reply_markup=get_main_menu())
    except Exception as e:
        logger.error(f"Error in show_history: {e}")
        await message.reply("Произошла ошибка. Обратитесь к администратору.", reply_markup=get_main_menu())

# Обработчик /create_request
@dp.message(Command("create_request"))
async def create_request(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id)
    if not check_access(user_id):
        await message.reply("❌ Доступ запрещен.", reply_markup=get_main_menu())
        return
    await state.clear()
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Существующий товар"), KeyboardButton(text="Кастомный товар")],
            [KeyboardButton(text="Начать заново")]
        ],
        resize_keyboard=True
    )
    await message.reply("Выберите тип заявки:", reply_markup=keyboard)
    await state.set_state(CreateRequest.choosing_type)

# Обработка выбора типа заявки
@dp.message(StateFilter(CreateRequest.choosing_type))
async def process_type(message: types.Message, state: FSMContext):
    if message.text == "Начать заново":
        await state.clear()
        await create_request(message, state)
        return
    if message.text == "Кастомный товар":
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Введите название кастомного товара:", reply_markup=keyboard)
        await state.set_state(CreateRequest.entering_custom_name)
    elif message.text == "Существующий товар":
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Введите название или часть названия товара для поиска:", reply_markup=keyboard)
        await state.set_state(CreateRequest.searching_product)
    else:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново")]
            ],
            resize_keyboard=True
        )
        await message.reply("Пожалуйста, выберите один из предложенных вариантов.", reply_markup=keyboard)

# Ввод названия кастомного товара
@dp.message(StateFilter(CreateRequest.entering_custom_name))
async def enter_custom_name(message: types.Message, state: FSMContext):
    if message.text == "Начать заново":
        await state.clear()
        await create_request(message, state)
        return
    if message.text == "Вернуться назад":
        await create_request(message, state)
        return
    custom_name = message.text.strip()
    if not custom_name:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Название товара не может быть пустым. Попробуйте снова.", reply_markup=keyboard)
        return
    await state.update_data(custom_name=custom_name)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
        ],
        resize_keyboard=True
    )
    await message.reply("Введите ФИО получателя:", reply_markup=keyboard)
    await state.set_state(CreateRequest.entering_fio)

# Поиск товара
@dp.message(StateFilter(CreateRequest.searching_product))
async def search_product(message: types.Message, state: FSMContext):
    if message.text == "Начать заново":
        await state.clear()
        await create_request(message, state)
        return
    if message.text == "Вернуться назад":
        await create_request(message, state)
        return
    try:
        query = message.text.strip()
        user_id = str(message.from_user.id)
        department = ALLOWED_USERS[user_id]['department']
        products = search_products(query, department)
        if not products:
            keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
                ],
                resize_keyboard=True
            )
            await message.reply("❌ Товары не найдены. Попробуйте другой запрос.", reply_markup=keyboard)
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[])
        for product in products:
            product_id = product['id']
            product_name = product['fields'].get('Название', 'Без названия')
            keyboard.inline_keyboard.append([InlineKeyboardButton(text=product_name, callback_data=f"product_{product_id}")])
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="Начать заново", callback_data="restart")])
        await message.reply("Выберите товар из списка:", reply_markup=keyboard)
        await state.update_data(search_query=query, product_list=products)
        await state.set_state(CreateRequest.selecting_product)
    except Exception as e:
        logger.error(f"Ошибка при поиске товаров: {e}")
        await message.reply("⚠ Ошибка при поиске товаров.", reply_markup=get_main_menu())
        await state.clear()

# Выбор товара
@dp.callback_query(StateFilter(CreateRequest.selecting_product))
async def select_product(callback_query: types.CallbackQuery, state: FSMContext):
    try:
        logger.debug(f"Processing callback: {callback_query.data}")
        data = await state.get_data()
        selected_products = data.get('selected_products', [])

        if callback_query.data == "restart":
            await state.clear()
            await create_request(callback_query.message, state)
            await callback_query.answer()
            return
        if callback_query.data == "add_more":
            await callback_query.message.edit_reply_markup(reply_markup=None)
            keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
                ],
                resize_keyboard=True
            )
            await callback_query.message.answer("Введите название или часть названия товара для поиска:", reply_markup=keyboard)
            await state.set_state(CreateRequest.searching_product)
            await callback_query.answer()
            return
        if callback_query.data == "back_to_search":
            await state.update_data(selected_products=[])
            products = data.get('product_list', [])
            keyboard = InlineKeyboardMarkup(inline_keyboard=[])
            for product in products:
                product_id = product['id']
                product_name = product['fields'].get('Название', 'Без названия')
                keyboard.inline_keyboard.append([InlineKeyboardButton(text=product_name, callback_data=f"product_{product_id}")])
            keyboard.inline_keyboard.append([InlineKeyboardButton(text="Начать заново", callback_data="restart")])
            await callback_query.message.edit_text("Выберите товар из списка:", reply_markup=keyboard)
            await callback_query.answer()
            return
        if callback_query.data == "finish_selection":
            if not selected_products:
                await callback_query.message.answer("Вы не выбрали ни одного товара.", reply_markup=get_main_menu())
                await state.clear()
                return
            num_products = len(selected_products)
            keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
                ],
                resize_keyboard=True
            )
            await callback_query.message.answer(
                f"Вы выбрали {num_products} товаров. Введите {num_products} количеств через запятую (например, 2,3):",
                reply_markup=keyboard
            )
            await state.set_state(CreateRequest.entering_quantity)
            await callback_query.answer()
            return
        if callback_query.data == "show_selected":
            if not selected_products:
                await callback_query.message.answer("Нет выбранных товаров.")
            else:
                product_names = [f"{product['name']} (Размер: {product.get('size', 'Не указан')})" for product in selected_products]
                keyboard = InlineKeyboardMarkup(inline_keyboard=[])
                for product in selected_products:
                    keyboard.inline_keyboard.append([InlineKeyboardButton(text=f"Удалить {product['name']}", callback_data=f"delete_product_{product['id']}")])
                keyboard.inline_keyboard.append([InlineKeyboardButton(text="Очистить все", callback_data="clear_all")])
                await callback_query.message.answer("Выбранные товары:\n" + "\n".join(product_names), reply_markup=keyboard)
            await callback_query.answer()
            return

        if callback_query.data.startswith('size_'):
            await select_size(callback_query, state)
            return

        if not callback_query.data.startswith('product_'):
            logger.warning(f"Invalid callback data: {callback_query.data}")
            await callback_query.answer("Неверный выбор")
            return

        product_id = callback_query.data.split('product_')[1]
        if any(product['id'] == product_id for product in selected_products):
            await callback_query.answer("❌ Этот товар уже выбран")
            return

        product_list = data.get('product_list', [])
        product = next((p for p in product_list if p['id'] == product_id), None)
        if not product:
            await callback_query.answer("❌ Товар не найден")
            return
        product_data = get_product_by_id(product_id)
        if not product_data:
            await callback_query.answer("❌ Товар удален")
            await state.clear()
            return
        product_name = product_data['fields']['Название']
        sizes = product_data['fields'].get('Размер', '')

        if sizes:
            size_list = [size.strip() for size in sizes.split(',') if size.strip()]
            keyboard = InlineKeyboardMarkup(inline_keyboard=[])
            for size in size_list:
                safe_size = size.replace('_', '__')
                keyboard.inline_keyboard.append([InlineKeyboardButton(text=size, callback_data=f"size_{product_id}_{safe_size}")])
            keyboard.inline_keyboard.append([InlineKeyboardButton(text="Вернуться назад", callback_data="back_to_search")])
            await callback_query.message.edit_text(f"Выберите размер для товара {product_name}:", reply_markup=keyboard)
            await state.update_data(current_product={'id': product_id, 'name': product_name})
            await callback_query.answer()
        else:
            selected_products.append({'id': product_id, 'name': product_name, 'size': "None"})
            await state.update_data(selected_products=selected_products)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Добавить еще товар", callback_data="add_more")],
                [InlineKeyboardButton(text="Вернуться к поиску", callback_data="back_to_search")],
                [InlineKeyboardButton(text="Завершить выбор", callback_data="finish_selection")],
                [InlineKeyboardButton(text="Показать выбранные товары", callback_data="show_selected")],
                [InlineKeyboardButton(text="Начать заново", callback_data="restart")]
            ])
            await callback_query.message.edit_text(
                f"✅ Выбран товар: {product_name}. Всего выбрано: {len(selected_products)} товаров. Хотите добавить еще?",
                reply_markup=keyboard
            )
            await callback_query.answer()
    except Exception as e:
        logger.error(f"Ошибка при выборе товара: {e}")
        await callback_query.message.edit_text("⚠ Произошла ошибка", reply_markup=None)
        await state.clear()
        await callback_query.answer()

# Обработчик выбора размера
@dp.callback_query(lambda c: c.data.startswith('size_'))
async def select_size(callback_query: types.CallbackQuery, state: FSMContext):
    try:
        logger.debug(f"Processing size selection: {callback_query.data}")
        data = await state.get_data()
        selected_products = data.get('selected_products', [])
        callback_data = callback_query.data.split('_')
        if len(callback_data) < 3:
            logger.warning(f"Invalid size callback data: {callback_query.data}")
            await callback_query.answer("Неверный выбор размера")
            return

        product_id = callback_data[1]
        size = '_'.join(callback_data[2:]).replace('__', '_')

        current_product = data.get('current_product', {})
        if not current_product or current_product['id'] != product_id:
            logger.warning(f"Product mismatch: expected {current_product.get('id')}, got {product_id}")
            await callback_query.answer("❌ Товар не соответствует текущему выбору")
            return

        selected_products.append({'id': product_id, 'name': current_product['name'], 'size': size})
        await state.update_data(selected_products=selected_products, current_product=None)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Добавить еще товар", callback_data="add_more")],
            [InlineKeyboardButton(text="Вернуться к поиску", callback_data="back_to_search")],
            [InlineKeyboardButton(text="Завершить выбор", callback_data="finish_selection")],
            [InlineKeyboardButton(text="Показать выбранные товары", callback_data="show_selected")],
            [InlineKeyboardButton(text="Начать заново", callback_data="restart")]
        ])
        await callback_query.message.edit_text(
            f"✅ Выбран товар: {current_product['name']} (Размер: {size}). Всего выбрано: {len(selected_products)} товаров. Хотите добавить еще?",
            reply_markup=keyboard
        )
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Ошибка при выборе размера: {e}")
        await callback_query.message.edit_text("⚠ Произошла ошибка", reply_markup=None)
        await state.clear()
        await callback_query.answer()

# Обработчик удаления товара
@dp.callback_query(lambda c: c.data.startswith('delete_product_') or c.data == 'clear_all')
async def handle_delete_product(callback_query: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        selected_products = data.get('selected_products', [])

        if callback_query.data == "clear_all":
            selected_products = []
            await state.update_data(selected_products=selected_products)
            await callback_query.message.edit_text("Все товары удалены из списка.", reply_markup=None)
            await callback_query.answer()
            return

        if callback_query.data.startswith('delete_product_'):
            product_id_to_delete = callback_query.data.split('delete_product_')[1]
            selected_products = [product for product in selected_products if product['id'] != product_id_to_delete]
            await state.update_data(selected_products=selected_products)

            if not selected_products:
                await callback_query.message.edit_text("Нет выбранных товаров.", reply_markup=None)
            else:
                product_names = [product['name'] for product in selected_products]
                keyboard = InlineKeyboardMarkup(inline_keyboard=[])
                for product in selected_products:
                    keyboard.inline_keyboard.append([InlineKeyboardButton(text=f"Удалить {product['name']}", callback_data=f"delete_product_{product['id']}")])
                keyboard.inline_keyboard.append([InlineKeyboardButton(text="Очистить все", callback_data="clear_all")])
                await callback_query.message.edit_text("Выбранные товары:\n" + "\n".join(product_names), reply_markup=keyboard)
            await callback_query.answer()
    except Exception as e:
        logger.error(f"Ошибка при удалении товара: {e}")
        await callback_query.message.edit_text("⚠ Произошла ошибка", reply_markup=None)
        await callback_query.answer()

# Ввод количества
@dp.message(StateFilter(CreateRequest.entering_quantity))
async def enter_quantity(message: types.Message, state: FSMContext):
    if message.text == "Начать заново":
        await state.clear()
        await create_request(message, state)
        return
    if message.text == "Вернуться назад":
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Введите название или часть названия товара для поиска:", reply_markup=keyboard)
        await state.set_state(CreateRequest.searching_product)
        return
    data = await state.get_data()
    selected_products = data.get('selected_products', [])
    num_products = len(selected_products)
    quantities_str = message.text.strip().split(',')
    if len(quantities_str) != num_products or not all(q.strip().isdigit() for q in quantities_str):
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply(
            f"❌ Ошибка: нужно ввести ровно {num_products} чисел через запятую (например, 2,3). Попробуйте снова:",
            reply_markup=keyboard
        )
        return
    quantities = [int(q.strip()) for q in quantities_str]
    await state.update_data(quantities=quantities)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
        ],
        resize_keyboard=True
    )
    await message.reply("Введите ФИО получателя:", reply_markup=keyboard)
    await state.set_state(CreateRequest.entering_fio)

# Ввод ФИО
@dp.message(StateFilter(CreateRequest.entering_fio))
async def enter_fio(message: types.Message, state: FSMContext):
    if message.text == "Начать заново":
        await state.clear()
        await create_request(message, state)
        return
    if message.text == "Вернуться назад":
        data = await state.get_data()
        if 'selected_products' in data:
            keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
                ],
                resize_keyboard=True
            )
            num_products = len(data.get('selected_products', []))
            await message.reply(
                f"Введите {num_products} количеств через запятую (например, 2,3):",
                reply_markup=keyboard
            )
            await state.set_state(CreateRequest.entering_quantity)
        else:
            keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
                ],
                resize_keyboard=True
            )
            await message.reply("Введите название кастомного товара:", reply_markup=keyboard)
            await state.set_state(CreateRequest.entering_custom_name)
        return
    fio = message.text.strip()
    if not fio:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("ФИО не может быть пустым. Попробуйте снова.", reply_markup=keyboard)
        return
    await state.update_data(fio=fio)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
        ],
        resize_keyboard=True
    )
    await message.reply("Введите номер телефона:", reply_markup=keyboard)
    await state.set_state(CreateRequest.entering_phone)

# Ввод телефона
@dp.message(StateFilter(CreateRequest.entering_phone))
async def enter_phone(message: types.Message, state: FSMContext):
    if message.text == "Начать заново":
        await state.clear()
        await create_request(message, state)
        return
    if message.text == "Вернуться назад":
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Введите ФИО получателя:", reply_markup=keyboard)
        await state.set_state(CreateRequest.entering_fio)
        return
    phone = message.text.strip()
    if not phone or not any(c.isdigit() for c in phone):
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Номер телефона должен содержать цифры. Попробуйте снова.", reply_markup=keyboard)
        return
    await state.update_data(phone=phone)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
        ],
        resize_keyboard=True
    )
    await message.reply("Введите адрес:", reply_markup=keyboard)
    await state.set_state(CreateRequest.entering_address)

# Ввод адреса
@dp.message(StateFilter(CreateRequest.entering_address))
async def enter_address(message: types.Message, state: FSMContext):
    if message.text == "Начать заново":
        await state.clear()
        await create_request(message, state)
        return
    if message.text == "Вернуться назад":
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Введите номер телефона:", reply_markup=keyboard)
        await state.set_state(CreateRequest.entering_phone)
        return
    address = message.text.strip()
    if not address:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Адрес не может быть пустым. Попробуйте снова.", reply_markup=keyboard)
        return
    await state.update_data(address=address)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
        ],
        resize_keyboard=True
    )
    await message.reply("Введите индекс:", reply_markup=keyboard)
    await state.set_state(CreateRequest.entering_index)

# Ввод индекса
@dp.message(StateFilter(CreateRequest.entering_index))
async def enter_index(message: types.Message, state: FSMContext):
    if message.text == "Начать заново":
        await state.clear()
        await create_request(message, state)
        return
    if message.text == "Вернуться назад":
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Введите адрес:", reply_markup=keyboard)
        await state.set_state(CreateRequest.entering_address)
        return
    index = message.text.strip()
    if not index or not index.isdigit():
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Индекс должен содержать только цифры. Попробуйте снова.", reply_markup=keyboard)
        return
    await state.update_data(index=index)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Почта"), KeyboardButton(text="Курьер"), KeyboardButton(text="CDEK")],
            [KeyboardButton(text="Свой вариант")],
            [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
        ],
        resize_keyboard=True
    )
    await message.reply("Выберите способ отправки:", reply_markup=keyboard)
    await state.set_state(CreateRequest.choosing_delivery)

# Выбор способа доставки
@dp.message(StateFilter(CreateRequest.choosing_delivery))
async def choose_delivery(message: types.Message, state: FSMContext):
    if message.text == "Начать заново":
        await state.clear()
        await create_request(message, state)
        return
    if message.text == "Вернуться назад":
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Введите индекс:", reply_markup=keyboard)
        await state.set_state(CreateRequest.entering_index)
        return
    if message.text in ["Почта", "Курьер", "CDEK"]:
        await state.update_data(delivery_method=message.text)
        await save_request(message, state)
    elif message.text == "Свой вариант":
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Введите свой способ доставки:", reply_markup=keyboard)
        await state.set_state(CreateRequest.entering_custom_delivery)
    else:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Почта"), KeyboardButton(text="Курьер"), KeyboardButton(text="CDEK")],
                [KeyboardButton(text="Свой вариант")],
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Пожалуйста, выберите один из предложенных вариантов.", reply_markup=keyboard)

# Ввод собственного способа доставки
@dp.message(StateFilter(CreateRequest.entering_custom_delivery))
async def enter_custom_delivery(message: types.Message, state: FSMContext):
    if message.text == "Начать заново":
        await state.clear()
        await create_request(message, state)
        return
    if message.text == "Вернуться назад":
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Почта"), KeyboardButton(text="Курьер"), KeyboardButton(text="CDEK")],
                [KeyboardButton(text="Свой вариант")],
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Выберите способ отправки:", reply_markup=keyboard)
        await state.set_state(CreateRequest.choosing_delivery)
        return
    custom_delivery = message.text.strip()
    if not custom_delivery:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать заново"), KeyboardButton(text="Вернуться назад")]
            ],
            resize_keyboard=True
        )
        await message.reply("Способ доставки не может быть пустым. Попробуйте снова.", reply_markup=keyboard)
        return
    await state.update_data(delivery_method=custom_delivery)
    await save_request(message, state)

# Сохранение заявки
async def save_request(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id)
    user_data = await state.get_data()
    try:
        user_record_id = ALLOWED_USERS.get(user_id)['record_id']
        table_name = "Заявки" if 'selected_products' in user_data else "Кастомные_заказы"
        headers = {'Authorization': f'Bearer {AIRTABLE_API_KEY}', 'Content-Type': 'application/json'}
        delivery_method = user_data.get('delivery_method', 'Не указано')
        if 'selected_products' in user_data:
            product_ids = [p['id'] for p in user_data['selected_products']]
            quantities = user_data.get('quantities', [])
            sizes = [p.get('size', 'Не указан') for p in user_data['selected_products']]
            logger.debug(f"Saving request with products: {product_ids}, quantities: {quantities}, sizes: {sizes}")
            payload = {
                "records": [{
                    "fields": {
                        "Товар": product_ids,
                        "Количество": ", ".join(str(q) for q in quantities),
                        "Размер": ", ".join(sizes),
                        "ФИО": user_data.get('fio', ''),
                        "Номер_телефона": user_data.get('phone', ''),
                        "Адрес": user_data.get('address', ''),
                        "Индекс": user_data.get('index', ''),
                        "Способ_отправки": delivery_method,
                        "Статус": "В обработке",
                        "Пользователь": [user_record_id]
                    }
                }]
            }
        else:
            payload = {
                "records": [{
                    "fields": {
                        "Название_кастома": user_data.get('custom_name', ''),
                        "ФИО": user_data.get('fio', ''),
                        "Номер_телефона": user_data.get('phone', ''),
                        "Адрес": user_data.get('address', ''),
                        "Индекс": user_data.get('index', ''),
                        "Способ_отправки": delivery_method,
                        "Статус": "В обработке",
                        "Пользователь": [user_record_id]
                    }
                }]
            }
        url = f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_name}'
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        request_number = response.json()['records'][0]['fields'].get('Номер_заявки', 'Неизвестно')
        await message.reply(f"✅ Заявка {request_number} успешно создана!", reply_markup=get_main_menu())
        await notify_teamlead(user_id, "Существующий товар" if 'selected_products' in user_data else "Кастомный товар", request_number)
        record_id = response.json()['records'][0]['id']
        REQUEST_STATUSES[record_id] = {
            'status': "В обработке",
            'tracking_number': None,
            'request_number': request_number
        }
        logger.info(f"Request {request_number} saved successfully for user {user_id}")
    except requests.exceptions.HTTPError as http_err:
        logger.error(f"HTTP ошибка: {http_err}, Ответ: {response.text}")
        await message.reply("❌ Ошибка при сохранении заявки. Попробуйте позже.", reply_markup=get_main_menu())
    except Exception as e:
        logger.error(f"Ошибка: {str(e)}")
        await message.reply(f"❌ Ошибка: {str(e)}. Попробуйте позже.", reply_markup=get_main_menu())
    await state.clear()

# Запуск бота
async def main():
    global ALLOWED_USERS, RECORD_ID_TO_TELEGRAM_ID, REQUEST_STATUSES
    ALLOWED_USERS = load_users()
    RECORD_ID_TO_TELEGRAM_ID = {data['record_id']: telegram_id for telegram_id, data in ALLOWED_USERS.items()}
    asyncio.create_task(check_request_updates())
    await dp.start_polling(bot)

def start_bot():
    asyncio.run(main())

if __name__ == '__main__':
    start_bot()
