"""Consultas ao espelho do WhatsApp (Postgres), sem passar pela ponte (fork-local, fase 3).

O espelho tem um schema por conta (número) do WhatsApp: `conta_<nome>`, com chats, mensagens,
transcrições dos áudios e mídias (ver espelho/principal.py). A tabela public.contas diz o dono de
cada conta: **só o dono usa a conta** (quem chama vem de identidade.py). Tudo aqui é só leitura:
cada consulta roda numa transação READ ONLY, com tempo máximo, no fuso de Porto Alegre e com o papel
leitor_<conta>, que não enxerga o schema de outra conta nem a tabela de contas.

Variável: ESPELHO_DB_URL (sem ela, as ferramentas avisam que o espelho não está configurado).
"""

import os
import re
from datetime import date, datetime, timedelta
from typing import Any

FUSO = "America/Sao_Paulo"
CLASSIFICACOES = ("trabalho", "pessoal", "ruido", "a_classificar")
CLASSIFICACOES_PADRAO = ("trabalho", "pessoal", "a_classificar")
MAX_CARACTERES = 60_000
MAX_LINHAS_SQL = 500
TEMPO_MAXIMO = "20s"

_NOME_CONTA = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
_SO_LEITURA = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
# Funções que leem o servidor ou mudam a sessão: fora do consultar_espelho, mesmo em SELECT.
_PROIBIDO = re.compile(
    r"\b(pg_read\w*|pg_ls\w*|pg_stat_file|lo_\w+|dblink\w*|set_config|pg_sleep\w*|pg_terminate\w*|"
    r"pg_cancel\w*|pg_reload\w*|query_to_xml\w*|copy)\b",
    re.IGNORECASE,
)
_MIDIA = {"audio": "áudio", "image": "foto", "video": "vídeo", "document": "documento", "call": "ligação"}


class ErroEspelho(Exception):
    pass


def nome_de_conta(conta: str) -> str:
    conta = (conta or "").strip().lower()
    if not _NOME_CONTA.match(conta):
        raise ErroEspelho(f"conta inválida: {conta!r} (letras minúsculas, números e _, começando por letra)")
    return conta


def conectar(**opcoes: Any):
    url = os.getenv("ESPELHO_DB_URL")
    if not url:
        raise ErroEspelho("o espelho não está configurado neste servidor (ESPELHO_DB_URL)")
    import psycopg

    return psycopg.connect(url, **opcoes)


def contas_de(pessoa: str | None) -> list[dict[str, Any]]:
    """As contas da pessoa (public.contas), a primeira vaga primeiro."""
    if not pessoa:
        raise ErroEspelho("não foi possível identificar quem chama (entre pelo Hub da Bomgado)")
    with conectar() as con:
        cur = con.execute(
            "SELECT conta, vaga, telefone, situacao FROM public.contas WHERE dono = %s ORDER BY vaga NULLS LAST, conta",
            (pessoa,),
        )
        return [dict(zip(("conta", "vaga", "telefone", "situacao"), linha, strict=True)) for linha in cur.fetchall()]


def conta_de(pessoa: str | None, conta: str | None = None) -> str:
    """A conta pedida, se for da pessoa; sem conta, a primeira dela."""
    minhas = [c["conta"] for c in contas_de(pessoa)]
    if not minhas:
        raise ErroEspelho("você ainda não tem número de WhatsApp no espelho: use adicionar_numero")
    if conta is None:
        return minhas[0]
    conta = nome_de_conta(conta)
    if conta not in minhas:
        raise ErroEspelho(f"a conta {conta!r} não é sua (as suas: {', '.join(minhas)})")
    return conta


def _ler(
    conta: str, sql: str, parametros: Any = None, limite: int | None = None
) -> tuple[list[str], list[tuple], bool]:
    """Roda uma consulta só de leitura no schema da conta, com o papel leitor_<conta>.
    Devolve (colunas, linhas, truncado)."""
    conta = nome_de_conta(conta)
    with conectar() as con:
        with con.transaction():
            con.execute("SET TRANSACTION READ ONLY")
            if not con.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (f"leitor_{conta}",)).fetchone():
                raise ErroEspelho(f"a conta {conta!r} ainda não está pronta no espelho (o worker cria em até 1 minuto)")
            con.execute(f"SET LOCAL statement_timeout = '{TEMPO_MAXIMO}'")
            con.execute(f"SET LOCAL TimeZone = '{FUSO}'")
            con.execute(f'SET LOCAL search_path = "conta_{conta}"')
            con.execute(f'SET LOCAL ROLE "leitor_{conta}"')
            cur = con.execute(sql, parametros)
            colunas = [c.name for c in cur.description or []]
            if limite is None:
                return colunas, cur.fetchall(), False
            linhas = cur.fetchmany(limite + 1)
            return colunas, linhas[:limite], len(linhas) > limite


