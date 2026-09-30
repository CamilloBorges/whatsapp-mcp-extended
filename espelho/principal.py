"""Worker do espelho: aplica as migrações e, a cada INTERVALO_S segundos, copia chats e mensagens
novas do banco da ponte para o Postgres do espelho e processa a fila de mídias (R2 + transcrição).

Variáveis: ESPELHO_DB_URL (Postgres), PONTE_DB (caminho do messages.db), INTERVALO_S (padrão 60),
FILA_SEGUNDOS (tempo máximo de fila por ciclo, padrão 50). A fila só roda com o R2 configurado
(R2_BUCKET); as demais variáveis dela estão em clientes.py.
"""
import logging
import os
import time
from pathlib import Path

import psycopg

from sincronizar import abrir_ponte, sincronizar_chats, sincronizar_mensagens

MIGRACOES = Path(__file__).with_name("migracoes")
log = logging.getLogger("espelho")


def aplicar_migracoes(pg: psycopg.Connection) -> None:
    for arquivo in sorted(MIGRACOES.glob("*.sql")):
        pg.execute(arquivo.read_text(encoding="utf-8"))
        log.info("migração aplicada: %s", arquivo.name)


def ciclo(url: str, ponte_db: str) -> None:
    ponte = abrir_ponte(ponte_db)
    try:
        with psycopg.connect(url, autocommit=True) as pg:
            chats = sincronizar_chats(ponte, pg)
            mensagens = sincronizar_mensagens(ponte, pg)
        if mensagens:
            log.info("sincronizado: %s chats, %s mensagens novas", chats, mensagens)
    finally:
        ponte.close()


def ciclo_fila(url: str, store: Path, segundos: int, clientes) -> None:
    from fila import enfileirar, processar_uma

    baixar, enviar, transcrever = clientes
    fim = time.monotonic() + segundos
    feitas = 0
    with psycopg.connect(url, autocommit=True) as pg:
        novas = enfileirar(pg)
        while time.monotonic() < fim and processar_uma(pg, store, baixar, enviar, transcrever):
            feitas += 1
    if novas or feitas:
        log.info("fila: %s novas, %s processadas", novas, feitas)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    url = os.environ["ESPELHO_DB_URL"]
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
        aplicar_migracoes(pg)
    while True:
        inicio = time.monotonic()
        try:
            ciclo(url, ponte_db)
        except Exception:  # noqa: BLE001 - registra e tenta no próximo ciclo
            log.exception("falha no ciclo de sincronização")
        if clientes:
            try:
                ciclo_fila(url, store, segundos_fila, clientes)
            except Exception:  # noqa: BLE001
                log.exception("falha no ciclo da fila")
        time.sleep(max(1, intervalo - (time.monotonic() - inicio)))


if __name__ == "__main__":
    main()
