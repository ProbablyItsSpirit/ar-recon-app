"""App-level authentication: password + TOTP (Google Authenticator).

Secrets are read from Streamlit secrets (.streamlit/secrets.toml locally,
the Secrets UI on Streamlit Community Cloud) — never from the repo.

Generate credentials with:  python -m src.auth
"""
from __future__ import annotations

import hashlib
import hmac
import secrets as pysecrets

import pyotp
import streamlit as st

MAX_ATTEMPTS = 5
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or pysecrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS)
    return f"pbkdf2${salt}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "pbkdf2":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk.hex(), digest)


def _load_auth_secrets() -> tuple[str, str, str] | None:
    try:
        auth = st.secrets["AUTH"]
        return str(auth["APP_PASSWORD_HASH"]), str(auth["TOTP_SECRET"]), str(auth.get("APP_NAME", "AR Reconciliation"))
    except Exception:
        return None


def require_auth() -> None:
    """Block the app until password + TOTP verify. Call before any data is shown."""
    if st.session_state.get("authenticated"):
        return

    cfg = _load_auth_secrets()
    if cfg is None:
        st.error(
            "Authentication is not configured. Create `.streamlit/secrets.toml` "
            "(run `python -m src.auth` to generate one) or set secrets in "
            "Streamlit Community Cloud → Settings → Secrets."
        )
        st.stop()
    app_hash, totp_secret, app_name = cfg

    st.title(f"🔐 {app_name}")

    left, center, right = st.columns([3, 2, 3])
    with center:
        with st.form("login", clear_on_submit=False):
            password = st.text_input("Password", type="password")
            code = st.text_input("Auth", max_chars=6)
            submitted = st.form_submit_button("Sign in", type="primary")

    if submitted:
        st.session_state["login_attempts"] = st.session_state.get("login_attempts", 0) + 1
        pw_ok = check_password(password, app_hash)
        otp_ok = bool(code) and pyotp.TOTP(totp_secret).verify(code.strip(), valid_window=1)
        if pw_ok and otp_ok:
            st.session_state["authenticated"] = True
            st.session_state.pop("login_attempts", None)
            st.rerun()
        remaining = MAX_ATTEMPTS - st.session_state["login_attempts"]
        if remaining <= 0:
            st.session_state["locked"] = True
            st.error("Too many failed attempts — reload the page to try again.")
        else:
            st.error(f"Invalid password or code. {remaining} attempt(s) left.")

    if st.session_state.get("locked"):
        st.stop()

    # Not authenticated: never render the rest of the app
    st.stop()


def logout_button() -> None:
    if st.sidebar.button("Log out"):
        for key in ("authenticated", "locked", "login_attempts", "results"):
            st.session_state.pop(key, None)
        st.rerun()


def _cli() -> int:
    """Generate a fresh password hash + TOTP secret and print setup instructions."""
    password = pysecrets.token_urlsafe(12)
    totp_secret = pyotp.random_base32()
    pw_hash = hash_password(password)
    print("Paste this into .streamlit/secrets.toml (or Streamlit Cloud -> Secrets):\n")
    print("[AUTH]")
    print(f'APP_PASSWORD_HASH = "{pw_hash}"')
    print(f'TOTP_SECRET = "{totp_secret}"')
    print('APP_NAME = "AR Reconciliation"')
    print(f"\nPassword (shown once, store it safely): {password}")
    print(f"Manual authenticator key: {totp_secret}")
    print("To enrol a phone: log in first, then use the 'Add authenticator app' panel in the sidebar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
