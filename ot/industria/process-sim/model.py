"""Modelo da planta industrial do lab OT (perfil WEG: motores e linhas).

Puro Python, sem dependencia externa, testavel sozinho. O server.py envolve isto
num servidor Modbus TCP + HTTP. Nao e um gemeo digital de verdade; e um modelo de
carga/producao por linha que reage a uma curva diaria acelerada (turnos) e a
falhas injetadas (superaquecimento, vibracao, parada) e ataques (comando nao
autorizado, spoof de telemetria).

Unidades: corrente em A, temperatura em C, vibracao em mm/s (RMS), rpm, producao
em pecas/hora (pph), consumo em kW.

Grandezas da planta vem da soma das linhas REAIS (medidas no barramento/MES); as
linhas reportam o _cur, que o spoof pode falsear. Sigma(linha reportada) !=
total da planta => incoerencia (a assinatura do spoofing). Alem disso, no spoof o
dano REAL (temperatura/vibracao) sobe enquanto a leitura reportada fica congelada
numa faixa benigna - o ataque esconde uma falha em desenvolvimento.
"""
import math
import random
import time

# 5 linhas/motores (IN-01..IN-05). Potencia do motor e producao nominal proprias,
# pra os numeros nao ficarem todos iguais na tela.
V_LL = 440.0                       # tensao de linha industrial (V)
FP = 0.88                          # fator de potencia tipico de motor
LINHAS = [
    {"id": "IN-01", "nome": "Extrusao", "p_nom_kw":  800, "prod_nom_pph": 1200},
    {"id": "IN-02", "nome": "Prensa",   "p_nom_kw": 1200, "prod_nom_pph":  900},
    {"id": "IN-03", "nome": "Fundicao", "p_nom_kw": 2000, "prod_nom_pph":  600},
    {"id": "IN-04", "nome": "Usinagem", "p_nom_kw":  600, "prod_nom_pph": 1500},
    {"id": "IN-05", "nome": "Montagem", "p_nom_kw":  400, "prod_nom_pph": 2000},
]
RPM_NOM = 1780.0       # motor de 4 polos (sincrono 1800), com escorregamento
TEMP_AMB = 30.0        # temperatura ambiente base da planta (C)
K_TEMP = 55.0          # aquecimento do motor a plena carga (C acima do ambiente)
VIB_BASE = 1.2         # vibracao de motor saudavel (mm/s)
K_VIB = 2.3            # vibracao adicional a plena carga
NOISE = 0.02           # ruido relativo na carga
# rampas do dano escondido pelo spoof (por segundo de ataque)
RAMP_TEMP = 0.09       # +0.09 C/s  -> +27 C em 300 s
RAMP_VIB = 0.012       # +0.012 mm/s/s


def _daily_shape(frac):
    """Curva de producao 0..1 ao longo do 'dia' (frac = 0..1). Fabrica de tres
    turnos: pico no turno do dia, media a noite, vale na virada."""
    dia = 0.42 * math.exp(-((frac - 0.5) ** 2) / (2 * 0.17 ** 2))
    return 0.35 + dia


class Linha:
    def __init__(self, cfg, offset):
        self.__dict__.update(cfg)
        self.offset = offset
        self.status = 1               # 1 rodando, 0 parada
        self.fault = None             # None|superaquecimento|vibracao_alta|parada|spoof
        self.fault_until = 0.0
        self._cur = {}                # o que vai pro Modbus (pode ser spoofado)
        self._real = {}               # a grandeza fisica de verdade
        self._spoof_frozen = None
        self._spoof_start = 0.0

    def _load_factor(self, day_frac):
        base = _daily_shape((day_frac + self.offset) % 1.0)
        return max(0.08, base + random.uniform(-NOISE, NOISE))

    def update(self, day_frac, amb_c, now):
        if self.fault and now >= self.fault_until:
            self.fault = None
            self._spoof_frozen = None
            if self.status == 0:
                self.status = 1       # religa depois da parada

        if self.status == 0:
            self._real = dict(corrente_a=0.0, temperatura_c=round(amb_c, 1),
                              vibracao_mms=0.0, rpm=0.0, producao_pph=0.0,
                              status=0.0, consumo_kw=0.0)
            self._cur = dict(self._real)
            return self._cur

        lf = self._load_factor(day_frac)
        consumo_kw = self.p_nom_kw * lf
        producao_pph = self.prod_nom_pph * lf
        i_a = consumo_kw * 1000.0 / (math.sqrt(3) * V_LL * FP)
        temp = amb_c + K_TEMP * lf
        vib = VIB_BASE + K_VIB * lf
        rpm = RPM_NOM * (1.0 - 0.03 * lf)

        if self.fault == "superaquecimento":
            temp += 45.0              # passa dos ~90 C: alarme de temperatura
        if self.fault == "vibracao_alta":
            vib += 6.0                # passa dos ~7 mm/s: defeito de rolamento (ISO 10816)
        if self.fault == "spoof":
            # dano REAL em desenvolvimento, escondido pela leitura congelada
            el = max(0.0, now - self._spoof_start)
            temp += RAMP_TEMP * el
            vib += RAMP_VIB * el

        self._real = dict(
            corrente_a=round(i_a, 1), temperatura_c=round(temp, 1),
            vibracao_mms=round(vib, 2), rpm=round(rpm, 0),
            producao_pph=round(producao_pph, 1), status=1.0,
            consumo_kw=round(consumo_kw, 1),
        )
        # reportado: no spoof, congela numa leitura benigna (capturada no inicio)
        # enquanto o real acima segue subindo -> esconde a falha e descola do total.
        if self.fault == "spoof":
            if self._spoof_frozen is None:
                self._spoof_frozen = dict(self._real)
            self._cur = dict(self._spoof_frozen, status=1.0)
        else:
            self._cur = dict(self._real)
        return self._cur

    def inject(self, tipo, dur_s, now):
        # unauth_command e, no chao de fabrica, uma PARADA por comando nao
        # autorizado (sabotagem). A diferenca e a ORIGEM, que o server classifica
        # como evento de seguranca. Aqui tratamos o efeito fisico.
        efeito = "parada" if tipo == "unauth_command" else tipo
        self.fault = efeito
        self.fault_until = now + dur_s
        if efeito == "parada":
            self.status = 0
        if efeito == "spoof":
            self._spoof_start = now
            self._spoof_frozen = None


