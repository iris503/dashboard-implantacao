#!/usr/bin/env python3
"""
Dashboard Implantação WMI — FastAPI Application
Fetches Jira data, processes metrics, serves a live dashboard.
Replaces the GitHub Actions + GitHub Pages pipeline.
"""

import os
import sys
import json
import base64
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

import requests as req_lib
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

# ─── Logging ───────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dashboard")

# ─── Configuration ─────────────────────────────────────────
JIRA_EMAIL = os.environ.get("JIRA_EMAIL")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN")
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "https://wmisolutions.atlassian.net")
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "1800"))  # 30 min default

IMPLEMENTERS = ['Jessica', 'Daniel', 'Fabio', 'Nino', 'Jorge', 'Anderson', 'Luiz', 'Fernanda']
EXCLUDE_ASSIGNEES = {'Yasmin', 'Michael', 'Iris'}

STATUS_COMPLETED = {'Concluído', 'Cancelado'}
STATUS_EM_ANDAMENTO = {'Em andamento'}
STATUS_PAUSED = {'Paused'}
STATUS_PENDENTE = {'Tarefas pendentes', 'Escalado'}
STATUS_WAITING = {'AGUARDANDO CLIENTE'}

# Estimated hours per module (menor média from historical data Jan-Mai/2026)
MODULE_HOURS = {
    'Interlac': 4.5,
    'NF': 3.2,
    'Upsell': 12.0,
    'Integração': 24.7,
    'Cloud': 47.0,
    'Assinatura': 5.3,
    'B2B': 12.0,
    'TAP': 1.5,
    'Novo': 100.0,
}
MODULE_DEFAULT_HOURS = 12.0  # fallback for unknown types

# In-memory cache
_dashboard_cache: Dict = {}
_cache_lock = asyncio.Lock()

# ─── Jira Client (async) ──────────────────────────────────
class JiraClient:
    def __init__(self, email: str, api_token: str, base_url: str):
        if not all([email, api_token, base_url]):
            raise ValueError("JIRA_EMAIL, JIRA_API_TOKEN, and JIRA_BASE_URL required")
        self.base_url = base_url.rstrip('/')
        auth_string = f"{email}:{api_token}"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()
        self.headers = {
            'Authorization': f'Basic {encoded_auth}',
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }

    async def get_epics(self) -> List[Dict]:
        epics = []
        max_results = 100
        start_at = 0
        current_year = datetime.now().strftime('%Y')
        jql = (
            f'project = IWN AND issuetype = Epic AND ('
            f'created >= {current_year}-01-01 OR '
            f'statusCategory != Done OR '
            f'resolutiondate >= {current_year}-01-01'
            f')'
        )
        fields = [
            'summary', 'status', 'assignee', 'customfield_10800',
            'aggregatetimespent', 'created', 'duedate', 'timetracking', 'updated',
            'customfield_10015', 'resolutiondate'
        ]

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            while True:
                url = f"{self.base_url}/rest/api/2/search"
                params = {'jql': jql, 'maxResults': max_results, 'startAt': start_at, 'fields': ','.join(fields)}
                
                    

                resp = await client.get(url, headers=self.headers, params=params)
                resp.raise_for_status()
                data = resp.json()
                issues = data.get('issues', [])
                if not issues:
                    break
                epics.extend(issues)
                start_at += len(issues)
                if start_at >= data.get('total', 0):
                    break

        logger.info(f"Fetched {len(epics)} epics from Jira")
        return epics


# ─── Data Processing (ported from generate_dashboard_v3.py) ──────

def classify_epic(epic: Dict) -> str:
    fields = epic.get('fields', {})
    tipo_negocio = fields.get('customfield_10800')
    if tipo_negocio is None:
        return 'Upsell'
    tipo_str = str(tipo_negocio).lower()
    if 'empresa nova' in tipo_str:
        return 'Novo'
    elif 'empresa existente' in tipo_str:
        return 'Upsell'
    return 'Upsell'


def get_status_category(status: str) -> str:
    s = status.strip()
    if s in STATUS_COMPLETED:
        return 'completed'
    s_lower = s.lower().replace('í','i').replace('ú','u')
    if s_lower in ('concluido', 'cancelado', 'done', 'closed'):
        return 'completed'
    if s in STATUS_EM_ANDAMENTO:
        return 'em_andamento'
    elif s in STATUS_PAUSED:
        return 'paused'
    elif s in STATUS_PENDENTE or s in STATUS_WAITING:
        return 'pendente'
    return 'pendente'


