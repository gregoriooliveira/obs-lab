"""Servidor do process-sim de gas: expoe o modelo da rede como Modbus TCP (o que
o SCADA/FUXA e o Telegraf leem) e um HTTP de controle (health, snapshot, injecao
de falha - o 'chaos' do OT).

Mapa de registradores (holding, FLOAT32 big-endian ABCD, 2 regs cada):
  City-gate/estacao, base 0:
    0  pressao_entrada_bar   2  vazao_entrada_m3h   4  pressao_saida_mbar
    6  temperatura_c         8  odorizacao_mgm3    10  zonas_ativas  12 hora_do_dia
  Zona z (0..4), base 100 + z*20:
    +0 pressao_mbar  +2 vazao_m3h  +4 consumo_m3h  +6 status
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

from model import ZONES, CityGate

DAY_MIN    = float(os.environ.get("SIM_DAY_MINUTES", "20"))
UPDATE_S   = float(os.environ.get("SIM_UPDATE_S", "5"))
MODBUS_PORT = int(os.environ.get("MODBUS_PORT", "502"))
HTTP_PORT   = int(os.environ.get("HTTP_PORT", "8080"))
AUTO_FAULT  = os.environ.get("AUTO_FAULT", "true") == "true"
AUTO_PROB   = float(os.environ.get("AUTO_FAULT_PROB", "0.05"))  # por update
CYBER_PROB  = float(os.environ.get("CYBER_FAULT_PROB", "0.25")) # fracao cyber
MQTT_HOST   = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT   = int(os.environ.get("MQTT_PORT", "1883"))

sub = CityGate(day_minutes=DAY_MIN)
last_snapshot = {}
ZONE_IDS = [z["id"] for z in ZONES]
FAULTS = ["vazamento", "sobrepressao", "valvula_fechada"]

# Classificacao do evento por tipo de falha. unauth_command, spoof e
# odorizacao_off sao ataque/risco (vao com severidade de seguranca); vazamento,
# sobrepressao e valvula_fechada sao operacao.
EVENTOS = {
    "unauth_command":  ("seguranca", "critical", "comando de fechamento de valvula nao autorizado"),
    "spoof":           ("seguranca", "high",     "telemetria da zona falseada (leitura congelada)"),
    "odorizacao_off":  ("seguranca", "high",     "odorizacao desligada - gas sem odor (risco)"),
    "vazamento":       ("operacao",  "high",     "vazamento detectado na zona"),
    "sobrepressao":    ("operacao",  "medium",   "sobrepressao na zona"),
    "valvula_fechada": ("operacao",  "medium",   "valvula fechada (bloqueio de zona)"),
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
    c = mqtt.Client(client_id="process-sim-gas")
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
            _mqc.publish(f"ot/gas/evento/{cat}", json.dumps(ev), qos=0)
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
    s = snap["estacao"]
    ctx.setValues(3, 0, floats_to_regs([
        s["pressao_entrada_bar"], s["vazao_entrada_m3h"], s["pressao_saida_mbar"],
        s["temperatura_c"], s["odorizacao_mgm3"], s["zonas_ativas"], s["hora_do_dia"]]))
    for i, zid in enumerate(ZONE_IDS):
        z = snap["zonas"][zid]
        ctx.setValues(3, 100 + i * 20, floats_to_regs([
            z["pressao_mbar"], z["vazao_m3h"], z["consumo_m3h"], z["status"]]))


def updater(ctx):
    global last_snapshot
    while True:
        snap = sub.update()
        last_snapshot = snap
        write_snapshot(ctx, snap)
        if AUTO_FAULT and random.random() < AUTO_PROB:
            if random.random() < CYBER_PROB:            # uma fracao e ataque cyber
                alvo = random.choice(ZONE_IDS)
                tipo = random.choice(["unauth_command", "spoof"])
            else:                                        # o resto e operacao/risco
                alvo = random.choice(ZONE_IDS + ["estacao"])
                tipo = "odorizacao_off" if alvo == "estacao" else random.choice(FAULTS)
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
            self._send(200, {"status": "ok", "service": "process-sim-gas"})
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
        zona = q.get("feeder", q.get("zona", ["GS-01"]))[0]
        tipo = q.get("tipo", ["vazamento"])[0]   # vazamento|sobrepressao|valvula_fechada|unauth_command|spoof|odorizacao_off
        dur = int(q.get("dur", ["30"])[0])
        ok = do_inject(zona, tipo, dur)
        self._send(200 if ok else 400,
                   {"status": "fault injected" if ok else "alvo invalido",
                    "zona": zona, "tipo": tipo, "dur_s": dur})


def main():
    block = ModbusSequentialDataBlock(0, [0] * 400)
    slave = ModbusSlaveContext(hr=block)
    context = ModbusServerContext(slaves=slave, single=True)

    mqtt_conectar()
    threading.Thread(target=updater, args=(slave,), daemon=True).start()
    threading.Thread(
        target=lambda: ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever(),
        daemon=True).start()
    print(f"process-sim gas | modbus :{MODBUS_PORT} | http :{HTTP_PORT} | "
          f"dia={DAY_MIN}min update={UPDATE_S}s", flush=True)
    StartTcpServer(context=context, address=("0.0.0.0", MODBUS_PORT))


if __name__ == "__main__":
    main()
