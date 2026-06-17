-- Marina WhatsApp Agent — business tables (system of record for KPIs / retries /
-- handoff). LangGraph's own checkpoint tables are created separately by
-- PostgresSaver.setup(). Apply this once against the Supabase Postgres DB.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
create table if not exists contacts (
    id           uuid primary key default gen_random_uuid(),
    wa_jid       text unique not null,
    phone        text,
    push_name    text,
    source_ad    text,                       -- creative/greeting attribution
    needs_human  boolean not null default false,
    created_at   timestamptz not null default now(),
    last_seen_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
create table if not exists conversations (
    id             uuid primary key default gen_random_uuid(),
    contact_id     uuid not null references contacts(id) on delete cascade,
    stage          text not null default 'welcome',
    brief          jsonb not null default '{}'::jsonb,
    chosen_variant text,
    regen_count    int  not null default 0,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);
create index if not exists conversations_contact_idx on conversations(contact_id);
create index if not exists conversations_stage_idx on conversations(stage);

-- ---------------------------------------------------------------------------
create table if not exists messages (
    id               uuid primary key default gen_random_uuid(),
    conversation_id  uuid references conversations(id) on delete cascade,
    contact_id       uuid references contacts(id) on delete cascade,
    direction        text not null check (direction in ('in','out')),
    wa_message_id    text unique,            -- Evolution key.id, for dedupe
    kind             text not null default 'text', -- text|audio|image|video|document
    content          text,
    media_url        text,
    transcript       text,
    raw              jsonb,
    created_at       timestamptz not null default now()
);
create index if not exists messages_conversation_idx on messages(conversation_id);

-- ---------------------------------------------------------------------------
create table if not exists generations (
    id              uuid primary key default gen_random_uuid(),
    conversation_id uuid references conversations(id) on delete cascade,
    kie_task_id     text unique,
    status          text not null default 'PENDING',
    payload         jsonb,
    variants        jsonb,                   -- [{id, audio_url, title}]
    preview_url     text,
    full_url        text,
    error           text,
    created_at      timestamptz not null default now(),
    completed_at    timestamptz
);
create index if not exists generations_conversation_idx on generations(conversation_id);

-- ---------------------------------------------------------------------------
create table if not exists orders (
    id              uuid primary key default gen_random_uuid(),
    conversation_id uuid references conversations(id) on delete cascade,
    amount_cents    int  not null default 2990,
    provider        text not null default 'mercadopago',
    mp_payment_id   text unique,
    txid            text,
    status          text not null default 'pending', -- pending|paid|failed|cancelled
    pix_copia_cola  text,
    created_at      timestamptz not null default now(),
    paid_at         timestamptz
);
create index if not exists orders_conversation_idx on orders(conversation_id);
create index if not exists orders_status_idx on orders(status);

-- ---------------------------------------------------------------------------
create table if not exists followups (
    id              uuid primary key default gen_random_uuid(),
    conversation_id uuid references conversations(id) on delete cascade,
    kind            text not null,           -- postsale|cold_1|cold_2|cold_3
    run_at          timestamptz not null,
    sent_at         timestamptz,
    status          text not null default 'scheduled', -- scheduled|sent|cancelled
    created_at      timestamptz not null default now()
);
create index if not exists followups_due_idx on followups(status, run_at);