def extract_implementer_name(assignee: Optional[Dict]) -> Optional[str]:
    if not assignee:
        return None
    display_name = assignee.get('displayName', '')
    for impl in IMPLEMENTERS:
        if impl.lower() in display_name.lower():
            return impl
    return None


def is_cloud_migration(summary: str) -> bool:
    keywords = ['Migration Module', 'Autolac Cloud', 'Migração']
    summary_lower = summary.lower()
    return any(kw.lower() in summary_lower for kw in keywords)


def parse_date(date_str: str) -> str:
    if not date_str:
        return ''
    return date_str.split('T')[0] if 'T' in date_str else date_str


def detect_porte(summary: str) -> Tuple[str, int, int]:
    summary_lower = summary.lower()
    if 'large' in summary_lower or 'grande' in summary_lower:
        return ('Large', 400, 120)
    elif 'medium' in summary_lower or 'médio' in summary_lower:
        return ('Medium', 200, 90)
    elif 'small' in summary_lower or 'pequeno' in summary_lower:
        return ('Small', 150, 60)
    return ('N/D', 100, 0)


def process_epics(epics: List[Dict], today: str) -> Tuple[Dict, List, List]:
    technicians = {impl: {
        'name': impl, 'total': 0, 'completed': 0, 'inProgress': 0, 'paused': 0,
        'pending': 0, 'waiting': 0, 'hours': 0.0, 'openEpics': [],
        'board': {'novo': 0, 'upsell': 0}, 'novoHours': 0.0, 'upsellHours': 0.0,
        'overdueCount': 0, 'zeroHoursOpen': 0, 'oldest': None,
        'novoStats': {'total': 0, 'completed': 0, 'inProgress': 0, 'paused': 0, 'pending': 0, 'waiting': 0, 'hours': 0.0},
        'upsellStats': {'total': 0, 'completed': 0, 'inProgress': 0, 'paused': 0, 'pending': 0, 'waiting': 0, 'hours': 0.0}
    } for impl in IMPLEMENTERS}

    yasmin_queue = []
    cloud_migrations = []
    excluded_open_hours = 0.0
    hours_cutoff = f'{datetime.now().strftime("%Y")}-04-01'

    for epic in epics:
        fields = epic.get('fields', {})
        summary = fields.get('summary', 'Unknown')
        key = epic.get('key', '')
        status = fields.get('status', {}).get('name', 'Unknown')
        jira_status_cat_key = fields.get('status', {}).get('statusCategory', {}).get('key', '')
        status_cat = 'completed' if jira_status_cat_key == 'done' else get_status_category(status)
        assignee = fields.get('assignee')
        created = parse_date(fields.get('created', ''))
        duedate = parse_date(fields.get('duedate', ''))
        time_spent = fields.get('aggregatetimespent', 0) or 0
        hours = time_spent / 3600 if time_spent else 0.0
        classification = classify_epic(epic)
        implementer = extract_implementer_name(assignee)
        is_cloud = is_cloud_migration(summary)
        is_open = status_cat != 'completed'
        include_hours = not created or created >= hours_cutoff
        kpi_hours = hours if include_hours else 0.0

        if not implementer or implementer in EXCLUDE_ASSIGNEES:
            if is_open:
                excluded_open_hours += kpi_hours
            if status_cat in ('pendente', 'waiting') or not implementer:
                yasmin_queue.append({'key': key, 'summary': summary, 'status': status, 'hours': round(hours, 1), 'created': created, 'duedate': duedate or None})
            if is_cloud:
                cloud_migrations.append({'key': key, 'summary': summary, 'assignee': assignee.get('displayName', 'Unassigned') if assignee else 'Unassigned', 'status': status, 'hours': round(hours, 1)})
            continue

        tech = technicians[implementer]
        tech['total'] += 1
        if is_open:
            tech['hours'] += kpi_hours

        if status_cat == 'completed': tech['completed'] += 1
        elif status_cat == 'em_andamento': tech['inProgress'] += 1
        elif status_cat == 'paused': tech['paused'] += 1
        elif status_cat == 'pendente': tech['pending'] += 1
        elif status_cat == 'waiting': tech['waiting'] += 1

        type_key = 'novoStats' if classification == 'Novo' else 'upsellStats'
        tech[type_key]['total'] += 1
        if is_open: tech[type_key]['hours'] += kpi_hours
        if status_cat == 'completed': tech[type_key]['completed'] += 1
        elif status_cat == 'em_andamento': tech[type_key]['inProgress'] += 1
        elif status_cat == 'paused': tech[type_key]['paused'] += 1
        elif status_cat in ('pendente', 'waiting'): tech[type_key]['pending'] += 1

        if is_open:
            if classification == 'Novo':
                tech['board']['novo'] += 1
                tech['novoHours'] += kpi_hours
            else:
                tech['board']['upsell'] += 1
                tech['upsellHours'] += kpi_hours

            is_overdue = False
            if duedate and duedate < today:
                is_overdue = True
                tech['overdueCount'] += 1
            if hours == 0:
                tech['zeroHoursOpen'] += 1
            if not tech['oldest'] or created < tech['oldest']:
                tech['oldest'] = created

            tech['openEpics'].append({
                'key': key, 'title': summary, 'created': created, 'status': status,
                'board': classification, 'due': duedate, 'hours': round(hours, 1), 'overdue': is_overdue
            })

        if is_cloud:
            cloud_migrations.append({'key': key, 'summary': summary, 'assignee': implementer, 'status': status, 'hours': round(hours, 1)})

    return technicians, yasmin_queue, cloud_migrations


