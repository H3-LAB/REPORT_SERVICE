"""Períodos dos reports e próxima execução agendada, no fuso horário da fonte/app."""

from datetime import datetime, time, timedelta

PERIODS = {
    'previous_day': 'Dia anterior',
    'last_7_days': 'Últimos 7 dias (até ontem)',
    'previous_week': 'Semana anterior (segunda a domingo)',
    'last_28_days': 'Últimos 28 dias (até ontem)',
    'previous_month': 'Mês anterior',
    'month_to_date': 'Mês atual até ontem',
}
FREQUENCIES = {'daily': 'Diário', 'weekly': 'Semanal', 'monthly': 'Mensal'}
WEEKDAYS = ['Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo']


def period_range(period, now):
    """``(início, fim)`` do período, com ``now`` já no fuso horário certo. O fim é o último instante do dia."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = today - timedelta(microseconds=1)  # ontem às 23:59:59.999999
    yesterday = today - timedelta(days=1)
    if period == 'previous_day':
        start = yesterday
    elif period == 'last_7_days':
        start = yesterday - timedelta(days=6)
    elif period == 'previous_week':
        start = today - timedelta(days=today.weekday() + 7)
        end = start + timedelta(days=7) - timedelta(microseconds=1)
    elif period == 'last_28_days':
        start = yesterday - timedelta(days=27)
    elif period == 'previous_month':
        end = today.replace(day=1) - timedelta(microseconds=1)
        start = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == 'month_to_date':
        # No dia 1 ainda não há "mês atual até ontem": usa o dia anterior
        start = yesterday.replace(day=1) if today.day == 1 else today.replace(day=1)
    else:
        raise ValueError(f'Período desconhecido: {period}')
    # Com zoneinfo a diferença horária vem da hora local: a mudança de hora fica certa sem normalizar
    return start, end


def next_run(schedule, after):
    """Próximo instante agendado depois de ``after`` (aware, no fuso do agendamento), ou ``None``.

    ``schedule``: ``{"freq": "daily|weekly|monthly", "time": "HH:MM", "weekday": 0-6, "day": 1-28}``.
    """
    if not schedule or schedule.get('freq') not in FREQUENCIES:
        return None
    hour, minute = (int(part) for part in schedule.get('time', '08:00').split(':'))
    tz = after.tzinfo
    day = after.date()
    # 32 dias chegam para qualquer frequência (o dia do mês vai até 28)
    for offset in range(0, 33):
        candidate_day = day + timedelta(days=offset)
        if schedule['freq'] == 'weekly' and candidate_day.weekday() != int(schedule.get('weekday', 0)):
            continue
        if schedule['freq'] == 'monthly' and candidate_day.day != int(schedule.get('day', 1)):
            continue
        candidate = datetime.combine(candidate_day, time(hour, minute), tzinfo=tz)
        if candidate > after:
            return candidate
    return None


def describe_schedule(schedule):
    if not schedule or schedule.get('freq') not in FREQUENCIES:
        return 'Sem agendamento'
    at = schedule.get('time', '08:00')
    if schedule['freq'] == 'weekly':
        return f"{WEEKDAYS[int(schedule.get('weekday', 0))]} às {at}"
    if schedule['freq'] == 'monthly':
        return f"Dia {schedule.get('day', 1)} às {at}"
    return f'Diário às {at}'
