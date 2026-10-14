"""Modelo dos medidores inteligentes do lab OT (fase 2).

Puro Python, testavel sozinho. O server.py envolve isto num cliente que le o
/snapshot do process-sim, roda este modelo e publica cada medidor por MQTT.

Ideia: a carga de cada ALIMENTADOR ja e modelada pelo process-sim. Aqui a gente
RATEIA essa carga entre os medidores daquele alimentador (por peso), tira a perda
tecnica, poe ruido e - nas UCs marcadas - subnotifica o consumo (furto). O que o
medidor REPORTA e o que a distribuidora fatura; o que o alimentador ENTREGA vem
do process-sim. A diferenca e a perda:

    perda_total(AL)        = potencia_alimentador - sum(reportado dos medidores)
    perda_tecnica          ~ TECH_LOSS * potencia_alimentador   (fio, trafo; honesta)
    perda_nao_tecnica(AL)  = sum( real_i * desvio_i )           (furto; a fraude)

O Splunk reconstroi perda_total do balanco e o Fusion aponta o alimentador e,
pela UC, a conta no portal (ponte com a TI).

Dois jeitos de furtar, pra ter dois sinais de deteccao diferentes:
  - desvio alto  (> 0.5)  -> adulteracao fisica: levanta flag de TAMPER (overt)
  - desvio baixo (<= 0.5) -> subnotificacao silenciosa: so o balanco acusa (covert)
"""
import csv
import os
import random

TECH_LOSS = 0.04       # perda tecnica honesta (fio/trafo), fracao da carga
NOISE = 0.06           # ruido por medidor, pra leitura nao ficar perfeita
V_NOMINAL = 220.0      # tensao de atendimento BT (V), escalada pela tensao do AL
TAMPER_DESVIO = 0.5    # acima disso, o furto tambem dispara flag de tamper


def carregar_ucs(caminho=None):
    """Le obs_lab_uc.csv (a verdade compartilhada OT<->TI)."""
    caminho = caminho or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "lookups", "obs_lab_uc.csv")
    with open(caminho, newline="", encoding="utf-8") as fh:
        ucs = []
        for r in csv.DictReader(fh):
            ucs.append({
                "uc": r["uc"], "conta_portal": r["conta_portal"],
                "medidor": r["medidor"], "alimentador": r["alimentador"],
                "classe": r["classe"], "cidade": r["cidade"],
                "lat": float(r["lat"]), "lon": float(r["lon"]),
                "peso": float(r["peso"]), "furto": int(r["furto"]),
                "desvio": float(r["desvio"]),
            })
        return ucs


class Medidor:
    def __init__(self, uc):
        self.__dict__.update(uc)
        self.energia_kwh = round(random.uniform(1000, 9000), 3)  # leitura inicial
        self.forcado = None     # override em runtime: (desvio, tamper) | None

    @property
    def desvio_efetivo(self):
        if self.forcado is not None:
            return self.forcado[0]
        return self.desvio if self.furto else 0.0

    @property
    def tamper(self):
        if self.forcado is not None:
            return bool(self.forcado[1])
        return bool(self.furto and self.desvio > TAMPER_DESVIO)

    def ler(self, p_real_kw, tensao_pu, status_al, dt_s):
        """p_real_kw = carga real rateada pra esta UC (antes do furto)."""
        if status_al == 0:            # alimentador em trip: UC sem energia
            return dict(potencia_kw=0.0, energia_kwh=round(self.energia_kwh, 3),
                        tensao_v=0.0, status=0, tamper=False,
                        perda_nao_tecnica_kw=0.0)
        desvio = self.desvio_efetivo
        reportado = p_real_kw * (1.0 - desvio)
        self.energia_kwh += reportado * dt_s / 3600.0   # fatura o que reporta
        tensao_v = round(V_NOMINAL * max(tensao_pu, 0.5), 1)
        return dict(
            potencia_kw=round(reportado, 4),
            energia_kwh=round(self.energia_kwh, 3),
            tensao_v=tensao_v, status=1, tamper=self.tamper,
            perda_nao_tecnica_kw=round(p_real_kw * desvio, 4),  # verdade p/ auto-teste
        )


