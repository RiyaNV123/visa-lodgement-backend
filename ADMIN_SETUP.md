# Adding an admin account

Students self-signup through the app. Admins don't — there is no admin signup screen anywhere in the app. You add admin accounts yourself, directly in the Google Sheet.

## Steps

1. Open the app's Google Sheet and go to the **Users** tab. It has 6 columns, in this order:

   | id | email | hashed_password | full_name | role | created_at |
   |---|---|---|---|---|---|

2. Pick the next **id**: one higher than whatever the highest number already in that column is.

3. Get a **hashed_password**. You cannot type a plain password here — the app only ever stores a hash, never the real password, so a plain-text value will never match at login. Instead, on the machine with the backend set up, run:

   ```bash
   cd backend
   source .venv/Scripts/activate
   python hash_password.py
   ```

   It asks for the password twice (hidden, like a normal password prompt) and prints a hash that looks like `$2b$12$......`. Copy that whole string into the `hashed_password` cell.

4. Fill in **email** and **full_name** normally.

5. **role** must be typed exactly as `admin` (lowercase). Anything else (including `Admin`, `staff`, or a typo) will break that account — the app will error out when it tries to read that row.

6. **created_at** can be left blank — it isn't used for anything for admin accounts.

7. Save the sheet. The new admin can log in immediately at the app's normal login page with the email and the password you hashed in step 3 (not the hash itself — the real password).

## Why not just build a signup form for this?

Keeping admin provisioning out of the app entirely means there's no admin-creation endpoint to secure, no risk of it being exposed by a bug, and no code path a student could ever reach. The one downside is exactly the friction above (steps 2, 3, 5) — if that becomes annoying, the next step up would be a proper in-app "add admin" screen visible only to existing admins, but that's a separate, larger piece of work (and needs at least one admin account created this way first, to bootstrap it).

## Alternative: create_admin.py

If you'd rather not touch the Sheet UI at all, `backend/create_admin.py` does steps 2–6 for you automatically (prompts for email/name/password, writes the row itself with the correct id and hash). Run it the same way:

```bash
cd backend
source .venv/Scripts/activate
python create_admin.py
```

It must be run in a real interactive terminal — piping input into it won't work (the password prompt reads directly from the console).
