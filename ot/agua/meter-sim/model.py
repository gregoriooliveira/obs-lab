"""Modelo dos hidrometros do lab OT - agua (fase 2).

Puro Python, testavel sozinho. O server.py envolve isto num cliente que le o
/snapshot do process-sim, roda este modelo e publica cada hidrometro por MQTT.

Ideia: o consumo de cada SETOR ja e modelado pelo process-sim. Aqui a gente
RATEIA esse consumo entre os hidrometros daquele setor (por peso), tira a perda
fisica (vazamento de rua), poe ruido e - nas UCs marcadas - subnotifica o consumo
(furto de agua). O que o hidrometro REPORTA e o que a companhia fatura; o que o
setor ENTREGA vem do process-sim. A diferenca e a perda:

    perda_total(AG)        = consumo_setor - sum(reportado dos hidrometros)
    perda_fisica           ~ TECH_LOSS * consumo_setor   (vazamento de rua; honesta)
    perda_nao_tecnica(AG)  = sum( real_i * desvio_i )     (furto; a fraude)

O Splunk reconstroi perda_total do balanco e o Fusion aponta o setor e, pela UC,
a conta no portal (ponte com a TI).

Dois jeitos de furtar, pra ter dois sinais de deteccao diferentes:
  - desvio alto  (> 0.5)  -> hidrometro adulterado: levanta flag de TAMPER (overt)
  - desvio baixo (<= 0.5) -> subnotificacao silenciosa: so o balanco acusa (covert)
"""
import csv
import os
import random

TECH_LOSS = 0.06       # perda fisica honesta (vazamento de rua), fracao do consumo
NOISE = 0.06           # ruido por hidrometro, pra leitura nao ficar perfeita
TAMPER_DESVIO = 0.5    # acima disso, o furto tambem dispara flag de tamper


def carregar_ucs(caminho=None):
    """Le obs_lab_uc_agua.csv (a verdade compartilhada OT<->TI)."""
    caminho = caminho or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "lookups", "obs_lab_uc_agua.csv")
    with open(caminho, newline="", encoding="utf-8") as fh:
        ucs = []
        for r in csv.DictReader(fh):
            ucs.append({
                "uc": r["uc"], "conta_portal": r["conta_portal"],
                "hidrometro": r["hidrometro"], "setor": r["setor"],
                "classe": r["classe"], "cidade": r["cidade"],
                "lat": float(r["lat"]), "lon": float(r["lon"]),
                "peso": float(r["peso"]), "furto": int(r["furto"]),
                "desvio": float(r["desvio"]),
            })
        return ucs


class Hidrometro:
    def __init__(self, uc):
        self.__dict__.update(uc)
        self.volume_m3 = round(random.uniform(200, 4000), 3)  # leitura inicial
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

    def ler(self, q_real_m3h, pressao_mca, status_setor, dt_s):
        """q_real_m3h = consumo real rateado pra esta UC (antes do furto)."""
        if status_setor == 0:         # setor parado: UC sem agua
            return dict(consumo_m3h=0.0, volume_m3=round(self.volume_m3, 3),
                        pressao_mca=0.0, status=0, tamper=False,
                        perda_nao_tecnica_m3h=0.0)
        desvio = self.desvio_efetivo
        reportado = q_real_m3h * (1.0 - desvio)
        self.volume_m3 += reportado * dt_s / 3600.0   # fatura o que reporta
        return dict(
            consumo_m3h=round(reportado, 4),
            volume_m3=round(self.volume_m3, 3),
            pressao_mca=round(pressao_mca, 2), status=1, tamper=self.tamper,
            perda_nao_tecnica_m3h=round(q_real_m3h * desvio, 4),  # verdade p/ auto-teste
        )


