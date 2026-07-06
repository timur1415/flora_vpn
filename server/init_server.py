import json

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from telegram.error import InvalidToken, TelegramError

from config.config import (
    BOT_TOKEN,
    PLATEGA_MERCHANT_ID,
    PLATEGA_SECRET,
    SECRET_KEY,
    WEBHOOK_URL,
)
from db.db import Base, engine, get_db
from db.models import PaymentTransaction, User
from db.telegram_service import (
    create_platega_payment,
    get_active_subscription,
    get_or_create_telegram_user,
    handle_platega_callback,
    PlategaIntegrationError,
)
from server.telegram_auth import validate_telegram_webapp_data_verbose
from telegram import Update

from tg_bot.init_bot import init_bot


def _is_placeholder(value: str | None) -> bool:
    if not value:
        return True
    return value.startswith("replace_with_")


def _has_value(value: str | None) -> bool:
    return not _is_placeholder(value)


def _get_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.scalar(select(User).where(User.id == user_id))


def init_server():
    app = FastAPI(lifespan=lifespan)
    cookie_https_only = bool(WEBHOOK_URL and WEBHOOK_URL.startswith("https://"))
    cookie_same_site = "none" if cookie_https_only else "lax"
    app.add_middleware(
        SessionMiddleware,
        secret_key=SECRET_KEY,
        same_site=cookie_same_site,
        https_only=cookie_https_only,
    )
    templates = Jinja2Templates(directory="templates")

    @app.get("/")
    async def read_root(request: Request):
        return templates.TemplateResponse(
            request,
            "index.html",
            {'request': request}
        )

    @app.get("/cabinet")
    async def cabinet(request: Request, db: Session = Depends(get_db)):
        user = _get_current_user(request, db)
        if not user:
            return templates.TemplateResponse(
                request,
                "cabinet.html",
                {
                    "request": request,
                    "user": None,
                    "subscription": None,
                },
            )

        subscription = get_active_subscription(db, user.id)

        return templates.TemplateResponse(
            request,
            "cabinet.html",
            {
                "request": request,
                "user": user,
                "subscription": subscription,
            },
        )

    @app.post("/auth/telegram")
    async def auth_telegram(request: Request, db: Session = Depends(get_db)):
        payload = await request.json()
        init_data = payload.get("init_data", "")
        validated, auth_error = validate_telegram_webapp_data_verbose(init_data, BOT_TOKEN)
        if not validated:
            raise HTTPException(status_code=401, detail=auth_error or "Invalid Telegram initData")

        telegram_user = json.loads(validated["user"])
        telegram_id = telegram_user.get("id")
        if not telegram_id:
            raise HTTPException(status_code=400, detail="Telegram user is missing id")

        user = get_or_create_telegram_user(db, telegram_user)

        request.session["user_id"] = user.id

        return JSONResponse(
            {
                "ok": True,
                "user": {
                    "id": user.id,
                    "telegram_id": user.telegram_id,
                    "username": user.username,
                    "first_name": user.first_name,
                },
            }
        )

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return JSONResponse({"ok": True})

    @app.post("/payments/platega/callback")
    async def platega_callback(request: Request, db: Session = Depends(get_db)):
        merchant_id = request.headers.get("X-MerchantId")
        secret = request.headers.get("X-Secret")
        payload = await request.json()

        if not merchant_id or not secret:
            raise HTTPException(status_code=401, detail="Missing Platega auth headers")
        if not PLATEGA_MERCHANT_ID or not PLATEGA_SECRET:
            raise HTTPException(status_code=503, detail="Platega is not configured")
        if merchant_id != PLATEGA_MERCHANT_ID or secret != PLATEGA_SECRET:
            raise HTTPException(status_code=401, detail="Invalid Platega auth headers")

        payment, confirmed = handle_platega_callback(db, payload)
        return JSONResponse(
            {
                "ok": True,
                "payment": {
                    "id": payment.platega_transaction_id,
                    "status": payment.status,
                    "confirmed": confirmed,
                    "plan_code": payment.plan_code,
                },
            }
        )

    @app.post("/api/payments/platega/create")
    async def create_payment(request: Request, db: Session = Depends(get_db)):
        user = _get_current_user(request, db)
        if not user:
            raise HTTPException(status_code=401, detail="Unauthorized")

        payload = await request.json()
        plan_code = payload.get("plan_code")
        if not plan_code:
            raise HTTPException(status_code=400, detail="Missing plan_code")

        try:
            payment = await create_platega_payment(db, user, plan_code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PlategaIntegrationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(
            {
                "ok": True,
                "payment": {
                    "id": payment.platega_transaction_id,
                    "url": payment.payment_url,
                    "amount": payment.amount,
                    "plan_name": payment.plan_name,
                    "status": payment.status,
                },
            }
        )

    @app.get("/api/payments/{payment_id}")
    async def get_payment_status(
        payment_id: str, request: Request, db: Session = Depends(get_db)
    ):
        user = _get_current_user(request, db)
        if not user:
            raise HTTPException(status_code=401, detail="Unauthorized")

        payment = db.scalar(
            select(PaymentTransaction).where(
                PaymentTransaction.platega_transaction_id == payment_id,
                PaymentTransaction.user_id == user.id,
            )
        )
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")

        return {
            "ok": True,
            "payment": {
                "id": payment.platega_transaction_id,
                "status": payment.status,
                "amount": payment.amount,
                "plan_name": payment.plan_name,
                "payment_url": payment.payment_url,
                "confirmed_at": payment.confirmed_at.isoformat() if payment.confirmed_at else None,
            },
        }

    @app.post("/telegram")
    async def get_update(request: Request):
        bot_app = getattr(request.app.state, "bot_app", None)
        if not bot_app:
            bot_error = getattr(request.app.state, "bot_error", "Telegram bot is not configured")
            return JSONResponse(
                {"ok": False, "detail": bot_error},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if getattr(request.app.state, "bot_mode", None) != "webhook":
            return JSONResponse(
                {"ok": False, "detail": "Webhook mode is disabled"},
                status_code=status.HTTP_409_CONFLICT,
            )
        payload = await request.json()
        update = Update.de_json(payload, bot_app.bot)
        await bot_app.update_queue.put(update)
        return Response(status_code=status.HTTP_200_OK)

    @app.get("/health/telegram")
    async def telegram_health(request: Request):
        return JSONResponse(
            {
                "configured": bool(getattr(request.app.state, "bot_app", None)),
                "mode": getattr(request.app.state, "bot_mode", None),
                "error": getattr(request.app.state, "bot_error", None),
            }
        )

    @app.get("/timur")
    async def timur(request: Request):
        bot_app = getattr(request.app.state, "bot_app", None)
        if not bot_app:
            return JSONResponse(
                {"ok": False, "detail": "Telegram bot is not configured"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        await bot_app.bot.send_message(
            chat_id=1668408264, text="кто то зашёл на страницу"
        )
        return JSONResponse({"message": "OK"})
    
    @app.get('/connect')
    async def connect(request: Request):
        return templates.TemplateResponse(
            request,
            "connect.html",
            {'request': request}
        )
    @app.get('/price')
    async def price(request: Request):
        return templates.TemplateResponse(
            request,
            "price.html",
            {'request': request}
        )

    @app.get("/payment-widget")
    async def payment_widget(request: Request):
        plan_code = request.query_params.get("plan_code", "")
        return templates.TemplateResponse(
            request,
            "payment_widget.html",
            {
                "request": request,
                "plan_code": plan_code,
            },
        )

    
    return app


async def lifespan(app: FastAPI):
    try:
        Base.metadata.create_all(bind=engine)
        app.state.db_ready = True
    except SQLAlchemyError as exc:
        app.state.db_ready = False
        app.state.db_error = str(exc)
        # Keep app alive to avoid full outage if DB credentials are invalid.
        print(f"[flora_vpn] Database startup warning: {exc}")

    app.state.bot_app = None
    app.state.bot_mode = None
    app.state.bot_error = None

    if not _has_value(BOT_TOKEN):
        app.state.bot_error = "BOT_TOKEN is not configured"
        print(f"[flora_vpn] Telegram bot startup skipped: {app.state.bot_error}")
        yield
        return

    try:
        bot_app = init_bot()
        app.state.bot_app = bot_app
        await bot_app.initialize()
        await bot_app.start()

        if _has_value(WEBHOOK_URL):
            await bot_app.bot.set_webhook(WEBHOOK_URL + "/telegram")
            app.state.bot_mode = "webhook"
            app.state.bot_error = None
            print("[flora_vpn] Telegram bot started in webhook mode")
        else:
            await bot_app.bot.delete_webhook(drop_pending_updates=True)
            if bot_app.updater is None:
                raise RuntimeError("Telegram updater is unavailable for polling mode")
            await bot_app.updater.start_polling()
            app.state.bot_mode = "polling"
            app.state.bot_error = None
            print("[flora_vpn] Telegram bot started in polling mode")

        yield
    except InvalidToken as exc:
        app.state.bot_app = None
        app.state.bot_mode = None
        app.state.bot_error = f"Invalid BOT_TOKEN: {exc}"
        print(f"[flora_vpn] Telegram bot startup skipped: {app.state.bot_error}")
        yield
    except TelegramError as exc:
        app.state.bot_app = None
        app.state.bot_mode = None
        app.state.bot_error = f"Telegram startup warning: {exc}"
        print(f"[flora_vpn] Telegram bot startup warning: {exc}")
        yield
    except Exception as exc:
        app.state.bot_app = None
        app.state.bot_mode = None
        app.state.bot_error = f"Telegram startup error: {exc}"
        print(f"[flora_vpn] Telegram bot startup error: {exc}")
        yield
    finally:
        bot_app = getattr(app.state, "bot_app", None)
        if bot_app:
            if app.state.bot_mode == "polling" and bot_app.updater is not None:
                await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()