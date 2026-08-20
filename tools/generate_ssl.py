#!/usr/bin/env python3
"""
IDOL / Knowledge Discovery SSL Certificate Generator (Python)

Builds a full CA hierarchy with the cryptography library — no OpenSSL
CA database (index.txt / serial) and no PowerShell Set-Content CRLF issues.

  Root CA  →  Intermediate CA  →  per-service leaf certificates

Also produces:
  - PEM certs + keys
  - PKCS#12 keystores (native via cryptography)
  - NiFi keystore.p12 / truststore.p12
  - Optional JKS via keytool (if a JDK is on PATH)
  - Root CA export for client distribution
  - Optional install into the Windows LocalMachine trust store

Usage:
  python tools/generate_ssl.py --auto
  python tools/generate_ssl.py --auto --output-dir ssl
  python tools/generate_ssl.py --services content,community,nifi
  python tools/generate_ssl.py --help

Environment (optional):
  EXTRA_IP_SANS_ENV   comma-separated IPs for --auto
  IDOL_NET_HOST_IP    default extra IP shown in interactive mode
  SSL_SERVICE_USER    (documented; ACL restriction is Windows-only / optional)
"""

from __future__ import annotations

_ANSI_YELLOW = "\033[33m"
_ANSI_RESET = "\033[0m"

import argparse
import ipaddress
import os
import platform
import secrets
import shutil
import socket
import string
import subprocess
import sys

try:
    import grp
    import pwd
except ImportError:  # pragma: no cover - grp/pwd are POSIX-only
    grp = None  # type: ignore[assignment]
    pwd = None  # type: ignore[assignment]
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
except ImportError:
    print(
        "FATAL: the 'cryptography' package is required.\n"
        "  pip install cryptography\n"
        "  or:  pip install -r requirements.txt",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Defaults (aligned with Generate-SSL.ps1)
# ---------------------------------------------------------------------------

DAYS_ROOT = 7300          # ~20 years
DAYS_INTERMEDIATE = 3650  # ~10 years
DAYS_CERT = 825           # ~2.25 years

DEFAULT_SERVICES = [
    "idol-docker-host",
    "obsidian",
    "idol-agentstore",
    "idol-category",
    "idol-categorisation-agentstore",
    "idol-community",
    "idol-content",
    "idol-find",
    "idol-httpd-reverse-proxy",
    "idol-licenseserver",
    "idol-nifi",
    "idol-view",
    "idol-dataadmin",
    "idol-dataadmin-community",
    "idol-dataadmin-viewserver",
    "idol-dataadmin-statsserver",
    "idol-dataadmin-find",
    "idol-qms",
    "idol-qms-agentstore",
    "idol-answerserver",
    "idol-passageextractor-agentstore",
    "idol-passageextractor-content",
    "idol-factbank-postgres",
    "idol-answerbank-agentstore",
    "idol-mediaserver",
    "idol-mmap-playlistserver",
    "idol-mmap-app",
]

# KD-focused shorter list when --kd-services is used
KD_SERVICES = [
    "idol-licenseserver",
    "idol-content",
    "idol-community",
    "idol-agentstore",
    "idol-category",
    "idol-nifi",
    "idol-find",
    "idol-view",
    "idol-dataadmin",
]


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------

class C:
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(kernel32.GetStdHandle(-11), ctypes.byref(mode)):
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), mode.value | 0x0004)
        except Exception:
            pass
    return sys.stdout.isatty()


USE_COLOR = _use_color()


def _c(code: str, text: str) -> str:
    return f"{code}{text}{C.RESET}" if USE_COLOR else text


def info(msg: str) -> None:
    print(_c(C.GREEN, f"[INFO]  {msg}"))


def warn(msg: str) -> None:
    print(_c(C.YELLOW, f"[WARN]  {msg}"))


def err(msg: str) -> None:
    print(_c(C.RED, f"[ERROR] {msg}"), file=sys.stderr)


def ok(msg: str) -> None:
    print(_c(C.GREEN, f"  [OK]  {msg}"))


def section(title: str) -> None:
    border = "=" * 65
    print()
    print(_c(C.CYAN, border))
    print(_c(C.CYAN, f"  {title:<63}"))
    print(_c(C.CYAN, border))
    print()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class SubjectInfo:
    country: str = "US"
    state: str = "State"
    city: str = "City"
    org: str = "Organization"
    ou: str = "IT"


@dataclass
class SslConfig:
    output_dir: Path
    services: List[str]
    subject: SubjectInfo = field(default_factory=SubjectInfo)
    external_hostname: str = "idol-docker-host"
    extra_dns_sans: List[str] = field(default_factory=list)
    extra_ip_sans: List[str] = field(default_factory=list)
    keystore_pass: str = ""
    truststore_pass: str = ""
    days_root: int = DAYS_ROOT
    days_intermediate: int = DAYS_INTERMEDIATE
    days_cert: int = DAYS_CERT
    auto: bool = False
    install_trust_store: bool = True
    build_jks: bool = True
    force: bool = False


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def cert_not_after(cert: x509.Certificate) -> datetime:
    """Return a certificate's expiry as a tz-aware datetime.

    `not_valid_after_utc` was only added in `cryptography` 42.0; older
    versions (still common on distro-packaged Python) only have the naive
    `not_valid_after`. Try the modern attribute first and fall back so this
    works across installed versions without requiring an upgrade.
    """
    try:
        return cert.not_valid_after_utc
    except AttributeError:
        return cert.not_valid_after.replace(tzinfo=timezone.utc)


def generate_rsa_key(bits: int = 4096) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def name_from_subject(subject: SubjectInfo, common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, subject.country),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, subject.state),
            x509.NameAttribute(NameOID.LOCALITY_NAME, subject.city),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, subject.org),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, subject.ou),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def random_serial() -> int:
    # Positive 64-bit serial (OpenSSL-style range)
    return secrets.randbits(63)


