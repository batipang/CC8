"""Own the paired LAN identity, TLS certificate and discovery authentication."""

from __future__ import annotations
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
from protocol import JsonObject, JsonValue, decode_json_object

ROOT = Path(__file__).resolve().parents[2]
PAIR_DIR = ROOT / '.toolchains/cc8/transport'
TLS_PORT = 18766


def private_ipv4(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
        return ip.version == 4 and (
            ip.is_loopback
            or any(
                ip in ipaddress.ip_network(n)
                for n in ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16']
            )
        )
    except ValueError:
        return False


def signature(token: str, message: str) -> str:
    return hmac.new(bytes.fromhex(token), message.encode(), hashlib.sha256).hexdigest()


def local_addresses() -> list[str]:
    values = []
    try:
        text = subprocess.run(['/sbin/ifconfig'], capture_output=True, text=True, timeout=3).stdout
        values = [
            value
            for value in re.findall(r'\binet (\d+\.\d+\.\d+\.\d+)', text)
            if private_ipv4(value) and not value.startswith('127.')
        ]
    except (OSError, subprocess.SubprocessError):
        pass
    return list(dict.fromkeys(values))[:8]


class Pairing:
    # 私有文件保存共享密钥与 TLS 证书；USB 负责初次交付设备侧配对信息。
    def __init__(self, folder: str | Path = PAIR_DIR, port: int = TLS_PORT) -> None:
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('Invalid TLS port')
        self.port = port
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        os.chmod(self.folder, 0o700)
        cert = self.folder / 'server.pem'
        key = self.folder / 'server-key.pem'
        config = self.folder / 'pairing.json'
        if not cert.exists() or not key.exists():
            subprocess.run(
                [
                    '/usr/bin/openssl',
                    'req',
                    '-x509',
                    '-newkey',
                    'rsa:2048',
                    '-sha256',
                    '-nodes',
                    '-days',
                    '3650',
                    '-subj',
                    '/CN=Bumblebee Local Bridge',
                    '-keyout',
                    str(key),
                    '-out',
                    str(cert),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=20,
            )
        os.chmod(key, 0o600)
        os.chmod(cert, 0o600)
        if config.exists():
            data = decode_json_object(config.read_bytes(), max_bytes=4096)
        else:
            data = {'server_id': secrets.token_hex(16), 'token': secrets.token_hex(32)}
            fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as out:
                json.dump(data, out)
        os.chmod(config, 0o600)
        server_id, token = data.get('server_id'), data.get('token')
        if (
            not isinstance(server_id, str)
            or not re.fullmatch('[0-9a-f]{32}', server_id)
            or not isinstance(token, str)
            or not re.fullmatch('[0-9a-f]{64}', token)
        ):
            raise ValueError('Invalid local transport pairing file')
        self.server_id = server_id
        self.token = token
        der = ssl.PEM_cert_to_DER_cert(cert.read_text())
        self.pin = hashlib.sha256(der).hexdigest()
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(str(cert), str(key))

    def device_config(self) -> JsonObject:
        hosts: list[JsonValue] = [*local_addresses()]
        return {
            'server_id': self.server_id,
            'token': self.token,
            'certificate_sha256': self.pin,
            'port': self.port,
            'hosts': hosts,
        }

    def offer(self, data: object) -> JsonObject | None:
        if not isinstance(data, dict) or data.get('type') != 'cc8_discover':
            return None
        nonce = data.get('nonce', '')
        mac = data.get('mac', '')
        if (
            not isinstance(nonce, str)
            or not re.fullmatch('[0-9a-f]{32}', nonce)
            or not isinstance(mac, str)
            or not re.fullmatch('[0-9a-f]{64}', mac)
        ):
            return None
        if not hmac.compare_digest(signature(self.token, 'discover:' + nonce), mac):
            return None
        return {
            'type': 'cc8_offer',
            'nonce': nonce,
            'server_id': self.server_id,
            'port': self.port,
            'mac': signature(
                self.token, 'offer:' + nonce + ':' + self.server_id + ':' + str(self.port)
            ),
        }
