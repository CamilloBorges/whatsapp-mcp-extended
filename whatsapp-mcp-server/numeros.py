"""Números (contas) do WhatsApp em vagas pré-criadas (fork-local).

Cada vaga é uma ponte que já está no ar (ver docker-compose.easypanel.yml); a reserva fica parada,
sem número, até alguém pedir o código de pareamento. Nada de QR: o código de 8 caracteres é
digitado no próprio celular do número ("Conectar com número de telefone").

- Uma vaga fica presa à primeira conta que a ocupa: o banco da ponte guarda as mensagens dela, e
  entregá-la a outra pessoa exigiria limpar o volume (feito à mão, pelo Camillo).
- Só o dono (quem adicionou) vê, reconecta ou remove a conta.

Variáveis: ESPELHO_VAGAS ({"1": "http://whatsapp-bridge:8080", "2": "http://whatsapp-bridge-2:8080"})
e API_KEY (a mesma chave interna das pontes).
"""

import json
import os
import re
from typing import Any

import requests

from espelho import ErroEspelho, _ler, conectar, conta_de, contas_de, guardar_transcricao, nome_de_conta

INSTRUCOES = [
    "No celular desse número, abra o WhatsApp.",
    "Toque nos três pontinhos (Android) ou em Configurações (iPhone) e depois em 'Aparelhos conectados'.",
    "Toque em 'Conectar aparelho' e, na tela da câmera, em 'Conectar com número de telefone'.",
    "Digite o código acima. Ele vale por cerca de 2 minutos; se vencer, peça outro com adicionar_numero.",
    "Depois confira com status_do_numero: o histórico chega aos poucos nos minutos seguintes.",
]


def vagas() -> dict[int, str]:
    return {int(v): url.rstrip("/") for v, url in json.loads(os.getenv("ESPELHO_VAGAS") or "{}").items()}


def _ponte(vaga: int, metodo: str, caminho: str, corpo: dict | None = None, timeout: int = 60) -> dict[str, Any]:
    url = vagas().get(vaga)
    if not url:
        raise ErroEspelho(f"a vaga {vaga} não está configurada neste servidor")
    chave = os.getenv("API_KEY") or os.getenv("WHATSAPP_API_KEY")
    try:
        r = requests.request(
            metodo, f"{url}{caminho}", json=corpo, headers={"X-API-Key": chave} if chave else {}, timeout=timeout
        )
        dados = r.json()
    except (requests.RequestException, ValueError) as e:
        raise ErroEspelho(f"a ponte da vaga {vaga} não respondeu: {e}") from e
    if r.status_code >= 400 or dados.get("success") is False:
        raise ErroEspelho(f"a ponte da vaga {vaga} recusou: {dados.get('error') or r.status_code}")
    return dados


def _telefone(telefone: str) -> str:
    digitos = re.sub(r"\D", "", telefone or "")
    if not 10 <= len(digitos) <= 15:
        raise ErroEspelho("telefone inválido: use o número completo com DDI e DDD, ex.: +55 51 99999-9999")
    return digitos


def adicionar(pessoa: str | None, telefone: str, conta: str) -> dict[str, Any]:
    if not pessoa:
        raise ErroEspelho("não foi possível identificar quem chama (entre pelo Hub da Bomgado)")
    digitos, conta = _telefone(telefone), nome_de_conta(conta)
    with conectar(autocommit=True) as con, con.transaction():
        con.execute("LOCK TABLE public.contas IN SHARE ROW EXCLUSIVE MODE")  # duas pessoas, uma vaga
        linha = con.execute("SELECT vaga, dono FROM public.contas WHERE conta = %s", (conta,)).fetchone()
        if linha:
            vaga, dono = linha
            if dono != pessoa:
                raise ErroEspelho(f"o nome de conta {conta!r} já é usado por outra pessoa: escolha outro")
            if vaga is None:
                raise ErroEspelho(f"a conta {conta!r} não tem mais vaga: fale com o Camillo")
            con.execute(
                "UPDATE public.contas SET telefone = %s, situacao = 'pareando', atualizada_em = now() WHERE conta = %s",
                (digitos, conta),
            )
        else:
            ocupadas = {v for (v,) in con.execute("SELECT vaga FROM public.contas WHERE vaga IS NOT NULL")}
            livres = sorted(set(vagas()) - ocupadas)
            if not livres:
                raise ErroEspelho("não há vaga livre para um número novo: fale com o Camillo")
            vaga = livres[0]
            con.execute(
                "INSERT INTO public.contas (conta, vaga, telefone, dono) VALUES (%s, %s, %s, %s)",
                (conta, vaga, digitos, pessoa),
            )
    situacao = _ponte(vaga, "GET", "/api/connection")
    if situacao.get("linked"):
        raise ErroEspelho(f"a conta {conta!r} já está conectada (use status_do_numero)")
    codigo = _ponte(vaga, "POST", "/api/pair", {"phone_number": digitos})
    bruto = (codigo.get("code") or "").replace("-", "")
    return {
        "conta": conta,
        "vaga": vaga,
        "codigo": f"{bruto[:4]}-{bruto[4:]}" if len(bruto) == 8 else codigo.get("code"),
        "expira_em_segundos": codigo.get("expires_in"),
        "instrucoes": INSTRUCOES,
    }


