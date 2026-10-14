"""Modelo hidraulico da distribuicao de agua do lab OT.

Puro Python, sem dependencia externa, para ser testavel sozinho. O server.py
envolve isto num servidor Modbus TCP + HTTP. Nao e um solver de redes de verdade
(isso seria EPANET/WNTR); e um modelo de queda de pressao por vazao, suficiente
pra demo: pressao, vazao e consumo por setor reagem a uma curva de consumo diaria
acelerada e a falhas injetadas (vazamento, rompimento, bomba fora, spoof, dosagem).

Unidades: pressao em mca (metros de coluna d'agua), vazao em L/s, consumo em m3/h,
nivel do reservatorio em %, turbidez em NTU, cloro em mg/L.
"""
import math
import random
import time

# 1 estacao (ETA/reservatorio), 5 setores de abastecimento (AG-01..AG-05).
# Consumo nominal proprio por setor, pra os numeros nao ficarem todos iguais.
PRESSAO_NOM_MCA = 45.0             # pressao nominal na saida da estacao
SETORES = [
    {"id": "AG-01", "nome": "Centro",      "q_nom_m3h": 320, "p_fator": 1.00},
    {"id": "AG-02", "nome": "Industrial",  "q_nom_m3h": 480, "p_fator": 0.92},
    {"id": "AG-03", "nome": "Residencial", "q_nom_m3h": 400, "p_fator": 1.00},
    {"id": "AG-04", "nome": "Rural",       "q_nom_m3h": 180, "p_fator": 0.85},
    {"id": "AG-05", "nome": "Comercial",   "q_nom_m3h": 350, "p_fator": 0.95},
]
K_DROP_MCA = 12.0      # queda de pressao (mca) no fim do setor a plena vazao
PERDA_FISICA = 6.0     # perda fisica de base do setor (%), sobe no vazamento
NOISE = 0.015          # ruido relativo no consumo, pra a tela nao ficar estatica


def _daily_shape(frac):
    """Curva de consumo 0..1 ao longo do 'dia' (frac = 0..1). Dois picos:
    manha (~0.4) e noite (~0.8), vale de madrugada. Base 0.50."""
    manha = 0.30 * math.exp(-((frac - 0.40) ** 2) / (2 * 0.06 ** 2))
    noite = 0.45 * math.exp(-((frac - 0.80) ** 2) / (2 * 0.08 ** 2))
    return 0.50 + manha + noite


