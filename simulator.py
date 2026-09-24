"""
simulator.py - Gera uma frota fictícia e os logs de telemetria que ela enviaria.

Cada log segue o protocolo do README: um JSON por evento, com número de
sequência (seq) por equipamento e o horário do próprio evento (ts). O servidor
carimba received_at quando a mensagem chega.

As falhas são inseridas DE PROPÓSITO, com data marcada, e anotadas num gabarito
(data/ground_truth.json). O motor de regras NUNCA lê o gabarito: ele só é usado
depois, em evaluate.py, para contar acertos, falsos alarmes e antecedência.

Também simulamos o modo limpeza (a cadeira avisa quando entra e sai) e o canal
reserva (erros saem por 4G/SMS quando a internet principal cai).

Também simulamos o que atrapalha na vida real: quedas de conexão (o equipamento
guarda os eventos e reenvia depois), mensagens duplicadas, eventos perdidos,
feriado, domingo fechado e "armadilhas" que parecem defeito, mas não são.
"""
from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from catalog import AVISO, ERRO, EVENT_CATALOG

TZ = timezone(timedelta(hours=-3))          # horário de Brasília
INICIO = datetime(2026, 9, 1, tzinfo=TZ)   # começo do período simulado (terça-feira)
FERIADOS = {date(2026, 9, 7)}               # 7 de setembro: quase todos os consultórios fecham

CIDADES = ["Ribeirão Preto", "Campinas", "São Paulo", "Franca", "São Carlos", "Araraquara",
           "Bauru", "Sertãozinho"]
NOMES_A = ["Clínica", "Odonto", "Instituto", "Centro Odontológico", "Consultório"]
NOMES_B = ["Sorriso", "Vida", "Bem-Estar", "Santa Luzia", "Primavera", "Jardim", "Central",
           "São Lucas", "Horizonte", "Aurora", "Ipê", "Boa Vista"]

CORRENTE_DESARME_A = 4.2  # corrente em que a proteção do motor da cadeira desarma

# Tipos de falha: 'taxa' = fração dos equipamentos daquele tipo que falha no período
TIPOS_FALHA = {
    "compressor":          {"equip": "CADEIRA", "familia": "AR", "precursor": True,
                            "taxa": 0.065, "dur_d": (2.5, 4.0)},
    "motor":               {"equip": "CADEIRA", "familia": "MOTOR", "precursor": True,
                            "taxa": 0.055, "dur_d": (3.0, 5.0)},
    "pedal_intermitente":  {"equip": "CADEIRA", "familia": "PEDAL", "precursor": True,
                            "taxa": 0.033, "dur_d": (1.0, 2.0)},
    "pedal_subito":        {"equip": "CADEIRA", "familia": "PEDAL", "precursor": False,
                            "taxa": 0.022, "dur_d": (0, 0)},
    "tubo":                {"equip": "PANORAMICO", "familia": "TUBO", "precursor": True,
                            "taxa": 0.19, "dur_d": (2.0, 3.5)},
    "sensor_intermitente": {"equip": "PANORAMICO", "familia": "SENSOR", "precursor": True,
                            "taxa": 0.12, "dur_d": (1.0, 2.0)},
    "gerador_subito":      {"equip": "PANORAMICO", "familia": "GERADOR", "precursor": False,
                            "taxa": 0.12, "dur_d": (0, 0)},
}

ERRO_DA_FAMILIA = {
    "AR": "CHR_AIR_PRESSURE_CRITICAL", "MOTOR": "CHR_MOTOR_OVERCURRENT_TRIP",
    "PEDAL": "CHR_PEDAL_COMM_LOST", "TUBO": "PAN_TUBE_OVERHEAT_LOCK",
    "SENSOR": "PAN_SENSOR_DISCONNECTED", "GERADOR": "PAN_GENERATOR_FAULT",
}

