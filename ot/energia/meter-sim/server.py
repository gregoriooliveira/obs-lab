"""Transporte dos medidores: le o /snapshot do process-sim, roda o modelo e
publica cada medidor no MQTT (o que um medidor inteligente faz na vida real).
Tambem expoe um HTTP de controle pra forcar/limpar furto numa UC (o 'chaos'
comercial do OT).

Topicos MQTT:
  ot/energia/medidor/<UC>        leitura periodica (consumo, tensao, status, tamper)
  ot/energia/evento/tamper       so quando uma UC levanta flag de adulteracao

O Telegraf assina ot/energia/medidor/# como metrica (ot_metrics) e
ot/energia/evento/# como evento (indice ot). Credencial do Splunk fica no
Telegraf, nunca aqui - o meter-sim so fala MQTT e HTTP interno.
"""
import json
import os
import random
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
TOPIC_MED = "ot/energia/medidor"
TOPIC_EVT = "ot/energia/evento"

parque = ParqueMedidores()
ultimo = {}            # uc -> ultima leitura, pra o /snapshot de controle
tamper_anterior = {}   # uc -> bool, pra so emitir evento na BORDA (mudou de estado)
comms_anterior = {}    # uc -> bool (comunicando), pra emitir sem_comunicacao na borda
feeders_ant = {}       # ultimo snapshot de alimentadores, pra detectar falta/religa
_client = None         # cliente MQTT, compartilhado com o HTTP de controle


def publicar_evento(ev):
    """Publica um evento no topico da sua categoria (o Telegraf roteia pro indice ot)."""
    if _client is None:
        return
    cat = ev.get("categoria", "operacao")
    _client.publish(f"{TOPIC_EVT}/{cat}", json.dumps(ev), qos=0)


def snapshot_sim():
    with urllib.request.urlopen(SIM_URL + "/snapshot", timeout=4) as r:
        return json.loads(r.read())


