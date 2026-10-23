"""Conexión con el servidor de citas: IP de respaldo y validación propia de su cadena TLS.

- El DNS de algunas redes devuelve una IP del sitio que deja de responder mientras la pública sigue arriba:
  si pasa, se pide la IP por DNS sobre HTTPS y se fija en el navegador (el certificado se valida igual).
- El servidor no envía su certificado intermedio y el navegador a veces lo rechaza: aquí se descarga el
  intermedio, se valida la cadena completa y el nombre contra las raíces de confianza y SOLO entonces se le
  dice al navegador que acepte esa clave concreta.
"""
import base64
import hashlib
import socket
import ssl
import warnings

import certifi
import requests
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID
from cryptography.x509.verification import PolicyBuilder, Store

ICP_HOST = "icp.administracionelectronica.gob.es"
DOH = ("https://dns.google/resolve", "https://cloudflare-dns.com/dns-query")


def conecta(ip: str) -> bool:
    try:
        socket.create_connection((ip, 443), timeout=8).close()
        return True
    except OSError:
        return False


def ip_alcanzable(host: str = ICP_HOST) -> str | None:
    """None si la IP del DNS local responde; si no, la primera IP por DNS sobre HTTPS que acepte conexión."""
    locales = {a[4][0] for a in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)}
    if any(conecta(ip) for ip in locales):
        return None
    for url in DOH:
        try:
            r = requests.get(url, params={"name": host, "type": "A"},
                             headers={"accept": "application/dns-json"}, timeout=10)
            ips = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
        except (requests.RequestException, ValueError):
            continue
        for ip in ips:
            if ip not in locales and conecta(ip):
                return ip
    raise ConnectionError(f"{host} no responde (ni por DNS local ni por DNS sobre HTTPS)")


def clave_verificada(host: str = ICP_HOST, ip: str | None = None) -> str:
    """Huella SPKI (sha256, base64) del certificado de `host`, solo si su cadena completa es válida.
    Si algo no cuadra, lanza excepción y no se confía en nada."""
    lector = ssl.create_default_context()
    lector.check_hostname, lector.verify_mode = False, ssl.CERT_NONE   # solo para leerlo; se valida abajo
    with socket.create_connection((ip or host, 443), timeout=20) as s, \
            lector.wrap_socket(s, server_hostname=host) as t:
        hoja = x509.load_der_x509_certificate(t.getpeercert(binary_form=True))
    aia = hoja.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
    url = next(d.access_location.value for d in aia if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS)
    crudo = requests.get(url, timeout=20).content
    intermedio = (x509.load_pem_x509_certificate(crudo) if crudo.startswith(b"-----")
                  else x509.load_der_x509_certificate(crudo))
    with open(certifi.where(), "rb") as f, warnings.catch_warnings():
        warnings.simplefilter("ignore")    # una raíz vieja de certifi tiene número de serie negativo
        raices = x509.load_pem_x509_certificates(f.read())
    PolicyBuilder().store(Store(raices)).build_server_verifier(x509.DNSName(host)).verify(hoja, [intermedio])
    spki = hoja.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()
