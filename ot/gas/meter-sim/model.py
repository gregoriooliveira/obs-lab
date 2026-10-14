"""Modelo dos medidores de gas do lab OT (fase 2).

Puro Python, testavel sozinho. O server.py envolve isto num cliente que le o
/snapshot do process-sim, roda este modelo e publica cada medidor por MQTT.

Ideia: a vazao de cada ZONA ja e modelada pelo process-sim. Aqui a gente RATEIA
essa vazao entre os medidores daquela zona (por peso), tira a perda tecnica, poe
ruido e - nas UCs marcadas - subnotifica o consumo (furto/bypass). O que o medidor
REPORTA e o que a distribuidora fatura; o que a zona ENTREGA vem do process-sim.
A diferenca e a perda:

    perda_total(zona)       = vazao_zona - sum(reportado dos medidores)
    perda_tecnica           ~ TECH_LOSS * vazao_zona   (rede; honesta)
    perda_nao_tecnica(zona) = sum( real_i * desvio_i )  (furto/bypass; a fraude)

O Splunk reconstroi perda_total do balanco e o Fusion aponta a zona e, pela UC,
a conta no portal (ponte com a TI).

Dois jeitos de furtar, pra ter dois sinais de deteccao diferentes:
  - desvio alto  (> 0.5)  -> adulteracao fisica: levanta flag de TAMPER (overt)
  - desvio baixo (<= 0.5) -> subnotificacao silenciosa: so o balanco acusa (covert)
"""
import csv
import os
import random

TECH_LOSS = 0.03       # perda tecnica honesta (rede de gas), fracao da vazao
NOISE = 0.06           # ruido por medidor, pra leitura nao ficar perfeita
TAMPER_DESVIO = 0.5    # acima disso, o furto tambem dispara flag de tamper


def carregar_ucs(caminho=None):
    """Le obs_lab_uc_gas.csv (a verdade compartilhada OT<->TI)."""
    caminho = caminho or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "lookups", "obs_lab_uc_gas.csv")
    with open(caminho, newline="", encoding="utf-8") as fh:
        ucs = []
        for r in csv.DictReader(fh):
            ucs.append({
                "uc": r["uc"], "conta_portal": r["conta_portal"],
                "medidor": r["medidor"], "zona": r["zona"],
                "classe": r["classe"], "cidade": r["cidade"],
                "lat": float(r["lat"]), "lon": float(r["lon"]),
                "peso": float(r["peso"]), "furto": int(r["furto"]),
                "desvio": float(r["desvio"]),
            })
        return ucs


class Medidor:
    def __init__(self, uc):
        self.__dict__.update(uc)
        self.volume_m3 = round(random.uniform(5000, 90000), 3)   # leitura inicial
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

    def ler(self, q_real_m3h, pressao_mbar, status_zona, dt_s):
        """q_real_m3h = vazao real rateada pra esta UC (antes do furto)."""
        if status_zona == 0:          # zona bloqueada: UC sem gas
            return dict(vazao_m3h=0.0, volume_m3=round(self.volume_m3, 3),
                        pressao_mbar=0.0, status=0, tamper=False,
                        perda_nao_tecnica_m3h=0.0)
        desvio = self.desvio_efetivo
        reportado = q_real_m3h * (1.0 - desvio)
        self.volume_m3 += reportado * dt_s / 3600.0   # fatura o que reporta
        return dict(
            vazao_m3h=round(reportado, 4),
            volume_m3=round(self.volume_m3, 3),
            pressao_mbar=round(pressao_mbar, 1), status=1, tamper=self.tamper,
            perda_nao_tecnica_m3h=round(q_real_m3h * desvio, 4),  # verdade p/ auto-teste
        )


class ParqueMedidores:
    def __init__(self, ucs=None):
        self.medidores = [Medidor(u) for u in (ucs or carregar_ucs())]
        self._por_zona = {}
        for m in self.medidores:
            self._por_zona.setdefault(m.zona, []).append(m)

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

    def ler(self, zonas, dt_s):
        """zonas: {GS-0x: {vazao_m3h, pressao_mbar, status}} (do /snapshot).
        Retorna uma leitura por medidor."""
        out = []
        for zid, ms in self._por_zona.items():
            z = zonas.get(zid, {})
            entregue = float(z.get("vazao_m3h", 0.0)) * (1.0 - TECH_LOSS)
            pressao = float(z.get("pressao_mbar", 0.0))
            status_zona = int(z.get("status", 1))
            soma_peso = sum(m.peso for m in ms) or 1.0
            for m in ms:
                quota = entregue * m.peso / soma_peso
                q_real = max(0.0, quota * (1.0 + random.uniform(-NOISE, NOISE)))
                r = m.ler(q_real, pressao, status_zona, dt_s)
                r.update(uc=m.uc, conta_portal=m.conta_portal, medidor=m.medidor,
                         zona=m.zona, classe=m.classe,
                         cidade=m.cidade, lat=m.lat, lon=m.lon)
                out.append(r)
        return out


if __name__ == "__main__":
    # auto-teste: sem rede, so o modelo. Confere o balanco de volume.
    ucs = carregar_ucs()
    parque = ParqueMedidores(ucs)
    # simula um /snapshot: vazao fixa por zona pra conferir a conta
    zonas = {
        "GS-01": {"vazao_m3h": 1600, "pressao_mbar": 290, "status": 1},
        "GS-02": {"vazao_m3h": 3000, "pressao_mbar": 285, "status": 1},
        "GS-03": {"vazao_m3h": 2200, "pressao_mbar": 292, "status": 1},
        "GS-04": {"vazao_m3h": 820,  "pressao_mbar": 294, "status": 1},
        "GS-05": {"vazao_m3h": 1400, "pressao_mbar": 291, "status": 1},
    }
    leituras = parque.ler(zonas, dt_s=5)
    assert len(leituras) == len(ucs), "faltou medidor"

    # balanco por zona: entregue - reportado deve bater com tecnica + furto
    for zid, z in zonas.items():
        ms = [l for l in leituras if l["zona"] == zid]
        entregue = z["vazao_m3h"]
        reportado = sum(l["vazao_m3h"] for l in ms)
        nao_tec = sum(l["perda_nao_tecnica_m3h"] for l in ms)
        perda = entregue - reportado
        perda_pct = perda / entregue * 100
        furtam = [l["uc"] for l in ms if l["perda_nao_tecnica_m3h"] > 0]
        assert perda + 1e-6 >= nao_tec, (zid, perda, nao_tec)
        assert perda > 0, zid
        marca = f"  FURTO {furtam}" if furtam else ""
        print(f"{zid}: entregue {entregue:5.0f} m3/h | reportado {reportado:6.1f} | "
              f"perda {perda_pct:4.1f}% (nao-tec {nao_tec:5.1f} m3/h){marca}")

    # zona bloqueada zera a UC
    zonas["GS-02"]["status"] = 0
    z0 = [l for l in parque.ler(zonas, 5) if l["zona"] == "GS-02"]
    assert all(l["vazao_m3h"] == 0.0 and l["status"] == 0 for l in z0), "bloqueio nao zerou"

    # forcar furto em runtime numa UC limpa
    limpa = next(m.uc for m in parque.medidores if not m.furto)
    parque.forcar(limpa, desvio=0.6, tamper=True)
    zonas["GS-02"]["status"] = 1
    l2 = next(l for l in parque.ler(zonas, 5) if l["uc"] == limpa)
    assert l2["tamper"] and l2["perda_nao_tecnica_m3h"] > 0, "forcar furto falhou"
    print(f"ok - balanco fecha; furto forcado em {limpa} (tamper on)")