class ParqueHidrometros:
    def __init__(self, ucs=None):
        self.hidrometros = [Hidrometro(u) for u in (ucs or carregar_ucs())]
        self._por_setor = {}
        for m in self.hidrometros:
            self._por_setor.setdefault(m.setor, []).append(m)

    def forcar(self, uc, desvio=0.5, tamper=True):
        for m in self.hidrometros:
            if m.uc == uc or m.hidrometro == uc or m.conta_portal == uc:
                m.forcado = (max(0.0, min(0.95, desvio)), tamper)
                return m.uc
        return None

    def limpar_forcado(self, uc):
        for m in self.hidrometros:
            if m.uc == uc:
                m.forcado = None
                return m.uc
        return None

    def ler(self, setores, dt_s):
        """setores: {AG-0x: {consumo_m3h, pressao_mca, status}} (do /snapshot).
        Retorna uma leitura por hidrometro."""
        out = []
        for sid, ms in self._por_setor.items():
            f = setores.get(sid, {})
            entregue = float(f.get("consumo_m3h", 0.0)) * (1.0 - TECH_LOSS)
            pressao_mca = float(f.get("pressao_mca", 0.0))
            status_setor = int(f.get("status", 1))
            soma_peso = sum(m.peso for m in ms) or 1.0
            for m in ms:
                quota = entregue * m.peso / soma_peso
                q_real = max(0.0, quota * (1.0 + random.uniform(-NOISE, NOISE)))
                r = m.ler(q_real, pressao_mca, status_setor, dt_s)
                r.update(uc=m.uc, conta_portal=m.conta_portal, hidrometro=m.hidrometro,
                         setor=m.setor, classe=m.classe,
                         cidade=m.cidade, lat=m.lat, lon=m.lon)
                out.append(r)
        return out


if __name__ == "__main__":
    # auto-teste: sem rede, so o modelo. Confere o balanco hidrico.
    ucs = carregar_ucs()
    parque = ParqueHidrometros(ucs)
    # simula um /snapshot: consumo fixo por setor pra conferir a conta
    setores = {
        "AG-01": {"consumo_m3h": 300, "pressao_mca": 35.0, "status": 1},
        "AG-02": {"consumo_m3h": 460, "pressao_mca": 30.0, "status": 1},
        "AG-03": {"consumo_m3h": 380, "pressao_mca": 37.0, "status": 1},
        "AG-04": {"consumo_m3h": 170, "pressao_mca": 33.0, "status": 1},
        "AG-05": {"consumo_m3h": 330, "pressao_mca": 34.0, "status": 1},
    }
    leituras = parque.ler(setores, dt_s=5)
    assert len(leituras) == len(ucs), "faltou hidrometro"

    # balanco por setor: entregue - reportado deve bater com fisica + furto
    for sid, f in setores.items():
        ms = [l for l in leituras if l["setor"] == sid]
        entregue = f["consumo_m3h"]
        reportado = sum(l["consumo_m3h"] for l in ms)
        nao_tec = sum(l["perda_nao_tecnica_m3h"] for l in ms)
        perda = entregue - reportado
        perda_pct = perda / entregue * 100
        furtam = [l["uc"] for l in ms if l["perda_nao_tecnica_m3h"] > 0]
        assert perda + 1e-6 >= nao_tec, (sid, perda, nao_tec)
        assert perda > 0, sid
        marca = f"  FURTO {furtam}" if furtam else ""
        print(f"{sid}: entregue {entregue:4.0f} m3/h | reportado {reportado:6.1f} | "
              f"perda {perda_pct:4.1f}% (nao-tec {nao_tec:5.1f} m3/h){marca}")

    # setor parado zera a UC
    setores["AG-02"]["status"] = 0
    z = [l for l in parque.ler(setores, 5) if l["setor"] == "AG-02"]
    assert all(l["consumo_m3h"] == 0.0 and l["status"] == 0 for l in z), "parada nao zerou"

    # forcar furto em runtime numa UC limpa
    limpa = next(m.uc for m in parque.hidrometros if not m.furto)
    parque.forcar(limpa, desvio=0.6, tamper=True)
    setores["AG-02"]["status"] = 1
    l2 = next(l for l in parque.ler(setores, 5) if l["uc"] == limpa)
    assert l2["tamper"] and l2["perda_nao_tecnica_m3h"] > 0, "forcar furto falhou"
    print(f"ok - balanco fecha; furto forcado em {limpa} (tamper on)")