def _data(texto: str) -> tuple[datetime, bool]:
    """('2026-10-19' → meia-noite, True) ou ('2026-10-19T14:00' → 14h, False). Hora local do espelho."""
    texto = texto.strip()
    try:
        if len(texto) == 10:
            return datetime.combine(date.fromisoformat(texto), datetime.min.time()), True
        return datetime.fromisoformat(texto).replace(tzinfo=None), False
    except ValueError as e:
        raise ErroEspelho(f"data inválida: {texto!r} (use AAAA-MM-DD ou AAAA-MM-DDTHH:MM)") from e


def _periodo(inicio: str, fim: str | None) -> tuple[datetime, datetime]:
    """Período [a, b): um dia sozinho é o dia inteiro, e um fim só com a data inclui esse dia."""
    a, _ = _data(inicio)
    if not fim:
        return a, a + timedelta(days=1)
    b, so_data = _data(fim)
    return a, b + timedelta(days=1) if so_data else b


def _linha(
    momento: datetime,
    quem: str,
    texto: str | None,
    midia: str | None,
    arquivo: str | None,
    transcricao: str | None,
    com_data: bool = False,
) -> str:
    hora = momento.strftime("%d/%m %H:%M" if com_data else "%H:%M")
    partes = []
    if midia:
        rotulo = _MIDIA.get(midia, midia)
        if midia == "document" and arquivo:
            rotulo = f"documento: {arquivo}"
        partes.append(f"[{rotulo}]")
    if transcricao:
        partes.append(transcricao.strip())
    if texto:
        partes.append(texto.strip())
    return f"{hora} {quem}: {' '.join(partes) or '(vazia)'}"


def _quem(de_mim: bool, remetente: str | None, chat: str, tipo: str) -> str:
    if de_mim:
        return "Eu"
    if remetente:
        return remetente
    return chat if tipo == "contato" else "?"


def resumo_do_periodo(
    pessoa: str | None,
    inicio: str,
    fim: str | None = None,
    conta: str | None = None,
    classificacoes: list[str] | None = None,
    chat: str | None = None,
    max_caracteres: int = MAX_CARACTERES,
) -> dict[str, Any]:
    conta = conta_de(pessoa, conta)
    a, b = _periodo(inicio, fim)
    escolhidas = list(classificacoes or CLASSIFICACOES_PADRAO)
    invalidas = set(escolhidas) - set(CLASSIFICACOES)
    if invalidas:
        raise ErroEspelho(f"classificação inválida: {', '.join(sorted(invalidas))} (use {', '.join(CLASSIFICACOES)})")
    filtro_chat, params = "", {"a": a, "b": b, "classes": escolhidas}
    if chat:
        filtro_chat = "AND (c.jid = %(chat)s OR c.nome ILIKE %(chat_like)s)"
        params |= {"chat": chat, "chat_like": f"%{chat}%"}
    _, linhas, _ = _ler(
        conta,
        f"""
        SELECT c.jid, coalesce(nullif(trim(c.nome), ''), c.jid), c.tipo, c.classificacao,
               m.momento, m.enviada_por_mim, coalesce(nullif(m.remetente_nome, ''), m.remetente),
               m.texto, m.tipo_midia, m.arquivo, t.texto, m.id
        FROM mensagens m
        JOIN chats c ON c.jid = m.chat_jid
        LEFT JOIN transcricoes t ON t.chat_jid = m.chat_jid AND t.id = m.id
        WHERE m.momento >= %(a)s AND m.momento < %(b)s AND NOT m.sistema
          AND c.classificacao = ANY(%(classes)s) {filtro_chat}
        ORDER BY m.momento""",
        params,
    )

    varios_dias = b - a > timedelta(days=1)
    chats: dict[str, dict[str, Any]] = {}
    for jid, nome, tipo, classe, momento, de_mim, remetente, texto, midia, arquivo, transcricao, mid in linhas:
        if not texto and not midia and not transcricao:
            continue
        item = chats.setdefault(
            jid,
            {
                "chat": nome,
                "chat_jid": jid,
                "tipo": tipo,
                "classificacao": classe,
                "mensagens": 0,
                "audios_transcritos": 0,
                "linhas": [],
            },
        )
        item["mensagens"] += 1
        item["audios_transcritos"] += 1 if transcricao else 0
        if midia == "audio" and not transcricao:
            # áudio ainda sem transcrição: midia_da_mensagem(chat_jid, message_id) busca e transcreve
            item.setdefault("audios_sem_transcricao", []).append(
                {"message_id": mid, "hora": momento.strftime("%d/%m %H:%M")}
            )
        item["linhas"].append(
            _linha(momento, _quem(de_mim, remetente, nome, tipo), texto, midia, arquivo, transcricao, varios_dias)
        )

    # Os chats com mais mensagens primeiro; o que passar do limite de tamanho sai só com a contagem.
    ordenados = sorted(chats.values(), key=lambda c: -c["mensagens"])
    usados, truncado, resultado, so_contagem = 0, False, [], []
    for item in ordenados:
        tamanho = sum(len(linha) + 1 for linha in item["linhas"])
        if usados + tamanho > max_caracteres:
            truncado = True
            so_contagem.append({"chat": item["chat"], "mensagens": item["mensagens"]})
            continue
        usados += tamanho
        resultado.append(item)
    sem_audio = sum(1 for linha in linhas if linha[8] == "audio" and not linha[10])
    return {
        "conta": conta,
        "periodo": {"inicio": a.isoformat(timespec="minutes"), "fim_exclusivo": b.isoformat(timespec="minutes")},
        "classificacoes": escolhidas,
        "total_mensagens": sum(c["mensagens"] for c in chats.values()),
        "audios_sem_transcricao": sem_audio,
        "chats": resultado,
        "chats_sem_detalhe": so_contagem,
        "truncado": truncado,
        "dica": "Se truncado, peça um chat por vez (parâmetro chat) ou um período menor." if truncado else None,
    }


