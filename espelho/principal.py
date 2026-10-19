"""Worker do espelho: aplica as migrações e, a cada INTERVALO_S segundos, copia chats e mensagens
novas do banco da ponte para o Postgres do espelho e processa a fila de mídias (R2 + transcrição).

Variáveis: ESPELHO_DB_URL (Postgres), PONTE_DB (caminho do messages.db), INTERVALO_S (padrão 60),
FILA_SEGUNDOS (tempo máximo de fila por ciclo, padrão 50). A fila só roda com o R2 configurado
(R2_BUCKET); as demais variáveis dela estão em clientes.py.

Várias contas (números) do WhatsApp: cada uma tem a sua ponte, o seu worker e o seu schema no
espelho (`conta_<nome>`, com as mesmas tabelas). CONTA diz qual é a deste worker (padrão
"camillo"). Um schema por conta, e não uma coluna, porque a mesma mensagem de um grupo em comum
chega com o mesmo id às duas pontes. As tabelas criadas antes das contas (no schema public)
passam para a conta CONTA_HERDA_PUBLIC (padrão "camillo") na primeira subida.
"""
import logging
import os
import re
import time
from pathlib import Path

import psycopg

from sincronizar import abrir_ponte, sincronizar_chats, sincronizar_mensagens

MIGRACOES = Path(__file__).with_name("migracoes")
CONTA_PADRAO = "camillo"
TABELAS = ("chats", "mensagens", "sincronizacao", "midias", "transcricoes", "fila")
_NOME_CONTA = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
log = logging.getLogger("espelho")


def esquema(conta: str) -> str:
    if not _NOME_CONTA.match(conta):
        raise ValueError(f"nome de conta inválido: {conta!r} (minúsculas, números e _)")
    return f"conta_{conta}"


def conectar(url: str, conta: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True, options=f"-c search_path={esquema(conta)}")


def herdar_public(pg: psycopg.Connection, conta: str) -> bool:
    """Move para o schema da conta as tabelas que ficaram no public (espelho de antes das contas)."""
    nome = esquema(conta)
    with pg.transaction():
        if pg.execute("SELECT to_regclass('public.mensagens') IS NULL OR to_regclass(%s) IS NOT NULL",
                      (f"{nome}.mensagens",)).fetchone()[0]:
            return False
        for tabela in TABELAS:
            if pg.execute("SELECT to_regclass(%s)", (f"public.{tabela}",)).fetchone()[0]:
                pg.execute(f'ALTER TABLE public.{tabela} SET SCHEMA "{nome}"')
    log.info("tabelas do schema public passadas para %s", nome)
    return True


def aplicar_migracoes(pg: psycopg.Connection, conta: str = CONTA_PADRAO, herdar: bool = False) -> None:
    nome = esquema(conta)
    pg.execute(f'CREATE SCHEMA IF NOT EXISTS "{nome}"')
    if herdar:
        herdar_public(pg, conta)
    pg.execute(f'SET search_path TO "{nome}"')
    for arquivo in sorted(MIGRACOES.glob("*.sql")):
        pg.execute(arquivo.read_text(encoding="utf-8"))
        log.info("migração aplicada em %s: %s", nome, arquivo.name)


def ciclo(url: str, conta: str, ponte_db: str) -> None:
    ponte = abrir_ponte(ponte_db)
    try:
        with conectar(url, conta) as pg:
            chats = sincronizar_chats(ponte, pg)
            mensagens = sincronizar_mensagens(ponte, pg)
        if mensagens:
            log.info("sincronizado: %s chats, %s mensagens novas", chats, mensagens)
    finally:
        ponte.close()


def ciclo_fila(url: str, conta: str, store: Path, segundos: int, clientes, prefixo: str = "") -> None:
    from fila import enfileirar, processar_uma

    baixar, enviar, transcrever = clientes
    fim = time.monotonic() + segundos
    feitas = 0
    with conectar(url, conta) as pg:
        novas = enfileirar(pg)
        while time.monotonic() < fim and processar_uma(pg, store, baixar, enviar, transcrever, prefixo):
            feitas += 1
    if novas or feitas:
        log.info("fila: %s novas, %s processadas", novas, feitas)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    url = os.environ["ESPELHO_DB_URL"]
    conta = os.environ.get("CONTA", CONTA_PADRAO)
    herdeira = os.environ.get("CONTA_HERDA_PUBLIC", CONTA_PADRAO)
    # As mídias da conta herdeira já estão no R2 sem prefixo; as das outras contas ficam em <conta>/.
    prefixo = "" if conta == herdeira else f"{conta}/"
    ponte_db = os.environ.get("PONTE_DB", "/ponte/messages.db")
    store = Path(ponte_db).parent
    intervalo = int(os.environ.get("INTERVALO_S", "60"))
    segundos_fila = int(os.environ.get("FILA_SEGUNDOS", "50"))
    clientes = None
    if os.environ.get("R2_BUCKET"):
        from clientes import baixar_da_ponte, criar_envio_r2, transcrever

        clientes = (baixar_da_ponte, criar_envio_r2(), transcrever)
    else:
        log.info("R2_BUCKET não definido: fila de mídias desligada")
    with psycopg.connect(url, autocommit=True) as pg:
        aplicar_migracoes(pg, conta, herdar=conta == herdeira)
    log.info("conta %s (schema %s)", conta, esquema(conta))
    while True:
        inicio = time.monotonic()
        try:
            ciclo(url, conta, ponte_db)
        except Exception:  # noqa: BLE001 - registra e tenta no próximo ciclo
            log.exception("falha no ciclo de sincronização")
        if clientes:
            try:
                ciclo_fila(url, conta, store, segundos_fila, clientes, prefixo)
            except Exception:  # noqa: BLE001
                log.exception("falha no ciclo da fila")
        time.sleep(max(1, intervalo - (time.monotonic() - inicio)))


if __name__ == "__main__":
    main()