def generate_risk_level(tech: Dict) -> str:
    open_count = tech['total'] - tech['completed']
    if open_count == 0: return 'low'
    completion_rate = (tech['completed'] / tech['total'] * 100) if tech['total'] > 0 else 0
    if tech['overdueCount'] > 5 or (open_count > 8 and completion_rate < 40): return 'high'
    elif tech['overdueCount'] > 3 or open_count > 6: return 'medium'
    return 'low'


def generate_strengths_risks(tech: Dict) -> Tuple[List[str], List[str]]:
    total, completed = tech['total'], tech['completed']
    open_count = total - completed
    rate = (completed / total * 100) if total > 0 else 0
    total_hours = tech['novoHours'] + tech['upsellHours']
    strengths, risks = [], []

    if rate >= 75: strengths.append(f"{int(rate)}% de conclusão ({completed}/{total})")
    if tech['overdueCount'] == 0: strengths.append("Zero vencidos — fila saudável")
    if tech['zeroHoursOpen'] == 0 and open_count > 0: strengths.append("Todos os epics com apontamento de horas")
    if tech['board']['novo'] > 0 and tech['board']['upsell'] > 0:
        strengths.append(f"Mix equilibrado Novo ({tech['board']['novo']}) e Upsell ({tech['board']['upsell']})")
    if tech['paused'] == 0 and open_count > 0: strengths.append("Zero Paused — fluxo contínuo")
    if total_hours < 100 and open_count > 0: strengths.append(f"Carga leve ({total_hours:.0f}h)")

    if tech['overdueCount'] > 0: risks.append(f"{tech['overdueCount']} epic(s) vencido(s)")
    if open_count > 8: risks.append(f"{open_count} abertos — WIP elevado")
    if tech['zeroHoursOpen'] > 0: risks.append(f"{tech['zeroHoursOpen']} epic(s) sem apontamento")
    if open_count <= 2 and total > 5: risks.append("WIP baixo — capacidade ociosa")
    if total_hours > 300: risks.append(f"Carga alta ({total_hours:.0f}h)")
    if not risks: risks.append("Monitorar evolução da fila")
    return strengths, risks


