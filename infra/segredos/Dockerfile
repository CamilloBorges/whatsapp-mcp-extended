# Serviço "segredos": lê o Azure Key Vault e entrega a cada serviço só o que ele usa. Ver segredos.py.
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY segredos.py .
CMD ["python", "segredos.py"]
