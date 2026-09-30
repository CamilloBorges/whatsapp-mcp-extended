import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from segredos import entregar, grupos, linha_env, nome_no_cofre  # noqa: E402

COFRE = {"postgres-password": "p0", "mcp-db-password": "m'1 $x", "entra-tenant-id": "t"}


def leitor(chamadas):
    def ler(nome):
        chamadas.append(nome)
        return COFRE.get(nome)
    return ler


def test_nome_no_cofre():
    assert nome_no_cofre("POSTGRES_PASSWORD") == "postgres-password"


def test_grupos_ignora_outras_variaveis():
    env = {"SEGREDOS_MCP": "MCP_DB_PASSWORD", "SEGREDOS_API": " A  B? ", "PATH": "x"}
    assert grupos(env) == {"mcp": ["MCP_DB_PASSWORD"], "api": ["A", "B?"]}


def test_entrega_por_grupo_lendo_cada_segredo_uma_vez(tmp_path):
    chamadas = []
    env = {
        "SEGREDOS_POSTGRES": "POSTGRES_PASSWORD MCP_DB_PASSWORD",
        "SEGREDOS_MCP": "MCP_DB_PASSWORD ENTRA_GROUP_ID?",
    }
    entregar(leitor(chamadas), env, tmp_path)
    assert (tmp_path / "postgres" / "segredos.env").read_text().count("\n") == 2
    mcp = (tmp_path / "mcp" / "segredos.env").read_text()
    assert mcp.startswith("MCP_DB_PASSWORD=") and "ENTRA_GROUP_ID" not in mcp
    assert chamadas.count("mcp-db-password") == 1


def test_obrigatorio_ausente_nao_grava_nada(tmp_path):
    env = {"SEGREDOS_A": "POSTGRES_PASSWORD", "SEGREDOS_B": "NAO_EXISTE"}
    with pytest.raises(LookupError, match="nao-existe"):
        entregar(leitor([]), env, tmp_path)
    assert not list(tmp_path.iterdir())


def test_quebra_de_linha_rejeitada():
    with pytest.raises(ValueError):
        linha_env("X", "a\nb")


@pytest.mark.skipif(not shutil.which("sh"), reason="sem sh")
def test_arquivo_carregado_pelo_sh_preserva_o_valor(tmp_path):
    arquivo = tmp_path / "s.env"
    valor = "a'b\"c $HOME `x` \\ ;d"
    arquivo.write_text(linha_env("X", valor))
    saida = subprocess.run(
        ["sh", "-c", f'set -a; . "{arquivo.as_posix()}"; printf %s "$X"'],
        capture_output=True, text=True, check=True,
    ).stdout
    assert saida == valor
