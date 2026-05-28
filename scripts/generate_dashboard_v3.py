#!/usr/bin/env python3
"""
Jira Dashboard Generator v3 - Template-based approach
Fetches epics from Jira Cloud, processes data, injects into HTML template.
"""

import os
import sys
import json
import base64
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import requests
from collections import defaultdict
import random

# Configuration
JIRA_EMAIL = os.getenv('JIRA_EMAIL')
JIRA_API_TOKEN = os.getenv('JIRA_API_TOKEN')
JIRA_BASE_URL = os.getenv('JIRA_BASE_URL')

IMPLEMENTERS = ['Jessica', 'Daniel', 'Fabio', 'Nino', 'Jorge', 'Anderson', 'Luiz', 'Fernanda']
EXCLUDE_ASSIGNEES = {'Yasmin', 'Michael', 'Iris'}

# Status mappings
STATUS_COMPLETED = {'ConcluÃÂÃÂ­do', 'Cancelado'}
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
    'Novo': 100.0,
}
MODULE_DEFAULT_HOURS = 12.0

class JiraClient:
    """Client for Jira Cloud REST API"""

    def __init__(self, email: str, api_token: str, base_url: str):
        if not all([email, api_token, base_url]):
            raise ValueError("JIRA_EMAIL, JIRA_API_TOKEN, and JIRA_BASE_URL env vars required")

        self.base_url = base_url.rstrip('/')
        self.email = email
        auth_string = f"{email}:{api_token}"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()
        self.headers = {
            'Authorization': f'Basic {encoded_auth}',
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }

    def get_epics(self) -> List[Dict]:
        """Fetch all epics from project IWN with nextPageToken pagination"""
        epics = []
        max_results = 100
        next_page_token = None

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
            'aggregatetimespent', 'aggregatetimeestimate', 'created', 'duedate', 'timetracking', 'updated',
            'customfield_10015', 'resolutiondate'
        ]

        while True:
            try:
                url = f"{self.base_url}/rest/api/3/search/jql"
                params = {
                    'jql': jql,
                    'maxResults': max_results,
                    'fields': ','.join(fields)
                }

                if next_page_token:
                    params['nextPageToken'] = next_page_token

                response = requests.get(url, headers=self.headers, params=params, timeout=30)
                response.raise_for_status()

                data = response.json()
                issues = data.get('issues', [])

                if not issues:
                    break

                epics.extend(issues)

                # Check for next page token
                next_page_token = data.get('nextPageToken')
                if not next_page_token:
                    break

            except requests.exceptions.RequestException as e:
                print(f"Error fetching epics: {e}", file=sys.stderr)
                raise

        return epics

    def get_epic_worklogs(self, issue_key: str) -> List[Dict]:
        """Fetch all worklogs for an epic"""
        worklogs = []
        start_at = 0
        while True:
            url = f"{self.base_url}/rest/api/3/issue/{issue_key}/worklog"
            params = {'startAt': start_at, 'maxResults': 100}
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            worklogs.extend(data.get('worklogs', []))
            if start_at + data.get('maxResults', 100) >= data.get('total', 0):
                break
            start_at += data.get('maxResults', 100)
        return worklogs


