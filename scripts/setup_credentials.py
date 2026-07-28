"""
Store your UQ username and password so the browser login auto-fills them.
You still approve the Okta push notification — credentials are never sent anywhere
except to UQ's own login page via the browser.

The password goes into the OS keyring (Windows Credential Manager / macOS
Keychain / Secret Service), never to a file. Only the username is written to
~/.blackboard_mcp/credentials.json.

Run once:  python scripts/setup_credentials.py
"""
import getpass
import sys
sys.path.insert(0, "src")

from blackboard_mcp.auth import CREDS_FILE, KEYRING_SERVICE, save_credentials

print("UQ Blackboard credentials setup")
print("Password is stored in your OS keyring; only the username hits disk.\n")

username = input("UQ username (e.g. s1234567): ").strip()
password = getpass.getpass("UQ password: ")

if not username or not password:
    print("Aborted — username or password was empty.")
    sys.exit(1)

save_credentials(username, password)
print(f"\nUsername saved to {CREDS_FILE}")
print(f"Password saved to the OS keyring under service '{KEYRING_SERVICE}'.")
print("Next login will auto-fill your credentials — just approve Okta on your phone.")