def buscar(
    pessoa: str | None,
    texto: str,
    conta: str | None = None,
    desde: str | None = None,
    ate: str | None = None,
    chat: str | None = None,
    limite: int = 20,
    contexto: int = 2,
) -> dict[str, Any]:
    conta = conta_de(pessoa, conta)
    if not texto or not texto.strip():
        raise ErroEspelho("informe o que buscar")
    limite, contexto = max(1, min(limite, 50)), max(0, min(contexto, 10))
    filtros, params = [], {"q": texto, "limite": limite}
    if desde:
        filtros.append("m.momento >= %(desde)s")
        params["desde"] = _data(desde)[0]
    if ate:
        filtros.append("m.momento < %(ate)s")
        params["ate"] = _periodo(ate, ate)[1]  # "até 2026-10-19" inclui o dia 19
    if chat:
        filtros.append("(c.jid = %(chat)s OR c.nome ILIKE %(chat_like)s)")
        params |= {"chat": chat, "chat_like": f"%{chat}%"}
    onde = "".join(f" AND {f}" for f in filtros)
    _, achados, mais = _ler(
        conta,
        f"""
        SELECT m.chat_jid, m.id, m.momento, coalesce(nullif(trim(c.nome), ''), c.jid), c.tipo
        FROM mensagens m
        JOIN chats c ON c.jid = m.chat_jid AND c.classificacao <> 'ignorar'
        LEFT JOIN transcricoes t ON t.chat_jid = m.chat_jid AND t.id = m.id,
             websearch_to_tsquery('portuguese', %(q)s) q
        WHERE (m.busca @@ q OR t.busca @@ q){onde}
        ORDER BY m.momento DESC
        LIMIT %(limite)s""",
        params,
        limite=limite,
    )

    resultados = []
    for chat_jid, mid, momento, nome, tipo in achados:
        _, vizinhas, _ = _ler(
            conta,
            """
            (SELECT m.id, m.momento, m.enviada_por_mim, coalesce(nullif(m.remetente_nome, ''), m.remetente),
                    m.texto, m.tipo_midia, m.arquivo, t.texto
             FROM mensagens m LEFT JOIN transcricoes t ON t.chat_jid = m.chat_jid AND t.id = m.id
             WHERE m.chat_jid = %(c)s AND m.momento <= %(t)s AND NOT m.sistema
             ORDER BY m.momento DESC LIMIT %(antes)s)
            UNION ALL
            (SELECT m.id, m.momento, m.enviada_por_mim, coalesce(nullif(m.remetente_nome, ''), m.remetente),
                    m.texto, m.tipo_midia, m.arquivo, t.texto
             FROM mensagens m LEFT JOIN transcricoes t ON t.chat_jid = m.chat_jid AND t.id = m.id
             WHERE m.chat_jid = %(c)s AND m.momento > %(t)s AND NOT m.sistema
             ORDER BY m.momento LIMIT %(depois)s)
            ORDER BY 2""",
            {"c": chat_jid, "t": momento, "antes": contexto + 1, "depois": contexto},
        )
        conversa = []
        for vid, vmomento, de_mim, remetente, vtexto, midia, arquivo, transcricao in vizinhas:
            linha = _linha(vmomento, _quem(de_mim, remetente, nome, tipo), vtexto, midia, arquivo, transcricao, True)
            conversa.append(("» " if vid == mid else "  ") + linha)
        resultados.append(
            {
                "chat": nome,
                "chat_jid": chat_jid,
                "message_id": mid,
                "momento": momento.strftime("%d/%m/%Y %H:%M"),
                "conversa": conversa,
            }
        )
    return {"conta": conta, "busca": texto, "resultados": resultados, "ha_mais": mais}


