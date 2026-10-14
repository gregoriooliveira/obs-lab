"""Modelo eletrico da distribuicao de energia do lab OT.

Puro Python, sem dependencia externa, para ser testavel sozinho. O server.py
envolve isto num servidor Modbus TCP + HTTP. Nao e um power flow de verdade
(isso seria pandapower/OpenDSS); e um modelo de queda de tensao por carga,
suficiente pra demo: V, I, P/Q por alimentador reagem a uma curva de carga
diaria acelerada e a falhas injetadas (trip, sobrecarga, afundamento).

Unidades: tensao em pu (1.0 = nominal), potencia em kW/kvar, corrente em A.
"""
import math
import random
import time

# 1 subestacao, 5 alimentadores (AL-01..AL-05). Carga nominal e fator de
# potencia proprios, pra os numeros nao ficarem todos iguais na tela.
VLL_KV = 13.8                      # tensao de linha nominal da subestacao
FEEDERS = [
    {"id": "AL-01", "nome": "Centro",     "p_nom_kw": 2800, "fp": 0.95},
    {"id": "AL-02", "nome": "Industrial", "p_nom_kw": 4200, "fp": 0.90},
    {"id": "AL-03", "nome": "Residencial","p_nom_kw": 3500, "fp": 0.97},
    {"id": "AL-04", "nome": "Rural",      "p_nom_kw": 1800, "fp": 0.92},
    {"id": "AL-05", "nome": "Comercial",  "p_nom_kw": 3100, "fp": 0.94},
]
K_DROP = 0.09          # queda de tensao no fim do alimentador a plena carga (pu)
NOISE = 0.015          # ruido relativo na carga, pra a tela nao ficar estatica


def _daily_shape(frac):
    """Curva de carga 0..1 ao longo do 'dia' (frac = 0..1). Dois picos:
    manha (~0.4) e noite (~0.8), vale de madrugada. Base 0.55."""
    manha = 0.30 * math.exp(-((frac - 0.40) ** 2) / (2 * 0.06 ** 2))
    noite = 0.45 * math.exp(-((frac - 0.80) ** 2) / (2 * 0.08 ** 2))
    return 0.50 + manha + noite


class Feeder:
    def __init__(self, cfg, offset):
        self.__dict__.update(cfg)
        self.offset = offset          # defasagem na curva, pra nao picarem juntos
        self.status = 1               # 1 ligado, 0 desligado (trip)
        self.fault = None             # None | "overload" | "sag" | "trip" | "spoof"
        self.fault_until = 0.0
        self._cur = {}                # o que vai pro Modbus (pode ser spoofado)
        self._real = {}               # a grandeza fisica de verdade (nunca spoofada)
        self._spoof_frozen = None     # leitura congelada durante o spoof

    def _load_factor(self, day_frac):
        base = _daily_shape((day_frac + self.offset) % 1.0)
        return max(0.05, base + random.uniform(-NOISE, NOISE))

    def update(self, day_frac, vsrc_pu, now):
        if self.fault and now >= self.fault_until:
            self.fault = None
            self._spoof_frozen = None
            if self.status == 0:
                self.status = 1       # religa depois do trip

        if self.status == 0:
            self._real = dict(tensao_pu=0.0, corrente_a=0.0, potencia_kw=0.0,
                              potencia_kvar=0.0, carga_pct=0.0, status=0.0)
            self._cur = dict(self._real)
            return self._cur

        lf = self._load_factor(day_frac)
        if self.fault == "overload":
            lf = max(lf, random.uniform(1.08, 1.20))
        p_kw = self.p_nom_kw * lf
        q_kvar = p_kw * math.tan(math.acos(self.fp))
        s_kva = math.hypot(p_kw, q_kvar)

        carga_pct = p_kw / self.p_nom_kw * 100.0
        # tensao no fim: fonte menos queda proporcional a carga
        v_end = vsrc_pu - K_DROP * (p_kw / self.p_nom_kw)
        if self.fault == "sag":
            v_end -= 0.06
        v_end = max(0.0, v_end)
        # corrente (A): S / (sqrt3 * Vll), Vll em V escalado pela tensao de fim
        v_ll_v = VLL_KV * 1000.0 * max(v_end, 0.5)
        i_a = s_kva * 1000.0 / (math.sqrt(3) * v_ll_v)

        # grandeza FISICA de verdade (vai pro total da subestacao, medido no barramento)
        self._real = dict(
            tensao_pu=round(v_end, 4), corrente_a=round(i_a, 1),
            potencia_kw=round(p_kw, 1), potencia_kvar=round(q_kvar, 1),
            carga_pct=round(carga_pct, 1), status=1.0,
        )
        # o que o alimentador REPORTA no Modbus. No spoof, congela numa leitura
        # benigna enquanto a carga real segue a curva -> o reportado nao bate com
        # o total da subestacao (a assinatura do spoofing de telemetria).
        if self.fault == "spoof":
            if self._spoof_frozen is None:
                self._spoof_frozen = dict(self._real)
            self._cur = dict(self._spoof_frozen, status=1.0)
        else:
            self._cur = dict(self._real)
        return self._cur

    def inject(self, tipo, dur_s, now):
        # unauth_command e, eletricamente, um trip (disjuntor abre); a diferenca
        # e a ORIGEM (comando nao autorizado), que o server classifica como evento
        # de seguranca. Aqui tratamos o efeito eletrico.
        efeito = "trip" if tipo == "unauth_command" else tipo
        self.fault = efeito
        self.fault_until = now + dur_s
        if efeito == "trip":
            self.status = 0