def write_pem_private_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def write_pem_cert(path: Path, cert: x509.Certificate) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def write_pem_chain(path: Path, certs: Sequence[x509.Certificate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs)
    path.write_bytes(data)


def write_pkcs12(
    path: Path,
    *,
    key: rsa.RSAPrivateKey,
    cert: x509.Certificate,
    ca_certs: Sequence[x509.Certificate],
    password: str,
    friendly_name: str,
) -> None:
    """Write a PKCS#12 file (compatible with OpenSSL pkcs12 / Java keystores)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    name_bytes = friendly_name.encode("utf-8")
    p12 = pkcs12.serialize_key_and_certificates(
        name=name_bytes,
        key=key,
        cert=cert,
        cas=list(ca_certs) if ca_certs else None,
        encryption_algorithm=serialization.BestAvailableEncryption(password.encode("utf-8")),
    )
    path.write_bytes(p12)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def write_truststore_p12(
    path: Path,
    *,
    ca_certs: Sequence[x509.Certificate],
    password: str,
    friendly_name: str = "ca-chain",
) -> None:
    """
    PKCS#12 truststore containing only CA certificates (no private key).
    cryptography requires a key for serialize_key_and_certificates, so we
    build a minimal self-contained trust bag via a throwaway approach:
    export each CA as a cert-only bag is not directly exposed; instead we
    write a standard PKCS12 with the first CA as 'cert' and the rest as cas,
    using a throwaway key that callers must not use for TLS. For NiFi-style
    truststores, cert-only PKCS12 is preferred — we use OpenSSL-compatible
    cert chain packaging via cryptography's PKCS12 with key=None when supported.

    cryptography >= 42 supports key=None for cert-only PKCS12.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not ca_certs:
        raise ValueError("ca_certs must not be empty")

    try:
        # cryptography 42+: key may be None for a cert-only store
        p12 = pkcs12.serialize_key_and_certificates(
            name=friendly_name.encode("utf-8"),
            key=None,
            cert=ca_certs[0],
            cas=list(ca_certs[1:]) if len(ca_certs) > 1 else None,
            encryption_algorithm=serialization.BestAvailableEncryption(password.encode("utf-8")),
        )
    except (TypeError, ValueError):
        # Older cryptography: include a disposable key (trust consumers only use certs)
        disposable = generate_rsa_key(2048)
        p12 = pkcs12.serialize_key_and_certificates(
            name=friendly_name.encode("utf-8"),
            key=disposable,
            cert=ca_certs[0],
            cas=list(ca_certs[1:]) if len(ca_certs) > 1 else None,
            encryption_algorithm=serialization.BestAvailableEncryption(password.encode("utf-8")),
        )
    path.write_bytes(p12)


def detect_hostname_fqdn() -> Tuple[str, str, str, str]:
    """Return (host_lower, host_upper, fqdn_lower, fqdn_upper). Empty FQDN if N/A."""
    host = socket.gethostname() or platform.node() or "localhost"
    host_lower = host.lower()
    host_upper = host.upper()
    fqdn_lower = ""
    fqdn_upper = ""
    try:
        fqdn = socket.getfqdn()
        if fqdn and fqdn.lower() != host_lower and "." in fqdn:
            fqdn_lower = fqdn.lower()
            fqdn_upper = fqdn.upper()
    except Exception:
        pass
    return host_lower, host_upper, fqdn_lower, fqdn_upper



def _load_extra_ips_from_kd_config(toolkit_root: Path) -> List[str]:
    """
    Read NiFi.ExternalIIPSAN (or legacy ExternalIpAddress) from config/my-config.json
    so SSL generation picks up the public IP even when --extra-ip was omitted.
    """
    candidates = [
        toolkit_root / "config" / "my-config.json",
        toolkit_root.parent / "config" / "my-config.json",
        Path.cwd() / "config" / "my-config.json",
    ]
    for cfg_path in candidates:
        try:
            if not cfg_path.is_file():
                continue
            import json
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            nifi = data.get("NiFi") or {}
            raw = str(
                nifi.get("ExternalIIPSAN")
                or nifi.get("ExternalIpAddress")
                or ""
            ).strip()
            if raw:
                ips = [x.strip() for x in raw.split(",") if x.strip()]
                if ips:
                    print(f"  Loaded ExternalIIPSAN from {cfg_path}: {', '.join(ips)}")
                    return ips
        except Exception:
            continue
    return []


def build_sans(
    service: str,
    *,
    external_hostname: str,
    host_lower: str,
    host_upper: str,
    fqdn_lower: str,
    fqdn_upper: str,
    extra_dns: Sequence[str],
    extra_ips: Sequence[str],
) -> x509.SubjectAlternativeName:
    dns_names: List[str] = []
    ip_addrs: List[ipaddress._BaseAddress] = []

    def add_dns(val: str) -> None:
        v = val.strip()
        if not v:
            return
        if v.lower() not in {d.lower() for d in dns_names}:
            dns_names.append(v)

    def add_ip(val: str) -> None:
        v = val.strip()
        if not v:
            return
        try:
            addr = ipaddress.ip_address(v)
        except ValueError:
            warn(f"Ignoring invalid IP SAN: {v}")
            return
        if addr not in ip_addrs:
            ip_addrs.append(addr)

    add_dns(service)
    add_dns(external_hostname)
    add_dns("localhost")
    add_ip("127.0.0.1")
    add_dns(host_lower)
    if host_upper != host_lower:
        add_dns(host_upper)
    if fqdn_lower:
        add_dns(fqdn_lower)
    if fqdn_upper and fqdn_upper != fqdn_lower:
        add_dns(fqdn_upper)
    for d in extra_dns:
        add_dns(d)
    for ip in extra_ips:
        add_ip(ip)
        # Also as DNSName: some TLS stacks / Jetty SNI paths match the
        # server_name string against DNS SANs even when the client connected
        # by IP (helps avoid HTTP 400 Invalid SNI on https://<IP>:port/nifi).
        add_dns(ip)

    general_names: List[x509.GeneralName] = [x509.DNSName(d) for d in dns_names]
    general_names.extend(x509.IPAddress(ip) for ip in ip_addrs)
    return x509.SubjectAlternativeName(general_names)


def random_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# CA + leaf builders
# ---------------------------------------------------------------------------

def create_root_ca(
    subject: SubjectInfo,
    days: int,
) -> Tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = generate_rsa_key(4096)
    name = name_from_subject(subject, "IDOL Root CA")
    now = utcnow()
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(random_serial())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    cert = builder.sign(private_key=key, algorithm=hashes.SHA256())
    return key, cert


def create_intermediate_ca(
    subject: SubjectInfo,
    days: int,
    root_key: rsa.RSAPrivateKey,
    root_cert: x509.Certificate,
) -> Tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = generate_rsa_key(4096)
    name = name_from_subject(subject, "IDOL Intermediate CA")
    now = utcnow()
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(root_cert.subject)
        .public_key(key.public_key())
        .serial_number(random_serial())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
    )
    cert = builder.sign(private_key=root_key, algorithm=hashes.SHA256())
    return key, cert


