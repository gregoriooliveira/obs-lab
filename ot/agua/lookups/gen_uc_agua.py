"""Gera a tabela de Unidades Consumidoras (UC) de AGUA do lab OT - a VERDADE
COMPARTILHADA entre o mundo OT (hidrometros) e o mundo TI (web store / portal).

Mesma ideia da energia: a UC e a entidade-ponte do Fusion. A mesma pessoa que tem
conta no portal (conta_portal = cust-NNN, o customerId que a web store de TI ja
usa) tem um hidrometro ligado a um setor de abastecimento no OT. Fraude no portal
(ATO, cartao) e fraude no campo (furto de agua, hidrometro adulterado) encontram-se
na UC.

Deterministico (seed fixa): rode em qualquer maquina e sai IGUAL, pra o meter-sim
e o lookup do Splunk nunca divergirem. Saida: obs_lab_uc_agua.csv ao lado deste
script. Seed propria (diferente da energia): quem furta agua nao e quem furta luz.

Colunas:
  uc            UC-A0001..           identidade da unidade consumidora de agua
  conta_portal  cust-NNN             = customerId da web store (ponte com a TI)
  hidrometro    HID-0001..           serie do hidrometro
  setor         AG-01..AG-05         de qual setor de abastecimento a UC pende
  classe        residencial|comercial|industrial|rural
  cidade,lat,lon                     mesma geografia do lookup obs_lab_geo (mapa)
  peso          float                participacao da UC no consumo do setor
  furto         0|1                  a UC desvia agua (subnotifica consumo)
  desvio        float                fracao desviada quando furto=1 (0=nada)
"""
import csv
import os
import random

SEED = 20261014
random.seed(SEED)

# Setor -> (classe predominante, cidade do lookup geo, lat, lon, consumo nominal m3/h).
# A cidade reusa obs_lab_geo pra UC e trafego de TI cairem no mesmo mapa.
SETORES = [
    ("AG-01", "Centro",      "comercial",   "Sao Paulo",      -23.55, -46.63, 320),
    ("AG-02", "Industrial",  "industrial",  "Campinas",       -22.91, -47.06, 480),
    ("AG-03", "Residencial", "residencial", "Belo Horizonte", -19.92, -43.94, 400),
    ("AG-04", "Rural",       "rural",       "Porto Alegre",   -30.03, -51.23, 180),
    ("AG-05", "Comercial",   "comercial",   "Rio de Janeiro", -22.91, -43.17, 350),
]
N_UC = 56                 # hidrometros no total (Telegraf le todos por MQTT, tranquilo)
FURTO_ALVO = 0.09         # ~9% das UCs desviam agua (perda nao-tecnica realista)


def _n_por_setor():
    """Rateia as N_UC entre setores na proporcao do consumo nominal."""
    total = sum(s[6] for s in SETORES)
    brutos = [N_UC * s[6] / total for s in SETORES]
    ns = [max(1, round(b)) for b in brutos]
    while sum(ns) > N_UC:
        ns[ns.index(max(ns))] -= 1
    while sum(ns) < N_UC:
        ns[ns.index(min(ns))] += 1
    return ns


def gerar():
    linhas = []
    n_por = _n_por_setor()
    uc_i = 0
    for (sid, _nome, classe, cidade, lat, lon, _q), n in zip(SETORES, n_por):
        for _ in range(n):
            uc_i += 1
            # conta no portal: UC 1..100 casa com cust-001..cust-100 da web store.
            conta = f"cust-{uc_i:03d}" if uc_i <= 100 else ""
            jlat = round(lat + random.uniform(-0.05, 0.05), 4)
            jlon = round(lon + random.uniform(-0.05, 0.05), 4)
            # peso de consumo: industrial puxa mais, residencial/rural menos
            base = {"industrial": 3.0, "comercial": 1.6,
                    "residencial": 1.0, "rural": 0.8}[classe]
            peso = round(base * random.uniform(0.6, 1.4), 3)
            linhas.append({
                "uc": f"UC-A{uc_i:04d}", "conta_portal": conta,
                "hidrometro": f"HID-{uc_i:04d}", "setor": sid,
                "classe": classe, "cidade": cidade, "lat": jlat, "lon": jlon,
                "peso": peso, "furto": 0, "desvio": 0.0,
            })
    # marca as UCs que furtam. Espalha pelos setores (no max 2 por setor) pra o
    # mapa de perda nao-tecnica acender em varios pontos, mas deixa o industrial
    # com 2 - e o hotspot natural (furto de agua pesa mais na conta industrial).
    k = max(1, round(len(linhas) * FURTO_ALVO))
    por_setor = {}
    alvos = set()
    ordem = linhas[:]
    random.shuffle(ordem)
    ordem.sort(key=lambda l: 0 if l["classe"] == "industrial" else 1)
    for l in ordem:
        if len(alvos) >= k:
            break
        if por_setor.get(l["setor"], 0) >= 2:
            continue
        alvos.add(id(l))
        por_setor[l["setor"]] = por_setor.get(l["setor"], 0) + 1
    for l in linhas:
        if id(l) in alvos:
            l["furto"] = 1
            l["desvio"] = round(random.uniform(0.30, 0.65), 3)
    return linhas


def main():
    linhas = gerar()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "obs_lab_uc_agua.csv")
    campos = ["uc", "conta_portal", "hidrometro", "setor", "classe",
              "cidade", "lat", "lon", "peso", "furto", "desvio"]
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=campos)
        w.writeheader()
        w.writerows(linhas)
    furtam = [l for l in linhas if l["furto"]]
    print(f"ok - {len(linhas)} UCs -> {out}")
    for sid, nome, *_ in SETORES:
        q = sum(1 for l in linhas if l["setor"] == sid)
        print(f"  {sid} {nome:12} {q:2} UCs")
    print(f"furtam: {len(furtam)} -> " +
          ", ".join(f"{l['uc']}({l['setor']},{l['desvio']})" for l in furtam))


if __name__ == "__main__":
    main()
