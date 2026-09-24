"""
evaluate.py - Mede o sistema contra o gabarito da simulação.

  * Recall: das falhas que existiram, quantas o sistema pegou, e quantas ANTES
    da ligação do cliente?
  * Precisão: dos alertas que acionaram alguém (P1/P2), quantos eram problema real?
  * Antecedência: quanto tempo antes da ligação o suporte ficou sabendo?
  * Agrupamento: quantas notificações sairiam sem agrupamento e tempo de silêncio?

Um alerta conta como ACERTO se é do mesmo equipamento e da mesma família da
falha e se o evento que o fez notificar aconteceu entre 2 h antes do início da
degradação e o reparo. Qualquer outro alerta que acionou alguém é FALSO ALARME.
Observações P3 não acionam ninguém; são contadas à parte.
"""
from __future__ import annotations

import random
from collections import Counter
from datetime import timedelta
from statistics import median

from catalog import AVISO, ERRO, RUIDO, TIERS
from engine import Motor, parse_ts

MARGEM = timedelta(hours=2)


def _casa(a, f):
    """O alerta a corresponde à falha f do gabarito?"""
    return (a.device_id == f["device_id"] and a.familia == f["familia"]
            and parse_ts(f["inicio_degradacao"]) - MARGEM <= a.primeira_notificacao_ts_evento <= parse_ts(f["reparo"]))


class TecnicoSimulado:
    """Simula o retorno do técnico ao fechar o chamado, de 2 a 24 h depois do
    alerta: ele liga ou vai ao consultório e descobre se o problema era real.
    Usa o gabarito porque representa o que o técnico vê em campo; o motor só
    recebe o veredito, e só depois desse tempo. Em produção, vem do sistema de chamados."""

    def __init__(self, gabarito, seed=7):
        self.falhas = gabarito["falhas"]
        self.rng = random.Random(seed)

    def veredito(self, a):
        real = any(_casa(a, f) for f in self.falhas)
        return real, a.primeira_notificacao + timedelta(hours=self.rng.uniform(2, 24))


def _primeiro_estado_notificado(a):
    for h in a.historico:
        if h.get("tier") and TIERS[h["tier"]]["notifica"]:
            return h
    return None


def avaliar(motor, gabarito, familia=None):
    falhas = [f for f in gabarito["falhas"] if familia is None or f["familia"] == familia]
    notificados = [a for a in motor.alertas
                   if a.primeira_notificacao is not None and (familia is None or a.familia == familia)]
    armadilhas = {}
    for arm in gabarito.get("armadilhas", []):
        armadilhas.setdefault(arm["device_id"], []).append(arm["tipo"])

    acertos, linhas = set(), []
    for f in falhas:
        ini, fim = parse_ts(f["inicio_degradacao"]) - MARGEM, parse_ts(f["reparo"])
        cands = [a for a in notificados if _casa(a, f)]
        acertos.update(a.id for a in cands)
        linha = {"falha": f["id"], "tipo": f["tipo"], "device_id": f["device_id"], "com_precursor": f["com_precursor"],
                 "detectada": False, "antes_da_ligacao": False, "antes_de_parar": False, "antecedencia_h": None,
                 "alerta": None, "prioridade_inicial": None, "regra": None, "notificado_em": None}
        if cands:
            a = min(cands, key=lambda x: x.primeira_notificacao)
            h = _primeiro_estado_notificado(a)
            ante = (parse_ts(f["ligacao_cliente"]) - a.primeira_notificacao).total_seconds() / 3600
            linha.update(detectada=True, antes_da_ligacao=ante > 0,
                         antes_de_parar=a.primeira_notificacao < parse_ts(f["falha"]),
                         antecedencia_h=round(ante, 2), alerta=a.id, prioridade_inicial=h["tier"], regra=h["regra"],
                         notificado_em=a.primeira_notificacao.isoformat(timespec="seconds"))
        linhas.append(linha)

    falsos = [a for a in notificados if a.id not in acertos]
    com_prec = [l["antecedencia_h"] for l in linhas if l["com_precursor"] and l["antes_da_ligacao"]]
    subitas = [l["antecedencia_h"] for l in linhas if not l["com_precursor"] and l["antes_da_ligacao"]]
    n = len(falhas)
    antes = sum(l["antes_da_ligacao"] for l in linhas)
    resumo = {
        "falhas_simuladas": n,
        "detectadas": sum(l["detectada"] for l in linhas),
        "detectadas_antes_da_ligacao": antes,
        "antecipadas_antes_de_parar": sum(l["antes_de_parar"] for l in linhas),
        "recall_antes_da_ligacao": antes / n if n else None,
        "alertas_que_notificaram": len(notificados),
        "falsos_alarmes": len(falsos),
        "precisao": (len(notificados) - len(falsos)) / len(notificados) if notificados else None,
        "antecedencia_mediana_h_com_precursor": round(median(com_prec), 1) if com_prec else None,
        "antecedencia_mediana_min_subitas": round(median(subitas) * 60) if subitas else None,
    }
    falsos_desc = []
    for a in falsos:
        h = _primeiro_estado_notificado(a)
        falsos_desc.append({
            "alerta": a.id, "device_id": a.device_id, "familia": a.familia, "prioridade": h["tier"],
            "regra": h["regra"], "titulo": h["titulo"], "detalhe": h["detalhe"],
            "notificado_em": a.primeira_notificacao.isoformat(timespec="seconds"),
            "explicacao": "; ".join(armadilhas.get(a.device_id, [])) or "sem falha real no gabarito",
        })
    return {"resumo": resumo, "falhas": linhas, "falsos_alarmes": falsos_desc}


