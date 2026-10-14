# meter-sim: medidores inteligentes. Contexto de build = ot/energia (precisa do
# lookup de UC, que e a verdade compartilhada OT<->TI).
FROM python:3.12-slim
RUN pip install --no-cache-dir "paho-mqtt>=1.6,<2.0"
WORKDIR /app
COPY meter-sim/model.py meter-sim/server.py /app/
COPY lookups/obs_lab_uc.csv /lookups/obs_lab_uc.csv
EXPOSE 8080
CMD ["python", "-u", "server.py"]
