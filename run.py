"""
run.py - Roda o sistema de ponta a ponta e imprime o resultado.

    python run.py                  # usa data/ (gera os logs se não existirem)
    python run.py --regerar        # gera logs novos (mesma seed = mesmos logs)
    python run.py --clinicas 300 --dias 5 --dados carga --sem-comparacao   # teste de volume

Saídas: output/alerts.json, output/notifications.json, output/metrics.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import simulator
from catalog import NOME_EQUIP, TIERS
from engine import Motor, alerta_para_dict, notificacao_para_dict
from evaluate import avaliar, comparar_regra_motor, funil

BASE = Path(__file__).resolve().parent


def carregar(pasta):
    pasta = Path(pasta)
    frota = json.loads((pasta / "fleet.json").read_text(encoding="utf-8"))
    gabarito = json.loads((pasta / "ground_truth.json").read_text(encoding="utf-8"))
    with open(pasta / "events.jsonl", encoding="utf-8") as fh:
        eventos = [json.loads(linha) for linha in fh if linha.strip()]
    return frota, eventos, gabarito


def rodar_tudo(pasta_dados=None, regerar=False, n_clinicas=30, dias=14, seed=42, comparar=True, guardar_log=True):
    pasta = Path(pasta_dados) if pasta_dados else BASE / "data"
    if regerar or not (pasta / "events.jsonl").exists():
        simulator.gerar(pasta, n_clinicas=n_clinicas, dias=dias, seed=seed)
    frota, eventos, gabarito = carregar(pasta)

    t0 = time.perf_counter()
    motor = Motor(frota, guardar_log=guardar_log).processar_todos(eventos)
    segundos = time.perf_counter() - t0

    av = avaliar(motor, gabarito)
    metricas = {
        "resumo": av["resumo"],
        "funil": funil(motor),
        "desempenho": {"eventos": len(eventos), "segundos": round(segundos, 3),
                       "eventos_por_segundo": round(len(eventos) / segundos) if segundos else None},
        "falhas": av["falhas"],
        "falsos_alarmes": av["falsos_alarmes"],
        "frota": {"clinicas": len(frota["clinicas"]), "equipamentos": len(frota["equipamentos"]),
                  "inicio": gabarito["inicio"], "fim": gabarito["fim"], "seed": gabarito.get("seed")},
    }
    if comparar:
        metricas["comparacao_regra_motor"] = comparar_regra_motor(eventos, frota, gabarito)
    return {"frota": frota, "eventos": eventos, "gabarito": gabarito, "motor": motor, "metricas": metricas}


def escolher_falha_pitch(metricas):
    """Cenário do pitch: o compressor avisado com mais antecedência."""
    boas = [l for l in metricas["falhas"] if l["antes_de_parar"] and l["com_precursor"]]
    comp = [l for l in boas if l["tipo"] == "compressor"] or boas
    return max(comp, key=lambda l: l["antecedencia_h"]) if comp else None


def salvar(res, pasta_saida):
    pasta = Path(pasta_saida)
    pasta.mkdir(parents=True, exist_ok=True)
    motor = res["motor"]
    (pasta / "alerts.json").write_text(json.dumps([alerta_para_dict(a) for a in motor.alertas],
                                                  ensure_ascii=False, indent=2), encoding="utf-8")
    (pasta / "notifications.json").write_text(json.dumps([notificacao_para_dict(n) for n in motor.notificacoes],
                                                         ensure_ascii=False, indent=2), encoding="utf-8")
    (pasta / "metrics.json").write_text(json.dumps(res["metricas"], ensure_ascii=False, indent=2), encoding="utf-8")


def _n(x):
    return f"{x:,}".replace(",", ".")


def _pct(x):
    return "—" if x is None else f"{100 * x:.0f}%"


def imprimir(res):
    m, motor = res["metricas"], res["motor"]
    r, f, d = m["resumo"], m["funil"], m["desempenho"]
    print("\n=== Telemetria de equipamentos · protótipo (dados SIMULADOS) ===")
    print(f"Frota: {m['frota']['clinicas']} consultórios, {m['frota']['equipamentos']} equipamentos, "
          f"{m['frota']['inicio'][:10]} a {m['frota']['fim'][:10]} (seed {m['frota']['seed']})")

    print("\nFUNIL: do log à notificação")
    print(f"  {_n(f['eventos_recebidos'])} mensagens recebidas  "
          f"({_n(f['duplicatas_descartadas'])} duplicatas descartadas, {_n(f['eventos_perdidos_detectados_pelo_seq'])} "
          f"perdidas detectadas pelo seq, {_n(f['eventos_que_chegaram_atrasados'])} chegaram atrasadas após queda de conexão)")
    print(f"  {_n(f['ruido'])} ruído | {_n(f['avisos'])} avisos | {_n(f['erros'])} erros  "
          f"({_n(f['ruido_promovido_a_aviso'])} ruídos promovidos a aviso por formarem padrão)")
    print(f"  {_n(f['alertas'])} alertas: {f['alertas_P1']} P1, {f['alertas_P2']} P2, "
          f"{f['observacoes_P3']} observações P3 (só painel)")
    print(f"  {_n(f['notificacoes'])} notificações ({f['notificacoes_novas']} novas, {f['escalonamentos']} escalonamentos, "
          f"{f['lembretes']} lembretes). Sem agrupamento seriam {_n(f['notificacoes_sem_agrupamento'])}.")

    print("\nRESULTADO CONTRA O GABARITO")
    print(f"  Falhas simuladas: {r['falhas_simuladas']} | detectadas: {r['detectadas']} | "
          f"antes da ligação do cliente: {r['detectadas_antes_da_ligacao']} ({_pct(r['recall_antes_da_ligacao'])}) | "
          f"antes de o equipamento parar: {r['antecipadas_antes_de_parar']}")
    print(f"  Precisão das notificações (P1+P2): {_pct(r['precisao'])} "
          f"({r['falsos_alarmes']} falsos alarmes em {r['alertas_que_notificaram']} alertas que acionaram alguém)")
    print(f"  Antecedência mediana, falhas com sinais precursores: {r['antecedencia_mediana_h_com_precursor']} h")
    print(f"  Falhas súbitas: alerta chega em mediana {r['antecedencia_mediana_min_subitas']} min antes da ligação")
    for l in m["falhas"]:
        if not l["antes_da_ligacao"]:
            atraso = f"{-l['antecedencia_h']:.1f} h DEPOIS da ligação" if l["detectada"] else "não detectada"
            print(f"  ! {l['falha']} {l['tipo']} em {l['device_id']}: {atraso}")

    if m["falsos_alarmes"]:
        print("\nFALSOS ALARMES (sendo honestos)")
        for x in m["falsos_alarmes"]:
            print(f"  {x['alerta']} {x['device_id']} [{x['prioridade']}/{x['regra']}] {x['titulo']}: {x['detalhe']} "
                  f"-> {x['explicacao']}")

    if "comparacao_regra_motor" in m:
        print("\nMOTOR DA CADEIRA: limiar fixo x desvio do normal de cada cadeira")
        for c in m["comparacao_regra_motor"]:
            print(f"  {c['regra']:<42} avisadas antes de travar: {c['avisadas_antes_de_travar']}/{c['falhas_de_motor']} | "
                  f"antecedência mediana: {c['antecedencia_mediana_h']} h | falsos alarmes: {c['falsos_alarmes']}")

    print(f"\nDESEMPENHO: {_n(d['eventos'])} mensagens em {d['segundos']:.2f} s "
          f"(≈ {_n(d['eventos_por_segundo'])} mensagens/s em um único processo)")

    pitch = escolher_falha_pitch(m)
    if pitch and motor.log:
        a = next(x for x in motor.alertas if x.id == pitch["alerta"])
        n = next(x for x in motor.notificacoes if x["alerta"] == a.id)
        ev = res["eventos"][n["evento_i"]]
        lg = motor.log[n["evento_i"]]
        print(f"\nPONTA A PONTA (cenário sugerido para o pitch: {pitch['falha']}, {pitch['tipo']}, "
              f"{pitch['antecedencia_h']:.0f} h antes da ligação)")
        print(f"  1. Log recebido:  {json.dumps(ev, ensure_ascii=False)}")
        print(f"  2. Classificação: {lg['base']} -> {lg['categoria']} ({lg['regra']})")
        print(f"  3. Alerta:        {lg['efeito']}")
        print(f"  4. Notificação:   para {n['para']} via {n['canal']} (prazo: {n['prazo']})")
        print(f"     \"{n['mensagem']}\"")

    agora = [a for a in motor.alertas if a.encerrado_em is None]
    agora.sort(key=lambda a: -a.score)
    print(f"\nPAINEL NO FIM DO PERÍODO: {len(agora)} alertas abertos (5 primeiros)")
    for a in agora[:5]:
        eq = motor.equip.get(a.device_id, {})
        print(f"  {TIERS[a.tier]['nome']:<16} {NOME_EQUIP.get(eq.get('tipo'), '')} {a.device_id}: {a.titulo} "
              f"({len(a.ocorrencias)} ocorrências, {a.pacientes_dia} pacientes/dia)")
    print()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Telemetria de equipamentos: simula, classifica, alerta e avalia.")
    ap.add_argument("--clinicas", type=int, default=30)
    ap.add_argument("--dias", type=int, default=14)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--regerar", action="store_true", help="gera os logs de novo")
    ap.add_argument("--dados", default=None, help="pasta dos logs (padrão: data/)")
    ap.add_argument("--saida", default=None, help="pasta dos resultados (padrão: output/)")
    ap.add_argument("--sem-comparacao", action="store_true", help="pula a comparação da regra do motor")
    args = ap.parse_args()

    res = rodar_tudo(args.dados, args.regerar, args.clinicas, args.dias, args.seed,
                     comparar=not args.sem_comparacao)
    salvar(res, args.saida or BASE / "output")
    imprimir(res)


if __name__ == "__main__":
    main()
