import json
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from telegram.error import InvalidToken, TelegramError

from config.config import BOT_TOKEN, SECRET_KEY, WEBHOOK_URL
from server.db import Base, engine, get_db
from server.models import Subscription, User, VpnKey
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


def _get_active_subscription(db: Session, user_id: int) -> Subscription | None:
    now = datetime.now(timezone.utc)
    return db.scalar(
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == "active",
            Subscription.expires_at > now,
        )
        .order_by(Subscription.expires_at.desc())
    )


def _ensure_default_amnezia_keys(db: Session, user_id: int) -> list[VpnKey]:
    existing = db.scalars(select(VpnKey).where(VpnKey.user_id == user_id)).all()
    if existing:
        return existing

    # Placeholder keys. Replace with real provisioning integration.
    defaults = [
        VpnKey(user_id=user_id, server_name="Netherlands", vpn_link="vpn://xxxx"),
        VpnKey(user_id=user_id, server_name="Germany", vpn_link="vpn://yyyy"),
    ]
    db.add_all(defaults)
    db.commit()
    return db.scalars(select(VpnKey).where(VpnKey.user_id == user_id)).all()


def _serialize_subscription(subscription: Subscription | None) -> dict | None:
    if not subscription:
        return None
    return {
        "plan_name": subscription.plan_name,
        "status": subscription.status,
        "start_date": subscription.start_date.isoformat(),
        "expires_at": subscription.expires_at.isoformat(),
    }


def _serialize_keys(keys: list[VpnKey]) -> list[dict]:
    return [
        {
            "id": key.id,
            "server_name": key.server_name,
            "vpn_link": key.vpn_link,
            "created_at": key.created_at.isoformat(),
        }
        for key in keys
    ]


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
                    "vpn_keys": [],
                },
            )

        subscription = _get_active_subscription(db, user.id)
        vpn_keys = []
        if subscription:
            vpn_keys = _ensure_default_amnezia_keys(db, user.id)

        return templates.TemplateResponse(
            request,
            "cabinet.html",
            {
                "request": request,
                "user": user,
                "subscription": subscription,
                "vpn_keys": vpn_keys,
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

        user = db.scalar(select(User).where(User.telegram_id == int(telegram_id)))
        if not user:
            user = User(
                telegram_id=int(telegram_id),
                username=telegram_user.get("username"),
                first_name=telegram_user.get("first_name"),
            )
            db.add(user)
            db.commit()
            db.refresh(user)

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

    @app.get("/api/me")
    async def me(request: Request, db: Session = Depends(get_db)):
        user = _get_current_user(request, db)
        if not user:
            raise HTTPException(status_code=401, detail="Unauthorized")

        subscription = _get_active_subscription(db, user.id)
        keys = db.scalars(select(VpnKey).where(VpnKey.user_id == user.id)).all()
        return {
            "user": {
                "id": user.id,
                "telegram_id": user.telegram_id,
                "username": user.username,
                "first_name": user.first_name,
                "created_at": user.created_at.isoformat(),
            },
            "subscription": _serialize_subscription(subscription),
            "vpn_keys": _serialize_keys(keys),
        }

    @app.post("/api/subscriptions/activate")
    async def activate_subscription(
        request: Request, db: Session = Depends(get_db)
    ):
        user = _get_current_user(request, db)
        if not user:
            raise HTTPException(status_code=401, detail="Unauthorized")

        payload = await request.json()
        plan_code = payload.get("plan_code")
        plans = {
            "200_1m": ("200/месяц", 30),
            "500_3m": ("500/3 месяца", 90),
            "900_6m": ("900/6 месяцев", 180),
        }

        plan_data = plans.get(plan_code)
        if not plan_data:
            raise HTTPException(status_code=400, detail="Unknown plan")

        plan_name, days = plan_data
        now = datetime.now(timezone.utc)

        db.query(Subscription).filter(
            Subscription.user_id == user.id,
            Subscription.status == "active",
        ).update({"status": "expired"})

        subscription = Subscription(
            user_id=user.id,
            plan_name=plan_name,
            status="active",
            start_date=now,
            expires_at=now + timedelta(days=days),
        )
        db.add(subscription)
        db.commit()
        db.refresh(subscription)

        keys = _ensure_default_amnezia_keys(db, user.id)
        return {
            "ok": True,
            "subscription": _serialize_subscription(subscription),
            "vpn_keys": _serialize_keys(keys),
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