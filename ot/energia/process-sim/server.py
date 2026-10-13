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

from model import FEEDERS, Substation

DAY_MIN    = float(os.environ.get("SIM_DAY_MINUTES", "20"))
UPDATE_S   = float(os.environ.get("SIM_UPDATE_S", "5"))
MODBUS_PORT = int(os.environ.get("MODBUS_PORT", "502"))
HTTP_PORT   = int(os.environ.get("HTTP_PORT", "8080"))
AUTO_FAULT  = os.environ.get("AUTO_FAULT", "true") == "true"
AUTO_PROB   = float(os.environ.get("AUTO_FAULT_PROB", "0.05"))  # por update

sub = Substation(day_minutes=DAY_MIN)
last_snapshot = {}
FEEDER_IDS = [f["id"] for f in FEEDERS]
FAULTS = ["overload", "sag", "trip"]


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


def updater(ctx):
    global last_snapshot
    while True:
        snap = sub.update()
        last_snapshot = snap
        write_snapshot(ctx, snap)
        if AUTO_FAULT and random.random() < AUTO_PROB:
            alvo = random.choice(FEEDER_IDS + ["subestacao"])
            tipo = "sag" if alvo == "subestacao" else random.choice(FAULTS)
            dur = random.randint(20, 60)
            sub.inject(alvo, tipo, dur)
            print(f"[auto-fault] {alvo} {tipo} {dur}s", flush=True)
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
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/fault":
            return self._send(404, {"error": "not found"})
        q = parse_qs(u.query)
        feeder = q.get("feeder", ["AL-01"])[0]
        tipo = q.get("tipo", ["overload"])[0]
        dur = int(q.get("dur", ["30"])[0])
        ok = sub.inject(feeder, tipo, dur)
        self._send(200 if ok else 400,
                   {"status": "fault injected" if ok else "alvo invalido",
                    "feeder": feeder, "tipo": tipo, "dur_s": dur})


def main():
    block = ModbusSequentialDataBlock(0, [0] * 400)
    slave = ModbusSlaveContext(hr=block)
    context = ModbusServerContext(slaves=slave, single=True)

    threading.Thread(target=updater, args=(slave,), daemon=True).start()
    threading.Thread(
        target=lambda: ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever(),
        daemon=True).start()
    print(f"process-sim energia | modbus :{MODBUS_PORT} | http :{HTTP_PORT} | "
          f"dia={DAY_MIN}min update={UPDATE_S}s", flush=True)
    StartTcpServer(context=context, address=("0.0.0.0", MODBUS_PORT))


if __name__ == "__main__":
    main()
