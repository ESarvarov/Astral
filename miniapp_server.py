import hmac
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl
import requests

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telegram.ext import Application

from bot import (
    ALLOWED,
    STATUS_PENDING,
    SWAP_NO_ASSETS,
    is_crypto_swap,
    get_effective_exchange_rates,
    init_db,
    is_user_blocked,
    register_user,
    save_order,
    notify_admins_about_clean_request,
    get_ticket,
    create_ticket,
    close_ticket,
    _all_admin_ids,
    get_chat_messages,
    save_chat_message,
)

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
MINIAPP_DIR = BASE_DIR / "miniapp"

app = FastAPI(title="Exchange Bot Mini App API")
app.mount("/static", StaticFiles(directory=MINIAPP_DIR), name="miniapp-static")


class OrderRequest(BaseModel):
    telegram_id: int = Field(..., gt=0)
    from_currency: str
    to_currency: str
    amount: float = Field(..., gt=0)
    wallet_address: str = Field(..., min_length=8)

class TicketMessage(BaseModel):
    telegram_id: int
    text: str


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(MINIAPP_DIR / "index.html")


@app.get("/api/config")
def config() -> Dict[str, Any]:
    return {"allowed": list(ALLOWED)}


@app.get("/api/rates")
def rates() -> Dict[str, Any]:
    try:
        return {"rates": get_effective_exchange_rates()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Не удалось получить курсы: {exc}") from exc


@app.post("/api/order")
def create_order(payload: OrderRequest) -> Dict[str, Any]:
    if is_user_blocked(payload.telegram_id):
        raise HTTPException(status_code=403, detail="Аккаунт заблокирован.")

    from_currency = payload.from_currency.upper()
    to_currency = payload.to_currency.upper()

    if is_crypto_swap(from_currency, to_currency):
        raise HTTPException(status_code=503, detail=SWAP_NO_ASSETS)

    if from_currency not in ALLOWED or to_currency not in ALLOWED:
        raise HTTPException(status_code=400, detail="Неверная валюта.")
    if from_currency == to_currency:
        raise HTTPException(status_code=400, detail="Валюты должны отличаться.")

    try:
        rates = get_effective_exchange_rates()
        rate = rates[from_currency] / rates[to_currency]
        total = payload.amount * rate
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Ошибка расчета курса: {exc}") from exc

    register_user(payload.telegram_id)
    order_id = save_order(
        telegram_id=payload.telegram_id,
        from_currency=from_currency,
        to_currency=to_currency,
        amount=payload.amount,
        rate=rate,
        total=total,
        wallet_address=payload.wallet_address.strip(),
    )
    return {
        "order_id": order_id,
        "rate": rate,
        "total": total,
        "status": STATUS_PENDING,
    }

def miniapp_order_owner(init_data: Optional[str]) -> int:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="Проверка Telegram временно недоступна.")
    try:
        fields = parse_qsl(init_data or "", keep_blank_values=True, strict_parsing=True)
        values = dict(fields)
        if len(values) != len(fields):
            raise ValueError("Duplicate initData fields")
        supplied_hash = values.pop("hash")
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
        expected_hash = hmac.digest(secret, check_string.encode(), "sha256").hex()
        if not hmac.compare_digest(expected_hash, supplied_hash):
            raise ValueError("Invalid initData signature")
        age_seconds = time.time() - int(values["auth_date"])
        if not 0 <= age_seconds <= 86400:
            raise ValueError("Expired initData")
        user_id = json.loads(values["user"])["id"]
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("Invalid Telegram user ID")
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Откройте Mini App заново через Telegram.") from exc
    return user_id


@app.get("/api/my_orders")
def get_my_orders(telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data")):
    from bot import get_user_orders
    orders = get_user_orders(miniapp_order_owner(telegram_init_data), limit=20)
    result = []
    for row in orders:
        result.append({
            "id": row[0],
            "from_currency": row[1],
            "to_currency": row[2],
            "amount": row[3],
            "total": row[5],
            "status": row[7],
            "created_at": row[8]
        })
    return {"orders": result}

@app.get("/api/avatar")
def get_avatar(telegram_id: int):
    """
    Безопасная загрузка аватарки.
    Вместо отдачи прямого URL (который содержит токен бота),
    мы скачиваем файл на сервер и отдаем его клиенту.
    """
    token = os.getenv("BOT_TOKEN")
    if not token:
        return {"url": None}
    try:
        # 1. Получаем file_id
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUserProfilePhotos",
            params={"user_id": telegram_id, "limit": 1}
        )
        data = resp.json()
        if not data.get("ok") or data.get("result", {}).get("total_count", 0) == 0:
            return {"url": None}
        
        photo_sizes = data["result"]["photos"][0]
        file_id = photo_sizes[-1]["file_id"]
        
        # 2. Получаем путь к файлу
        resp_file = requests.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id}
        )
        file_data = resp_file.json()
        if not file_data.get("ok"):
            return {"url": None}
        
        file_path = file_data["result"]["file_path"]
        
        # 3. Скачиваем картинку (чтобы не палить токен в URL)
        img_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        img_resp = requests.get(img_url, stream=True)
        if img_resp.status_code != 200:
            return {"url": None}
        
        # Сохраняем локально во временную папку
        local_path = MINIAPP_DIR / f"avatar_{telegram_id}.jpg"
        with open(local_path, "wb") as f:
            for chunk in img_resp.iter_content(1024):
                f.write(chunk)
        
        # Возвращаем локальный URL (безопасный)
        return {"url": f"/static/avatar_{telegram_id}.jpg"}
    except Exception as e:
        print(f"Ошибка получения аватарки: {e}")
        return {"url": None}