def generate_backlog_data(technicians_dict: Dict, epics: List[Dict], today: str) -> Dict:
    CAPACITY_MONTHLY = 140
    novo_epics = [e for e in epics if classify_epic(e) == 'Novo']
    upsell_epics = [e for e in epics if classify_epic(e) == 'Upsell']
    novo_with_data = []

    for epic in novo_epics:
        fields = epic.get('fields', {})
        key = epic.get('key', '')
        summary = fields.get('summary', '')
        assignee = fields.get('assignee')
        status = fields.get('status', {}).get('name', 'Unknown')
        status_cat = fields.get('status', {}).get('statusCategory', {}).get('key', '')
        start_date = fields.get('customfield_10015', '')
        created = parse_date(fields.get('created', ''))
        duedate = parse_date(fields.get('duedate', ''))
        time_spent = fields.get('aggregatetimespent', 0) or 0
        gasto = time_spent / 3600

        if status_cat == 'done' or status.lower().replace('í','i') in ('concluido', 'cancelado'):
            continue

        porte, meta, days = detect_porte(summary)
        restante = max(meta - gasto, 10) if gasto < meta else max(meta - gasto, 10)
        progresso = (gasto / meta) if meta > 0 else 0
        prazo_wmi = 'N/D'
        status_prazo = 'Sem porte'
        status_prazo_type = 'noporte'
        base_date = start_date if start_date else created
        if days > 0 and base_date:
            deadline = datetime.strptime(base_date, '%Y-%m-%d') + timedelta(days=days)
            prazo_wmi = deadline.strftime('%d/%m/%Y')
            today_dt = datetime.strptime(today, '%Y-%m-%d')
            days_diff = (today_dt - deadline).days
            if days_diff > 0:
                status_prazo = f'+{days_diff} dias'
                status_prazo_type = 'overdue'
            elif days_diff < -90:
                status_prazo = f'{-days_diff} dias'
                status_prazo_type = 'ok'
            else:
                status_prazo = f'{-days_diff} dias'
                status_prazo_type = 'warning'

        impl = extract_implementer_name(assignee)
        novo_with_data.append({
            'key': key, 'summary': summary, 'assignee': impl or 'Yasmin', 'porte': porte,
            'status': status, 'gasto': round(gasto, 1), 'meta': float(meta),
            'restante': round(restante, 1), 'progresso': round(progresso, 2),
            'criacao': created, 'prazoWmi': prazo_wmi,
            'statusPrazo': status_prazo, 'statusPrazoType': status_prazo_type
        })

    novo_with_data.sort(key=lambda x: x['gasto'], reverse=True)
    novo_open = [e for e in novo_with_data if e['status'] not in STATUS_COMPLETED]

    fila_yasmin = []
    for epic in epics:
        fields = epic.get('fields', {})
        status = fields.get('status', {}).get('name', 'Unknown')
        status_cat = fields.get('status', {}).get('statusCategory', {}).get('key', '')
        if status_cat == 'done' or status.lower().replace('í','i') in ('concluido', 'cancelado'):
            continue
        key = epic.get('key', '')
        summary = fields.get('summary', '')
        assignee = fields.get('assignee')
        created = parse_date(fields.get('created', ''))
        duedate = parse_date(fields.get('duedate', ''))
        time_spent = fields.get('aggregatetimespent', 0) or 0
        hours = time_spent / 3600
        impl = extract_implementer_name(assignee)
        if not impl or impl in EXCLUDE_ASSIGNEES:
            tipo = 'Upsell'
            sl = summary.lower()
            if 'interlac' in sl: tipo = 'Interlac'
            elif 'nota fiscal' in sl or 'nf ' in sl or sl.startswith('nf-') or sl.startswith('nf '): tipo = 'NF'
            elif 'cloud' in sl or 'migração' in sl: tipo = 'Cloud'
            elif 'integra' in sl or 'api' in sl: tipo = 'Integração'
            elif 'assinatura' in sl: tipo = 'Assinatura'
            elif 'tap' in sl or 'solicitação de tap' in sl or 'solicitacao de tap' in sl: tipo = 'TAP'
            elif 'b2b' in sl: tipo = 'B2B'
            elif 'implementation project' in sl or 'novo' in sl: tipo = 'Novo'
            if tipo == 'Novo':
                continue
            estimated = MODULE_HOURS.get(tipo, MODULE_DEFAULT_HOURS)
            fila_yasmin.append({'key': key, 'summary': summary, 'tipo': tipo, 'status': status, 'hours': round(hours, 1), 'estimatedHours': estimated, 'criado': created, 'dueDate': duedate or '—'})

    total_novo_restante = sum(e['restante'] for e in novo_open)
    upsell_restante = sum(
        max(12 - (e.get('fields', {}).get('aggregatetimespent', 0) or 0) / 3600, 0)
        for e in upsell_epics if e.get('fields', {}).get('status', {}).get('name', '') not in STATUS_COMPLETED
    )
    yasmin_hours = sum(e['estimatedHours'] for e in fila_yasmin)
    total_restante = total_novo_restante + upsell_restante + yasmin_hours
    num_techs = len([t for t in technicians_dict.values() if t.get('total', 0) > 0])
    backlog_months = total_restante / (CAPACITY_MONTHLY * max(1, num_techs)) if num_techs > 0 else 0

    novo_pct = int((total_novo_restante / total_restante) * 100) if total_restante > 0 else 0
    upsell_pct = int((upsell_restante / total_restante) * 100) if total_restante > 0 else 0
    yasmin_pct = int((yasmin_hours / total_restante) * 100) if total_restante > 0 else 0

    capacity_table = []
    for tech_name in IMPLEMENTERS:
        tech = technicians_dict.get(tech_name, {})
        if tech.get('total', 0) == 0: continue
        novo_count = tech.get('board', {}).get('novo', 0)
        upsell_count = tech.get('board', {}).get('upsell', 0)
        epics_str = f"{novo_count + upsell_count} ({novo_count}N + {upsell_count}U)"
        novo_rest = sum(e['restante'] for e in novo_open if e['assignee'] == tech_name)
        upsell_rest = sum(
            max(12 - (ep.get('fields', {}).get('aggregatetimespent', 0) or 0) / 3600, 0)
            for ep in upsell_epics
            if extract_implementer_name(ep.get('fields', {}).get('assignee')) == tech_name
        )
        total_rest = novo_rest + upsell_rest
        meses = total_rest / CAPACITY_MONTHLY if total_rest > 0 else 0
        novos_em_andamento = sum(1 for e in novo_open if e['assignee'] == tech_name and e['status'] == 'Em andamento')
        novos_str = f"{novos_em_andamento} em andamento" if novos_em_andamento > 0 else "0"
        ocupacao = (total_rest / (CAPACITY_MONTHLY * 3)) * 100 if total_rest > 0 else 0
        risco = 'ALTO' if ocupacao > 100 else 'MÉDIO' if ocupacao > 50 else 'BAIXO'
        capacity_table.append({
            'name': tech_name, 'epicsAbertos': epics_str, 'horasNovo': round(novo_rest, 1),
            'horasUpsell': round(upsell_rest, 1), 'totalRestante': round(total_rest, 1),
            'meses': round(meses, 1), 'novosSimultaneos': novos_str,
            'ocupacao': round(ocupacao, 1), 'risco': risco
        })
    capacity_table.sort(key=lambda x: x['totalRestante'], reverse=True)

    insights = []
    overdue_novos = sum(1 for e in novo_open if e['statusPrazoType'] == 'overdue')
    if overdue_novos > 0:
        insights.append({'title': f'{overdue_novos} Novo epics ATRASADOS', 'text': f'{overdue_novos} ultrapassaram o prazo WMI.', 'level': 'danger'})
    high_risk = [t['name'] for t in capacity_table if t['risco'] == 'ALTO']
    if high_risk:
        insights.append({'title': f'{len(high_risk)} técnico(s) acima da capacidade', 'text': f'{", ".join(high_risk)} com ocupação >100%.', 'level': 'danger'})
    available = [t['name'] for t in capacity_table if t['ocupacao'] < 30]
    if available:
        insights.append({'title': f'Capacidade disponível: {", ".join(available)}', 'text': 'Espaço para absorver mais Novos.', 'level': 'success'})

    return {
        'backlogSummary': {
            'totalRestante': round(total_restante, 1), 'novoRestante': round(total_novo_restante, 1),
            'novoPercent': novo_pct, 'upsellRestante': round(upsell_restante, 1), 'upsellPercent': upsell_pct,
            'yasminEpics': len(fila_yasmin), 'yasminHours': round(yasmin_hours, 1), 'yasminPercent': yasmin_pct,
            'backlogMonths': round(backlog_months, 1)
        },
        'capacityTable': capacity_table, 'backlogNovo': novo_open,
        'filaYasmin': fila_yasmin, 'backlogInsights': insights
    }


