#!/usr/bin/env bash
# Zera o histórico de um número (Supabase + memória do LangGraph) pra recomeçar
# a conversa do zero nos testes.
#
# Uso:  ./scripts/reset.sh 5514998859650
#
# Lê PUBLIC_BASE_URL e ADMIN_TOKEN do agent/.env automaticamente.
set -euo pipefail

if [ "${1:-}" = "" ]; then
  echo "uso: $0 <telefone-ou-wa_jid>   (ex: $0 5514998859650)" >&2
  exit 1
fi

# Carrega .env (mesma pasta do agent), sem vazar pro ambiente do shell.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
if [ -f "$ENV_FILE" ]; then
  PUBLIC_BASE_URL="$(grep -E '^PUBLIC_BASE_URL=' "$ENV_FILE" | tail -1 | cut -d= -f2-)"
  ADMIN_TOKEN="$(grep -E '^ADMIN_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2-)"
fi

: "${PUBLIC_BASE_URL:?defina PUBLIC_BASE_URL no .env}"
: "${ADMIN_TOKEN:?defina ADMIN_TOKEN no .env}"

curl -sS -X POST "${PUBLIC_BASE_URL%/}/admin/reset-contact" \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "content-type: application/json" \
  -d "{\"phone\":\"$1\"}"
echo
