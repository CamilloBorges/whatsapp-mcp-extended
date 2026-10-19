-- Contas (números) do WhatsApp e a vaga (ponte) de cada uma. Fica no schema public, fora do
-- alcance dos papéis de leitura das contas (leitor_<conta>), que só enxergam o próprio schema.
-- Escrita: o worker (conta fixa da vaga) e o MCP (adicionar_numero / remover_numero).
CREATE TABLE IF NOT EXISTS public.contas (
    conta     text PRIMARY KEY CHECK (conta ~ '^[a-z][a-z0-9_]{0,30}$'),
    vaga      integer UNIQUE,                 -- NULL: conta removida (o histórico continua no schema)
    telefone  text,
    dono      text NOT NULL,                  -- pessoa (como no Hub): só ela usa a conta
    situacao  text NOT NULL DEFAULT 'pareando'
              CHECK (situacao IN ('pareando', 'ativa', 'removida')),
    criada_em timestamptz NOT NULL DEFAULT now(),
    atualizada_em timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON public.contas FROM PUBLIC;
