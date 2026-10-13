import hashlib
import hmac
import secrets


def scram_calculate(password: str, salt_hex: str, iterations: int, auth_msg: str) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex)
    salted_pwd = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32).hex()

    client_key = hmac.new(b"Client Key", bytes.fromhex(salted_pwd), hashlib.sha256).hexdigest()
    stored_key = hashlib.sha256(bytes.fromhex(client_key)).hexdigest()
    sign_pwd = hmac.new(auth_msg.encode("utf-8"), bytes.fromhex(stored_key), hashlib.sha256).hexdigest()

    ck_bytes = bytes.fromhex(client_key)
    sp_bytes = bytes.fromhex(sign_pwd)
    client_proof = bytes(a ^ b for a, b in zip(ck_bytes, sp_bytes)).hex()

    server_key = hmac.new(b"Server Key", bytes.fromhex(salted_pwd), hashlib.sha256).hexdigest()
    server_proof = hmac.new(auth_msg.encode("utf-8"), bytes.fromhex(server_key), hashlib.sha256).hexdigest()

    return client_proof, server_proof


def generate_nonce() -> str:
    return secrets.token_hex(32)
