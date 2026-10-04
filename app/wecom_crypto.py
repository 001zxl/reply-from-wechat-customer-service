"""企业微信回调加解密（WXBizMsgCrypt 的最小可用实现）。

只实现本系统需要的两件事：
1. 回调 URL 验证（解密 echostr）
2. 解密回调 body 里的 Encrypt
加密回包用不到 —— 微信客服是异步拉取模式，回调只需要返回空串。
"""

from __future__ import annotations

import base64
import hashlib
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class WeComCryptoError(Exception):
    pass


def _unpad(data: bytes) -> bytes:
    if not data:
        raise WeComCryptoError("空密文")
    pad = data[-1]
    if pad < 1 or pad > 32 or pad > len(data):
        raise WeComCryptoError("填充非法")
    return data[:-pad]


class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, receive_id: str) -> None:
        if len(encoding_aes_key) != 43:
            raise WeComCryptoError("EncodingAESKey 长度必须为 43")
        try:
            self.key = base64.b64decode(encoding_aes_key + "=")
        except Exception as exc:
            raise WeComCryptoError(f"EncodingAESKey 不是合法 base64：{exc}") from exc
        self.token = token
        self.receive_id = receive_id
        self.iv = self.key[:16]

    def signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        parts = sorted([self.token, timestamp, nonce, encrypt])
        return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    def verify_signature(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
        return self.signature(timestamp, nonce, encrypt) == msg_signature

    def decrypt(self, encrypt: str) -> str:
        try:
            raw = base64.b64decode(encrypt)
        except Exception as exc:
            raise WeComCryptoError(f"密文不是合法 base64：{exc}") from exc
        decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).decryptor()
        plain = _unpad(decryptor.update(raw) + decryptor.finalize())
        if len(plain) < 20:
            raise WeComCryptoError("明文长度异常")
        msg_len = struct.unpack("!I", plain[16:20])[0]
        if msg_len < 0 or 20 + msg_len > len(plain):
            raise WeComCryptoError("明文长度字段异常")
        message = plain[20 : 20 + msg_len]
        receive_id = plain[20 + msg_len :].decode("utf-8", "ignore")
        if self.receive_id and receive_id != self.receive_id:
            raise WeComCryptoError("receive_id 不匹配，可能不是本企业的回调")
        return message.decode("utf-8")

    def decrypt_echo(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        if not self.verify_signature(msg_signature, timestamp, nonce, echostr):
            raise WeComCryptoError("URL 验证签名不通过")
        return self.decrypt(echostr)
