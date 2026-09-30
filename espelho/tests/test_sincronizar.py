"""Testes da cópia ponte → espelho. Precisam de um Postgres descartável em ESPELHO_TEST_DB_URL."""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from principal import aplicar_migracoes  # noqa: E402
from sincronizar import sincronizar_chats, sincronizar_mensagens, tipo_chat  # noqa: E402

URL = os.environ.get("ESPELHO_TEST_DB_URL")
pytestmark = pytest.mark.skipif(not URL, reason="defina ESPELHO_TEST_DB_URL (Postgres descartável)")

ESQUEMA_PONTE = """
CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
CREATE TABLE messages (
    id TEXT, chat_jid TEXT, sender TEXT, sender_name TEXT, content TEXT, timestamp TIMESTAMP,
    is_from_me BOOLEAN, media_type TEXT, filename TEXT, file_length INTEGER,
    reply_to_message_id TEXT, is_forwarded BOOLEAN DEFAULT 0, is_edited BOOLEAN DEFAULT 0,
    is_system_message BOOLEAN DEFAULT 0, PRIMARY KEY (id, chat_jid)
);
"""


@pytest.fixture
def ponte(tmp_path):
    caminho = tmp_path / "messages.db"
    con = sqlite3.connect(caminho)
    con.executescript(ESQUEMA_PONTE)
    con.executemany("INSERT INTO chats VALUES (?, ?, ?)", [
        ("120363145616168799@g.us", "Operacional Fazenda Bomgado ", "2026-09-30 07:47:51-03:00"),
        ("status@broadcast", "", "2026-09-30 08:00:00-03:00"),
        ("555199999999@s.whatsapp.net", "Fornecedor", "2026-09-30 09:00:00-03:00"),
    ])
    con.executemany(
        "INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, media_type, filename, file_length) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("A1", "120363145616168799@g.us", "245", "Ricardo", "Diário de ontem.", "2026-09-30 06:01:30-03:00", 0, "", "", 0),
            ("A2", "120363145616168799@g.us", "245", "Ricardo", "", "2026-09-30 07:47:51-03:00", 0, "audio", "audio_1.ogg", 9039),
            ("S1", "status@broadcast", "111", "", "", "2026-09-30 08:00:00-03:00", 0, "video", "v.mp4", 10),
            ("F1", "555199999999@s.whatsapp.net", "555199999999", "", "Segue a nota", "2026-09-30 09:00:00-03:00", 1, "", "", 0),
        ],
    )
    con.commit()
    con.close()
    ro = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
    yield ro, caminho
    ro.close()


@pytest.fixture
def pg():
    with psycopg.connect(URL, autocommit=True) as con:
        con.execute("DROP TABLE IF EXISTS chats, mensagens, sincronizacao")
        aplicar_migracoes(con)
        yield con


def test_tipo_chat():
    assert tipo_chat("status@broadcast") == "status"
    assert tipo_chat("120363145616168799@g.us") == "grupo"
    assert tipo_chat("555199999999@s.whatsapp.net") == "contato"
    assert tipo_chat("96941807521806@lid") == "contato"


def test_copia_inicial_ignora_status(ponte, pg):
    ro, _ = ponte
    assert sincronizar_chats(ro, pg) == 3
    assert sincronizar_mensagens(ro, pg) == 3  # o Status (S1) fica de fora
    assert pg.execute("SELECT classificacao, classificado_por FROM chats WHERE jid = 'status@broadcast'").fetchone() == ("ignorar", "regra")
    assert pg.execute("SELECT count(*) FROM chats WHERE classificacao = 'a_classificar'").fetchone()[0] == 2
    audio = pg.execute("SELECT tipo_midia, arquivo, tamanho, texto FROM mensagens WHERE id = 'A2'").fetchone()
    assert audio == ("audio", "audio_1.ogg", 9039, None)
    momento = pg.execute("SELECT to_char(momento AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI') FROM mensagens WHERE id = 'A1'").fetchone()[0]
    assert momento == "2026-09-30 06:01"
    assert pg.execute("SELECT enviada_por_mim FROM mensagens WHERE id = 'F1'").fetchone()[0] is True


def test_incremental_e_idempotente(ponte, pg):
    ro, caminho = ponte
    sincronizar_chats(ro, pg)
    sincronizar_mensagens(ro, pg)
    assert sincronizar_mensagens(ro, pg) == 0  # nada novo
    escrita = sqlite3.connect(caminho)
    escrita.execute(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES "
        "('A3', '120363145616168799@g.us', '100', 'Vamos começar pelo P1', '2026-09-30 18:19:06-03:00', 0)"
    )
    escrita.commit()
    escrita.close()
    assert sincronizar_mensagens(ro, pg) == 1
    assert pg.execute("SELECT count(*) FROM mensagens").fetchone()[0] == 4


def test_busca_em_portugues(ponte, pg):
    ro, _ = ponte
    sincronizar_chats(ro, pg)
    sincronizar_mensagens(ro, pg)
    achou = pg.execute("SELECT id FROM mensagens WHERE busca @@ plainto_tsquery('portuguese', 'diários')").fetchall()
    assert achou == [("A1",)]


def test_reclassificar_para_ignorar_nao_apaga_nome(ponte, pg):
    ro, _ = ponte
    sincronizar_chats(ro, pg)
    pg.execute("UPDATE chats SET classificacao = 'trabalho', classificado_por = 'camillo' WHERE jid = '120363145616168799@g.us'")
    sincronizar_chats(ro, pg)  # uma nova leitura não pode desfazer a classificação
    assert pg.execute("SELECT classificacao FROM chats WHERE jid = '120363145616168799@g.us'").fetchone()[0] == "trabalho"