def ciclo(client):
    global ultimo, feeders_ant
    try:
        snap = snapshot_sim()
    except Exception as e:
        print(f"[meter-sim] process-sim indisponivel: {e}", flush=True)
        return
    feeders = snap.get("alimentadores", {})
    # eventos de EXPERIENCIA na borda: last gasp + reclamacao no portal quando o
    # alimentador cai ou a tensao afunda. Compara com o snapshot anterior.
    if feeders_ant:
        for ev in parque.eventos_de_falta(feeders_ant, feeders):
            publicar_evento(ev)
            if ev["evento"] == "reclamacao":
                print(f"[reclamacao] {ev['uc']} ({ev['conta_portal']}) {ev['motivo']}", flush=True)
    feeders_ant = feeders
    leituras = parque.ler(feeders, POLL_S)
    agora = ultimo = {}
    for r in leituras:
        uc = r["uc"]
        agora[uc] = r
        m = parque._buscar(uc)
        comunicando = m.comunicando if m else True
        # comunicacao: evento na borda; medidor sem comunicacao nao publica metrica
        # (e assim a taxa de leitura cai - o sinal do NOC da AMI).
        if comunicando != comms_anterior.get(uc, True):
            comms_anterior[uc] = comunicando
            if not comunicando:
                publicar_evento({"evento": "medidor_sem_comunicacao",
                                 "categoria": "operacao", "severidade": "low",
                                 "uc": uc, "medidor": r["medidor"],
                                 "alimentador": r["alimentador"],
                                 "msg": "medidor parou de comunicar", "valor": 1})
        if not comunicando:
            continue
        # a perda nao-tecnica e verdade interna do lab; nao vai no fio (o medidor
        # nao se denuncia). O Splunk a RECONSTROI pelo balanco alimentador-medidor.
        payload = {k: v for k, v in r.items() if k != "perda_nao_tecnica_kw"}
        payload["tamper"] = int(payload["tamper"])   # 0/1: o parser json do Telegraf ignora bool
        client.publish(f"{TOPIC_MED}/{uc}", json.dumps(payload), qos=0)
        # evento de tamper so na transicao (liga/desliga), nao a cada leitura
        if r["tamper"] != tamper_anterior.get(uc, False):
            tamper_anterior[uc] = r["tamper"]
            if r["tamper"]:
                ev = {"evento": "medidor_tamper", "categoria": "seguranca",
                      "uc": uc, "medidor": r["medidor"],
                      "conta_portal": r["conta_portal"], "alimentador": r["alimentador"],
                      "classe": r["classe"], "severidade": "high",
                      "msg": "flag de adulteracao no medidor", "valor": 1}
                client.publish(f"{TOPIC_EVT}/tamper", json.dumps(ev), qos=0)
                print(f"[tamper] {uc} ({r['conta_portal']})", flush=True)


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
            self._send(200, {"status": "ok", "service": "meter-sim-energia",
                             "medidores": len(parque.medidores)})
        elif p == "/snapshot":
            self._send(200, ultimo)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/fraude":
            uc = q.get("uc", [""])[0]
            desvio = float(q.get("desvio", ["0.6"])[0])
            tamper = q.get("tamper", ["true"])[0] != "false"
            alvo = parque.forcar(uc, desvio, tamper)
            return self._send(200 if alvo else 400,
                       {"status": "furto forcado" if alvo else "uc nao encontrada",
                        "uc": alvo, "desvio": desvio, "tamper": tamper})
        if u.path == "/cortar":
            # marca a UC como cortada no cadastro. Se continuar consumindo, o
            # Splunk acusa religacao a revelia (situacao=cortada x potencia>0).
            uc = q.get("uc", [""])[0]
            estado = q.get("estado", ["cortada"])[0]
            janela = q.get("janela", ["dentro"])[0]
            alvo = parque.cortar(uc, estado)
            if alvo and estado == "cortada":
                m = parque._buscar(alvo)
                publicar_evento({"evento": "corte_remoto", "categoria": "operacao",
                                 "severidade": "medium", "uc": alvo,
                                 "conta_portal": m.conta_portal, "alimentador": m.alimentador,
                                 "janela": janela, "msg": f"corte remoto ({janela} de janela)",
                                 "valor": 1})
            return self._send(200 if alvo else 400,
                       {"status": "ok" if alvo else "uc nao encontrada",
                        "uc": alvo, "estado": estado})
        if u.path == "/corte_massa":
            # dispara um lote de cortes remotos (fora de janela por padrao) - o
            # cenario de HES comprometido/integracao em pane (UC-017).
            n = int(q.get("n", ["20"])[0])
            janela = q.get("janela", ["fora"])[0]
            alvos = random.sample(parque.medidores, min(n, len(parque.medidores)))
            for m in alvos:
                m.situacao = "cortada"
                publicar_evento({"evento": "corte_remoto", "categoria": "operacao",
                                 "severidade": "medium", "uc": m.uc,
                                 "conta_portal": m.conta_portal, "alimentador": m.alimentador,
                                 "janela": janela, "msg": f"corte remoto em massa ({janela} de janela)",
                                 "valor": 1})
            return self._send(200, {"status": "corte em massa", "n": len(alvos), "janela": janela})
        if u.path == "/comms":
            # liga/desliga a comunicacao de um medidor (NOC da AMI, UC-022).
            uc = q.get("uc", [""])[0]
            ok = q.get("ok", ["false"])[0] != "false"
            alvo = parque.set_comunicacao(uc, ok)
            return self._send(200 if alvo else 400,
                       {"status": "ok" if alvo else "uc nao encontrada",
                        "uc": alvo, "comunicando": ok})
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path != "/fraude":
            return self._send(404, {"error": "not found"})
        uc = parse_qs(u.query).get("uc", [""])[0]
        alvo = parque.limpar_forcado(uc)
        self._send(200 if alvo else 400,
                   {"status": "furto limpo" if alvo else "uc nao encontrada", "uc": alvo})


def conectar_mqtt():
    c = mqtt.Client(client_id="meter-sim-energia")
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
    global _client
    client = conectar_mqtt()
    _client = client
    threading.Thread(target=loop, args=(client,), daemon=True).start()
    print(f"meter-sim energia | {len(parque.medidores)} medidores | "
          f"sim={SIM_URL} poll={POLL_S}s | http :{HTTP_PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
