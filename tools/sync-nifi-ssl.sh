#!/usr/bin/env bash
# Sync SSL keystore/truststore + passwords from SETUP/ssl into NiFi conf.
# Usage:
#   ./tools/sync-nifi-ssl.sh [SETUP_DIR] [NIFI_HOME]
# Defaults:
#   SETUP_DIR = parent of tools/ (toolkit root)
#   NIFI_HOME = $BasePath/NiFi or /opt/KnowledgeDiscovery/NiFi
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP="${1:-$(cd "$SCRIPT_DIR/.." && pwd)}"
NIFI_HOME="${2:-}"

if [[ -z "$NIFI_HOME" ]]; then
  if [[ -f "$SETUP/config/my-config.json" ]]; then
    BASE=$(python3 -c "import json; print(json.load(open('$SETUP/config/my-config.json')).get('BasePath',''))" 2>/dev/null || true)
    if [[ -n "$BASE" && -d "$BASE/NiFi" ]]; then
      NIFI_HOME="$BASE/NiFi"
    fi
  fi
fi
NIFI_HOME="${NIFI_HOME:-/opt/KnowledgeDiscovery/NiFi}"

SSL_DIR="$SETUP/ssl"
PASS_FILE="$SSL_DIR/ssl-passwords.txt"
KS_SRC="$SSL_DIR/intermediate/nifi/keystore.p12"
TS_SRC="$SSL_DIR/intermediate/nifi/truststore.p12"
CONF="$NIFI_HOME/conf"
PROPS="$CONF/nifi.properties"

YELLOW='\033[33m';GREEN='\033[32m';RED='\033[31m';NC='\033[0m'

echo -e "${YELLOW}=== sync-nifi-ssl ===${NC}"
echo "  SETUP:     $SETUP"
echo "  NIFI_HOME: $NIFI_HOME"
echo "  SSL_DIR:   $SSL_DIR"

if [[ ! -f "$KS_SRC" || ! -f "$TS_SRC" ]]; then
  echo -e "${RED}Missing keystore/truststore under $SSL_DIR/intermediate/nifi/${NC}"
  echo "  Run: python tools/generate_ssl.py --auto --kd-services --force --no-trust-store --extra-ip <PUBLIC_IP> --output-dir ssl"
  exit 1
fi
if [[ ! -f "$PASS_FILE" ]]; then
  echo -e "${RED}Missing $PASS_FILE${NC}"
  exit 1
fi
if [[ ! -f "$PROPS" ]]; then
  echo -e "${RED}Missing $PROPS — is NiFi installed at $NIFI_HOME?${NC}"
  exit 1
fi

KS_PASS=$(grep -i '^KeyStore password:' "$PASS_FILE" | head -1 | sed 's/^[^:]*: *//' | tr -d '\r')
TS_PASS=$(grep -i '^TrustStore password:' "$PASS_FILE" | head -1 | sed 's/^[^:]*: *//' | tr -d '\r')
if [[ -z "$KS_PASS" || -z "$TS_PASS" ]]; then
  echo -e "${RED}Could not parse passwords from $PASS_FILE${NC}"
  exit 1
fi

mkdir -p "$CONF"
cp -v "$KS_SRC" "$CONF/keystore.p12"
cp -v "$TS_SRC" "$CONF/truststore.p12"

# Update passwords in nifi.properties
sed -i "s|^nifi.security.keystore=.*|nifi.security.keystore=./conf/keystore.p12|" "$PROPS"
sed -i "s|^nifi.security.truststore=.*|nifi.security.truststore=./conf/truststore.p12|" "$PROPS"
sed -i "s|^nifi.security.keystoreType=.*|nifi.security.keystoreType=PKCS12|" "$PROPS"
sed -i "s|^nifi.security.truststoreType=.*|nifi.security.truststoreType=PKCS12|" "$PROPS"
sed -i "s|^nifi.security.keystorePasswd=.*|nifi.security.keystorePasswd=${KS_PASS}|" "$PROPS"
sed -i "s|^nifi.security.keyPasswd=.*|nifi.security.keyPasswd=${KS_PASS}|" "$PROPS"
sed -i "s|^nifi.security.truststorePasswd=.*|nifi.security.truststorePasswd=${TS_PASS}|" "$PROPS"

# Prefer public IP first in proxy.host when ExternalIIPSAN is in my-config
if [[ -f "$SETUP/config/my-config.json" ]]; then
  EXTRA=$(python3 -c "
import json
n=json.load(open('$SETUP/config/my-config.json')).get('NiFi') or {}
print((n.get('ExternalIIPSAN') or n.get('ExternalIpAddress') or '').strip())
" 2>/dev/null || true)
  PORT=$(python3 -c "
import json
n=json.load(open('$SETUP/config/my-config.json')).get('NiFi') or {}
print(str(n.get('WebHttpsPort') or '8443'))
" 2>/dev/null || echo 8443)
  if [[ -n "$EXTRA" ]]; then
    PROXY="${EXTRA}:${PORT},localhost:${PORT},0.0.0.0:${PORT},127.0.0.1:${PORT}"
    sed -i "s|^nifi.web.proxy.host=.*|nifi.web.proxy.host=${PROXY}|" "$PROPS"
    echo "  proxy.host -> $PROXY"
  fi
fi

echo -e "${GREEN}[OK] keystore + truststore + passwords synced to $CONF${NC}"
echo "  keystorePasswd / keyPasswd updated from ssl-passwords.txt"
echo "  truststorePasswd updated from ssl-passwords.txt"
echo
echo "Verify SANs (SNI requires your public IP as IP Address):"
openssl pkcs12 -in "$CONF/keystore.p12" -nokeys -passin "pass:${KS_PASS}" 2>/dev/null \
  | openssl x509 -noout -text 2>/dev/null \
  | grep -A3 'Subject Alternative Name' || true
echo
echo "Restart NiFi:  sudo systemctl restart kd-nifi.service"
