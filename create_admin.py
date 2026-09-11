"""One-off CLI to provision an admin account.

There is no public signup path for admins -- run this manually (once per
admin hire) on a machine that has access to the Apps Script Web App. If you'd
rather add the row directly in the Google Sheet yourself, see hash_password.py
instead -- it only prints the password hash you need to paste in.

Usage:
    python create_admin.py
"""

import getpass

from app.auth import hash_password
from app.models import UserRole
from app.sheet_store import StoreError, create_user, find_user_by_email


def main():
    try:
        email = input("Admin email: ").strip().lower()
        full_name = input("Full name: ").strip()
        password = getpass.getpass("Password (min 8 chars): ")
        if len(password) < 8:
            print("Password must be at least 8 characters.")
            return

        if find_user_by_email(email):
            print(f"A user with email {email} already exists.")
            return

        create_user(email, hash_password(password), full_name, UserRole.admin)
        print(f"Admin account created for {email}.")
    except StoreError as exc:
        print(f"Could not reach Google Sheets: {exc}")


if __name__ == "__main__":
    main()
