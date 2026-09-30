"""Clientes externos da fila: API da ponte (download), Cloudflare R2 (S3) e Whisper (transcrição).

Variáveis:
  PONTE_URL (padrão http://whatsapp-bridge:8080), WHATSAPP_API_KEY
  R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
  WHISPER_URL, WHISPER_CF_CLIENT_ID, WHISPER_CF_CLIENT_SECRET (token do Cloudflare Access), WHISPER_LANGUAGE
"""
import os
from pathlib import Path

import boto3
import httpx
from botocore.config import Config


def baixar_da_ponte(chat_jid: str, msg_id: str) -> str:
    """Pede à ponte que baixe e decifre a mídia; devolve o caminho relativo (store/media/...)."""
    url = os.environ.get("PONTE_URL", "http://whatsapp-bridge:8080") + "/api/download"
    r = httpx.post(url, json={"chat_jid": chat_jid, "message_id": msg_id},
                   headers={"X-API-Key": os.environ["WHATSAPP_API_KEY"]}, timeout=120)
    corpo = r.json()
    if r.status_code != 200 or not corpo.get("success"):
        raise RuntimeError(f"ponte: {r.status_code} {corpo.get('message') or corpo}")
    return corpo["path"]


def criar_envio_r2():
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        # boto3 >= 1.36 manda somas de verificação novas por padrão; serviços compatíveis com S3
        # (R2, MinIO) nem sempre aceitam. Só quando a operação exige.
        config=Config(request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"),
    )
    bucket = os.environ["R2_BUCKET"]

    def enviar(chave: str, dados: bytes) -> None:
        s3.put_object(Bucket=bucket, Key=chave, Body=dados)

    return enviar


def transcrever(caminho: Path) -> tuple[str, str | None]:
    headers = {}
    if os.environ.get("WHISPER_CF_CLIENT_ID"):
        headers = {
            "CF-Access-Client-Id": os.environ["WHISPER_CF_CLIENT_ID"],
            "CF-Access-Client-Secret": os.environ["WHISPER_CF_CLIENT_SECRET"],
        }
    with caminho.open("rb") as f:
        r = httpx.post(os.environ["WHISPER_URL"], headers=headers,
                       data={"language": os.environ.get("WHISPER_LANGUAGE", "pt")},
                       files={"file": (caminho.name, f)}, timeout=600)
    r.raise_for_status()
    corpo = r.json()
    return (corpo.get("text") or "").strip(), corpo.get("language")
