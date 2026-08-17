FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kwim_api ./kwim_api
COPY with-secrets.sh /app/with-secrets.sh
RUN chmod +x /app/with-secrets.sh

ENV PYTHONUNBUFFERED=1

# KWIM service listens on 8000; backend URLs/secrets come from env (built from
# mounted secret files by the Deployment's start command).
EXPOSE 8000
CMD ["uvicorn", "kwim_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
