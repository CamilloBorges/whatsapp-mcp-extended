"""Grava no Azure Key Vault as variáveis de um arquivo .env (carga inicial e trocas de senha).

Roda na máquina do Camillo, com login interativo (navegador) do próprio usuário, que precisa do
papel "Administrador do Cofre de Chaves" ou "Responsável pelos Segredos do Cofre de Chaves".
O IP da máquina precisa estar liberado no firewall do cofre. Nunca imprime valores.

    python infra/segredos/carregar_cofre.py <arquivo .env>

Nome no cofre = variável em minúsculas com hífen (POSTGRES_PASSWORD -> postgres-password).
Valores vazios são ignorados; valores iguais aos do cofre não geram versão nova.
"""
import argparse
from pathlib import Path

from segredos import nome_no_cofre

COFRE = "https://kv-bomgado-mcp.vault.azure.net/"
TENANT = "e97132bc-f926-4fba-9f6e-0379c8f25b23"
# Variáveis que não vão para o cofre: a credencial do próprio cofre e sobras antigas.
IGNORAR = {"AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "KEYVAULT_URL", "AIRFLOW_USERS"}


def ler_env(caminho: Path) -> dict[str, str]:
    valores = {}
    for linha in caminho.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        nome, valor = linha.split("=", 1)
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "'\"":
            valor = valor[1:-1]
        valores[nome.strip()] = valor
    return valores


def main() -> None:
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("env", type=Path, help="arquivo .env com as variáveis")
    args.add_argument("--cofre", default=COFRE)
    args.add_argument("--tenant", default=TENANT)
    args.add_argument("--simular", action="store_true", help="só lista o que faria, sem login")
    opcoes = args.parse_args()

    variaveis = {k: v for k, v in ler_env(opcoes.env).items() if k not in IGNORAR}
    if opcoes.simular:
        for nome, valor in variaveis.items():
            print(f"{nome_no_cofre(nome):28} {'(vazio, ignorado)' if not valor else ''}")
        return

    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import InteractiveBrowserCredential
    from azure.keyvault.secrets import SecretClient

    cliente = SecretClient(opcoes.cofre, InteractiveBrowserCredential(tenant_id=opcoes.tenant))
    for nome, valor in variaveis.items():
        segredo = nome_no_cofre(nome)
        if not valor:
            print(f"{segredo:28} vazio, ignorado")
            continue
        try:
            atual = cliente.get_secret(segredo).value
        except ResourceNotFoundError:
            atual = None
        if atual == valor:
            print(f"{segredo:28} igual, sem mudança")
            continue
        cliente.set_secret(segredo, valor, content_type="plataforma-dados")
        print(f"{segredo:28} {'criado' if atual is None else 'atualizado'}")


if __name__ == "__main__":
    main()
