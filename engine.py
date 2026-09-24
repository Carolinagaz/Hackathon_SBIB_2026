"""
engine.py - Motor de classificação e alertas.

Para cada log recebido, na ordem em que chega ao servidor:
  1. valida o formato;
  2. descarta duplicatas (mesmo device_id + seq; o MQTT QoS 1 pode reenviar);
  3. classifica o evento em ERRO, AVISO ou RUÍDO (catálogo + contexto);
  4. abre, agrupa ou escala o alerta do par (equipamento, família);
  5. notifica quem precisa saber, respeitando o tempo de silêncio.

Janelas de tempo usam o horário do EVENTO (ts). Antecedência e prazos usam o
horário de CHEGADA (received_at), porque é quando o suporte consegue agir.

Todo o estado é POR EQUIPAMENTO: em produção, o fluxo pode ser dividido por
device_id entre várias cópias deste motor rodando em paralelo.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right, insort
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, pstdev

from catalog import (ACAO_P3, ACOES, AVISO, DRIFT_RULE, ENCERRA_APOS_USO_H, ERRO, EVENT_CATALOG,
                     NOME_EQUIP, RANK, RUIDO, TIERS, WINDOW_RULES)

CAMPOS_OBRIGATORIOS = ("v", "device_id", "seq", "ts", "code")


def parse_ts(s):
    return s if isinstance(s, datetime) else datetime.fromisoformat(s)


def num(x, casas=1):
    """Número no formato brasileiro (vírgula decimal)."""
    return f"{x:.{casas}f}".replace(".", ",")


def desc_curta(code):
    return EVENT_CATALOG[code]["desc"].split(";")[0]


def limite_desvio(mu, sigma):
    """Limite da regra R6: normal da cadeira + max(k desvios da média móvel, subida mínima)."""
    r = DRIFT_RULE
    a = r["alfa"]
    return mu + max(r["k_sigma"] * sigma * math.sqrt(a / (2 - a)), r["subida_min_a"])


@dataclass
class EstadoEquipamento:
    ligado: bool = False
    ultimo_ts: datetime | None = None
    ultimo_recebido: datetime | None = None
    uso_s: float = 0.0                              # segundos de uso (só conta ligado)
    ultimo_seq: int | None = None
    amostras: list = field(default_factory=list)   # R6: aprendendo o normal da cadeira
    mu: float | None = None
    sigma: float | None = None
    ewma: float | None = None


@dataclass
class Alerta:
    id: str
    device_id: str
    clinic_id: str
    familia: str
    tier: str
    tipo: str
    titulo: str
    regra: str
    detalhe: str
    acao: str
    pacientes_dia: int
    aberto_em: datetime
    ultima_ocorrencia: datetime
    ultima_ocorrencia_uso_s: float
    evento_gatilho: dict
    ocorrencias: list = field(default_factory=list)   # instantes de chegada de cada ocorrência
    historico: list = field(default_factory=list)     # aberto / escalado / encerrado
    primeira_notificacao: datetime | None = None
    primeira_notificacao_ts_evento: datetime | None = None
    ultima_notificacao: datetime | None = None
    n_notificacoes: int = 0
    encerrado_em: datetime | None = None

    @property
    def score(self):
        """Ordem do painel: primeiro a faixa (P1 > P2 > P3), depois pacientes/dia em risco."""
        return TIERS[self.tier]["base"] + min(99, self.pacientes_dia)


class Motor:
    def __init__(self, frota, modo_motor="desvio", limite_fixo_a=None, guardar_log=True):
        """modo_motor='desvio' usa a regra R6 (normal de cada cadeira).
        modo_motor='fixo' troca a R6 por um limiar fixo, só para comparação."""
        self.equip = {e["device_id"]: e for e in frota["equipamentos"]}
        self.clinicas = {c["clinic_id"]: c for c in frota["clinicas"]}
        self.modo_motor = modo_motor
        self.limite_fixo_a = limite_fixo_a
        self.guardar_log = guardar_log
        self.estado = defaultdict(EstadoEquipamento)
        self.vistos = set()
        self.janelas = defaultdict(list)
        self.abertos = {}                      # (device_id, familia) -> Alerta aberto
        self.abertos_do_equip = defaultdict(set)
        self.alertas = []
        self.notificacoes = []
        self.contadores = Counter()
        self.log = []

    # ------------------------------------------------------------------ fluxo
    def processar_todos(self, eventos):
        for i, ev in enumerate(eventos):
            self.processar(ev, i)
        return self

    def processar(self, ev, i=None):
        c = self.contadores
        c["recebidos"] += 1
        r = {"i": i, "device_id": ev.get("device_id"), "seq": ev.get("seq"), "code": ev.get("code"),
             "value": ev.get("value"), "ts": ev.get("ts"), "recebido": ev.get("received_at"),
             "base": None, "categoria": None, "regra": "", "efeito": "", "alerta": None}

        faltando = [k for k in CAMPOS_OBRIGATORIOS if k not in ev]
        if faltando:
            c["invalidos"] += 1
            r["efeito"] = f"descartado: faltam os campos {', '.join(faltando)}"
            return self._registrar(r)

        chave = (ev["device_id"], ev["seq"])
        if chave in self.vistos:
            c["duplicados"] += 1
            r["efeito"] = "duplicata descartada (mesmo device_id + seq já processado)"
            return self._registrar(r)
        self.vistos.add(chave)

        ts = parse_ts(ev["ts"])
        agora = parse_ts(ev["received_at"]) if ev.get("received_at") else ts
        dev = ev["device_id"]
        st = self.estado[dev]
        self._atualizar_estado(st, ev, ts, agora)
        self._encerrar_inativos(dev, st, agora)

        code = ev["code"]
        info = EVENT_CATALOG.get(code)
        if info is None:
            c["desconhecidos"] += 1
            c[RUIDO] += 1
            r.update(base=RUIDO, categoria=RUIDO, efeito="armazenado",
                     regra="código fora do catálogo: guardado para análise, sem alerta")
            return self._registrar(r)

        base, fam = info["base"], info["familia"]
        r.update(base=base, categoria=base, efeito="armazenado")
        valor = ev.get("value")

        if base == ERRO:
            r["regra"] = "R1: evento de erro"
            detalhe = desc_curta(code) + (f" ({num(valor)} {ev.get('unit', '')})" if valor is not None else "")
            self._acionar(r, ev, ts, agora, fam, "P1", "Falha ativa", "R1", info["titulo"], detalhe)

        elif base == AVISO:
            regra = WINDOW_RULES.get(code)
            n = self._contar_janela(dev, code, ts, regra["janela_h"]) if regra else 1
            if regra and n >= regra["min"]:
                r["regra"] = f"{regra['id']}: {n} eventos em {regra['janela_h']} h"
                detalhe = regra["detalhe"].format(n=n, valor=num(valor) if valor is not None else "-")
                self._acionar(r, ev, ts, agora, fam, regra["tier"], "Padrão preditivo", regra["id"],
                              regra["titulo"], detalhe)
            else:
                r["regra"] = "R0: aviso isolado"
                detalhe = desc_curta(code) + (f": {num(valor)} {ev.get('unit', '')}" if valor is not None else "")
                self._acionar(r, ev, ts, agora, fam, "P3", "Aviso isolado", "R0",
                              f"Aviso isolado: {desc_curta(code).lower()}", detalhe)

        else:  # RUÍDO: só vira alerta se formar padrão
            regra = WINDOW_RULES.get(code)
            if regra:
                n = self._contar_janela(dev, code, ts, regra["janela_h"])
                if n >= regra["min"]:
                    r["categoria"] = AVISO
                    c["promovidos"] += 1
                    r["regra"] = f"{regra['id']}: {n} eventos em {regra['janela_h']} h (ruído virou padrão)"
                    self._acionar(r, ev, ts, agora, fam, regra["tier"], "Padrão preditivo", regra["id"],
                                  regra["titulo"], regra["detalhe"].format(n=n, valor="-"))
            elif code == DRIFT_RULE["code"] and valor is not None:
                desvio, detalhe = self._desvio_motor(st, float(valor))
                if desvio:
                    r["categoria"] = AVISO
                    c["promovidos"] += 1
                    r["regra"] = f"R6: {detalhe}"
                    self._acionar(r, ev, ts, agora, fam, DRIFT_RULE["tier"], "Desvio de comportamento",
                                  "R6", DRIFT_RULE["titulo"], detalhe)

        c[r["categoria"]] += 1
        return self._registrar(r)

    # ---------------------------------------------------------- estado/tempo
    def _atualizar_estado(self, st, ev, ts, agora):
        if st.ligado and st.ultimo_ts is not None and ts > st.ultimo_ts:
            st.uso_s += (ts - st.ultimo_ts).total_seconds()
        if st.ultimo_seq is not None and ev["seq"] > st.ultimo_seq + 1:
            self.contadores["perdidos"] += ev["seq"] - st.ultimo_seq - 1  # salto no seq
        if st.ultimo_seq is None or ev["seq"] > st.ultimo_seq:
            st.ultimo_seq = ev["seq"]
        if agora - ts > timedelta(minutes=5):
            self.contadores["atrasados"] += 1  # veio do buffer depois de uma queda de conexão
        st.ligado = not ev["code"].endswith("POWER_OFF")
        if st.ultimo_ts is None or ts > st.ultimo_ts:
            st.ultimo_ts = ts
        st.ultimo_recebido = agora

    def _encerrar_inativos(self, dev, st, agora):
        """Encerra alertas sem ocorrência há ENCERRA_APOS_USO_H horas de USO.
        Noite, domingo e feriado não contam: o equipamento estava desligado."""
        for fam in list(self.abertos_do_equip[dev]):
            a = self.abertos[(dev, fam)]
            if st.uso_s - a.ultima_ocorrencia_uso_s >= ENCERRA_APOS_USO_H * 3600:
                a.encerrado_em = agora
                a.historico.append({"em": agora, "evento": f"encerrado: {ENCERRA_APOS_USO_H} h de uso sem nova ocorrência",
                                    "tier": None})
                del self.abertos[(dev, fam)]
                self.abertos_do_equip[dev].discard(fam)

    def _contar_janela(self, dev, code, ts, horas):
        lista = self.janelas[(dev, code)]
        insort(lista, ts)
        ini = bisect_left(lista, ts - timedelta(hours=horas))
        n = bisect_right(lista, ts) - ini
        if ini > 200:  # o que já saiu da janela não volta (eventos de um equipamento chegam em ordem)
            del lista[:ini]
        return n

    def _desvio_motor(self, st, valor):
        r = DRIFT_RULE
        a = r["alfa"]
        if self.modo_motor == "fixo":
            st.ewma = valor if st.ewma is None else a * valor + (1 - a) * st.ewma
            if st.ewma > self.limite_fixo_a:
                return True, f"corrente média {num(st.ewma, 2)} A acima do limiar fixo de {num(self.limite_fixo_a, 2)} A"
            return False, ""
        if st.mu is None:  # ainda aprendendo o normal desta cadeira
            st.amostras.append(valor)
            if len(st.amostras) >= r["ciclos_aprendizado"]:
                st.mu = mean(st.amostras)
                st.sigma = max(pstdev(st.amostras), r["sigma_min_a"])
                st.ewma = st.mu
                st.amostras = []
            return False, ""
        st.ewma = a * valor + (1 - a) * st.ewma
        if st.ewma > limite_desvio(st.mu, st.sigma):
            return True, (f"corrente média {num(st.ewma, 2)} A contra {num(st.mu, 2)} A no normal desta "
                          f"cadeira (+{num(100 * (st.ewma / st.mu - 1), 0)}%)")
        return False, ""

    # --------------------------------------------------------------- alertas
    def _snapshot(self, a, agora, texto, ev):
        return {"em": agora, "evento": texto, "tier": a.tier, "tipo": a.tipo, "titulo": a.titulo,
                "regra": a.regra, "detalhe": a.detalhe, "acao": a.acao, "gatilho": ev}

    def _acionar(self, r, ev, ts, agora, fam, tier, tipo, regra, titulo, detalhe):
        dev = ev["device_id"]
        st = self.estado[dev]
        chave = (dev, fam)
        acao = ACOES.get((fam, tier), ACAO_P3) if tier != "P3" else ACAO_P3
        a = self.abertos.get(chave)
        if a is None:
            eq = self.equip.get(dev, {})
            a = Alerta(id=f"A-{len(self.alertas) + 1:04d}", device_id=dev, clinic_id=eq.get("clinic_id", "?"),
                       familia=fam, tier=tier, tipo=tipo, titulo=titulo, regra=regra, detalhe=detalhe, acao=acao,
                       pacientes_dia=eq.get("pacientes_dia", 0), aberto_em=agora, ultima_ocorrencia=agora,
                       ultima_ocorrencia_uso_s=st.uso_s, evento_gatilho=ev)
            a.ocorrencias.append(agora)
            a.historico.append(self._snapshot(a, agora, f"aberto como {tier}", ev))
            self.abertos[chave] = a
            self.abertos_do_equip[dev].add(fam)
            self.alertas.append(a)
            r["efeito"] = f"abriu o alerta {a.id} ({tier})"
            if TIERS[tier]["notifica"]:
                self._notificar(a, agora, ts, "novo alerta", r["i"])
                r["efeito"] += " e notificou"
        else:
            a.ocorrencias.append(agora)
            a.ultima_ocorrencia = agora
            a.ultima_ocorrencia_uso_s = st.uso_s
            if RANK[tier] > RANK[a.tier]:  # piorou: escala o MESMO alerta
                antes = a.tier
                a.tier, a.tipo, a.titulo, a.regra, a.detalhe, a.acao = tier, tipo, titulo, regra, detalhe, acao
                a.evento_gatilho = ev
                a.historico.append(self._snapshot(a, agora, f"escalado de {antes} para {tier}", ev))
                r["efeito"] = f"escalou o alerta {a.id} de {antes} para {tier}"
                if TIERS[tier]["notifica"]:
                    self._notificar(a, agora, ts, f"escalado de {antes} para {tier}", r["i"])
                    r["efeito"] += " e notificou"
            else:
                if RANK[tier] == RANK[a.tier]:
                    a.detalhe = detalhe
                silencio = TIERS[a.tier]["silencio_h"]
                if (TIERS[a.tier]["notifica"] and a.ultima_notificacao is not None
                        and agora - a.ultima_notificacao >= timedelta(hours=silencio)):
                    self._notificar(a, agora, ts, f"lembrete: continua ocorrendo ({len(a.ocorrencias)} ocorrências)", r["i"])
                    r["efeito"] = f"agrupado em {a.id} + lembrete (passou o tempo de silêncio de {silencio} h)"
                else:
                    r["efeito"] = f"agrupado em {a.id} (ocorrência nº {len(a.ocorrencias)}, sem nova notificação)"
        r["alerta"] = a.id

    def _notificar(self, a, agora, ts_evento, motivo, i_evento):
        t = TIERS[a.tier]
        eq = self.equip.get(a.device_id, {})
        cl = self.clinicas.get(a.clinic_id, {})
        nome_eq = f"{NOME_EQUIP.get(eq.get('tipo'), 'Equipamento')} {a.device_id}"
        para = t["quem"] + (f" ({cl.get('cidade', '?')})" if a.tier == "P1" else "")
        n = {
            "id": f"N-{len(self.notificacoes) + 1:04d}", "alerta": a.id, "em": agora, "tier": a.tier,
            "motivo": motivo, "para": para, "canal": t["canal"], "prazo": t["prazo"], "evento_i": i_evento,
            "mensagem": (f"[{a.tier}] {cl.get('nome', '?')} ({cl.get('cidade', '?')}, porte {cl.get('porte', '?')}) · "
                         f"{nome_eq}: {a.titulo}. {a.detalhe}. Ação sugerida: {a.acao}"),
        }
        if a.tier == "P1" and not motivo.startswith("lembrete"):
            n["aviso_consultorio"] = (f"WhatsApp para {cl.get('nome', 'o consultório')}: \"Identificamos um problema "
                                      f"no equipamento {a.device_id}. Nosso técnico vai ligar para vocês em instantes.\"")
        self.notificacoes.append(n)
        a.n_notificacoes += 1
        a.ultima_notificacao = agora
        if a.primeira_notificacao is None:
            a.primeira_notificacao = agora
            a.primeira_notificacao_ts_evento = ts_evento

    def _registrar(self, r):
        if self.guardar_log:
            self.log.append(r)
        return r


# ---------------------------------------------------------------- exportação
def _iso(x):
    return x.isoformat(timespec="seconds") if isinstance(x, datetime) else x


def alerta_para_dict(a):
    return {
        "id": a.id, "device_id": a.device_id, "clinic_id": a.clinic_id, "familia": a.familia,
        "prioridade": a.tier, "score": a.score, "tipo": a.tipo, "titulo": a.titulo, "regra": a.regra,
        "detalhe": a.detalhe, "acao_sugerida": a.acao, "pacientes_dia": a.pacientes_dia,
        "aberto_em": _iso(a.aberto_em), "ultima_ocorrencia": _iso(a.ultima_ocorrencia),
        "encerrado_em": _iso(a.encerrado_em), "ocorrencias": len(a.ocorrencias),
        "notificacoes": a.n_notificacoes, "primeira_notificacao": _iso(a.primeira_notificacao),
        "historico": [{k: _iso(v) for k, v in h.items() if k != "gatilho"} for h in a.historico],
        "evento_gatilho": a.evento_gatilho,
    }


def notificacao_para_dict(n):
    return {k: _iso(v) for k, v in n.items()}
