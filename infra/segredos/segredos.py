"""Entrega os segredos do Azure Key Vault aos demais serviços da plataforma.

É o único contêiner com a credencial do cofre (app "plataforma-dados-vps", papel "Usuário de
Segredos do Cofre de Chaves", só leitura). Cada consumidor tem o próprio volume em memória
(tmpfs), montado aqui em /run/segredos/<grupo>, e recebe nele só o que usa, num arquivo
segredos.env que o com_segredos.sh carrega antes de iniciar o processo.

Grupos: uma variável SEGREDOS_<GRUPO> por consumidor, com os nomes das variáveis de ambiente
separados por espaço. No cofre, o nome é a variável em minúsculas com hífen
(POSTGRES_PASSWORD -> postgres-password). Sufixo "?" = opcional (se não existir, fica de fora).

Relê o cofre a cada ATUALIZAR_A_CADA segundos (padrão 1 h): uma senha trocada no cofre chega ao
serviço no próximo reinício dele. Falha na releitura só registra e mantém os arquivos atuais.
"""
import logging
import os
import signal
import sys
import time
from pathlib import Path

PREFIXO = "SEGREDOS_"
PASTA = Path("/run/segredos")
PRONTO = Path("/tmp/pronto")  # healthcheck: primeira entrega completa

log = logging.getLogger("segredos")


def nome_no_cofre(variavel: str) -> str:
    return variavel.lower().replace("_", "-")


def grupos(env: dict[str, str]) -> dict[str, list[str]]:
    return {k[len(PREFIXO):].lower(): v.split() for k, v in env.items() if k.startswith(PREFIXO)}


def linha_env(nome: str, valor: str) -> str:
    """NOME='valor' com aspas simples escapadas, seguro para `. arquivo` no sh."""
    if "\n" in valor:
        raise ValueError(f"{nome}: valor com quebra de linha não é suportado")
    return f"{nome}='" + valor.replace("'", "'\\''") + "'\n"


def entregar(ler_segredo, env: dict[str, str], pasta: Path = PASTA) -> None:
    """ler_segredo(nome_no_cofre) -> valor, ou None se não existir no cofre."""
    lidos: dict[str, str | None] = {}
    arquivos = {}
    for grupo, variaveis in grupos(env).items():
        valores = {}
        for item in variaveis:
            variavel = item.rstrip("?")
            nome = nome_no_cofre(variavel)
            if nome not in lidos:
                lidos[nome] = ler_segredo(nome)
            if lidos[nome] is not None:
                valores[variavel] = lidos[nome]
            elif not item.endswith("?"):
                raise LookupError(f"segredo '{nome}' (grupo {grupo}) não existe no cofre")
        arquivos[grupo] = "".join(linha_env(k, v) for k, v in valores.items())
    # Só grava depois de ler tudo: ou todos os grupos são atualizados, ou nenhum.
    for grupo, conteudo in arquivos.items():
        destino = pasta / grupo
        destino.mkdir(parents=True, exist_ok=True)
        temp = destino / ".segredos.env.tmp"
        temp.write_text(conteudo, encoding="utf-8")
        temp.chmod(0o644)  # volume exclusivo do consumidor; o usuário dele nem sempre é root
        temp.replace(destino / "segredos.env")
        log.info("grupo %s: %d segredo(s)", grupo, conteudo.count("\n"))


def _leitor_do_cofre():
    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import ClientSecretCredential
    from azure.keyvault.secrets import SecretClient

    cliente = SecretClient(
        vault_url=os.environ["KEYVAULT_URL"],
        credential=ClientSecretCredential(
            os.environ["AZURE_TENANT_ID"], os.environ["AZURE_CLIENT_ID"], os.environ["AZURE_CLIENT_SECRET"]
        ),
    )

    def ler(nome: str) -> str | None:
        try:
            return cliente.get_secret(nome).value
        except ResourceNotFoundError:
            return None

    return ler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # PID 1 não termina com SIGTERM sem handler
    if not grupos(dict(os.environ)):
        raise SystemExit("nenhum grupo SEGREDOS_<GRUPO> definido")
    ler = _leitor_do_cofre()
    entregar(ler, dict(os.environ))  # falha aqui = contêiner cai e o Docker tenta de novo
    PRONTO.touch()
    intervalo = int(os.getenv("ATUALIZAR_A_CADA", "3600"))
    while True:
        time.sleep(intervalo)
        try:
            entregar(ler, dict(os.environ))
        except Exception as erro:  # noqa: BLE001 - mantém os arquivos atuais
            log.error("releitura do cofre falhou, mantendo os segredos atuais: %s", erro)


if __name__ == "__main__":
    main()
