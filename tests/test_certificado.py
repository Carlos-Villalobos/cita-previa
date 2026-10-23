"""Re-cifrado del .p12 con un certificado de prueba generado al vuelo (nunca uno real)."""
import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12
from cryptography.x509.oid import NameOID

from cita_previa.certificado import recifrar


def fake_p12(password: bytes) -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PRUEBA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    return pkcs12.serialize_key_and_certificates(b"prueba", key, cert, None, BestAvailableEncryption(password))


def test_recifra_con_la_misma_clave():
    nuevo, titular = recifrar(fake_p12(b"secreto"), b"secreto")
    assert titular == "CN=PRUEBA"
    assert pkcs12.load_pkcs12(nuevo, b"secreto").key is not None


def test_clave_incorrecta():
    with pytest.raises(ValueError):
        recifrar(fake_p12(b"secreto"), b"otra")
