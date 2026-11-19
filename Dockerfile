# MVP completo (cadastro por foto + risco por CEP + disparo de alertas)
# Roda igual no Hugging Face Spaces, no Render e no Azure App Service.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-por libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY visao ./visao
COPY pipeline ./pipeline
COPY mvp ./mvp
# pasta do banco gravável também quando a plataforma roda o container sem root (Hugging Face usa uid 1000)
RUN mkdir -p /home/dados /tmp/lake && chmod 777 /home/dados /tmp/lake
# /home é o disco persistente do App Service; no Hugging Face e no Render free o banco recomeça a cada reinício
ENV CADASTRO_DB=/home/dados/cadastros.sqlite3 PIPELINE_LAKE=/tmp/lake PIPELINE_MODO=offline \
    PIPELINE_INTERVALO_MIN=60 OMP_THREAD_LIMIT=1 PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000
# 1 worker: o agendador do pipeline roda dentro do processo
CMD ["sh", "-c", "uvicorn mvp.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
