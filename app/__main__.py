"""``python -m app [--host 0.0.0.0] [--port 8000]``"""

import argparse
import os

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="하원 셔틀 웹 서비스")
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"), help="0.0.0.0 lets phones on the same Wi-Fi connect")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = ap.parse_args()
    # Behind a hosting platform's proxy, set FORWARDED_ALLOW_IPS="*" so client addresses are real.
    trusted = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
    uvicorn.run("app.main:app", host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips=trusted)


if __name__ == "__main__":
    main()
