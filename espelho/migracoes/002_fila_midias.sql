-- Fase 2: mídias no Cloudflare R2, transcrição dos áudios e a fila que processa as duas coisas.

-- Uma mídia por mensagem, já guardada no R2.
CREATE TABLE IF NOT EXISTS midias (
    chat_jid    text NOT NULL,
    id          text NOT NULL,
    tipo        text NOT NULL,          -- audio | image | video | document
    chave_r2    text NOT NULL,          -- caminho do objeto no bucket
    tamanho     bigint NOT NULL,
    sha256      text NOT NULL,
    guardada_em timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_jid, id)
);

CREATE TABLE IF NOT EXISTS transcricoes (
    chat_jid     text NOT NULL,
    id           text NOT NULL,
    texto        text NOT NULL,
    idioma       text,
    transcrita_em timestamptz NOT NULL DEFAULT now(),
    busca        tsvector GENERATED ALWAYS AS (to_tsvector('portuguese', texto)) STORED,
    PRIMARY KEY (chat_jid, id)
);
CREATE INDEX IF NOT EXISTS transcricoes_busca_idx ON transcricoes USING gin (busca);

-- Fila durável: uma tarefa por mensagem com mídia (baixar → R2 → transcrever se for áudio).
-- Consumida uma por vez com FOR UPDATE SKIP LOCKED; erro volta para a fila com espera crescente.
CREATE TABLE IF NOT EXISTS fila (
    chat_jid          text NOT NULL,
    id                text NOT NULL,
    status            text NOT NULL DEFAULT 'pendente'
                      CHECK (status IN ('pendente', 'concluida', 'falhou')),
    tentativas        integer NOT NULL DEFAULT 0,
    proxima_tentativa timestamptz NOT NULL DEFAULT now(),
    erro              text,
    criada_em         timestamptz NOT NULL DEFAULT now(),
    concluida_em      timestamptz,
    PRIMARY KEY (chat_jid, id)
);
CREATE INDEX IF NOT EXISTS fila_pendentes_idx ON fila (proxima_tentativa) WHERE status = 'pendente';
