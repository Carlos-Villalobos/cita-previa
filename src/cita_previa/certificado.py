"""Prepara tu certificado digital para el agente.

El .p12 que exporta Acceso a Llaveros usa un cifrado antiguo (RC2/3DES) que Playwright no abre. Este
comando lo lee con tu contraseña y lo guarda en data/certificado.p12 cifrado con AES-256, con la MISMA
contraseña y permisos solo para tu usuario. La contraseña se pide en la terminal: no se guarda ni se muestra.

    .venv/bin/python -m cita_previa.certificado ~/Desktop/Certificados.p12
"""
import argparse
import getpass
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import PrivateFormat, pkcs12

from cita_previa.config import DATA_DIR

DESTINO = DATA_DIR / "certificado.p12"


def recifrar(datos: bytes, clave: bytes) -> tuple[bytes, str]:
    """(.p12 con AES-256, titular del certificado)."""
    p12 = pkcs12.load_pkcs12(datos, clave)
    if p12.key is None or p12.cert is None:
        raise ValueError("el archivo no tiene clave privada y certificado: exporta el certificado CON su clave")
    cifrado = (PrivateFormat.PKCS12.encryption_builder().kdf_rounds(100_000)
               .key_cert_algorithm(pkcs12.PBES.PBESv2SHA256AndAES256CBC).hmac_hash(hashes.SHA256()).build(clave))
    nuevo = pkcs12.serialize_key_and_certificates(p12.cert.friendly_name, p12.key, p12.cert.certificate,
                                                  [c.certificate for c in p12.additional_certs], cifrado)
    return nuevo, p12.cert.certificate.subject.rfc4514_string()


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-cifra el .p12 exportado del Llavero para el agente")
    parser.add_argument("p12", type=Path, help="El .p12 exportado de Acceso a Llaveros")
    args = parser.parse_args()
    clave = getpass.getpass("Contraseña del .p12 (no se muestra): ").encode()
    try:
        nuevo, titular = recifrar(args.p12.expanduser().read_bytes(), clave)
    except ValueError as e:
        raise SystemExit(f"No pude abrirlo: {e}. ¿La contraseña es la de la exportación?") from None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DESTINO.write_bytes(nuevo)
    os.chmod(DESTINO, 0o600)
    print(f"Listo: {DESTINO} (titular: {titular}).")
    print("Pon esa misma contraseña en .env como CITA_CERT_PASSWORD y borra el .p12 original del Escritorio.")


if __name__ == "__main__":
    main()