def generate_dashboard_data(epics: List[Dict]) -> Dict:
    today = datetime.now().strftime('%Y-%m-%d')
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M BRT')
    technicians, yasmin_queue, cloud_migrations = process_epics(epics, today)
    technicians_array = []
    excluded_open_hours = 0.0

    for i, name in enumerate(IMPLEMENTERS):
        tech = technicians[name]
        open_count = tech['total'] - tech['completed']
        total_hours = tech['novoHours'] + tech['upsellHours']
        hoursPerEpic = (total_hours / tech['total']) if tech['total'] > 0 else 0
        strengths, risks = generate_strengths_risks(tech)

        ns = tech['novoStats']
        us = tech['upsellStats']
        ns['hoursPerEpic'] = round(ns['hours'] / ns['total'], 1) if ns['total'] > 0 else 0
        us['hoursPerEpic'] = round(us['hours'] / us['total'], 1) if us['total'] > 0 else 0
        ns['hours'] = round(ns['hours'], 1)
        us['hours'] = round(us['hours'], 1)

        technicians_array.append({
            'id': i + 1, 'name': name, 'total': tech['total'], 'completed': tech['completed'],
            'inProgress': tech['inProgress'], 'paused': tech['paused'], 'pending': tech['pending'],
            'waiting': tech['waiting'], 'hours': round(tech['hours'], 1),
            'hoursPerEpic': round(hoursPerEpic, 1), 'openCount': open_count,
            'overdueCount': tech['overdueCount'], 'zeroHoursOpen': tech['zeroHoursOpen'],
            'oldest': tech['oldest'], 'board': tech['board'],
            'novoHours': round(tech['novoHours'], 1), 'upsellHours': round(tech['upsellHours'], 1),
            'totalHours': round(total_hours, 1), 'openEpics': tech['openEpics'][:25],
            'riskLevel': generate_risk_level(tech),
            'strengths': strengths, 'risks': risks,
            'novoStats': ns, 'upsellStats': us
        })

    novo_summary = {'total': 0, 'completed': 0, 'inProgress': 0, 'paused': 0, 'pending': 0,
                    'totalHours': 0.0, 'activeHours': 0.0, 'avgHoursPerEpic': 0.0}
    upsell_summary = {'total': 0, 'completed': 0, 'inProgress': 0, 'paused': 0, 'pending': 0,
                      'totalHours': 0.0, 'activeHours': 0.0, 'avgHoursPerEpic': 0.0}

    hours_cutoff = f'{datetime.now().strftime("%Y")}-04-01'
    for epic in epics:
        fields = epic.get('fields', {})
        jira_sc_key = fields.get('status', {}).get('statusCategory', {}).get('key', '')
        status_cat = 'completed' if jira_sc_key == 'done' else get_status_category(fields.get('status', {}).get('name', ''))
        is_open = status_cat != 'completed'
        hours = (fields.get('aggregatetimespent', 0) or 0) / 3600
        epic_created = parse_date(fields.get('created', ''))
        include_hours = not epic_created or epic_created >= hours_cutoff
        kpi_hours = hours if include_hours else 0.0
        summary = novo_summary if classify_epic(epic) == 'Novo' else upsell_summary
        summary['total'] += 1
        if is_open: summary['totalHours'] += kpi_hours
        if status_cat == 'completed': summary['completed'] += 1
        elif status_cat == 'em_andamento': summary['inProgress'] += 1; summary['activeHours'] += kpi_hours
        elif status_cat == 'paused': summary['paused'] += 1
        elif status_cat == 'pendente': summary['pending'] += 1

    for s in [novo_summary, upsell_summary]:
        if s['total'] > 0:
            s['avgHoursPerEpic'] = round(s['totalHours'] / s['total'], 1)

    backlog_data = generate_backlog_data(technicians, epics, today)

    return {
        'timestamp': timestamp, 'technicians': technicians_array,
        'yasminQueue': yasmin_queue, 'migracaoCloud': cloud_migrations,
        'novoSummary': novo_summary, 'upsellSummary': upsell_summary,
        'excludedOpenHours': round(excluded_open_hours, 1),
        **{k: backlog_data[k] for k in backlog_data}
    }


