"""Modelo dos medidores de producao do lab OT industria (fase 2).

Puro Python, testavel sozinho. O server.py envolve isto num cliente que le o
/snapshot do process-sim, roda este modelo e publica cada contador por MQTT.

Ideia: a producao de cada LINHA ja e modelada pelo process-sim. Aqui a gente
RATEIA essa producao entre os contadores daquela linha (por peso), poe ruido e -
nos ativos marcados - subnotifica a producao (fraude de OEE / desvio de pecas). O
que o contador REPORTA e o que entra no MES/ERP; o que a linha PRODUZIU de fato
vem do process-sim. A diferenca e a producao desviada:

    producao_real(linha)     = producao_pph da linha (process-sim)
    producao_reportada       = sum(reportado dos contadores)
    producao_desviada(linha) = sum( real_i * desvio_i )   (a fraude de producao)

O Splunk reconstroi a producao desviada do balanco linha-contadores e aponta a
linha e, pelo ativo, a conta corporativa no portal (ponte, fraca, com a TI).

Dois jeitos de fraudar, pra ter dois sinais de deteccao:
  - desvio alto  (> 0.5)  -> adulteracao do sensor/contador: flag de TAMPER (overt)
  - desvio baixo (<= 0.5) -> subnotificacao silenciosa: so o balanco acusa (covert)
"""
import csv
import os
import random

NOISE = 0.05           # ruido por contador, pra leitura nao ficar perfeita
TAMPER_DESVIO = 0.5    # acima disso, o desvio tambem dispara flag de tamper


def carregar_ativos(caminho=None):
    """Le obs_lab_ativo_industria.csv (a verdade compartilhada OT<->TI)."""
    caminho = caminho or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "lookups",
        "obs_lab_ativo_industria.csv")
    with open(caminho, newline="", encoding="utf-8") as fh:
        ativos = []
        for r in csv.DictReader(fh):
            ativos.append({
                "ativo": r["ativo"], "conta_portal": r["conta_portal"],
                "medidor": r["medidor"], "linha": r["linha"], "tipo": r["tipo"],
                "cidade": r["cidade"], "lat": float(r["lat"]), "lon": float(r["lon"]),
                "peso": float(r["peso"]), "furto": int(r["furto"]),
                "desvio": float(r["desvio"]),
            })
        return ativos


class Medidor:
    def __init__(self, ativo):
        self.__dict__.update(ativo)
        self.pecas_total = round(random.uniform(10000, 90000), 0)  # contador inicial
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

    def ler(self, p_real_pph, status_linha, dt_s):
        """p_real_pph = producao real rateada pra este contador (antes do desvio)."""
        if status_linha == 0:            # linha parada: contador sem producao
            return dict(producao_pph=0.0, pecas_total=round(self.pecas_total, 0),
                        status=0, tamper=False, producao_real_pph=0.0,
                        producao_desviada_pph=0.0)
        desvio = self.desvio_efetivo
        reportado = p_real_pph * (1.0 - desvio)
        self.pecas_total += reportado * dt_s / 3600.0   # conta o que reporta
        return dict(
            producao_pph=round(reportado, 2),
            pecas_total=round(self.pecas_total, 0),
            status=1, tamper=self.tamper,
            producao_real_pph=round(p_real_pph, 4),          # verdade p/ auto-teste
            producao_desviada_pph=round(p_real_pph * desvio, 4),
        )


class ParqueMedidores:
    def __init__(self, ativos=None):
        self.medidores = [Medidor(a) for a in (ativos or carregar_ativos())]
        self._por_linha = {}
        for m in self.medidores:
            self._por_linha.setdefault(m.linha, []).append(m)

    def forcar(self, ativo, desvio=0.5, tamper=True):
        for m in self.medidores:
            if m.ativo == ativo or m.medidor == ativo or m.conta_portal == ativo:
                m.forcado = (max(0.0, min(0.95, desvio)), tamper)
                return m.ativo
        return None

    def limpar_forcado(self, ativo):
        for m in self.medidores:
            if m.ativo == ativo:
                m.forcado = None
                return m.ativo
        return None

    def ler(self, linhas, dt_s):
        """linhas: {IN-0x: {producao_pph, status}} (do /snapshot).
        Retorna uma leitura por contador."""
        out = []
        for lid, ms in self._por_linha.items():
            f = linhas.get(lid, {})
            producao = float(f.get("producao_pph", 0.0))
            status_linha = int(f.get("status", 1))
            soma_peso = sum(m.peso for m in ms) or 1.0
            for m in ms:
                quota = producao * m.peso / soma_peso
                p_real = max(0.0, quota * (1.0 + random.uniform(-NOISE, NOISE)))
                r = m.ler(p_real, status_linha, dt_s)
                r.update(ativo=m.ativo, conta_portal=m.conta_portal, medidor=m.medidor,
                         linha=m.linha, tipo=m.tipo, cidade=m.cidade,
                         lat=m.lat, lon=m.lon)
                out.append(r)
        return out


if __name__ == "__main__":
    # auto-teste: sem rede, so o modelo. Confere o balanco de producao.
    ativos = carregar_ativos()
    parque = ParqueMedidores(ativos)
    linhas = {
        "IN-01": {"producao_pph": 1100, "status": 1},
        "IN-02": {"producao_pph":  820, "status": 1},
        "IN-03": {"producao_pph":  540, "status": 1},
        "IN-04": {"producao_pph": 1380, "status": 1},
        "IN-05": {"producao_pph": 1850, "status": 1},
    }
    leituras = parque.ler(linhas, dt_s=5)
    assert len(leituras) == len(ativos), "faltou contador"

    # balanco por linha: real - reportado tem que bater com a producao desviada
    for lid in linhas:
        ms = [l for l in leituras if l["linha"] == lid]
        real = sum(l["producao_real_pph"] for l in ms)
        reportado = sum(l["producao_pph"] for l in ms)
        desviada = sum(l["producao_desviada_pph"] for l in ms)
        # tolerancia de 1 pph: reportado e arredondado a 2 casas por contador
        assert abs((real - reportado) - desviada) < 1.0, (lid, real, reportado, desviada)
        fraudam = [l["ativo"] for l in ms if l["producao_desviada_pph"] > 0]
        perda_pct = (desviada / real * 100) if real else 0
        marca = f"  FRAUDE {fraudam}" if fraudam else ""
        print(f"{lid}: real {real:6.0f} pph | reportado {reportado:6.0f} | "
              f"desviada {desviada:5.0f} pph ({perda_pct:4.1f}%){marca}")

    # parada zera o contador
    linhas["IN-02"]["status"] = 0
    z = [l for l in parque.ler(linhas, 5) if l["linha"] == "IN-02"]
    assert all(l["producao_pph"] == 0.0 and l["status"] == 0 for l in z), "parada nao zerou"

    # forcar fraude em runtime num ativo limpo
    linhas["IN-02"]["status"] = 1
    limpo = next(m.ativo for m in parque.medidores if not m.furto)
    parque.forcar(limpo, desvio=0.6, tamper=True)
    l2 = next(l for l in parque.ler(linhas, 5) if l["ativo"] == limpo)
    assert l2["tamper"] and l2["producao_desviada_pph"] > 0, "forcar fraude falhou"
    print(f"ok - balanco de producao fecha; fraude forcada em {limpo} (tamper on)")
