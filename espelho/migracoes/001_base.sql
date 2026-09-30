-- Espelho do WhatsApp: chats e mensagens copiados do banco interno da ponte (messages.db).
-- Idempotente: roda a cada início do worker.

CREATE TABLE IF NOT EXISTS chats (
    jid              text PRIMARY KEY,
    nome             text,
    tipo             text NOT NULL,          -- grupo | contato | status | newsletter | outro
    -- trabalho/pessoal: espelho + mídias + transcrição; ruido: só o texto; ignorar: fora do espelho.
    classificacao    text NOT NULL DEFAULT 'a_classificar'
                     CHECK (classificacao IN ('a_classificar', 'trabalho', 'pessoal', 'ruido', 'ignorar')),
    classificado_por text,                   -- regra | ia | camillo
    motivo           text,
    classificado_em  timestamptz,
    ultima_mensagem  timestamptz,
    atualizado_em    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mensagens (
    chat_jid        text NOT NULL,
    id              text NOT NULL,
    remetente       text,
    remetente_nome  text,
    enviada_por_mim boolean NOT NULL DEFAULT false,
    momento         timestamptz NOT NULL,
    texto           text,
    tipo_midia      text,                    -- audio | image | video | document | call | NULL (texto)
    arquivo         text,
    tamanho         bigint,
    resposta_a      text,
    encaminhada     boolean NOT NULL DEFAULT false,
    editada         boolean NOT NULL DEFAULT false,
    sistema         boolean NOT NULL DEFAULT false,
    busca           tsvector GENERATED ALWAYS AS (to_tsvector('portuguese', coalesce(texto, ''))) STORED,
    importada_em    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_jid, id)
);
CREATE INDEX IF NOT EXISTS mensagens_momento_idx ON mensagens (momento);
CREATE INDEX IF NOT EXISTS mensagens_chat_momento_idx ON mensagens (chat_jid, momento);
CREATE INDEX IF NOT EXISTS mensagens_busca_idx ON mensagens USING gin (busca);

-- Posição da última leitura de cada fonte (rowid do SQLite da ponte).
CREATE TABLE IF NOT EXISTS sincronizacao (
    fonte         text PRIMARY KEY,
    ultimo_rowid  bigint NOT NULL DEFAULT 0,
    atualizado_em timestamptz NOT NULL DEFAULT now()
);
