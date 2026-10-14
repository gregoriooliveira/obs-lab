"""Gera a tabela de Unidades Consumidoras (UC) do lab OT - a VERDADE COMPARTILHADA
entre o mundo OT (medidores) e o mundo TI (web store / portal).

A UC e a entidade-ponte do Fusion Center: a mesma pessoa que tem conta no portal
(conta_portal = cust-NNN, o customerId que a web store de TI ja usa) tem um
medidor ligado a um alimentador no OT. Fraude no portal (ATO, cartao) e fraude no
campo (furto, adulteracao) encontram-se na UC.

Deterministico (seed fixa): rode em qualquer maquina e sai IGUAL, pra o meter-sim
e o lookup do Splunk nunca divergirem. Saida: obs_lab_uc.csv ao lado deste script.

Colunas:
  uc            UC-0001..            identidade da unidade consumidora
  conta_portal  cust-NNN             = customerId da web store (ponte com a TI)
  medidor       MED-0001..           serie do medidor inteligente
  alimentador   AL-01..AL-05         de qual alimentador a UC pende
  classe        residencial|comercial|industrial|rural
  cidade,lat,lon                     mesma geografia do lookup obs_lab_geo (mapa)
  peso          float                participacao da UC na carga do alimentador
  furto         0|1                  a UC desvia energia (subnotifica consumo)
  desvio        float                fracao desviada quando furto=1 (0=nada)
"""
import csv
import os
import random

SEED = 20261013
random.seed(SEED)

# Alimentador -> (classe predominante, cidade do lookup geo, lat, lon, peso nominal).
# A cidade reusa obs_lab_geo pra UC e trafego de TI cairem no mesmo mapa.
FEEDERS = [
    ("AL-01", "Centro",      "comercial",   "Sao Paulo",        -23.55, -46.63, 2800),
    ("AL-02", "Industrial",  "industrial",  "Campinas",         -22.91, -47.06, 4200),
    ("AL-03", "Residencial", "residencial", "Belo Horizonte",   -19.92, -43.94, 3500),
    ("AL-04", "Rural",       "rural",       "Porto Alegre",     -30.03, -51.23, 1800),
    ("AL-05", "Comercial",   "comercial",   "Rio de Janeiro",   -22.91, -43.17, 3100),
]
N_UC = 60                 # medidores no total (Telegraf le todos por MQTT, tranquilo)
FURTO_ALVO = 0.09         # ~9% das UCs desviam energia (perda nao-tecnica realista)


def _n_por_alimentador():
    """Rateia as N_UC entre alimentadores na proporcao da carga nominal."""
    total = sum(f[6] for f in FEEDERS)
    brutos = [N_UC * f[6] / total for f in FEEDERS]
    ns = [max(1, round(b)) for b in brutos]
    # ajusta pra somar exatamente N_UC
    while sum(ns) > N_UC:
        ns[ns.index(max(ns))] -= 1
    while sum(ns) < N_UC:
        ns[ns.index(min(ns))] += 1
    return ns


def gerar():
    linhas = []
    n_por = _n_por_alimentador()
    uc_i = 0
    for (alid, _nome, classe, cidade, lat, lon, _p), n in zip(FEEDERS, n_por):
        for _ in range(n):
            uc_i += 1
            # conta no portal: UC 1..100 casa com cust-001..cust-100 da web store.
            conta = f"cust-{uc_i:03d}" if uc_i <= 100 else ""
            # jitter geografico pra os medidores nao empilharem no mesmo ponto
            jlat = round(lat + random.uniform(-0.05, 0.05), 4)
            jlon = round(lon + random.uniform(-0.05, 0.05), 4)
            # peso de carga: industrial puxa mais, residencial/rural menos
            base = {"industrial": 3.0, "comercial": 1.6,
                    "residencial": 1.0, "rural": 0.8}[classe]
            peso = round(base * random.uniform(0.6, 1.4), 3)
            linhas.append({
                "uc": f"UC-{uc_i:04d}", "conta_portal": conta,
                "medidor": f"MED-{uc_i:04d}", "alimentador": alid,
                "classe": classe, "cidade": cidade, "lat": jlat, "lon": jlon,
                "peso": peso, "furto": 0, "desvio": 0.0,
            })
    # marca as UCs que furtam. Espalha pelos alimentadores (no max 2 por
    # alimentador) pra o mapa de perda nao-tecnica acender em varios pontos, mas
    # deixa o industrial com 2 - e o hotspot natural (furto pesa mais na conta).
    k = max(1, round(len(linhas) * FURTO_ALVO))
    por_al = {}
    alvos = set()
    ordem = linhas[:]
    random.shuffle(ordem)
    ordem.sort(key=lambda l: 0 if l["classe"] == "industrial" else 1)
    for l in ordem:
        if len(alvos) >= k:
            break
        if por_al.get(l["alimentador"], 0) >= 2:
            continue
        alvos.add(id(l))
        por_al[l["alimentador"]] = por_al.get(l["alimentador"], 0) + 1
    for l in linhas:
        if id(l) in alvos:
            l["furto"] = 1
            l["desvio"] = round(random.uniform(0.30, 0.65), 3)
    return linhas


def main():
    linhas = gerar()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "obs_lab_uc.csv")
    campos = ["uc", "conta_portal", "medidor", "alimentador", "classe",
              "cidade", "lat", "lon", "peso", "furto", "desvio"]
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=campos)
        w.writeheader()
        w.writerows(linhas)
    furtam = [l for l in linhas if l["furto"]]
    print(f"ok - {len(linhas)} UCs -> {out}")
    for alid, nome, *_ in FEEDERS:
        q = sum(1 for l in linhas if l["alimentador"] == alid)
        print(f"  {alid} {nome:12} {q:2} UCs")
    print(f"furtam: {len(furtam)} -> " +
          ", ".join(f"{l['uc']}({l['alimentador']},{l['desvio']})" for l in furtam))


if __name__ == "__main__":
    main()
