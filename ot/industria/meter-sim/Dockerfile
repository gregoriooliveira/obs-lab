# meter-sim: contadores de producao. Contexto de build = ot/industria (precisa do
# lookup de ativos, a verdade compartilhada OT<->TI).
FROM python:3.12-slim
RUN pip install --no-cache-dir "paho-mqtt>=1.6,<2.0"
WORKDIR /app
COPY meter-sim/model.py meter-sim/server.py /app/
COPY lookups/obs_lab_ativo_industria.csv /lookups/obs_lab_ativo_industria.csv
EXPOSE 8080
CMD ["python", "-u", "server.py"]
