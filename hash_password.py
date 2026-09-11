"""Prints a bcrypt hash for a password, to paste into the Google Sheet's
Users tab (hashed_password column) when adding an admin row by hand.

The Sheet's Users columns are, in order:
    id, email, hashed_password, full_name, role, created_at

- id: one higher than the current highest id in the Users tab (check the sheet).
- hashed_password: paste the output of this script.
- role: must be exactly "admin" (or "student") -- any other value breaks login
  for that row.
- created_at: any ISO date/time, or leave blank -- it isn't used for anything.

Usage:
    python hash_password.py
"""

import getpass

from app.auth import hash_password


def main():
    password = getpass.getpass("Password to hash: ")
    confirm = getpass.getpass("Type it again to confirm: ")
    if password != confirm:
        print("Passwords didn't match -- nothing printed, try again.")
        return

    print()
    print("Paste this into the hashed_password column:")
    print(hash_password(password))


if __name__ == "__main__":
    main()