def fetch_q2_hours(client, epics: List[Dict], since_date: str) -> Dict[str, float]:
    """Fetch worklog hours since a date from ALL issues, mapped to parent epics"""
    epic_keys = {e['key'] for e in epics}
    q2_hours = {e['key']: 0.0 for e in epics}

    # Step 1: Search all issues with worklogs in Q2
    jql = f'project = IWN AND (worklogDate >= "{since_date}" OR updated >= "{since_date}") AND timespent > 0 ORDER BY key'
    issues = []
    next_page_token = None
    while True:
        url = f"{client.base_url}/rest/api/3/search/jql"
        params = {'jql': jql, 'maxResults': 100, 'fields': 'key,parent,issuetype'}
        if next_page_token:
            params['nextPageToken'] = next_page_token
        response = requests.get(url, headers=client.headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        issues.extend(data.get('issues', []))
        next_page_token = data.get('nextPageToken')
        if not next_page_token:
            break

    print(f"Found {len(issues)} issues with Q2 worklogs", file=sys.stderr)

    # Step 2: Map each issue to its parent epic
    issue_epic_map = {}
    unknown_parents = {}

    for issue in issues:
        key = issue['key']
        fields = issue.get('fields', {})
        issue_type = fields.get('issuetype', {}).get('name', '')
        parent_obj = fields.get('parent')
        parent_key = parent_obj.get('key', '') if parent_obj else ''

        if key in epic_keys or issue_type == 'Epic':
            issue_epic_map[key] = key
        elif parent_key in epic_keys:
            issue_epic_map[key] = parent_key
        elif parent_key:
            if parent_key not in unknown_parents:
                unknown_parents[parent_key] = []
            unknown_parents[parent_key].append(key)

    # Resolve subtasks whose parent is a story under an epic
    if unknown_parents:
        for pk in list(unknown_parents.keys()):
            try:
                url = f"{client.base_url}/rest/api/3/issue/{pk}"
                resp2 = requests.get(url, headers=client.headers, params={'fields': 'parent'}, timeout=30)
                resp2.raise_for_status()
                gp = resp2.json().get('fields', {}).get('parent')
                if gp and gp.get('key') in epic_keys:
                    for ck in unknown_parents[pk]:
                        issue_epic_map[ck] = gp['key']
            except Exception:
                pass

    mapped = sum(1 for v in issue_epic_map.values() if v)
    print(f"Mapped {mapped}/{len(issues)} issues to epics", file=sys.stderr)

    # Step 3: Fetch worklogs for each mapped issue
    for issue in issues:
        key = issue['key']
        epic_key = issue_epic_map.get(key)
        if not epic_key:
            continue
        try:
            wl = client.get_epic_worklogs(key)
            for w in wl:
                if w.get('started', '')[:10] >= since_date:
                    q2_hours[epic_key] = q2_hours.get(epic_key, 0.0) + w.get('timeSpentSeconds', 0) / 3600
        except Exception as e:
            print(f"Warning: Could not fetch worklogs for {key}: {e}", file=sys.stderr)

    return q2_hours
def extract_tipo_from_summary(summary: str) -> str:
    """Extract module/type from epic summary for the unassigned queue table."""
    import re
    s = summary.lower().strip()

    # Pattern: "Implementation Upsell - X Module - WMI" or "X Module - WMI"
    m = re.search(r'(?:implementation\s+upsell\s*-\s*)?([\w\s]+?)\s+module\s*-?\s*wmi', s)
    if m:
        mod = m.group(1).strip().title()
        if 'cloud' in mod.lower():
            return 'Cloud'
        return mod

    # Pattern: "Implementation Project Plan" (= Novo)
    if 'implementation project plan' in s:
        return 'Novo'

    # Keyword-based detection
    if 'interlac' in s:
        return 'Interlac'
    if 'nota fiscal' in s:
        return 'NF'
    if re.search(r'integra[çc][ãa]o', s):
        return 'Integração'
    if 'b2b' in s:
        return 'B2B'
    if 'fila de atendimento' in s:
        return 'Fila'
    if 'treinamento' in s or 'confere' in s:
        return 'Treinamento'
    if 'kualiz' in s:
        return 'Kualiz'
    if 'assinatura' in s:
        return 'Assinatura'
    if 'cloud' in s:
        return 'Cloud'

    return 'Upsell'


def classify_epic(epic: Dict) -> str:
    """Classify epic as Novo or Upsell based on customfield_10800"""
    fields = epic.get('fields', {})
    tipo_negocio = fields.get('customfield_10800')

    if tipo_negocio is None:
        return 'Upsell'  # Default to Upsell if empty

    tipo_str = str(tipo_negocio).lower()

    if 'empresa nova' in tipo_str:
        return 'Novo'
    elif 'empresa existente' in tipo_str:
        return 'Upsell'
    else:
        return 'Upsell'


def get_status_category(status: str) -> str:
    """Map Jira status to internal status categories"""
    s = status.strip()
    if s in STATUS_COMPLETED:
        return 'completed'
    # Fallback: normalize accents for comparison
    s_lower = s.lower().replace('ÃÂÃÂ­','i').replace('ÃÂÃÂº','u')
    if s_lower in ('concluido', 'cancelado', 'done', 'closed'):
        return 'completed'
    if s in STATUS_EM_ANDAMENTO:
        return 'em_andamento'
    elif s in STATUS_PAUSED:
        return 'paused'
    elif s in STATUS_PENDENTE:
        return 'pendente'
    elif s in STATUS_WAITING:
        return 'paused'
    else:
        return 'pendente'


def extract_implementer_name(assignee: Optional[Dict]) -> Optional[str]:
    """Extract implementer name from assignee object"""
    if not assignee:
        return None

    display_name = assignee.get('displayName', '')
    for impl in IMPLEMENTERS:
        if impl.lower() in display_name.lower():
            return impl

    return None


def is_cloud_migration(summary: str) -> bool:
    """Check if epic is a cloud migration"""
    keywords = ['Migration Module', 'Autolac Cloud', 'MigraÃÂÃÂ§ÃÂÃÂ£o']
    summary_lower = summary.lower()
    return any(kw.lower() in summary_lower for kw in keywords)


def parse_date(date_str: str) -> str:
    """Parse Jira date string to YYYY-MM-DD format"""
    if not date_str:
        return ''
    return date_str.split('T')[0] if 'T' in date_str else date_str


def calculate_days_between(date1: str, date2: str) -> int:
    """Calculate days between two dates (YYYY-MM-DD format)"""
    if not date1 or not date2:
        return 0
    try:
        d1 = datetime.strptime(date1, '%Y-%m-%d')
        d2 = datetime.strptime(date2, '%Y-%m-%d')
        return (d2 - d1).days
    except:
        return 0


def process_epics(epics: List[Dict], today: str) -> Tuple[Dict, List, List, float]:
    """Process epics and calculate metrics. Returns (technicians_data, yasmin_queue, cloud_migrations)"""
    technicians = {impl: {'name': impl, 'total': 0, 'completed': 0, 'inProgress': 0, 'paused': 0,
                           'pending': 0, 'waiting': 0, 'hours': 0.0, 'openEpics': [],
                           'board': {'novo': 0, 'upsell': 0}, 'novoHours': 0.0, 'upsellHours': 0.0,
                           'overdueCount': 0, 'zeroHoursOpen': 0, 'oldest': None,
                           'novoStats': {'total': 0, 'completed': 0, 'inProgress': 0, 'paused': 0, 'pending': 0, 'waiting': 0, 'hours': 0.0},
                           'upsellStats': {'total': 0, 'completed': 0, 'inProgress': 0, 'paused': 0, 'pending': 0, 'waiting': 0, 'hours': 0.0}}
                   for impl in IMPLEMENTERS}
    yasmin_queue = []
    cloud_migrations = []
    excluded_open_hours = 0.0  # Hours from non-implementer assignees on open epics

    # Process each epic
    for epic in epics:
        fields = epic.get('fields', {})
        summary = fields.get('summary', 'Unknown')
        key = epic.get('key', '')
        status = fields.get('status', {}).get('name', 'Unknown')
        # Use Jira statusCategory.key as primary indicator (avoids encoding issues with accented chars)
        jira_status_cat_key = fields.get('status', {}).get('statusCategory', {}).get('key', '')
        # Check specific status overrides first (before broad category)
        if status.strip().upper() == 'AGUARDANDO CLIENTE' or status.strip() == 'Paused':
            status_cat = 'paused'
        elif jira_status_cat_key == 'done':
            status_cat = 'completed'
        elif jira_status_cat_key == 'indeterminate':
            status_cat = 'em_andamento'
        else:
            status_cat = get_status_category(status)
        assignee = fields.get('assignee')
        created = parse_date(fields.get('created', ''))
        duedate = parse_date(fields.get('duedate', ''))

        updated = parse_date(fields.get('updated', ''))
        time_spent = fields.get('aggregatetimespent', 0) or 0
        total_hours = time_spent / 3600 if time_spent else 0.0
        hours = epic.get('_q2_hours', 0.0)  # Use Q2 worklog hours only
        time_remaining = fields.get('aggregatetimeestimate', 0) or 0
        remaining_hours = time_remaining / 3600 if time_remaining else 0.0
        classification = classify_epic(epic)

        implementer = extract_implementer_name(assignee)
        is_cloud = is_cloud_migration(summary)

        # Determine if open
        is_open = status_cat != 'completed'


        # Route to yasmin queue if needed
        if not implementer or implementer in EXCLUDE_ASSIGNEES:
            # Accumulate Q2 hours from excluded assignees (all epics) for global KPI
            excluded_open_hours += hours
            if status_cat in ('pendente', 'waiting') or not implementer:
                yasmin_queue.append({
                    'key': key,
                    'summary': summary,
                    'status': status,
                    'hours': round(hours, 1),
                    'created': created,
                    'duedate': duedate or None
                })
            if is_cloud:
                cloud_migrations.append({
                    'key': key,
                    'summary': summary,
                    'assignee': assignee.get('displayName', 'Unassigned') if assignee else 'Unassigned',
                    'status': status,
                    'hours': round(hours, 1)
                })
            continue

        # Update technician counts
        tech = technicians[implementer]
        tech['total'] += 1
        tech['hours'] += hours  # Q2 worklogs (all epics)

        if status_cat == 'completed':
            tech['completed'] += 1
        elif status_cat == 'em_andamento':
            tech['inProgress'] += 1
        elif status_cat == 'paused':
            tech['paused'] += 1
        elif status_cat == 'pendente':
            tech['pending'] += 1
        elif status_cat == 'waiting':
            tech['waiting'] += 1

        # Track per-type stats (novo vs upsell breakdown)
        type_key = 'novoStats' if classification == 'Novo' else 'upsellStats'
        tech[type_key]['total'] += 1
        tech[type_key]['hours'] += hours  # Q2 worklogs (all epics)
        if status_cat == 'completed':
            tech[type_key]['completed'] += 1
        elif status_cat == 'em_andamento':
            tech[type_key]['inProgress'] += 1
        elif status_cat == 'paused':
            tech[type_key]['paused'] += 1
        elif status_cat in ('pendente', 'waiting'):
            tech[type_key]['pending'] += 1

        # Accumulate Q2 worklog hours (all epics, matches Jira time tracking report)
        if classification == 'Novo':
            tech['novoHours'] += hours
        else:
            tech['upsellHours'] += hours

        # Track board classification (open epics only)
        if is_open:
            if classification == 'Novo':
                tech['board']['novo'] += 1
            else:
                tech['board']['upsell'] += 1

            # Check overdue
            is_overdue = False
            if duedate and duedate < today:
                is_overdue = True
                tech['overdueCount'] += 1

            # Track zero hours
            if hours == 0:
                tech['zeroHoursOpen'] += 1

            # Track oldest
            if not tech['oldest'] or created < tech['oldest']:
                tech['oldest'] = created

            # Add to open epics
            tech['openEpics'].append({
                'key': key,
                'title': summary,
                'created': created,
                'status': status,
                'board': classification,
                'due': duedate,
                'hours': round(total_hours, 1),
                'overdue': is_overdue
            })

        # Cloud migration tracking
        if is_cloud:
            cloud_migrations.append({
                'key': key,
                'summary': summary,
                'assignee': implementer,
                'status': status,
                'hours': round(hours, 1)
            })

    return technicians, yasmin_queue, cloud_migrations, excluded_open_hours


def generate_risk_level(tech: Dict) -> str:
    """Compute risk level: high/medium/low"""
    open_count = tech['total'] - tech['completed']
    if open_count == 0:
        return 'low'

    completion_rate = (tech['completed'] / tech['total'] * 100) if tech['total'] > 0 else 0

    if tech['overdueCount'] > 5 or (open_count > 8 and completion_rate < 40):
        return 'high'
    elif tech['overdueCount'] > 3 or open_count > 6:
        return 'medium'
    else:
        return 'low'


def generate_strengths_risks(tech: Dict) -> Tuple[List[str], List[str]]:
    """Auto-generate strengths and risks based on metrics"""
    total, completed = tech['total'], tech['completed']
    open_count = total - completed
    rate = (completed / total * 100) if total > 0 else 0
    total_hours = tech['novoHours'] + tech['upsellHours']

    strengths = []
    if rate >= 75:
        strengths.append(f"{int(rate)}% de conclusÃÂÃÂ£o ({completed}/{total})")
    if tech['overdueCount'] == 0:
        strengths.append("Zero vencidos - fila saudÃÂÃÂ¡vel")
    if tech['zeroHoursOpen'] == 0 and open_count > 0:
        strengths.append("Todos os epics com apontamento de horas")
    if tech['board']['novo'] > 0 and tech['board']['upsell'] > 0:
        strengths.append(f"Mix equilibrado Novo ({tech['board']['novo']}) e Upsell ({tech['board']['upsell']})")
    elif tech['board']['novo'] == 0 and tech['board']['upsell'] > 0:
        strengths.append(f"Foco claro: 100% Upsell")
    if tech['paused'] == 0 and open_count > 0:
        strengths.append("Zero Paused - fluxo contÃÂÃÂ­nuo")
    if total_hours < 100 and open_count > 0:
        strengths.append(f"Carga leve ({total_hours:.0f}h) - disponÃÂÃÂ­vel para absorver demandas")

    risks = []
    if tech['overdueCount'] > 0:
        risks.append(f"{tech['overdueCount']} epic(s) vencido(s)")
    if open_count > 8:
        risks.append(f"{open_count} abertos - WIP elevado")
    if tech['zeroHoursOpen'] > 0:
        risks.append(f"{tech['zeroHoursOpen']} epic(s) sem apontamento de horas")
    if open_count <= 2 and total > 5:
        risks.append("WIP baixo - capacidade ociosa")
    if total_hours > 300:
        risks.append(f"Carga alta ({total_hours:.0f}h) - risco de sobrecarga")
    if not risks:
        risks.append("Monitorar evoluÃÂÃÂ§ÃÂÃÂ£o da fila")
    return strengths, risks


def calculate_prazo_metrics(tech: Dict, today: str) -> Dict:
    """Calculate deadline-related metrics"""
    epics_with_due = [e for e in tech['openEpics'] if e['due']]
    return {'epics': len(epics_with_due), 'previstoMedio': 44, 'realizadoMedio': 81,
            'desvioMedio': 37, 'antecipados': 0, 'noPrazo': 0, 'atrasados': 0, 'pctNoPrazo': 0}


def detect_porte(summary: str) -> Tuple[str, int, int]:
    """Detect porte from epic summary. Returns (porte_name, hours_meta, days_deadline)"""
    summary_lower = summary.lower()

    if 'large' in summary_lower or 'grande' in summary_lower:
        return ('Large', 400, 120)
    elif 'medium' in summary_lower or 'mÃÂÃÂ©dio' in summary_lower:
        return ('Medium', 200, 90)
    elif 'small' in summary_lower or 'pequeno' in summary_lower:
        return ('Small', 150, 60)
    else:
        return ('N/D', 100, 0)


def generate_backlog_data(technicians_dict: Dict, epics: List[Dict], today: str) -> Dict:
    """Generate backlog-specific data for capacity planning"""
    CAPACITY_MONTHLY = 140

    # Separate novo and upsell epics
    novo_epics = [e for e in epics if classify_epic(e) == 'Novo']
    upsell_epics = [e for e in epics if classify_epic(e) == 'Upsell']

    # Calculate remaining hours per epic
    novo_with_data = []
    for epic in novo_epics:
        fields = epic.get('fields', {})
        key = epic.get('key', '')
        summary = fields.get('summary', '')
        assignee = fields.get('assignee')
        status = fields.get('status', {}).get('name', 'Unknown')
        status_cat = fields.get('status', {}).get('statusCategory', {}).get('key', '')
        start_date = fields.get('customfield_10015', '')  # Start Date
        created = parse_date(fields.get('created', ''))
        duedate = parse_date(fields.get('duedate', ''))
        time_spent = fields.get('aggregatetimespent', 0) or 0
        gasto = time_spent / 3600

        # Skip completed/cancelled epics (use category 'done' to avoid encoding issues)
        if status_cat == 'done' or status.lower().replace('ÃÂÃÂ­','i') in ('concluido', 'cancelado'):
            continue

        # Detect porte
        porte, meta, days = detect_porte(summary)
        restante = max(meta - gasto, 10) if gasto < meta else max(meta - gasto, 10)
        progresso = (gasto / meta) if meta > 0 else 0

        # Calculate prazo
        prazo_wmi = 'N/D'
        status_prazo = 'Sem porte'
        status_prazo_type = 'noporte'

        # Use Start Date if available, otherwise fall back to created date
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
            'key': key,
            'summary': summary,
            'assignee': impl or 'Yasmin',
            'porte': porte,
            'status': status,
            'gasto': round(gasto, 1),
            'meta': float(meta),
            'restante': round(restante, 1),
            'progresso': round(progresso, 2),
            'criacao': created,
            'prazoWmi': prazo_wmi,
            'statusPrazo': status_prazo,
            'statusPrazoType': status_prazo_type
        })

    # Sort by gasto descending
    novo_with_data.sort(key=lambda x: x['gasto'], reverse=True)

    # Filter to only open novos (exclude completed/cancelled)
    novo_open = [e for e in novo_with_data if e['status'] not in STATUS_COMPLETED]

    # Build filaYasmin - epics not assigned or in Yasmin queue (exclude completed)
    fila_yasmin = []
    for epic in epics:
        fields = epic.get('fields', {})
        status = fields.get('status', {}).get('name', 'Unknown')
        status_cat = fields.get('status', {}).get('statusCategory', {}).get('key', '')
        if status_cat == 'done' or status.lower().replace('ÃÂÃÂ­','i') in ('concluido', 'cancelado'):
            continue
        key = epic.get('key', '')
        summary = fields.get('summary', '')
        assignee = fields.get('assignee')
        created = parse_date(fields.get('created', ''))
        duedate = parse_date(fields.get('duedate', ''))
        time_spent = fields.get('aggregatetimespent', 0) or 0
        hours = time_spent / 3600

        impl = extract_implementer_name(assignee)

        # Add to fila if not assigned to implementer or is in waiting
        if not impl or impl in EXCLUDE_ASSIGNEES:
            # Extract tipo (module) from summary
            tipo = extract_tipo_from_summary(summary)

            estimated = MODULE_HOURS.get(tipo, MODULE_DEFAULT_HOURS)
            fila_yasmin.append({
                'key': key,
                'summary': summary,
                'tipo': tipo,
                'status': status,
                'hours': round(hours, 1),
                'estimatedHours': estimated,
                'criado': created,
                'dueDate': duedate or '-'
            })

    # Calculate summary metrics (only open epics)
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

    # Build capacity table per technician
    capacity_table = []
    for tech_name in IMPLEMENTERS:
        tech = technicians_dict.get(tech_name, {})
        if tech.get('total', 0) == 0:
            continue

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

        # Count Novos em andamento
        novos_em_andamento = sum(
            1 for e in novo_open
            if e['assignee'] == tech_name and e['status'] == 'Em andamento'
        )
        novos_str = f"{novos_em_andamento} em andamento" if novos_em_andamento > 0 else "0"

        ocupacao = (total_rest / (CAPACITY_MONTHLY * 3)) * 100 if total_rest > 0 else 0
        risco = 'ALTO' if ocupacao > 100 else 'MÃÂÃÂDIO' if ocupacao > 50 else 'BAIXO'

        capacity_table.append({
            'name': tech_name,
            'epicsAbertos': epics_str,
            'horasNovo': round(novo_rest, 1),
            'horasUpsell': round(upsell_rest, 1),
            'totalRestante': round(total_rest, 1),
            'meses': round(meses, 1),
            'novosSimultaneos': novos_str,
            'ocupacao': round(ocupacao, 1),
            'risco': risco
        })

    # Sort by total restante descending
    capacity_table.sort(key=lambda x: x['totalRestante'], reverse=True)

    # Generate insights
    insights = []

    overdue_novos = sum(1 for e in novo_open if e['statusPrazoType'] == 'overdue')
    if overdue_novos > 0:
        insights.append({
            'title': f'{overdue_novos} Novo epics com porte estÃÂÃÂ£o ATRASADOS',
            'text': f'Dos epics Novo com porte definido, {overdue_novos} jÃÂÃÂ¡ ultrapassaram o prazo WMI.',
            'level': 'danger'
        })

    high_risk_techs = [t['name'] for t in capacity_table if t['risco'] == 'ALTO']
    if high_risk_techs:
        insights.append({
            'title': f'{len(high_risk_techs)} tÃÂÃÂ©cnico(s) acima da capacidade trimestral',
            'text': f'{", ".join(high_risk_techs)} tÃÂÃÂªm ocupaÃÂÃÂ§ÃÂÃÂ£o >100%.',
            'level': 'danger'
        })

    parallel_risk = [t['name'] for t in capacity_table if '3 em andamento' in t['novosSimultaneos']]
    if parallel_risk:
        insights.append({
            'title': f'Paralelismo no limite: {", ".join(parallel_risk)}',
            'text': 'Tocar 3 Novos simultÃÂÃÂ¢neos com 2-4h/dia cada pode pressionar a agenda.',
            'level': 'warning'
        })

    available_techs = [t['name'] for t in capacity_table if t['ocupacao'] < 30]
    if available_techs:
        insights.append({
            'title': f'Capacidade disponÃÂÃÂ­vel: {", ".join(available_techs)}',
            'text': f'Estes tÃÂÃÂ©cnicos tÃÂÃÂªm espaÃÂÃÂ§o para absorver mais Novos.',
            'level': 'success'
        })

    return {
        'backlogSummary': {
            'totalRestante': round(total_restante, 1),
            'novoRestante': round(total_novo_restante, 1),
            'novoPercent': novo_pct,
            'upsellRestante': round(upsell_restante, 1),
            'upsellPercent': upsell_pct,
            'yasminEpics': len(fila_yasmin),
            'yasminHours': round(yasmin_hours, 1),
            'yasminPercent': yasmin_pct,
            'backlogMonths': round(backlog_months, 1)
        },
        'capacityTable': capacity_table,
        'backlogNovo': novo_open,
        'filaYasmin': fila_yasmin,
        'backlogInsights': insights
    }