def status(pessoa: str | None, conta: str | None = None) -> dict[str, Any]:
    minhas = contas_de(pessoa)
    if conta is not None:
        alvo = conta_de(pessoa, conta)
        minhas = [c for c in minhas if c["conta"] == alvo]
    resultado = []
    for c in minhas:
        item = dict(c)
        if c["vaga"] is not None and c["situacao"] != "removida":
            try:
                ponte = _ponte(c["vaga"], "GET", "/api/connection")
                item |= {"conectada": ponte.get("connected"), "pareada": ponte.get("linked")}
                if not ponte.get("linked"):
                    pareamento = _ponte(c["vaga"], "GET", "/api/pairing")
                    item["codigo_pendente"] = pareamento.get("in_progress") and pareamento.get("expires_in", 0) > 0
                elif c["situacao"] != "ativa":
                    with conectar(autocommit=True) as con:
                        con.execute(
                            "UPDATE public.contas SET situacao = 'ativa', atualizada_em = now() WHERE conta = %s",
                            (c["conta"],),
                        )
                    item["situacao"] = "ativa"
            except ErroEspelho as e:
                item["erro_ponte"] = str(e)
        try:
            _, linhas, _ = _ler(c["conta"], "SELECT count(*), max(momento) FROM mensagens")
            item["mensagens_no_espelho"], ultima = linhas[0]
            item["ultima_mensagem"] = ultima.strftime("%d/%m/%Y %H:%M") if ultima else None
        except ErroEspelho as e:
            item["espelho"] = str(e)
        resultado.append(item)
    livres = sorted(set(vagas()) - _vagas_ocupadas())
    return {"contas": resultado, "vagas_livres": len(livres)}


def _vagas_ocupadas() -> set[int]:
    with conectar() as con:
        return {v for (v,) in con.execute("SELECT vaga FROM public.contas WHERE vaga IS NOT NULL")}


def _arquivo_local(vaga: int, caminho_na_ponte: str) -> str:
    """O caminho que a ponte devolve ("store/media/..." ou "/app/whatsapp-bridge/store/media/...")
    no volume dela montado aqui (ESPELHO_STORES: {"1": "/app/store", "2": "/app/store2"})."""
    raiz = json.loads(os.getenv("ESPELHO_STORES") or "{}").get(str(vaga))
    if not raiz:
        raise ErroEspelho(f"o volume da vaga {vaga} não está montado neste servidor (ESPELHO_STORES)")
    partes = caminho_na_ponte.replace("\\", "/").split("/")
    if "store" not in partes:
        raise ErroEspelho(f"caminho inesperado da ponte: {caminho_na_ponte!r}")
    relativo = partes[partes.index("store") + 1 :]
    if ".." in relativo:
        raise ErroEspelho("caminho inválido")
    return os.path.join(raiz, *relativo)


def midia(pessoa: str | None, chat_jid: str, message_id: str, conta: str | None = None, transcrever=None) -> dict:
    """Busca a mídia de uma mensagem na ponte da conta de quem chama (se o servidor do WhatsApp já
    apagou, a ponte pede ao celular que reenvie). Áudio: transcreve e guarda no espelho."""
    conta = conta_de(pessoa, conta)
    vaga = next(c["vaga"] for c in contas_de(pessoa) if c["conta"] == conta)
    if vaga is None:
        raise ErroEspelho(f"a conta {conta!r} não tem mais vaga")
    _, linhas, _ = _ler(
        conta,
        """SELECT m.tipo_midia, m.arquivo, t.texto FROM mensagens m
           LEFT JOIN transcricoes t ON t.chat_jid = m.chat_jid AND t.id = m.id
           WHERE m.chat_jid = %s AND m.id = %s""",
        (chat_jid, message_id),
    )
    if not linhas:
        raise ErroEspelho("mensagem não encontrada no espelho da sua conta")
    tipo, arquivo, transcricao = linhas[0]
    if not tipo:
        raise ErroEspelho("essa mensagem não tem mídia")
    resultado: dict[str, Any] = {"conta": conta, "chat_jid": chat_jid, "message_id": message_id, "tipo": tipo}
    if arquivo:
        resultado["nome_original"] = arquivo
    # até 45 s esperando o celular reenviar uma mídia que o servidor do WhatsApp já apagou
    baixado = _ponte(vaga, "POST", "/api/download", {"message_id": message_id, "chat_jid": chat_jid}, timeout=120)
    resultado["arquivo"] = _arquivo_local(vaga, baixado.get("path") or "")
    resultado["via_celular"] = bool(baixado.get("via_celular"))
    if tipo == "audio":
        if transcricao:
            resultado["transcricao"] = transcricao
        elif transcrever:
            r = transcrever(resultado["arquivo"])
            if "text" in r:
                resultado["transcricao"] = r["text"]
                guardar_transcricao(conta, chat_jid, message_id, r["text"], r.get("language"))
            else:
                resultado["erro_transcricao"] = r.get("error", "erro desconhecido")
    return resultado


def remover(pessoa: str | None, conta: str, confirmar: bool = False) -> dict[str, Any]:
    conta = conta_de(pessoa, conta)
    linha = next(c for c in contas_de(pessoa) if c["conta"] == conta)
    if not confirmar:
        return {
            "conta": conta,
            "confirmar": "Isto desconecta o número do servidor (como 'Sair' em Aparelhos conectados). "
            "O histórico já copiado continua no espelho, só para você, e a vaga continua sua para reconectar. "
            "Para seguir, chame de novo com confirmar=true.",
        }
    if linha["vaga"] is not None:
        _ponte(linha["vaga"], "POST", "/api/logout")
    with conectar(autocommit=True) as con:
        con.execute("UPDATE public.contas SET situacao = 'removida', atualizada_em = now() WHERE conta = %s", (conta,))
    return {"conta": conta, "removida": True, "historico": "continua no espelho", "vaga": linha["vaga"]}
