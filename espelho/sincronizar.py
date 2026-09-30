"""Cópia incremental do banco interno da ponte (messages.db, SQLite) para o espelho (Postgres).

Lê só as mensagens que entraram depois da última posição (rowid) e grava com upsert, então
repetir um lote é seguro. Mensagens de chats classificados como "ignorar" não entram.
O SQLite é aberto em modo só leitura: quem escreve nele é a ponte.
"""
import sqlite3

import psycopg

LOTE = 2000

# Classificação automática na criação do chat (o resto nasce "a_classificar").
REGRAS_INICIAIS = {"status@broadcast": "ignorar"}

COLUNAS = (
    "rowid, chat_jid, id, sender, sender_name, is_from_me, timestamp, content, media_type, "
    "filename, file_length, reply_to_message_id, is_forwarded, is_edited, is_system_message"
)


def tipo_chat(jid: str) -> str:
    if jid == "status@broadcast":
        return "status"
    if jid.endswith("@g.us"):
        return "grupo"
    if jid.endswith("@newsletter"):
        return "newsletter"
    if jid.endswith("@s.whatsapp.net") or jid.endswith("@lid"):
        return "contato"
    return "outro"


def abrir_ponte(caminho: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)


def sincronizar_chats(ponte: sqlite3.Connection, pg: psycopg.Connection) -> int:
    linhas = ponte.execute("SELECT jid, name, last_message_time FROM chats").fetchall()
    with pg.transaction(), pg.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO chats (jid, nome, tipo, classificacao, classificado_por, classificado_em, ultima_mensagem)
            VALUES (%(jid)s, %(nome)s, %(tipo)s, %(classificacao)s, %(por)s,
                    CASE WHEN %(por)s::text IS NULL THEN NULL ELSE now() END, %(ultima)s)
            ON CONFLICT (jid) DO UPDATE SET
                nome = coalesce(nullif(EXCLUDED.nome, ''), chats.nome),
                ultima_mensagem = greatest(chats.ultima_mensagem, EXCLUDED.ultima_mensagem),
                atualizado_em = now()
            """,
            [
                {
                    "jid": jid,
                    "nome": nome,
                    "tipo": tipo_chat(jid),
                    "classificacao": REGRAS_INICIAIS.get(jid, "a_classificar"),
                    "por": "regra" if jid in REGRAS_INICIAIS else None,
                    "ultima": ultima or None,
                }
                for jid, nome, ultima in linhas
            ],
        )
    return len(linhas)


def _registro(linha: tuple) -> dict:
    (_, chat, mid, remetente, nome, de_mim, momento, texto, midia, arquivo, tamanho,
     resposta, encaminhada, editada, sistema) = linha
    return {
        "chat": chat, "id": mid, "remetente": remetente or None, "nome": nome or None,
        "de_mim": bool(de_mim), "momento": momento, "texto": texto or None,
        "midia": midia or None, "arquivo": arquivo or None, "tamanho": tamanho or None,
        "resposta": resposta or None, "encaminhada": bool(encaminhada),
        "editada": bool(editada), "sistema": bool(sistema),
    }


def sincronizar_mensagens(ponte: sqlite3.Connection, pg: psycopg.Connection) -> int:
    """Copia as mensagens novas em lotes; devolve quantas foram gravadas."""
    pg.execute("INSERT INTO sincronizacao (fonte) VALUES ('messages') ON CONFLICT DO NOTHING")
    ultimo = pg.execute("SELECT ultimo_rowid FROM sincronizacao WHERE fonte = 'messages'").fetchone()[0]
    ignorados = {jid for (jid,) in pg.execute("SELECT jid FROM chats WHERE classificacao = 'ignorar'")}
    gravadas = 0
    while True:
        linhas = ponte.execute(
            f"SELECT {COLUNAS} FROM messages WHERE rowid > ? ORDER BY rowid LIMIT ?", (ultimo, LOTE)
        ).fetchall()
        if not linhas:
            return gravadas
        registros = [_registro(l) for l in linhas if l[1] not in ignorados and l[6]]
        with pg.transaction(), pg.cursor() as cur:
            if registros:
                cur.executemany(
                    """
                    INSERT INTO mensagens (chat_jid, id, remetente, remetente_nome, enviada_por_mim, momento,
                                           texto, tipo_midia, arquivo, tamanho, resposta_a, encaminhada, editada, sistema)
                    VALUES (%(chat)s, %(id)s, %(remetente)s, %(nome)s, %(de_mim)s, %(momento)s, %(texto)s,
                            %(midia)s, %(arquivo)s, %(tamanho)s, %(resposta)s, %(encaminhada)s, %(editada)s, %(sistema)s)
                    ON CONFLICT (chat_jid, id) DO UPDATE SET
                        texto = EXCLUDED.texto, editada = EXCLUDED.editada,
                        remetente_nome = coalesce(EXCLUDED.remetente_nome, mensagens.remetente_nome)
                    """,
                    registros,
                )
            ultimo = linhas[-1][0]
            cur.execute(
                "UPDATE sincronizacao SET ultimo_rowid = %s, atualizado_em = now() WHERE fonte = 'messages'",
                (ultimo,),
            )
        gravadas += len(registros)
