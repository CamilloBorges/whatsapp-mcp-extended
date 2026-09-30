"""Fila de mídias do espelho: baixar da ponte, guardar no Cloudflare R2 e transcrever os áudios.

Entra na fila toda mensagem com mídia de um chat "trabalho" ou "pessoal" que ainda não tem a mídia
guardada. Como a regra é reavaliada a cada ciclo, reclassificar um chat já traz o histórico dele
(dentro de DIAS_MAX dias, porque o WhatsApp não mantém a mídia para sempre).

O arquivo vem, nesta ordem: do download automático da ponte (store/<jid>/<arquivo>, se o tamanho
bater) ou da API da ponte (POST /api/download, que grava em store/media/<jid>/<id>.<ext>).
"""
import hashlib
import logging
import re
from pathlib import Path

import psycopg

TIPOS = ("audio", "image", "video", "document")
DIAS_MAX = 45
MAX_TENTATIVAS = 8
log = logging.getLogger("espelho.fila")

_INSEGURO = re.compile(r"[^a-zA-Z0-9._-]")


def enfileirar(pg: psycopg.Connection, dias: int = DIAS_MAX) -> int:
    """Põe na fila as mídias ainda não guardadas dos chats trabalho/pessoal; devolve quantas entraram."""
    cur = pg.execute(
        """
        INSERT INTO fila (chat_jid, id)
        SELECT m.chat_jid, m.id
        FROM mensagens m
        JOIN chats c ON c.jid = m.chat_jid AND c.classificacao IN ('trabalho', 'pessoal')
        WHERE m.tipo_midia = ANY(%s)
          AND m.momento > now() - make_interval(days => %s)
          AND NOT EXISTS (SELECT 1 FROM midias d WHERE d.chat_jid = m.chat_jid AND d.id = m.id)
        ON CONFLICT DO NOTHING
        """,
        (list(TIPOS), dias),
    )
    return cur.rowcount


def caminho_auto(store: Path, chat_jid: str, arquivo: str | None, msg_id: str) -> Path:
    """Onde o download automático da ponte grava (ver autoDownloadMedia no fork)."""
    nome = arquivo or f"{msg_id}.bin"
    return store / _INSEGURO.sub("_", chat_jid) / _INSEGURO.sub("_", nome)


def chave_r2(chat_jid: str, momento, msg_id: str, arquivo: Path) -> str:
    return f"{_INSEGURO.sub('_', chat_jid)}/{momento:%Y/%m}/{_INSEGURO.sub('_', msg_id)}{arquivo.suffix.lower()}"


def obter_arquivo(store: Path, tarefa: dict, baixar_da_ponte) -> Path:
    local = caminho_auto(store, tarefa["chat_jid"], tarefa["arquivo"], tarefa["id"])
    if local.is_file() and (not tarefa["tamanho"] or local.stat().st_size == tarefa["tamanho"]):
        return local
    relativo = baixar_da_ponte(tarefa["chat_jid"], tarefa["id"])  # ex.: store/media/<jid>/<id>.ogg
    return store / Path(relativo).relative_to("store")


def processar_uma(pg: psycopg.Connection, store: Path, baixar_da_ponte, enviar_r2, transcrever) -> bool:
    """Processa a próxima tarefa pendente. Devolve False se a fila está vazia."""
    with pg.transaction():
        tarefa = pg.execute(
            """
            SELECT f.chat_jid, f.id, f.tentativas, m.tipo_midia, m.arquivo, m.tamanho, m.momento
            FROM fila f JOIN mensagens m USING (chat_jid, id)
            WHERE f.status = 'pendente' AND f.proxima_tentativa <= now()
            ORDER BY m.momento DESC
            LIMIT 1
            FOR UPDATE OF f SKIP LOCKED
            """
        ).fetchone()
        if tarefa is None:
            return False
        chat, mid, tentativas, tipo, arquivo, tamanho, momento = tarefa
        t = {"chat_jid": chat, "id": mid, "arquivo": arquivo, "tamanho": tamanho}
        try:
            with pg.transaction():  # savepoint: um erro aqui não aborta o registro da falha abaixo
                caminho = obter_arquivo(store, t, baixar_da_ponte)
                dados = caminho.read_bytes()
                chave = chave_r2(chat, momento, mid, caminho)
                enviar_r2(chave, dados)
                pg.execute(
                    """INSERT INTO midias (chat_jid, id, tipo, chave_r2, tamanho, sha256)
                       VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                    (chat, mid, tipo, chave, len(dados), hashlib.sha256(dados).hexdigest()),
                )
                if tipo == "audio":
                    texto, idioma = transcrever(caminho)
                    pg.execute(
                        """INSERT INTO transcricoes (chat_jid, id, texto, idioma) VALUES (%s, %s, %s, %s)
                           ON CONFLICT (chat_jid, id) DO UPDATE SET texto = EXCLUDED.texto, idioma = EXCLUDED.idioma""",
                        (chat, mid, texto, idioma),
                    )
                pg.execute(
                    "UPDATE fila SET status = 'concluida', concluida_em = now(), erro = NULL WHERE chat_jid = %s AND id = %s",
                    (chat, mid),
                )
        except Exception as e:  # noqa: BLE001 - a tarefa volta para a fila com espera crescente
            n = tentativas + 1
            status = "falhou" if n >= MAX_TENTATIVAS else "pendente"
            espera_min = min(2 ** n, 360)
            pg.execute(
                """UPDATE fila SET tentativas = %s, status = %s, erro = %s,
                          proxima_tentativa = now() + make_interval(mins => %s)
                   WHERE chat_jid = %s AND id = %s""",
                (n, status, str(e)[:1000], espera_min, chat, mid),
            )
            log.warning("mídia %s/%s: tentativa %s falhou: %s", chat, mid, n, e)
    return True