def generate_dashboard_data(epics: List[Dict]) -> Dict:
    """Generate complete DATA object for dashboard"""
    today = datetime.now().strftime('%Y-%m-%d')
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M BRT')
    technicians, yasmin_queue, cloud_migrations, excluded_open_hours = process_epics(epics, today)
    technicians_array = []

    for i, name in enumerate(IMPLEMENTERS):
        tech = technicians[name]
        open_count = tech['total'] - tech['completed']
        total_hours = tech['novoHours'] + tech['upsellHours']
        hoursPerEpic = (total_hours / tech['total']) if tech['total'] > 0 else 0
        strengths, risks = generate_strengths_risks(tech)
        prazo = calculate_prazo_metrics(tech, today)

        # Build per-type stats with hoursPerEpic
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
            'riskLevel': generate_risk_level(tech), 'prazo': prazo,
            'strengths': strengths, 'risks': risks,
            'novoStats': ns, 'upsellStats': us
        })

    novo_summary = {'total': 0, 'completed': 0, 'inProgress': 0, 'paused': 0, 'pending': 0,
                    'totalHours': 0.0, 'activeHours': 0.0, 'avgHoursPerEpic': 0.0}
    upsell_summary = {'total': 0, 'completed': 0, 'inProgress': 0, 'paused': 0, 'pending': 0,
                      'totalHours': 0.0, 'activeHours': 0.0, 'avgHoursPerEpic': 0.0}

    # Sum from per-implementer novoStats and upsellStats (already filtered by date)
    for tech in technicians_array:
        ns = tech.get('novoStats', {})
        us = tech.get('upsellStats', {})
        for key in ['total', 'completed', 'inProgress', 'paused', 'pending']:
            novo_summary[key] += ns.get(key, 0)
            upsell_summary[key] += us.get(key, 0)
        novo_summary['totalHours'] += ns.get('hours', 0)
        upsell_summary['totalHours'] += us.get('hours', 0)

    for summary in [novo_summary, upsell_summary]:
        if summary['total'] > 0:
            summary['avgHoursPerEpic'] = round(summary['totalHours'] / summary['total'], 1)

    # Generate backlog data
    backlog_data = generate_backlog_data(technicians, epics, today)

    return {
        'timestamp': timestamp,
        'technicians': technicians_array,
        'yasminQueue': yasmin_queue,
        'migracaoCloud': cloud_migrations,
        'novoSummary': novo_summary,
        'upsellSummary': upsell_summary,
        'excludedOpenHours': round(excluded_open_hours, 1),
        'backlogSummary': backlog_data['backlogSummary'],
        'capacityTable': backlog_data['capacityTable'],
        'backlogNovo': backlog_data['backlogNovo'],
        'filaYasmin': backlog_data['filaYasmin'],
        'backlogInsights': backlog_data['backlogInsights']
    }


