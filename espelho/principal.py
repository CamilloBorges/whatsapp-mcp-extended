"""Worker do espelho: aplica as migrações e, a cada INTERVALO_S segundos, copia chats e mensagens
novas do banco da ponte para o Postgres do espelho.

Variáveis: ESPELHO_DB_URL (Postgres), PONTE_DB (caminho do messages.db), INTERVALO_S (padrão 60).
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    url = os.environ["ESPELHO_DB_URL"]
    ponte_db = os.environ.get("PONTE_DB", "/ponte/messages.db")
    intervalo = int(os.environ.get("INTERVALO_S", "60"))
    with psycopg.connect(url, autocommit=True) as pg:
        aplicar_migracoes(pg)
    while True:
        try:
            ciclo(url, ponte_db)
        except Exception:  # noqa: BLE001 - registra e tenta no próximo ciclo
            log.exception("falha no ciclo de sincronização")
        time.sleep(intervalo)


if __name__ == "__main__":
    main()
