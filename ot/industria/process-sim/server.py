"""Servidor do process-sim industria: expoe o modelo da planta como Modbus TCP
(o que o SCADA/FUXA e o Telegraf leem) e um HTTP de controle (health, snapshot,
injecao de falha - o 'chaos' do OT). Eventos de seguranca/operacao saem por MQTT.

Mapa de registradores (holding, FLOAT32 big-endian ABCD, 2 regs cada):
  Planta, base 0:
    0  consumo_total_kw   2  producao_total_pph   4  motores_ativos
    6  temperatura_ambiente_c   8  hora_do_dia
  Linha f (0..4), base 100 + f*20:
    +0 corrente_a  +2 temperatura_c  +4 vibracao_mms  +6 rpm
    +8 producao_pph  +10 status  +12 consumo_kw
"""
import json
import os
import random
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from pymodbus.datastore import (ModbusSequentialDataBlock, ModbusServerContext,
                                ModbusSlaveContext)
from pymodbus.server import StartTcpServer

try:
    import paho.mqtt.client as mqtt       # eventos (indice ot). Opcional: sem
except Exception:                          # broker o sim roda igual, so loga.
    mqtt = None

from model import LINHAS, Planta

DAY_MIN    = float(os.environ.get("SIM_DAY_MINUTES", "20"))
UPDATE_S   = float(os.environ.get("SIM_UPDATE_S", "5"))
MODBUS_PORT = int(os.environ.get("MODBUS_PORT", "502"))
HTTP_PORT   = int(os.environ.get("HTTP_PORT", "8080"))
AUTO_FAULT  = os.environ.get("AUTO_FAULT", "true") == "true"
AUTO_PROB   = float(os.environ.get("AUTO_FAULT_PROB", "0.05"))  # por update
CYBER_PROB  = float(os.environ.get("CYBER_FAULT_PROB", "0.25")) # fracao cyber
MQTT_HOST   = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT   = int(os.environ.get("MQTT_PORT", "1883"))

sub = Planta(day_minutes=DAY_MIN)
last_snapshot = {}
LINE_IDS = [l["id"] for l in LINHAS]
FAULTS = ["superaquecimento", "vibracao_alta", "parada"]

# Classificacao do evento por tipo de falha. unauth_command e spoof sao ATAQUE
# (severidade de seguranca); os demais sao operacao normal de uma planta.
EVENTOS = {
    "unauth_command":   ("seguranca", "critical", "comando de parada nao autorizado na linha (sabotagem)"),
    "spoof":            ("seguranca", "high",     "telemetria da linha falseada (leitura congelada esconde falha)"),
    "superaquecimento": ("operacao",  "medium",   "temperatura do motor acima do limite"),
    "vibracao_alta":    ("operacao",  "medium",   "vibracao acima do limite (defeito de rolamento)"),
    "parada":           ("operacao",  "medium",   "linha parada"),
}
_mqc = None
eventos = []     # ultimos eventos, pro /events


def mqtt_conectar():
    """Conecta ao broker em background, com retentativa. Nunca bloqueia o Modbus:
    se o broker nao subir, os eventos saem so no log."""
    global _mqc
    if mqtt is None:
        print("[process-sim] paho ausente; eventos so no log", flush=True)
        return
    c = mqtt.Client(client_id="process-sim-industria")
    c.reconnect_delay_set(min_delay=1, max_delay=15)
    _mqc = c

    def _try():
        for _ in range(60):
            try:
                c.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
                c.loop_start()
                print(f"[process-sim] MQTT {MQTT_HOST}:{MQTT_PORT} ok", flush=True)
                return
            except Exception:
                time.sleep(2)
        print("[process-sim] MQTT nao conectou; eventos so no log", flush=True)

    threading.Thread(target=_try, daemon=True).start()


