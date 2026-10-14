"""Servidor do process-sim: expoe o modelo eletrico como Modbus TCP (o que o
SCADA/FUXA e o Telegraf leem) e um HTTP de controle (health, snapshot, injecao
de falha - o 'chaos' do OT).

Mapa de registradores (holding, FLOAT32 big-endian ABCD, 2 regs cada):
  Subestacao, base 0:
    0  tensao_entrada_kv   2  potencia_total_mw   4  potencia_total_mvar
    6  frequencia_hz       8  alimentadores_ativos 10 hora_do_dia
  Alimentador f (0..4), base 100 + f*20:
    +0 tensao_pu  +2 corrente_a  +4 potencia_kw  +6 potencia_kvar
    +8 carga_pct  +10 status
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

from model import FEEDERS, Substation

DAY_MIN    = float(os.environ.get("SIM_DAY_MINUTES", "20"))
UPDATE_S   = float(os.environ.get("SIM_UPDATE_S", "5"))
MODBUS_PORT = int(os.environ.get("MODBUS_PORT", "502"))
HTTP_PORT   = int(os.environ.get("HTTP_PORT", "8080"))
AUTO_FAULT  = os.environ.get("AUTO_FAULT", "true") == "true"
AUTO_PROB   = float(os.environ.get("AUTO_FAULT_PROB", "0.05"))  # por update
CYBER_PROB  = float(os.environ.get("CYBER_FAULT_PROB", "0.25")) # fracao cyber
MQTT_HOST   = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT   = int(os.environ.get("MQTT_PORT", "1883"))

sub = Substation(day_minutes=DAY_MIN)
last_snapshot = {}
FEEDER_IDS = [f["id"] for f in FEEDERS]
FAULTS = ["overload", "sag", "trip"]

# Classificacao do evento por tipo de falha. unauth_command e spoof sao ataque
# (vao com severidade de seguranca); trip/overload/sag sao operacao normal.
EVENTOS = {
    "unauth_command": ("seguranca", "critical", "comando de abertura nao autorizado no disjuntor"),
    "spoof":          ("seguranca", "high",     "telemetria do alimentador falseada (leitura congelada)"),
    "config_change":  ("seguranca", "high",     "mudanca de parametro/firmware no rele sem mudanca aprovada"),
    "trip":           ("operacao",  "medium",   "disjuntor abriu por protecao"),
    "overload":       ("operacao",  "medium",   "sobrecarga no alimentador"),
    "sag":            ("operacao",  "low",      "afundamento de tensao na subestacao"),
    "religamento":    ("operacao",  "low",      "alimentador reenergizado (religamento)"),
}
_mqc = None
eventos = []     # ultimos eventos, pro /events
status_ant = {}  # feeder -> status anterior, pra detectar religamento (0 -> 1)


def mqtt_conectar():
    """Conecta ao broker em background, com retentativa. Nunca bloqueia o Modbus:
    se o broker nao subir, os eventos saem so no log."""
    global _mqc
    if mqtt is None:
        print("[process-sim] paho ausente; eventos so no log", flush=True)
        return
    c = mqtt.Client(client_id="process-sim-energia")
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
    print(f"[evento:{cat}] {ev.get('feeder')} {ev.get('tipo')} {ev.get('severidade')}", flush=True)
    if _mqc is not None:
        try:
            _mqc.publish(f"ot/energia/evento/{cat}", json.dumps(ev), qos=0)
        except Exception:
            pass


def do_inject(alvo, tipo, dur):
    """Aplica a falha E emite o evento classificado. Usado pelo auto-fault e pelo
    /fault, pra operacao e ataque sairem pelo mesmo caminho."""
    ok = sub.inject(alvo, tipo, dur)
    if ok:
        cat, sev, msg = EVENTOS.get(tipo, ("operacao", "low", tipo))
        emit_event({"evento": f"ot_{tipo}", "categoria": cat, "severidade": sev,
                    "feeder": alvo, "tipo": tipo, "dur_s": dur, "msg": msg,
                    "valor": 1})   # campo numerico: o parser json do Telegraf exige um
    return ok


def floats_to_regs(values):
    regs = []
    for v in values:
        hi, lo = struct.unpack(">HH", struct.pack(">f", float(v)))
        regs += [hi, lo]
    return regs


def write_snapshot(ctx, snap):
    s = snap["subestacao"]
    ctx.setValues(3, 0, floats_to_regs([
        s["tensao_entrada_kv"], s["potencia_total_mw"], s["potencia_total_mvar"],
        s["frequencia_hz"], s["alimentadores_ativos"], s["hora_do_dia"]]))
    for i, fid in enumerate(FEEDER_IDS):
        f = snap["alimentadores"][fid]
        ctx.setValues(3, 100 + i * 20, floats_to_regs([
            f["tensao_pu"], f["corrente_a"], f["potencia_kw"],
            f["potencia_kvar"], f["carga_pct"], f["status"]]))


def emit_religamento(fid):
    """Evento de reenergizacao (status 0 -> 1). Sustenta a contagem de religamentos
    sucessivos (UC-027) e o alerta de energizacao em trecho com equipe (UC-025)."""
    cat, sev, msg = EVENTOS["religamento"]
    emit_event({"evento": "ot_religamento", "categoria": cat, "severidade": sev,
                "feeder": fid, "tipo": "religamento", "msg": msg, "valor": 1})


def detectar_religamento(snap):
    """Compara o status de cada alimentador com o ciclo anterior e emite
    religamento na borda 0 -> 1 (o alimentador religou apos um trip)."""
    global status_ant
    for fid in FEEDER_IDS:
        s = int(snap["alimentadores"][fid]["status"])
        if status_ant.get(fid, 1) == 0 and s == 1:
            emit_religamento(fid)
        status_ant[fid] = s


def updater(ctx):
    global last_snapshot
    while True:
        snap = sub.update()
        last_snapshot = snap
        write_snapshot(ctx, snap)
        detectar_religamento(snap)
        if AUTO_FAULT and random.random() < AUTO_PROB:
            if random.random() < CYBER_PROB:            # uma fracao e ataque cyber
                tipo = random.choice(["unauth_command", "spoof", "config_change"])
                alvo = random.choice(FEEDER_IDS)
                if tipo == "config_change":
                    # nao e falha fisica (nao passa pelo sub.inject): e evento puro.
                    # mudanca_aprovada=0 na maioria (sem ordem de mudanca) = o alerta.
                    cat, sev, msg = EVENTOS["config_change"]
                    aprovada = 1 if random.random() < 0.2 else 0
                    emit_event({"evento": "ot_config_change", "categoria": cat,
                                "severidade": sev, "feeder": alvo, "tipo": "config_change",
                                "mudanca_aprovada": aprovada, "msg": msg, "valor": 1})
                else:
                    do_inject(alvo, tipo, random.randint(20, 60))
            else:                                        # o resto e operacao
                alvo = random.choice(FEEDER_IDS + ["subestacao"])
                tipo = "sag" if alvo == "subestacao" else random.choice(FAULTS)
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
            self._send(200, {"status": "ok", "service": "process-sim-energia"})
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
        feeder = q.get("feeder", ["AL-01"])[0]
        tipo = q.get("tipo", ["overload"])[0]   # overload|trip|sag|unauth_command|spoof
        dur = int(q.get("dur", ["30"])[0])
        ok = do_inject(feeder, tipo, dur)
        self._send(200 if ok else 400,
                   {"status": "fault injected" if ok else "alvo invalido",
                    "feeder": feeder, "tipo": tipo, "dur_s": dur})


def main():
    block = ModbusSequentialDataBlock(0, [0] * 400)
    slave = ModbusSlaveContext(hr=block)
    context = ModbusServerContext(slaves=slave, single=True)

    mqtt_conectar()
    threading.Thread(target=updater, args=(slave,), daemon=True).start()
    threading.Thread(
        target=lambda: ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever(),
        daemon=True).start()
    print(f"process-sim energia | modbus :{MODBUS_PORT} | http :{HTTP_PORT} | "
          f"dia={DAY_MIN}min update={UPDATE_S}s", flush=True)
    StartTcpServer(context=context, address=("0.0.0.0", MODBUS_PORT))


if __name__ == "__main__":
    main()