class Setor:
    def __init__(self, cfg, offset):
        self.__dict__.update(cfg)
        self.offset = offset          # defasagem na curva, pra nao picarem juntos
        self.status = 1               # 1 em operacao, 0 parado (rompimento)
        self.fault = None             # None | vazamento | rompimento | bomba_off | spoof
        self.fault_until = 0.0
        self._cur = {}                # o que vai pro Modbus (pode ser spoofado)
        self._real = {}               # a grandeza fisica de verdade (nunca spoofada)
        self._spoof_frozen = None     # leitura congelada durante o spoof

    def _load_factor(self, day_frac):
        base = _daily_shape((day_frac + self.offset) % 1.0)
        return max(0.05, base + random.uniform(-NOISE, NOISE))

    def update(self, day_frac, p_src_mca, now):
        if self.fault and now >= self.fault_until:
            self.fault = None
            self._spoof_frozen = None
            if self.status == 0:
                self.status = 1       # volta depois do rompimento

        if self.status == 0:
            self._real = dict(pressao_mca=0.0, vazao_ls=0.0, consumo_m3h=0.0,
                              perda_pct=0.0, status=0.0)
            self._cur = dict(self._real)
            return self._cur

        lf = self._load_factor(day_frac)
        perda_pct = PERDA_FISICA
        if self.fault == "vazamento":
            lf = max(lf, random.uniform(1.10, 1.25))   # vazao sobe
            perda_pct = random.uniform(18.0, 30.0)      # muita agua perdida no trecho
        consumo_m3h = self.q_nom_m3h * lf
        vazao_ls = consumo_m3h * 1000.0 / 3600.0        # m3/h -> L/s

        # pressao no fim: fonte (escalada pelo fator do setor) menos queda com a vazao
        p_end = p_src_mca * self.p_fator - K_DROP_MCA * (consumo_m3h / self.q_nom_m3h)
        if self.fault == "vazamento":
            p_end -= 6.0                                 # vazamento derruba a pressao
        if self.fault == "bomba_off":
            p_end = 0.0                                  # sem recalque, sem pressao
        p_end = max(0.0, p_end)

        # grandeza FISICA de verdade (vai pro total da estacao, medido na saida)
        self._real = dict(
            pressao_mca=round(p_end, 2), vazao_ls=round(vazao_ls, 2),
            consumo_m3h=round(consumo_m3h, 2), perda_pct=round(perda_pct, 1),
            status=1.0,
        )
        # o que o setor REPORTA no Modbus. No spoof, congela numa leitura benigna
        # enquanto o consumo real segue a curva -> o reportado nao bate com a vazao
        # de entrada da estacao (a assinatura do spoofing de telemetria).
        if self.fault == "spoof":
            if self._spoof_frozen is None:
                self._spoof_frozen = dict(self._real)
            self._cur = dict(self._spoof_frozen, status=1.0)
        else:
            self._cur = dict(self._real)
        return self._cur

    def inject(self, tipo, dur_s, now):
        # unauth_command fecha a valvula do setor: hidraulicamente e uma parada
        # (status 0), igual ao rompimento; a diferenca e a ORIGEM (comando nao
        # autorizado), que o server classifica como evento de seguranca.
        efeito = "rompimento" if tipo == "unauth_command" else tipo
        self.fault = efeito
        self.fault_until = now + dur_s
        if efeito == "rompimento":
            self.status = 0


class Estacao:
    def __init__(self, day_minutes=20.0):
        self.day_s = day_minutes * 60.0
        self.t0 = time.time()
        self.setores = [Setor(c, i / len(SETORES)) for i, c in enumerate(SETORES)]
        self.p_src_mca = PRESSAO_NOM_MCA
        self.dosagem_until = 0.0      # ataque na dosagem de cloro (contaminacao)

    def day_frac(self, now):
        return ((now - self.t0) % self.day_s) / self.day_s

    def update(self, now=None):
        now = now or time.time()
        self.p_src_mca = round(PRESSAO_NOM_MCA * random.uniform(0.99, 1.01), 2)
        df = self.day_frac(now)
        ss = [s.update(df, self.p_src_mca, now) for s in self.setores]  # reportado
        real = [s._real for s in self.setores]                          # fisico
        ativos = sum(1 for r in real if r["status"] == 1.0)
        consumo_total = sum(r["consumo_m3h"] for r in real)
        nom_total = sum(s.q_nom_m3h for s in self.setores)
        # nivel do reservatorio: cai um pouco no pico de consumo
        nivel = 88.0 - 22.0 * (consumo_total / nom_total) + random.uniform(-1.5, 1.5)
        # cloro: normal ~1.2 mg/L; na dosagem adulterada despenca (risco sanitario)
        cloro = round(random.uniform(0.05, 0.20), 3) if now < self.dosagem_until \
            else round(random.uniform(1.0, 1.4), 3)
        turbidez = round(random.uniform(0.3, 0.8), 3)
        # a estacao mede na saida (grandeza real); os setores reportam o _cur (que
        # o spoof pode falsear). Σ vazao dos setores != vazao de entrada => incoerencia.
        return {
            "estacao": {
                "nivel_reservatorio_pct": round(max(0.0, min(100.0, nivel)), 2),
                "vazao_entrada_ls": round(sum(r["vazao_ls"] for r in real), 2),
                "pressao_saida_mca": round(self.p_src_mca, 2),
                "turbidez_ntu": turbidez,
                "cloro_mgl": cloro,
                "setores_ativos": float(ativos),
                "hora_do_dia": round(df * 24.0, 2),
            },
            "setores": {self.setores[i].id: ss[i] for i in range(len(ss))},
        }

    def inject(self, setor_id, tipo, dur_s, now=None):
        now = now or time.time()
        if setor_id == "estacao" and tipo == "dosagem":
            self.dosagem_until = now + dur_s
            return True
        for s in self.setores:
            if s.id == setor_id:
                s.inject(tipo, dur_s, now)
                return True
        return False


