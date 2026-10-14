"""Gera a tabela de Unidades Consumidoras (UC) de GAS do lab OT - a VERDADE
COMPARTILHADA entre o mundo OT (medidores de gas) e o mundo TI (web store).

A UC e a entidade-ponte do Fusion Center: a mesma pessoa que tem conta no portal
(conta_portal = cust-NNN, o customerId que a web store de TI ja usa) tem um
medidor de gas ligado a uma zona. Fraude no portal (ATO, cartao) e fraude no
campo (furto/bypass, adulteracao) encontram-se na UC.

Deterministico (seed fixa): rode em qualquer maquina e sai IGUAL, pra o meter-sim
e o lookup do Splunk nunca divergirem. Saida: obs_lab_uc_gas.csv ao lado deste.

Colunas:
  uc            UC-G0001..           identidade da unidade consumidora de gas
  conta_portal  cust-NNN             = customerId da web store (ponte com a TI)
  medidor       MEG-0001..           serie do medidor de gas
  zona          GS-01..GS-05         de qual zona a UC pende
  classe        residencial|comercial|industrial|rural
  cidade,lat,lon                     mesma geografia do lookup obs_lab_geo (mapa)
  peso          float                participacao da UC na vazao da zona
  furto         0|1                  a UC desvia gas (subnotifica consumo / bypass)
  desvio        float                fracao desviada quando furto=1 (0=nada)
"""
import csv
import os
import random

SEED = 20261014
random.seed(SEED)

# Zona -> (classe predominante, cidade do lookup geo, lat, lon, vazao nominal).
# A cidade reusa obs_lab_geo pra UC e trafego de TI cairem no mesmo mapa.
ZONES = [
    ("GS-01", "Centro",      "comercial",   "Sao Paulo",        -23.55, -46.63, 1800),
    ("GS-02", "Industrial",  "industrial",  "Campinas",         -22.91, -47.06, 3200),
    ("GS-03", "Residencial", "residencial", "Belo Horizonte",   -19.92, -43.94, 2400),
    ("GS-04", "Rural",       "rural",       "Porto Alegre",     -30.03, -51.23, 900),
    ("GS-05", "Comercial",   "comercial",   "Rio de Janeiro",   -22.91, -43.17, 1500),
]
N_UC = 55                 # medidores no total (Telegraf le todos por MQTT, tranquilo)
FURTO_ALVO = 0.09         # ~9% das UCs desviam gas (perda nao-tecnica realista)


def _n_por_zona():
    """Rateia as N_UC entre zonas na proporcao da vazao nominal."""
    total = sum(z[6] for z in ZONES)
    brutos = [N_UC * z[6] / total for z in ZONES]
    ns = [max(1, round(b)) for b in brutos]
    while sum(ns) > N_UC:
        ns[ns.index(max(ns))] -= 1
    while sum(ns) < N_UC:
        ns[ns.index(min(ns))] += 1
    return ns


def gerar():
    linhas = []
    n_por = _n_por_zona()
    uc_i = 0
    for (zid, _nome, classe, cidade, lat, lon, _p), n in zip(ZONES, n_por):
        for _ in range(n):
            uc_i += 1
            # conta no portal: UC 1..100 casa com cust-001..cust-100 da web store.
            conta = f"cust-{uc_i:03d}" if uc_i <= 100 else ""
            jlat = round(lat + random.uniform(-0.05, 0.05), 4)
            jlon = round(lon + random.uniform(-0.05, 0.05), 4)
            base = {"industrial": 3.0, "comercial": 1.6,
                    "residencial": 1.0, "rural": 0.8}[classe]
            peso = round(base * random.uniform(0.6, 1.4), 3)
            linhas.append({
                "uc": f"UC-G{uc_i:04d}", "conta_portal": conta,
                "medidor": f"MEG-{uc_i:04d}", "zona": zid,
                "classe": classe, "cidade": cidade, "lat": jlat, "lon": jlon,
                "peso": peso, "furto": 0, "desvio": 0.0,
            })
    # marca as UCs que furtam. Espalha pelas zonas (no max 2 por zona) pra o mapa
    # de perda nao-tecnica acender em varios pontos; industrial e o hotspot natural.
    k = max(1, round(len(linhas) * FURTO_ALVO))
    por_zona = {}
    alvos = set()
    ordem = linhas[:]
    random.shuffle(ordem)
    ordem.sort(key=lambda l: 0 if l["classe"] == "industrial" else 1)
    for l in ordem:
        if len(alvos) >= k:
            break
        if por_zona.get(l["zona"], 0) >= 2:
            continue
        alvos.add(id(l))
        por_zona[l["zona"]] = por_zona.get(l["zona"], 0) + 1
    for l in linhas:
        if id(l) in alvos:
            l["furto"] = 1
            l["desvio"] = round(random.uniform(0.30, 0.65), 3)
    return linhas


def main():
    linhas = gerar()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "obs_lab_uc_gas.csv")
    campos = ["uc", "conta_portal", "medidor", "zona", "classe",
              "cidade", "lat", "lon", "peso", "furto", "desvio"]
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=campos)
        w.writeheader()
        w.writerows(linhas)
    furtam = [l for l in linhas if l["furto"]]
    print(f"ok - {len(linhas)} UCs -> {out}")
    for zid, nome, *_ in ZONES:
        q = sum(1 for l in linhas if l["zona"] == zid)
        print(f"  {zid} {nome:12} {q:2} UCs")
    print(f"furtam: {len(furtam)} -> " +
          ", ".join(f"{l['uc']}({l['zona']},{l['desvio']})" for l in furtam))


if __name__ == "__main__":
    main()