# ─── Background refresh task ──────────────────────────────
async def refresh_data():
    """Fetch fresh data from Jira and update cache."""
    global _dashboard_cache
    try:
        if not all([JIRA_EMAIL, JIRA_API_TOKEN]):
            logger.warning("Jira credentials not set — skipping refresh")
            return
        client = JiraClient(JIRA_EMAIL, JIRA_API_TOKEN, JIRA_BASE_URL)
        epics = await client.get_epics()
        data = generate_dashboard_data(epics)
        async with _cache_lock:
            _dashboard_cache = data
        logger.info(f"Cache updated: {len(epics)} epics, {len(data.get('technicians', []))} technicians")
    except Exception as e:
        logger.error(f"Refresh failed: {e}", exc_info=True)


async def periodic_refresh():
    """Run refresh_data every REFRESH_INTERVAL seconds."""
    while True:
        await refresh_data()
        await asyncio.sleep(REFRESH_INTERVAL)


# ─── FastAPI App ───────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(periodic_refresh())
    yield
    task.cancel()

app = FastAPI(title="Dashboard Implantação WMI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/health")
async def health():
    has_data = bool(_dashboard_cache)
    return {"status": "ok", "has_data": has_data, "timestamp": _dashboard_cache.get("timestamp", "no data yet")}


@app.get("/api/data")
async def api_data():
    async with _cache_lock:
        return JSONResponse(content=_dashboard_cache or {"error": "Data not loaded yet"})


@app.post("/api/refresh")
async def api_refresh():
    await refresh_data()
    return {"status": "refreshed", "timestamp": _dashboard_cache.get("timestamp")}


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()