def emit_event(ev):
    ev["ts"] = time.time()
    eventos.append(ev)
    del eventos[:-200]
    cat = ev.get("categoria", "operacao")
    print(f"[evento:{cat}] {ev.get('linha')} {ev.get('tipo')} {ev.get('severidade')}", flush=True)
    if _mqc is not None:
        try:
            _mqc.publish(f"ot/industria/evento/{cat}", json.dumps(ev), qos=0)
        except Exception:
            pass


def do_inject(alvo, tipo, dur):
    """Aplica a falha E emite o evento classificado. Usado pelo auto-fault e pelo
    /fault, pra operacao e ataque sairem pelo mesmo caminho."""
    ok = sub.inject(alvo, tipo, dur)
    if ok:
        cat, sev, msg = EVENTOS.get(tipo, ("operacao", "low", tipo))
        emit_event({"evento": f"ot_{tipo}", "categoria": cat, "severidade": sev,
                    "linha": alvo, "tipo": tipo, "dur_s": dur, "msg": msg,
                    "valor": 1})   # campo numerico: o parser json do Telegraf exige um
    return ok


def floats_to_regs(values):
    regs = []
    for v in values:
        hi, lo = struct.unpack(">HH", struct.pack(">f", float(v)))
        regs += [hi, lo]
    return regs


def write_snapshot(ctx, snap):
    s = snap["planta"]
    ctx.setValues(3, 0, floats_to_regs([
        s["consumo_total_kw"], s["producao_total_pph"], s["motores_ativos"],
        s["temperatura_ambiente_c"], s["hora_do_dia"]]))
    for i, lid in enumerate(LINE_IDS):
        l = snap["linhas"][lid]
        ctx.setValues(3, 100 + i * 20, floats_to_regs([
            l["corrente_a"], l["temperatura_c"], l["vibracao_mms"], l["rpm"],
            l["producao_pph"], l["status"], l["consumo_kw"]]))


def updater(ctx):
    global last_snapshot
    while True:
        snap = sub.update()
        last_snapshot = snap
        write_snapshot(ctx, snap)
        if AUTO_FAULT and random.random() < AUTO_PROB:
            alvo = random.choice(LINE_IDS)
            if random.random() < CYBER_PROB:            # uma fracao e ataque cyber
                tipo = random.choice(["unauth_command", "spoof"])
            else:                                        # o resto e operacao
                tipo = random.choice(FAULTS)
            do_inject(alvo, tipo, random.randint(20, 60))
        time.sleep(UPDATE_S)


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
            self._send(200, {"status": "ok", "service": "process-sim-industria"})
        elif p == "/snapshot":
            self._send(200, last_snapshot)
        elif p == "/events":
            self._send(200, {"eventos": eventos[-50:]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/fault":
            return self._send(404, {"error": "not found"})
        q = parse_qs(u.query)
        linha = q.get("linha", ["IN-01"])[0]
        tipo = q.get("tipo", ["superaquecimento"])[0]   # superaquecimento|vibracao_alta|parada|unauth_command|spoof
        dur = int(q.get("dur", ["30"])[0])
        ok = do_inject(linha, tipo, dur)
        self._send(200 if ok else 400,
                   {"status": "fault injected" if ok else "alvo invalido",
                    "linha": linha, "tipo": tipo, "dur_s": dur})


def main():
    block = ModbusSequentialDataBlock(0, [0] * 400)
    slave = ModbusSlaveContext(hr=block)
    context = ModbusServerContext(slaves=slave, single=True)

    mqtt_conectar()
    threading.Thread(target=updater, args=(slave,), daemon=True).start()
    threading.Thread(
        target=lambda: ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever(),
        daemon=True).start()
    print(f"process-sim industria | modbus :{MODBUS_PORT} | http :{HTTP_PORT} | "
          f"dia={DAY_MIN}min update={UPDATE_S}s", flush=True)
    StartTcpServer(context=context, address=("0.0.0.0", MODBUS_PORT))


if __name__ == "__main__":
    main()
