"""Ferramentas do espelho (fase 3). Precisam de um Postgres descartável em ESPELHO_TEST_DB_URL."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import espelho  # noqa: E402

URL = os.environ.get("ESPELHO_TEST_DB_URL")
pytestmark = pytest.mark.skipif(not URL, reason="defina ESPELHO_TEST_DB_URL (Postgres descartável)")

MIGRACOES = Path(__file__).resolve().parents[2] / "espelho" / "migracoes"
FAZENDA = "120363145616168799@g.us"
FORNECEDOR = "555199999999@s.whatsapp.net"
RUIDO = "555100000000-1@g.us"


@pytest.fixture
def banco(monkeypatch):
    import psycopg

    with psycopg.connect(URL, autocommit=True) as con:
        con.execute("DROP SCHEMA IF EXISTS conta_teste, conta_outra CASCADE")
        for conta in ("teste", "outra"):
            con.execute(f"CREATE SCHEMA conta_{conta}")
            con.execute(f"SET search_path TO conta_{conta}")
            for arquivo in sorted(MIGRACOES.glob("*.sql")):
                con.execute(arquivo.read_text(encoding="utf-8"))
        con.execute("SET search_path TO conta_teste")
        con.execute("SET TimeZone = 'America/Sao_Paulo'")
        con.execute(
            "INSERT INTO chats (jid, nome, tipo, classificacao) VALUES "
            "(%s, 'Operacional Fazenda', 'grupo', 'trabalho'), (%s, 'Fornecedor Carne', 'contato', 'trabalho'), "
            "(%s, 'Promoções', 'grupo', 'ruido')",
            (FAZENDA, FORNECEDOR, RUIDO),
        )
        con.execute(
            """INSERT INTO mensagens (chat_jid, id, remetente_nome, enviada_por_mim, momento, texto, tipo_midia, arquivo) VALUES
               (%(f)s, 'A1', 'Ricardo', false, '2026-10-19 06:01', 'Diário de ontem: 40 terneiros no piquete 3', NULL, NULL),
               (%(f)s, 'A2', 'Ricardo', false, '2026-10-19 07:47', NULL, 'audio', 'audio_1.ogg'),
               (%(f)s, 'A3', NULL, true, '2026-10-19 08:10', 'Ok, obrigado', NULL, NULL),
               (%(p)s, 'P1', NULL, false, '2026-10-19 09:00', 'Segue a nota fiscal', 'document', 'NF 123.pdf'),
               (%(p)s, 'P2', NULL, true, '2026-10-18 17:00', 'Pode mandar amanhã', NULL, NULL),
               (%(r)s, 'R1', 'Loja', false, '2026-10-19 10:00', 'Promoção de vacina', NULL, NULL)""",
            {"f": FAZENDA, "p": FORNECEDOR, "r": RUIDO},
        )
        con.execute(
            "INSERT INTO transcricoes (chat_jid, id, texto, idioma) VALUES (%s, 'A2', 'A vacina do rebanho chegou hoje', 'pt')",
            (FAZENDA,),
        )
        monkeypatch.setenv("ESPELHO_DB_URL", URL)
        yield con
        con.execute("DROP SCHEMA conta_teste, conta_outra CASCADE")


def test_resumo_do_dia_com_audio_transcrito_e_sem_ruido(banco):
    r = espelho.resumo_do_periodo("2026-10-19", conta="teste")
    assert r["total_mensagens"] == 4  # o ruído e a mensagem do dia 18 ficam de fora
    nomes = [c["chat"] for c in r["chats"]]
    assert nomes == ["Operacional Fazenda", "Fornecedor Carne"]  # mais mensagens primeiro
    fazenda = r["chats"][0]["linhas"]
    assert fazenda[0] == "06:01 Ricardo: Diário de ontem: 40 terneiros no piquete 3"
    assert fazenda[1] == "07:47 Ricardo: [áudio] A vacina do rebanho chegou hoje"
    assert fazenda[2] == "08:10 Eu: Ok, obrigado"
    assert r["chats"][1]["linhas"] == ["09:00 Fornecedor Carne: [documento: NF 123.pdf] Segue a nota fiscal"]
    assert r["audios_sem_transcricao"] == 0 and r["truncado"] is False


def test_resumo_de_varios_dias_mostra_a_data_e_filtra_chat(banco):
    r = espelho.resumo_do_periodo("2026-10-18", "2026-10-19", conta="teste", chat="fornecedor")
    assert [c["chat"] for c in r["chats"]] == ["Fornecedor Carne"]
    assert r["chats"][0]["linhas"][0] == "18/10 17:00 Eu: Pode mandar amanhã"


def test_resumo_trunca_pelo_tamanho(banco):
    r = espelho.resumo_do_periodo("2026-10-19", conta="teste", max_caracteres=100)
    assert r["truncado"] is True
    assert {c["chat"] for c in r["chats_sem_detalhe"]} >= {"Operacional Fazenda"}


def test_busca_acha_no_audio_com_contexto(banco):
    r = espelho.buscar("vacinas", conta="teste")  # no plural; na Fazenda só existe na transcrição do áudio
    chats = {x["chat"] for x in r["resultados"]}
    assert "Operacional Fazenda" in chats
    achado = next(x for x in r["resultados"] if x["chat"] == "Operacional Fazenda")
    assert achado["message_id"] == "A2"
    marcada = [linha for linha in achado["conversa"] if linha.startswith("»")]
    assert marcada == ["» 19/10 07:47 Ricardo: [áudio] A vacina do rebanho chegou hoje"]
    assert len(achado["conversa"]) == 3  # 06:01, 07:47 e 08:10


def test_busca_por_chat_e_periodo(banco):
    assert espelho.buscar("vacina", conta="teste", chat="Promoções")["resultados"][0]["message_id"] == "R1"
    assert espelho.buscar("vacina", conta="teste", ate="2026-10-18")["resultados"] == []


def test_contas_isoladas(banco):
    assert {"teste", "outra"} <= set(espelho.contas())
    assert espelho.resumo_do_periodo("2026-10-19", conta="outra")["total_mensagens"] == 0
    with pytest.raises(espelho.ErroEspelho, match="não existe"):
        espelho.resumo_do_periodo("2026-10-19", conta="inexistente")
    with pytest.raises(espelho.ErroEspelho, match="inválida"):
        espelho.resumo_do_periodo("2026-10-19", conta="x; drop")


def test_consultar_so_leitura(banco):
    r = espelho.consultar("SELECT classificacao, count(*) AS n FROM chats GROUP BY 1 ORDER BY 1", conta="teste")
    assert r["colunas"] == ["classificacao", "n"]
    assert r["linhas"] == [["ruido", 1], ["trabalho", 2]]
    for proibida in (
        "DELETE FROM chats",
        "SELECT 1; DELETE FROM chats",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT set_config('default_transaction_read_only', 'off', false)",
    ):
        with pytest.raises(espelho.ErroEspelho):
            espelho.consultar(proibida, conta="teste")


def test_consultar_nao_escreve_nem_com_cte(banco):
    import psycopg

    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        espelho.consultar("WITH x AS (DELETE FROM chats RETURNING 1) SELECT count(*) FROM x", conta="teste")
    assert banco.execute("SELECT count(*) FROM chats").fetchone()[0] == 3


def test_limite_de_linhas(banco):
    r = espelho.consultar("SELECT generate_series(1, 10) AS n", conta="teste", limite=3)
    assert r["linhas"] == [[1], [2], [3]] and r["truncado"] is True


def test_sem_banco_configurado(monkeypatch):
    monkeypatch.delenv("ESPELHO_DB_URL", raising=False)
    with pytest.raises(espelho.ErroEspelho, match="não está configurado"):
        espelho.resumo_do_periodo("2026-10-19")
