"""``python -m app [--host 0.0.0.0] [--port 8000]``"""

import argparse

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="하원 셔틀 웹 서비스")
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 lets phones on the same Wi-Fi connect")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    uvicorn.run("app.main:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