UNIDADE = {
    "CHR_MOVE_CYCLE": "A", "CHR_MOTOR_OVERCURRENT_TRIP": "A",
    "CHR_AIR_PRESSURE_LOW": "bar", "CHR_AIR_PRESSURE_CRITICAL": "bar",
    "PAN_EXAM_OK": "°C", "PAN_TUBE_TEMP_HIGH": "°C", "PAN_TUBE_OVERHEAT_LOCK": "°C",
}


# ---------------------------------------------------------------------------
# utilidades
# ---------------------------------------------------------------------------
def _iso(dt: datetime, ms: bool = False) -> str:
    return dt.isoformat(timespec="milliseconds" if ms else "seconds")


def _min(x: float) -> timedelta:
    return timedelta(minutes=x)


def _as(d: date, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limite, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limite:
            return k
        k += 1


def nivel_firmware(code: str) -> str:
    """Nível que o PRÓPRIO firmware marca. Nossa classificação pode discordar
    (ex.: retransmissão vem como WARN, mas para nós é ruído até formar padrão)."""
    base = EVENT_CATALOG[code]["base"]
    if base == ERRO:
        return "ERROR"
    if base == AVISO or "RETRY" in code:
        return "WARN"
    return "INFO"


def _ligado_a_partir(intervalos, t, margem_min=30):
    """Primeiro instante >= t em que o equipamento está ligado (longe das bordas)."""
    m = _min(margem_min)
    for on, off in intervalos:
        if off - on <= 2 * m:
            continue
        if t < on + m:
            return on + m
        if t <= off - m:
            return t
    return None


def _tempos_poisson(rng, intervalos, taxa_h, t0=None, t1=None, passo_min=10):
    """Instantes aleatórios de eventos (processo de Poisson) SÓ enquanto o
    equipamento está ligado. taxa_h(t) = eventos por hora de uso no instante t."""
    tempos = []
    for on, off in intervalos:
        a = on if t0 is None else max(on, t0)
        b = off if t1 is None else min(off, t1)
        t = a
        while t < b:
            prox = min(t + _min(passo_min), b)
            lam = taxa_h(t) * (prox - t).total_seconds() / 3600
            for _ in range(_poisson(rng, lam)):
                tempos.append(t + (prox - t) * rng.random())
            t = prox
    tempos.sort()
    return tempos


def _rajadas(rng, intervalos, t0, t1, tamanho):
    """Rajadas de retransmissão: 1 a 3 por dia de uso, cada uma com dezenas de eventos."""
    tempos = []
    for on, off in intervalos:
        a, b = max(on, t0), min(off, t1)
        if b - a < _min(60):
            continue
        for _ in range(rng.randint(1, 3)):
            dur = _min(rng.uniform(20, 60))
            ini = a + (b - a - dur) * rng.random() if b - a > dur else a
            tempos += [ini + dur * rng.random() for _ in range(rng.randint(*tamanho))]
    return sorted(tempos)


# ---------------------------------------------------------------------------
# frota, agenda e cenários
# ---------------------------------------------------------------------------
def _frota(rng, n_clinicas):
    clinicas, equipamentos, oculto = [], [], {}
    n_cad = n_pan = 0
    for i in range(n_clinicas):
        porte = rng.choices(["pequeno", "médio", "grande"], weights=[55, 33, 12])[0]
        n_cadeiras = rng.randint(*{"pequeno": (1, 2), "médio": (3, 5), "grande": (6, 9)}[porte])
        cid = f"CLI-{i + 1:03d}"
        clinicas.append({"clinic_id": cid, "nome": f"{rng.choice(NOMES_A)} {rng.choice(NOMES_B)}",
                         "cidade": rng.choice(CIDADES), "porte": porte, "cadeiras": n_cadeiras})
        for _ in range(n_cadeiras):
            n_cad += 1
            did = f"CAD-{n_cad:04d}"
            equipamentos.append({"device_id": did, "tipo": "CADEIRA", "modelo": rng.choice(["C-100", "C-200"]),
                                 "fw": rng.choice(["3.1.0", "3.1.2", "3.2.0"]), "clinic_id": cid,
                                 "pacientes_dia": rng.randint(8, 14)})
            alta = rng.random() < 0.15  # cadeiras cujo normal já é mais alto (instalação, carga)
            oculto[did] = {"mu": round(rng.uniform(3.2, 3.4) if alta else rng.uniform(2.0, 3.0), 3),
                           "corrente_alta": alta, "mov_h": rng.uniform(2.0, 4.0),
                           "retry_h": rng.uniform(0.02, 0.08)}
        if rng.random() < {"pequeno": 0.35, "médio": 0.7, "grande": 1.0}[porte]:
            n_pan += 1
            did = f"PAN-{n_pan:04d}"
            exames = rng.randint(*{"pequeno": (4, 8), "médio": (10, 18), "grande": (20, 35)}[porte])
            equipamentos.append({"device_id": did, "tipo": "PANORAMICO", "modelo": "PX-1",
                                 "fw": rng.choice(["5.0.3", "5.1.0"]), "clinic_id": cid,
                                 "pacientes_dia": exames})
            oculto[did] = {"exames_dia": exames, "p_quente": 0.025 if porte == "grande" else 0.01,
                           "retry_h": rng.uniform(0.02, 0.06)}
    return clinicas, equipamentos, oculto


def _agenda(rng, dias):
    """Horário de funcionamento do consultório em cada dia do período."""
    agenda = []
    for i in range(dias):
        d = (INICIO + timedelta(days=i)).date()
        if d.weekday() == 6 or (d in FERIADOS and rng.random() < 0.9):
            continue  # domingo e feriado: fechado
        if d.weekday() == 5:
            if rng.random() < 0.5:
                continue  # metade não abre no sábado
            agenda.append((_as(d, 8) + _min(rng.uniform(0, 30)), _as(d, 12) + _min(rng.uniform(0, 60))))
        else:
            agenda.append((_as(d, 7, 30) + _min(rng.uniform(0, 60)), _as(d, 17, 30) + _min(rng.uniform(0, 90))))
    return agenda


def _intervalos_ligado(rng, agenda):
    out = []
    for abre, fecha in agenda:
        if rng.random() < 0.06:
            continue  # equipamento não usado nesse dia
        out.append((abre + _min(rng.uniform(0, 20)), fecha - _min(rng.uniform(0, 15))))
    return out


def _sortear_falhas(rng, equipamentos, intervalos, dias):
    fim_periodo = INICIO + timedelta(days=dias)
    livres = {"CADEIRA": [], "PANORAMICO": []}
    for e in equipamentos:
        livres[e["tipo"]].append(e["device_id"])
    total = {k: len(v) for k, v in livres.items()}
    for pool in livres.values():
        rng.shuffle(pool)
    falhas = []
    for tipo, cfg in TIPOS_FALHA.items():
        pool = livres[cfg["equip"]]
        for _ in range(max(1, round(cfg["taxa"] * total[cfg["equip"]]))):
            if not pool:
                break
            did = pool.pop()
            iv = intervalos[did]
            inicio = falha = None
            for _tentativa in range(30):
                if cfg["precursor"]:
                    dia_min = 4 if tipo == "motor" else 3  # motor: dá tempo de aprender o normal
                    inicio = _ligado_a_partir(iv, INICIO + timedelta(days=rng.uniform(dia_min, max(dia_min, dias - 6))))
                    falha = inicio and _ligado_a_partir(iv, inicio + timedelta(days=rng.uniform(*cfg["dur_d"])))
                else:
                    falha = _ligado_a_partir(iv, INICIO + timedelta(days=rng.uniform(2, max(2, dias - 2))))
                    inicio = falha
                if inicio and falha and falha < fim_periodo - timedelta(days=1):
                    break
                inicio = falha = None
            if falha is None:
                pool.insert(0, did)
                continue
            falhas.append({
                "id": f"F{len(falhas) + 1:02d}", "tipo": tipo, "familia": cfg["familia"],
                "device_id": did, "com_precursor": cfg["precursor"],
                "_inicio": inicio, "_falha": falha,
                "_ligacao": falha + _min(rng.uniform(15, 60)),        # cliente percebe e liga
                "_reparo": falha + timedelta(hours=rng.uniform(4, 30)),
            })
    return falhas


def _sortear_armadilhas(rng, equipamentos, oculto, falhas, intervalos):
    """Situações que PARECEM defeito, mas não são. Servem para medir falso alarme."""
    com_falha = {f["device_id"] for f in falhas}
    saudaveis = [e["device_id"] for e in equipamentos
                 if e["tipo"] == "CADEIRA" and e["device_id"] not in com_falha and intervalos[e["device_id"]]]
    rng.shuffle(saudaveis)
    arm = []
    for did in saudaveis[:2]:
        on, off = intervalos[did][rng.randrange(len(intervalos[did]))]
        arm.append({"device_id": did, "tipo": "limpeza do pedal",
                    "descricao": "Pedal desconectado e reconectado na limpeza do fim do dia: rajada de "
                                 "retransmissões sem defeito real (a cadeira avisa o modo limpeza).",
                    "quando": _iso(off - _min(35)), "_off": off, "_ini": off - _min(rng.uniform(25, 40)),
                    "_n": rng.randint(15, 22)})
    for did in saudaveis[2:4]:
        oculto[did]["retry_h"] = 0.8
        arm.append({"device_id": did, "tipo": "retransmissões espalhadas",
                    "descricao": "Cerca de 8 retransmissões do pedal por dia, espalhadas: incômodo, "
                                 "mas não é padrão de falha."})
    for e in equipamentos:
        did = e["device_id"]
        if e["tipo"] == "CADEIRA" and oculto[did]["corrente_alta"] and did not in com_falha:
            arm.append({"device_id": did, "tipo": "corrente naturalmente alta",
                        "descricao": f"O normal desta cadeira é {oculto[did]['mu']:.2f} A, acima da média "
                                     "da frota, sem defeito algum."})
    return arm


def _sortear_quedas(rng, equipamentos, intervalos, falhas):
    """Quedas de conexão: o equipamento continua funcionando e guarda os eventos."""
    quedas = {}
    subitas = [f for f in falhas if f["tipo"] == "gerador_subito"] or [f for f in falhas if not f["com_precursor"]]
    if subitas:  # cenário de teste A: falha súbita bem no meio de uma queda
        f = subitas[0]
        quedas[f["device_id"]] = [(f["_falha"] - _min(90), f["_falha"] + timedelta(hours=rng.uniform(3, 5)),
                                   "teste: falha súbita durante queda de conexão")]
    lentas = [f for f in falhas if f["tipo"] == "tubo" and f["device_id"] not in quedas]
    if lentas:   # cenário de teste B: queda no meio de uma degradação lenta
        f = lentas[0]
        ini = f["_inicio"] + (f["_falha"] - f["_inicio"]) * 0.3
        fim = min(ini + timedelta(hours=rng.uniform(10, 16)), f["_falha"] - timedelta(hours=2))
        if fim > ini:
            quedas[f["device_id"]] = [(ini, fim, "teste: queda no meio da degradação")]
    for e in equipamentos:
        did = e["device_id"]
        iv = intervalos[did]
        if did in quedas or not iv or rng.random() >= 0.12:
            continue
        on, off = iv[rng.randrange(len(iv))]
        ini = on + (off - on) * rng.random()
        quedas[did] = [(ini, ini + timedelta(hours=rng.uniform(1, 20)), "aleatória")]
    return quedas


# ---------------------------------------------------------------------------
# eventos de cada equipamento
# ---------------------------------------------------------------------------
def _valor_erro(rng, code):
    if code == "CHR_AIR_PRESSURE_CRITICAL":
        return round(rng.uniform(3.0, 4.2), 1)
    if code == "CHR_MOTOR_OVERCURRENT_TRIP":
        return round(rng.uniform(4.2, 4.5), 2)
    if code == "PAN_TUBE_OVERHEAT_LOCK":
        return round(rng.uniform(68, 72), 1)
    return None


def _eventos_da_falha(rng, f, iv, fracao):
    ev = []
    ini, fim = f["_inicio"], f["_falha"]
    if f["tipo"] == "compressor":
        for t in _tempos_poisson(rng, iv, lambda t: 0.05 + 0.95 * fracao(t), ini, fim):
            ev.append((t, "CHR_AIR_PRESSURE_LOW", round(5.4 - 0.8 * fracao(t) + rng.gauss(0, 0.08), 1)))
    elif f["tipo"] == "tubo":
        for t in _tempos_poisson(rng, iv, lambda t: 0.05 + 1.1 * fracao(t), ini, fim):
            ev.append((t, "PAN_TUBE_TEMP_HIGH", round(60.5 + 6 * fracao(t) + rng.gauss(0, 0.8), 1)))
    elif f["tipo"] == "pedal_intermitente":
        ev += [(t, "CHR_PEDAL_COMM_RETRY", None) for t in _rajadas(rng, iv, ini, fim, (12, 40))]
    elif f["tipo"] == "sensor_intermitente":
        ev += [(t, "PAN_SENSOR_COMM_RETRY", None) for t in _rajadas(rng, iv, ini, fim, (8, 25))]
    # motor: a degradação aparece na corrente de cada movimento (ver _eventos)

    # o erro em si, repetido enquanto o consultório tenta usar, até o reparo
    code = ERRO_DA_FAMILIA[f["familia"]]
    t = fim
    while t is not None and t < f["_reparo"]:
        ev.append((t, code, _valor_erro(rng, code)))
        t = _ligado_a_partir(iv, t + _min(rng.uniform(20, 45)), margem_min=0)
    return ev


def _eventos(rng, eq, oc, iv, falha, armadilhas, rng2):
    px = "CHR" if eq["tipo"] == "CADEIRA" else "PAN"
    ev = []
    if eq["tipo"] == "CADEIRA":
        # limpeza no fim do expediente: a cadeira avisa quando entra e sai do modo limpeza
        forcadas = {a["_off"]: a["_ini"] for a in armadilhas if a["tipo"] == "limpeza do pedal"}
        for on, off in iv:
            if off in forcadas:
                ev.append((forcadas[off] - _min(1), "CHR_CLEANING_START", None))
                ev.append((forcadas[off] + _min(22), "CHR_CLEANING_END", None))
            elif rng2.random() < 0.5:
                ini = off - _min(rng2.uniform(20, 40))
                ev.append((ini, "CHR_CLEANING_START", None))
                ev.append((ini + _min(rng2.uniform(12, 18)), "CHR_CLEANING_END", None))
    for on, off in iv:
        ev.append((on, f"{px}_POWER_ON", None))
        t = on + _min(60)
        while t < off:
            ev.append((t, f"{px}_HEARTBEAT", None))
            t += _min(60)
        ev.append((off, f"{px}_POWER_OFF", None))

    def fracao(t):  # 0 no início da degradação, 1 no momento da falha
        if not falha or not falha["_inicio"] <= t < falha["_falha"] or falha["_falha"] <= falha["_inicio"]:
            return 0.0
        return (t - falha["_inicio"]) / (falha["_falha"] - falha["_inicio"])

    def parado(t):
        return bool(falha) and falha["_falha"] <= t < falha["_reparo"]

    if eq["tipo"] == "CADEIRA":
        motor_ruim = bool(falha) and falha["tipo"] == "motor"
        for t in _tempos_poisson(rng, iv, lambda t: oc["mov_h"]):
            if motor_ruim and parado(t):
                continue
            extra = (CORRENTE_DESARME_A - oc["mu"]) * fracao(t) if motor_ruim else 0.0
            ev.append((t, "CHR_MOVE_CYCLE", round(rng.gauss(oc["mu"] + extra, 0.10), 2)))
        for t in _tempos_poisson(rng, iv, lambda t: oc["retry_h"]):
            ev.append((t, "CHR_PEDAL_COMM_RETRY", None))
        for on, off in iv:  # compressor enchendo de manhã: aviso isolado e benigno
            if rng.random() < 0.04:
                ev.append((on + _min(rng.uniform(1, 25)), "CHR_AIR_PRESSURE_LOW", round(rng.uniform(5.0, 5.4), 1)))
    else:
        tubo_ruim = bool(falha) and falha["tipo"] == "tubo"
        for t in _tempos_poisson(rng, iv, lambda t: oc["exames_dia"] / 10.0):
            if parado(t):
                continue
            if rng.random() < 0.03:
                ev.append((t, "PAN_EXAM_ABORTED_USER", None))
                continue
            aquec = 12 * fracao(t) if tubo_ruim else 0.0
            ev.append((t, "PAN_EXAM_OK", round(rng.gauss(44 + 0.2 * oc["exames_dia"] + aquec, 3), 1)))
            if rng.random() < oc["p_quente"]:  # dia puxado: tubo esquenta um pouco
                ev.append((t + timedelta(seconds=rng.uniform(5, 60)), "PAN_TUBE_TEMP_HIGH",
                           round(rng.uniform(60, 63), 1)))
        for t in _tempos_poisson(rng, iv, lambda t: oc["retry_h"]):
            ev.append((t, "PAN_SENSOR_COMM_RETRY", None))

    if falha:
        ev += _eventos_da_falha(rng, falha, iv, fracao)
    for a in armadilhas:
        if a["tipo"] == "limpeza do pedal":
            ev += [(a["_ini"] + _min(rng.uniform(0, 20)), "CHR_PEDAL_COMM_RETRY", None) for _ in range(a["_n"])]
    ev.sort(key=lambda e: e[0])
    return ev


def _emitir(rng_principal, eq, eventos, quedas, rng2):
    """Transforma eventos em mensagens do protocolo e decide QUANDO cada uma chega.
    Durante uma queda, o equipamento guarda os eventos e reenvia tudo, em ordem,
    na reconexão. MQTT QoS 1 = 'pelo menos uma vez', então às vezes chega duplicado."""
    linhas = []
    seq = rng_principal.randint(1000, 90000)
    ultimo = None
    for ts, code, value in eventos:
        seq += 1
        rng = rng2 if code.startswith("CHR_CLEANING") else rng_principal
        if rng.random() < 0.0003:
            continue  # perdido: nunca chega (o salto no seq denuncia)
        rec = ts + timedelta(seconds=rng.uniform(0.3, 4.0))
        guardado = False
        for ini, reconexao, _motivo in quedas:
            if ini <= ts < reconexao:
                rec, guardado = reconexao, True
                break
        if ultimo is not None and rec <= ultimo:
            rec = ultimo + timedelta(milliseconds=20)  # fila: um de cada vez, em ordem
        ultimo = rec
        msg = {"v": 1, "device_id": eq["device_id"], "model": eq["modelo"], "fw": eq["fw"], "seq": seq,
               "ts": _iso(ts), "code": code, "lvl": nivel_firmware(code)}
        if value is not None:
            msg["value"] = value
            msg["unit"] = UNIDADE[code]
        linhas.append((rec, {**msg, "received_at": _iso(rec, ms=True)}))
        if guardado and msg["lvl"] == "ERROR":
            # canal reserva (4G/SMS): o erro chega em ~1 min; a cópia do buffer vira duplicata
            reserva = ts + timedelta(seconds=rng2.uniform(20, 90))
            linhas.append((reserva, {**msg, "canal": "reserva", "received_at": _iso(reserva, ms=True)}))
        if rng.random() < (0.05 if guardado else 0.001):
            dup = rec + (timedelta(milliseconds=rng.uniform(1, 15)) if guardado
                         else timedelta(seconds=rng.uniform(1, 30)))
            linhas.append((dup, {**msg, "received_at": _iso(dup, ms=True)}))
    return linhas


# ---------------------------------------------------------------------------
# principal
# ---------------------------------------------------------------------------
def gerar(pasta="data", n_clinicas=30, dias=14, seed=42) -> dict:
    rng = random.Random(seed)
    rng2 = random.Random(seed + 1000)  # só para o que foi acrescentado nas correções
    clinicas, equipamentos, oculto = _frota(rng, n_clinicas)
    agenda = {c["clinic_id"]: _agenda(rng, dias) for c in clinicas}
    intervalos = {e["device_id"]: _intervalos_ligado(rng, agenda[e["clinic_id"]]) for e in equipamentos}
    falhas = _sortear_falhas(rng, equipamentos, intervalos, dias)
    armadilhas = _sortear_armadilhas(rng, equipamentos, oculto, falhas, intervalos)
    quedas = _sortear_quedas(rng, equipamentos, intervalos, falhas)

    falha_do = {f["device_id"]: f for f in falhas}
    arm_do = {}
    for a in armadilhas:
        arm_do.setdefault(a["device_id"], []).append(a)

    linhas = []
    for e in equipamentos:
        did = e["device_id"]
        evs = _eventos(rng, e, oculto[did], intervalos[did], falha_do.get(did), arm_do.get(did, []), rng2)
        # se a conexão volta com o equipamento desligado, ele só reenvia quando for ligado
        q = [(ini, _ligado_a_partir(intervalos[did], fim, margem_min=0) or fim, motivo)
             for ini, fim, motivo in quedas.get(did, [])]
        quedas[did] = q
        linhas += _emitir(rng, e, evs, q, rng2)
    linhas.sort(key=lambda x: (x[0], x[1]["device_id"], x[1]["seq"]))

    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    with open(pasta / "events.jsonl", "w", encoding="utf-8") as fh:
        for _, msg in linhas:
            fh.write(json.dumps(msg, ensure_ascii=False) + "\n")

    frota = {"clinicas": clinicas, "equipamentos": equipamentos}
    (pasta / "fleet.json").write_text(json.dumps(frota, ensure_ascii=False, indent=2), encoding="utf-8")

    clinica_do = {e["device_id"]: e["clinic_id"] for e in equipamentos}
    gabarito = {
        "aviso": "Gabarito da simulação. O motor de regras NÃO lê este arquivo; ele só é usado em evaluate.py.",
        "seed": seed, "inicio": _iso(INICIO), "fim": _iso(INICIO + timedelta(days=dias)),
        "falhas": [{
            "id": f["id"], "tipo": f["tipo"], "familia": f["familia"], "device_id": f["device_id"],
            "clinic_id": clinica_do[f["device_id"]], "com_precursor": f["com_precursor"],
            "inicio_degradacao": _iso(f["_inicio"]), "falha": _iso(f["_falha"]),
            "ligacao_cliente": _iso(f["_ligacao"]), "reparo": _iso(f["_reparo"]),
        } for f in falhas],
        "quedas_conexao": [{"device_id": did, "inicio": _iso(ini), "reconexao": _iso(rec), "motivo": motivo}
                           for did, lst in quedas.items() for ini, rec, motivo in lst],
        "armadilhas": [{k: v for k, v in a.items() if not k.startswith("_")} for a in armadilhas],
    }
    (pasta / "ground_truth.json").write_text(json.dumps(gabarito, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"mensagens": len(linhas), "equipamentos": len(equipamentos), "clinicas": len(clinicas),
            "falhas": len(falhas)}


if __name__ == "__main__":
    print(gerar())
