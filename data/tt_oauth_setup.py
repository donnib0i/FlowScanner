"""
One-time TastyTrade OAuth setup:  python3 -m data.tt_oauth_setup [--railway]

An OAuth grant is the only TastyTrade auth that completes without an SMS, so
it is the only one that works on the Railway container (live flow, dxFeed open
interest) and the only one that keeps working locally past a session's expiry.

Values are read with getpass -- never echoed, never in shell history -- then
proven against the token endpoint before anything is saved. --railway also
sets them on the linked Railway service (check `railway status` first).
"""
import getpass
import subprocess
import sys

import httpx

from data.tt_flow import TT_OAUTH_TOKEN_URL, USER_AGENT, save_oauth


def _exchange(secret: str, refresh: str, client_id: str) -> bool:
    payload = {"grant_type": "refresh_token", "refresh_token": refresh,
               "client_secret": secret}
    if client_id:
        payload["client_id"] = client_id
    r = httpx.post(TT_OAUTH_TOKEN_URL, data=payload, timeout=15,
                   headers={"User-Agent": USER_AGENT})
    # Status only: the body can echo the submitted secret.
    print(f"  token endpoint: HTTP {r.status_code}")
    return r.status_code == 200 and bool(r.json().get("access_token"))


def main() -> int:
    print("TastyTrade OAuth grant -- paste values (input is hidden).")
    secret = getpass.getpass("  Client secret: ").strip()
    refresh = getpass.getpass("  Refresh token (from Create Grant): ").strip()
    client_id = getpass.getpass("  Client ID (optional, Enter to skip): ").strip()
    if not secret or not refresh:
        print("Both the client secret and the refresh token are required.")
        return 1

    if not _exchange(secret, refresh, client_id):
        print("Rejected -- nothing saved. Re-copy both values and retry.")
        return 1
    print(f"  saved: {save_oauth(secret, refresh, client_id)}")

    if "--railway" in sys.argv:
        args = ["railway", "variables",
                "--set", f"TT_CLIENT_SECRET={secret}",
                "--set", f"TT_REFRESH_TOKEN={refresh}"]
        if client_id:
            args += ["--set", f"TT_CLIENT_ID={client_id}"]
        ok = subprocess.run(args, stdout=subprocess.DEVNULL).returncode == 0
        print("  railway: set (redeploying)" if ok else "  railway: FAILED")
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
