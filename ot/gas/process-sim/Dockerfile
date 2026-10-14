# Imagem custom do simulador de processo (o unico container nao-oficial do stack).
# Base oficial python slim; so o pymodbus/paho entram. Hospedar no ghcr depois.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY model.py server.py ./
# 502 = Modbus TCP (SCADA/Telegraf leem aqui); 8080 = HTTP de controle
EXPOSE 502 8080
CMD ["python", "server.py"]
