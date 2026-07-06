import asyncio
import json
import urllib.error
import urllib.request
from uuid import uuid4
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.config import (
    PLATEGA_API_URL,
    PLATEGA_FAILED_URL,
    PLATEGA_MERCHANT_ID,
    PLATEGA_RETURN_URL,
    PLATEGA_SECRET,
    WEBHOOK_URL,
)
from db.models import PaymentTransaction, Subscription, User, VpnKey


PLAN_DEFINITIONS = {
    "200_1m": {"plan_name": "200/месяц", "days": 30, "amount": 200},
    "500_3m": {"plan_name": "500/3 месяца", "days": 90, "amount": 500},
    "900_6m": {"plan_name": "900/6 месяцев", "days": 180, "amount": 900},
}


class PlategaIntegrationError(RuntimeError):
    pass


def get_or_create_telegram_user(db: Session, telegram_user: dict) -> User:
    telegram_id = telegram_user.get("id")
    if not telegram_id:
        raise ValueError("Telegram user is missing id")

    user = db.scalar(select(User).where(User.telegram_id == int(telegram_id)))
    if not user:
        user = User(
            telegram_id=int(telegram_id),
            username=telegram_user.get("username"),
            first_name=telegram_user.get("first_name"),
        )
        db.add(user)
    else:
        user.username = telegram_user.get("username")
        user.first_name = telegram_user.get("first_name")

    db.commit()
    db.refresh(user)
    return user


def get_active_subscription(db: Session, user_id: int) -> Subscription | None:
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


def ensure_default_amnezia_keys(db: Session, user_id: int) -> list[VpnKey]:
    existing = db.scalars(select(VpnKey).where(VpnKey.user_id == user_id)).all()
    if existing:
        return existing

    defaults = [
        VpnKey(user_id=user_id, server_name="Netherlands", vpn_link="vpn://xxxx"),
        VpnKey(user_id=user_id, server_name="Germany", vpn_link="vpn://yyyy"),
    ]
    db.add_all(defaults)
    db.commit()
    return db.scalars(select(VpnKey).where(VpnKey.user_id == user_id)).all()


def activate_subscription(db: Session, user_id: int, plan_code: str) -> tuple[Subscription, list[VpnKey]]:
    plan_data = PLAN_DEFINITIONS.get(plan_code)
    if not plan_data:
        raise ValueError("Unknown plan")

    now = datetime.now(timezone.utc)
    db.query(Subscription).filter(
        Subscription.user_id == user_id,
        Subscription.status == "active",
    ).update({"status": "expired"})

    subscription = Subscription(
        user_id=user_id,
        plan_name=plan_data["plan_name"],
        status="active",
        start_date=now,
        expires_at=now + timedelta(days=plan_data["days"]),
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)

    keys = ensure_default_amnezia_keys(db, user_id)
    return subscription, keys


def _build_public_url(path: str) -> str | None:
    if PLATEGA_RETURN_URL and path == "/cabinet":
        return PLATEGA_RETURN_URL
    if PLATEGA_FAILED_URL and path == "/price":
        return PLATEGA_FAILED_URL
    if not WEBHOOK_URL:
        raise PlategaIntegrationError(
            "WEBHOOK_URL or Platega return URLs are not configured"
        )
    return WEBHOOK_URL.rstrip("/") + path


def _create_platega_transaction(payload: dict) -> dict:
    if not PLATEGA_MERCHANT_ID or not PLATEGA_SECRET:
        raise PlategaIntegrationError("Platega credentials are not configured")

    request_body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{PLATEGA_API_URL.rstrip('/')}/v2/transaction/process",
        data=request_body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-MerchantId": PLATEGA_MERCHANT_ID,
            "X-Secret": PLATEGA_SECRET,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw_response = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        raise PlategaIntegrationError(f"Platega API error: {exc.code} {error_body}") from exc
    except urllib.error.URLError as exc:
        raise PlategaIntegrationError(f"Platega API unavailable: {exc.reason}") from exc

    try:
        return json.loads(raw_response or "{}")
    except json.JSONDecodeError as exc:
        raise PlategaIntegrationError("Platega API returned invalid JSON") from exc


async def create_platega_payment(
    db: Session,
    user: User,
    plan_code: str,
) -> PaymentTransaction:
    plan_data = PLAN_DEFINITIONS.get(plan_code)
    if not plan_data:
        raise ValueError("Unknown plan")

    payment_ref = str(uuid4())
    payment_payload = f"flora_vpn:{user.id}:{plan_code}:{payment_ref}"
    payment_details = {
        "amount": plan_data["amount"],
        "currency": "RUB",
        "description": f"Flora VPN — {plan_data['plan_name']}",
        "return": _build_public_url("/cabinet"),
        "failedUrl": _build_public_url("/price"),
        "payload": payment_payload,
    }
    metadata = {
        "userId": str(user.telegram_id),
        "userName": f"@{user.username}" if user.username else (user.first_name or str(user.telegram_id)),
    }
    response = await asyncio.to_thread(
        _create_platega_transaction,
        {
            "paymentDetails": payment_details,
            "metadata": metadata,
        },
    )

    transaction_id = response.get("transactionId")
    payment_url = response.get("url")
    if not transaction_id or not payment_url:
        raise PlategaIntegrationError("Platega API response is missing transaction data")

    payment = PaymentTransaction(
        user_id=user.id,
        plan_code=plan_code,
        plan_name=plan_data["plan_name"],
        amount=plan_data["amount"],
        currency="RUB",
        status=response.get("status", "PENDING"),
        platega_transaction_id=transaction_id,
        payment_url=payment_url,
        payload=payment_payload,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


def _find_payment_by_reference(db: Session, payment_id: str) -> PaymentTransaction | None:
    return db.scalar(
        select(PaymentTransaction).where(
            PaymentTransaction.platega_transaction_id == payment_id,
        )
    )


def handle_platega_callback(db: Session, callback_payload: dict) -> tuple[PaymentTransaction, bool]:
    payment_id = callback_payload.get("id")
    status = (callback_payload.get("status") or "").upper()
    if not payment_id:
        raise ValueError("Missing payment id")

    payment = _find_payment_by_reference(db, str(payment_id))
    if not payment:
        raise ValueError("Payment transaction not found")

    if payment.status == "CONFIRMED" and status == "CONFIRMED":
        return payment, False

    payment.status = status or payment.status

    if status == "CONFIRMED":
        payment.confirmed_at = datetime.now(timezone.utc)
        payment_user = db.scalar(select(User).where(User.id == payment.user_id))
        if not payment_user:
            raise ValueError("Payment user not found")
        activate_subscription(db, payment_user.id, payment.plan_code)

    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment, status == "CONFIRMED"


def serialize_subscription(subscription: Subscription | None) -> dict | None:
    if not subscription:
        return None

    return {
        "plan_name": subscription.plan_name,
        "status": subscription.status,
        "start_date": subscription.start_date.isoformat(),
        "expires_at": subscription.expires_at.isoformat(),
    }


def serialize_keys(keys: list[VpnKey]) -> list[dict]:
    return [
        {
            "id": key.id,
            "server_name": key.server_name,
            "vpn_link": key.vpn_link,
            "created_at": key.created_at.isoformat(),
        }
        for key in keys
    ]
