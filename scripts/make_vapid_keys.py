"""通知を送るための鍵の組（VAPID）を作って、ファイルに書き出す。

    python scripts/make_vapid_keys.py

できた vapid_keys.txt の2行を、Render の環境変数（VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY）に入れる。
秘密の鍵は 人に見せない（ファイルは git に入れない。入れ終わったら消してよい）。
"""

import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

OUT = Path(__file__).resolve().parent.parent / "vapid_keys.txt"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_keys() -> tuple[str, str]:
    """（公開鍵, 秘密鍵）。どちらも 通知の仕組みで決まっている形（base64url）。"""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64(public), b64(private)


if __name__ == "__main__":
    if OUT.exists():
        raise SystemExit(f"{OUT.name} はもうあります（作り直すと、今の通知の登録が使えなくなる）。作り直すなら 先に消してね。")
    public, private = make_keys()
    OUT.write_text(f"VAPID_PUBLIC_KEY={public}\nVAPID_PRIVATE_KEY={private}\n", encoding="utf-8")
    print(f"{OUT} に書き出したよ。")