if __name__ == "__main__":
    # auto-teste: roda o dia inteiro em passos e confere invariantes
    e = Estacao(day_minutes=20.0)
    base = e.t0
    pmin = pmax = None
    for step in range(0, 1200, 5):            # 20 min em passos de 5s
        snap = e.update(now=base + step)
        for sid, s in snap["setores"].items():
            assert s["pressao_mca"] >= 0, (sid, s)
            assert s["vazao_ls"] >= 0
            p = s["pressao_mca"]
            pmin = p if pmin is None else min(pmin, p)
            pmax = p if pmax is None else max(pmax, p)
    # falhas operacionais
    e.inject("AG-02", "vazamento", 30, now=base + 100)
    snap = e.update(now=base + 110)
    assert snap["setores"]["AG-02"]["perda_pct"] > PERDA_FISICA, "vazamento nao subiu a perda"
    e.inject("AG-03", "rompimento", 20, now=base + 200)
    snap = e.update(now=base + 205)
    assert snap["setores"]["AG-03"]["status"] == 0.0, "rompimento nao parou o setor"
    e.inject("AG-01", "bomba_off", 20, now=base + 300)
    snap = e.update(now=base + 305)
    assert snap["setores"]["AG-01"]["pressao_mca"] == 0.0, "bomba_off nao zerou a pressao"

    # cyber: dosagem derruba o cloro na estacao (risco de contaminacao)
    e.inject("estacao", "dosagem", 15, now=base + 400)
    snap = e.update(now=base + 405)
    assert snap["estacao"]["cloro_mgl"] < 0.5, "dosagem nao derrubou o cloro"

    # cyber: unauth_command fecha a valvula (hidraulicamente = parada)
    e2 = Estacao(day_minutes=20.0)
    b2 = e2.t0
    e2.inject("AG-01", "unauth_command", 30, now=b2 + 10)
    snap = e2.update(now=b2 + 15)
    assert snap["setores"]["AG-01"]["status"] == 0.0, "unauth_command nao fechou a valvula"

    # cyber: spoof congela a leitura do setor enquanto o consumo real anda ->
    # a soma das vazoes reportadas descola da vazao de entrada medida na estacao
    e3 = Estacao(day_minutes=20.0)
    b3 = e3.t0
    e3.update(now=b3 + 0)
    e3.inject("AG-05", "spoof", 600, now=b3 + 5)
    r1 = e3.update(now=b3 + 10)["setores"]["AG-05"]["vazao_ls"]
    snap = e3.update(now=b3 + 400)          # bem mais tarde no "dia"
    r2 = snap["setores"]["AG-05"]["vazao_ls"]
    assert r1 == r2, "spoof nao congelou o reportado"
    soma_rep = sum(s["vazao_ls"] for s in snap["setores"].values())
    total_real_ls = snap["estacao"]["vazao_entrada_ls"]
    assert abs(soma_rep - total_real_ls) > 0.5, "spoof nao gerou incoerencia estacao vs setores"
    print(f"ok - vazamento/rompimento/bomba_off/dosagem ok; unauth fecha valvula")
    print(f"ok - spoof congela ({r1:.1f} L/s) e descola {abs(soma_rep-total_real_ls):.1f} L/s da estacao")
    print(f"ok - pressao de fim de setor variou {pmin:.1f}..{pmax:.1f} mca no dia")
    print("pico vazao de entrada (L/s):", round(max(
        Estacao(day_minutes=20).update(now=base + t)["estacao"]["vazao_entrada_ls"]
        for t in range(0, 1200, 30)), 1))
