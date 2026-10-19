"""Quem chama o MCP do WhatsApp (fork-local, vagas de número).

Dois caminhos, os dois com o JWT do Cloudflare Access (`Cf-Access-Jwt-Assertion`), conferido com as
chaves públicas da equipe (assinatura, emissor, validade e `aud`):
- pelo Hub da Bomgado: o Hub repassa o JWT do login da pessoa (aud = ACESSO_AUD_HUB); o e-mail
  vira a pessoa pela tabela ACESSO_PESSOAS ({"email": "Nome"}), a mesma do Hub;
- direto em whatsapp-mcp.bomgado.net (aud = ACESSO_AUD_DIRETO): o token de serviço do Camillo,
  tratado como ACESSO_DONO_DIRETO (padrão "Camillo"). Sai quando o acesso direto for desligado.
Um cabeçalho sem JWT válido não identifica ninguém: nada de confiar em cabeçalho simples.
"""

import json
import os

import jwt


class Identidade:
    def __init__(
        self, equipe: str, aud_hub: str, aud_direto: str, pessoas: dict[str, str], dono_direto: str, jwks=None
    ):
        self.emissor = f"https://{equipe}"
        self.aud_hub = aud_hub
        self.aud_direto = aud_direto
        self.pessoas = {email.lower(): nome for email, nome in pessoas.items()}
        self.dono_direto = dono_direto
        self._jwks = jwks

    @classmethod
    def do_ambiente(cls) -> "Identidade":
        return cls(
            os.getenv("ACESSO_EQUIPE", "bomgado.cloudflareaccess.com"),
            os.getenv("ACESSO_AUD_HUB", ""),
            os.getenv("ACESSO_AUD_DIRETO", ""),
            json.loads(os.getenv("ACESSO_PESSOAS") or "{}"),
            os.getenv("ACESSO_DONO_DIRETO", "Camillo"),
        )

    @property
    def jwks(self):
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(f"{self.emissor}/cdn-cgi/access/certs", cache_keys=True)
        return self._jwks

    def pessoa(self, cabecalhos) -> str | None:
        token = (cabecalhos or {}).get("cf-access-jwt-assertion")
        auds = [a for a in (self.aud_hub, self.aud_direto) if a]
        if not token or not auds:
            return None
        try:
            chave = self.jwks.get_signing_key_from_jwt(token).key
            dados = jwt.decode(token, chave, algorithms=["RS256"], audience=auds, issuer=self.emissor)
        except jwt.PyJWTError:
            return None
        aud = dados.get("aud")
        aud = aud if isinstance(aud, list) else [aud]
        if self.aud_hub and self.aud_hub in aud:
            return self.pessoas.get((dados.get("email") or "").lower())
        if self.aud_direto and self.aud_direto in aud:
            return self.dono_direto
        return None