class Substation:
    def __init__(self, day_minutes=20.0):
        self.day_s = day_minutes * 60.0
        self.t0 = time.time()
        self.feeders = [Feeder(c, i / len(FEEDERS)) for i, c in enumerate(FEEDERS)]
        self.vsrc_pu = 1.0
        self.sag_until = 0.0

    def day_frac(self, now):
        return ((now - self.t0) % self.day_s) / self.day_s

    def update(self, now=None):
        now = now or time.time()
        self.vsrc_pu = 0.94 if now < self.sag_until else round(random.uniform(0.99, 1.01), 4)
        df = self.day_frac(now)
        fs = [f.update(df, self.vsrc_pu, now) for f in self.feeders]   # reportado
        real = [f._real for f in self.feeders]                          # fisico
        ativos = sum(1 for r in real if r["status"] == 1.0)
        # a subestacao mede no barramento (grandeza real); os alimentadores reportam
        # o _cur (que o spoof pode falsear). Σ alimentador != total => incoerencia.
        return {
            "subestacao": {
                "tensao_entrada_kv": round(VLL_KV * self.vsrc_pu, 3),
                "potencia_total_mw": round(sum(r["potencia_kw"] for r in real) / 1000.0, 3),
                "potencia_total_mvar": round(sum(r["potencia_kvar"] for r in real) / 1000.0, 3),
                "frequencia_hz": round(60.0 + random.uniform(-0.03, 0.03), 3),
                "alimentadores_ativos": float(ativos),
                "hora_do_dia": round(df * 24.0, 2),
            },
            "alimentadores": {self.feeders[i].id: fs[i] for i in range(len(fs))},
        }

    def inject(self, feeder_id, tipo, dur_s, now=None):
        now = now or time.time()
        if feeder_id == "subestacao" and tipo == "sag":
            self.sag_until = now + dur_s
            return True
        for f in self.feeders:
            if f.id == feeder_id:
                f.inject(tipo, dur_s, now)
                return True
        return False


if __name__ == "__main__":
    # auto-teste: roda o dia inteiro em passos e confere invariantes
    s = Substation(day_minutes=20.0)
    base = s.t0
    vmin = vmax = None
    for step in range(0, 1200, 5):            # 20 min em passos de 5s
        snap = s.update(now=base + step)
        for fid, f in snap["alimentadores"].items():
            assert 0.0 <= f["tensao_pu"] <= 1.05, (fid, f)
            assert f["corrente_a"] >= 0
            v = f["tensao_pu"]
            vmin = v if vmin is None else min(vmin, v)
            vmax = v if vmax is None else max(vmax, v)
    # falhas
    s.inject("AL-02", "overload", 30, now=base + 100)
    snap = s.update(now=base + 110)
    assert snap["alimentadores"]["AL-02"]["carga_pct"] > 100, "overload nao subiu carga"
    s.inject("AL-03", "trip", 20, now=base + 200)
    snap = s.update(now=base + 205)
    assert snap["alimentadores"]["AL-03"]["status"] == 0.0, "trip nao desligou"
    s.inject("subestacao", "sag", 15, now=base + 300)
    snap = s.update(now=base + 305)
    assert snap["subestacao"]["tensao_entrada_kv"] < VLL_KV * 0.96, "sag nao caiu"

    # cyber: unauth_command abre o disjuntor (eletricamente = trip)
    s2 = Substation(day_minutes=20.0)
    b2 = s2.t0
    s2.inject("AL-01", "unauth_command", 30, now=b2 + 10)
    snap = s2.update(now=b2 + 15)
    assert snap["alimentadores"]["AL-01"]["status"] == 0.0, "unauth_command nao abriu"

    # cyber: spoof congela a leitura do alimentador enquanto a carga real anda ->
    # a soma dos alimentadores reportados descola do total medido na subestacao
    s3 = Substation(day_minutes=20.0)
    b3 = s3.t0
    s3.update(now=b3 + 0)
    s3.inject("AL-05", "spoof", 600, now=b3 + 5)
    r1 = s3.update(now=b3 + 10)["alimentadores"]["AL-05"]["potencia_kw"]
    snap = s3.update(now=b3 + 400)          # bem mais tarde no "dia"
    r2 = snap["alimentadores"]["AL-05"]["potencia_kw"]
    assert r1 == r2, "spoof nao congelou o reportado"
    soma_rep = sum(f["potencia_kw"] for f in snap["alimentadores"].values())
    total_real_kw = snap["subestacao"]["potencia_total_mw"] * 1000.0
    assert abs(soma_rep - total_real_kw) > 1.0, "spoof nao gerou incoerencia SE vs alimentadores"
    print(f"ok - unauth_command abre; spoof congela ({r1:.0f} kW) e descola "
          f"{abs(soma_rep-total_real_kw):.0f} kW do total da subestacao")
    print(f"ok - tensao fim de alimentador variou {vmin:.3f}..{vmax:.3f} pu no dia")
    print("pico MW:", max(
        Substation(day_minutes=20).update(now=base + t)["subestacao"]["potencia_total_mw"]
        for t in range(0, 1200, 30)))