def funil(motor):
    c = motor.contadores
    por_tier = Counter(a.tier for a in motor.alertas)
    motivos = Counter(n["motivo"].split(" ")[0].rstrip(":") for n in motor.notificacoes)
    so_p3 = [a for a in motor.alertas if a.tier == "P3"]
    return {
        "eventos_recebidos": c["recebidos"],
        "duplicatas_descartadas": c["duplicados"],
        "eventos_perdidos_detectados_pelo_seq": c["perdidos"],
        "eventos_que_chegaram_atrasados": c["atrasados"],
        "invalidos": c["invalidos"],
        "ruido": c[RUIDO], "avisos": c[AVISO], "erros": c[ERRO],
        "ruido_promovido_a_aviso": c["promovidos"],
        "alertas": len(motor.alertas),
        "alertas_P1": por_tier["P1"], "alertas_P2": por_tier["P2"], "observacoes_P3": por_tier["P3"],
        "observacoes_P3_que_nunca_notificaram": len(so_p3),
        "notificacoes": len(motor.notificacoes),
        "notificacoes_novas": motivos["novo"], "escalonamentos": motivos["escalado"], "lembretes": motivos["lembrete"],
        "notificacoes_sem_agrupamento": c[AVISO] + c[ERRO],
        "retransmissoes_ignoradas_na_limpeza": c["ignorados_na_limpeza"],
        "erros_pelo_canal_reserva": c["via_canal_reserva"],
        "vereditos_confirmados": c["vereditos_confirmados"],
        "vereditos_falso_alarme": c["vereditos_falsos"],
        "ocorrencias_rebaixadas_por_confiabilidade": c["rebaixados_por_confiabilidade"],
    }


def confiabilidade(motor):
    """Acertos de cada regra segundo o retorno dos técnicos."""
    return [{"regra": r, "confirmados": c["acertos"], "vereditos": c["vereditos"],
             "em_revisao": motor._em_revisao(r)} for r, c in sorted(motor.confiab.items())]


def comparar_versoes(eventos, frota, gabarito, motor_corrigido):
    """Mesmos logs: a primeira versão (sem correções) contra a atual."""
    v1 = Motor(frota, correcoes=False, guardar_log=False).processar_todos(eventos)
    saida = []
    for nome, m in [("Primeira versão", v1), ("Com as correções", motor_corrigido)]:
        av, fu = avaliar(m, gabarito), funil(m)
        r = av["resumo"]
        saida.append({
            "versao": nome, "falsos_alarmes": r["falsos_alarmes"], "precisao": r["precisao"],
            "antes_de_parar": r["antecipadas_antes_de_parar"], "antes_da_ligacao": r["detectadas_antes_da_ligacao"],
            "falhas": r["falhas_simuladas"], "antecedencia_mediana_h": r["antecedencia_mediana_h_com_precursor"],
            "notificacoes": fu["notificacoes"], "lembretes": fu["lembretes"],
        })
    return saida


def comparar_regra_motor(eventos, frota, gabarito):
    """Mesmos logs, três jeitos de vigiar o motor da cadeira."""
    cenarios = [("Limiar fixo: média > 3,3 A", "fixo", 3.3),
                ("Limiar fixo: média > 3,6 A", "fixo", 3.6),
                ("Desvio do normal de cada cadeira (R6)", "desvio", None)]
    saida = []
    for nome, modo, limite in cenarios:
        m = Motor(frota, modo_motor=modo, limite_fixo_a=limite, guardar_log=False).processar_todos(eventos)
        av = avaliar(m, gabarito, familia="MOTOR")
        det = [l["antecedencia_h"] for l in av["falhas"] if l["detectada"]]
        saida.append({
            "regra": nome,
            "falhas_de_motor": av["resumo"]["falhas_simuladas"],
            "avisadas_antes_de_travar": av["resumo"]["antecipadas_antes_de_parar"],
            "antecedencia_mediana_h": round(median(det), 1) if det else None,
            "falsos_alarmes": av["resumo"]["falsos_alarmes"],
        })
    return saida
