"""Mint a token for manual testing until the auth service exists.

    python -m common.devtoken admin [analyst ...]
"""
import sys

from common.auth import ROLES, create_token

if __name__ == "__main__":
    roles = sys.argv[1:] or ["admin"]
    if set(roles) - ROLES:
        sys.exit(f"roles must be among {sorted(ROLES)}")
    print(create_token(f"dev-{'-'.join(roles)}", roles))
