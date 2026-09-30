"""Testes da fila de mídias (R2 e Whisper simulados). Precisam de ESPELHO_TEST_DB_URL."""
import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from fila import caminho_auto, enfileirar, processar_uma  # noqa: E402
from principal import aplicar_migracoes  # noqa: E402

URL = os.environ.get("ESPELHO_TEST_DB_URL")
pytestmark = pytest.mark.skipif(not URL, reason="defina ESPELHO_TEST_DB_URL (Postgres descartável)")

FAZENDA = "120363145616168799@g.us"
COTACAO = "555199584848-1605631030@g.us"


@pytest.fixture
def pg():
    with psycopg.connect(URL, autocommit=True) as con:
        con.execute("DROP TABLE IF EXISTS chats, mensagens, sincronizacao, midias, transcricoes, fila")
        aplicar_migracoes(con)
        con.execute(
            "INSERT INTO chats (jid, nome, tipo, classificacao) VALUES (%s, 'Operacional Fazenda', 'grupo', 'trabalho'), "
            "(%s, 'COTAÇÃO', 'grupo', 'ruido')", (FAZENDA, COTACAO))
        con.execute(
            """INSERT INTO mensagens (chat_jid, id, momento, tipo_midia, arquivo, tamanho, texto) VALUES
               (%(f)s, 'AUD1', now() - interval '1 hour', 'audio', 'audio_1.ogg', 5, NULL),
               (%(f)s, 'IMG1', now() - interval '2 hours', 'image', 'image_1.jpg', 3, NULL),
               (%(f)s, 'TXT1', now(), NULL, NULL, NULL, 'só texto'),
               (%(f)s, 'VELHO', now() - interval '90 days', 'image', 'velho.jpg', 3, NULL),
               (%(c)s, 'COT1', now(), 'audio', 'cot.ogg', 5, NULL)""",
            {"f": FAZENDA, "c": COTACAO})
        yield con


class Simulados:
    def __init__(self, store: Path):
        self.store, self.r2, self.ponte, self.falhar_r2 = store, {}, [], False

    def baixar(self, chat, mid):
        self.ponte.append(mid)
        destino = self.store / "media" / chat / f"{mid}.jpg"
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b"xyz")
        return f"store/media/{chat}/{mid}.jpg"

    def enviar(self, chave, dados):
        if self.falhar_r2:
            raise RuntimeError("R2 fora do ar")
        self.r2[chave] = dados

    def transcrever(self, caminho):
        return (f"transcrição de {caminho.name}", "pt")


def test_enfileira_so_trabalho_pessoal_com_midia_recente(pg):
    assert enfileirar(pg) == 2  # AUD1 e IMG1; nem o texto, nem o antigo, nem o chat de ruído
    assert enfileirar(pg) == 0  # não duplica
    assert {r[0] for r in pg.execute("SELECT id FROM fila")} == {"AUD1", "IMG1"}


def test_processa_audio_do_download_automatico_e_transcreve(pg, tmp_path):
    local = caminho_auto(tmp_path, FAZENDA, "audio_1.ogg", "AUD1")
    local.parent.mkdir(parents=True)
    local.write_bytes(b"ogg!!")  # 5 bytes = tamanho da mensagem
    s = Simulados(tmp_path)
    enfileirar(pg)
    assert processar_uma(pg, tmp_path, s.baixar, s.enviar, s.transcrever)  # a mais recente: AUD1
    assert s.ponte == []  # usou o arquivo local, sem pedir à ponte
    chave = pg.execute("SELECT chave_r2 FROM midias WHERE id = 'AUD1'").fetchone()[0]
    assert chave.startswith("120363145616168799_g.us/") and chave.endswith("/AUD1.ogg")
    assert s.r2[chave] == b"ogg!!"
    assert pg.execute("SELECT sha256 FROM midias WHERE id = 'AUD1'").fetchone()[0] == hashlib.sha256(b"ogg!!").hexdigest()
    assert pg.execute("SELECT texto FROM transcricoes WHERE id = 'AUD1'").fetchone()[0] == "transcrição de audio_1.ogg"
    assert pg.execute("SELECT status FROM fila WHERE id = 'AUD1'").fetchone()[0] == "concluida"


def test_sem_arquivo_local_pede_a_ponte_e_nao_transcreve_imagem(pg, tmp_path):
    s = Simulados(tmp_path)
    enfileirar(pg)
    processar_uma(pg, tmp_path, s.baixar, s.enviar, s.transcrever)  # AUD1 (sem arquivo local → ponte)
    processar_uma(pg, tmp_path, s.baixar, s.enviar, s.transcrever)  # IMG1
    assert s.ponte == ["AUD1", "IMG1"]
    assert pg.execute("SELECT count(*) FROM transcricoes WHERE id = 'IMG1'").fetchone()[0] == 0
    assert processar_uma(pg, tmp_path, s.baixar, s.enviar, s.transcrever) is False  # fila vazia


def test_falha_volta_para_a_fila_sem_registro_parcial(pg, tmp_path):
    s = Simulados(tmp_path)
    s.falhar_r2 = True
    enfileirar(pg)
    processar_uma(pg, tmp_path, s.baixar, s.enviar, s.transcrever)
    status, tentativas, erro, futura = pg.execute(
        "SELECT status, tentativas, erro, proxima_tentativa > now() FROM fila WHERE id = 'AUD1'").fetchone()
    assert (status, tentativas, futura) == ("pendente", 1, True)
    assert "R2 fora do ar" in erro
    assert pg.execute("SELECT count(*) FROM midias").fetchone()[0] == 0


def test_reclassificar_traz_o_historico(pg):
    pg.execute("UPDATE chats SET classificacao = 'pessoal' WHERE jid = %s", (COTACAO,))
    assert enfileirar(pg) == 3  # AUD1, IMG1 e agora o COT1
