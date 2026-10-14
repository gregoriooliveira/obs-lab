"""Modelo da distribuicao de gas canalizado do lab OT.

Puro Python, sem dependencia externa, para ser testavel sozinho. O server.py
envolve isto num servidor Modbus TCP + HTTP. Nao e um escoamento de fluido de
verdade (isso seria pandapipes); e um modelo de queda de pressao por vazao,
suficiente pra demo: pressao e vazao por zona reagem a uma curva de consumo
diaria acelerada e a falhas injetadas (vazamento, sobrepressao, valvula fechada).

Unidades: pressao de entrada em bar, pressao de distribuicao em mbar, vazao e
consumo em m3/h, odorizacao em mg/m3.
"""
import math
import random
import time

# 1 city-gate/UTR + 5 zonas (GS-01..GS-05). Vazao nominal propria, pra os numeros
# nao ficarem todos iguais na tela.
P_ENTRADA_BAR = 7.0                # pressao de entrada no city-gate (media pressao)
P_SAIDA_MBAR = 300.0               # pressao de distribuicao apos regulagem
ODOR_NOM = 20.0                    # odorizacao nominal (mg/m3) - gas tem que "cheirar"
ZONES = [
    {"id": "GS-01", "nome": "Centro",     "q_nom_m3h": 1800, "classe": "comercial"},
    {"id": "GS-02", "nome": "Industrial", "q_nom_m3h": 3200, "classe": "industrial"},
    {"id": "GS-03", "nome": "Residencial","q_nom_m3h": 2400, "classe": "residencial"},
    {"id": "GS-04", "nome": "Rural",      "q_nom_m3h": 900,  "classe": "rural"},
    {"id": "GS-05", "nome": "Comercial",  "q_nom_m3h": 1500, "classe": "comercial"},
]
K_DROP = 0.08          # queda de pressao no fim da zona a plena vazao (fracao)
NOISE = 0.015          # ruido relativo no consumo, pra a tela nao ficar estatica


def _daily_shape(frac):
    """Curva de consumo 0..1 ao longo do 'dia' (frac = 0..1). Dois picos:
    manha (~0.4) e noite (~0.8), vale de madrugada. Base 0.55."""
    manha = 0.30 * math.exp(-((frac - 0.40) ** 2) / (2 * 0.06 ** 2))
    noite = 0.45 * math.exp(-((frac - 0.80) ** 2) / (2 * 0.08 ** 2))
    return 0.50 + manha + noite


class Zone:
    def __init__(self, cfg, offset):
        self.__dict__.update(cfg)
        self.offset = offset          # defasagem na curva, pra nao picarem juntas
        self.status = 1               # 1 aberta, 0 fechada (valvula fechada)
        self.fault = None             # None | vazamento | sobrepressao | valvula_fechada | spoof
        self.fault_until = 0.0
        self._cur = {}                # o que vai pro Modbus (pode ser spoofado)
        self._real = {}               # a grandeza fisica de verdade (nunca spoofada)
        self._spoof_frozen = None     # leitura congelada durante o spoof

    def _load_factor(self, day_frac):
        base = _daily_shape((day_frac + self.offset) % 1.0)
        return max(0.05, base + random.uniform(-NOISE, NOISE))

    def update(self, day_frac, now):
        if self.fault and now >= self.fault_until:
            self.fault = None
            self._spoof_frozen = None
            if self.status == 0:
                self.status = 1       # reabre depois do bloqueio

        if self.status == 0:
            self._real = dict(pressao_mbar=0.0, vazao_m3h=0.0, consumo_m3h=0.0, status=0.0)
            self._cur = dict(self._real)
            return self._cur

        lf = self._load_factor(day_frac)
        if self.fault == "vazamento":
            lf = max(lf, random.uniform(1.10, 1.35))   # vazamento puxa vazao
        q = self.q_nom_m3h * lf
        consumo = q * random.uniform(0.97, 1.0)        # consumo faturavel ~ vazao

        # pressao no fim: saida menos queda proporcional a vazao
        p_end = P_SAIDA_MBAR - K_DROP * P_SAIDA_MBAR * (q / self.q_nom_m3h)
        if self.fault == "vazamento":
            p_end -= 0.10 * P_SAIDA_MBAR               # vazamento derruba a pressao
        if self.fault == "sobrepressao":
            p_end = P_SAIDA_MBAR * random.uniform(1.15, 1.30)
        p_end = max(0.0, p_end)

        # grandeza FISICA de verdade (vai pro total do city-gate, medido na entrada)
        self._real = dict(
            pressao_mbar=round(p_end, 1), vazao_m3h=round(q, 1),
            consumo_m3h=round(consumo, 1), status=1.0,
        )
        # o que a zona REPORTA no Modbus. No spoof, congela numa leitura benigna
        # enquanto a vazao real segue a curva -> o reportado nao bate com a vazao
        # de entrada do city-gate (a assinatura do spoofing de telemetria).
        if self.fault == "spoof":
            if self._spoof_frozen is None:
                self._spoof_frozen = dict(self._real)
            self._cur = dict(self._spoof_frozen, status=1.0)
        else:
            self._cur = dict(self._real)
        return self._cur

    def inject(self, tipo, dur_s, now):
        # unauth_command e, no processo, um fechamento de valvula; a diferenca e a
        # ORIGEM (comando nao autorizado), que o server classifica como seguranca.
        efeito = "valvula_fechada" if tipo == "unauth_command" else tipo
        self.fault = efeito
        self.fault_until = now + dur_s
        if efeito == "valvula_fechada":
            self.status = 0


