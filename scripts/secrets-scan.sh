#!/usr/bin/env bash
# Varre arquivos versionados e TODO o historico do git em busca de segredos.
# Usa gitleaks se disponivel; senao cai num grep de padroes conhecidos.
set -uo pipefail
cd "$(dirname "$0")/.."

echo "=== obs-lab · secrets scan ==="

if command -v gitleaks >/dev/null 2>&1; then
  echo "-- gitleaks (historico completo)"
  gitleaks detect --no-banner --redact && echo "  ok  gitleaks: nada encontrado"
  exit $?
fi

echo "-- gitleaks nao instalado, usando fallback grep"

fail=0

echo "-- 1. arquivos de segredo versionados"
bad=$(git ls-files | grep -E '(^|/)\.env$|\.env\.|\.pem$|\.key$|\.p12$|\.pfx$|\.jks$|^kubeconfig' \
      | grep -v '^\.env\.example$' || true)
if [[ -n "$bad" ]]; then echo "$bad" | sed 's/^/  x /'; fail=1; else echo "  ok  nenhum"; fi

echo "-- 2. valores literais em variaveis sensiveis (historico completo)"
pattern='(ACCESS_TOKEN|ACCOUNT_TOKEN|ACCESS_KEY|HEC_TOKEN|TUNNEL_TOKEN|API_?KEY|SECRET|PASSWORD|PASSWD)[[:space:]]*[:=][[:space:]]*"?[A-Za-z0-9_/+-]{8,}'
hits=$(git grep -nIE "$pattern" $(git rev-list --all) 2>/dev/null \
  | grep -vE '\$\{|\$env:|\$\(|process\.env|valueFrom|secretKeyRef|from-literal|seu_|sua_|troque_|SET_VIA|PLACEHOLDER|:\?|scripts/(pre-commit|secrets-scan)' || true)
if [[ -n "$hits" ]]; then echo "$hits" | sed 's/^/  x /' | head -40; fail=1; else echo "  ok  nenhum"; fi

echo "-- 3. tokens de alta entropia conhecidos (JWT, chave base64 longa)"
hits=$(git grep -nIE 'eyJ[A-Za-z0-9_-]{15,}\.|[A-Za-z0-9+/]{48,}={0,2}' $(git rev-list --all) 2>/dev/null | head -20 || true)
if [[ -n "$hits" ]]; then echo "$hits" | sed 's/^/  ? /'; else echo "  ok  nenhum"; fi

echo
[[ $fail -eq 0 ]] && echo "=== resultado: limpo ===" || echo "=== resultado: ACHADOS acima ==="
exit $fail
