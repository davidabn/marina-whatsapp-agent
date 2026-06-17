# Marina — Agente de WhatsApp (LangGraph) do funil Music AI Ads

Agente conversacional que opera **todo o funil de venda no WhatsApp** na voz da
persona **Marina**: recebe o lead vindo do anúncio (Instagram/Facebook →
click-to-WhatsApp), faz a descoberta emocional, gera a música com IA, envia uma
prévia, cobra via PIX e entrega a música completa — automaticamente.

## Decisões do produto (travadas)
- **Pagamento:** Mercado Pago — cobrança PIX dinâmica (copia-e-cola) + confirmação
  automática por webhook.
- **Prévia:** 1 geração, 1 prévia de ~45s **em áudio**.
- **Voz:** Marina só em **texto**; o único áudio enviado é a própria música.
- **LLM:** GPT-4o mini (OpenAI) em todos os nós; STT também via OpenAI.
- **Música:** KIE.ai / Suno V5 (reaproveita o pipeline em `../music-pipeline`).

## Arquitetura
Máquina de estados **LangGraph** (não um agente ReAct livre): o LLM escreve as
falas e interpreta a mensagem, mas o **grafo controla a ordem** das etapas de
forma determinística. Invariantes garantidas em código: preço só aparece no
`anchor`; PIX só depois da prévia; cap de regerações.

```
welcome → discovery_recipient → discovery_story → style → anchor
   → songwriter → generate → (KIE webhook) → preview → choice
   → pix → (MP webhook) → deliver → followup
```

`FastAPI` expõe os webhooks; o `runner` é a API que a camada web chama.

```
app/
  config.py          settings (.env)
  graph/             state.py, router.py, build.py, runner.py, nodes/*
  llm/               persona.py, llm.py, extract.py, reply.py, songwriter.py
  music/             kie.py, preview.py, styles.py, lyrics.py, schema.py
  media/             stt.py, storage.py
  evolution/         client.py, parser.py, types.py   (WhatsApp)
  payments/          base.py, mercadopago.py          (PIX)
  db/                repo.py, migrations/0001_init.sql
  scheduler/         followups.py
  webhooks/          evolution.py, kie.py, payments.py
  main.py            FastAPI app + lifespan
```

## Setup local
```bash
cd agent
python3.12 -m venv .venv && source .venv/bin/activate    # 3.12 recomendado
pip install -r requirements.txt
cp .env.example .env        # e preencha as chaves (veja abaixo)
```
Requer **ffmpeg/ffprobe** no PATH (prévia de 45s).

### Variáveis de ambiente
Preencha o `.env` (template em `.env.example`). Grupos:
`OPENAI_*` (LLM+STT) · `KIE_*` (música) · `EVOLUTION_*` (WhatsApp) · `MP_*`
(Mercado Pago) · `SUPABASE_*` (DB+Storage) · `EASYPANEL_*` (deploy) · app
(`PUBLIC_BASE_URL`, `PRICE_CENTS=2990`, `TZ=America/Sao_Paulo`).

### Banco (Supabase)
1. Aplique a migração de negócio: rode o SQL de `app/db/migrations/0001_init.sql`
   no Postgres do Supabase (SQL Editor ou psql).
2. As tabelas de checkpoint do LangGraph são criadas automaticamente no boot
   (`PostgresSaver.setup()`).
3. **`SUPABASE_DB_URL` deve ser a conexão *session pooler / direct* (porta 5432)**,
   não a transaction pooler.
4. Crie um bucket público de Storage com o nome de `SUPABASE_STORAGE_BUCKET`
   (default `marina-media`).

### Webhooks (apontar para o domínio público)
- Evolution → `POST {PUBLIC_BASE_URL}/webhooks/evolution` (evento `messages.upsert`;
  opcional header `x-webhook-token` = `EVOLUTION_WEBHOOK_TOKEN`).
- KIE.ai `KIE_CALLBACK_URL` → `{PUBLIC_BASE_URL}/webhooks/kie`.
- Mercado Pago → `{PUBLIC_BASE_URL}/webhooks/payments`.

## Rodar
```bash
PYTHONPATH=. uvicorn app.main:app --host 0.0.0.0 --port 8000
# health: GET /health  → {status, ffmpeg, db}
```

## Testes
```bash
cd agent && PYTHONPATH=. python -m pytest -q
```
Os testes são offline (LLM e IO externos são mockados).

## Deploy (EasyPanel)
- Imagem via `Dockerfile` (Python 3.12 + ffmpeg). `CMD` sobe o uvicorn na `$PORT`.
- Configure as mesmas variáveis de ambiente no app do EasyPanel.
- Healthcheck: `GET /health`.
- Aponte os webhooks (Evolution/KIE/MP) para o domínio público do serviço.
- Rode o scheduler de follow-ups em **uma única réplica** (ou use cron chamando
  `POST /tasks/run-due-followups`).
