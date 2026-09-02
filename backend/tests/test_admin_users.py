from app.auth import create_user


def login(client, email, password):
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200


def test_admin_can_create_and_disable_user_while_member_cannot(client, session_factory):
    with session_factory() as db:
        admin = create_user(
            db,
            email="admin@example.com",
            display_name="管理员",
            password="Admin-password-2026",
            role="admin",
        )
        member = create_user(
            db,
            email="member@example.com",
            display_name="普通用户",
            password="Member-password-2026",
        )
        admin_id = admin.id
        member_id = member.id
    login(client, "member@example.com", "Member-password-2026")
    assert client.get("/api/v1/admin/users").status_code == 403
    client.post("/api/v1/auth/logout")
    login(client, "admin@example.com", "Admin-password-2026")
    created = client.post(
        "/api/v1/admin/users",
        json={
            "account": "created-reader",
            "temporary_password": "Temporary-password-2026",
        },
    )
    assert created.status_code == 201
    assert created.json()["email"] == "created-reader"
    assert created.json()["must_change_password"] is True
    disabled = client.patch(f"/api/v1/admin/users/{member_id}", json={"is_active": False})
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False
    self_disable = client.patch(f"/api/v1/admin/users/{admin_id}", json={"is_active": False})
    assert self_disable.status_code == 422
