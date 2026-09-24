"""
catalog.py - Catálogo de eventos, regras de alerta e fluxo de notificação.

Aqui fica todo o CONHECIMENTO DE DOMÍNIO do sistema. A pessoa de clínica ou de
suporte ajusta o que é ERRO, AVISO ou RUÍDO, os limites das regras e as ações
sugeridas sem precisar mexer no motor (engine.py).

ATENÇÃO: códigos de evento, limites numéricos e ações sugeridas são
ILUSTRATIVOS, criados para o hackathon. Em produção viriam do firmware dos
equipamentos e da base de conhecimento do suporte da Alliage.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. As três categorias de evento pedidas no desafio
# ---------------------------------------------------------------------------
ERRO = "ERRO"
AVISO = "AVISO"
RUIDO = "RUÍDO"

CATEGORIAS = {
    ERRO: {
        "criterio": "O equipamento parou ou está impedido de atender (bloqueio, desarme, "
                    "perda de comunicação essencial).",
        "consequencia": "Alerta P1 na hora: técnico de plantão é acionado e o chamado é aberto "
                        "automaticamente.",
    },
    AVISO: {
        "criterio": "Grandeza fora da faixa recomendada, mas o equipamento ainda funciona. "
                    "Pode anteceder um erro.",
        "consequencia": "Isolado: observação P3, só no painel. Repetido (padrão na janela de "
                        "tempo): alerta P2 para a fila do suporte.",
    },
    RUIDO: {
        "criterio": "Operação normal ou evento esperado (liga/desliga, sinal de vida, exame "
                    "concluído, retransmissão isolada).",
        "consequencia": "Só é armazenado (histórico e linha de base). Nunca gera alerta sozinho; "
                        "se formar padrão, é reclassificado como AVISO.",
    },
}

# ---------------------------------------------------------------------------
# 2. Famílias: um alerta é sempre do par (equipamento, família).
#    Assim, 200 eventos do mesmo defeito viram UM alerta que evolui.
# ---------------------------------------------------------------------------
FAMILIAS = {
    "AR": "Compressor e ar comprimido",
    "MOTOR": "Motor de elevação da cadeira",
    "PEDAL": "Pedal de comando",
    "TUBO": "Tubo de raio X e arrefecimento",
    "SENSOR": "Sensor de imagem",
    "GERADOR": "Gerador de alta tensão",
}

NOME_EQUIP = {"CADEIRA": "Cadeira", "PANORAMICO": "Panorâmico"}


def _ev(equip, base, familia, desc, titulo=None):
    return {"equip": equip, "base": base, "familia": familia, "desc": desc, "titulo": titulo}


# ---------------------------------------------------------------------------
# 3. Catálogo de códigos que os equipamentos enviam
#    (desc: o texto depois de ";" explica o campo value)
# ---------------------------------------------------------------------------
EVENT_CATALOG = {
    # Cadeira odontológica
    "CHR_POWER_ON": _ev("CADEIRA", RUIDO, None, "Cadeira ligada"),
    "CHR_POWER_OFF": _ev("CADEIRA", RUIDO, None, "Cadeira desligada normalmente"),
    "CHR_HEARTBEAT": _ev("CADEIRA", RUIDO, None, "Sinal de vida enviado a cada 60 min"),
    "CHR_MOVE_CYCLE": _ev("CADEIRA", RUIDO, "MOTOR",
                          "Movimento da cadeira concluído; value = corrente de pico do motor (A)"),
    "CHR_PEDAL_COMM_RETRY": _ev("CADEIRA", RUIDO, "PEDAL", "Pedal precisou retransmitir um comando"),
    "CHR_AIR_PRESSURE_LOW": _ev("CADEIRA", AVISO, "AR", "Pressão de ar abaixo do mínimo; value = bar"),
    "CHR_AIR_PRESSURE_CRITICAL": _ev("CADEIRA", ERRO, "AR",
                                     "Pressão insuficiente para as peças de mão; value = bar",
                                     "Compressor sem pressão: peças de mão paradas"),
    "CHR_MOTOR_OVERCURRENT_TRIP": _ev("CADEIRA", ERRO, "MOTOR",
                                      "Proteção de sobrecorrente do motor desarmou; value = A",
                                      "Cadeira travada: motor de elevação desarmou"),
    "CHR_PEDAL_COMM_LOST": _ev("CADEIRA", ERRO, "PEDAL", "Pedal sem comunicação com a cadeira",
                               "Pedal sem comunicação: comandos não chegam"),
    # Raio X panorâmico
    "PAN_POWER_ON": _ev("PANORAMICO", RUIDO, None, "Panorâmico ligado"),
    "PAN_POWER_OFF": _ev("PANORAMICO", RUIDO, None, "Panorâmico desligado normalmente"),
    "PAN_HEARTBEAT": _ev("PANORAMICO", RUIDO, None, "Sinal de vida enviado a cada 60 min"),
    "PAN_EXAM_OK": _ev("PANORAMICO", RUIDO, None,
                       "Exame concluído; value = temperatura do tubo ao final (°C)"),
    "PAN_EXAM_ABORTED_USER": _ev("PANORAMICO", RUIDO, None, "Exame cancelado pelo operador"),
    "PAN_SENSOR_COMM_RETRY": _ev("PANORAMICO", RUIDO, "SENSOR", "Sensor precisou retransmitir dados"),
    "PAN_TUBE_TEMP_HIGH": _ev("PANORAMICO", AVISO, "TUBO",
                              "Tubo acima da temperatura recomendada; value = °C"),
    "PAN_TUBE_OVERHEAT_LOCK": _ev("PANORAMICO", ERRO, "TUBO",
                                  "Bloqueio por superaquecimento do tubo; value = °C",
                                  "Panorâmico bloqueado por superaquecimento"),
    "PAN_SENSOR_DISCONNECTED": _ev("PANORAMICO", ERRO, "SENSOR", "Sensor de imagem desconectado",
                                   "Sensor desconectado: sem aquisição de imagem"),
    "PAN_GENERATOR_FAULT": _ev("PANORAMICO", ERRO, "GERADOR", "Falha no gerador de alta tensão",
                               "Falha no gerador de alta tensão"),
}

# ---------------------------------------------------------------------------
# 4. Regras de alerta
#    R0 - evento AVISO que não completa nenhum padrão  -> observação P3
#    R1 - qualquer evento ERRO                          -> alerta P1
#    R2..R5 - janela deslizante: N eventos do mesmo código, no mesmo
#             equipamento, dentro de H horas (contadas pelo ts do evento)
#    R6 - desvio de comportamento em relação ao normal da PRÓPRIA cadeira
# ---------------------------------------------------------------------------
WINDOW_RULES = {
    "CHR_AIR_PRESSURE_LOW": {
        "id": "R2", "min": 3, "janela_h": 24, "tier": "P2",
        "titulo": "Compressor perdendo rendimento",
        "detalhe": "{n} avisos de pressão baixa em 24 h (último: {valor} bar)",
    },
    "PAN_TUBE_TEMP_HIGH": {
        "id": "R3", "min": 4, "janela_h": 24, "tier": "P2",
        "titulo": "Arrefecimento do tubo perdendo eficiência",
        "detalhe": "{n} avisos de tubo quente em 24 h (último: {valor} °C)",
    },
    "CHR_PEDAL_COMM_RETRY": {
        "id": "R4", "min": 15, "janela_h": 2, "tier": "P2",
        "titulo": "Comunicação do pedal instável",
        "detalhe": "{n} retransmissões do pedal em 2 h",
    },
    "PAN_SENSOR_COMM_RETRY": {
        "id": "R5", "min": 10, "janela_h": 4, "tier": "P2",
        "titulo": "Conexão do sensor instável",
        "detalhe": "{n} retransmissões do sensor em 4 h",
    },
}

DRIFT_RULE = {
    "id": "R6", "code": "CHR_MOVE_CYCLE", "familia": "MOTOR", "tier": "P2",
    "titulo": "Motor de elevação exigindo mais corrente que o normal desta cadeira",
    "ciclos_aprendizado": 60,  # os primeiros movimentos de CADA cadeira definem o normal dela
    "alfa": 0.1,               # suavização da média móvel exponencial (EWMA)
    "k_sigma": 5.0,            # limite estatístico: k desvios-padrão da média móvel
    "subida_min_a": 0.25,      # e no mínimo +0,25 A acima do normal (evita alarme por ruído)
    "sigma_min_a": 0.03,
}

# Limiar fixo usado SÓ para comparação em evaluate.py (não é usado no sistema)
LIMIARES_FIXOS_COMPARACAO_A = [3.3, 3.6]

# ---------------------------------------------------------------------------
# 5. Prioridade e fluxo de notificação
#    score = base da faixa + pacientes/dia que dependem do equipamento (máx. 99)
#    -> primeiro o que parou, depois o que vai parar, depois o que merece olhar;
#       dentro da faixa, quem tem mais pacientes em risco.
# ---------------------------------------------------------------------------
TIERS = {
    "P1": {
        "nome": "P1 · agir agora", "icone": "🔴", "base": 300, "notifica": True, "silencio_h": 4,
        "quem": "Técnico de plantão da região", "canal": "push no app + SMS",
        "prazo": "ligar para o consultório em até 30 min",
        "faz": "Abre chamado automaticamente; se ninguém confirmar em 30 min, escala para a "
               "coordenação. O consultório recebe um WhatsApp avisando que o suporte já está cuidando.",
    },
    "P2": {
        "nome": "P2 · agir hoje", "icone": "🟠", "base": 200, "notifica": True, "silencio_h": 24,
        "quem": "Fila do suporte técnico", "canal": "painel + e-mail",
        "prazo": "contato proativo com o consultório em até 1 dia útil",
        "faz": "Analista liga para o consultório e decide entre orientação remota e visita "
               "preventiva, antes de o equipamento parar.",
    },
    "P3": {
        "nome": "P3 · observar", "icone": "🟡", "base": 100, "notifica": False, "silencio_h": None,
        "quem": "Ninguém é acionado", "canal": "somente painel",
        "prazo": "revisado no resumo diário",
        "faz": "Fica visível no painel; o sistema promove para P2 automaticamente se o aviso "
               "se repetir.",
    },
}
RANK = {"P3": 1, "P2": 2, "P1": 3}

ENCERRA_APOS_USO_H = 8  # alerta se encerra após 8 h de USO do equipamento sem nova ocorrência
SEM_SINAL_H = 2         # equipamento ligado que não manda nada há 2 h aparece como "sem sinal"

# ---------------------------------------------------------------------------
# 6. Ação sugerida por (família, prioridade) - ILUSTRATIVAS, validar com o suporte
# ---------------------------------------------------------------------------
ACOES = {
    ("AR", "P1"): "Orientar por telefone: desligar a cadeira, drenar o reservatório e religar. Se a "
                  "pressão não voltar em 10 min, visita técnica prioritária com compressor reserva.",
    ("AR", "P2"): "Ligar para o consultório: pedir a drenagem do reservatório do compressor e checar "
                  "vazamentos audíveis. Se os avisos continuarem, agendar visita preventiva com kit "
                  "de reparo do compressor.",
    ("MOTOR", "P1"): "Orientar a não forçar a cadeira. Visita técnica prioritária levando motor de "
                     "elevação de reposição.",
    ("MOTOR", "P2"): "Agendar visita preventiva: inspeção e lubrificação do fuso de elevação; levar "
                     "motor de reposição caso a corrente não normalize.",
    ("PEDAL", "P1"): "Orientar a reconectar o cabo do pedal. Se não resolver, enviar técnico com pedal "
                     "reserva.",
    ("PEDAL", "P2"): "Orientar a verificar se o cabo do pedal está pisado, dobrado ou mal encaixado. Se "
                     "persistir, enviar cabo de reposição.",
    ("TUBO", "P1"): "Orientar a aguardar o resfriamento (cerca de 20 min) antes de novo exame. Agendar "
                    "visita para o sistema de arrefecimento.",
    ("TUBO", "P2"): "Confirmar se a ventilação traseira está desobstruída e se o intervalo entre exames "
                    "é respeitado. Agendar inspeção do arrefecimento.",
    ("SENSOR", "P1"): "Orientar a reconectar o sensor e reiniciar o software de aquisição. Se "
                      "persistir, enviar cabo ou sensor reserva.",
    ("SENSOR", "P2"): "Orientar a trocar a porta de conexão e verificar o conector do sensor. Se "
                      "persistir, enviar cabo de reposição.",
    ("GERADOR", "P1"): "Manter o equipamento desligado. Visita técnica prioritária para o gerador de "
                       "alta tensão.",
}
ACAO_P3 = "Sem ação imediata. O sistema acompanha e promove para P2 se o aviso se repetir."
