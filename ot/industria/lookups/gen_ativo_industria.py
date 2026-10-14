"""Gera a tabela de ATIVOS da planta industrial do lab OT (perfil WEG: motores e
linhas de producao). E a VERDADE COMPARTILHADA entre o mundo OT (medidores de
producao) e o mundo TI (portal corporativo).

A ponte com a TI aqui e mais FRACA que na energia: conta_portal = cust-NNN e a
conta corporativa no portal (mesmo customerId da web store de TI). A fraude que
cruza os dois mundos e insider/sabotagem - um operador cuja conta no portal foi
comprometida e que tambem adultera o contador de producao no chao de fabrica.

Deterministico (seed fixa): roda em qualquer maquina e sai IGUAL, pra o meter-sim
e o lookup do Splunk nunca divergirem. Saida: obs_lab_ativo_industria.csv.

Colunas:
  ativo         AT-0001..            identidade do ativo (contador de producao)
  conta_portal  cust-NNN             = customerId do portal (ponte com a TI)
  medidor       MED-0001..           serie do contador
  linha         IN-01..IN-05         de qual linha o ativo faz parte
  tipo          motor | linha        natureza do ativo
  cidade,lat,lon                     planta onde o ativo fica (mapa)
  peso          float                participacao do ativo na producao da linha
  furto         0|1                  o ativo subnotifica producao (fraude de OEE)
  desvio        float                fracao subnotificada quando furto=1
"""
import csv
import os
import random

SEED = 20261013
random.seed(SEED)

# Linha -> (nome, cidade da planta, lat, lon, producao nominal pph). As cidades
# reusam a geografia do lab (mesmas do obs_lab_geo da energia) pra tudo cair no
# mesmo mapa.
LINHAS = [
    ("IN-01", "Extrusao",  "Sao Paulo",      -23.55, -46.63, 1200),
    ("IN-02", "Prensa",    "Campinas",       -22.91, -47.06,  900),
    ("IN-03", "Fundicao",  "Belo Horizonte", -19.92, -43.94,  600),
    ("IN-04", "Usinagem",  "Porto Alegre",   -30.03, -51.23, 1500),
    ("IN-05", "Montagem",  "Rio de Janeiro", -22.91, -43.17, 2000),
]
N_ATIVO = 50              # contadores de producao no total
FURTO_ALVO = 0.10         # ~10% subnotificam producao (fraude de OEE)


def _n_por_linha():
    """Rateia os N_ATIVO entre linhas na proporcao da producao nominal."""
    total = sum(l[5] for l in LINHAS)
    brutos = [N_ATIVO * l[5] / total for l in LINHAS]
    ns = [max(1, round(b)) for b in brutos]
    while sum(ns) > N_ATIVO:
        ns[ns.index(max(ns))] -= 1
    while sum(ns) < N_ATIVO:
        ns[ns.index(min(ns))] += 1
    return ns


def gerar():
    linhas = []
    n_por = _n_por_linha()
    i = 0
    for (lid, _nome, cidade, lat, lon, _p), n in zip(LINHAS, n_por):
        for j in range(n):
            i += 1
            conta = f"cust-{i:03d}" if i <= 100 else ""
            # 1o ativo da linha e o contador da LINHA; os demais sao por MOTOR
            tipo = "linha" if j == 0 else "motor"
            jlat = round(lat + random.uniform(-0.04, 0.04), 4)
            jlon = round(lon + random.uniform(-0.04, 0.04), 4)
            peso = round(random.uniform(0.6, 1.6), 3)
            linhas.append({
                "ativo": f"AT-{i:04d}", "conta_portal": conta,
                "medidor": f"MED-{i:04d}", "linha": lid, "tipo": tipo,
                "cidade": cidade, "lat": jlat, "lon": jlon,
                "peso": peso, "furto": 0, "desvio": 0.0,
            })
    # marca os ativos que subnotificam producao. Espalha pelas linhas (no max 2
    # por linha) pra a perda de producao acender em varios pontos do mapa.
    k = max(1, round(len(linhas) * FURTO_ALVO))
    por_linha = {}
    alvos = set()
    ordem = linhas[:]
    random.shuffle(ordem)
    for l in ordem:
        if len(alvos) >= k:
            break
        if por_linha.get(l["linha"], 0) >= 2:
            continue
        alvos.add(id(l))
        por_linha[l["linha"]] = por_linha.get(l["linha"], 0) + 1
    for l in linhas:
        if id(l) in alvos:
            l["furto"] = 1
            l["desvio"] = round(random.uniform(0.30, 0.65), 3)
    return linhas


def main():
    linhas = gerar()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "obs_lab_ativo_industria.csv")
    campos = ["ativo", "conta_portal", "medidor", "linha", "tipo",
              "cidade", "lat", "lon", "peso", "furto", "desvio"]
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=campos)
        w.writeheader()
        w.writerows(linhas)
    furtam = [l for l in linhas if l["furto"]]
    print(f"ok - {len(linhas)} ativos -> {out}")
    for lid, nome, *_ in LINHAS:
        q = sum(1 for l in linhas if l["linha"] == lid)
        print(f"  {lid} {nome:10} {q:2} ativos")
    print(f"subnotificam producao: {len(furtam)} -> " +
          ", ".join(f"{l['ativo']}({l['linha']},{l['desvio']})" for l in furtam))


if __name__ == "__main__":
    main()