@app.post("/api/ticket/start")
async def start_ticket(payload: dict):
    telegram_id = payload.get("telegram_id")
    first_name = payload.get("first_name", "Пользователь")
    username = payload.get("username")
    if not telegram_id:
        raise HTTPException(status_code=400, detail="Не указан telegram_id")
    
    # Проверяем, есть ли уже активный тикет
    existing = get_ticket(telegram_id)
    if existing:
        return {"status": "already_exists", "admin_id": existing[1]}
    
    # Создаем объект пользователя
    class FakeUser:
        def __init__(self, uid, fname, uname):
            self.id = uid
            self.full_name = fname
            self.username = uname

    user = FakeUser(telegram_id, first_name, username)
    
    # Создаем временный объект Application для отправки сообщения
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="BOT_TOKEN не настроен")

    bot_app = Application.builder().token(token).build()
    
    # Сначала создаем тикет в БАЗЕ, чтобы данные точно сохранились
    admins = _all_admin_ids()
    admin_id = list(admins)[0] if admins else 0
    create_ticket(telegram_id, admin_id, first_name, username)
    
    # Затем уведомляем админов
    try:
        ok = await notify_admins_about_clean_request(bot_app, user, source="через Mini App.")
        if not ok:
            raise Exception("Не удалось уведомить администраторов")
    except Exception as e:
        print(f"Ошибка при уведомлении админов: {e}")
        # Не выбрасываем ошибку, чтобы клиент не видел сбой, если админ недоступен
        pass

    return {"status": "created", "admin_id": admin_id}

@app.get("/api/ticket/status")
def ticket_status(telegram_id: int):
    ticket = get_ticket(telegram_id)
    if not ticket:
        return {"active": False}
    return {"active": True, "admin_id": ticket[1], "created_at": ticket[2]}

@app.post("/api/ticket/send")
def send_ticket_message(payload: TicketMessage):
    ticket = get_ticket(payload.telegram_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Активный диалог не найден")
    
    save_chat_message(payload.telegram_id, "user", payload.text)
    
    admin_id = ticket[1]
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="BOT_TOKEN не настроен")
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": admin_id,
                "text": f"📨 Сообщение от пользователя (Mini App)\nID: {payload.telegram_id}\n\n{payload.text}"
            }
        )
        if not resp.ok:
            raise Exception(f"Telegram API error: {resp.text}")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Не удалось отправить сообщение: {e}")
    return {"status": "sent"}

@app.get("/api/ticket/messages")
def get_ticket_messages(telegram_id: int):
    messages = get_chat_messages(telegram_id)
    result = []
    for row in messages:
        result.append({
            "id": row[0],
            "sender": row[1],
            "text": row[2],
            "created_at": row[3]
        })
    return {"messages": result}

@app.post("/api/ticket/close")
def close_ticket_api(payload: dict):
    telegram_id = payload.get("telegram_id")
    if not telegram_id:
        raise HTTPException(status_code=400, detail="Не указан telegram_id")
    ticket = get_ticket(telegram_id)
    if ticket:
        admin_id = ticket[1]
        close_ticket(telegram_id)
        token = os.getenv("BOT_TOKEN")
        if token:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": admin_id,
                        "text": f"Пользователь {telegram_id} завершил обращение через Mini App."
                    }
                )
            except Exception:
                pass
    return {"status": "closed"}

if __name__ == "__main__":
    import uvicorn
    host = os.getenv("MINI_APP_HOST", "0.0.0.0")
    port = int(os.getenv("MINI_APP_PORT", "8080"))
    uvicorn.run("miniapp_server:app", host=host, port=port, reload=False)