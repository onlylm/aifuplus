"""python -m app.manage create-admin --email you@example.com"""
import argparse
import getpass

from .security import hash_password

from .factory import build_core


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    ca = sub.add_parser("create-admin")
    ca.add_argument("--email", required=True)
    ca.add_argument("--password", help="omit to be prompted")
    args = ap.parse_args()
    core = build_core()
    pw = args.password or getpass.getpass("Password (>= 8 chars): ")
    if len(pw) < 8:
        raise SystemExit("password too short")
    email = args.email.strip().lower()
    row = core.conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if row:
        core.conn.execute("UPDATE users SET password_hash=?, is_admin=1 WHERE id=?", (hash_password(pw), row["id"]))
        print("updated existing user and granted admin:", email)
    else:
        core.conn.execute("INSERT INTO users(email, password_hash, is_admin, created_at) VALUES (?,?,1,?)",
                          (email, hash_password(pw), core.now()))
        print("admin created:", email)


if __name__ == "__main__":
    main()