def create_service_cert(
    service: str,
    subject: SubjectInfo,
    days: int,
    intermediate_key: rsa.RSAPrivateKey,
    intermediate_cert: x509.Certificate,
    san: x509.SubjectAlternativeName,
) -> Tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = generate_rsa_key(2048)
    name = name_from_subject(subject, service)
    now = utcnow()
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(intermediate_cert.subject)
        .public_key(key.public_key())
        .serial_number(random_serial())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                key_cert_sign=False,
                crl_sign=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(san, critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(intermediate_key.public_key()),
            critical=False,
        )
    )
    cert = builder.sign(private_key=intermediate_key, algorithm=hashes.SHA256())
    return key, cert


def create_self_signed_server(
    common_name: str,
    subject: SubjectInfo,
    days: int,
) -> Tuple[rsa.RSAPrivateKey, x509.Certificate]:
    """Used for MMAP-style self-signed keystore."""
    key = generate_rsa_key(2048)
    name = name_from_subject(subject, common_name)
    now = utcnow()
    san = x509.SubjectAlternativeName(
        [x509.DNSName(common_name), x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(random_serial())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                key_cert_sign=False,
                crl_sign=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(san, critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    cert = builder.sign(private_key=key, algorithm=hashes.SHA256())
    return key, cert


# ---------------------------------------------------------------------------
# Optional keytool (JKS)
# ---------------------------------------------------------------------------

def find_keytool() -> Optional[str]:
    for name in ("keytool", "keytool.exe"):
        path = shutil.which(name)
        if path and "WindowsApps" not in path:
            return path
    java_home = os.environ.get("JAVA_HOME") or os.environ.get("JDK_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / ("keytool.exe" if sys.platform == "win32" else "keytool")
        if candidate.is_file():
            return str(candidate)
    return None


def p12_to_jks(
    p12_path: Path,
    jks_path: Path,
    password: str,
    alias: str,
    chain_pem: Optional[Path] = None,
) -> bool:
    keytool = find_keytool()
    if not keytool:
        warn("keytool not found — skipping JKS generation (PKCS12 is still available)")
        return False
    if jks_path.exists():
        jks_path.unlink()
    try:
        subprocess.run(
            [
                keytool,
                "-importkeystore",
                "-srckeystore", str(p12_path),
                "-srcstoretype", "PKCS12",
                "-srcstorepass", password,
                "-destkeystore", str(jks_path),
                "-deststoretype", "JKS",
                "-deststorepass", password,
                "-destkeypass", password,
                "-srcalias", alias,
                "-destalias", alias,
                "-noprompt",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if chain_pem and chain_pem.is_file():
            # Import CA chain under a separate alias when possible
            subprocess.run(
                [
                    keytool,
                    "-importcert",
                    "-file", str(chain_pem),
                    "-alias", "ca-chain",
                    "-keystore", str(jks_path),
                    "-storepass", password,
                    "-noprompt",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        return True
    except subprocess.CalledProcessError as e:
        warn(f"keytool failed for {jks_path.name}: {e.stderr or e.stdout or e}")
        return False
    except Exception as e:
        warn(f"keytool error: {e}")
        return False


# ---------------------------------------------------------------------------
# Windows trust store
# ---------------------------------------------------------------------------

def install_windows_trust_store(root_pem: Path, intermediate_pem: Path) -> None:
    if sys.platform != "win32":
        info("Not Windows — skipping LocalMachine trust store install")
        return

    section("Installing CAs into Windows Trusted Root Store")
    import tempfile

    def pem_to_der(pem_path: Path, der_path: Path) -> None:
        data = pem_path.read_bytes()
        # Strip PEM headers via cryptography for a clean DER
        cert = x509.load_pem_x509_certificate(data)
        der_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))

    with tempfile.TemporaryDirectory() as tmp:
        root_der = Path(tmp) / "root.cer"
        inter_der = Path(tmp) / "intermediate.cer"
        pem_to_der(root_pem, root_der)
        pem_to_der(intermediate_pem, inter_der)

        for store, path, label in (
            ("Root", root_der, "Root CA"),
            ("CA", inter_der, "Intermediate CA"),
        ):
            try:
                r = subprocess.run(
                    ["certutil", "-addstore", "-f", store, str(path)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if r.returncode == 0:
                    ok(f"{label} -> Cert:\\LocalMachine\\{store}")
                else:
                    warn(f"certutil failed for {label}: {r.stderr or r.stdout}")
            except Exception as e:
                warn(f"Could not install {label}: {e}")


# ---------------------------------------------------------------------------
# Password / subject collection
# ---------------------------------------------------------------------------

def prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{_ANSI_YELLOW}  {label}{suffix}: {_ANSI_RESET}").strip()
    except EOFError:
        return default
    return val if val else default


def collect_subject(cfg: SslConfig) -> None:
    section("Certificate Subject Information")
    if cfg.auto:
        env_ips = os.environ.get("EXTRA_IP_SANS_ENV", "")
        if env_ips:
            cfg.extra_ip_sans = [x.strip() for x in env_ips.split(",") if x.strip()]
        info("Auto mode — using default subject values.")
        print(f"  C={cfg.subject.country} ST={cfg.subject.state} L={cfg.subject.city}")
        print(f"  O={cfg.subject.org} OU={cfg.subject.ou}")
        if cfg.extra_ip_sans:
            print(f"  Extra IP SANs: {', '.join(cfg.extra_ip_sans)}")
        return

    print("  Press Enter to accept the default shown in [brackets].")
    print()
    country = prompt("Country Name (2-letter ISO code)", cfg.subject.country).upper()
    while len(country) != 2 or not country.isalpha():
        warn("Country must be exactly 2 letters (e.g. US, GB, IL).")
        country = prompt("Country Name (2-letter ISO code)", "US").upper()
    cfg.subject.country = country
    cfg.subject.state = prompt("State / Province Name", cfg.subject.state)
    cfg.subject.city = prompt("Locality / City", cfg.subject.city)
    cfg.subject.org = prompt("Organization Name", cfg.subject.org)
    cfg.subject.ou = prompt("Organizational Unit", cfg.subject.ou)

    print()
    print("  Extra DNS SANs (comma-separated, or Enter to skip)")
    dns_in = prompt("Extra DNS SANs", "")
    if dns_in:
        cfg.extra_dns_sans = [x.strip() for x in dns_in.split(",") if x.strip()]

    ip_default = os.environ.get("IDOL_NET_HOST_IP") or os.environ.get("EXTRA_IP_SANS_ENV") or ""
    print("  Extra IP SANs (comma-separated). For cloud VMs, enter the public/private IP.")
    ip_in = prompt("Extra IP SANs", ip_default)
    if ip_in:
        cfg.extra_ip_sans = [x.strip() for x in ip_in.split(",") if x.strip()]

    print()
    print("  Confirm subject / SANs above.")
    prompt("Press Enter to continue (Ctrl+C to abort)", "")


def setup_passwords(cfg: SslConfig) -> None:
    section("Keystore / Truststore Passwords")
    if cfg.auto:
        cfg.keystore_pass = cfg.keystore_pass or random_password(32)
        cfg.truststore_pass = cfg.truststore_pass or random_password(32)
        ok(f"Auto-generated KeyStore password:   {cfg.keystore_pass}")
        ok(f"Auto-generated TrustStore password: {cfg.truststore_pass}")
        return

    choice = prompt("Generate random passwords? (Y/n)", "Y").upper()
    if choice != "N":
        cfg.keystore_pass = random_password(32)
        cfg.truststore_pass = random_password(32)
        ok(f"KeyStore password:   {cfg.keystore_pass}")
        ok(f"TrustStore password: {cfg.truststore_pass}")
    else:
        while True:
            p1 = prompt("Enter KeyStore password")
            p2 = prompt("Confirm KeyStore password")
            if p1 and p1 == p2:
                cfg.keystore_pass = p1
                break
            warn("Passwords do not match or empty — try again.")
        while True:
            p1 = prompt("Enter TrustStore password")
            p2 = prompt("Confirm TrustStore password")
            if p1 and p1 == p2:
                cfg.truststore_pass = p1
                break
            warn("Passwords do not match or empty — try again.")


def save_passwords_env(path: Path, cfg: SslConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Bash-compatible so existing tooling can `source` it
    content = (
        f"# Generated by tools/generate_ssl.py — {utcnow().isoformat()}\n"
        f"export IDOL_CERT_KEYSTORE_PASS='{cfg.keystore_pass}'\n"
        f"export IDOL_CERT_TRUSTSTORE_PASS='{cfg.truststore_pass}'\n"
    )
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    # Also write a plain .env for non-shell consumers
    env_path = path.with_suffix(".env")
    env_path.write_text(
        f"IDOL_CERT_KEYSTORE_PASS={cfg.keystore_pass}\n"
        f"IDOL_CERT_TRUSTSTORE_PASS={cfg.truststore_pass}\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    # Plain-text summary next to the .sh (same directory) for operators
    txt_path = path.parent / "ssl-passwords.txt"
    txt_path.write_text(
        f"# Generated by tools/generate_ssl.py — {utcnow().isoformat()}\n"
        f"# Keep this file private. Do not commit to source control.\n"
        f"KeyStore password:   {cfg.keystore_pass}\n"
        f"TrustStore password: {cfg.truststore_pass}\n",
        encoding="utf-8",
    )
    ok(f"Credentials saved: {path}")
    ok(f"Credentials saved: {env_path}")
    ok(f"Credentials saved: {txt_path}")


# ---------------------------------------------------------------------------
# Ownership helpers
# ---------------------------------------------------------------------------


def _load_basepath_from_config(toolkit_root: Path) -> Optional[Path]:
    for cfg_path in (
        toolkit_root / "config" / "my-config.json",
        toolkit_root / "config" / "default-config.json",
        Path.cwd() / "config" / "my-config.json",
    ):
        try:
            if not cfg_path.is_file():
                continue
            import json
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            base = str(data.get("BasePath") or "").strip()
            if base:
                return Path(base)
        except Exception:
            continue
    return None


def sync_nifi_ssl_properties_and_verify(
    ssl_root: Path,
    cfg: "SslConfig",
    toolkit_root: Optional[Path] = None,
) -> bool:
    """
    Immediately after keystore/truststore generation:
      1. Write passwords into <SETUP>/nifi/conf/nifi.properties
      2. Copy keystore.p12 + truststore.p12 into that conf/
      3. Overwrite the same into <BasePath>/NiFi/conf/ when BasePath exists
      4. Verify SANs via openssl (exit path returns False on failure)

    Returns True if verification passed (or openssl unavailable but files synced).
    """
    toolkit_root = toolkit_root or Path(__file__).resolve().parent.parent
    nifi_src_dir = ssl_root / "intermediate" / "nifi"
    src_ks = nifi_src_dir / "keystore.p12"
    src_ts = nifi_src_dir / "truststore.p12"
    if not src_ks.is_file() or not src_ts.is_file():
        warn("NiFi keystore/truststore not generated — skip nifi.properties sync")
        return True  # non-fatal if nifi was not in service list

    section("Sync NiFi SSL → nifi.properties + verify SAN")

    targets = []
    setup_props = toolkit_root / "nifi" / "conf" / "nifi.properties"
    setup_conf = toolkit_root / "nifi" / "conf"
    targets.append(("SETUP", setup_conf, setup_props))

    base = _load_basepath_from_config(toolkit_root)
    if base is not None:
        target_conf = base / "NiFi" / "conf"
        target_props = target_conf / "nifi.properties"
        targets.append(("TARGET", target_conf, target_props))
    # Common default install path
    default_target = Path("/opt/KnowledgeDiscovery/NiFi/conf")
    if default_target.is_dir() and (not base or default_target.resolve() != (base / "NiFi" / "conf").resolve()):
        targets.append(("TARGET", default_target, default_target / "nifi.properties"))

    def _update_props(props_path: Path) -> None:
        if not props_path.is_file():
            # Create minimal security section if template missing
            props_path.parent.mkdir(parents=True, exist_ok=True)
            if not props_path.is_file():
                warn(f"  nifi.properties missing at {props_path} — creating security keys only")
                props_path.write_text(
                    f"nifi.security.keystore=./conf/keystore.p12\n"
                    f"nifi.security.keystoreType=PKCS12\n"
                    f"nifi.security.keystorePasswd={cfg.keystore_pass}\n"
                    f"nifi.security.keyPasswd={cfg.keystore_pass}\n"
                    f"nifi.security.truststore=./conf/truststore.p12\n"
                    f"nifi.security.truststoreType=PKCS12\n"
                    f"nifi.security.truststorePasswd={cfg.truststore_pass}\n",
                    encoding="utf-8",
                )
                return
        content = props_path.read_text(encoding="utf-8", errors="replace")
        import re
        def repl(key: str, val: str, text: str) -> str:
            pat = re.compile(rf"^{re.escape(key)}=.*$", re.M)
            if pat.search(text):
                return pat.sub(f"{key}={val}", text)
            return text + f"\n{key}={val}\n"
        content = repl("nifi.security.keystore", "./conf/keystore.p12", content)
        content = repl("nifi.security.keystoreType", "PKCS12", content)
        content = repl("nifi.security.keystorePasswd", cfg.keystore_pass, content)
        content = repl("nifi.security.keyPasswd", cfg.keystore_pass, content)
        content = repl("nifi.security.truststore", "./conf/truststore.p12", content)
        content = repl("nifi.security.truststoreType", "PKCS12", content)
        content = repl("nifi.security.truststorePasswd", cfg.truststore_pass, content)
        props_path.write_text(content, encoding="utf-8")

    for label, conf_dir, props_path in targets:
        try:
            conf_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_ks, conf_dir / "keystore.p12")
            shutil.copy2(src_ts, conf_dir / "truststore.p12")
            _update_props(props_path)
            ok(f"[{label}] keystore+truststore+passwords → {conf_dir}")
        except Exception as e:
            err(f"[{label}] failed to sync NiFi SSL material: {e}")
            return False

    # --- openssl SAN verification (must pass) ---
    verify_ks = setup_conf / "keystore.p12"
    if not verify_ks.is_file():
        verify_ks = src_ks
    openssl = shutil.which("openssl")
    if not openssl:
        err("openssl not found on PATH — cannot verify keystore SANs")
        return False

    try:
        proc = subprocess.run(
            [
                openssl, "pkcs12", "-in", str(verify_ks),
                "-nokeys", "-passin", f"pass:{cfg.keystore_pass}",
            ],
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            err(f"openssl pkcs12 failed (bad password or corrupt keystore): "
                f"{(proc.stderr or b'').decode('utf-8', 'replace')[:300]}")
            return False
        x509 = subprocess.run(
            [openssl, "x509", "-noout", "-text"],
            input=proc.stdout,
            capture_output=True,
            timeout=30,
        )
        if x509.returncode != 0:
            err("openssl x509 failed while reading keystore certificate")
            return False
        text_out = (x509.stdout or b"").decode("utf-8", "replace")
        # Print SAN block
        lines = text_out.splitlines()
        san_lines = []
        capture = False
        for i, line in enumerate(lines):
            if "Subject Alternative Name" in line:
                capture = True
                san_lines.append(line)
                continue
            if capture:
                san_lines.append(line)
                if i + 1 < len(lines) and lines[i + 1].strip() and not lines[i + 1].startswith(" "):
                    break
                if line.strip() and not line.startswith(" ") and "DNS" not in line and "IP" not in line:
                    break
        if not san_lines:
            # fallback grep-style
            for i, line in enumerate(lines):
                if "Subject Alternative Name" in line:
                    san_lines = lines[i:i+3]
                    break
        print("  openssl SAN verification:")
        for line in san_lines:
            print(f"    {line.rstrip()}")
        san_blob = "\n".join(san_lines)
        if "DNS:" not in san_blob and "IP Address:" not in san_blob and "IP:" not in san_blob:
            err("Subject Alternative Name missing or empty in keystore certificate")
            return False
        # If extra IP SANs were requested, require them in the cert
        missing = []
        for ip in (cfg.extra_ip_sans or []):
            ip = ip.strip()
            if not ip:
                continue
            if ip not in san_blob:
                missing.append(ip)
        if missing:
            err(f"Required IP SAN(s) not found in keystore: {', '.join(missing)}")
            err("Regenerate with --extra-ip <ip> or set NiFi.ExternalIIPSAN in my-config.json")
            return False
        ok("openssl SAN verification passed")
        return True
    except subprocess.TimeoutExpired:
        err("openssl verification timed out")
        return False
    except Exception as e:
        err(f"openssl verification error: {e}")
        return False



def invoking_user_ids() -> Tuple[Optional[int], Optional[int]]:
    """Return the (uid, gid) of the user who *invoked* the command, even when
    this script is running elevated via sudo. Falls back to (None, None) when
    that can't be determined (not running under sudo, non-POSIX platform).
    """
    if pwd is None:
        return None, None
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        try:
            return int(sudo_uid), int(sudo_gid)
        except ValueError:
            pass
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            entry = pwd.getpwnam(sudo_user)
            return entry.pw_uid, entry.pw_gid
        except KeyError:
            pass
    return None, None


def chown_tree(path: Path, uid: Optional[int], gid: Optional[int]) -> None:
    """Recursively set ownership of path to uid:gid.

    No-op when the ids are unknown, on non-POSIX platforms, or when we're
    not actually running as root (a non-root chown to another user would
    just fail, so don't bother trying and don't warn about the expected
    failure).
    """
    if uid is None or gid is None or os.name != "posix":
        return
    if os.geteuid() != 0:
        return
    try:
        os.chown(path, uid, gid)
        for root, dirs, files in os.walk(path):
            for name in dirs:
                os.chown(os.path.join(root, name), uid, gid)
            for name in files:
                os.chown(os.path.join(root, name), uid, gid)
    except OSError as exc:
        warn(f"Could not set ownership on {path}: {exc}")


# ---------------------------------------------------------------------------
# Main generation
# ---------------------------------------------------------------------------

def generate_all(cfg: SslConfig) -> int:
    ssl_root = cfg.output_dir
    ca_dir = ssl_root / "intermediate"
    certs_dir = ca_dir / "certs"
    private_dir = ca_dir / "private"
    nifi_dir = ca_dir / "nifi"
    issued_dir = ca_dir / "issued"

    if ssl_root.exists() and any(ssl_root.iterdir()):
        if cfg.force or cfg.auto:
            warn(f"Removing existing SSL directory: {ssl_root}")
            shutil.rmtree(ssl_root)
        else:
            answer = prompt(
                f"Existing SSL dir {ssl_root} — regenerate (r) / abort (a)",
                "a",
            ).lower()
            if answer not in ("r", "regen", "regenerate"):
                info("Aborted.")
                return 0
            shutil.rmtree(ssl_root)

    for d in (certs_dir, private_dir, nifi_dir, issued_dir):
        d.mkdir(parents=True, exist_ok=True)

    # When run elevated (e.g. re-exec'd under sudo by install-kd-menu.sh),
    # mkdir() above creates the SSL folder tree as root. Hand ownership back
    # to the user who actually ran the command so they can read/manage their
    # own certificates without needing sudo for every subsequent operation.
    owner_uid, owner_gid = invoking_user_ids()
    chown_tree(ssl_root, owner_uid, owner_gid)

    host_lower, host_upper, fqdn_lower, fqdn_upper = detect_hostname_fqdn()
    info(f"Hostname: {host_lower}" + (f"  FQDN: {fqdn_lower}" if fqdn_lower else ""))

    # --- Root ---
    section("Step 1 - Root CA")
    root_key, root_cert = create_root_ca(cfg.subject, cfg.days_root)
    write_pem_private_key(private_dir / "ca.key.pem", root_key)
    write_pem_cert(certs_dir / "ca.cert.pem", root_cert)
    ok(f"Root CA cert: {certs_dir / 'ca.cert.pem'}")
    print(f"  Subject: {root_cert.subject.rfc4514_string()}")
    print(f"  Not after: {cert_not_after(root_cert).date()}")

    # --- Intermediate ---
    section("Step 2 - Intermediate CA")
    inter_key, inter_cert = create_intermediate_ca(
        cfg.subject, cfg.days_intermediate, root_key, root_cert
    )
    write_pem_private_key(private_dir / "intermediate.key.pem", inter_key)
    write_pem_cert(certs_dir / "intermediate.cert.pem", inter_cert)
    write_pem_chain(certs_dir / "ca-chain.cert.pem", [inter_cert, root_cert])
    write_pkcs12(
        certs_dir / "intermediate.pkcs12",
        key=inter_key,
        cert=inter_cert,
        ca_certs=[root_cert],
        password=cfg.keystore_pass,
        friendly_name="intermediate-ca",
    )
    # Verify chain
    if inter_cert.issuer == root_cert.subject:
        ok("Intermediate CA issuer matches Root CA subject")
    else:
        err("Intermediate CA issuer mismatch")
        return 1
    ok("Intermediate cert + PKCS12 created")
    print(f"  Subject: {inter_cert.subject.rfc4514_string()}")
    print(f"  Not after: {cert_not_after(inter_cert).date()}")

    # --- Service certs ---
    section(f"Step 3 - Service Certificates  ({len(cfg.services)} services)")
    print(f"  Default SANs: (service), {cfg.external_hostname}, localhost, 127.0.0.1")
    print(f"  Auto-injected: {host_lower}" + (f", {fqdn_lower}" if fqdn_lower else ""))
    if cfg.extra_dns_sans:
        print(f"  Extra DNS: {', '.join(cfg.extra_dns_sans)}")
    if cfg.extra_ip_sans:
        print(f"  Extra IP:  {', '.join(cfg.extra_ip_sans)}")
    print()

    failures = 0
    service_material: dict = {}

    for service in cfg.services:
        try:
            san = build_sans(
                service,
                external_hostname=cfg.external_hostname,
                host_lower=host_lower,
                host_upper=host_upper,
                fqdn_lower=fqdn_lower,
                fqdn_upper=fqdn_upper,
                extra_dns=cfg.extra_dns_sans,
                extra_ips=cfg.extra_ip_sans,
            )
            key, cert = create_service_cert(
                service,
                cfg.subject,
                cfg.days_cert,
                inter_key,
                inter_cert,
                san,
            )
            write_pem_private_key(certs_dir / f"{service}.key.pem", key)
            write_pem_cert(certs_dir / f"{service}.cert.pem", cert)
            write_pem_chain(
                certs_dir / f"{service}-fullchain.cert.pem",
                [cert, inter_cert, root_cert],
            )
            service_material[service] = (key, cert)
            ok(service)
            # Compact SAN summary
            san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            sans_str = ", ".join(
                (n.value if isinstance(n, x509.DNSName) else str(n.value))
                for n in san_ext.value
            )
            print(f"     SANs: {sans_str}")
        except Exception as e:
            failures += 1
            err(f"{service}: {e}")

    # --- NiFi keystores ---
    section("Step 4 - NiFi PKCS12 KeyStore and TrustStore")
    nifi_name = "idol-nifi"
    if nifi_name in service_material:
        nifi_key, nifi_cert = service_material[nifi_name]
        write_pkcs12(
            nifi_dir / "keystore.p12",
            key=nifi_key,
            cert=nifi_cert,
            ca_certs=[inter_cert, root_cert],
            password=cfg.keystore_pass,
            friendly_name="nifi-key",
        )
        ok(f"keystore.p12  -> {nifi_dir / 'keystore.p12'}")
        write_truststore_p12(
            nifi_dir / "truststore.p12",
            ca_certs=[inter_cert, root_cert],
            password=cfg.truststore_pass,
            friendly_name="ca-chain",
        )
        ok(f"truststore.p12 -> {nifi_dir / 'truststore.p12'}")
    else:
        warn(f"{nifi_name} not in service list — skipping NiFi keystores")

    # --- Find / DataAdmin PKCS12 (+ optional JKS) ---
    section("Step 5-6 - IDOL Find / DataAdmin KeyStores")
    chain_pem = certs_dir / "ca-chain.cert.pem"
    for svc in ("idol-find", "idol-dataadmin"):
        if svc not in service_material:
            warn(f"{svc} not in service list — skip")
            continue
        key, cert = service_material[svc]
        p12_path = certs_dir / f"{svc}.pkcs12"
        write_pkcs12(
            p12_path,
            key=key,
            cert=cert,
            ca_certs=[inter_cert, root_cert],
            password=cfg.keystore_pass,
            friendly_name=svc,
        )
        jks_ok = False
        if cfg.build_jks:
            jks_ok = p12_to_jks(
                p12_path,
                certs_dir / f"{svc}.jks",
                cfg.keystore_pass,
                alias=svc,
                chain_pem=chain_pem,
            )
        ok(f"{svc}.pkcs12" + ("  +  .jks" if jks_ok else ""))

    # --- MMAP self-signed ---
    section("Step 6c - MMAP KeyStore (self-signed)")
    mmap_key, mmap_cert = create_self_signed_server("mmap", cfg.subject, 365)
    write_pkcs12(
        certs_dir / "idol-mmap-app.pkcs12",
        key=mmap_key,
        cert=mmap_cert,
        ca_certs=[],
        password=cfg.keystore_pass,
        friendly_name="mmap",
    )
    ok("idol-mmap-app.pkcs12 created (self-signed, 365 days)")

    # --- Export root for clients ---
    section("Step 9 - Exporting Root CA for Client Distribution")
    root_crt = ssl_root / "idol-root-ca.crt"
    write_pem_cert(root_crt, root_cert)
    ok(f"Root CA (distribute to clients): {root_crt}")

    # --- Passwords file ---
    # Always save credentials inside the SSL output folder (primary location).
    # Also keep a copy under repo env/ for existing tooling that sources it.
    script_dir = Path(__file__).resolve().parent.parent
    env_dir = script_dir / "env"
    try:
        save_passwords_env(ssl_root / ".idol-ssl-passwords.sh", cfg)
    except OSError as e:
        warn(f"Could not save passwords under SSL folder: {e}")
    try:
        save_passwords_env(env_dir / ".idol-ssl-passwords.sh", cfg)
    except OSError:
        pass
    else:
        # env/ lives outside ssl_root, so give it back to the invoking user too.
        chown_tree(env_dir, owner_uid, owner_gid)

    # --- OS trust store (Windows only; no-op on Linux) ---
    if cfg.install_trust_store and sys.platform.startswith("win"):
        install_windows_trust_store(certs_dir / "ca.cert.pem", certs_dir / "intermediate.cert.pem")

    # Re-assert ownership on the SSL folder now that every cert/key/password
    # file has been written (the first pass right after mkdir only covered
    # the empty directory tree).
    chown_tree(ssl_root, owner_uid, owner_gid)

    # --- Sync passwords + keystores into nifi.properties (SETUP + TARGET) ---
    # Fail the whole generation if openssl SAN verification fails.
    if not sync_nifi_ssl_properties_and_verify(ssl_root, cfg, toolkit_root=script_dir):
        err("NiFi SSL sync/verify failed — fix SANs/passwords and re-run generate_ssl.py")
        return 1

    # --- Summary ---
    section("Summary Report")
    print(f"  Service certificates: {len(cfg.services) - failures} ok, {failures} failed")
    print(f"  Output directory:     {ssl_root}")
    print(f"  Root CA:              {certs_dir / 'ca.cert.pem'}")
    print(f"  Intermediate:         {certs_dir / 'intermediate.cert.pem'}")
    print(f"  CA chain:             {certs_dir / 'ca-chain.cert.pem'}")
    print(f"  Client root export:   {root_crt}")
    print()
    print(_c(C.YELLOW, f"  KeyStore password:    {cfg.keystore_pass}"))
    print(_c(C.YELLOW, f"  TrustStore password:  {cfg.truststore_pass}"))
    print()
    print(f"  Expiry — Root: ~{(utcnow() + timedelta(days=cfg.days_root)).date()}")
    print(f"           Intermediate: ~{(utcnow() + timedelta(days=cfg.days_intermediate)).date()}")
    print(f"           Service certs: ~{(utcnow() + timedelta(days=cfg.days_cert)).date()}")
    print()
    if failures:
        err(f"{failures} certificate(s) failed.")
        return 1
    print(_c(C.GREEN, "  [SUCCESS] All certificates generated with cryptography (no OpenSSL CA db)."))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="generate_ssl.py",
        description="IDOL/KD SSL certificate generator (Python cryptography — no OpenSSL CA database)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python tools/generate_ssl.py --auto
  python tools/generate_ssl.py --auto --kd-services
  python tools/generate_ssl.py --auto --services content,community,nifi
  python tools/generate_ssl.py --auto --output-dir C:\\KD-Setup\\ssl --no-trust-store

notes:
  Replaces Generate-SSL.ps1 for environments where OpenSSL's CA index.txt
  fails on Windows (CRLF / encoding). PKCS12 is written natively; JKS still
  needs keytool from a JDK if you want .jks files.
""",
    )
    p.add_argument("--auto", "-a", action="store_true", help="Non-interactive; random passwords + defaults")
    p.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=None,
        help="SSL output root (default: <repo>/ssl)",
    )
    p.add_argument(
        "--services",
        type=str,
        default=None,
        help="Comma-separated service CN list (default: full IDOL list)",
    )
    p.add_argument(
        "--kd-services",
        action="store_true",
        help="Use a shorter KD-focused service list",
    )
    p.add_argument("--external-hostname", default="idol-docker-host", help="DNS SAN added to every cert")
    p.add_argument("--country", default="US")
    p.add_argument("--state", default="State")
    p.add_argument("--city", default="City")
    p.add_argument("--org", default="Organization")
    p.add_argument("--ou", default="IT")
    p.add_argument("--keystore-pass", default="", help="KeyStore password (auto-generated if empty + --auto)")
    p.add_argument("--truststore-pass", default="", help="TrustStore password (auto-generated if empty + --auto)")
    p.add_argument("--extra-dns", default="", help="Comma-separated extra DNS SANs")
    p.add_argument("--extra-ip", default="", help="Comma-separated extra IP SANs")
    p.add_argument("--force", action="store_true", help="Overwrite existing output dir without prompting")
    p.add_argument("--no-trust-store", action="store_true", help="Do not install CAs into Windows trust store")
    p.add_argument("--no-jks", action="store_true", help="Skip keytool JKS generation")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    script_dir = Path(__file__).resolve().parent.parent
    output_dir = args.output_dir or (script_dir / "ssl")

    if args.services:
        services = [s.strip() for s in args.services.split(",") if s.strip()]
        # Allow short names like "content" → "idol-content"
        normalized = []
        for s in services:
            if s.startswith("idol-") or s in ("obsidian", "mmap"):
                normalized.append(s)
            else:
                normalized.append(f"idol-{s}")
        services = normalized
    elif args.kd_services:
        services = list(KD_SERVICES)
    else:
        services = list(DEFAULT_SERVICES)

    extra_dns = [x.strip() for x in args.extra_dns.split(",") if x.strip()]
    extra_ip = [x.strip() for x in args.extra_ip.split(",") if x.strip()]
    if not extra_ip:
        # Permanent SNI fix: pull NiFi.ExternalIIPSAN from my-config.json when
        # the caller (or interactive path) did not pass --extra-ip.
        toolkit_guess = Path(__file__).resolve().parent.parent
        extra_ip = _load_extra_ips_from_kd_config(toolkit_guess)

    cfg = SslConfig(
        output_dir=output_dir.resolve(),
        services=services,
        subject=SubjectInfo(
            country=args.country,
            state=args.state,
            city=args.city,
            org=args.org,
            ou=args.ou,
        ),
        external_hostname=args.external_hostname,
        extra_dns_sans=extra_dns,
        extra_ip_sans=extra_ip,
        keystore_pass=args.keystore_pass,
        truststore_pass=args.truststore_pass,
        auto=args.auto,
        install_trust_store=not args.no_trust_store,
        build_jks=not args.no_jks,
        force=args.force,
    )

    section("IDOL SSL Certificate Generation (Python / cryptography)")
    info(f"Output:  {cfg.output_dir}")
    info(f"Mode:    {'non-interactive (--auto)' if cfg.auto else 'interactive'}")
    info(f"Services: {len(cfg.services)}")

    collect_subject(cfg)
    setup_passwords(cfg)
    return generate_all(cfg)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
