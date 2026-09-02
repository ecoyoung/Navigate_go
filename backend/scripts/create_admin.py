import argparse
import secrets
import string

from sqlalchemy import select

from app.auth import create_user, normalize_email
from app.database import SessionLocal
from app.models import User


def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "-_!@"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(char.islower() for char in password) and any(
            char.isupper() for char in password
        ) and any(char.isdigit() for char in password):
            return password


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the first local Navigate admin")
    parser.add_argument("--email", default="admin@navigate.local")
    parser.add_argument("--display-name", default="Navigate 管理员")
    parser.add_argument(
        "--generate-password",
        action="store_true",
        help="Generate and print a one-time temporary password.",
    )
    args = parser.parse_args()
    if not args.generate_password:
        parser.error("--generate-password is required; plaintext CLI passwords are not accepted")
    password = generate_password()
    with SessionLocal() as db:
        email = normalize_email(args.email)
        existing = db.scalar(select(User).where(User.email == email))
        if existing:
            raise SystemExit(f"Admin already exists: {email}")
        user = create_user(
            db,
            email=email,
            display_name=args.display_name,
            password=password,
            role="admin",
            must_change_password=True,
        )
    print(f"Admin created: {user.email}")
    print(f"Temporary password: {password}")
    print("Change this password immediately after first login.")


if __name__ == "__main__":
    main()
