"""Servidor do process-sim de agua: expoe o modelo hidraulico como Modbus TCP (o
que o SCADA/FUXA e o Telegraf leem) e um HTTP de controle (health, snapshot,
injecao de falha - o 'chaos' do OT).

Mapa de registradores (holding, FLOAT32 big-endian ABCD, 2 regs cada):
  Estacao, base 0:
    0  nivel_reservatorio_pct  2  vazao_entrada_ls   4  pressao_saida_mca
    6  turbidez_ntu            8  cloro_mgl          10 setores_ativos   12 hora_do_dia
  Setor s (0..4), base 100 + s*20:
    +0 pressao_mca  +2 vazao_ls  +4 consumo_m3h  +6 perda_pct  +8 status
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

from model import SETORES, Estacao

DAY_MIN    = float(os.environ.get("SIM_DAY_MINUTES", "20"))
UPDATE_S   = float(os.environ.get("SIM_UPDATE_S", "5"))
MODBUS_PORT = int(os.environ.get("MODBUS_PORT", "502"))
HTTP_PORT   = int(os.environ.get("HTTP_PORT", "8080"))
AUTO_FAULT  = os.environ.get("AUTO_FAULT", "true") == "true"
AUTO_PROB   = float(os.environ.get("AUTO_FAULT_PROB", "0.05"))  # por update
CYBER_PROB  = float(os.environ.get("CYBER_FAULT_PROB", "0.25")) # fracao cyber
MQTT_HOST   = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT   = int(os.environ.get("MQTT_PORT", "1883"))

sub = Estacao(day_minutes=DAY_MIN)
last_snapshot = {}
SETOR_IDS = [s["id"] for s in SETORES]
FAULTS = ["vazamento", "rompimento", "bomba_off"]

# Classificacao do evento por tipo de falha. unauth_command, spoof e dosagem sao
# ataque (severidade de seguranca); vazamento/rompimento/bomba_off sao operacao.
EVENTOS = {
    "unauth_command": ("seguranca", "critical", "fechamento de valvula por comando nao autorizado"),
    "spoof":          ("seguranca", "high",     "telemetria do setor falseada (leitura congelada)"),
    "dosagem":        ("seguranca", "high",     "dosagem de cloro adulterada (risco de contaminacao)"),
    "rompimento":     ("operacao",  "high",     "rompimento de adutora (setor sem pressao)"),
    "vazamento":      ("operacao",  "medium",   "vazamento no setor (vazao alta, pressao baixa)"),
    "bomba_off":      ("operacao",  "medium",   "bomba fora de operacao (setor sem pressao)"),
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
    c = mqtt.Client(client_id="process-sim-agua")
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
            _mqc.publish(f"ot/agua/evento/{cat}", json.dumps(ev), qos=0)
        except Exception:
            pass


def do_inject(alvo, tipo, dur):
    """Aplica a falha E emite o evento classificado. Usado pelo auto-fault e pelo
    /fault, pra operacao e ataque sairem pelo mesmo caminho."""
    ok = sub.inject(alvo, tipo, dur)
    if ok:
        cat, sev, msg = EVENTOS.get(tipo, ("operacao", "low", tipo))
        # o modelo usa "estacao" como alvo; no evento emite o ID real (ETA-) pra o
        # Splunk derivar o setor pelo prefixo (macro obs_lab_ot_setor).
        feeder = "ETA-Central" if alvo == "estacao" else alvo
        emit_event({"evento": f"ot_{tipo}", "categoria": cat, "severidade": sev,
                    "feeder": feeder, "tipo": tipo, "dur_s": dur, "msg": msg,
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
        s["nivel_reservatorio_pct"], s["vazao_entrada_ls"], s["pressao_saida_mca"],
        s["turbidez_ntu"], s["cloro_mgl"], s["setores_ativos"], s["hora_do_dia"]]))
    for i, sid in enumerate(SETOR_IDS):
        f = snap["setores"][sid]
        ctx.setValues(3, 100 + i * 20, floats_to_regs([
            f["pressao_mca"], f["vazao_ls"], f["consumo_m3h"],
            f["perda_pct"], f["status"]]))


def updater(ctx):
    global last_snapshot
    while True:
        snap = sub.update()
        last_snapshot = snap
        write_snapshot(ctx, snap)
        if AUTO_FAULT and random.random() < AUTO_PROB:
            if random.random() < CYBER_PROB:            # uma fracao e ataque cyber
                if random.random() < 0.5:
                    alvo, tipo = "estacao", "dosagem"    # contaminacao na estacao
                else:
                    alvo = random.choice(SETOR_IDS)
                    tipo = random.choice(["unauth_command", "spoof"])
            else:                                        # o resto e operacao
                alvo = random.choice(SETOR_IDS)
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
            self._send(200, {"status": "ok", "service": "process-sim-agua"})
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
        feeder = q.get("feeder", ["AG-01"])[0]
        tipo = q.get("tipo", ["vazamento"])[0]   # vazamento|rompimento|bomba_off|unauth_command|spoof|dosagem
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
    print(f"process-sim agua | modbus :{MODBUS_PORT} | http :{HTTP_PORT} | "
          f"dia={DAY_MIN}min update={UPDATE_S}s", flush=True)
    StartTcpServer(context=context, address=("0.0.0.0", MODBUS_PORT))


if __name__ == "__main__":
    main()
