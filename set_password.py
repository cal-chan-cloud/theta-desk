"""Set or change the Theta Desk password.

    python set_password.py              # prompt twice, nothing echoed
    python set_password.py --generate   # make a strong one and write it out
    python set_password.py --status     # is a password configured?
    python set_password.py --clear      # remove the gate entirely

The plaintext is never stored or printed to the terminal by default -- only a
PBKDF2 hash in data/auth.json, which is gitignored.  `--generate` writes the new
password to data/INITIAL_PASSWORD.txt (also gitignored) so it never has to
travel through a shell history or a scrollback buffer.
"""

import argparse
import getpass
import os
import secrets
import string
import sys

import auth
import config

PLAINTEXT_FILE = os.path.join(config.DATA_DIR, "INITIAL_PASSWORD.txt")


def generate(length=20):
    """Ambiguity-free alphabet: no l/1/I, no O/0, so it can be read aloud."""
    alphabet = ("abcdefghijkmnopqrstuvwxyz"
                "ABCDEFGHJKLMNPQRSTUVWXYZ"
                "23456789" + "!@#$%^&*-_=+")
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in string.punctuation for c in pw)):
            return pw


def main():
    ap = argparse.ArgumentParser(description="Manage the Theta Desk password")
    ap.add_argument("--generate", action="store_true",
                    help="generate a strong password and write it to a local file")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--clear", action="store_true")
    args = ap.parse_args()

    if args.status:
        if os.environ.get("THETA_DESK_PASSWORD"):
            print("A password is set via the THETA_DESK_PASSWORD environment variable.")
        elif auth.is_enabled():
            print("A password is set (hash stored in %s)." % auth.AUTH_FILE)
        else:
            print("NO password is set — the site is open to anyone who can reach it.")
        return 0

    if args.clear:
        data = auth._read()
        data.pop("password", None)
        auth._write(data)
        print("Password removed. The site is now open to anyone who can reach it.")
        return 0

    if args.generate:
        pw = generate()
        auth.set_password(pw)
        with open(PLAINTEXT_FILE, "w", encoding="utf-8") as f:
            f.write(pw + "\n")
        try:
            os.chmod(PLAINTEXT_FILE, 0o600)
        except OSError:
            pass
        print("A strong password has been generated and set.")
        print("It is written to:\n    %s" % PLAINTEXT_FILE)
        print("\nThat file is gitignored. Save the password somewhere safe, then")
        print("delete the file. Change it any time with:  python set_password.py")
        return 0

    pw = getpass.getpass("New password (min 8 chars, not echoed): ")
    again = getpass.getpass("Confirm: ")
    if pw != again:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    try:
        auth.set_password(pw)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print("Password set. Restart the server for it to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
