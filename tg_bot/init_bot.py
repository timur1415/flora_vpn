import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    PicklePersistence,
)

from config.config import BOT_TOKEN, WEBHOOK_URL
from db.db import SessionLocal
from db.telegram_service import (
    ensure_default_amnezia_keys,
    get_active_subscription,
    get_or_create_telegram_user,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

MAIN_MENU = 0


def _build_cabinet_url() -> str | None:
    if not WEBHOOK_URL:
        return None
    return WEBHOOK_URL.rstrip("/") + "/cabinet"


def _build_payment_widget_url(plan_code: str) -> str | None:
    if not WEBHOOK_URL:
        return None
    return WEBHOOK_URL.rstrip("/") + f"/payment-widget?plan_code={plan_code}"


def _build_main_keyboard() -> InlineKeyboardMarkup:
    rows = []
    cabinet_url = _build_cabinet_url()
    if cabinet_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Профиль",
                    web_app=WebAppInfo(url=cabinet_url),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Тарифы", callback_data="menu:plans")])
    rows.append([InlineKeyboardButton(text="Мои ключи", callback_data="menu:keys")])
    return InlineKeyboardMarkup(rows)


def _build_plans_keyboard(payment_urls: list[str | None]) -> InlineKeyboardMarkup:
    def _plan_button(text: str, url: str | None) -> InlineKeyboardButton:
        if url:
            return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))
        return InlineKeyboardButton(text=f"{text} - оплата недоступна", callback_data="menu:main")

    return InlineKeyboardMarkup(
        [
            [_plan_button("Оплатить 200 ₽ / месяц", payment_urls[0])],
            [_plan_button("Оплатить 500 ₽ / 3 месяца", payment_urls[1])],
            [_plan_button("Оплатить 900 ₽ / 6 месяцев", payment_urls[2])],
            [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
        ]
    )

def _format_subscription(subscription) -> str:
    if not subscription:
        return "Активной подписки пока нет."

    return (
        f"Тариф: {subscription.plan_name}\n"
        f"Статус: {subscription.status}\n"
        f"Действует до: {subscription.expires_at.strftime('%d.%m.%Y')}"
    )


def _format_keys(keys) -> str:
    if not keys:
        return "Ключи пока не созданы."

    lines = ["Ваши ключи:"]
    for key in keys:
        lines.append(f"• {key.server_name}: {key.vpn_link}")
    return "\n".join(lines)


def _get_bot_user_payload(update: Update) -> dict | None:
    user = update.effective_user
    if not user:
        return None

    return {
        "id": user.id,
        "username": user.username,
        "first_name": user.first_name,
    }


async def _show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Flora VPN\n\n"
        "Профиль открывается в мини-аппе, а тарифы и ключи теперь управляются прямо в боте."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=_build_main_keyboard())
        return

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=_build_main_keyboard(),
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _show_main_menu(update, context)
    return MAIN_MENU


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return MAIN_MENU

    await query.answer()
    data = query.data or ""
    action, _, value = data.partition(":")

    if action == "menu" and value == "main":
        await _show_main_menu(update, context)
        return MAIN_MENU

    telegram_user = _get_bot_user_payload(update)
    if not telegram_user:
        await query.edit_message_text("Не удалось определить пользователя.")
        return MAIN_MENU

    db = SessionLocal()
    try:
        user = get_or_create_telegram_user(db, telegram_user)

        if action == "menu" and value == "plans":
            await query.edit_message_text(
                "Выберите тариф и откройте оплату в виджете.",
                reply_markup=_build_plans_keyboard([
                    _build_payment_widget_url("200_1m"),
                    _build_payment_widget_url("500_3m"),
                    _build_payment_widget_url("900_6m"),
                ]),
            )
            return MAIN_MENU

        if action == "menu" and value == "keys":
            subscription = get_active_subscription(db, user.id)
            if not subscription:
                await query.edit_message_text(
                    "Сначала активируйте подписку в разделе тарифов.",
                    reply_markup=_build_main_keyboard(),
                )
                return MAIN_MENU

            keys = ensure_default_amnezia_keys(db, user.id)
            await query.edit_message_text(
                _format_keys(keys),
                reply_markup=_build_main_keyboard(),
            )
            return MAIN_MENU

        await query.edit_message_text(
            "Меню обновлено.",
            reply_markup=_build_main_keyboard(),
        )
        return MAIN_MENU
    finally:
        db.close()


def create_bot_app() -> Application:
    persistence = PicklePersistence(filepath="flora_vpn_bot")
    application = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(menu_callback, pattern=r"^menu:"),
            ]
        },
        name="flora_vpn_bot",
        persistent=True,
        fallbacks=[CommandHandler("start", start)],
    )

    application.add_handler(conv_handler)
    return application


def init_bot() -> Application:
    return create_bot_app()
