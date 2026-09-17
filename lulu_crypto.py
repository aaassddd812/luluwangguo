# -*- coding: utf-8 -*-
"""露露王国应用层加解密（逆向自热更 DLL AesUtils.cs）
格式: Base64( 0x01 || IV(16) || AES-CBC-PKCS7(encKey, pt) || HMAC-SHA256(macKey)(32) )
encKey = HMAC_SHA256(sharedKey, "simplecrypt/v1 enc")[:len(sharedKey)]
macKey = HMAC_SHA256(sharedKey, "simplecrypt/v1 mac")
"""
import base64
import hmac
import hashlib

from Crypto.Cipher import AES

SHARED_KEY = base64.b64decode("qxc0UsIWDisnibB5DlGyHcfDX+whozD5+a+qsOWM3JU=")


def derive_keys(key: bytes):
    enc = hmac.new(key, b"simplecrypt/v1 enc", hashlib.sha256).digest()[:len(key)]
    mac = hmac.new(key, b"simplecrypt/v1 mac", hashlib.sha256).digest()
    return enc, mac


_ENC, _MAC = derive_keys(SHARED_KEY)


def decrypt(packet_b64: str) -> str:
    data = base64.b64decode(packet_b64)
    assert data[0] == 1, "版本字节不是 1"
    iv = data[1:17]
    tag = data[-32:]
    ct = data[17:-32]
    # 校验 HMAC: over (ver || iv || ct)
    expect = hmac.new(_MAC, bytes([1]) + iv + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expect):
        raise ValueError("HMAC 校验失败")
    pt = AES.new(_ENC, AES.MODE_CBC, iv).decrypt(ct)
    pad = pt[-1]
    if not (1 <= pad <= 16) or pt[-pad:] != bytes([pad]) * pad:
        raise ValueError("填充错误")
    return pt[:-pad].decode("utf-8")


def encrypt(plaintext: str) -> str:
    pt = plaintext.encode("utf-8")
    pad = 16 - len(pt) % 16
    pt += bytes([pad]) * pad
    iv = hashlib.sha256(bytes([len(plaintext) & 0xFF])).digest()[:16]  # 占位，见下方随机版
    import os
    iv = os.urandom(16)
    ct = AES.new(_ENC, AES.MODE_CBC, iv).encrypt(pt)
    tag = hmac.new(_MAC, bytes([1]) + iv + ct, hashlib.sha256).digest()
    return base64.b64encode(bytes([1]) + iv + ct + tag).decode()


if __name__ == "__main__":
    # 自测
    s = '{"hello":"world"}'
    assert decrypt(encrypt(s)) == s
    print("[OK] 自测通过, encKey =", _ENC.hex(), " macKey =", _MAC.hex())
