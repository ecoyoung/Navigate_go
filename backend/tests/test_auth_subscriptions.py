from sqlalchemy import func, select

from app.auth import COOKIE_NAME, create_user, verify_password
from app.models import AuthSession, Domain, User, UserSubscription

ADMIN_EMAIL = "admin@navigate.local"
ADMIN_PASSWORD = "Initial-Admin-2026!"


def seed_admin_and_domain(session_factory):
    with session_factory() as db:
        admin = create_user(
            db,
            email=ADMIN_EMAIL,
            display_name="Navigate 管理员",
            password=ADMIN_PASSWORD,
            role="admin",
            must_change_password=True,
        )
        domain = Domain(key="beauty", name="美妆行业", is_enabled=True)
        db.add(domain)
        db.commit()
        return admin.id, domain.id


def test_first_registration_bootstraps_admin_with_account_and_password(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "account": "platform-owner",
            "password": "Reader-Password-2026!",
        },
    )
    assert response.status_code == 201
    assert response.json()["user"]["email"] == "platform-owner"
    assert response.json()["user"]["role"] == "admin"

    login = client.post(
        "/api/v1/auth/login",
        json={"account": "platform-owner", "password": "Reader-Password-2026!"},
    )
    assert login.status_code == 200


def test_login_session_and_logout(client, session_factory):
    seed_admin_and_domain(session_factory)
    login = client.post(
        "/api/v1/auth/login",
        json={"email": ADMIN_EMAIL.upper(), "password": ADMIN_PASSWORD},
    )
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "admin"
    assert COOKIE_NAME in login.cookies
    assert login.cookies.get(COOKIE_NAME) != ADMIN_PASSWORD
    assert client.get("/api/v1/auth/me").json()["email"] == ADMIN_EMAIL

    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AuthSession)) == 1
        assert db.scalar(select(AuthSession)).revoked_at is not None


def test_member_registration_and_domain_subscription(client, session_factory):
    seed_admin_and_domain(session_factory)
    register = client.post(
        "/api/v1/auth/register",
        json={
            "account": "industry-reader",
            "password": "Reader-Password-2026!",
        },
    )
    assert register.status_code == 201
    assert register.json()["user"]["email"] == "industry-reader"

    subscribed = client.put(
        "/api/v1/subscriptions/beauty",
        json={"status": "active", "delivery_type": "daily_brief"},
    )
    assert subscribed.status_code == 200
    assert subscribed.json()["domain_key"] == "beauty"
    assert subscribed.json()["status"] == "active"

    paused = client.put(
        "/api/v1/subscriptions/beauty",
        json={"status": "paused", "delivery_type": "daily_brief"},
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    assert client.get("/api/v1/subscriptions").json()[0]["status"] == "paused"

    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(UserSubscription)) == 1
        member = db.scalar(select(User).where(User.email == "industry-reader"))
        assert member is not None
        assert verify_password("Reader-Password-2026!", member.password_hash)
        assert "Reader-Password-2026!" not in member.password_hash


def test_admin_can_replace_temporary_password(client, session_factory):
    seed_admin_and_domain(session_factory)
    client.post(
        "/api/v1/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    changed = client.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": ADMIN_PASSWORD,
            "new_password": "Replacement-Admin-2026!",
        },
    )
    assert changed.status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401
    relogin = client.post(
        "/api/v1/auth/login",
        json={"email": ADMIN_EMAIL, "password": "Replacement-Admin-2026!"},
    )
    assert relogin.status_code == 200
    assert relogin.json()["user"]["must_change_password"] is False