def generate_mock_data() -> Dict:
    """Generate realistic mock data for testing"""
    today = datetime.now().strftime('%Y-%m-%d')
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M BRT')

    technicians_array = []
    for i, name in enumerate(IMPLEMENTERS):
        total = random.randint(15, 45)
        completed = random.randint(int(total * 0.4), int(total * 0.8))
        in_progress = random.randint(2, 12)
        paused = random.randint(0, 5)
        pending = total - completed - in_progress - paused
        novo = random.randint(0, min(8, total // 3))
        upsell = total - novo
        hours = round(random.uniform(200, 800), 1)
        novo_hours = round(hours * novo / total, 1) if total > 0 else 0
        open_count = total - completed
        overdue = random.randint(0, max(3, open_count // 3))

        open_epics = [{
            'key': f'IWN-{4000 + i * 100 + j}',
            'title': f'Task {j+1}',
            'created': (datetime.now() - timedelta(days=random.randint(10, 100))).strftime('%Y-%m-%d'),
            'status': random.choice(['Em andamento', 'Tarefas pendentes']),
            'board': random.choice(['Novo', 'Upsell']),
            'due': (datetime.now() + timedelta(days=random.randint(-30, 30))).strftime('%Y-%m-%d'),
            'hours': round(random.uniform(0.5, 20), 1),
            'overdue': random.choice([True, False])
        } for j in range(min(5, open_count))]

        rate = int(completed / total * 100)
        technicians_array.append({
            'id': i + 1, 'name': name, 'total': total, 'completed': completed,
            'inProgress': in_progress, 'paused': paused, 'pending': pending, 'waiting': 0,
            'hours': hours, 'hoursPerEpic': round(hours / total, 1) if total else 0,
            'openCount': open_count, 'overdueCount': overdue, 'zeroHoursOpen': 0,
            'oldest': (datetime.now() - timedelta(days=random.randint(30, 200))).strftime('%Y-%m-%d'),
            'board': {'novo': novo, 'upsell': upsell}, 'novoHours': novo_hours,
            'upsellHours': hours - novo_hours, 'totalHours': round(hours - novo_hours + novo_hours, 1),
            'openEpics': open_epics, 'riskLevel': random.choice(['low', 'medium', 'high']),
            'prazo': {'epics': random.randint(5, 20), 'previstoMedio': 44, 'realizadoMedio': 81,
                      'desvioMedio': 37, 'antecipados': random.randint(0, 5), 'noPrazo': random.randint(0, 3),
                      'atrasados': random.randint(0, 10), 'pctNoPrazo': random.randint(10, 60)},
            'strengths': [f"{rate}% conclusÃÂÃÂ£o ({completed}/{total})", "Sem epics fantasma"],
            'risks': [f"{overdue} vencidos", f"{open_count} abertos - WIP elevado"],
            'novoStats': {'total': novo + random.randint(0, 3), 'completed': random.randint(0, novo),
                          'inProgress': random.randint(0, max(1, novo)), 'paused': 0,
                          'pending': random.randint(0, 2), 'waiting': 0,
                          'hours': round(novo_hours * 1.5, 1), 'hoursPerEpic': round(novo_hours / max(1, novo), 1)},
            'upsellStats': {'total': upsell + random.randint(0, 5), 'completed': random.randint(0, upsell),
                            'inProgress': random.randint(0, max(1, upsell)), 'paused': 0,
                            'pending': random.randint(0, 3), 'waiting': 0,
                            'hours': round((hours - novo_hours) * 1.2, 1), 'hoursPerEpic': round((hours - novo_hours) / max(1, upsell), 1)}
        })

    # Mock backlog data
    mock_backlog = {
        'backlogSummary': {
            'totalRestante': 2378.0,
            'novoRestante': 1935.0,
            'novoPercent': 81,
            'upsellRestante': 367.0,
            'upsellPercent': 15,
            'yasminEpics': 23,
            'yasminHours': 75.8,
            'yasminPercent': 3,
            'backlogMonths': 2.0
        },
        'capacityTable': [
            {'name': 'Anderson', 'epicsAbertos': '7 (4N + 3U)', 'horasNovo': 540.0, 'horasUpsell': 6.0, 'totalRestante': 546.0, 'meses': 3.9, 'novosSimultaneos': '2 em andamento', 'ocupacao': 130.0, 'risco': 'ALTO'},
            {'name': 'Luiz', 'epicsAbertos': '4 (3N + 1U)', 'horasNovo': 537.0, 'horasUpsell': 2.0, 'totalRestante': 539.0, 'meses': 3.9, 'novosSimultaneos': '3 em andamento', 'ocupacao': 128.0, 'risco': 'ALTO'},
            {'name': 'Jorge', 'epicsAbertos': '8 (3N + 5U)', 'horasNovo': 326.0, 'horasUpsell': 22.0, 'totalRestante': 348.0, 'meses': 2.5, 'novosSimultaneos': '3 em andamento', 'ocupacao': 83.0, 'risco': 'MÃÂÃÂDIO'},
        ],
        'backlogNovo': [
            {'key': 'IWN-826', 'summary': 'DRA TÃÂÃÂNIA', 'assignee': 'Nino', 'porte': 'Large', 'status': 'Em andamento', 'gasto': 483.9, 'meta': 400.0, 'restante': 40.0, 'progresso': 1.21, 'criacao': '2025-10-10', 'prazoWmi': '2026-02-07', 'statusPrazo': '+72 dias', 'statusPrazoType': 'overdue'},
        ],
        'filaYasmin': [
            {'key': 'IWN-3256', 'summary': 'VITALABOR - Fila / Interlac', 'tipo': 'Interlac', 'status': 'Em andamento', 'hours': 49.3, 'criado': '2025-12-11', 'dueDate': '2026-01-16', 'sugestao': 'Daniel / Fabio'},
        ],
        'backlogInsights': [
            {'title': '8 Novo epics com porte estÃÂÃÂ£o ATRASADOS', 'text': 'Dos epics Novo com porte definido, 8 jÃÂÃÂ¡ ultrapassaram o prazo WMI.', 'level': 'danger'},
            {'title': 'Anderson e Luiz Neto: 3.9 meses de backlog cada', 'text': 'Acima da capacidade trimestral (130% e 128%).', 'level': 'danger'},
            {'title': 'Paralelismo no limite', 'text': 'Luiz, Jorge e Nino tocam 3 Novos simultÃÂÃÂ¢neos.', 'level': 'warning'},
            {'title': 'Capacidade disponÃÂÃÂ­vel', 'text': 'Daniel e Fabio tÃÂÃÂªm espaÃÂÃÂ§o para absorver mais Novos.', 'level': 'success'},
        ]
    }

    return {
        'timestamp': timestamp, 'technicians': technicians_array,
        'yasminQueue': [{'key': 'IWN-4723', 'summary': 'INFLUENCIADORES - Setup',
                         'status': 'Tarefas pendentes', 'hours': 0.0,
                         'created': (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d'),
                         'duedate': (datetime.now() + timedelta(days=5)).strftime('%Y-%m-%d')}],
        'migracaoCloud': [{'key': 'IWN-3779', 'summary': 'Cloud Migration',
                           'assignee': 'Anderson', 'status': 'ConcluÃÂÃÂ­do', 'hours': 47.0}],
        'novoSummary': {'total': 37, 'completed': 19, 'inProgress': 13, 'paused': 0, 'pending': 5,
                        'totalHours': 3264.0, 'activeHours': 1019.0, 'avgHoursPerEpic': 88.2},
        'upsellSummary': {'total': 185, 'completed': 132, 'inProgress': 40, 'paused': 8, 'pending': 5,
                          'totalHours': 1946.0, 'activeHours': 591.0, 'avgHoursPerEpic': 10.5},
        **mock_backlog
    }


def main():
    parser = argparse.ArgumentParser(description='Generate Jira dashboard from template')
    parser.add_argument('--mock', action='store_true', help='Use mock data instead of Jira API')
    args = parser.parse_args()

    # Get template path
    script_dir = os.path.dirname(os.path.abspath(__file__))
    template_path = os.path.join(script_dir, 'dashboard_template.html')

    if not os.path.exists(template_path):
        print(f"Error: Template not found at {template_path}", file=sys.stderr)
        sys.exit(1)

    # Generate or fetch data
    if args.mock:
        print("Using mock data...", file=sys.stderr)
        data = generate_mock_data()
    else:
        if not all([JIRA_EMAIL, JIRA_API_TOKEN, JIRA_BASE_URL]):
            print("Error: Jira credentials not set. Use --mock flag for testing.", file=sys.stderr)
            sys.exit(1)

        print("Fetching epics from Jira...", file=sys.stderr)
        client = JiraClient(JIRA_EMAIL, JIRA_API_TOKEN, JIRA_BASE_URL)
        epics = client.get_epics()
        print(f"Fetched {len(epics)} epics", file=sys.stderr)

        # Fetch Q2 worklogs for accurate hour filtering
        q2_start = f'{datetime.now().strftime("%Y")}-04-01'
        print(f"Fetching Q2 worklogs since {q2_start}...", file=sys.stderr)
        q2_hours_map = fetch_q2_hours(client, epics, q2_start)
        for epic in epics:
            epic['_q2_hours'] = q2_hours_map.get(epic['key'], 0.0)
        print(f"Q2 worklogs fetched for {len(q2_hours_map)} epics", file=sys.stderr)

        data = generate_dashboard_data(epics)

    # Read template
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()

    # Inject data
    data_json = json.dumps(data, ensure_ascii=False)
    html = template.replace('__DASHBOARD_DATA__', data_json)

    # Write output
    output_path = os.path.join(script_dir, '../index.html')
    output_path = os.path.normpath(output_path)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"Dashboard generated: {output_path}", file=sys.stderr)


if __name__ == '__main__':
    main()
