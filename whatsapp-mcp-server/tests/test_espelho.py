"""Ferramentas do espelho (fase 3) e posse das contas. Precisam de um Postgres descartável em
ESPELHO_TEST_DB_URL."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "espelho"))

import espelho  # noqa: E402

URL = os.environ.get("ESPELHO_TEST_DB_URL")
pytestmark = pytest.mark.skipif(not URL, reason="defina ESPELHO_TEST_DB_URL (Postgres descartável)")

FAZENDA = "120363145616168799@g.us"
FORNECEDOR = "555199999999@s.whatsapp.net"
RUIDO = "555100000000-1@g.us"


@pytest.fixture
def banco(monkeypatch):
    import psycopg
    from principal import aplicar_migracoes  # o worker do espelho cria contas, schemas e papéis

    with psycopg.connect(URL, autocommit=True) as con:
        con.execute("DROP SCHEMA IF EXISTS conta_teste, conta_outra CASCADE")
        con.execute("DROP TABLE IF EXISTS public.contas")
        for conta in ("teste", "outra"):
            aplicar_migracoes(con, conta)
        con.execute(
            "INSERT INTO public.contas (conta, vaga, dono, situacao) VALUES ('teste', 1, 'Camillo', 'ativa'), "
            "('outra', 2, 'Ana', 'ativa')"
        )
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
        con.execute("DROP TABLE public.contas")


def test_resumo_do_dia_com_audio_transcrito_e_sem_ruido(banco):
    r = espelho.resumo_do_periodo("Camillo", "2026-10-19")
    assert r["conta"] == "teste"  # sem conta: a primeira do dono
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
    r = espelho.resumo_do_periodo("Camillo", "2026-10-18", "2026-10-19", chat="fornecedor")
    assert [c["chat"] for c in r["chats"]] == ["Fornecedor Carne"]
    assert r["chats"][0]["linhas"][0] == "18/10 17:00 Eu: Pode mandar amanhã"


def test_resumo_trunca_pelo_tamanho(banco):
    r = espelho.resumo_do_periodo("Camillo", "2026-10-19", max_caracteres=100)
    assert r["truncado"] is True
    assert {c["chat"] for c in r["chats_sem_detalhe"]} >= {"Operacional Fazenda"}


def test_busca_acha_no_audio_com_contexto(banco):
    r = espelho.buscar("Camillo", "vacinas")  # no plural; na Fazenda só existe na transcrição do áudio
    achado = next(x for x in r["resultados"] if x["chat"] == "Operacional Fazenda")
    assert achado["message_id"] == "A2"
    marcada = [linha for linha in achado["conversa"] if linha.startswith("»")]
    assert marcada == ["» 19/10 07:47 Ricardo: [áudio] A vacina do rebanho chegou hoje"]
    assert len(achado["conversa"]) == 3  # 06:01, 07:47 e 08:10


def test_busca_por_chat_e_periodo(banco):
    assert espelho.buscar("Camillo", "vacina", chat="Promoções")["resultados"][0]["message_id"] == "R1"
    assert espelho.buscar("Camillo", "vacina", ate="2026-10-18")["resultados"] == []


def test_so_o_dono_usa_a_conta(banco):
    assert espelho.resumo_do_periodo("Ana", "2026-10-19")["conta"] == "outra"  # a dela, vazia
    assert espelho.resumo_do_periodo("Ana", "2026-10-19")["total_mensagens"] == 0
    with pytest.raises(espelho.ErroEspelho, match="não é sua"):
        espelho.resumo_do_periodo("Ana", "2026-10-19", conta="teste")
    with pytest.raises(espelho.ErroEspelho, match="não é sua"):
        espelho.buscar("Ana", "vacina", conta="teste")
    with pytest.raises(espelho.ErroEspelho, match="identificar"):
        espelho.resumo_do_periodo(None, "2026-10-19")
    with pytest.raises(espelho.ErroEspelho, match="ainda não tem"):
        espelho.resumo_do_periodo("Antonio", "2026-10-19")
    with pytest.raises(espelho.ErroEspelho, match="inválida"):
        espelho.resumo_do_periodo("Camillo", "2026-10-19", conta="x; drop")


def test_sql_da_ana_nao_alcanca_a_conta_do_camillo_nem_a_tabela_de_contas(banco):
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        espelho.consultar("Ana", "SELECT count(*) FROM conta_teste.mensagens")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        espelho.consultar("Ana", "SELECT * FROM public.contas")
    assert espelho.consultar("Ana", "SELECT count(*) FROM mensagens")["linhas"] == [[0]]


def test_consultar_so_leitura(banco):
    r = espelho.consultar("Camillo", "SELECT classificacao, count(*) AS n FROM chats GROUP BY 1 ORDER BY 1")
    assert r["colunas"] == ["classificacao", "n"]
    assert r["linhas"] == [["ruido", 1], ["trabalho", 2]]
    for proibida in (
        "DELETE FROM chats",
        "SELECT 1; DELETE FROM chats",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT set_config('role', 'espelho', false)",
    ):
        with pytest.raises(espelho.ErroEspelho):
            espelho.consultar("Camillo", proibida)


def test_consultar_nao_escreve_nem_com_cte(banco):
    import psycopg

    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        espelho.consultar("Camillo", "WITH x AS (DELETE FROM chats RETURNING 1) SELECT count(*) FROM x")
    assert banco.execute("SELECT count(*) FROM chats").fetchone()[0] == 3


def test_limite_de_linhas(banco):
    r = espelho.consultar("Camillo", "SELECT generate_series(1, 10) AS n", limite=3)
    assert r["linhas"] == [[1], [2], [3]] and r["truncado"] is True


def test_sem_banco_configurado(monkeypatch):
    monkeypatch.delenv("ESPELHO_DB_URL", raising=False)
    with pytest.raises(espelho.ErroEspelho, match="não está configurado"):
        espelho.resumo_do_periodo("Camillo", "2026-10-19")


# ---- vagas de número (numeros.py), com pontes simuladas ----------------------------------------


class PontesSimuladas:
    def __init__(self):
        self.pareadas: set[int] = set()
        self.pedidos: list[tuple[int, str, str, dict | None]] = []

    def __call__(self, metodo, url, json=None, headers=None, timeout=None):
        vaga = 2 if "bridge-2" in url else 1
        caminho = url.split(":8080", 1)[1]
        self.pedidos.append((vaga, metodo, caminho, json))
        corpo = {"success": True}
        if caminho == "/api/connection":
            corpo |= {"linked": vaga in self.pareadas, "connected": vaga in self.pareadas}
        elif caminho == "/api/pair":
            corpo |= {"code": "K7QX29MD", "expires_in": 160}
        elif caminho == "/api/pairing":
            corpo |= {"in_progress": True, "expires_in": 120}
        elif caminho == "/api/logout":
            self.pareadas.discard(vaga)
        elif caminho == "/api/download":
            corpo |= {"path": f"/app/whatsapp-bridge/store/media/{json['chat_jid']}/{json['message_id']}.ogg"}
            corpo["via_celular"] = getattr(self, "via_celular", False)

        class Resposta:
            status_code = 200

            def json(self):
                return corpo

        return Resposta()


@pytest.fixture
def vagas(banco, monkeypatch):
    import numeros

    banco.execute("DELETE FROM public.contas WHERE conta = 'outra'")  # a vaga 2 fica livre
    monkeypatch.setenv("ESPELHO_VAGAS", '{"1": "http://whatsapp-bridge:8080", "2": "http://whatsapp-bridge-2:8080"}')
    pontes = PontesSimuladas()
    pontes.pareadas.add(1)
    monkeypatch.setattr(numeros.requests, "request", pontes)
    yield numeros, pontes
    banco.execute("DROP SCHEMA IF EXISTS conta_ana CASCADE")


def test_adicionar_numero_ocupa_a_vaga_livre_e_devolve_o_codigo(vagas, banco):
    numeros, pontes = vagas
    r = numeros.adicionar("Ana", "+55 (51) 99999-1234", "ana")
    assert (r["conta"], r["vaga"], r["codigo"], r["expira_em_segundos"]) == ("ana", 2, "K7QX-29MD", 160)
    assert "Conectar com número de telefone" in " ".join(r["instrucoes"])
    assert (2, "POST", "/api/pair", {"phone_number": "5551999991234"}) in pontes.pedidos
    assert banco.execute("SELECT vaga, dono, situacao FROM public.contas WHERE conta = 'ana'").fetchone() == (
        2,
        "Ana",
        "pareando",
    )
    with pytest.raises(espelho.ErroEspelho, match="não há vaga livre"):
        numeros.adicionar("Antonio", "+55 51 98888-0000", "antonio")
    with pytest.raises(espelho.ErroEspelho, match="outra pessoa"):
        numeros.adicionar("Camillo", "+55 51 98888-0000", "ana")


def test_status_e_remover_so_do_dono(vagas, banco):
    numeros, pontes = vagas
    numeros.adicionar("Ana", "+55 51 99999-1234", "ana")
    st = numeros.status("Ana")
    assert st["contas"][0]["conta"] == "ana" and st["contas"][0]["codigo_pendente"] is True
    assert st["vagas_livres"] == 0
    pontes.pareadas.add(2)  # a Ana digitou o código
    assert numeros.status("Ana")["contas"][0]["situacao"] == "ativa"
    assert [c["conta"] for c in numeros.status("Camillo")["contas"]] == ["teste"]  # não vê a da Ana
    with pytest.raises(espelho.ErroEspelho, match="não é sua"):
        numeros.remover("Camillo", "ana", confirmar=True)
    assert "confirmar" in numeros.remover("Ana", "ana")  # sem confirmar, só explica
    assert 2 in pontes.pareadas
    assert numeros.remover("Ana", "ana", confirmar=True)["removida"] is True
    assert 2 not in pontes.pareadas
    assert banco.execute("SELECT vaga, situacao FROM public.contas WHERE conta = 'ana'").fetchone() == (2, "removida")
    numeros.adicionar("Ana", "+55 51 99999-1234", "ana")  # a vaga continua dela para reconectar


def test_telefone_invalido(vagas):
    numeros, _ = vagas
    with pytest.raises(espelho.ErroEspelho, match="telefone inválido"):
        numeros.adicionar("Ana", "1234", "ana")


def test_classificar_so_os_chats_da_propria_conta(banco):
    r = espelho.classificar("Camillo", ["operacional fazenda", FORNECEDOR, "Não existe"], "pessoal", motivo="teste")
    assert sorted(r["classificados"]) == ["Fornecedor Carne", "Operacional Fazenda"]
    assert r["nao_encontrados"] == ["Não existe"]
    assert banco.execute(
        "SELECT classificacao, classificado_por, motivo FROM chats WHERE jid = %s", (FAZENDA,)
    ).fetchone() == (
        "pessoal",
        "camillo",
        "teste",
    )
    with pytest.raises(espelho.ErroEspelho, match="não é sua"):
        espelho.classificar("Ana", [FAZENDA], "ruido", conta="teste")
    assert espelho.classificar("Ana", [FAZENDA], "ruido")["nao_encontrados"] == [FAZENDA]  # na conta dela não existe
    with pytest.raises(espelho.ErroEspelho, match="inválida"):
        espelho.classificar("Camillo", [FAZENDA], "importante")


def test_midia_da_mensagem_busca_na_ponte_da_conta_e_guarda_a_transcricao(banco, monkeypatch, tmp_path):
    import numeros

    pontes = PontesSimuladas()
    monkeypatch.setattr(numeros.requests, "request", pontes)
    monkeypatch.setenv("ESPELHO_VAGAS", '{"1": "http://whatsapp-bridge:8080", "2": "http://whatsapp-bridge-2:8080"}')
    monkeypatch.setenv("ESPELHO_STORES", json.dumps({"1": str(tmp_path)}))
    banco.execute(
        "INSERT INTO mensagens (chat_jid, id, momento, tipo_midia, arquivo) VALUES (%s, 'A9', '2026-10-19 09:30', 'audio', 'a9.ogg')",
        (FAZENDA,),
    )
    transcritos = []

    def transcrever(caminho):
        transcritos.append(caminho)
        return {"text": "Chegou o sal mineral", "language": "pt"}

    pontes.via_celular = True
    r = numeros.midia("Camillo", FAZENDA, "A9", transcrever=transcrever)
    assert r["transcricao"] == "Chegou o sal mineral" and r["via_celular"] is True
    assert r["arquivo"] == os.path.join(str(tmp_path), "media", FAZENDA, "A9.ogg")
    assert (1, "POST", "/api/download", {"message_id": "A9", "chat_jid": FAZENDA}) in pontes.pedidos
    assert banco.execute("SELECT texto FROM transcricoes WHERE id = 'A9'").fetchone()[0] == "Chegou o sal mineral"
    # o resumo passa a mostrar o áudio transcrito; antes, ele vinha em audios_sem_transcricao
    fazenda = espelho.resumo_do_periodo("Camillo", "2026-10-19")["chats"][0]
    assert any(linha.endswith("[áudio] Chegou o sal mineral") for linha in fazenda["linhas"])
    assert "audios_sem_transcricao" not in fazenda
    # áudio já transcrito: não transcreve de novo
    numeros.midia("Camillo", FAZENDA, "A9", transcrever=transcrever)
    assert len(transcritos) == 1
    with pytest.raises(espelho.ErroEspelho, match="não encontrada"):
        numeros.midia("Ana", FAZENDA, "A9", transcrever=transcrever)  # a mensagem não é da conta dela
    with pytest.raises(espelho.ErroEspelho, match="não tem mídia"):
        numeros.midia("Camillo", FAZENDA, "A1", transcrever=transcrever)


def test_resumo_lista_os_audios_sem_transcricao(banco):
    banco.execute(
        "INSERT INTO mensagens (chat_jid, id, momento, tipo_midia, arquivo) VALUES (%s, 'A8', '2026-10-19 09:20', 'audio', 'a8.ogg')",
        (FAZENDA,),
    )
    fazenda = espelho.resumo_do_periodo("Camillo", "2026-10-19")["chats"][0]
    assert fazenda["chat_jid"] == FAZENDA
    assert fazenda["audios_sem_transcricao"] == [{"message_id": "A8", "hora": "19/10 09:20"}]


# ---- identidade (identidade.py) ------------------------------------------------------------------


def test_identidade_pelo_hub_e_pelo_acesso_direto():
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from identidade import Identidade

    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class Chaves:
        def get_signing_key_from_jwt(self, token):
            return type("K", (), {"key": chave.public_key()})()

    ident = Identidade(
        "bomgado.cloudflareaccess.com",
        "aud-hub",
        "aud-direto",
        {"Ana.Borges@bomgado.com": "Ana"},
        "Camillo",
        jwks=Chaves(),
    )

    def token(aud, email=None, chave_assinatura=chave):
        dados = {"aud": [aud], "iss": "https://bomgado.cloudflareaccess.com", "exp": int(time.time()) + 60}
        if email:
            dados["email"] = email
        return jwt.encode(dados, chave_assinatura, algorithm="RS256")

    assert ident.pessoa({"cf-access-jwt-assertion": token("aud-hub", "ana.borges@bomgado.com")}) == "Ana"
    assert ident.pessoa({"cf-access-jwt-assertion": token("aud-hub", "estranho@x.com")}) is None
    assert ident.pessoa({"cf-access-jwt-assertion": token("aud-direto")}) == "Camillo"
    assert ident.pessoa({"cf-access-jwt-assertion": token("outra-aplicacao", "ana.borges@bomgado.com")}) is None
    outra_chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert ident.pessoa({"cf-access-jwt-assertion": token("aud-hub", "ana.borges@bomgado.com", outra_chave)}) is None
    assert ident.pessoa({"x-bomgado-pessoa": "Camillo"}) is None  # cabeçalho simples não identifica
    assert ident.pessoa({}) is None