class ParqueMedidores:
    def __init__(self, ucs=None):
        self.medidores = [Medidor(u) for u in (ucs or carregar_ucs())]
        self._por_al = {}
        for m in self.medidores:
            self._por_al.setdefault(m.alimentador, []).append(m)

    def forcar(self, uc, desvio=0.5, tamper=True):
        for m in self.medidores:
            if m.uc == uc or m.medidor == uc or m.conta_portal == uc:
                m.forcado = (max(0.0, min(0.95, desvio)), tamper)
                return m.uc
        return None

    def limpar_forcado(self, uc):
        for m in self.medidores:
            if m.uc == uc:
                m.forcado = None
                return m.uc
        return None

    def ler(self, feeders, dt_s):
        """feeders: {AL-0x: {potencia_kw, tensao_pu, status}} (do /snapshot).
        Retorna uma leitura por medidor."""
        out = []
        for alid, ms in self._por_al.items():
            f = feeders.get(alid, {})
            entregue = float(f.get("potencia_kw", 0.0)) * (1.0 - TECH_LOSS)
            tensao_pu = float(f.get("tensao_pu", 1.0))
            status_al = int(f.get("status", 1))
            soma_peso = sum(m.peso for m in ms) or 1.0
            for m in ms:
                quota = entregue * m.peso / soma_peso
                p_real = max(0.0, quota * (1.0 + random.uniform(-NOISE, NOISE)))
                r = m.ler(p_real, tensao_pu, status_al, dt_s)
                r.update(uc=m.uc, conta_portal=m.conta_portal, medidor=m.medidor,
                         alimentador=m.alimentador, classe=m.classe,
                         cidade=m.cidade, lat=m.lat, lon=m.lon)
                out.append(r)
        return out


if __name__ == "__main__":
    # auto-teste: sem rede, so o modelo. Confere o balanco de energia.
    ucs = carregar_ucs()
    parque = ParqueMedidores(ucs)
    # simula um /snapshot: carga fixa por alimentador pra conferir a conta
    feeders = {
        "AL-01": {"potencia_kw": 2600, "tensao_pu": 0.96, "status": 1},
        "AL-02": {"potencia_kw": 3900, "tensao_pu": 0.94, "status": 1},
        "AL-03": {"potencia_kw": 3200, "tensao_pu": 0.97, "status": 1},
        "AL-04": {"potencia_kw": 1700, "tensao_pu": 0.95, "status": 1},
        "AL-05": {"potencia_kw": 2900, "tensao_pu": 0.95, "status": 1},
    }
    leituras = parque.ler(feeders, dt_s=5)
    assert len(leituras) == len(ucs), "faltou medidor"

    # balanco por alimentador: entregue - reportado deve bater com tecnica + furto
    for alid, f in feeders.items():
        ms = [l for l in leituras if l["alimentador"] == alid]
        entregue = f["potencia_kw"]
        reportado = sum(l["potencia_kw"] for l in ms)
        nao_tec = sum(l["perda_nao_tecnica_kw"] for l in ms)
        perda = entregue - reportado
        perda_pct = perda / entregue * 100
        furtam = [l["uc"] for l in ms if l["perda_nao_tecnica_kw"] > 0]
        # perda total >= perda nao-tecnica, e >= perda tecnica nominal (menos ruido)
        assert perda + 1e-6 >= nao_tec, (alid, perda, nao_tec)
        assert perda > 0, alid
        marca = f"  FURTO {furtam}" if furtam else ""
        print(f"{alid}: entregue {entregue:5.0f} kW | reportado {reportado:6.1f} | "
              f"perda {perda_pct:4.1f}% (nao-tec {nao_tec:5.1f} kW){marca}")

    # trip zera a UC
    feeders["AL-02"]["status"] = 0
    z = [l for l in parque.ler(feeders, 5) if l["alimentador"] == "AL-02"]
    assert all(l["potencia_kw"] == 0.0 and l["status"] == 0 for l in z), "trip nao zerou"

    # forcar furto em runtime numa UC limpa
    limpa = next(m.uc for m in parque.medidores if not m.furto)
    parque.forcar(limpa, desvio=0.6, tamper=True)
    feeders["AL-02"]["status"] = 1
    l2 = next(l for l in parque.ler(feeders, 5) if l["uc"] == limpa)
    assert l2["tamper"] and l2["perda_nao_tecnica_kw"] > 0, "forcar furto falhou"
    print(f"ok - balanco fecha; furto forcado em {limpa} (tamper on)")
