"""
dashboard.py - Tela de alertas do protótipo.

    streamlit run dashboard.py

Responde "quais equipamentos eu preciso cuidar agora?" e mostra, de ponta a
ponta, um log entrando e virando alerta. Tudo roda sobre os logs SIMULADOS.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import timedelta

import altair as alt
import pandas as pd
import streamlit as st

from catalog import (CATEGORIAS, CONFIABILIDADE, DRIFT_RULE, ENCERRA_APOS_USO_H, EVENT_CATALOG, FAMILIAS,
                     LIMPEZA_MAX_H, NOME_EQUIP, SEM_SINAL_H, TIERS, WINDOW_RULES)
from engine import limite_desvio, parse_ts
from run import escolher_falha_pitch, rodar_tudo
from simulator import TZ

st.set_page_config(page_title="Telemetria · painel do suporte", page_icon="🦷", layout="wide")

CORES_CAT = alt.Scale(domain=["RUÍDO", "AVISO", "ERRO"], range=["#94a3b8", "#f59e0b", "#dc2626"])
MARCOS = ["Início da degradação (gabarito)", "Suporte avisado", "Equipamento parou", "Cliente ligaria"]
CORES_MARCOS = alt.Scale(domain=MARCOS, range=["#64748b", "#16a34a", "#dc2626", "#1e293b"])
NOME_FALHA = {
    "compressor": "Compressor degradando", "motor": "Desgaste do motor de elevação",
    "pedal_intermitente": "Pedal com mau contato", "pedal_subito": "Pedal parou de repente",
    "tubo": "Arrefecimento do tubo degradando", "sensor_intermitente": "Sensor com mau contato",
    "gerador_subito": "Gerador parou de repente",
}


def local(dt):
    """Converte para horário de Brasília sem fuso (o Streamlit lida melhor assim)."""
    if dt is None or (isinstance(dt, float) and pd.isna(dt)):
        return None
    if isinstance(dt, str):
        dt = parse_ts(dt)
    return dt.astimezone(TZ).replace(tzinfo=None)


def duracao(td) -> str:
    h = td.total_seconds() / 3600
    if h < 1:
        return f"{max(0, round(h * 60))} min"
    if h < 48:
        return f"{h:.0f} h"
    return f"{h / 24:.1f} dias".replace(".", ",")


def hm(dt) -> str:
    return dt.strftime("%d/%m %H:%M") if dt is not None else "—"


def grafico(ch):
    try:
        st.altair_chart(ch, width="stretch")
    except TypeError:  # versões antigas do Streamlit
        st.altair_chart(ch, use_container_width=True)


@st.cache_resource(show_spinner="Gerando e processando os logs simulados (só na primeira vez)…")
def carregar():
    d = rodar_tudo()
    motor = d["motor"]
    alertas = [{"a": a, "abre": local(a.aberto_em), "fecha": local(a.encerrado_em),
                "hist": [(local(h["em"]), h) for h in a.historico],
                "occ": [local(t) for t in a.ocorrencias]} for a in motor.alertas]
    log = pd.DataFrame(motor.log)
    log["recebido"] = pd.to_datetime([local(x) for x in log["recebido"]])
    log["ts"] = pd.to_datetime([local(x) for x in log["ts"]])
    unicos = log[~log["efeito"].str.startswith("duplicata") & ~log["efeito"].str.startswith("ignorado")]
    ultimo = {}
    for did, g in unicos.sort_values("recebido").groupby("device_id", sort=False):
        ultimo[did] = ([t.to_pydatetime() for t in g["recebido"]], list(g["code"]))
    notif = pd.DataFrame(motor.notificacoes)
    notif["em"] = pd.to_datetime([local(x) for x in notif["em"]])
    pitch = escolher_falha_pitch(d["metricas"])
    i_pitch, t_padrao = 0, local(d["gabarito"]["inicio"]) + timedelta(days=7, hours=10)
    if pitch:
        n0 = next(n for n in motor.notificacoes if n["alerta"] == pitch["alerta"])
        i_pitch = n0["evento_i"]
        t_padrao = local(n0["em"]) + timedelta(minutes=30)
    return d, {"alertas": alertas, "log": log, "ultimo": ultimo, "notif": notif, "pitch": pitch,
               "i_pitch": i_pitch, "t_padrao": t_padrao}


d, P = carregar()
motor, gab, frota, met = d["motor"], d["gabarito"], d["frota"], d["metricas"]
EQ = {e["device_id"]: e for e in frota["equipamentos"]}
CL = {c["clinic_id"]: c for c in frota["clinicas"]}
AV_FALHA = {l["falha"]: l for l in met["falhas"]}


def nome_eq(did):
    return f"{NOME_EQUIP[EQ[did]['tipo']]} {did}"


def nome_cl(did):
    c = CL[EQ[did]["clinic_id"]]
    return f"{c['nome']} · {c['cidade']}"


def abertos_em(T):
    linhas = []
    for x in P["alertas"]:
        if x["abre"] > T or (x["fecha"] is not None and x["fecha"] <= T):
            continue
        estado = None
        for t, h in x["hist"]:
            if t > T:
                break
            if h.get("tier"):
                estado = h
        if estado is None:
            continue
        a, tier = x["a"], estado["tier"]
        linhas.append({
            "id": a.id, "score": TIERS[tier]["base"] + min(99, a.pacientes_dia),
            "Prioridade": f"{TIERS[tier]['icone']} {TIERS[tier]['nome']}",
            "Equipamento": nome_eq(a.device_id), "Consultório": nome_cl(a.device_id),
            "Porte": CL[a.clinic_id]["porte"], "Problema": estado["titulo"], "Tipo": estado["tipo"],
            "Detalhe": estado["detalhe"], "Aberto há": duracao(T - x["abre"]),
            "Ocorrências": bisect_right(x["occ"], T), "Pacientes/dia": a.pacientes_dia,
            "Ação sugerida": estado["acao"], "Quem foi acionado": f"{TIERS[tier]['quem']} ({TIERS[tier]['canal']})",
        })
    df = pd.DataFrame(linhas)
    return df.sort_values(["score", "Ocorrências"], ascending=False) if not df.empty else df


def sem_sinal_em(T):
    linhas = []
    for did, (tempos, codigos) in P["ultimo"].items():
        k = bisect_right(tempos, T) - 1
        if k < 0 or codigos[k].endswith("POWER_OFF"):
            continue
        silencio = T - tempos[k]
        if silencio >= timedelta(hours=SEM_SINAL_H):
            linhas.append({"Equipamento": nome_eq(did), "Consultório": nome_cl(did),
                           "Último contato": hm(tempos[k]), "Sem sinal há": duracao(silencio)})
    return pd.DataFrame(linhas)


st.title("Telemetria de equipamentos · painel do suporte")
st.caption("Protótipo do Hackathon Alliage, Desafio 02. Frota, logs e falhas são simulados; "
           "códigos de evento e ações sugeridas são ilustrativos. Detalhes no README.")

aba_agora, aba_pitch, aba_met, aba_logs, aba_regras = st.tabs(
    ["🚨 Agora", "🎬 Cenário do pitch", "📊 Métricas", "🔎 Logs ponta a ponta", "📐 Regras e protocolo"])

# ============================================================== AGORA
with aba_agora:
    ini, fim = local(gab["inicio"]), local(gab["fim"])
    T = st.slider("Relógio simulado: arraste para ver o painel em qualquer momento do período",
                  min_value=ini, max_value=fim, value=min(max(P["t_padrao"], ini), fim),
                  step=timedelta(minutes=30), format="DD/MM HH:mm")
    abertos, sem_sinal = abertos_em(T), sem_sinal_em(T)
    conta = abertos["Prioridade"].str[2:4].value_counts() if not abertos.empty else {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🔴 P1 · agir agora", int(conta.get("P1", 0)))
    c2.metric("🟠 P2 · agir hoje", int(conta.get("P2", 0)))
    c3.metric("🟡 P3 · observar", int(conta.get("P3", 0)))
    c4.metric("📡 Sem sinal", len(sem_sinal), help=f"Ligado e sem mandar nada há {SEM_SINAL_H} h ou mais")

    st.subheader(f"Quem precisa de cuidado em {hm(T)}")
    if abertos.empty:
        st.success("Nenhum alerta aberto neste momento. Arraste o relógio para outro dia.")
    else:
        st.dataframe(abertos, hide_index=True,
                     column_order=["Prioridade", "Equipamento", "Consultório", "Problema", "Detalhe", "Aberto há",
                                   "Ocorrências", "Pacientes/dia", "Ação sugerida", "Quem foi acionado", "Porte",
                                   "Tipo"])
        st.caption("Ordem: primeiro o que parou (P1), depois o que vai parar (P2), depois o que merece "
                   "olhar (P3). Dentro de cada faixa, quem tem mais pacientes por dia dependendo do equipamento.")

        rotulos = {r["id"]: f"{r['Prioridade'][:2]} {r['Equipamento']}: {r['Problema']}" for _, r in abertos.iterrows()}
        escolha = st.selectbox("Abrir um alerta", list(rotulos), format_func=rotulos.get)
        x = next(x for x in P["alertas"] if x["a"].id == escolha)
        a = x["a"]
        estado = [h for t, h in x["hist"] if t <= T and h.get("tier")][-1]
        tier = TIERS[estado["tier"]]
        e1, e2 = st.columns([3, 2])
        with e1:
            st.markdown(f"#### {tier['icone']} {estado['titulo']}")
            st.write(f"**{nome_eq(a.device_id)}** em {nome_cl(a.device_id)} (porte {CL[a.clinic_id]['porte']})")
            st.write(f"**O que o sistema viu:** {estado['detalhe']} · regra {estado['regra']}")
            st.write(f"**Ação sugerida:** {estado['acao']}")
            st.write(f"**Quem é acionado:** {tier['quem']} por {tier['canal']}; prazo: {tier['prazo']}. {tier['faz']}")
            hist = pd.DataFrame([{"Quando": hm(t), "O que aconteceu": h["evento"], "Regra": h.get("regra", ""),
                                  "Detalhe": h.get("detalhe", "")} for t, h in x["hist"] if t <= T])
            st.dataframe(hist, hide_index=True)
            ns = P["notif"][(P["notif"]["alerta"] == a.id) & (P["notif"]["em"] <= T)]
            for _, n in ns.iterrows():
                st.info(f"**{hm(n['em'])} · {n['motivo']}** → {n['para']} ({n['canal']})\n\n{n['mensagem']}")
                if isinstance(n.get("aviso_consultorio"), str):
                    st.caption(n["aviso_consultorio"])
        with e2:
            st.markdown("**Log que levou o alerta a este estado**")
            st.json(estado["gatilho"])

    st.subheader("Equipamentos sem sinal")
    if sem_sinal.empty:
        st.write("Todos os equipamentos ligados estão se comunicando. Desligados normalmente não entram aqui.")
    else:
        st.dataframe(sem_sinal, hide_index=True)
        st.caption("Os eventos continuam guardados no equipamento e chegam quando a conexão voltar.")

# ============================================================== PITCH
with aba_pitch:
    falhas = gab["falhas"]
    idx = next((i for i, f in enumerate(falhas) if P["pitch"] and f["id"] == P["pitch"]["falha"]), 0)

    def rotulo_falha(f):
        l = AV_FALHA[f["id"]]
        if not l["detectada"]:
            s = "não detectada"
        elif l["antecedencia_h"] >= 1:
            s = f"avisado {l['antecedencia_h']:.0f} h antes da ligação"
        elif l["antecedencia_h"] > 0:
            s = f"avisado {l['antecedencia_h'] * 60:.0f} min antes da ligação"
        else:
            s = f"avisado {-l['antecedencia_h']:.1f} h DEPOIS da ligação"
        return f"{f['id']} · {NOME_FALHA[f['tipo']]} · {f['device_id']} · {s}"

    f = st.selectbox("Falha simulada", falhas, index=idx, format_func=rotulo_falha)
    l = AV_FALHA[f["id"]]
    did = f["device_id"]
    t_ini, t_falha, t_lig, t_rep = (local(f[k]) for k in ("inicio_degradacao", "falha", "ligacao_cliente", "reparo"))
    t_not = local(l["notificado_em"]) if l["detectada"] else None

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Suporte avisado", hm(t_not))
    k2.metric("Equipamento parou", hm(t_falha))
    k3.metric("Cliente ligaria", hm(t_lig))
    if l["detectada"]:
        ante = l["antecedencia_h"]
        k4.metric("Antecedência", duracao(timedelta(hours=abs(ante))) + (" antes" if ante > 0 else " depois"))
    st.write(f"**{NOME_FALHA[f['tipo']]}** em {nome_eq(did)}, {nome_cl(did)}. "
             + (f"Primeiro alerta que acionou alguém: {l['prioridade_inicial']} pela regra {l['regra']}."
                if l["detectada"] else ""))

    j_ini = (t_ini if f["com_precursor"] else t_falha) - timedelta(days=1)
    j_fim = max(t_rep, t_lig) + timedelta(hours=6)
    L = P["log"]
    base = L[(L["device_id"] == did) & (L["ts"] >= j_ini) & (L["ts"] <= j_fim) & L["categoria"].notna()]
    marcos = pd.DataFrame([{"marco": m, "t": t} for m, t in zip(MARCOS, [t_ini if f["com_precursor"] else None,
                                                                          t_not, t_falha, t_lig]) if t is not None])
    x_enc = alt.X("ts:T", title=None, axis=alt.Axis(format="%d/%m %Hh"))
    regras = alt.Chart(marcos).mark_rule(strokeWidth=2, strokeDash=[6, 3]).encode(
        x=alt.X("t:T"), color=alt.Color("marco:N", scale=CORES_MARCOS, legend=alt.Legend(title=None, orient="bottom")),
        tooltip=[alt.Tooltip("marco:N", title=""), alt.Tooltip("t:T", format="%d/%m %H:%M", title="quando")])

    if f["familia"] == "MOTOR":
        mov = base[base["code"] == "CHR_MOVE_CYCLE"].copy()
        stq = motor.estado[did]
        refs = [{"ref": "Limiar fixo (3,6 A)", "y": 3.6}]
        if stq.mu is not None:
            refs += [{"ref": f"Normal desta cadeira ({stq.mu:.2f} A)", "y": stq.mu},
                     {"ref": f"Limite da regra R6 ({limite_desvio(stq.mu, stq.sigma):.2f} A)",
                      "y": limite_desvio(stq.mu, stq.sigma)}]
        linha = alt.Chart(mov).mark_circle(size=18, opacity=0.6).encode(
            x=x_enc, y=alt.Y("value:Q", title="Corrente de pico do motor (A)", scale=alt.Scale(zero=False)),
            color=alt.Color("categoria:N", scale=CORES_CAT, legend=alt.Legend(title="Categoria", orient="bottom")),
            tooltip=["ts:T", "value:Q", "categoria:N", "regra:N"])
        hr = alt.Chart(pd.DataFrame(refs)).mark_rule(strokeDash=[2, 2]).encode(
            y="y:Q", color=alt.Color("ref:N", legend=alt.Legend(title=None, orient="bottom")))
        grafico(alt.layer(linha, hr, regras).resolve_scale(color="independent").properties(height=380))
        st.caption("Cada ponto é um movimento da cadeira. A regra R6 compara a média móvel com o normal "
                   "aprendido desta cadeira; o limiar fixo só dispara bem mais tarde.")
    else:
        codigos = [c for c, i in EVENT_CATALOG.items() if i["familia"] == f["familia"]]
        ev = base[base["code"].isin(codigos)]
        pts = alt.Chart(ev).mark_tick(thickness=2, size=22).encode(
            x=x_enc, y=alt.Y("code:N", title=None),
            color=alt.Color("categoria:N", scale=CORES_CAT, legend=alt.Legend(title="Categoria", orient="bottom")),
            tooltip=["ts:T", "code:N", "value:Q", "categoria:N", "regra:N", "efeito:N"])
        grafico(alt.layer(pts, regras).resolve_scale(color="independent").properties(height=260))
        st.caption("Cada traço é um evento desta família no equipamento, na hora em que aconteceu.")

    if l["detectada"]:
        a = next(a for a in motor.alertas if a.id == l["alerta"])
        ns = P["notif"][P["notif"]["alerta"] == a.id]
        n0 = ns.iloc[0]
        c1, c2 = st.columns([3, 2])
        with c1:
            st.markdown("**Primeira notificação enviada**")
            st.info(f"**{hm(n0['em'])}** → {n0['para']} ({n0['canal']}; prazo: {n0['prazo']})\n\n{n0['mensagem']}")
            st.write(f"Depois disso: {len(ns) - 1} notificações (escalonamentos e lembretes) para "
                     f"{len(a.ocorrencias)} ocorrências agrupadas neste mesmo alerta.")
        with c2:
            st.markdown("**Log que disparou**")
            st.json(d["eventos"][int(n0["evento_i"])])

# ============================================================== MÉTRICAS
with aba_met:
    r, fu, de = met["resumo"], met["funil"], met["desempenho"]
    st.warning("Números medidos contra falhas que NÓS simulamos. Servem para testar a lógica das regras, "
               "não para prometer desempenho em campo.")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Falhas avisadas antes da ligação", f"{r['detectadas_antes_da_ligacao']}/{r['falhas_simuladas']}",
              help="Recall: o suporte soube antes de o cliente ligar")
    m2.metric("Avisadas antes de parar", f"{r['antecipadas_antes_de_parar']}/{r['falhas_simuladas']}")
    m3.metric("Precisão das notificações", f"{100 * r['precisao']:.0f}%",
              help="Dos alertas que acionaram alguém (P1/P2), quantos eram problema real")
    m4.metric("Antecedência mediana", f"{r['antecedencia_mediana_h_com_precursor']:.0f} h",
              help="Falhas com sinais precursores. Falhas súbitas: alerta chega "
                   f"{r['antecedencia_mediana_min_subitas']} min antes da ligação (mediana)")

    st.subheader("Do log à notificação")
    etapas = pd.DataFrame([
        {"etapa": "1. Mensagens recebidas", "n": fu["eventos_recebidos"]},
        {"etapa": "2. Avisos e erros", "n": fu["avisos"] + fu["erros"]},
        {"etapa": "3. Alertas abertos", "n": fu["alertas"]},
        {"etapa": "4. Notificações a pessoas", "n": fu["notificacoes"]},
    ])
    barras = alt.Chart(etapas).mark_bar(color="#0f766e").encode(
        x=alt.X("n:Q", scale=alt.Scale(type="log"), title="quantidade (escala log)"),
        y=alt.Y("etapa:N", sort=None, title=None), tooltip=["etapa", "n"])
    grafico(barras + barras.mark_text(align="left", dx=4).encode(text="n:Q"))
    st.write(f"{fu['duplicatas_descartadas']} duplicatas descartadas, {fu['eventos_perdidos_detectados_pelo_seq']} "
             f"perdas detectadas pelo seq, {fu['eventos_que_chegaram_atrasados']} mensagens chegaram atrasadas depois "
             f"de quedas de conexão. Sem agrupamento, cada aviso ou erro viraria uma notificação: "
             f"**{fu['notificacoes_sem_agrupamento']}**. Com agrupamento e tempo de silêncio: **{fu['notificacoes']}** "
             f"({fu['notificacoes_novas']} novas, {fu['escalonamentos']} escalonamentos, {fu['lembretes']} lembretes).")
    st.write(f"Desempenho: {de['eventos']:,} mensagens em {de['segundos']:.2f} s, cerca de "
             f"{de['eventos_por_segundo']:,} mensagens/s em um processo.".replace(",", "."))

    st.subheader("Cada falha simulada")
    st.dataframe(pd.DataFrame([{
        "Falha": x["falha"], "Tipo": NOME_FALHA[x["tipo"]], "Equipamento": x["device_id"],
        "Tinha sinais antes?": "sim" if x["com_precursor"] else "não (súbita)",
        "Avisado antes de parar?": "sim" if x["antes_de_parar"] else "não",
        "Antecedência (h)": x["antecedencia_h"], "Prioridade": x["prioridade_inicial"], "Regra": x["regra"],
    } for x in met["falhas"]]), hide_index=True)

    if "antes_e_depois" in met:
        st.subheader("Antes e depois das correções")
        st.dataframe(pd.DataFrame([{
            "Versão": v["versao"], "Falsos alarmes": v["falsos_alarmes"], "Precisão": f"{100 * v['precisao']:.0f}%",
            "Avisadas antes da ligação": f"{v['antes_da_ligacao']}/{v['falhas']}",
            "Avisadas antes de parar": f"{v['antes_de_parar']}/{v['falhas']}",
            "Notificações": v["notificacoes"], "Lembretes": v["lembretes"],
        } for v in met["antes_e_depois"]]), hide_index=True)
        st.write(f"**Modo limpeza:** {fu['retransmissoes_ignoradas_na_limpeza']} retransmissões do pedal durante a "
                 f"limpeza deixaram de contar. **Canal reserva:** {fu['erros_pelo_canal_reserva']} erros chegaram por "
                 f"4G/SMS durante quedas da internet. **Retorno dos técnicos:** alerta confirmado não gera mais "
                 f"lembrete, porque alguém já está cuidando; foram {fu['vereditos_confirmados']} confirmados e "
                 f"{fu['vereditos_falso_alarme']} marcados como falsos.")
        st.caption("Os 2 compressores que só foram vistos ao parar quase não deram sinal antes: um teve zero avisos e "
                   "o outro, dois. Nenhuma regra antecipa isso; a janela em horas de uso ficou como melhoria para "
                   "degradações que atravessam fim de semana e feriado.")

    st.subheader("Confiabilidade das regras (retorno dos técnicos)")
    st.dataframe(pd.DataFrame([{"Regra": c["regra"], "Confirmados em campo": f"{c['confirmados']} de {c['vereditos']}",
                                "Situação": "em revisão: só painel" if c["em_revisao"] else "ativa"}
                               for c in met.get("confiabilidade_das_regras", [])]), hide_index=True)
    st.caption(f"Cada notificação mostra esse histórico. Uma regra com menos de "
               f"{100 * CONFIABILIDADE['min_acerto']:.0f}% de acerto, depois de {CONFIABILIDADE['min_vereditos']} "
               "vereditos, para de acionar pessoas e vira P3 até ser revisada. Erros (R1) sempre acionam.")

    st.subheader("Falsos alarmes")
    if met["falsos_alarmes"]:
        st.dataframe(pd.DataFrame([{"Alerta": x["alerta"], "Equipamento": x["device_id"], "Regra": x["regra"],
                                    "O que disparou": x["detalhe"], "Por que era falso": x["explicacao"]}
                                   for x in met["falsos_alarmes"]]), hide_index=True)
    else:
        st.write("Nenhum na versão corrigida. Na primeira versão foram 2, ambos causados pela limpeza do pedal "
                 "(veja a tabela de antes e depois).")

    if "comparacao_regra_motor" in met:
        st.subheader("Motor da cadeira: limiar fixo contra desvio do normal de cada cadeira")
        st.dataframe(pd.DataFrame([{"Regra": c["regra"], "Avisadas antes de travar": f"{c['avisadas_antes_de_travar']}/{c['falhas_de_motor']}",
                                    "Antecedência mediana (h)": c["antecedencia_mediana_h"],
                                    "Falsos alarmes": c["falsos_alarmes"]} for c in met["comparacao_regra_motor"]]),
                     hide_index=True)
        st.caption("Limiar baixo acusa as cadeiras cujo normal já é alto; limiar alto demora a perceber. "
                   "Comparar cada cadeira com ela mesma resolve os dois problemas.")

# ============================================================== LOGS
with aba_logs:
    L = P["log"]
    f1, f2, f3 = st.columns(3)
    dev = f1.selectbox("Equipamento", ["Todos"] + sorted(EQ))
    cats = f2.multiselect("Categoria final", ["ERRO", "AVISO", "RUÍDO"], default=["ERRO", "AVISO", "RUÍDO"])
    so_alerta = f3.checkbox("Só eventos que mexeram em algum alerta", value=True)
    filtro = L
    if dev != "Todos":
        filtro = filtro[filtro["device_id"] == dev]
    filtro = filtro[filtro["categoria"].isin(cats) | filtro["categoria"].isna()]
    if so_alerta:
        filtro = filtro[filtro["alerta"].notna()]
    st.caption(f"{len(filtro):,} eventos (mostrando até 1.000)".replace(",", "."))
    st.dataframe(filtro.head(1000).rename(columns={"i": "#"}), hide_index=True,
                 column_order=["#", "recebido", "ts", "device_id", "code", "value", "base", "categoria", "regra",
                               "efeito"])

    st.subheader("Um log, do começo ao fim")
    i = int(st.number_input("Nº do evento (coluna #)", min_value=0, max_value=len(d["eventos"]) - 1,
                            value=int(P["i_pitch"]), step=1))
    lg = motor.log[i]
    ca, cb = st.columns(2)
    with ca:
        st.markdown("**1. Mensagem recebida pelo servidor**")
        st.json(d["eventos"][i])
    with cb:
        st.markdown("**2. Classificação**")
        st.write(f"Catálogo diz **{lg['base'] or '—'}**; categoria final **{lg['categoria'] or '—'}**. "
                 f"Regra: {lg['regra'] or 'nenhuma'}.")
        st.markdown("**3. Efeito no alerta**")
        st.write(lg["efeito"])
        st.markdown("**4. Notificação**")
        ns = [n for n in motor.notificacoes if n["evento_i"] == i]
        if ns:
            for n in ns:
                st.info(f"→ {n['para']} ({n['canal']}; prazo: {n['prazo']})\n\n{n['mensagem']}")
        else:
            st.write("Nenhuma. Este evento foi guardado ou agrupado sem acionar ninguém.")

# ============================================================== REGRAS
with aba_regras:
    st.subheader("Categorias de evento")
    st.dataframe(pd.DataFrame([{"Categoria": k, "Critério": v["criterio"], "Consequência": v["consequencia"]}
                               for k, v in CATEGORIAS.items()]), hide_index=True)

    st.subheader("Regras de alerta")
    regras_tab = [{"Regra": "R0", "Quando": "Evento AVISO que não completa nenhum padrão", "Resultado": "Observação P3"},
                  {"Regra": "R1", "Quando": "Qualquer evento de categoria ERRO", "Resultado": "Alerta P1 · falha ativa"}]
    for code, rg in WINDOW_RULES.items():
        janela = f"{rg['janela_uso_h']} h de uso" if "janela_uso_h" in rg else f"{rg['janela_h']} h (pelo horário do evento)"
        regras_tab.append({"Regra": rg["id"], "Quando": f"≥ {rg['min']} eventos {code} do mesmo equipamento em {janela}"
                                                        + (" (fora do modo limpeza)" if code == "CHR_PEDAL_COMM_RETRY" else ""),
                           "Resultado": f"Alerta {rg['tier']} · {rg['titulo']}"})
    regras_tab.append({"Regra": "R6", "Quando": f"Média móvel (EWMA, α={DRIFT_RULE['alfa']}) da corrente de "
                                                f"{DRIFT_RULE['code']} acima do normal da própria cadeira + "
                                                f"max({DRIFT_RULE['k_sigma']:.0f}σ, {DRIFT_RULE['subida_min_a']} A); "
                                                f"normal aprendido nos primeiros {DRIFT_RULE['ciclos_aprendizado']} movimentos",
                       "Resultado": f"Alerta P2 · {DRIFT_RULE['titulo']}"})
    st.dataframe(pd.DataFrame(regras_tab), hide_index=True)
    st.write(f"**Modo limpeza:** entre CHR_CLEANING_START e CHR_CLEANING_END (máximo de {LIMPEZA_MAX_H} h), as "
             "retransmissões do pedal ficam guardadas, mas não contam para a R4. **Janelas R2 e R3:** contadas em "
             "horas de uso, então noite, domingo e feriado não zeram a contagem. **Canal reserva:** com a internet "
             "fora, só os erros saem por 4G/SMS; o resto espera a reconexão. **Retorno do técnico:** ao fechar o "
             "chamado, ele confirma ou marca falso alarme; isso alimenta a confiabilidade de cada regra.")
    st.write(f"**Agrupamento:** um alerta por equipamento e família ({', '.join(FAMILIAS.values())}). "
             "Novas ocorrências somam no mesmo alerta; se piorar, o mesmo alerta escala (P3 → P2 → P1). "
             f"**Tempo de silêncio:** lembrete só depois de {TIERS['P1']['silencio_h']} h (P1) ou "
             f"{TIERS['P2']['silencio_h']} h (P2) e se o problema continuar. **Encerramento:** após "
             f"{ENCERRA_APOS_USO_H} h de uso sem nova ocorrência (noite, domingo e feriado não contam).")

    st.subheader("Fluxo de notificação")
    st.dataframe(pd.DataFrame([{"Prioridade": f"{t['icone']} {t['nome']}", "Quem": t["quem"], "Canal": t["canal"],
                                "Prazo": t["prazo"], "O que acontece": t["faz"]} for t in TIERS.values()]),
                 hide_index=True)

    st.subheader("Catálogo de eventos (ilustrativo)")
    st.dataframe(pd.DataFrame([{"Código": c, "Equipamento": NOME_EQUIP[i["equip"]], "Categoria base": i["base"],
                                "Família": i["familia"] or "", "Descrição": i["desc"]}
                               for c, i in EVENT_CATALOG.items()]), hide_index=True)

    st.subheader("Exemplo de mensagem (protocolo v1)")
    st.json(d["eventos"][P["i_pitch"]])
    st.caption("received_at é carimbado pelo servidor na chegada; o resto vem do equipamento.")