class CityGate:
    def __init__(self, day_minutes=20.0):
        self.day_s = day_minutes * 60.0
        self.t0 = time.time()
        self.zones = [Zone(c, i / len(ZONES)) for i, c in enumerate(ZONES)]
        self.odor_off_until = 0.0

    def day_frac(self, now):
        return ((now - self.t0) % self.day_s) / self.day_s

    def update(self, now=None):
        now = now or time.time()
        df = self.day_frac(now)
        zs = [z.update(df, now) for z in self.zones]   # reportado
        real = [z._real for z in self.zones]            # fisico
        ativos = sum(1 for r in real if r["status"] == 1.0)
        odor = 1.0 if now < self.odor_off_until else round(ODOR_NOM + random.uniform(-1, 1), 1)
        # o city-gate mede a vazao de entrada (grandeza real); as zonas reportam o
        # _cur (que o spoof pode falsear). Σ zonas != entrada => incoerencia.
        return {
            "estacao": {
                "pressao_entrada_bar": round(P_ENTRADA_BAR + random.uniform(-0.1, 0.1), 2),
                "vazao_entrada_m3h": round(sum(r["vazao_m3h"] for r in real), 1),
                "pressao_saida_mbar": round(P_SAIDA_MBAR + random.uniform(-3, 3), 1),
                "temperatura_c": round(20.0 + random.uniform(-2, 2), 1),
                "odorizacao_mgm3": odor,
                "zonas_ativas": float(ativos),
                "hora_do_dia": round(df * 24.0, 2),
            },
            "zonas": {self.zones[i].id: zs[i] for i in range(len(zs))},
        }

    def inject(self, zone_id, tipo, dur_s, now=None):
        now = now or time.time()
        if zone_id == "estacao" and tipo == "odorizacao_off":
            self.odor_off_until = now + dur_s
            return True
        for z in self.zones:
            if z.id == zone_id:
                z.inject(tipo, dur_s, now)
                return True
        return False


if __name__ == "__main__":
    # auto-teste: roda o dia inteiro em passos e confere invariantes
    s = CityGate(day_minutes=20.0)
    base = s.t0
    pmin = pmax = None
    for step in range(0, 1200, 5):            # 20 min em passos de 5s
        snap = s.update(now=base + step)
        for zid, z in snap["zonas"].items():
            assert z["pressao_mbar"] >= 0, (zid, z)
            assert z["vazao_m3h"] >= 0
            p = z["pressao_mbar"]
            pmin = p if pmin is None else min(pmin, p)
            pmax = p if pmax is None else max(pmax, p)

    # falhas operacionais
    s.inject("GS-02", "vazamento", 30, now=base + 100)
    snap = s.update(now=base + 110)
    assert snap["zonas"]["GS-02"]["vazao_m3h"] > 3200, "vazamento nao subiu a vazao"
    s.inject("GS-01", "sobrepressao", 30, now=base + 150)
    snap = s.update(now=base + 160)
    assert snap["zonas"]["GS-01"]["pressao_mbar"] > P_SAIDA_MBAR * 1.1, "sobrepressao nao subiu"
    s.inject("GS-03", "valvula_fechada", 20, now=base + 200)
    snap = s.update(now=base + 205)
    assert snap["zonas"]["GS-03"]["status"] == 0.0 and snap["zonas"]["GS-03"]["vazao_m3h"] == 0.0, "valvula nao fechou"
    s.inject("estacao", "odorizacao_off", 15, now=base + 300)
    snap = s.update(now=base + 305)
    assert snap["estacao"]["odorizacao_mgm3"] < 5, "odorizacao nao caiu"

    # cyber: unauth_command fecha a valvula (no processo = valvula_fechada)
    s2 = CityGate(day_minutes=20.0)
    b2 = s2.t0
    s2.inject("GS-01", "unauth_command", 30, now=b2 + 10)
    snap = s2.update(now=b2 + 15)
    assert snap["zonas"]["GS-01"]["status"] == 0.0, "unauth_command nao fechou"

    # cyber: spoof congela a leitura da zona enquanto a vazao real anda -> a soma
    # das zonas reportadas descola da vazao de entrada medida no city-gate
    s3 = CityGate(day_minutes=20.0)
    b3 = s3.t0
    s3.update(now=b3 + 0)
    s3.inject("GS-05", "spoof", 600, now=b3 + 5)
    r1 = s3.update(now=b3 + 10)["zonas"]["GS-05"]["vazao_m3h"]
    snap = s3.update(now=b3 + 400)          # bem mais tarde no "dia"
    r2 = snap["zonas"]["GS-05"]["vazao_m3h"]
    assert r1 == r2, "spoof nao congelou o reportado"
    soma_rep = sum(z["vazao_m3h"] for z in snap["zonas"].values())
    entrada = snap["estacao"]["vazao_entrada_m3h"]
    assert abs(soma_rep - entrada) > 1.0, "spoof nao gerou incoerencia city-gate vs zonas"
    print(f"ok - unauth_command fecha; spoof congela ({r1:.0f} m3/h) e descola "
          f"{abs(soma_rep-entrada):.0f} m3/h da vazao de entrada")
    print(f"ok - pressao de zona variou {pmin:.0f}..{pmax:.0f} mbar no dia")
    print("pico vazao m3/h:", max(
        CityGate(day_minutes=20).update(now=base + t)["estacao"]["vazao_entrada_m3h"]
        for t in range(0, 1200, 30)))