class Planta:
    def __init__(self, day_minutes=20.0):
        self.day_s = day_minutes * 60.0
        self.t0 = time.time()
        self.linhas = [Linha(c, i / len(LINHAS)) for i, c in enumerate(LINHAS)]

    def day_frac(self, now):
        return ((now - self.t0) % self.day_s) / self.day_s

    def update(self, now=None):
        now = now or time.time()
        df = self.day_frac(now)
        amb = TEMP_AMB + 4.0 * math.sin(2 * math.pi * df)   # ambiente oscila no dia
        ls = [l.update(df, amb, now) for l in self.linhas]   # reportado
        real = [l._real for l in self.linhas]                 # fisico
        ativos = sum(1 for r in real if r["status"] == 1.0)
        return {
            "planta": {
                "consumo_total_kw": round(sum(r["consumo_kw"] for r in real), 1),
                "producao_total_pph": round(sum(r["producao_pph"] for r in real), 1),
                "motores_ativos": float(ativos),
                "temperatura_ambiente_c": round(amb, 1),
                "hora_do_dia": round(df * 24.0, 2),
            },
            "linhas": {self.linhas[i].id: ls[i] for i in range(len(ls))},
        }

    def inject(self, linha_id, tipo, dur_s, now=None):
        now = now or time.time()
        for l in self.linhas:
            if l.id == linha_id:
                l.inject(tipo, dur_s, now)
                return True
        return False


if __name__ == "__main__":
    # auto-teste: roda o dia inteiro e confere invariantes
    p = Planta(day_minutes=20.0)
    base = p.t0
    prodmin = prodmax = None
    for step in range(0, 1200, 5):
        snap = p.update(now=base + step)
        for lid, l in snap["linhas"].items():
            assert l["producao_pph"] >= 0 and l["corrente_a"] >= 0, (lid, l)
            assert l["rpm"] >= 0 and l["temperatura_c"] >= 0, (lid, l)
            pr = snap["planta"]["producao_total_pph"]
            prodmin = pr if prodmin is None else min(prodmin, pr)
            prodmax = pr if prodmax is None else max(prodmax, pr)

    # operacao: superaquecimento sobe a temperatura
    p.inject("IN-03", "superaquecimento", 30, now=base + 100)
    s = p.update(now=base + 105)
    assert s["linhas"]["IN-03"]["temperatura_c"] > 90, "superaquecimento nao esquentou"

    # operacao: vibracao_alta sobe a vibracao
    p.inject("IN-04", "vibracao_alta", 30, now=base + 200)
    s = p.update(now=base + 205)
    assert s["linhas"]["IN-04"]["vibracao_mms"] > 7, "vibracao nao subiu"

    # operacao: parada zera a linha
    p.inject("IN-02", "parada", 30, now=base + 300)
    s = p.update(now=base + 305)
    assert s["linhas"]["IN-02"]["status"] == 0.0 and s["linhas"]["IN-02"]["producao_pph"] == 0.0, "parada nao zerou"

    # cyber: unauth_command para a linha (sabotagem)
    p2 = Planta(day_minutes=20.0); b2 = p2.t0
    p2.inject("IN-01", "unauth_command", 30, now=b2 + 10)
    s = p2.update(now=b2 + 15)
    assert s["linhas"]["IN-01"]["status"] == 0.0, "unauth_command nao parou"

    # cyber: spoof congela a leitura reportada enquanto o dano real cresce ->
    # (1) producao reportada fica estatica, (2) Sigma(linhas) descola do total,
    # (3) a temperatura/vibracao REAL sobe escondida.
    p3 = Planta(day_minutes=20.0); b3 = p3.t0
    p3.update(now=b3 + 0)
    p3.inject("IN-05", "spoof", 600, now=b3 + 5)
    p3.update(now=b3 + 10)
    linha5 = next(l for l in p3.linhas if l.id == "IN-05")
    rep1, temp_real1 = linha5._cur["producao_pph"], linha5._real["temperatura_c"]
    snap = p3.update(now=b3 + 400)
    rep2, temp_real2 = linha5._cur["producao_pph"], linha5._real["temperatura_c"]
    assert rep1 == rep2, "spoof nao congelou a producao reportada"
    assert temp_real2 > temp_real1 + 10, "spoof nao escondeu dano (temp real nao subiu)"
    soma_rep = sum(l["producao_pph"] for l in snap["linhas"].values())
    total_real = snap["planta"]["producao_total_pph"]
    assert abs(soma_rep - total_real) > 1.0, "spoof nao gerou incoerencia planta vs linhas"
    print(f"ok - superaquecimento/vibracao/parada e unauth_command batem")
    print(f"ok - spoof congela producao ({rep1:.0f} pph), esconde +{temp_real2-temp_real1:.0f} C "
          f"de dano e descola {abs(soma_rep-total_real):.0f} pph do total da planta")
    print(f"producao total variou {prodmin:.0f}..{prodmax:.0f} pph no dia")
