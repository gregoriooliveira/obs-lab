"""Transporte dos contadores de producao: le o /snapshot do process-sim, roda o
modelo e publica cada contador no MQTT. Tambem expoe um HTTP de controle pra
forcar/limpar fraude de producao num ativo (o 'chaos' comercial do OT industria).

Topicos MQTT:
  ot/industria/medidor/<ATIVO>     leitura periodica (producao, pecas, status, tamper)
  ot/industria/evento/seguranca    so quando um ativo levanta flag de tamper

O Telegraf assina ot/industria/medidor/# como metrica (ot_metrics) e
ot/industria/evento/# como evento (indice ot). Credencial do Splunk fica no
Telegraf, nunca aqui - o meter-sim so fala MQTT e HTTP interno.
"""
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import paho.mqtt.client as mqtt

from model import ParqueMedidores

SIM_URL   = os.environ.get("PROCESS_SIM_URL", "http://process-sim:8080")
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
POLL_S    = float(os.environ.get("METER_POLL_S", "5"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
TOPIC_MED = "ot/industria/medidor"
TOPIC_EVT = "ot/industria/evento"

parque = ParqueMedidores()
ultimo = {}            # ativo -> ultima leitura, pro /snapshot de controle
tamper_anterior = {}   # ativo -> bool, pra so emitir evento na BORDA


def snapshot_sim():
    with urllib.request.urlopen(SIM_URL + "/snapshot", timeout=4) as r:
        return json.loads(r.read())


def ciclo(client):
    global ultimo
    try:
        snap = snapshot_sim()
    except Exception as e:
        print(f"[meter-sim] process-sim indisponivel: {e}", flush=True)
        return
    linhas = snap.get("linhas", {})
    leituras = parque.ler(linhas, POLL_S)
    agora = ultimo = {}
    for r in leituras:
        ativo = r["ativo"]
        agora[ativo] = r
        # a producao desviada e verdade interna do lab; nao vai no fio (o contador
        # nao se denuncia). O Splunk a RECONSTROI pelo balanco linha-contadores.
        payload = {k: v for k, v in r.items()
                   if k not in ("producao_real_pph", "producao_desviada_pph")}
        payload["tamper"] = int(payload["tamper"])   # 0/1: o parser json do Telegraf ignora bool
        client.publish(f"{TOPIC_MED}/{ativo}", json.dumps(payload), qos=0)
        # evento de tamper so na transicao (liga/desliga), nao a cada leitura
        if r["tamper"] != tamper_anterior.get(ativo, False):
            tamper_anterior[ativo] = r["tamper"]
            if r["tamper"]:
                ev = {"evento": "contador_tamper", "categoria": "seguranca",
                      "ativo": ativo, "medidor": r["medidor"],
                      "conta_portal": r["conta_portal"], "linha": r["linha"],
                      "tipo": r["tipo"], "severidade": "high",
                      "msg": "flag de adulteracao no contador de producao", "valor": 1}
                client.publish(f"{TOPIC_EVT}/seguranca", json.dumps(ev), qos=0)
                print(f"[tamper] {ativo} ({r['conta_portal']})", flush=True)


def loop(client):
    while True:
        ciclo(client)
        time.sleep(POLL_S)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/health":
            self._send(200, {"status": "ok", "service": "meter-sim-industria",
                             "contadores": len(parque.medidores)})
        elif p == "/snapshot":
            self._send(200, ultimo)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/fraude":
            return self._send(404, {"error": "not found"})
        q = parse_qs(u.query)
        ativo = q.get("ativo", [""])[0]
        desvio = float(q.get("desvio", ["0.6"])[0])
        tamper = q.get("tamper", ["true"])[0] != "false"
        alvo = parque.forcar(ativo, desvio, tamper)
        self._send(200 if alvo else 400,
                   {"status": "fraude forcada" if alvo else "ativo nao encontrado",
                    "ativo": alvo, "desvio": desvio, "tamper": tamper})

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path != "/fraude":
            return self._send(404, {"error": "not found"})
        ativo = parse_qs(u.query).get("ativo", [""])[0]
        alvo = parque.limpar_forcado(ativo)
        self._send(200 if alvo else 400,
                   {"status": "fraude limpa" if alvo else "ativo nao encontrado", "ativo": alvo})


def conectar_mqtt():
    c = mqtt.Client(client_id="meter-sim-industria")
    for i in range(30):
        try:
            c.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            c.loop_start()
            print(f"[meter-sim] MQTT {MQTT_HOST}:{MQTT_PORT} ok", flush=True)
            return c
        except Exception as e:
            print(f"[meter-sim] aguardando MQTT ({i+1}/30): {e}", flush=True)
            time.sleep(2)
    raise SystemExit("MQTT indisponivel")


def main():
    client = conectar_mqtt()
    threading.Thread(target=loop, args=(client,), daemon=True).start()
    print(f"meter-sim industria | {len(parque.medidores)} contadores | "
          f"sim={SIM_URL} poll={POLL_S}s | http :{HTTP_PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