CLASSIFICAVEIS = ("trabalho", "pessoal", "ruido", "ignorar", "a_classificar")


def classificar(
    pessoa: str | None, chats: list[str], classificacao: str, conta: str | None = None, motivo: str | None = None
) -> dict[str, Any]:
    """Classifica chats da própria conta (pelo JID ou pelo nome exato, sem diferenciar maiúsculas).
    trabalho/pessoal: mídias guardadas e áudios transcritos; ruido: só o texto; ignorar: as
    mensagens novas deixam de entrar no espelho."""
    conta = conta_de(pessoa, conta)
    if classificacao not in CLASSIFICAVEIS:
        raise ErroEspelho(f"classificação inválida: {classificacao!r} (use {', '.join(CLASSIFICAVEIS)})")
    pedidos = [c.strip() for c in (chats or []) if c and c.strip()]
    if not pedidos:
        raise ErroEspelho("informe pelo menos um chat (JID ou nome exato)")
    if len(pedidos) > 200:
        raise ErroEspelho("no máximo 200 chats por vez")
    feitos, nao_achados = [], []
    with conectar() as con, con.transaction():
        con.execute(f'SET LOCAL search_path = "conta_{conta}"')
        for pedido in pedidos:
            linhas = con.execute(
                """UPDATE chats SET classificacao = %s, classificado_por = %s, motivo = coalesce(%s, motivo),
                          classificado_em = now(), atualizado_em = now()
                   WHERE jid = %s OR lower(trim(nome)) = lower(%s)
                   RETURNING coalesce(nullif(trim(nome), ''), jid)""",
                (classificacao, pessoa.lower(), motivo, pedido, pedido),
            ).fetchall()
            if linhas:
                feitos.extend(nome for (nome,) in linhas)
            else:
                nao_achados.append(pedido)
    return {
        "conta": conta,
        "classificacao": classificacao,
        "classificados": feitos,
        "nao_encontrados": nao_achados,
        "aviso": "As mídias recentes dos chats de trabalho e pessoal entram na fila em até 1 minuto."
        if classificacao in ("trabalho", "pessoal")
        else None,
    }


def guardar_transcricao(conta: str, chat_jid: str, message_id: str, texto: str, idioma: str | None) -> None:
    conta = nome_de_conta(conta)
    with conectar() as con, con.transaction():
        con.execute(f'SET LOCAL search_path = "conta_{conta}"')
        con.execute(
            """INSERT INTO transcricoes (chat_jid, id, texto, idioma) VALUES (%s, %s, %s, %s)
               ON CONFLICT (chat_jid, id) DO UPDATE SET texto = EXCLUDED.texto, idioma = EXCLUDED.idioma""",
            (chat_jid, message_id, texto, idioma),
        )


def consultar(pessoa: str | None, sql: str, conta: str | None = None, limite: int = 200) -> dict[str, Any]:
    conta = conta_de(pessoa, conta)
    texto = (sql or "").strip().rstrip(";").strip()
    if not _SO_LEITURA.match(texto):
        raise ErroEspelho("só consultas SELECT ou WITH")
    if ";" in texto:
        raise ErroEspelho("uma consulta por vez (sem ';' no meio)")
    if _PROIBIDO.search(texto):
        raise ErroEspelho("essa função não é permitida aqui")
    colunas, linhas, truncado = _ler(conta, texto, limite=max(1, min(limite, MAX_LINHAS_SQL)))
    return {
        "conta": conta,
        "colunas": colunas,
        "linhas": [
            [v if isinstance(v, (int, float, bool)) or v is None else str(v) for v in linha] for linha in linhas
        ],
        "truncado": truncado,
    }
